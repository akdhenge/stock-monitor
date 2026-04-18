import json
import logging
import os
import queue
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from PyQt5.QtCore import QThread, pyqtSignal

from core.ai_research_store import get_cached_entry, get_cached_symbols
from core.models import AlertRecord
from core.scan_results_store import load_scan_results
from core.watchlist_store import load_watchlist

_log = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_PUBLISH_DIR = os.path.join(_DATA_DIR, "web_publish")
_HISTORY_RETENTION_DAYS = 30

_APP_VERSION = "0.3.0"

_TRIGGER_PRIORITY = [
    "manual", "telegram",
    "deep_scan_complete", "complete_scan_complete",
    "alert", "watchlist_changed", "interval",
]

_SCAN_PUBLISHABLE = (
    "symbol", "total_score", "score_value", "score_growth", "score_technical",
    "price", "sector", "scan_mode", "ai_rank",
    "pe_ratio", "peg_ratio", "debt_equity",
    "revenue_growth", "roe", "rsi",
    "score_congressional", "timestamp",
)


def _priority(trigger: str) -> int:
    try:
        return _TRIGGER_PRIORITY.index(trigger)
    except ValueError:
        return len(_TRIGGER_PRIORITY)


def _utcnow() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


class WebPublisher(QThread):
    publish_started   = pyqtSignal(str)        # trigger
    publish_succeeded = pyqtSignal(str, str)   # trigger, utc_timestamp
    publish_failed    = pyqtSignal(str, str)   # trigger, error_message

    def __init__(
        self,
        get_settings: Callable[[], Dict[str, Any]],
        get_alerts: Callable[[], List[AlertRecord]],
        parent=None,
    ):
        super().__init__(parent)
        self._get_settings = get_settings
        self._get_alerts = get_alerts
        self._queue: queue.Queue = queue.Queue()
        self._running = False
        self._history: List[Dict[str, Any]] = []  # last 10 publish events (newest first)
        self._last_publish_utc: Optional[str] = None
        self._last_trigger: Optional[str] = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def request_publish(self, trigger: str) -> None:
        self._queue.put(trigger)

    def stop(self) -> None:
        self._running = False
        self._queue.put(None)

    def get_last_publish_info(self) -> Dict[str, Any]:
        return {
            "last_utc": self._last_publish_utc,
            "last_trigger": self._last_trigger,
            "history": list(self._history),
        }

    # ── Thread run loop ────────────────────────────────────────────────────────

    def run(self) -> None:
        self._running = True
        while self._running:
            try:
                trigger = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if trigger is None:
                break

            # Coalesce: absorb any additional triggers that arrive in the next 2 s
            batch = [trigger]
            deadline = time.monotonic() + 2.0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    t = self._queue.get(timeout=min(0.1, remaining))
                    if t is None:
                        self._running = False
                        break
                    batch.append(t)
                except queue.Empty:
                    break

            if not self._running:
                break

            best = min(batch, key=_priority)
            self.publish_started.emit(best)
            try:
                self._do_publish(best)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                _log.error("WebPublisher: publish failed (trigger=%s): %s", best, err)
                self._record_event(best, "failed", err)
                self.publish_failed.emit(best, err)

    # ── Serialization ──────────────────────────────────────────────────────────

    def _do_publish(self, trigger: str) -> None:
        os.makedirs(_PUBLISH_DIR, exist_ok=True)
        now_utc = _utcnow()

        # Write data files first, meta.json last (atomic-ish pointer)
        latest_data = self._serialize_scan_results()
        self._write_json("latest.json", latest_data)

        watchlist_data = self._serialize_watchlist(now_utc)
        self._write_json("watchlist.json", watchlist_data)

        alerts_data = self._serialize_alerts(now_utc)
        self._write_json("alerts.json", alerts_data)

        self._write_ai_research(now_utc)

        self._update_history_snapshot(latest_data, now_utc)

        settings = self._get_settings()
        poll_interval_min = settings.get("web_publish_interval_minutes", 15)
        meta = {
            "schema_version": 1,
            "last_updated_utc": now_utc,
            "trigger": trigger,
            "app_version": _APP_VERSION,
            "poll_interval_seconds": max(poll_interval_min * 60, 60),
        }
        self._write_json("meta.json", meta)

        # Upload to R2 if credentials are present (local files already written above)
        r2_err = self._upload_to_r2(settings)
        if r2_err:
            _log.warning("WebPublisher: R2 upload failed: %s", r2_err)
            # Don't raise — local write succeeded; surface via signal
            self._last_publish_utc = now_utc
            self._last_trigger = trigger
            self._record_event(trigger, "local_only", r2_err)
            self.publish_succeeded.emit(trigger, now_utc)
            return

        self._last_publish_utc = now_utc
        self._last_trigger = trigger
        self._record_event(trigger, "ok", "")
        self.publish_succeeded.emit(trigger, now_utc)
        _log.info("WebPublisher: published (trigger=%s) at %s", trigger, now_utc)

    def _write_json(self, rel_path: str, data: Any) -> None:
        path = os.path.join(_PUBLISH_DIR, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    # ── R2 upload ──────────────────────────────────────────────────────────────

    def _upload_to_r2(self, settings: Dict[str, Any]) -> Optional[str]:
        """Upload all local web_publish files to R2. Returns error string or None."""
        account_id = settings.get("r2_account_id", "").strip()
        access_key = settings.get("r2_access_key_id", "").strip()
        secret_key = settings.get("r2_secret_access_key", "").strip()
        bucket     = settings.get("r2_bucket", "trader-data").strip()
        endpoint   = settings.get("r2_endpoint_url", "").strip()

        if not all([account_id, access_key, secret_key, bucket]):
            return None  # not configured — local-only mode, no error

        if not endpoint:
            endpoint = f"https://{account_id}.r2.cloudflarestorage.com"

        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            return "boto3 not installed — run: py -3.9 -m pip install boto3"

        try:
            s3 = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=Config(signature_version="s3v4"),
            )
            uploaded = 0
            errors = []
            for root, _dirs, files in os.walk(_PUBLISH_DIR):
                for fname in files:
                    local_path = os.path.join(root, fname)
                    key = os.path.relpath(local_path, _PUBLISH_DIR).replace("\\", "/")
                    content_type = "application/json" if fname.endswith(".json") else "application/octet-stream"
                    try:
                        with open(local_path, "rb") as f:
                            s3.put_object(
                                Bucket=bucket,
                                Key=key,
                                Body=f,
                                ContentType=content_type,
                                CacheControl="no-cache, max-age=0",
                            )
                        uploaded += 1
                    except Exception as e:
                        errors.append(f"{key}: {e}")

            if errors:
                return f"{len(errors)} file(s) failed: {errors[0]}"
            _log.info("WebPublisher: R2 upload complete (%d files)", uploaded)
            return None
        except Exception as exc:
            return str(exc)

    def test_r2_connection(self, settings: Dict[str, Any]) -> tuple:
        """Returns (ok: bool, message: str). Safe to call from main thread."""
        account_id = settings.get("r2_account_id", "").strip()
        access_key = settings.get("r2_access_key_id", "").strip()
        secret_key = settings.get("r2_secret_access_key", "").strip()
        bucket     = settings.get("r2_bucket", "trader-data").strip()
        endpoint   = settings.get("r2_endpoint_url", "").strip() or \
                     f"https://{account_id}.r2.cloudflarestorage.com"

        if not all([account_id, access_key, secret_key, bucket]):
            return False, "R2 credentials incomplete."
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            return False, "boto3 not installed — run: py -3.9 -m pip install boto3"
        try:
            s3 = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=Config(signature_version="s3v4"),
            )
            marker = "__connection_test__.json"
            s3.put_object(Bucket=bucket, Key=marker,
                          Body=b'{"test":true}', ContentType="application/json")
            s3.delete_object(Bucket=bucket, Key=marker)
            return True, f"Connected to R2 bucket '{bucket}' successfully."
        except Exception as exc:
            return False, str(exc)

    def _serialize_scan_results(self) -> Dict[str, Any]:
        results = load_scan_results()
        deep_results  = [r for r in results if r.scan_mode == "deep"]
        complete_results = [r for r in results if r.scan_mode == "complete"]
        # Fall back to any scan mode if we have no mode-specific results
        if not deep_results and not complete_results:
            deep_results = results

        def _scan_ts(rs):
            if not rs:
                return None
            return max(r.timestamp for r in rs).strftime("%Y-%m-%dT%H:%M:%SZ")

        def _rows(rs, n):
            rows = []
            for r in rs[:n]:
                row = {}
                for k in _SCAN_PUBLISHABLE:
                    v = getattr(r, k, None)
                    if hasattr(v, "isoformat"):
                        v = v.strftime("%Y-%m-%dT%H:%M:%SZ")
                    row[k] = v
                rows.append(row)
            return rows

        return {
            "deep": {
                "scan_timestamp_utc": _scan_ts(deep_results),
                "universe_size": len(deep_results),
                "top10": _rows(deep_results, 10),
            },
            "complete": {
                "scan_timestamp_utc": _scan_ts(complete_results),
                "universe_size": len(complete_results),
                "top5": _rows(complete_results, 5),
            },
        }

    def _serialize_watchlist(self, now_utc: str) -> Dict[str, Any]:
        entries = load_watchlist()
        _target_hit_map = {
            "OK": "none",
            "BELOW LOW": "low_hit",
            "ABOVE HIGH": "high_hit",
        }
        return {
            "updated_utc": now_utc,
            "entries": [
                {
                    "symbol": e.symbol,
                    "low": e.low_target,
                    "high": e.high_target,
                    "notes": e.notes,
                    "last_price": e.current_price,
                    "target_hit_state": _target_hit_map.get(e.alert_status, "none"),
                }
                for e in entries
            ],
        }

    def _serialize_alerts(self, now_utc: str) -> Dict[str, Any]:
        records = self._get_alerts()
        _type_map = {
            "ABOVE HIGH": "price_high_hit",
            "BELOW LOW":  "price_low_hit",
        }
        events = []
        for r in records[:200]:
            events.append({
                "ts_utc": r.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "type":   _type_map.get(r.direction, r.direction.lower().replace(" ", "_")),
                "symbol": r.symbol,
                "context": {"price": r.price, "target": r.target},
            })
        return {"updated_utc": now_utc, "events": events}

    def _write_ai_research(self, now_utc: str) -> None:
        ai_dir = os.path.join(_PUBLISH_DIR, "ai_research")
        os.makedirs(ai_dir, exist_ok=True)
        symbols = get_cached_symbols()
        index_entries = []
        for sym in sorted(symbols):
            entry = get_cached_entry(sym)
            if entry is None:
                continue
            safe_entry = {
                "symbol": sym,
                "generated_utc": entry.get("timestamp", now_utc),
                "provider": entry.get("source", "unknown"),
                "model": "",
                "summary": entry.get("summary", ""),
                "sentiment": entry.get("sentiment", ""),
                "direction": entry.get("direction", ""),
                "timeframe": entry.get("timeframe", ""),
                "short_term": entry.get("short_term", ""),
                "long_term": entry.get("long_term", ""),
                "catalysts": entry.get("catalysts", ""),
                "stock_strategy": entry.get("stock_strategy", ""),
            }
            self._write_json(f"ai_research/{sym}.json", safe_entry)
            index_entries.append({
                "symbol": sym,
                "generated_utc": safe_entry["generated_utc"],
                "provider": safe_entry["provider"],
                "sentiment": safe_entry["sentiment"],
            })
        self._write_json("ai_research/index.json", {
            "updated_utc": now_utc,
            "entries": index_entries,
        })

    def _update_history_snapshot(self, latest_data: Dict[str, Any], now_utc: str) -> None:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        history_dir = os.path.join(_PUBLISH_DIR, "history")
        os.makedirs(history_dir, exist_ok=True)

        snap_path = os.path.join(history_dir, f"{today}.json")
        if not os.path.exists(snap_path):
            snapshot = {
                "date": today,
                "deep_top10": latest_data.get("deep", {}).get("top10", []),
            }
            with open(snap_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2)

        # Rebuild index (drop entries older than retention window)
        snapshots = []
        for fname in sorted(os.listdir(history_dir), reverse=True):
            if not fname.endswith(".json") or fname == "index.json":
                continue
            date_str = fname[:-5]
            try:
                snap_dt = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                continue
            age_days = (datetime.utcnow() - snap_dt).days
            if age_days > _HISTORY_RETENTION_DAYS:
                os.remove(os.path.join(history_dir, fname))
                continue
            snapshots.append({
                "date": date_str,
                "deep_top10_path": f"history/{fname}",
            })

        self._write_json("history/index.json", {
            "snapshots": snapshots,
            "retention_days": _HISTORY_RETENTION_DAYS,
        })

    # ── Internal bookkeeping ───────────────────────────────────────────────────

    def _record_event(self, trigger: str, outcome: str, error: str) -> None:
        event = {
            "utc": _utcnow(),
            "trigger": trigger,
            "outcome": outcome,
            "error": error,
        }
        self._history.insert(0, event)
        if len(self._history) > 10:
            self._history.pop()
