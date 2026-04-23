"""
self_tuner.py — Automatic parameter tuning from trade journal analysis.

Called by TraderAgent after every TUNE_EVERY_N_TRADES closed trades.
Sends trade statistics to Qwen3-coder via Ollama, parses JSON suggestions,
applies bounded adjustments to trader_config.json, and logs what changed.

Tunable parameters (all bounded to safe ranges):
  min_decision_score      [60, 85]
  default_stop_loss_pct   [0.04, 0.15]
  default_trailing_stop_pct [0.06, 0.20]
  max_position_pct        [0.03, 0.12]
  max_position_age_days   [5, 40]
"""
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.portfolio import load_trader_config, save_trader_config
from core.trade_journal import read_journal

_log = logging.getLogger(__name__)

TUNE_EVERY_N_TRADES = 10   # run after every N closed trades

_DATA_DIR   = os.path.join(os.path.dirname(__file__), "..", "data")
_TUNER_LOG  = os.path.join(_DATA_DIR, "self_tuner_log.jsonl")

# Allowed parameter bounds — keeps the agent from going off the rails
_BOUNDS: Dict[str, Tuple[float, float]] = {
    "min_decision_score":          (60.0, 85.0),
    "default_stop_loss_pct":       (0.04, 0.15),
    "default_trailing_stop_pct":   (0.06, 0.20),
    "max_position_pct":            (0.03, 0.12),
    "max_position_age_days":       (5.0,  40.0),
}


def should_tune(last_tune_count: int, total_closed: int) -> bool:
    return total_closed >= last_tune_count + TUNE_EVERY_N_TRADES


def run_tuning_cycle(last_tune_count: int) -> int:
    """
    Analyse journal, call Qwen3-coder, apply bounded changes.
    Returns new last_tune_count (total closed trades at time of tuning).
    """
    fills   = _get_closed_fills()
    n_total = len(fills)

    if n_total < TUNE_EVERY_N_TRADES:
        return last_tune_count

    stats   = _compute_stats(fills)
    config  = load_trader_config()
    prompt  = _build_prompt(stats, config)

    _log.info("SelfTuner: running tuning cycle (%d closed trades, win_rate=%.0f%%)",
              n_total, stats.get("win_rate_pct", 0))

    suggestion = _call_qwen(prompt)
    if suggestion is None:
        _log.warning("SelfTuner: no response from Qwen3 — skipping this cycle")
        return n_total

    changes = _apply_bounded(config, suggestion)
    if changes:
        save_trader_config(config)
        _log_tuning(stats, suggestion, changes)
        _log.info("SelfTuner: applied %d changes: %s", len(changes), changes)
    else:
        _log.info("SelfTuner: no parameter changes this cycle")

    return n_total


# ── Stats ──────────────────────────────────────────────────────────────────────

def _get_closed_fills() -> List[Dict]:
    journal = read_journal(last_n=2000)
    return [e for e in journal if e.get("type") == "fill" and e.get("side") == "SELL"]


def _compute_stats(fills: List[Dict]) -> Dict[str, Any]:
    wins   = [f for f in fills if (f.get("realized_pnl") or 0) > 0]
    losses = [f for f in fills if (f.get("realized_pnl") or 0) <= 0]

    win_rate  = round(len(wins) / len(fills) * 100, 1) if fills else 0
    avg_win   = round(sum(f.get("realized_pnl", 0) for f in wins)   / len(wins),   2) if wins   else 0
    avg_loss  = round(sum(f.get("realized_pnl", 0) for f in losses) / len(losses), 2) if losses else 0
    gross_win = sum(f.get("realized_pnl", 0) for f in wins)
    gross_loss = abs(sum(f.get("realized_pnl", 0) for f in losses))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

    # Decision score distribution for wins vs losses (from paired decision log)
    journal_all = read_journal(last_n=5000)
    decisions   = {e.get("symbol", "") + e.get("ts", "")[:10]: e
                   for e in journal_all if e.get("type") == "decision" and e.get("action") == "BUY"}

    win_scores  = [decisions.get(w["symbol"] + w.get("ts", "")[:10], {}).get("decision_score") for w in wins]
    loss_scores = [decisions.get(l["symbol"] + l.get("ts", "")[:10], {}).get("decision_score") for l in losses]
    win_scores  = [s for s in win_scores  if s is not None]
    loss_scores = [s for s in loss_scores if s is not None]

    avg_win_score  = round(sum(win_scores)  / len(win_scores),  1) if win_scores  else None
    avg_loss_score = round(sum(loss_scores) / len(loss_scores), 1) if loss_scores else None

    # Average holding period
    hold_days = []
    for f in fills:
        reason = f.get("exit_reason", "")
        ts = f.get("ts", "")
        if ts:
            try:
                hold = (datetime.fromisoformat(ts) - datetime(int(ts[:4]), 1, 1)).days % 365
                hold_days.append(hold)
            except Exception:
                pass

    # Most common exit reasons
    reasons: Dict[str, int] = {}
    for f in fills[-50:]:
        r = (f.get("exit_reason") or "unknown").split("—")[0].strip()
        reasons[r] = reasons.get(r, 0) + 1
    top_exits = sorted(reasons.items(), key=lambda x: -x[1])[:4]

    return {
        "n_trades":         len(fills),
        "win_rate_pct":     win_rate,
        "avg_win_dollars":  avg_win,
        "avg_loss_dollars": avg_loss,
        "profit_factor":    profit_factor,
        "avg_decision_score_wins":   avg_win_score,
        "avg_decision_score_losses": avg_loss_score,
        "top_exit_reasons": top_exits,
    }


