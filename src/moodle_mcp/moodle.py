import json
import re
import time
from contextvars import ContextVar
from enum import Enum

import requests
from glom import delete
from mcp.server.mcpserver.exceptions import ToolError

from .logger import logger
from .utils import getenv

MOODLE_URL = getenv("MOODLE_URL")
MOODLE_TOKEN = getenv("MOODLE_TOKEN")
TIMEOUT = float(getenv("MOODLE_MCP_TIMEOUT", "30"))
MAX_RETRIES = 2
RETRY_BACKOFF = 0.5

# Name of the MCP tool being executed, set by the tool wrapper in server.py.
current_tool: ContextVar[str | None] = ContextVar("current_tool", default=None)

# The server is read-only. Quizzes can allow a single attempt, so starting,
# saving or submitting anything by mistake cannot be undone.
DENIED_FUNCTION = re.compile(r"(^|_)(start|process|save|submit)")


class ErrorKind:
    NETWORK = "network"
    TIMEOUT = "timeout"
    ACCESS_DENIED = "access_denied"
    NOT_ENABLED = "not_enabled"
    INVALID_PARAM = "invalid_param"
    BLOCKED = "blocked"
    UNSUPPORTED = "unsupported"
    MOODLE_ERROR = "moodle_error"
    INTERNAL = "internal"


RETRYABLE = {ErrorKind.NETWORK, ErrorKind.TIMEOUT}

# Moodle errorcodes (and this server's own codes) mapped to an error kind.
_KIND_BY_CODE = {
    "network_error": ErrorKind.NETWORK,
    "timeout": ErrorKind.TIMEOUT,
    "accessexception": ErrorKind.ACCESS_DENIED,
    "webservice_access_exception": ErrorKind.ACCESS_DENIED,
    "nopermissions": ErrorKind.ACCESS_DENIED,
    "requireloginerror": ErrorKind.ACCESS_DENIED,
    "invalidtoken": ErrorKind.ACCESS_DENIED,
    "servicerequireslogin": ErrorKind.ACCESS_DENIED,
    "errorcoursecontextnotvalid": ErrorKind.ACCESS_DENIED,
    "noreview": ErrorKind.ACCESS_DENIED,
    "redirect": ErrorKind.ACCESS_DENIED,
    "config_error": ErrorKind.ACCESS_DENIED,
    "not_enabled": ErrorKind.NOT_ENABLED,
    "blocked": ErrorKind.BLOCKED,
    "invalidparameter": ErrorKind.INVALID_PARAM,
    "invalidrecord": ErrorKind.INVALID_PARAM,
    "invalidrecordunknown": ErrorKind.INVALID_PARAM,
    "missingparam": ErrorKind.INVALID_PARAM,
    "invalid_url": ErrorKind.INVALID_PARAM,
    "invalid_pages": ErrorKind.INVALID_PARAM,
    "invalid_path": ErrorKind.INVALID_PARAM,
    "not_found": ErrorKind.INVALID_PARAM,
    "file_too_large": ErrorKind.UNSUPPORTED,
    "unsupported_type": ErrorKind.UNSUPPORTED,
    "invalid_pdf": ErrorKind.UNSUPPORTED,
}


