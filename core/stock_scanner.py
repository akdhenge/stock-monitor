"""
StockScanner — QThread that fetches a stock universe and scores each symbol.

Modes:
  'quick'  — Mode 1: batch price/filter pass; emits quick_scan_complete
  'deep'   — Mode 2: per-symbol full fundamental + technical scoring; emits deep_scan_complete
"""
import time
from typing import Dict, List, Optional, Set, Tuple

import yfinance as yf
from PyQt5.QtCore import QThread, pyqtSignal

from core.scan_result import ScanResult

# Fallback universe if Wikipedia fetch fails
_FALLBACK_SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "JPM", "V",
    "JNJ", "UNH", "XOM", "PG", "MA", "HD", "CVX", "MRK", "LLY", "ABBV",
    "PEP", "KO", "BAC", "COST", "AVGO", "MCD", "TMO", "CSCO", "ACN", "WMT",
    "ABT", "DHR", "TXN", "NEE", "CRM", "VZ", "PM", "INTC", "RTX", "BMY",
    "AMGN", "INTU", "HON", "QCOM", "T", "IBM", "GS", "CAT", "BA", "GE",
]


class StockScanner(QThread):
    quick_scan_complete = pyqtSignal(list)   # List[ScanResult]
    deep_scan_complete = pyqtSignal(list)    # List[ScanResult] sorted desc
    # symbol, total_score, value_score, growth_score, tech_score
    new_top5_entry = pyqtSignal(str, float, float, float, float)
    scan_progress = pyqtSignal(int)          # 0–100 %
    scan_status = pyqtSignal(str)
    scan_error = pyqtSignal(str)

    def __init__(self, mode: str = "quick", universe_size: int = 200, parent=None):
        super().__init__(parent)
        self._mode = mode
        self._universe_size = universe_size
        self._running = False
        self._previous_top5: Set[str] = set()
        # For deep scan, caller may inject quick-scan candidates
        self._candidates: Optional[List[str]] = None

    def set_candidates(self, symbols: List[str]) -> None:
        """Inject pre-filtered candidates for deep scan (Mode 2 input)."""
        self._candidates = list(symbols)

    def set_previous_top5(self, symbols: Set[str]) -> None:
        self._previous_top5 = set(symbols)

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
        except Exception as exc:
            self.scan_error.emit(f"Scanner error: {exc}")

    # ── Universe ──────────────────────────────────────────────────────────────

    def fetch_universe(self, size: int = 200) -> List[str]:
        """Return list of S&P 500 symbols (up to `size`) from Wikipedia."""
        try:
            import pandas as pd
            tables = pd.read_html(
                "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
            )
            symbols = tables[0]["Symbol"].tolist()
            # Fix BRK.B → BRK-B style
            symbols = [str(s).replace(".", "-") for s in symbols]
            return symbols[:size]
        except Exception:
            return _FALLBACK_SYMBOLS[:size]

    # ── Mode 1: Quick Scan ────────────────────────────────────────────────────

    def _do_quick_scan(self) -> None:
        self.scan_status.emit("Fetching universe…")
        universe = self.fetch_universe(self._universe_size)

        self.scan_status.emit(f"Batch-fetching prices for {len(universe)} symbols…")
        self.scan_progress.emit(5)

        if not self._running:
            return

        candidates: List[ScanResult] = []
        # Batch in chunks of 50 to avoid yfinance timeouts
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
                        if price is None or high52 is None:
                            continue
                        # Filter: below 70% of 52-week high, positive rev growth, valid PE
                        if high52 <= 0:
                            continue
                        ratio = price / high52
                        if ratio >= 0.70:
                            continue
                        if rev_growth is None or rev_growth <= 0:
                            continue
                        if pe is None:
                            continue
                        r = ScanResult(
                            symbol=sym,
                            score_value=0.0,
                            score_growth=0.0,
                            score_technical=0.0,
                            total_score=0.0,
                            pe_ratio=float(pe),
                            peg_ratio=info.get("pegRatio"),
                            debt_equity=info.get("debtToEquity"),
                            price=float(price),
                            week52_high=float(high52),
                            sector=info.get("sector"),
                            revenue_growth=float(rev_growth),
                            free_cash_flow=info.get("freeCashflow"),
                            roe=info.get("returnOnEquity"),
                            rsi=None,
                            macd_bullish=None,
                            near_200d_ma=None,
                            volume_spike=None,
                            scan_mode="quick",
                        )
                        candidates.append(r)
                    except Exception:
                        pass
            except Exception as exc:
                self.scan_error.emit(f"Batch fetch error (chunk {ci}): {exc}")

            pct = int(5 + (ci + 1) / len(chunks) * 90)
            self.scan_progress.emit(pct)

        self.scan_progress.emit(100)
        self.scan_status.emit(
            f"Quick scan complete — {len(candidates)} candidates found."
        )
        self.quick_scan_complete.emit(candidates)

    # ── Mode 2: Deep Scan ─────────────────────────────────────────────────────

    def _do_deep_scan(self) -> None:
        if self._candidates is not None:
            symbols = self._candidates
        else:
            # Fall back to running a quick filter first
            self.scan_status.emit("No candidates supplied — running quick filter…")
            universe = self.fetch_universe(self._universe_size)
            symbols = universe[:50]  # Limit for standalone deep scan

        total = len(symbols)
        if total == 0:
            self.scan_error.emit("No symbols to deep-scan.")
            return

        self.scan_status.emit(f"Deep scan: {total} symbols…")
        results: List[ScanResult] = []
        sector_pe_data: Dict[str, List[float]] = {}

        for idx, sym in enumerate(symbols):
            if not self._running:
                break
            self.scan_status.emit(f"Analyzing {sym} ({idx + 1}/{total})…")
            try:
                r = self._analyze_symbol(sym)
                if r is not None:
                    results.append(r)
                    # Collect PE for sector median computation
                    if r.sector and r.pe_ratio is not None:
                        sector_pe_data.setdefault(r.sector, []).append(r.pe_ratio)
            except Exception:
                pass

            pct = int((idx + 1) / total * 85)
            self.scan_progress.emit(pct)
            # Respectful delay between calls
            time.sleep(2)

        if not self._running:
            return

        self.scan_status.emit("Computing sector median P/E…")
        sector_median_pe = self._compute_sector_median_pe(sector_pe_data)

        self.scan_status.emit("Scoring all symbols…")
        for r in results:
            med_pe = sector_median_pe.get(r.sector or "", None)
            r.score_value = self._score_value(r, med_pe)
            r.score_growth = self._score_growth(r)
            r.score_technical = self._score_technical(r)
            r.total_score = round(
                r.score_value * 0.4 + r.score_growth * 0.3 + r.score_technical * 0.3, 2
            )

        results.sort(key=lambda x: x.total_score, reverse=True)
        top20 = results[:20]

        # Alert on new top-5 entries
        new_top5_symbols = {r.symbol for r in results[:5]}
        for r in results[:5]:
            if r.symbol not in self._previous_top5:
                self.new_top5_entry.emit(
                    r.symbol, r.total_score, r.score_value,
                    r.score_growth, r.score_technical
                )
        self._previous_top5 = new_top5_symbols

        self.scan_progress.emit(100)
        self.scan_status.emit(
            f"Deep scan complete — top score: "
            f"{top20[0].symbol if top20 else 'N/A'} "
            f"({top20[0].total_score if top20 else 0})"
        )
        self.deep_scan_complete.emit(top20)

    def _analyze_symbol(self, sym: str) -> Optional[ScanResult]:
        ticker = yf.Ticker(sym)
        info = ticker.info
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        high52 = info.get("fiftyTwoWeekHigh")

        # Technical indicators from 1-year history
        rsi_val: Optional[float] = None
        macd_bullish: Optional[bool] = None
        near_200d_ma: Optional[bool] = None
        volume_spike: Optional[bool] = None

        try:
            hist = ticker.history(period="1y")
            if not hist.empty and len(hist) >= 20:
                rsi_val = self._compute_rsi(hist["Close"])
                macd_bullish = self._compute_macd_bullish(hist["Close"])
                near_200d_ma = self._compute_near_200d_ma(hist["Close"], price)
                volume_spike = self._compute_volume_spike(hist["Volume"])
        except Exception:
            pass

        return ScanResult(
            symbol=sym,
            score_value=0.0,       # computed later
            score_growth=0.0,
            score_technical=0.0,
            total_score=0.0,
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
            scan_mode="deep",
        )

    # ── Technical Indicators (manual, no pandas_ta) ───────────────────────────

    def _compute_rsi(self, close, length: int = 14) -> Optional[float]:
        try:
            import pandas as pd
            delta = close.diff()
            gain = delta.clip(lower=0).rolling(length).mean()
            loss = (-delta.clip(upper=0)).rolling(length).mean()
            rs = gain / loss.replace(0, float("nan"))
            rsi_series = 100 - (100 / (1 + rs))
            val = rsi_series.dropna()
            if val.empty:
                return None
            return round(float(val.iloc[-1]), 2)
        except Exception:
            return None

    def _compute_macd_bullish(self, close) -> Optional[bool]:
        """True if previous MACD < previous Signal AND current MACD > current Signal."""
        try:
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            macd = ema12 - ema26
            signal = macd.ewm(span=9, adjust=False).mean()
            if len(macd) < 2:
                return None
            prev_cross = macd.iloc[-2] < signal.iloc[-2]
            curr_cross = macd.iloc[-1] > signal.iloc[-1]
            return bool(prev_cross and curr_cross)
        except Exception:
            return None

    def _compute_near_200d_ma(self, close, price: Optional[float]) -> Optional[bool]:
        """True if current price is within 5% of 200-day MA."""
        try:
            if price is None or len(close) < 200:
                # Use whatever data we have
                ma = float(close.mean())
            else:
                ma = float(close.iloc[-200:].mean())
            if ma <= 0:
                return None
            ratio = abs(price - ma) / ma
            return bool(ratio <= 0.05)
        except Exception:
            return None

    def _compute_volume_spike(self, volume) -> Optional[bool]:
        """True if the latest volume > 1.5× the 20-day average."""
        try:
            if len(volume) < 20:
                return None
            avg20 = float(volume.iloc[-21:-1].mean())
            latest = float(volume.iloc[-1])
            if avg20 <= 0:
                return None
            return bool(latest > avg20 * 1.5)
        except Exception:
            return None

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _compute_sector_median_pe(
        self, sector_pe_data: Dict[str, List[float]]
    ) -> Dict[str, float]:
        result: Dict[str, float] = {}
        for sector, pe_list in sector_pe_data.items():
            if pe_list:
                sorted_pe = sorted(pe_list)
                n = len(sorted_pe)
                mid = n // 2
                if n % 2 == 0:
                    median = (sorted_pe[mid - 1] + sorted_pe[mid]) / 2
                else:
                    median = sorted_pe[mid]
                result[sector] = median
        return result

    def _score_value(self, r: ScanResult, sector_median_pe: Optional[float]) -> float:
        pts = 0.0

        # P/E vs sector median (25 pts)
        pe = r.pe_ratio
        if pe is not None and sector_median_pe and sector_median_pe > 0:
            ratio = pe / sector_median_pe
            if ratio < 0.50:
                pts += 25
            elif ratio < 0.75:
                pts += 18
            elif ratio < 1.00:
                pts += 10
            elif ratio < 1.25:
                pts += 5

        # PEG (25 pts)
        peg = r.peg_ratio
        if peg is not None:
            if peg < 0.75:
                pts += 25
            elif peg < 1.00:
                pts += 20
            elif peg < 1.50:
                pts += 10

        # D/E (25 pts)
        de = r.debt_equity
        if de is None:
            pts += 12  # neutral
        else:
            if de < 0.3:
                pts += 25
            elif de < 0.7:
                pts += 18
            elif de < 1.0:
                pts += 10
            elif de < 1.5:
                pts += 5

        # Price vs 52-week high (25 pts)
        price = r.price
        high52 = r.week52_high
        if price is not None and high52 and high52 > 0:
            ratio = price / high52
            if ratio < 0.50:
                pts += 25
            elif ratio < 0.60:
                pts += 22
            elif ratio < 0.70:
                pts += 18
            elif ratio < 0.80:
                pts += 10
            elif ratio < 0.90:
                pts += 5

        return min(pts, 100.0)

    def _score_growth(self, r: ScanResult) -> float:
        pts = 0.0

        # Revenue growth (40 pts)
        rg = r.revenue_growth
        if rg is not None:
            if rg > 0.30:
                pts += 40
            elif rg > 0.20:
                pts += 32
            elif rg > 0.10:
                pts += 24
            elif rg > 0.05:
                pts += 12
            elif rg > 0.00:
                pts += 6

        # FCF (30 pts)
        fcf = r.free_cash_flow
        if fcf is not None:
            if fcf > 1_000_000_000:
                pts += 30
            elif fcf > 100_000_000:
                pts += 22
            elif fcf > 0:
                pts += 12

        # ROE (30 pts)
        roe = r.roe
        if roe is not None:
            if roe > 0.30:
                pts += 30
            elif roe > 0.20:
                pts += 22
            elif roe > 0.12:
                pts += 14
            elif roe > 0.05:
                pts += 6

        return min(pts, 100.0)

    def _score_technical(self, r: ScanResult) -> float:
        pts = 0.0

        # RSI (40 pts)
        rsi = r.rsi
        if rsi is not None:
            if rsi < 20:
                pts += 40
            elif rsi < 30:
                pts += 32
            elif rsi < 40:
                pts += 18
            elif rsi < 50:
                pts += 8

        # MACD bullish crossover (20 pts)
        if r.macd_bullish:
            pts += 20

        # Near 200-day MA within 5% (20 pts)
        if r.near_200d_ma:
            pts += 20

        # Volume spike > 1.5× 20-day avg (20 pts)
        if r.volume_spike:
            pts += 20

        return min(pts, 100.0)
