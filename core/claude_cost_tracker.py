"""Shared Claude API cost tracking — all call sites write here."""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict

_log = logging.getLogger(__name__)

_DATA_DIR  = os.path.join(os.path.dirname(__file__), "..", "data")
_USAGE_LOG = os.path.join(_DATA_DIR, "claude_usage_log.json")

COST_PER_M: Dict[str, Dict[str, float]] = {
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00},
    "claude-opus-4-7":           {"input": 15.00, "output": 75.00},
    "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00},
    "claude-haiku-20240307":     {"input": 0.25,  "output": 1.25},
}
_DEFAULT_COST = {"input": 3.00, "output": 15.00}


def compute_cost(model: str, in_tok: int, out_tok: int) -> float:
    rates = COST_PER_M.get(model, _DEFAULT_COST)
    return (in_tok / 1_000_000) * rates["input"] + (out_tok / 1_000_000) * rates["output"]


def log_usage(model: str, in_tok: int, out_tok: int, trigger: str) -> None:
    record = {
        "ts":            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model":         model,
        "input_tokens":  in_tok,
        "output_tokens": out_tok,
        "cost_usd":      round(compute_cost(model, in_tok, out_tok), 6),
        "trigger":       trigger,
    }
    os.makedirs(_DATA_DIR, exist_ok=True)
    log: list = []
    if os.path.exists(_USAGE_LOG):
        try:
            with open(_USAGE_LOG, "r", encoding="utf-8") as f:
                log = json.load(f)
        except Exception:
            log = []
    log.append(record)
    try:
        with open(_USAGE_LOG, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        _log.debug("claude_cost_tracker: write failed: %s", exc)


def get_today_cost() -> float:
    """Return total cost_usd for today (UTC) from the usage log."""
    if not os.path.exists(_USAGE_LOG):
        return 0.0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with open(_USAGE_LOG, "r", encoding="utf-8") as f:
            log = json.load(f)
        return sum(
            r.get("cost_usd", 0.0)
            for r in log
            if isinstance(r, dict) and r.get("ts", "").startswith(today)
        )
    except Exception:
        return 0.0
