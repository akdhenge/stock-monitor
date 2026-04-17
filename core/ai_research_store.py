"""
Cache for AI research results — stored in data/ai_research_cache.json.
Entries expire after 6 hours.
"""
import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_CACHE_PATH = os.path.join(_DATA_DIR, "ai_research_cache.json")
_TTL_HOURS = 6


def _ensure_data_dir() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)


def _load_cache() -> Dict[str, Any]:
    _ensure_data_dir()
    if not os.path.exists(_CACHE_PATH):
        return {}
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError, OSError):
        return {}


def _save_cache(cache: Dict[str, Any]) -> None:
    _ensure_data_dir()
    with open(_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def get_cached_entry(symbol: str) -> Optional[Dict[str, Any]]:
    """Return cached research entry for symbol if it is < 6 hours old, else None."""
    cache = _load_cache()
    entry = cache.get(symbol)
    if entry is None:
        return None
    try:
        ts = datetime.fromisoformat(entry["timestamp"])
        if datetime.now() - ts < timedelta(hours=_TTL_HOURS):
            return entry
    except (KeyError, ValueError, TypeError):
        pass
    return None


def get_cached_symbols() -> set:
    """Return set of symbols that have a valid (non-expired) cache entry."""
    cache = _load_cache()
    now = datetime.now()
    result = set()
    for symbol, entry in cache.items():
        try:
            ts = datetime.fromisoformat(entry["timestamp"])
            if now - ts < timedelta(hours=_TTL_HOURS):
                result.add(symbol)
        except (KeyError, ValueError, TypeError):
            pass
    return result


def save_entry(symbol: str, entry: Dict[str, Any]) -> None:
    """Merge-save a research entry; entry must contain a 'timestamp' key (ISO string)."""
    cache = _load_cache()
    cache[symbol] = entry
    _save_cache(cache)
