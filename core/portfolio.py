"""
portfolio.py — Local position metadata store.

Alpaca is the source of truth for shares, cash, and P&L.
This module tracks the *why* behind each position: thesis, stop levels,
scan score at entry, and target price — things Alpaca doesn't know about.

All persistent state lives in data/portfolio_meta.json (atomic writes).
"""
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Dict, Optional

_DATA_DIR  = os.path.join(os.path.dirname(__file__), "..", "data")
_META_PATH = os.path.join(_DATA_DIR, "portfolio_meta.json")
_CFG_PATH  = os.path.join(_DATA_DIR, "trader_config.json")

_CONFIG_DEFAULTS: Dict = {
    "enabled":                False,
    "dry_run":                True,
    "mode":                   "balanced",
    "max_position_pct":       0.08,
    "min_position_pct":       0.02,
    "max_sector_exposure_pct": 0.30,
    "max_positions":          15,
    "cash_reserve_pct":       0.10,
    "default_stop_loss_pct":  0.08,
    "default_trailing_stop_pct": 0.12,
    "max_daily_loss_pct":     0.03,
    "max_drawdown_halt_pct":  0.15,
    "min_scan_score":         60.0,
    "min_decision_score":     70.0,
    "min_price":              5.0,
    "min_avg_volume":         1_000_000,
    "max_position_age_days":  20,
    "vix_halt_threshold":     30.0,
    "earnings_block_days":    3,
}


@dataclass
class PositionMeta:
    symbol: str
    thesis: str
    scan_score_at_entry: float
    ai_rank_at_entry: Optional[int]
    entry_price: float
    stop_loss_price: float
    trailing_stop_pct: float
    high_water_price: float
    target_price: Optional[float]
    sector: Optional[str]
    opened_at: str          # ISO timestamp string


def _ensure_data_dir() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)


# ── Config ─────────────────────────────────────────────────────────────────────

def load_trader_config() -> Dict:
    _ensure_data_dir()
    config = dict(_CONFIG_DEFAULTS)
    if os.path.exists(_CFG_PATH):
        try:
            with open(_CFG_PATH, encoding="utf-8") as f:
                saved = json.load(f)
            config.update(saved)
        except (json.JSONDecodeError, ValueError):
            pass
    return config


def save_trader_config(config: Dict) -> None:
    _ensure_data_dir()
    tmp = _CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, _CFG_PATH)


def init_trader_config() -> Dict:
    """Write default config if file doesn't exist; return the config."""
    _ensure_data_dir()
    if not os.path.exists(_CFG_PATH):
        save_trader_config(_CONFIG_DEFAULTS)
    return load_trader_config()


# ── Position metadata ──────────────────────────────────────────────────────────

def _load_meta_raw() -> Dict[str, Dict]:
    _ensure_data_dir()
    if not os.path.exists(_META_PATH):
        return {}
    try:
        with open(_META_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return {}


def _save_meta_raw(data: Dict[str, Dict]) -> None:
    _ensure_data_dir()
    tmp = _META_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _META_PATH)


def load_all_meta() -> Dict[str, PositionMeta]:
    raw = _load_meta_raw()
    result = {}
    for sym, d in raw.items():
        try:
            result[sym] = PositionMeta(**d)
        except (TypeError, KeyError):
            pass
    return result


def get_position_meta(symbol: str) -> Optional[PositionMeta]:
    return load_all_meta().get(symbol.upper())


def save_position_meta(meta: PositionMeta) -> None:
    raw = _load_meta_raw()
    raw[meta.symbol.upper()] = asdict(meta)
    _save_meta_raw(raw)


def update_high_water(symbol: str, price: float) -> None:
    raw = _load_meta_raw()
    sym = symbol.upper()
    if sym in raw and price > raw[sym].get("high_water_price", 0):
        raw[sym]["high_water_price"] = price
        _save_meta_raw(raw)


def delete_position_meta(symbol: str) -> None:
    raw = _load_meta_raw()
    raw.pop(symbol.upper(), None)
    _save_meta_raw(raw)


def build_entry_meta(
    symbol: str,
    entry_price: float,
    scan_score: float,
    ai_rank: Optional[int],
    thesis: str,
    stop_loss_pct: float,
    trailing_stop_pct: float,
    target_price: Optional[float],
    sector: Optional[str],
) -> PositionMeta:
    stop_price = round(entry_price * (1 - stop_loss_pct), 4)
    return PositionMeta(
        symbol=symbol.upper(),
        thesis=thesis,
        scan_score_at_entry=scan_score,
        ai_rank_at_entry=ai_rank,
        entry_price=entry_price,
        stop_loss_price=stop_price,
        trailing_stop_pct=trailing_stop_pct,
        high_water_price=entry_price,
        target_price=target_price,
        sector=sector,
        opened_at=datetime.now().isoformat(),
    )