class MoodleAPIError(ToolError):
    """Raised when a Moodle call fails.

    Subclasses ToolError so the message reaches the MCP client instead of a
    generic "Error executing tool" result. The message is a JSON object with
    tool, ws_function, kind, code, message and retryable, so the model can
    tell a disabled function from a network problem.
    """

    def __init__(self, error_code: str, message: str, function: str | None, kind: str | None = None):
        self.error_code = error_code
        self.message = redact(message)
        self.function = function
        self.kind = kind or _KIND_BY_CODE.get(error_code, ErrorKind.MOODLE_ERROR)
        self.tool = current_tool.get()
        if self.kind == ErrorKind.ACCESS_DENIED and error_code in (
            "accessexception",
            "webservice_access_exception",
        ):
            # Moodle uses the same exception for "function not in the token's
            # service" and for missing permissions; the site info tells them apart.
            enabled = enabled_functions(fetch=False)
            if enabled is not None and function not in enabled:
                self.kind = ErrorKind.NOT_ENABLED
        super().__init__(self.to_json())

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "ws_function": self.function,
            "kind": self.kind,
            "code": self.error_code,
            "message": self.message,
            "retryable": self.retryable,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class APIFunction(Enum):
    core_calendar_get_calendar_upcoming_view = (
        "core_calendar_get_calendar_upcoming_view"
    )
    core_webservice_get_site_info = "core_webservice_get_site_info"
    core_enrol_get_users_courses = "core_enrol_get_users_courses"
    core_course_get_contents = "core_course_get_contents"
    mod_assign_get_assignments = "mod_assign_get_assignments"
    mod_assign_get_submission_status = "mod_assign_get_submission_status"
    gradereport_overview_get_course_grades = (
        "gradereport_overview_get_course_grades"
    )
    gradereport_user_get_grade_items = "gradereport_user_get_grade_items"
    core_course_get_updates_since = "core_course_get_updates_since"
    mod_forum_get_forums_by_courses = "mod_forum_get_forums_by_courses"
    mod_forum_get_forum_discussions = "mod_forum_get_forum_discussions"
    core_completion_get_course_completion_status = (
        "core_completion_get_course_completion_status"
    )
    core_calendar_get_calendar_events = "core_calendar_get_calendar_events"
    mod_quiz_get_quizzes_by_courses = "mod_quiz_get_quizzes_by_courses"
    mod_quiz_get_user_attempts = "mod_quiz_get_user_attempts"
    mod_quiz_get_attempt_review = "mod_quiz_get_attempt_review"


# Fields not needed for specific API functions
# Using `glom` to extract fields not needed
DELETE_FIELDS = {
    APIFunction.core_calendar_get_calendar_upcoming_view: [
        "events.*.course.courseimage"
    ],
    APIFunction.core_course_get_contents: [
        "*.modules.*.modicon",
        "*.modules.*.modplural",
        "*.modules.*.onclick",
        "*.modules.*.afterlink",
        "*.modules.*.customdata",
        "*.modules.*.contents.*.filepath",
        "*.modules.*.contents.*.timecreated",
        "*.modules.*.contents.*.timemodified",
        "*.modules.*.contents.*.sortorder",
        "*.modules.*.contents.*.isexternalfile",
        "*.modules.*.contents.*.repositorytype",
        "*.modules.*.contents.*.userid",
        "*.modules.*.contents.*.author",
        "*.modules.*.contents.*.license",
        "*.modules.*.contents.*.mimetype",
        "*.modules.*.completiondata",
        "*.modules.*.contentsinfo",
    ],
    APIFunction.mod_assign_get_submission_status: [
        "lastattempt.submission.plugins",
        "feedback.plugins",
        "feedback.grade.grader",
    ],
    APIFunction.mod_forum_get_forum_discussions: [
        "discussions.*.messageinlinefiles",
        "discussions.*.attachments",
    ],
}


def format_moodle_array_params(key: str, values: list) -> dict:
    """Format a list of values as Moodle-style array parameters.

    e.g. format_moodle_array_params('courseids', [5, 10])
         -> {'courseids[0]': 5, 'courseids[1]': 10}
    """
    return {f"{key}[{i}]": v for i, v in enumerate(values)}


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

# Secrets besides the token, e.g. the user's private access key that Moodle
# puts in tokenpluginfile.php URLs.
_extra_secrets: set[str] = set()
_TOKEN_PARAM = re.compile(r"((?:[?&]|&amp;)(?:ws)?token=)[^&\s\"'<>]+", re.IGNORECASE)


def add_secret(value: str | None) -> None:
    if value and len(value) >= 8:
        _extra_secrets.add(value)


def redact(text: str) -> str:
    """Remove the token and other secrets from text meant for logs or the model."""
    if not isinstance(text, str):
        return text
    # Real tokens are long hex strings; skip trivially short values.
    for secret in (MOODLE_TOKEN, *_extra_secrets):
        if secret and len(secret) >= 8 and secret in text:
            text = text.replace(secret, "***")
    if "token=" in text.lower():
        text = _TOKEN_PARAM.sub(r"\1***", text)
    return text


_redact = redact


