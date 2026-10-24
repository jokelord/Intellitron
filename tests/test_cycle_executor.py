"""
Tests for backend/cycle/executor.py
"""

import asyncio
import json
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from backend.cycle.executor import CycleExecutor
from backend.cycle.models import CycleStepRequest
from backend.security.path_policy import PathPolicy


def parse_sse_events(raw_lines: list[str]) -> list[dict]:
    """解析 SSE 事件行列表为 dict 列表"""
    events = []
    for line in raw_lines:
        line = line.strip()
        if line.startswith("data: ") and line != "data: [DONE]":
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return events


@pytest.fixture
def mock_agent_runner():
    runner = MagicMock()

    def fake_stage(session_id, *_args, **_kwargs):
        return SimpleNamespace(session_id=session_id, _accepted=False)

    async def fake_stream(
        session_id,
        message,
        mode,
        images=None,
        admission_stage=None,
        on_run_registered=None,
        **_kwargs,
    ):
        if on_run_registered is not None:
            on_run_registered(admission_stage, frozenset())
        if admission_stage is not None:
            admission_stage._accepted = True
        yield 'data: {"type": "content", "content": "test reply"}\n\n'
        yield 'data: {"type": "done"}\n\n'

    runner.stage_agent_request_admission.side_effect = fake_stage
    runner.run_stream = fake_stream
    return runner


@pytest.fixture
def mock_camera_service():
    service = MagicMock()
    service.capture.return_value = [
        {"camera_index": 0, "image_base64": "data:image/jpeg;base64,AAAA"}
    ]
    return service


@pytest.fixture
def executor(mock_agent_runner, mock_camera_service):
    return CycleExecutor(
        agent_runner=mock_agent_runner,
        camera_service=mock_camera_service,
        max_depth=5,
        max_duration=3_600,
    )


class TestWaitStep:
    """测试 wait 步骤"""

    @pytest.mark.anyio
    async def test_wait_step_emits_events(self, executor):
        steps = [CycleStepRequest(type="wait", wait=0.05)]
        events_raw = []
        async for chunk in executor.start("s1", steps, agent_mode=False):
            events_raw.append(chunk)
        events = parse_sse_events(events_raw)
        types = [e["type"] for e in events]
        assert "cycle_started" in types
        assert "step_begin" in types
        assert "step_end" in types
        assert "cycle_completed" in types

    @pytest.mark.anyio
    async def test_wait_step_path(self, executor):
        steps = [CycleStepRequest(type="wait", wait=0.01)]
        events_raw = []
        async for chunk in executor.start("s1", steps, agent_mode=False):
            events_raw.append(chunk)
        events = parse_sse_events(events_raw)
        begin = [e for e in events if e["type"] == "step_begin"][0]
        assert begin["path"] == [0]


