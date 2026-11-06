"""Pure checks behind the repair issues: fault, clock drift, maintenance due."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, tzinfo
from typing import Any, Mapping

from .const import CONF_LAST_MAINTENANCE, CONF_MAINTENANCE_INTERVAL
from .parsing import build_device_time, decode_fault_status, extract_value_segment, is_fault
from .sensors import DEVICE_TIME_KEYS

FAULT_BLOCK_KEY = "sg_canopen_fehlerblock"
# Drift tolerated before warning; the device clock has minute resolution.
CLOCK_DRIFT_LIMIT_MINUTES = 5


def active_fault(data: Mapping[str, Any]) -> tuple[str, int | None] | None:
    """Return (message type, error number) while register 121 reports an error."""
    block = data.get(FAULT_BLOCK_KEY)
    if not block:
        return None
    status = extract_value_segment(block.get("value_hex", ""), 2, 2)
    if status is None or not is_fault(status[1]):
        return None
    number = extract_value_segment(block.get("value_hex", ""), 4, 2)
    return decode_fault_status(status[1]), None if number is None else number[1]


def clock_drift_minutes(
    data: Mapping[str, Any], device_timezone: tzinfo, now: datetime
) -> float | None:
    """Return how many minutes the device clock is off, or None if it was not read."""
    if not all(key in data for key in DEVICE_TIME_KEYS):
        return None
    device_time = build_device_time(
        {key: data[key]["value_int"] for key in DEVICE_TIME_KEYS}, device_timezone
    )
    if device_time is None:
        return None
    return abs((now - device_time).total_seconds()) / 60


def add_months(day: date, months: int) -> date:
    """Return day plus months, clamped to the last day of the target month."""
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, monthrange(year, month)[1]))


def next_maintenance(options: Mapping[str, Any]) -> date | None:
    """Return when the next maintenance is due, or None when the reminder is off or unset."""
    months = options.get(CONF_MAINTENANCE_INTERVAL, 0)
    last = options.get(CONF_LAST_MAINTENANCE)
    if not months or not last:
        return None
    return add_months(date.fromisoformat(last), months)
