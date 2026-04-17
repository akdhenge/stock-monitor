import logging
from datetime import datetime
from typing import Dict, List, Optional, Set

from PyQt5.QtCore import Qt, QTimer

_log = logging.getLogger(__name__)
from PyQt5.QtWidgets import (
    QAction, QLabel, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QTabWidget, QToolBar, QVBoxLayout, QWidget,
)

from core.ai_followup import AIFollowUp
from core.ai_research_store import get_cached_entry
from core.ai_researcher import AIResearcher
from core.alert_manager import AlertManager
from core.models import AlertRecord, StockEntry
from core.price_poller import PricePoller
from core.scan_result import ScanResult
from core.scan_results_store import load_scan_results, save_scan_results
from core.settings_store import load_settings
from core.stock_scanner import StockScanner
from core.watchlist_store import load_watchlist, save_watchlist
from gui.add_edit_dialog import AddEditDialog
from gui.ai_research_dialog import AIResearchDialog
from gui.alert_history_panel import AlertHistoryPanel
from gui.log_panel import LogPanel, setup_log_handler
from gui.settings_dialog import SettingsDialog
from gui.smart_scanner_panel import SmartScannerPanel
from gui.watchlist_table import WatchlistTable
from notifiers.email_notifier import EmailNotifier
from notifiers.telegram_notifier import TelegramNotifier


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Stock Monitor")
        self.setMinimumSize(900, 600)

        self._watchlist: List[StockEntry] = load_watchlist()
        self._settings = load_settings()
        self._alert_manager = AlertManager(
            cooldown_minutes=self._settings.get("cooldown_minutes", 30)
        )
        self._alert_manager.set_alert_callback(self._on_alert_fired)

        # Scanner state
        self._scanner: Optional[StockScanner] = None
        self._scanner_top5:  Set[str] = set()
        self._scanner_top10: Set[str] = set()
        self._scanner_prev_scores: Dict[str, float] = {}
        self._quick_candidates: List[str] = []

        # Scheduler state
        self._last_deep_scan_dt: Optional[datetime] = None
        self._last_complete_scan_hhmm: str = ""   # prevents double-fire within same minute

        # Daily summary tracking (resets on app restart or date rollover)
        self._complete_summary_sent_date: Optional[str] = None
        self._deep_summary_sent_date: Optional[str] = None

        # Command poller
        self._cmd_poller = None

        # AI Research dialogs — hold references so they are not garbage-collected
        self._ai_dialogs: List[AIResearchDialog] = []

        # AI Research threads from Telegram /aiscan — prevent GC
        self._ai_researchers: List[AIResearcher] = []

        # AI follow-up threads — prevent GC
        self._ai_followups: List[AIFollowUp] = []

        # Last /aiscan research result per chat_id, used for follow-up questions
        self._aiscan_context: Dict[str, dict] = {}

        # Auto AI ranking state (top 10 after deep/complete scans)
        self._ai_rank_researchers: List[AIResearcher] = []
        self._ai_rank_pending: int = 0
        self._ai_rank_queue_idx: int = 0   # index of the next researcher to launch
        self._ai_rank_results: Dict[str, Optional[float]] = {}  # symbol -> raw score (None=error)
        self._ai_rank_top10: List[ScanResult] = []  # held for post-ranking finalization

        self._setup_ui()
        self._apply_settings(self._settings)
        self._watchlist_table.refresh(self._watchlist)

        QTimer.singleShot(500, self._start_poller)

        # 60-second tick for scheduled scans
        self._schedule_timer = QTimer(self)
        self._schedule_timer.timeout.connect(self._check_scheduled_scans)
        self._schedule_timer.start(60_000)

        self._load_saved_scan_results()

    # ── UI Setup ──────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu("File")
        settings_action = QAction("Settings…", self)
        settings_action.triggered.connect(self._open_settings)
        file_menu.addAction(settings_action)
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        toolbar = QToolBar("Main toolbar")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        for label, slot in [
            ("Add",          self._add_stock),
            ("Edit",         self._edit_stock),
            ("Remove",       self._remove_stock),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            toolbar.addWidget(btn)

        toolbar.addSeparator()
        refresh_btn = QPushButton("Refresh Now")
        refresh_btn.clicked.connect(self._manual_refresh)
        toolbar.addWidget(refresh_btn)

        toolbar.addSeparator()
        quick_btn = QPushButton("Quick Scan")
        quick_btn.clicked.connect(self._trigger_quick_scan)
        toolbar.addWidget(quick_btn)

        # Tab layout
        central = QWidget()
        self.setCentralWidget(central)
        vl = QVBoxLayout(central)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        self._tabs = QTabWidget()
        vl.addWidget(self._tabs)

        # Tab 1 — Watchlist
        wl_widget = QWidget()
        wl_layout = QVBoxLayout(wl_widget)
        wl_layout.setContentsMargins(6, 6, 6, 6)
        wl_layout.setSpacing(4)
        splitter = QSplitter(Qt.Vertical)
        self._watchlist_table = WatchlistTable()
        splitter.addWidget(self._watchlist_table)
        self._history_panel = AlertHistoryPanel()
        splitter.addWidget(self._history_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        wl_layout.addWidget(splitter)
        self._tabs.addTab(wl_widget, "Watchlist")

        # Tab 2 — Smart Scanner
        self._scanner_panel = SmartScannerPanel()
        self._scanner_panel.request_quick_scan.connect(self._trigger_quick_scan)
        self._scanner_panel.request_deep_scan.connect(self._trigger_deep_scan)
        self._scanner_panel.request_complete_scan.connect(self._trigger_complete_scan)
        self._scanner_panel.request_cancel_scan.connect(self._cancel_scan)
        self._scanner_panel.add_to_watchlist.connect(self._add_scanner_results)
        self._scanner_panel.research_requested.connect(self._on_research_requested)
        self._tabs.addTab(self._scanner_panel, "Smart Scanner")

        # Tab 3 — Logs
        self._log_panel = LogPanel()
        self._log_handler = setup_log_handler(self._log_panel)
        self._tabs.addTab(self._log_panel, "Logs")

        # Split status bar
        self._poll_status_label = QLabel("Not yet polled")
        self._scan_status_label = QLabel("")
        self.statusBar().addWidget(self._poll_status_label, stretch=1)
        self.statusBar().addPermanentWidget(self._scan_status_label)

    # ── Price Poller ──────────────────────────────────────────────────────────

    def _start_poller(self) -> None:
        symbols = [e.symbol for e in self._watchlist]
        interval = self._settings.get("poll_interval_seconds", 60)
        self._poller = PricePoller(symbols, interval)
        self._poller.prices_updated.connect(self._on_prices_updated)
        self._poller.poll_error.connect(self._on_poll_error)
        self._poller.start()

    def _restart_poller(self) -> None:
        if hasattr(self, "_poller") and self._poller.isRunning():
            self._poller.stop()
            self._poller.wait()
        self._start_poller()

    # ── Command Poller ────────────────────────────────────────────────────────

    def _start_command_poller(self) -> None:
        from notifiers.telegram_command_poller import TelegramCommandPoller
        token   = self._settings.get("telegram_token", "")
        chat_id = self._settings.get("telegram_chat_id", "")
        if not token or not chat_id:
            return
        self._cmd_poller = TelegramCommandPoller(token, chat_id)
        self._cmd_poller.cmd_add.connect(self._on_cmd_add)
        self._cmd_poller.cmd_remove.connect(self._on_cmd_remove)
        self._cmd_poller.cmd_list.connect(self._on_cmd_list)
        self._cmd_poller.cmd_scan.connect(self._on_cmd_scan)
        self._cmd_poller.cmd_top.connect(self._on_cmd_top)
        self._cmd_poller.cmd_detail.connect(self._on_cmd_detail)
        self._cmd_poller.cmd_aiscan.connect(self._on_cmd_aiscan)
        self._cmd_poller.cmd_aifollow.connect(self._on_cmd_aifollow)
        self._cmd_poller.cmd_stopaiscan.connect(self._on_cmd_stopaiscan)
        self._cmd_poller.cmd_mute.connect(self._on_cmd_mute)
        self._cmd_poller.cmd_revise.connect(self._on_cmd_revise)
        self._cmd_poller.poll_error.connect(
            lambda msg: self._poll_status_label.setText(f"Bot: {msg}")
        )
        self._cmd_poller.start()

    def _stop_command_poller(self) -> None:
        if self._cmd_poller is not None and self._cmd_poller.isRunning():
            self._cmd_poller.stop()
            self._cmd_poller.wait()
        self._cmd_poller = None

    # ── Scanner triggers ──────────────────────────────────────────────────────

    def _trigger_quick_scan(self) -> None:
        if self._scanner is not None and self._scanner.isRunning():
            return
        universe_size = self._settings.get("scanner_universe_size", 500)
        self._scanner = StockScanner(mode="quick", universe_size=universe_size, settings=self._settings)
        self._scanner.quick_scan_complete.connect(self._on_quick_scan_complete)
        self._scanner.scan_progress.connect(self._scanner_panel.update_progress)
        self._scanner.scan_status.connect(self._scanner_panel.update_status)
        self._scanner.scan_error.connect(
            lambda msg: self._scan_status_label.setText(f"Scan error: {msg}")
        )
        self._scanner.start()
        self._scanner_panel.set_scan_running("Quick Scan")
        self._scan_status_label.setText("Quick scan running…")
        self._tabs.setCurrentIndex(1)

    def _trigger_deep_scan(self, candidates: Optional[List[str]] = None) -> None:
        if self._scanner is not None and self._scanner.isRunning():
            return
        universe_size = self._settings.get("scanner_universe_size", 500)
        self._scanner = StockScanner(mode="deep", universe_size=universe_size, settings=self._settings)
        if candidates:
            self._scanner.set_candidates(candidates)
        elif self._quick_candidates:
            self._scanner.set_candidates(self._quick_candidates)
        self._scanner.set_previous_top10(self._scanner_top10)
        self._scanner.set_previous_scores(self._scanner_prev_scores)
        self._scanner.deep_scan_complete.connect(self._on_deep_scan_complete)
        self._scanner.new_alert_entry.connect(self._on_deep_alert_entry)
        self._scanner.scan_progress.connect(self._scanner_panel.update_progress)
        self._scanner.scan_status.connect(self._scanner_panel.update_status)
        self._scanner.scan_error.connect(
            lambda msg: (
                self._scan_status_label.setText(f"Scan error: {msg}"),
                _log.error("Deep scan error: %s", msg),
            )
        )
        self._scanner.start()
        self._scanner_panel.set_scan_running("Deep Scan")
        self._scan_status_label.setText("Deep scan running…")
        self._tabs.setCurrentIndex(1)

    def _trigger_complete_scan(self) -> None:
        if self._scanner is not None and self._scanner.isRunning():
            return
        universe_size = self._settings.get("scanner_universe_size", 500)
        self._scanner = StockScanner(mode="complete", universe_size=universe_size, settings=self._settings)
        self._scanner.set_previous_top5(self._scanner_top5)
        self._scanner.set_previous_scores(self._scanner_prev_scores)
        self._scanner.complete_scan_complete.connect(self._on_complete_scan_complete)
        self._scanner.new_top5_entry.connect(self._on_new_top5)
        self._scanner.scan_progress.connect(self._scanner_panel.update_progress)
        self._scanner.scan_status.connect(self._scanner_panel.update_status)
        self._scanner.scan_error.connect(
            lambda msg: self._scan_status_label.setText(f"Scan error: {msg}")
        )
        self._scanner.start()
        self._scanner_panel.set_scan_running("Complete Scan")
        self._scan_status_label.setText("Complete scan running…")
        self._tabs.setCurrentIndex(1)

    def _cancel_scan(self) -> None:
        if self._scanner is not None and self._scanner.isRunning():
            self._scanner.stop()
            self._scanner.wait()
        self._scanner_panel.set_scan_idle("Cancelled")
        self._scan_status_label.setText("Scan cancelled.")

    # ── Scheduler ─────────────────────────────────────────────────────────────

    def _check_scheduled_scans(self) -> None:
        now = datetime.now()
        current_hhmm = now.strftime("%H:%M")

        # Deep scan — hourly
        if self._settings.get("scanner_deep_scan_enabled"):
            interval_h = self._settings.get("scanner_deep_scan_interval_hours", 1)
            if self._last_deep_scan_dt is None:
                due = True
            else:
                elapsed = (now - self._last_deep_scan_dt).total_seconds() / 3600
                due = elapsed >= interval_h
            if due and (self._scanner is None or not self._scanner.isRunning()):
                self._last_deep_scan_dt = now
                self._trigger_deep_scan()

        # Complete scan — at configured times
        if self._settings.get("scanner_complete_scan_enabled"):
            times_str = self._settings.get("scanner_complete_scan_times_et", "")
            times = [t.strip() for t in times_str.split(",") if t.strip()]
            if current_hhmm in times and current_hhmm != self._last_complete_scan_hhmm:
                self._last_complete_scan_hhmm = current_hhmm
                if self._scanner is None or not self._scanner.isRunning():
                    self._trigger_complete_scan()

    def _load_saved_scan_results(self) -> None:
        results = load_scan_results()
        if results:
            # Use the timestamp of the most recent result so the label reflects the real scan time
            scan_time = max(r.timestamp for r in results)
            self._scanner_panel.display_results(results, scan_time=scan_time)
            self._scanner_top5  = {r.symbol for r in results[:5]}
            self._scanner_top10 = {r.symbol for r in results[:10]}
            self._scanner_prev_scores = {r.symbol: r.total_score for r in results}

            # Seed the last-scan timestamp so the scheduler doesn't immediately
            # trigger a new deep scan if the saved data is still fresh.
            self._last_deep_scan_dt = scan_time

    # ── Price Poller slots ────────────────────────────────────────────────────

    def _on_prices_updated(self, prices: Dict[str, float]) -> None:
        for entry in self._watchlist:
            if entry.symbol in prices:
                entry.current_price = prices[entry.symbol]
            self._alert_manager.check_and_alert(entry)
        self._watchlist_table.refresh(self._watchlist)
        self._poll_status_label.setText(
            f"Last updated: {datetime.now().strftime('%H:%M:%S')}"
        )

    def _on_poll_error(self, msg: str) -> None:
        self._poll_status_label.setText(f"Poll error: {msg}")

    def _on_alert_fired(self, record: AlertRecord) -> None:
        self._history_panel.add_record(record)

    # ── Scanner slots ─────────────────────────────────────────────────────────

    def _on_quick_scan_complete(self, results: List[ScanResult]) -> None:
        self._scanner_panel.display_results(results)
        self._scanner_panel.set_scan_idle("Quick Scan (complete)")
        self._quick_candidates = [r.symbol for r in results]
        self._scan_status_label.setText(
            f"Quick scan: {len(results)} candidates  ({datetime.now().strftime('%H:%M')})"
        )
        save_scan_results(results)

    def _on_deep_scan_complete(self, results: List[ScanResult]) -> None:
        _log.info("Deep scan complete — %d stocks scored; top: %s",
                  len(results),
                  ", ".join(f"{r.symbol}({r.total_score:.0f})" for r in results[:5]))
        self._scanner_panel.display_results(results)
        self._scanner_panel.set_scan_idle("Deep Scan (complete)")
        self._scanner_top10 = {r.symbol for r in results[:10]}
        threshold = self._settings.get("scanner_deep_alert_threshold", 60)

        # Alert: newly crossed threshold
        token   = self._settings.get("telegram_token", "")
        chat_id = self._settings.get("telegram_chat_id", "")
        if token and chat_id and self._settings.get("telegram_enabled"):
            for r in results:
                prev_score = self._scanner_prev_scores.get(r.symbol, 0.0)
                if r.total_score >= threshold and prev_score < threshold:
                    TelegramNotifier.send_message(
                        token, chat_id,
                        f"📈 <b>{r.symbol}</b> crossed score threshold!\n"
                        f"Score: <b>{r.total_score}</b>  "
                        f"(Val:{r.score_value} Grw:{r.score_growth} Tch:{r.score_technical})\n"
                        f"P/E:{r.pe_ratio or '—'}  RSI:{r.rsi or '—'}  "
                        f"Sector: {r.sector or '—'}"
                    )

            # Once-daily deep scan summary
            today = datetime.now().strftime("%Y-%m-%d")
            if self._deep_summary_sent_date != today:
                self._send_deep_scan_summary(results, token, chat_id)
                self._deep_summary_sent_date = today

        self._scanner_prev_scores = {r.symbol: r.total_score for r in results}
        self._scan_status_label.setText(
            f"Deep scan: {len(results)} scored  ({datetime.now().strftime('%H:%M')})"
        )
        save_scan_results(results)
        self._start_auto_ai_ranking(results)

    def _on_deep_alert_entry(self, symbol: str, score: float) -> None:
        """Fired by scanner for new top-10 entries during deep scan."""
        token   = self._settings.get("telegram_token", "")
        chat_id = self._settings.get("telegram_chat_id", "")
        if token and chat_id and self._settings.get("telegram_enabled"):
            TelegramNotifier.send_message(
                token, chat_id,
                f"🏆 <b>{symbol}</b> entered the Deep Scan Top-10!\n"
                f"Score: <b>{score}</b>"
            )

    def _on_complete_scan_complete(self, results: List[ScanResult]) -> None:
        self._scanner_panel.display_results(results)
        self._scanner_panel.set_scan_idle("Complete Scan (complete)")
        self._scanner_top5 = {r.symbol for r in results[:5]}
        threshold = self._settings.get("scanner_complete_alert_threshold", 60)

        token   = self._settings.get("telegram_token", "")
        chat_id = self._settings.get("telegram_chat_id", "")
        tg_on   = bool(token and chat_id and self._settings.get("telegram_enabled"))

        if tg_on:
            # Alert: newly crossed threshold
            for r in results:
                prev_score = self._scanner_prev_scores.get(r.symbol, 0.0)
                if r.total_score >= threshold and prev_score < threshold:
                    TelegramNotifier.send_message(
                        token, chat_id,
                        f"🌟 <b>{r.symbol}</b> crossed score threshold in Complete Scan!\n"
                        f"Score: <b>{r.total_score}</b>  "
                        f"(Val:{r.score_value} Grw:{r.score_growth} Tch:{r.score_technical})\n"
                        f"P/E:{r.pe_ratio or '—'}  RSI:{r.rsi or '—'}  "
                        f"Sector: {r.sector or '—'}"
                    )

            # Once-daily summary (top 10)
            today = datetime.now().strftime("%Y-%m-%d")
            if self._complete_summary_sent_date != today:
                self._send_complete_scan_summary(results, token, chat_id)
                self._complete_summary_sent_date = today

        self._scanner_prev_scores = {r.symbol: r.total_score for r in results}
        self._scan_status_label.setText(
            f"Complete scan: {len(results)} scored  ({datetime.now().strftime('%H:%M')})"
        )
        save_scan_results(results)
        self._start_auto_ai_ranking(results)

    def _on_new_top5(
        self, symbol: str, total: float, value: float, growth: float, tech: float
    ) -> None:
        token   = self._settings.get("telegram_token", "")
        chat_id = self._settings.get("telegram_chat_id", "")
        if token and chat_id and self._settings.get("telegram_enabled"):
            TelegramNotifier.send_message(
                token, chat_id,
                f"⭐ <b>{symbol}</b> entered the Complete Scan Top-5!\n"
                f"Score: <b>{total}</b>  "
                f"(Val:{value}  Grw:{growth}  Tch:{tech})"
            )

    def _send_complete_scan_summary(
        self, results: List[ScanResult], token: str, chat_id: str
    ) -> None:
        """Send a simplified top-10 summary after every complete scan."""
        top10 = results[:10]
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [f"📊 <b>Complete Scan Summary</b> — {now_str} ET\n"]
        prev = self._scanner_prev_scores

        for i, r in enumerate(top10, 1):
            prev_score = prev.get(r.symbol, None)
            if prev_score is None:
                tag = " 🆕"
            elif r.total_score > prev_score + 1:
                tag = " ↑"
            elif r.total_score < prev_score - 1:
                tag = " ↓"
            else:
                tag = ""
            color = "🟢" if r.total_score >= 65 else "🟡" if r.total_score >= 50 else "🟠"
            lines.append(
                f"{i}. {color} <b>{r.symbol}</b>  {r.total_score}{tag}"
                f"  <i>{r.sector or '—'}</i>"
            )

        lines.append("\nSend /detail for full breakdown table.")
        TelegramNotifier.send_message(token, chat_id, "\n".join(lines))

    def _send_deep_scan_summary(
        self, results: List[ScanResult], token: str, chat_id: str
    ) -> None:
        """Send a once-daily top-10 summary after a deep scan."""
        top10 = results[:10]
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [f"🔎 <b>Deep Scan Summary</b> — {now_str} ET\n"]
        prev = self._scanner_prev_scores

        for i, r in enumerate(top10, 1):
            prev_score = prev.get(r.symbol, None)
            if prev_score is None:
                tag = " 🆕"
            elif r.total_score > prev_score + 1:
                tag = " ↑"
            elif r.total_score < prev_score - 1:
                tag = " ↓"
            else:
                tag = ""
            color = "🟢" if r.total_score >= 65 else "🟡" if r.total_score >= 50 else "🟠"
            lines.append(
                f"{i}. {color} <b>{r.symbol}</b>  {r.total_score}{tag}"
                f"  <i>{r.sector or '—'}</i>"
            )

        lines.append("\nSend /detail for full breakdown table.")
        TelegramNotifier.send_message(token, chat_id, "\n".join(lines))

    # ── Auto AI Ranking ─────────────────────────────────────────────────────

    @staticmethod
    def _compute_ai_rank(result: dict) -> float:
        """Compute a composite AI rank (1.0–10.0) from sentiment, direction, timeframe."""
        sentiment_map = {"BULLISH": 3, "NEUTRAL": 2, "BEARISH": 1}
        direction_map = {"UP": 3, "SIDEWAYS": 2, "DOWN": 1}

        sentiment_score = sentiment_map.get(result.get("sentiment", "NEUTRAL"), 2)
        direction_score = direction_map.get(result.get("direction", "SIDEWAYS"), 2)

        # Timeframe: short-term keywords score higher
        timeframe = (result.get("timeframe") or "").lower()
        if any(kw in timeframe for kw in ("week", "days", "1-2", "2-4")):
            timeframe_score = 3
        elif any(kw in timeframe for kw in ("month", "1-3", "2-3")):
            timeframe_score = 2
        else:
            timeframe_score = 1

        raw = sentiment_score + direction_score + timeframe_score  # range [3, 9]
        rank = 1.0 + (raw - 3) * 1.5  # maps [3,9] -> [1.0, 10.0]
        return round(min(10.0, max(1.0, rank)), 1)

    def _start_auto_ai_ranking(self, results: List[ScanResult]) -> None:
        """Launch AI research for the top 10 scan results to compute AI ranks.

        Skipped entirely if all top-10 stocks already have cached research
        fresher than the `ai_rank_refresh_hours` setting (default 4 h).
        """
        top10 = results[:10]
        if not top10:
            return

        refresh_hours = self._settings.get("ai_rank_refresh_hours", 4)
        stale = [r for r in top10 if get_cached_entry(r.symbol) is None]
        if not stale:
            _log.info(
                "Auto AI ranking skipped — all top %d stocks have cached research "
                "< %d h old", len(top10), refresh_hours
            )
            # Lights are already green from _populate_table; nothing more to do
            return

        # Stop any previously running rank researchers
        for r in self._ai_rank_researchers:
            if r.isRunning():
                r.stop()
                r.wait(2000)
        self._ai_rank_researchers.clear()
        self._ai_rank_pending = 0
        self._ai_rank_results.clear()

        self._ai_rank_top10 = top10
        self._ai_rank_pending = len(top10)
        self._ai_rank_queue_idx = 0
        symbols = [r.symbol for r in top10]
        _log.info("Auto AI ranking started for %d stocks: %s", len(top10), ", ".join(symbols))
        self._scan_status_label.setText(
            f"AI ranking top {len(top10)} stocks... (0/{len(top10)})"
        )
        self._scanner_panel.set_ai_rank_status(
            f"AI Ranking: 0/{len(top10)} stocks", visible=True
        )

        # Create all researchers but do NOT start them yet — sequential launch via queue
        for r in top10:
            researcher = AIResearcher(
                r.symbol, r, self._settings, force_refresh=False, parent=None
            )
            researcher.research_complete.connect(
                lambda result, sym=r.symbol: self._on_auto_rank_complete(sym, result)
            )
            researcher.research_error.connect(
                lambda err, sym=r.symbol: self._on_auto_rank_error(sym, err)
            )
            self._ai_rank_researchers.append(researcher)

        # Start only the first one; subsequent ones launched in _on_auto_rank_complete/error
        self._launch_next_ai_rank_researcher()

    def _launch_next_ai_rank_researcher(self) -> None:
        """Start the next queued AIResearcher for auto-ranking (sequential mode)."""
        idx = self._ai_rank_queue_idx
        if idx >= len(self._ai_rank_researchers):
            return
        symbol = self._ai_rank_top10[idx].symbol
        total = len(self._ai_rank_researchers)
        _log.info("AI ranking: starting %s (%d/%d)", symbol, idx + 1, total)
        self._scanner_panel.set_research_light(symbol, "yellow")
        self._scanner_panel.set_ai_rank_status(f"AI researching {symbol} — {idx + 1}/{total}")
        # Pipe per-stock intermediate status (fetching news, calling LLM, timeout/retry) to detail line
        researcher = self._ai_rank_researchers[idx]
        researcher.research_status.connect(
            lambda msg, sym=symbol, n=idx + 1, t=total: self._scanner_panel.set_ai_rank_status(
                f"AI researching {sym} — {n}/{t}", msg
            )
        )
        researcher.start()

    def _on_auto_rank_complete(self, symbol: str, result: dict) -> None:
        raw = self._compute_ai_rank(result)
        self._ai_rank_results[symbol] = raw
        self._ai_rank_pending -= 1
        total = len(self._ai_rank_researchers)
        done = total - self._ai_rank_pending
        _log.info(
            "AI rank received for %s — raw=%.1f  sentiment=%s  direction=%s  (%d/%d done)",
            symbol, raw, result.get("sentiment"), result.get("direction"), done, total,
        )
        # Research is now cached — turn light green and refresh inline panel
        self._scanner_panel.set_research_light(symbol, "green")
        self._scanner_panel.refresh_research_panel(symbol)
        self._scanner_panel.set_ai_rank_status(
            f"AI researching {symbol} — {done}/{total}", "✓ Done"
        )
        # Advance queue
        self._ai_rank_queue_idx += 1
        self._launch_next_ai_rank_researcher()
        if self._ai_rank_pending > 0:
            self._scan_status_label.setText(
                f"AI ranking top {total} stocks... ({done}/{total})"
            )
        else:
            self._finalize_ai_ranking()
            self._scan_status_label.setText(f"AI ranking complete ({total}/{total})")

    def _on_auto_rank_error(self, symbol: str, error: str) -> None:
        self._ai_rank_results[symbol] = None  # will fall back to total_score ordering
        self._ai_rank_pending -= 1
        total = len(self._ai_rank_researchers)
        done = total - self._ai_rank_pending
        _log.warning("AI rank FAILED for %s: %s  (%d/%d done)", symbol, error, done, total)
        short_err = error.split("\n")[0][:80]
        self._scanner_panel.set_ai_rank_status(
            f"AI researching {symbol} — {done}/{total}", f"⚠ {short_err}"
        )
        # Light stays red — research not cached; advance queue to continue with next stock
        self._ai_rank_queue_idx += 1
        self._launch_next_ai_rank_researcher()
        if self._ai_rank_pending <= 0:
            self._finalize_ai_ranking()
            self._scan_status_label.setText(f"AI ranking complete ({done}/{total})")

    def _finalize_ai_ranking(self) -> None:
        """Assign unique integer ranks 1–N once all AI research is done.

        Primary sort: AI raw score descending (higher = more bullish/confident).
        Tiebreaker: total_score descending (falls back to scan ranking if AI is unsure).
        """
        entries = []
        for r in self._ai_rank_top10:
            raw = self._ai_rank_results.get(r.symbol)
            entries.append((r.symbol, raw if raw is not None else -1.0, r.total_score))

        entries.sort(key=lambda x: (x[1], x[2]), reverse=True)

        rank_lines = []
        for final_rank, (symbol, _raw, _total) in enumerate(entries, start=1):
            self._ai_rank_results[symbol] = float(final_rank)
            self._scanner_panel.update_ai_rank(symbol, float(final_rank))
            # Persist ai_rank back into the ScanResult so it survives app restarts
            for r in self._scanner_panel._results:
                if r.symbol == symbol:
                    r.ai_rank = final_rank
                    break
            rank_lines.append(f"#{final_rank} {symbol} (raw={_raw:.1f} score={_total:.1f})")

        _log.info("AI ranking finalized: %s", "  ".join(rank_lines))
        save_scan_results(self._scanner_panel._results)
        n = len(self._ai_rank_top10)
        self._scanner_panel.set_ai_rank_status(f"AI Ranking complete ✓  ({n} stocks ranked)", "")
        QTimer.singleShot(5000, lambda: self._scanner_panel.set_ai_rank_status("", visible=False))

    # ── Bot Command slots ─────────────────────────────────────────────────────

    def _on_cmd_add(
        self, symbol: str, low: float, high: float, notes: str, reply_chat_id: str
    ) -> None:
        token = self._settings.get("telegram_token", "")
        existing = next((e for e in self._watchlist if e.symbol == symbol), None)
        if existing is not None:
            existing.low_target = low
            existing.high_target = high
            if notes:
                existing.notes = notes
            save_watchlist(self._watchlist)
            self._watchlist_table.refresh(self._watchlist)
            TelegramNotifier.send_message(
                token, reply_chat_id,
                f"✏️ Updated <b>{symbol}</b> — Low: ${low}, High: ${high}"
                + (f"\nNotes: {notes}" if notes else "")
            )
            return
        entry = StockEntry(symbol=symbol, low_target=low, high_target=high, notes=notes)
        self._watchlist.append(entry)
        save_watchlist(self._watchlist)
        self._watchlist_table.refresh(self._watchlist)
        if hasattr(self, "_poller"):
            self._poller.update_symbols([e.symbol for e in self._watchlist])
        TelegramNotifier.send_message(
            token, reply_chat_id,
            f"✅ Added <b>{symbol}</b> — Low: ${low}, High: ${high}"
            + (f"\nNotes: {notes}" if notes else "")
        )

    def _on_cmd_remove(self, symbol: str, reply_chat_id: str) -> None:
        token = self._settings.get("telegram_token", "")
        before = len(self._watchlist)
        self._watchlist = [e for e in self._watchlist if e.symbol != symbol]
        if len(self._watchlist) < before:
            save_watchlist(self._watchlist)
            self._watchlist_table.refresh(self._watchlist)
            if hasattr(self, "_poller"):
                self._poller.update_symbols([e.symbol for e in self._watchlist])
            TelegramNotifier.send_message(
                token, reply_chat_id, f"✅ Removed <b>{symbol}</b> from watchlist."
            )
        else:
            TelegramNotifier.send_message(
                token, reply_chat_id, f"⚠️ {symbol} not found in watchlist."
            )

    def _on_cmd_list(self, reply_chat_id: str) -> None:
        token = self._settings.get("telegram_token", "")
        if not self._watchlist:
            TelegramNotifier.send_message(token, reply_chat_id, "Watchlist is empty.")
            return
        lines = ["<b>Watchlist:</b>"]
        for e in self._watchlist:
            price_str = f"${e.current_price:.2f}" if e.current_price else "—"
            lines.append(
                f"• <b>{e.symbol}</b>  price={price_str}  "
                f"low=${e.low_target}  high=${e.high_target}"
            )
        TelegramNotifier.send_message(token, reply_chat_id, "\n".join(lines))

    def _on_cmd_scan(self, reply_chat_id: str) -> None:
        token = self._settings.get("telegram_token", "")
        if self._scanner is not None and self._scanner.isRunning():
            TelegramNotifier.send_message(
                token, reply_chat_id, "⏳ A scan is already running."
            )
            return
        TelegramNotifier.send_message(token, reply_chat_id, "🔍 Starting quick scan…")
        self._trigger_quick_scan()

    def _on_cmd_top(self, reply_chat_id: str) -> None:
        """Simplified top-10 summary (same format as auto-sent after complete scan)."""
        token = self._settings.get("telegram_token", "")
        results = load_scan_results()
        if not results:
            TelegramNotifier.send_message(
                token, reply_chat_id,
                "No scan results yet. Run /scan or trigger a scan from the app."
            )
            return
        self._send_complete_scan_summary(results, token, reply_chat_id)

    def _on_cmd_detail(self, reply_chat_id: str) -> None:
        """Full top-20 table with all sub-scores and key metrics."""
        token = self._settings.get("telegram_token", "")
        results = load_scan_results()
        if not results:
            TelegramNotifier.send_message(
                token, reply_chat_id,
                "No scan results yet. Run /scan or trigger a scan from the app."
            )
            return
        top20 = results[:20]
        lines = [f"📋 <b>Full Scan Detail</b> — top {len(top20)} stocks\n"]
        for i, r in enumerate(top20, 1):
            lines.append(
                f"{i}. <b>{r.symbol}</b>  Total:<b>{r.total_score}</b>\n"
                f"   Val:{r.score_value}  Grw:{r.score_growth}  Tch:{r.score_technical}\n"
                f"   P/E:{r.pe_ratio or '—'}  PEG:{r.peg_ratio or '—'}  "
                f"D/E:{r.debt_equity or '—'}  RSI:{r.rsi or '—'}\n"
                f"   RevGrow:{f'{r.revenue_growth*100:.1f}%' if r.revenue_growth else '—'}  "
                f"ROE:{f'{r.roe*100:.1f}%' if r.roe else '—'}  "
                f"Sector:{r.sector or '—'}"
            )
        # Telegram has 4096 char limit — send in chunks if needed
        msg = "\n".join(lines)
        if len(msg) <= 4096:
            TelegramNotifier.send_message(token, reply_chat_id, msg)
        else:
            chunk: List[str] = [lines[0]]
            for line in lines[1:]:
                if sum(len(l) for l in chunk) + len(line) > 3800:
                    TelegramNotifier.send_message(token, reply_chat_id, "\n".join(chunk))
                    chunk = []
                chunk.append(line)
            if chunk:
                TelegramNotifier.send_message(token, reply_chat_id, "\n".join(chunk))

    def _on_cmd_aiscan(self, symbol: str, reply_chat_id: str) -> None:
        token = self._settings.get("telegram_token", "")
        TelegramNotifier.send_message(
            token, reply_chat_id,
            f"Starting AI research for <b>{symbol}</b>..."
        )

        # Look up ScanResult for the symbol (may be None — that's fine)
        results = load_scan_results()
        scan_result = next((r for r in results if r.symbol == symbol), None) if results else None

        researcher = AIResearcher(symbol, scan_result, self._settings, force_refresh=True)
        researcher.research_complete.connect(
            lambda result, cid=reply_chat_id: self._on_aiscan_complete(result, cid)
        )
        researcher.research_error.connect(
            lambda err, cid=reply_chat_id: TelegramNotifier.send_message(
                self._settings.get("telegram_token", ""), cid, f"AI Research error: {err}"
            )
        )
        researcher.finished.connect(
            lambda r=researcher: self._ai_researchers.remove(r) if r in self._ai_researchers else None
        )
        self._ai_researchers.append(researcher)
        researcher.start()

    def _on_aiscan_complete(self, result: dict, reply_chat_id: str) -> None:
        """Send the research report and open a follow-up session for the chat."""
        self._send_aiscan_telegram(result, reply_chat_id)
        # Store context and register follow-up session
        self._aiscan_context[reply_chat_id] = result
        if self._cmd_poller is not None:
            self._cmd_poller.register_followup_session(reply_chat_id, result.get("symbol", ""))
        token = self._settings.get("telegram_token", "")
        TelegramNotifier.send_message(
            token, reply_chat_id,
            "💬 <i>You can now ask follow-up questions about this stock for the next 30 minutes. Use /stopaiscan to stop</i>"
        )

    def _send_aiscan_telegram(self, result: dict, reply_chat_id: str) -> None:
        token = self._settings.get("telegram_token", "")
        sentiment = result.get("sentiment", "NEUTRAL")
        emoji = {"BULLISH": "\U0001f7e2", "BEARISH": "\U0001f534", "NEUTRAL": "\U0001f7e1"}.get(
            sentiment, "\U0001f7e1"
        )
        direction = result.get("direction", "SIDEWAYS")
        dir_emoji = {"UP": "\u2B06\uFE0F", "DOWN": "\u2B07\uFE0F", "SIDEWAYS": "\u27A1\uFE0F"}.get(
            direction, "\u27A1\uFE0F"
        )
        lines = [
            f"{emoji} <b>AI Research: {result.get('symbol', '?')}</b>  ({sentiment})\n",
            f"<b>Short-Term:</b> {result.get('short_term', 'N/A')}",
            f"<b>Long-Term:</b> {result.get('long_term', 'N/A')}",
            f"<b>Catalysts:</b> {result.get('catalysts', 'N/A')}",
            f"\n{dir_emoji} <b>Direction:</b> {direction}  |  <b>Timeframe:</b> {result.get('timeframe', 'N/A')}",
            f"<b>Congressional Signal:</b> {result.get('congressional_signal', 'NONE')}",
            f"<b>Stock Strategy:</b> {result.get('stock_strategy', 'N/A')}",
            f"<b>Options Strategy:</b> {result.get('options_strategy', 'N/A')}",
            f"\n<b>Summary:</b> {result.get('summary', 'N/A')}",
            f"\n<i>Source: {result.get('source', '?')} | {result.get('timestamp', '')}</i>",
        ]
        msg = "\n".join(lines)

        if len(msg) <= 4096:
            TelegramNotifier.send_message(token, reply_chat_id, msg)
        else:
            # Split into chunks that fit the Telegram limit
            chunk: List[str] = [lines[0]]
            for line in lines[1:]:
                if sum(len(l) for l in chunk) + len(line) + len(chunk) > 3800:
                    TelegramNotifier.send_message(token, reply_chat_id, "\n".join(chunk))
                    chunk = []
                chunk.append(line)
            if chunk:
                TelegramNotifier.send_message(token, reply_chat_id, "\n".join(chunk))

    def _on_cmd_aifollow(self, symbol: str, question: str, reply_chat_id: str) -> None:
        token = self._settings.get("telegram_token", "")
        research = self._aiscan_context.get(reply_chat_id)
        if not research:
            TelegramNotifier.send_message(
                token, reply_chat_id,
                f"⚠️ No active research context for {symbol}. Run /aiscan {symbol} first."
            )
            return
        TelegramNotifier.send_message(
            token, reply_chat_id,
            f"🤔 <i>Thinking about your question on {symbol}...</i>"
        )
        thread = AIFollowUp(symbol, research, question, self._settings)
        thread.followup_complete.connect(
            lambda answer, cid=reply_chat_id: TelegramNotifier.send_message(
                self._settings.get("telegram_token", ""), cid,
                f"💬 <b>{symbol} — Follow-up</b>\n\n{answer}"
            )
        )
        thread.followup_error.connect(
            lambda err, cid=reply_chat_id: TelegramNotifier.send_message(
                self._settings.get("telegram_token", ""), cid, f"AI error: {err}"
            )
        )
        thread.finished.connect(
            lambda t=thread: self._ai_followups.remove(t) if t in self._ai_followups else None
        )
        self._ai_followups.append(thread)
        thread.start()

    def _on_cmd_stopaiscan(self, reply_chat_id: str) -> None:
        token = self._settings.get("telegram_token", "")
        if reply_chat_id in self._aiscan_context:
            self._cmd_poller.clear_followup_session(reply_chat_id)
            self._aiscan_context.pop(reply_chat_id, None)
            TelegramNotifier.send_message(
                token, reply_chat_id,
                "✅ Follow-up session ended. Send /aiscan SYMBOL to start a new one."
            )
        else:
            TelegramNotifier.send_message(
                token, reply_chat_id,
                "ℹ️ No active follow-up session to stop."
            )

    def _on_cmd_mute(self, symbol: str, reply_chat_id: str) -> None:
        token = self._settings.get("telegram_token", "")
        entry = next((e for e in self._watchlist if e.symbol == symbol), None)
        if entry is None:
            TelegramNotifier.send_message(
                token, reply_chat_id,
                f"⚠️ {symbol} is not in your watchlist."
            )
            return
        self._alert_manager.mute_symbol(symbol)
        TelegramNotifier.send_message(
            token, reply_chat_id,
            f"🔕 Alerts for <b>{symbol}</b> silenced for the rest of today."
        )

    def _on_cmd_revise(self, symbol: str, side: str, new_price: float, reply_chat_id: str) -> None:
        token = self._settings.get("telegram_token", "")
        entry = next((e for e in self._watchlist if e.symbol == symbol), None)
        if entry is None:
            TelegramNotifier.send_message(
                token, reply_chat_id,
                f"⚠️ {symbol} is not in your watchlist."
            )
            return
        if side == "low":
            old = entry.low_target
            entry.low_target = new_price
            entry.last_low_alert = None  # reset cooldown so revised target takes effect immediately
        else:
            old = entry.high_target
            entry.high_target = new_price
            entry.last_high_alert = None
        save_watchlist(self._watchlist)
        self._watchlist_table.refresh(self._watchlist)
        TelegramNotifier.send_message(
            token, reply_chat_id,
            f"✏️ <b>{symbol}</b> {side} target updated: ${old:.2f} → <b>${new_price:.2f}</b>"
        )

    # ── Toolbar / watchlist actions ───────────────────────────────────────────

    def _add_stock(self) -> None:
        dlg = AddEditDialog(parent=self)
        if dlg.exec_() == AddEditDialog.Accepted:
            new_entry = dlg.get_entry()
            if new_entry.symbol in {e.symbol for e in self._watchlist}:
                QMessageBox.warning(
                    self, "Duplicate",
                    f"{new_entry.symbol} is already in the watchlist."
                )
                return
            self._watchlist.append(new_entry)
            save_watchlist(self._watchlist)
            self._watchlist_table.refresh(self._watchlist)
            if hasattr(self, "_poller"):
                self._poller.update_symbols([e.symbol for e in self._watchlist])

    def _edit_stock(self) -> None:
        row = self._watchlist_table.selected_row()
        if row < 0:
            QMessageBox.information(self, "Edit", "Select a stock to edit.")
            return
        entry = self._watchlist[row]
        dlg = AddEditDialog(entry=entry, parent=self)
        if dlg.exec_() == AddEditDialog.Accepted:
            updated = dlg.get_entry()
            entry.low_target  = updated.low_target
            entry.high_target = updated.high_target
            entry.notes       = updated.notes
            save_watchlist(self._watchlist)
            self._watchlist_table.refresh(self._watchlist)

    def _remove_stock(self) -> None:
        row = self._watchlist_table.selected_row()
        if row < 0:
            QMessageBox.information(self, "Remove", "Select a stock to remove.")
            return
        symbol = self._watchlist[row].symbol
        reply = QMessageBox.question(
            self, "Remove", f"Remove {symbol} from watchlist?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self._watchlist.pop(row)
            save_watchlist(self._watchlist)
            self._watchlist_table.refresh(self._watchlist)
            if hasattr(self, "_poller"):
                self._poller.update_symbols([e.symbol for e in self._watchlist])

    def _manual_refresh(self) -> None:
        self._restart_poller()

    def _add_scanner_results(self, results: List[ScanResult]) -> None:
        added = []
        existing = {e.symbol for e in self._watchlist}
        for r in results:
            if r.symbol in existing:
                continue
            low  = round(r.price * 0.90, 2) if r.price else 0.0
            high = round(r.price * 1.15, 2) if r.price else 0.0
            self._watchlist.append(StockEntry(
                symbol=r.symbol, low_target=low, high_target=high,
                notes=f"Added from scanner (score {r.total_score})",
            ))
            existing.add(r.symbol)
            added.append(r.symbol)
        if added:
            save_watchlist(self._watchlist)
            self._watchlist_table.refresh(self._watchlist)
            if hasattr(self, "_poller"):
                self._poller.update_symbols([e.symbol for e in self._watchlist])
            self._tabs.setCurrentIndex(0)
            QMessageBox.information(
                self, "Added to Watchlist", f"Added: {', '.join(added)}"
            )
        else:
            QMessageBox.information(
                self, "Added to Watchlist",
                "All selected stocks are already in the watchlist."
            )

    def _on_research_requested(self, symbol: str) -> None:
        scan_result = next(
            (r for r in self._scanner_panel._results if r.symbol == symbol), None
        )
        dlg = AIResearchDialog(symbol, scan_result, self._settings, parent=self)
        self._ai_dialogs.append(dlg)
        dlg.finished.connect(lambda _: self._ai_dialogs.remove(dlg) if dlg in self._ai_dialogs else None)
        # When dialog completes: turn light green and refresh the inline panel
        dlg.research_complete.connect(
            lambda sym: self._scanner_panel.set_research_light(sym, "green")
        )
        dlg.research_complete.connect(self._scanner_panel.refresh_research_panel)
        dlg.show()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(parent=self)
        if dlg.exec_() == SettingsDialog.Accepted:
            self._settings = dlg.get_settings()
            self._apply_settings(self._settings)
            self._restart_poller()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _apply_settings(self, settings: dict) -> None:
        self._alert_manager.cooldown_minutes = settings.get("cooldown_minutes", 30)
        notifiers = []
        if settings.get("telegram_enabled") and settings.get("telegram_token"):
            notifiers.append(TelegramNotifier(
                token=settings["telegram_token"],
                chat_id=settings.get("telegram_chat_id", ""),
            ))
        if settings.get("email_enabled") and settings.get("email_username"):
            notifiers.append(EmailNotifier(
                smtp_host=settings.get("email_smtp_host", "smtp.gmail.com"),
                smtp_port=settings.get("email_smtp_port", 587),
                username=settings.get("email_username", ""),
                password=settings.get("email_password", ""),
                to_addr=settings.get("email_to", ""),
            ))
        self._alert_manager.set_notifiers(notifiers)

        if settings.get("telegram_command_polling_enabled"):
            if self._cmd_poller is None or not self._cmd_poller.isRunning():
                self._start_command_poller()
        else:
            self._stop_command_poller()

    def closeEvent(self, event) -> None:
        logging.getLogger().removeHandler(self._log_handler)
        if hasattr(self, "_poller") and self._poller.isRunning():
            self._poller.stop()
            self._poller.wait()
        self._stop_command_poller()
        if self._scanner is not None and self._scanner.isRunning():
            self._scanner.stop()
            self._scanner.wait()
        # Stop any running AI rank researchers
        for r in self._ai_rank_researchers:
            if r.isRunning():
                r.stop()
                r.wait(2000)
        self._ai_rank_researchers.clear()
        event.accept()