class TestMessageStep:
    """测试 message 步骤"""

    @pytest.mark.anyio
    async def test_message_step_calls_llm(self, executor):
        steps = [CycleStepRequest(type="message", message="Hello")]
        events_raw = []
        async for chunk in executor.start("s1", steps, agent_mode=True):
            events_raw.append(chunk)
        events = parse_sse_events(events_raw)
        types = [e["type"] for e in events]
        assert "step_begin" in types
        assert "llm_done" in types

    @pytest.mark.anyio
    async def test_message_with_photo(self, executor, mock_camera_service):
        steps = [CycleStepRequest(type="message", message="Describe", photo=[0])]
        events_raw = []
        async for chunk in executor.start("s1", steps, agent_mode=True):
            events_raw.append(chunk)
        events = parse_sse_events(events_raw)
        types = [e["type"] for e in events]
        assert "photo_captured" in types
        mock_camera_service.capture.assert_called_once_with([0])

    @pytest.mark.anyio
    async def test_llm_done_content(self, executor):
        steps = [CycleStepRequest(type="message", message="Hello")]
        events_raw = []
        async for chunk in executor.start("s1", steps, agent_mode=True):
            events_raw.append(chunk)
        events = parse_sse_events(events_raw)
        done_events = [e for e in events if e["type"] == "llm_done"]
        assert len(done_events) == 1
        assert done_events[0]["content"] == "test reply"

    @pytest.mark.anyio
    async def test_share_context_reuses_same_session_across_rounds(self, executor):
        calls = []

        async def fake_stream(session_id, message, mode, **kwargs):
            calls.append(session_id)
            stage = kwargs["admission_stage"]
            kwargs["on_run_registered"](stage, frozenset())
            stage._accepted = True
            yield 'data: {"type": "content", "content": "ok"}\n\n'
            yield 'data: {"type": "done"}\n\n'

        executor._agent_runner.run_stream = fake_stream

        steps = [
            CycleStepRequest(type="message", message="Round 1"),
            CycleStepRequest(type="message", message="Round 2"),
        ]

        events_raw = []
        async for chunk in executor.start(
            "cycle-shared", steps, agent_mode=True, share_context=True
        ):
            events_raw.append(chunk)

        assert len(calls) == 2
        assert calls[0] == calls[1]
        assert calls[0] != "cycle-shared"
        assert PathPolicy.is_canonical_session_id(calls[0])

    @pytest.mark.anyio
    async def test_without_share_context_uses_distinct_cycle_private_sessions(
        self, executor
    ):
        calls = []

        async def fake_stream(session_id, message, mode, **kwargs):
            calls.append(session_id)
            stage = kwargs["admission_stage"]
            kwargs["on_run_registered"](stage, frozenset())
            stage._accepted = True
            yield 'data: {"type": "session_start", "session_id": "generated-a"}\n\n'
            yield 'data: {"type": "content", "content": "ok"}\n\n'
            yield 'data: {"type": "done"}\n\n'

        executor._agent_runner.run_stream = fake_stream

        steps = [
            CycleStepRequest(type="message", message="Round 1"),
            CycleStepRequest(type="message", message="Round 2"),
        ]

        async for _ in executor.start(
            "cycle-fresh", steps, agent_mode=True, share_context=False
        ):
            pass

        assert len(calls) == 2
        assert calls[0] != calls[1]
        assert all(PathPolicy.is_canonical_session_id(value) for value in calls)

    @pytest.mark.anyio
    async def test_pre_accept_failure_rolls_back_cycle_admission(self):
        """Removing Cycle's rollback finally must leave the fake pending set dirty."""

        class PendingRunner:
            def __init__(self):
                self.pending: set[str] = set()
                self.rolled_back: list[str] = []

            def stage_agent_request_admission(self, session_id, *_args, **_kwargs):
                if session_id in self.pending:
                    raise RuntimeError("session remains pending")
                self.pending.add(session_id)
                return SimpleNamespace(session_id=session_id, _accepted=False)

            async def run_stream(self, *_args, **_kwargs):
                raise RuntimeError("pre-accept startup failed")
                yield  # pragma: no cover

            def rollback_agent_request_stage(self, stage):
                self.pending.discard(stage.session_id)
                self.rolled_back.append(stage.session_id)

        runner = PendingRunner()
        executor = CycleExecutor(runner, MagicMock())
        events = [
            value
            async for value in executor.start(
                str(uuid4()),
                [CycleStepRequest(type="message", message="hello")],
                agent_mode=True,
                share_context=True,
            )
        ]

        assert runner.pending == set()
        assert len(runner.rolled_back) == 1
        assert any('"type": "step_failed"' in event for event in events)
        assert any('"type": "cycle_completed"' in event for event in events)

    @pytest.mark.anyio
    async def test_subscriber_cancellation_retains_admission_until_explicit_stop(self):
        """Detaching does not cancel work; Stop releases the exact admission."""

        entered = asyncio.Event()

        class PendingRunner:
            def __init__(self):
                self.pending: set[str] = set()

            def stage_agent_request_admission(self, session_id, *_args, **_kwargs):
                self.pending.add(session_id)
                return SimpleNamespace(session_id=session_id, _accepted=False)

            async def run_stream(self, *_args, **_kwargs):
                entered.set()
                await asyncio.Event().wait()
                yield  # pragma: no cover

            def rollback_agent_request_stage(self, stage):
                self.pending.discard(stage.session_id)

        runner = PendingRunner()
        executor = CycleExecutor(runner, MagicMock())
        run_id = str(uuid4())

        async def consume():
            async for _ in executor.start(
                run_id,
                [CycleStepRequest(type="message", message="hello")],
                agent_mode=True,
                share_context=True,
            ):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(runner.pending) == 1
        executor.stop(run_id)
        await asyncio.wait_for(executor.registry.resolve(run_id).producer_task, 1)
        assert runner.pending == set()


