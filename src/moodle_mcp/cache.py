"""Small disk cache for course contents and downloaded files.

Reading a long PDF takes several read_course_file calls, and each one used to
download the whole file again. Entries expire after a TTL; files are also keyed
by their timemodified when known, so a new version is fetched as soon as the
course page reports it.

The cache holds course material and personal data, so it lives in the user's
cache directory. MOODLE_MCP_CACHE_DIR="" disables it. Any disk error only
disables caching for that call.
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from .logger import logger
from .utils import getenv

FILES_TTL = float(getenv("MOODLE_MCP_CACHE_TTL_HOURS", "24")) * 3600
# Course pages change during the day (new lesson material), so they expire sooner.
CONTENTS_TTL = float(getenv("MOODLE_MCP_CONTENTS_TTL_MINUTES", "60")) * 60
MAX_BYTES = int(float(getenv("MOODLE_MCP_CACHE_MAX_MB", "500")) * 1024 * 1024)


def _default_dir() -> Path:
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "moodle-mcp" / "cache"
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "moodle-mcp"


def cache_dir() -> Path | None:
    configured = getenv("MOODLE_MCP_CACHE_DIR")
    if configured == "":
        return None
    return Path(configured) if configured else _default_dir()


def _path(namespace: str, key: str) -> Path | None:
    root = cache_dir()
    if root is None:
        return None
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:40]
    return root / namespace / digest


def get(namespace: str, key: str, ttl: float) -> tuple[bytes, dict] | None:
    """Return (content, metadata) if a fresh entry exists."""
    path = _path(namespace, key)
    if path is None:
        return None
    data, meta = path.with_suffix(".bin"), path.with_suffix(".json")
    try:
        if time.time() - data.stat().st_mtime > ttl:
            data.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            return None
        return data.read_bytes(), json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def put(namespace: str, key: str, content: bytes, metadata: dict | None = None) -> None:
    path = _path(namespace, key)
    if path is None or len(content) > MAX_BYTES:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(content)
        path.with_suffix(".json").write_text(json.dumps(metadata or {}), encoding="utf-8")
        os.replace(tmp, path.with_suffix(".bin"))
        _evict()
    except OSError as e:
        logger.warning(f"Cache write failed, continuing without cache: {e}")


def _evict() -> None:
    """Delete the oldest entries while the cache is over MAX_BYTES."""
    root = cache_dir()
    entries = sorted(root.glob("*/*.bin"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in entries)
    for entry in entries:
        if total <= MAX_BYTES:
            break
        total -= entry.stat().st_size
        entry.unlink(missing_ok=True)
        entry.with_suffix(".json").unlink(missing_ok=True)
