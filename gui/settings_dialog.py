from typing import Any, Dict

from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
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
        tabs.addTab(self._build_ai_tab(), "AI")
        tabs.addTab(self._build_web_tab(), "Web Publishing")

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
        layout.setSpacing(10)

        # ── Telegram command polling ──
        self._cmd_polling_enabled = QCheckBox("Enable Telegram command polling")
        self._cmd_polling_enabled.setChecked(
            self._settings.get("telegram_command_polling_enabled", False)
        )
        layout.addWidget(self._cmd_polling_enabled)

        layout.addSpacing(4)

        # ── Quick Scan (manual only) ──
        quick_box = QGroupBox("Quick Scan  —  S&P 500, price filter only")
        quick_layout = QVBoxLayout(quick_box)
        quick_layout.addWidget(QLabel("Manual trigger only (toolbar button or /scan bot command)."))
        layout.addWidget(quick_box)

        # ── Deep Scan (hourly, S&P 500) ──
        deep_box = QGroupBox("Deep Scan  —  S&P 500 (~500 stocks), full scoring")
        deep_form = QFormLayout(deep_box)

        self._deep_scan_enabled = QCheckBox("Enable hourly deep scan")
        self._deep_scan_enabled.setChecked(
            self._settings.get("scanner_deep_scan_enabled", False)
        )
        deep_form.addRow(self._deep_scan_enabled)

        self._deep_interval_spin = QSpinBox()
        self._deep_interval_spin.setRange(1, 24)
        self._deep_interval_spin.setSuffix(" hr")
        self._deep_interval_spin.setValue(
            self._settings.get("scanner_deep_scan_interval_hours", 1)
        )
        deep_form.addRow("Run every:", self._deep_interval_spin)

        self._deep_threshold_spin = QSpinBox()
        self._deep_threshold_spin.setRange(0, 100)
        self._deep_threshold_spin.setValue(
            self._settings.get("scanner_deep_alert_threshold", 60)
        )
        deep_form.addRow("Alert threshold (score ≥):", self._deep_threshold_spin)

        layout.addWidget(deep_box)

        # ── Complete Scan (scheduled, full universe) ──
        complete_box = QGroupBox("Complete Scan  —  Full universe (up to 1500 stocks)")
        complete_form = QFormLayout(complete_box)

        self._complete_scan_enabled = QCheckBox("Enable scheduled complete scan")
        self._complete_scan_enabled.setChecked(
            self._settings.get("scanner_complete_scan_enabled", False)
        )
        complete_form.addRow(self._complete_scan_enabled)

        self._complete_times_edit = QLineEdit(
            self._settings.get("scanner_complete_scan_times_et", "09:00,13:00,16:15")
        )
        self._complete_times_edit.setPlaceholderText("e.g. 09:00,13:00,16:15")
        complete_form.addRow("Run times ET (comma-separated):", self._complete_times_edit)

        self._complete_threshold_spin = QSpinBox()
        self._complete_threshold_spin.setRange(0, 100)
        self._complete_threshold_spin.setValue(
            self._settings.get("scanner_complete_alert_threshold", 60)
        )
        complete_form.addRow("Alert threshold (score ≥):", self._complete_threshold_spin)

        self._universe_spin = QSpinBox()
        self._universe_spin.setRange(50, 1500)
        self._universe_spin.setSingleStep(50)
        self._universe_spin.setValue(self._settings.get("scanner_universe_size", 500))
        complete_form.addRow("Universe size:", self._universe_spin)

        layout.addWidget(complete_box)

        layout.addStretch()

        note = QLabel(
            "Universe: S&P 500 → S&P 400 MidCap → S&P 600 SmallCap → NASDAQ 100\n"
            "(deduplicated, loaded in that order). Max ~1500 unique symbols.\n"
            "All times are compared to local machine clock — keep machine in ET\n"
            "or adjust times accordingly."
        )
        note.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(note)

        return w

    # ── AI Tab ────────────────────────────────────────────────────────────────

    def _build_ai_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(10)

        # Provider selector
        provider_form = QFormLayout()
        self._ai_provider_combo = QComboBox()
        self._ai_provider_combo.addItem("Ollama (local, free)", "ollama")
        self._ai_provider_combo.addItem("Claude API (Anthropic)", "claude")
        self._ai_provider_combo.addItem("OpenRouter API", "openrouter")
        current_provider = self._settings.get("ai_provider", "ollama")
        idx = self._ai_provider_combo.findData(current_provider)
        if idx >= 0:
            self._ai_provider_combo.setCurrentIndex(idx)
        provider_form.addRow("AI Provider:", self._ai_provider_combo)
        layout.addLayout(provider_form)

        # Ollama group
        ollama_box = QGroupBox("Ollama (local LLM)")
        ollama_form = QFormLayout(ollama_box)

        self._ollama_url = QLineEdit(
            self._settings.get("ai_ollama_url", "http://localhost:11434/api/generate")
        )
        ollama_form.addRow("API URL:", self._ollama_url)

        self._ollama_model = QLineEdit(
            self._settings.get("ai_ollama_model", "mistral")
        )
        self._ollama_model.setPlaceholderText("e.g. mistral, llama3, gemma")
        ollama_form.addRow("Model name:", self._ollama_model)

        layout.addWidget(ollama_box)

        # Claude API group
        claude_box = QGroupBox("Claude API (Anthropic)")
        claude_form = QFormLayout(claude_box)

        self._claude_api_key = QLineEdit(
            self._settings.get("ai_claude_api_key", "")
        )
        self._claude_api_key.setEchoMode(QLineEdit.Password)
        self._claude_api_key.setPlaceholderText("sk-ant-…")
        claude_form.addRow("API Key:", self._claude_api_key)

        self._claude_model = QLineEdit(
            self._settings.get("ai_claude_model", "claude-haiku-20240307")
        )
        self._claude_model.setPlaceholderText("e.g. claude-haiku-20240307")
        claude_form.addRow("Model:", self._claude_model)

        layout.addWidget(claude_box)

        # OpenRouter group
        or_box = QGroupBox("OpenRouter API")
        or_form = QFormLayout(or_box)

        self._openrouter_api_key = QLineEdit(
            self._settings.get("ai_openrouter_api_key", "")
        )
        self._openrouter_api_key.setEchoMode(QLineEdit.Password)
        self._openrouter_api_key.setPlaceholderText("sk-or-…")
        or_form.addRow("API Key:", self._openrouter_api_key)

        self._openrouter_model = QLineEdit(
            self._settings.get("ai_openrouter_model", "qwen/qwen3-coder:free")
        )
        self._openrouter_model.setPlaceholderText("e.g. qwen/qwen3-coder:free")
        or_form.addRow("Model:", self._openrouter_model)

        layout.addWidget(or_box)

        # Auto AI ranking freshness
        rank_box = QGroupBox("Auto AI Ranking")
        rank_form = QFormLayout(rank_box)
        self._ai_rank_refresh_spin = QSpinBox()
        self._ai_rank_refresh_spin.setRange(1, 48)
        self._ai_rank_refresh_spin.setSuffix(" hrs")
        self._ai_rank_refresh_spin.setValue(
            self._settings.get("ai_rank_refresh_hours", 4)
        )
        rank_form.addRow("Skip re-rank if fresher than:", self._ai_rank_refresh_spin)
        layout.addWidget(rank_box)

        # Congressional trading signal
        cong_box = QGroupBox("Congressional Trading Signal")
        cong_form = QFormLayout(cong_box)

        self._congressional_politicians_edit = QPlainTextEdit()
        self._congressional_politicians_edit.setPlaceholderText(
            "One name per line. Leave empty to track ALL politicians.\n"
            "e.g.\nNancy Pelosi\nTommy Tuberville"
        )
        raw_politicians = self._settings.get("congressional_tracked_politicians", "")
        self._congressional_politicians_edit.setPlainText(
            "\n".join(p.strip() for p in raw_politicians.split(",") if p.strip())
        )
        self._congressional_politicians_edit.setFixedHeight(120)
        cong_form.addRow("Tracked Politicians\n(one per line):",
                         self._congressional_politicians_edit)

        layout.addWidget(cong_box)

        layout.addStretch()

        note = QLabel(
            "Ollama setup: install from https://ollama.com, then run:\n"
            "  ollama pull mistral     (one-time ~4 GB download)\n"
            "Ollama runs fully on your local machine — no API key needed.\n\n"
            "Claude API: get a key at https://console.anthropic.com\n"
            "Note: this uses the Anthropic API (paid), not a Claude Pro subscription.\n\n"
            "OpenRouter: get a free key at https://openrouter.ai — routes to many models\n"
            "including free tiers (e.g. qwen/qwen3-coder:free)."
        )
        note.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(note)

        return w

    # ── Web Publishing Tab ────────────────────────────────────────────────────

    def _build_web_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(10)

        self._web_enabled = QCheckBox("Enable web publishing")
        self._web_enabled.setChecked(self._settings.get("web_publishing_enabled", False))
        layout.addWidget(self._web_enabled)

        self._web_cmd_polling_enabled = QCheckBox("Enable web command polling (write from browser)")
        self._web_cmd_polling_enabled.setChecked(self._settings.get("web_command_polling_enabled", False))
        layout.addWidget(self._web_cmd_polling_enabled)

        form = QFormLayout()
        layout.addLayout(form)

        self._web_interval_spin = QSpinBox()
        self._web_interval_spin.setRange(5, 120)
        self._web_interval_spin.setSuffix(" min")
        self._web_interval_spin.setValue(self._settings.get("web_publish_interval_minutes", 15))
        form.addRow("Safety-net interval:", self._web_interval_spin)

        r2_box = QGroupBox("Cloudflare R2 credentials (for Phase 2 upload)")
        r2_form = QFormLayout(r2_box)

        self._r2_account_id = QLineEdit(self._settings.get("r2_account_id", ""))
        self._r2_account_id.setPlaceholderText("Cloudflare account ID")
        r2_form.addRow("Account ID:", self._r2_account_id)

        self._r2_access_key = QLineEdit(self._settings.get("r2_access_key_id", ""))
        self._r2_access_key.setPlaceholderText("Access key ID")
        r2_form.addRow("Access Key ID:", self._r2_access_key)

        self._r2_secret_key = QLineEdit(self._settings.get("r2_secret_access_key", ""))
        self._r2_secret_key.setEchoMode(QLineEdit.Password)
        self._r2_secret_key.setPlaceholderText("Secret access key")
        r2_form.addRow("Secret Key:", self._r2_secret_key)

        self._r2_bucket = QLineEdit(self._settings.get("r2_bucket", "trader-data"))
        r2_form.addRow("Bucket name:", self._r2_bucket)

        self._r2_public_base_url = QLineEdit(
            self._settings.get("r2_public_base_url", "https://data.trader.akshaydhenge.uk")
        )
        r2_form.addRow("Public base URL:", self._r2_public_base_url)

        layout.addWidget(r2_box)

        test_r2_btn = QPushButton("Test R2 Connection")
        test_r2_btn.clicked.connect(self._test_r2)
        layout.addWidget(test_r2_btn)

        layout.addStretch()

        note = QLabel(
            "Install boto3 for R2 upload: py -3.9 -m pip install boto3\n"
            "Without R2 credentials, publishing writes locally to data/web_publish/ only.\n"
            "Local preview: py -3.9 -m http.server 8000  →  http://localhost:8000/web/"
        )
        note.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(note)

        return w

    def _test_r2(self) -> None:
        from core.web_publisher import WebPublisher
        tmp_settings = {
            "r2_account_id":       self._r2_account_id.text().strip(),
            "r2_access_key_id":    self._r2_access_key.text().strip(),
            "r2_secret_access_key": self._r2_secret_key.text().strip(),
            "r2_bucket":           self._r2_bucket.text().strip(),
            "r2_endpoint_url":     "",
        }
        pub = WebPublisher(get_settings=lambda: tmp_settings, get_alerts=lambda: [])
        ok, msg = pub.test_r2_connection(tmp_settings)
        if ok:
            QMessageBox.information(self, "R2 Connection", msg)
        else:
            QMessageBox.warning(self, "R2 Connection", msg)

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
            "scanner_deep_scan_enabled": self._deep_scan_enabled.isChecked(),
            "scanner_deep_scan_interval_hours": self._deep_interval_spin.value(),
            "scanner_deep_alert_threshold": self._deep_threshold_spin.value(),
            "scanner_complete_scan_enabled": self._complete_scan_enabled.isChecked(),
            "scanner_complete_scan_times_et": self._complete_times_edit.text().strip(),
            "scanner_complete_alert_threshold": self._complete_threshold_spin.value(),
            "scanner_universe_size": self._universe_spin.value(),
            "ai_rank_refresh_hours": self._ai_rank_refresh_spin.value(),
            "ai_provider":       self._ai_provider_combo.currentData(),
            "ai_ollama_url":     self._ollama_url.text().strip(),
            "ai_ollama_model":   self._ollama_model.text().strip(),
            "ai_claude_api_key":     self._claude_api_key.text().strip(),
            "ai_claude_model":       self._claude_model.text().strip(),
            "ai_openrouter_api_key": self._openrouter_api_key.text().strip(),
            "ai_openrouter_model":   self._openrouter_model.text().strip(),
            "congressional_tracked_politicians": ",".join(
                line.strip()
                for line in self._congressional_politicians_edit.toPlainText().splitlines()
                if line.strip()
            ),
            "web_publishing_enabled": self._web_enabled.isChecked(),
            "web_command_polling_enabled": self._web_cmd_polling_enabled.isChecked(),
            "web_publish_interval_minutes": self._web_interval_spin.value(),
            "r2_account_id": self._r2_account_id.text().strip(),
            "r2_access_key_id": self._r2_access_key.text().strip(),
            "r2_secret_access_key": self._r2_secret_key.text().strip(),
            "r2_bucket": self._r2_bucket.text().strip(),
            "r2_public_base_url": self._r2_public_base_url.text().strip(),
        })
        save_settings(self._settings)
        self.accept()

    def get_settings(self) -> Dict[str, Any]:
        return self._settings
