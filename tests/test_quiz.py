import json

import pytest

from moodle_mcp import moodle, quiz
from moodle_mcp.moodle import DENIED_FUNCTION, MoodleAPIError, post_with_retry

QUESTION_HTML = """
<div id="question-1-1" class="que multichoice deferredfeedback incorrect">
 <div class="info"><h3 class="no">Domanda <span class="qno">1</span></h3>
  <div class="questionflag editable"><input type="checkbox" name="flag" value="1" />
  <span>Contrassegna domanda</span></div></div>
 <div class="content"><div class="formulation clearfix">
  <h4 class="accesshide">Testo della domanda</h4>
  <input type="hidden" name="q1:1_:sequencecheck" value="3" />
  <div class="qtext"><p>Il rango di \\( A \\) &egrave;:</p></div>
  <div class="answer">
   <div class="r0"><input type="radio" name="q1:1_answer" value="0" /><label>a. righe non nulle</label></div>
   <div class="r1"><input type="radio" name="q1:1_answer" value="1" checked="checked" /><label>b. colonne</label>
    <i class="icon" title="Risposta errata"></i></div>
  </div>
  <label>Risposta: <span class="sr-only">Domanda 1</span></label>
  <input type="text" name="q1:2_answer" value="7680" readonly="readonly" />
  <select name="q1:3_sub0"><option value="0">Scegli...</option><option value="2" selected="selected">Vero</option></select>
 </div>
 <div class="outcome"><div class="feedback"><div class="rightanswer">La risposta corretta &egrave;: a</div></div></div>
 </div>
 <script>var require = {baseUrl: "x"};</script>
</div>
"""


@pytest.mark.parametrize(
    "function",
    [
        "mod_quiz_start_attempt",
        "mod_quiz_process_attempt",
        "mod_quiz_save_attempt",
        "mod_assign_save_submission",
        "mod_assign_submit_for_grading",
        "mod_assign_start_submission",
        "start_anything",
    ],
)
def test_denylist_blocks_write_functions(fake_moodle, function):
    assert DENIED_FUNCTION.search(function)
    with pytest.raises(MoodleAPIError) as excinfo:
        post_with_retry(moodle.MOODLE_URL, {"wsfunction": function}, function)
    assert json.loads(str(excinfo.value))["kind"] == "blocked"
    assert fake_moodle.calls == []


@pytest.mark.parametrize(
    "function",
    [
        "mod_assign_get_submission_status",
        "mod_quiz_get_user_attempts",
        "mod_quiz_get_attempt_review",
        "mod_quiz_get_quizzes_by_courses",
        "core_course_get_updates_since",
        "core_webservice_get_site_info",
    ],
)
def test_denylist_allows_read_functions(function):
    assert not DENIED_FUNCTION.search(function)


def attempts_payload(*states):
    return {
        "attempts": [
            {"id": 100 + i, "attempt": i + 1, "state": s, "timestart": 1700000000 + i, "timefinish": 1700000100 + i * 10, "sumgrades": "3.0"}
            for i, s in enumerate(states)
        ]
    }


def test_review_never_opens_unfinished_attempts(fake_moodle):
    fake_moodle.responses["mod_quiz_get_user_attempts"] = attempts_payload("inprogress", "overdue")
    with pytest.raises(MoodleAPIError):
        quiz.get_quiz_review(7)
    assert fake_moodle.count("mod_quiz_get_attempt_review") == 0


def test_review_refuses_an_open_attempt_id(fake_moodle):
    fake_moodle.responses["mod_quiz_get_user_attempts"] = attempts_payload("finished", "inprogress")
    with pytest.raises(MoodleAPIError):
        quiz.get_quiz_review(7, attemptid=101)
    assert fake_moodle.count("mod_quiz_get_attempt_review") == 0


def test_review_defaults_to_latest_finished_attempt(fake_moodle):
    fake_moodle.responses["mod_quiz_get_user_attempts"] = attempts_payload("finished", "finished", "inprogress")
    fake_moodle.responses["mod_quiz_get_attempt_review"] = {
        "grade": "8.00",
        "additionaldata": [{"id": "feedback", "title": "Feedback", "content": "<p>Bene</p>"}],
        "questions": [
            {"slot": 1, "number": 1, "type": "multichoice", "state": "gradedwrong", "status": "Risposta errata", "mark": "0", "maxmark": "1.0000000", "html": QUESTION_HTML}
        ],
    }
    review = quiz.get_quiz_review(7)

    review_call = next(c for c in fake_moodle.calls if c["key"] == "mod_quiz_get_attempt_review")
    assert review_call["data"]["attemptid"] == "101"
    assert review["reviewed_attempt_id"] == 101
    assert [a["id"] for a in review["attempts"]] == [100, 101]
    assert review["feedback"] == ["Bene"]
    assert review["questions"][0]["maxmark"] == 1.0


def test_question_text_keeps_answers_and_drops_noise():
    text = quiz.question_text(QUESTION_HTML)
    assert "Il rango di \\( A \\) è:" in text
    assert "[ ] a. righe non nulle" in text.replace("\n", " ")
    assert "[x] b. colonne" in text.replace("\n", " ")
    assert "(Risposta errata)" in text
    assert "[7680]" in text
    assert "[Vero]" in text and "Scegli..." not in text
    assert "La risposta corretta è: a" in text
    for noise in ("Contrassegna", "Testo della domanda", "Domanda 1", "baseUrl", "sequencecheck"):
        assert noise not in text


def test_get_quizzes(fake_moodle, site_info, monkeypatch):
    monkeypatch.setattr(quiz.time, "time", lambda: 1_750_000_000)
    fake_moodle.responses["core_webservice_get_site_info"] = site_info
    fake_moodle.responses["core_enrol_get_users_courses"] = [{"id": 5, "fullname": "Analisi 1", "shortname": "AN1"}]
    fake_moodle.responses["mod_quiz_get_quizzes_by_courses"] = {
        "quizzes": [
            {"id": 7, "coursemodule": 70, "course": 5, "name": "Quiz 1", "intro": "<p>Limiti</p>", "timeopen": 1_749_000_000, "timeclose": 1_751_000_000, "timelimit": 1800, "attempts": 1}
        ]
    }
    fake_moodle.responses["mod_quiz_get_user_attempts"] = attempts_payload("finished")
    monkeypatch.setattr(quiz, "get_my_courses", lambda: [{"id": 5, "fullname": "Analisi 1"}])

    [q] = quiz.get_quizzes()
    assert q["open_now"] is True
    assert q["timelimit_minutes"] == 30
    assert q["attempts_allowed"] == 1
    assert q["attempts_finished"] == 1
    assert q["last_finished_attempt_id"] == 100
    assert q["intro"] == "Limiti"
    assert q["course_name"] == "Analisi 1"
