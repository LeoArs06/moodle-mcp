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
import zipfile
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from mcp_types import BlobResourceContents, EmbeddedResource, ImageContent
from typing_extensions import TypedDict

from . import cache
from .logger import logger
from .moodle import (
    MOODLE_TOKEN,
    MOODLE_URL,
    APIFunction,
    TIMEOUT,
    MoodleAPIError,
    get_moodle_api_data,
    post_with_retry,
    response_hooks,
)
from .utils import getenv

PLUGINFILE_PATH = "/webservice/pluginfile.php"
DEFAULT_MAX_CHARS = 20_000
MAX_RENDER_PAGES = 8
DEFAULT_DPI = 110
# Longest side of rendered pages; larger images get downscaled by Claude anyway.
RENDER_LONG_SIDE = 1568
# Total JPEG bytes per view_course_file_pages call; fewer pages are returned
# when large or detailed pages would exceed it.
MAX_RENDER_BYTES = 3_000_000
# A page with fewer letters and digits than this has no usable text layer.
MIN_PAGE_CHARS = 50
# Share of such pages above which a PDF counts as scanned or handwritten.
# Slides often have a few near-empty pages; handwritten notes have almost all.
NO_TEXT_SHARE = 0.6
# Pages spread over the document that are also checked for that decision.
SAMPLE_PAGES = 5
MAX_DOWNLOAD_MB = float(getenv("MOODLE_MAX_DOWNLOAD_MB", "20"))
# Raw blobs go into the model context as base64 (+33%), so keep them smaller.
MAX_BLOB_MB = 10
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


class ZipEntry(TypedDict):
    path: str
    size: int
    compressed_size: int
    mimetype: str | None


class FileText(TypedDict):
    pages_total: int | None
    has_text_layer: bool | str | None
    next_pages: str | None
    filename: str
    mimetype: str | None
    pages: str | None
    truncated: bool
    pages_without_text: str | None
    hint: str | None
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

# timemodified of each file seen in course contents, keyed by its path after
# pluginfile.php, so that a cached copy is dropped when the file changes.
_file_versions: dict[str, int] = {}


def _file_key(fileurl: str) -> str:
    path = unquote(urlsplit(fileurl).path)
    return path.split("pluginfile.php", 1)[-1]


def _remember_file_versions(sections) -> None:
    for section in sections if isinstance(sections, list) else []:
        for module in section.get("modules") or []:
            for content in module.get("contents") or []:
                if content.get("fileurl") and content.get("timemodified"):
                    _file_versions[_file_key(content["fileurl"])] = content["timemodified"]


response_hooks.setdefault(APIFunction.core_course_get_contents, []).append(_remember_file_versions)


def fetch_file(fileurl: str, max_bytes: int | None = None) -> tuple[bytes, str | None, str]:
    """Download a file, or take it from the disk cache. Returns (content, mimetype, filename)."""
    url = normalize_file_url(fileurl)
    max_bytes = max_bytes or int(MAX_DOWNLOAD_MB * 1024 * 1024)
    filename = unquote(url.rsplit("/", 1)[-1])

    cache_key = f"{url}@{_file_versions.get(_file_key(url), '')}"
    hit = cache.get("files", cache_key, cache.FILES_TTL)
    if hit and len(hit[0]) <= max_bytes:
        logger.info(f"Using cached file {filename}")
        return hit[0], hit[1].get("mimetype"), filename

    content, mimetype = _download(url, filename, max_bytes)
    cache.put("files", cache_key, content, {"mimetype": mimetype})
    return content, mimetype, filename


def _download(url: str, filename: str, max_bytes: int) -> tuple[bytes, str | None]:
    logger.info(f"Downloading file {filename}")
    # No redirects: requests re-sends the POST body (and the token) on 307/308,
    # possibly to another host.
    rsp = post_with_retry(
        url, {"token": MOODLE_TOKEN}, "pluginfile", timeout=2 * TIMEOUT, stream=True, allow_redirects=False
    )

    with rsp:
        if rsp.is_redirect:
            raise MoodleAPIError(
                "redirect",
                f"Moodle redirected the download of {filename}; the file may need a"
                " browser login or live on another site",
                "pluginfile",
            )
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

    return content, mimetype


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


def _meaningful_chars(text: str) -> int:
    """Letters and digits, ignoring OCR noise such as private-use glyphs."""
    if not text:
        return 0
    junk = sum(1 for c in text if c == "\ufffd" or "\ue000" <= c <= "\uf8ff")
    if junk > 0.3 * len(text):
        return 0
    return sum(1 for c in text if c.isalnum())


