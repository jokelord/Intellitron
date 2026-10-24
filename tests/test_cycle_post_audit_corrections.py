import asyncio
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agent.session_hub import AgentSessionHub
from backend.api.cycle import router as cycle_router
from backend.cycle.executor import CycleExecutor
from backend.cycle.models import CycleStartRequest, CycleStepRequest, ScheduledMessageRequest
from backend.cycle.scheduler import ScheduledTrack
from backend.session_hub_runtime import SessionHubRuntime


def _item(*, schedule_id="report", at="12:00:00", recurrence="once", day_count=1):
    return ScheduledMessageRequest(
        id=schedule_id,
        time=at,
        recurrence=recurrence,
        day_count=day_count,
        message="report",
    )


def test_session_hub_runtime_accepts_cycle_and_describes_all_supported_kinds():
    runtime = SessionHubRuntime()
    entry = runtime.register(session_id="cycle-entry", kind="cycle", hub=AgentSessionHub())
    assert entry.kind == "cycle"
    with pytest.raises(ValueError, match="chat.*agent.*cycle"):
        runtime.register(session_id="invalid-kind", kind="other", hub=AgentSessionHub())  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_cycle_stop_abandons_due_occurrences_before_terminalization():
    now = [datetime(2026, 9, 17, 11, 59, tzinfo=UTC)]
    executor = CycleExecutor(None, None, now=lambda: now[0], heartbeat_seconds=0.001)
    record = executor.ensure_run(
        CycleStartRequest(
            cycle_run_id="stop-due",
            timezone="UTC",
            steps=[CycleStepRequest(type="wait", wait=0.05)],
            scheduled_messages=[_item()],
        )
    )
    record.schedule.collect_due(now[0] + timedelta(minutes=1))
    assert record.schedule.has_due  # establish that work was dispatchable
    assert executor.stop("stop-due") is True
    assert not record.schedule.has_due
    assert record.schedule.drain_due() == ()
    await asyncio.wait_for(record.producer_task, 1)


def test_registry_distinguishes_expired_retention_from_unavailable_run():
    clock = [0.0]
    module = __import__("backend.cycle.run_registry", fromlist=["CycleRunRegistry"])
    registry = module.CycleRunRegistry(retention_seconds=2, max_records=3, clock=lambda: clock[0])
    record, _ = registry.admit("expired", "fingerprint")
    record.finish("completed", {"type": "cycle_completed"})
    clock[0] = 3
    with pytest.raises(LookupError) as expired_error:
        registry.resolve("expired")
    assert type(expired_error.value).__name__ == "CycleRunExpired"
    assert expired_error.value.args == ("cycle_run_expired",)
    with pytest.raises(LookupError) as unavailable_error:
        module.CycleRunRegistry().resolve("after-backend-restart")
    assert type(unavailable_error.value).__name__ == "CycleRunUnavailable"
    assert unavailable_error.value.args == ("cycle_run_unavailable",)


def _api_app(executor):
    app = FastAPI()
    app.include_router(cycle_router)
    app.state.cycle_executor = executor
    app.state.camera_service = None
    return app


def test_cycle_api_returns_410_for_retention_expiry_and_404_for_unavailable():
    expired_executor = CycleExecutor(None, None, event_journal_retention_seconds=0.01)
    with TestClient(_api_app(expired_executor)) as client:
        response = client.post("/v1/cycle/start", json={"cycle_run_id": "expired-api", "steps": []})
        assert response.status_code == 200
        time.sleep(0.03)
        expired = client.get("/v1/cycle/expired-api")
        assert expired.status_code == 410
        assert expired.json()["detail"] == "cycle_run_expired"
        expired_events = client.get("/v1/cycle/expired-api/events")
        assert expired_events.status_code == 410
        assert expired_events.json()["detail"] == "cycle_run_expired"

    with TestClient(_api_app(CycleExecutor(None, None))) as client:
        unavailable = client.get("/v1/cycle/after-backend-restart")
        assert unavailable.status_code == 404
        assert unavailable.json()["detail"] == "cycle_run_unavailable"
        unavailable_events = client.get("/v1/cycle/after-backend-restart/events")
        assert unavailable_events.status_code == 404
        assert unavailable_events.json()["detail"] == "cycle_run_unavailable"


@pytest.mark.anyio
async def test_cycle_timeline_exposes_next_deadline_and_approved_busy_reason():
    now = [datetime(2026, 9, 17, 11, 59, tzinfo=UTC)]
    executor = CycleExecutor(None, None, now=lambda: now[0], heartbeat_seconds=0.001)
    record = executor.ensure_run(
        CycleStartRequest(
            cycle_run_id="observable",
            timezone="UTC",
            steps=[],
            scheduled_messages=[_item(at="12:00:00", recurrence="daily", day_count=2)],
        )
    )
    await asyncio.sleep(0)
    started = record.hub.records[0][1].payload
    assert started["next_scheduled_for"] == "2026-09-17T12:00:00+00:00"
    assert record.snapshot()["next_scheduled_for"] == "2026-09-17T12:00:00+00:00"
    record.active_track = "ordinary"
    now[0] = datetime(2026, 9, 17, 12, 0, 1, tzinfo=UTC)
    executor._collect_due(record)
    queued = record.hub.records[-1][1].payload
    assert queued["reason"] == "ordinary_dispatch_busy"
    assert queued["next_scheduled_for"] == "2026-09-18T12:00:00+00:00"
    executor.stop("observable")
    await asyncio.wait_for(record.producer_task, 1)


def test_provider_neutral_abort_does_not_call_sglang_for_vllm_or_openai_proxy():
    from backend.llm_service import abort_request_by_id, register_upstream_stream, unregister_upstream_stream

    for provider in ("vllm", "openai_proxy"):
        request_id = f"post-audit-{provider}"
        closer = MagicMock()
        registration = register_upstream_stream(request_id, provider, closer)
        try:
            with patch("backend.llm_service.abort_sglang_request") as abort_sglang:
                assert abort_request_by_id(request_id, "http://sglang", fallback_to_abort_all=False) is True
            abort_sglang.assert_not_called()
            closer.assert_called_once_with()
        finally:
            unregister_upstream_stream(request_id, registration)
