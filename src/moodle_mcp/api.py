"""Data tools: courses, calendar, assignments, grades, forums and course updates.

Every function returns what Moodle reports, cleaned up (HTML to text, local
times). Summaries and plans are left to the model and to the MCP prompts.
"""

import time
from typing import Literal

from glom import Coalesce, glom
from typing_extensions import TypedDict

from .files import html_to_text
from .logger import logger
from .moodle import (
    APIFunction,
    MoodleAPIError,
    format_moodle_array_params,
    get_moodle_api_data,
    get_site_info,
)
from .utils import local_time, to_json_file

INTRO_MAX_CHARS = 1500
MESSAGE_MAX_CHARS = 2000


# ---------------------------------------------------------------------------
# TypedDict definitions
# ---------------------------------------------------------------------------


class UpcomingEvent(TypedDict):
    id: int
    name: str
    course_name: str | None
    modulename: str | None
    instance: int | None
    eventtype: str | None
    start_local: str | None
    end_local: str | None
    overdue: bool
    url: str | None
    description: str


class Course(TypedDict):
    id: int
    fullname: str
    shortname: str
    category: int
    progress: float | None
    format: str
    startdate: int
    enddate: int


class CourseModule(TypedDict):
    id: int
    name: str
    modname: str
    url: str | None
    contents: list[dict] | None


class CourseSection(TypedDict):
    name: str
    section: int
    visible: int
    uservisible: bool
    modules: list[CourseModule]


class Assignment(TypedDict):
    id: int
    name: str
    courseid: int
    course_name: str
    duedate_local: str | None
    cutoffdate_local: str | None
    days_until_due: float | None
    submission_status: str | None
    submitted: bool | None
    graded: bool | None
    intro: str


class CourseGrade(TypedDict):
    courseid: int
    course_name: str
    grade: str
    rank: str | None


class GradeItem(TypedDict):
    itemname: str
    grade: str | None
    percentage: str | None
    feedback: str | None


class SearchResult(TypedDict):
    title: str
    url: str
    content: str
    course_name: str | None


class RecentActivity(TypedDict):
    course_name: str
    section: str | None
    module_id: int
    module_name: str | None
    modname: str | None
    changes: list[str]
    updated_local: str | None
    url: str | None


class CourseAnnouncement(TypedDict):
    id: int
    discussion_id: int | None
    subject: str
    message: str
    course_name: str
    author: str
    date_local: str | None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_user_id() -> int:
    """The current Moodle user ID (site info is cached)."""
    return glom(get_site_info(), "userid")


def _days_ago(days: int) -> int:
    return int(time.time()) - days * 86400


# ---------------------------------------------------------------------------
# Courses
# ---------------------------------------------------------------------------


def get_my_courses() -> list[Course]:
    user_id = _get_user_id()
    data = get_moodle_api_data(
        APIFunction.core_enrol_get_users_courses,
        params={"userid": str(user_id)},
    )

    to_json_file(data, "user_courses.json")

    spec = [
        {
            "id": "id",
            "fullname": "fullname",
            "shortname": "shortname",
            "category": Coalesce("category", default=0),
            "progress": Coalesce("progress", default=None),
            "format": Coalesce("format", default=""),
            "startdate": Coalesce("startdate", default=0),
            "enddate": Coalesce("enddate", default=0),
        }
    ]

    courses = glom(data, spec)
    logger.info(f"Extracted {len(courses)} courses")
    return courses


def _course_names() -> dict[int, str]:
    return {c["id"]: c["fullname"] for c in get_my_courses()}


def get_course_content(courseid: int, refresh: bool = False) -> list[CourseSection]:
    data = get_moodle_api_data(
        APIFunction.core_course_get_contents,
        params={"courseid": str(courseid)},
        use_original_data=False,
        refresh=refresh,
    )

    to_json_file(data, f"course_content_{courseid}.json")

    module_spec = {
        "id": "id",
        "name": "name",
        "modname": "modname",
        "url": Coalesce("url", default=None),
        "contents": Coalesce("contents", default=None),
    }

    section_spec = {
        "name": "name",
        "section": "section",
        "visible": Coalesce("visible", default=1),
        "uservisible": Coalesce("uservisible", default=True),
        "modules": (Coalesce("modules", default=[]), [module_spec]),
    }

    sections = glom(data, [section_spec])
    logger.info(f"Extracted {len(sections)} sections for course {courseid}")
    return sections


