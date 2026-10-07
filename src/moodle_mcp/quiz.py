"""Read-only access to quizzes and to the review of finished attempts.

Nothing here starts, saves or submits an attempt: quizzes often allow a single
attempt, and opening one by mistake uses it up. The web service client also
refuses such functions (moodle.DENIED_FUNCTION).
"""

import re
import time
from html.parser import HTMLParser

from typing_extensions import TypedDict

from .api import get_my_courses
from .files import html_to_text
from .logger import logger
from .moodle import APIFunction, MoodleAPIError, format_moodle_array_params, get_moodle_api_data
from .utils import local_time

INTRO_MAX_CHARS = 1500


class Quiz(TypedDict):
    id: int
    coursemodule: int
    courseid: int
    course_name: str | None
    name: str
    intro: str
    timeopen: int
    timeopen_local: str | None
    timeclose: int
    timeclose_local: str | None
    open_now: bool
    timelimit_minutes: float | None
    attempts_allowed: int | None
    attempts_finished: int
    attempts_left: int | None
    attempt_in_progress: bool
    last_finished_attempt_id: int | None


class QuizQuestion(TypedDict):
    number: str | None
    type: str
    state: str | None
    status: str | None
    mark: str | None
    maxmark: float | None
    text: str


class QuizAttempt(TypedDict):
    id: int
    attempt: int
    timestart_local: str | None
    timefinish_local: str | None
    sumgrades: float | None


class QuizReview(TypedDict):
    quizid: int
    attempts: list[QuizAttempt]
    reviewed_attempt_id: int
    grade: str | None
    feedback: list[str]
    questions: list[QuizQuestion]


def _finished_attempts(quizid: int) -> tuple[list[dict], bool]:
    """Return (finished attempts, whether an attempt is still open)."""
    data = get_moodle_api_data(
        APIFunction.mod_quiz_get_user_attempts,
        params={"quizid": str(quizid), "status": "all", "includepreviews": "0"},
    )
    attempts = data.get("attempts") or []
    # Filter here too: an attempt that is still open must never be reviewed.
    finished = [a for a in attempts if a.get("state") == "finished"]
    in_progress = any(a.get("state") in ("inprogress", "overdue") for a in attempts)
    return sorted(finished, key=lambda a: a.get("timefinish") or 0), in_progress


def get_quizzes(courseids: list[int] | None = None) -> list[Quiz]:
    courses = get_my_courses()
    names = {c["id"]: c["fullname"] for c in courses}
    ids = courseids or list(names)
    data = get_moodle_api_data(
        APIFunction.mod_quiz_get_quizzes_by_courses,
        params=format_moodle_array_params("courseids", ids),
    )

    now = int(time.time())
    result: list[Quiz] = []
    for quiz in data.get("quizzes") or []:
        try:
            finished, in_progress = _finished_attempts(quiz["id"])
        except MoodleAPIError as e:
            logger.warning(f"Cannot read attempts of quiz {quiz['id']}: {e.message}")
            finished, in_progress = [], False

        timeopen, timeclose = quiz.get("timeopen") or 0, quiz.get("timeclose") or 0
        intro = html_to_text(quiz.get("intro") or "")
        timelimit = quiz.get("timelimit")
        allowed = quiz.get("attempts")
        result.append(
            {
                "id": quiz["id"],
                "coursemodule": quiz.get("coursemodule"),
                "courseid": quiz.get("course"),
                "course_name": names.get(quiz.get("course")),
                "name": quiz.get("name", ""),
                "intro": intro[:INTRO_MAX_CHARS],
                "timeopen": timeopen,
                "timeopen_local": local_time(timeopen),
                "timeclose": timeclose,
                "timeclose_local": local_time(timeclose),
                "open_now": (not timeopen or timeopen <= now) and (not timeclose or now < timeclose),
                "timelimit_minutes": round(timelimit / 60, 1) if timelimit else None,
                # 0 means unlimited; None when Moodle does not tell the student.
                "attempts_allowed": allowed,
                "attempts_finished": len(finished),
                # None when unlimited or unknown.
                "attempts_left": max(allowed - len(finished), 0) if allowed else None,
                "attempt_in_progress": in_progress,
                "last_finished_attempt_id": finished[-1]["id"] if finished else None,
            }
        )

    result.sort(key=lambda q: (q["courseid"] or 0, q["timeopen"] or 0, q["id"]))
    logger.info(f"Found {len(result)} quizzes")
    return result