def pdf_to_text(content: bytes, pages: str | None, max_chars: int) -> dict:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            reader.decrypt("")
        total = len(reader.pages)
    except Exception as e:
        raise MoodleAPIError("invalid_pdf", f"Cannot read PDF: {e}", "read_course_file") from None

    texts: dict[int, str] = {}

    def page_text(idx: int) -> str:
        if idx not in texts:
            try:
                texts[idx] = (reader.pages[idx].extract_text() or "").strip()
            except Exception as e:
                texts[idx] = f"[text extraction failed: {e}]"
        return texts[idx]

    wanted = _parse_pages(pages, total)
    parts: list[str] = []
    done: list[int] = []
    empty: list[int] = []
    used = 0
    for idx in wanted:
        text = page_text(idx)
        if _meaningful_chars(text) < MIN_PAGE_CHARS:
            empty.append(idx)
            block = f"--- page {idx + 1} ---\n[little or no text: image, diagram, scan or handwriting]"
        else:
            block = f"--- page {idx + 1} ---\n{text}"
        if parts and used + len(block) > max_chars:
            break
        parts.append(block[:max_chars] if not parts else block)
        used += len(block)
        done.append(idx)

    remaining = [i for i in wanted if i not in set(done)]
    empty = [i for i in empty if i in set(done)]
    if remaining:
        next_pages = _format_ranges(remaining)
    elif done and done[-1] + 1 < total:
        # Everything requested was returned; point at the rest of the document.
        next_pages = f"{done[-1] + 2}-{total}"
    else:
        next_pages = None
    result = {
        "pages_total": total,
        "has_text_layer": True,
        "next_pages": next_pages,
        "pages": _format_ranges(done) if done else None,
        "truncated": bool(remaining) or used > max_chars,
        "pages_without_text": _format_ranges(empty) if empty else None,
        "hint": None,
        "text": "\n\n".join(parts),
    }

    # Judge the whole document, not just the requested pages: a slide deck
    # read from its title page would otherwise look handwritten.
    sample = set(done) | {round(i * (total - 1) / (SAMPLE_PAGES - 1)) for i in range(SAMPLE_PAGES)}
    sample_empty = [i for i in sample if _meaningful_chars(page_text(i)) < MIN_PAGE_CHARS]
    if done and empty and len(sample_empty) >= NO_TEXT_SHARE * len(sample):
        # Mostly scans or handwriting: the few characters found are headers or
        # OCR noise, so they are not returned.
        suggested = _format_ranges(empty[:MAX_RENDER_PAGES])
        result.update(
            has_text_layer=False,
            text="",
            hint=(
                f"{len(empty)} of {len(done)} pages have little or no extractable text"
                " (probably scanned or handwritten). Read them as images with"
                f" view_course_file_pages, pages='{suggested}'."
            ),
        )
    elif empty:
        result.update(
            has_text_layer="partial",
            hint=(
                "Pages listed in pages_without_text are images, diagrams or scans;"
                " use view_course_file_pages if their content matters."
            ),
        )
    return result


# ---------------------------------------------------------------------------
# Zip archives (read in memory only, nothing is extracted to disk)
# ---------------------------------------------------------------------------


def _zip_name(info: zipfile.ZipInfo) -> str:
    # Without the UTF-8 flag, zipfile decodes names as cp437; archives made on
    # Windows or macOS often hold UTF-8 names anyway (accents come out garbled).
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def _open_zip(content: bytes, filename: str) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise MoodleAPIError(
            "unsupported_type", f"{filename} is not a zip archive", "zip"
        ) from None


def _zip_entries(archive: zipfile.ZipFile) -> list[tuple[str, zipfile.ZipInfo]]:
    return [
        (_zip_name(info), info)
        for info in archive.infolist()
        if not info.is_dir() and not _zip_name(info).startswith("__MACOSX/")
    ]


