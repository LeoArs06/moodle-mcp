import functools
import json
import logging
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_types import ImageContent

from . import api, files, quiz
from .moodle import (
    MOODLE_URL,
    ErrorKind,
    MoodleAPIError,
    current_tool,
    get_site_info,
    redact_result,
)

try:
    __version__ = version("moodle-mcp")
except PackageNotFoundError:
    __version__ = "0.0.0"

logger = logging.getLogger("moodle-mcp.protocol")

# Pseudo-requirement: the token's service must allow file downloads.
DOWNLOADFILES = "downloadfiles"

_ENROL = "core_enrol_get_users_courses"
_CONTENTS = "core_course_get_contents"

# Web service functions each tool cannot work without. Tools whose functions
# are not enabled for the token are not registered (see apply_availability).
# Calls a tool can do without are not listed.
TOOL_WS: dict[str, tuple[str, ...]] = {
    "diagnose": ("core_webservice_get_site_info",),
    "get_my_courses": (_ENROL,),
    "get_course_content": (_CONTENTS,),
    "list_course_files": (_CONTENTS,),
    "read_course_file": (DOWNLOADFILES,),
    "list_zip_contents": (DOWNLOADFILES,),
    "view_course_file_pages": (DOWNLOADFILES,),
    "get_upcoming_events": ("core_calendar_get_calendar_upcoming_view",),
    "get_assignments": ("mod_assign_get_assignments", "mod_assign_get_submission_status"),
    "get_grades": ("gradereport_overview_get_course_grades", "gradereport_user_get_grade_items", _ENROL),
    "search_course_materials": (_ENROL, _CONTENTS),
    "get_recent_activity": (_ENROL, "core_course_get_updates_since", _CONTENTS),
    "get_course_announcements": (
        _ENROL,
        "mod_forum_get_forums_by_courses",
        "mod_forum_get_forum_discussions",
    ),
    "get_quizzes": (_ENROL, "mod_quiz_get_quizzes_by_courses"),
    "get_quiz_review": ("mod_quiz_get_user_attempts", "mod_quiz_get_attempt_review"),
}

# Tools removed at startup, with the requirements they were missing.
unavailable_tools: dict[str, list[str]] = {}
startup_probe_error: dict | None = None


async def log_requests(ctx, call_next):
    """Log every inbound MCP message, so a dropped client session can be traced."""
    kind = "notification" if ctx.request_id is None else f"request id={ctx.request_id}"
    logger.info("<- %s (%s)", ctx.method, kind)
    try:
        result = await call_next(ctx)
    except Exception as e:
        logger.warning("-> %s failed: %s", ctx.method, e)
        raise
    if ctx.request_id is not None:
        logger.info("-> %s ok", ctx.method)
    return result


mcp = MCPServer(
    "moodle-mcp",
    version=__version__,
    dependencies=["glom", "requests"],
    middleware=[log_requests],
)


def tool(fn):
    """Register fn as an MCP tool that reports failures as structured errors.

    Moodle errors keep their JSON message; any other exception becomes an
    `internal` error instead of a bare "Error executing tool". Results are
    redacted so the token never reaches the model.
    """
    name = fn.__name__

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        reset = current_tool.set(name)
        try:
            return redact_result(fn(*args, **kwargs))
        except ToolError:
            raise
        except Exception as e:
            logger.exception("Tool %s crashed", name)
            # First line only: some libraries put whole tracebacks in the message.
            detail = str(e).strip().splitlines()[0][:300] if str(e).strip() else ""
            raise MoodleAPIError(
                "internal", f"{type(e).__name__}: {detail}", None, kind=ErrorKind.INTERNAL
            ) from None
        finally:
            current_tool.reset(reset)

    mcp.tool(structured_output=False)(wrapper)
    return fn


def missing_requirements(name: str, info: dict) -> list[str]:
    enabled = {f["name"] for f in info.get("functions") or []}
    missing = []
    for requirement in TOOL_WS.get(name, ()):
        if requirement == DOWNLOADFILES:
            if not info.get("downloadfiles"):
                missing.append("downloadfiles (the token's service does not allow file downloads)")
        elif requirement not in enabled:
            missing.append(requirement)
    return missing


