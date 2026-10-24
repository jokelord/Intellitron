import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from backend.cycle.executor import CycleExecutor
from backend.cycle.models import CycleStartRequest, CycleStepRequest, ScheduledMessageRequest


def payload(frame):
    return json.loads(frame.strip()[6:])


class Runner:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cleaned = []
        self.aborted = []
        self.sessions = {}

    def stage_agent_request_admission(self, sid, *_args):
        return SimpleNamespace(session_id=sid)

    async def run_stream(self, sid, message, mode, **kwargs):
        self.calls.append((sid, message, kwargs.get('images')))
        kwargs['on_run_registered'](kwargs['admission_stage'], frozenset({'scope'}))
        self.entered.set()
        if message == 'blocked':
            await self.release.wait()
        if message == 'timeout':
            yield 'data: {"type":"error","error":"upstream timeout"}\n\n'
            return
        yield 'data: {"type":"content","content":"reply"}\n\n'

    def rollback_agent_request_stage(self, stage):
        pass

    async def wait_for_agent_request_run_cleanup(self, stage, scopes):
        self.cleaned.append(stage.session_id)

    def abort_session(self, sid):
        self.aborted.append(sid)
        self.release.set()

    def get_current_request_id(self, sid):
        return None


def scheduled(time='12:00:01', **kwargs):
    return ScheduledMessageRequest(id='alarm', time=time, recurrence='once', message='scheduled', **kwargs)


@pytest.mark.anyio
async def test_detach_does_not_cancel_producer_and_duplicate_start_attaches():
    runner = Runner()
    executor = CycleExecutor(runner, None)
    request = CycleStartRequest(cycle_run_id='recover', steps=[CycleStepRequest(type='message', message='blocked')], agent_mode=True)
    assert hasattr(executor, 'ensure_run')
    record = executor.ensure_run(request)
    stream = record.events(0)
    assert payload(await anext(stream))['type'] == 'cycle_started'
    await runner.entered.wait()
    await stream.aclose()
    assert not record.producer_task.done()
    assert executor.ensure_run(request) is record
    runner.release.set()
    await asyncio.wait_for(record.producer_task, 1)
    assert len(runner.calls) == 1
    events = [payload(frame) async for frame in record.events(1) if '[DONE]' not in frame]
    assert any(e['type'] == 'llm_done' and e['content'] == 'reply' for e in events)
    assert events[-1]['type'] == 'cycle_completed'
    assert runner.cleaned == [runner.calls[0][0]]


@pytest.mark.anyio
async def test_due_during_agent_queues_then_runs_before_next_ordinary_and_captures_at_dispatch():
    now = [datetime(2026, 9, 17, 12, tzinfo=UTC)]
    runner = Runner()
    captures = []
    camera = SimpleNamespace(capture=lambda ids: captures.append(ids) or [{'camera_index': 0, 'image_base64': 'image'}])
    executor = CycleExecutor(runner, camera, now=lambda: now[0], heartbeat_seconds=0.01)
    request = CycleStartRequest(cycle_run_id='ordering', timezone='UTC', agent_mode=True, share_context=False,
        steps=[CycleStepRequest(type='message', message='blocked'), CycleStepRequest(type='message', message='ordinary')],
        scheduled_messages=[scheduled(photo=[0])])
    record = executor.ensure_run(request)
    await runner.entered.wait()
    now[0] += timedelta(seconds=2)
    for _ in range(100):
        if any(r.payload['type'] == 'scheduled_message_queued' for _, r in record.hub.records):
            break
        await asyncio.sleep(0.001)
    assert any(r.payload['type'] == 'scheduled_message_queued' for _, r in record.hub.records)
    assert [call[1] for call in runner.calls] == ['blocked']
    assert captures == []
    runner.release.set()
    await asyncio.wait_for(record.producer_task, 1)
    assert [call[1] for call in runner.calls] == ['blocked', 'scheduled', 'ordinary']
    assert len(set(call[0] for call in runner.calls)) == 3
    assert runner.calls[1][2] == ['image']
    assert len(record.agent_session_ids) == 3


@pytest.mark.anyio
async def test_wait_is_not_preempted_and_boundary_tie_prefers_scheduled():
    now = [datetime(2026, 9, 17, 12, tzinfo=UTC)]
    runner = Runner()
    executor = CycleExecutor(runner, None, now=lambda: now[0], heartbeat_seconds=0.001)
    record = executor.ensure_run(CycleStartRequest(cycle_run_id='wait', timezone='UTC', agent_mode=True,
        steps=[CycleStepRequest(type='wait', wait=0.08), CycleStepRequest(type='message', message='ordinary')],
        scheduled_messages=[scheduled()]))
    await asyncio.sleep(0.01)
    now[0] += timedelta(seconds=1)
    await asyncio.sleep(0.01)
    assert runner.calls == []
    await asyncio.wait_for(record.producer_task, 1)
    assert [call[1] for call in runner.calls] == ['scheduled', 'ordinary']


