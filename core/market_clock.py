"""
market_clock.py — US equity market session helpers.

All times expressed in US/Eastern. Handles EDT/EST automatically via pytz.
Covers NYSE/NASDAQ hours; does not account for early-close days (Christmas Eve,
Black Friday, etc.) — treat those as edge cases acceptable for paper trading.
"""
import datetime
from typing import Optional

import pytz

_ET = pytz.timezone("US/Eastern")

# 2026–2027 NYSE holidays (observed dates)
_HOLIDAYS: frozenset = frozenset([
    # 2026
    datetime.date(2026, 1, 1),    # New Year's Day
    datetime.date(2026, 1, 19),   # MLK Jr. Day
    datetime.date(2026, 2, 16),   # Presidents' Day
    datetime.date(2026, 4, 3),    # Good Friday
    datetime.date(2026, 5, 25),   # Memorial Day
    datetime.date(2026, 6, 19),   # Juneteenth
    datetime.date(2026, 7, 3),    # Independence Day (observed)
    datetime.date(2026, 9, 7),    # Labor Day
    datetime.date(2026, 11, 26),  # Thanksgiving
    datetime.date(2026, 12, 25),  # Christmas
    # 2027
    datetime.date(2027, 1, 1),    # New Year's Day
    datetime.date(2027, 1, 18),   # MLK Jr. Day
    datetime.date(2027, 2, 15),   # Presidents' Day
    datetime.date(2027, 3, 26),   # Good Friday
    datetime.date(2027, 5, 31),   # Memorial Day
    datetime.date(2027, 6, 18),   # Juneteenth (observed)
    datetime.date(2027, 7, 5),    # Independence Day (observed)
    datetime.date(2027, 9, 6),    # Labor Day
    datetime.date(2027, 11, 25),  # Thanksgiving
    datetime.date(2027, 12, 24),  # Christmas (observed)
])

_RTH_OPEN  = datetime.time(9, 30)
_RTH_CLOSE = datetime.time(16, 0)
_PRE_OPEN  = datetime.time(4, 0)
_AH_CLOSE  = datetime.time(20, 0)


def now_et() -> datetime.datetime:
    return datetime.datetime.now(_ET)


def is_holiday(d: Optional[datetime.date] = None) -> bool:
    if d is None:
        d = now_et().date()
    return d in _HOLIDAYS


def is_trading_day(d: Optional[datetime.date] = None) -> bool:
    if d is None:
        d = now_et().date()
    return d.weekday() < 5 and not is_holiday(d)


def is_rth() -> bool:
    """True if currently inside regular trading hours (09:30–16:00 ET, Mon–Fri)."""
    now = now_et()
    return (
        is_trading_day(now.date())
        and _RTH_OPEN <= now.time() < _RTH_CLOSE
    )


def is_pre_market() -> bool:
    """True during pre-market (04:00–09:30 ET) on a trading day."""
    now = now_et()
    return (
        is_trading_day(now.date())
        and _PRE_OPEN <= now.time() < _RTH_OPEN
    )


def is_after_hours() -> bool:
    """True during after-hours (16:00–20:00 ET) on a trading day."""
    now = now_et()
    return (
        is_trading_day(now.date())
        and _RTH_CLOSE <= now.time() < _AH_CLOSE
    )


def seconds_to_rth_open() -> Optional[int]:
    """Seconds until next RTH open, or None if already in RTH."""
    if is_rth():
        return None
    now = now_et()
    candidate = now.date()
    # If today's open already passed (or today is holiday/weekend), advance
    open_today = _ET.localize(
        datetime.datetime.combine(candidate, _RTH_OPEN)
    )
    if now >= open_today:
        candidate += datetime.timedelta(days=1)
    # Advance to next trading day
    while not is_trading_day(candidate):
        candidate += datetime.timedelta(days=1)
    next_open = _ET.localize(datetime.datetime.combine(candidate, _RTH_OPEN))
    return max(0, int((next_open - now).total_seconds()))


def session_label() -> str:
    """Human-readable current session name."""
    if is_rth():
        return "RTH"
    if is_pre_market():
        return "PRE"
    if is_after_hours():
        return "AH"
    return "CLOSED"
