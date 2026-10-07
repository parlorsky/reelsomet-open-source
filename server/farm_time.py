"""Timezone helpers for farm scheduling and timestamp serialization."""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

DEFAULT_FARM_TIMEZONE = "Europe/Moscow"


def utcnow_naive() -> datetime:
    """Return current UTC time as a naive datetime (SQLite-compatible)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_farm_zone(timezone_name: str | None) -> ZoneInfo:
    """Resolve a farm timezone with a safe fallback for invalid config."""
    tz_name = (timezone_name or "").strip() or DEFAULT_FARM_TIMEZONE
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning(
            "Unknown farm timezone %r, falling back to %s",
            tz_name,
            DEFAULT_FARM_TIMEZONE,
        )
        return ZoneInfo(DEFAULT_FARM_TIMEZONE)


def to_utc_aware(dt: datetime) -> datetime:
    """Attach UTC tzinfo to a naive UTC datetime."""
    return dt.replace(tzinfo=timezone.utc)


def isoformat_utc(dt: datetime | None) -> str | None:
    """Serialize a naive UTC datetime as an offset-aware ISO string."""
    if dt is None:
        return None
    return to_utc_aware(dt).isoformat()


def utc_naive_to_local(dt: datetime, timezone_name: str | None) -> datetime:
    """Convert a naive UTC datetime to an aware local datetime."""
    return to_utc_aware(dt).astimezone(get_farm_zone(timezone_name))


def local_day_bounds_utc(
    now_utc: datetime,
    timezone_name: str | None,
) -> tuple[datetime, datetime]:
    """Return the current local day bounds as UTC-naive datetimes."""
    local_now = utc_naive_to_local(now_utc, timezone_name)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc).replace(tzinfo=None),
        local_end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def compute_future_slots_utc(
    times_str: list[str],
    now_utc: datetime,
    timezone_name: str | None,
    jitter_std: int,
    days: int = 2,
) -> list[datetime]:
    """Compute future posting slots in UTC-naive storage format."""
    tz = get_farm_zone(timezone_name)
    now_local = to_utc_aware(now_utc).astimezone(tz)
    local_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    slots: list[datetime] = []

    for day_offset in range(days):
        base_local = local_today + timedelta(days=day_offset)
        for time_str in times_str:
            try:
                hour, minute = (int(x) for x in time_str.split(":"))
            except (ValueError, AttributeError):
                continue

            candidate_local = base_local.replace(
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
            if jitter_std > 0:
                candidate_local += timedelta(
                    seconds=int(random.gauss(0, jitter_std)),
                )
            if candidate_local <= now_local:
                continue

            slots.append(
                candidate_local.astimezone(timezone.utc).replace(tzinfo=None),
            )

    slots.sort()
    return slots