class TestCycleStep:
    """测试嵌套 cycle 步骤"""

    @pytest.mark.anyio
    async def test_cycle_repeats(self, executor):
        steps = [
            CycleStepRequest(
                type="cycle",
                cycle_count=2,
                cycle_steps=[CycleStepRequest(type="wait", wait=0.01)],
            )
        ]
        events_raw = []
        async for chunk in executor.start("s1", steps, agent_mode=False):
            events_raw.append(chunk)
        events = parse_sse_events(events_raw)
        step_begins = [e for e in events if e["type"] == "step_begin"]
        # 1 cycle step_begin + 2 x wait step_begin = 3
        assert len(step_begins) == 3

    @pytest.mark.anyio
    async def test_multiple_steps_in_sequence(self, executor):
        steps = [
            CycleStepRequest(type="wait", wait=0.01),
            CycleStepRequest(type="wait", wait=0.01),
            CycleStepRequest(type="wait", wait=0.01),
        ]
        events_raw = []
        async for chunk in executor.start("s1", steps, agent_mode=False):
            events_raw.append(chunk)
        events = parse_sse_events(events_raw)
        step_begins = [e for e in events if e["type"] == "step_begin"]
        assert len(step_begins) == 3
        # Check paths
        assert step_begins[0]["path"] == [0]
        assert step_begins[1]["path"] == [1]
        assert step_begins[2]["path"] == [2]


class TestStop:
    """测试停止功能"""

    @pytest.mark.anyio
    async def test_stop_cancels_execution(self, executor):
        steps = [
            CycleStepRequest(type="wait", wait=10),
            CycleStepRequest(type="wait", wait=10),
        ]

        events_raw = []

        async def collect():
            async for chunk in executor.start("s1", steps, agent_mode=False):
                events_raw.append(chunk)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.1)
        executor.stop("s1")
        await task

        events = parse_sse_events(events_raw)
        types = [e["type"] for e in events]
        assert "cycle_stopped" in types
        assert "cycle_completed" not in types

    def test_stop_uses_targeted_request_abort_when_agent_request_is_active(self):
        agent_runner = MagicMock()
        agent_runner.get_current_request_id.return_value = "req-cycle-123"
        executor = CycleExecutor(
            agent_runner=agent_runner,
            camera_service=MagicMock(),
            max_depth=5,
            max_duration=3_600,
        )
        executor._cancel_events["cycle-session"] = asyncio.Event()
        executor._agent_session_ids["cycle-session"] = "agent-session"

        with (
            patch("backend.config.get_config") as mock_get_config,
            patch("backend.llm_service.abort_request_by_id") as mock_abort_request_by_id,
            patch("backend.llm_service.abort_sglang_all") as mock_abort_all,
        ):
            mock_get_config.return_value = MagicMock(
                sglang=MagicMock(host="127.0.0.1", port=30000)
            )

            stopped = executor.stop("cycle-session")

        assert stopped is True
        agent_runner.abort_session.assert_called_once_with("agent-session")
        agent_runner.get_current_request_id.assert_called_once_with("agent-session")
        mock_abort_request_by_id.assert_called_once_with(
            "req-cycle-123",
            "http://127.0.0.1:30000",
            fallback_to_abort_all=False,
        )
        mock_abort_all.assert_not_called()


class TestDepthLimit:
    """测试嵌套深度限制"""

    @pytest.mark.anyio
    async def test_exceeds_max_depth(self):
        executor = CycleExecutor(
            agent_runner=MagicMock(),
            camera_service=MagicMock(),
            max_depth=1,
            max_duration=3_600,
        )
        steps = [
            CycleStepRequest(
                type="cycle",
                cycle_count=1,
                cycle_steps=[
                    CycleStepRequest(
                        type="cycle",
                        cycle_count=1,
                        cycle_steps=[CycleStepRequest(type="wait", wait=0.01)],
                    )
                ],
            )
        ]
        events_raw = []
        async for chunk in executor.start("s1", steps, agent_mode=False):
            events_raw.append(chunk)
        events = parse_sse_events(events_raw)
        types = [e["type"] for e in events]
        assert "cycle_error" in types


class TestDoneTerminator:
    """测试 SSE 终止符"""

    @pytest.mark.anyio
    async def test_ends_with_done(self, executor):
        steps = [CycleStepRequest(type="wait", wait=0.01)]
        events_raw = []
        async for chunk in executor.start("s1", steps, agent_mode=False):
            events_raw.append(chunk)
        assert any("data: [DONE]" in line for line in events_raw)
