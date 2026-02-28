from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class StockEntry:
    symbol: str
    low_target: float
    high_target: float
    notes: str = ""
    current_price: Optional[float] = None
    alert_status: str = "OK"  # "OK" | "BELOW LOW" | "ABOVE HIGH"
    last_low_alert: Optional[datetime] = None   # runtime only, not persisted
    last_high_alert: Optional[datetime] = None  # runtime only, not persisted


@dataclass
class AlertRecord:
    timestamp: datetime
    symbol: str
    direction: str    # "ABOVE HIGH" | "BELOW LOW"
    price: float
    target: float
    notified: bool = False
