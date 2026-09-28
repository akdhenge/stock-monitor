"""
drawdown_spread_builder.py — builds a concrete bull call (debit) spread
proposal for a DrawdownResult candidate, tiered by result.confidence_score
(sentiment_drawdown_screener_spec.md §5, redesigned to be confidence-driven).

Confidence tier table (target DTE window / long-strike OTM%):
  >= 85        14-30 DTE     ~2-4% OTM     ("2wk-1mo")
  70-84        30-90 DTE     ~4-7% OTM     ("1-3mo")
  55-69        90-180 DTE    ~7-12% OTM    ("3-6mo")
  40-54        180-270 DTE   ~12-18% OTM   ("6-9mo")
  < 40         270-365 DTE   ~15-25% OTM,  ("9-12mo")
               capped so long_strike never exceeds the analyst consensus target

confidence_score <= 0 (unset — e.g. a stale pre-redesign DrawdownResult loaded
from data/drawdown_results.json) is treated as NEUTRAL (mapped into the 3-6mo
tier), not as the lowest-confidence tier — see _select_tier().

Width scales with underlying price level (nearest-available-strike rounding
applies on top of these target widths):
  price < $50        2.5pt
  $50 <= price < 150   5pt
  $150 <= price < 400  10pt
  price >= $400       20pt  (top of the spec's suggested 15-20pt range —
                             megacap strike grids are typically 5/10pt spaced
                             above $400, so a flat 20pt is both simpler than a
                             graduated 15-20 split and realistically available)

Expiration selection, earnings interaction:
  - Nearest available expiry to the tier's target DTE (window midpoint),
    within the tier's [min_dte, max_dte] window if possible, else nearest
    available expiry beyond min_dte.
  - If the next earnings date falls inside the tier's DTE window:
      * Short/medium tiers (target max DTE <= 180 — the 2wk-1mo/1-3mo/3-6mo
        tiers): the tier's whole job is a quick sentiment re-rate, not
        surviving an earnings print, so it's safer to expire just BEFORE
        earnings than to blow out the DTE window chasing a post-earnings
        buffer. If there's at least ~10 DTE of room before earnings, shrink
        the window to end there; only if earnings is too close for that
        (< ~10 DTE out) does it fall through to the long-tier rule below.
      * Long tiers (6-9mo/9-12mo) and the short-tier earnings-too-close
        fallback: push the window to start just past earnings, using a
        small ~10-14 day buffer rather than the old fixed 90-day
        _MIN_DAYS_PAST_EARNINGS rule — 90 days used to be appropriate when
        every trade used the same 6-18mo hold; now that a 270-365 DTE trade
        and a 30-90 DTE trade share this logic, a flat 90-day buffer would
        silently evict earnings from short/medium tiers and barely matter
        for long tiers. Whichever adjustment (shrink-before vs push-past)
        keeps the expiry closest to the tier's target DTE wins.

Chain access: this codebase has no Alpaca options-chain fetch path anywhere
(core/options_executor.py only submits/manages orders, never fetches
chains). The one real, working chain-fetch pattern already in the app is
yfinance's ticker.option_chain() (used by core/options_strategy.py and
core/iv_tracker.py) — reused here via its helper functions instead of
re-implementing chain parsing or introducing an unproven Alpaca dependency.

Returns None cleanly (mirrors core/options_strategy.py's contract) whenever
chain data, a viable expiration, or a viable strike pair can't be found.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, NamedTuple, Optional, Tuple

from core.drawdown_result import DrawdownResult

_log = logging.getLogger(__name__)

_EARNINGS_BUFFER_DAYS = 12   # small buffer used both for "expire before earnings"
                             # and "push past earnings" adjustments (see module docstring)
_MIN_DTE_BEFORE_EARNINGS = 10  # minimum days of runway required to prefer "expire before earnings"


class _Tier(NamedTuple):
    min_confidence: float   # inclusive lower bound
    min_dte: int
    max_dte: int
    otm_pct: float           # target long-strike OTM%, as a fraction (0.03 = 3%)
    label: str
    cap_at_target: bool      # if True, cap long_strike at the analyst consensus target


# Ordered highest-confidence first; _select_tier walks down until min_confidence is cleared.
_TIERS: List[_Tier] = [
    _Tier(85.0, 14, 30, 0.03, "2wk-1mo", False),
    _Tier(70.0, 30, 90, 0.055, "1-3mo", False),
    _Tier(55.0, 90, 180, 0.095, "3-6mo", False),
    _Tier(40.0, 180, 270, 0.15, "6-9mo", False),
    _Tier(0.0, 270, 365, 0.20, "9-12mo", True),
]

_NEUTRAL_TIER_INDEX = 2  # "3-6mo" — used when confidence_score is unset (<= 0)


def _select_tier(confidence_score: float) -> _Tier:
    if confidence_score is None or confidence_score <= 0:
        # Unset/stale data (e.g. a pre-redesign DrawdownResult with no confidence_score) —
        # treat as neutral, NOT as the lowest-confidence tier. See module docstring.
        return _TIERS[_NEUTRAL_TIER_INDEX]
    for tier in _TIERS:
        if confidence_score >= tier.min_confidence:
            return tier
    return _TIERS[-1]


def _spread_width(price: float) -> float:
    if price < 50:
        return 2.5
    if price < 150:
        return 5.0
    if price < 400:
        return 10.0
    return 20.0


@dataclass
class DebitSpreadProposal:
    symbol: str
    long_strike: float
    short_strike: float
    expiration: str                  # YYYY-MM-DD
    long_contract_symbol: str
    short_contract_symbol: str
    net_debit: float                 # per-share
    max_profit_per_contract: float   # dollars, 1 contract (100 shares)
    max_loss_per_contract: float     # dollars, 1 contract
    breakeven: float
    underlying_price: float
    analyst_target: float
    drawdown_score: float
    cause_label: str
    cause_summary: str
    confidence_score: float = 0.0
    horizon_label: str = ""


def build_spread(result: DrawdownResult) -> Optional[DebitSpreadProposal]:
    """Fetch a live option chain for result.symbol and build a bull call spread,
    sized and dated per the confidence-score tier table above. Returns None
    cleanly on any missing chain/strike/expiration data."""
    try:
        import yfinance as yf
        from core.options_strategy import _occ_symbol, _mid_price, _row_for_strike, _otm_strike
    except Exception:
        _log.warning("drawdown_spread_builder: import failure")
        return None

    symbol = result.symbol
    current_price = result.current_price
    if not current_price or current_price <= 0:
        return None

    tier = _select_tier(result.confidence_score)

    try:
        ticker = yf.Ticker(symbol)
        expirations = ticker.options
    except Exception as exc:
        _log.warning("drawdown_spread_builder: chain list fetch failed for %s: %s", symbol, exc)
        return None
    if not expirations:
        return None

    expiry = _pick_expiry_for_tier(list(expirations), tier, result.next_earnings_date)
    if expiry is None:
        _log.info("drawdown_spread_builder: no viable expiry for %s (tier %s)", symbol, tier.label)
        return None

    try:
        chain = ticker.option_chain(expiry)
        calls = chain.calls
    except Exception as exc:
        _log.warning("drawdown_spread_builder: option_chain failed for %s %s: %s", symbol, expiry, exc)
        return None
    if calls is None or calls.empty:
        return None

    otm_pct = tier.otm_pct
    if tier.cap_at_target:
        consensus_target = current_price * (1 + max(result.analyst_upside_pct, 0.0))
        target_price = current_price * (1 + otm_pct)
        if target_price > consensus_target > current_price:
            otm_pct = (consensus_target - current_price) / current_price

    long_strike = _otm_strike(current_price, calls, otm_pct)
    if long_strike is None:
        return None

    width = _spread_width(current_price)
    short_strike = _nearest_strike_at_or_above(calls, long_strike + width)
    if short_strike is None:
        # Chain doesn't reach the target width (thin/short chain) — fall back to the
        # nearest strike strictly above long_strike so the trade can still be built.
        strikes_above = [s for s in calls["strike"].tolist() if s > long_strike]
        if not strikes_above:
            return None
        short_strike = min(strikes_above)

    long_row = _row_for_strike(calls, long_strike)
    short_row = _row_for_strike(calls, short_strike)
    if long_row is None or short_row is None:
        return None

    long_premium = _mid_price(long_row)
    short_premium = _mid_price(short_row)
    if long_premium <= 0:
        return None

    net_debit = round(long_premium - short_premium, 2)
    if net_debit <= 0:
        return None

    max_profit = round((short_strike - long_strike - net_debit) * 100, 2)
    max_loss = round(net_debit * 100, 2)
    if max_profit <= 0 or max_loss <= 0:
        return None

    consensus_target = current_price * (1 + max(result.analyst_upside_pct, 0.0))

    return DebitSpreadProposal(
        symbol=symbol,
        long_strike=float(long_strike),
        short_strike=float(short_strike),
        expiration=expiry,
        long_contract_symbol=_occ_symbol(symbol, expiry, "call", long_strike),
        short_contract_symbol=_occ_symbol(symbol, expiry, "call", short_strike),
        net_debit=net_debit,
        max_profit_per_contract=max_profit,
        max_loss_per_contract=max_loss,
        breakeven=round(long_strike + net_debit, 2),
        underlying_price=current_price,
        analyst_target=round(consensus_target, 2),
        drawdown_score=result.score,
        cause_label=result.cause_label,
        cause_summary=result.cause_summary,
        confidence_score=result.confidence_score,
        horizon_label=tier.label,
    )


def _nearest_strike_at_or_above(calls, target: float) -> Optional[float]:
    candidates = [s for s in calls["strike"].tolist() if s >= target]
    return min(candidates) if candidates else None


def _pick_expiry_for_tier(
    expirations: List[str], tier: _Tier, next_earnings_date: Optional[str]
) -> Optional[str]:
    """Nearest available expiry to the tier's target DTE, honoring the
    earnings-interaction rule described in the module docstring.

    Filters against the ACTUAL parsed expiry list rather than computing a
    synthetic sub-window and hoping something lands in it — a fabricated
    [min_dte, earnings_dte-2] window can be narrower than the real gap
    between available expiries and come up empty even when a perfectly good
    pre-earnings expiry exists a few days further out.
    """
    today = date.today()
    min_dte, max_dte = tier.min_dte, tier.max_dte
    target_dte = (min_dte + max_dte) / 2.0

    parsed: List[Tuple[date, int]] = []
    for e in expirations:
        try:
            d = date.fromisoformat(e)
        except ValueError:
            continue
        parsed.append((d, (d - today).days))

    earnings_dte: Optional[int] = None
    if next_earnings_date:
        try:
            ed = datetime.strptime(next_earnings_date, "%Y-%m-%d").date()
            earnings_dte = (ed - today).days
        except ValueError:
            earnings_dte = None

    if earnings_dte is not None and 0 <= earnings_dte <= max_dte:
        is_short_or_medium_tier = max_dte <= 180
        before_earnings = [(d, dte) for d, dte in parsed if dte < earnings_dte]
        max_before = max((dte for _, dte in before_earnings), default=-1)

        if is_short_or_medium_tier and max_before >= _MIN_DTE_BEFORE_EARNINGS:
            # A real, tradable expiry exists with enough runway before earnings —
            # pick the one closest to this tier's original target DTE.
            best = min(before_earnings, key=lambda t: abs(t[1] - target_dte))
            return best[0].isoformat()

        # Either a long tier, or no pre-earnings expiry has enough runway —
        # push the window to start just past earnings + a small buffer.
        min_dte = earnings_dte + _EARNINGS_BUFFER_DAYS
        max_dte = max(min_dte, tier.max_dte)
        target_dte = (min_dte + max_dte) / 2.0

    in_window = [(d, dte) for d, dte in parsed if min_dte <= dte <= max_dte]
    if in_window:
        best = min(in_window, key=lambda t: abs(t[1] - target_dte))
        return best[0].isoformat()

    beyond = [(d, dte) for d, dte in parsed if dte >= min_dte]
    if beyond:
        best = min(beyond, key=lambda t: abs(t[1] - target_dte))
        return best[0].isoformat()

    return None
