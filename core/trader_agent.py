"""
trader_agent.py — Autonomous paper-trading decision loop.

Architecture
------------
TraderAgent is a QThread that runs a message-queue loop. The GUI thread
delivers events (scan results, price ticks) via queue_*() methods which
are safe to call from any thread. All Alpaca calls happen inside run(),
never in the GUI thread.

Signal flow (wired in MainWindow):
  scanner.deep_scan_complete      → agent.queue_scan_results
  scanner.complete_scan_complete  → agent.queue_scan_results
  price_poller.prices_updated     → agent.queue_price_tick
  agent.trade_executed            → main_window._on_agent_trade  (→ Telegram)
  agent.agent_status              → main_window.statusBar().showMessage

Enable/disable: set 'enabled' and 'dry_run' in data/trader_config.json.
The agent reloads config on every scan cycle so changes take effect live.
"""
import json
import logging
import os
import queue
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from PyQt5.QtCore import QThread, pyqtSignal

from core import market_clock
from core.ai_research_store import get_cached_entry, get_cached_entry_merged
from core.portfolio import (
    build_entry_meta, delete_position_meta, get_position_meta,
    init_trader_config, load_all_meta, load_trader_config,
    save_position_meta, save_trader_config, update_high_water,
)
from core.risk_manager import (
    check_circuit_breaker, check_exit, check_hard_gates,
    compute_conviction_score, compute_decision_score, compute_sector_exposure, size_position,
)
from core.trade_journal import log_decision, log_fill
from core.performance_tracker import maybe_snapshot
from core.self_tuner import run_tuning_cycle, should_tune

_log = logging.getLogger(__name__)

_RANKING_CACHE = os.path.join(os.path.dirname(__file__), "..", "data", "claude_ranking_cache.json")
_HEARTBEAT_INTERVAL = 900   # 15 minutes
_MAX_STEPS = 100


