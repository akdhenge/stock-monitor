"""
dry_run.py — Full pipeline simulation, no trades placed.

Runs the complete decision flow: regime detection → hard gates →
decision score → Claude debate, for every ranked candidate.
Prints a step-by-step trace and final cost breakdown.

Usage:
    py -3.12 dry_run.py
"""
import json
import os
import sys
from datetime import datetime

# Make core imports work from stock-monitor root
sys.path.insert(0, os.path.dirname(__file__))

import yfinance as yf

from core.scan_result import ScanResult
from core.market_regime import detect_regime, format_regime_log
from core.risk_manager import check_hard_gates, compute_conviction_score, size_position, _REGIME_SIZE_MULT
from core.trade_debate import run_debate
from core.settings_store import load_settings

DATA_DIR    = os.path.join(os.path.dirname(__file__), "data")
RANKING_FILE = os.path.join(DATA_DIR, "claude_ranking_cache.json")
SCAN_FILE    = os.path.join(DATA_DIR, "scan_results.json")
AI_FILE      = os.path.join(DATA_DIR, "ai_research_cache.json")

DIVIDER  = "-" * 70
DDIVIDER = "=" * 70


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_scan_lookup(scan_data):
    """Return {symbol: ScanResult} using the most recent entry per symbol."""
    by_sym = {}
    for d in scan_data:
        sym = d["symbol"]
        if sym not in by_sym or d["timestamp"] > by_sym[sym]["timestamp"]:
            by_sym[sym] = d
    results = {}
    for sym, d in by_sym.items():
        results[sym] = ScanResult(
            symbol=sym,
            score_value=d.get("score_value", 0),
            score_growth=d.get("score_growth", 0),
            score_technical=d.get("score_technical", 0),
            total_score=d.get("total_score", 0),
            pe_ratio=d.get("pe_ratio"),
            peg_ratio=d.get("peg_ratio"),
            debt_equity=d.get("debt_equity"),
            price=d.get("price"),
            week52_high=d.get("week52_high"),
            sector=d.get("sector"),
            revenue_growth=d.get("revenue_growth"),
            free_cash_flow=d.get("free_cash_flow"),
            roe=d.get("roe"),
            rsi=d.get("rsi"),
            macd_bullish=d.get("macd_bullish", False),
            near_200d_ma=d.get("near_200d_ma", False),
            volume_spike=d.get("volume_spike", False),
            scan_mode=d.get("scan_mode", "deep"),
            ai_rank=d.get("ai_rank"),
            volatility_20d=d.get("volatility_20d"),
            avg_volume_20d=d.get("avg_volume_20d"),
            timestamp=datetime.fromisoformat(d["timestamp"]),
        )
    return results


def fmt(label, value, status=""):
    status_icon = {"ok": "OK", "fail": "!!", "warn": "??", "info": "--"}.get(status, "  ")
    print(f"  {status_icon}  {label:<40} {value}")


