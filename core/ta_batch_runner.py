"""
ta_batch_runner.py — Pre-market TradingAgents batch runner (QThread).

Fires once per calendar day at the configured pre-market time (ta_batch_time_et,
default 07:00 ET). Calls run_stock_monitor_batch.py in a subprocess using the
TradingAgents venv interpreter, passes current watchlist symbols, and streams
stdout to data/ta_batch.log.

Config keys in trader_config.json:
  ta_batch_enabled        bool   whether the auto-run is active (default false)
  ta_batch_time_et        str    "HH:MM" in US/Eastern (default "07:00")
  tradingagents_python_path  str  path to TradingAgents venv python.exe
  tradingagents_repo_dir     str  path to TradingAgents repo root
"""
import logging
import os
import subprocess
from datetime import datetime
from typing import List, Optional

import pytz
from PyQt5.QtCore import QThread, pyqtSignal

from core.portfolio import load_trader_config, save_trader_config
from core.watchlist_store import load_watchlist

_log = logging.getLogger(__name__)

_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "ta_batch.log")
_EASTERN = pytz.timezone("US/Eastern")

_DEFAULT_PYTHON = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        "..", "..", "TradingAgents", ".venv", "Scripts", "python.exe",
    )
)
_DEFAULT_REPO = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "TradingAgents")
)
_BATCH_SCRIPT = "scripts/run_stock_monitor_batch.py"


class TABatchRunner(QThread):
    """Fires once per day before market open to populate tradingagents_research.json."""

    batch_status = pyqtSignal(str)   # progress messages → agent activity log

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True
        self._active_proc: Optional[subprocess.Popen] = None

    def stop(self) -> None:
        self._running = False
        if self._active_proc is not None:
            try:
                self._active_proc.terminate()
            except Exception:
                pass

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Poll every 60 s; fire batch when clock passes ta_batch_time_et today."""
        while self._running:
            try:
                self._maybe_run_batch()
            except Exception as exc:
                _log.exception("TABatchRunner tick error: %s", exc)
            self.msleep(60_000)

    def _maybe_run_batch(self) -> None:
        config = load_trader_config()
        if not config.get("ta_batch_enabled", False):
            return

        batch_time_str = config.get("ta_batch_time_et", "07:00")
        last_run_date = config.get("last_ta_batch_date", "")

        now_et = datetime.now(_EASTERN)
        today_str = now_et.strftime("%Y-%m-%d")

        if last_run_date == today_str:
            return  # already ran today

        try:
            hour, minute = (int(x) for x in batch_time_str.split(":"))
        except (ValueError, AttributeError):
            _log.warning("TABatchRunner: invalid ta_batch_time_et %r", batch_time_str)
            return

        if now_et.hour < hour or (now_et.hour == hour and now_et.minute < minute):
            return  # not yet time

        if self._active_proc is not None and self._active_proc.poll() is None:
            _log.debug("TABatchRunner: subprocess still running, skipping duplicate trigger")
            return

        self._run_batch(config, today_str)

    # ── Subprocess execution ──────────────────────────────────────────────────

    def _run_batch(self, config: dict, today_str: str) -> None:
        python_path = config.get(
            "tradingagents_python_path", _DEFAULT_PYTHON
        )
        repo_dir = config.get("tradingagents_repo_dir", _DEFAULT_REPO)
        stock_monitor_data_dir = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "data")
        )

        if not os.path.isfile(python_path):
            self.batch_status.emit(
                f"TA batch SKIPPED: python not found at {python_path}"
            )
            _log.error("TABatchRunner: python not found at %s", python_path)
            return

        script_path = os.path.join(repo_dir, _BATCH_SCRIPT)
        if not os.path.isfile(script_path):
            self.batch_status.emit(f"TA batch SKIPPED: script not found at {script_path}")
            _log.error("TABatchRunner: script not found at %s", script_path)
            return

        tickers = self._get_watchlist_tickers()
        if not tickers:
            self.batch_status.emit("TA batch SKIPPED: watchlist is empty")
            return

        cmd = [
            python_path, script_path,
            "--tickers", *tickers,
            "--stock-monitor-dir", stock_monitor_data_dir,
        ]

        self.batch_status.emit(
            f"TA batch START: {len(tickers)} tickers [{', '.join(tickers)}]"
        )
        _log.info("TABatchRunner: launching %s", " ".join(cmd))

        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as log_file:
                log_file.write(
                    f"\n{'='*60}\n"
                    f"TABatch run {today_str}  cmd: {' '.join(cmd)}\n"
                    f"{'='*60}\n"
                )
                self._active_proc = subprocess.Popen(
                    cmd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    cwd=repo_dir,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                )

            # Stream is closed; wait for exit
            exit_code = self._active_proc.wait()
            self._active_proc = None

        except Exception as exc:
            self.batch_status.emit(f"TA batch ERROR: {exc}")
            _log.exception("TABatchRunner subprocess error")
            self._active_proc = None
            return

        # Always mark today as attempted so we don't retry every 60s on failure.
        # A failed run is still "done for today" — operator can check ta_batch.log.
        cfg = load_trader_config()
        cfg["last_ta_batch_date"] = today_str
        save_trader_config(cfg)

        if exit_code == 0:
            self.batch_status.emit(f"TA batch DONE: exit 0 — research written to tradingagents_research.json")
            self._record_batch_cost(tickers)
        else:
            self.batch_status.emit(f"TA batch FAILED: exit code {exit_code} — see data/ta_batch.log")
            _log.error("TABatchRunner: subprocess exited with code %d", exit_code)

    def _record_batch_cost(self, tickers: List[str]) -> None:
        """Write an estimated cost entry for the TradingAgents batch run.

        TradingAgents calls Claude via LangChain and never writes to
        claude_usage_log.json directly, so costs are invisible to the UI.
        We estimate: 4 analysts + 1 researcher per ticker × sonnet rates.
        The estimate is conservative — actual usage depends on context length.
        """
        import json as _json
        from datetime import timezone as _tz
        from core.claude_cost_tracker import log_usage as _log_cu

        # Rough token budget per ticker for 4 analysts + final trader call via Sonnet:
        # ~8 000 input tokens + ~3 000 output tokens each.
        est_in_tok  = 8_000 * len(tickers)
        est_out_tok = 3_000 * len(tickers)
        _log_cu(
            model="claude-sonnet-4-6",
            in_tok=est_in_tok,
            out_tok=est_out_tok,
            trigger=f"ta_batch:{','.join(tickers)}",
        )
        _log.info(
            "TABatchRunner: logged estimated cost for %d tickers "
            "(~%d in, ~%d out tokens)", len(tickers), est_in_tok, est_out_tok,
        )

    def _get_watchlist_tickers(self) -> List[str]:
        try:
            entries = load_watchlist()
            return [e.symbol.upper() for e in entries if getattr(e, "symbol", None)]
        except Exception as exc:
            _log.warning("TABatchRunner: could not load watchlist: %s", exc)
            return []
