from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from core.models import AlertRecord, StockEntry
from notifiers.base_notifier import BaseNotifier


class AlertManager:
    def __init__(self, cooldown_minutes: int = 30):
        self.cooldown_minutes = cooldown_minutes
        self._notifiers: List[BaseNotifier] = []
        self._on_alert: Optional[Callable[[AlertRecord], None]] = None

    def set_notifiers(self, notifiers: List[BaseNotifier]) -> None:
        self._notifiers = notifiers

    def set_alert_callback(self, callback: Callable[[AlertRecord], None]) -> None:
        self._on_alert = callback

    def _cooldown_expired(self, last_alert: Optional[datetime]) -> bool:
        if last_alert is None:
            return True
        return datetime.now() - last_alert >= timedelta(minutes=self.cooldown_minutes)

    def check_and_alert(self, entry: StockEntry) -> Optional[AlertRecord]:
        if entry.current_price is None:
            return None

        price = entry.current_price
        record: Optional[AlertRecord] = None

        if price < entry.low_target and self._cooldown_expired(entry.last_low_alert):
            record = AlertRecord(
                timestamp=datetime.now(),
                symbol=entry.symbol,
                direction="BELOW LOW",
                price=price,
                target=entry.low_target,
            )
            entry.last_low_alert = record.timestamp
            entry.alert_status = "BELOW LOW"

        elif price > entry.high_target and self._cooldown_expired(entry.last_high_alert):
            record = AlertRecord(
                timestamp=datetime.now(),
                symbol=entry.symbol,
                direction="ABOVE HIGH",
                price=price,
                target=entry.high_target,
            )
            entry.last_high_alert = record.timestamp
            entry.alert_status = "ABOVE HIGH"

        else:
            entry.alert_status = "OK"

        if record is not None:
            self._dispatch(record)
            if self._on_alert:
                self._on_alert(record)

        return record

    def _dispatch(self, record: AlertRecord) -> None:
        for notifier in self._notifiers:
            try:
                ok = notifier.send(record)
                if ok:
                    record.notified = True
            except Exception:
                pass
