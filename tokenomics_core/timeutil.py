"""UTC time-bucketing helpers. All keys are computed in UTC so a ledger written
in one timezone rolls up identically everywhere (matches the Rust ``chrono`` Utc
formatting used by the companion Rust CLI and the TS ``@openclaw/tokenomics``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_DAY = timedelta(days=1)


def day_key(d: datetime) -> str:
    """``YYYY-MM-DD`` (UTC)."""
    d = d.astimezone(timezone.utc)
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def month_key(d: datetime) -> str:
    """``YYYY-MM`` (UTC)."""
    d = d.astimezone(timezone.utc)
    return f"{d.year:04d}-{d.month:02d}"


def year_key(d: datetime) -> str:
    """``YYYY`` (UTC)."""
    return f"{d.astimezone(timezone.utc).year:04d}"


def hour_key(d: datetime) -> str:
    """``YYYY-MM-DD HH:00`` (UTC)."""
    d = d.astimezone(timezone.utc)
    return f"{day_key(d)} {d.hour:02d}:00"


def week_key(d: datetime) -> str:
    """``YYYY-Www`` (UTC, ISO-8601). The week-year can differ from the calendar
    year near January/December, exactly as ``chrono::IsoWeek`` produces."""
    iso = d.astimezone(timezone.utc).isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def start_of_day_utc(d: datetime) -> datetime:
    """Midnight UTC at the start of ``d``'s day."""
    d = d.astimezone(timezone.utc)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def parse_date(s: str, end_of_day: bool = False) -> datetime:
    """Parse ``YYYY-MM-DD`` to a UTC datetime. ``end_of_day`` snaps to 23:59:59."""
    s = s.strip()
    try:
        base = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise ValueError(f"bad date {s!r} (want YYYY-MM-DD)") from e
    return base.replace(hour=23, minute=59, second=59) if end_of_day else base


def days_between(since: datetime, until: datetime) -> int:
    """Days between two datetimes (>= 1)."""
    return max(1, (until - since).days)