def _zip_member(content: bytes, filename: str, inner_path: str) -> tuple[bytes, str | None, str]:
    """Return (content, mimetype, name) of one file inside a zip archive."""
    archive = _open_zip(content, filename)
    entries = _zip_entries(archive)
    wanted = inner_path.strip().replace("\\", "/").lstrip("/")

    matches = [e for e in entries if e[0] == wanted]
    if not matches:
        # Accept a bare file name or different case when it is unambiguous.
        low = wanted.lower()
        matches = [e for e in entries if e[0].lower() == low] or [
            e for e in entries if e[0].rsplit("/", 1)[-1].lower() == low
        ]
    if len(matches) != 1:
        problem = "not found in" if not matches else "is ambiguous in"
        raise MoodleAPIError(
            "invalid_path",
            f"'{inner_path}' {problem} {filename}; use a path from list_zip_contents",
            "zip",
        )

    name, info = matches[0]
    max_bytes = int(MAX_DOWNLOAD_MB * 1024 * 1024)
    too_large = MoodleAPIError(
        "file_too_large",
        f"{name} unpacks to more than {MAX_DOWNLOAD_MB:g} MB (MOODLE_MAX_DOWNLOAD_MB)",
        "zip",
    )
    if info.file_size > max_bytes:
        raise too_large
    # The header size can lie (zip bombs), so also cap what is actually read.
    try:
        with archive.open(info) as f:
            data = f.read(max_bytes + 1)
    except (RuntimeError, NotImplementedError, zipfile.BadZipFile) as e:
        # RuntimeError: encrypted member; NotImplementedError: unsupported compression.
        raise MoodleAPIError("unsupported_type", f"Cannot unpack {name}: {e}", "zip") from None
    if len(data) > max_bytes:
        raise too_large

    return data, mimetypes.guess_type(name)[0], name.rsplit("/", 1)[-1]


def _fetch(fileurl: str, inner_path: str | None) -> tuple[bytes, str | None, str]:
    content, mimetype, filename = fetch_file(fileurl)
    if inner_path:
        return _zip_member(content, filename, inner_path)
    return content, mimetype, filename


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def list_course_files(
    courseid: int, query: str | None = None, mimetype: str | None = None
) -> list[CourseFile]:
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

    if query:
        words = query.lower().split()
        files = [
            f
            for f in files
            if all(w in f"{f['filename']} {f['module_name']} {f['section']}".lower() for w in words)
        ]
    if mimetype:
        files = [f for f in files if (f["mimetype"] or "").startswith(mimetype.lower())]

    logger.info(f"Found {len(files)} files in course {courseid}")
    return files


def _looks_like_text(content: bytes) -> bool:
    """Source code and other files with no known mimetype: UTF-8 without NUL bytes."""
    sample = content[:8192]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as e:
        # A multibyte character cut at the end of the sample is fine.
        return e.start >= len(sample) - 3
    return True


def list_zip_contents(fileurl: str) -> list[ZipEntry]:
    content, _, filename = fetch_file(fileurl)
    archive = _open_zip(content, filename)
    return [
        {
            "path": name,
            "size": info.file_size,
            "compressed_size": info.compress_size,
            "mimetype": mimetypes.guess_type(name)[0],
        }
        for name, info in _zip_entries(archive)
    ]


def read_course_file(
    fileurl: str,
    pages: str | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    inner_path: str | None = None,
) -> FileText:
    content, mimetype, filename = _fetch(fileurl, inner_path)
    max_chars = max(1000, max_chars)

    is_pdf = mimetype == "application/pdf" or content[:5] == b"%PDF-"
    if is_pdf:
        result = pdf_to_text(content, pages, max_chars)
        # Paging fields first, so they are read before the text.
        return {
            "pages_total": result.pop("pages_total"),
            "has_text_layer": result.pop("has_text_layer"),
            "next_pages": result.pop("next_pages"),
            "filename": filename,
            "mimetype": "application/pdf",
            **result,
        }

    if (mimetype and (mimetype.startswith(TEXT_MIMETYPES) or mimetype.endswith("+xml"))) or (
        not mimetype and _looks_like_text(content)
    ):
        mimetype = mimetype or "text/plain"
        text = content.decode("utf-8", errors="replace")
        if mimetype == "text/html" or filename.endswith((".html", ".htm")):
            text = html_to_text(text)
        return {
            "pages_total": None,
            "has_text_layer": None,
            "next_pages": None,
            "filename": filename,
            "mimetype": mimetype,
            "pages": None,
            "truncated": len(text) > max_chars,
            "pages_without_text": None,
            "hint": None,
            "text": text[:max_chars],
        }

    if mimetype == "application/zip" or content[:4] == b"PK\x03\x04":
        hint = "call list_zip_contents, then pass one of its paths as inner_path"
    else:
        hint = "use download_course_file to get the raw file"
    raise MoodleAPIError(
        "unsupported_type",
        f"Cannot extract text from {filename} ({mimetype or 'unknown type'})."
        f" Only PDF, HTML and plain text are supported; {hint}.",
        "read_course_file",
    )


