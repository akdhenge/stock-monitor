from typing import Dict, List, Optional

import yfinance as yf
from PyQt5.QtCore import QThread, pyqtSignal


class PricePoller(QThread):
    prices_updated = pyqtSignal(dict)   # {symbol: float}
    poll_error = pyqtSignal(str)        # error message

    def __init__(self, symbols: List[str], interval_seconds: int = 60, parent=None):
        super().__init__(parent)
        self._symbols = list(symbols)
        self._interval = interval_seconds
        self._running = False

    def update_symbols(self, symbols: List[str]) -> None:
        self._symbols = list(symbols)

    def update_interval(self, interval_seconds: int) -> None:
        self._interval = interval_seconds

    def run(self) -> None:
        self._running = True
        while self._running:
            if self._symbols:
                prices = self._fetch_prices()
                if prices is not None:
                    self.prices_updated.emit(prices)
            # Sleep in 1-second chunks to allow clean shutdown
            for _ in range(self._interval):
                if not self._running:
                    break
                self.msleep(1000)

    def stop(self) -> None:
        self._running = False

    def _fetch_prices(self) -> Optional[Dict[str, float]]:
        try:
            tickers = yf.Tickers(" ".join(self._symbols))
            prices: Dict[str, float] = {}
            for sym in self._symbols:
                ticker = tickers.tickers.get(sym)
                if ticker is None:
                    continue
                try:
                    price = ticker.fast_info.last_price
                    if price is not None:
                        prices[sym] = float(price)
                except Exception:
                    pass
            return prices if prices else None
        except Exception as exc:
            self.poll_error.emit(str(exc))
            return None
