"""
StockScanner — QThread that fetches a stock universe and scores each symbol.

Modes:
  'quick'    — Batch price/filter pass over S&P 500; fast; manual only.
  'deep'     — Full fundamental + technical scoring, S&P 500 (~500 stocks);
               run hourly; alerts on new score >= threshold OR new top-10.
  'complete' — Full scoring over all indices (up to 1500 stocks);
               run 1-3x/day; alerts on new top-5 + score >= threshold +
               sends daily simplified summary via Telegram.
"""
import json
import os
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import yfinance as yf
from PyQt5.QtCore import QThread, pyqtSignal

from core.scan_result import ScanResult

# Fallback universe if all Wikipedia fetches fail
_FALLBACK_SYMBOLS = [
    # S&P 500 large-caps
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "JPM", "V",
    "JNJ", "UNH", "XOM", "PG", "MA", "HD", "CVX", "MRK", "LLY", "ABBV",
    "PEP", "KO", "BAC", "COST", "AVGO", "MCD", "TMO", "CSCO", "ACN", "WMT",
    "ABT", "DHR", "TXN", "NEE", "CRM", "VZ", "PM", "INTC", "RTX", "BMY",
    "AMGN", "INTU", "HON", "QCOM", "T", "IBM", "GS", "CAT", "BA", "GE",
    # S&P 400 mid-caps
    "EXR", "ALLE", "AFG", "AYI", "BKH", "BC", "BIO", "BRX", "CASY", "CLH",
    "CMC", "CNX", "COLB", "CRUS", "CW", "DKS", "EAT", "EFC", "EME", "ENVA",
    "EXPO", "FAF", "FHN", "FR", "GATX", "GGG", "GPI", "HHC", "HLI", "HQY",
    "IDXX", "IEX", "ITT", "JWN", "KBH", "KNX", "LNW", "M", "MAN", "MDC",
    "MKL", "MOG-A", "MSA", "MSM", "MTZ", "NVT", "OGE", "ORI", "PNFP", "PNM",
    # S&P 600 small-caps
    "ABM", "ACAD", "AEIS", "ALEX", "AMBC", "AMSF", "ANDE", "ANF", "AOSL", "APOG",
    "AROC", "ASTH", "AVA", "AWR", "BL", "BMBL", "BOX", "BRC", "CADE", "CAKE",
    "CALM", "CAMT", "CASH", "CCOI", "CENT", "CEVA", "CHCO", "CHDN", "CLB", "CLFD",
    "COKE", "CONN", "COOK", "CPRX", "CRGY", "CSGS", "CSWI", "DORM", "DRQ", "ECPG",
    "ENS", "EPAC", "ETD", "EVRI", "FCFS", "FELE", "FLGT", "FULT", "GRBK", "HALO",
    # NASDAQ 100 extras
    "ADBE", "ADP", "ADSK", "ALGN", "ANSS", "CDNS", "CTAS", "DXCM", "EA", "EBAY",
    "FAST", "FTNT", "GEHC", "GILD", "ILMN", "KDP", "KLAC", "LRCX",
    "MCHP", "MDLZ", "MNST", "MRNA", "MRVL", "MTCH", "MU", "NXPI", "ODFL", "ON",
    "ORLY", "PANW", "PAYX", "PCAR", "PDD", "REGN", "ROST", "SBUX", "SGEN", "SNPS",
    "TEAM", "TMUS", "TTWO", "VRSK", "VRSN", "VRTX", "WBA", "WBD", "XEL",
]

# Wikipedia index sources: (url, table_index, symbol_column)
_INDEX_SOURCES = [
    ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", 0, "Symbol"),
    ("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", 0, "Symbol"),
    ("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", 0, "Symbol"),
    ("https://en.wikipedia.org/wiki/Nasdaq-100", 4, "Ticker"),
]

# Universe size cap for deep scan (S&P 500 only)
_DEEP_SCAN_UNIVERSE = 500