def main():
    print(DDIVIDER)
    print("  TRADER AGENT DRY RUN -- full pipeline simulation")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(DDIVIDER)

    # ── Load data ──────────────────────────────────────────────────────────────
    print("\n[1] LOADING DATA")
    ranking_data = load_json(RANKING_FILE)
    scan_data    = load_json(SCAN_FILE)
    ai_cache     = load_json(AI_FILE) if os.path.exists(AI_FILE) else {}
    settings     = load_settings()

    ranked_list  = ranking_data.get("ranked", [])
    scan_lookup  = build_scan_lookup(scan_data)
    ranking_ts   = ranking_data.get("generated_at", "unknown")
    ranking_model= ranking_data.get("model", "unknown")
    ranking_cost = ranking_data.get("cost_usd", 0)
    ranking_in   = ranking_data.get("input_tokens", 0)
    ranking_out  = ranking_data.get("output_tokens", 0)

    fmt("Ranking cache generated", ranking_ts, "info")
    fmt("Ranking model", ranking_model, "info")
    fmt("Ranked candidates", len(ranked_list), "info")
    fmt("Scan results loaded", len(scan_lookup), "info")
    fmt("AI research cache entries", len(ai_cache), "info")

    # ── Regime detection ───────────────────────────────────────────────────────
    print(f"\n[2] MARKET REGIME DETECTION")
    vix_val  = None
    spy_hist = None
    try:
        vix_val  = float(yf.Ticker("^VIX").fast_info.last_price or 20.0)
        spy_hist = yf.Ticker("SPY").history(period="1y", interval="1d")
        fmt("VIX", f"{vix_val:.1f}", "info")
    except Exception as e:
        fmt("VIX/SPY fetch", f"FAILED ({e}) — defaulting to neutral", "warn")

    regime = detect_regime(spy_hist, vix_val)
    print(f"\n  >> {format_regime_log(regime)}\n")

    # ── Adaptive thresholds (read-only — dry run does not append to history) ──
    from core.score_history import (
        load_history, compute_adaptive_threshold, compute_adaptive_decision_threshold,
    )
    _score_hist        = load_history()
    _adaptive          = compute_adaptive_threshold(_score_hist, regime, settings)
    _decision_adaptive = compute_adaptive_decision_threshold(_score_hist, regime, settings)
    _regime_size_mult  = _REGIME_SIZE_MULT.get(regime.label, 1.0)

    # ── Build effective config ─────────────────────────────────────────────────
    effective_config = {
        "max_positions":           15,
        "min_position_pct":        0.02,
        "max_position_pct":        0.08,
        "cash_reserve_pct":        0.10,
        "max_sector_exposure_pct": 0.30,
        "min_price":               5.0,
        "min_avg_volume":          1_000_000,
        "min_scan_score":          _adaptive["effective_min_scan_score"],
        "min_decision_score":      _decision_adaptive["effective_min_decision_score"],
        "debate_model":            settings.get("debate_model", "claude-sonnet-4-6"),
    }

    print(f"[3] EFFECTIVE THRESHOLDS  (regime: {regime.label.upper()})")
    fmt("min_scan_score (adaptive)", effective_config["min_scan_score"], "info")
    fmt(f"  ^ p{_adaptive['percentile_used']} of {_adaptive['n_samples']} samples",
        f"floor={_adaptive['floor_used']:.1f}  regime_floor={_adaptive['regime_floor']:.1f}  mode={_adaptive['mode']}", "info")
    fmt("min_conviction (adaptive)", effective_config["min_decision_score"], "info")
    fmt(f"  ^ p{_decision_adaptive['percentile_used']} of {_decision_adaptive['n_samples']} samples",
        f"floor={_decision_adaptive['floor_used']:.1f}  regime_floor={_decision_adaptive['regime_floor']:.1f}  mode={_decision_adaptive['mode']}", "info")
    fmt("regime size multiplier", f"{_regime_size_mult:.2f}x", "info")
    fmt("max_candidates", regime.max_candidates, "info")
    fmt("debate_model", effective_config["debate_model"], "info")

    # Mock account for gate checks (paper account, no real call)
    MOCK_NAV  = 100_000.0
    MOCK_CASH = 90_000.0
    open_symbols    = []
    sector_exposure = {}

    # ── Candidate evaluation ───────────────────────────────────────────────────
    print(f"\n[4] CANDIDATE EVALUATION  (top {regime.max_candidates})")
    print(DIVIDER)

    debate_cost_total = 0.0
    debate_in_total   = 0
    debate_out_total  = 0
    debate_calls      = 0
    approved = []
    rejected = []

    for ranked_item in ranked_list[:regime.max_candidates]:
        sym        = ranked_item.get("symbol", "").upper()
        rank       = ranked_item.get("rank")
        alloc_pct  = ranked_item.get("allocation_pct")
        rationale  = ranked_item.get("rationale", "")

        print(f"\n  #{rank}  {sym}  (allocation_pct={alloc_pct}%)")

        scan_result = scan_lookup.get(sym)
        if scan_result is None:
            fmt("Scan result", "NOT IN SCAN BATCH — skip", "fail")
            rejected.append((sym, "not in scan batch", None))
            continue

        ai_research = ai_cache.get(sym)
        if ai_research:
            fmt("AI research", f"cached  sentiment={ai_research.get('sentiment','?')}  direction={ai_research.get('direction','?')}", "info")
        else:
            fmt("AI research", "not cached", "warn")

        # --- Hard gates ---
        passed, reject_reason = check_hard_gates(
            symbol=sym,
            scan_result=scan_result,
            ai_research=ai_research,
            open_symbols=open_symbols,
            sector_exposure=sector_exposure,
            cash=MOCK_CASH,
            nav=MOCK_NAV,
            config=effective_config,
        )
        fmt(f"scan score {scan_result.total_score:.1f}", f"gate={effective_config['min_scan_score']}", "ok" if passed or "scan score" not in reject_reason else "fail")
        if not passed:
            fmt("Hard gate", f"REJECTED - {reject_reason}", "fail")
            rejected.append((sym, f"hard gate: {reject_reason}", scan_result.total_score))
            continue
        fmt("Hard gates", "ALL PASSED", "ok")

        # --- Conviction score (gates entry + drives sizing) ---
        conviction, cv_breakdown = compute_conviction_score(scan_result, rank, ai_research, alloc_pct)
        min_conviction = effective_config["min_decision_score"]
        cv_ok = conviction >= min_conviction
        fmt(
            f"Conviction {conviction:.1f} / gate {min_conviction:.1f}",
            f"scan={cv_breakdown.get('scan',0):.1f} rank={cv_breakdown.get('rank',0):.1f} "
            f"alloc={cv_breakdown.get('ranker_alloc',0):.1f} sent={cv_breakdown.get('sentiment',0):.0f} "
            f"dir={cv_breakdown.get('direction',0):.0f} tech={cv_breakdown.get('tech',0):.0f}",
            "ok" if cv_ok else "fail",
        )
        if not cv_ok:
            fmt("Conviction gate", f"REJECTED -- {conviction:.1f} < {min_conviction:.1f}", "fail")
            rejected.append((sym, f"conviction {conviction:.1f} < gate {min_conviction:.1f}", scan_result.total_score))
            continue

        # --- Position sizing ---
        dollars, size_breakdown = size_position(
            conviction_score=conviction,
            effective_min_conviction=min_conviction,
            volatility=scan_result.volatility_20d,
            regime_label=regime.label,
            nav=MOCK_NAV,
            cash=MOCK_CASH,
            config=effective_config,
        )
        fmt(
            f"Size ${dollars:,.0f}",
            f"base={size_breakdown.get('base_pct',0)*100:.1f}%  "
            f"vol={size_breakdown.get('vol_factor',1):.2f}x  "
            f"regime={size_breakdown.get('regime_mult',1):.2f}x  "
            f"-> {size_breakdown.get('final_pct',0)*100:.1f}%",
            "ok",
        )

        # --- Claude debate ---
        fmt("-> Claude debate", f"calling {effective_config['debate_model']} [{regime.label}]...", "info")
        debate_calls += 1
        debate_ok, debate_verdict = run_debate(
            symbol=sym,
            scan_result=scan_result,
            ai_research=ai_research,
            claude_rationale=rationale,
            decision_score=conviction,
            regime=regime.label,
            settings=settings,
        )

        # Read cost from usage log (last entry for this symbol)
        entry_cost, entry_in, entry_out = _read_last_debate_cost(sym)
        debate_cost_total += entry_cost
        debate_in_total   += entry_in
        debate_out_total  += entry_out

        if debate_ok:
            fmt("Debate verdict", f"BUY OK  -- {debate_verdict[:80]}", "ok")
            approved.append((sym, rank, alloc_pct, conviction, dollars, debate_verdict))
            open_symbols.append(sym)
        else:
            fmt("Debate verdict", f"PASS !!  -- {debate_verdict[:80]}", "fail")
            rejected.append((sym, f"debate: {debate_verdict[:80]}", scan_result.total_score))

    # ── Results ────────────────────────────────────────────────────────────────
    print(f"\n{DDIVIDER}")
    print(f"  RESULTS")
    print(DDIVIDER)
    print(f"\n  APPROVED FOR TRADE  ({len(approved)})")
    if approved:
        for sym, rank, alloc_pct, conviction, dollars, verdict in approved:
            print(f"    OK  #{rank:2d}  {sym:<6}  conviction={conviction:.0f}  size=${dollars:,.0f}")
            print(f"         {verdict[:90]}")
    else:
        print("    (none)")

    print(f"\n  REJECTED  ({len(rejected)})")
    for sym, reason, scan_score in rejected:
        score_str = f"scan={scan_score:.0f}" if scan_score else ""
        print(f"    !!  {sym:<6}  {score_str:<10}  {reason[:70]}")

    # ── Cost breakdown ─────────────────────────────────────────────────────────
    debate_model   = effective_config["debate_model"]
    from core.trade_debate import _COST_PER_M, _DEFAULT_COST
    rates = _COST_PER_M.get(debate_model, _DEFAULT_COST)
    debate_cost_check = (debate_in_total / 1e6) * rates["input"] + (debate_out_total / 1e6) * rates["output"]

    total_in  = ranking_in  + debate_in_total
    total_out = ranking_out + debate_out_total
    total_cost= ranking_cost + debate_cost_total

    print(f"\n{DDIVIDER}")
    print(f"  COST BREAKDOWN")
    print(DDIVIDER)
    print(f"  {'Component':<30} {'In tokens':>10} {'Out tokens':>10} {'Cost USD':>10}")
    print(f"  {'-'*30} {'-'*10} {'-'*10} {'-'*10}")
    print(f"  {'Claude Ranking':<30} {ranking_in:>10,} {ranking_out:>10,} ${ranking_cost:>9.4f}")
    print(f"  {f'Debate ({debate_calls} calls, {debate_model})':<30} {debate_in_total:>10,} {debate_out_total:>10,} ${debate_cost_total:>9.4f}")
    print(f"  {'-'*30} {'-'*10} {'-'*10} {'-'*10}")
    print(f"  {'TOTAL per cycle':<30} {total_in:>10,} {total_out:>10,} ${total_cost:>9.4f}")
    print(f"\n  Note: Ranking runs ~hourly. Debate runs only for candidates passing all gates.")
    print(f"        Cost per day ~= ${total_cost * 8:.3f} (8 scan cycles/RTH day)")
    print(DDIVIDER + "\n")


def _read_last_debate_cost(symbol: str):
    """Read the most recent debate cost entry for this symbol from the usage log."""
    log_path = os.path.join(DATA_DIR, "claude_usage_log.json")
    if not os.path.exists(log_path):
        return 0.0, 0, 0
    try:
        with open(log_path, encoding="utf-8") as f:
            log = json.load(f)
        trigger = f"debate:{symbol}:"
        for entry in reversed(log):
            if entry.get("trigger", "").startswith(trigger):
                return (
                    entry.get("cost_usd", 0.0),
                    entry.get("input_tokens", 0),
                    entry.get("output_tokens", 0),
                )
    except Exception:
        pass
    return 0.0, 0, 0


if __name__ == "__main__":
    main()
