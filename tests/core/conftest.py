"""Shared helpers. Tests run on Tallinn wall-clock time (it has a DST change to test)."""

from datetime import datetime
from zoneinfo import ZoneInfo

TLL = ZoneInfo("Europe/Tallinn")


def at(day: int, hour: int, minute: int = 0, second: int = 0, month: int = 9) -> datetime:
    return datetime(2026, month, day, hour, minute, second, tzinfo=TLL)
