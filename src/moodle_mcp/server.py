import functools
import logging
import time
from importlib.metadata import PackageNotFoundError, version

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_types import EmbeddedResource, ImageContent

from . import api, files, quiz
from .moodle import (
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
_ASSIGNS = "mod_assign_get_assignments"
_SUBMISSION = "mod_assign_get_submission_status"
_GRADES_OVERVIEW = "gradereport_overview_get_course_grades"
_GRADE_ITEMS = "gradereport_user_get_grade_items"
_UPCOMING = "core_calendar_get_calendar_upcoming_view"

# Web service functions each tool cannot work without. Tools whose functions
# are not enabled for the token are not registered (see apply_availability).
# Optional calls with a fallback (e.g. completion status) are not listed.
TOOL_WS: dict[str, tuple[str, ...]] = {
    "diagnose": ("core_webservice_get_site_info",),
    "get_upcoming_events": (_UPCOMING,),
    "get_my_courses": (_ENROL,),
    "get_course_content": (_CONTENTS,),
    "list_course_files": (_CONTENTS,),
    "read_course_file": (DOWNLOADFILES,),
    "list_zip_contents": (DOWNLOADFILES,),
    "view_course_file_pages": (DOWNLOADFILES,),
    "download_course_file": (DOWNLOADFILES,),
    "get_assignments": (_ASSIGNS,),
    "get_assignment_status": (_SUBMISSION,),
    "get_upcoming_deadlines": (_ASSIGNS, _SUBMISSION),
    "get_grades": (_GRADES_OVERVIEW, _GRADE_ITEMS, _ENROL),
    "search_course_materials": (_ENROL, _CONTENTS),
    "semester_dashboard": (_ENROL, _ASSIGNS, _SUBMISSION),
    "get_actionable_tasks": (_ASSIGNS, _SUBMISSION),
    "get_overdue_assignments": (_ASSIGNS, _SUBMISSION),
    "get_recent_activity": (_ENROL, "core_course_get_updates_since"),
    "get_course_announcements": (
        _ENROL,
        "mod_forum_get_forums_by_courses",
        "mod_forum_get_forum_discussions",
    ),
    "get_course_health": (_ENROL, _ASSIGNS, _SUBMISSION),
    "get_course_progress": (_ENROL,),
    "get_study_load": (_ASSIGNS,),
    "daily_briefing": (_ASSIGNS, _SUBMISSION, _UPCOMING),
    "weekly_review": (_ENROL, _ASSIGNS, _SUBMISSION),
    "ask_moodle": (_ENROL,),
    "analyze_assignment": (_ASSIGNS, _SUBMISSION),
    "extract_assignment_requirements": (_ASSIGNS,),
    "find_relevant_materials": (_ASSIGNS, _CONTENTS),
    "decompose_task": (_ASSIGNS,),
    "create_implementation_plan": (_ASSIGNS,),
    "get_quizzes": (_ENROL, "mod_quiz_get_quizzes_by_courses", "mod_quiz_get_user_attempts"),
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
    info = get_site_info(refresh=True)
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
def get_upcoming_events() -> list[api.UpcomingEvent]:
    """Get upcoming events from moodle"""
    return api.get_upcoming_events()


@tool
def get_my_courses() -> list[api.Course]:
    """Get all courses the current user is enrolled in"""
    return api.get_my_courses()


@tool
def get_course_content(courseid: int) -> list[api.CourseSection]:
    """Get sections and modules for a specific course by its ID"""
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
def download_course_file(fileurl: str) -> list[str | EmbeddedResource]:
    """Download a course file as-is and return it as an embedded binary resource (also saved to MOODLE_DOWNLOAD_DIR on the server when configured). Files over 10 MB return only metadata and how to read them. Prefer read_course_file to read the content"""
    return files.download_course_file(fileurl)


@tool
def get_assignments(courseids: list[int] | None = None) -> list[api.Assignment]:
    """Get assignments for courses. Optionally filter by course IDs. Returns all enrolled courses' assignments if no course IDs are provided."""
    return api.get_assignments(courseids)


@tool
def get_assignment_status(assignid: int) -> api.AssignmentStatus:
    """Get submission and grading status for a specific assignment by its ID"""
    return api.get_assignment_status(assignid)


@tool
def get_upcoming_deadlines() -> list[api.UpcomingDeadline]:
    """Get upcoming assignment deadlines across all courses, sorted by due date"""
    return api.get_upcoming_deadlines()


@tool
def get_grades(courseid: int | None = None) -> list[api.CourseGrade] | list[api.GradeItem]:
    """Get grade overview for all courses, or detailed grades for a specific course if courseid is provided"""
    return api.get_grades(courseid)


@tool
def search_course_materials(query: str) -> list[api.SearchResult]:
    """Search across all course materials by query string"""
    return api.search_course_materials(query)


@tool
def semester_dashboard() -> api.SemesterDashboard:
    """Get an aggregated overview combining courses, upcoming deadlines, and grades"""
    return api.semester_dashboard()


@tool
def get_actionable_tasks() -> list[api.ActionableTask]:
    """Returns prioritized list of tasks needing action, sorted by urgency (overdue first)"""
    return api.get_actionable_tasks()


@tool
def get_overdue_assignments() -> list[api.OverdueAssignment]:
    """Returns assignments past due date that are unsubmitted, sorted by most overdue first"""
    return api.get_overdue_assignments()


@tool
def get_recent_activity(since: int | None = None) -> list[api.RecentActivity]:
    """Returns recent activity/updates across courses. Optionally specify 'since' as Unix timestamp (defaults to 7 days ago)"""
    return api.get_recent_activity(since)


@tool
def get_course_announcements(courseid: int | None = None) -> list[api.CourseAnnouncement]:
    """Gets announcements from course news forums. Optionally filter by course ID"""
    return api.get_course_announcements(courseid)


@tool
def get_course_health(courseid: int) -> api.CourseHealth:
    """Overall health check for a course: progress, grades, unsubmitted/overdue counts"""
    return api.get_course_health(courseid)


@tool
def get_course_progress(courseid: int | None = None) -> list[api.CourseProgress]:
    """Progress/completion for courses. Optionally specify a course ID, or get all courses"""
    return api.get_course_progress(courseid)


@tool
def get_study_load() -> api.StudyLoad:
    """Study load analysis showing assignment distribution by week, identifying heavy weeks"""
    return api.get_study_load()


@tool
def daily_briefing() -> api.DailyBriefing:
    """Aggregated daily summary: overdue count, today's deadlines, recent grades, upcoming events, actionable tasks"""
    return api.daily_briefing()


@tool
def weekly_review() -> api.WeeklyReview:
    """Aggregated weekly summary: submitted/graded counts, upcoming deadlines, overdue count, progress"""
    return api.weekly_review()


@tool
def ask_moodle(question: str) -> api.MoodleAnswer:
    """Ask a natural language question about your Moodle data. Routes to the right data sources based on your question"""
    return api.ask_moodle(question)


@tool
def analyze_assignment(assignid: int) -> api.AssignmentAnalysis:
    """Comprehensive analysis of an assignment: status, requirements, materials count, course progress, and deadline info"""
    return api.analyze_assignment(assignid)


@tool
def extract_assignment_requirements(assignid: int) -> api.AssignmentRequirements:
    """Extract and structure requirements, deliverables, constraints, and evaluation criteria from an assignment description"""
    return api.extract_assignment_requirements(assignid)


@tool
def find_relevant_materials(assignid: int) -> api.RelevantMaterials:
    """Find course content and search results relevant to a specific assignment, ranked by relevance"""
    return api.find_relevant_materials(assignid)


@tool
def decompose_task(assignid: int) -> api.TaskDecomposition:
    """Break down an assignment into subtasks with estimated effort, dependencies, and critical path"""
    return api.decompose_task(assignid)


@tool
def create_implementation_plan(assignid: int) -> api.ImplementationPlan:
    """Create a step-by-step implementation plan for completing an assignment, with timeline, resources, milestones, and risk factors"""
    return api.create_implementation_plan(assignid)


@tool
def get_quizzes(courseids: list[int] | None = None) -> list[quiz.Quiz]:
    """List quizzes of the enrolled courses (or of courseids): opening and closing time, time limit, attempts allowed (0 = unlimited) and how many the user has finished. Read-only: this server never starts or submits an attempt"""
    return quiz.get_quizzes(courseids)


@tool
def get_quiz_review(quizid: int, attemptid: int | None = None) -> quiz.QuizReview:
    """Review of a finished quiz attempt: each question with the given answer, state, marks and feedback, as far as the quiz settings allow the student to see them. Defaults to the latest finished attempt; attempts still in progress are never opened"""
    return quiz.get_quiz_review(quizid, attemptid)


def main():
    from .__main__ import main as cli_main

    return cli_main()