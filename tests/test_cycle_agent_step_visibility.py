import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from backend.cycle.executor import CycleExecutor
from backend.cycle.models import (
    CycleStartRequest,
    CycleStepRequest,
    ScheduledMessageRequest,
)


VISIBLE_AGENT_TYPES = {
    "agent_step",
    "orchestration_plan",
    "orchestration_update",
    "orchestration_done",
    "timeline_event",
}


def payloads(record):
    return [record.payload for _, record in record.hub.records]


class VisibilityRunner:
    def __init__(self):
        self.stream_kwargs = []

    def stage_agent_request_admission(self, session_id, *_args, **_kwargs):
        return SimpleNamespace(session_id=session_id)

    async def run_stream(self, session_id, message, mode, **kwargs):
        self.stream_kwargs.append(kwargs)
        kwargs["on_run_registered"](kwargs["admission_stage"], frozenset())
        yield 'data: {"type":"session_start","session_id":"agent-session"}\n\n'
        yield (
            'data: {"type":"agent_step","step_type":"tool_call",'
            '"content":"inspect","raw_tool_call":{"name":"inspect"},'
            '"name":"inspect","input":{"path":"/tmp/item"},'
            '"partial":true,"dispatch_status":"running"}\n\n'
        )
        yield 'data: {"type":"orchestration_plan","stage":1,"dispatch_fields":{"child_id":"child-1"}}\n\n'
        yield 'data: {"type":"orchestration_update","stage":1,"status":"running"}\n\n'
        yield 'data: {"type":"orchestration_done","stage":1,"status":"completed"}\n\n'
        yield (
            'data: {"type":"timeline_event","event":{"kind":"message_delta",'
            '"message_id":"agent-message","text":"working"}}\n\n'
        )
        yield 'data: {"type":"content","content":"reply"}\n\n'
        yield 'data: {"type":"done"}\n\n'

    def rollback_agent_request_stage(self, _stage):
        pass


def make_executor(runner):
    return CycleExecutor(
        runner,
        None,
        now=lambda: datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC),
        heartbeat_seconds=0.001,
    )


def make_request(
    *, cycle_run_id, show_agent_steps=False, enable_thinking=False, scheduled=False
):
    schedule = []
    steps = [CycleStepRequest(type="message", message="ordinary")] if not scheduled else []
    if scheduled:
        schedule = [
            ScheduledMessageRequest(
                id="scheduled-1",
                time="12:00:00",
                recurrence="once",
                message="scheduled",
            )
        ]
    return CycleStartRequest(
        cycle_run_id=cycle_run_id,
        steps=steps,
        agent_mode=True,
        timezone="UTC" if scheduled else None,
        scheduled_messages=schedule,
        show_agent_steps=show_agent_steps,
        enable_thinking=enable_thinking,
    )


@pytest.mark.anyio
async def test_cycle_start_defaults_show_agent_steps_to_false_and_started_event_records_it():
    request = CycleStartRequest(steps=[])
    assert request.show_agent_steps is False
    assert request.enable_thinking is False

    runner = VisibilityRunner()
    executor = make_executor(runner)
    record = executor.ensure_run(make_request(cycle_run_id="visibility-default"))
    await asyncio.wait_for(record.producer_task, 1)

    started = next(item for item in payloads(record) if item["type"] == "cycle_started")
    assert started["show_agent_steps"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("scheduled", [False, True])
async def test_enabled_cycle_forwards_agent_operations_for_both_tracks(scheduled):
    runner = VisibilityRunner()
    executor = make_executor(runner)
    record = executor.ensure_run(
        make_request(
            cycle_run_id=f"visibility-enabled-{scheduled}",
            show_agent_steps=True,
            scheduled=scheduled,
        )
    )
    await asyncio.wait_for(record.producer_task, 1)

    events = payloads(record)
    visible = [event for event in events if event["type"] in VISIBLE_AGENT_TYPES]
    assert [event["type"] for event in visible] == [
        "agent_step",
        "orchestration_plan",
        "orchestration_update",
        "orchestration_done",
        "timeline_event",
    ]
    assert all(event["track"] == ("scheduled" if scheduled else "ordinary") for event in visible)
    assert all("path" in event for event in visible)
    if scheduled:
        assert all(event["schedule_id"] == "scheduled-1" for event in visible)
        assert all(event["occurrence_index"] == 1 for event in visible)
    else:
        assert all(event["path"] == [0] for event in visible)

    agent_step = visible[0]
    assert agent_step["step_type"] == "tool_call"
    assert agent_step["raw_tool_call"] == {"name": "inspect"}
    assert agent_step["name"] == "inspect"
    assert agent_step["input"] == {"path": "/tmp/item"}
    assert agent_step["partial"] is True
    assert agent_step["dispatch_status"] == "running"
    assert visible[-1]["event"] == {
        "kind": "message_delta",
        "message_id": "agent-message",
        "text": "working",
    }
    assert any(event["type"] == "llm_done" and event["content"] == "reply" for event in events)
    sequences = [event_record.sequence for _, event_record in record.hub.records]
    assert sequences == sorted(sequences)
    assert all("show_agent_steps" not in kwargs for kwargs in runner.stream_kwargs)


@pytest.mark.anyio
@pytest.mark.parametrize("scheduled", [False, True])
async def test_disabled_cycle_omits_agent_operations_but_keeps_llm_done(scheduled):
    runner = VisibilityRunner()
    executor = make_executor(runner)
    record = executor.ensure_run(
        make_request(
            cycle_run_id=f"visibility-disabled-{scheduled}",
            show_agent_steps=False,
            scheduled=scheduled,
        )
    )
    await asyncio.wait_for(record.producer_task, 1)

    events = payloads(record)
    assert not any(event["type"] in VISIBLE_AGENT_TYPES for event in events)
    assert any(event["type"] == "llm_done" and event["content"] == "reply" for event in events)


@pytest.mark.anyio
@pytest.mark.parametrize("scheduled", [False, True])
@pytest.mark.parametrize("enable_thinking", [False, True])
async def test_cycle_reasoning_toggle_reaches_runner_for_both_tracks(
    scheduled, enable_thinking
):
    runner = VisibilityRunner()
    executor = make_executor(runner)
    record = executor.ensure_run(
        make_request(
            cycle_run_id=f"thinking-{scheduled}-{enable_thinking}",
            enable_thinking=enable_thinking,
            scheduled=scheduled,
        )
    )
    await asyncio.wait_for(record.producer_task, 1)

    assert len(runner.stream_kwargs) == 1
    assert runner.stream_kwargs[0]["enable_thinking"] is enable_thinking
    assert "show_agent_steps" not in runner.stream_kwargs[0]
