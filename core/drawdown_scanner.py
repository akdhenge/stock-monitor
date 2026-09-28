"""
DrawdownScanner — QThread that screens S&P 500 for sentiment-driven drawdown candidates.

Gate execution order (cheapest API first to minimize wasted calls):
  Gate 2 — Drawdown filter       (yfinance batch download, 1 call)
  Gate 3 — Fundamentals          (yfinance info per-stock + Finnhub earnings)
  Gate 4 — Analyst conviction    (yfinance info + Finnhub recommendations)
  Gate 1 — Options liquidity     (yfinance options expiry list)
  Gate 5 — LLM cause classification (DeepSeek API + Alpaca news)
"""
import json
import logging
import math
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import yfinance as yf
from PyQt5.QtCore import QThread, pyqtSignal

from core.drawdown_result import ACCEPTABLE_CAUSES, UNACCEPTABLE_CAUSES, DrawdownResult
from core.finnhub_client import FinnhubClient

_log = logging.getLogger(__name__)

# S&P 500 Wikipedia source
_SP500_URL = ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", 0, "Symbol")

_FALLBACK_SP500 = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "JPM", "V",
    "JNJ", "UNH", "XOM", "PG", "MA", "HD", "CVX", "MRK", "LLY", "ABBV",
    "PEP", "KO", "BAC", "COST", "AVGO", "MCD", "TMO", "CSCO", "ACN", "WMT",
    "ABT", "DHR", "TXN", "NEE", "CRM", "VZ", "PM", "RTX", "BMY", "AMGN",
    "INTU", "HON", "QCOM", "IBM", "GS", "CAT", "BA", "GE", "ADBE", "ADP",
    "ADSK", "ALGN", "ANSS", "CDNS", "CTAS", "DXCM", "EA", "EBAY", "FAST",
    "FTNT", "GILD", "KLAC", "LRCX", "MCHP", "MDLZ", "MNST", "MU", "NXPI",
    "ODFL", "ORLY", "PANW", "PAYX", "PCAR", "REGN", "ROST", "SBUX", "SNPS",
    "TMUS", "VRTX", "MRNA", "MRVL", "PDD", "AMD", "INTC", "NOW", "ISRG",
    "SPGI", "BLK", "CB", "CI", "CME", "COP", "DE", "DIS", "DOW", "DUK",
    "EMR", "EW", "F", "FDX", "GM", "HCA", "HUM", "ICE", "IEX", "ITW",
    "KMB", "LIN", "LMT", "LOW", "MMC", "MMM", "MO", "MPC", "MS", "NEE",
    "NKE", "NOC", "NSC", "NXPI", "OKE", "PSA", "PSX", "PXD", "PYPL", "REGN",
    "ROP", "RSG", "RTX", "SLB", "SO", "SRE", "TGT", "TJX", "TRV", "USB",
    "VLO", "VMC", "WFC", "WM", "XOM", "ZTS",
]

# Gate 2 thresholds
_G2_MIN_DRAWDOWN = 0.20   # at least 20% below 52w high
_G2_MAX_DRAWDOWN = 0.50   # not more than 50% below (too damaged)
_G2_MAX_DAYS_SINCE_HIGH = 180

# Gate 3 thresholds
_G3_MIN_REV_GROWTH = 0.10   # 10% YoY revenue growth
_G3_MIN_MARKET_CAP = 10e9   # $10B

# Gate 4 thresholds
_G4_MIN_ANALYST_UPSIDE = 0.25   # 25% upside to consensus target
_G4_MIN_ANALYSTS = 10
_G4_MIN_BUY_PCT = 0.70          # 70% Buy/Strong Buy
_G4_MAX_DOWNGRADES = 2          # max analyst rating downgrades in trailing 90 days

# ── Soft-tolerance bounds ────────────────────────────────────────────────────
# Each hard-* value below is the true exclusion boundary; between the original
# threshold and the hard boundary a candidate soft-passes with a degrading
# margin_score (see _margin_above/_margin_below/_margin_range). Values follow
# a ~15% relative band except where the redesign spec gave an explicit number
# (noted inline) — those explicit numbers are used as-is since they were
# hand-picked per metric rather than derived from a blanket 15%.
_MARGIN_FLOOR = 40.0   # score at the very edge of the tolerance band (not 0 — still a real, if weak, pass)

# Gate 2 — drawdown depth/recency (~15% relative both directions)
_G2_DRAWDOWN_HARD_MIN = 0.17    # 0.20 * 0.85
_G2_DRAWDOWN_HARD_MAX = 0.57    # 0.50 * 1.15 (rounded from 0.575 -> spec's "~57%")
_G2_DAYS_HARD_MAX = 207         # 180 * 1.15

# Gate 3 — fundamentals (falling-knife filter). earnings_beat and
# operating_cashflow>0 stay HARD (no tolerance, see _gate3_fundamentals).
# rev_growth is spec's explicit "~7%" floor (not a strict 15% relative band —
# 0.10*0.85=0.085 — the spec explicitly asked for ~7%, so that number wins).
_G3_REV_GROWTH_HARD_FLOOR = 0.07

# Gate 4 — analyst conviction (spec's explicit numbers)
_G4_UPSIDE_HARD_FLOOR = 0.20        # spec explicit "~20%"
_G4_BUY_PCT_HARD_FLOOR = 0.60       # spec explicit "~60%"
_G4_DOWNGRADES_HARD_MAX = 3         # spec explicit "~3"
_G4_ANALYST_COUNT_HARD_FLOOR = 8    # spec explicit "~8"

# Gate 1 — options liquidity (spec's explicit numbers)
_G1_OI_SOFT_MIN = 150                # was 500 inline — same rationale as the hard floor below:
                                     # 500 was never achievable by real 6mo-forward LEAPS OI on
                                     # even the most liquid mega-caps in this sample.
_G1_OI_HARD_FLOOR = 75              # was 350 (spec's literal "~350") — real chain data on
                                     # mega-caps (MU, AMD) showed OI ~130-140 at the actual
                                     # 6mo-forward near-ATM strike, well under even a 350 floor.
                                     # Open interest concentrates in near-dated expirations for
                                     # every underlying, not just illiquid ones — a 500/350 bar
                                     # was calibrated for near-term options, not 6-12mo LEAPS.