class TraderAgent(QThread):
    trade_executed = pyqtSignal(dict)   # forwarded to Telegram by MainWindow
    agent_status   = pyqtSignal(str)    # status bar text
    agent_halted   = pyqtSignal(str)    # circuit breaker — MainWindow sends Telegram alert

    def __init__(self, get_settings, parent=None):
        super().__init__(parent)
        self._get_settings = get_settings
        self._running      = False
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._executor     = None           # TradeExecutor — lazily built in run()
        self._ath_nav      = 0.0            # all-time-high NAV
        self._last_snapshot_date = ""       # YYYY-MM-DD
        self._last_tune_count    = 0        # closed-trade count at last self-tune
        self._news_alert_cooldown: Dict[str, float] = {}  # sym → last alert epoch
        self._steps: list = []       # last 30 audit steps written to agent_status.json
        self._cb_active: bool = False  # circuit breaker state for web status
        self._options_executor = None  # OptionsExecutor — lazily built after TradeExecutor

    # ── Step logging ───────────────────────────────────────────────────────────

    def _log_step(self, text: str, status: str = "ok") -> None:
        entry = {
            "utc": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "text": text,
            "status": status,
        }
        self._steps.append(entry)
        if len(self._steps) > _MAX_STEPS:
            self._steps = self._steps[-_MAX_STEPS:]
        _log.info("STEP [%s] %s", status, text)

    def _write_agent_status(self, status_text: str = "") -> None:
        """Write agent_status.json locally and upload to R2."""
        positions_count = len(load_all_meta())
        payload = {
            "schema_version": 1,
            "last_updated_utc": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status_text": status_text or (self._steps[-1]["text"] if self._steps else "Idle"),
            "circuit_breaker_active": self._cb_active,
            "positions_count": positions_count,
            "ath_nav": self._ath_nav,
            "steps": list(self._steps),
        }
        pub_dir = os.path.join(os.path.dirname(__file__), "..", "data", "web_publish")
        os.makedirs(pub_dir, exist_ok=True)
        path = os.path.join(pub_dir, "agent_status.json")
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, path)
        except Exception as exc:
            _log.warning("Could not write agent_status.json: %s", exc)
            return
        self._upload_status_to_r2(path)

    def _upload_status_to_r2(self, path: str) -> None:
        settings = self._get_settings()
        account_id = settings.get("r2_account_id", "").strip()
        access_key = settings.get("r2_access_key_id", "").strip()
        secret_key = settings.get("r2_secret_access_key", "").strip()
        bucket     = settings.get("r2_bucket", "trader-data").strip()
        endpoint   = settings.get("r2_endpoint_url", "").strip()
        if not all([account_id, access_key, secret_key, bucket]):
            return
        if not endpoint:
            endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        try:
            import boto3
            from botocore.config import Config
            s3 = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=Config(signature_version="s3v4"),
            )
            with open(path, "rb") as f:
                s3.put_object(
                    Bucket=bucket,
                    Key="agent_status.json",
                    Body=f,
                    ContentType="application/json",
                    CacheControl="no-cache, max-age=0",
                )
        except Exception as exc:
            _log.debug("agent_status.json R2 upload failed: %s", exc)

    # ── Public slot-like methods (called from GUI thread) ──────────────────────

    def queue_scan_results(self, results: list) -> None:
        """Called by MainWindow when deep/complete scan finishes."""
        try:
            self._queue.put_nowait(("scan", results))
        except queue.Full:
            _log.warning("TraderAgent: scan queue full — scan result dropped")

    def queue_price_tick(self, prices: dict) -> None:
        """Called by MainWindow on every price_poller tick. High-frequency — keep fast."""
        try:
            self._queue.put_nowait(("prices", prices))
        except queue.Full:
            pass  # price ticks are transient; silently drop when backed up

    def stop(self) -> None:
        self._running = False
        try:
            self._queue.put_nowait(("stop", None))
        except queue.Full:
            pass

    def queue_forced_sell(self, symbol: str) -> None:
        """Force-close a position by symbol. Safe to call from any thread."""
        try:
            self._queue.put_nowait(("sell", symbol.upper()))
        except queue.Full:
            _log.warning("TraderAgent: queue full — forced sell %s dropped", symbol)

    # ── QThread entry point ────────────────────────────────────────────────────

    def run(self) -> None:
        self._running = True
        _log.info("TraderAgent: starting")
        self.agent_status.emit("Trader: starting…")

        # Ensure trader_config.json exists with defaults
        init_trader_config()

        # Lazily connect to Alpaca (keys may not be set yet)
        self._executor = self._init_executor()
        if self._executor is None:
            self.agent_status.emit("Trader: no Alpaca keys — disabled")
            self._log_step("Startup failed — no Alpaca API keys configured", "error")
            self._write_agent_status("Trader: no Alpaca keys — disabled")
            return

        # Seed all-time-high from portfolio history
        try:
            self._ath_nav = self._executor.get_all_time_high_nav()
        except Exception:
            pass

        self._options_executor = self._init_options_executor()

        mode_str = "dry-run" if load_trader_config().get("dry_run") else "LIVE paper"
        self.agent_status.emit(f"Trader: running ({mode_str})")
        _log.info("TraderAgent: executor ready, ATH NAV=%.2f", self._ath_nav)
        self._log_step(f"Agent ready ({mode_str}) — ATH NAV ${self._ath_nav:,.0f}")
        self._write_agent_status()

        last_heartbeat = time.time()

        while self._running:
            # Heartbeat
            if time.time() - last_heartbeat >= _HEARTBEAT_INTERVAL:
                self._heartbeat()
                last_heartbeat = time.time()

            # Drain queue with a short timeout so heartbeat fires on schedule
            try:
                msg_type, data = self._queue.get(timeout=5.0)
            except queue.Empty:
                continue

            if msg_type == "stop":
                break
            elif msg_type == "scan":
                self._process_scan(data)
                self._process_options_scan(data)
            elif msg_type == "prices":
                self._process_price_tick(data)
            elif msg_type == "sell":
                self._process_forced_sell(data)

        _log.info("TraderAgent: stopped")
        self.agent_status.emit("Trader: stopped")

    # ── Scan processing (entry logic) ─────────────────────────────────────────

    def _process_scan(self, results: list) -> None:
        config = load_trader_config()
        if not config.get("enabled", False):
            return
        if not market_clock.is_rth():
            _log.debug("TraderAgent: scan received outside RTH — skipping entries")
            return

        self.agent_status.emit("Trader: evaluating scan results…")
        self._log_step(f"Scan received — {len(results)} stocks to evaluate")
        cycle_id = str(uuid.uuid4())[:8]

        # Fetch VIX + SPY once — used for both regime detection and existing gates
        import yfinance as yf
        from core.market_regime import detect_regime, format_regime_log

        vix_val  = None
        spy_hist = None
        try:
            vix_val  = float(yf.Ticker("^VIX").fast_info.last_price or 20.0)
            spy_hist = yf.Ticker("SPY").history(period="1y", interval="1d")
        except Exception as exc:
            _log.warning("TraderAgent: VIX/SPY fetch failed: %s", exc)

        # VIX halt gate
        vix_threshold = config.get("vix_halt_threshold", 30.0)
        if vix_val and vix_val > vix_threshold:
            _log.info("TraderAgent: VIX %.1f > %.0f — skipping entries", vix_val, vix_threshold)
            self._log_step(f"VIX: {vix_val:.1f} > {vix_threshold:.0f} — entries paused (high fear)", "warn")
            self.agent_status.emit(f"Trader: VIX {vix_val:.1f} > {vix_threshold:.0f} — entries paused")
            self._write_agent_status()
            return
        elif vix_val:
            self._log_step(f"VIX: {vix_val:.1f}")

        # Detect market regime — drives thresholds + candidate pool size
        regime = detect_regime(spy_hist, vix_val)
        self._log_step(format_regime_log(regime))
        self.agent_status.emit(f"Trader: regime {regime.label.upper()} — evaluating…")

        # Build effective config with regime-adjusted thresholds
        effective_config = dict(config)
        effective_config["min_scan_score"]     = regime.min_scan_score
        effective_config["min_decision_score"] = regime.min_decision_score

        # Adaptive threshold: replace fixed regime gate with percentile-based one
        from core.score_history import (
            load_history, append_scan_results, compute_adaptive_threshold,
            compute_adaptive_decision_threshold, append_decision_score,
            save_history as save_score_history, check_per_symbol_quality,
        )
        from core.risk_manager import _REGIME_SIZE_MULT
        from core.history_store import log_decision as hist_log_decision, log_debate as hist_log_debate
        _score_hist = load_history()
        append_scan_results(_score_hist, results, regime.label)
        _adaptive = compute_adaptive_threshold(_score_hist, regime, config)
        _decision_adaptive = compute_adaptive_decision_threshold(_score_hist, regime, config)
        save_score_history(_score_hist)
        effective_config["min_scan_score"]     = _adaptive["effective_min_scan_score"]
        effective_config["min_decision_score"] = _decision_adaptive["effective_min_decision_score"]
        _regime_size_mult = _REGIME_SIZE_MULT.get(regime.label, 1.0)
        self._log_step(
            f"Adaptive scan gate: >={_adaptive['effective_min_scan_score']:.1f} "
            f"(p{_adaptive['percentile_used']} / {_adaptive['n_samples']}s, mode={_adaptive['mode']})"
        )
        self._log_step(
            f"Adaptive conviction gate: >={_decision_adaptive['effective_min_decision_score']:.1f} "
            f"(p{_decision_adaptive['percentile_used']} / {_decision_adaptive['n_samples']}s, "
            f"mode={_decision_adaptive['mode']}) | regime size mult={_regime_size_mult}"
        )

        # SPY position-size factor (unchanged: halve sizes when below 200d MA)
        spy_regime_factor = 1.0
        if spy_hist is not None and len(spy_hist) >= 200:
            sma200    = float(spy_hist["Close"].tail(200).mean())
            spy_price = float(spy_hist["Close"].iloc[-1])
            if spy_price < sma200:
                spy_regime_factor = 0.5
                self._log_step(f"SPY: ${spy_price:.0f} < 200d ${sma200:.0f} — half-size mode", "warn")

        try:
            account = self._executor.get_account()
        except Exception as exc:
            _log.error("TraderAgent: could not fetch account: %s", exc)
            self._log_step(f"Alpaca account fetch failed: {exc}", "error")
            self._write_agent_status()
            return

        nav  = account["equity"]
        cash = account["cash"]
        self._log_step(f"Account: NAV ${nav:,.0f} / cash ${cash:,.0f}")

        # Circuit breaker check
        intraday_pnl = account["day_pnl"]
        halted, halt_reason = check_circuit_breaker(nav, self._ath_nav, intraday_pnl, config)
        if halted:
            self._cb_active = True
            msg = f"TraderAgent HALTED: {halt_reason}"
            _log.warning(msg)
            self._log_step(f"Circuit breaker TRIGGERED — {halt_reason}", "error")
            self.agent_halted.emit(msg)
            self.agent_status.emit(f"Trader: HALTED — {halt_reason}")
            if nav > self._ath_nav:
                self._ath_nav = nav
            self._write_agent_status()
            return
        self._cb_active = False
        self._log_step("Circuit breaker: clear")

        # Update ATH
        if nav > self._ath_nav:
            self._ath_nav = nav

        # Current positions + sector exposure
        try:
            alpaca_positions = self._executor.get_positions()
        except Exception as exc:
            _log.error("TraderAgent: could not fetch positions: %s", exc)
            return

        open_symbols     = [p["symbol"] for p in alpaca_positions]
        position_values  = {p["symbol"]: p.get("market_value", 0) or 0 for p in alpaca_positions}
        meta_all         = load_all_meta()
        sector_exposure  = compute_sector_exposure(meta_all, position_values, nav)

        # Load ClaudeRankingAnalyst cache
        ranking = self._load_ranking_cache()
        ranked_list = ranking.get("ranked", []) if ranking else []
        if ranked_list:
            self._log_step(f"Claude ranking loaded — {len(ranked_list)} candidates")
        else:
            self._log_step("No Claude ranking cached — evaluating by scan score only", "warn")

        # Build a lookup of scan results by symbol
        scan_by_symbol = {r.symbol: r for r in results if hasattr(r, "symbol")}

        # Candidate pool size is regime-dependent: bear/neutral opens more candidates
        # so the debate filter can select the genuinely tradeable ones.
        max_candidates = regime.max_candidates
        self._log_step(f"Evaluating top {max_candidates} ranked candidates (regime: {regime.label})")
        settings = self._get_settings()
        entries_made = 0

        for ranked_item in ranked_list[:max_candidates]:
            if not self._running:
                break

            sym = ranked_item.get("symbol", "").upper()
            if not sym or sym in open_symbols:
                continue

            scan_result = scan_by_symbol.get(sym)
            if scan_result is None:
                _log.debug("TraderAgent: %s ranked but not in this scan batch — skipped", sym)
                self._log_step(f"{sym}: not in this scan batch — skipped", "skip")
                continue

            # Earnings gate — block entry within N days of earnings
            block_days = config.get("earnings_block_days", 3)
            dte = self._days_to_earnings(sym)
            if dte is not None and dte <= block_days:
                _log.info("TraderAgent: %s skipped — earnings in %d day(s)", sym, dte)
                self._log_step(f"{sym}: skipped — earnings in {dte}d (block {block_days}d)", "skip")
                log_decision(sym, "REJECT", f"earnings in {dte}d (block_days={block_days})",
                             scan_score=scan_result.total_score, nav_at_eval=nav, cycle_id=cycle_id)
                continue

            ai_research  = get_cached_entry_merged(sym)
            ai_rank      = ranked_item.get("rank")
            alloc_pct    = ranked_item.get("allocation_pct")

            # TradingAgents veto: Sell/Underweight blocks entry regardless of technicals.
            # Never upgrades a bear technical regime — only blocks.
            if ai_research and ai_research.get("source") == "tradingagents":
                ta_rating = ai_research.get("ta_rating", "")
                if ta_rating in ("Sell", "Underweight"):
                    self._log_step(
                        f"{sym}: VETOED by TradingAgents ({ta_rating}) — skipping entry", "skip"
                    )
                    log_decision(sym, "REJECT", f"TradingAgents veto: {ta_rating}",
                                 scan_score=scan_result.total_score,
                                 ai_rank=ai_rank, nav_at_eval=nav, cycle_id=cycle_id)
                    continue

            # Parse entry/stop/target from ranked_item's stock_play string
            target_price = self._parse_target(ranked_item.get("stock_play", ""), scan_result.price)
            stop_pct     = config.get("default_stop_loss_pct", 0.08)
            trail_pct    = config.get("default_trailing_stop_pct", 0.12)

            # 1. Hard gates (regime-adjusted min_scan_score applied via effective_config)
            passed, reject_reason = check_hard_gates(
                symbol=sym,
                scan_result=scan_result,
                ai_research=ai_research,
                open_symbols=open_symbols,
                sector_exposure=sector_exposure,
                cash=cash,
                nav=nav,
                config=effective_config,
            )
            if not passed:
                self._log_step(f"{sym}: rejected — {reject_reason}", "skip")
                log_decision(sym, "REJECT", reject_reason,
                             scan_score=scan_result.total_score,
                             ai_rank=ai_rank, nav_at_eval=nav, cycle_id=cycle_id)
                continue

            # 1b. Per-symbol quality gate (soft: score must be near 30d mean)
            sym_ok, sym_reason = check_per_symbol_quality(sym, scan_result.total_score, _score_hist)
            if not sym_ok:
                self._log_step(f"{sym}: per-symbol gate — {sym_reason}", "skip")
                log_decision(sym, "REJECT", sym_reason,
                             scan_score=scan_result.total_score,
                             ai_rank=ai_rank, nav_at_eval=nav, cycle_id=cycle_id)
                continue

            # 2. Conviction score — gates entry AND drives sizing
            ai_sentiment = (ai_research or {}).get("sentiment", "")
            conviction, cv_breakdown = compute_conviction_score(
                scan_result, ai_rank, ai_research, alloc_pct
            )
            min_conviction = effective_config.get("min_decision_score", 55.0)
            append_decision_score(
                _score_hist, sym, conviction, regime.label,
                passed_gate=(conviction >= min_conviction),
                scan_score=scan_result.total_score,
                ai_rank=ai_rank, sentiment=ai_sentiment,
            )
            try:
                hist_log_decision(
                    symbol=sym, conviction=conviction,
                    conviction_gate=min_conviction, breakdown=cv_breakdown,
                    scan_score=scan_result.total_score,
                    scan_gate=effective_config["min_scan_score"],
                    regime_label=regime.label, ai_rank=ai_rank,
                    sentiment=ai_sentiment,
                    passed=(conviction >= min_conviction),
                )
            except Exception:
                pass
            if conviction < min_conviction:
                self._log_step(
                    f"{sym}: conviction {conviction:.1f} < gate {min_conviction:.1f} — rejected "
                    f"[scan={cv_breakdown.get('scan',0):.1f} rank={cv_breakdown.get('rank',0):.1f} "
                    f"alloc={cv_breakdown.get('ranker_alloc',0):.1f} sent={cv_breakdown.get('sentiment',0):.0f} "
                    f"tech={cv_breakdown.get('tech',0):.0f}]", "skip"
                )
                log_decision(sym, "REJECT", f"conviction {conviction:.1f} < gate {min_conviction:.1f}",
                             scan_score=scan_result.total_score, decision_score=conviction,
                             ai_rank=ai_rank, nav_at_eval=nav, cycle_id=cycle_id)
                continue

            # 3. Size position (conviction-driven curve + regime multiplier)
            dollars, size_breakdown = size_position(
                conviction_score=conviction,
                effective_min_conviction=min_conviction,
                volatility=scan_result.volatility_20d,
                regime_label=regime.label,
                nav=nav,
                cash=cash,
                config=effective_config,
            )
            if dollars <= 0:
                self._log_step(f"{sym}: insufficient cash after reserve — rejected", "skip")
                log_decision(sym, "REJECT", "insufficient cash after reserve for minimum size",
                             scan_score=scan_result.total_score, decision_score=conviction,
                             ai_rank=ai_rank, nav_at_eval=nav, cycle_id=cycle_id)
                continue

            if spy_regime_factor < 1.0:
                dollars = round(dollars * spy_regime_factor, 2)

            thesis = self._build_thesis(sym, ai_research, ranked_item)
            self._log_step(
                f"{sym}: conviction={conviction:.1f} size={size_breakdown.get('final_pct',0)*100:.1f}% "
                f"(base={size_breakdown.get('base_pct',0)*100:.1f}% "
                f"vol={size_breakdown.get('vol_factor',1):.2f}x "
                f"regime={size_breakdown.get('regime_mult',1):.2f}x) "
                f"-> ${dollars:,.0f}"
            )
            log_decision(sym, "BUY", "all gates passed",
                         scan_score=scan_result.total_score, decision_score=conviction,
                         ai_rank=ai_rank, size_dollars=dollars, nav_at_eval=nav,
                         ai_sentiment=ai_research.get("sentiment") if ai_research else None,
                         cycle_id=cycle_id)

            # 4. Bull/Bear debate — final check via Claude (regime-aware)
            from core.trade_debate import run_debate
            _bearish_tag = " [BEARISH]" if ai_sentiment == "BEARISH" else ""
            self._log_step(f"{sym}: sending to Claude debate ({regime.label} regime{_bearish_tag})...")
            debate_ok, debate_verdict = run_debate(
                symbol=sym,
                scan_result=scan_result,
                ai_research=ai_research,
                claude_rationale=ranked_item.get("rationale", ""),
                decision_score=conviction,
                conviction_breakdown=cv_breakdown,
                regime=regime.label,
                settings=settings,
            )
            _debate_verdict_str = "BUY" if debate_ok else "PASS"
            try:
                hist_log_debate(
                    symbol=sym, conviction=conviction,
                    verdict=_debate_verdict_str, reasoning=debate_verdict,
                    regime_label=regime.label,
                    model=settings.get("debate_model", "claude-sonnet-4-6"),
                    cost_usd=0.0,
                    price_at_eval=scan_result.price,
                )
            except Exception:
                pass

            if not debate_ok:
                self._log_step(f"{sym}: debate SKIP -- {debate_verdict[:80]}", "skip")
                log_decision(sym, "REJECT", f"debate: {debate_verdict[:120]}",
                             scan_score=scan_result.total_score, decision_score=conviction,
                             ai_rank=ai_rank, nav_at_eval=nav, cycle_id=cycle_id)
                continue
            self._log_step(f"{sym}: debate BUY -- {debate_verdict[:80]}")

            if config.get("dry_run", True):
                _log.info("DRY-RUN BUY %s $%.0f (score=%.1f) — no order placed", sym, dollars, dec_score)
                self._log_step(f"DRY-RUN BUY {sym} ${dollars:,.0f} (score {dec_score:.0f}) — no order placed", "trade")
                self.agent_status.emit(f"Trader: dry-run BUY {sym} ${dollars:.0f}")
                entries_made += 1
                continue

            # 5. Execute
            success = self._execute_entry(
                symbol=sym, dollars=dollars, current_price=scan_result.price,
                scan_result=scan_result, ai_rank=ai_rank, thesis=thesis,
                target_price=target_price, stop_loss_pct=stop_pct,
                trailing_stop_pct=trail_pct, cycle_id=cycle_id,
            )
            if success:
                self._log_step(f"BUY {sym} ${dollars:,.0f} placed (rank #{ai_rank}, score {dec_score:.0f})", "trade")
                open_symbols.append(sym)
                cash -= dollars
                entries_made += 1

        # Persist history with newly appended decision scores
        save_score_history(_score_hist)

        summary = f"Scan cycle complete — {entries_made} entr{'y' if entries_made == 1 else 'ies'} placed"
        try:
            from core.ticker_memory import get_rolling_alpha
            alpha = get_rolling_alpha(n_trades=10)
            if alpha is not None:
                summary += f" | rolling alpha vs SPY: {alpha:+.1f}%"
        except Exception:
            pass
        self.agent_status.emit(f"Trader: {summary}")
        self._log_step(summary)
        self._write_agent_status()

    # ── Price-tick processing (exit logic) ────────────────────────────────────

    def _process_price_tick(self, prices: dict) -> None:
        config = load_trader_config()
        if not config.get("enabled", False):
            return

        meta_all = load_all_meta()
        if not meta_all:
            return

        for sym, meta in list(meta_all.items()):
            price = prices.get(sym) or prices.get(sym.upper())
            if price is None:
                continue

            # Update trailing stop high-water mark
            update_high_water(sym, price)

            # Reload meta with updated high_water
            meta = get_position_meta(sym) or meta

            opened = datetime.fromisoformat(meta.opened_at)
            days_held = max(0, (datetime.now() - opened).days)

            ai_research = get_cached_entry(sym)
            should_exit, exit_reason = check_exit(meta, price, ai_research, days_held, config)

            if should_exit:
                log_decision(sym, "SELL", exit_reason, nav_at_eval=None)
                if not config.get("dry_run", True):
                    self._execute_exit(sym, exit_reason, meta, cycle_id=None)
                else:
                    _log.info("DRY-RUN SELL %s — %s", sym, exit_reason)
                continue

            # Partial exit: target hit and not yet taken
            if meta.target_price and price >= meta.target_price:
                _log.info("TARGET HIT %s @ $%.2f (target $%.2f) — partial exit in next cycle",
                          sym, price, meta.target_price)
                # Handled in heartbeat to avoid rapid-fire sells on tick volatility

    # ── Forced sell (Telegram /sell command) ──────────────────────────────────

    def _process_forced_sell(self, symbol: str) -> None:
        meta = get_position_meta(symbol)
        config = load_trader_config()
        if meta is None:
            _log.warning("Forced sell %s: no position meta found", symbol)
            return
        _log.info("Forced sell requested for %s", symbol)
        if not config.get("dry_run", True):
            self._execute_exit(symbol, "forced sell via Telegram", meta, cycle_id=None)
        else:
            _log.info("DRY-RUN forced sell %s — no order placed", symbol)

    # ── Heartbeat (NAV snapshot, partial exits, daily summary) ───────────────

    def _heartbeat(self) -> None:
        config = load_trader_config()
        et_time = market_clock.now_et().strftime("%H:%M ET")
        self._log_step(f"Heartbeat — {et_time}")

        self._last_snapshot_date = maybe_snapshot(self._executor, self._last_snapshot_date)

        # Self-tuner: check if enough new trades have accumulated
        try:
            from core.trade_journal import read_journal
            fills = [e for e in read_journal(last_n=2000)
                     if e.get("type") == "fill" and e.get("side") == "SELL"]
            if should_tune(self._last_tune_count, len(fills)):
                self._log_step(f"Self-tuner: {len(fills)} closed trades — running parameter adjustment")
                self.agent_status.emit("Trader: self-tuning parameters…")
                self._last_tune_count = run_tuning_cycle(self._last_tune_count)
                self._log_step("Self-tuner: parameters updated")
        except Exception as exc:
            _log.warning("SelfTuner error: %s", exc)

        if not config.get("enabled", False):
            self._write_agent_status()
            return
        if not market_clock.is_rth():
            self._write_agent_status()
            return

        try:
            account = self._executor.get_account()
            nav     = account["equity"]
            if nav > self._ath_nav:
                self._ath_nav = nav
            try:
                from core.ticker_memory import get_rolling_alpha
                alpha = get_rolling_alpha(n_trades=10)
                alpha_str = f" | rolling alpha: {alpha:+.1f}%" if alpha is not None else ""
            except Exception:
                alpha_str = ""
            self._log_step(f"NAV: ${nav:,.0f} (ATH: ${self._ath_nav:,.0f}){alpha_str}")
        except Exception:
            self._write_agent_status()
            return

        # Partial target exits
        meta_all = load_all_meta()
        try:
            alpaca_positions = self._executor.get_positions()
        except Exception:
            return

        price_by_sym = {p["symbol"]: p.get("current_price") for p in alpaca_positions}

        for sym, meta in list(meta_all.items()):
            price = price_by_sym.get(sym)
            if price is None or meta.target_price is None:
                continue
            if price < meta.target_price:
                continue
            # Target hit — sell half, move stop to breakeven
            _log.info("Heartbeat: target hit for %s @ $%.2f — partial exit", sym, price)
            self._log_step(f"Target hit: {sym} @ ${price:.2f} (target ${meta.target_price:.2f}) — partial exit queued", "trade")
            if not config.get("dry_run", True):
                self._execute_partial_exit(sym, meta, price)

        # News surge detection for open positions
        self._check_position_news(meta_all)
        self._write_agent_status()

        # Options exits and covered call overlay
        if config.get("options_enabled", False) and self._options_executor:
            try:
                self._check_options_exits(config)
            except Exception as exc:
                _log.warning("Options exits check error: %s", exc)
            try:
                self._evaluate_covered_call_overlay(config, nav)
            except Exception as exc:
                _log.warning("Covered call overlay error: %s", exc)

        # IV snapshot — builds history for IVR computation (Phase 2 options)
        try:
            from core.iv_tracker import get_current_iv, update_iv_snapshot
            for sym in list(meta_all.keys()):
                iv = get_current_iv(sym)
                if iv:
                    update_iv_snapshot(sym, iv)
        except Exception as exc:
            _log.debug("IV snapshot error: %s", exc)

        # Outcome tracking — check debate calls from N days ago
        try:
            from core.history_store import check_and_log_outcomes
            for _horizon in (5, 10, 20):
                check_and_log_outcomes(days_back=_horizon)
        except Exception as exc:
            _log.debug("Outcome tracking error: %s", exc)

    # ── Market intelligence helpers ───────────────────────────────────────────

    _NEWS_SURGE_THRESHOLD = 3      # articles in the last hour to trigger alert
    _NEWS_COOLDOWN_SECS   = 7200   # 2 hours between re-alerts per symbol

    def _check_position_news(self, meta_all: dict) -> None:
        """
        Scan recent news for each open position. If 3+ articles published in
        the last hour, emit a status alert so the user can run /aiscan.
        Uses a per-symbol 2-hour cooldown to avoid repeated alerts.
        """
        if not meta_all:
            return
        import yfinance as yf
        cutoff_ts = time.time() - 3600  # epoch timestamp 1 hour ago
        for sym in list(meta_all.keys()):
            last_alert = self._news_alert_cooldown.get(sym, 0)
            if time.time() - last_alert < self._NEWS_COOLDOWN_SECS:
                continue
            try:
                news = yf.Ticker(sym).news or []
                recent = [n for n in news
                          if n.get("providerPublishTime", 0) >= cutoff_ts]
                if len(recent) >= self._NEWS_SURGE_THRESHOLD:
                    titles = "; ".join(n.get("title", "")[:60] for n in recent[:3])
                    _log.warning("NEWS SURGE %s: %d articles in last hour — %s",
                                 sym, len(recent), titles)
                    self._log_step(f"News surge: {sym} ({len(recent)} articles in 1h) — /aiscan recommended", "warn")
                    self.agent_status.emit(
                        f"News surge on {sym} ({len(recent)} articles) — consider /aiscan {sym}"
                    )
                    self._news_alert_cooldown[sym] = time.time()
            except Exception:
                pass

    # ── Trade execution helpers ────────────────────────────────────────────────

    def _execute_entry(
        self, symbol: str, dollars: float, current_price: Optional[float],
        scan_result, ai_rank: Optional[int], thesis: str,
        target_price: Optional[float], stop_loss_pct: float,
        trailing_stop_pct: float, cycle_id: str,
    ) -> bool:
        order_id, status = self._executor.buy(symbol, dollars)
        if order_id is None:
            _log.error("Entry failed for %s: %s", symbol, status)
            return False

        fill_price = self._executor.get_filled_price(order_id, max_wait_secs=15) or current_price or 0
        fill_qty   = self._executor.get_filled_qty(order_id) or round(dollars / fill_price, 4) if fill_price else 0

        meta = build_entry_meta(
            symbol=symbol,
            entry_price=fill_price,
            scan_score=scan_result.total_score,
            ai_rank=ai_rank,
            thesis=thesis,
            stop_loss_pct=stop_loss_pct,
            trailing_stop_pct=trailing_stop_pct,
            target_price=target_price,
            sector=scan_result.sector,
        )
        save_position_meta(meta)

        from core.ticker_memory import store_buy
        store_buy(symbol, fill_price, thesis)

        log_fill(symbol=symbol, side="BUY", shares=fill_qty, fill_price=fill_price,
                 order_id=order_id, stop_loss=meta.stop_loss_price,
                 target=target_price, thesis=thesis, cycle_id=cycle_id)

        msg = {
            "action":     "BUY",
            "symbol":     symbol,
            "shares":     fill_qty,
            "fill_price": fill_price,
            "dollars":    round(fill_qty * fill_price, 2),
            "stop":       meta.stop_loss_price,
            "target":     target_price,
            "thesis":     thesis[:120],
        }
        self.trade_executed.emit(msg)
        _log.info("BUY FILLED %s × %.2f @ $%.2f (stop $%.2f)", symbol, fill_qty, fill_price, meta.stop_loss_price)
        return True

    def _execute_exit(self, symbol: str, reason: str, meta, cycle_id: Optional[str]) -> None:
        order_id, status = self._executor.sell(symbol)
        if order_id is None:
            _log.error("Exit failed for %s: %s", symbol, status)
            return

        fill_price = self._executor.get_filled_price(order_id, max_wait_secs=15) or meta.entry_price
        fill_qty   = self._executor.get_filled_qty(order_id) or 0
        realized   = round((fill_price - meta.entry_price) * fill_qty, 2)

        log_fill(symbol=symbol, side="SELL", shares=fill_qty, fill_price=fill_price,
                 order_id=order_id, realized_pnl=realized, exit_reason=reason,
                 cycle_id=cycle_id)
        delete_position_meta(symbol)

        from core.ticker_memory import finalize_outcome
        finalize_outcome(symbol, fill_price, reason)

        msg = {
            "action":       "SELL",
            "symbol":       symbol,
            "shares":       fill_qty,
            "fill_price":   fill_price,
            "realized_pnl": realized,
            "realized_pct": round(realized / (meta.entry_price * fill_qty) * 100, 2) if meta.entry_price and fill_qty else 0,
            "reason":       reason,
        }
        self.trade_executed.emit(msg)
        _log.info("SELL FILLED %s × %.2f @ $%.2f P&L=$%.2f (%s)",
                  symbol, fill_qty, fill_price, realized, reason)

    def _execute_partial_exit(self, symbol: str, meta, current_price: float) -> None:
        """Sell ~half the position when target is hit; move stop to breakeven."""
        try:
            alpaca_positions = self._executor.get_positions()
        except Exception:
            return
        pos = next((p for p in alpaca_positions if p["symbol"] == symbol), None)
        if pos is None:
            return

        half_qty = round(pos["qty"] / 2, 0)
        if half_qty < 1:
            return

        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        try:
            from alpaca.trading.client import TradingClient
            req = MarketOrderRequest(
                symbol=symbol, qty=half_qty,
                side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
            )
            order = self._executor._client.submit_order(req)
            fill_price = self._executor.get_filled_price(str(order.id), 15) or current_price
            realized   = round((fill_price - meta.entry_price) * half_qty, 2)

            # Move stop to breakeven on remaining half
            from core.portfolio import _load_meta_raw, _save_meta_raw
            raw = _load_meta_raw()
            if symbol in raw:
                raw[symbol]["stop_loss_price"] = meta.entry_price
                _save_meta_raw(raw)

            log_fill(symbol=symbol, side="SELL", shares=half_qty, fill_price=fill_price,
                     order_id=str(order.id), realized_pnl=realized,
                     exit_reason="target hit — partial exit, stop moved to breakeven")

            msg = {
                "action":       "SELL",
                "symbol":       symbol,
                "shares":       half_qty,
                "fill_price":   fill_price,
                "realized_pnl": realized,
                "reason":       f"target ${meta.target_price:.2f} hit — sold half, stop → breakeven",
            }
            self.trade_executed.emit(msg)
        except Exception as exc:
            _log.error("Partial exit failed for %s: %s", symbol, exc)

    # ── Utilities ──────────────────────────────────────────────────────────────

    def _init_executor(self):
        try:
            from core.trade_executor import TradeExecutor
            settings = self._get_settings()
            executor = TradeExecutor(settings)
            return executor
        except Exception as exc:
            _log.error("TraderAgent: could not initialize executor: %s", exc)
            self.agent_status.emit(f"Trader: init failed — {exc}")
            return None

    def _load_ranking_cache(self) -> Optional[dict]:
        if not os.path.exists(_RANKING_CACHE):
            return None
        try:
            with open(_RANKING_CACHE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def _build_thesis(self, symbol: str, ai_research: Optional[dict], ranked_item: dict) -> str:
        parts = []
        if ai_research:
            short = ai_research.get("short_term", "")
            cats  = ai_research.get("catalysts", "")
            if short:
                parts.append(short[:100])
            if cats:
                parts.append(f"Catalysts: {cats[:80]}")
        play = ranked_item.get("stock_play", "")
        if play:
            parts.append(play[:80])
        return " | ".join(parts) if parts else f"{symbol} ranked #{ranked_item.get('rank', '?')}"

    @staticmethod
    def _days_to_earnings(symbol: str) -> Optional[int]:
        """Return days until next earnings, or None if unavailable."""
        try:
            import yfinance as yf
            from datetime import date, timezone
            cal = yf.Ticker(symbol).calendar
            if cal is None:
                return None
            # calendar can be a dict or DataFrame depending on yfinance version
            if hasattr(cal, "to_dict"):
                cal = cal.to_dict()
            dates = cal.get("Earnings Date") or cal.get("earnings_date") or []
            if not dates:
                return None
            today = date.today()
            future = []
            for d in (dates if isinstance(dates, list) else [dates]):
                if hasattr(d, "date"):
                    d = d.date()
                elif hasattr(d, "to_pydatetime"):
                    d = d.to_pydatetime().date()
                if isinstance(d, datetime):
                    d = d.date()
                if d >= today:
                    future.append((d - today).days)
            return min(future) if future else None
        except Exception:
            return None

    # ── Options layer ──────────────────────────────────────────────────────────

    def _init_options_executor(self):
        """Wrap the existing Alpaca client with an OptionsExecutor."""
        if self._executor is None:
            return None
        try:
            from core.options_executor import OptionsExecutor
            opts = OptionsExecutor(self._executor._client)
            level = opts.detect_options_level()
            self._log_step(f"Options executor ready — approval level {level}")
            return opts
        except Exception as exc:
            _log.warning("OptionsExecutor init failed: %s", exc)
            return None

    def _process_options_scan(self, results: list) -> None:
        """
        Options entry logic — runs after _process_scan() on the same scan batch.
        Evaluates the same ranked candidates through the options strategy router.
        """
        config = load_trader_config()
        if not config.get("options_enabled", False):
            return
        if not config.get("enabled", False):
            return
        if not market_clock.is_rth():
            return
        if self._options_executor is None:
            return

        options_level = config.get("options_approval_level") or 0
        if options_level < 2:
            _log.debug("Options: approval level %d < 2 — skipping", options_level)
            return

        import core.iv_tracker as _iv_mod
        from core.iv_tracker import get_current_iv, update_iv_snapshot
        from core.market_regime import detect_regime
        from core.options_risk_manager import (
            compute_ivr_or_proxy, classify_iv, select_strategy,
            options_budget_remaining, size_options_trade,
        )
        from core.options_strategy import build_options_play
        from core.options_portfolio import save_option_meta, OptionPositionMeta, OptionLeg
        from core.trade_journal import log_options_fill
        import uuid, yfinance as yf

        ranking = self._load_ranking_cache()
        ranked_list = ranking.get("ranked", []) if ranking else []
        if not ranked_list:
            return

        scan_by_symbol = {r.symbol: r for r in results if hasattr(r, "symbol")}

        try:
            vix_val  = float(yf.Ticker("^VIX").fast_info.last_price or 20.0)
            spy_hist = yf.Ticker("SPY").history(period="1y", interval="1d")
        except Exception:
            vix_val, spy_hist = 20.0, None

        from core.market_regime import detect_regime, format_regime_log
        regime = detect_regime(spy_hist, vix_val)

        try:
            account = self._executor.get_account()
            nav = account["equity"]
        except Exception:
            return

        budget = options_budget_remaining(nav, config)
        if budget <= 0:
            self._log_step("Options: capital cap reached — no new options entries", "skip")
            return

        cycle_id = str(uuid.uuid4())[:8]
        entries_made = 0
        max_options_per_cycle = 2  # cap to avoid overloading on a single scan

        for ranked_item in ranked_list[:regime.max_candidates]:
            if entries_made >= max_options_per_cycle:
                break
            if not self._running:
                break

            sym = ranked_item.get("symbol", "").upper()
            if not sym:
                continue

            scan_result = scan_by_symbol.get(sym)
            if scan_result is None:
                continue

            ai_research = get_cached_entry_merged(sym)
            ai_rank     = ranked_item.get("rank")
            alloc_pct   = ranked_item.get("allocation_pct")

            # TradingAgents veto: Sell/Underweight blocks options entry regardless of technicals.
            if ai_research and ai_research.get("source") == "tradingagents":
                ta_rating = ai_research.get("ta_rating", "")
                if ta_rating in ("Sell", "Underweight"):
                    self._log_step(
                        f"OPTIONS {sym}: VETOED by TradingAgents ({ta_rating}) — skipping", "skip"
                    )
                    continue

            conviction, _ = compute_conviction_score(scan_result, ai_rank, ai_research, alloc_pct)

            ivr = compute_ivr_or_proxy(sym, scan_result, _iv_mod)
            iv_level = classify_iv(ivr, config)

            strategy_type = select_strategy(
                regime=regime.label,
                iv_level=iv_level,
                conviction=conviction,
                options_level=options_level,
            )
            if strategy_type is None:
                continue

            # Estimate max loss per contract for sizing (use premium * 100 * 1 contract as proxy)
            # Real max_loss comes from build_options_play after chain lookup
            contracts, max_loss_est = size_options_trade(
                conviction=conviction,
                nav=nav,
                max_loss_per_contract=100.0,  # $100 placeholder; refined after play is built
                budget_remaining=budget,
                config=config,
            )
            if contracts < 1:
                continue

            play = build_options_play(sym, strategy_type, scan_result, contracts)
            if play is None:
                self._log_step(f"OPTIONS {sym}: could not build {strategy_type} play — no chain data", "skip")
                continue

            # Refine contracts with actual max_loss from play
            actual_max_loss_per_contract = play["max_loss"] / max(contracts, 1)
            if actual_max_loss_per_contract > 0:
                contracts, _ = size_options_trade(
                    conviction=conviction,
                    nav=nav,
                    max_loss_per_contract=actual_max_loss_per_contract,
                    budget_remaining=budget,
                    config=config,
                )
            if contracts < 1:
                continue

            # Rebuild play with corrected contract count
            play = build_options_play(sym, strategy_type, scan_result, contracts)
            if play is None:
                continue

            play["ivr_at_entry"] = ivr

            ivr_str = f"{ivr:.0f}" if ivr is not None else "N/A"
            self._log_step(
                f"OPTIONS {sym}: {strategy_type} | IVR={ivr_str} conviction={conviction:.1f} | {play['thesis'][:80]}"
            )

            # Options debate — same Claude gate as stock trades
            from core.trade_debate import run_options_debate
            self._log_step(f"OPTIONS {sym}: sending to Claude debate ({regime.label} regime)...")
            opt_ok, opt_verdict = run_options_debate(
                symbol=sym,
                scan_result=scan_result,
                ai_research=ai_research,
                play=play,
                conviction=conviction,
                ivr=ivr,
                regime=regime.label,
                settings=settings,
            )
            if not opt_ok:
                self._log_step(f"OPTIONS {sym}: debate SKIP -- {opt_verdict[:80]}", "skip")
                continue
            self._log_step(f"OPTIONS {sym}: debate BUY -- {opt_verdict[:80]}")

            if config.get("dry_run", True):
                self._log_step(f"DRY-RUN OPTIONS {sym} {strategy_type} — no order placed", "ok")
                entries_made += 1
                continue

            order_ids, exec_err = self._execute_options_entry(play, config, cycle_id)
            if not order_ids:
                self._log_step(f"OPTIONS ORDER FAILED {sym} {strategy_type}: {exec_err}", "error")
                continue

            # Persist metadata
            position_id = str(uuid.uuid4())[:8]
            legs = [OptionLeg(**leg) for leg in play["legs"]]
            meta = OptionPositionMeta(
                position_id=position_id,
                symbol=sym,
                strategy_type=strategy_type,
                legs=legs,
                entry_premium=play["entry_premium_estimate"],
                capital_deployed=play["capital_deployed_estimate"],
                max_loss=play["max_loss"],
                target_premium=play["entry_premium_estimate"] * (1 + config.get("options_profit_take_pct", 0.50)),
                stop_premium=play["entry_premium_estimate"] * (1 - config.get("options_stop_loss_pct", 0.50)),
                thesis=play["thesis"],
                ivr_at_entry=ivr,
                underlying_price_at_entry=play["underlying_price"],
                underlying_stop_loss=play.get("underlying_stop_loss"),
                scan_score_at_entry=scan_result.total_score,
                opened_at=datetime.now().isoformat(),
            )
            save_option_meta(meta)
            log_options_fill(
                position_id=position_id, symbol=sym,
                strategy_type=strategy_type, side="OPEN",
                contracts=contracts, fill_price=play["entry_premium_estimate"],
                order_id=order_ids[0] if order_ids else "dry-run",
                thesis=play["thesis"], cycle_id=cycle_id,
            )
            budget -= play["capital_deployed_estimate"]
            entries_made += 1
            self._log_step(f"OPTIONS BUY {sym} {strategy_type} placed (conviction {conviction:.1f})", "trade")

    def _execute_options_entry(self, play: dict, config: dict, cycle_id: str) -> tuple:
        """Submit orders for all legs of an options play. Returns (order_ids, error_str)."""
        order_ids = []
        errors    = []
        legs = play.get("legs", [])
        strategy_type = play.get("strategy_type", "")

        # Multi-leg strategies (spreads, condors) — use spread order if Level 3
        if len(legs) > 1 and config.get("options_approval_level", 2) >= 3:
            spread_legs = []
            for leg in legs:
                spread_legs.append({
                    "contract_symbol": leg["contract_symbol"],
                    "side": "buy" if leg["side"] == "long" else "sell",
                    "qty": leg["contracts"],
                })
            limit = abs(play.get("entry_premium_estimate", 0.01))
            oid, status = self._options_executor.submit_spread(spread_legs, limit_price=limit)
            if oid:
                order_ids.append(oid)
            else:
                errors.append(f"spread submit failed: {status}")
        else:
            # Submit each leg individually
            for leg in legs:
                price_estimate = abs(play.get("entry_premium_estimate", 0.01))
                if leg["side"] == "long":
                    oid, status = self._options_executor.buy_option(
                        leg["contract_symbol"], leg["contracts"], price_estimate
                    )
                else:
                    oid, status = self._options_executor.sell_option(
                        leg["contract_symbol"], leg["contracts"], price_estimate
                    )
                if oid:
                    order_ids.append(oid)
                else:
                    errors.append(f"{leg.get('side','?')} leg {leg.get('contract_symbol','?')} failed: {status}")

        return order_ids, "; ".join(errors) if errors else "ok"

    def _execute_options_exit(self, meta, reason: str, cycle_id: Optional[str]) -> None:
        """Close all legs of an options position."""
        from core.options_portfolio import delete_option_meta
        from core.trade_journal import log_options_fill

        # Compute net current value with the same sign convention as entry_premium:
        # long legs are positive, short legs are negative.
        net_current = 0.0
        last_close_price = 0.0
        for leg in meta.legs:
            current_price = self._options_executor.get_option_current_price(leg.contract_symbol) or 0.0
            sign = 1 if leg.side == "long" else -1
            net_current += current_price * sign
            last_close_price = current_price
            self._options_executor.close_option_position(leg.contract_symbol, leg.contracts)

        contracts_per_leg = meta.legs[0].contracts if meta.legs else 1
        total_pnl = (net_current - meta.entry_premium) * 100 * contracts_per_leg

        log_options_fill(
            position_id=meta.position_id, symbol=meta.symbol,
            strategy_type=meta.strategy_type, side="CLOSE",
            contracts=sum(leg.contracts for leg in meta.legs),
            fill_price=last_close_price,
            order_id="close",
            realized_pnl=round(total_pnl, 2),
            exit_reason=reason, cycle_id=cycle_id,
        )
        delete_option_meta(meta.position_id)

        msg = {
            "action":       "OPTIONS_CLOSE",
            "symbol":       meta.symbol,
            "strategy":     meta.strategy_type,
            "realized_pnl": round(total_pnl, 2),
            "reason":       reason,
        }
        self.trade_executed.emit(msg)
        _log.info("OPTIONS CLOSE %s %s P&L=$%.2f (%s)",
                  meta.symbol, meta.strategy_type, total_pnl, reason)

    def _check_options_exits(self, config: dict) -> None:
        """Check all open options positions for exit conditions (heartbeat)."""
        from core.options_portfolio import load_all_option_meta
        from core.options_risk_manager import check_options_exit
        from core.iv_tracker import get_ivr
        from core.options_strategy import days_to_expiry

        all_meta = load_all_option_meta()
        if not all_meta:
            return

        try:
            alpaca_opts = self._options_executor.get_options_positions()
        except Exception:
            alpaca_opts = []

        price_by_contract = {p["contract_symbol"]: p.get("current_price") for p in alpaca_opts}

        import yfinance as yf
        for pid, meta in list(all_meta.items()):
            dte = days_to_expiry(meta.legs[0].expiration if meta.legs else "2099-01-01")
            current_premium = None
            for leg in meta.legs:
                cp = price_by_contract.get(leg.contract_symbol)
                if cp:
                    current_premium = cp
                    break

            try:
                underlying_price = float(yf.Ticker(meta.symbol).fast_info.last_price or 0) or None
            except Exception:
                underlying_price = None

            current_ivr = get_ivr(meta.symbol)

            should_exit, exit_reason = check_options_exit(
                meta=meta,
                current_premium=current_premium,
                underlying_price=underlying_price,
                days_to_expiry=dte,
                current_ivr=current_ivr,
                config=config,
            )
            if should_exit:
                self._log_step(f"OPTIONS EXIT {meta.symbol} {meta.strategy_type}: {exit_reason}", "trade")
                if not config.get("dry_run", True):
                    self._execute_options_exit(meta, exit_reason, cycle_id=None)
                else:
                    _log.info("DRY-RUN OPTIONS EXIT %s — %s", meta.symbol, exit_reason)

    def _evaluate_covered_call_overlay(self, config: dict, nav: float) -> None:
        """
        For existing stock positions held > N days with unrealized gain > threshold,
        sell a 30-delta covered call at 14–21 DTE to collect premium.
        """
        min_hold_days = config.get("options_covered_call_min_hold_days", 7)
        min_gain_pct  = config.get("options_covered_call_min_gain_pct", 0.05)
        options_level = config.get("options_approval_level", 0) or 0
        if options_level < 2:
            return

        from core.options_risk_manager import options_budget_remaining
        budget = options_budget_remaining(nav, config)
        if budget <= 0:
            return

        from core.options_strategy import build_options_play
        from core.options_portfolio import save_option_meta, OptionPositionMeta, OptionLeg, get_options_for_symbol
        from core.trade_journal import log_options_fill
        import uuid

        meta_all = load_all_meta()

        try:
            alpaca_positions = self._executor.get_positions()
        except Exception:
            return

        for p in alpaca_positions:
            sym = p["symbol"]
            meta = meta_all.get(sym)
            if meta is None:
                continue

            # Check hold period
            opened = datetime.fromisoformat(meta.opened_at)
            days_held = max(0, (datetime.now() - opened).days)
            if days_held < min_hold_days:
                continue

            # Check unrealized gain
            entry = meta.entry_price
            current = p.get("current_price") or entry
            if entry <= 0 or (current - entry) / entry < min_gain_pct:
                continue

            # Already have a covered call on this position?
            existing = get_options_for_symbol(sym)
            if any(m.strategy_type == "covered_call" for m in existing):
                continue

            qty = p.get("qty", 0)
            if qty < 100:
                continue

            contracts = int(qty // 100)
            play = build_options_play(sym, "covered_call", type("sr", (), {
                "total_score": meta.scan_score_at_entry,
                "volatility_20d": None,
            })(), contracts)

            if play is None:
                continue

            self._log_step(
                f"COVERED CALL overlay: {sym} held {days_held}d gain "
                f"{(current-entry)/entry*100:.1f}% — {play['thesis'][:80]}",
                "trade" if not config.get("dry_run", True) else "ok",
            )

            if config.get("dry_run", True):
                continue

            oids, cc_err = self._execute_options_entry(play, config, cycle_id="cc")
            if not oids:
                self._log_step(f"COVERED CALL ORDER FAILED {sym}: {cc_err}", "error")
                continue

            position_id = str(uuid.uuid4())[:8]
            legs = [OptionLeg(**leg) for leg in play["legs"]]
            cc_meta = OptionPositionMeta(
                position_id=position_id,
                symbol=sym,
                strategy_type="covered_call",
                legs=legs,
                entry_premium=play["entry_premium_estimate"],
                capital_deployed=0.0,
                max_loss=0.0,
                target_premium=play["entry_premium_estimate"] * (1 + config.get("options_profit_take_pct", 0.50)),
                stop_premium=0.0,
                thesis=play["thesis"],
                ivr_at_entry=None,
                underlying_price_at_entry=current,
                underlying_stop_loss=meta.stop_loss_price,
                scan_score_at_entry=meta.scan_score_at_entry,
                opened_at=datetime.now().isoformat(),
            )
            save_option_meta(cc_meta)
            log_options_fill(
                position_id=position_id, symbol=sym,
                strategy_type="covered_call", side="OPEN",
                contracts=contracts, fill_price=play["entry_premium_estimate"],
                order_id=oids[0], thesis=play["thesis"],
            )

    @staticmethod
    def _parse_target(stock_play: str, current_price: Optional[float]) -> Optional[float]:
        """Extract target price from ClaudeRankingAnalyst stock_play string like 'Entry: $X | Target: $Y | Stop: $Z'."""
        import re
        m = re.search(r"[Tt]arget[:\s]*\$?([\d.]+)", stock_play)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        if current_price:
            return round(current_price * 1.15, 2)  # 15% default target
        return None
