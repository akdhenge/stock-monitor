"""
performance_tracker.py — Portfolio metrics computed from the equity curve + trade journal.

All functions are pure reads — no I/O side effects except log_nav_snapshot()
which is called by TraderAgent on its daily heartbeat.

Metrics returned:
  total_return_pct, cagr, sharpe_30d, sharpe_all, max_drawdown_pct,
  win_rate_pct, profit_factor, avg_win, avg_loss, spy_alpha_pct
"""
import math
from datetime import datetime
from typing import Dict, List, Optional

from core.trade_journal import read_equity_curve, read_journal, log_nav_snapshot


# ── Snapshot ───────────────────────────────────────────────────────────────────

def maybe_snapshot(executor, last_snapshot_date: str) -> str:
    """
    Write a daily NAV snapshot if we haven't done one today.
    Returns today's date string (caller should store it for the next check).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    if last_snapshot_date == today:
        return today
    try:
        acct = executor.get_account()
        spy  = executor.get_spy_price()
        nav  = acct["equity"]
        cash = acct["cash"]
        log_nav_snapshot(
            nav=nav,
            cash=cash,
            positions_value=round(nav - cash, 2),
            spy_close=spy,
        )
    except Exception:
        pass
    return today


# ── Core metrics ───────────────────────────────────────────────────────────────

def compute_metrics(last_n_curve: int = 500) -> Dict:
    curve   = read_equity_curve(last_n=last_n_curve)
    journal = read_journal(last_n=2000)

    if not curve:
        return {"error": "No equity curve data yet — agent has not run a daily snapshot."}

    equities = [e["nav"] for e in curve if "nav" in e]
    if not equities:
        return {"error": "Equity curve has no valid NAV entries."}

    starting  = equities[0]
    current   = equities[-1]
    n_days    = len(equities)

    total_return_pct = _pct(current - starting, starting)

    # CAGR (only meaningful after 30+ days)
    cagr = None
    if n_days >= 30:
        years = n_days / 252
        cagr  = round(((current / starting) ** (1 / years) - 1) * 100, 2) if starting > 0 else None

    # Max drawdown
    max_drawdown_pct = _max_drawdown(equities)

    # Sharpe ratios
    sharpe_all = _sharpe(equities)
    sharpe_30d = _sharpe(equities[-30:]) if len(equities) >= 30 else None

    # SPY benchmark
    spy_return_pct = None
    alpha_pct      = None
    spy_prices = [e.get("spy_close") for e in curve if e.get("spy_close")]
    if len(spy_prices) >= 2:
        spy_return_pct = _pct(spy_prices[-1] - spy_prices[0], spy_prices[0])
        alpha_pct      = round(total_return_pct - spy_return_pct, 2)

    # Trade stats
    fills = [e for e in journal if e.get("type") == "fill" and e.get("side") == "SELL"]
    wins  = [f for f in fills if (f.get("realized_pnl") or 0) > 0]
    losses = [f for f in fills if (f.get("realized_pnl") or 0) <= 0]

    win_rate  = round(len(wins) / len(fills) * 100, 1) if fills else None
    avg_win   = round(sum(f.get("realized_pnl", 0) for f in wins) / len(wins), 2)   if wins   else None
    avg_loss  = round(sum(f.get("realized_pnl", 0) for f in losses) / len(losses), 2) if losses else None
    gross_win = sum(f.get("realized_pnl", 0) for f in wins)
    gross_loss= abs(sum(f.get("realized_pnl", 0) for f in losses))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

    # Rejection breakdown (top reasons agent passed)
    decisions = [e for e in journal if e.get("type") == "decision"]
    rejects   = [e for e in decisions if e.get("action") == "REJECT"]
    reject_reasons: Dict[str, int] = {}
    for r in rejects:
        reason = r.get("reason", "unknown")
        reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
    top_reject_reasons = sorted(reject_reasons.items(), key=lambda x: -x[1])[:5]

    return {
        "starting_nav":        starting,
        "current_nav":         current,
        "total_return_pct":    total_return_pct,
        "cagr_pct":            cagr,
        "max_drawdown_pct":    max_drawdown_pct,
        "sharpe_all":          sharpe_all,
        "sharpe_30d":          sharpe_30d,
        "days_tracked":        n_days,
        "spy_return_pct":      spy_return_pct,
        "alpha_pct":           alpha_pct,
        "total_closed_trades": len(fills),
        "wins":                len(wins),
        "losses":              len(losses),
        "win_rate_pct":        win_rate,
        "avg_win_dollars":     avg_win,
        "avg_loss_dollars":    avg_loss,
        "profit_factor":       profit_factor,
        "top_reject_reasons":  top_reject_reasons,
    }


def format_performance_telegram(metrics: Dict) -> str:
    if "error" in metrics:
        return f"Performance: {metrics['error']}"

    lines = [
        f"<b>Performance</b>",
        f"NAV: <b>${metrics['current_nav']:,.0f}</b>  ({metrics['total_return_pct']:+.1f}%)",
    ]
    if metrics.get("alpha_pct") is not None:
        lines.append(f"vs SPY: <b>{metrics['alpha_pct']:+.1f}%</b> alpha")
    if metrics.get("max_drawdown_pct") is not None:
        lines.append(f"Max DD: {metrics['max_drawdown_pct']:.1f}%")
    if metrics.get("sharpe_30d") is not None:
        lines.append(f"Sharpe (30d): {metrics['sharpe_30d']:.2f}")
    if metrics.get("win_rate_pct") is not None:
        lines.append(
            f"Trades: {metrics['total_closed_trades']} "
            f"({metrics['win_rate_pct']:.0f}% win)"
        )
    if metrics.get("profit_factor") is not None:
        lines.append(f"Profit factor: {metrics['profit_factor']:.2f}")
    return "\n".join(lines)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _pct(delta: float, base: float) -> float:
    return round(delta / base * 100, 2) if base else 0.0


def _max_drawdown(equities: List[float]) -> float:
    peak = equities[0]
    max_dd = 0.0
    for eq in equities:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
    return round(max_dd * 100, 2)


def _sharpe(equities: List[float], rf_daily: float = 0.0) -> Optional[float]:
    if len(equities) < 5:
        return None
    returns = [(equities[i] - equities[i-1]) / equities[i-1]
               for i in range(1, len(equities)) if equities[i-1] > 0]
    if not returns:
        return None
    mean_r = sum(returns) / len(returns) - rf_daily
    variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
    std_r = math.sqrt(variance)
    if std_r == 0:
        return None
    return round(mean_r / std_r * math.sqrt(252), 3)