def apply_availability(timeout: float = 10) -> None:
    """Unregister the tools whose web service functions are not enabled.

    Runs once at startup, before the client connects, so it uses a short
    timeout and no retries. If Moodle cannot be reached, every tool stays
    registered and `diagnose` reports the failure.
    """
    global startup_probe_error
    try:
        info = get_site_info(timeout=timeout, retries=0)
    except MoodleAPIError as e:
        startup_probe_error = e.to_dict()
        logger.warning("Startup check failed, keeping all tools: %s", e.message)
        return

    for name in TOOL_WS:
        if name == "diagnose":
            continue
        missing = missing_requirements(name, info)
        if missing:
            mcp.remove_tool(name)
            unavailable_tools[name] = missing
            logger.warning("Tool %s disabled, missing: %s", name, ", ".join(missing))


@tool
def diagnose(include_all_functions: bool = False) -> dict:
    """Check the Moodle connection and what this token allows: user, Moodle version, the web service functions each tool needs and whether they are enabled, and the tools disabled at startup. Call this when a tool fails and the reason is unclear. include_all_functions lists every enabled function (long)"""
    started = time.monotonic()
    try:
        # A diagnosis should come back quickly, so no retries here.
        info = get_site_info(refresh=True, timeout=15, retries=0)
    except MoodleAPIError as e:
        result = {
            "connection": {"ok": False, "error": e.to_dict()},
            "moodle_url": MOODLE_URL,
            "tools_disabled_at_startup": unavailable_tools,
        }
        if startup_probe_error:
            result["startup_probe_error"] = startup_probe_error
        return result
    elapsed_ms = round((time.monotonic() - started) * 1000)
    enabled = {f["name"] for f in info.get("functions") or []}
    needed = sorted({f for reqs in TOOL_WS.values() for f in reqs if f != DOWNLOADFILES})

    result = {
        "connection": {"ok": True, "site_info_ms": elapsed_ms},
        "user": {
            "id": info.get("userid"),
            "username": info.get("username"),
            "fullname": info.get("fullname"),
        },
        "site": {
            "name": info.get("sitename"),
            "url": info.get("siteurl"),
            "release": info.get("release"),
            "version": info.get("version"),
            "lang": info.get("lang"),
        },
        "token": {
            # site_info does not name the service; its function list is what matters.
            "functions_enabled": len(enabled),
            "downloadfiles": bool(info.get("downloadfiles")),
        },
        "ws_functions_used_by_tools": {f: f in enabled for f in needed},
        "tools_unavailable_now": {
            name: missing for name in TOOL_WS if (missing := missing_requirements(name, info))
        },
        "tools_disabled_at_startup": unavailable_tools,
    }
    if startup_probe_error:
        result["startup_probe_error"] = startup_probe_error
    if include_all_functions:
        result["functions"] = sorted(enabled)
    return result


@tool
def get_my_courses() -> list[api.Course]:
    """Courses the user is enrolled in: id, fullname, progress. Name courses by fullname; shortname is often an internal code"""
    return api.get_my_courses()


@tool
def get_course_content(courseid: int) -> list[api.CourseSection]:
    """Sections and activities of a course, with the files of each activity. Long for large courses: to find files, list_course_files is shorter"""
    return api.get_course_content(courseid)


@tool
def list_course_files(
    courseid: int, query: str | None = None, mimetype: str | None = None
) -> list[files.CourseFile]:
    """List files attached to a course (resources, folders, pages) with their fileurl, size and type. Large courses have hundreds of files: filter with query (words matched in filename, module or section name) and/or mimetype (prefix, e.g. 'application/pdf'). Entries with external=true are links to other sites and cannot be downloaded"""
    return files.list_course_files(courseid, query, mimetype)


