[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/loyaniu-moodle-mcp-badge.png)](https://mseep.ai/app/loyaniu-moodle-mcp)

# Moodle-MCP

> A Model Context Protocol (MCP) server implementation that provides capabilities to interact with Moodle LMS.

## Features

The server is read-only. It exposes 15 data tools, 4 prompts and one resource.

### Tools and the web service functions they need

| Tool | What it returns | Moodle web service functions |
| --- | --- | --- |
| `diagnose` | User, Moodle release, which functions each tool needs and whether the token has them, tools disabled at startup | `core_webservice_get_site_info` |
| `get_my_courses` | Enrolled courses (id, fullname, progress) | `core_enrol_get_users_courses` |
| `get_course_content` | Sections and activities of a course, with their files | `core_course_get_contents` |
| `list_course_files` | Files of a course with `fileurl`, size and type, filtered by `query` or `mimetype` | `core_course_get_contents` |
| `read_course_file` | Text of a PDF (by page ranges), HTML page or text file; `inner_path` reads a file inside a zip | file download (`downloadfiles`) |
| `list_zip_contents` | Files inside a zip archive | file download |
| `view_course_file_pages` | PDF pages as images, up to 8 per call (`dpi`, `grayscale`) | file download |
| `get_upcoming_events` | Calendar events of all courses, local start and end time | `core_calendar_get_calendar_upcoming_view` |
| `get_assignments` | Assignments with due date, `days_until_due` and submission status; `only` = `upcoming`, `overdue` or `all` | `mod_assign_get_assignments`, `mod_assign_get_submission_status` |
| `get_grades` | Grade per course, or grade items with feedback for one course | `gradereport_overview_get_course_grades`, `gradereport_user_get_grade_items` |
| `search_course_materials` | Activities and files whose name matches, across all courses | `core_enrol_get_users_courses`, `core_course_get_contents` |
| `get_recent_activity` | Activities created or changed in the last `days`, with name, section and kind of change | `core_course_get_updates_since`, `core_course_get_contents` |
| `get_course_announcements` | Posts of the news forums, optionally of the last `days` | `mod_forum_get_forums_by_courses`, `mod_forum_get_forum_discussions` |
| `get_quizzes` | Quizzes with opening and closing time, time limit, attempts allowed, finished and left | `mod_quiz_get_quizzes_by_courses`, `mod_quiz_get_user_attempts` |
| `get_quiz_review` | Questions of a finished attempt with the given answer, state, marks and feedback | `mod_quiz_get_user_attempts`, `mod_quiz_get_attempt_review` |

At startup the server reads the token's enabled functions and does not register the tools that could not work (the map lives in `TOOL_WS` in `server.py`). `diagnose` lists what was disabled and why. If Moodle cannot be reached at startup, every tool stays registered.

Times are ISO 8601 in the machine's time zone. HTML (intros, announcements, feedback) is converted to text.

### Errors

A failing tool returns a JSON object instead of a stack trace:

```json
{"tool": "get_quiz_review", "ws_function": "mod_quiz_get_attempt_review", "kind": "access_denied",
 "code": "noreview", "message": "...", "retryable": false}
```

`kind` is one of `network`, `timeout`, `access_denied`, `not_enabled` (the function is not in the token's service), `invalid_param`, `blocked`, `unsupported`, `moodle_error` or `internal`. Network errors, timeouts and HTTP 502-504 are retried twice with backoff.

### Read-only

Any web service function whose name contains `start`, `process`, `save` or `submit` (for example `mod_quiz_start_attempt`) is refused before the request is sent. Quizzes often allow a single attempt, and opening one by mistake would use it up. `get_quiz_review` only opens attempts that are already finished.

The token, `token=` URL parameters and the user's private access key are removed from every tool result and log line.

### Files

Files are only fetched from the Moodle host in `MOODLE_URL`, with the token sent in the POST body, so links to other sites never receive it.

`read_course_file` puts `pages_total`, `has_text_layer` and `next_pages` first. Pages with fewer than 50 letters and digits are listed in `pages_without_text`. When most of a PDF (judged on pages spread over the document) has no text, it is treated as scanned or handwritten: no text is returned, only a hint with the pages to pass to `view_course_file_pages`.

Downloaded files are cached on disk, so reading a long PDF in chunks downloads it once. The cache key includes the file's `timemodified` when the course page reported it. Course contents are cached for an hour. The cache holds course material: it lives in `%LOCALAPPDATA%\moodle-mcp\cache` (Windows) or `~/.cache/moodle-mcp`.

### Prompts

Picked from the client's prompt menu (in Italian). They only say which tools to use and how to present the result, and ask the model to say when data is missing instead of guessing.

| Prompt | Arguments | Does |
| --- | --- | --- |
| `briefing` | | Deadlines of the next 7 days, announcements of the last 7 days, quizzes still open |
| `prepara-lezione` | `corso`, `argomento` (optional) | Finds the material of a lesson and proposes what to read |
| `revisione-quiz` | `corso`, `quiz` (optional) | Reviews a finished quiz and lists the mistakes |
| `settimana` | | What came out this week on each course |

### Resource

`moodle://courses`: id, fullname and shortname of the enrolled courses. Clients such as Claude Desktop only read it when you attach it.

## API Reference

For available Moodle API functions, please refer to the [official documentation](https://docs.moodle.org/dev/Web_service_API_functions).

## Setup Instructions

### Method 1: Using `mcp` CLI (recommended)

1. Create your own `.env` file from `.env.example`
2. Assume you have `uv` installed, run `uv add "mcp[cli]"` to install the MCP CLI tools
3. Run `mcp install main.py -f .env` to add the moodle-mcp server to Claude app

### Method 2: Using `uvx`

Go to Claude > Settings > Developer > Edit Config > claude_desktop_config.json to include the following

```json
{
  "mcpServers": {
    "moodle-mcp": {
      "command": "uvx",
      "args": ["moodle-mcp"],
      "env": {
        "MOODLE_URL": "https://{your-moodle-url}/webservice/rest/server.php",
        "MOODLE_TOKEN": "{your-moodle-token}"
      }
    }
  }
}
```

### Method 3: MetaMCP (remote clients over SSE / streamable HTTP)

[MetaMCP](https://github.com/metatool-ai/metamcp) runs the server over stdio and exposes it to remote clients such as claude.ai, Cowork or the Claude mobile app. Add a **STDIO** server with:

- **Command:** `uvx`
- **Arguments:** `--from https://github.com/<owner>/moodle-mcp/archive/<commit-or-branch>.zip moodle-mcp`
- **Environment variables:**

  ```
  MOODLE_URL=https://{your-moodle-url}/webservice/rest/server.php
  MOODLE_TOKEN={your-moodle-token}
  ```

Pin a commit hash rather than a branch name so every restart runs the same code, and update the hash to upgrade.

The server writes nothing to the working directory, so it runs from read-only containers. The file cache goes to the user cache directory; set `MOODLE_MCP_CACHE_DIR` to a writable path, or to an empty value to turn it off.

To check the setup from the MetaMCP host:

```bash
MOODLE_URL=... MOODLE_TOKEN=... uvx --from <zip-url> moodle-mcp --health
```

### Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `MOODLE_URL` | (required) | `https://{your-moodle-url}/webservice/rest/server.php` |
| `MOODLE_TOKEN` | (required) | Web service token, see [Authentication](#authentication) |
| `MOODLE_MAX_DOWNLOAD_MB` | `20` | Largest file (or unpacked zip member) the file tools will fetch |
| `MOODLE_MCP_TIMEOUT` | `30` | Seconds to wait for Moodle (downloads get twice as long) |
| `MOODLE_MCP_FILTER_TOOLS` | `1` | `0` keeps every tool registered even if its functions are not enabled |
| `MOODLE_MCP_CACHE_DIR` | user cache dir | Where the disk cache lives; empty value disables it |
| `MOODLE_MCP_CACHE_TTL_HOURS` | `24` | How long downloaded files stay cached |
| `MOODLE_MCP_CONTENTS_TTL_MINUTES` | `60` | How long course contents stay cached |
| `MOODLE_MCP_CACHE_MAX_MB` | `500` | Size limit of the cache; oldest entries go first |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `MCP_HTTP_HOST` / `MCP_HTTP_PORT` | `127.0.0.1` / `8000` | Bind address for `streamable-http` |
| `MOODLE_MCP_LOG_LEVEL` | `INFO` | Log level (logs go to stderr) |
| `MOODLE_MCP_LOG_FILE` | unset | Also append logs to this file |
| `MOODLE_MCP_DUMP_DIR` | unset | Dump raw Moodle responses here for debugging (contains personal data) |

`streamable-http` has no authentication of its own. Keep it on localhost or put it behind a proxy that checks credentials.

## Authentication

### Getting your Moodle token

1. Navigate to your Moodle token management page `https://{your-moodle-url}/user/managetoken.php`
2. Use the token with `Moodle mobile web service` in the `Service` column
3. Add this token to your `.env` file

### If `managetoken.php` is empty (SSO logins)

On sites that log you in through SSO (Shibboleth, SAML, CAS...), the token page is often empty and you can't create a token there. You can still get the same token the Moodle mobile app uses:

1. Log in to Moodle in your browser.
2. Open the developer tools (F12), go to the **Network** tab and turn on **Preserve log**.
3. In the same tab, open:

   ```
   https://{your-moodle-url}/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=12345&urlscheme=moodlemobile
   ```

4. The page redirects to a `moodlemobile://token=...` link that the browser can't open. Find that redirect in the Network list and copy the value after `token=` from its `Location` header.
5. Decode it:

   ```bash
   echo '<value-after-token=>' | base64 -d
   ```

   You get `<site-hash>:::<token>` or `<site-hash>:::<token>:::<private-token>`. The middle part is your `MOODLE_TOKEN`.
6. Check that it works:

   ```bash
   curl -s -d wstoken=<token> -d wsfunction=core_webservice_get_site_info -d moodlewsrestformat=json \
     https://{your-moodle-url}/webservice/rest/server.php
   ```

Notes:

- Keep `urlscheme=moodlemobile` exactly as written. Moodle rejects schemes with non-alphanumeric characters, and with an `https` scheme the browser lowercases the value, which breaks the base64.
- Browsers driven by automation tools usually can't read this redirect. Do it by hand.
- The token has the same rights as your account in the mobile app, including sending messages and posting in forums. Keep it private and don't commit it. Ignore the private token, the server doesn't need it.
- If the token leaks, remove it under **Preferences > Security keys** if your site shows that page, otherwise ask your Moodle admins to reset it.
- Check your institution's rules on API and token use before running it on a schedule.

## Development

```bash
uv run pytest                    # unit tests, recorded Moodle answers, no network
uv run python scripts/smoke.py   # read-only calls against the Moodle in .env, with timings
```
