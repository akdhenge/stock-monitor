"""
debit_spread_trader.py — orchestrates the options_debit_spread sleeve.

No auto-execution: approve_spread() and close_spread() are the ONLY functions
in this feature allowed to call OptionsExecutor.submit_spread() /
close_option_position(), and both are only ever invoked from an explicit
Telegram /approvespread or /closespread command — never from the proposal
or exit-check paths below, and never from a scan loop.

Cadence: propose_entries() / check_exits() are called once per calendar day,
right after the headless drawdown screen completes (see
core/daily_drawdown_runner.py → TraderAgent.queue_propose_spreads), not on
every 5-15 minute trader scan tick. A daily cadence combined with the
pending-proposal dedup in propose_entries() is what keeps Telegram from being
spammed with the same still-undecided candidate.

All Alpaca-touching calls here (executor.get_positions/get_account,
options_executor.*) are expected to run on TraderAgent's own QThread, which
already owns a live, single-owner Alpaca client — see trader_agent.py's
_process_propose_spreads / _process_approve_spread / _process_close_spread.
"""
import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional

from core.debit_spread_proposals_store import (
    SpreadProposal,
    delete as delete_proposal,
    get as get_proposal,
    load_all as load_all_proposals,
    pending_entries,
    save as save_proposal,
)
from core.drawdown_results_store import load_drawdown_results
from core.drawdown_spread_builder import build_spread
from core.capital_ledger import sleeve_budget_remaining
from core.options_portfolio import (
    OptionLeg,
    OptionPositionMeta,
    delete_option_meta,
    get_options_for_symbol,
    load_all_option_meta,
    save_option_meta,
)
from core.trade_journal import log_options_decision, log_options_fill

_log = logging.getLogger(__name__)

_PROPOSAL_TTL_DAYS = 7  # a stale, un-acted-on proposal is dropped; may resurface on a later scan

TelegramSend = Callable[[str], None]


# ── Entry proposals ───────────────────────────────────────────────────────────

