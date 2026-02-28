import json
import os
from typing import Any, Dict

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_SETTINGS_PATH = os.path.join(_DATA_DIR, "settings.json")

_DEFAULTS: Dict[str, Any] = {
    "poll_interval_seconds": 60,
    "cooldown_minutes": 30,
    "telegram_enabled": False,
    "telegram_token": "",
    "telegram_chat_id": "",
    "email_enabled": False,
    "email_smtp_host": "smtp.gmail.com",
    "email_smtp_port": 587,
    "email_username": "",
    "email_password": "",
    "email_to": "",
}


def _ensure_data_dir():
    os.makedirs(_DATA_DIR, exist_ok=True)


def load_settings() -> Dict[str, Any]:
    _ensure_data_dir()
    settings = dict(_DEFAULTS)
    if not os.path.exists(_SETTINGS_PATH):
        return settings
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        settings.update(saved)
    except (json.JSONDecodeError, ValueError):
        pass
    return settings


def save_settings(settings: Dict[str, Any]) -> None:
    _ensure_data_dir()
    with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
