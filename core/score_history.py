"""
score_history.py — Rolling score history and adaptive threshold computation.

Instead of fixed thresholds (bull=60) that may never be reachable in the
current market environment, the adaptive system computes the top-N percentile
of the universe scores seen in the last window_days and uses that as the gate,
clamped by a floor derived from the regime floor.

Modes
-----
bootstrap  — fewer than 20 rows in window; falls back to regime floor
adaptive   — 20+ rows; uses percentile threshold clamped by adaptive floor

The adaptive floor = max(regime.min_scan_score * floor_fraction, hard_floor)
so we never invest in genuine junk even if the whole market is depressed.
"""
import json
import os
from datetime import datetime, timedelta
from typing import Optional

_DATA_DIR     = os.path.join(os.path.dirname(__file__), "..", "data")
_HISTORY_PATH = os.path.join(_DATA_DIR, "score_history.json")
_SCAN_PATH    = os.path.join(_DATA_DIR, "scan_results.json")
_MAX_ROWS          = 10_000
_MAX_DECISION_ROWS = 2_000

# Percentile targets by regime: gate = score at this percentile of the window
# "top 20%" in bull → we gate at the 80th percentile of the distribution
_PERCENTILE_BY_REGIME = {
    "bull":    80,  # top 20%
    "neutral": 75,  # top 25%
    "bear":    70,  # top 30%
}

_DEFAULT_FLOOR_FRACTION = 0.60
_DEFAULT_HARD_FLOOR     = 25.0

# Decision score threshold percentiles — looser than scan because the funnel
# already narrowed (only ~5 candidates/day vs ~50 scan scores/day)
_DECISION_PERCENTILE_BY_REGIME = {
    "bull":    70,  # top 30%
    "neutral": 60,  # top 40%
    "bear":    50,  # top 50%
}

_DECISION_DEFAULT_FLOOR_FRACTION = 0.85   # bull bootstrap floor = 65 * 0.85 = 55.25
_DECISION_DEFAULT_HARD_FLOOR     = 40.0


# ── Public API ────────────────────────────────────────────────────────────────

def load_history(path: str = _HISTORY_PATH) -> dict:
    """
    Load score history from disk.
    On first run (file absent), backfills from scan_results.json automatically.
    """
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") == 1 and "rows" in data:
                # Ensure decision_rows key exists for older history files
                data.setdefault("decision_rows", [])
                return data
        except Exception:
            pass

    history = {"version": 1, "updated_at": "", "rows": [], "per_symbol": {}, "decision_rows": []}
    if os.path.exists(_SCAN_PATH):
        _backfill(history)
    return history