def propose_entries(
    executor,
    settings: dict,
    config: dict,
    telegram_send: TelegramSend,
) -> List[SpreadProposal]:
    """Screen → filter → size → propose. Never calls submit_spread()."""
    results = load_drawdown_results()
    if not results:
        _log.info("debit_spread_trader: no cached drawdown results — skipping proposal cycle")
        return []

    passed = [r for r in results if r.failed_gate is None and r.score > 0]
    # Sort by confidence_score (the new, tier-driving signal) with a fallback to the
    # raw composite score for stale/unset (<=0) confidence_score rows so pre-redesign
    # cached results don't all collapse to the bottom of the ranking.
    passed.sort(key=lambda r: r.confidence_score if r.confidence_score > 0 else r.score, reverse=True)

    top_n = config.get("drawdown_max_candidates_per_cycle", 10)
    candidates = passed[:top_n]

    try:
        positions = executor.get_positions()
        account = executor.get_account()
    except Exception as exc:
        _log.warning("debit_spread_trader: could not fetch account/positions: %s", exc)
        return []

    nav, cash = account.get("equity", 0.0), account.get("cash", 0.0)

    held_option_symbols = {
        m.symbol.upper() for m in load_all_option_meta().values()
        if m.strategy_type == "bull_call_spread"
    }
    existing_proposal_symbols = {p.symbol for p in pending_entries()}

    block_days = config.get("earnings_block_days", 3)
    new_proposals: List[SpreadProposal] = []

    for r in candidates:
        sym = r.symbol.upper()
        if sym in held_option_symbols or sym in existing_proposal_symbols:
            continue

        dte_earn = _days_to_earnings(r.next_earnings_date)
        if dte_earn is not None and dte_earn <= block_days:
            continue

        spread = build_spread(r)
        if spread is None:
            continue

        budget = sleeve_budget_remaining("options_debit_spread", nav, cash, positions, config)
        if budget <= 0:
            _log.info("debit_spread_trader: options sleeve budget exhausted — stopping proposals")
            break

        max_loss_dollar_cap = (
            nav * config.get("options_sleeve_pct", 0.5) * config.get("debit_spread_max_loss_pct", 0.05)
        )
        per_trade_cap = min(budget, max_loss_dollar_cap)
        contracts = int(per_trade_cap // spread.max_loss_per_contract) if spread.max_loss_per_contract > 0 else 0
        if contracts < 1:
            log_options_decision(
                sym, "REJECT", "debit-spread sizing yielded 0 contracts",
                strategy_type="bull_call_spread", nav_at_eval=nav,
                extra={"drawdown_score": r.score, "sleeve": "options_debit_spread"},
            )
            continue

        proposal = SpreadProposal(
            symbol=sym, kind="entry", status="pending",
            long_strike=spread.long_strike, short_strike=spread.short_strike,
            expiration=spread.expiration,
            long_contract_symbol=spread.long_contract_symbol,
            short_contract_symbol=spread.short_contract_symbol,
            net_debit=spread.net_debit,
            max_profit_per_contract=spread.max_profit_per_contract,
            max_loss_per_contract=spread.max_loss_per_contract,
            suggested_contracts=contracts,
            drawdown_score=r.score, cause_label=r.cause_label, cause_summary=r.cause_summary,
            analyst_target=spread.analyst_target,
            underlying_price_at_proposal=spread.underlying_price,
            confidence_score=spread.confidence_score,
            horizon_label=spread.horizon_label,
        )
        save_proposal(proposal)
        new_proposals.append(proposal)

        log_options_decision(
            sym, "ENTER", "debit spread proposed — awaiting Telegram approval",
            strategy_type="bull_call_spread", scan_score=r.score, nav_at_eval=nav,
            extra={
                "drawdown_score": r.score, "sleeve": "options_debit_spread",
                "long_strike": spread.long_strike, "short_strike": spread.short_strike,
                "expiration": spread.expiration, "suggested_contracts": contracts,
            },
        )

        telegram_send(_format_entry_proposal(proposal))

    _expire_stale_proposals()
    return new_proposals


def _format_entry_proposal(p: SpreadProposal) -> str:
    otm_pct = (
        (p.long_strike - p.underlying_price_at_proposal) / p.underlying_price_at_proposal * 100
        if p.underlying_price_at_proposal else 0.0
    )
    horizon = f" ({p.horizon_label} tier)" if p.horizon_label else ""
    return (
        f"\U0001F4C9 <b>Debit Spread Candidate: {p.symbol}</b>\n"
        f"Score: {p.drawdown_score:.1f} | Confidence: {p.confidence_score:.0f}{horizon} | "
        f"Cause: {p.cause_label}\n"
        f"<i>{p.cause_summary}</i>\n\n"
        f"Long ${p.long_strike:.0f}C ({otm_pct:+.1f}% OTM) / Short ${p.short_strike:.0f}C  "
        f"exp {p.expiration}\n"
        f"Net debit: ${p.net_debit:.2f}/sh  (${p.net_debit * 100:.0f}/contract)\n"
        f"Max profit: ${p.max_profit_per_contract:.0f}/contract | "
        f"Max loss: ${p.max_loss_per_contract:.0f}/contract\n"
        f"Analyst target: ${p.analyst_target:.2f}  |  Underlying: ${p.underlying_price_at_proposal:.2f}\n"
        f"Suggested size: <b>{p.suggested_contracts} contract(s)</b>\n\n"
        f"Approve: <code>/approvespread {p.symbol}</code>"
    )


def _days_to_earnings(next_earnings_date: Optional[str]) -> Optional[int]:
    if not next_earnings_date:
        return None
    try:
        d = datetime.strptime(next_earnings_date, "%Y-%m-%d").date()
        return (d - date.today()).days
    except ValueError:
        return None


def _expire_stale_proposals() -> None:
    cutoff = datetime.now() - timedelta(days=_PROPOSAL_TTL_DAYS)
    for p in load_all_proposals().values():
        if p.status != "pending":
            continue
        try:
            proposed_at = datetime.fromisoformat(p.proposed_at)
        except ValueError:
            continue
        if proposed_at < cutoff:
            delete_proposal(p.symbol)
            _log.info("debit_spread_trader: expired stale %s proposal for %s", p.kind, p.symbol)


def _live_price(symbol: str) -> Optional[float]:
    try:
        import yfinance as yf
        px = yf.Ticker(symbol).fast_info.last_price
        return float(px) if px else None
    except Exception:
        return None


# ── Exit-side monitoring ──────────────────────────────────────────────────────

def check_exits(
    executor,
    options_executor,
    settings: dict,
    config: dict,
    telegram_send: TelegramSend,
) -> None:
    """Push a close proposal via Telegram when an open debit-spread position
    hits profit-take / target-proximity / close-DTE / max-hold-days, or push
    a review flag when a previously-passed candidate breaks down on rescreen.
    Never closes anything itself."""
    open_spreads = [m for m in load_all_option_meta().values() if m.strategy_type == "bull_call_spread"]
    if not open_spreads:
        return

    results = load_drawdown_results()
    all_by_symbol = {r.symbol.upper(): r for r in results}
    passed_symbols = {
        sym for sym, r in all_by_symbol.items() if r.failed_gate is None and r.score > 0
    }

    pending_close_symbols = {p.symbol for p in load_all_proposals().values()
                              if p.kind == "close" and p.status == "pending"}
    # Dedup the qualitative breakdown flag so it fires at most once/day per
    # symbol instead of re-sending on every daily cycle until manually closed.
    reviewed_today_symbols = {
        p.symbol for p in load_all_proposals().values()
        if p.kind == "review" and p.status == "pending"
        and p.proposed_at[:10] == date.today().isoformat()
    }

    from core.options_strategy import days_to_expiry

    close_dte_threshold = settings.get("options_close_dte", 21) if isinstance(settings, dict) else 21

    for meta in open_spreads:
        sym = meta.symbol.upper()
        if sym in pending_close_symbols:
            continue  # already awaiting the user's /closespread decision

        long_leg = next((l for l in meta.legs if l.side == "long"), None)
        short_leg = next((l for l in meta.legs if l.side == "short"), None)
        if long_leg is None or short_leg is None:
            continue

        reason = _check_exit_conditions(
            meta, long_leg, short_leg, options_executor, all_by_symbol.get(sym),
            config, close_dte_threshold, days_to_expiry,
        )

        if reason:
            proposal = SpreadProposal(
                symbol=sym, kind="close", status="pending",
                position_id=meta.position_id, close_reason=reason,
                long_strike=long_leg.strike, short_strike=short_leg.strike,
                expiration=long_leg.expiration,
                long_contract_symbol=long_leg.contract_symbol,
                short_contract_symbol=short_leg.contract_symbol,
                net_debit=meta.entry_premium,
            )
            save_proposal(proposal)
            telegram_send(
                f"\U0001F4B0 <b>Close Suggested: {sym}</b>\n{reason}\n"
                f"Close: <code>/closespread {sym}</code>"
            )
            continue

        # Qualitative breakdown check: previously passed, now fails on rescreen.
        # A hint for manual review only — never auto-closed. Deduped to once/day.
        cm = all_by_symbol.get(sym)
        if (
            cm is not None and cm.failed_gate is not None
            and sym not in passed_symbols and sym not in reviewed_today_symbols
        ):
            save_proposal(SpreadProposal(symbol=sym, kind="review", status="pending"))
            telegram_send(
                f"⚠️ <b>Review Suggested: {sym}</b>\n"
                f"Previously-passed candidate now fails <b>{cm.failed_gate}</b> on rescreen. "
                f"Position not auto-closed — please review.\n"
                f"Close if warranted: <code>/closespread {sym}</code>"
            )


def _check_exit_conditions(
    meta: OptionPositionMeta,
    long_leg: OptionLeg,
    short_leg: OptionLeg,
    options_executor,
    candidate,
    config: dict,
    close_dte_threshold: int,
    days_to_expiry_fn,
) -> Optional[str]:
    long_px = options_executor.get_option_current_price(long_leg.contract_symbol)
    short_px = options_executor.get_option_current_price(short_leg.contract_symbol)

    max_profit_per_contract = (short_leg.strike - long_leg.strike) * 100 - meta.entry_premium * 100

    if long_px is not None and short_px is not None and max_profit_per_contract > 0:
        current_spread_value = long_px - short_px
        current_profit_per_contract = (current_spread_value - meta.entry_premium) * 100
        take_pct = config.get("debit_spread_profit_take_pct", 0.70)
        if current_profit_per_contract >= max_profit_per_contract * take_pct:
            pct_of_max = current_profit_per_contract / max_profit_per_contract * 100
            return (
                f"profit target hit — ${current_profit_per_contract:.0f}/"
                f"${max_profit_per_contract:.0f} ({pct_of_max:.0f}% of max)"
            )

    if candidate is not None and candidate.current_price:
        target = candidate.current_price * (1 + candidate.analyst_upside_pct)
        underlying_now = _live_price(meta.symbol)
        if underlying_now and target > 0:
            proximity = abs(target - underlying_now) / target
            if proximity <= config.get("debit_spread_target_proximity_pct", 0.05):
                return (
                    f"underlying ${underlying_now:.2f} within {proximity * 100:.1f}% "
                    f"of consensus target ${target:.2f}"
                )

    dte = days_to_expiry_fn(long_leg.expiration)
    if dte <= close_dte_threshold:
        return f"{dte}d to expiration (close threshold {close_dte_threshold}d)"

    try:
        opened = datetime.fromisoformat(meta.opened_at)
        held_days = (datetime.now() - opened).days
        max_hold = config.get("debit_spread_max_hold_days", 545)
        if held_days >= max_hold:
            return f"held {held_days}d >= max hold {max_hold}d"
    except ValueError:
        pass

    return None


# ── Approve / close (the ONLY functions allowed to touch the broker) ──────────

def approve_spread(symbol: str, executor, options_executor, config: dict) -> str:
    """The ONLY function allowed to call submit_spread(). Returns a reply string."""
    symbol = symbol.upper()
    proposal = get_proposal(symbol)
    if proposal is None or proposal.kind != "entry" or proposal.status != "pending":
        return f"No pending debit-spread proposal for {symbol}."

    # Re-check the sleeve budget at approval time, not just at proposal time —
    # propose_entries() sizes each candidate independently against the same
    # snapshot budget, so several proposals can each look affordable alone
    # while collectively overdrawing the sleeve if the user approves more than
    # one. This is the commitment point, so it's the right place to enforce it.
    trade_max_loss = proposal.max_loss_per_contract * proposal.suggested_contracts
    try:
        positions = executor.get_positions()
        account = executor.get_account()
        budget = sleeve_budget_remaining(
            "options_debit_spread", account.get("equity", 0.0), account.get("cash", 0.0),
            positions, config,
        )
        if trade_max_loss > budget:
            return (
                f"Skipped {symbol}: sleeve budget exhausted "
                f"(trade needs ${trade_max_loss:,.0f}, ${budget:,.0f} remaining). "
                f"Not submitted."
            )
    except Exception as exc:
        _log.warning("debit_spread_trader: could not re-check sleeve budget for %s: %s", symbol, exc)
        # Fail closed only if we can't even reach the broker for account state —
        # if account/positions fetch itself is broken, submit_spread would fail too.
        return f"Approve failed for {symbol}: could not verify sleeve budget ({exc})."

    legs = [
        {"contract_symbol": proposal.long_contract_symbol, "side": "buy", "qty": proposal.suggested_contracts},
        {"contract_symbol": proposal.short_contract_symbol, "side": "sell", "qty": proposal.suggested_contracts},
    ]
    order_id, status = options_executor.submit_spread(legs, limit_price=proposal.net_debit)
    if order_id is None:
        proposal.status = "rejected"
        save_proposal(proposal)
        return f"Order failed for {symbol}: {status}"

    fill_price = options_executor.get_filled_price(order_id, max_wait_secs=10)
    spread_width = proposal.short_strike - proposal.long_strike
    if fill_price is None or not (0 < fill_price < spread_width):
        # Multi-leg parent-order filled_avg_price isn't guaranteed to be the net
        # debit per share (it can echo a single leg's price on some fill shapes).
        # Fall back to the proposed debit rather than record an implausible
        # entry_premium that would corrupt max_loss/capital_deployed and every
        # later sleeve_budget_remaining() calculation.
        if fill_price is not None:
            _log.warning(
                "debit_spread_trader: implausible fill_price %.2f for %s (spread width %.2f) "
                "— falling back to proposed net debit %.2f",
                fill_price, symbol, spread_width, proposal.net_debit,
            )
        fill_price = proposal.net_debit

    position_id = str(uuid.uuid4())[:8]
    legs_meta = [
        OptionLeg(contract_symbol=proposal.long_contract_symbol, option_type="call",
                  strike=proposal.long_strike, expiration=proposal.expiration,
                  contracts=proposal.suggested_contracts, side="long"),
        OptionLeg(contract_symbol=proposal.short_contract_symbol, option_type="call",
                  strike=proposal.short_strike, expiration=proposal.expiration,
                  contracts=proposal.suggested_contracts, side="short"),
    ]
    meta = OptionPositionMeta(
        position_id=position_id, symbol=symbol, strategy_type="bull_call_spread",
        legs=legs_meta,
        entry_premium=fill_price,
        capital_deployed=fill_price * 100 * proposal.suggested_contracts,
        max_loss=proposal.max_loss_per_contract * proposal.suggested_contracts,
        target_premium=fill_price * (1 + config.get("debit_spread_profit_take_pct", 0.70)),
        stop_premium=0.0,  # defined-risk debit spread: max loss is structural, no separate premium stop
        thesis=f"Drawdown screener [{proposal.cause_label}]: {proposal.cause_summary[:150]}",
        ivr_at_entry=None,
        underlying_price_at_entry=proposal.underlying_price_at_proposal,
        underlying_stop_loss=None,
        scan_score_at_entry=proposal.drawdown_score,
        opened_at=datetime.now().isoformat(),
    )
    save_option_meta(meta)

    log_options_fill(
        position_id=position_id, symbol=symbol, strategy_type="bull_call_spread",
        side="OPEN", contracts=proposal.suggested_contracts, fill_price=fill_price,
        order_id=order_id, thesis=meta.thesis,
        extra={"strategy": "debit_spread_screener", "drawdown_score": proposal.drawdown_score},
    )

    delete_proposal(symbol)

    return (
        f"Approved: {symbol} bull call spread "
        f"${proposal.long_strike:.0f}/${proposal.short_strike:.0f} exp {proposal.expiration} "
        f"x{proposal.suggested_contracts} @ ${fill_price:.2f} net debit. Order {order_id} ({status})."
    )


def close_spread(symbol: str, options_executor, config: dict) -> str:
    """The ONLY function allowed to close a debit-spread position."""
    symbol = symbol.upper()
    positions = [m for m in get_options_for_symbol(symbol) if m.strategy_type == "bull_call_spread"]
    if not positions:
        return f"No open debit-spread position for {symbol}."

    meta = positions[0]
    proposal = get_proposal(symbol)
    close_reason = proposal.close_reason if (proposal and proposal.kind == "close") else "manual close"

    long_leg = next((l for l in meta.legs if l.side == "long"), None)
    short_leg = next((l for l in meta.legs if l.side == "short"), None)
    contracts = long_leg.contracts if long_leg else 1

    exit_long = options_executor.get_option_current_price(long_leg.contract_symbol) if long_leg else None
    exit_short = options_executor.get_option_current_price(short_leg.contract_symbol) if short_leg else None

    # Close the SHORT leg first. If closing it fails partway through, leaving
    # the long leg open still means the position is fully hedged (worst case:
    # holding an extra long call). Closing long-first and failing on the short
    # would leave a naked short call — an unbounded-risk position — so that
    # ordering is never acceptable here.
    if short_leg:
        _, short_close_status = options_executor.close_option_position(
            short_leg.contract_symbol, short_leg.contracts
        )
        if short_close_status not in ("closed",) and "filled" not in str(short_close_status).lower():
            _log.warning(
                "debit_spread_trader: short leg close for %s returned %r — "
                "aborting before touching the long leg to avoid a naked short",
                symbol, short_close_status,
            )
            return (
                f"Close FAILED for {symbol}: short leg did not confirm closed "
                f"({short_close_status}). Long leg left untouched — position still "
                f"hedged. Retry /closespread {symbol}."
            )
    if long_leg:
        options_executor.close_option_position(long_leg.contract_symbol, long_leg.contracts)

    realized_pnl: Optional[float] = None
    if exit_long is not None and exit_short is not None:
        exit_value = exit_long - exit_short
        realized_pnl = round((exit_value - meta.entry_premium) * 100 * contracts, 2)

    log_options_fill(
        position_id=meta.position_id, symbol=symbol, strategy_type="bull_call_spread",
        side="CLOSE", contracts=contracts, fill_price=meta.entry_premium,
        order_id="manual_close_multi_leg", thesis=meta.thesis,
        realized_pnl=realized_pnl, exit_reason=close_reason,
        extra={"strategy": "debit_spread_screener", "drawdown_score": meta.scan_score_at_entry},
    )

    delete_option_meta(meta.position_id)
    if proposal is not None:
        delete_proposal(symbol)

    if realized_pnl is not None:
        sign = "+" if realized_pnl >= 0 else ""
        pnl_str = f"Realized P&L: {sign}${realized_pnl:.0f}."
    else:
        pnl_str = "P&L unavailable (could not fetch exit price before close)."
    return f"Closed: {symbol} bull call spread. Reason: {close_reason}. {pnl_str}"


# ── Status (read-only, no Alpaca call — safe to run on the GUI thread) ────────

def format_spreads_status() -> str:
    proposals = load_all_proposals()
    open_positions = [m for m in load_all_option_meta().values() if m.strategy_type == "bull_call_spread"]

    pend_entry = [p for p in proposals.values() if p.kind == "entry" and p.status == "pending"]
    pend_close = [p for p in proposals.values() if p.kind == "close" and p.status == "pending"]

    lines = ["<b>Debit Spreads</b>"]

    if pend_entry:
        lines.append("\n<b>Pending entry proposals:</b>")
        for p in pend_entry:
            lines.append(
                f"- {p.symbol}: ${p.long_strike:.0f}/${p.short_strike:.0f} exp {p.expiration} "
                f"x{p.suggested_contracts} debit ${p.net_debit:.2f} -- /approvespread {p.symbol}"
            )

    if pend_close:
        lines.append("\n<b>Pending close proposals:</b>")
        for p in pend_close:
            lines.append(f"- {p.symbol}: {p.close_reason} -- /closespread {p.symbol}")

    if open_positions:
        lines.append("\n<b>Open positions:</b>")
        for m in open_positions:
            try:
                days = (datetime.now() - datetime.fromisoformat(m.opened_at)).days
            except ValueError:
                days = 0
            long_leg = next((l for l in m.legs if l.side == "long"), None)
            short_leg = next((l for l in m.legs if l.side == "short"), None)
            strikes = f"${long_leg.strike:.0f}/${short_leg.strike:.0f}" if long_leg and short_leg else "?"
            expiry = long_leg.expiration if long_leg else "?"
            lines.append(
                f"- {m.symbol}: {strikes} exp {expiry} "
                f"({days}d held, entry debit ${m.entry_premium:.2f})"
            )

    if not pend_entry and not pend_close and not open_positions:
        lines.append("No pending proposals or open debit-spread positions.")

    return "\n".join(lines)
