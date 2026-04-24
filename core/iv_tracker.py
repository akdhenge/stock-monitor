"""
iv_tracker.py — Daily IV snapshot per symbol for IVR computation.

IVR (IV Rank) = (current_iv - 52wk_low) / (52wk_high - 52wk_low) * 100

Used in Phase 2 options entry decisions. Snapshots are built up passively
from open stock positions in the heartbeat; no extra API calls needed
for watched names.

Usage:
    update_iv_snapshot(symbol, iv)  — record today's IV (call once per day)
    get_ivr(symbol)                 — 0–100 IVR, or None if < 30 data points
    get_current_iv(symbol)          — fetch ATM IV from nearest expiry chain
"""
import json
import os
from datetime import date, timedelta
from typing import Optional

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_IV_PATH  = os.path.join(_DATA_DIR, "iv_history.json")


def get_current_iv(symbol: str) -> Optional[float]:
    """Fetch ATM implied volatility from the nearest options expiry via yfinance."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        expirations = ticker.options
        if not expirations:
            return None
        chain = ticker.option_chain(expirations[0])
        calls = chain.calls
        if calls.empty:
            return None
        current_price = ticker.fast_info.last_price
        if not current_price:
            return None
        atm_row = calls.iloc[(calls["strike"] - float(current_price)).abs().argsort()[:1]]
        iv = atm_row["impliedVolatility"].iloc[0]
        return float(iv) if iv and float(iv) > 0 else None
    except Exception:
        return None


def update_iv_snapshot(symbol: str, iv: float) -> None:
    """Record today's IV for symbol; prune entries older than 365 days."""
    data = _load()
    sym = symbol.upper()
    if sym not in data:
        data[sym] = {}
    data[sym][str(date.today())] = round(iv, 4)
    cutoff = str(date.today() - timedelta(days=365))
    data[sym] = {d: v for d, v in data[sym].items() if d >= cutoff}
    _save(data)


def get_ivr(symbol: str) -> Optional[float]:
    """
    Return 0–100 IV Rank, or None if fewer than 30 daily snapshots available.
    IVR > 50 = elevated IV (good for selling premium).
    IVR < 30 = compressed IV (good for buying premium).
    """
    data = _load()
    sym = symbol.upper()
    series = data.get(sym, {})
    if len(series) < 30:
        return None
    values = list(series.values())
    iv_min = min(values)
    iv_max = max(values)
    current = values[-1]
    if iv_max == iv_min:
        return 50.0
    return round((current - iv_min) / (iv_max - iv_min) * 100, 1)


def _load() -> dict:
    if not os.path.exists(_IV_PATH):
        return {}
    try:
        with open(_IV_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = _IV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, _IV_PATH)