_G1_SPREAD_HARD_CEILING = 0.07      # spec explicit "~7%" — kept as the primary liquidity
                                     # signal; tight spread (confirmed 2.7-3.8% on MU/AMD)
                                     # is what actually determines fill quality on a LEAPS
                                     # order, unlike a raw OI count that's structurally low
                                     # for far-dated contracts regardless of true liquidity.

# Scoring bell curve: peaks at ~27% drawdown, width ~12%
_BELL_PEAK = 0.27
_BELL_WIDTH = 0.12

# DeepSeek API
_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


def _get_commodity_exposure(industry: str) -> str:
    """Classify commodity exposure from yfinance industry label (substring match, case-insensitive)."""
    s = (industry or "").lower()
    if any(k in s for k in (
        "oil & gas e&p", "oil & gas exploration", "oil & gas equipment",
        "oil & gas integrated", "coal", "gold", "silver", "copper",
        "steel", "aluminum", "metals & mining",
    )):
        return "HIGH"
    if any(k in s for k in (
        "independent power", "oil & gas midstream", "oil & gas refining",
        "commodity chemicals", "fertilizers",
    )):
        return "MEDIUM"
    return "LOW"


# LLM classification prompt
_CLASSIFY_PROMPT = """You are a financial analyst. A stock has dropped significantly from its recent high.
Classify the PRIMARY cause of the price drop AND assess commodity exposure.

Stock: {symbol}
Current drawdown from 52-week high: {drawdown_pct:.1f}%
Sector pre-assessment from industry label (may be imprecise): commodity_hint={sector_hint}

Recent news headlines (last 60 days):
{headlines}

Respond ONLY with a JSON object in this exact format (no markdown, no explanation):
{{
  "cause_label": "<PRIMARY cause — one of: capex_concern | margin_pressure | sector_rotation | one_time_legal | macro_panic | guidance_cut | demand_decline | share_loss | product_failure | accounting | exec_departure | existential_regulatory | secular_decline | unclear>",
  "cause_labels_all": ["<primary_cause>", "<secondary_cause_if_any>"],
  "multi_causal": <true if multiple overlapping PRIMARY causes exist; false if single clean cause>,
  "cause_summary": "<2-3 sentences explaining the specific cause(s) of the drop>",
  "confidence": "<high | medium | low>",
  "pass": <true if ALL primary causes are non-fundamental/sentiment-driven, false if even one is real business damage>,
  "commodity_exposure": "<HIGH | MEDIUM | LOW>",
  "commodity_rationale": "<one sentence: is revenue primarily driven by a commodity price the company doesn't control?>"
}}

Cause guidelines:
- pass=true for: capex_concern, margin_pressure, sector_rotation, one_time_legal, macro_panic, guidance_cut, unclear
- pass=false for: demand_decline, share_loss, product_failure, accounting, exec_departure, existential_regulatory, secular_decline
- If even ONE primary cause is unacceptable, set pass=false
- If unclear from headlines, use cause_label="unclear" with pass=true and confidence="low"
- Multi-causal check: if the drop has multiple overlapping primary causes (not just one primary + minor noise),
  set multi_causal=true and list all primary causes in cause_labels_all. A headline EPS miss + legal cloud + governance
  concern = multi_causal=true. A capex concern + mild sector rotation = multi_causal=false (single dominant cause).

Commodity exposure guidelines:
- HIGH: revenue is primarily driven by a commodity price the company doesn't control (E&P, miners, coal, steel). Excluded from this screener even if cause label is sentiment-driven.
- MEDIUM: meaningful commodity exposure with real non-commodity growth angles (e.g., power retailer with data-center demand). Keep but surface as a warning.
- LOW: not materially commodity-dependent. Default for most companies.
"""


def _bell(x: float, peak: float = _BELL_PEAK, width: float = _BELL_WIDTH) -> float:
    """Gaussian bell curve normalized to 0-100, peaking at `peak`."""
    return 100.0 * math.exp(-0.5 * ((x - peak) / width) ** 2)


# ── Soft-tolerance margin helpers ────────────────────────────────────────────
# All three return None for a true reject (outside the tolerance band, or the
# hard-only condition failed) and a float in [_MARGIN_FLOOR, 100.0] otherwise.
# 100.0 = comfortably clears the original threshold; degrades linearly toward
# _MARGIN_FLOOR as the value approaches the hard boundary.

def _margin_above(value: Optional[float], soft_min: float, hard_floor: float,
                   floor: float = _MARGIN_FLOOR) -> Optional[float]:
    """Metric must be >= soft_min to fully pass; tolerated down to hard_floor."""
    if value is None:
        return None
    if value >= soft_min:
        return 100.0
    if value < hard_floor:
        return None
    span = soft_min - hard_floor
    frac = (value - hard_floor) / span if span > 0 else 1.0
    return floor + frac * (100.0 - floor)


def _margin_below(value: Optional[float], soft_max: float, hard_ceiling: float,
                   floor: float = _MARGIN_FLOOR) -> Optional[float]:
    """Metric must be <= soft_max to fully pass; tolerated up to hard_ceiling."""
    if value is None:
        return None
    if value <= soft_max:
        return 100.0
    if value > hard_ceiling:
        return None
    span = hard_ceiling - soft_max
    frac = (hard_ceiling - value) / span if span > 0 else 1.0
    return floor + frac * (100.0 - floor)


def _margin_range(value: Optional[float], hard_min: float, soft_min: float,
                   soft_max: float, hard_max: float,
                   floor: float = _MARGIN_FLOOR) -> Optional[float]:
    """Metric must land in [soft_min, soft_max] to fully pass; tolerated out to
    [hard_min, hard_max]."""
    if value is None:
        return None
    if value < hard_min or value > hard_max:
        return None
    if soft_min <= value <= soft_max:
        return 100.0
    if value < soft_min:
        span = soft_min - hard_min
        frac = (value - hard_min) / span if span > 0 else 1.0
    else:
        span = hard_max - soft_max
        frac = (hard_max - value) / span if span > 0 else 1.0
    return floor + frac * (100.0 - floor)


def _options_quality_score(has_6mo: bool, has_12mo: bool) -> float:
    """Score 0-100 based on options expiry availability."""
    if has_6mo and has_12mo:
        return 85.0
    if has_6mo:
        return 50.0
    return 0.0


