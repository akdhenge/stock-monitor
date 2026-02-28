import json
import os
from typing import List

from core.models import StockEntry

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_WATCHLIST_PATH = os.path.join(_DATA_DIR, "watchlist.json")


def _ensure_data_dir():
    os.makedirs(_DATA_DIR, exist_ok=True)


def load_watchlist() -> List[StockEntry]:
    _ensure_data_dir()
    if not os.path.exists(_WATCHLIST_PATH):
        return []
    try:
        with open(_WATCHLIST_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        entries = []
        for item in raw:
            entries.append(StockEntry(
                symbol=item["symbol"],
                low_target=float(item["low_target"]),
                high_target=float(item["high_target"]),
                notes=item.get("notes", ""),
            ))
        return entries
    except (json.JSONDecodeError, KeyError, ValueError):
        return []


def save_watchlist(entries: List[StockEntry]) -> None:
    _ensure_data_dir()
    raw = []
    for e in entries:
        raw.append({
            "symbol": e.symbol,
            "low_target": e.low_target,
            "high_target": e.high_target,
            "notes": e.notes,
        })
    with open(_WATCHLIST_PATH, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)
