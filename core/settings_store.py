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
    "telegram_command_polling_enabled": False,
    "scanner_universe_size": 500,
    "scanner_daily_scan_enabled": False,
    "scanner_weekly_scan_enabled": False,
    "scanner_daily_scan_time_et": "16:15",
    "scanner_weekly_scan_day": 6,
    "scanner_weekly_scan_time_et": "20:00",
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