def redact_result(value):
    """Recursively redact the strings of a tool result (dicts, lists, strings).

    Other objects (images, embedded resources) hold base64 data built by this
    server and are left alone.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_result(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_result(v) for v in value)
    return value


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def post_with_retry(
    url: str, data: dict, function: str, timeout: float | None = None, retries: int = MAX_RETRIES, **kwargs
):
    """POST, retrying up to `retries` times on network errors, timeouts and 502-504.

    Only read-only functions get here (see DENIED_FUNCTION), so repeating a
    request cannot change anything on Moodle.
    """
    wsfunction = data.get("wsfunction") or ""
    if DENIED_FUNCTION.search(wsfunction):
        raise MoodleAPIError(
            "blocked", f"{wsfunction} could change data on Moodle; this server is read-only", wsfunction
        )

    timeout = timeout or TIMEOUT
    for attempt in range(retries + 1):
        last = attempt == retries
        try:
            rsp = requests.post(url, data=data, timeout=timeout, **kwargs)
        except requests.Timeout:
            error = MoodleAPIError(
                "timeout", f"No answer from Moodle within {timeout:g} s (MOODLE_MCP_TIMEOUT)", function
            )
        except requests.RequestException as e:
            error = MoodleAPIError("network_error", str(e), function)
        else:
            if rsp.status_code not in (502, 503, 504) or last:
                return rsp
            rsp.close()
            error = None
        if last:
            logger.error(f"{error.kind} calling {function}: {error.message}")
            raise error
        delay = RETRY_BACKOFF * 2**attempt
        logger.warning(f"Retrying {function} in {delay:g} s (attempt {attempt + 2})")
        time.sleep(delay)


def get_moodle_api_data(
    function: APIFunction,
    params: dict = None,
    use_original_data=True,
    timeout: float | None = None,
    retries: int = MAX_RETRIES,
):
    if not MOODLE_URL or not MOODLE_TOKEN:
        raise MoodleAPIError(
            "config_error",
            "MOODLE_URL and MOODLE_TOKEN environment variables must be set",
            function.value,
        )

    request_params = {
        "wstoken": MOODLE_TOKEN,
        "wsfunction": function.value,
        "moodlewsrestformat": "json",
    }
    if params:
        request_params.update(params)

    logger.info(
        f"Getting moodle data for `{function.value}`"
        f" with params: {list(params.keys()) if params else 'none'}"
    )

    # POST keeps the token out of URLs, which end up in logs and error messages.
    rsp = post_with_retry(MOODLE_URL, request_params, function.value, timeout=timeout, retries=retries)

    if rsp.status_code != 200:
        logger.error(f"Moodle API HTTP error: {rsp.status_code} for {function.value}")
        raise MoodleAPIError(
            "network_error" if rsp.status_code >= 500 else "http_error",
            f"HTTP {rsp.status_code}: {rsp.text[:200]}",
            function.value,
        )

    try:
        data = rsp.json()
    except ValueError:
        logger.error(f"Non-JSON response for {function.value}")
        raise MoodleAPIError(
            "invalid_response",
            "Moodle did not return JSON; check that MOODLE_URL points to"
            " .../webservice/rest/server.php",
            function.value,
        ) from None

    # Moodle returns errors as JSON with 'errorcode' and 'message' fields
    if isinstance(data, dict) and "errorcode" in data:
        error_msg = data.get("message", "Unknown Moodle API error")
        error_code = data.get("errorcode", "unknown")
        logger.error(f"Moodle API error: [{error_code}] {redact(error_msg)}")
        raise MoodleAPIError(error_code, error_msg, function.value)

    if use_original_data:
        return data

    for field_path in DELETE_FIELDS.get(function, []):
        delete(data, field_path, ignore_missing=True)

    return data


# ---------------------------------------------------------------------------
# Site info
# ---------------------------------------------------------------------------

_site_info: dict | None = None


def get_site_info(refresh: bool = False, timeout: float | None = None, retries: int = MAX_RETRIES) -> dict:
    """core_webservice_get_site_info, cached for the life of the process."""
    global _site_info
    if _site_info is None or refresh:
        info = get_moodle_api_data(
            APIFunction.core_webservice_get_site_info, timeout=timeout, retries=retries
        )
        add_secret(info.get("userprivateaccesskey"))
        _site_info = info
    return _site_info


def enabled_functions(fetch: bool = True) -> set[str] | None:
    """Names of the web service functions enabled for the token.

    With fetch=False, returns None instead of calling Moodle when the site
    info has not been loaded yet.
    """
    if _site_info is None and not fetch:
        return None
    return {f["name"] for f in get_site_info().get("functions") or []}
