"""
trade_journal.py — Append-only JSONL decision + fill log.

Every agent action is recorded here, including rejections.
Rejections are the most valuable records: after weeks of paper trading,
reading why the agent passed on winners tells you how to improve the decision function.

Record types:
  decision — agent evaluated a symbol (action: BUY / SELL / REJECT)
  fill      — Alpaca order confirmed (type: fill, side: BUY / SELL)
"""
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

_DATA_DIR     = os.path.join(os.path.dirname(__file__), "..", "data")
_JOURNAL_PATH = os.path.join(_DATA_DIR, "trade_journal.jsonl")
_CURVE_PATH   = os.path.join(_DATA_DIR, "equity_curve.jsonl")


def _ensure_data_dir() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)


def _append(record: Dict[str, Any]) -> None:
    _ensure_data_dir()
    with open(_JOURNAL_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── Writers ────────────────────────────────────────────────────────────────────

def log_decision(
    symbol: str,
    action: str,                     # "BUY" | "SELL" | "REJECT"
    reason: str,
    scan_score: Optional[float] = None,
    decision_score: Optional[float] = None,
    ai_rank: Optional[int] = None,
    ai_sentiment: Optional[str] = None,
    size_dollars: Optional[float] = None,
    nav_at_eval: Optional[float] = None,
    macro_regime: Optional[str] = None,
    cycle_id: Optional[str] = None,
    extra: Optional[Dict] = None,
) -> None:
    record: Dict[str, Any] = {
        "type":           "decision",
        "ts":             datetime.now().astimezone().isoformat(),
        "symbol":         symbol.upper(),
        "action":         action.upper(),
        "reason":         reason,
    }
    if scan_score      is not None: record["scan_score"]      = round(scan_score, 2)
    if decision_score  is not None: record["decision_score"]  = round(decision_score, 2)
    if ai_rank         is not None: record["ai_rank"]         = ai_rank
    if ai_sentiment    is not None: record["ai_sentiment"]    = ai_sentiment
    if size_dollars    is not None: record["size_dollars"]    = round(size_dollars, 2)
    if nav_at_eval     is not None: record["nav_at_eval"]     = round(nav_at_eval, 2)
    if macro_regime    is not None: record["macro_regime"]    = macro_regime
    if cycle_id        is not None: record["cycle_id"]        = cycle_id
    if extra:                       record.update(extra)
    _append(record)


def log_fill(
    symbol: str,
    side: str,                       # "BUY" | "SELL"
    shares: float,
    fill_price: float,
    order_id: str,
    stop_loss: Optional[float] = None,
    target: Optional[float] = None,
    thesis: Optional[str] = None,
    realized_pnl: Optional[float] = None,
    exit_reason: Optional[str] = None,
    cycle_id: Optional[str] = None,
) -> None:
    record: Dict[str, Any] = {
        "type":       "fill",
        "ts":         datetime.now().astimezone().isoformat(),
        "symbol":     symbol.upper(),
        "side":       side.upper(),
        "shares":     shares,
        "fill_price": fill_price,
        "order_id":   order_id,
    }
    if stop_loss    is not None: record["stop_loss"]    = stop_loss
    if target       is not None: record["target"]       = target
    if thesis       is not None: record["thesis"]       = thesis[:200]  # cap length
    if realized_pnl is not None: record["realized_pnl"] = round(realized_pnl, 2)
    if exit_reason  is not None: record["exit_reason"]  = exit_reason
    if cycle_id     is not None: record["cycle_id"]     = cycle_id
    _append(record)


def log_nav_snapshot(nav: float, cash: float, positions_value: float,
                     spy_close: Optional[float] = None) -> None:
    record: Dict[str, Any] = {
        "ts":              datetime.now().astimezone().isoformat(),
        "date":            datetime.now().strftime("%Y-%m-%d"),
        "nav":             round(nav, 2),
        "cash":            round(cash, 2),
        "positions_value": round(positions_value, 2),
    }
    if spy_close is not None:
        record["spy_close"] = round(spy_close, 4)
    _ensure_data_dir()
    with open(_CURVE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ── Readers ────────────────────────────────────────────────────────────────────

def read_journal(last_n: int = 100, action: Optional[str] = None) -> List[Dict]:
    _ensure_data_dir()
    if not os.path.exists(_JOURNAL_PATH):
        return []
    lines = []
    with open(_JOURNAL_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if action is None or rec.get("action", rec.get("side", "")).upper() == action.upper():
                    lines.append(rec)
            except json.JSONDecodeError:
                pass
    return lines[-last_n:]


def read_equity_curve(last_n: int = 500) -> List[Dict]:
    _ensure_data_dir()
    if not os.path.exists(_CURVE_PATH):
        return []
    lines = []
    with open(_CURVE_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return lines[-last_n:]
