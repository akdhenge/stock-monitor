import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.scan_result import ScanResult

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_RESULTS_PATH = os.path.join(_DATA_DIR, "scan_results.json")


def _ensure_data_dir() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)


def _result_to_dict(r: ScanResult) -> Dict[str, Any]:
    return {
        "symbol": r.symbol,
        "score_value": r.score_value,
        "score_growth": r.score_growth,
        "score_technical": r.score_technical,
        "total_score": r.total_score,
        "pe_ratio": r.pe_ratio,
        "peg_ratio": r.peg_ratio,
        "debt_equity": r.debt_equity,
        "price": r.price,
        "week52_high": r.week52_high,
        "sector": r.sector,
        "revenue_growth": r.revenue_growth,
        "free_cash_flow": r.free_cash_flow,
        "roe": r.roe,
        "rsi": r.rsi,
        "macd_bullish": r.macd_bullish,
        "near_200d_ma": r.near_200d_ma,
        "volume_spike": r.volume_spike,
        "scan_mode": r.scan_mode,
        "score_congressional": r.score_congressional,
        "ai_rank": r.ai_rank,
        "timestamp": r.timestamp.isoformat(),
    }


def _dict_to_result(d: Dict[str, Any]) -> Optional[ScanResult]:
    try:
        ts_str = d.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            ts = datetime.now()
        return ScanResult(
            symbol=d["symbol"],
            score_value=float(d.get("score_value", 0.0)),
            score_growth=float(d.get("score_growth", 0.0)),
            score_technical=float(d.get("score_technical", 0.0)),
            total_score=float(d.get("total_score", 0.0)),
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
            macd_bullish=d.get("macd_bullish"),
            near_200d_ma=d.get("near_200d_ma"),
            volume_spike=d.get("volume_spike"),
            scan_mode=d.get("scan_mode", "quick"),
            score_congressional=float(d.get("score_congressional", 0.0)),
            ai_rank=int(d["ai_rank"]) if d.get("ai_rank") is not None else None,
            timestamp=ts,
        )
    except (KeyError, TypeError, ValueError):
        return None


def load_scan_results() -> List[ScanResult]:
    _ensure_data_dir()
    if not os.path.exists(_RESULTS_PATH):
        return []
    try:
        with open(_RESULTS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        results = []
        for d in raw:
            r = _dict_to_result(d)
            if r is not None:
                results.append(r)
        return results
    except (json.JSONDecodeError, ValueError, TypeError):
        return []


def save_scan_results(results: List[ScanResult]) -> None:
    _ensure_data_dir()
    with open(_RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump([_result_to_dict(r) for r in results], f, indent=2)