def search_course_materials(query: str) -> list[SearchResult]:
    """Search the user's course materials for a query string.

    Moodle global search (core_search_*) is an optional subsystem that is
    disabled by default and needs a configured search engine, so it cannot be
    relied on. Instead we search client-side over the contents of the enrolled
    courses, matching section names, activity names and file names. This works
    on any Moodle instance.
    """
    needle = query.lower().strip()
    results: list[SearchResult] = []

    for course in get_my_courses():
        try:
            sections = get_course_content(course["id"])
        except MoodleAPIError as e:
            logger.warning(f"Skipping course {course['id']} in search: {e.message}")
            continue

        for section in sections:
            section_name = section.get("name") or ""
            for module in section.get("modules", []):
                name = module.get("name") or ""
                filenames = [
                    c.get("filename", "")
                    for c in (module.get("contents") or [])
                    if isinstance(c, dict)
                ]
                haystack = " ".join([name, section_name, *filenames]).lower()
                if needle and needle in haystack:
                    results.append(
                        {
                            "title": name,
                            "url": module.get("url") or "",
                            "content": section_name,
                            "course_name": course.get("fullname"),
                        }
                    )

    logger.info(f"Search for '{query}' matched {len(results)} materials")
    return results


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def get_upcoming_events() -> list[UpcomingEvent]:
    data = get_moodle_api_data(APIFunction.core_calendar_get_calendar_upcoming_view)

    to_json_file(data, "calendar_upcoming_view.json")

    events: list[UpcomingEvent] = []
    for e in data.get("events") or []:
        start = e.get("timestart") or 0
        duration = e.get("timeduration") or 0
        events.append(
            {
                "id": e.get("id"),
                "name": e.get("name") or "",
                "course_name": (e.get("course") or {}).get("fullname"),
                "modulename": e.get("modulename"),
                # Id of the activity (quiz id, assignment id, ...) for other tools.
                "instance": e.get("instance"),
                "eventtype": e.get("eventtype"),
                "start_local": local_time(start),
                "end_local": local_time(start + duration) if duration else None,
                "overdue": bool(e.get("overdue")),
                "url": e.get("url"),
                "description": html_to_text(e.get("description") or "")[:INTRO_MAX_CHARS],
            }
        )

    logger.info(f"Extracted {len(events)} upcoming events")
    return events


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------


def _submission_status(assignid: int) -> dict:
    data = get_moodle_api_data(
        APIFunction.mod_assign_get_submission_status,
        params={"assignid": str(assignid)},
        use_original_data=False,
    )
    lastattempt = data.get("lastattempt") or {}
    submission = lastattempt.get("submission") or lastattempt.get("teamsubmission") or {}
    status = submission.get("status")
    return {
        "submission_status": status,
        "submitted": status == "submitted",
        "graded": bool(lastattempt.get("graded")) or lastattempt.get("gradingstatus") == "graded",
    }


