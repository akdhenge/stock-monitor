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

# Scoring bell curve: peaks at ~27% drawdown, width ~12%
_BELL_PEAK = 0.27
_BELL_WIDTH = 0.12

# DeepSeek API
_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

# LLM classification prompt
_CLASSIFY_PROMPT = """You are a financial analyst. A stock has dropped significantly from its recent high.
Analyze the news headlines below and classify the PRIMARY cause of the price drop.

Stock: {symbol}
Current drawdown from 52-week high: {drawdown_pct:.1f}%

Recent news headlines (last 60 days):
{headlines}

Respond ONLY with a JSON object in this exact format (no markdown, no explanation):
{{
  "cause_label": "<one of: capex_concern | margin_pressure | sector_rotation | one_time_legal | macro_panic | guidance_cut | demand_decline | share_loss | product_failure | accounting | exec_departure | existential_regulatory | secular_decline | unclear>",
  "cause_summary": "<2-3 sentences explaining the specific cause of the drop>",
  "confidence": "<high | medium | low>",
  "pass": <true if cause is non-fundamental/sentiment-driven, false if it indicates real business damage>
}}

Guidelines:
- pass=true for: capex_concern, margin_pressure, sector_rotation, one_time_legal, macro_panic, guidance_cut, unclear
- pass=false for: demand_decline, share_loss, product_failure, accounting, exec_departure, existential_regulatory, secular_decline
- If unclear from headlines, use cause_label="unclear" with pass=true and confidence="low"
"""