@tool
def read_course_file(
    fileurl: str,
    pages: str | None = None,
    max_chars: int = files.DEFAULT_MAX_CHARS,
    inner_path: str | None = None,
) -> files.FileText:
    """Download a course file and return its text (PDF, HTML pages, plain text). Use the fileurl from list_course_files or get_course_content. For long PDFs pass pages like '1-10' and continue with next_pages. If has_text_layer is false (scans, handwriting) no text is returned: use view_course_file_pages with the suggested pages. For a file inside a zip archive, pass the zip's fileurl and inner_path from list_zip_contents"""
    return files.read_course_file(fileurl, pages, max_chars, inner_path)


@tool
def list_zip_contents(fileurl: str) -> list[files.ZipEntry]:
    """List the files inside a zip archive from a course, without extracting them. Read one with read_course_file or view_course_file_pages by passing its path as inner_path"""
    return files.list_zip_contents(fileurl)


@tool
def view_course_file_pages(
    fileurl: str,
    pages: str = "1-3",
    inner_path: str | None = None,
    dpi: int = files.DEFAULT_DPI,
    grayscale: bool = True,
) -> list[str | ImageContent]:
    """Render PDF pages as images (up to 8 per call). Use this when read_course_file finds no text layer (scans, handwritten notes) or when layout, formulas or diagrams matter. Raise dpi (max 200) for small handwriting, set grayscale=false when colours matter. For a PDF inside a zip archive, pass inner_path from list_zip_contents"""
    return files.view_course_file_pages(fileurl, pages, inner_path, dpi, grayscale)


@tool
def get_upcoming_events() -> list[api.UpcomingEvent]:
    """Upcoming calendar events of all courses (deadlines, quiz closing times, bookings, ...) with local start and end time. instance is the id of the activity, e.g. a quiz id for get_quiz_review"""
    return api.get_upcoming_events()


@tool
def get_assignments(
    courseids: list[int] | None = None,
    only: Literal["upcoming", "overdue", "all"] = "upcoming",
) -> list[api.Assignment]:
    """Assignments with local due date, days_until_due, submission and grading status, and description. only: 'upcoming' (default), 'overdue' (past due and not submitted) or 'all' (slower: one status request per assignment). Optionally limited to courseids"""
    return api.get_assignments(courseids, only)


@tool
def get_grades(courseid: int | None = None) -> list[api.CourseGrade] | list[api.GradeItem]:
    """Grades: one line per course, or the grade items with feedback of one course when courseid is given"""
    return api.get_grades(courseid)


@tool
def search_course_materials(query: str) -> list[api.SearchResult]:
    """Find activities and files whose name, file name or section name contains query, across all enrolled courses. Matches names only, not the text inside files"""
    return api.search_course_materials(query)


@tool
def get_recent_activity(days: int = 7, courseid: int | None = None) -> list[api.RecentActivity]:
    """Activities created or changed in the last days (default 7): course, section, activity name, kind of change (configuration, contentfiles, discussions, ...) and when. Optionally a single course"""
    return api.get_recent_activity(days, courseid)


@tool
def get_course_announcements(
    courseid: int | None = None, days: int | None = None
) -> list[api.CourseAnnouncement]:
    """Posts in the announcement (news) forums of the courses, newest first, as text. Optionally a single course and/or only the last days"""
    return api.get_course_announcements(courseid, days)


@tool
def get_quizzes(courseids: list[int] | None = None) -> list[quiz.Quiz]:
    """List quizzes of the enrolled courses (or of courseids): opening and closing time, time limit, attempts allowed (0 = unlimited), finished and left. Read-only: this server never starts or submits an attempt"""
    return quiz.get_quizzes(courseids)


@tool
def get_quiz_review(quizid: int, attemptid: int | None = None) -> quiz.QuizReview:
    """Review of a finished quiz attempt: each question with the given answer, state, marks and feedback, as far as the quiz settings allow the student to see them. Defaults to the latest finished attempt; attempts still in progress are never opened"""
    return quiz.get_quiz_review(quizid, attemptid)


# ---------------------------------------------------------------------------
# Prompts: picked by the user from the client's menu. They only say which
# tools to use and how to present the result; facts come from the tools.
# ---------------------------------------------------------------------------

