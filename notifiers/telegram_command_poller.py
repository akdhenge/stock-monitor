"""
TelegramCommandPoller — polls getUpdates and emits Qt signals for bot commands.

Accepted commands:
  /add SYMBOL LOW HIGH [notes]
  /remove SYMBOL
  /list
  /scan
  /top
  /aiscan SYMBOL
  /stopaiscan — end the active follow-up session early
  (plain text) — follow-up question after /aiscan, if a session is active
"""
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional

import requests
from PyQt5.QtCore import QThread, pyqtSignal

from notifiers.telegram_notifier import TelegramNotifier

_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_POLL_TIMEOUT = 4   # seconds for long-poll getUpdates
_SLEEP_INTERVAL = 5  # seconds between polls (chunked into 1-s sleeps)
_FOLLOWUP_TTL_MINUTES = 30  # how long a follow-up session stays active


class TelegramCommandPoller(QThread):
    # symbol, low, high, notes, reply_chat_id
    cmd_add = pyqtSignal(str, float, float, str, str)
    # symbol, reply_chat_id
    cmd_remove = pyqtSignal(str, str)
    # reply_chat_id
    cmd_list = pyqtSignal(str)
    # reply_chat_id
    cmd_scan = pyqtSignal(str)
    # reply_chat_id
    cmd_top    = pyqtSignal(str)
    # reply_chat_id — full detailed table
    cmd_detail = pyqtSignal(str)
    # symbol, reply_chat_id
    cmd_aiscan = pyqtSignal(str, str)
    # symbol, question, reply_chat_id
    cmd_aifollow = pyqtSignal(str, str, str)
    # reply_chat_id
    cmd_stopaiscan = pyqtSignal(str)
    # symbol, reply_chat_id
    cmd_mute = pyqtSignal(str, str)
    # symbol, side ("low"|"high"), new_price, reply_chat_id
    cmd_revise = pyqtSignal(str, str, float, str)
    # reply_chat_id
    cmd_portfolio   = pyqtSignal(str)
    cmd_positions   = pyqtSignal(str)
    cmd_performance = pyqtSignal(str)
    cmd_tradelog    = pyqtSignal(str)
    cmd_pause       = pyqtSignal(str)
    cmd_resume      = pyqtSignal(str)
    # symbol, reply_chat_id
    cmd_sell = pyqtSignal(str, str)
    # error message
    poll_error = pyqtSignal(str)

    def __init__(self, token: str, allowed_chat_id: str, parent=None):
        super().__init__(parent)
        self._token = token
        self._allowed_chat_id = allowed_chat_id
        self._running = False
        self._offset: Optional[int] = None
        self._followup_lock = threading.Lock()
        # chat_id -> {"symbol": str, "expires": datetime}
        self._followup_sessions: Dict[str, dict] = {}

    def register_followup_session(self, chat_id: str, symbol: str) -> None:
        """Call from MainWindow after /aiscan completes to open a follow-up window."""
        with self._followup_lock:
            self._followup_sessions[chat_id] = {
                "symbol": symbol,
                "expires": datetime.now() + timedelta(minutes=_FOLLOWUP_TTL_MINUTES),
            }

    def clear_followup_session(self, chat_id: str) -> None:
        """End the follow-up session for a chat immediately."""
        with self._followup_lock:
            self._followup_sessions.pop(chat_id, None)

    def run(self) -> None:
        self._running = True
        while self._running:
            try:
                self._poll_once()
            except Exception as exc:
                self.poll_error.emit(f"Command poller error: {exc}")
            # Sleep in 1-second chunks so stop() is responsive
            for _ in range(_SLEEP_INTERVAL):
                if not self._running:
                    break
                self.msleep(1000)

    def stop(self) -> None:
        self._running = False

    def _poll_once(self) -> None:
        url = _API_BASE.format(token=self._token, method="getUpdates")
        params = {"timeout": _POLL_TIMEOUT}
        if self._offset is not None:
            params["offset"] = self._offset

        try:
            resp = requests.get(url, params=params, timeout=_POLL_TIMEOUT + 5)
        except requests.RequestException as exc:
            self.poll_error.emit(f"getUpdates request failed: {exc}")
            return

        if not resp.ok:
            self.poll_error.emit(f"getUpdates HTTP {resp.status_code}")
            return

        data = resp.json()
        updates = data.get("result", [])
        for update in updates:
            update_id = update.get("update_id", 0)
            # Advance offset so we don't replay this message
            self._offset = update_id + 1

            message = update.get("message") or update.get("edited_message")
            if not message:
                continue

            # Security: only handle messages from the configured chat
            chat_id = str(message.get("chat", {}).get("id", ""))
            if self._allowed_chat_id and chat_id != self._allowed_chat_id:
                continue

            text = (message.get("text") or "").strip()
            if not text.startswith("/"):
                # Check for active follow-up session
                with self._followup_lock:
                    session = self._followup_sessions.get(chat_id)
                    if session and datetime.now() < session["expires"]:
                        symbol = session["symbol"]
                    else:
                        symbol = None
                if symbol:
                    self.cmd_aifollow.emit(symbol, text, chat_id)
                continue

            self._dispatch_command(text, chat_id)

    def _dispatch_command(self, text: str, reply_chat_id: str) -> None:
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]  # strip @botname suffix

        if cmd == "/add":
            self._handle_add(parts, reply_chat_id)
        elif cmd == "/remove":
            self._handle_remove(parts, reply_chat_id)
        elif cmd == "/list":
            self.cmd_list.emit(reply_chat_id)
        elif cmd == "/scan":
            self.cmd_scan.emit(reply_chat_id)
        elif cmd == "/top":
            self.cmd_top.emit(reply_chat_id)
        elif cmd == "/detail":
            self.cmd_detail.emit(reply_chat_id)
        elif cmd == "/aiscan":
            self._handle_aiscan(parts, reply_chat_id)
        elif cmd == "/stopaiscan":
            self.cmd_stopaiscan.emit(reply_chat_id)
        elif cmd == "/mute":
            self._handle_mute(parts, reply_chat_id)
        elif cmd == "/revise":
            self._handle_revise(parts, reply_chat_id)
        elif cmd == "/portfolio":
            self.cmd_portfolio.emit(reply_chat_id)
        elif cmd == "/positions":
            self.cmd_positions.emit(reply_chat_id)
        elif cmd == "/performance":
            self.cmd_performance.emit(reply_chat_id)
        elif cmd == "/tradelog":
            self.cmd_tradelog.emit(reply_chat_id)
        elif cmd == "/pause":
            self.cmd_pause.emit(reply_chat_id)
        elif cmd == "/resume":
            self.cmd_resume.emit(reply_chat_id)
        elif cmd == "/sell":
            self._handle_sell(parts, reply_chat_id)
        else:
            TelegramNotifier.send_message(
                self._token,
                reply_chat_id,
                "Unknown command.\n"
                "<b>Watchlist:</b> /add /remove /list /mute /revise\n"
                "<b>Scanner:</b> /scan /top /detail /aiscan /stopaiscan\n"
                "<b>Trader:</b> /portfolio /positions /performance /tradelog /pause /resume /sell SYMBOL",
            )

    def _handle_add(self, parts: list, reply_chat_id: str) -> None:
        # /add SYMBOL LOW HIGH [notes...]
        if len(parts) < 4:
            TelegramNotifier.send_message(
                self._token,
                reply_chat_id,
                "Usage: /add SYMBOL LOW HIGH [notes]",
            )
            return
        symbol = parts[1].upper()
        try:
            low = float(parts[2])
            high = float(parts[3])
        except ValueError:
            TelegramNotifier.send_message(
                self._token,
                reply_chat_id,
                "LOW and HIGH must be numbers. Usage: /add SYMBOL LOW HIGH [notes]",
            )
            return
        notes = " ".join(parts[4:]) if len(parts) > 4 else ""
        self.cmd_add.emit(symbol, low, high, notes, reply_chat_id)

    def _handle_remove(self, parts: list, reply_chat_id: str) -> None:
        # /remove SYMBOL
        if len(parts) < 2:
            TelegramNotifier.send_message(
                self._token,
                reply_chat_id,
                "Usage: /remove SYMBOL",
            )
            return
        symbol = parts[1].upper()
        self.cmd_remove.emit(symbol, reply_chat_id)

    def _handle_aiscan(self, parts: list, reply_chat_id: str) -> None:
        # /aiscan SYMBOL
        if len(parts) < 2:
            TelegramNotifier.send_message(
                self._token,
                reply_chat_id,
                "Usage: /aiscan SYMBOL",
            )
            return
        symbol = parts[1].upper()
        self.cmd_aiscan.emit(symbol, reply_chat_id)

    def _handle_mute(self, parts: list, reply_chat_id: str) -> None:
        # /mute SYMBOL
        if len(parts) < 2:
            TelegramNotifier.send_message(
                self._token,
                reply_chat_id,
                "Usage: /mute SYMBOL",
            )
            return
        symbol = parts[1].upper()
        self.cmd_mute.emit(symbol, reply_chat_id)

    def _handle_sell(self, parts: list, reply_chat_id: str) -> None:
        if len(parts) < 2:
            TelegramNotifier.send_message(
                self._token, reply_chat_id, "Usage: /sell SYMBOL"
            )
            return
        self.cmd_sell.emit(parts[1].upper(), reply_chat_id)

    def _handle_revise(self, parts: list, reply_chat_id: str) -> None:
        # /revise SYMBOL low|high NEW_PRICE
        if len(parts) < 4:
            TelegramNotifier.send_message(
                self._token,
                reply_chat_id,
                "Usage: /revise SYMBOL low|high NEW_PRICE\nExample: /revise AAPL low 145.00",
            )
            return
        symbol = parts[1].upper()
        side = parts[2].lower()
        if side not in ("low", "high"):
            TelegramNotifier.send_message(
                self._token,
                reply_chat_id,
                "Side must be 'low' or 'high'. Example: /revise AAPL low 145.00",
            )
            return
        try:
            new_price = float(parts[3])
        except ValueError:
            TelegramNotifier.send_message(
                self._token,
                reply_chat_id,
                "NEW_PRICE must be a number. Example: /revise AAPL low 145.00",
            )
            return
        self.cmd_revise.emit(symbol, side, new_price, reply_chat_id)