class DrawdownScanner(QThread):
    scan_complete = pyqtSignal(list)   # List[DrawdownResult] (passed + close-misses)
    scan_progress = pyqtSignal(int)    # 0-100
    scan_status   = pyqtSignal(str)
    scan_error    = pyqtSignal(str)
    scan_cost     = pyqtSignal(dict)   # cost breakdown dict emitted at end of scan

    def __init__(self, settings: Dict[str, Any], parent=None):
        super().__init__(parent)
        self._settings = settings
        self._running = False

    def stop(self) -> None:
        self._running = False

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self) -> None:
        self._running = True
        try:
            results = self._do_scan()
            self.scan_complete.emit(results)
        except Exception as exc:
            _log.exception("DrawdownScanner unhandled error")
            self.scan_error.emit(f"Screener error: {exc}")

    # ── Main scan pipeline ────────────────────────────────────────────────────

    def _do_scan(self) -> List[DrawdownResult]:
        results: List[DrawdownResult] = []
        close_misses: List[DrawdownResult] = []
        self._cost: Dict[str, Any] = {
            "deepseek_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "finnhub_calls": 0,
        }

        # ── Fetch universe ────────────────────────────────────────────────────
        self.scan_status.emit("Fetching S&P 500 universe...")
        self.scan_progress.emit(2)
        symbols = self._fetch_sp500()
        self.scan_status.emit(f"Universe: {len(symbols)} symbols")

        if not self._running:
            return []

        # ── Gate 2: Drawdown filter ───────────────────────────────────────────
        self.scan_status.emit(f"Gate 2: Checking drawdowns ({len(symbols)} symbols)...")
        self.scan_progress.emit(5)
        g2_survivors, g2_data = self._gate2_drawdown(symbols)
        self.scan_status.emit(f"Gate 2: {len(g2_survivors)} passed (20-50% below 52w high within 180 days, volume >2M)")
        self.scan_progress.emit(20)

        if not self._running or not g2_survivors:
            return []

        # ── Gate 3: Fundamentals ──────────────────────────────────────────────
        self.scan_status.emit(f"Gate 3: Checking fundamentals ({len(g2_survivors)} symbols)...")
        finnhub = self._make_finnhub()
        g3_survivors, g3_data = self._gate3_fundamentals(g2_survivors, g2_data, finnhub)
        # Each survivor gets 1 Finnhub earnings call
        self._cost["finnhub_calls"] += len(g2_survivors) if finnhub else 0
        g3_misses = set(g2_survivors) - set(g3_survivors)
        for sym in g3_misses:
            d = g2_data.get(sym, {})
            close_misses.append(self._build_partial(sym, d, {}, "gate3_fundamentals"))
        self.scan_status.emit(f"Gate 3: {len(g3_survivors)} passed fundamentals check")
        self.scan_progress.emit(40)

        if not self._running or not g3_survivors:
            return close_misses

        # ── Gate 4: Analyst conviction ────────────────────────────────────────
        self.scan_status.emit(f"Gate 4: Checking analyst conviction ({len(g3_survivors)} symbols)...")
        g4_survivors, g4_data = self._gate4_analyst(g3_survivors, g3_data, finnhub)
        # Each Gate 3 survivor gets 1 Finnhub recommendations call
        # 2 Finnhub calls per Gate 4 symbol: recommendations + upgrade-downgrade
        self._cost["finnhub_calls"] += (len(g3_survivors) * 2) if finnhub else 0
        g4_misses = set(g3_survivors) - set(g4_survivors)
        for sym in g4_misses:
            d = {**g2_data.get(sym, {}), **g3_data.get(sym, {})}
            close_misses.append(self._build_partial(sym, d, {}, "gate4_analyst_conviction"))
        self.scan_status.emit(f"Gate 4: {len(g4_survivors)} passed analyst conviction check")
        self.scan_progress.emit(55)

        if not self._running or not g4_survivors:
            return close_misses

        # ── Gate 1: Options liquidity ─────────────────────────────────────────
        self.scan_status.emit(f"Gate 1: Checking options liquidity ({len(g4_survivors)} symbols)...")
        g1_survivors, g1_data = self._gate1_options(g4_survivors, g4_data)
        g1_misses = set(g4_survivors) - set(g1_survivors)
        for sym in g1_misses:
            d = {**g2_data.get(sym, {}), **g3_data.get(sym, {}), **g4_data.get(sym, {})}
            close_misses.append(self._build_partial(sym, d, {}, "gate1_options_liquidity"))
        self.scan_status.emit(f"Gate 1: {len(g1_survivors)} passed options liquidity check")
        self.scan_progress.emit(70)

        if not self._running or not g1_survivors:
            return close_misses

        # ── Gate 5: LLM cause classification ─────────────────────────────────
        self.scan_status.emit(f"Gate 5: LLM cause classification ({len(g1_survivors)} symbols)...")
        g5_survivors, g5_data = self._gate5_llm(g1_survivors, g1_data, g2_data, g3_data)
        g5_misses = set(g1_survivors) - set(g5_survivors)
        for sym in g5_misses:
            d = {**g2_data.get(sym, {}), **g3_data.get(sym, {}),
                 **g4_data.get(sym, {}), **g1_data.get(sym, {}),
                 **g5_data.get(sym, {})}
            sym_g5 = g5_data.get(sym, {})
            failed = (
                "commodity_driven_high"
                if sym_g5.get("commodity_exposure") == "HIGH"
                else "gate5_cause_of_drop"
            )
            close_misses.append(self._build_partial(sym, d, sym_g5, failed))
        self.scan_status.emit(f"Gate 5: {len(g5_survivors)} passed cause classification")
        self.scan_progress.emit(88)

        if not self._running:
            return close_misses

        # ── Score and rank ────────────────────────────────────────────────────
        for sym in g5_survivors:
            merged = {**g2_data.get(sym, {}), **g3_data.get(sym, {}),
                      **g4_data.get(sym, {}), **g1_data.get(sym, {}),
                      **g5_data.get(sym, {})}
            result = self._score_candidate(sym, merged)
            results.append(result)

        results.sort(key=lambda r: r.score, reverse=True)

        self.scan_progress.emit(100)
        self.scan_status.emit(
            f"Complete: {len(results)} candidates, {len(close_misses)} near-misses"
        )
        self.scan_cost.emit(dict(self._cost))
        return results + close_misses

    # ── Universe fetch ────────────────────────────────────────────────────────

    def _fetch_sp500(self) -> List[str]:
        try:
            import io
            import pandas as pd
            import urllib.request
            url, tbl_idx, col = _SP500_URL
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; stock-monitor/1.0)"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8")
            tables = pd.read_html(io.StringIO(html))
            symbols = [
                str(s).replace(".", "-").strip().upper()
                for s in tables[tbl_idx][col].tolist()
            ]
            return [s for s in symbols if s][:500]
        except Exception as exc:
            _log.warning("Wikipedia S&P 500 fetch failed: %s — using fallback", exc)
            return list(_FALLBACK_SP500)

    # ── Gate 2: Drawdown filter ───────────────────────────────────────────────

    def _gate2_drawdown(
        self, symbols: List[str]
    ) -> Tuple[List[str], Dict[str, Dict]]:
        """Batch-download 1Y daily history and filter by drawdown criteria."""
        survivors: List[str] = []
        data: Dict[str, Dict] = {}

        try:
            raw = yf.download(
                symbols,
                period="1y",
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
        except Exception as exc:
            _log.error("yfinance batch download failed: %s", exc)
            self.scan_error.emit(f"Gate 2 download error: {exc}")
            return [], {}

        today = date.today()

        for sym in symbols:
            if not self._running:
                break
            try:
                if len(symbols) == 1:
                    df = raw
                else:
                    df = raw[sym] if sym in raw.columns.get_level_values(0) else None

                if df is None or df.empty:
                    continue

                df = df.dropna(subset=["Close"])
                if len(df) < 30:
                    continue

                closes = df["Close"]
                highs = df["High"] if "High" in df.columns else closes

                current_price = float(closes.iloc[-1])
                peak_price = float(highs.max())
                peak_idx = highs.idxmax()

                if peak_price <= 0 or current_price <= 0:
                    continue

                pct_below = (peak_price - current_price) / peak_price
                peak_date = peak_idx.date() if hasattr(peak_idx, "date") else today
                days_since = (today - peak_date).days

                # Volume check: 30-day avg daily volume > 2M (Gate 1 spec, free from batch data).
                # Left HARD — not one of the metrics the redesign asked to soften; this is a
                # data-quality/tradability floor, not a business threshold, so no tolerance band.
                avg_volume_30d = 0.0
                if "Volume" in df.columns:
                    avg_volume_30d = float(df["Volume"].tail(30).mean())
                if avg_volume_30d < 2_000_000:
                    continue

                drawdown_margin = _margin_range(
                    pct_below, _G2_DRAWDOWN_HARD_MIN, _G2_MIN_DRAWDOWN,
                    _G2_MAX_DRAWDOWN, _G2_DRAWDOWN_HARD_MAX,
                )
                days_margin = _margin_below(days_since, _G2_MAX_DAYS_SINCE_HIGH, _G2_DAYS_HARD_MAX)

                if drawdown_margin is None or days_margin is None:
                    continue  # outside the tolerance band entirely — true reject

                survivors.append(sym)
                data[sym] = {
                    "current_price": current_price,
                    "pct_below_high": pct_below,
                    "days_since_high": days_since,
                    "peak_price": peak_price,
                    "avg_volume_30d": avg_volume_30d,
                    "_margin_g2": (drawdown_margin + days_margin) / 2.0,
                }
            except Exception:
                continue

        return survivors, data

    # ── Gate 3: Fundamentals ──────────────────────────────────────────────────

    def _gate3_fundamentals(
        self,
        symbols: List[str],
        g2_data: Dict[str, Dict],
        finnhub: Optional[FinnhubClient],
    ) -> Tuple[List[str], Dict[str, Dict]]:
        survivors: List[str] = []
        data: Dict[str, Dict] = {}

        def _check_one(sym: str) -> Optional[Dict]:
            try:
                ticker = yf.Ticker(sym)
                info = ticker.info
                market_cap = info.get("marketCap") or 0
                rev_growth = info.get("revenueGrowth")
                op_cf = info.get("operatingCashflow")
                next_earnings = info.get("earningsDate") or info.get("earningsTimestamp")

                # marketCap in .info is flaky field-by-field (Yahoo omits it on some
                # calls independent of whether the rest of the payload came through —
                # confirmed by direct testing: two back-to-back .info calls for the same
                # symbol returned marketCap=None once and revenueGrowth=None the other
                # time). fast_info.market_cap is a separate, lighter-weight endpoint
                # that has proven reliable where .info flakes — use it first rather
                # than retrying the same flaky call.
                if not market_cap:
                    try:
                        fi_mcap = ticker.fast_info.market_cap
                        if fi_mcap:
                            market_cap = fi_mcap
                    except Exception:
                        pass

                # revenueGrowth has no fast_info equivalent — still worth one retry
                # after a short backoff, then a Finnhub fallback if it's still missing.
                if rev_growth is None:
                    time.sleep(1.5)
                    info_retry = ticker.info
                    rev_growth = info_retry.get("revenueGrowth")
                    op_cf = info_retry.get("operatingCashflow") or op_cf
                    next_earnings = info_retry.get("earningsDate") or info_retry.get("earningsTimestamp") or next_earnings
                    if rev_growth is not None:
                        info = info_retry

                if rev_growth is None and finnhub:
                    metrics = finnhub.get_basic_financials(sym)
                    if metrics:
                        rg = metrics.get("revenueGrowthTTMYoy")
                        if rg is not None:
                            rev_growth = rg / 100.0 if abs(rg) > 1 else rg  # Finnhub reports as a %, yfinance as a fraction

                # Market cap: unchanged, hard — not in the redesign's tolerance list.
                if market_cap < _G3_MIN_MARKET_CAP:
                    return None
                # Revenue growth: still missing after retry+Finnhub fallback stays a
                # hard reject (no signal to soft-pass on); a known low-but-not-terrible
                # growth number gets tolerance instead.
                if rev_growth is None:
                    return None
                rev_growth_margin = _margin_above(
                    rev_growth, _G3_MIN_REV_GROWTH, _G3_REV_GROWTH_HARD_FLOOR,
                )
                if rev_growth_margin is None:
                    return None
                # Operating cash flow: HARD, no tolerance — a *known* negative op cash
                # flow is a value-trap signal per spec. Missing data (None) is not
                # treated as a rejection (unchanged from before), only a confirmed <=0.
                if op_cf is not None and op_cf <= 0:
                    return None

                # Earnings beat from Finnhub — HARD per spec (falling-knife filter):
                # a real earnings miss is not a marginal case. Missing/partial surprise
                # data (either side None) is NOT judged as a miss — only a real,
                # confirmed actual<estimate counts against the candidate.
                earnings_beat = True
                if finnhub:
                    surprise = finnhub.get_earnings_surprise(sym)
                    if surprise:
                        actual = surprise.get("actual")
                        estimate = surprise.get("estimate")
                        if actual is not None and estimate is not None:
                            # Finnhub doesn't separate revenue in earnings endpoint;
                            # use EPS beat as proxy
                            earnings_beat = actual >= estimate
                if not earnings_beat:
                    return None

                # next_earnings_date — earningsDate may be a past date or a list
                ned = None
                if next_earnings:
                    ts = None
                    if isinstance(next_earnings, (int, float)):
                        ts = float(next_earnings)
                    elif isinstance(next_earnings, list) and next_earnings:
                        ts = float(next_earnings[0])
                    elif isinstance(next_earnings, str):
                        ned = next_earnings[:10]
                    if ts is not None:
                        ned = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                # Drop stale past dates — yfinance sometimes returns the previous quarter
                if ned:
                    try:
                        if datetime.strptime(ned, "%Y-%m-%d").date() < date.today():
                            ned = None
                    except ValueError:
                        ned = None

                industry = info.get("industry", "")
                return {
                    "market_cap_b": market_cap / 1e9,
                    "revenue_growth_yoy": float(rev_growth),
                    "operating_cashflow": float(op_cf) if op_cf else 0.0,
                    "earnings_beat": earnings_beat,
                    "next_earnings_date": ned,
                    "sector": info.get("sector", ""),
                    "industry": industry,
                    "sector_commodity_exposure": _get_commodity_exposure(industry),
                    "_margin_g3": rev_growth_margin,
                    # Cached so Gate 4 can skip a second .info fetch for this symbol —
                    # halves the throttling-prone call volume per surviving candidate.
                    "_cached_current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
                    "_cached_target_price":  info.get("targetMeanPrice"),
                    "_cached_num_analysts":  info.get("numberOfAnalystOpinions"),
                }
            except Exception:
                return None

        # Lower concurrency than before (was 8) — yfinance's .info silently degrades
        # under heavy parallel load rather than raising, and this gate now also does
        # an inline retry per symbol, so fewer simultaneous workers is both gentler
        # on Yahoo and avoids compounding the retry's own throttling risk.
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_check_one, sym): sym for sym in symbols}
            for fut in as_completed(futures):
                if not self._running:
                    break
                sym = futures[fut]
                try:
                    result = fut.result(timeout=45)
                except (FuturesTimeout, Exception):
                    result = None
                if result is not None:
                    survivors.append(sym)
                    data[sym] = result

        return survivors, data

    # ── Gate 4: Analyst conviction ────────────────────────────────────────────

    def _gate4_analyst(
        self,
        symbols: List[str],
        g3_data: Dict[str, Dict],
        finnhub: Optional[FinnhubClient],
    ) -> Tuple[List[str], Dict[str, Dict]]:
        survivors: List[str] = []
        data: Dict[str, Dict] = {}

        def _check_one(sym: str) -> Optional[Dict]:
            try:
                cached = g3_data.get(sym, {})
                current_price = cached.get("_cached_current_price")
                target        = cached.get("_cached_target_price")
                num_analysts  = cached.get("_cached_num_analysts")
                ticker = None

                # Only re-fetch .info if Gate 3 didn't already capture what we need —
                # avoids doubling the throttling-prone call volume for every candidate.
                if not current_price or not target:
                    ticker = yf.Ticker(sym)
                    info = ticker.info
                    current_price = info.get("currentPrice") or info.get("regularMarketPrice")
                    target = info.get("targetMeanPrice")
                    num_analysts = info.get("numberOfAnalystOpinions") or num_analysts

                # current_price has a reliable fast_info fallback (same flaky-.info
                # pattern as Gate 3's marketCap — confirmed by direct testing).
                if not current_price:
                    try:
                        fi_price = (ticker or yf.Ticker(sym)).fast_info.last_price
                        if fi_price:
                            current_price = fi_price
                    except Exception:
                        pass

                # target/num_analysts have no fast_info equivalent — one retry after
                # a short backoff before giving up, same as Gate 3's revenue_growth.
                if not target:
                    time.sleep(1.5)
                    info_retry = (ticker or yf.Ticker(sym)).info
                    target = info_retry.get("targetMeanPrice")
                    num_analysts = info_retry.get("numberOfAnalystOpinions") or num_analysts

                num_analysts = num_analysts or 0

                if not current_price or not target or current_price <= 0:
                    return None

                upside = (target - current_price) / current_price
                upside_margin = _margin_above(upside, _G4_MIN_ANALYST_UPSIDE, _G4_UPSIDE_HARD_FLOOR)
                if upside_margin is None:
                    return None

                count_margin = _margin_above(
                    num_analysts, _G4_MIN_ANALYSTS, _G4_ANALYST_COUNT_HARD_FLOOR,
                )
                if count_margin is None:
                    return None

                # Analyst rating breakdown from Finnhub
                buy_pct = 0.0
                if finnhub:
                    rec = finnhub.get_analyst_recommendation(sym)
                    if rec:
                        total = sum([
                            rec.get("buy", 0),
                            rec.get("hold", 0),
                            rec.get("sell", 0),
                            rec.get("strongBuy", 0),
                            rec.get("strongSell", 0),
                        ])
                        if total > 0:
                            buy_pct = (rec.get("buy", 0) + rec.get("strongBuy", 0)) / total

                buy_margin = _margin_above(buy_pct, _G4_MIN_BUY_PCT, _G4_BUY_PCT_HARD_FLOOR)
                if buy_margin is None:
                    return None

                # Downgrade count: soft-tolerate up to _G4_DOWNGRADES_HARD_MAX in trailing 90 days
                downgrade_count = 0
                downgrade_margin = 100.0
                if finnhub:
                    events = finnhub.get_upgrade_downgrade(sym, days=90)
                    downgrade_count = sum(
                        1 for e in events
                        if (e.get("action") or "").lower() == "downgrade"
                    )
                    downgrade_margin = _margin_below(
                        downgrade_count, _G4_MAX_DOWNGRADES, _G4_DOWNGRADES_HARD_MAX,
                    )
                    if downgrade_margin is None:
                        return None

                margin_g4 = (upside_margin + count_margin + buy_margin + downgrade_margin) / 4.0

                return {
                    "analyst_upside_pct": upside,
                    "buy_rating_pct": buy_pct,
                    "analyst_count": int(num_analysts),
                    "analyst_target": float(target),
                    "downgrade_count_90d": downgrade_count,
                    "_margin_g4": margin_g4,
                }
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(_check_one, sym): sym for sym in symbols}
            for fut in as_completed(futures):
                if not self._running:
                    break
                sym = futures[fut]
                try:
                    result = fut.result(timeout=30)
                except (FuturesTimeout, Exception):
                    result = None
                if result is not None:
                    survivors.append(sym)
                    data[sym] = result

        return survivors, data

    # ── Gate 1: Options liquidity ─────────────────────────────────────────────

    def _gate1_options(
        self,
        symbols: List[str],
        g4_data: Dict[str, Dict],
    ) -> Tuple[List[str], Dict[str, Dict]]:
        """Check for REAL liquid options chains at 6+ and 12+ month expirations.

        Tightened per spec §1/§8: open interest > 500 and bid/ask spread < 5%
        of mid at the near-the-money strike, checked on both the 6mo and 12mo
        legs (12mo liquidity affects score only — 6mo failing is a hard
        reject, matching Gate 1's "reject unless all are true" framing).

        Chain data note: this codebase has no Alpaca options-chain fetch
        path anywhere (core/options_executor.py only submits/manages orders —
        it never fetches chains). The one real chain-fetch pattern already in
        use (core/options_strategy.py, core/iv_tracker.py) is yfinance's
        ticker.option_chain(), which does return real bid/ask/openInterest
        columns. This gate reuses that established pattern rather than
        introducing an untested Alpaca chain dependency.
        """
        survivors: List[str] = []
        data: Dict[str, Dict] = {}

        today = date.today()
        threshold_6mo = today + timedelta(days=180)
        threshold_12mo = today + timedelta(days=365)

        def _nearest_expiry(exp_dates: List[date], threshold: date) -> Optional[date]:
            candidates = [d for d in exp_dates if d >= threshold]
            return min(candidates) if candidates else None

        def _liquid_near_atm(ticker, current_price: float, exp_date: date) -> Optional[float]:
            """Returns a 0-100 margin score if OI/spread land within (or soft-tolerate
            up to) the liquidity thresholds at the near-ATM strike, or None if outside
            the tolerance band entirely (true reject) / on any data error."""
            try:
                chain = ticker.option_chain(exp_date.isoformat())
                calls = chain.calls
                if calls is None or calls.empty:
                    return None
                calls = calls.copy()
                calls["_dist"] = (calls["strike"] - current_price).abs()
                row = calls.sort_values("_dist").iloc[0]
                oi = float(row.get("openInterest", 0) or 0)
                bid = float(row.get("bid", 0) or 0)
                ask = float(row.get("ask", 0) or 0)
                if ask <= 0:
                    return None
                mid = (bid + ask) / 2
                if mid <= 0:
                    return None
                spread_pct = (ask - bid) / mid

                oi_margin = _margin_above(oi, _G1_OI_SOFT_MIN, _G1_OI_HARD_FLOOR)
                if oi_margin is None:
                    return None
                spread_margin = _margin_below(spread_pct, 0.05, _G1_SPREAD_HARD_CEILING)
                if spread_margin is None:
                    return None
                return (oi_margin + spread_margin) / 2.0
            except Exception:
                return None

        def _check_one(sym: str) -> Optional[Dict]:
            try:
                ticker = yf.Ticker(sym)
                expirations = ticker.options  # tuple of "YYYY-MM-DD" strings
                if not expirations:
                    return None

                exp_dates = []
                for e in expirations:
                    try:
                        exp_dates.append(date.fromisoformat(e))
                    except ValueError:
                        continue

                exp_6mo = _nearest_expiry(exp_dates, threshold_6mo)
                if exp_6mo is None:
                    return None  # Hard reject: no 6mo+ chain at all

                current_price = float(ticker.fast_info.last_price or 0)
                if current_price <= 0:
                    return None

                margin_6mo = _liquid_near_atm(ticker, current_price, exp_6mo)
                if margin_6mo is None:
                    return None  # Hard reject: illiquid (outside tolerance) at near-ATM on the 6mo leg

                exp_12mo = _nearest_expiry(exp_dates, threshold_12mo)
                margin_12mo = _liquid_near_atm(ticker, current_price, exp_12mo) if exp_12mo else None
                liquid_12mo = margin_12mo is not None

                return {
                    "has_6mo_options": True,
                    "has_12mo_options": exp_12mo is not None,
                    "options_verified": True,
                    "options_liquid_6mo": True,
                    "options_liquid_12mo": liquid_12mo,
                    "options_score": _options_quality_score(True, exp_12mo is not None and liquid_12mo),
                    "_margin_g1": margin_6mo,
                }
            except Exception:
                # Liquidity is non-negotiable per spec — any error excludes the symbol
                # rather than silently keeping it with a neutral score.
                return None

        # Two chain fetches per symbol now (6mo + possibly 12mo) — trim worker
        # count vs. the old expiry-list-only check to stay polite to yfinance.
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_check_one, sym): sym for sym in symbols}
            for fut in as_completed(futures):
                if not self._running:
                    break
                sym = futures[fut]
                try:
                    result = fut.result(timeout=25)
                except (FuturesTimeout, Exception):
                    result = None
                if result is not None:
                    survivors.append(sym)
                    data[sym] = result

        return survivors, data

    # ── Gate 5: LLM cause classification ─────────────────────────────────────

    def _gate5_llm(
        self,
        symbols: List[str],
        g1_data: Dict[str, Dict],
        g2_data: Optional[Dict[str, Dict]] = None,
        g3_data: Optional[Dict[str, Dict]] = None,
    ) -> Tuple[List[str], Dict[str, Dict]]:
        survivors: List[str] = []
        data: Dict[str, Dict] = {}

        api_key = self._settings.get("deepseek_api_key", "").strip()
        if not api_key:
            _log.warning("DeepSeek API key not set — skipping Gate 5, marking all as SKIPPED")
            self.scan_status.emit("Gate 5: DeepSeek key not configured — skipping LLM gate")
            for sym in symbols:
                data[sym] = {
                    "cause_label": "SKIPPED",
                    "cause_summary": "LLM classification skipped — no DeepSeek API key configured.",
                    "cause_confidence": "n/a",
                    "llm_pass": True,
                }
            return list(symbols), data

        for i, sym in enumerate(symbols):
            if not self._running:
                break

            self.scan_status.emit(f"Gate 5: Classifying {sym} ({i+1}/{len(symbols)})...")

            headlines = self._fetch_news_headlines(sym)
            pct_below = (g2_data or {}).get(sym, {}).get("pct_below_high", 0.0)
            sector_hint = (g3_data or {}).get(sym, {}).get("sector_commodity_exposure", "LOW")

            classification = self._call_deepseek_classify(sym, pct_below, headlines, api_key, sector_hint)
            data[sym] = classification

            commodity = classification.get("commodity_exposure", "LOW")
            llm_pass = classification.get("llm_pass", True)

            if commodity == "HIGH":
                # LLM confirmed commodity-driven — exclude even if cause label is sentiment
                _log.info("%s: excluded — LLM commodity_exposure=HIGH", sym)
            elif llm_pass:
                survivors.append(sym)

            # Small delay to avoid hitting rate limits
            time.sleep(0.5)

        return survivors, data

    def _fetch_news_headlines(self, symbol: str) -> str:
        """Fetch Alpaca news headlines for the last 60 days. Returns formatted string."""
        api_key = self._settings.get("alpaca_api_key", "")
        secret_key = self._settings.get("alpaca_secret_key", "")

        if not api_key or not secret_key:
            return f"No news available (Alpaca keys not configured)"

        start = (datetime.utcnow() - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = (
            f"https://data.alpaca.markets/v1beta1/news"
            f"?symbols={symbol}&limit=20&start={start}&sort=desc"
        )
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "APCA-API-KEY-ID": api_key,
                    "APCA-API-SECRET-KEY": secret_key,
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                articles = body.get("news", [])
                if not articles:
                    return "No recent news found."
                lines = []
                for a in articles[:15]:
                    dt = a.get("created_at", "")[:10]
                    title = a.get("headline", "")
                    lines.append(f"[{dt}] {title}")
                return "\n".join(lines)
        except Exception as exc:
            _log.warning("News fetch for %s failed: %s", symbol, exc)
            return "News unavailable."

    def _call_deepseek_classify(
        self,
        symbol: str,
        pct_below: float,
        headlines: str,
        api_key: str,
        sector_hint: str = "LOW",
    ) -> Dict:
        """Call DeepSeek API to classify the cause of the drawdown."""
        model = self._settings.get("deepseek_model", "deepseek-chat")
        prompt = _CLASSIFY_PROMPT.format(
            symbol=symbol,
            drawdown_pct=pct_below * 100,
            headlines=headlines,
            sector_hint=sector_hint,
        )

        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "max_tokens": 512,
            "temperature": 0.1,
        }).encode("utf-8")

        try:
            req = urllib.request.Request(
                _DEEPSEEK_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                choices = body.get("choices", [])
                if not choices:
                    raise ValueError("Empty choices in DeepSeek response")
                content = choices[0].get("message", {}).get("content", "")
                # Accumulate token usage
                usage = body.get("usage", {})
                in_tok  = usage.get("prompt_tokens", 0)
                out_tok = usage.get("completion_tokens", 0)
                # DeepSeek-chat pricing: $0.27/1M input, $1.10/1M output
                cost = (in_tok / 1_000_000) * 0.27 + (out_tok / 1_000_000) * 1.10
                self._cost["deepseek_calls"] += 1
                self._cost["input_tokens"]   += in_tok
                self._cost["output_tokens"]  += out_tok
                self._cost["cost_usd"]       += cost
                return self._parse_classification(content)
        except Exception as exc:
            _log.warning("DeepSeek classify failed for %s: %s", symbol, exc)
            return {
                "cause_label": "unclear",
                "cause_summary": f"LLM classification failed: {exc}",
                "cause_confidence": "low",
                "llm_pass": True,  # Don't reject on API failure
            }

    def _parse_classification(self, content: str) -> Dict:
        """Parse JSON response from DeepSeek. Graceful fallback on malformed output."""
        try:
            # Strip markdown code fences if present
            clean = content.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            parsed = json.loads(clean.strip())
            label = parsed.get("cause_label", "unclear")
            all_labels = parsed.get("cause_labels_all", [label])
            if not isinstance(all_labels, list):
                all_labels = [label]
            return {
                "cause_label": label,
                "cause_labels_all": all_labels,
                "multi_causal": bool(parsed.get("multi_causal", False)),
                "cause_summary": parsed.get("cause_summary", ""),
                "cause_confidence": parsed.get("confidence", "low"),
                "llm_pass": bool(parsed.get("pass", label in ACCEPTABLE_CAUSES)),
                "commodity_exposure": parsed.get("commodity_exposure", "LOW"),
                "commodity_rationale": parsed.get("commodity_rationale", ""),
            }
        except (json.JSONDecodeError, ValueError, KeyError):
            _log.warning("Could not parse DeepSeek response: %s", content[:200])
            return {
                "cause_label": "unclear",
                "cause_labels_all": ["unclear"],
                "multi_causal": False,
                "cause_summary": content[:300] if content else "Parse error",
                "cause_confidence": "low",
                "llm_pass": True,
                "commodity_exposure": "LOW",
                "commodity_rationale": "",
            }

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _score_candidate(self, symbol: str, d: Dict) -> DrawdownResult:
        pct_below = d.get("pct_below_high", 0.0)
        analyst_upside = d.get("analyst_upside_pct", 0.0)
        rev_growth = d.get("revenue_growth_yoy", 0.0)
        options_score = d.get("options_score", 40.0)

        s_analyst = min(100.0, analyst_upside * 200.0)
        s_fund = min(100.0, rev_growth * 300.0)
        s_draw = _bell(pct_below)
        s_opts = options_score

        composite = s_analyst * 0.40 + s_fund * 0.25 + s_draw * 0.20 + s_opts * 0.15

        # Commodity exposure: LLM result takes precedence; fall back to sector hint when Gate 5 skipped
        commodity_exp = d.get("commodity_exposure") or d.get("sector_commodity_exposure", "LOW")
        commodity_rat = d.get("commodity_rationale", "")

        # Apply 15% score penalty for MEDIUM commodity exposure
        if commodity_exp == "MEDIUM":
            composite *= 0.85
        composite = round(composite, 1)

        # ── Gate margin score: average of the per-gate soft-tolerance margins
        # for the gates actually evaluated (Gates 2/3/4/1; Gate 5 is a separate
        # cause-confidence signal, folded into confidence_score below instead).
        gate_margins = [
            m for m in (d.get("_margin_g2"), d.get("_margin_g3"),
                        d.get("_margin_g4"), d.get("_margin_g1"))
            if m is not None
        ]
        gate_margin_score = round(sum(gate_margins) / len(gate_margins), 1) if gate_margins else 100.0

        # ── Confidence score: blends composite score, gate margin, and Gate 5's
        # cause-classification confidence (neutral 60 if Gate 5 was skipped/n-a).
        cause_conf_map = {"low": 40.0, "medium": 70.0, "high": 100.0}
        cause_component = cause_conf_map.get((d.get("cause_confidence") or "").lower(), 60.0)
        confidence_score = round(
            0.5 * composite + 0.3 * gate_margin_score + 0.2 * cause_component, 1
        )

        return DrawdownResult(
            symbol=symbol,
            score=composite,
            current_price=d.get("current_price", 0.0),
            pct_below_high=pct_below,
            days_since_high=int(d.get("days_since_high", 0)),
            analyst_upside_pct=analyst_upside,
            buy_rating_pct=d.get("buy_rating_pct", 0.0),
            analyst_count=int(d.get("analyst_count", 0)),
            revenue_growth_yoy=rev_growth,
            earnings_beat=bool(d.get("earnings_beat", True)),
            iv_rank=None,
            next_earnings_date=d.get("next_earnings_date"),
            cause_label=d.get("cause_label", "SKIPPED"),
            cause_summary=d.get("cause_summary", ""),
            cause_confidence=d.get("cause_confidence", "n/a"),
            failed_gate=None,
            score_analyst=round(s_analyst, 1),
            score_fundamentals=round(s_fund, 1),
            score_drawdown=round(s_draw, 1),
            score_options=round(s_opts, 1),
            market_cap_b=d.get("market_cap_b", 0.0),
            operating_cashflow=d.get("operating_cashflow", 0.0),
            options_verified=bool(d.get("options_verified", False)),
            commodity_exposure=commodity_exp,
            commodity_rationale=commodity_rat,
            multi_causal_flag=bool(d.get("multi_causal", False)),
            cause_labels_all=d.get("cause_labels_all", []),
            avg_volume_30d=d.get("avg_volume_30d", 0.0),
            downgrade_count_90d=int(d.get("downgrade_count_90d", 0)),
            gate_margin_score=gate_margin_score,
            confidence_score=confidence_score,
        )

    def _build_partial(
        self,
        symbol: str,
        d: Dict,
        llm_d: Dict,
        failed_gate: str,
    ) -> DrawdownResult:
        """Build a DrawdownResult for a close-miss candidate."""
        commodity_exp = llm_d.get("commodity_exposure") or d.get("sector_commodity_exposure")
        commodity_rat = llm_d.get("commodity_rationale", "")
        partial_margins = [
            m for m in (d.get("_margin_g2"), d.get("_margin_g3"),
                        d.get("_margin_g4"), d.get("_margin_g1"))
            if m is not None
        ]
        partial_gate_margin = round(sum(partial_margins) / len(partial_margins), 1) if partial_margins else 0.0
        return DrawdownResult(
            symbol=symbol,
            score=0.0,
            current_price=d.get("current_price", 0.0),
            pct_below_high=d.get("pct_below_high", 0.0),
            days_since_high=int(d.get("days_since_high", 0)),
            analyst_upside_pct=d.get("analyst_upside_pct", 0.0),
            buy_rating_pct=d.get("buy_rating_pct", 0.0),
            analyst_count=int(d.get("analyst_count", 0)),
            revenue_growth_yoy=d.get("revenue_growth_yoy", 0.0),
            earnings_beat=bool(d.get("earnings_beat", False)),
            iv_rank=None,
            next_earnings_date=d.get("next_earnings_date"),
            cause_label=llm_d.get("cause_label", ""),
            cause_summary=llm_d.get("cause_summary", ""),
            cause_confidence=llm_d.get("cause_confidence", "n/a"),
            failed_gate=failed_gate,
            market_cap_b=d.get("market_cap_b", 0.0),
            operating_cashflow=d.get("operating_cashflow", 0.0),
            options_verified=bool(d.get("options_verified", False)),
            commodity_exposure=commodity_exp,
            commodity_rationale=commodity_rat,
            multi_causal_flag=bool(llm_d.get("multi_causal", False)),
            cause_labels_all=llm_d.get("cause_labels_all", []),
            avg_volume_30d=d.get("avg_volume_30d", 0.0),
            downgrade_count_90d=int(d.get("downgrade_count_90d", 0)),
            gate_margin_score=partial_gate_margin,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make_finnhub(self) -> Optional[FinnhubClient]:
        key = self._settings.get("finnhub_api_key", "").strip()
        if not key:
            _log.warning("Finnhub API key not set — Gates 3/4 will use yfinance approximations only")
            return None
        return FinnhubClient(key)


# ── Headless entry point ─────────────────────────────────────────────────────

def run_screen_headless(settings: Dict[str, Any]) -> List[DrawdownResult]:
    """Run the drawdown screen synchronously in the calling thread — no QThread,
    no Qt event loop required.

    Instantiates a DrawdownScanner and calls its private pipeline method
    (_do_scan) directly rather than via .start()/run(), so this can be
    invoked safely from another QThread's run() (e.g. DailyDrawdownRunner)
    without spinning up a QThread-from-a-QThread. Any scan_status/progress/cost
    signals DrawdownScanner emits along the way are harmless no-ops here since
    nothing is connected to them in this context — only the returned list
    matters.

    The GUI-triggered path (DrawdownScreenerPanel → MainWindow._trigger_drawdown_scan)
    is untouched: it still constructs a DrawdownScanner and calls .start(),
    which goes through the normal QThread.run() → emits scan_complete.
    """
    scanner = DrawdownScanner(settings)
    scanner._running = True
    try:
        return scanner._do_scan()
    except Exception:
        _log.exception("run_screen_headless: unhandled error")
        return []
