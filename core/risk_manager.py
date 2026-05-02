"""
risk_manager.py — Pure-function risk gates and position sizing.

No I/O, no threads. Takes in current state and proposed trade,
returns a decision. All thresholds come from the trader_config dict.

Call order for entry:
  1. check_hard_gates()   — binary pass/fail
  2. compute_decision_score() — 0–100 composite
  3. size_position()      — dollar amount if score passes threshold

Call order for exit (per-position, on every price tick):
  1. check_exit()         — returns (should_exit, reason) or (False, "")
"""
from typing import Dict, List, Optional, Tuple


# ── Entry gates ────────────────────────────────────────────────────────────────

def check_hard_gates(
    symbol: str,
    scan_result,                   # ScanResult
    ai_research: Optional[dict],   # cached AIResearcher output or None
    open_symbols: List[str],       # currently held symbols
    sector_exposure: Dict[str, float],  # {sector: pct_of_nav}
    cash: float,
    nav: float,
    config: dict,
) -> Tuple[bool, str]:
    """
    Return (passed, rejection_reason).
    All hard gates must pass; first failure short-circuits.
    """
    # Already in portfolio
    if symbol.upper() in [s.upper() for s in open_symbols]:
        return False, "already held"

    # Too many positions
    if len(open_symbols) >= config.get("max_positions", 15):
        return False, f"max positions ({config['max_positions']}) reached"

    # Cash floor
    min_order = nav * config.get("min_position_pct", 0.02)
    reserve   = nav * config.get("cash_reserve_pct", 0.10)
    if cash - reserve < min_order:
        return False, "insufficient cash after reserve"

    # Price floor (penny stock gate)
    if scan_result.price and scan_result.price < config.get("min_price", 5.0):
        return False, f"price ${scan_result.price:.2f} below min ${config['min_price']}"

    # Liquidity gate
    min_vol = config.get("min_avg_volume", 1_000_000)
    if scan_result.avg_volume_20d and scan_result.avg_volume_20d < min_vol:
        return False, f"avg volume {scan_result.avg_volume_20d:.0f} below min {min_vol}"

    # Scan score floor
    if scan_result.total_score < config.get("min_scan_score", 60.0):
        return False, f"scan score {scan_result.total_score:.1f} below min {config['min_scan_score']}"

    # Sector concentration
    sector = scan_result.sector or "Unknown"
    sector_pct = sector_exposure.get(sector, 0.0)
    max_sector = config.get("max_sector_exposure_pct", 0.30)
    if sector_pct >= max_sector:
        return False, f"sector '{sector}' at {sector_pct*100:.0f}% cap ({max_sector*100:.0f}% max)"

    # AI sentiment gate (if research is available)
    if ai_research:
        sentiment = ai_research.get("sentiment", "NEUTRAL").upper()
        if sentiment == "BEARISH":
            return False, "AI sentiment BEARISH"

    return True, ""


