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
from core.ai_research_store import get_cached_entry
from core.portfolio import (
    build_entry_meta, delete_position_meta, get_position_meta,
    init_trader_config, load_all_meta, load_trader_config,
    save_position_meta, save_trader_config, update_high_water,
)
from core.risk_manager import (
    check_circuit_breaker, check_exit, check_hard_gates,
    compute_decision_score, compute_sector_exposure, size_position,
)
from core.trade_journal import log_decision, log_fill
from core.performance_tracker import maybe_snapshot
from core.self_tuner import run_tuning_cycle, should_tune

_log = logging.getLogger(__name__)

_RANKING_CACHE = os.path.join(os.path.dirname(__file__), "..", "data", "claude_ranking_cache.json")
_HEARTBEAT_INTERVAL = 900   # 15 minutes


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
            return

        # Seed all-time-high from portfolio history
        try:
            self._ath_nav = self._executor.get_all_time_high_nav()
        except Exception:
            pass

        self.agent_status.emit("Trader: running (dry-run)" if load_trader_config().get("dry_run") else "Trader: LIVE paper")
        _log.info("TraderAgent: executor ready, ATH NAV=%.2f", self._ath_nav)

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
        cycle_id = str(uuid.uuid4())[:8]

        # VIX gate — halt new entries if market fear is elevated
        vix_threshold = config.get("vix_halt_threshold", 30.0)
        try:
            import yfinance as yf
            vix = yf.Ticker("^VIX").fast_info.last_price
            if vix and float(vix) > vix_threshold:
                _log.info("TraderAgent: VIX %.1f > %.0f threshold — skipping entries this cycle",
                          float(vix), vix_threshold)
                self.agent_status.emit(f"Trader: VIX {float(vix):.1f} > {vix_threshold:.0f} — entries paused")
                return
        except Exception:
            pass  # if VIX fetch fails, proceed normally

        # SPY regime — halve position sizes when SPY is below its 200-DMA
        spy_regime_factor = 1.0
        try:
            import yfinance as yf
            spy_hist = yf.Ticker("SPY").history(period="1y", interval="1d")
            if len(spy_hist) >= 200:
                sma200    = float(spy_hist["Close"].tail(200).mean())
                spy_price = float(spy_hist["Close"].iloc[-1])
                if spy_price < sma200:
                    spy_regime_factor = 0.5
                    _log.info("TraderAgent: SPY $%.2f < 200-DMA $%.2f — half-size mode",
                              spy_price, sma200)
                    self.agent_status.emit(
                        f"Trader: SPY ${spy_price:.0f} < 200-DMA ${sma200:.0f} — half-size entries"
                    )
        except Exception:
            pass

        try:
            account = self._executor.get_account()
        except Exception as exc:
            _log.error("TraderAgent: could not fetch account: %s", exc)
            return

        nav  = account["equity"]
        cash = account["cash"]

        # Circuit breaker check
        intraday_pnl = account["day_pnl"]
        halted, halt_reason = check_circuit_breaker(nav, self._ath_nav, intraday_pnl, config)
        if halted:
            msg = f"TraderAgent HALTED: {halt_reason}"
            _log.warning(msg)
            self.agent_halted.emit(msg)
            self.agent_status.emit(f"Trader: HALTED — {halt_reason}")
            if nav > self._ath_nav:
                self._ath_nav = nav
            return

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

        # Build a lookup of scan results by symbol
        scan_by_symbol = {r.symbol: r for r in results if hasattr(r, "symbol")}

        # Evaluate ranked candidates (top 5 only — they have allocation_pct)
        ranked = ranking.get("ranked", []) if ranking else []
        entries_made = 0

        for ranked_item in ranked[:5]:
            if not self._running:
                break

            sym = ranked_item.get("symbol", "").upper()
            if not sym or sym in open_symbols:
                continue

            scan_result = scan_by_symbol.get(sym)
            if scan_result is None:
                _log.debug("TraderAgent: %s ranked but not in this scan batch — skipped", sym)
                continue

            # Earnings gate — block entry within N days of earnings
            block_days = config.get("earnings_block_days", 3)
            dte = self._days_to_earnings(sym)
            if dte is not None and dte <= block_days:
                _log.info("TraderAgent: %s skipped — earnings in %d day(s)", sym, dte)
                log_decision(sym, "REJECT", f"earnings in {dte}d (block_days={block_days})",
                             scan_score=scan_result.total_score, nav_at_eval=nav, cycle_id=cycle_id)
                continue

            ai_research  = get_cached_entry(sym)
            ai_rank      = ranked_item.get("rank")
            alloc_pct    = ranked_item.get("allocation_pct")

            # Parse entry/stop/target from ranked_item's stock_play string
            target_price = self._parse_target(ranked_item.get("stock_play", ""), scan_result.price)
            stop_pct     = config.get("default_stop_loss_pct", 0.08)
            trail_pct    = config.get("default_trailing_stop_pct", 0.12)

            # 1. Hard gates
            passed, reject_reason = check_hard_gates(
                symbol=sym,
                scan_result=scan_result,
                ai_research=ai_research,
                open_symbols=open_symbols,
                sector_exposure=sector_exposure,
                cash=cash,
                nav=nav,
                config=config,
            )
            if not passed:
                log_decision(sym, "REJECT", reject_reason,
                             scan_score=scan_result.total_score,
                             ai_rank=ai_rank, nav_at_eval=nav, cycle_id=cycle_id)
                continue

            # 2. Decision score
            dec_score = compute_decision_score(scan_result, ai_rank, ai_research, alloc_pct)
            min_score = config.get("min_decision_score", 70.0)
            if dec_score < min_score:
                log_decision(sym, "REJECT", f"decision score {dec_score:.1f} < {min_score}",
                             scan_score=scan_result.total_score, decision_score=dec_score,
                             ai_rank=ai_rank, nav_at_eval=nav, cycle_id=cycle_id)
                continue

            # 3. Size position
            dollars = size_position(
                decision_score=dec_score,
                allocation_pct=alloc_pct,
                volatility=scan_result.volatility_20d,
                nav=nav,
                cash=cash,
                config=config,
            )
            if dollars <= 0:
                log_decision(sym, "REJECT", "insufficient cash after reserve for minimum size",
                             scan_score=scan_result.total_score, decision_score=dec_score,
                             ai_rank=ai_rank, nav_at_eval=nav, cycle_id=cycle_id)
                continue

            if spy_regime_factor < 1.0:
                dollars = round(dollars * spy_regime_factor, 2)

            thesis = self._build_thesis(sym, ai_research, ranked_item)
            log_decision(sym, "BUY", "all gates passed",
                         scan_score=scan_result.total_score, decision_score=dec_score,
                         ai_rank=ai_rank, size_dollars=dollars, nav_at_eval=nav,
                         ai_sentiment=ai_research.get("sentiment") if ai_research else None,
                         cycle_id=cycle_id)

            if config.get("dry_run", True):
                _log.info("DRY-RUN BUY %s $%.0f (score=%.1f) — no order placed", sym, dollars, dec_score)
                self.agent_status.emit(f"Trader: dry-run BUY {sym} ${dollars:.0f}")
                continue

            # 4. Execute
            success = self._execute_entry(
                symbol=sym, dollars=dollars, current_price=scan_result.price,
                scan_result=scan_result, ai_rank=ai_rank, thesis=thesis,
                target_price=target_price, stop_loss_pct=stop_pct,
                trailing_stop_pct=trail_pct, cycle_id=cycle_id,
            )
            if success:
                open_symbols.append(sym)
                cash -= dollars
                entries_made += 1

        self.agent_status.emit(
            f"Trader: scan cycle done — {entries_made} entr{'y' if entries_made == 1 else 'ies'} placed"
        )

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
        self._last_snapshot_date = maybe_snapshot(self._executor, self._last_snapshot_date)

        # Self-tuner: check if enough new trades have accumulated
        try:
            from core.trade_journal import read_journal
            fills = [e for e in read_journal(last_n=2000)
                     if e.get("type") == "fill" and e.get("side") == "SELL"]
            if should_tune(self._last_tune_count, len(fills)):
                self.agent_status.emit("Trader: self-tuning parameters…")
                self._last_tune_count = run_tuning_cycle(self._last_tune_count)
        except Exception as exc:
            _log.warning("SelfTuner error: %s", exc)

        if not config.get("enabled", False):
            return
        if not market_clock.is_rth():
            return

        try:
            account = self._executor.get_account()
            nav     = account["equity"]
            if nav > self._ath_nav:
                self._ath_nav = nav
        except Exception:
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
            if not config.get("dry_run", True):
                self._execute_partial_exit(sym, meta, price)

        # News surge detection for open positions
        self._check_position_news(meta_all)

        # IV snapshot — builds history for IVR computation (Phase 2 options)
        try:
            from core.iv_tracker import get_current_iv, update_iv_snapshot
            for sym in list(meta_all.keys()):
                iv = get_current_iv(sym)
                if iv:
                    update_iv_snapshot(sym, iv)
        except Exception as exc:
            _log.debug("IV snapshot error: %s", exc)

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
