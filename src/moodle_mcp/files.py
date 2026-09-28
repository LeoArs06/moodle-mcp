"""Listing, reading and downloading files attached to Moodle courses.

Files are fetched from `/webservice/pluginfile.php` with the web service
token sent in the POST body, so it never appears in a URL. Only URLs on the
Moodle host from MOODLE_URL are accepted: course pages also link to external
sites, and the token must never be sent there.
"""

import base64
import io
import json
import logging
import mimetypes
import os
import re
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import requests
from mcp_types import BlobResourceContents, EmbeddedResource
from typing_extensions import TypedDict

from .logger import logger
from .moodle import (
    MOODLE_TOKEN,
    MOODLE_URL,
    APIFunction,
    MoodleAPIError,
    _redact,
    get_moodle_api_data,
)
from .utils import getenv

PLUGINFILE_PATH = "/webservice/pluginfile.php"
DEFAULT_MAX_CHARS = 40_000
MAX_DOWNLOAD_MB = float(getenv("MOODLE_MAX_DOWNLOAD_MB", "10"))
DOWNLOAD_DIR = getenv("MOODLE_DOWNLOAD_DIR")

TEXT_MIMETYPES = ("text/", "application/json", "application/xml")

# pypdf logs a warning per font it cannot fully decode; the text is still usable.
logging.getLogger("pypdf").setLevel(logging.ERROR)


class CourseFile(TypedDict):
    section: str
    module_id: int
    module_name: str
    modname: str
    filename: str
    filesize: int
    mimetype: str | None
    timemodified: int | None
    fileurl: str
    external: bool


class FileText(TypedDict):
    filename: str
    mimetype: str | None
    total_pages: int | None
    pages: str | None
    truncated: bool
    next_pages: str | None
    text: str


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------


def _moodle_base() -> tuple[str, str, str]:
    """Return (scheme, host, site path prefix) from MOODLE_URL."""
    if not MOODLE_URL or not MOODLE_TOKEN:
        raise MoodleAPIError(
            "config_error",
            "MOODLE_URL and MOODLE_TOKEN environment variables must be set",
            "pluginfile",
        )
    parts = urlsplit(MOODLE_URL)
    # MOODLE_URL is <site>/webservice/rest/server.php; the site may live in a subpath.
    prefix = parts.path.split("/webservice/")[0]
    return parts.scheme, parts.netloc.lower(), prefix


