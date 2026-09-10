r"""
Local config for the Print Agent - one JSON file per Windows user, living in
%APPDATA%\ExpressPrintAgent\config.json. Holds only what this one installation
needs to talk to the backend and print: which tenant/agent it is, its API key,
and which locally-installed printer to send labels to.

Deliberately NOT stored in the program's own install folder - %APPDATA% is
writable without admin rights and survives a re-install/update of the exe.
"""

import json
import os

API_BASE_URL = "https://express-print-saas.onrender.com"

APP_NAME = "ExpressPrintAgent"
_CONFIG_DIR = os.path.join(os.environ["APPDATA"], APP_NAME)
CONFIG_PATH = os.path.join(_CONFIG_DIR, "config.json")

REQUIRED_KEYS = ("agent_id", "api_key", "printer_name")


def load_config() -> dict | None:
    if not os.path.exists(CONFIG_PATH):
        return None
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    if not all(data.get(key) for key in REQUIRED_KEYS):
        return None
    return data


def save_config(agent_id: str, api_key: str, printer_name: str) -> None:
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"agent_id": agent_id, "api_key": api_key, "printer_name": printer_name}, f)
