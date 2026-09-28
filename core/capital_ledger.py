"""
capital_ledger.py — Two-sleeve capital allocation (stock vs. options/debit-spread).

Pure functions only, no persisted state — Alpaca positions are already the
source of truth for what capital is deployed (see portfolio.py's docstring),
so tracking a second, separately-updated ledger would just be one more thing
that can drift out of sync with the broker. Instead, "deployed" is computed
fresh each cycle straight from the live position list.

Sleeves:
  "stock"                — all non-option positions
  "options_debit_spread" — all option positions (bull call spreads only, by
                            construction: nothing else is allowed to enter
                            this sleeve — see core/debit_spread_trader.py)

Config keys: stock_sleeve_pct, options_sleeve_pct (fraction of NAV each
sleeve is capped at — a ceiling, not a fill target).
"""
from typing import Dict, List

_OPTION_ASSET_CLASSES = {"us_option"}

_SLEEVE_PCT_KEY = {
    "stock":                 "stock_sleeve_pct",
    "options_debit_spread":  "options_sleeve_pct",
}


def sleeve_deployed(positions: List[dict], sleeve: str) -> float:
    """Sum of |market_value| for positions belonging to a sleeve."""
    total = 0.0
    for p in positions:
        is_option = p.get("asset_class") in _OPTION_ASSET_CLASSES
        if sleeve == "options_debit_spread" and is_option:
            total += abs(p.get("market_value", 0) or 0)
        elif sleeve == "stock" and not is_option:
            total += abs(p.get("market_value", 0) or 0)
    return total


def sleeve_budget_remaining(
    sleeve: str,
    nav: float,
    cash: float,
    positions: List[dict],
    config: dict,
) -> float:
    """
    Dollars still available for this sleeve. Hard-clamped to real broker
    cash so the two sleeves can never collectively overdraw the account,
    regardless of how their individual caps are configured.
    """
    pct_key   = _SLEEVE_PCT_KEY[sleeve]
    alloc_pct = config.get(pct_key, 0.5)
    deployed  = sleeve_deployed(positions, sleeve)
    sleeve_cap = nav * alloc_pct
    remaining_in_sleeve = max(0.0, sleeve_cap - deployed)
    return min(remaining_in_sleeve, max(0.0, cash))


def sleeve_summary(nav: float, cash: float, positions: List[dict], config: dict) -> Dict[str, dict]:
    """Snapshot of both sleeves for logging/status display."""
    summary = {}
    for sleeve in ("stock", "options_debit_spread"):
        deployed  = sleeve_deployed(positions, sleeve)
        alloc_pct = config.get(_SLEEVE_PCT_KEY[sleeve], 0.5)
        summary[sleeve] = {
            "deployed":  round(deployed, 2),
            "cap":       round(nav * alloc_pct, 2),
            "remaining": round(sleeve_budget_remaining(sleeve, nav, cash, positions, config), 2),
        }
    return summary