def normalize_file_url(fileurl: str) -> str:
    """Validate a Moodle file URL and return the canonical pluginfile URL.

    Accepts both /pluginfile.php and /webservice/pluginfile.php links, drops
    query parameters (token, forcedownload) and re-encodes the path, since
    copied URLs often come back with %20 or %5B already decoded.
    """
    scheme, host, prefix = _moodle_base()
    parts = urlsplit(fileurl.strip())

    if parts.netloc.lower() != host:
        raise MoodleAPIError(
            "invalid_url",
            f"Only files hosted on {host} can be fetched; got '{parts.netloc or fileurl}'."
            " External links (modname 'url') must be opened directly.",
            "pluginfile",
        )

    path = unquote(parts.path)
    for marker in (PLUGINFILE_PATH, "/pluginfile.php"):
        start = path.find(marker + "/")
        if start != -1 and path[:start] == prefix:
            file_path = path[start + len(marker):]
            break
    else:
        raise MoodleAPIError(
            "invalid_url",
            "Not a Moodle file URL (expected .../pluginfile.php/...). Use the"
            " 'fileurl' returned by list_course_files.",
            "pluginfile",
        )

    if "/../" in file_path + "/":
        raise MoodleAPIError("invalid_url", "Path traversal is not allowed", "pluginfile")

    return urlunsplit((scheme, host, prefix + PLUGINFILE_PATH + quote(file_path), "", ""))


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def fetch_file(fileurl: str, max_bytes: int | None = None) -> tuple[bytes, str | None, str]:
    """Download a file. Returns (content, mimetype, filename)."""
    url = normalize_file_url(fileurl)
    max_bytes = max_bytes or int(MAX_DOWNLOAD_MB * 1024 * 1024)
    filename = unquote(url.rsplit("/", 1)[-1])

    logger.info(f"Downloading file {filename}")
    try:
        rsp = requests.post(url, data={"token": MOODLE_TOKEN}, timeout=60, stream=True)
    except requests.RequestException as e:
        raise MoodleAPIError("network_error", _redact(str(e)), "pluginfile") from None

    with rsp:
        if rsp.status_code != 200:
            raise MoodleAPIError(
                "http_error", f"HTTP {rsp.status_code} for {filename}", "pluginfile"
            )

        declared = rsp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise MoodleAPIError(
                "file_too_large",
                f"{filename} is {int(declared) / 1_048_576:.1f} MB, limit is"
                f" {max_bytes / 1_048_576:.1f} MB (MOODLE_MAX_DOWNLOAD_MB)",
                "pluginfile",
            )

        chunks, size = [], 0
        for chunk in rsp.iter_content(64 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise MoodleAPIError(
                    "file_too_large",
                    f"{filename} exceeds {max_bytes / 1_048_576:.1f} MB (MOODLE_MAX_DOWNLOAD_MB)",
                    "pluginfile",
                )
            chunks.append(chunk)
        content = b"".join(chunks)
        mimetype = (rsp.headers.get("Content-Type") or "").split(";")[0].strip() or None

    # Moodle reports errors (bad token, missing file) as JSON with HTTP 200.
    if mimetype == "application/json" or content[:1] == b"{":
        try:
            data = json.loads(content)
        except ValueError:
            data = None
        if isinstance(data, dict) and "errorcode" in data:
            raise MoodleAPIError(
                data.get("errorcode", "unknown"),
                data.get("error") or data.get("message") or "Unknown Moodle error",
                "pluginfile",
            )

    if not mimetype or mimetype == "application/octet-stream":
        mimetype = mimetypes.guess_type(filename)[0] or mimetype

    return content, mimetype, filename


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "table"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.parts.append(f" [{href}] ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _parse_pages(pages: str | None, total: int) -> list[int]:
    """Parse '1-5,8' into zero-based page indexes, clamped to the document."""
    if not pages:
        return list(range(total))
    result: list[int] = []
    for part in pages.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+)?)?", part)
        if not m:
            raise MoodleAPIError(
                "invalid_pages", f"Invalid page range '{part}', use e.g. '1-5,8'", "read_course_file"
            )
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else (total if part.endswith("-") else start)
        result.extend(i - 1 for i in range(max(start, 1), min(end, total) + 1))
    return sorted(set(result))


def _format_ranges(indexes: list[int]) -> str:
    ranges, start = [], None
    for i, idx in enumerate(indexes):
        if start is None:
            start = idx
        if i + 1 == len(indexes) or indexes[i + 1] != idx + 1:
            ranges.append(f"{start + 1}" if start == idx else f"{start + 1}-{idx + 1}")
            start = None
    return ",".join(ranges)