def compute_conviction_score(
    scan_result,
    ai_rank: Optional[int],
    ai_research: Optional[dict],
    ranker_allocation_pct: Optional[float],
) -> Tuple[float, Dict[str, float]]:
    """
    Unified conviction score (0–100) that drives both the invest/pass gate
    and position sizing.  Returns (score, breakdown_dict) for explainability.

    Components
    ----------
    scan quality    35 pts  — total_score / 100 * 35
    AI rank         20 pts  — rank 1→20, rank 10→2, unranked→8
    ranker alloc    15 pts  — ranker's allocation hint (0–25%) scaled to 0–15
    sentiment       12 pts  — BULLISH=12, NEUTRAL=6, BEARISH=0
    direction        6 pts  — UP=6, SIDEWAYS=2, DOWN=0
    technicals      12 pts  — macd=4, rsi<40=4, volume_spike=4
    """
    breakdown: Dict[str, float] = {}

    # 35% scan quality
    scan_pts = round(scan_result.total_score * 0.35, 2)
    breakdown["scan"] = scan_pts

    # 20% AI rank
    if ai_rank is not None:
        rank_pts = round(max(0.0, (11 - ai_rank) / 10 * 20), 2)
    else:
        rank_pts = 8.0
    breakdown["rank"] = rank_pts

    # 15% ranker allocation hint  (ranker says "give this 22%" → strong conviction)
    if ranker_allocation_pct is not None:
        alloc_pts = round(min(ranker_allocation_pct, 25) / 25 * 15, 2)
    else:
        alloc_pts = 5.0
    breakdown["ranker_alloc"] = alloc_pts

    # 12% sentiment
    sent = ""
    if ai_research:
        sent = ai_research.get("sentiment", "NEUTRAL").upper()
    sent_pts = {"BULLISH": 12, "NEUTRAL": 6, "BEARISH": 0}.get(sent, 6)
    breakdown["sentiment"] = float(sent_pts)

    # 6% direction
    dirn_pts = 2.0
    if ai_research:
        dirn = ai_research.get("direction", "SIDEWAYS").upper()
        dirn_pts = {"UP": 6.0, "SIDEWAYS": 2.0, "DOWN": 0.0}.get(dirn, 2.0)
    breakdown["direction"] = dirn_pts

    # 12% technicals
    tech_pts = 0.0
    if scan_result.macd_bullish:
        tech_pts += 4
    if scan_result.rsi and scan_result.rsi < 40:
        tech_pts += 4
    if scan_result.volume_spike:
        tech_pts += 4
    breakdown["tech"] = tech_pts

    total = min(round(
        scan_pts + rank_pts + alloc_pts + sent_pts + dirn_pts + tech_pts, 2
    ), 100.0)
    return total, breakdown


def compute_decision_score(
    scan_result,
    ai_rank: Optional[int],
    ai_research: Optional[dict],
    allocation_pct: Optional[float],
) -> float:
    """Backward-compat shim — delegates to compute_conviction_score."""
    score, _ = compute_conviction_score(scan_result, ai_rank, ai_research, allocation_pct)
    return score


_REGIME_SIZE_MULT: Dict[str, float] = {"bull": 1.0, "neutral": 0.85, "bear": 0.65}


def size_position(
    conviction_score: float,
    effective_min_conviction: float,
    volatility: Optional[float],
    regime_label: str,
    nav: float,
    cash: float,
    config: dict,
) -> Tuple[float, Dict[str, float]]:
    """
    Return (dollars_to_deploy, breakdown_dict).

    Sizing curve: concave ramp anchored at the gate.
        t = (conviction - gate) / span,  clipped [0, 1]
        base_pct = min_pct + (max_pct - min_pct) * t^0.7

    Concrete examples (gate=55, bull):
        conviction 55 → 2.0%   ($2 000)
        conviction 70 → ~4.5%  ($4 500)
        conviction 85 → ~6.5%  ($6 500)
        conviction 100→  8.0%  ($8 000)

    Adjustments applied after the curve:
        * volatility factor  — high-vol stocks sized smaller
        * regime multiplier  — bear 0.65×, neutral 0.85×, bull 1.0×
    """
    max_pct   = config.get("max_position_pct", 0.08)
    min_pct   = config.get("min_position_pct", 0.02)
    reserve   = nav * config.get("cash_reserve_pct", 0.10)
    available = cash - reserve
    min_order = nav * min_pct

    breakdown: Dict[str, float] = {}

    if available < min_order:
        return 0.0, {"reason": "insufficient cash after reserve"}

    # Conviction curve
    gate = effective_min_conviction
    span = max(20.0, 100.0 - gate)
    t    = min(1.0, max(0.0, (conviction_score - gate) / span))
    base_pct = min_pct + (max_pct - min_pct) * (t ** 0.7)
    breakdown["base_pct"] = round(base_pct, 4)

    # Volatility factor
    vol_factor = 1.0
    if volatility and volatility > 0:
        target_vol = 0.25
        vol_factor = min(target_vol / volatility, 1.0)
        vol_factor = max(vol_factor, 0.5)
    breakdown["vol_factor"] = round(vol_factor, 3)

    # Regime multiplier
    regime_mult = _REGIME_SIZE_MULT.get(regime_label, 1.0)
    breakdown["regime_mult"] = regime_mult

    final_pct = base_pct * vol_factor * regime_mult
    breakdown["final_pct"] = round(final_pct, 4)

    dollars = nav * final_pct
    dollars = min(dollars, available)
    dollars = max(dollars, min_order)           # floor at min position
    dollars = min(dollars, nav * max_pct)       # hard cap at max position
    dollars = round(dollars, 2)
    breakdown["dollars"] = dollars

    return dollars, breakdown