def view_course_file_pages(
    fileurl: str,
    pages: str = "1-3",
    inner_path: str | None = None,
    dpi: int = DEFAULT_DPI,
    grayscale: bool = True,
) -> list:
    """Render PDF pages as JPEG images, for scanned or handwritten documents."""
    import pypdfium2 as pdfium

    dpi = min(max(dpi, 50), 200)

    content, mimetype, filename = _fetch(fileurl, inner_path)
    if not (mimetype == "application/pdf" or content[:5] == b"%PDF-"):
        raise MoodleAPIError(
            "unsupported_type",
            f"{filename} is not a PDF ({mimetype or 'unknown type'})",
            "view_course_file_pages",
        )

    try:
        pdf = pdfium.PdfDocument(content)
    except pdfium.PdfiumError as e:
        raise MoodleAPIError("invalid_pdf", f"Cannot read PDF: {e}", "view_course_file_pages") from None

    try:
        total = len(pdf)
        wanted = _parse_pages(pages, total)
        shown, rest = wanted[:MAX_RENDER_PAGES], wanted[MAX_RENDER_PAGES:]
        if not shown:
            raise MoodleAPIError(
                "invalid_pages", f"No pages in range '{pages}' ({total} pages)", "view_course_file_pages"
            )

        images: list[tuple[int, bytes]] = []
        used = 0
        for idx in shown:
            page = pdf[idx]
            width, height = page.get_size()
            # PDF sizes are in points (1/72 inch).
            scale = min(dpi / 72, RENDER_LONG_SIDE / max(width, height, 1))
            image = page.render(scale=scale, grayscale=grayscale).to_pil()
            image = image.convert("L" if grayscale else "RGB")
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=80, optimize=True)
            if images and used + buf.tell() > MAX_RENDER_BYTES:
                break
            images.append((idx, buf.getvalue()))
            used += buf.tell()

        rendered = [idx for idx, _ in images]
        rest = [i for i in wanted if i not in set(rendered)]
        summary = f"{filename}: pages {_format_ranges(rendered)} of {total}"
        if rest:
            summary += (
                f". At most {MAX_RENDER_PAGES} pages (or {MAX_RENDER_BYTES // 1_000_000} MB) per call;"
                f" continue with pages='{_format_ranges(rest)}'"
            )
        result: list = [summary]
        for idx, data in images:
            result.append(f"--- page {idx + 1} ---")
            result.append(
                ImageContent(
                    type="image",
                    data=base64.b64encode(data).decode("ascii"),
                    mime_type="image/jpeg",
                )
            )
        return result
    finally:
        pdf.close()


def _safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "file"


def _file_summary(content: bytes, mimetype: str | None, filename: str) -> dict:
    """Metadata and next steps for a file too large to return as a blob."""
    summary: dict = {
        "filename": filename,
        "mimetype": mimetype,
        "size_mb": round(len(content) / 1_048_576, 1),
        "returned_inline": False,
        "reason": f"larger than {MAX_BLOB_MB} MB",
    }
    if mimetype == "application/pdf" or content[:5] == b"%PDF-":
        try:
            from pypdf import PdfReader

            summary["pages_total"] = len(PdfReader(io.BytesIO(content)).pages)
        except Exception:
            pass
        summary["next_step"] = (
            "read_course_file with pages='1-10' (then next_pages), or"
            " view_course_file_pages for scanned or handwritten pages"
        )
    elif mimetype == "application/zip" or content[:4] == b"PK\x03\x04":
        summary["next_step"] = "list_zip_contents, then read one file with inner_path"
    else:
        summary["next_step"] = "read_course_file if it is text, PDF or HTML"
    return summary


def download_course_file(fileurl: str) -> list:
    """Return the raw file as an embedded resource, and save it to
    MOODLE_DOWNLOAD_DIR when that is set."""
    content, mimetype, filename = fetch_file(fileurl)
    url = normalize_file_url(fileurl)
    result: list = []
    blob_too_large = len(content) > MAX_BLOB_MB * 1024 * 1024

    if DOWNLOAD_DIR:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        # Prefix with the context id so same-named files from different courses don't collide.
        context_id = url.split(PLUGINFILE_PATH + "/", 1)[1].split("/", 1)[0]
        path = os.path.join(DOWNLOAD_DIR, f"{context_id}_{_safe_filename(filename)}")
        with open(path, "wb") as f:
            f.write(content)
        logger.info(f"Saved {filename} ({len(content)} bytes)")
        result.append(f"Saved {filename} ({len(content)} bytes) to {os.path.abspath(path)}")

    if blob_too_large:
        result.append(json.dumps(_file_summary(content, mimetype, filename), ensure_ascii=False))
        return result

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