@pytest.mark.anyio
async def test_finite_daily_run_beyond_old_limit_and_local_failure_continues():
    now = [datetime(2026, 9, 17, 12, tzinfo=UTC)]
    runner = Runner()
    executor = CycleExecutor(runner, None, max_duration=0, now=lambda: now[0], heartbeat_seconds=0.001)
    item = ScheduledMessageRequest(id='daily', time='12:00:00', recurrence='daily', day_count=2, message='timeout')
    record = executor.ensure_run(CycleStartRequest(cycle_run_id='days', timezone='UTC', agent_mode=True, steps=[], scheduled_messages=[item]))
    await runner.entered.wait()
    now[0] += timedelta(days=1)
    await asyncio.wait_for(record.producer_task, 1)
    assert len(runner.calls) == 2
    assert record.status == 'completed'
    events = [r.payload for _, r in record.hub.records]
    assert len([e for e in events if e['type'] == 'scheduled_message_failed']) == 2
    assert not any(e['type'] == 'llm_done' for e in events)


@pytest.mark.anyio
async def test_stop_discards_due_work_and_waits_for_owned_cleanup():
    runner = Runner()
    executor = CycleExecutor(runner, None)
    request = CycleStartRequest(cycle_run_id='stop', agent_mode=True, steps=[CycleStepRequest(type='message', message='blocked')])
    assert hasattr(executor, 'ensure_run')
    record = executor.ensure_run(request)
    await runner.entered.wait()
    assert executor.stop('stop')
    await asyncio.wait_for(record.producer_task, 1)
    assert record.status == 'stopped'
    assert runner.aborted == [runner.calls[0][0]]
    assert runner.cleaned == [runner.calls[0][0]]
    assert not executor.stop('stop')


@pytest.mark.anyio
async def test_static_schedule_horizon_rejected_before_producer_creation():
    executor = CycleExecutor(None, None, scheduled_track_max_duration_seconds=1)
    assert hasattr(executor, 'ensure_run')
    request = CycleStartRequest(cycle_run_id='invalid', timezone='UTC', steps=[], scheduled_messages=[scheduled(time='12:00:00')])
    with pytest.raises(ValueError, match='scheduled_horizon_exceeded'):
        executor.ensure_run(request)


@pytest.mark.anyio
async def test_horizon_expires_queued_work_at_safe_boundary_without_preemption():
    now = [datetime(2026, 9, 17, 12, tzinfo=UTC)]
    runner = Runner()
    executor = CycleExecutor(runner, None, now=lambda: now[0], heartbeat_seconds=0.001,
                             scheduled_track_max_duration_seconds=2)
    record = executor.ensure_run(CycleStartRequest(cycle_run_id='expires', timezone='UTC', agent_mode=True,
        steps=[CycleStepRequest(type='message', message='blocked')], scheduled_messages=[scheduled()]))
    await runner.entered.wait()
    now[0] += timedelta(seconds=3)
    await asyncio.sleep(0.01)
    assert not record.producer_task.done()
    assert runner.aborted == []
    runner.release.set()
    await asyncio.wait_for(record.producer_task, 1)
    assert [call[1] for call in runner.calls] == ['blocked']
    assert record.status == 'expired'
    terminal = record.hub.records[-1][1].payload
    assert terminal['type'] == 'cycle_expired'
    assert terminal['scope'] == 'scheduled_track'


@pytest.mark.anyio
async def test_cleanup_captures_inner_worker_scopes_and_survives_producer_cancellation():
    from backend.agent.agent_runner import SessionStore

    runner = Runner()
    runner.sessions = SessionStore()
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
    captured = []
    runner.capture_agent_request_run_scopes = lambda stage: frozenset({'inner-worker'})

    async def cleanup(stage, scopes):
        captured.append(scopes)
        cleanup_entered.set()
        await cleanup_release.wait()

    runner.wait_for_agent_request_run_cleanup = cleanup
    executor = CycleExecutor(runner, None)
    record = executor.ensure_run(CycleStartRequest(cycle_run_id='barrier', agent_mode=True,
        steps=[CycleStepRequest(type='message', message='blocked')]))
    await runner.entered.wait()
    executor.stop('barrier')
    await cleanup_entered.wait()
    record.producer_task.cancel()
    await asyncio.sleep(0.01)
    still_pinned = runner.sessions.cycle_pinned_session_ids()
    still_running = record.finished_at is None
    cleanup_release.set()
    await asyncio.wait_for(record.producer_task, 1)
    assert still_running
    assert still_pinned == frozenset({runner.calls[0][0]})
    assert captured == [frozenset({'scope', 'inner-worker'})]
    assert record.status == 'stopped'
    assert runner.sessions.cycle_pinned_session_ids() == frozenset()
