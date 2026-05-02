"""
history_store.py — Append-only weekly JSONL archive for strategy recalibration.

Layout
------
data/history/
  scans/      YYYY-WNN.jsonl   one row per ScanResult
  decisions/  YYYY-WNN.jsonl   one row per conviction evaluation (pass + fail)
  debates/    YYYY-WNN.jsonl   one row per Claude debate verdict
  outcomes/   YYYY-WNN.jsonl   one row per price-outcome check (N days later)

All writes are single-line JSON appends — no file rewriting, minimal I/O.
Read with read_weeks(subdir) for recalibration; it streams line-by-line.

Outcome tracking
----------------
check_and_log_outcomes(days_back) is designed to be called from the heartbeat.
It reads the decisions file from `days_back` days ago, fetches current prices
via yfinance, and writes to outcomes/.  After enough cycles you can evaluate
whether the debate/conviction gates were making correct calls.
"""
import json
import logging
import os
from datetime import datetime, date, timedelta
from typing import Iterator, Optional

_log  = logging.getLogger(__name__)
_BASE = os.path.join(os.path.dirname(__file__), "..", "data", "history")

# Check outcomes at these intervals (days after evaluation)
OUTCOME_HORIZONS = (5, 10, 20)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _week_label(dt: Optional[datetime] = None) -> str:
    """'2026-W18' from a datetime (UTC now if omitted)."""
    d = (dt or datetime.utcnow()).date()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _week_path(subdir: str, dt: Optional[datetime] = None) -> str:
    folder = os.path.join(_BASE, subdir)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{_week_label(dt)}.jsonl")


