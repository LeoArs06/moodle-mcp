"""Shared test setup.

The Moodle settings are read when the package is imported, so they are set
here first. load_dotenv() does not override variables that are already set,
so a real .env file cannot leak into the tests.
"""

import json
import os
from pathlib import Path

os.environ["MOODLE_URL"] = "https://moodle.example.org/webservice/rest/server.php"
os.environ["MOODLE_TOKEN"] = "0123456789abcdef0123456789abcdef"
os.environ["MOODLE_MCP_CACHE_DIR"] = ""
os.environ.pop("MOODLE_DOWNLOAD_DIR", None)

import pytest  # noqa: E402
import requests  # noqa: E402

from moodle_mcp import moodle  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = os.environ["MOODLE_TOKEN"]


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, payload=None, status_code=200, content: bytes | None = None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content if content is not None else json.dumps(payload).encode()
        self.headers = headers or {}
        self.is_redirect = status_code in (301, 302, 303, 307, 308)

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.content)

    def iter_content(self, size):
        for i in range(0, len(self.content), size):
            yield self.content[i : i + size]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Seq(list):
    """Answers returned one after the other by FakeMoodle."""


class FakeMoodle:
    """Stands in for requests.post: answers web service calls from a dict.

    responses maps a wsfunction (or a pluginfile URL) to a payload, a
    FakeResponse, an exception instance, or a Seq of those consumed in order
    (the last one repeats).
    """

    def __init__(self):
        self.responses: dict = {}
        self.calls: list[dict] = []

    def __call__(self, url, data=None, timeout=None, **kwargs):
        key = (data or {}).get("wsfunction") or url
        self.calls.append({"key": key, "url": url, "data": dict(data or {}), "timeout": timeout})
        if key not in self.responses:
            raise AssertionError(f"Unexpected request to {key}")
        answer = self.responses[key]
        if isinstance(answer, Seq):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, FakeResponse):
            return answer
        return FakeResponse(answer)

    def count(self, key):
        return sum(1 for c in self.calls if c["key"] == key)


@pytest.fixture
def fake_moodle(monkeypatch):
    fake = FakeMoodle()
    monkeypatch.setattr(requests, "post", fake)
    monkeypatch.setattr(moodle, "RETRY_BACKOFF", 0)
    monkeypatch.setattr(moodle, "_site_info", None)
    monkeypatch.setattr(moodle, "_extra_secrets", set())
    return fake


@pytest.fixture
def site_info():
    return load_fixture("site_info.json")
