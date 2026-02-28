from datetime import datetime
from typing import Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QAction, QLabel, QMainWindow, QMessageBox, QPushButton,
    QHBoxLayout, QSplitter, QStatusBar, QToolBar, QVBoxLayout, QWidget,
)

from core.alert_manager import AlertManager
from core.models import AlertRecord, StockEntry
from core.price_poller import PricePoller
from core.settings_store import load_settings
from core.watchlist_store import load_watchlist, save_watchlist
from gui.add_edit_dialog import AddEditDialog
from gui.alert_history_panel import AlertHistoryPanel
from gui.settings_dialog import SettingsDialog
from gui.watchlist_table import WatchlistTable
from notifiers.email_notifier import EmailNotifier
from notifiers.telegram_notifier import TelegramNotifier


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Stock Monitor")
        self.setMinimumSize(720, 500)

        self._watchlist: List[StockEntry] = load_watchlist()
        self._settings = load_settings()
        self._alert_manager = AlertManager(
            cooldown_minutes=self._settings.get("cooldown_minutes", 30)
        )
        self._alert_manager.set_alert_callback(self._on_alert_fired)

        self._setup_ui()
        self._apply_settings(self._settings)
        self._watchlist_table.refresh(self._watchlist)

        # Start first poll immediately (after a short delay so the window paints)
        QTimer.singleShot(500, self._start_poller)

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

        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self._watchlist_table = WatchlistTable()
        layout.addWidget(self._watchlist_table, stretch=3)

        self._history_panel = AlertHistoryPanel()
        layout.addWidget(self._history_panel, stretch=1)

        # Status bar
        self._status_label = QLabel("Not yet polled")
        self.statusBar().addWidget(self._status_label)

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

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_prices_updated(self, prices: Dict[str, float]) -> None:
        for entry in self._watchlist:
            if entry.symbol in prices:
                entry.current_price = prices[entry.symbol]
            self._alert_manager.check_and_alert(entry)

        self._watchlist_table.refresh(self._watchlist)
        now = datetime.now().strftime("%H:%M:%S")
        self._status_label.setText(f"Last updated: {now}")

    def _on_poll_error(self, msg: str) -> None:
        self._status_label.setText(f"Poll error: {msg}")

    def _on_alert_fired(self, record: AlertRecord) -> None:
        self._history_panel.add_record(record)

    # ── Toolbar Actions ───────────────────────────────────────────────────────

    def _add_stock(self) -> None:
        dlg = AddEditDialog(parent=self)
        if dlg.exec_() == AddEditDialog.Accepted:
            new_entry = dlg.get_entry()
            # Prevent duplicates
            existing_symbols = {e.symbol for e in self._watchlist}
            if new_entry.symbol in existing_symbols:
                QMessageBox.warning(
                    self, "Duplicate", f"{new_entry.symbol} is already in the watchlist."
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
        if not self._watchlist:
            return
        if hasattr(self, "_poller") and self._poller.isRunning():
            # Restart poller to trigger an immediate poll cycle
            self._restart_poller()
        else:
            self._restart_poller()

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

    def closeEvent(self, event) -> None:
        if hasattr(self, "_poller") and self._poller.isRunning():
            self._poller.stop()
            self._poller.wait()
        event.accept()