_NO_GUESSING = (
    "Se uno strumento fallisce o non restituisce dati, dillo esplicitamente"
    " invece di dedurre o riempire i vuoti."
)


@mcp.prompt(name="briefing", description="Scadenze dei prossimi 7 giorni, annunci recenti e quiz aperti, per tutti i corsi")
def briefing_prompt() -> str:
    return (
        "Preparami il briefing di oggi da Moodle.\n"
        "- Scadenze dei prossimi 7 giorni: get_upcoming_events e get_assignments.\n"
        "- Annunci degli ultimi 7 giorni: get_course_announcements con days=7.\n"
        "- Quiz che posso ancora fare: get_quizzes, quelli con open_now vero e attempts_left diverso da 0.\n"
        "Rispondi con tre sezioni brevi (Scadenze, Annunci, Quiz aperti), in ordine di data,"
        " indicando il corso con il nome completo e data e ora locali.\n" + _NO_GUESSING
    )


@mcp.prompt(name="prepara-lezione", description="Trova il materiale di una lezione e prepara un percorso di lettura")
def prepare_lesson_prompt(corso: str | None = None, argomento: str | None = None) -> str:
    target = "Aiutami a preparare una lezione"
    if corso:
        target += f" del corso «{corso}»"
    if argomento:
        target += f" sull'argomento «{argomento}»"
    return (
        f"{target}.\n"
        "- Individua il corso con get_my_courses; se non è chiaro quale, chiedimelo.\n"
        "- Trova il materiale con list_course_files (filtra con query) o get_course_content.\n"
        "- Leggi i file con read_course_file; per le pagine senza testo usa view_course_file_pages.\n"
        "Restituisci prima l'elenco dei file trovati (nome, sezione, pagine), poi un percorso di lettura"
        " con le pagine da leggere e cosa contengono. Riassumi solo ciò che hai letto davvero.\n" + _NO_GUESSING
    )


@mcp.prompt(name="revisione-quiz", description="Rivede un quiz chiuso ed elenca gli errori da ripassare")
def quiz_review_prompt(corso: str | None = None, quiz: str | None = None) -> str:
    target = "Rivediamo un quiz che ho già completato"
    if quiz:
        target += f" («{quiz}»)"
    if corso:
        target += f" del corso «{corso}»"
    return (
        f"{target}.\n"
        "- Con get_quizzes trova i quiz con almeno un tentativo completato; se non ho indicato quale"
        " e ce n'è più di uno, prendi quello con il tentativo più recente e dimmi quale hai scelto.\n"
        "- Usa get_quiz_review. Per ogni domanda sbagliata o con punteggio parziale riporta: la domanda"
        " in breve, la mia risposta, la risposta corretta se Moodle la mostra, il concetto da ripassare.\n"
        "- Chiudi con i 2-3 argomenti da ripassare per primi.\n"
        "Non avviare mai un nuovo tentativo. Se la revisione non è consentita dalle impostazioni del quiz,"
        " dillo.\n" + _NO_GUESSING
    )


@mcp.prompt(name="settimana", description="Cosa è uscito questa settimana su ogni corso")
def week_prompt() -> str:
    return (
        "Fammi il riepilogo della settimana su Moodle (ultimi 7 giorni).\n"
        "- Nuovi materiali e attività modificate: get_recent_activity con days=7.\n"
        "- Annunci: get_course_announcements con days=7.\n"
        "- In arrivo nei prossimi 7 giorni: get_upcoming_events, get_assignments e get_quizzes.\n"
        "Una sezione per corso (nome completo) con elenchi brevi; i corsi senza novità in una riga finale.\n"
        + _NO_GUESSING
    )


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource(
    "moodle://courses",
    name="courses",
    description="Enrolled courses: id, fullname, shortname",
    mime_type="application/json",
)
def courses_resource() -> str:
    courses = [
        {"id": c["id"], "fullname": c["fullname"], "shortname": c["shortname"]}
        for c in api.get_my_courses()
    ]
    return json.dumps(courses, ensure_ascii=False, indent=1)


def main():
    from .__main__ import main as cli_main

    return cli_main()