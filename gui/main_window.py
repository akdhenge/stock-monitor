from datetime import datetime
from typing import Dict, List, Optional, Set

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QAction, QLabel, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QStatusBar, QTabWidget, QToolBar, QVBoxLayout, QWidget,
)

from core.alert_manager import AlertManager
from core.models import AlertRecord, StockEntry
from core.price_poller import PricePoller
from core.scan_result import ScanResult
from core.scan_results_store import load_scan_results, save_scan_results
from core.settings_store import load_settings, save_settings
from core.stock_scanner import StockScanner
from core.watchlist_store import load_watchlist, save_watchlist
from gui.add_edit_dialog import AddEditDialog
from gui.alert_history_panel import AlertHistoryPanel
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
        self._scanner_top5: Set[str] = set()
        self._quick_candidates: List[str] = []

        # Command poller (lazily started)
        self._cmd_poller = None

        self._setup_ui()
        self._apply_settings(self._settings)
        self._watchlist_table.refresh(self._watchlist)

        # Start price poller after short delay
        QTimer.singleShot(500, self._start_poller)

        # 60-second timer for scheduled scans
        self._schedule_timer = QTimer(self)
        self._schedule_timer.timeout.connect(self._check_scheduled_scans)
        self._schedule_timer.start(60_000)

        # Load previous scan results into panel
        self._load_saved_scan_results()

    # ── UI Setup ──────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        # Menu bar
        menu = self.menuBar()
        file_menu = menu.addMenu("File")

        settings_action = QAction("Settings…", self)
        settings_action.triggered.connect(self._open_settings)
        file_menu.addAction(settings_action)

        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # Toolbar
        toolbar = QToolBar("Main toolbar")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add_stock)
        toolbar.addWidget(add_btn)

        edit_btn = QPushButton("Edit")
        edit_btn.clicked.connect(self._edit_stock)
        toolbar.addWidget(edit_btn)

        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._remove_stock)
        toolbar.addWidget(remove_btn)

        toolbar.addSeparator()

        refresh_btn = QPushButton("Refresh Now")
        refresh_btn.clicked.connect(self._manual_refresh)
        toolbar.addWidget(refresh_btn)

        toolbar.addSeparator()

        quick_scan_btn = QPushButton("Quick Scan")
        quick_scan_btn.clicked.connect(self._trigger_quick_scan)
        toolbar.addWidget(quick_scan_btn)

        # Central widget — tab layout
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)

        # Tab 1: Watchlist
        watchlist_widget = QWidget()
        wl_layout = QVBoxLayout(watchlist_widget)
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

        self._tabs.addTab(watchlist_widget, "Watchlist")

        # Tab 2: Smart Scanner
        self._scanner_panel = SmartScannerPanel()
        self._scanner_panel.request_quick_scan.connect(self._trigger_quick_scan)
        self._scanner_panel.request_deep_scan.connect(self._trigger_deep_scan)
        self._scanner_panel.request_cancel_scan.connect(self._cancel_scan)
        self._scanner_panel.add_to_watchlist.connect(self._add_scanner_results)
        self._tabs.addTab(self._scanner_panel, "Smart Scanner")

        # Split status bar
        self._poll_status_label = QLabel("Not yet polled")
        self._scan_status_label = QLabel("")
        self.statusBar().addWidget(self._poll_status_label, stretch=1)
        self.statusBar().addPermanentWidget(self._scan_status_label)

    # ── Poller ────────────────────────────────────────────────────────────────

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
        token = self._settings.get("telegram_token", "")
        chat_id = self._settings.get("telegram_chat_id", "")
        if not token or not chat_id:
            return
        self._cmd_poller = TelegramCommandPoller(token, chat_id)
        self._cmd_poller.cmd_add.connect(self._on_cmd_add)
        self._cmd_poller.cmd_remove.connect(self._on_cmd_remove)
        self._cmd_poller.cmd_list.connect(self._on_cmd_list)
        self._cmd_poller.cmd_scan.connect(self._on_cmd_scan)
        self._cmd_poller.cmd_top.connect(self._on_cmd_top)
        self._cmd_poller.poll_error.connect(
            lambda msg: self._poll_status_label.setText(f"Bot: {msg}")
        )
        self._cmd_poller.start()

    def _stop_command_poller(self) -> None:
        if self._cmd_poller is not None and self._cmd_poller.isRunning():
            self._cmd_poller.stop()
            self._cmd_poller.wait()
        self._cmd_poller = None

    # ── Scanner ───────────────────────────────────────────────────────────────

    def _trigger_quick_scan(self) -> None:
        if self._scanner is not None and self._scanner.isRunning():
            return
        universe_size = self._settings.get("scanner_universe_size", 200)
        self._scanner = StockScanner(mode="quick", universe_size=universe_size)
        self._scanner.set_previous_top5(self._scanner_top5)
        self._scanner.quick_scan_complete.connect(self._on_quick_scan_complete)
        self._scanner.scan_progress.connect(self._scanner_panel.update_progress)
        self._scanner.scan_status.connect(self._scanner_panel.update_status)
        self._scanner.scan_error.connect(
            lambda msg: self._scan_status_label.setText(f"Scan error: {msg}")
        )
        self._scanner.start()
        self._scanner_panel.set_scan_running("Quick Scan")
        self._scan_status_label.setText("Quick scan running…")
        # Switch to scanner tab
        self._tabs.setCurrentIndex(1)

    def _trigger_deep_scan(self, candidates: Optional[List[str]] = None) -> None:
        if self._scanner is not None and self._scanner.isRunning():
            return
        universe_size = self._settings.get("scanner_universe_size", 200)
        self._scanner = StockScanner(mode="deep", universe_size=universe_size)
        if candidates:
            self._scanner.set_candidates(candidates)
        elif self._quick_candidates:
            self._scanner.set_candidates(self._quick_candidates)
        self._scanner.set_previous_top5(self._scanner_top5)
        self._scanner.deep_scan_complete.connect(self._on_deep_scan_complete)
        self._scanner.new_top5_entry.connect(self._on_new_top5)
        self._scanner.scan_progress.connect(self._scanner_panel.update_progress)
        self._scanner.scan_status.connect(self._scanner_panel.update_status)
        self._scanner.scan_error.connect(
            lambda msg: self._scan_status_label.setText(f"Scan error: {msg}")
        )
        self._scanner.start()
        self._scanner_panel.set_scan_running("Deep Scan")
        self._scan_status_label.setText("Deep scan running…")
        self._tabs.setCurrentIndex(1)

    def _cancel_scan(self) -> None:
        if self._scanner is not None and self._scanner.isRunning():
            self._scanner.stop()
            self._scanner.wait()
        self._scanner_panel.set_scan_idle("Cancelled")
        self._scan_status_label.setText("Scan cancelled.")

    def _check_scheduled_scans(self) -> None:
        """Called every 60 s; fires scheduled scans at configured ET times."""
        now = datetime.now()
        weekday = now.weekday()   # 0=Mon … 6=Sun
        current_hhmm = now.strftime("%H:%M")

        if self._settings.get("scanner_daily_scan_enabled"):
            target_time = self._settings.get("scanner_daily_scan_time_et", "16:15")
            # Mon–Fri only
            if weekday < 5 and current_hhmm == target_time:
                if self._scanner is None or not self._scanner.isRunning():
                    self._trigger_quick_scan()

        if self._settings.get("scanner_weekly_scan_enabled"):
            target_day = self._settings.get("scanner_weekly_scan_day", 6)
            target_time = self._settings.get("scanner_weekly_scan_time_et", "20:00")
            if weekday == target_day and current_hhmm == target_time:
                if self._scanner is None or not self._scanner.isRunning():
                    self._trigger_deep_scan()

    def _load_saved_scan_results(self) -> None:
        results = load_scan_results()
        if results:
            self._scanner_panel.display_results(results)
            self._scanner_top5 = {r.symbol for r in results[:5]}

    # ── Price Poller Slots ────────────────────────────────────────────────────

    def _on_prices_updated(self, prices: Dict[str, float]) -> None:
        for entry in self._watchlist:
            if entry.symbol in prices:
                entry.current_price = prices[entry.symbol]
            self._alert_manager.check_and_alert(entry)

        self._watchlist_table.refresh(self._watchlist)
        now = datetime.now().strftime("%H:%M:%S")
        self._poll_status_label.setText(f"Last updated: {now}")

    def _on_poll_error(self, msg: str) -> None:
        self._poll_status_label.setText(f"Poll error: {msg}")

    def _on_alert_fired(self, record: AlertRecord) -> None:
        self._history_panel.add_record(record)

    # ── Scanner Slots ─────────────────────────────────────────────────────────

    def _on_quick_scan_complete(self, results: List[ScanResult]) -> None:
        self._scanner_panel.display_results(results)
        self._scanner_panel.set_scan_idle("Quick Scan (complete)")
        self._quick_candidates = [r.symbol for r in results]
        self._scan_status_label.setText(
            f"Quick scan: {len(results)} candidates  "
            f"({datetime.now().strftime('%H:%M')})"
        )
        save_scan_results(results)

    def _on_deep_scan_complete(self, results: List[ScanResult]) -> None:
        self._scanner_panel.display_results(results)
        self._scanner_panel.set_scan_idle("Deep Scan (complete)")
        self._scanner_top5 = {r.symbol for r in results[:5]}
        self._scan_status_label.setText(
            f"Deep scan: top {len(results)} stocks  "
            f"({datetime.now().strftime('%H:%M')})"
        )
        save_scan_results(results)

    def _on_new_top5(
        self, symbol: str, total: float, value: float, growth: float, tech: float
    ) -> None:
        token = self._settings.get("telegram_token", "")
        chat_id = self._settings.get("telegram_chat_id", "")
        if token and chat_id and self._settings.get("telegram_enabled"):
            msg = (
                f"⭐ New Top-5 Entry: <b>{symbol}</b>\n"
                f"Total: {total} | Value: {value} | Growth: {growth} | Tech: {tech}"
            )
            TelegramNotifier.send_message(token, chat_id, msg)

    # ── Bot Command Slots ─────────────────────────────────────────────────────

    def _on_cmd_add(
        self, symbol: str, low: float, high: float, notes: str, reply_chat_id: str
    ) -> None:
        token = self._settings.get("telegram_token", "")
        existing = {e.symbol for e in self._watchlist}
        if symbol in existing:
            TelegramNotifier.send_message(
                token, reply_chat_id,
                f"⚠️ {symbol} is already in your watchlist."
            )
            return
        entry = StockEntry(
            symbol=symbol,
            low_target=low,
            high_target=high,
            notes=notes,
        )
        self._watchlist.append(entry)
        save_watchlist(self._watchlist)
        self._watchlist_table.refresh(self._watchlist)
        if hasattr(self, "_poller"):
            self._poller.update_symbols([e.symbol for e in self._watchlist])
        TelegramNotifier.send_message(
            token, reply_chat_id,
            f"✅ Added {symbol} — Low: ${low}, High: ${high}"
            + (f", Notes: {notes}" if notes else "")
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
                token, reply_chat_id, f"✅ Removed {symbol} from watchlist."
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
                f"• {e.symbol}  price={price_str}  "
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
        TelegramNotifier.send_message(
            token, reply_chat_id, "🔍 Starting quick scan…"
        )
        self._trigger_quick_scan()

    def _on_cmd_top(self, reply_chat_id: str) -> None:
        token = self._settings.get("telegram_token", "")
        results = load_scan_results()
        if not results:
            TelegramNotifier.send_message(
                token, reply_chat_id,
                "No scan results yet. Run /scan first."
            )
            return
        top5 = results[:5]
        lines = ["<b>Top 5 Undervalued Stocks:</b>"]
        for i, r in enumerate(top5, 1):
            lines.append(
                f"{i}. {r.symbol} — Score: {r.total_score} "
                f"(V:{r.score_value} G:{r.score_growth} T:{r.score_technical})"
            )
        TelegramNotifier.send_message(token, reply_chat_id, "\n".join(lines))

    # ── Toolbar Actions ───────────────────────────────────────────────────────

    def _add_stock(self) -> None:
        dlg = AddEditDialog(parent=self)
        if dlg.exec_() == AddEditDialog.Accepted:
            new_entry = dlg.get_entry()
            existing_symbols = {e.symbol for e in self._watchlist}
            if new_entry.symbol in existing_symbols:
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
            entry.low_target = updated.low_target
            entry.high_target = updated.high_target
            entry.notes = updated.notes
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
        """Add scanner-selected stocks to the watchlist with default targets."""
        added = []
        existing = {e.symbol for e in self._watchlist}
        for r in results:
            if r.symbol in existing:
                continue
            low = round(r.price * 0.90, 2) if r.price else 0.0
            high = round(r.price * 1.15, 2) if r.price else 0.0
            entry = StockEntry(
                symbol=r.symbol,
                low_target=low,
                high_target=high,
                notes=f"Added from scanner (score {r.total_score})",
            )
            self._watchlist.append(entry)
            existing.add(r.symbol)
            added.append(r.symbol)

        if added:
            save_watchlist(self._watchlist)
            self._watchlist_table.refresh(self._watchlist)
            if hasattr(self, "_poller"):
                self._poller.update_symbols([e.symbol for e in self._watchlist])
            self._tabs.setCurrentIndex(0)
            QMessageBox.information(
                self, "Added to Watchlist",
                f"Added: {', '.join(added)}"
            )
        else:
            QMessageBox.information(
                self, "Added to Watchlist",
                "All selected stocks are already in the watchlist."
            )

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
            notifiers.append(
                TelegramNotifier(
                    token=settings["telegram_token"],
                    chat_id=settings.get("telegram_chat_id", ""),
                )
            )
        if settings.get("email_enabled") and settings.get("email_username"):
            notifiers.append(
                EmailNotifier(
                    smtp_host=settings.get("email_smtp_host", "smtp.gmail.com"),
                    smtp_port=settings.get("email_smtp_port", 587),
                    username=settings.get("email_username", ""),
                    password=settings.get("email_password", ""),
                    to_addr=settings.get("email_to", ""),
                )
            )
        self._alert_manager.set_notifiers(notifiers)

        # Start/stop command poller based on setting
        if settings.get("telegram_command_polling_enabled"):
            if self._cmd_poller is None or not self._cmd_poller.isRunning():
                self._start_command_poller()
        else:
            self._stop_command_poller()

    def closeEvent(self, event) -> None:
        if hasattr(self, "_poller") and self._poller.isRunning():
            self._poller.stop()
            self._poller.wait()
        self._stop_command_poller()
        if self._scanner is not None and self._scanner.isRunning():
            self._scanner.stop()
            self._scanner.wait()
        event.accept()
