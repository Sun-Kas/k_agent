"""Timezone-aware next-occurrence calculation for the small v1 rule set."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from access_layer.scheduled_tasks.models import ScheduledTaskInput


UTC = timezone.utc


class ScheduleValidationError(ValueError):
    """A schedule cannot produce a valid future occurrence."""


def validate_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleValidationError(f"Unknown IANA timezone: {name}") from exc


def compute_next_run(rule: ScheduledTaskInput, after: datetime) -> datetime | None:
    """Return the first valid UTC occurrence strictly after ``after``.

    Round-tripping through UTC detects nonexistent local wall times during a DST
    jump. Ambiguous times use ``fold=0`` so a repeated wall clock fires once.
    """

    zone = validate_timezone(rule.timezone)
    after_utc = _as_utc(after)
    local_after = after_utc.astimezone(zone)
    hour, minute = (int(part) for part in rule.local_time.split(":"))

    if rule.schedule_kind == "once":
        try:
            local_day = date.fromisoformat(rule.local_date or "")
        except ValueError as exc:
            raise ScheduleValidationError("localDate must use YYYY-MM-DD") from exc
        candidate = _valid_local_candidate(local_day, hour, minute, zone)
        if candidate is None:
            raise ScheduleValidationError("The selected local time does not exist")
        candidate_utc = candidate.astimezone(UTC)
        if candidate_utc <= after_utc:
            raise ScheduleValidationError("A once schedule must be in the future")
        return candidate_utc

    for offset in range(0, 370):
        local_day = local_after.date() + timedelta(days=offset)
        if rule.schedule_kind == "weekly" and local_day.isoweekday() not in rule.weekdays:
            continue
        candidate = _valid_local_candidate(local_day, hour, minute, zone)
        if candidate is None:
            continue
        candidate_utc = candidate.astimezone(UTC)
        if candidate_utc > after_utc:
            return candidate_utc
    raise ScheduleValidationError("Unable to find the next schedule occurrence")


def compute_following_run(rule: ScheduledTaskInput, scheduled_for: datetime) -> datetime | None:
    """Advance from a theoretical trigger rather than completion wall time."""

    if rule.schedule_kind == "once":
        return None
    return compute_next_run(rule, _as_utc(scheduled_for) + timedelta(microseconds=1))


def _valid_local_candidate(day: date, hour: int, minute: int, zone: ZoneInfo) -> datetime | None:
    local = datetime.combine(day, time(hour, minute), tzinfo=zone).replace(fold=0)
    # A nonexistent time normalizes to a different wall time after a UTC round trip.
    round_trip = local.astimezone(UTC).astimezone(zone)
    if round_trip.replace(tzinfo=None) != local.replace(tzinfo=None):
        return None
    return local


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ScheduleValidationError("datetime must be timezone-aware")
    return value.astimezone(UTC)
