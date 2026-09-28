"""
daily_drawdown_runner.py — fires the headless drawdown screen once per
calendar day at drawdown_scan_time_et, then hands results to MainWindow so
they can be persisted/displayed and forwarded to TraderAgent
(queue_propose_spreads) to run the options_debit_spread proposal/exit cycle.

Mirrors core/ta_batch_runner.py's daily-fire pattern: a standalone QThread
polling every 60s with a date-guard so it fires exactly once per day, rather
than piggybacking on MainWindow's shared 60s scheduler QTimer — this keeps
the (potentially slow, S&P-500-wide) screen off the GUI thread entirely.

Config keys (trader_config.json):
  drawdown_scan_time_et      str   "HH:MM" ET, default "07:30"
  last_drawdown_scan_date    str   internal — set after each run
"""
import logging
from datetime import datetime
from typing import Callable, Dict

import pytz
from PyQt5.QtCore import QThread, pyqtSignal

from core.portfolio import load_trader_config, save_trader_config

_log = logging.getLogger(__name__)
_EASTERN = pytz.timezone("US/Eastern")


class DailyDrawdownRunner(QThread):
    scan_complete = pyqtSignal(list)   # List[DrawdownResult]
    scan_status = pyqtSignal(str)

    def __init__(self, get_settings: Callable[[], Dict], parent=None):
        super().__init__(parent)
        self._get_settings = get_settings
        self._running = True

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        while self._running:
            try:
                self._maybe_run()
            except Exception:
                _log.exception("DailyDrawdownRunner tick error")
            for _ in range(60):
                if not self._running:
                    break
                self.msleep(1000)

    def _maybe_run(self) -> None:
        config = load_trader_config()
        scan_time_str = config.get("drawdown_scan_time_et", "07:30")
        last_run_date = config.get("last_drawdown_scan_date", "")

        now_et = datetime.now(_EASTERN)
        today_str = now_et.strftime("%Y-%m-%d")
        if last_run_date == today_str:
            return

        try:
            hour, minute = (int(x) for x in scan_time_str.split(":"))
        except (ValueError, AttributeError):
            _log.warning("DailyDrawdownRunner: invalid drawdown_scan_time_et %r", scan_time_str)
            return
        if now_et.hour < hour or (now_et.hour == hour and now_et.minute < minute):
            return

        self.scan_status.emit("Drawdown screen: starting daily headless run...")
        _log.info("DailyDrawdownRunner: starting headless screen for %s", today_str)

        from core.drawdown_scanner import run_screen_headless

        settings = self._get_settings()
        try:
            results = run_screen_headless(settings)
        except Exception as exc:
            self.scan_status.emit(f"Drawdown screen ERROR: {exc}")
            _log.exception("DailyDrawdownRunner: headless screen failed")
            results = []

        # Always mark today as attempted so we don't retry every 60s on failure.
        cfg = load_trader_config()
        cfg["last_drawdown_scan_date"] = today_str
        save_trader_config(cfg)

        self.scan_status.emit(f"Drawdown screen: complete — {len(results)} results")
        self.scan_complete.emit(results)
