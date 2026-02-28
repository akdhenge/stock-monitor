from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ScanResult:
    symbol: str
    score_value: float          # 0–100, weight 40%
    score_growth: float         # 0–100, weight 30%
    score_technical: float      # 0–100, weight 30%
    total_score: float          # = V*0.4 + G*0.3 + T*0.3
    pe_ratio: Optional[float]
    peg_ratio: Optional[float]
    debt_equity: Optional[float]
    price: Optional[float]
    week52_high: Optional[float]
    sector: Optional[str]
    revenue_growth: Optional[float]
    free_cash_flow: Optional[float]
    roe: Optional[float]
    rsi: Optional[float]
    macd_bullish: Optional[bool]
    near_200d_ma: Optional[bool]
    volume_spike: Optional[bool]
    scan_mode: str = "quick"
    timestamp: datetime = field(default_factory=datetime.now)