def get_assignments(
    courseids: list[int] | None = None,
    only: Literal["upcoming", "overdue", "all"] = "upcoming",
) -> list[Assignment]:
    params = format_moodle_array_params("courseids", courseids) if courseids else None
    data = get_moodle_api_data(APIFunction.mod_assign_get_assignments, params=params)

    to_json_file(data, "assignments.json")

    now = int(time.time())
    result: list[Assignment] = []
    for course in data.get("courses") or []:
        for assign in course.get("assignments") or []:
            duedate = assign.get("duedate") or 0
            if only == "upcoming" and not duedate > now:
                continue
            if only == "overdue" and not (duedate and duedate <= now):
                continue

            # One request per assignment, so only for the ones returned.
            try:
                status = _submission_status(assign["id"])
            except MoodleAPIError as e:
                logger.warning(f"No submission status for assignment {assign['id']}: {e.message}")
                status = {"submission_status": None, "submitted": None, "graded": None}
            if only == "overdue" and status["submitted"]:
                continue

            result.append(
                {
                    "id": assign["id"],
                    "name": assign.get("name") or "",
                    "courseid": course.get("id"),
                    "course_name": course.get("fullname") or "",
                    "duedate_local": local_time(duedate),
                    # After the cut-off date Moodle no longer accepts submissions.
                    "cutoffdate_local": local_time(assign.get("cutoffdate")),
                    "days_until_due": round((duedate - now) / 86400, 1) if duedate else None,
                    **status,
                    "intro": html_to_text(assign.get("intro") or "")[:INTRO_MAX_CHARS],
                }
            )

    result.sort(key=lambda a: a["days_until_due"] if a["days_until_due"] is not None else float("inf"))
    logger.info(f"Found {len(result)} assignments ({only})")
    return result


# ---------------------------------------------------------------------------
# Grades
# ---------------------------------------------------------------------------


def get_grades(courseid: int | None = None) -> list[CourseGrade] | list[GradeItem]:
    if courseid:
        return _get_course_grades_detail(courseid)
    return _get_course_grades_overview()


def _get_course_grades_overview() -> list[CourseGrade]:
    user_id = _get_user_id()
    data = get_moodle_api_data(
        APIFunction.gradereport_overview_get_course_grades,
        params={"userid": str(user_id)},
    )

    to_json_file(data, "grades_overview.json")

    course_map = _course_names()
    result: list[CourseGrade] = []
    for g in data.get("grades", []):
        course_id = g.get("courseid", 0)
        result.append(
            {
                "courseid": course_id,
                "course_name": course_map.get(course_id, "Unknown"),
                "grade": g.get("grade", ""),
                "rank": g.get("rank"),
            }
        )

    logger.info(f"Extracted {len(result)} course grade overviews")
    return result


def _get_course_grades_detail(courseid: int) -> list[GradeItem]:
    user_id = _get_user_id()
    data = get_moodle_api_data(
        APIFunction.gradereport_user_get_grade_items,
        params={"courseid": str(courseid), "userid": str(user_id)},
    )

    to_json_file(data, f"grades_detail_{courseid}.json")

    user_grades = data.get("usergrades", [])
    if not user_grades:
        return []

    result: list[GradeItem] = []
    for item in user_grades[0].get("gradeitems", []):
        # itemname can be a string or an object with a 'value' key
        itemname = item.get("itemname", "")
        if isinstance(itemname, dict):
            itemname = itemname.get("value", "")

        result.append(
            {
                "itemname": itemname,
                "grade": item.get("gradeformatted", None) or item.get("grade", None),
                "percentage": item.get("percentageformatted", None)
                or item.get("percentage", None),
                "feedback": html_to_text(item.get("feedback") or "") or None,
            }
        )

    logger.info(f"Extracted {len(result)} grade items for course {courseid}")
    return result


# ---------------------------------------------------------------------------
# Course updates
# ---------------------------------------------------------------------------


def _modules_by_id(courseid: int, refresh: bool = False) -> dict[int, tuple[str, dict]]:
    """Map module id -> (section name, module) for a course."""
    modules: dict[int, tuple[str, dict]] = {}
    try:
        for section in get_course_content(courseid, refresh=refresh):
            for module in section.get("modules") or []:
                modules[module["id"]] = (section.get("name") or "", module)
    except MoodleAPIError as e:
        logger.warning(f"No contents for course {courseid}: {e.message}")
    return modules


