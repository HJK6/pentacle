"""Pure recurrence math for durable recurring schedules.

A rule is ``{"every": "daily|weekdays|weekly", "at_time": "HH:MM", "days": [...],
"tz": "<IANA zone>"}``. Occurrences are wall-clock times in the rule's zone:
a wall time inside a spring-forward gap resolves to the first valid instant
after the gap starts (the transition itself); an ambiguous fall-back wall time
resolves to its first occurrence (``fold=0``).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "America/Chicago"
EVERY = ("daily", "weekdays", "weekly")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_AT_TIME_RE = re.compile(r"([01]\d|2[0-3]):([0-5]\d)\Z")


class RecurrenceError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise RecurrenceError("invalid_timezone", f"unknown time zone {name!r}") from exc


def normalize(rule: dict) -> dict:
    """Return the canonical ``{every, at_time, days, tz}`` or raise RecurrenceError."""
    every = str(rule.get("every") or "").strip().lower()
    if every not in EVERY:
        raise RecurrenceError("invalid_recurrence", f"every must be one of {', '.join(EVERY)}")
    at_time = str(rule.get("at_time") or "").strip()
    if _AT_TIME_RE.fullmatch(at_time) is None:
        raise RecurrenceError("invalid_recurrence", "at_time must be HH:MM (24-hour)")
    raw_days = rule.get("days") or []
    if isinstance(raw_days, str):
        raw_days = [part for part in raw_days.split(",") if part.strip()]
    days = [str(day).strip().lower() for day in raw_days]
    if any(day not in DAYS for day in days) or len(set(days)) != len(days):
        raise RecurrenceError("invalid_recurrence", f"days must be distinct of {', '.join(DAYS)}")
    if every == "weekly":
        if not days:
            raise RecurrenceError("invalid_recurrence", "weekly requires days")
        days = [day for day in DAYS if day in days]
    elif days:
        raise RecurrenceError("invalid_recurrence", "days apply only to weekly")
    tz = str(rule.get("tz") or DEFAULT_TZ).strip()
    zone(tz)
    return {"every": every, "at_time": at_time, "days": days, "tz": tz}


def _weekdays(rule: dict) -> frozenset[int]:
    if rule["every"] == "weekdays":
        return frozenset(range(5))
    if rule["every"] == "weekly":
        return frozenset(DAYS.index(day) for day in rule["days"])
    return frozenset(range(7))


def _resolve(day: date, hour: int, minute: int, tz: ZoneInfo) -> datetime:
    wall = datetime(day.year, day.month, day.day, hour, minute)
    first = wall.replace(tzinfo=tz, fold=0)
    as_utc = first.astimezone(timezone.utc)
    if as_utc.astimezone(tz).replace(tzinfo=None) == wall:
        return as_utc  # exists (an ambiguous wall time keeps fold=0)
    # Spring-forward gap: walk from the later offset's reading to the transition.
    probe = wall.replace(tzinfo=tz, fold=1).astimezone(timezone.utc)
    after = first.utcoffset()
    while probe.astimezone(tz).utcoffset() == after:
        probe += timedelta(minutes=1)
    return probe


def next_occurrence(rule: dict, after_utc: datetime) -> datetime:
    """First occurrence strictly after ``after_utc`` (aware), as an aware UTC datetime."""
    rule = normalize(rule)
    tz = zone(rule["tz"])
    hour, minute = (int(part) for part in rule["at_time"].split(":"))
    allowed = _weekdays(rule)
    start = after_utc.astimezone(tz).date() - timedelta(days=1)
    for offset in range(0, 16):
        day = start + timedelta(days=offset)
        if day.weekday() not in allowed:
            continue
        candidate = _resolve(day, hour, minute, tz)
        if candidate > after_utc:
            return candidate
    raise RecurrenceError("invalid_recurrence", "no occurrence found")  # unreachable for valid rules


def describe(rule: dict) -> str:
    """Human rendering in the rule's zone, e.g. ``weekly mon,wed at 08:00 America/Chicago``."""
    days = f" {','.join(rule['days'])}" if rule.get("days") else ""
    return f"{rule['every']}{days} at {rule['at_time']} {rule['tz']}"
