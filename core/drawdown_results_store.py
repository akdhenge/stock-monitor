"""
Persistence for DrawdownScanner results.
Saves/loads List[DrawdownResult] to data/drawdown_results.json.
"""
import dataclasses
import json
import logging
import os
from datetime import datetime
from typing import List

from core.drawdown_result import DrawdownResult

_log = logging.getLogger(__name__)
_RESULTS_PATH = os.path.join("data", "drawdown_results.json")


def _ensure_data_dir() -> None:
    os.makedirs("data", exist_ok=True)


def save_drawdown_results(results: List[DrawdownResult]) -> None:
    """Serialize results to JSON. Overwrites any previous save."""
    _ensure_data_dir()
    rows = []
    for r in results:
        d = dataclasses.asdict(r)
        d["timestamp"] = r.timestamp.isoformat()
        rows.append(d)
    payload = {"saved_at": datetime.now().isoformat(), "results": rows}
    tmp = _RESULTS_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _RESULTS_PATH)
    except Exception:
        _log.exception("Failed to save drawdown results")


def load_drawdown_results() -> List[DrawdownResult]:
    """Load persisted results. Returns [] on missing file or any error."""
    if not os.path.exists(_RESULTS_PATH):
        return []
    try:
        with open(_RESULTS_PATH, encoding="utf-8") as f:
            payload = json.load(f)
        rows = payload.get("results", [])
        results = []
        for row in rows:
            row["timestamp"] = datetime.fromisoformat(row.get("timestamp", datetime.now().isoformat()))
            results.append(DrawdownResult(**row))
        return results
    except Exception:
        _log.warning("Could not load drawdown_results.json — starting fresh")
        return []
