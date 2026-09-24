"""Command line entry point for the Moodle MCP server.

Startup layout (argparse options, stderr-only logging with an optional log
file, secret-free startup banner, --health) follows erpipe-org/mcp-odoo (MIT).
"""

import argparse
import atexit
import json
import logging
import os
import platform
import signal
import sys
from importlib.metadata import PackageNotFoundError, version

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
TRANSPORTS = ("stdio", "streamable-http")
SECRET_SUFFIXES = ("_TOKEN", "_PASSWORD", "_API_KEY", "_SECRET")

logger = logging.getLogger("moodle-mcp")


def package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def setup_logging(level: str, log_file: str | None) -> None:
    """Log to stderr only (stdout carries the MCP protocol), plus an optional file."""
    level = level.upper() if level.upper() in LOG_LEVELS else "INFO"
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        try:
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as e:
            print(f"moodle-mcp: cannot open log file {log_file}: {e}", file=sys.stderr)

    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)


def log_startup(args: argparse.Namespace) -> None:
    logger.info(
        "Starting moodle-mcp %s (mcp %s, Python %s, %s) over %s",
        package_version("moodle-mcp"),
        package_version("mcp"),
        platform.python_version(),
        sys.platform,
        args.transport,
    )
    logger.info("cwd=%s pid=%s", os.getcwd(), os.getpid())
    for key in sorted(os.environ):
        if key.startswith(("MOODLE_", "MCP_")):
            secret = key.upper().endswith(SECRET_SUFFIXES)
            value = "***set***" if secret else os.environ[key]
            logger.info("env %s=%s", key, value)


def install_exit_logging() -> None:
    """Record why the process ends, so a closed connection can be explained."""

    def on_signal(signum, _frame):
        logger.warning("Received signal %s, shutting down", signal.Signals(signum).name)
        sys.exit(128 + signum)

    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), on_signal)

    atexit.register(lambda: logger.info("moodle-mcp process exiting"))


def health() -> int:
    """Print non-personal diagnostics as JSON and check the Moodle connection."""
    from .moodle import APIFunction, MOODLE_TOKEN, MOODLE_URL, MoodleAPIError, get_moodle_api_data

    payload: dict[str, object] = {
        "moodle_mcp": package_version("moodle-mcp"),
        "mcp": package_version("mcp"),
        "python": platform.python_version(),
        "cwd": os.getcwd(),
        "moodle_url": MOODLE_URL,
        "moodle_token_set": bool(MOODLE_TOKEN),
    }
    try:
        info = get_moodle_api_data(APIFunction.core_webservice_get_site_info)
        payload["moodle"] = {"ok": True, "release": info.get("release"), "sitename": info.get("sitename")}
    except MoodleAPIError as e:
        payload["moodle"] = {"ok": False, "error": str(e)}

    print(json.dumps(payload, indent=2))
    return 0 if payload["moodle"]["ok"] else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Moodle MCP server.")
    parser.add_argument(
        "--transport",
        choices=TRANSPORTS,
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        help="MCP transport. Defaults to MCP_TRANSPORT or stdio.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MCP_HTTP_HOST", "127.0.0.1"),
        help="Bind host for streamable-http.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MCP_HTTP_PORT", "8000")),
        help="Bind port for streamable-http.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("MOODLE_MCP_LOG_LEVEL", "INFO"),
        help="Log level (DEBUG, INFO, WARNING, ERROR).",
    )
    parser.add_argument(
        "--log-file",
        default=os.environ.get("MOODLE_MCP_LOG_FILE"),
        help="Also append logs to this file. Defaults to MOODLE_MCP_LOG_FILE.",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="Print diagnostics, check the Moodle connection and exit.",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    setup_logging(args.log_level, args.log_file)

    if args.health:
        return health()

    log_startup(args)
    install_exit_logging()

    try:
        from .server import mcp

        if args.transport == "stdio":
            mcp.run()
        else:
            mcp.run(transport="streamable-http", host=args.host, port=args.port)
    except KeyboardInterrupt:
        logger.info("Interrupted")
    except Exception:
        logger.exception("moodle-mcp crashed")
        return 1

    logger.info("MCP session ended (stdin closed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