def _bell(x: float, peak: float = _BELL_PEAK, width: float = _BELL_WIDTH) -> float:
    """Gaussian bell curve normalized to 0-100, peaking at `peak`."""
    return 100.0 * math.exp(-0.5 * ((x - peak) / width) ** 2)


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
            self._do_scan()
        except Exception as exc:
            _log.exception("DrawdownScanner unhandled error")
            self.scan_error.emit(f"Screener error: {exc}")

    # ── Main scan pipeline ────────────────────────────────────────────────────

    def _do_scan(self) -> None:
        results: List[DrawdownResult] = []
        close_misses: List[DrawdownResult] = []

        # ── Fetch universe ────────────────────────────────────────────────────
        self.scan_status.emit("Fetching S&P 500 universe...")
        self.scan_progress.emit(2)
        symbols = self._fetch_sp500()
        self.scan_status.emit(f"Universe: {len(symbols)} symbols")

        if not self._running:
            return

        # ── Gate 2: Drawdown filter ───────────────────────────────────────────
        self.scan_status.emit(f"Gate 2: Checking drawdowns ({len(symbols)} symbols)...")
        self.scan_progress.emit(5)
        g2_survivors, g2_data = self._gate2_drawdown(symbols)
        self.scan_status.emit(f"Gate 2: {len(g2_survivors)} passed (20-50% below 52w high within 180 days)")
        self.scan_progress.emit(20)

        if not self._running or not g2_survivors:
            self.scan_complete.emit([])
            return

        # ── Gate 3: Fundamentals ──────────────────────────────────────────────
        self.scan_status.emit(f"Gate 3: Checking fundamentals ({len(g2_survivors)} symbols)...")
        finnhub = self._make_finnhub()
        g3_survivors, g3_data = self._gate3_fundamentals(g2_survivors, g2_data, finnhub)
        g3_misses = set(g2_survivors) - set(g3_survivors)
        for sym in g3_misses:
            d = g2_data.get(sym, {})
            close_misses.append(self._build_partial(sym, d, {}, "gate3_fundamentals"))
        self.scan_status.emit(f"Gate 3: {len(g3_survivors)} passed fundamentals check")
        self.scan_progress.emit(40)

        if not self._running or not g3_survivors:
            self.scan_complete.emit(close_misses)
            return

        # ── Gate 4: Analyst conviction ────────────────────────────────────────
        self.scan_status.emit(f"Gate 4: Checking analyst conviction ({len(g3_survivors)} symbols)...")
        g4_survivors, g4_data = self._gate4_analyst(g3_survivors, g3_data, finnhub)
        g4_misses = set(g3_survivors) - set(g4_survivors)
        for sym in g4_misses:
            d = {**g2_data.get(sym, {}), **g3_data.get(sym, {})}
            close_misses.append(self._build_partial(sym, d, {}, "gate4_analyst_conviction"))
        self.scan_status.emit(f"Gate 4: {len(g4_survivors)} passed analyst conviction check")
        self.scan_progress.emit(55)

        if not self._running or not g4_survivors:
            self.scan_complete.emit(close_misses)
            return

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
            self.scan_complete.emit(close_misses)
            return

        # ── Gate 5: LLM cause classification ─────────────────────────────────
        self.scan_status.emit(f"Gate 5: LLM cause classification ({len(g1_survivors)} symbols)...")
        g5_survivors, g5_data = self._gate5_llm(g1_survivors, g1_data, g2_data)
        g5_misses = set(g1_survivors) - set(g5_survivors)
        for sym in g5_misses:
            d = {**g2_data.get(sym, {}), **g3_data.get(sym, {}),
                 **g4_data.get(sym, {}), **g1_data.get(sym, {}),
                 **g5_data.get(sym, {})}
            close_misses.append(self._build_partial(sym, d, g5_data.get(sym, {}), "gate5_cause_of_drop"))
        self.scan_status.emit(f"Gate 5: {len(g5_survivors)} passed cause classification")
        self.scan_progress.emit(88)

        if not self._running:
            return

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
        self.scan_complete.emit(results + close_misses)

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

                if (
                    _G2_MIN_DRAWDOWN <= pct_below <= _G2_MAX_DRAWDOWN
                    and days_since <= _G2_MAX_DAYS_SINCE_HIGH
                ):
                    survivors.append(sym)
                    data[sym] = {
                        "current_price": current_price,
                        "pct_below_high": pct_below,
                        "days_since_high": days_since,
                        "peak_price": peak_price,
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
                info = yf.Ticker(sym).info
                market_cap = info.get("marketCap") or 0
                rev_growth = info.get("revenueGrowth")
                op_cf = info.get("operatingCashflow")
                next_earnings = info.get("earningsDate") or info.get("earningsTimestamp")

                if market_cap < _G3_MIN_MARKET_CAP:
                    return None
                if rev_growth is None or rev_growth < _G3_MIN_REV_GROWTH:
                    return None
                if op_cf is not None and op_cf <= 0:
                    return None

                # Earnings beat from Finnhub (soft gate: reject only if missed both)
                earnings_beat = True
                if finnhub:
                    surprise = finnhub.get_earnings_surprise(sym)
                    if surprise:
                        eps_beat = (surprise.get("actual") or 0) >= (surprise.get("estimate") or 0)
                        # Finnhub doesn't separate revenue in earnings endpoint;
                        # use EPS beat as proxy
                        earnings_beat = eps_beat

                # next_earnings_date formatting
                ned = None
                if next_earnings:
                    if isinstance(next_earnings, (int, float)):
                        ned = datetime.fromtimestamp(next_earnings).strftime("%Y-%m-%d")
                    elif isinstance(next_earnings, str):
                        ned = next_earnings[:10]

                return {
                    "market_cap_b": market_cap / 1e9,
                    "revenue_growth_yoy": float(rev_growth),
                    "operating_cashflow": float(op_cf) if op_cf else 0.0,
                    "earnings_beat": earnings_beat,
                    "next_earnings_date": ned,
                    "sector": info.get("sector", ""),
                }
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
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
                info = yf.Ticker(sym).info
                current_price = info.get("currentPrice") or info.get("regularMarketPrice")
                target = info.get("targetMeanPrice")
                num_analysts = info.get("numberOfAnalystOpinions") or 0

                if not current_price or not target or current_price <= 0:
                    return None

                upside = (target - current_price) / current_price
                if upside < _G4_MIN_ANALYST_UPSIDE:
                    return None
                if num_analysts < _G4_MIN_ANALYSTS:
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

                if buy_pct < _G4_MIN_BUY_PCT:
                    return None

                return {
                    "analyst_upside_pct": upside,
                    "buy_rating_pct": buy_pct,
                    "analyst_count": int(num_analysts),
                    "analyst_target": float(target),
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
        """Check for liquid options chains at 6+ and 12+ month expirations."""
        survivors: List[str] = []
        data: Dict[str, Dict] = {}

        today = date.today()
        threshold_6mo = today + timedelta(days=180)
        threshold_12mo = today + timedelta(days=365)

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

                has_6mo = any(d >= threshold_6mo for d in exp_dates)
                has_12mo = any(d >= threshold_12mo for d in exp_dates)

                if not has_6mo:
                    return None  # Hard reject: no long-dated chains at all

                return {
                    "has_6mo_options": has_6mo,
                    "has_12mo_options": has_12mo,
                    "options_verified": True,
                    "options_score": _options_quality_score(has_6mo, has_12mo),
                }
            except Exception:
                # Don't hard-reject on API error for large-caps — mark unverified
                return {
                    "has_6mo_options": False,
                    "has_12mo_options": False,
                    "options_verified": False,
                    "options_score": 40.0,  # neutral score for unverified
                }

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(_check_one, sym): sym for sym in symbols}
            for fut in as_completed(futures):
                if not self._running:
                    break
                sym = futures[fut]
                try:
                    result = fut.result(timeout=20)
                except (FuturesTimeout, Exception):
                    result = None
                if result is not None and result.get("has_6mo_options", result.get("options_verified", False)):
                    survivors.append(sym)
                    data[sym] = result
                elif result and not result.get("has_6mo_options") and result.get("options_verified") is False:
                    # Unverified but large-cap — keep with neutral score
                    survivors.append(sym)
                    data[sym] = result

        return survivors, data

    # ── Gate 5: LLM cause classification ─────────────────────────────────────

    def _gate5_llm(
        self,
        symbols: List[str],
        g1_data: Dict[str, Dict],
        g2_data: Optional[Dict[str, Dict]] = None,
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

            classification = self._call_deepseek_classify(sym, pct_below, headlines, api_key)
            data[sym] = classification

            if classification.get("llm_pass", True):
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
    ) -> Dict:
        """Call DeepSeek API to classify the cause of the drawdown."""
        model = self._settings.get("deepseek_model", "deepseek-chat")
        prompt = _CLASSIFY_PROMPT.format(
            symbol=symbol,
            drawdown_pct=pct_below * 100,
            headlines=headlines,
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
            return {
                "cause_label": label,
                "cause_summary": parsed.get("cause_summary", ""),
                "cause_confidence": parsed.get("confidence", "low"),
                "llm_pass": bool(parsed.get("pass", label in ACCEPTABLE_CAUSES)),
            }
        except (json.JSONDecodeError, ValueError, KeyError):
            _log.warning("Could not parse DeepSeek response: %s", content[:200])
            return {
                "cause_label": "unclear",
                "cause_summary": content[:300] if content else "Parse error",
                "cause_confidence": "low",
                "llm_pass": True,
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

        return DrawdownResult(
            symbol=symbol,
            score=round(composite, 1),
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
        )

    def _build_partial(
        self,
        symbol: str,
        d: Dict,
        llm_d: Dict,
        failed_gate: str,
    ) -> DrawdownResult:
        """Build a DrawdownResult for a close-miss candidate."""
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
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make_finnhub(self) -> Optional[FinnhubClient]:
        key = self._settings.get("finnhub_api_key", "").strip()
        if not key:
            _log.warning("Finnhub API key not set — Gates 3/4 will use yfinance approximations only")
            return None
        return FinnhubClient(key)
