import smtplib
import ssl
from email.message import EmailMessage
from typing import Tuple

from core.models import AlertRecord
from notifiers.base_notifier import BaseNotifier


class EmailNotifier(BaseNotifier):
    def __init__(self, smtp_host: str, smtp_port: int, username: str, password: str, to_addr: str):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.to_addr = to_addr

    def _build_message(self, subject: str, body: str) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.username
        msg["To"] = self.to_addr
        msg["Subject"] = subject
        msg.set_content(body)
        return msg

    def _send_message(self, msg: EmailMessage) -> Tuple[bool, str]:
        context = ssl.create_default_context()
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as server:
                server.ehlo()
                server.starttls(context=context)
                server.login(self.username, self.password)
                server.send_message(msg)
            return True, "Email sent."
        except smtplib.SMTPAuthenticationError:
            return False, "Authentication failed. Check username/password."
        except smtplib.SMTPException as exc:
            return False, f"SMTP error: {exc}"
        except OSError as exc:
            return False, f"Network error: {exc}"

    def send(self, record: AlertRecord) -> bool:
        action = "SELL signal" if record.direction == "ABOVE HIGH" else "BUY opportunity"
        subject = f"[Stock Monitor] {record.symbol} — {action}"
        body = (
            f"{record.symbol} — {action}\n\n"
            f"Current price: ${record.price:.2f}\n"
            f"Target: ${record.target:.2f} ({record.direction})\n"
            f"Time: {record.timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        ok, _ = self._send_message(self._build_message(subject, body))
        return ok

    def test_connection(self) -> Tuple[bool, str]:
        if not all([self.smtp_host, self.username, self.password, self.to_addr]):
            return False, "Email settings incomplete."
        subject = "[Stock Monitor] Test Connection"
        body = "This is a test email from Stock Monitor. Connection is working correctly."
        return self._send_message(self._build_message(subject, body))
