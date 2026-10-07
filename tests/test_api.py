import asyncio

import pytest
import requests

from moodle_mcp import api, quiz, server
from moodle_mcp.moodle import MoodleAPIError

NOW = 1_790_000_000
DAY = 86400


@pytest.fixture
def frozen_time(monkeypatch):
    monkeypatch.setattr(api.time, "time", lambda: NOW)


@pytest.fixture
def one_course(fake_moodle, site_info):
    fake_moodle.responses["core_webservice_get_site_info"] = site_info
    fake_moodle.responses["core_enrol_get_users_courses"] = [
        {"id": 5, "fullname": "Analisi Matematica 1", "shortname": "2026-IN0001-XYZ"}
    ]
    return fake_moodle


def contents(*modules):
    return [{"name": "Settimana 2", "section": 2, "modules": list(modules)}]


def test_recent_activity_names_modules(one_course, frozen_time):
    one_course.responses["core_course_get_updates_since"] = {
        "instances": [
            {"contextlevel": "module", "id": 70, "updates": [{"name": "configuration", "timeupdated": NOW - DAY}, {"name": "contentfiles"}]}
        ]
    }
    one_course.responses["core_course_get_contents"] = contents(
        {"id": 70, "name": "Slide limiti", "modname": "resource", "url": "https://moodle.example.org/mod/resource/view.php?id=70"}
    )
    [activity] = api.get_recent_activity(days=7)

    assert activity["module_name"] == "Slide limiti"
    assert activity["section"] == "Settimana 2"
    assert activity["changes"] == ["configuration", "contentfiles"]
    assert activity["updated_local"] is not None
    since = next(c for c in one_course.calls if c["key"] == "core_course_get_updates_since")["data"]["since"]
    assert since == str(NOW - 7 * DAY)


def test_recent_activity_fails_loudly_when_every_course_fails(one_course):
    one_course.responses["core_course_get_updates_since"] = requests.ConnectionError("down")
    with pytest.raises(MoodleAPIError):
        api.get_recent_activity()


def assignments_payload(*duedates):
    return {
        "courses": [
            {"id": 5, "fullname": "Analisi Matematica 1", "assignments": [
                {"id": 100 + i, "name": f"Esercizi {i}", "duedate": d, "cutoffdate": 0, "intro": "<p>Consegna PDF</p>"}
                for i, d in enumerate(duedates)
            ]}
        ]
    }


def status(state):
    return {"lastattempt": {"submission": {"status": state}, "gradingstatus": "notgraded"}}


def test_assignments_upcoming_only_fetches_status_for_future_ones(fake_moodle, frozen_time):
    fake_moodle.responses["mod_assign_get_assignments"] = assignments_payload(NOW - DAY, NOW + 2 * DAY, 0)
    fake_moodle.responses["mod_assign_get_submission_status"] = status("new")

    [a] = api.get_assignments()
    assert a["id"] == 101
    assert a["days_until_due"] == 2.0
    assert a["submitted"] is False
    assert a["intro"] == "Consegna PDF"
    assert fake_moodle.count("mod_assign_get_submission_status") == 1


def test_assignments_overdue_skips_submitted(fake_moodle, frozen_time):
    fake_moodle.responses["mod_assign_get_assignments"] = assignments_payload(NOW - DAY, NOW - 2 * DAY)
    from conftest import Seq

    fake_moodle.responses["mod_assign_get_submission_status"] = Seq([status("submitted"), status("new")])
    overdue = api.get_assignments(only="overdue")
    assert [a["id"] for a in overdue] == [101]
    assert overdue[0]["days_until_due"] == -2.0


def test_announcements_filter_by_days(one_course, frozen_time):
    one_course.responses["mod_forum_get_forums_by_courses"] = [{"id": 9, "course": 5, "type": "news"}, {"id": 10, "course": 5, "type": "general"}]
    one_course.responses["mod_forum_get_forum_discussions"] = {
        "discussions": [
            {"id": 1, "discussion": 11, "subject": "Nuovo", "message": "<p>Aula cambiata</p>", "created": NOW - DAY, "userfullname": "Prof"},
            {"id": 2, "discussion": 12, "subject": "Vecchio", "message": "x", "created": NOW - 30 * DAY, "userfullname": "Prof"},
        ]
    }
    result = api.get_course_announcements(days=7)
    assert [a["subject"] for a in result] == ["Nuovo"]
    assert result[0]["message"] == "Aula cambiata"
    assert result[0]["course_name"] == "Analisi Matematica 1"
    # Only the news forum is read.
    assert one_course.count("mod_forum_get_forum_discussions") == 1


def test_announcements_report_failure_instead_of_empty_list(one_course):
    one_course.responses["mod_forum_get_forums_by_courses"] = [{"id": 9, "course": 5, "type": "news"}]
    one_course.responses["mod_forum_get_forum_discussions"] = {
        "exception": "webservice_access_exception", "errorcode": "accessexception", "message": "Access control exception"
    }
    with pytest.raises(MoodleAPIError):
        api.get_course_announcements()


def test_prompts_are_registered_with_optional_arguments():
    prompts = {p.name: p for p in asyncio.run(server.mcp.list_prompts())}
    assert set(prompts) == {"briefing", "prepara-lezione", "revisione-quiz", "settimana"}
    for prompt in prompts.values():
        assert all(not a.required for a in prompt.arguments or [])


@pytest.mark.parametrize("name", ["briefing", "prepara-lezione", "revisione-quiz", "settimana"])
def test_prompts_only_name_existing_tools_and_forbid_guessing(name):
    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    # Prompts may also name fields of the results.
    tools |= set(quiz.Quiz.__annotations__)
    text = asyncio.run(server.mcp.get_prompt(name, {})).messages[0].content.text
    mentioned = {word.strip(".,:;()") for word in text.split() if "_" in word}
    assert mentioned <= tools, mentioned - tools
    assert "invece di dedurre" in text


def test_prompt_arguments_are_used():
    text = asyncio.run(server.mcp.get_prompt("prepara-lezione", {"corso": "Fisica 2", "argomento": "induzione"})).messages[0].content.text
    assert "«Fisica 2»" in text and "«induzione»" in text


def test_hidden_module_refreshes_contents_only_once(one_course, monkeypatch, tmp_path):
    monkeypatch.setenv("MOODLE_MCP_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(api, "_hidden_modules", set())
    one_course.responses["core_course_get_updates_since"] = {
        "instances": [{"contextlevel": "module", "id": 99, "updates": [{"name": "configuration"}]}]
    }
    one_course.responses["core_course_get_contents"] = contents()
    api.get_recent_activity()
    api.get_recent_activity()
    # First call: cached read + one refresh; second call: cache only.
    assert one_course.count("core_course_get_contents") == 2
