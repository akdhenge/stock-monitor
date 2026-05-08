"""
options_risk_manager.py — Budget tracking, IVR gating, and sizing for options.

No I/O beyond reading options_portfolio_meta.json (via options_portfolio).
Designed to be called from trader_agent after the conviction gate passes.
"""
import logging
from typing import Optional, Tuple

_log = logging.getLogger(__name__)

# Strategy type constants
LONG_CALL        = "long_call"
LONG_PUT         = "long_put"
CSP              = "csp"              # cash-secured put
COVERED_CALL     = "covered_call"
BULL_CALL_SPREAD = "bull_call_spread"
BEAR_PUT_SPREAD  = "bear_put_spread"
IRON_CONDOR      = "iron_condor"

# IV level thresholds (applied to IVR 0–100)
_IV_LOW  = "low"   # IVR < ivr_low_threshold  → buy premium
_IV_MID  = "mid"   # ivr_low ≤ IVR < ivr_high → mixed
_IV_HIGH = "high"  # IVR ≥ ivr_high_threshold  → sell premium


def compute_ivr_or_proxy(symbol: str, scan_result, iv_tracker) -> Optional[float]:
    """
    Return IV Rank (0–100) for the symbol.
    Tries real IVR from iv_tracker first; falls back to volatility_20d proxy
    if fewer than 30 daily snapshots exist.

    Proxy formula: min(volatility_20d * 200, 100)
      vol 0.20 → IVR proxy ~40
      vol 0.35 → IVR proxy ~70
      vol 0.50 → IVR proxy ~100
    """
    try:
        ivr = iv_tracker.get_ivr(symbol)
        if ivr is not None:
            return ivr
    except Exception:
        pass

    vol = getattr(scan_result, "volatility_20d", None)
    if vol and vol > 0:
        return min(vol * 200.0, 100.0)

    return None


def classify_iv(ivr: Optional[float], config: dict) -> str:
    """Return 'low', 'mid', or 'high' based on IVR thresholds from config."""
    if ivr is None:
        return _IV_MID  # treat unknown as mid
    low_thresh  = config.get("options_ivr_low", 30)
    high_thresh = config.get("options_ivr_high", 60)
    if ivr < low_thresh:
        return _IV_LOW
    if ivr >= high_thresh:
        return _IV_HIGH
    return _IV_MID


def select_strategy(
    regime: str,
    iv_level: str,
    conviction: float,
    options_level: int,
    is_overlay: bool = False,
) -> Optional[str]:
    """
    Map (regime, iv_level, conviction, options_level) → strategy type.
    Returns None if no strategy is suitable for the given conditions.

    options_level: 2 = long calls/puts + CSP + covered calls only
                   3 = adds spreads, condors (multi-leg)
    is_overlay: True when evaluating an existing stock position for covered call.
    """
    if is_overlay:
        return COVERED_CALL

    if regime == "bull":
        if iv_level == _IV_LOW:
            if conviction >= 70:
                return BULL_CALL_SPREAD if options_level >= 3 else LONG_CALL
        elif iv_level == _IV_HIGH:
            if conviction >= 65:
                return BULL_CALL_SPREAD if options_level >= 3 else CSP
        else:  # mid
            if conviction >= 70:
                return BULL_CALL_SPREAD if options_level >= 3 else LONG_CALL

    elif regime == "neutral":
        if iv_level == _IV_LOW:
            if conviction >= 75:
                return BULL_CALL_SPREAD if options_level >= 3 else LONG_CALL
        elif iv_level == _IV_HIGH:
            if conviction >= 62:
                return IRON_CONDOR if options_level >= 3 else CSP
        else:  # mid
            if conviction >= 70:
                return BULL_CALL_SPREAD if options_level >= 3 else LONG_CALL

    elif regime == "bear":
        if iv_level == _IV_LOW:
            if conviction >= 70:
                return BEAR_PUT_SPREAD if options_level >= 3 else LONG_PUT
        elif iv_level == _IV_HIGH:
            if conviction >= 62:
                return IRON_CONDOR if options_level >= 3 else None  # skip directional in bear+high-iv with L2
        else:  # mid
            if conviction >= 70:
                return BEAR_PUT_SPREAD if options_level >= 3 else LONG_PUT

    return None


