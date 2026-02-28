from typing import Any, Dict

from PyQt5.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSpinBox, QTabWidget, QTimeEdit, QVBoxLayout, QWidget,
)
from PyQt5.QtCore import QTime

from core.settings_store import load_settings, save_settings
from notifiers.email_notifier import EmailNotifier
from notifiers.telegram_notifier import TelegramNotifier


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)
        self._settings = load_settings()

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        tabs.addTab(self._build_general_tab(), "General")
        tabs.addTab(self._build_telegram_tab(), "Telegram")
        tabs.addTab(self._build_email_tab(), "Email")
        tabs.addTab(self._build_scanner_tab(), "Scanner")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ── General Tab ───────────────────────────────────────────────────────────

    def _build_general_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        self._poll_spin = QSpinBox()
        self._poll_spin.setRange(10, 3600)
        self._poll_spin.setSuffix(" s")
        self._poll_spin.setValue(self._settings.get("poll_interval_seconds", 60))
        form.addRow("Poll interval:", self._poll_spin)

        self._cooldown_spin = QSpinBox()
        self._cooldown_spin.setRange(1, 1440)
        self._cooldown_spin.setSuffix(" min")
        self._cooldown_spin.setValue(self._settings.get("cooldown_minutes", 30))
        form.addRow("Alert cooldown:", self._cooldown_spin)

        return w

    # ── Telegram Tab ──────────────────────────────────────────────────────────

    def _build_telegram_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        self._tg_enabled = QCheckBox("Enable Telegram alerts")
        self._tg_enabled.setChecked(self._settings.get("telegram_enabled", False))
        layout.addWidget(self._tg_enabled)

        form = QFormLayout()
        layout.addLayout(form)

        self._tg_token = QLineEdit(self._settings.get("telegram_token", ""))
        self._tg_token.setPlaceholderText("Bot token from @BotFather")
        self._tg_token.setEchoMode(QLineEdit.Password)
        form.addRow("Bot Token:", self._tg_token)

        chat_row = QHBoxLayout()
        self._tg_chat = QLineEdit(self._settings.get("telegram_chat_id", ""))
        self._tg_chat.setPlaceholderText("Numeric chat ID")
        chat_row.addWidget(self._tg_chat)
        detect_btn = QPushButton("Auto-detect")
        detect_btn.clicked.connect(self._auto_detect_chat_id)
        chat_row.addWidget(detect_btn)
        form.addRow("Chat ID:", chat_row)

        test_btn = QPushButton("Test Connection")
        test_btn.clicked.connect(self._test_telegram)
        layout.addWidget(test_btn)

        layout.addStretch()

        instructions = QLabel(
            "Setup: create a bot via @BotFather, paste the token above,\n"
            "send any message to your bot, then click Auto-detect."
        )
        instructions.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(instructions)

        return w

    def _auto_detect_chat_id(self) -> None:
        token = self._tg_token.text().strip()
        ok, chat_id, msg = TelegramNotifier.get_updates(token)
        if ok and chat_id:
            self._tg_chat.setText(chat_id)
        QMessageBox.information(self, "Auto-detect Chat ID", msg)

    def _test_telegram(self) -> None:
        token = self._tg_token.text().strip()
        chat_id = self._tg_chat.text().strip()
        notifier = TelegramNotifier(token, chat_id)
        ok, msg = notifier.test_connection()
        if ok:
            QMessageBox.information(self, "Telegram Test", msg)
        else:
            QMessageBox.warning(self, "Telegram Test", msg)

    # ── Email Tab ─────────────────────────────────────────────────────────────

    def _build_email_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        self._email_enabled = QCheckBox("Enable email alerts")
        self._email_enabled.setChecked(self._settings.get("email_enabled", False))
        layout.addWidget(self._email_enabled)

        form = QFormLayout()
        layout.addLayout(form)

        self._smtp_host = QLineEdit(self._settings.get("email_smtp_host", "smtp.gmail.com"))
        form.addRow("SMTP Host:", self._smtp_host)

        self._smtp_port = QSpinBox()
        self._smtp_port.setRange(1, 65535)
        self._smtp_port.setValue(self._settings.get("email_smtp_port", 587))
        form.addRow("SMTP Port:", self._smtp_port)

        self._email_user = QLineEdit(self._settings.get("email_username", ""))
        self._email_user.setPlaceholderText("your@gmail.com")
        form.addRow("Username:", self._email_user)

        self._email_pass = QLineEdit(self._settings.get("email_password", ""))
        self._email_pass.setEchoMode(QLineEdit.Password)
        self._email_pass.setPlaceholderText("App password (not your account password)")
        form.addRow("Password:", self._email_pass)

        self._email_to = QLineEdit(self._settings.get("email_to", ""))
        self._email_to.setPlaceholderText("recipient@example.com")
        form.addRow("Send To:", self._email_to)

        test_btn = QPushButton("Test Connection")
        test_btn.clicked.connect(self._test_email)
        layout.addWidget(test_btn)

        layout.addStretch()
        return w

    # ── Scanner Tab ───────────────────────────────────────────────────────────

    def _build_scanner_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        # Telegram command polling
        self._cmd_polling_enabled = QCheckBox("Enable Telegram command polling")
        self._cmd_polling_enabled.setChecked(
            self._settings.get("telegram_command_polling_enabled", False)
        )
        layout.addWidget(self._cmd_polling_enabled)

        layout.addSpacing(8)

        # Daily quick scan
        self._daily_scan_enabled = QCheckBox("Enable daily quick scan (after market close)")
        self._daily_scan_enabled.setChecked(
            self._settings.get("scanner_daily_scan_enabled", False)
        )
        layout.addWidget(self._daily_scan_enabled)

        form = QFormLayout()
        layout.addLayout(form)

        self._daily_scan_time = QTimeEdit()
        self._daily_scan_time.setDisplayFormat("HH:mm")
        time_str = self._settings.get("scanner_daily_scan_time_et", "16:15")
        try:
            h, m = [int(x) for x in time_str.split(":")]
        except (ValueError, AttributeError):
            h, m = 16, 15
        self._daily_scan_time.setTime(QTime(h, m))
        form.addRow("Daily scan time (ET):", self._daily_scan_time)

        layout.addSpacing(8)

        # Weekly deep scan
        self._weekly_scan_enabled = QCheckBox("Enable weekly deep scan")
        self._weekly_scan_enabled.setChecked(
            self._settings.get("scanner_weekly_scan_enabled", False)
        )
        layout.addWidget(self._weekly_scan_enabled)

        form2 = QFormLayout()
        layout.addLayout(form2)

        self._weekly_scan_time = QTimeEdit()
        self._weekly_scan_time.setDisplayFormat("HH:mm")
        wtime_str = self._settings.get("scanner_weekly_scan_time_et", "20:00")
        try:
            wh, wm = [int(x) for x in wtime_str.split(":")]
        except (ValueError, AttributeError):
            wh, wm = 20, 0
        self._weekly_scan_time.setTime(QTime(wh, wm))
        form2.addRow("Weekly scan time (ET):", self._weekly_scan_time)

        layout.addSpacing(8)

        # Universe size
        form3 = QFormLayout()
        layout.addLayout(form3)

        self._universe_spin = QSpinBox()
        self._universe_spin.setRange(50, 1500)
        self._universe_spin.setSingleStep(50)
        self._universe_spin.setValue(self._settings.get("scanner_universe_size", 200))
        form3.addRow("Universe size (top N S&P 500):", self._universe_spin)

        layout.addStretch()

        note = QLabel(
            "Daily scan runs at the configured ET time Mon–Fri.\n"
            "Weekly scan runs every Sunday at the configured ET time.\n"
            "Universe pulls from S&P 500 + S&P 400 MidCap + S&P 600 SmallCap\n"
            "+ NASDAQ 100 (deduplicated). Max ~1500 unique symbols."
        )
        note.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(note)

        return w

    def _test_email(self) -> None:
        notifier = EmailNotifier(
            smtp_host=self._smtp_host.text().strip(),
            smtp_port=self._smtp_port.value(),
            username=self._email_user.text().strip(),
            password=self._email_pass.text(),
            to_addr=self._email_to.text().strip(),
        )
        ok, msg = notifier.test_connection()
        if ok:
            QMessageBox.information(self, "Email Test", msg)
        else:
            QMessageBox.warning(self, "Email Test", msg)

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save_and_accept(self) -> None:
        daily_t = self._daily_scan_time.time()
        weekly_t = self._weekly_scan_time.time()
        self._settings.update({
            "poll_interval_seconds": self._poll_spin.value(),
            "cooldown_minutes": self._cooldown_spin.value(),
            "telegram_enabled": self._tg_enabled.isChecked(),
            "telegram_token": self._tg_token.text().strip(),
            "telegram_chat_id": self._tg_chat.text().strip(),
            "email_enabled": self._email_enabled.isChecked(),
            "email_smtp_host": self._smtp_host.text().strip(),
            "email_smtp_port": self._smtp_port.value(),
            "email_username": self._email_user.text().strip(),
            "email_password": self._email_pass.text(),
            "email_to": self._email_to.text().strip(),
            "telegram_command_polling_enabled": self._cmd_polling_enabled.isChecked(),
            "scanner_daily_scan_enabled": self._daily_scan_enabled.isChecked(),
            "scanner_daily_scan_time_et": f"{daily_t.hour():02d}:{daily_t.minute():02d}",
            "scanner_weekly_scan_enabled": self._weekly_scan_enabled.isChecked(),
            "scanner_weekly_scan_day": 6,
            "scanner_weekly_scan_time_et": f"{weekly_t.hour():02d}:{weekly_t.minute():02d}",
            "scanner_universe_size": self._universe_spin.value(),
        })
        save_settings(self._settings)
        self.accept()

    def get_settings(self) -> Dict[str, Any]:
        return self._settings
