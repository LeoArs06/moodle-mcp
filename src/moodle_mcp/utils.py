import json
import os

from dotenv import load_dotenv

ENV_LOADED = False


def to_json_file(data, filename, folder=None):
    # Debug dumps of raw Moodle responses (may contain personal data).
    # Disabled unless MOODLE_MCP_DUMP_DIR is set, so the server works from
    # read-only working directories (e.g. MCP gateways running in containers).
    folder = folder or getenv("MOODLE_MCP_DUMP_DIR")
    if not folder:
        return

    os.makedirs(folder, exist_ok=True)

    with open(f"{folder}/{filename}", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def getenv(key: str, default: str = None) -> str:
    global ENV_LOADED

    if not ENV_LOADED:
        load_dotenv()
        ENV_LOADED = True

    return os.getenv(key, default)
