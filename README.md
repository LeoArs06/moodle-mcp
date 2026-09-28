[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/loyaniu-moodle-mcp-badge.png)](https://mseep.ai/app/loyaniu-moodle-mcp)

# Moodle-MCP

> A Model Context Protocol (MCP) server implementation that provides capabilities to interact with Moodle LMS.

## Features

The server exposes the following tools.

### Courses & content

| Tool | Description |
| --- | --- |
| `get_my_courses` | Get all courses the current user is enrolled in |
| `get_course_content` | Get sections and modules for a specific course by its ID |
| `search_course_materials` | Search across all course materials by query string |
| `get_course_announcements` | Get announcements from course news forums, optionally filtered by course ID |
| `get_recent_activity` | Get recent activity and updates across courses since a given time |

### Files

| Tool | Description |
| --- | --- |
| `list_course_files` | List files in a course (resources, folders, pages) with `fileurl`, size and type, optionally filtered by `query` or `mimetype`; external links are flagged |
| `read_course_file` | Download a file and return its text: PDF (page by page, with ranges like `1-10`), HTML pages, plain text |
| `download_course_file` | Return the raw file as an embedded binary resource, and save it to `MOODLE_DOWNLOAD_DIR` when set |

`read_course_file` is the one to use from remote clients (claude.ai, Cowork, MetaMCP): the text goes straight into the conversation. Scanned PDFs have no text layer; those pages come back as `[no text layer: ...]`.

Files are only fetched from the Moodle host in `MOODLE_URL`, with the token sent in the POST body, so links to other sites never receive it.

### Assignments & deadlines

| Tool | Description |
| --- | --- |
| `get_assignments` | Get assignments for courses, optionally filtered by course IDs |
| `get_assignment_status` | Get submission and grading status for a specific assignment |
| `get_upcoming_deadlines` | Get upcoming assignment deadlines across all courses, sorted by due date |
| `get_overdue_assignments` | Get unsubmitted assignments past their due date, most overdue first |
| `get_actionable_tasks` | Get a prioritized list of tasks needing action, sorted by urgency |
| `analyze_assignment` | Analyze an assignment: status, requirements, materials, progress, deadline |
| `extract_assignment_requirements` | Extract requirements, deliverables, constraints, and evaluation criteria from an assignment |
| `find_relevant_materials` | Find course content relevant to an assignment, ranked by relevance |
| `decompose_task` | Break an assignment into subtasks with effort, dependencies, and critical path |
| `create_implementation_plan` | Build a step-by-step plan with timeline, resources, milestones, and risks |

### Grades & progress

| Tool | Description |
| --- | --- |
| `get_grades` | Get a grade overview for all courses, or detailed grades for one course |
| `get_course_progress` | Get progress and completion for one course or all courses |
| `get_course_health` | Health check for a course: progress, grades, unsubmitted and overdue counts |
| `get_study_load` | Analyze assignment distribution by week to identify heavy weeks |

### Aggregated overviews

| Tool | Description |
| --- | --- |
| `get_upcoming_events` | Get upcoming events from Moodle |
| `semester_dashboard` | Combined overview of courses, upcoming deadlines, and grades |
| `daily_briefing` | Daily summary of overdue count, today's deadlines, recent grades, events, and tasks |
| `weekly_review` | Weekly summary of submitted/graded counts, deadlines, overdue count, and progress |
| `ask_moodle` | Ask a natural language question and have it routed to the right data sources |

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

The server writes nothing to the working directory, so it runs from read-only containers. Files returned by `download_course_file` are saved on the MetaMCP host, not on your computer, so remote clients should use `read_course_file` instead.

To check the setup from the MetaMCP host:

```bash
MOODLE_URL=... MOODLE_TOKEN=... uvx --from <zip-url> moodle-mcp --health
```

### Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `MOODLE_URL` | (required) | `https://{your-moodle-url}/webservice/rest/server.php` |
| `MOODLE_TOKEN` | (required) | Web service token, see [Authentication](#authentication) |
| `MOODLE_MAX_DOWNLOAD_MB` | `10` | Largest file `read_course_file` / `download_course_file` will fetch |
| `MOODLE_DOWNLOAD_DIR` | unset | If set, `download_course_file` also saves files here |
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
