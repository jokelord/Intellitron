"""Pure, timezone-aware planning primitives for Cycle's scheduled track.

This module deliberately contains no asyncio, HTTP, timeline, or LLM code.
The executor will own the scheduler clock and dispatch arbiter; this module only
turns an explicit browser timezone plus wall-clock values into deterministic,
idempotent scheduled occurrences.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time as wall_time, timedelta
from typing import Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.cycle.models import ScheduledMessageRequest


def validate_timezone(name: str) -> ZoneInfo:
    """Resolve an explicit IANA timezone name without host-timezone fallback."""
    if not isinstance(name, str) or not name or name.startswith(("+", "-")):
        raise ValueError("timezone must be a valid IANA timezone")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc


def parse_hhmmss(value: str) -> wall_time:
    """Parse the request model's already-strict ``HH:mm:ss`` value."""
    try:
        return wall_time.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("time must use exact HH:mm:ss format") from exc


def _require_aware(value: datetime, *, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be an aware datetime")


def _local_wall_time_or_none(
    local_date: date, target_time: wall_time, zone: ZoneInfo
) -> Optional[datetime]:
    """Return fold=0 local time, or None when DST makes it nonexistent."""
    candidate = datetime.combine(local_date, target_time, tzinfo=zone).replace(fold=0)
    round_tripped = candidate.astimezone(UTC).astimezone(zone)
    if (
        round_tripped.date() != local_date
        or round_tripped.timetz().replace(tzinfo=None) != target_time
    ):
        return None
    return candidate


def _next_valid_local_wall_time(
    start_date: date, target_time: wall_time, zone: ZoneInfo
) -> datetime:
    candidate_date = start_date
    while True:
        candidate = _local_wall_time_or_none(candidate_date, target_time, zone)
        if candidate is not None:
            return candidate
        candidate_date += timedelta(days=1)


def first_occurrence(
    started_at: datetime, item: ScheduledMessageRequest, zone: ZoneInfo
) -> datetime:
    """Find the first eligible local occurrence for an item.

    Equality with the start instant is eligible today. A passed target, or a
    nonexistent DST local time, advances to the next valid local date. Skipped
    DST dates do not consume a daily occurrence index.
    """
    _require_aware(started_at, name="started_at")
    local_started_at = started_at.astimezone(zone)
    target_time = parse_hhmmss(item.time)
    candidate = _local_wall_time_or_none(local_started_at.date(), target_time, zone)
    if candidate is not None and candidate >= local_started_at:
        return candidate
    return _next_valid_local_wall_time(
        local_started_at.date() + timedelta(days=1), target_time, zone
    )


@dataclass(frozen=True)
class ScheduledOccurrence:
    """One concrete, finite dispatch candidate from a scheduled definition."""

    schedule_id: str
    occurrence_index: int
    scheduled_for: datetime
    editor_order: int
    scheduled_local_date: date
    item: ScheduledMessageRequest = field(compare=False, repr=False)

    @property
    def key(self) -> tuple[str, int, date]:
        """Stable idempotency key scoped to one Cycle run's scheduled track."""
        return (self.schedule_id, self.occurrence_index, self.scheduled_local_date)

    @property
    def ordering_key(self) -> tuple[datetime, int, int, str]:
        return (
            self.scheduled_for,
            self.editor_order,
            self.occurrence_index,
            self.schedule_id,
        )


class ScheduledTrack:
    """Finite next-occurrence planner plus deterministic due queue."""

    def __init__(
        self,
        items: Iterable[ScheduledMessageRequest],
        *,
        timezone: ZoneInfo,
        started_at: datetime,
    ) -> None:
        _require_aware(started_at, name="started_at")
        self._timezone = timezone
        self._next_by_schedule_id: dict[str, ScheduledOccurrence] = {}
        self._due_by_key: dict[tuple[str, int, date], ScheduledOccurrence] = {}
        self._dispatched_keys: set[tuple[str, int, date]] = set()

        for editor_order, item in enumerate(items):
            if item.id in self._next_by_schedule_id:
                raise ValueError("scheduled message ids must be unique")
            first = first_occurrence(started_at, item, timezone)
            self._next_by_schedule_id[item.id] = ScheduledOccurrence(
                schedule_id=item.id,
                occurrence_index=1,
                scheduled_for=first,
                editor_order=editor_order,
                scheduled_local_date=first.date(),
                item=item,
            )

    @property
    def next_deadline(self) -> Optional[datetime]:
        """The earliest still-planned deadline, or None once planning is done."""
        if not self._next_by_schedule_id:
            return None
        return min(
            self._next_by_schedule_id.values(), key=lambda value: value.ordering_key
        ).scheduled_for

    @property
    def is_complete(self) -> bool:
        """Whether neither future nor queued scheduled work remains."""
        return not self._next_by_schedule_id and not self._due_by_key

    @property
    def has_due(self) -> bool:
        """Whether an occurrence is queued but has not entered dispatch."""
        return bool(self._due_by_key)

    def abandon_due(self) -> tuple[ScheduledOccurrence, ...]:
        """Drop queued, undelivered occurrences when the Cycle is stopped."""
        abandoned = tuple(
            sorted(self._due_by_key.values(), key=lambda value: value.ordering_key)
        )
        self._due_by_key.clear()
        return abandoned

    def collect_due(self, now: datetime) -> tuple[ScheduledOccurrence, ...]:
        """Queue every newly due occurrence once, ordered deterministically."""
        _require_aware(now, name="now")
        queued: list[ScheduledOccurrence] = []
        while self._next_by_schedule_id:
            occurrence = min(
                self._next_by_schedule_id.values(),
                key=lambda value: value.ordering_key,
            )
            if occurrence.scheduled_for > now:
                break
            self._next_by_schedule_id.pop(occurrence.schedule_id)
            if (
                occurrence.key not in self._due_by_key
                and occurrence.key not in self._dispatched_keys
            ):
                self._due_by_key[occurrence.key] = occurrence
                queued.append(occurrence)
            successor = self._successor(occurrence)
            if successor is not None:
                self._next_by_schedule_id[successor.schedule_id] = successor
        return tuple(sorted(queued, key=lambda value: value.ordering_key))

    def drain_due(self) -> tuple[ScheduledOccurrence, ...]:
        """Return queued work once in scheduler ordering and mark it dispatched."""
        drained = tuple(
            sorted(self._due_by_key.values(), key=lambda value: value.ordering_key)
        )
        self._dispatched_keys.update(occurrence.key for occurrence in drained)
        self._due_by_key.clear()
        return drained

    def _successor(
        self, occurrence: ScheduledOccurrence
    ) -> Optional[ScheduledOccurrence]:
        item = occurrence.item
        if item.recurrence != "daily" or occurrence.occurrence_index >= item.day_count:
            return None
        scheduled_for = _next_valid_local_wall_time(
            occurrence.scheduled_local_date + timedelta(days=1),
            parse_hhmmss(item.time),
            self._timezone,
        )
        return ScheduledOccurrence(
            schedule_id=item.id,
            occurrence_index=occurrence.occurrence_index + 1,
            scheduled_for=scheduled_for,
            editor_order=occurrence.editor_order,
            scheduled_local_date=scheduled_for.date(),
            item=item,
        )