# ── Exit checks ────────────────────────────────────────────────────────────────

def check_exit(
    meta,                           # PositionMeta
    current_price: float,
    ai_research: Optional[dict],
    days_held: int,
    config: dict,
) -> Tuple[bool, str]:
    """
    Return (should_exit, reason). Checked on every price tick for open positions.
    Priority: hard stop → trailing stop → thesis broken → target → time stop.
    """
    # Hard stop-loss
    if current_price <= meta.stop_loss_price:
        return True, f"stop-loss hit @ ${current_price:.2f} (stop ${meta.stop_loss_price:.2f})"

    # Trailing stop (activates once position is up 10%+)
    pct_gain = (current_price - meta.entry_price) / meta.entry_price
    if pct_gain >= 0.10 and meta.trailing_stop_pct > 0:
        trail_level = meta.high_water_price * (1 - meta.trailing_stop_pct)
        if current_price <= trail_level:
            return True, (f"trailing stop hit @ ${current_price:.2f} "
                          f"(trail off ${meta.high_water_price:.2f} × {1-meta.trailing_stop_pct:.0%})")

    # Thesis broken: fresh AI research turned bearish
    if ai_research:
        cache_age_hrs = _cache_age_hours(ai_research)
        if cache_age_hrs <= 8 and ai_research.get("sentiment", "").upper() == "BEARISH":
            return True, "thesis broken — AI sentiment turned BEARISH"

    # Target price reached → handled in trader_agent (partial exit, not full)
    # Not a full exit here; trader_agent handles the partial + stop move

    # Time stop: held too long with no meaningful move
    max_days = config.get("max_position_age_days", 20)
    if days_held >= max_days:
        pct_move = abs(pct_gain)
        if pct_move < 0.03:  # less than 3% move after 20 days
            return True, f"time stop: {days_held}d held, only {pct_move*100:.1f}% move"

    return False, ""


def check_circuit_breaker(
    nav: float,
    all_time_high_nav: float,
    intraday_pnl: float,
    config: dict,
) -> Tuple[bool, str]:
    """
    Return (halt, reason). If halted, agent should stop entering new positions.
    """
    # Daily loss breaker
    max_daily_loss = nav * config.get("max_daily_loss_pct", 0.03)
    if intraday_pnl <= -max_daily_loss:
        return True, f"daily loss breaker: intraday P&L ${intraday_pnl:.0f}"

    # Drawdown breaker
    max_dd_pct = config.get("max_drawdown_halt_pct", 0.15)
    if all_time_high_nav > 0:
        drawdown = (all_time_high_nav - nav) / all_time_high_nav
        if drawdown >= max_dd_pct:
            return True, f"drawdown halt: {drawdown*100:.1f}% below all-time high"

    return False, ""


def compute_sector_exposure(positions_meta: dict, position_values: Dict[str, float],
                            nav: float) -> Dict[str, float]:
    """
    Return {sector: fraction_of_nav} for all open positions.
    positions_meta: {symbol: PositionMeta}
    position_values: {symbol: market_value_dollars}
    """
    exposure: Dict[str, float] = {}
    for sym, meta in positions_meta.items():
        sector = meta.sector or "Unknown"
        val = position_values.get(sym, 0.0)
        exposure[sector] = exposure.get(sector, 0.0) + (val / nav if nav > 0 else 0)
    return exposure


def _cache_age_hours(research: dict) -> float:
    from datetime import datetime
    ts_str = research.get("timestamp", "")
    if not ts_str:
        return 999.0
    try:
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.now().astimezone().tzinfo)
        delta = datetime.now().astimezone() - ts
        return delta.total_seconds() / 3600
    except Exception:
        return 999.0
