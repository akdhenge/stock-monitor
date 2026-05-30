from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class DrawdownResult:
    symbol: str
    score: float                    # 0–100 composite
    current_price: float
    pct_below_high: float           # 0.23 = 23% below 52w high
    days_since_high: int
    analyst_upside_pct: float       # 0.37 = 37% upside to consensus target
    buy_rating_pct: float           # 0.96 = 96% Buy/Strong Buy
    analyst_count: int
    revenue_growth_yoy: float       # 0.33 = 33% YoY
    earnings_beat: bool             # True if beat at least one of revenue/EPS vs estimates
    iv_rank: Optional[float]        # 0–100, relative to stock's 1Y IV history
    next_earnings_date: Optional[str]  # ISO date string
    cause_label: str                # e.g. "capex_concern" | "unclear" | "SKIPPED"
    cause_summary: str              # 2-3 sentence LLM summary
    cause_confidence: str           # "high" | "medium" | "low" | "n/a"
    failed_gate: Optional[str]      # set for close-miss candidates; which gate they failed
    score_analyst: float = 0.0
    score_fundamentals: float = 0.0
    score_drawdown: float = 0.0
    score_options: float = 0.0
    market_cap_b: float = 0.0       # market cap in billions
    operating_cashflow: float = 0.0
    options_verified: bool = False
    commodity_exposure: Optional[str] = None  # "HIGH" | "MEDIUM" | "LOW" | None
    commodity_rationale: str = ""
    multi_causal_flag: bool = False            # True if >1 primary cause identified (NFLX pattern)
    cause_labels_all: list = field(default_factory=list)  # all causes identified by LLM
    avg_volume_30d: float = 0.0               # 30-day avg daily volume
    downgrade_count_90d: int = 0              # analyst rating downgrades in last 90 days
    timestamp: datetime = field(default_factory=datetime.now)


# Cause labels that indicate acceptable (non-fundamental) drop causes
ACCEPTABLE_CAUSES = {
    "capex_concern",
    "margin_pressure",
    "sector_rotation",
    "one_time_legal",
    "macro_panic",
    "guidance_cut",
    "unclear",
}

# Cause labels that indicate real fundamental damage — screen rejects these
UNACCEPTABLE_CAUSES = {
    "demand_decline",
    "share_loss",
    "product_failure",
    "accounting",
    "exec_departure",
    "existential_regulatory",
    "secular_decline",
}
