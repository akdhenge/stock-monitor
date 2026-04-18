"""
TickerLookupWorker — QThread that fetches and scores a single ticker on demand.
Reuses StockScanner's private analysis/scoring methods without running a full scan.
"""
from typing import Any, Dict, Optional

from PyQt5.QtCore import QThread, pyqtSignal

from core.scan_result import ScanResult
from core.stock_scanner import StockScanner


class TickerLookupWorker(QThread):
    lookup_complete = pyqtSignal(object)  # ScanResult
    lookup_error = pyqtSignal(str)

    def __init__(self, symbol: str, settings: Optional[Dict[str, Any]] = None, parent=None):
        super().__init__(parent)
        self._symbol = symbol.strip().upper()
        self._settings = settings or {}

    def run(self) -> None:
        try:
            scanner = StockScanner(mode="quick", settings=self._settings)
            r = scanner._analyze_symbol(self._symbol, "lookup")
            if r is None:
                self.lookup_error.emit(f"No data returned for {self._symbol}")
                return
            r.score_value = scanner._score_value(r, None)
            r.score_growth = scanner._score_growth(r)
            r.score_technical = scanner._score_technical(r)
            r.total_score = round(
                r.score_value * 0.4 + r.score_growth * 0.3 + r.score_technical * 0.3, 2
            )
            r.scan_mode = "lookup"
            self.lookup_complete.emit(r)
        except Exception as exc:
            self.lookup_error.emit(str(exc))
