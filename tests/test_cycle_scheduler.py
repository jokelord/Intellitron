"""Tests for the Cycle scheduled-message request contract and pure scheduler."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from backend.cycle.models import CycleStartRequest, CycleStepRequest, ScheduledMessageRequest
from backend.cycle.scheduler import ScheduledTrack, first_occurrence, parse_hhmmss, validate_timezone


def scheduled_item(**overrides) -> ScheduledMessageRequest:
    payload = {
        "id": "morning-report",
        "time": "08:30:00",
        "recurrence": "once",
        "message": "Report the current scene",
    }
    payload.update(overrides)
    return ScheduledMessageRequest(**payload)


@pytest.mark.parametrize("value", ["00:00:00", "08:30:59", "23:59:59"])
def test_scheduled_message_accepts_exact_hhmmss(value):
    assert scheduled_item(time=value).time == value
    assert parse_hhmmss(value).isoformat() == value


@pytest.mark.parametrize(
    "value", ["8:30:00", "08:30", "24:00:00", "12:60:00", "12:00:60", "12:00:00 "]
)
def test_scheduled_message_rejects_non_exact_hhmmss(value):
    with pytest.raises(ValidationError):
        scheduled_item(time=value)


def test_scheduled_message_requires_stable_id_and_nonempty_payload():
    with pytest.raises(ValidationError):
        scheduled_item(id="has spaces")
    with pytest.raises(ValidationError):
        scheduled_item(message="", photo=[])
    assert scheduled_item(message="", photo=[0]).photo == [0]


def test_recurrence_validates_day_count_per_item():
    assert scheduled_item(recurrence="once").day_count == 1
    assert scheduled_item(recurrence="daily", day_count=3).day_count == 3
    with pytest.raises(ValidationError):
        scheduled_item(recurrence="once", day_count=2)
    with pytest.raises(ValidationError):
        scheduled_item(recurrence="daily", day_count=0)


def test_start_request_keeps_scheduled_track_top_level_and_legacy_compatible():
    legacy = CycleStartRequest(steps=[CycleStepRequest(type="wait", wait=1)])
    assert legacy.scheduled_messages == []
    assert legacy.timezone is None

    request = CycleStartRequest(
        cycle_run_id="cycle-run-1",
        session_id="cycle-run-1",
        timezone="Asia/Shanghai",
        steps=[],
        scheduled_messages=[scheduled_item()],
    )
    assert request.cycle_run_id == "cycle-run-1"
    assert request.scheduled_messages[0].id == "morning-report"

    from backend.cycle.executor import CycleExecutor
    from backend.cycle.run_registry import CycleRunConflict
    # Identity is an admission conflict (HTTP 409), not a schema error (422).
    with pytest.raises(CycleRunConflict, match="cycle_run_identity_mismatch"):
        CycleExecutor(None, None).ensure_run(CycleStartRequest(
            cycle_run_id="new-run",
            session_id="legacy-run",
            timezone="Asia/Shanghai",
            steps=[],
        ))
    with pytest.raises(ValidationError):
        CycleStartRequest(steps=[], scheduled_messages=[scheduled_item()])
    with pytest.raises(ValidationError):
        CycleStartRequest(steps=[], timezone="Not/A_Zone")


def test_ordinary_cycle_steps_remain_limited_to_existing_types():
    with pytest.raises(ValidationError):
        CycleStepRequest(type="scheduled_message", message="not nested")


def test_timezone_validation_requires_iana_zoneinfo_name():
    assert validate_timezone("Asia/Shanghai").key == "Asia/Shanghai"
    with pytest.raises(ValueError, match="IANA"):
        validate_timezone("+08:00")


def test_first_occurrence_uses_today_when_target_equals_start_time():
    zone = validate_timezone("Asia/Shanghai")
    started_at = datetime(2026, 9, 17, 8, 30, tzinfo=zone)
    occurrence = first_occurrence(started_at, scheduled_item(), zone)
    assert occurrence == started_at


def test_first_occurrence_moves_to_next_local_day_after_target_passes():
    zone = validate_timezone("Asia/Shanghai")
    started_at = datetime(2026, 9, 17, 8, 30, 1, tzinfo=zone)
    occurrence = first_occurrence(started_at, scheduled_item(), zone)
    assert occurrence == datetime(2026, 9, 18, 8, 30, tzinfo=zone)


def test_first_occurrence_skips_nonexistent_dst_wall_time_without_consuming_day():
    zone = validate_timezone("America/New_York")
    started_at = datetime(2026, 3, 8, 0, 0, tzinfo=zone)
    item = scheduled_item(time="02:30:00", recurrence="daily", day_count=2)
    occurrence = first_occurrence(started_at, item, zone)
    assert occurrence == datetime(2026, 3, 9, 2, 30, tzinfo=zone)


def test_first_occurrence_uses_first_fold_for_ambiguous_dst_wall_time():
    zone = validate_timezone("America/New_York")
    started_at = datetime(2026, 11, 1, 0, 0, tzinfo=zone)
    occurrence = first_occurrence(started_at, scheduled_item(time="01:30:00"), zone)
    assert occurrence.fold == 0
    assert occurrence.utcoffset() == timedelta(hours=-4)


def test_scheduled_track_orders_simultaneous_occurrences_and_is_idempotent():
    zone = validate_timezone("Asia/Shanghai")
    started_at = datetime(2026, 9, 17, 8, 0, tzinfo=zone)
    track = ScheduledTrack(
        [
            scheduled_item(id="second", time="08:30:00"),
            scheduled_item(id="first", time="08:30:00"),
        ],
        timezone=zone,
        started_at=started_at,
    )
    now = datetime(2026, 9, 17, 8, 30, tzinfo=zone)

    queued = track.collect_due(now)
    assert [item.schedule_id for item in queued] == ["second", "first"]
    assert track.collect_due(now) == ()
    assert [item.schedule_id for item in track.drain_due()] == ["second", "first"]
    assert track.drain_due() == ()


def test_scheduled_track_emits_finite_daily_occurrences_after_busy_gap():
    zone = validate_timezone("Asia/Shanghai")
    started_at = datetime(2026, 9, 17, 7, 0, tzinfo=zone)
    track = ScheduledTrack(
        [scheduled_item(recurrence="daily", day_count=3, time="08:00:00")],
        timezone=zone,
        started_at=started_at,
    )

    queued = track.collect_due(datetime(2026, 9, 20, 9, 0, tzinfo=zone))
    assert [item.occurrence_index for item in queued] == [1, 2, 3]
    assert [item.scheduled_for.date().isoformat() for item in queued] == [
        "2026-09-17",
        "2026-09-18",
        "2026-09-19",
    ]
    assert track.next_deadline is None


def test_scheduled_track_requires_aware_clock_values():
    item = scheduled_item()
    zone = validate_timezone("Asia/Shanghai")
    with pytest.raises(ValueError, match="aware"):
        ScheduledTrack([item], timezone=zone, started_at=datetime(2026, 9, 17, 8, 0))
    track = ScheduledTrack(
        [item], timezone=zone, started_at=datetime(2026, 9, 17, 8, 0, tzinfo=UTC)
    )
    with pytest.raises(ValueError, match="aware"):
        track.collect_due(datetime(2026, 9, 17, 9, 0))
