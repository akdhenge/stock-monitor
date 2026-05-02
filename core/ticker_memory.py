"""
ticker_memory.py — Per-ticker trade outcome memory for prompt injection.

Write path (called from trader_agent):
  store_buy()        — after BUY fill: records entry price + thesis
  finalize_outcome() — after SELL fill: records outcome + spawns Qwen3-coder reflection

Read path (called from claude_ranking_analyst):
  get_past_context() — returns formatted string with past decisions + reflections
                       to inject into the Claude ranking prompt
"""
import json
import logging
import os
import threading
import urllib.request
from datetime import datetime, timedelta
from typing import Optional

_log = logging.getLogger(__name__)

_DATA_DIR    = os.path.join(os.path.dirname(__file__), "..", "data")
_MEMORY_PATH = os.path.join(_DATA_DIR, "ticker_memory.jsonl")
_LOCK        = threading.Lock()

_OLLAMA_URL         = "http://localhost:11434/api/generate"
_REFLECTION_MODEL   = "qwen3-coder:30b"
_REFLECTION_TOKENS  = 150


# ── Public write API ───────────────────────────────────────────────────────────

def store_buy(symbol: str, entry_price: float, thesis: str) -> None:
    """Append an open entry immediately after a confirmed BUY fill."""
    entry = {
        "symbol":          symbol.upper(),
        "buy_date":        datetime.now().strftime("%Y-%m-%d"),
        "entry_price":     round(entry_price, 4),
        "thesis":          thesis[:300],
        "exit_price":      None,
        "exit_date":       None,
        "exit_reason":     None,
        "raw_return_pct":  None,
        "spy_return_pct":  None,
        "alpha_pct":       None,
        "holding_days":    None,
        "reflection":      None,
        "status":          "open",
    }
    _append_line(entry)
    _log.info("ticker_memory: recorded BUY %s @ $%.2f", symbol.upper(), entry_price)


def finalize_outcome(symbol: str, exit_price: float, exit_reason: str) -> None:
    """
    Update the most recent open entry for symbol with outcome metrics,
    then spawn a background daemon thread to generate a Qwen3-coder reflection.
    Returns immediately — the reflection writes itself back when ready.
    """
    symbol = symbol.upper()
    updated = False

    with _LOCK:
        entries = _read_raw()
        for i in range(len(entries) - 1, -1, -1):
            e = entries[i]
            if e.get("symbol") == symbol and e.get("status") == "open":
                buy_date    = e.get("buy_date", "")
                entry_price = e.get("entry_price") or exit_price
                thesis      = e.get("thesis", "")

                raw_return  = _safe_return(entry_price, exit_price)
                spy_return  = _get_spy_return(buy_date)
                alpha       = round(raw_return - spy_return, 2) if (raw_return is not None and spy_return is not None) else None

                try:
                    held = (datetime.now() - datetime.strptime(buy_date, "%Y-%m-%d")).days
                except Exception:
                    held = None

                entries[i].update({
                    "exit_price":     round(exit_price, 4),
                    "exit_date":      datetime.now().strftime("%Y-%m-%d"),
                    "exit_reason":    exit_reason[:200],
                    "raw_return_pct": raw_return,
                    "spy_return_pct": spy_return,
                    "alpha_pct":      alpha,
                    "holding_days":   held,
                    "status":         "closed",
                })
                _write_raw(entries)
                updated = True
                _log.info(
                    "ticker_memory: outcome for %s — return %.1f%% alpha %s%%",
                    symbol, raw_return or 0,
                    f"{alpha:+.1f}" if alpha is not None else "n/a",
                )

                # Kick off reflection in background
                threading.Thread(
                    target=_generate_reflection,
                    args=(symbol, buy_date, entry_price, exit_price,
                          raw_return, alpha, held, exit_reason, thesis),
                    daemon=True,
                ).start()
                break

    if not updated:
        _log.debug("ticker_memory: no open entry found for %s — outcome skipped", symbol)


# ── Public read API ────────────────────────────────────────────────────────────

def get_past_context(symbol: str, n: int = 3) -> str:
    """
    Return a formatted block of past decisions for symbol (most recent first).
    Empty string if no history. Designed to be injected verbatim into a prompt.
    """
    with _LOCK:
        entries = _read_raw()

    symbol = symbol.upper()
    past = [
        e for e in entries
        if e.get("symbol") == symbol and e.get("status") in ("closed", "reflected")
    ]
    if not past:
        return ""

    past = past[-n:]
    lines = [f"Past trades for {symbol}:"]
    for e in reversed(past):
        ret   = f"{e['raw_return_pct']:+.1f}%" if e.get("raw_return_pct") is not None else "n/a"
        alpha = f"{e['alpha_pct']:+.1f}%"      if e.get("alpha_pct")      is not None else "n/a"
        held  = f"{e['holding_days']}d"         if e.get("holding_days")   is not None else "?"
        reason = (e.get("exit_reason") or "")[:60]
        lines.append(
            f"  [{e.get('buy_date')} → {e.get('exit_date', '?')}] "
            f"Return: {ret} | Alpha vs SPY: {alpha} | Held: {held} | Exit: {reason}"
        )
        if e.get("reflection"):
            lines.append(f"  Reflection: {e['reflection'][:280]}")
    return "\n".join(lines)