def get_recent_activity(days: int = 7, courseid: int | None = None) -> list[RecentActivity]:
    """Activities created or changed in the last days, named and placed in their section.

    core_course_get_updates_since only returns module ids and change types
    (configuration, contentfiles, discussions, ...); names come from the
    course contents, which are cached.
    """
    since = _days_ago(days)
    courses = get_my_courses()
    if courseid:
        courses = [c for c in courses if c["id"] == courseid]

    activities: list[RecentActivity] = []
    errors: list[MoodleAPIError] = []
    for course in courses:
        try:
            data = get_moodle_api_data(
                APIFunction.core_course_get_updates_since,
                params={"courseid": str(course["id"]), "since": str(since)},
            )
        except MoodleAPIError as e:
            logger.warning(f"Course updates unavailable for course {course['id']}: {e.message}")
            errors.append(e)
            continue

        instances = [i for i in data.get("instances") or [] if i.get("contextlevel") == "module"]
        if not instances:
            continue

        modules = _modules_by_id(course["id"])
        if any(i.get("id") not in modules for i in instances):
            # The cached course page predates a new activity.
            modules = _modules_by_id(course["id"], refresh=True)

        for inst in instances:
            section, module = modules.get(inst.get("id"), (None, {}))
            updates = inst.get("updates") or []
            times = [u["timeupdated"] for u in updates if u.get("timeupdated")]
            activities.append(
                {
                    "course_name": course["fullname"],
                    "section": section,
                    "module_id": inst.get("id"),
                    # None when the activity is not on the course page.
                    "module_name": module.get("name"),
                    "modname": module.get("modname"),
                    "changes": [u.get("name") for u in updates if u.get("name")],
                    "updated_local": local_time(max(times)) if times else None,
                    "url": module.get("url"),
                }
            )

    # Every course failed: report why instead of claiming nothing changed.
    if errors and len(errors) == len(courses):
        raise errors[0]

    activities.sort(key=lambda a: a["updated_local"] or "", reverse=True)
    logger.info(f"Found {len(activities)} updated activities in the last {days} days")
    return activities


# ---------------------------------------------------------------------------
# Announcements
# ---------------------------------------------------------------------------


def get_course_announcements(
    courseid: int | None = None, days: int | None = None
) -> list[CourseAnnouncement]:
    """Posts in the courses' news forums, newest first."""
    course_map = _course_names()
    course_ids = [courseid] if courseid else list(course_map)
    since = _days_ago(days) if days else 0

    forums_data = get_moodle_api_data(
        APIFunction.mod_forum_get_forums_by_courses,
        params=format_moodle_array_params("courseids", course_ids),
    )
    to_json_file(forums_data, "forums.json")

    # Handle both dict and list response formats
    forums_list = forums_data if isinstance(forums_data, list) else forums_data.get("forums", [])

    news_forums = [f for f in forums_list if f.get("type") == "news"]
    found: list[tuple[int, CourseAnnouncement]] = []
    errors: list[MoodleAPIError] = []
    for forum in news_forums:

        forum_id = forum.get("id", 0)
        try:
            discussions_data = get_moodle_api_data(
                APIFunction.mod_forum_get_forum_discussions,
                params={"forumid": str(forum_id), "page": "0", "perpage": "20"},
                use_original_data=False,
            )
        except MoodleAPIError as e:
            logger.warning(f"Could not get discussions for forum {forum_id}: {e.message}")
            errors.append(e)
            continue

        to_json_file(discussions_data, f"forum_discussions_{forum_id}.json")

        for disc in discussions_data.get("discussions") or []:
            created = disc.get("created", 0) or disc.get("timemodified", 0)
            if created < since:
                continue
            found.append(
                (
                    created,
                    {
                        "id": disc.get("id", 0),
                        "discussion_id": disc.get("discussion"),
                        "subject": disc.get("subject", ""),
                        "message": html_to_text(disc.get("message") or "")[:MESSAGE_MAX_CHARS],
                        "course_name": course_map.get(forum.get("course"), ""),
                        "author": disc.get("userfullname", ""),
                        "date_local": local_time(created),
                    },
                )
            )

    # Every forum failed: report why instead of claiming there are no announcements.
    if errors and len(errors) == len(news_forums):
        raise errors[0]

    found.sort(key=lambda item: item[0], reverse=True)
    logger.info(f"Found {len(found)} announcements")
    return [announcement for _, announcement in found]