# ---------------------------------------------------------------------------
# Attempt review
# ---------------------------------------------------------------------------


class _QuestionText(HTMLParser):
    """Text of a question as shown in the review page, answers included.

    The generic HTML to text conversion drops form fields, which is where
    the student's answers are: text inputs keep their value, radio buttons
    and checkboxes become [x] / [ ], and icon titles ("Risposta corretta")
    are kept.
    """

    BLOCK = {"p", "div", "br", "li", "tr", "h3", "h4", "table", "label"}
    VOID = {"input", "img", "br", "hr", "meta", "link", "source"}
    SKIP_CLASSES = {"info", "accesshide", "sr-only", "questionflag", "im-controls"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_tag: str | None = None
        self._skip_depth = 0
        self._option_selected: bool | None = None

    def handle_starttag(self, tag, attrs):
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        if tag in ("script", "style") or classes & self.SKIP_CLASSES:
            if tag not in self.VOID:
                self._skip_tag, self._skip_depth = tag, 1
            return

        if tag == "input":
            kind = (a.get("type") or "text").lower()
            if kind in ("radio", "checkbox"):
                self.parts.append("[x] " if "checked" in a else "[ ] ")
            elif kind not in ("hidden", "submit", "button"):
                self.parts.append(f"[{a.get('value') or ''}]")
        elif tag == "option":
            self._option_selected = "selected" in a
        elif tag == "select":
            self.parts.append(" ")
        elif tag == "i" and a.get("title"):
            self.parts.append(f" ({a['title']})")
        elif tag == "img" and a.get("alt"):
            self.parts.append(f"[image: {a['alt']}]")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if not self._skip_depth:
                    self._skip_tag = None
            return
        if tag == "option":
            self._option_selected = None
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_tag or self._option_selected is False:
            return
        if self._option_selected:
            data = f"[{data.strip()}]"
        self.parts.append(data)


def question_text(html: str) -> str:
    parser = _QuestionText()
    parser.feed(html)
    text = "".join(parser.parts).replace("\r", "")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_quiz_review(quizid: int, attemptid: int | None = None) -> QuizReview:
    finished, _ = _finished_attempts(quizid)
    if not finished:
        raise MoodleAPIError(
            "not_found",
            f"Quiz {quizid} has no finished attempt to review. Attempts that are"
            " still open are never opened by this server.",
            "mod_quiz_get_user_attempts",
        )
    if attemptid is None:
        attempt = finished[-1]
    else:
        attempt = next((a for a in finished if a["id"] == attemptid), None)
        if attempt is None:
            raise MoodleAPIError(
                "not_found",
                f"Attempt {attemptid} is not a finished attempt of quiz {quizid};"
                f" finished attempts: {[a['id'] for a in finished]}",
                "mod_quiz_get_user_attempts",
            )

    data = get_moodle_api_data(
        APIFunction.mod_quiz_get_attempt_review,
        params={"attemptid": str(attempt["id"]), "page": "-1"},
    )

    questions: list[QuizQuestion] = []
    for q in data.get("questions") or []:
        questions.append(
            {
                "number": q.get("questionnumber") or q.get("number"),
                "type": q.get("type", ""),
                "state": q.get("state"),
                "status": q.get("status"),
                "mark": q.get("mark"),
                "maxmark": _as_float(q.get("maxmark")),
                "text": question_text(q.get("html") or ""),
            }
        )

    return {
        "quizid": quizid,
        "attempts": [
            {
                "id": a["id"],
                "attempt": a.get("attempt"),
                "timestart_local": local_time(a.get("timestart")),
                "timefinish_local": local_time(a.get("timefinish")),
                "sumgrades": _as_float(a.get("sumgrades")),
            }
            for a in finished
        ],
        "reviewed_attempt_id": attempt["id"],
        "grade": None if data.get("grade") is None else str(data.get("grade")),
        "feedback": [
            html_to_text(d.get("content") or "")
            for d in data.get("additionaldata") or []
            if d.get("content")
        ],
        "questions": questions,
    }
