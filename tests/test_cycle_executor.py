"""
Tests for backend/cycle/executor.py
"""

import asyncio
import json
import pytest
from unittest.mock import MagicMock

from backend.cycle.executor import CycleExecutor
from backend.cycle.models import CycleStepRequest


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

    async def fake_stream(session_id, message, mode, images=None):
        yield 'data: {"type": "content", "content": "test reply"}\n\n'
        yield 'data: {"type": "done"}\n\n'

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
        max_duration=3600,
    )


class TestWaitStep:
    """测试 wait 步骤"""

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
    async def test_message_step_calls_llm(self, executor):
        steps = [CycleStepRequest(type="message", message="Hello")]
        events_raw = []
        async for chunk in executor.start("s1", steps, agent_mode=True):
            events_raw.append(chunk)
        events = parse_sse_events(events_raw)
        types = [e["type"] for e in events]
        assert "step_begin" in types
        assert "llm_done" in types

    @pytest.mark.asyncio
    async def test_message_with_photo(self, executor, mock_camera_service):
        steps = [CycleStepRequest(type="message", message="Describe", photo=[0])]
        events_raw = []
        async for chunk in executor.start("s1", steps, agent_mode=True):
            events_raw.append(chunk)
        events = parse_sse_events(events_raw)
        types = [e["type"] for e in events]
        assert "photo_captured" in types
        mock_camera_service.capture.assert_called_once_with([0])

    @pytest.mark.asyncio
    async def test_photo_hint_prepended_to_message(self, mock_camera_service):
        """When photos are captured, executor prepends an inline-photo hint
        so the VLM knows images are already in the message."""
        captured_args: list[tuple] = []

        async def spy_stream(session_id, message, mode, images=None):
            captured_args.append((session_id, message, mode, images))
            yield 'data: {"type": "content", "content": "ok"}\n\n'
            yield 'data: {"type": "done"}\n\n'

        spy_runner = MagicMock()
        spy_runner.run_stream = spy_stream

        exec_ = CycleExecutor(spy_runner, mock_camera_service)
        steps = [CycleStepRequest(type="message", message="Describe", photo=[0])]
        async for _ in exec_.start("s1", steps, agent_mode=True):
            pass

        assert len(captured_args) == 1
        _, msg, _, imgs = captured_args[0]
        assert "直接分析" in msg
        assert "load_images" in msg
        assert msg.endswith("Describe")
        assert imgs == ["data:image/jpeg;base64,AAAA"]

    @pytest.mark.asyncio
    async def test_no_photo_hint_without_photos(self):
        """Without photos, message should not be modified."""
        captured_args: list[tuple] = []

        async def spy_stream(session_id, message, mode, images=None):
            captured_args.append((session_id, message, mode, images))
            yield 'data: {"type": "content", "content": "ok"}\n\n'
            yield 'data: {"type": "done"}\n\n'

        spy_runner = MagicMock()
        spy_runner.run_stream = spy_stream

        exec_ = CycleExecutor(spy_runner, MagicMock())
        steps = [CycleStepRequest(type="message", message="Hello")]
        async for _ in exec_.start("s1", steps, agent_mode=True):
            pass

        assert len(captured_args) == 1
        _, msg, _, imgs = captured_args[0]
        assert msg == "Hello"
        assert imgs is None

    @pytest.mark.asyncio
    async def test_llm_done_content(self, executor):
        steps = [CycleStepRequest(type="message", message="Hello")]
        events_raw = []
        async for chunk in executor.start("s1", steps, agent_mode=True):
            events_raw.append(chunk)
        events = parse_sse_events(events_raw)
        done_events = [e for e in events if e["type"] == "llm_done"]
        assert len(done_events) == 1
        assert done_events[0]["content"] == "test reply"


class TestCycleStep:
    """测试嵌套 cycle 步骤"""

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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


class TestDepthLimit:
    """测试嵌套深度限制"""

    @pytest.mark.asyncio
    async def test_exceeds_max_depth(self):
        executor = CycleExecutor(
            agent_runner=MagicMock(),
            camera_service=MagicMock(),
            max_depth=1,
            max_duration=3600,
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

    @pytest.mark.asyncio
    async def test_ends_with_done(self, executor):
        steps = [CycleStepRequest(type="wait", wait=0.01)]
        events_raw = []
        async for chunk in executor.start("s1", steps, agent_mode=False):
            events_raw.append(chunk)
        assert any("data: [DONE]" in line for line in events_raw)
