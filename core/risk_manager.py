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


def compute_decision_score(
    scan_result,
    ai_rank: Optional[int],            # 1–10 from ClaudeRankingAnalyst; None = not ranked
    ai_research: Optional[dict],
    allocation_pct: Optional[float],   # from ClaudeRankingAnalyst
) -> float:
    """
    Composite 0–100 score combining scan quality, AI rank, and sentiment signals.
    Threshold is config['min_decision_score'] (default 70).
    """
    score = 0.0

    # 40% from scan total score
    score += scan_result.total_score * 0.40

    # 25% from AI portfolio rank (rank 1 → 25 pts, rank 10 → 2.5 pts)
    if ai_rank is not None:
        rank_pts = max(0, (11 - ai_rank) / 10 * 25)
        score += rank_pts
    else:
        score += 10.0  # neutral if not ranked yet

    # 20% from AI research signals
    if ai_research:
        sent = ai_research.get("sentiment", "NEUTRAL").upper()
        if sent == "BULLISH":
            score += 14
        elif sent == "NEUTRAL":
            score += 7

        dirn = ai_research.get("direction", "SIDEWAYS").upper()
        if dirn == "UP":
            score += 6

    # 15% from technical confirmations
    tech_pts = 0.0
    if scan_result.macd_bullish:
        tech_pts += 5
    if scan_result.rsi and scan_result.rsi < 40:
        tech_pts += 5
    if scan_result.volume_spike:
        tech_pts += 5
    score += tech_pts

    return min(round(score, 2), 100.0)


def size_position(
    decision_score: float,
    allocation_pct: Optional[float],  # hint from ClaudeRankingAnalyst (0–20)
    volatility: Optional[float],      # annualized vol (0.3 = 30%)
    nav: float,
    cash: float,
    config: dict,
) -> float:
    """
    Return the dollar amount to deploy, respecting max_position_pct and cash_reserve.
    Uses allocation_pct as a hint then adjusts for volatility.
    """
    max_pct   = config.get("max_position_pct", 0.08)
    min_pct   = config.get("min_position_pct", 0.02)
    reserve   = nav * config.get("cash_reserve_pct", 0.10)
    available = cash - reserve

    # Base from ClaudeRankingAnalyst's allocation suggestion (it gives 5–25 for top-5)
    if allocation_pct is not None:
        base_pct = min(allocation_pct / 100.0, max_pct)
    else:
        # Scale by decision score: score 70 → min_pct, score 100 → max_pct
        t = max(0, (decision_score - 70) / 30)
        base_pct = min_pct + t * (max_pct - min_pct)

    # Volatility adjustment: high vol stocks get smaller allocation
    if volatility is not None and volatility > 0:
        # Target vol = 0.25 (25% annualized). If stock is more volatile, scale down.
        target_vol = 0.25
        vol_factor = min(target_vol / volatility, 1.0)
        vol_factor = max(vol_factor, 0.5)  # floor at 50% — don't zero out
        base_pct *= vol_factor

    dollars = nav * base_pct
    dollars = min(dollars, available)
    dollars = max(dollars, nav * min_pct)
    dollars = min(dollars, nav * max_pct)

    return round(dollars, 2)


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