# ── Rolling alpha ──────────────────────────────────────────────────────────────

def get_rolling_alpha(n_trades: int = 10) -> Optional[float]:
    """
    Average alpha vs SPY over the last n closed trades.
    Returns None when fewer than 3 trades have outcome data (not enough signal).
    """
    with _LOCK:
        entries = _read_raw()
    closed = [
        e for e in entries
        if e.get("status") in ("closed", "reflected") and e.get("alpha_pct") is not None
    ]
    if len(closed) < 3:
        return None
    recent = closed[-n_trades:]
    return round(sum(e["alpha_pct"] for e in recent) / len(recent), 2)


# ── Background reflection ──────────────────────────────────────────────────────

def _generate_reflection(
    symbol: str,
    buy_date: str,
    entry_price: float,
    exit_price: float,
    raw_return: Optional[float],
    alpha: Optional[float],
    holding_days: Optional[int],
    exit_reason: str,
    thesis: str,
) -> None:
    """Daemon thread: calls Qwen3-coder, patches the closed entry with the reflection."""
    ret_str   = f"{raw_return:+.1f}%"  if raw_return   is not None else "unknown"
    alpha_str = f"{alpha:+.1f}%"       if alpha        is not None else "unknown"

    prompt = (
        f"You are a trading analyst writing a brief post-trade reflection.\n\n"
        f"Symbol: {symbol}\n"
        f"Entry: ${entry_price:.2f} on {buy_date} | Exit: ${exit_price:.2f} ({exit_reason})\n"
        f"Return: {ret_str} | Alpha vs SPY: {alpha_str} | Held: {holding_days}d\n"
        f"Original thesis: {thesis}\n\n"
        f"Write exactly 2-3 sentences: what drove the outcome, whether the original thesis proved "
        f"correct, and one concrete lesson for future trades of {symbol}. "
        f"No hedging. No preamble. Start directly with the observation. /no_think"
    )

    try:
        payload = json.dumps({
            "model":   _REFLECTION_MODEL,
            "prompt":  prompt,
            "stream":  False,
            "options": {"num_predict": _REFLECTION_TOKENS, "temperature": 0.3},
        }).encode("utf-8")
        req = urllib.request.Request(
            _OLLAMA_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        reflection = body.get("response", "").strip()
        if not reflection:
            return
    except Exception as exc:
        _log.warning("ticker_memory: reflection failed for %s: %s", symbol, exc)
        return

    # Patch entry with reflection
    with _LOCK:
        entries = _read_raw()
        for i in range(len(entries) - 1, -1, -1):
            e = entries[i]
            if (e.get("symbol") == symbol
                    and e.get("buy_date") == buy_date
                    and e.get("status") == "closed"):
                entries[i]["reflection"] = reflection
                entries[i]["status"]     = "reflected"
                _write_raw(entries)
                _log.info("ticker_memory: reflection saved for %s (%s)", symbol, buy_date)
                return


# ── File I/O helpers (all callers must hold _LOCK) ─────────────────────────────

def _read_raw() -> list:
    if not os.path.exists(_MEMORY_PATH):
        return []
    entries = []
    with open(_MEMORY_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def _write_raw(entries: list) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = _MEMORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, _MEMORY_PATH)


def _append_line(entry: dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    with _LOCK:
        with open(_MEMORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── Computation helpers ────────────────────────────────────────────────────────

def _safe_return(entry_price: float, exit_price: float) -> Optional[float]:
    if not entry_price:
        return None
    return round((exit_price - entry_price) / entry_price * 100, 2)


def _get_spy_return(buy_date: str) -> Optional[float]:
    """SPY return from buy_date to today using yfinance."""
    try:
        import yfinance as yf
        end_dt = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
        spy = yf.Ticker("SPY").history(start=buy_date, end=end_dt, interval="1d")
        if len(spy) >= 2:
            start_price = float(spy["Close"].iloc[0])
            end_price   = float(spy["Close"].iloc[-1])
            return round((end_price - start_price) / start_price * 100, 2)
    except Exception as exc:
        _log.debug("ticker_memory: SPY fetch failed: %s", exc)
    return None
