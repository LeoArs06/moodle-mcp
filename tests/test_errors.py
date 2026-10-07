import asyncio
import json

import pytest
import requests

from conftest import TOKEN, FakeResponse, Seq
from moodle_mcp import moodle, server
from moodle_mcp.moodle import APIFunction, MoodleAPIError, get_moodle_api_data

ACCESS_EXCEPTION = {
    "exception": "webservice_access_exception",
    "errorcode": "accessexception",
    "message": "Access control exception",
}


def error_payload(excinfo) -> dict:
    return json.loads(str(excinfo.value))


def test_moodle_error_is_structured(fake_moodle):
    fake_moodle.responses["core_course_get_contents"] = {
        "exception": "invalid_parameter_exception",
        "errorcode": "invalidparameter",
        "message": "Invalid parameter value detected",
    }
    with pytest.raises(MoodleAPIError) as excinfo:
        get_moodle_api_data(APIFunction.core_course_get_contents, {"courseid": "1"})

    payload = error_payload(excinfo)
    assert payload["kind"] == "invalid_param"
    assert payload["ws_function"] == "core_course_get_contents"
    assert payload["retryable"] is False
    assert set(payload) == {"tool", "ws_function", "kind", "code", "message", "retryable"}


def test_access_exception_for_disabled_function_is_not_enabled(fake_moodle, site_info):
    fake_moodle.responses["core_webservice_get_site_info"] = site_info
    fake_moodle.responses["core_course_get_updates_since"] = ACCESS_EXCEPTION
    moodle.get_site_info()

    with pytest.raises(MoodleAPIError) as excinfo:
        get_moodle_api_data(APIFunction.core_course_get_updates_since)
    assert error_payload(excinfo)["kind"] == "not_enabled"


def test_access_exception_for_enabled_function_is_access_denied(fake_moodle, site_info):
    fake_moodle.responses["core_webservice_get_site_info"] = site_info
    fake_moodle.responses["core_course_get_contents"] = ACCESS_EXCEPTION
    moodle.get_site_info()

    with pytest.raises(MoodleAPIError) as excinfo:
        get_moodle_api_data(APIFunction.core_course_get_contents)
    assert error_payload(excinfo)["kind"] == "access_denied"


def test_retries_network_errors_then_succeeds(fake_moodle):
    fake_moodle.responses["core_course_get_contents"] = Seq(
        [requests.ConnectionError("connection reset"), requests.Timeout("read timed out"), []]
    )
    assert get_moodle_api_data(APIFunction.core_course_get_contents) == []
    assert fake_moodle.count("core_course_get_contents") == 3


def test_gives_up_after_two_retries(fake_moodle):
    fake_moodle.responses["core_course_get_contents"] = requests.Timeout("read timed out")
    with pytest.raises(MoodleAPIError) as excinfo:
        get_moodle_api_data(APIFunction.core_course_get_contents)

    payload = error_payload(excinfo)
    assert payload["kind"] == "timeout"
    assert payload["retryable"] is True
    assert fake_moodle.count("core_course_get_contents") == 3


def test_does_not_retry_moodle_errors(fake_moodle):
    fake_moodle.responses["core_course_get_contents"] = ACCESS_EXCEPTION
    with pytest.raises(MoodleAPIError):
        get_moodle_api_data(APIFunction.core_course_get_contents)
    assert fake_moodle.count("core_course_get_contents") == 1


def test_retries_bad_gateway(fake_moodle):
    fake_moodle.responses["core_course_get_contents"] = Seq([FakeResponse(status_code=503, content=b""), []])
    assert get_moodle_api_data(APIFunction.core_course_get_contents) == []


def test_token_is_redacted_from_network_errors(fake_moodle):
    fake_moodle.responses["core_course_get_contents"] = requests.ConnectionError(
        f"failed https://moodle.example.org/?wstoken={TOKEN}"
    )
    with pytest.raises(MoodleAPIError) as excinfo:
        get_moodle_api_data(APIFunction.core_course_get_contents)
    assert TOKEN not in str(excinfo.value)


