"""
market_regime.py — Market regime detection and regime-aware threshold table.

detect_regime() takes already-fetched SPY history and VIX so _process_scan
doesn't make extra network calls.  Returns a RegimeState with the label,
key prices, and the threshold overrides to apply for that regime.

Regimes
-------
bull   — SPY clearly above 200d MA (>2%) AND VIX < 20
         Standard momentum market.  Stocks need technical confirmation.
neutral — SPY within ±5% of 200d MA OR VIX 20–28
          Mixed signals.  Relax scan score, lean on debate.
bear   — SPY below 200d MA by >5% OR VIX > 28
         Mean-reversion market.  Scan scores are structurally low because
         nothing is in a technical uptrend.  Relax thresholds significantly,
         but run every candidate through the Claude debate filter.
"""
from dataclasses import dataclass
from typing import Optional

import logging

_log = logging.getLogger(__name__)


@dataclass
class RegimeState:
    label: str              # "bull" | "neutral" | "bear"
    spy_price: float
    sma200: float
    vix: float
    spy_vs_sma_pct: float   # positive = above, negative = below
    # Threshold overrides (applied on top of trader_config.json)
    min_scan_score: float
    min_decision_score: float
    max_candidates: int     # how many ranked items to consider per cycle


# ── Thresholds by regime ───────────────────────────────────────────────────────
#
# Bear market: scan scores are structurally low because MACD/200d-MA technicals
# are all negative when the market is in a downtrend.  Mean-reversion setups
# are valid but require the debate filter to do the heavy lifting.

_REGIME_TABLE = {
    "bull": {
        "min_scan_score":     60.0,
        "min_decision_score": 65.0,
        "max_candidates":     5,
    },
    "neutral": {
        "min_scan_score":     48.0,
        "min_decision_score": 57.0,
        "max_candidates":     7,
    },
    "bear": {
        "min_scan_score":     36.0,
        "min_decision_score": 48.0,
        "max_candidates":     8,
    },
}


def detect_regime(
    spy_hist,                   # pd.DataFrame from yfinance, ≥200 rows
    vix: Optional[float],
) -> RegimeState:
    """
    Classify market regime from pre-fetched data.
    Falls back to "neutral" if data is insufficient.
    """
    try:
        if spy_hist is None or len(spy_hist) < 200:
            return _make_regime("neutral", 0.0, 0.0, vix or 20.0)

        sma200    = float(spy_hist["Close"].tail(200).mean())
        spy_price = float(spy_hist["Close"].iloc[-1])
        vix_val   = float(vix) if vix else 20.0

        spy_vs_sma_pct = (spy_price - sma200) / sma200 * 100

        if spy_vs_sma_pct < -5.0 or vix_val > 28:
            label = "bear"
        elif spy_vs_sma_pct > 2.0 and vix_val < 20:
            label = "bull"
        else:
            label = "neutral"

        regime = _make_regime(label, spy_price, sma200, vix_val)
        _log.info(
            "market_regime: %s | SPY $%.0f vs 200d $%.0f (%+.1f%%) | VIX %.1f",
            label.upper(), spy_price, sma200, spy_vs_sma_pct, vix_val,
        )
        return regime

    except Exception as exc:
        _log.warning("market_regime: detection failed (%s) — defaulting to neutral", exc)
        return _make_regime("neutral", 0.0, 0.0, 20.0)


def _make_regime(label: str, spy_price: float, sma200: float, vix: float) -> RegimeState:
    t = _REGIME_TABLE[label]
    spy_vs_sma = (spy_price - sma200) / sma200 * 100 if sma200 else 0.0
    return RegimeState(
        label=label,
        spy_price=spy_price,
        sma200=sma200,
        vix=vix,
        spy_vs_sma_pct=spy_vs_sma,
        min_scan_score=t["min_scan_score"],
        min_decision_score=t["min_decision_score"],
        max_candidates=t["max_candidates"],
    )


def format_regime_log(regime: RegimeState) -> str:
    """One-line summary for step log."""
    arrow = "^" if regime.spy_vs_sma_pct >= 0 else "v"
    return (
        f"Regime: {regime.label.upper()} | "
        f"SPY ${regime.spy_price:.0f} {arrow}{abs(regime.spy_vs_sma_pct):.1f}% vs 200d | "
        f"VIX {regime.vix:.1f} | "
        f"thresholds -> scan>={regime.min_scan_score:.0f} "
        f"score>={regime.min_decision_score:.0f} "
        f"candidates={regime.max_candidates}"
    )
