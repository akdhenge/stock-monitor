from typing import Any, Dict, List, Optional, Tuple

import requests

from core.models import AlertRecord
from notifiers.base_notifier import BaseNotifier

_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_TIMEOUT = 10


class TelegramNotifier(BaseNotifier):
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    def _url(self, method: str) -> str:
        return _API_BASE.format(token=self.token, method=method)

    def send(self, record: AlertRecord) -> bool:
        direction_emoji = "🔴" if record.direction == "ABOVE HIGH" else "🟢"
        action = "SELL signal" if record.direction == "ABOVE HIGH" else "BUY opportunity"
        text = (
            f"{direction_emoji} <b>{record.symbol}</b> — {action}\n"
            f"Price: <b>${record.price:.2f}</b>\n"
            f"Target: ${record.target:.2f} ({record.direction})\n"
            f"Time: {record.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        try:
            resp = requests.post(
                self._url("sendMessage"),
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=_TIMEOUT,
            )
            return resp.ok
        except requests.RequestException:
            return False

    def test_connection(self) -> Tuple[bool, str]:
        if not self.token:
            return False, "No Telegram token configured."
        try:
            resp = requests.get(self._url("getMe"), timeout=_TIMEOUT)
            if not resp.ok:
                return False, f"API error: {resp.status_code} {resp.text}"
            data = resp.json()
            bot_name = data.get("result", {}).get("username", "?")
            if not self.chat_id:
                return False, f"Connected as @{bot_name}, but no Chat ID set."
            # Send a test message
            test_resp = requests.post(
                self._url("sendMessage"),
                json={"chat_id": self.chat_id, "text": "✅ Stock Monitor test — connection OK!"},
                timeout=_TIMEOUT,
            )
            if test_resp.ok:
                return True, f"Test message sent via @{bot_name}."
            return False, f"Bot reachable but message failed: {test_resp.text}"
        except requests.RequestException as exc:
            return False, f"Connection error: {exc}"

    @staticmethod
    def send_message(token: str, chat_id: str, text: str) -> bool:
        """Send a plain-text message to a Telegram chat. Returns True on success."""
        if not token or not chat_id:
            return False
        url = _API_BASE.format(token=token, method="sendMessage")
        try:
            resp = requests.post(
                url,
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=_TIMEOUT,
            )
            return resp.ok
        except requests.RequestException:
            return False

    @staticmethod
    def get_updates(token: str) -> Tuple[bool, Optional[str], str]:
        """
        Poll getUpdates to auto-detect a chat_id.
        Returns (success, chat_id_or_None, message).
        """
        if not token:
            return False, None, "No token provided."
        url = _API_BASE.format(token=token, method="getUpdates")
        try:
            resp = requests.get(url, timeout=_TIMEOUT)
            if not resp.ok:
                return False, None, f"API error: {resp.status_code}"
            results: List[Dict[str, Any]] = resp.json().get("result", [])
            if not results:
                return False, None, (
                    "No messages found. Send any message to your bot in Telegram first, then retry."
                )
            chat_id = str(results[-1]["message"]["chat"]["id"])
            return True, chat_id, f"Detected chat ID: {chat_id}"
        except (requests.RequestException, KeyError) as exc:
            return False, None, f"Error: {exc}"