def test_redact_result_removes_token_and_private_key(fake_moodle, site_info):
    fake_moodle.responses["core_webservice_get_site_info"] = site_info
    moodle.get_site_info()
    key = site_info["userprivateaccesskey"]
    result = moodle.redact_result(
        {
            "a": f"https://moodle.example.org/tokenpluginfile.php/{key}/1/f.pdf",
            "b": [f"https://moodle.example.org/pluginfile.php/1/f.pdf?token={TOKEN}&x=1"],
            "c": "https://moodle.example.org/file.php?token=abcdef12&forcedownload=1",
        }
    )
    text = json.dumps(result)
    assert key not in text and TOKEN not in text and "abcdef12" not in text
    assert "forcedownload=1" in text


def call_tool(name, args=None):
    return asyncio.run(server.mcp.call_tool(name, args or {}))


def test_tool_errors_name_the_tool(fake_moodle):
    fake_moodle.responses["core_course_get_contents"] = ACCESS_EXCEPTION
    with pytest.raises(Exception) as excinfo:
        call_tool("get_course_content", {"courseid": 5})
    message = str(excinfo.value)
    payload = json.loads(message[message.index("{"):])
    assert payload["tool"] == "get_course_content"
    assert payload["ws_function"] == "core_course_get_contents"


def test_unexpected_exceptions_become_internal_errors(fake_moodle, monkeypatch):
    def boom(courseid):
        raise KeyError("modules")

    monkeypatch.setattr(server.api, "get_course_content", boom)
    with pytest.raises(Exception) as excinfo:
        call_tool("get_course_content", {"courseid": 5})
    message = str(excinfo.value)
    payload = json.loads(message[message.index("{"):])
    assert payload["kind"] == "internal"
    assert "KeyError" in payload["message"]
    assert "Traceback" not in message


def test_tool_results_are_redacted(fake_moodle):
    fake_moodle.responses["core_course_get_contents"] = [
        {
            "name": "Week 1",
            "section": 1,
            "modules": [
                {"id": 1, "name": "Notes", "modname": "url", "url": f"https://x.org/f?token={TOKEN}"}
            ],
        }
    ]
    result = call_tool("get_course_content", {"courseid": 5})
    assert TOKEN not in json.dumps([c.model_dump() for c in result.content])


def test_every_tool_has_a_requirement_entry():
    registered = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert registered == set(server.TOOL_WS)


def test_tools_with_disabled_functions_are_removed(fake_moodle, site_info, monkeypatch):
    site_info["functions"] = [
        f for f in site_info["functions"] if f["name"] != "mod_forum_get_forum_discussions"
    ]
    site_info["downloadfiles"] = 0
    fake_moodle.responses["core_webservice_get_site_info"] = site_info
    removed = []
    monkeypatch.setattr(server.mcp, "remove_tool", removed.append)
    monkeypatch.setattr(server, "unavailable_tools", {})

    server.apply_availability()

    assert "get_course_announcements" in removed
    assert "read_course_file" in removed
    assert "get_my_courses" not in removed
    assert "diagnose" not in removed


def test_unreachable_moodle_keeps_all_tools(fake_moodle, monkeypatch):
    fake_moodle.responses["core_webservice_get_site_info"] = requests.ConnectionError("down")
    removed = []
    monkeypatch.setattr(server.mcp, "remove_tool", removed.append)
    monkeypatch.setattr(server, "startup_probe_error", None)

    server.apply_availability()

    assert removed == []
    assert server.startup_probe_error["kind"] == "network"
    # No retries during startup, the client is waiting.
    assert fake_moodle.count("core_webservice_get_site_info") == 1


def test_diagnose_reports_missing_functions_without_secrets(fake_moodle, site_info):
    fake_moodle.responses["core_webservice_get_site_info"] = site_info
    result = server.diagnose()
    assert result["user"]["id"] == 42
    assert result["ws_functions_used_by_tools"]["core_course_get_updates_since"] is False
    assert "get_recent_activity" in result["tools_unavailable_now"]
    assert site_info["userprivateaccesskey"] not in json.dumps(moodle.redact_result(result))
    assert "functions" not in result