class StockScanner(QThread):
    quick_scan_complete    = pyqtSignal(list)   # List[ScanResult]
    deep_scan_complete     = pyqtSignal(list)   # List[ScanResult] sorted desc
    complete_scan_complete = pyqtSignal(list)   # List[ScanResult] sorted desc

    # symbol, total_score, value_score, growth_score, tech_score
    new_top5_entry  = pyqtSignal(str, float, float, float, float)
    # symbol, total_score  — deep: new entry >= threshold OR new top-10
    new_alert_entry = pyqtSignal(str, float)

    scan_progress = pyqtSignal(int)   # 0–100 %
    scan_status   = pyqtSignal(str)
    scan_error    = pyqtSignal(str)

    def __init__(
        self,
        mode: str = "quick",
        universe_size: int = 500,
        settings: Optional[Dict[str, Any]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._mode = mode
        self._universe_size = universe_size
        self._settings: Dict[str, Any] = settings or {}
        self._running = False
        self._previous_top5:  Set[str] = set()
        self._previous_top10: Set[str] = set()
        # Previous scored symbols {symbol: score} for threshold diffing
        self._previous_scores: Dict[str, float] = {}
        # For deep scan: caller may inject quick-scan candidates
        self._candidates: Optional[List[str]] = None

    def set_candidates(self, symbols: List[str]) -> None:
        self._candidates = list(symbols)

    def set_previous_top5(self, symbols: Set[str]) -> None:
        self._previous_top5 = set(symbols)

    def set_previous_top10(self, symbols: Set[str]) -> None:
        self._previous_top10 = set(symbols)

    def set_previous_scores(self, scores: Dict[str, float]) -> None:
        self._previous_scores = dict(scores)

    def stop(self) -> None:
        self._running = False

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self) -> None:
        self._running = True
        try:
            if self._mode == "quick":
                self._do_quick_scan()
            elif self._mode == "deep":
                self._do_deep_scan()
            elif self._mode == "complete":
                self._do_complete_scan()
        except Exception as exc:
            self.scan_error.emit(f"Scanner error: {exc}")

    # ── Universe ──────────────────────────────────────────────────────────────

    def fetch_universe(self, size: int = 500) -> List[str]:
        """
        Return a deduplicated symbol list (up to `size`) pulled from:
          S&P 500 → S&P 400 MidCap → S&P 600 SmallCap → NASDAQ 100.
        Falls back to hardcoded list if all sources fail.
        """
        import pandas as pd

        seen: Set[str] = set()
        combined: List[str] = []

        for url, tbl_idx, col in _INDEX_SOURCES:
            if len(combined) >= size:
                break
            try:
                tables = pd.read_html(url)
                for sym in tables[tbl_idx][col].tolist():
                    sym = str(sym).replace(".", "-").strip().upper()
                    if sym and sym not in seen:
                        seen.add(sym)
                        combined.append(sym)
                        if len(combined) >= size:
                            break
                self.scan_status.emit(
                    f"Universe: {len(combined)} symbols loaded…"
                )
            except Exception:
                continue

        return combined[:size] if combined else _FALLBACK_SYMBOLS[:size]

    # ── Mode 1: Quick Scan ────────────────────────────────────────────────────

    def _do_quick_scan(self) -> None:
        self.scan_status.emit("Fetching S&P 500 universe…")
        # Quick scan always uses S&P 500 only
        universe = self.fetch_universe(_DEEP_SCAN_UNIVERSE)

        self.scan_status.emit(f"Batch-fetching prices for {len(universe)} symbols…")
        self.scan_progress.emit(5)

        if not self._running:
            return

        candidates: List[ScanResult] = []
        chunk_size = 50
        chunks = [universe[i:i + chunk_size] for i in range(0, len(universe), chunk_size)]

        for ci, chunk in enumerate(chunks):
            if not self._running:
                return
            try:
                tickers = yf.Tickers(" ".join(chunk))
                for sym in chunk:
                    ticker = tickers.tickers.get(sym)
                    if ticker is None:
                        continue
                    try:
                        fi = ticker.fast_info
                        price = fi.last_price
                        high52 = fi.fifty_two_week_high
                        info = ticker.info
                        rev_growth = info.get("revenueGrowth")
                        pe = info.get("trailingPE")
                        if price is None or high52 is None or high52 <= 0:
                            continue
                        if price / high52 >= 0.70:
                            continue
                        if rev_growth is None or rev_growth <= 0:
                            continue
                        if pe is None:
                            continue
                        candidates.append(ScanResult(
                            symbol=sym,
                            score_value=0.0, score_growth=0.0,
                            score_technical=0.0, total_score=0.0,
                            pe_ratio=float(pe),
                            peg_ratio=info.get("pegRatio"),
                            debt_equity=info.get("debtToEquity"),
                            price=float(price),
                            week52_high=float(high52),
                            sector=info.get("sector"),
                            revenue_growth=float(rev_growth),
                            free_cash_flow=info.get("freeCashflow"),
                            roe=info.get("returnOnEquity"),
                            rsi=None, macd_bullish=None,
                            near_200d_ma=None, volume_spike=None,
                            scan_mode="quick",
                        ))
                    except Exception:
                        pass
            except Exception as exc:
                self.scan_error.emit(f"Batch fetch error (chunk {ci}): {exc}")

            self.scan_progress.emit(int(5 + (ci + 1) / len(chunks) * 90))

        self.scan_progress.emit(100)
        self.scan_status.emit(
            f"Quick scan complete — {len(candidates)} candidates found."
        )
        self.quick_scan_complete.emit(candidates)

    # ── Mode 2: Deep Scan (S&P 500 only, hourly) ─────────────────────────────

    def _do_deep_scan(self) -> None:
        if self._candidates is not None:
            symbols = self._candidates
        else:
            self.scan_status.emit("Fetching S&P 500 for deep scan…")
            symbols = self.fetch_universe(_DEEP_SCAN_UNIVERSE)

        results = self._do_full_scoring(symbols, scan_mode="deep")
        if results is None:
            return

        results.sort(key=lambda x: x.total_score, reverse=True)

        # Alert: new top-10 entry
        new_top10 = {r.symbol for r in results[:10]}
        for r in results[:10]:
            if r.symbol not in self._previous_top10:
                self.new_alert_entry.emit(r.symbol, r.total_score)
        self._previous_top10 = new_top10

        # Alert: new score >= threshold (handled in MainWindow via new_alert_entry)
        # Emit all results; MainWindow filters by threshold against previous_scores
        self._previous_scores = {r.symbol: r.total_score for r in results}

        # Keep only top 30 results
        results = results[:30]

        top_result = results[0] if results else None
        self.scan_progress.emit(100)
        self.scan_status.emit(
            f"Deep scan complete — {len(results)} scored, "
            f"top: {top_result.symbol if top_result else 'N/A'} "
            f"({top_result.total_score if top_result else 0})"
        )
        self.deep_scan_complete.emit(results)

    # ── Mode 3: Complete Scan (full universe, scheduled) ─────────────────────

    def _do_complete_scan(self) -> None:
        self.scan_status.emit(f"Fetching full universe (up to {self._universe_size} symbols)…")
        symbols = self.fetch_universe(self._universe_size)

        results = self._do_full_scoring(symbols, scan_mode="complete")
        if results is None:
            return

        results.sort(key=lambda x: x.total_score, reverse=True)

        # Alert: new top-5 entry
        new_top5 = {r.symbol for r in results[:5]}
        for r in results[:5]:
            if r.symbol not in self._previous_top5:
                self.new_top5_entry.emit(
                    r.symbol, r.total_score,
                    r.score_value, r.score_growth, r.score_technical
                )
        self._previous_top5 = new_top5

        # Store scores for threshold diffing (MainWindow handles Telegram)
        self._previous_scores = {r.symbol: r.total_score for r in results}

        # Keep only top 30 results
        results = results[:30]

        top_result = results[0] if results else None
        self.scan_progress.emit(100)
        self.scan_status.emit(
            f"Complete scan done — {len(results)} scored, "
            f"top: {top_result.symbol if top_result else 'N/A'} "
            f"({top_result.total_score if top_result else 0})"
        )
        self.complete_scan_complete.emit(results)

    # ── Shared full-scoring pipeline ──────────────────────────────────────────

    def _do_full_scoring(
        self, symbols: List[str], scan_mode: str
    ) -> Optional[List[ScanResult]]:
        """Fetch fundamentals + technicals for each symbol, score, return results."""
        total = len(symbols)
        if total == 0:
            self.scan_error.emit("No symbols to scan.")
            return None

        self.scan_status.emit(f"{scan_mode.capitalize()} scan: analyzing {total} symbols…")

        # Pre-load congressional data once for the entire scan
        tracked_politicians = [
            p.strip()
            for p in self._settings.get("congressional_tracked_politicians", "").split(",")
            if p.strip()
        ]
        self.scan_status.emit("Loading congressional trade data…")
        congressional_lookup = self._preload_congressional_data(tracked_politicians)

        results: List[ScanResult] = []
        sector_pe_data: Dict[str, List[float]] = {}

        for idx, sym in enumerate(symbols):
            if not self._running:
                return None
            self.scan_status.emit(f"[{scan_mode}] {sym} ({idx + 1}/{total})…")
            try:
                r = self._analyze_symbol(sym, scan_mode)
                if r is not None:
                    r.score_congressional = self._score_congressional(
                        congressional_lookup.get(sym.upper(), [])
                    )
                    results.append(r)
                    if r.sector and r.pe_ratio is not None:
                        sector_pe_data.setdefault(r.sector, []).append(r.pe_ratio)
            except Exception:
                pass

            self.scan_progress.emit(int((idx + 1) / total * 85))
            time.sleep(2)

        if not self._running:
            return None

        self.scan_status.emit("Computing sector median P/E…")
        sector_median_pe = self._compute_sector_median_pe(sector_pe_data)

        self.scan_status.emit("Scoring…")
        for r in results:
            med_pe = sector_median_pe.get(r.sector or "", None)
            r.score_value     = self._score_value(r, med_pe)
            r.score_growth    = self._score_growth(r)
            r.score_technical = self._score_technical(r)
            # Congressional bonus: up to +15 pts on top of base score
            cong_bonus = (r.score_congressional / 100.0) * 15.0
            r.total_score = round(
                r.score_value * 0.4 + r.score_growth * 0.3 + r.score_technical * 0.3
                + cong_bonus, 2
            )

        return results

    def _analyze_symbol(self, sym: str, scan_mode: str) -> Optional[ScanResult]:
        ticker = yf.Ticker(sym)
        info = ticker.info
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        high52 = info.get("fiftyTwoWeekHigh")

        rsi_val: Optional[float] = None
        macd_bullish: Optional[bool] = None
        near_200d_ma: Optional[bool] = None
        volume_spike: Optional[bool] = None
        volatility_20d: Optional[float] = None
        avg_volume_20d: Optional[float] = None

        try:
            hist = ticker.history(period="1y")
            if not hist.empty and len(hist) >= 20:
                rsi_val        = self._compute_rsi(hist["Close"])
                macd_bullish   = self._compute_macd_bullish(hist["Close"])
                near_200d_ma   = self._compute_near_200d_ma(hist["Close"], price)
                volume_spike   = self._compute_volume_spike(hist["Volume"])
                volatility_20d = self._compute_volatility_20d(hist["Close"])
                avg_volume_20d = self._compute_avg_volume_20d(hist["Volume"])
        except Exception:
            pass

        return ScanResult(
            symbol=sym,
            score_value=0.0, score_growth=0.0,
            score_technical=0.0, total_score=0.0,
            pe_ratio=info.get("trailingPE"),
            peg_ratio=info.get("pegRatio"),
            debt_equity=info.get("debtToEquity"),
            price=float(price) if price is not None else None,
            week52_high=float(high52) if high52 is not None else None,
            sector=info.get("sector"),
            revenue_growth=info.get("revenueGrowth"),
            free_cash_flow=info.get("freeCashflow"),
            roe=info.get("returnOnEquity"),
            rsi=rsi_val,
            macd_bullish=macd_bullish,
            near_200d_ma=near_200d_ma,
            volume_spike=volume_spike,
            scan_mode=scan_mode,
            volatility_20d=volatility_20d,
            avg_volume_20d=avg_volume_20d,
        )

    # ── Technical Indicators ──────────────────────────────────────────────────

    def _compute_rsi(self, close, length: int = 14) -> Optional[float]:
        try:
            delta = close.diff()
            gain  = delta.clip(lower=0).rolling(length).mean()
            loss  = (-delta.clip(upper=0)).rolling(length).mean()
            rs    = gain / loss.replace(0, float("nan"))
            rsi_series = 100 - (100 / (1 + rs))
            val = rsi_series.dropna()
            return round(float(val.iloc[-1]), 2) if not val.empty else None
        except Exception:
            return None

    def _compute_macd_bullish(self, close) -> Optional[bool]:
        try:
            ema12  = close.ewm(span=12, adjust=False).mean()
            ema26  = close.ewm(span=26, adjust=False).mean()
            macd   = ema12 - ema26
            signal = macd.ewm(span=9, adjust=False).mean()
            if len(macd) < 2:
                return None
            return bool(macd.iloc[-2] < signal.iloc[-2] and macd.iloc[-1] > signal.iloc[-1])
        except Exception:
            return None

    def _compute_near_200d_ma(self, close, price: Optional[float]) -> Optional[bool]:
        try:
            ma = float(close.iloc[-200:].mean() if len(close) >= 200 else close.mean())
            if ma <= 0 or price is None:
                return None
            return bool(abs(price - ma) / ma <= 0.05)
        except Exception:
            return None

    def _compute_volume_spike(self, volume) -> Optional[bool]:
        try:
            if len(volume) < 20:
                return None
            avg20  = float(volume.iloc[-21:-1].mean())
            latest = float(volume.iloc[-1])
            return bool(avg20 > 0 and latest > avg20 * 1.5)
        except Exception:
            return None

    def _compute_volatility_20d(self, close) -> Optional[float]:
        try:
            if len(close) < 21:
                return None
            returns = close.pct_change().dropna()
            vol = float(returns.iloc[-20:].std()) * (252 ** 0.5)
            return round(vol, 4)
        except Exception:
            return None

    def _compute_avg_volume_20d(self, volume) -> Optional[float]:
        try:
            if len(volume) < 20:
                return None
            return round(float(volume.iloc[-20:].mean()), 0)
        except Exception:
            return None

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _preload_congressional_data(
        self, tracked: List[str], days: int = 90
    ) -> Dict[str, List[dict]]:
        """Download House + Senate trade JSONs once; return {TICKER: [trades]} lookup."""
        import urllib.request as _req
        data_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data"))
        sources = [
            (
                os.path.join(data_dir, "house_trades_cache.json"),
                "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json",
                "representative",
                "district",
            ),
            (
                os.path.join(data_dir, "senate_trades_cache.json"),
                "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json",
                "senator",
                "state",
            ),
        ]

        cutoff = time.time() - days * 86400
        lookup: Dict[str, List[dict]] = {}

        for cache_path, url, name_field, loc_field in sources:
            all_trades = None
            try:
                if os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < 86400:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        all_trades = json.load(f)
            except Exception:
                pass
            if all_trades is None:
                try:
                    req = _req.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    with _req.urlopen(req, timeout=30) as resp:
                        all_trades = json.loads(resp.read().decode("utf-8"))
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(all_trades, f)
                except Exception:
                    continue

            for trade in all_trades:
                ticker = str(trade.get("ticker", "")).strip().upper().replace(".", "-")
                if not ticker or ticker == "--":
                    continue
                # Date filter
                tx_str = str(trade.get("transaction_date", ""))[:10]
                try:
                    import datetime as _dt
                    tx_ts = _dt.datetime.strptime(tx_str, "%Y-%m-%d").timestamp()
                    if tx_ts < cutoff:
                        continue
                except Exception:
                    pass
                # Politician filter
                raw_name = trade.get(name_field, "")
                if isinstance(raw_name, dict):
                    name = f"{raw_name.get('first_name','')} {raw_name.get('last_name','')}".strip()
                else:
                    name = str(raw_name)
                if tracked and not self._congressional_name_matches(name, tracked):
                    continue
                lookup.setdefault(ticker, []).append({
                    "date":           tx_str,
                    "representative": name,
                    "type":           trade.get("type", ""),
                    "amount":         trade.get("amount", ""),
                })

        return lookup

    @staticmethod
    def _congressional_name_matches(name: str, tracked: List[str]) -> bool:
        name_lower = name.lower()
        for politician in tracked:
            pol_lower = politician.lower()
            if pol_lower in name_lower:
                return True
            words = pol_lower.split()
            if len(words) >= 2 and all(w in name_lower for w in words):
                return True
        return False

    def _score_congressional(self, trades: List[dict]) -> float:
        """Score 0–100: tracked-politician buys raise score, sells lower it."""
        buys  = sum(1 for t in trades if "purchase" in t.get("type", "").lower())
        sells = sum(1 for t in trades if "sale" in t.get("type", "").lower())
        pts = min(buys * 50, 100)
        pts = max(pts - sells * 20, 0)
        return float(pts)

    def _compute_sector_median_pe(
        self, sector_pe_data: Dict[str, List[float]]
    ) -> Dict[str, float]:
        result: Dict[str, float] = {}
        for sector, pe_list in sector_pe_data.items():
            if pe_list:
                s = sorted(pe_list)
                n = len(s)
                result[sector] = (s[n // 2 - 1] + s[n // 2]) / 2 if n % 2 == 0 else s[n // 2]
        return result

    def _score_value(self, r: ScanResult, sector_median_pe: Optional[float]) -> float:
        pts = 0.0
        pe = r.pe_ratio
        if pe is not None and sector_median_pe and sector_median_pe > 0:
            ratio = pe / sector_median_pe
            if ratio < 0.50:   pts += 25
            elif ratio < 0.75: pts += 18
            elif ratio < 1.00: pts += 10
            elif ratio < 1.25: pts += 5
        peg = r.peg_ratio
        if peg is not None:
            if peg < 0.75:   pts += 25
            elif peg < 1.00: pts += 20
            elif peg < 1.50: pts += 10
        de = r.debt_equity
        if de is None:
            pts += 12
        elif de < 0.3:   pts += 25
        elif de < 0.7:   pts += 18
        elif de < 1.0:   pts += 10
        elif de < 1.5:   pts += 5
        price, high52 = r.price, r.week52_high
        if price is not None and high52 and high52 > 0:
            ratio = price / high52
            if ratio < 0.50:   pts += 25
            elif ratio < 0.60: pts += 22
            elif ratio < 0.70: pts += 18
            elif ratio < 0.80: pts += 10
            elif ratio < 0.90: pts += 5
        return min(pts, 100.0)

    def _score_growth(self, r: ScanResult) -> float:
        pts = 0.0
        rg = r.revenue_growth
        if rg is not None:
            if rg > 0.30:   pts += 40
            elif rg > 0.20: pts += 32
            elif rg > 0.10: pts += 24
            elif rg > 0.05: pts += 12
            elif rg > 0.00: pts += 6
        fcf = r.free_cash_flow
        if fcf is not None:
            if fcf > 1_000_000_000:   pts += 30
            elif fcf > 100_000_000:   pts += 22
            elif fcf > 0:             pts += 12
        roe = r.roe
        if roe is not None:
            if roe > 0.30:   pts += 30
            elif roe > 0.20: pts += 22
            elif roe > 0.12: pts += 14
            elif roe > 0.05: pts += 6
        return min(pts, 100.0)

    def _score_technical(self, r: ScanResult) -> float:
        pts = 0.0
        rsi = r.rsi
        if rsi is not None:
            if rsi < 20:   pts += 40
            elif rsi < 30: pts += 32
            elif rsi < 40: pts += 18
            elif rsi < 50: pts += 8
        if r.macd_bullish:  pts += 20
        if r.near_200d_ma:  pts += 20
        if r.volume_spike:  pts += 20
        return min(pts, 100.0)
