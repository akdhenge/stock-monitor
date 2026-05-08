"""
options_portfolio.py — Local metadata store for open options positions.

Parallel to portfolio.py (which handles stock positions).
Alpaca is source of truth for P&L; this module tracks the strategy context:
strike structure, thesis, IVR at entry, and exit thresholds.

All persistent state lives in data/options_portfolio_meta.json (atomic writes).
"""
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

_DATA_DIR  = os.path.join(os.path.dirname(__file__), "..", "data")
_META_PATH = os.path.join(_DATA_DIR, "options_portfolio_meta.json")


@dataclass
class OptionLeg:
    contract_symbol: str   # OCC format e.g. "AAPL241231C00150000"
    option_type: str       # "call" | "put"
    strike: float
    expiration: str        # YYYY-MM-DD
    contracts: int         # number of contracts (each = 100 shares)
    side: str              # "long" | "short"


@dataclass
class OptionPositionMeta:
    position_id: str               # UUID short (8 chars)
    symbol: str                    # underlying ticker
    strategy_type: str             # "long_call" | "long_put" | "csp" | "covered_call" |
                                   # "bull_call_spread" | "bear_put_spread" | "iron_condor"
    legs: List[OptionLeg]
    entry_premium: float           # total premium paid (+) or received (-) per contract set
    capital_deployed: float        # cash tied up (premium for debits, collateral for CSP)
    max_loss: float                # maximum possible loss in dollars
    target_premium: float          # premium at which to take profit
    stop_premium: float            # premium at which to stop loss
    thesis: str
    ivr_at_entry: Optional[float]  # IV Rank 0–100; None if proxy was used
    underlying_price_at_entry: float
    underlying_stop_loss: Optional[float]  # close if underlying drops here
    scan_score_at_entry: float
    opened_at: str                 # ISO timestamp string


def _ensure_data_dir() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)


def _load_raw() -> Dict[str, dict]:
    _ensure_data_dir()
    if not os.path.exists(_META_PATH):
        return {}
    try:
        with open(_META_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return {}


def _save_raw(data: Dict[str, dict]) -> None:
    _ensure_data_dir()
    tmp = _META_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _META_PATH)


def _from_dict(d: dict) -> OptionPositionMeta:
    legs = [OptionLeg(**leg) for leg in d.get("legs", [])]
    return OptionPositionMeta(
        position_id=d["position_id"],
        symbol=d["symbol"],
        strategy_type=d["strategy_type"],
        legs=legs,
        entry_premium=d["entry_premium"],
        capital_deployed=d["capital_deployed"],
        max_loss=d["max_loss"],
        target_premium=d["target_premium"],
        stop_premium=d["stop_premium"],
        thesis=d["thesis"],
        ivr_at_entry=d.get("ivr_at_entry"),
        underlying_price_at_entry=d["underlying_price_at_entry"],
        underlying_stop_loss=d.get("underlying_stop_loss"),
        scan_score_at_entry=d.get("scan_score_at_entry", 0.0),
        opened_at=d["opened_at"],
    )


def load_all_option_meta() -> Dict[str, OptionPositionMeta]:
    """Return {position_id: OptionPositionMeta} for all open options positions."""
    raw = _load_raw()
    result = {}
    for pid, d in raw.items():
        try:
            result[pid] = _from_dict(d)
        except (TypeError, KeyError):
            pass
    return result


def get_options_for_symbol(symbol: str) -> List[OptionPositionMeta]:
    """Return all open options positions for a given underlying symbol."""
    all_meta = load_all_option_meta()
    return [m for m in all_meta.values() if m.symbol.upper() == symbol.upper()]


def save_option_meta(meta: OptionPositionMeta) -> None:
    raw = _load_raw()
    d = asdict(meta)
    raw[meta.position_id] = d
    _save_raw(raw)


def delete_option_meta(position_id: str) -> None:
    raw = _load_raw()
    raw.pop(position_id, None)
    _save_raw(raw)


def total_capital_deployed() -> float:
    """Sum of capital_deployed across all open options positions."""
    return sum(m.capital_deployed for m in load_all_option_meta().values())
