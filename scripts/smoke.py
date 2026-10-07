"""Smoke test against the real Moodle configured in .env (read-only calls).

    uv run python scripts/smoke.py

Calls diagnose, get_my_courses, get_quizzes and reads one page of the first
PDF found, through the MCP tool layer, and prints how long each call took.
Prints counts and names only, never the token.
"""

import asyncio
import json
import sys
import time

from moodle_mcp import files, server


def call(name: str, args: dict | None = None):
    started = time.monotonic()
    try:
        result = asyncio.run(server.mcp.call_tool(name, args or {}))
    except Exception as e:
        print(f"FAIL {name:<22} {time.monotonic() - started:6.2f}s  {e}")
        return None
    elapsed = time.monotonic() - started
    texts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
    print(f"ok   {name:<22} {elapsed:6.2f}s  {len(result.content)} content blocks")
    return [json.loads(t) if t[:1] in "[{" else t for t in texts]


def main() -> int:
    failed = False

    diag = call("diagnose")
    if diag:
        d = diag[0]
        print(f"     Moodle {d['site']['release']}, {d['token']['functions_enabled']} functions enabled")
        if d["tools_unavailable_now"]:
            print(f"     unavailable tools: {', '.join(d['tools_unavailable_now'])}")
    failed |= diag is None

    courses = call("get_my_courses")
    failed |= courses is None
    for course in courses or []:
        print(f"     {course['id']}: {course['shortname']}")

    quizzes = call("get_quizzes")
    failed |= quizzes is None
    if quizzes:
        print(f"     {len(quizzes)} quizzes, {sum(q['open_now'] for q in quizzes)} open now")

    pdf = None
    for course in courses or []:
        found = files.list_course_files(course["id"], mimetype="application/pdf")
        if found:
            pdf = found[0]
            break
    if pdf:
        page = call("read_course_file", {"fileurl": pdf["fileurl"], "pages": "1"})
        failed |= page is None
        if page:
            p = page[0]
            print(f"     {p['filename']}: {p['pages_total']} pages, text layer {p['has_text_layer']}")
        again = call("read_course_file", {"fileurl": pdf["fileurl"], "pages": "2"})
        failed |= again is None
    else:
        print("skip read_course_file: no PDF found")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
