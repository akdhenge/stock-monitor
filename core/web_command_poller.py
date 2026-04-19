import json
import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from PyQt5.QtCore import QThread, pyqtSignal

_log = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


class WebCommandPoller(QThread):
    """Polls R2 cmds/pending/ for web-initiated commands and emits them for MainWindow to handle."""

    cmd_received = pyqtSignal(dict)  # full command dict including cmd_id

    def __init__(self, get_settings: Callable[[], Dict[str, Any]], parent=None):
        super().__init__(parent)
        self._get_settings = get_settings
        self._running = False

    # ── Public API ─────────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._running = False

    def write_done(self, cmd_id: str, status: str, message: str) -> None:
        """Write a done marker to cmds/done/{cmd_id}.json in R2."""
        s3 = self._make_r2_client()
        if s3 is None:
            return
        settings = self._get_settings()
        bucket = settings.get("r2_bucket", "trader-data")
        payload = json.dumps({
            "cmd_id": cmd_id,
            "status": status,
            "message": message,
            "ts_utc": _utcnow(),
        }).encode("utf-8")
        try:
            s3.put_object(
                Bucket=bucket,
                Key=f"cmds/done/{cmd_id}.json",
                Body=payload,
                ContentType="application/json",
                CacheControl="no-cache, max-age=0",
            )
        except Exception as exc:
            _log.error("WebCommandPoller: failed to write done marker %s: %s", cmd_id, exc)

    # ── Thread run loop ────────────────────────────────────────────────────────

    def run(self) -> None:
        self._running = True
        while self._running:
            try:
                self._poll_once()
            except Exception as exc:
                _log.error("WebCommandPoller: poll error: %s", exc)
            for _ in range(30):
                if not self._running:
                    break
                time.sleep(1)

    # ── Internal ───────────────────────────────────────────────────────────────

    def _make_r2_client(self):
        settings = self._get_settings()
        account_id = settings.get("r2_account_id", "").strip()
        access_key = settings.get("r2_access_key_id", "").strip()
        secret_key = settings.get("r2_secret_access_key", "").strip()
        endpoint   = settings.get("r2_endpoint_url", "").strip()

        if not all([account_id, access_key, secret_key]):
            return None

        if not endpoint:
            endpoint = f"https://{account_id}.r2.cloudflarestorage.com"

        try:
            import boto3
            from botocore.config import Config
            return boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=Config(signature_version="s3v4"),
            )
        except ImportError:
            _log.error("WebCommandPoller: boto3 not installed")
            return None
        except Exception as exc:
            _log.error("WebCommandPoller: failed to create R2 client: %s", exc)
            return None

    def _poll_once(self) -> None:
        s3 = self._make_r2_client()
        if s3 is None:
            return

        settings = self._get_settings()
        bucket = settings.get("r2_bucket", "trader-data")

        try:
            resp = s3.list_objects_v2(Bucket=bucket, Prefix="cmds/pending/")
        except Exception as exc:
            _log.error("WebCommandPoller: list_objects_v2 error: %s", exc)
            return

        if resp.get("IsTruncated"):
            _log.warning("WebCommandPoller: more than 1000 pending commands — truncated listing")

        for obj in resp.get("Contents", []):
            key = obj["Key"]
            try:
                get_resp = s3.get_object(Bucket=bucket, Key=key)
                body = get_resp["Body"].read()
                cmd = json.loads(body)
            except Exception as exc:
                _log.error("WebCommandPoller: failed to read command %s: %s", key, exc)
                continue

            cmd_id = key.split("/")[-1].replace(".json", "")
            cmd["cmd_id"] = cmd_id

            # Delete from pending before emitting so replays don't happen on crash
            try:
                s3.delete_object(Bucket=bucket, Key=key)
            except Exception as exc:
                _log.error("WebCommandPoller: failed to delete pending %s: %s", key, exc)

            _log.info("WebCommandPoller: received command type=%s id=%s", cmd.get("type"), cmd_id)
            self.cmd_received.emit(cmd)