def options_budget_remaining(nav: float, config: dict) -> float:
    """
    Return how many dollars can still be deployed into options.
    Reads current deployed capital from options_portfolio_meta.json.
    """
    from core.options_portfolio import total_capital_deployed
    cap_pct   = config.get("options_capital_pct", 0.20)
    cap_limit = nav * cap_pct
    deployed  = total_capital_deployed()
    return max(0.0, cap_limit - deployed)


def size_options_trade(
    conviction: float,
    nav: float,
    max_loss_per_contract: float,
    budget_remaining: float,
    config: dict,
) -> Tuple[int, float]:
    """
    Return (contracts, max_loss_dollars) for an options trade.

    Sizing logic:
    - Max loss per trade = 1% NAV (options_max_loss_per_trade_pct)
    - Scale by conviction: gate=55 → 0.5% NAV, conviction 100 → 1% NAV
    - Clamp to budget_remaining
    - contracts = floor(max_loss_dollars / max_loss_per_contract)

    Returns (0, 0.0) if trade would be too small.
    """
    gate     = config.get("min_decision_score", 55.0)
    max_pct  = config.get("options_max_loss_per_trade_pct", 0.01)
    span     = max(20.0, 100.0 - gate)
    t        = min(1.0, max(0.0, (conviction - gate) / span))
    loss_pct = (max_pct * 0.5) + (max_pct * 0.5) * (t ** 0.7)  # 0.5% → 1% of NAV

    max_loss_dollars = nav * loss_pct
    max_loss_dollars = min(max_loss_dollars, budget_remaining)

    if max_loss_per_contract <= 0:
        return 0, 0.0

    contracts = int(max_loss_dollars / max_loss_per_contract)
    if contracts < 1:
        return 0, 0.0

    actual_loss = contracts * max_loss_per_contract
    return contracts, round(actual_loss, 2)


def check_options_exit(
    meta,          # OptionPositionMeta
    current_premium: Optional[float],
    underlying_price: Optional[float],
    days_to_expiry: int,
    current_ivr: Optional[float],
    config: dict,
) -> Tuple[bool, str]:
    """
    Return (should_exit, reason) for an open options position.
    Checked in the heartbeat loop.

    Priority: profit target → stop loss → DTE decay → underlying stop → IV crush.
    """
    profit_take = config.get("options_profit_take_pct", 0.50)
    stop_loss   = config.get("options_stop_loss_pct",   0.50)
    close_dte   = config.get("options_close_dte",        21)

    if current_premium is not None and meta.entry_premium != 0:
        pnl_pct = (current_premium - meta.entry_premium) / abs(meta.entry_premium)
        if pnl_pct >= profit_take:
            return True, f"profit target hit: {pnl_pct*100:.0f}% gain on premium"
        if pnl_pct <= -stop_loss:
            return True, f"stop loss hit: {pnl_pct*100:.0f}% loss on premium"

    if days_to_expiry <= close_dte:
        return True, f"DTE {days_to_expiry} <= {close_dte} — closing to avoid gamma risk"

    if underlying_price is not None and meta.underlying_stop_loss is not None:
        if underlying_price <= meta.underlying_stop_loss:
            return True, f"underlying stop: ${underlying_price:.2f} <= ${meta.underlying_stop_loss:.2f}"

    # IV crush: for short-premium strategies, close if IVR collapsed
    short_premium_strategies = {CSP, COVERED_CALL, IRON_CONDOR}
    if meta.strategy_type in short_premium_strategies:
        iv_crush_floor = config.get("options_iv_crush_close_ivr", 15)
        if current_ivr is not None and current_ivr < iv_crush_floor:
            return True, f"IV crush: IVR {current_ivr:.0f} < {iv_crush_floor} — locking in theta gain"

    return False, ""