def _append(path: str, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts_from_result(scan_result) -> str:
    ts = getattr(scan_result, "timestamp", None)
    if isinstance(ts, datetime):
        return ts.isoformat()
    return ts or _now_iso()


# ── Write API ─────────────────────────────────────────────────────────────────

def log_scan(scan_result, regime_label: str) -> None:
    """Archive one ScanResult. Called from score_history.append_scan_results."""
    ts = _ts_from_result(scan_result)
    try:
        dt = datetime.fromisoformat(ts)
    except Exception:
        dt = None
    _append(_week_path("scans", dt), {
        "ts":       ts,
        "sym":      scan_result.symbol,
        "total":    scan_result.total_score,
        "value":    scan_result.score_value,
        "growth":   scan_result.score_growth,
        "tech":     scan_result.score_technical,
        "pe":       scan_result.pe_ratio,
        "peg":      scan_result.peg_ratio,
        "rsi":      scan_result.rsi,
        "macd":     scan_result.macd_bullish,
        "near_ma":  scan_result.near_200d_ma,
        "vol_spike":scan_result.volume_spike,
        "price":    scan_result.price,
        "vol20d":   scan_result.volatility_20d,
        "regime":   regime_label,
        "mode":     getattr(scan_result, "scan_mode", ""),
    })


def log_decision(
    symbol: str,
    conviction: float,
    conviction_gate: float,
    breakdown: dict,
    scan_score: float,
    scan_gate: float,
    regime_label: str,
    ai_rank: Optional[int],
    sentiment: str,
    passed: bool,
) -> None:
    """Archive one conviction evaluation (pass OR fail) with full breakdown."""
    _append(_week_path("decisions"), {
        "ts":         _now_iso(),
        "sym":        symbol,
        "conviction": conviction,
        "gate":       conviction_gate,
        "passed":     passed,
        "scan_score": scan_score,
        "scan_gate":  scan_gate,
        "regime":     regime_label,
        "rank":       ai_rank,
        "sentiment":  sentiment,
        # Flattened breakdown — easier to query/plot without nested keys
        "b_scan":     breakdown.get("scan", 0),
        "b_rank":     breakdown.get("rank", 0),
        "b_alloc":    breakdown.get("ranker_alloc", 0),
        "b_sent":     breakdown.get("sentiment", 0),
        "b_dir":      breakdown.get("direction", 0),
        "b_tech":     breakdown.get("tech", 0),
    })


def log_debate(
    symbol: str,
    conviction: float,
    verdict: str,          # "BUY" or "PASS"
    reasoning: str,
    regime_label: str,
    model: str,
    cost_usd: float,
    price_at_eval: Optional[float] = None,
) -> None:
    """Archive one Claude debate outcome with reasoning excerpt."""
    _append(_week_path("debates"), {
        "ts":         _now_iso(),
        "sym":        symbol,
        "conviction": conviction,
        "verdict":    verdict,
        "regime":     regime_label,
        "model":      model,
        "cost_usd":   round(cost_usd, 6),
        "price_eval": price_at_eval,
        "reasoning":  reasoning[:600],
    })


def log_outcome(
    symbol: str,
    ts_eval: str,
    conviction: float,
    debate_verdict: str,
    regime_at_eval: str,
    price_at_eval: float,
    price_now: float,
    days_elapsed: int,
) -> None:
    """Archive a price-outcome check. was_correct reflects debate direction."""
    pct = round((price_now - price_at_eval) / price_at_eval * 100, 2) if price_at_eval else None
    if debate_verdict == "BUY":
        was_correct = (pct > 0) if pct is not None else None
    elif debate_verdict == "PASS":
        was_correct = (pct <= 0) if pct is not None else None
    else:
        was_correct = None
    _append(_week_path("outcomes"), {
        "ts":          _now_iso(),
        "sym":         symbol,
        "ts_eval":     ts_eval,
        "conviction":  conviction,
        "verdict":     debate_verdict,
        "regime_eval": regime_at_eval,
        "price_eval":  price_at_eval,
        "price_now":   round(price_now, 4),
        "pct":         pct,
        "days":        days_elapsed,
        "correct":     was_correct,
    })


# ── Outcome check (called from heartbeat) ────────────────────────────────────

def check_and_log_outcomes(days_back: int = 5) -> int:
    """
    Find debate records from `days_back` days ago, fetch current prices,
    write to outcomes/.  Returns the number of outcomes logged.

    Safe to call every heartbeat — skips symbols already logged for this
    (ts_eval, days) pair.
    """
    import yfinance as yf

    target_date = (date.today() - timedelta(days=days_back)).isoformat()
    dt_target   = datetime.utcnow() - timedelta(days=days_back)

    # Read already-logged outcomes to avoid duplicates
    logged = set()
    for rec in read_weeks("outcomes", n_weeks=4):
        if str(rec.get("days")) == str(days_back):
            logged.add((rec.get("sym"), rec.get("ts_eval", "")[:10]))

    # Find debate records from `days_back` days ago
    candidates = []
    for rec in read_weeks("debates", n_weeks=max(days_back // 7 + 2, 2)):
        ts = rec.get("ts", "")
        if ts[:10] == target_date:
            key = (rec["sym"], ts[:10])
            if key not in logged and rec.get("price_eval"):
                candidates.append(rec)

    if not candidates:
        return 0

    # Batch price fetch
    symbols = list({r["sym"] for r in candidates})
    try:
        tickers = yf.Tickers(" ".join(symbols))
        prices  = {}
        for sym in symbols:
            try:
                prices[sym] = float(tickers.tickers[sym].fast_info.last_price or 0)
            except Exception:
                prices[sym] = 0.0
    except Exception as exc:
        _log.warning("history_store: outcome price fetch failed: %s", exc)
        return 0

    count = 0
    for rec in candidates:
        sym       = rec["sym"]
        price_now = prices.get(sym, 0.0)
        if price_now <= 0:
            continue
        log_outcome(
            symbol=sym,
            ts_eval=rec["ts"],
            conviction=rec.get("conviction", 0),
            debate_verdict=rec.get("verdict", ""),
            regime_at_eval=rec.get("regime", ""),
            price_at_eval=rec.get("price_eval", 0),
            price_now=price_now,
            days_elapsed=days_back,
        )
        count += 1

    if count:
        _log.info("history_store: logged %d outcomes (%dd horizon)", count, days_back)
    return count


# ── Read API (for recalibration scripts) ─────────────────────────────────────

def read_weeks(subdir: str, n_weeks: int = 8) -> Iterator[dict]:
    """
    Yield all records from the last n_weeks of JSONL files in subdir.
    Streams line-by-line — does not load entire files into memory.
    """
    folder = os.path.join(_BASE, subdir)
    if not os.path.exists(folder):
        return
    seen: set = set()
    for i in range(n_weeks):
        dt   = datetime.utcnow() - timedelta(weeks=i)
        path = os.path.join(folder, f"{_week_label(dt)}.jsonl")
        if path in seen or not os.path.exists(path):
            continue
        seen.add(path)
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        pass


# ── One-shot backfill ─────────────────────────────────────────────────────────

def backfill_scans(scan_results_path: Optional[str] = None) -> int:
    """
    Seed the scans/ archive from the existing scan_results.json on first run.
    Skips if data/history/scans/ already has any files.
    Returns number of records written.
    """
    scans_folder = os.path.join(_BASE, "scans")
    if os.path.exists(scans_folder) and os.listdir(scans_folder):
        return 0  # already seeded

    path = scan_results_path or os.path.join(
        os.path.dirname(__file__), "..", "data", "scan_results.json"
    )
    if not os.path.exists(path):
        return 0

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return 0

    count = 0
    for d in data:
        ts_str = d.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts_str)
        except Exception:
            dt = None
        _append(_week_path("scans", dt), {
            "ts":       ts_str,
            "sym":      d.get("symbol", ""),
            "total":    d.get("total_score", 0),
            "value":    d.get("score_value", 0),
            "growth":   d.get("score_growth", 0),
            "tech":     d.get("score_technical", 0),
            "pe":       d.get("pe_ratio"),
            "peg":      d.get("peg_ratio"),
            "rsi":      d.get("rsi"),
            "macd":     d.get("macd_bullish", False),
            "near_ma":  d.get("near_200d_ma", False),
            "vol_spike":d.get("volume_spike", False),
            "price":    d.get("price"),
            "vol20d":   d.get("volatility_20d"),
            "regime":   "unknown",
            "mode":     d.get("scan_mode", ""),
        })
        count += 1

    _log.info("history_store: backfilled %d scan records", count)
    return count