# ── Prompt ─────────────────────────────────────────────────────────────────────

def _build_prompt(stats: Dict, config: Dict) -> str:
    current = {k: config.get(k) for k in _BOUNDS}
    return f"""You are a quantitative trading system self-tuner. Analyse these paper-trading statistics and suggest conservative parameter adjustments.

CURRENT PERFORMANCE:
{json.dumps(stats, indent=2)}

CURRENT PARAMETERS:
{json.dumps(current, indent=2)}

PARAMETER BOUNDS (hard limits):
{json.dumps({k: {"min": v[0], "max": v[1]} for k, v in _BOUNDS.items()}, indent=2)}

RULES:
- Only suggest changes that are clearly supported by the data
- Make small, conservative adjustments (typically ±5-10% of current value)
- If win_rate_pct > 55% and profit_factor > 1.5: the system is working — make no changes or minor ones
- If avg_decision_score_wins > avg_decision_score_losses by >10pts: raise min_decision_score slightly
- If most exits are "hard stop": stop_loss_pct may be too tight — consider loosening
- If most exits are "time stop": max_position_age_days may be too short

Respond ONLY with a valid JSON object containing the parameters you want to change (omit unchanged params):
{{"min_decision_score": 72.0, "default_stop_loss_pct": 0.09}}

If no changes are recommended, respond with an empty JSON object: {{}}"""


# ── Ollama call ────────────────────────────────────────────────────────────────

def _call_qwen(prompt: str) -> Optional[Dict]:
    try:
        import requests
        from core.settings_store import load_settings
        settings = load_settings()
        url   = settings.get("ai_ollama_url", "http://localhost:11434/api/generate")
        model = "qwen3-coder:30b"
        resp  = requests.post(url, json={"model": model, "prompt": prompt, "stream": False}, timeout=120)
        if not resp.ok:
            return None
        raw = resp.json().get("response", "")

        # Strip <think> blocks (Qwen3 reasoning)
        import re
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

        # Extract JSON object
        m = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
        if not m:
            return {}
        return json.loads(m.group())
    except Exception as exc:
        _log.error("SelfTuner: Qwen3 call failed: %s", exc)
        return None


# ── Apply + bound ──────────────────────────────────────────────────────────────

def _apply_bounded(config: Dict, suggestion: Dict) -> Dict[str, Any]:
    changes: Dict[str, Any] = {}
    for param, value in suggestion.items():
        if param not in _BOUNDS:
            continue
        lo, hi = _BOUNDS[param]
        try:
            clamped = max(lo, min(hi, float(value)))
        except (TypeError, ValueError):
            continue
        old = config.get(param)
        if old != clamped:
            config[param] = clamped
            changes[param] = {"from": old, "to": clamped}
    return changes


# ── Tuning log ─────────────────────────────────────────────────────────────────

def _log_tuning(stats: Dict, suggestion: Dict, changes: Dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    record = {
        "ts":         datetime.now().astimezone().isoformat(),
        "n_trades":   stats.get("n_trades"),
        "win_rate":   stats.get("win_rate_pct"),
        "suggestion": suggestion,
        "applied":    changes,
    }
    with open(_TUNER_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