def pdf_to_text(content: bytes, pages: str | None, max_chars: int) -> dict:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            reader.decrypt("")
        total = len(reader.pages)
    except Exception as e:
        raise MoodleAPIError("invalid_pdf", f"Cannot read PDF: {e}", "read_course_file") from None

    wanted = _parse_pages(pages, total)
    parts: list[str] = []
    done: list[int] = []
    used = 0
    for idx in wanted:
        try:
            text = (reader.pages[idx].extract_text() or "").strip()
        except Exception as e:
            text = f"[text extraction failed: {e}]"
        block = f"--- page {idx + 1} ---\n" + (text or "[no text layer: scanned page or image only]")
        if parts and used + len(block) > max_chars:
            break
        parts.append(block[:max_chars] if not parts else block)
        used += len(block)
        done.append(idx)

    remaining = [i for i in wanted if i not in set(done)]
    return {
        "total_pages": total,
        "pages": _format_ranges(done) if done else None,
        "truncated": bool(remaining) or used > max_chars,
        "next_pages": _format_ranges(remaining) if remaining else None,
        "text": "\n\n".join(parts),
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def list_course_files(courseid: int) -> list[CourseFile]:
    data = get_moodle_api_data(
        APIFunction.core_course_get_contents,
        params={"courseid": str(courseid)},
    )

    files: list[CourseFile] = []
    for section in data:
        for module in section.get("modules") or []:
            for content in module.get("contents") or []:
                fileurl = content.get("fileurl")
                if not fileurl:
                    continue
                external = content.get("type") == "url" or bool(content.get("isexternalfile"))
                filename = content.get("filename") or ""
                if content.get("filepath") not in (None, "/"):
                    filename = content["filepath"].lstrip("/") + filename
                files.append(
                    {
                        "section": section.get("name") or "",
                        "module_id": module.get("id"),
                        "module_name": module.get("name") or "",
                        "modname": module.get("modname") or "",
                        "filename": filename,
                        "filesize": content.get("filesize") or 0,
                        "mimetype": content.get("mimetype")
                        or (None if external else mimetypes.guess_type(filename)[0]),
                        "timemodified": content.get("timemodified"),
                        "fileurl": fileurl,
                        "external": external,
                    }
                )

    logger.info(f"Found {len(files)} files in course {courseid}")
    return files


def read_course_file(
    fileurl: str, pages: str | None = None, max_chars: int = DEFAULT_MAX_CHARS
) -> FileText:
    content, mimetype, filename = fetch_file(fileurl)
    max_chars = max(1000, max_chars)

    is_pdf = mimetype == "application/pdf" or content[:5] == b"%PDF-"
    if is_pdf:
        result = pdf_to_text(content, pages, max_chars)
        return {"filename": filename, "mimetype": "application/pdf", **result}

    if mimetype and (mimetype.startswith(TEXT_MIMETYPES) or mimetype.endswith("+xml")):
        text = content.decode("utf-8", errors="replace")
        if mimetype == "text/html" or filename.endswith((".html", ".htm")):
            text = html_to_text(text)
        return {
            "filename": filename,
            "mimetype": mimetype,
            "total_pages": None,
            "pages": None,
            "truncated": len(text) > max_chars,
            "next_pages": None,
            "text": text[:max_chars],
        }

    raise MoodleAPIError(
        "unsupported_type",
        f"Cannot extract text from {filename} ({mimetype or 'unknown type'})."
        " Only PDF, HTML and plain text are supported; use download_course_file"
        " to get the raw file.",
        "read_course_file",
    )


def _safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "file"


def download_course_file(fileurl: str) -> list:
    """Return the raw file as an embedded resource, and save it to
    MOODLE_DOWNLOAD_DIR when that is set."""
    content, mimetype, filename = fetch_file(fileurl)
    url = normalize_file_url(fileurl)
    result: list = []

    if DOWNLOAD_DIR:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        # Prefix with the context id so same-named files from different courses don't collide.
        context_id = url.split(PLUGINFILE_PATH + "/", 1)[1].split("/", 1)[0]
        path = os.path.join(DOWNLOAD_DIR, f"{context_id}_{_safe_filename(filename)}")
        with open(path, "wb") as f:
            f.write(content)
        logger.info(f"Saved {filename} ({len(content)} bytes)")
        result.append(f"Saved {filename} ({len(content)} bytes) to {os.path.abspath(path)}")

    result.append(
        EmbeddedResource(
            type="resource",
            resource=BlobResourceContents(
                uri=url,
                mime_type=mimetype or "application/octet-stream",
                blob=base64.b64encode(content).decode("ascii"),
            ),
        )
    )
    return result
