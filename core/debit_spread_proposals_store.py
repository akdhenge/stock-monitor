"""
debit_spread_proposals_store.py — persistence for pending Telegram-proposed
debit-spread entries/closes awaiting user /approvespread or /closespread.

Keyed by symbol: one open proposal per symbol at a time. A fresh daily scan
skips a symbol that already has a pending proposal (see
core/debit_spread_trader.py::propose_entries) rather than stacking a second
one, so Telegram isn't spammed with duplicates on every cycle.

All persistent state lives in data/debit_spread_proposals.json (atomic writes),
parallel to core/options_portfolio.py's pattern for open positions.
"""
import dataclasses
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_PATH = os.path.join(_DATA_DIR, "debit_spread_proposals.json")


@dataclass
class SpreadProposal:
    symbol: str
    kind: str                       # "entry" | "close"
    status: str                     # "pending" | "approved" | "rejected" | "closed" | "expired"
    long_strike: float = 0.0
    short_strike: float = 0.0
    expiration: str = ""
    long_contract_symbol: str = ""
    short_contract_symbol: str = ""
    net_debit: float = 0.0
    max_profit_per_contract: float = 0.0
    max_loss_per_contract: float = 0.0
    suggested_contracts: int = 0
    drawdown_score: float = 0.0
    cause_label: str = ""
    cause_summary: str = ""
    analyst_target: float = 0.0
    underlying_price_at_proposal: float = 0.0
    confidence_score: float = 0.0
    horizon_label: str = ""
    close_reason: str = ""          # populated for kind="close"
    position_id: str = ""           # populated for kind="close" — links to OptionPositionMeta
    proposed_at: str = field(default_factory=lambda: datetime.now().isoformat())


def _ensure_dir() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)


def _load_raw() -> Dict[str, dict]:
    _ensure_dir()
    if not os.path.exists(_PATH):
        return {}
    try:
        with open(_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_raw(data: Dict[str, dict]) -> None:
    _ensure_dir()
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _PATH)


def load_all() -> Dict[str, SpreadProposal]:
    raw = _load_raw()
    out: Dict[str, SpreadProposal] = {}
    for sym, d in raw.items():
        try:
            out[sym] = SpreadProposal(**d)
        except TypeError:
            pass
    return out


def get(symbol: str) -> Optional[SpreadProposal]:
    return load_all().get(symbol.upper())


def save(proposal: SpreadProposal) -> None:
    raw = _load_raw()
    raw[proposal.symbol.upper()] = dataclasses.asdict(proposal)
    _save_raw(raw)


def delete(symbol: str) -> None:
    raw = _load_raw()
    raw.pop(symbol.upper(), None)
    _save_raw(raw)


def pending_entries() -> List[SpreadProposal]:
    return [p for p in load_all().values() if p.kind == "entry" and p.status == "pending"]


def pending_closes() -> List[SpreadProposal]:
    return [p for p in load_all().values() if p.kind == "close" and p.status == "pending"]