def save_history(history: dict, path: str = _HISTORY_PATH) -> None:
    """Atomic write of history dict to disk."""
    history["updated_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(history, f, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        pass


def append_scan_results(history: dict, results: list, regime_label: str) -> None:
    """
    Append a list of ScanResult dataclass instances to history.
    Prunes to _MAX_ROWS, rebuilds per_symbol cache, and archives to weekly JSONL.
    """
    from core.history_store import log_scan, backfill_scans
    backfill_scans()   # no-op after first run

    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    for r in results:
        try:
            log_scan(r, regime_label)
        except Exception:
            pass
        ts = getattr(r, "timestamp", None)
        if isinstance(ts, datetime):
            ts = ts.isoformat()
        history["rows"].append({
            "symbol":          r.symbol,
            "total_score":     r.total_score,
            "score_value":     r.score_value,
            "score_growth":    r.score_growth,
            "score_technical": r.score_technical,
            "regime":          regime_label,
            "scan_mode":       getattr(r, "scan_mode", "deep"),
            "timestamp":       ts or now_iso,
        })

    if len(history["rows"]) > _MAX_ROWS:
        history["rows"] = history["rows"][-_MAX_ROWS:]

    _rebuild_per_symbol(history)


def compute_adaptive_threshold(history: dict, regime, config: dict) -> dict:
    """
    Return the effective min_scan_score to use for this scan cycle.

    regime   — RegimeState (has .label and .min_scan_score)
    config   — trader_config dict (may contain adaptive_* overrides)

    Return dict keys:
      effective_min_scan_score, percentile_used, floor_used,
      n_samples, mode ("bootstrap" | "adaptive" | "disabled"), regime_floor
    """
    regime_floor = regime.min_scan_score
    label        = getattr(regime, "label", "neutral")

    if not config.get("adaptive_thresholds_enabled", True):
        return _result(regime_floor, 0, regime_floor, 0, "disabled", regime_floor)

    window_days = int(config.get("score_history_window_days", 45))
    cutoff      = (datetime.utcnow() - timedelta(days=window_days)).isoformat()

    window_scores = [
        r["total_score"] for r in history["rows"]
        if r.get("timestamp", "") >= cutoff
    ]
    n = len(window_scores)

    floor_fraction = float(config.get("adaptive_floor_fraction", _DEFAULT_FLOOR_FRACTION))
    hard_floor     = float(config.get("adaptive_hard_floor",     _DEFAULT_HARD_FLOOR))
    adaptive_floor = max(regime_floor * floor_fraction, hard_floor)

    pct_key = {
        "bull":    int(config.get("adaptive_percentile_bull",    80)),
        "neutral": int(config.get("adaptive_percentile_neutral", 75)),
        "bear":    int(config.get("adaptive_percentile_bear",    70)),
    }
    pct_target = pct_key.get(label, 75)

    if n < 20:
        return _result(regime_floor, pct_target, adaptive_floor, n, "bootstrap", regime_floor)

    pct_threshold = _percentile(window_scores, pct_target)
    effective     = round(max(pct_threshold, adaptive_floor), 1)
    return _result(effective, pct_target, adaptive_floor, n, "adaptive", regime_floor)


def append_decision_score(
    history: dict,
    symbol: str,
    conviction_score: float,
    regime_label: str,
    passed_gate: bool,
    scan_score: float,
    ai_rank: Optional[int],
    sentiment: str,
) -> None:
    """
    Record a conviction score evaluation (pass OR fail) in decision_rows.
    Called every time compute_conviction_score runs so we build a realistic
    distribution of what scores actually look like, not just the survivors.
    """
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    history.setdefault("decision_rows", []).append({
        "symbol":          symbol,
        "conviction_score": conviction_score,
        "regime":          regime_label,
        "passed_gate":     passed_gate,
        "scan_score":      scan_score,
        "ai_rank":         ai_rank,
        "sentiment":       sentiment,
        "timestamp":       now_iso,
    })
    if len(history["decision_rows"]) > _MAX_DECISION_ROWS:
        history["decision_rows"] = history["decision_rows"][-_MAX_DECISION_ROWS:]


def compute_adaptive_decision_threshold(history: dict, regime, config: dict) -> dict:
    """
    Compute effective min_conviction_score based on rolling percentile of
    observed conviction scores (both passed and failed evaluations).

    Returns dict with:
      effective_min_decision_score, percentile_used, floor_used,
      n_samples, mode ("bootstrap" | "adaptive" | "disabled"), regime_floor
    """
    regime_floor = regime.min_decision_score
    label        = getattr(regime, "label", "neutral")

    if not config.get("adaptive_decision_thresholds_enabled", True):
        return _decision_result(regime_floor, 0, regime_floor, 0, "disabled", regime_floor)

    window_days = int(config.get("score_history_window_days", 45))
    cutoff      = (datetime.utcnow() - timedelta(days=window_days)).isoformat()

    window_scores = [
        r["conviction_score"] for r in history.get("decision_rows", [])
        if r.get("timestamp", "") >= cutoff
    ]
    n = len(window_scores)

    floor_fraction = float(config.get("adaptive_decision_floor_fraction", _DECISION_DEFAULT_FLOOR_FRACTION))
    hard_floor     = float(config.get("adaptive_decision_hard_floor",     _DECISION_DEFAULT_HARD_FLOOR))
    adaptive_floor = max(regime_floor * floor_fraction, hard_floor)

    pct_key = {
        "bull":    int(config.get("adaptive_decision_percentile_bull",    70)),
        "neutral": int(config.get("adaptive_decision_percentile_neutral", 60)),
        "bear":    int(config.get("adaptive_decision_percentile_bear",    50)),
    }
    pct_target  = pct_key.get(label, 60)
    min_samples = int(config.get("adaptive_decision_min_samples", 15))

    if n < min_samples:
        # Bootstrap: use softened regime floor so we still get some trades
        return _decision_result(round(adaptive_floor, 1), pct_target, adaptive_floor, n, "bootstrap", regime_floor)

    pct_threshold = _percentile(window_scores, pct_target)
    effective     = round(max(pct_threshold, adaptive_floor), 1)
    return _decision_result(effective, pct_target, adaptive_floor, n, "adaptive", regime_floor)


def check_per_symbol_quality(symbol: str, score: float, history: dict) -> tuple:
    """
    Soft gate: score must be >= 95% of the symbol's 30-day mean score.
    Bypassed when the symbol has fewer than 3 history entries (new or rare).
    Returns (passed: bool, reason: str).
    """
    stats = history.get("per_symbol", {}).get(symbol)
    if not stats or stats.get("n", 0) < 3:
        return True, ""
    mean_30d = stats.get("mean_30d", 0)
    if mean_30d <= 0:
        return True, ""
    threshold = mean_30d * 0.95
    if score < threshold:
        return False, (f"{symbol} score {score:.1f} < 95% of 30d mean "
                       f"{mean_30d:.1f} (min {threshold:.1f})")
    return True, ""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _result(effective, pct, floor, n, mode, regime_floor) -> dict:
    return {
        "effective_min_scan_score": effective,
        "percentile_used":          pct,
        "floor_used":               floor,
        "n_samples":                n,
        "mode":                     mode,
        "regime_floor":             regime_floor,
    }


def _decision_result(effective, pct, floor, n, mode, regime_floor) -> dict:
    return {
        "effective_min_decision_score": effective,
        "percentile_used":              pct,
        "floor_used":                   floor,
        "n_samples":                    n,
        "mode":                         mode,
        "regime_floor":                 regime_floor,
    }


def _percentile(data: list, p: float) -> float:
    """Linear-interpolation percentile; p is 0–100."""
    if not data:
        return 0.0
    s   = sorted(data)
    n   = len(s)
    idx = (p / 100.0) * (n - 1)
    lo  = int(idx)
    hi  = min(lo + 1, n - 1)
    return s[lo] + (idx - lo) * (s[hi] - s[lo])


def _backfill(history: dict) -> None:
    """One-shot seed from scan_results.json when history file doesn't exist yet."""
    try:
        with open(_SCAN_PATH, encoding="utf-8") as f:
            scan_data = json.load(f)
        for d in scan_data:
            history["rows"].append({
                "symbol":          d.get("symbol", ""),
                "total_score":     d.get("total_score", 0),
                "score_value":     d.get("score_value", 0),
                "score_growth":    d.get("score_growth", 0),
                "score_technical": d.get("score_technical", 0),
                "regime":          "unknown",
                "scan_mode":       d.get("scan_mode", "deep"),
                "timestamp":       d.get("timestamp", ""),
            })
        _rebuild_per_symbol(history)
    except Exception:
        pass


def _rebuild_per_symbol(history: dict) -> None:
    """Recompute per_symbol stats dict from all rows. 30d window for mean."""
    cutoff_30d = (datetime.utcnow() - timedelta(days=30)).isoformat()
    acc: dict  = {}

    for row in history["rows"]:
        sym   = row["symbol"]
        score = row["total_score"]
        ts    = row.get("timestamp", "")

        if sym not in acc:
            acc[sym] = {"max_score": score, "scores_30d": [], "last_score": score, "last_seen": ts, "n": 0}

        e = acc[sym]
        e["n"] += 1
        if score > e["max_score"]:
            e["max_score"] = score
        if ts >= e["last_seen"]:
            e["last_score"] = score
            e["last_seen"]  = ts
        if ts >= cutoff_30d:
            e["scores_30d"].append(score)

    history["per_symbol"] = {
        sym: {
            "max_score":  v["max_score"],
            "mean_30d":   round(sum(v["scores_30d"]) / len(v["scores_30d"]), 2)
                          if v["scores_30d"] else v["last_score"],
            "n":          v["n"],
            "last_score": v["last_score"],
            "last_seen":  v["last_seen"],
        }
        for sym, v in acc.items()
    }
