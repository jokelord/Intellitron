import pytest
import asyncio
import json
import logging
import time
import threading
from contextlib import ExitStack
from copy import deepcopy
from unittest.mock import MagicMock, patch
from backend.agent import agent_runner as agent_runner_module
from backend.agent.agent_runner import (
    AgentRunner,
    AgentSession,
    RawToolCallDebug,
    SecondaryHandoffRequest,
    abort_manager,
    current_session_context,
    run_abort_manager,
    session_request_manager,
)
from backend.api.agent import AgentChatRequest, agent_chat
from backend.agent.local_llm_model import LocalLLMModel
from backend.agent.text_tool_protocol import TextToolProtocolError
from backend.config import get_config
from backend.llm_service import active_request_ids, is_request_aborted
from backend.request_lifecycle import RequestLifecycleRegistry
from backend.session_hub_runtime import SessionHubRuntime
from PIL import Image
from smolagents.memory import ActionStep, TaskStep
from smolagents.monitoring import Timing
from smolagents.agents import ActionOutput, ToolOutput
from smolagents.memory import ToolCall as MemoryToolCall
from smolagents.models import ChatMessage, ChatMessageStreamDelta, MessageRole
from smolagents.utils import AgentGenerationError, AgentParsingError
from fastapi import FastAPI, Request


@pytest.fixture
def mock_llm_service():
    """Mock LLMService to simulate tool calling"""
    mock_service = MagicMock()
    # Simulate one current-protocol tool call response that would trigger a cycle.
    mock_service.chat.return_value = MagicMock()
    mock_service.chat.return_value.content = """<<TOOL_CALL>>
{
  "name": "test_tool",
  "arguments": {
  "arg": "value"
}
}
<<END_TOOL_CALL>>"""
    return mock_service


def test_shared_llm_fixture_uses_framed_tool_call_protocol(mock_llm_service):
    assert mock_llm_service.chat.return_value.content == """<<TOOL_CALL>>
{
  "name": "test_tool",
  "arguments": {
  "arg": "value"
}
}
<<END_TOOL_CALL>>"""


@pytest.fixture
def agent_runner(mock_llm_service):
    config = deepcopy(get_config())
    config.inference.tool_protocol = "legacy_text"
    config.model.model_type = "text"  # 避免 VLM 相关复杂性
    runner = AgentRunner(mock_llm_service, config)
    original_get_runtime_agent = runner._get_runtime_agent
    parser = LocalLLMModel.__new__(LocalLLMModel)

    def get_runtime_agent_with_real_protocol(session):
        runtime_agent = original_get_runtime_agent(session)
        model = getattr(runtime_agent, "model", None)
        resolver = getattr(model, "_resolve_assistant_control", None)
        if isinstance(resolver, MagicMock) and resolver.side_effect is None:
            resolver.side_effect = parser._resolve_assistant_control
        return runtime_agent

    runner._get_runtime_agent = get_runtime_agent_with_real_protocol
    return runner


async def _collect_coordinator_events(stream):
    return [item async for item in stream]


async def _wait_threading_event(event: threading.Event, timeout: float = 1.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while not event.is_set() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    return event.is_set()


_SWITCH_BASIC = "<<SWITCH_AGENT>> basic_agent"


def _routing_stream_with_raw_state(model, *reasoning_chunks, raw_content=_SWITCH_BASIC):
    """Model-faithful routing stub: controls stay raw; only reasoning is visible."""

    def _stream(*_args, **kwargs):
        model._set_stream_state(
            request_id=kwargs["request_id"],
            content=raw_content,
            reasoning="".join(reasoning_chunks),
        )
        for chunk in reasoning_chunks:
            delta = ChatMessageStreamDelta(content=chunk)
            delta.reasoning = True
            yield delta

    return _stream


@pytest.mark.anyio
async def test_session_id_set_in_run_stream(agent_runner, mock_llm_service):
    """测试 run_stream 中 session_id 正确回写并传递给 agent"""
    session_id_input = None  # Simulate first request with None

    with patch("backend.agent.agent_runner.LLMService", return_value=mock_llm_service):
        # Mock the agent run to yield minimal events
        mock_agent = MagicMock()
        mock_agent.run.return_value = []  # Empty to avoid complex mocking
        mock_agent.memory.steps = []
        mock_agent.state = {}
        mock_agent.tools = {}

        with patch.object(agent_runner, "get_or_create_session") as mock_get_session:
            mock_session = AgentSession(
                session_id="generated-uuid-123",
                agent=mock_agent,
                mode="tool_calling",
            )
            mock_get_session.return_value = mock_session

            async def minimal_active_stream(*_args, **_kwargs):
                mock_session.has_run = True
                yield 'data: {"type": "done", "session_id": "generated-uuid-123"}\n\n'

            # Collect events from run_stream
            events = []
            with patch.object(
                agent_runner,
                "_stream_active_agent",
                side_effect=minimal_active_stream,
            ):
                async for event in agent_runner.run_stream(
                    session_id_input, "test message"
                ):
                    events.append(event)

            # current_session_context 是线程局部存储，在 _run_agent 的后台线程中
            # 设置并在 finally 中清理。主线程无法看到后台线程的 thread-local 值。
            # 因此验证 session_id 回写到 session 对象即可。
            assert mock_session.session_id == "generated-uuid-123"

            # 验证 session 的 has_run 被设置为 True（表明 agent 确实执行了）
            assert mock_session.has_run is True


@pytest.mark.anyio
async def test_generate_answer_now_reply_uses_active_runtime_agent(agent_runner, mock_llm_service):
    """answer-now 应延续当前 runtime agent 的记忆和执行语义。"""

    class DummyTool:
        pass

    primary_tool = DummyTool()
    runtime_tool = DummyTool()

    primary_agent = MagicMock()
    primary_agent.tools = {"primary": primary_tool}
    primary_agent.memory.steps = [TaskStep(task="旧的主 agent 上下文")]
    primary_agent.model.generate_stream.return_value = iter(
        [ChatMessageStreamDelta(content="基于主 agent 上下文的答案")]
    )

    runtime_agent = MagicMock()
    runtime_agent.tools = {"runtime": runtime_tool}
    runtime_agent.memory.steps = [TaskStep(task="当前 runtime agent 上下文")]
    runtime_agent.managed_agents = {}
    runtime_agent.write_memory_to_messages.return_value = [
        ChatMessage(role=MessageRole.USER, content=[{"type": "text", "text": "当前 runtime agent 上下文"}])
    ]
    runtime_agent.model.generate_stream.return_value = iter(
        [ChatMessageStreamDelta(content="基于当前上下文的答案")]
    )

    session = AgentSession(
        session_id="runtime-tool-session",
        agent=primary_agent,
        mode="tool_calling",
    )
    session.active_agent = runtime_agent
    session.has_run = True
    agent_runner.sessions.put(session)

    result = await agent_runner.generate_answer_now_reply("runtime-tool-session")

    assert result == {
        "status": "ok",
        "session_id": "runtime-tool-session",
        "answer": "基于当前上下文的答案",
    }
    assert not hasattr(primary_tool, "_injected_session_id")
    assert getattr(runtime_tool, "_injected_session_id") == "runtime-tool-session"
    primary_agent.model.generate_stream.assert_not_called()
    runtime_agent.model.generate_stream.assert_called_once()
    messages = runtime_agent.model.generate_stream.call_args.args[0]
    assert "当前 runtime agent 上下文" in messages[0].content[0]["text"]
    assert "旧的主 agent 上下文" not in messages[0].content[0]["text"]


@pytest.mark.anyio
async def test_context_cleared_after_run_stream(agent_runner, mock_llm_service):
    """测试 run_stream 结束后 context 被清理"""
    session_id_input = "test-session-456"

    with patch("backend.agent.agent_runner.LLMService", return_value=mock_llm_service):
        mock_agent = MagicMock()
        mock_agent.run.return_value = []

        with patch.object(agent_runner, "get_or_create_session") as mock_get_session:
            mock_session = MagicMock()
            mock_session.session_id = "test-session-456"
            mock_session.agent = mock_agent
            mock_session.has_run = False
            mock_session.check_aborted.return_value = False
            mock_session.check_answer_now.return_value = False
            mock_get_session.return_value = mock_session

            async for _ in agent_runner.run_stream(session_id_input, "test message"):
                pass

            # After run_stream completes, context should be cleared
            assert current_session_context.get() is None


@pytest.mark.anyio
async def test_run_stream_with_dict_agent_state_does_not_hang(agent_runner):
    """回滚快照需要复制 dict state 时，run_stream 不应挂住。"""
    mock_agent = MagicMock()
    mock_agent.memory.steps = []
    mock_agent.tools = {}
    mock_agent.state = {"counter": 1}
    mock_agent.run.return_value = []

    session = AgentSession(
        session_id="dict-state-session",
        agent=mock_agent,
        mode="tool_calling",
    )

    with patch.object(agent_runner, "get_or_create_session", return_value=session):
        events = await asyncio.wait_for(
            _collect_stream_events(
                agent_runner.run_stream("dict-state-session", "test message")
            ),
            timeout=1,
        )

    assert any('"type": "session_start"' in event for event in events)
    assert any('"type": "done"' in event for event in events)


def test_run_sync_decodes_and_passes_images(agent_runner):
    primary_agent = MagicMock()
    primary_agent.run.return_value = "final answer"
    primary_agent.tools = {"primary": MagicMock()}
    runtime_agent = MagicMock()
    runtime_agent.tools = {"runtime": MagicMock()}

    with patch.object(agent_runner, "get_or_create_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.session_id = "sync-session-1"
        mock_session.agent = primary_agent
        mock_session.active_agent = runtime_agent
        mock_session.has_run = False
        mock_get_session.return_value = mock_session

        test_image = Image.new("RGB", (8, 8), "red")
        with patch.object(
            agent_runner, "_decode_request_images", return_value=[test_image]
        ) as mock_decode:
            result = asyncio.run(
                agent_runner.run_sync(
                    None,
                    "图中是什么？",
                    images=["data:image/jpeg;base64,abc"],
                )
            )

    assert result["answer"] == "final answer"
    mock_decode.assert_called_once_with(["data:image/jpeg;base64,abc"])
    primary_agent.run.assert_called_once_with(
        "图中是什么？",
        reset=True,
        images=[test_image],
    )
    runtime_agent.run.assert_not_called()


def test_abort_session_keeps_session_reusable(agent_runner):
    primary_agent = MagicMock()
    primary_agent.memory.steps = ["stale-primary-step"]
    primary_agent.state = {"source": "primary"}

    runtime_agent = MagicMock()
    runtime_agent.memory.steps = ["prior-step"]
    runtime_agent.state = {"source": "runtime"}

    session = AgentSession(
        session_id="session-keep-context",
        agent=primary_agent,
        mode="tool_calling",
    )
    session.active_agent = runtime_agent
    session.has_run = True
    agent_runner.sessions.put(session)

    assert agent_runner.abort_session("session-keep-context") is True

    rebuilt_entry_agent = MagicMock()
    rebuilt_entry_agent.memory.steps = []

    with patch.object(
        agent_runner, "_build_profile_agent", return_value=rebuilt_entry_agent
    ) as mock_build_profile:
        reused = agent_runner.get_or_create_session("session-keep-context")

    assert reused is session
    # _build_profile_agent 的真实签名：name + kwargs (session, runtime_context)
    mock_build_profile.assert_called_once()
    call_args = mock_build_profile.call_args
    assert call_args.args[0] == agent_runner.config.agent.entry_agent_name
    assert call_args.kwargs.get("session") is session
    assert reused.agent is rebuilt_entry_agent
    assert reused.active_agent is rebuilt_entry_agent
    assert reused.agent.memory.steps == ["prior-step"]
    assert reused.agent.state == {"source": "runtime"}
    assert reused.has_shared_history is True
    assert reused.aborted is False


def test_pending_abort_marker_applies_to_new_session(agent_runner):
    abort_manager.abort("pending-agent-stop")
    try:
        session = agent_runner.get_or_create_session("pending-agent-stop")
        assert session.check_aborted() is True
    finally:
        abort_manager.cleanup("pending-agent-stop")


def test_bootstrap_context_messages_imports_prior_chat_turns_without_current_user(agent_runner):
    mock_agent = MagicMock()
    mock_agent.memory.steps = []
    session = AgentSession(
        session_id="mode-switch-session",
        agent=mock_agent,
        mode="tool_calling",
    )

    imported = agent_runner._bootstrap_session_from_context_messages(
        session,
        [
            {"role": "system", "content": "普通 chat 的 system prompt 不应进入 agent memory"},
            {"role": "user", "content": "嘿，你是谁"},
            {"role": "assistant", "content": "你好！我是运行在 NVIDIA Jetson Thor 平台上的 AI 助手。"},
            {"role": "user", "content": "搜索：黄仁勋 最近去了哪个大学演讲"},
        ],
        current_message="搜索：黄仁勋 最近去了哪个大学演讲",
    )

    assert imported is True
    assert len(mock_agent.memory.steps) == 2
    assert isinstance(mock_agent.memory.steps[0], TaskStep)
    assert mock_agent.memory.steps[0].task == "嘿，你是谁"
    assert isinstance(mock_agent.memory.steps[1], ActionStep)
    assert mock_agent.memory.steps[1].model_output == "你好！我是运行在 NVIDIA Jetson Thor 平台上的 AI 助手。"
    assert session.has_run is True


def test_bootstrap_context_messages_appends_chat_turns_missing_from_existing_agent_memory(agent_runner):
    mock_agent = MagicMock()
    mock_agent.memory.steps = [
        TaskStep(task="先用 agent 搜索 A"),
        ActionStep(
            step_number=1,
            timing=Timing(start_time=0.0, end_time=0.0),
            model_output="agent 搜索 A 的答案",
            action_output="agent 搜索 A 的答案",
            is_final_answer=True,
        ),
    ]
    session = AgentSession(
        session_id="mode-switch-existing-session",
        agent=mock_agent,
        mode="tool_calling",
    )
    session.has_run = True

    imported = agent_runner._bootstrap_session_from_context_messages(
        session,
        [
            {"role": "user", "content": "先用 agent 搜索 A"},
            {"role": "assistant", "content": "agent 搜索 A 的答案"},
            {"role": "user", "content": "切到 chat 后我说 B"},
            {"role": "assistant", "content": "chat 回答 B"},
            {"role": "user", "content": "现在切回 agent，我之前说了什么？"},
        ],
        current_message="现在切回 agent，我之前说了什么？",
    )

    assert imported is True
    assert len(mock_agent.memory.steps) == 4
    assert isinstance(mock_agent.memory.steps[2], TaskStep)
    assert mock_agent.memory.steps[2].task == "切到 chat 后我说 B"
    assert isinstance(mock_agent.memory.steps[3], ActionStep)
    assert mock_agent.memory.steps[3].model_output == "chat 回答 B"


def test_get_or_create_session_maps_code_mode_to_tool_calling(agent_runner):
    with patch.object(
        agent_runner, "_build_profile_agent", return_value=MagicMock()
    ) as mock_build_profile:
        session = agent_runner.get_or_create_session(None, mode="code")

    # _build_profile_agent 的真实签名：name + kwargs (session, runtime_context)
    mock_build_profile.assert_called_once()
    call_args = mock_build_profile.call_args
    assert call_args.args[0] == agent_runner.config.agent.entry_agent_name
    assert session.mode == "tool_calling"


def test_get_current_request_id_survives_stale_clear_for_older_run(agent_runner):
    session_request_manager.set("shared-session", "req-old")
    session_request_manager.set("shared-session", "req-new")

    try:
        session_request_manager.clear("shared-session", "req-old")
        assert agent_runner.get_current_request_id("shared-session") == "req-new"

        session_request_manager.clear("shared-session", "req-new")
        assert agent_runner.get_current_request_id("shared-session") is None
    finally:
        session_request_manager.cleanup("shared-session")


def test_agent_session_defaults_active_agent_to_primary_agent():
    primary_agent = MagicMock()

    session = AgentSession(
        session_id="session-defaults",
        agent=primary_agent,
        mode="tool_calling",
    )

    assert session.current_agent_name == "coordinator_1"
    assert session.active_agent is primary_agent
    assert session.turn_switch_count == 0
    assert session.has_shared_history is False
    assert session.run_in_progress is False


def test_abort_rollback_keeps_current_turn_prompt_and_partial_output(agent_runner):
    prior_task = TaskStep(task="之前的问题")
    interrupted_task = TaskStep(task="OK，你还记得我让你搜索过什么内容吗")
    partial_step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.1),
        model_output="用户问我是否记得之前搜索过什么内容。",
        tool_calls=[],
        observations=None,
    )

    mock_agent = MagicMock()
    mock_agent.memory.steps = [prior_task, interrupted_task, partial_step]
    mock_agent.state = {"foo": "bar"}

    session = AgentSession(
        session_id="session-stop-memory",
        agent=mock_agent,
        mode="tool_calling",
    )
    session.has_run = True
    session.abort()

    session.rollback_run_state(
        baseline_step_count=1,
        baseline_has_run=True,
        baseline_state={},
    )

    remaining_steps = session.agent.memory.steps
    assert len(remaining_steps) == 3
    assert isinstance(remaining_steps[1], TaskStep)
    assert remaining_steps[1].task == "OK，你还记得我让你搜索过什么内容吗"
    assert isinstance(remaining_steps[2], ActionStep)
    assert remaining_steps[2].model_output == "用户问我是否记得之前搜索过什么内容。"


def test_abort_rollback_marks_session_as_started_when_current_turn_is_preserved(agent_runner):
    interrupted_task = TaskStep(task="请搜索张雪机车")
    partial_step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.1),
        model_output="用户想要了解张雪机车的相关信息。",
        tool_calls=[],
        observations=None,
    )

    mock_agent = MagicMock()
    mock_agent.memory.steps = [interrupted_task, partial_step]
    mock_agent.state = {"foo": "bar"}

    session = AgentSession(
        session_id="session-stop-first-run",
        agent=mock_agent,
        mode="tool_calling",
    )
    session.has_run = True
    session.abort()

    session.rollback_run_state(
        baseline_step_count=0,
        baseline_has_run=False,
        baseline_state={},
    )

    assert session.has_run is True


def test_step_stream_keeps_reasoning_partial_when_stream_aborted(agent_runner):
    agent = agent_runner._build_agent("tool_calling")
    memory_step = ActionStep(step_number=1, timing=Timing(start_time=0.0, end_time=0.1))

    agent.model.generate_stream = MagicMock(
        return_value=iter(
            [
                ChatMessageStreamDelta(content="先解释背景。"),
            ]
        )
    )
    agent.model._last_stream_content = ""
    agent.model._last_stream_reasoning = "先解释背景。"
    agent.tools = {}
    agent.managed_agents = {}

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        outputs = list(agent._step_stream(memory_step))

    assert any(isinstance(output, ChatMessageStreamDelta) for output in outputs)
    assert memory_step.model_output == "先解释背景。"


def test_step_stream_without_parent_live_does_not_create_per_step_live(agent_runner):
    """Direct _step_stream calls must not recreate Rich Live per step.

    Regression for the second recurrence of the terminal-duplication bug:
    the original Bug 1 fix promoted Live to _run_basic_stream, but kept a
    fallback `with Live(...)` path inside _step_stream for direct calls. Any
    production path that reaches _step_stream without a parent _cli_live then
    regresses to one frozen Rich snapshot per smolagents step. The safe
    fallback is no CLI Live rendering; normal _run_basic_stream still sets the
    shared Live explicitly.
    """
    agent = agent_runner._build_agent("tool_calling")
    memory_step = ActionStep(step_number=1, timing=Timing(start_time=0.0, end_time=0.1))

    agent.model.generate_stream = MagicMock(
        return_value=iter([ChatMessageStreamDelta(content="streamed text")])
    )
    agent.model._last_stream_content = "streamed text"
    agent.model._last_stream_reasoning = ""
    agent.tools = {}
    agent.managed_agents = {}
    agent._cli_live = None

    with patch.object(agent, "write_memory_to_messages", return_value=[]), patch(
        "rich.live.Live"
    ) as mock_live:
        outputs = list(agent._step_stream(memory_step))

    assert any(isinstance(output, ChatMessageStreamDelta) for output in outputs)
    mock_live.assert_not_called()


@pytest.mark.parametrize(
    "abort_during_request_registration",
    [False, True],
    ids=["session_already_aborted", "abort_during_request_registration"],
)
def test_step_stream_abort_gate_does_not_start_a_provider_request(
    agent_runner, abort_during_request_registration
):
    session_id = f"step-stream-abort-gate-{abort_during_request_registration}"
    session = AgentSession(
        session_id=session_id,
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="basic_agent",
        is_child_context=True,
    )
    agent = agent_runner._build_profile_agent("basic_agent", session=session)
    memory_step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.1),
    )
    agent.model.generate_stream = MagicMock(
        return_value=iter([ChatMessageStreamDelta(content="must not reach provider")])
    )
    agent.tools = {}
    agent.managed_agents = {}

    real_set = session_request_manager.set
    registered_request_ids: list[str] = []

    def register_then_maybe_abort(request_session_id, request_id):
        registered_request_ids.append(request_id)
        real_set(request_session_id, request_id)
        if abort_during_request_registration:
            session.abort()
            abort_manager.abort(request_session_id)

    current_session_context.set(session_id, None)
    try:
        if not abort_during_request_registration:
            session.abort()
            abort_manager.abort(session_id)

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    session_request_manager,
                    "set",
                    side_effect=register_then_maybe_abort,
                )
            )
            stack.enter_context(
                patch("backend.llm_service.abort_sglang_request", return_value=True)
            )
            stack.enter_context(
                patch.object(agent, "write_memory_to_messages", return_value=[])
            )
            with pytest.raises(AgentGenerationError):
                list(agent._step_stream(memory_step))

        agent.model.generate_stream.assert_not_called()
        assert session_request_manager.get(session_id) is None
        assert all(
            request_id not in active_request_ids()
            and not is_request_aborted(request_id)
            for request_id in registered_request_ids
        )
    finally:
        current_session_context.clear()
        session_request_manager.cleanup(session_id)
        abort_manager.cleanup(session_id)
        session.clear_abort()


def test_step_stream_missing_scoped_state_does_not_consume_usage(agent_runner):
    agent = agent_runner._build_agent("tool_calling")
    memory_step = ActionStep(step_number=1, timing=Timing(start_time=0.0, end_time=0.1))
    agent.model.generate_stream = MagicMock(
        return_value=iter([ChatMessageStreamDelta(content="current request output")])
    )
    agent.model.get_stream_state = MagicMock(return_value=None)
    agent.model.llm_service.get_stream_usage.reset_mock()
    agent.tools = {}
    agent.managed_agents = {}

    current_session_context.set("missing-state-session", None)
    try:
        with patch.object(agent, "write_memory_to_messages", return_value=[]):
            outputs = list(agent._step_stream(memory_step))
    finally:
        current_session_context.clear()

    assert any(isinstance(output, ChatMessageStreamDelta) for output in outputs)
    agent.model.llm_service.get_stream_usage.assert_not_called()
    assert memory_step.model_output == "current request output"


def _tool_block(name: str, arguments: str) -> str:
    return (
        f'<<TOOL_CALL>>\n{{"name":"{name}","arguments":{arguments}}}\n'
        "<<END_TOOL_CALL>>"
    )


def _prepare_protocol_step(agent_runner, raw_content, visible_deltas=()):
    agent = agent_runner._build_agent("tool_calling")
    memory_step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.1),
    )
    agent.model.generate_stream = MagicMock(return_value=iter(visible_deltas))
    agent.model.get_stream_state = MagicMock(
        return_value={"content": raw_content, "reasoning": ""}
    )
    agent.model.llm_service.get_stream_usage.return_value = None
    agent.process_tool_calls = MagicMock(return_value=iter(()))
    return agent, memory_step


def test_step_stream_non_stream_protocol_error_is_reported_as_parsing_error(
    agent_runner,
):
    agent = agent_runner._build_agent("tool_calling")
    agent.stream_outputs = False
    agent.model.generate = MagicMock(
        side_effect=TextToolProtocolError(
            "unsupported_xml_protocol",
            "Unsupported XML-style tool-call protocol.",
        )
    )
    memory_step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.1),
    )

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        with pytest.raises(AgentParsingError, match="XML-style"):
            list(agent._step_stream(memory_step))


def test_step_stream_non_stream_provider_failure_remains_generation_error(
    agent_runner,
):
    agent = agent_runner._build_agent("tool_calling")
    agent.stream_outputs = False
    agent.model.generate = MagicMock(side_effect=RuntimeError("provider offline"))
    memory_step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.1),
    )

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        with pytest.raises(AgentGenerationError, match="provider offline"):
            list(agent._step_stream(memory_step))


def test_step_stream_resolves_two_framed_calls_atomically_without_leaking_control(
    agent_runner,
):
    raw = "\n\n".join(
        [
            _tool_block("read_file", '{\n  "file_path": "/tmp/a"\n}'),
            _tool_block("grep", '{\n  "pattern": "needle",\n  "path": "/tmp"\n}'),
        ]
    )
    agent, memory_step = _prepare_protocol_step(agent_runner, raw)

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        outputs = list(agent._step_stream(memory_step))

    raw_debug = [item for item in outputs if hasattr(item, "raw_tool_call")]
    assert len(raw_debug) == 1
    assert raw_debug[0].raw_tool_call == raw
    call_message = agent.process_tool_calls.call_args.args[0]
    assert [call.function.name for call in call_message.tool_calls] == [
        "read_file",
        "grep",
    ]
    assert memory_step.model_output is None
    assert memory_step.model_output_message.content is None
    assert all("<<TOOL_CALL>>" not in str(getattr(item, "content", "")) for item in outputs)
    stop_sequences = agent.model.generate_stream.call_args.kwargs["stop_sequences"]
    assert "<<TOOL_CALL>>" not in stop_sequences
    assert "<<SWITCH_AGENT>>" not in stop_sequences
    assert stop_sequences == ["Observation:"]


def test_step_stream_missing_closer_yields_raw_debug_then_error_without_memory_leak(
    agent_runner,
):
    raw = '<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{\"file_path\":\"/tmp/a\"}}'
    agent, memory_step = _prepare_protocol_step(agent_runner, raw)
    agent.memory.steps = [memory_step]
    outputs = []

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        stream = agent._step_stream(memory_step)
        with pytest.raises(AgentParsingError, match="<<END_TOOL_CALL>>"):
            while True:
                outputs.append(next(stream))

    assert [item.raw_tool_call for item in outputs if hasattr(item, "raw_tool_call")] == [raw]
    assert memory_step.model_output is None
    assert memory_step.model_output_message.content is None
    assert raw not in "\n".join(str(message.content) for message in agent.write_memory_to_messages())
    compact_messages = agent_runner._session_memory_to_messages(MagicMock(agent=agent))
    assert raw not in "\n".join(str(message.content) for message in compact_messages)
    agent.process_tool_calls.assert_not_called()


@pytest.mark.parametrize(
    ("raw", "error_pattern"),
    [
        (
            "I will inspect the file now.\n"
            '<<TOOL_CALL>> read_file {"file_path":"/tmp/a"}',
            "Control protocol error",
        ),
        (
            "I will route now.\n<<SWITCH_AGENT>> basic_agent",
            "agent switch",
        ),
    ],
)
def test_step_stream_rejects_noncanonical_textual_attempt_before_execution(
    agent_runner,
    raw,
    error_pattern,
):
    agent, memory_step = _prepare_protocol_step(agent_runner, raw)
    agent.memory.steps = [memory_step]
    outputs = []

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        stream = agent._step_stream(memory_step)
        with pytest.raises(AgentParsingError, match=error_pattern):
            while True:
                outputs.append(next(stream))

    assert [
        output.raw_tool_call
        for output in outputs
        if isinstance(output, RawToolCallDebug)
    ] == [raw]
    assert memory_step.model_output is None
    assert memory_step.model_output_message.content is None
    assert not any(
        isinstance(output, ActionOutput) and output.is_final_answer
        for output in outputs
    )
    agent.process_tool_calls.assert_not_called()


def test_step_stream_keeps_xml_like_tool_text_as_natural_final_answer(agent_runner):
    raw = (
        "<think>consider</think>\n<tool_call>\n<function=read_file>\n"
        "<parameter=file_path>/tmp/a</parameter>\n</function>\n</tool_call>"
    )
    agent, memory_step = _prepare_protocol_step(agent_runner, raw)

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        outputs = list(agent._step_stream(memory_step))

    assert not any(isinstance(output, RawToolCallDebug) for output in outputs)
    assert any(
        isinstance(output, ActionOutput)
        and output.is_final_answer
        and output.output == raw
        for output in outputs
    )
    agent.process_tool_calls.assert_not_called()


@pytest.mark.parametrize(
    "raw",
    [
        "```text\nCalling tools:\n[]\n```",
        "~~~text\nCalling tools:\n[]",
    ],
)
def test_step_stream_keeps_fenced_legacy_examples_out_of_raw_debug(
    agent_runner, raw
):
    agent, memory_step = _prepare_protocol_step(
        agent_runner,
        raw,
        visible_deltas=(ChatMessageStreamDelta(content=raw),),
    )

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        outputs = list(agent._step_stream(memory_step))

    assert not any(isinstance(output, RawToolCallDebug) for output in outputs)
    assert any(
        isinstance(output, ActionOutput)
        and output.is_final_answer
        and output.output == raw
        for output in outputs
    )
    agent.process_tool_calls.assert_not_called()


def test_step_stream_executes_tool_after_visible_prose_without_finalizing(
    agent_runner,
):
    prose = "好的，我先确认文件内容。\n"
    raw = prose + _tool_block(
        "read_file",
        '{\n  "file_path": "/tmp/a"\n}',
    )
    agent, memory_step = _prepare_protocol_step(
        agent_runner,
        raw,
        visible_deltas=(ChatMessageStreamDelta(content=prose),),
    )

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        outputs = list(agent._step_stream(memory_step))

    assert [
        output.raw_tool_call
        for output in outputs
        if isinstance(output, RawToolCallDebug)
    ] == [raw]
    call_message = agent.process_tool_calls.call_args.args[0]
    assert [call.function.name for call in call_message.tool_calls] == ["read_file"]
    assert call_message.tool_calls[0].function.arguments == {
        "file_path": "/tmp/a"
    }
    assert memory_step.model_output is None
    assert memory_step.model_output_message.content is None
    assert not any(
        isinstance(output, ActionOutput) and output.is_final_answer
        for output in outputs
    )


def test_step_stream_real_adapter_hides_prefixed_control_before_execution(agent_runner):
    prose = "先检查。\n"
    frame = _tool_block("read_file", '{"file_path":"/tmp/a"}')
    chunks = [
        {"content": prose},
        {"content": "<<TOOL_"},
        {"content": frame[len("<<TOOL_") :]},
    ]
    agent = agent_runner._build_agent("tool_calling")
    memory_step = ActionStep(step_number=1, timing=Timing(start_time=0.0, end_time=0.1))
    agent.model.llm_service.chat.return_value = iter(chunks)
    agent.model.llm_service.get_stream_usage.return_value = None
    agent.process_tool_calls = MagicMock(return_value=iter(()))

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        outputs = list(agent._step_stream(memory_step))

    visible = "".join(
        output.content or ""
        for output in outputs
        if isinstance(output, ChatMessageStreamDelta)
        and not getattr(output, "reasoning", False)
    )
    assert visible == prose
    agent.process_tool_calls.assert_called_once()
    assert not any(
        isinstance(output, ActionOutput) and output.is_final_answer
        for output in outputs
    )


@pytest.mark.parametrize(
    "raw",
    [
        "~~~text\n" + _tool_block("read_file", "{}"),
        "```text\n<<SWITCH_AGENT>> basic_agent\n```",
    ],
)
def test_step_stream_real_adapter_never_executes_fenced_controls(agent_runner, raw):
    agent = agent_runner._build_agent("tool_calling")
    memory_step = ActionStep(step_number=1, timing=Timing(start_time=0.0, end_time=0.1))
    midpoint = max(1, len(raw) // 2)
    agent.model.llm_service.chat.return_value = iter(
        [{"content": raw[:midpoint]}, {"content": raw[midpoint:]}]
    )
    agent.model.llm_service.get_stream_usage.return_value = None
    agent.process_tool_calls = MagicMock(return_value=iter(()))

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        outputs = list(agent._step_stream(memory_step))

    assert any(
        isinstance(output, ActionOutput)
        and output.is_final_answer
        and output.output == raw
        for output in outputs
    )
    agent.process_tool_calls.assert_not_called()


def test_step_stream_rejects_tool_attempt_after_unicode_line_separator(
    agent_runner,
):
    raw = (
        "I will inspect the file now.\u2028"
        + _tool_block("read_file", '{\n  "file_path": "/tmp/a"\n}')
    )
    agent, memory_step = _prepare_protocol_step(agent_runner, raw)
    outputs = []

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        stream = agent._step_stream(memory_step)
        with pytest.raises(AgentParsingError, match="No tool was executed"):
            while True:
                outputs.append(next(stream))

    assert [
        output.raw_tool_call
        for output in outputs
        if isinstance(output, RawToolCallDebug)
    ] == [raw]
    assert memory_step.model_output is None
    assert memory_step.model_output_message.content is None
    assert not any(
        isinstance(output, ActionOutput) and output.is_final_answer
        for output in outputs
    )
    agent.process_tool_calls.assert_not_called()


def test_step_stream_empty_fallback_executes_text_protocol_without_dual_encoding(
    agent_runner,
):
    raw = _tool_block("read_file", '{\n  "file_path": "/tmp/a"\n}')
    fallback_result = MagicMock()
    fallback_result.text = raw
    fallback_result.tool_calls = None

    def chat_side_effect(**kwargs):
        return iter(()) if kwargs.get("stream") else fallback_result

    agent = agent_runner._build_agent("tool_calling")
    memory_step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.1),
    )
    agent.model.llm_service.chat.side_effect = chat_side_effect
    agent.model.llm_service.get_stream_usage.return_value = None
    agent.process_tool_calls = MagicMock(return_value=iter(()))

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        outputs = list(agent._step_stream(memory_step))

    call_message = agent.process_tool_calls.call_args.args[0]
    assert [call.function.name for call in call_message.tool_calls] == ["read_file"]
    assert memory_step.model_output is None
    assert all(
        "<<TOOL_CALL>>" not in str(getattr(output, "content", ""))
        for output in outputs
    )


@pytest.mark.parametrize(
    ("raw", "provider_tool_calls", "error_pattern"),
    [
        (
            '<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{\"file_path\":\"/tmp/a\"}}',
            None,
            "<<END_TOOL_CALL>>",
        ),
        ("Calling tools:\n[]", None, "Unsupported legacy tool-call protocol"),
        (
            _tool_block("read_file", '{\n  "file_path": "/tmp/text"\n}'),
            [
                {
                    "id": "provider-dual-1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"file_path":"/tmp/native"}',
                    },
                }
            ],
            "cannot coexist",
        ),
    ],
)
def test_step_stream_empty_fallback_emits_raw_before_actionable_parsing_error(
    agent_runner,
    raw,
    provider_tool_calls,
    error_pattern,
):
    fallback_result = MagicMock()
    fallback_result.text = raw
    fallback_result.tool_calls = provider_tool_calls

    def chat_side_effect(**kwargs):
        return iter(()) if kwargs.get("stream") else fallback_result

    agent = agent_runner._build_agent("tool_calling")
    memory_step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.1),
    )
    agent.model.llm_service.chat.side_effect = chat_side_effect
    agent.model.llm_service.get_stream_usage.return_value = None
    agent.process_tool_calls = MagicMock(return_value=iter(()))
    outputs = []

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        stream = agent._step_stream(memory_step)
        with pytest.raises(AgentParsingError, match=error_pattern):
            while True:
                outputs.append(next(stream))

    assert [
        output.raw_tool_call
        for output in outputs
        if isinstance(output, RawToolCallDebug)
    ] == [raw]
    assert memory_step.model_output is None
    assert memory_step.model_output_message.content is None
    agent.process_tool_calls.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("raw", "provider_tool_calls"),
    [
        (
            '<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{\"file_path\":\"/tmp/a\"}}',
            None,
        ),
        ("Calling tools:\n[]", None),
        (
            _tool_block("read_file", '{\n  "file_path": "/tmp/text"\n}'),
            [
                {
                    "id": "provider-dual-sse-1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"file_path":"/tmp/native"}',
                    },
                }
            ],
        ),
    ],
)
async def test_empty_fallback_raw_debug_precedes_parsing_observation_sse(
    agent_runner,
    raw,
    provider_tool_calls,
):
    fallback_results = iter(
        [
            (raw, provider_tool_calls),
            ("Recovered final answer.", None),
        ]
    )

    def chat_side_effect(**kwargs):
        if kwargs.get("stream"):
            return iter(())
        text, tool_calls = next(fallback_results)
        result = MagicMock()
        result.text = text
        result.tool_calls = tool_calls
        result.usage = None
        return result

    agent = agent_runner._build_agent("tool_calling")
    agent.max_steps = 2
    agent.model.llm_service.chat.side_effect = chat_side_effect
    agent.model.llm_service.get_stream_usage.return_value = None
    agent.process_tool_calls = MagicMock(return_value=iter(()))
    session = AgentSession(
        session_id=f"empty-fallback-error-{abs(hash(raw))}",
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )

    payloads = [
        _sse_payload(event)
        async for event in agent_runner._run_basic_stream(session, "read", None)
    ]

    raw_index = next(
        index
        for index, payload in enumerate(payloads)
        if payload.get("raw_tool_call") == raw
    )
    error_index = next(
        index
        for index, payload in enumerate(payloads)
        if payload.get("step_type") == "observation"
        and "No tool was executed" in payload.get("content", "")
    )
    assert raw_index < error_index
    assert "Correction:" in payloads[error_index]["content"]
    assert not any(payload.get("step_type") == "tool_call" for payload in payloads)
    agent.process_tool_calls.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("with_native_call", [False, True])
async def test_empty_fallback_fenced_tool_example_is_not_a_text_control_sse(
    agent_runner,
    with_native_call,
):
    raw = "```json\n" + _tool_block(
        "read_file", '{\n  "file_path": "/tmp/fenced-text"\n}'
    ) + "\n```"
    provider_tool_calls = (
        [
            {
                "id": "provider-fenced-sse-native-1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": '{"file_path":"/tmp/fenced-native"}',
                },
            }
        ]
        if with_native_call
        else None
    )
    fallback_results = iter(
        [(raw, provider_tool_calls), ("Recovered final answer.", None)]
    )

    def chat_side_effect(**kwargs):
        if kwargs.get("stream"):
            return iter(())
        text, tool_calls = next(fallback_results)
        return MagicMock(text=text, tool_calls=tool_calls, usage=None)

    agent = agent_runner._build_agent("tool_calling")
    agent.max_steps = 2
    agent.model.llm_service.chat.side_effect = chat_side_effect
    agent.model.llm_service.get_stream_usage.return_value = None
    agent.process_tool_calls = MagicMock(return_value=iter(()))
    session = AgentSession(
        session_id=f"empty-fallback-fenced-{with_native_call}",
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )

    payloads = [
        _sse_payload(event)
        async for event in agent_runner._run_basic_stream(session, "read", None)
    ]

    assert not any(payload.get("raw_tool_call") == raw for payload in payloads)
    assert not any(
        payload.get("step_type") == "observation"
        and "No tool was executed" in payload.get("content", "")
        for payload in payloads
    )
    if with_native_call:
        call_message = agent.process_tool_calls.call_args.args[0]
        assert call_message.tool_calls[0].function.arguments == {
            "file_path": "/tmp/fenced-native"
        }
    else:
        agent.process_tool_calls.assert_not_called()
        assert any(payload.get("content") == raw for payload in payloads)


def test_step_stream_empty_fallback_keeps_native_only_call(agent_runner):
    fallback_result = MagicMock()
    fallback_result.text = "I will read the file."
    fallback_result.tool_calls = [
        {
            "id": "provider-native-only-1",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"file_path":"/tmp/native"}',
            },
        }
    ]

    def chat_side_effect(**kwargs):
        return iter(()) if kwargs.get("stream") else fallback_result

    agent = agent_runner._build_agent("tool_calling")
    memory_step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.1),
    )
    agent.model.llm_service.chat.side_effect = chat_side_effect
    agent.model.llm_service.get_stream_usage.return_value = None
    agent.process_tool_calls = MagicMock(return_value=iter(()))

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        outputs = list(agent._step_stream(memory_step))

    call_message = agent.process_tool_calls.call_args.args[0]
    assert [call.function.name for call in call_message.tool_calls] == ["read_file"]
    assert call_message.tool_calls[0].function.arguments == {
        "file_path": "/tmp/native"
    }
    assert not any(isinstance(output, RawToolCallDebug) for output in outputs)


def test_step_stream_keeps_whole_content_fenced_tool_example_inert(
    agent_runner,
):
    raw = "```json\n" + _tool_block(
        "read_file", '{\n  "file_path": "/tmp/fenced"\n}'
    ) + "\n```"
    agent, memory_step = _prepare_protocol_step(agent_runner, raw)

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        outputs = list(agent._step_stream(memory_step))

    assert not any(isinstance(output, RawToolCallDebug) for output in outputs)
    assert any(
        isinstance(output, ActionOutput)
        and output.is_final_answer
        and output.output == raw
        for output in outputs
    )
    agent.process_tool_calls.assert_not_called()
    assert memory_step.model_output == raw
    assert memory_step.model_output_message.content == raw


def test_step_stream_split_reasoning_fence_keeps_content_markers_inert(
    agent_runner,
):
    content = _tool_block(
        "read_file", '{\n  "file_path": "/tmp/split"\n}'
    ) + "\n```"
    agent, memory_step = _prepare_protocol_step(agent_runner, content)
    agent.model.get_stream_state.return_value = {
        "content": content,
        "reasoning": "Reasoning remains non-executable.\n```xml\n",
    }

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        outputs = list(agent._step_stream(memory_step))

    assert not any(isinstance(output, RawToolCallDebug) for output in outputs)
    assert any(
        isinstance(output, ActionOutput)
        and output.is_final_answer
        and output.output == content
        for output in outputs
    )
    agent.process_tool_calls.assert_not_called()


def test_step_stream_malformed_split_fence_remains_inert_natural_text(
    agent_runner,
):
    content = '<<TOOL_CALL>> read_file\n{\n  "file_path": "/tmp/split"\n}\n```'
    agent, memory_step = _prepare_protocol_step(agent_runner, content)
    agent.model.get_stream_state.return_value = {
        "content": content,
        "reasoning": "Do not execute this reasoning.\n```text\n",
    }
    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        outputs = list(agent._step_stream(memory_step))

    assert not any(isinstance(output, RawToolCallDebug) for output in outputs)
    assert any(
        isinstance(output, ActionOutput)
        and output.is_final_answer
        and output.output == content
        for output in outputs
    )
    agent.process_tool_calls.assert_not_called()


def test_step_stream_whole_fenced_missing_tool_closer_is_inert(agent_runner):
    raw = "```json\n<<TOOL_CALL>> read_file\n{}\n```"
    agent, memory_step = _prepare_protocol_step(agent_runner, raw)
    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        outputs = list(agent._step_stream(memory_step))

    assert any(
        isinstance(output, ActionOutput)
        and output.is_final_answer
        and output.output == raw
        for output in outputs
    )
    assert not any(isinstance(output, RawToolCallDebug) for output in outputs)
    agent.process_tool_calls.assert_not_called()


def test_step_stream_empty_fallback_fenced_tool_example_is_inert(agent_runner):
    raw = "```text\n" + _tool_block(
        "read_file", '{\n  "file_path": "/tmp/fallback-fenced"\n}'
    ) + "\n```"
    fallback_result = MagicMock(text=raw, tool_calls=None)

    def chat_side_effect(**kwargs):
        return iter(()) if kwargs.get("stream") else fallback_result

    agent = agent_runner._build_agent("tool_calling")
    memory_step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.1),
    )
    agent.model.llm_service.chat.side_effect = chat_side_effect
    agent.model.llm_service.get_stream_usage.return_value = None
    agent.process_tool_calls = MagicMock(return_value=iter(()))

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        outputs = list(agent._step_stream(memory_step))

    assert not any(isinstance(output, RawToolCallDebug) for output in outputs)
    assert any(
        isinstance(output, ActionOutput)
        and output.is_final_answer
        and output.output == raw
        for output in outputs
    )
    agent.process_tool_calls.assert_not_called()


def test_step_stream_empty_fallback_native_plus_fenced_uses_native_call(
    agent_runner,
):
    raw = "```json\n" + _tool_block(
        "read_file", '{\n  "file_path": "/tmp/text"\n}'
    ) + "\n```"
    fallback_result = MagicMock(
        text=raw,
        tool_calls=[
            {
                "id": "provider-fenced-dual-1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": '{"file_path":"/tmp/native"}',
                },
            }
        ],
    )

    def chat_side_effect(**kwargs):
        return iter(()) if kwargs.get("stream") else fallback_result

    agent = agent_runner._build_agent("tool_calling")
    memory_step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.1),
    )
    agent.model.llm_service.chat.side_effect = chat_side_effect
    agent.model.llm_service.get_stream_usage.return_value = None
    agent.process_tool_calls = MagicMock(return_value=iter(()))
    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        outputs = list(agent._step_stream(memory_step))

    assert not any(isinstance(output, RawToolCallDebug) for output in outputs)
    assert agent.process_tool_calls.call_count == 1
    call_message = agent.process_tool_calls.call_args.args[0]
    assert call_message.tool_calls[0].function.arguments == {
        "file_path": "/tmp/native"
    }


def test_step_stream_rejects_malformed_second_block_before_execution(agent_runner):
    raw = "\n".join(
        [
            _tool_block("read_file", '{\n  "file_path": "/tmp/a"\n}'),
            _tool_block("grep", '{\n  "pattern": "needle",\n}'),
        ]
    )
    agent, memory_step = _prepare_protocol_step(agent_runner, raw)

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        with pytest.raises(AgentParsingError, match="No tool was executed"):
            list(agent._step_stream(memory_step))

    agent.process_tool_calls.assert_not_called()


def test_step_stream_emits_secondary_handoff_without_control_text(agent_runner):
    raw = "<<SWITCH_AGENT>> basic_agent"
    agent, memory_step = _prepare_protocol_step(agent_runner, raw)

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        outputs = list(agent._step_stream(memory_step))

    assert outputs == [SecondaryHandoffRequest(target_agent="basic_agent")]
    assert memory_step.model_output is None
    assert memory_step.model_output_message.content is None
    agent.process_tool_calls.assert_not_called()


def test_process_tool_calls_basic_agent_handoff_uses_validated_native_arguments(
    agent_runner,
):
    agent = agent_runner._build_agent("tool_calling")
    function = MagicMock()
    function.name = "basic_agent"
    function.arguments = {
        "task": "Inspect the failure",
        "priority": "high",
    }
    tool_call = MagicMock()
    tool_call.id = "provider_handoff_1"
    tool_call.function = function
    chat_message = MagicMock()
    chat_message.tool_calls = [tool_call]
    memory_step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.1),
    )

    outputs = list(agent.process_tool_calls(chat_message, memory_step))

    assert outputs == [
        SecondaryHandoffRequest(
            target_agent="basic_agent",
            task="Inspect the failure",
            additional_args={"priority": "high"},
        )
    ]
    assert memory_step.tool_calls[0].name == "basic_agent"
    assert memory_step.tool_calls[0].arguments == function.arguments


def test_step_stream_ignores_control_marker_in_reasoning_and_keeps_fenced_example(
    agent_runner,
):
    fenced = (
        "```text\nExample only:\n"
        + _tool_block("read_file", '{\n  "file_path": "/tmp/demo"\n}')
        + "\n```"
    )
    reasoning = ChatMessageStreamDelta(content="<<TOOL_CALL>> is being considered")
    reasoning.reasoning = True
    visible = ChatMessageStreamDelta(content=fenced)
    visible.reasoning = False
    agent, memory_step = _prepare_protocol_step(
        agent_runner,
        fenced,
        (reasoning, visible),
    )
    agent.model.get_stream_state.return_value = {
        "content": fenced,
        "reasoning": reasoning.content,
    }

    with patch.object(agent, "write_memory_to_messages", return_value=[]):
        outputs = list(agent._step_stream(memory_step))

    assert outputs[-1].is_final_answer is True
    assert outputs[-1].output == fenced
    agent.process_tool_calls.assert_not_called()


def test_coordinator_routing_stream_emits_running_text_delta(agent_runner):
    coordinator_agent = agent_runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []

    session = AgentSession(
        session_id="coordinator-routing-stream-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )

    async def _collect():
        return [
            item
            async for item in agent_runner._run_coordinator_routing_stream(
                session, "使用浏览器 打开 example.com", None
            )
        ]

    with patch.object(coordinator_agent, "write_memory_to_messages", return_value=[]), patch.object(
        coordinator_agent.model,
        "generate_stream",
        side_effect=_routing_stream_with_raw_state(
            coordinator_agent.model,
            "正在分析是否需要切换执行代理",
        ),
    ) as mock_generate_stream:
        outputs = asyncio.run(_collect())

    assert outputs[0] == {
        "kind": "dispatch",
        "dispatch_status": "running",
        "dispatch_stage": "routing",
        "dispatch_kind": "routing",
        "display_slot": "before_body",
        "dispatch_id": "dispatch-coordinator-routing-stream-session-routing",
    }
    assert outputs[1] == {
        "kind": "timeline",
        "event": {
            "kind": "routing_started",
            "source": "coordinator_1",
            "message_id": "routing-card-coordinator-routing-stream-session",
        },
    }
    assert outputs[2] == {
        "kind": "timeline",
        "event": {
            "kind": "routing_delta",
            "source": "coordinator_1",
            "message_id": "routing-card-coordinator-routing-stream-session",
            "text": "正在分析是否需要切换执行代理",
            "partial": True,
        },
    }
    assert outputs[3] == {
        "kind": "timeline",
        "event": {
            "kind": "dispatch_delta",
            "source": "coordinator_1",
            "message_id": "routing-card-coordinator-routing-stream-session",
            "dispatch_id": "dispatch-coordinator-routing-stream-session-routing",
            "dispatch_kind": "routing",
            "display": "dispatch_card",
            "display_slot": "before_body",
            "text": "正在分析是否需要切换执行代理",
            "partial": True,
        },
    }
    assert outputs[4] == {
        "kind": "dispatch",
        "dispatch_status": "running",
        "dispatch_stage": "routing",
        "dispatch_kind": "routing",
        "display_slot": "before_body",
        "dispatch_id": "dispatch-coordinator-routing-stream-session-routing",
        "dispatch_message": "正在分析是否需要切换执行代理",
    }
    assert outputs[5] == {
        "kind": "timeline",
        "event": {
            "kind": "routing_decision",
            "source": "coordinator_1",
            "message_id": "routing-card-coordinator-routing-stream-session",
            "target_agent": "basic_agent",
        },
    }
    assert outputs[6] == {
        "kind": "result",
        "route": {"kind": "switch", "target_agent": "basic_agent"},
    }
    routing_request_id = mock_generate_stream.call_args.kwargs["request_id"]
    coordinator_agent.model.llm_service.discard_stream_usage.assert_called_once_with(
        routing_request_id
    )


def test_coordinator_timeout_worker_cleans_late_state_only_after_exit(agent_runner):
    coordinator_agent = agent_runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []
    session = AgentSession(
        session_id="coordinator-late-cleanup-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )
    agent_runner.config.agent.coordinator_1_timeout_seconds = 0.01
    worker_wrote_state = threading.Event()
    worker_exited = threading.Event()
    usage_ids = set()
    coordinator_agent.model.llm_service.discard_stream_usage.side_effect = usage_ids.discard

    def delayed_stream(*args, **kwargs):
        request_id = kwargs["request_id"]
        time.sleep(0.12)
        coordinator_agent.model._set_stream_state(
            request_id=request_id,
            content="late output",
            reasoning="",
        )
        usage_ids.add(request_id)
        worker_wrote_state.set()
        try:
            yield ChatMessageStreamDelta(content="late output")
        finally:
            worker_exited.set()

    async def _collect():
        return [
            item
            async for item in agent_runner._run_coordinator_routing_stream(
                session, "route slowly", None
            )
        ]

    with patch.object(
        coordinator_agent, "write_memory_to_messages", return_value=[]
    ), patch.object(
        coordinator_agent.model, "generate_stream", side_effect=delayed_stream
    ) as generate_stream:
        outputs = asyncio.run(_collect())

    request_id = generate_stream.call_args.kwargs["request_id"]
    assert outputs[-1]["route"]["reason"] == "timeout_fallback"
    assert not worker_wrote_state.is_set()
    assert session_request_manager.get(session.session_id) == request_id

    assert worker_exited.wait(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while coordinator_agent.model.get_stream_state(request_id) is not None:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert session_request_manager.get(session.session_id) is None
    assert request_id not in usage_ids


def test_coordinator_routing_stream_logs_aggregate_once(agent_runner, caplog):
    coordinator_agent = agent_runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []
    switch_payload = _SWITCH_BASIC

    session = AgentSession(
        session_id="coordinator-routing-log-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )

    async def _collect():
        return [
            item
            async for item in agent_runner._run_coordinator_routing_stream(
                session, "使用工具", None
            )
        ]

    caplog.set_level(logging.INFO, logger=agent_runner_module.logger.name)
    with patch.object(coordinator_agent, "write_memory_to_messages", return_value=[]), patch.object(
        coordinator_agent.model,
        "generate_stream",
        side_effect=_routing_stream_with_raw_state(coordinator_agent.model),
    ):
        asyncio.run(_collect())

    coordinator_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == agent_runner_module.logger.name
        and "[COORDINATOR]" in record.getMessage()
    ]
    assert len(coordinator_logs) == 1
    assert switch_payload in coordinator_logs[0]


def test_coordinator_routing_stream_reasoning_only_does_not_become_final_answer(
    agent_runner,
):
    coordinator_agent = agent_runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []

    session = AgentSession(
        session_id="coordinator-routing-reasoning-only-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )

    async def _collect():
        return [
            item
            async for item in agent_runner._run_coordinator_routing_stream(
                session, "你好", None
            )
        ]

    with patch.object(coordinator_agent, "write_memory_to_messages", return_value=[]), patch.object(
        coordinator_agent.model,
        "generate_stream",
        return_value=iter(
            [
                ChatMessageStreamDelta(content="ThinkingProcess: Analyze the request"),
            ]
        ),
    ), patch.object(
        coordinator_agent.model,
        "get_stream_state",
        return_value={
            "content": "",
            "reasoning": "ThinkingProcess: Analyze the request",
        },
    ):
        outputs = asyncio.run(_collect())

    assert outputs[0] == {
        "kind": "dispatch",
        "dispatch_status": "running",
        "dispatch_stage": "routing",
        "dispatch_kind": "routing",
        "display_slot": "before_body",
        "dispatch_id": "dispatch-coordinator-routing-reasoning-only-session-routing",
    }
    assert outputs[1] == {
        "kind": "timeline",
        "event": {
            "kind": "routing_started",
            "source": "coordinator_1",
            "message_id": "routing-card-coordinator-routing-reasoning-only-session",
        },
    }
    assert outputs[2] == {
        "kind": "timeline",
        "event": {
            "kind": "routing_delta",
            "source": "coordinator_1",
            "message_id": "routing-card-coordinator-routing-reasoning-only-session",
            "text": "ThinkingProcess: Analyze the request",
            "partial": True,
        },
    }
    assert outputs[3] == {
        "kind": "timeline",
        "event": {
            "kind": "dispatch_delta",
            "source": "coordinator_1",
            "message_id": "routing-card-coordinator-routing-reasoning-only-session",
            "dispatch_id": "dispatch-coordinator-routing-reasoning-only-session-routing",
            "dispatch_kind": "routing",
            "display": "dispatch_card",
            "display_slot": "before_body",
            "text": "ThinkingProcess: Analyze the request",
            "partial": True,
        },
    }
    assert outputs[4] == {
        "kind": "dispatch",
        "dispatch_status": "running",
        "dispatch_stage": "routing",
        "dispatch_kind": "routing",
        "display_slot": "before_body",
        "dispatch_id": "dispatch-coordinator-routing-reasoning-only-session-routing",
        "dispatch_message": "ThinkingProcess: Analyze the request",
    }
    assert outputs[5] == {
        "kind": "result",
        "route": {
            "kind": "invalid",
            "error": "Coordinator returned an empty routing response",
        },
    }


def test_coordinator_terminal_logging_can_be_disabled_or_throttled_separately(
    agent_runner,
):
    assert hasattr(agent_runner, "_should_emit_cli_stream_chunk")
    assert (
        agent_runner._should_emit_cli_stream_chunk(
            "01234567", source="coordinator_1"
        )
        is False
    )
    assert (
        agent_runner._should_emit_cli_stream_chunk(
            "01234567", source="coordinator_agent"
        )
        is False
    )
    assert agent_runner._should_emit_cli_stream_chunk("short", source="basic_agent") is False
    assert (
        agent_runner._should_emit_cli_stream_chunk(
            "01234567", source="basic_agent"
        )
        is True
    )
    assert (
        agent_runner._should_emit_cli_stream_chunk(
            "01234567", source="coordinator_2"
        )
        is True
    )


def test_coordinator_routing_stream_keeps_full_timeline_delta_text(agent_runner):
    coordinator_agent = agent_runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []
    long_text = "0123456789" * 15

    session = AgentSession(
        session_id="coordinator-routing-long-delta-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )

    async def _collect():
        return [
            item
            async for item in agent_runner._run_coordinator_routing_stream(
                session, "请继续分析这个复杂问题", None
            )
        ]

    with patch.object(coordinator_agent, "write_memory_to_messages", return_value=[]), patch.object(
        coordinator_agent.model,
        "generate_stream",
        side_effect=_routing_stream_with_raw_state(coordinator_agent.model, long_text),
    ):
        outputs = asyncio.run(_collect())

    assert outputs[1] == {
        "kind": "timeline",
        "event": {
            "kind": "routing_started",
            "source": "coordinator_1",
            "message_id": "routing-card-coordinator-routing-long-delta-session",
        },
    }
    assert outputs[2] == {
        "kind": "timeline",
        "event": {
            "kind": "routing_delta",
            "source": "coordinator_1",
            "message_id": "routing-card-coordinator-routing-long-delta-session",
            "text": long_text,
            "partial": True,
        },
    }
    assert outputs[3] == {
        "kind": "timeline",
        "event": {
            "kind": "dispatch_delta",
            "source": "coordinator_1",
            "message_id": "routing-card-coordinator-routing-long-delta-session",
            "dispatch_id": "dispatch-coordinator-routing-long-delta-session-routing",
            "dispatch_kind": "routing",
            "display": "dispatch_card",
            "display_slot": "before_body",
            "text": "0123456789" * 10,
            "partial": True,
        },
    }
    assert outputs[4] == {
        "kind": "dispatch",
        "dispatch_status": "running",
        "dispatch_stage": "routing",
        "dispatch_kind": "routing",
        "display_slot": "before_body",
        "dispatch_id": "dispatch-coordinator-routing-long-delta-session-routing",
        "dispatch_message": "0123456789" * 10,
    }


def test_coordinator_routing_stream_preserves_raw_whitespace_in_timeline(agent_runner):
    coordinator_agent = agent_runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []
    raw_text = "第一行\n\n  第二行"

    session = AgentSession(
        session_id="coordinator-routing-whitespace-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )

    async def _collect():
        return [
            item
            async for item in agent_runner._run_coordinator_routing_stream(
                session, "请分析这个换行输入", None
            )
        ]

    with patch.object(coordinator_agent, "write_memory_to_messages", return_value=[]), patch.object(
        coordinator_agent.model,
        "generate_stream",
        side_effect=_routing_stream_with_raw_state(coordinator_agent.model, raw_text),
    ):
        outputs = asyncio.run(_collect())

    assert outputs[1] == {
        "kind": "timeline",
        "event": {
            "kind": "routing_started",
            "source": "coordinator_1",
            "message_id": "routing-card-coordinator-routing-whitespace-session",
        },
    }
    assert outputs[2] == {
        "kind": "timeline",
        "event": {
            "kind": "routing_delta",
            "source": "coordinator_1",
            "message_id": "routing-card-coordinator-routing-whitespace-session",
            "text": raw_text,
            "partial": True,
        },
    }
    assert outputs[3] == {
        "kind": "timeline",
        "event": {
            "kind": "dispatch_delta",
            "source": "coordinator_1",
            "message_id": "routing-card-coordinator-routing-whitespace-session",
            "dispatch_id": "dispatch-coordinator-routing-whitespace-session-routing",
            "dispatch_kind": "routing",
            "display": "dispatch_card",
            "display_slot": "before_body",
            "text": "第一行 第二行",
            "partial": True,
        },
    }
    assert outputs[4] == {
        "kind": "dispatch",
        "dispatch_status": "running",
        "dispatch_stage": "routing",
        "dispatch_kind": "routing",
        "display_slot": "before_body",
        "dispatch_id": "dispatch-coordinator-routing-whitespace-session-routing",
        "dispatch_message": "第一行 第二行",
    }


def test_coordinator_routing_stream_dispatch_message_preserves_word_boundaries(agent_runner):
    coordinator_agent = agent_runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []

    session = AgentSession(
        session_id="coordinator-routing-word-boundary-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )

    async def _collect():
        return [
            item
            async for item in agent_runner._run_coordinator_routing_stream(
                session, "Analyze the request", None
            )
        ]

    with patch.object(coordinator_agent, "write_memory_to_messages", return_value=[]), patch.object(
        coordinator_agent.model,
        "generate_stream",
        side_effect=_routing_stream_with_raw_state(
            coordinator_agent.model,
            "Analyze ",
            "the request",
        ),
    ):
        outputs = asyncio.run(_collect())

    assert outputs[7] == {
        "kind": "dispatch",
        "dispatch_status": "running",
        "dispatch_stage": "routing",
        "dispatch_kind": "routing",
        "display_slot": "before_body",
        "dispatch_id": "dispatch-coordinator-routing-word-boundary-session-routing",
        "dispatch_message": "Analyze the request",
    }


def test_coordinator_routing_stream_direct_answer_emits_terminal_routing_decision(
    agent_runner,
):
    coordinator_agent = agent_runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []

    session = AgentSession(
        session_id="coordinator-routing-direct-answer-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )

    async def _collect():
        return [
            item
            async for item in agent_runner._run_coordinator_routing_stream(
                session, "直接回答这个问题", None
            )
        ]

    with patch.object(coordinator_agent, "write_memory_to_messages", return_value=[]), patch.object(
        coordinator_agent.model,
        "generate_stream",
        side_effect=_routing_stream_with_raw_state(
            coordinator_agent.model,
            "协调器直接回复",
            raw_content="协调器直接回复",
        ),
    ):
        outputs = asyncio.run(_collect())

    assert outputs[-2] == {
        "kind": "timeline",
        "event": {
            "kind": "routing_decision",
            "source": "coordinator_1",
            "message_id": "routing-card-coordinator-routing-direct-answer-session",
            "decision": "direct_answer",
        },
    }
    assert outputs[-1] == {
        "kind": "result",
        "route": {"kind": "final", "answer": "协调器直接回复"},
    }


def test_coordinator_routing_stream_dispatch_truncation_avoids_mid_word_cut(agent_runner):
    coordinator_agent = agent_runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []
    long_text = "word " * 19 + "extraordinary"

    session = AgentSession(
        session_id="coordinator-routing-truncation-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )

    async def _collect():
        return [
            item
            async for item in agent_runner._run_coordinator_routing_stream(
                session, "truncate carefully", None
            )
        ]

    with patch.object(coordinator_agent, "write_memory_to_messages", return_value=[]), patch.object(
        coordinator_agent.model,
        "generate_stream",
        side_effect=_routing_stream_with_raw_state(coordinator_agent.model, long_text),
    ):
        outputs = asyncio.run(_collect())

    assert outputs[3] == {
        "kind": "timeline",
        "event": {
            "kind": "dispatch_delta",
            "source": "coordinator_1",
            "message_id": "routing-card-coordinator-routing-truncation-session",
            "dispatch_id": "dispatch-coordinator-routing-truncation-session-routing",
            "dispatch_kind": "routing",
            "display": "dispatch_card",
            "display_slot": "before_body",
            "text": "word " * 19,
            "partial": True,
        },
    }
    assert outputs[4] == {
        "kind": "dispatch",
        "dispatch_status": "running",
        "dispatch_stage": "routing",
        "dispatch_kind": "routing",
        "display_slot": "before_body",
        "dispatch_id": "dispatch-coordinator-routing-truncation-session-routing",
        "dispatch_message": "word " * 19,
    }


def test_coordinator_routing_stream_uses_canonical_timeline_source_for_legacy_profile(
    agent_runner,
):
    coordinator_agent = agent_runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []

    session = AgentSession(
        session_id="coordinator-routing-legacy-source-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_agent",
    )

    async def _collect():
        return [
            item
            async for item in agent_runner._run_coordinator_routing_stream(
                session, "legacy route", None
            )
        ]

    with patch.object(coordinator_agent, "write_memory_to_messages", return_value=[]), patch.object(
        coordinator_agent.model,
        "generate_stream",
        side_effect=_routing_stream_with_raw_state(
            coordinator_agent.model,
            "正在分析是否需要切换执行代理",
        ),
    ):
        outputs = asyncio.run(_collect())

    assert outputs[1] == {
        "kind": "timeline",
        "event": {
            "kind": "routing_started",
            "source": "coordinator_1",
            "message_id": "routing-card-coordinator-routing-legacy-source-session",
        },
    }
    assert outputs[2]["event"]["source"] == "coordinator_1"
    assert outputs[5]["event"]["source"] == "coordinator_1"


def test_coordinator_routing_stream_dispatch_truncation_avoids_mid_word_cut_after_accumulation(
    agent_runner,
):
    coordinator_agent = agent_runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []

    session = AgentSession(
        session_id="coordinator-routing-accumulated-truncation-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )

    async def _collect():
        return [
            item
            async for item in agent_runner._run_coordinator_routing_stream(
                session, "truncate across chunks", None
            )
        ]

    with patch.object(coordinator_agent, "write_memory_to_messages", return_value=[]), patch.object(
        coordinator_agent.model,
        "generate_stream",
        side_effect=_routing_stream_with_raw_state(
            coordinator_agent.model,
            ("word " * 18) + "wor",
            "d extraordinary",
        ),
    ):
        outputs = asyncio.run(_collect())

    assert outputs[7] == {
        "kind": "dispatch",
        "dispatch_status": "running",
        "dispatch_stage": "routing",
        "dispatch_kind": "routing",
        "display_slot": "before_body",
        "dispatch_id": "dispatch-coordinator-routing-accumulated-truncation-session-routing",
        "dispatch_message": "word " * 19,
    }


def test_coordinator_routing_stream_reasoning_only_switch_command_does_not_route(
    agent_runner,
):
    coordinator_agent = agent_runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []
    switch_command = "<<SWITCH_AGENT>> basic_agent"

    session = AgentSession(
        session_id="coordinator-routing-reasoning-switch-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )

    async def _collect():
        return [
            item
            async for item in agent_runner._run_coordinator_routing_stream(
                session, "使用浏览器 打开 example.com", None
            )
        ]

    reasoning_delta = ChatMessageStreamDelta(content=switch_command)
    reasoning_delta.reasoning = True
    with patch.object(coordinator_agent, "write_memory_to_messages", return_value=[]), patch.object(
        coordinator_agent.model,
        "generate_stream",
        return_value=iter([reasoning_delta]),
    ), patch.object(
        coordinator_agent.model,
        "get_stream_state",
        return_value={"content": "", "reasoning": switch_command},
    ):
        outputs = asyncio.run(_collect())

    route = next(item["route"] for item in outputs if item["kind"] == "result")
    assert route["kind"] == "invalid"
    assert not any(
        item.get("event", {}).get("target_agent") == "basic_agent"
        for item in outputs
    )


def test_coordinator_routing_stream_control_command_does_not_leak(
    agent_runner,
):
    coordinator_agent = agent_runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []

    session = AgentSession(
        session_id="coordinator-routing-split-switch-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )

    async def _collect():
        return [
            item
            async for item in agent_runner._run_coordinator_routing_stream(
                session, "split switch command", None
            )
        ]

    with patch.object(coordinator_agent, "write_memory_to_messages", return_value=[]), patch.object(
        coordinator_agent.model,
        "generate_stream",
        side_effect=_routing_stream_with_raw_state(coordinator_agent.model),
    ):
        outputs = asyncio.run(_collect())

    assert outputs == [
        {
            "kind": "dispatch",
            "dispatch_status": "running",
            "dispatch_stage": "routing",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
            "dispatch_id": "dispatch-coordinator-routing-split-switch-session-routing",
        },
        {
            "kind": "timeline",
            "event": {
                "kind": "routing_started",
                "source": "coordinator_1",
                "message_id": "routing-card-coordinator-routing-split-switch-session",
            },
        },
        {
            "kind": "timeline",
            "event": {
                "kind": "routing_decision",
                "source": "coordinator_1",
                "message_id": "routing-card-coordinator-routing-split-switch-session",
                "target_agent": "basic_agent",
            },
        },
        {
            "kind": "result",
            "route": {"kind": "switch", "target_agent": "basic_agent"},
        },
    ]


def test_coordinator_routing_stream_timeout_falls_back_to_basic_agent(agent_runner):
    coordinator_agent = agent_runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []

    session = AgentSession(
        session_id="coordinator-routing-timeout-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )
    agent_runner.config.agent.coordinator_1_timeout_seconds = 1

    async def _collect():
        return [
            item
            async for item in agent_runner._run_coordinator_routing_stream(
                session, "帮我搜索 Jetson Thor 部署方案", None
            )
        ]

    monotonic_values = iter([100.0, 100.2, 101.3])
    last_monotonic = 101.3

    def _fake_monotonic():
        nonlocal last_monotonic
        try:
            last_monotonic = next(monotonic_values)
        except StopIteration:
            pass
        return last_monotonic

    with patch.object(coordinator_agent, "write_memory_to_messages", return_value=[]), patch.object(
        coordinator_agent.model,
        "generate_stream",
        return_value=iter(
            [
                ChatMessageStreamDelta(content=""),
                ChatMessageStreamDelta(content="这条增量不应被消费"),
            ]
        ),
    ), patch("backend.agent.agent_runner.time.monotonic", side_effect=_fake_monotonic):
        outputs = asyncio.run(_collect())

    assert outputs == [
        {
            "kind": "dispatch",
            "dispatch_status": "running",
            "dispatch_stage": "routing",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
            "dispatch_id": "dispatch-coordinator-routing-timeout-session-routing",
        },
        {
            "kind": "timeline",
            "event": {
                "kind": "routing_started",
                "source": "coordinator_1",
                "message_id": "routing-card-coordinator-routing-timeout-session",
            },
        },
        {
            "kind": "timeline",
            "event": {
                "kind": "routing_timeout_fallback",
                "source": "coordinator_1",
                "message_id": "routing-card-coordinator-routing-timeout-session",
                "from_agent": "coordinator_1",
                "target_agent": "basic_agent",
                "reason": "timeout_fallback",
            },
        },
        {
            "kind": "timeline",
            "event": {
                "kind": "dispatch_delta",
                "source": "coordinator_1",
                "message_id": "routing-card-coordinator-routing-timeout-session",
                "dispatch_id": "dispatch-coordinator-routing-timeout-session-routing",
                "dispatch_kind": "routing",
                "display": "dispatch_card",
                "display_slot": "before_body",
                "target_agent": "basic_agent",
                "text": "coordinator_1 路由超时，回退到 basic_agent",
                "partial": True,
            },
        },
        {
            "kind": "dispatch",
            "dispatch_status": "running",
            "dispatch_stage": "routing",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
            "dispatch_id": "dispatch-coordinator-routing-timeout-session-routing",
            "dispatch_message": "coordinator_1 路由超时，回退到 basic_agent",
        },
        {
            "kind": "result",
            "route": {
                "kind": "switch",
                "target_agent": "basic_agent",
                "reason": "timeout_fallback",
            },
        },
    ]


@pytest.mark.anyio
async def test_coordinator_routing_stream_timeout_falls_back_when_stream_blocks_before_first_delta(
    agent_runner,
):
    coordinator_agent = agent_runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []

    session = AgentSession(
        session_id="coordinator-routing-blocked-timeout-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )
    agent_runner.config.agent.coordinator_1_timeout_seconds = 0.05

    started_block = threading.Event()
    worker_stopped = threading.Event()

    class BlockingStream:
        def __init__(self, request_id):
            self.request_id = request_id

        def __iter__(self):
            return self

        def __next__(self):
            started_block.set()
            while not is_request_aborted(self.request_id):
                time.sleep(0.001)
            time.sleep(0.02)
            worker_stopped.set()
            raise StopIteration

    outputs = None
    error = None
    worker = None
    worker_stopped_before_return = None

    def _run_stream():
        nonlocal outputs, error, worker_stopped_before_return
        try:
            outputs = asyncio.run(
                _collect_coordinator_events(
                    agent_runner._run_coordinator_routing_stream(
                        session, "帮我搜索 Jetson Thor 部署方案", None
                    )
                )
            )
            worker_stopped_before_return = worker_stopped.is_set()
        except Exception as exc:  # pragma: no cover - test failure path
            error = exc

    def _build_blocking_stream(*args, **kwargs):
        return BlockingStream(kwargs["request_id"])

    with patch.object(coordinator_agent, "write_memory_to_messages", return_value=[]), patch.object(
        coordinator_agent.model,
        "generate_stream",
        side_effect=_build_blocking_stream,
    ):
        worker = threading.Thread(target=_run_stream, daemon=True)
        worker.start()
        assert started_block.wait(timeout=0.1) is True
        worker.join(timeout=0.4)

    if worker is not None and worker.is_alive():
        worker.join(timeout=1)
        pytest.fail("routing stream did not preempt blocked iterator before timeout")
    if error is not None:
        raise error

    assert started_block.is_set() is True
    assert worker_stopped.is_set() is True
    assert worker_stopped_before_return is True
    assert outputs == [
        {
            "kind": "dispatch",
            "dispatch_status": "running",
            "dispatch_stage": "routing",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
            "dispatch_id": "dispatch-coordinator-routing-blocked-timeout-session-routing",
        },
        {
            "kind": "timeline",
            "event": {
                "kind": "routing_started",
                "source": "coordinator_1",
                "message_id": "routing-card-coordinator-routing-blocked-timeout-session",
            },
        },
        {
            "kind": "timeline",
            "event": {
                "kind": "routing_timeout_fallback",
                "source": "coordinator_1",
                "message_id": "routing-card-coordinator-routing-blocked-timeout-session",
                "from_agent": "coordinator_1",
                "target_agent": "basic_agent",
                "reason": "timeout_fallback",
            },
        },
        {
            "kind": "timeline",
            "event": {
                "kind": "dispatch_delta",
                "source": "coordinator_1",
                "message_id": "routing-card-coordinator-routing-blocked-timeout-session",
                "dispatch_id": "dispatch-coordinator-routing-blocked-timeout-session-routing",
                "dispatch_kind": "routing",
                "display": "dispatch_card",
                "display_slot": "before_body",
                "target_agent": "basic_agent",
                "text": "coordinator_1 路由超时，回退到 basic_agent",
                "partial": True,
            },
        },
        {
            "kind": "dispatch",
            "dispatch_status": "running",
            "dispatch_stage": "routing",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
            "dispatch_id": "dispatch-coordinator-routing-blocked-timeout-session-routing",
            "dispatch_message": "coordinator_1 路由超时，回退到 basic_agent",
        },
        {
            "kind": "result",
            "route": {
                "kind": "switch",
                "target_agent": "basic_agent",
                "reason": "timeout_fallback",
            },
        },
    ]


def test_build_answer_now_prompt_is_self_contained_no_tool_tail(agent_runner):
    agent = MagicMock()
    agent.memory.steps = [
        TaskStep(task="请搜索张雪机车的相关信息，并整理成详细答案。"),
        ActionStep(
            step_number=1,
            timing=Timing(start_time=0.0, end_time=0.1),
            model_output="我已经找到多个来源，张雪机车是一家机车相关企业。",
            tool_calls=[],
            observations="来源包括百科和媒体报道。",
        ),
    ]
    session = AgentSession(session_id="answer-now-session", agent=agent, mode="tool_calling")

    prompt = agent_runner._build_answer_now_prompt(session)

    assert "Do not call or execute any tool in this Answer Now response" in prompt
    assert "Use no tools for this response only" in prompt
    assert "This restriction does not apply to later user turns" in prompt
    assert "Do not route, hand off, continue planning" in prompt
    assert "answer-now-session" not in prompt
    assert "请搜索张雪机车" not in prompt
    assert "来源包括百科" not in prompt


@pytest.mark.anyio
async def test_answer_now_waits_for_captured_worker_before_freezing_memory(agent_runner):
    model_called = threading.Event()
    mock_model = MagicMock()
    mock_model.generate_stream.side_effect = lambda *args, **kwargs: (
        model_called.set() or iter([ChatMessageStreamDelta(content="final")])
    )
    mock_agent = MagicMock()
    mock_agent.model = mock_model
    mock_agent.memory.steps = [TaskStep(task="original")]
    mock_agent.tools = {"read_file": MagicMock()}
    mock_agent.managed_agents = {}

    def serialize_memory():
        return [
            ChatMessage(role=MessageRole.USER, content=[{"type": "text", "text": "original"}]),
            ChatMessage(role=MessageRole.ASSISTANT, content=[{"type": "text", "text": "old partial"}]),
        ]

    mock_agent.write_memory_to_messages.side_effect = serialize_memory
    session = AgentSession(session_id="answer-now-barrier", agent=mock_agent, mode="tool_calling")
    agent_runner.sessions.put(session)
    old_scope = session.begin_run()
    assert session.try_begin_answer_now()

    stream = agent_runner.run_answer_now_stream(
        session.session_id,
        captured_scope_ids=frozenset({old_scope}),
        single_flight_acquired=True,
    )
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.05)
    assert not model_called.is_set()

    session.finish_run(old_scope)
    assert "session_start" in await pending
    events = [event async for event in stream]
    assert any('"type": "done"' in event for event in events)

    replacement_messages = mock_model.generate_stream.call_args.args[0]
    assert replacement_messages[:-1] == serialize_memory()
    assert replacement_messages[-1].role == MessageRole.USER
    tail = replacement_messages[-1].content[0]["text"]
    assert "Do not call or execute any tool in this Answer Now response" in tail
    assert "Use no tools for this response only" in tail
    assert "This restriction does not apply to later user turns" in tail
    assert mock_model.generate_stream.call_args.kwargs["tools_to_call_from"] == list(
        mock_agent.tools.values()
    )


@pytest.mark.anyio
async def test_answer_now_cancelled_during_barrier_never_starts_replacement(agent_runner):
    mock_agent = MagicMock()
    mock_agent.model = MagicMock()
    mock_agent.memory.steps = []
    session = AgentSession(session_id="answer-now-cancelled-wait", agent=mock_agent, mode="tool_calling")
    agent_runner.sessions.put(session)
    old_scope = session.begin_run()
    cancelled = threading.Event()
    assert session.try_begin_answer_now()

    task = asyncio.create_task(
        agent_runner.generate_answer_now_reply(
            session.session_id,
            captured_scope_ids=frozenset({old_scope}),
            single_flight_acquired=True,
            cancelled=cancelled.is_set,
        )
    )
    await asyncio.sleep(0.05)
    cancelled.set()
    result = await task

    assert result["status"] == "cancelled"
    mock_agent.model.generate_stream.assert_not_called()
    assert session.try_begin_answer_now() is False
    session.finish_answer_now()
    assert session.try_begin_answer_now() is True
    session.finish_answer_now()
    session.finish_run(old_scope)


@pytest.mark.anyio
async def test_answer_now_task_cancelled_during_barrier_keeps_external_single_flight(
    agent_runner,
):
    mock_agent = MagicMock()
    mock_agent.memory.steps = []
    session = AgentSession(session_id="answer-now-task-cancel", agent=mock_agent, mode="tool_calling")
    agent_runner.sessions.put(session)
    old_scope = session.begin_run()
    assert session.try_begin_answer_now()

    stream = agent_runner.run_answer_now_stream(
        session.session_id,
        captured_scope_ids=frozenset({old_scope}),
        single_flight_acquired=True,
    )
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.05)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert session.try_begin_answer_now() is False
    session.finish_answer_now()
    assert session.try_begin_answer_now() is True
    session.finish_answer_now()
    session.finish_run(old_scope)


@pytest.mark.anyio
async def test_answer_now_external_single_flight_remains_owned_after_runner_stream_closes(
    agent_runner,
):
    model = MagicMock()
    model.generate_stream.return_value = iter(
        [ChatMessageStreamDelta(content="final")]
    )
    runtime_agent = MagicMock()
    runtime_agent.model = model
    runtime_agent.memory.steps = []
    runtime_agent.tools = {}
    runtime_agent.managed_agents = {}
    runtime_agent.write_memory_to_messages.return_value = []
    session = AgentSession(
        session_id="answer-now-outer-owner",
        agent=runtime_agent,
        mode="tool_calling",
    )
    agent_runner.sessions.put(session)
    assert session.try_begin_answer_now() is True

    events = [
        event
        async for event in agent_runner.run_answer_now_stream(
            session.session_id,
            single_flight_acquired=True,
        )
    ]

    assert '"type": "done"' in "".join(events)
    assert session.try_begin_answer_now() is False
    session.finish_answer_now()
    assert session.try_begin_answer_now() is True
    session.finish_answer_now()


@pytest.mark.anyio
async def test_answer_now_detects_textual_tool_syntax_without_leaking_attempt(agent_runner):
    framed = _tool_block("read_file", '{\n  "file_path": "/tmp/a"\n}')
    model = MagicMock()
    model.generate_stream.side_effect = [
        iter([ChatMessageStreamDelta(content=framed)]),
        iter([ChatMessageStreamDelta(content="safe answer")]),
    ]
    runtime_agent = MagicMock()
    runtime_agent.model = model
    runtime_agent.memory.steps = []
    runtime_agent.tools = {}
    runtime_agent.managed_agents = {}
    runtime_agent.write_memory_to_messages.return_value = []
    session = AgentSession(session_id="answer-now-text-tool", agent=runtime_agent, mode="tool_calling")
    agent_runner.sessions.put(session)

    events = [event async for event in agent_runner.run_answer_now_stream(session.session_id)]

    body = "".join(events)
    assert framed not in body
    assert "safe answer" in body
    assert model.generate_stream.call_count == 2


@pytest.mark.anyio
async def test_answer_now_rejects_gated_raw_framed_call_and_replays_only_retry(
    agent_runner,
):
    parser = LocalLLMModel.__new__(LocalLLMModel)
    framed = _tool_block("read_file", '{\n  "file_path": "/tmp/a"\n}')
    model = MagicMock()
    model._resolve_assistant_control.side_effect = parser._resolve_assistant_control
    model.generate_stream.side_effect = [
        iter(()),  # StreamControlGate suppresses every protocol byte.
        iter([ChatMessageStreamDelta(content="safe answer")]),
    ]
    model.get_stream_state.side_effect = [
        {"content": framed, "reasoning": ""},
        {"content": "safe answer", "reasoning": ""},
    ]
    read_file = MagicMock()
    read_file.name = "read_file"
    read_file.inputs = {"file_path": {"type": "string", "required": True}}
    runtime_agent = MagicMock()
    runtime_agent.model = model
    runtime_agent.memory.steps = []
    runtime_agent.tools = {"read_file": read_file}
    runtime_agent.managed_agents = {}
    runtime_agent.write_memory_to_messages.return_value = []
    runtime_agent.process_tool_calls = MagicMock()
    session = AgentSession(
        session_id="answer-now-gated-framed",
        agent=runtime_agent,
        mode="tool_calling",
    )
    agent_runner.sessions.put(session)

    events = [event async for event in agent_runner.run_answer_now_stream(session.session_id)]

    body = "".join(events)
    assert framed not in body
    assert "safe answer" in body
    assert model.generate_stream.call_count == 2
    runtime_agent.process_tool_calls.assert_not_called()


@pytest.mark.anyio
async def test_answer_now_rejects_native_plus_new_text_dual_encoding_atomically(
    agent_runner,
):
    parser = LocalLLMModel.__new__(LocalLLMModel)
    framed = _tool_block("read_file", '{\n  "file_path": "/tmp/a"\n}')
    native_delta = ChatMessageStreamDelta(tool_calls=[MagicMock()])
    model = MagicMock()
    model._resolve_assistant_control.side_effect = parser._resolve_assistant_control
    model.generate_stream.side_effect = [
        iter([native_delta]),
        iter([ChatMessageStreamDelta(content="corrected answer")]),
    ]
    model.get_stream_state.side_effect = [
        {"content": framed, "reasoning": ""},
        {"content": "corrected answer", "reasoning": ""},
    ]
    runtime_agent = MagicMock()
    runtime_agent.model = model
    runtime_agent.memory.steps = []
    runtime_agent.tools = {}
    runtime_agent.managed_agents = {}
    runtime_agent.write_memory_to_messages.return_value = []
    runtime_agent.process_tool_calls = MagicMock()
    session = AgentSession(
        session_id="answer-now-dual-control",
        agent=runtime_agent,
        mode="tool_calling",
    )
    agent_runner.sessions.put(session)

    events = [event async for event in agent_runner.run_answer_now_stream(session.session_id)]

    body = "".join(events)
    assert framed not in body
    assert "corrected answer" in body
    runtime_agent.process_tool_calls.assert_not_called()


@pytest.mark.anyio
async def test_answer_now_second_gated_framed_violation_is_explicit_error(agent_runner):
    parser = LocalLLMModel.__new__(LocalLLMModel)
    framed = _tool_block("read_file", '{\n  "file_path": "/tmp/a"\n}')
    model = MagicMock()
    model._resolve_assistant_control.side_effect = parser._resolve_assistant_control
    model.generate_stream.side_effect = [iter(()), iter(())]
    model.get_stream_state.side_effect = [
        {"content": framed, "reasoning": ""},
        {"content": framed, "reasoning": ""},
    ]
    read_file = MagicMock()
    read_file.name = "read_file"
    read_file.inputs = {"file_path": {"type": "string", "required": True}}
    runtime_agent = MagicMock()
    runtime_agent.model = model
    runtime_agent.memory.steps = []
    runtime_agent.tools = {"read_file": read_file}
    runtime_agent.managed_agents = {}
    runtime_agent.write_memory_to_messages.return_value = []
    runtime_agent.process_tool_calls = MagicMock()
    session = AgentSession(
        session_id="answer-now-second-gated-control",
        agent=runtime_agent,
        mode="tool_calling",
    )
    agent_runner.sessions.put(session)

    events = [event async for event in agent_runner.run_answer_now_stream(session.session_id)]

    body = "".join(events)
    assert "emitted tool calls twice" in body
    assert framed not in body
    assert '"type": "content"' not in body
    runtime_agent.process_tool_calls.assert_not_called()


@pytest.mark.anyio
async def test_answer_now_does_not_parse_tool_transcript_from_reasoning(agent_runner):
    reasoning_delta = ChatMessageStreamDelta(
        content=(
            "Reviewing an earlier transcript:\n"
            + _tool_block("read_file", '{\n  "file_path": "/tmp/a"\n}')
        )
    )
    reasoning_delta.reasoning = True
    answer_delta = ChatMessageStreamDelta(content="safe final answer")
    answer_delta.reasoning = False

    model = MagicMock()
    model.generate_stream.side_effect = [
        iter([reasoning_delta, answer_delta]),
        iter([ChatMessageStreamDelta(content="unnecessary corrected answer")]),
    ]
    runtime_agent = MagicMock()
    runtime_agent.model = model
    runtime_agent.memory.steps = []
    runtime_agent.tools = {"read_file": MagicMock()}
    runtime_agent.managed_agents = {}
    runtime_agent.write_memory_to_messages.return_value = []
    session = AgentSession(
        session_id="answer-now-reasoning-tool-text",
        agent=runtime_agent,
        mode="tool_calling",
    )
    agent_runner.sessions.put(session)

    events = [event async for event in agent_runner.run_answer_now_stream(session.session_id)]

    body = "".join(events)
    assert "safe final answer" in body
    assert "unnecessary corrected answer" not in body
    assert model.generate_stream.call_count == 1
    model._resolve_assistant_control.assert_called_once()
    assert model._resolve_assistant_control.call_args.args[0] == "safe final answer"


@pytest.mark.anyio
async def test_answer_now_stream_replays_accepted_deltas_in_order_after_completion(agent_runner):
    allow_second = threading.Event()

    def generate_stream(*args, **kwargs):
        yield ChatMessageStreamDelta(content="first")
        allow_second.wait(timeout=1)
        yield ChatMessageStreamDelta(content="second")

    model = MagicMock()
    model.generate_stream.side_effect = generate_stream
    runtime_agent = MagicMock()
    runtime_agent.model = model
    runtime_agent.memory.steps = []
    runtime_agent.tools = {}
    runtime_agent.managed_agents = {}
    runtime_agent.write_memory_to_messages.return_value = []
    session = AgentSession(session_id="answer-now-incremental", agent=runtime_agent, mode="tool_calling")
    agent_runner.sessions.put(session)

    stream = agent_runner.run_answer_now_stream(session.session_id)
    assert "session_start" in await anext(stream)
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.05)
    assert not pending.done()
    allow_second.set()
    first = await asyncio.wait_for(pending, timeout=1)
    rest = [event async for event in stream]
    assert "first" in first
    assert "second" in "".join(rest)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool_syntax",
    [
        _tool_block("read_file", '{\n  "file_path": "/tmp/a"\n}'),
        "<<SWITCH_AGENT>> basic_agent",
        '<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{\"file_path\":\"/tmp/a\"}}',
        'Calling tools:\n[{"function":{"name":"read_file","arguments":{}}}]',
    ],
    ids=["tool-call", "switch-agent", "missing-closer", "legacy-opener"],
)
async def test_answer_now_buffers_whole_attempt_before_rejecting_textual_tool_calls(
    agent_runner, tool_syntax
):
    model = MagicMock()
    model.generate_stream.side_effect = [
        iter([ChatMessageStreamDelta(content=tool_syntax)]),
        iter(
            [
                ChatMessageStreamDelta(content="safe "),
                ChatMessageStreamDelta(content="answer"),
            ]
        ),
    ]
    runtime_agent = MagicMock()
    runtime_agent.model = model
    runtime_agent.memory.steps = []
    runtime_agent.tools = {"read_file": MagicMock()}
    runtime_agent.managed_agents = {}
    runtime_agent.write_memory_to_messages.return_value = []
    runtime_agent.process_tool_calls = MagicMock()
    session = AgentSession(session_id="answer-now-text-syntax", agent=runtime_agent, mode="tool_calling")
    agent_runner.sessions.put(session)

    events = [event async for event in agent_runner.run_answer_now_stream(session.session_id)]

    body = "".join(events)
    assert tool_syntax not in body
    assert body.index('"content": "safe "') < body.index('"content": "answer"')
    assert model.generate_stream.call_count == 2
    runtime_agent.process_tool_calls.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "stream_events",
    [
        [],
        [ChatMessageStreamDelta(content=" \t\n")],
    ],
    ids=["empty", "whitespace-only"],
)
async def test_answer_now_blank_stream_does_not_replay_or_commit_final_step(
    agent_runner, stream_events
):
    model = MagicMock()
    model.generate_stream.return_value = iter(stream_events)
    runtime_agent = MagicMock()
    runtime_agent.model = model
    runtime_agent.memory.steps = []
    runtime_agent.tools = {}
    runtime_agent.managed_agents = {}
    runtime_agent.write_memory_to_messages.return_value = []
    session = AgentSession(session_id="answer-now-empty", agent=runtime_agent, mode="tool_calling")
    agent_runner.sessions.put(session)

    events = [event async for event in agent_runner.run_answer_now_stream(session.session_id)]

    assert runtime_agent.memory.steps == []
    body = "".join(events)
    assert '"type": "content"' not in body
    assert '"type": "error"' in body


@pytest.mark.anyio
async def test_answer_now_aborted_partial_stream_does_not_commit_final_step(agent_runner):
    model = MagicMock()
    model.generate_stream.return_value = iter([ChatMessageStreamDelta(content="partial")])
    model.was_stream_aborted.return_value = True
    runtime_agent = MagicMock()
    runtime_agent.model = model
    runtime_agent.memory.steps = []
    runtime_agent.tools = {}
    runtime_agent.managed_agents = {}
    runtime_agent.write_memory_to_messages.return_value = []
    session = AgentSession(session_id="answer-now-aborted", agent=runtime_agent, mode="tool_calling")
    agent_runner.sessions.put(session)

    events = [event async for event in agent_runner.run_answer_now_stream(session.session_id)]

    assert runtime_agent.memory.steps == []
    assert '"type": "error"' in "".join(events)


@pytest.mark.anyio
async def test_answer_now_cancellation_after_replacement_starts_target_aborts_without_commit(
    agent_runner,
):
    replacement_started = threading.Event()
    release_provider = threading.Event()
    cancelled = threading.Event()

    def generate_stream(*args, **kwargs):
        replacement_started.set()
        release_provider.wait(timeout=2)
        yield ChatMessageStreamDelta(content="must not commit")

    model = MagicMock()
    model.generate_stream.side_effect = generate_stream
    runtime_agent = MagicMock()
    runtime_agent.model = model
    runtime_agent.memory.steps = []
    runtime_agent.tools = {}
    runtime_agent.managed_agents = {}
    runtime_agent.write_memory_to_messages.return_value = []
    session = AgentSession(session_id="answer-now-disconnect", agent=runtime_agent, mode="tool_calling")
    agent_runner.sessions.put(session)

    with patch("backend.llm_service.abort_request_by_id") as targeted_abort:
        task = asyncio.create_task(
            agent_runner.generate_answer_now_reply(
                session.session_id,
                cancelled=cancelled.is_set,
            )
        )
        for _ in range(100):
            if replacement_started.is_set():
                break
            await asyncio.sleep(0.01)
        assert replacement_started.is_set()
        request_id = model.generate_stream.call_args.kwargs["request_id"]
        cancelled.set()

        for _ in range(50):
            if targeted_abort.called:
                break
            await asyncio.sleep(0.01)
        targeted_abort.assert_called_once()
        assert targeted_abort.call_args.args[0] == request_id
        assert targeted_abort.call_args.kwargs["fallback_to_abort_all"] is False
        release_provider.set()
        result = await asyncio.wait_for(task, timeout=2)

    assert result["status"] == "cancelled"
    assert runtime_agent.memory.steps == []


@pytest.mark.anyio
async def test_answer_now_tool_call_gets_one_append_only_correction(agent_runner):
    mock_model = MagicMock()
    mock_model.generate_stream.side_effect = [
        iter([ChatMessageStreamDelta(tool_calls=[MagicMock()])]),
        iter([ChatMessageStreamDelta(content="corrected final")]),
    ]
    mock_agent = MagicMock()
    mock_agent.model = mock_model
    mock_agent.memory.steps = []
    mock_agent.tools = {"read_file": MagicMock()}
    mock_agent.managed_agents = {}
    mock_agent.write_memory_to_messages.return_value = []
    mock_agent.process_tool_calls = MagicMock()
    session = AgentSession(session_id="answer-now-correction", agent=mock_agent, mode="tool_calling")
    agent_runner.sessions.put(session)

    events = [event async for event in agent_runner.run_answer_now_stream(session.session_id)]

    assert any("corrected final" in event for event in events)
    assert mock_model.generate_stream.call_count == 2
    first_messages = mock_model.generate_stream.call_args_list[0].args[0]
    second_messages = mock_model.generate_stream.call_args_list[1].args[0]
    first_request_id = mock_model.generate_stream.call_args_list[0].kwargs["request_id"]
    second_request_id = mock_model.generate_stream.call_args_list[1].kwargs["request_id"]
    assert second_messages[:-1] == first_messages
    assert second_messages[-1].role == MessageRole.USER
    assert second_request_id != first_request_id
    mock_model.clear_stream_state.assert_any_call(first_request_id)
    mock_model.clear_stream_state.assert_any_call(second_request_id)
    mock_agent.process_tool_calls.assert_not_called()


@pytest.mark.anyio
async def test_answer_now_second_tool_call_violation_is_explicit_error(agent_runner):
    mock_model = MagicMock()
    mock_model.generate_stream.side_effect = [
        iter([ChatMessageStreamDelta(tool_calls=[MagicMock()])]),
        iter([ChatMessageStreamDelta(tool_calls=[MagicMock()])]),
    ]
    mock_agent = MagicMock()
    mock_agent.model = mock_model
    mock_agent.memory.steps = []
    mock_agent.tools = {"read_file": MagicMock()}
    mock_agent.managed_agents = {}
    mock_agent.write_memory_to_messages.return_value = []
    mock_agent.process_tool_calls = MagicMock()
    session = AgentSession(session_id="answer-now-second-violation", agent=mock_agent, mode="tool_calling")
    agent_runner.sessions.put(session)

    events = [event async for event in agent_runner.run_answer_now_stream(session.session_id)]

    assert any("emitted tool calls twice" in event for event in events)
    assert not any('"type": "content"' in event for event in events)
    mock_agent.process_tool_calls.assert_not_called()


@pytest.mark.anyio
async def test_generate_answer_now_reply_uses_same_session_without_reset(agent_runner):
    mock_agent = MagicMock()
    mock_agent.tools = {}
    mock_agent.memory.steps = [TaskStep(task="原始任务")]
    mock_agent.managed_agents = {}
    mock_agent.write_memory_to_messages.return_value = []
    mock_agent.model.generate_stream.return_value = iter(
        [ChatMessageStreamDelta(content="基于已有信息整理出的最终答案")]
    )

    session = AgentSession(
        session_id="answer-now-session",
        agent=mock_agent,
        mode="tool_calling",
    )
    session.has_run = True
    agent_runner.sessions.put(session)

    result = await agent_runner.generate_answer_now_reply("answer-now-session")

    assert result == {
        "status": "ok",
        "session_id": "answer-now-session",
        "answer": "基于已有信息整理出的最终答案",
    }
    mock_agent.model.generate_stream.assert_called_once()
    assert session.answering_now is False


@pytest.mark.anyio
async def test_generate_answer_now_reply_uses_fresh_run_abort_scope(agent_runner):
    observed_session_abort_states = []
    observed_run_abort_states = []

    mock_agent = MagicMock()
    mock_agent.memory.steps = [TaskStep(task="原始任务")]
    mock_agent.tools = {}

    def _run(*args, **kwargs):
        run_scope_id = current_session_context.get_abort_scope()
        observed_session_abort_states.append(
            abort_manager.is_aborted("answer-now-abort-session")
        )
        observed_run_abort_states.append(run_abort_manager.is_aborted(run_scope_id))
        return iter([ChatMessageStreamDelta(content="answer-now answer")])

    mock_agent.managed_agents = {}
    mock_agent.write_memory_to_messages.return_value = []
    mock_agent.model.generate_stream.side_effect = _run

    session = AgentSession(
        session_id="answer-now-abort-session",
        agent=mock_agent,
        mode="tool_calling",
    )
    session.has_run = True
    session.abort()
    agent_runner.sessions.put(session)
    abort_manager.abort("answer-now-abort-session")

    try:
        result = await agent_runner.generate_answer_now_reply("answer-now-abort-session")
    finally:
        abort_manager.cleanup("answer-now-abort-session")

    assert result == {
        "status": "ok",
        "session_id": "answer-now-abort-session",
        "answer": "answer-now answer",
    }
    assert observed_session_abort_states == [True]
    assert observed_run_abort_states == [False]
    assert session.aborted is True


@pytest.mark.anyio
async def test_generate_answer_now_reply_preserves_existing_run_abort_scope(agent_runner):
    observed_original_scope_states = []
    observed_replacement_scope_states = []

    mock_agent = MagicMock()
    mock_agent.memory.steps = [TaskStep(task="原始任务")]
    mock_agent.tools = {}

    session = AgentSession(
        session_id="answer-now-preserve-abort-scope",
        agent=mock_agent,
        mode="tool_calling",
    )
    session.has_run = True
    agent_runner.sessions.put(session)

    original_run_scope_id = session.begin_run()
    run_abort_manager.abort(original_run_scope_id)

    def _run(*args, **kwargs):
        replacement_scope_id = current_session_context.get_abort_scope()
        observed_original_scope_states.append(
            run_abort_manager.is_aborted(original_run_scope_id)
        )
        observed_replacement_scope_states.append(
            run_abort_manager.is_aborted(replacement_scope_id)
        )
        return iter([ChatMessageStreamDelta(content="answer-now answer")])

    mock_agent.managed_agents = {}
    mock_agent.write_memory_to_messages.return_value = []
    mock_agent.model.generate_stream.side_effect = _run

    try:
        result = await agent_runner.generate_answer_now_reply(
            "answer-now-preserve-abort-scope"
        )
        assert result == {
            "status": "ok",
            "session_id": "answer-now-preserve-abort-scope",
            "answer": "answer-now answer",
        }
        assert observed_original_scope_states == [True]
        assert observed_replacement_scope_states == [False]
        assert run_abort_manager.is_aborted(original_run_scope_id) is True
    finally:
        session.finish_run(original_run_scope_id)


@pytest.mark.anyio
async def test_run_stream_executes_primary_session_agent_not_active_agent(agent_runner):
    primary_agent = MagicMock()
    primary_agent.memory.steps = []
    primary_agent.tools = {"primary": MagicMock()}
    primary_agent.run.return_value = []

    runtime_agent = MagicMock()
    runtime_agent.memory.steps = []
    runtime_agent.tools = {"runtime": MagicMock()}

    session = AgentSession(
        session_id="stream-primary-agent-session",
        agent=primary_agent,
        mode="tool_calling",
    )
    session.active_agent = runtime_agent

    with patch.object(agent_runner, "get_or_create_session", return_value=session):
        events = await _collect_stream_events(
            agent_runner.run_stream("stream-primary-agent-session", "test message")
        )

    assert any('"type": "session_start"' in event for event in events)
    assert any('"type": "done"' in event for event in events)
    primary_agent.run.assert_called_once_with(
        "test message",
        stream=True,
        images=None,
        reset=True,
    )
    runtime_agent.run.assert_not_called()


@pytest.mark.anyio
async def test_run_answer_now_stream_uses_active_runtime_agent(agent_runner):
    primary_model = MagicMock()
    primary_agent = MagicMock()
    primary_agent.model = primary_model
    primary_agent.memory.steps = [TaskStep(task="主 agent 任务")]
    primary_agent.tools = {"primary": MagicMock()}
    primary_agent.managed_agents = {}
    primary_agent.write_memory_to_messages.return_value = []

    runtime_model = MagicMock()
    runtime_agent = MagicMock()
    runtime_agent.model = runtime_model
    runtime_agent.memory.steps = [TaskStep(task="runtime agent 任务")]
    runtime_agent.tools = {"runtime": MagicMock()}
    runtime_agent.managed_agents = {}
    runtime_agent.write_memory_to_messages.return_value = [
        ChatMessage(
            role=MessageRole.USER,
            content=[{"type": "text", "text": "runtime agent 任务"}],
        )
    ]

    session = AgentSession(
        session_id="answer-now-stream-primary-agent-session",
        agent=primary_agent,
        mode="tool_calling",
    )
    session.active_agent = runtime_agent
    session.has_run = True
    agent_runner.sessions.put(session)

    runtime_model.generate_stream.return_value = iter(
        [ChatMessageStreamDelta(content="runtime agent 输出")]
    )

    events = [
        event
        async for event in agent_runner.run_answer_now_stream(
            "answer-now-stream-primary-agent-session"
        )
    ]

    payloads = [json.loads(event[6:]) for event in events]
    assert payloads[1] == {"type": "content", "content": "runtime agent 输出"}
    primary_model.generate_stream.assert_not_called()
    runtime_model.generate_stream.assert_called_once()
    messages = runtime_model.generate_stream.call_args.args[0]
    assert "runtime agent 任务" in messages[0].content[0]["text"]
    assert "主 agent 任务" not in messages[0].content[0]["text"]


@pytest.mark.anyio
async def test_run_basic_stream_cleanup_does_not_clear_newer_request_id(agent_runner):
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    session = AgentSession(
        session_id="stream-request-cleanup-session",
        agent=agent,
        mode="tool_calling",
    )

    def _run(*args, **kwargs):
        session_request_manager.set(session.session_id, "req-new")
        return iter([])

    agent.run.side_effect = _run

    session_request_manager.set(session.session_id, "req-old")
    try:
        events = [
            event
            async for event in agent_runner._run_basic_stream(session, "hello", None)
        ]
        assert any('"type": "done"' in event for event in events)
        assert agent_runner.get_current_request_id(session.session_id) == "req-new"
    finally:
        session_request_manager.cleanup(session.session_id)


@pytest.mark.anyio
async def test_run_basic_stream_generator_close_keeps_run_busy_until_worker_exits(agent_runner):
    started = threading.Event()
    release = threading.Event()

    class BlockingRun:
        def __iter__(self):
            return self

        def __next__(self):
            started.set()
            release.wait(timeout=2)
            raise StopIteration

    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}
    agent.managed_agents = {}
    agent.stream_outputs = False
    agent.run.return_value = BlockingRun()

    session = AgentSession(
        session_id="generator-close-worker-still-running",
        agent=agent,
        mode="tool_calling",
    )

    stream = agent_runner._run_basic_stream(session, "hi", None)
    try:
        first_event = await anext(stream)
        assert '"type": "session_start"' in first_event
        assert started.wait(timeout=1)

        await stream.aclose()

        assert session.run_in_progress is True
        assert session.active_run_scope_ids
    finally:
        release.set()

    for _ in range(50):
        if not session.run_in_progress:
            break
        await asyncio.sleep(0.02)

    assert session.run_in_progress is False
    assert session.active_run_scope_ids == []


@pytest.mark.anyio
async def test_real_agent_runner_lifecycle_early_close_waits_for_inner_worker_scope(
    agent_runner,
):
    started = threading.Event()
    release = threading.Event()

    class BlockingRun:
        def __iter__(self):
            return self

        def __next__(self):
            started.set()
            release.wait(timeout=2)
            raise StopIteration

    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}
    agent.managed_agents = {}
    agent.run.return_value = BlockingRun()

    request_id = "018f6f44-75f4-7def-b746-01a31bc3d200"
    registry = RequestLifecycleRegistry()
    app = FastAPI()
    app.state.agent_runner = agent_runner
    app.state.request_lifecycle_registry = registry
    app.state.session_hub_runtime = SessionHubRuntime()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/agent/chat",
        "raw_path": b"/v1/agent/chat",
        "query_string": b"",
        "headers": [],
        "client": ("test", 1),
        "server": ("test", 80),
        "app": app,
    }
    raw_request = Request(scope)
    acceptance_sent = asyncio.Event()
    received_request = False

    async def receive():
        nonlocal received_request
        if not received_request:
            received_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await acceptance_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if (
            message.get("type") == "http.response.body"
            and b'"type": "request_accepted"' in message.get("body", b"")
        ):
            acceptance_sent.set()

    with patch.object(agent_runner, "_build_profile_agent", return_value=agent):
        response = await agent_chat(
            AgentChatRequest(
                session_id="real-runner-early-close",
                message="inspect",
                stream=True,
                request_id=request_id,
            ),
            raw_request,
        )
        await asyncio.wait_for(response(scope, receive, send), timeout=1)
        assert await _wait_threading_event(started)

        session = agent_runner.sessions.get("real-runner-early-close")
        assert session is not None
        assert session.run_in_progress is True
        assert session.active_run_scope_ids
        assert session.event_hub.subscriber_count == 0
        assert registry.get_status(request_id) == "running"

        release.set()
        for _ in range(100):
            if (
                not session.run_in_progress
                and registry.get_status(request_id) == "terminal"
            ):
                break
            await asyncio.sleep(0.01)

    assert session.run_in_progress is False
    assert session.active_run_scope_ids == []
    assert registry.get_status(request_id) == "terminal"


def test_finish_run_only_releases_the_exact_active_scope():
    session = AgentSession(
        session_id="exact-run-scope-cleanup",
        agent=MagicMock(),
        mode="tool_calling",
    )
    first_scope_id = session.begin_run()
    second_scope_id = session.begin_run()

    session.finish_run("unknown-stale-scope")
    assert session.active_run_scope_ids == [first_scope_id, second_scope_id]
    assert session._run_depth == 2
    assert session.run_in_progress is True

    session.finish_run(first_scope_id)
    assert session.active_run_scope_ids == [second_scope_id]
    assert session._run_depth == 1
    assert session.run_in_progress is True

    session.finish_run(first_scope_id)
    assert session.active_run_scope_ids == [second_scope_id]
    assert session._run_depth == 1
    assert session.run_in_progress is True

    session.finish_run(second_scope_id)
    assert session.active_run_scope_ids == []
    assert session._run_depth == 0
    assert session.run_in_progress is False


def test_wait_for_run_scopes_waits_only_for_captured_scopes():
    session = AgentSession(
        session_id="captured-run-scope-wait",
        agent=MagicMock(),
        mode="tool_calling",
    )
    first_scope_id = session.begin_run()
    second_scope_id = session.begin_run()
    captured_scope_ids = session.capture_active_run_scopes()
    result = []
    waiter = threading.Thread(
        target=lambda: result.append(session.wait_for_run_scopes(captured_scope_ids))
    )

    waiter.start()
    session.finish_run(first_scope_id)
    assert waiter.is_alive()

    later_scope_id = session.begin_run()
    session.finish_run(second_scope_id)
    waiter.join(timeout=1)

    assert result == [True]
    assert later_scope_id in session.active_run_scope_ids
    session.finish_run(later_scope_id)


def test_wait_for_run_scopes_stops_when_cancelled():
    session = AgentSession(
        session_id="cancelled-run-scope-wait",
        agent=MagicMock(),
        mode="tool_calling",
    )
    scope_id = session.begin_run()
    cancelled = threading.Event()
    result = []
    waiter = threading.Thread(
        target=lambda: result.append(
            session.wait_for_run_scopes(
                session.capture_active_run_scopes(), cancelled=cancelled.is_set
            )
        )
    )

    waiter.start()
    cancelled.set()
    waiter.join(timeout=1)

    assert result == [False]
    assert scope_id in session.active_run_scope_ids
    session.finish_run(scope_id)


def test_wait_for_run_scopes_cancellation_wins_when_last_scope_completes():
    session = AgentSession(
        session_id="cancelled-as-last-run-scope-completes",
        agent=MagicMock(),
        mode="tool_calling",
    )
    scope_id = session.begin_run()
    cancellation_checked = threading.Event()
    result = []

    def cancelled():
        cancellation_checked.set()
        return scope_id not in session.active_run_scope_ids

    waiter = threading.Thread(
        target=lambda: result.append(
            session.wait_for_run_scopes(
                session.capture_active_run_scopes(),
                cancelled=cancelled,
            )
        )
    )

    waiter.start()
    assert cancellation_checked.wait(timeout=1)
    session.finish_run(scope_id)
    waiter.join(timeout=1)

    assert result == [False]


def test_answer_now_is_single_flight():
    session = AgentSession(
        session_id="answer-now-single-flight",
        agent=MagicMock(),
        mode="tool_calling",
    )

    assert session.try_begin_answer_now() is True
    assert session.try_begin_answer_now() is False

    session.finish_answer_now()

    assert session.try_begin_answer_now() is True
    session.finish_answer_now()


@pytest.mark.anyio
async def test_run_sync_tracks_run_in_progress_for_full_execution(agent_runner):
    started = threading.Event()
    release = threading.Event()

    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    session = AgentSession(
        session_id="sync-run-flag-session",
        agent=agent,
        mode="tool_calling",
    )

    def _blocking_run(*args, **kwargs):
        started.set()
        assert session.run_in_progress is True
        release.wait(timeout=1)
        return "done"

    agent.run.side_effect = _blocking_run

    with patch.object(agent_runner, "get_or_create_session", return_value=session), patch.object(
        agent_runner, "_decode_request_images", return_value=None
    ), patch.object(
        agent_runner,
        "_run_coordinator_routing_pass",
        create=True,
        return_value={"kind": "passthrough"},
    ):
        task = asyncio.create_task(
            agent_runner.run_sync("sync-run-flag-session", "test message")
        )
        await _wait_threading_event(started)
        assert session.run_in_progress is True
        release.set()
        result = await task

    assert result["answer"] == "done"
    assert session.run_in_progress is False


@pytest.mark.anyio
async def test_run_answer_now_stream_uses_model_stream_without_tools_and_appends_final_answer(agent_runner):
    mock_model = MagicMock()
    mock_model.generate_stream.return_value = iter(
        [
            ChatMessageStreamDelta(content="第一段"),
            ChatMessageStreamDelta(content="第二段"),
        ]
    )
    mock_agent = MagicMock()
    mock_agent.model = mock_model
    mock_agent.memory.steps = [TaskStep(task="原始任务")]
    mock_agent.tools = {"search": MagicMock()}
    mock_agent.managed_agents = {}

    session = AgentSession(
        session_id="answer-now-stream-session",
        agent=mock_agent,
        mode="tool_calling",
    )
    session.has_run = True
    agent_runner.sessions.put(session)

    mock_agent.write_memory_to_messages.return_value = []
    events = [
        event
        async for event in agent_runner.run_answer_now_stream(
            "answer-now-stream-session"
        )
    ]

    payloads = []
    for event in events:
        assert event.startswith("data: ")
        payloads.append(json.loads(event[6:]))

    assert payloads[0] == {
        "type": "session_start",
        "session_id": "answer-now-stream-session",
    }
    assert payloads[1] == {"type": "content", "content": "第一段"}
    assert payloads[2] == {"type": "content", "content": "第二段"}
    assert payloads[-1] == {
        "type": "done",
        "session_id": "answer-now-stream-session",
    }

    mock_model.generate_stream.assert_called_once()
    assert mock_model.generate_stream.call_args.kwargs["tools_to_call_from"] == list(
        mock_agent.tools.values()
    )
    assert session.answering_now is False
    appended_step = session.agent.memory.steps[-1]
    assert isinstance(appended_step, ActionStep)
    assert appended_step.is_final_answer is True
    assert appended_step.action_output == "第一段第二段"
    assert appended_step.model_output == "第一段第二段"
    answer_now_request_id = mock_model.generate_stream.call_args.kwargs["request_id"]
    mock_model.llm_service.discard_stream_usage.assert_called_once_with(answer_now_request_id)
    mock_model.clear_stream_state.assert_called_once_with(answer_now_request_id)


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["error", "cancel"])
async def test_run_answer_now_stream_clears_exact_scoped_resources_on_non_success(
    agent_runner, outcome
):
    mock_model = MagicMock()

    def _error_stream():
        raise RuntimeError("answer-now failure")
        yield

    if outcome == "error":
        mock_model.generate_stream.return_value = _error_stream()
    else:
        mock_model.generate_stream.return_value = iter(
            [ChatMessageStreamDelta(content="partial")]
        )

    mock_agent = MagicMock()
    mock_agent.model = mock_model
    mock_agent.memory.steps = [TaskStep(task="原始任务")]
    mock_agent.tools = {}
    mock_agent.managed_agents = {}
    mock_agent.write_memory_to_messages.return_value = []
    session = AgentSession(
        session_id=f"answer-now-{outcome}-cleanup-session",
        agent=mock_agent,
        mode="tool_calling",
    )
    session.has_run = True
    agent_runner.sessions.put(session)

    stream = agent_runner.run_answer_now_stream(session.session_id)
    if outcome == "error":
        events = [event async for event in stream]
        assert any('"type": "error"' in event for event in events)
    else:
        assert '"type": "session_start"' in await anext(stream)
        assert '"type": "content"' in await anext(stream)
        await stream.aclose()
        for _ in range(50):
            if not session.run_in_progress:
                break
            await asyncio.sleep(0.01)

    request_id = mock_model.generate_stream.call_args.kwargs["request_id"]
    mock_model.clear_stream_state.assert_called_once_with(request_id)
    mock_model.llm_service.discard_stream_usage.assert_called_once_with(request_id)


@pytest.mark.anyio
async def test_run_answer_now_stream_close_keeps_run_busy_until_worker_exits(
    agent_runner,
):
    started = threading.Event()
    release = threading.Event()
    scoped_output_ids = set()
    scoped_usage_ids = set()

    class BlockingStream:
        def __iter__(self):
            return self

        def __next__(self):
            started.set()
            release.wait(timeout=2)
            raise StopIteration

    mock_model = MagicMock()

    def _generate_stream(*args, **kwargs):
        request_id = kwargs["request_id"]
        scoped_output_ids.add(request_id)
        scoped_usage_ids.add(request_id)
        return BlockingStream()

    mock_model.generate_stream.side_effect = _generate_stream
    mock_model.clear_stream_state.side_effect = scoped_output_ids.discard
    mock_model.llm_service.discard_stream_usage.side_effect = scoped_usage_ids.discard
    mock_agent = MagicMock()
    mock_agent.model = mock_model
    mock_agent.memory.steps = [TaskStep(task="原始任务")]
    mock_agent.tools = {}
    mock_agent.managed_agents = {}
    mock_agent.write_memory_to_messages.return_value = []
    session = AgentSession(
        session_id="answer-now-blocked-worker-session",
        agent=mock_agent,
        mode="tool_calling",
    )
    session.has_run = True
    agent_runner.sessions.put(session)

    stream = agent_runner.run_answer_now_stream(session.session_id)
    assert '"type": "session_start"' in await anext(stream)
    pending_next = asyncio.create_task(anext(stream))
    assert await _wait_threading_event(started)
    pending_next.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending_next

    request_id = mock_model.generate_stream.call_args.kwargs["request_id"]
    assert session.run_in_progress is True
    assert request_id in scoped_output_ids
    assert request_id in scoped_usage_ids

    release.set()
    for _ in range(50):
        if not session.run_in_progress:
            break
        await asyncio.sleep(0.02)

    assert session.run_in_progress is False
    assert session.active_run_scope_ids == []
    assert request_id not in scoped_output_ids
    assert request_id not in scoped_usage_ids
    mock_model.clear_stream_state.assert_called_once_with(request_id)
    mock_model.llm_service.discard_stream_usage.assert_called_once_with(request_id)


@pytest.mark.anyio
async def test_run_answer_now_stream_commits_memory_before_releasing_run_scope(
    agent_runner,
):
    worker_finished = threading.Event()
    mock_model = MagicMock()

    def _successful_stream():
        yield ChatMessageStreamDelta(content="worker-owned final")
        worker_finished.set()

    mock_model.generate_stream.return_value = _successful_stream()
    mock_agent = MagicMock()
    mock_agent.model = mock_model
    mock_agent.memory.steps = [TaskStep(task="原始任务")]
    mock_agent.tools = {}
    mock_agent.managed_agents = {}
    mock_agent.write_memory_to_messages.return_value = []
    session = AgentSession(
        session_id="answer-now-worker-memory-commit-session",
        agent=mock_agent,
        mode="tool_calling",
    )
    session.has_run = True
    agent_runner.sessions.put(session)

    stream = agent_runner.run_answer_now_stream(session.session_id)
    assert '"type": "session_start"' in await anext(stream)
    assert '"type": "content"' in await anext(stream)
    assert await _wait_threading_event(worker_finished)
    for _ in range(50):
        if not session.run_in_progress:
            break
        await asyncio.sleep(0.01)

    committed_steps = [
        step
        for step in mock_agent.memory.steps
        if isinstance(step, ActionStep) and step.is_final_answer
    ]
    assert session.run_in_progress is False
    assert len(committed_steps) == 1
    assert committed_steps[0].action_output == "worker-owned final"

    remaining_events = [event async for event in stream]
    assert any('"type": "done"' in event for event in remaining_events)
    committed_steps_after_drain = [
        step
        for step in mock_agent.memory.steps
        if isinstance(step, ActionStep) and step.is_final_answer
    ]
    assert len(committed_steps_after_drain) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure_stage", "expected_finish_run_count"),
    [
        ("runtime_agent", 0),
        ("tool_schema", 0),
        ("prompt", 0),
        ("begin_run", 0),
        ("memory", 1),
    ],
)
async def test_run_answer_now_stream_setup_exceptions_release_owned_state_once(
    agent_runner, failure_stage, expected_finish_run_count
):
    class RaisingToolSchemaAgent:
        model = MagicMock()
        memory = MagicMock(steps=[])
        tools = {}
        managed_agents = {}

        @property
        def tools_and_managed_agents(self):
            raise RuntimeError("tool schema failure")

        def write_memory_to_messages(self):
            if failure_stage == "memory":
                raise RuntimeError("memory failure")
            return []

    runtime_agent = RaisingToolSchemaAgent() if failure_stage == "tool_schema" else MagicMock()
    if failure_stage != "tool_schema":
        runtime_agent.model = MagicMock()
        runtime_agent.memory.steps = []
        runtime_agent.tools = {}
        runtime_agent.managed_agents = {}
        if failure_stage == "memory":
            runtime_agent.write_memory_to_messages.side_effect = RuntimeError("memory failure")
        else:
            runtime_agent.write_memory_to_messages.return_value = []

    session = AgentSession(
        session_id=f"answer-now-setup-{failure_stage}",
        agent=runtime_agent,
        mode="tool_calling",
    )
    agent_runner.sessions.put(session)

    original_finish_answer_now = session.finish_answer_now
    original_finish_run = session.finish_run
    session.finish_answer_now = MagicMock(wraps=original_finish_answer_now)
    session.finish_run = MagicMock(wraps=original_finish_run)

    patches = []
    if failure_stage == "runtime_agent":
        patches.append(
            patch.object(
                agent_runner,
                "_get_runtime_agent",
                side_effect=RuntimeError("runtime agent failure"),
            )
        )
    if failure_stage == "prompt":
        patches.append(
            patch.object(
                agent_runner,
                "_build_answer_now_prompt",
                side_effect=RuntimeError("prompt failure"),
            )
        )
    if failure_stage == "begin_run":
        patches.append(
            patch.object(session, "begin_run", side_effect=RuntimeError("begin run failure"))
        )

    with ExitStack() as stack:
        for active_patch in patches:
            stack.enter_context(active_patch)
        events = [
            event
            async for event in agent_runner.run_answer_now_stream(session.session_id)
        ]

    assert '"type": "error"' in "".join(events)
    session.finish_answer_now.assert_called_once_with()
    assert session.finish_run.call_count == expected_finish_run_count
    assert session.answering_now is False
    assert session.run_in_progress is False


@pytest.mark.anyio
async def test_run_answer_now_stream_close_after_session_start_releases_unowned_run_once(
    agent_runner,
):
    runtime_agent = MagicMock()
    runtime_agent.model = MagicMock()
    runtime_agent.memory.steps = []
    runtime_agent.tools = {}
    runtime_agent.managed_agents = {}
    runtime_agent.write_memory_to_messages.return_value = []
    session = AgentSession(
        session_id="answer-now-close-after-session-start",
        agent=runtime_agent,
        mode="tool_calling",
    )
    agent_runner.sessions.put(session)

    original_finish_answer_now = session.finish_answer_now
    original_finish_run = session.finish_run
    session.finish_answer_now = MagicMock(wraps=original_finish_answer_now)
    session.finish_run = MagicMock(wraps=original_finish_run)

    stream = agent_runner.run_answer_now_stream(session.session_id)
    assert '"type": "session_start"' in await anext(stream)
    await stream.aclose()

    runtime_agent.write_memory_to_messages.assert_not_called()
    runtime_agent.model.generate_stream.assert_not_called()
    session.finish_answer_now.assert_called_once_with()
    session.finish_run.assert_called_once_with(session.finish_run.call_args.args[0])
    assert session.answering_now is False
    assert session.run_in_progress is False


async def _collect_stream_events(stream):
    events = []
    async for event in stream:
        events.append(event)
    return events


def _sse_payload(event: str) -> dict:
    assert event.startswith("data: ")
    return json.loads(event[6:])


@pytest.mark.anyio
async def test_run_basic_stream_emits_tool_call_before_tool_output(agent_runner):
    """A parsed tool call must reach the UI before a slow tool returns."""
    release_tool = threading.Event()
    tool_call = MemoryToolCall(
        name="search_and_fetch",
        arguments={"query": "Jetson Thor SGLang"},
        id="call-search-1",
    )

    def _tool_stream():
        yield ChatMessageStreamDelta(content="我需要先搜索资料。")
        yield tool_call
        release_tool.wait(timeout=2)
        yield ToolOutput(
            id="call-search-1",
            output="搜索结果正文",
            is_final_answer=False,
            observation="搜索结果正文",
            tool_call=tool_call,
        )

    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}
    agent.managed_agents = {}
    agent.stream_outputs = True
    agent._cli_stream_source = "basic_agent"
    agent._cli_live = None
    agent.run.side_effect = lambda *args, **kwargs: _tool_stream()

    session = AgentSession(
        session_id="tool-call-before-output-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )

    stream = agent_runner._run_basic_stream(session, "search it", None)
    pending_next = None
    try:
        session_start = _sse_payload(await asyncio.wait_for(stream.__anext__(), timeout=1))
        assert session_start["type"] == "session_start"

        thought = _sse_payload(await asyncio.wait_for(stream.__anext__(), timeout=1))
        assert thought["type"] == "agent_step"
        assert thought["step_type"] == "thought"

        pending_next = asyncio.create_task(stream.__anext__())
        done, _pending = await asyncio.wait({pending_next}, timeout=0.25)
        assert done, "tool_call SSE should be emitted before the slow tool returns"

        tool_event = _sse_payload(pending_next.result())
        assert tool_event["type"] == "agent_step"
        assert tool_event["step_type"] == "tool_call"
        assert tool_event["name"] == "search_and_fetch"

        release_tool.set()
        rest = []
        async for event in stream:
            rest.append(_sse_payload(event))
        assert any(
            event.get("step_type") == "observation" and "搜索结果正文" in event.get("content", "")
            for event in rest
        )
    finally:
        release_tool.set()
        if pending_next is not None and not pending_next.done():
            pending_next.cancel()
            try:
                await pending_next
            except (asyncio.CancelledError, StopAsyncIteration):
                pass


@pytest.mark.anyio
async def test_run_basic_stream_projects_raw_tool_call_only_on_thought_step(agent_runner):
    raw = _tool_block("read_file", '{\n  "file_path": "/tmp/a"\n}')
    tool_call = MemoryToolCall(
        name="read_file", arguments={"file_path": "/tmp/a"}, id="call-raw-1"
    )

    def _agent_stream():
        yield ChatMessageStreamDelta(content="先检查文件。")
        yield RawToolCallDebug(raw_tool_call=raw)
        yield tool_call

    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}
    agent.managed_agents = {}
    agent.run.side_effect = lambda *args, **kwargs: _agent_stream()
    session = AgentSession(
        session_id="raw-debug-valid-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )

    payloads = [
        _sse_payload(event)
        async for event in agent_runner._run_basic_stream(session, "read", None)
    ]
    raw_steps = [item for item in payloads if item.get("raw_tool_call") == raw]

    assert len(raw_steps) == 1
    assert raw_steps[0]["type"] == "agent_step"
    assert raw_steps[0]["step_type"] == "thought"
    assert all(item.get("content") != raw for item in payloads)
    assert not any(
        "raw_tool_call" in item and item.get("step_type") != "thought"
        for item in payloads
    )


@pytest.mark.anyio
async def test_run_basic_stream_real_adapter_does_not_leak_prefixed_control_content(
    agent_runner,
):
    prose = "先检查。\n"
    frame = _tool_block("read_file", '{"file_path":"/tmp/a"}')
    agent = agent_runner._build_agent("tool_calling")
    memory_step = ActionStep(step_number=1, timing=Timing(start_time=0.0, end_time=0.1))
    agent.model.llm_service.chat.return_value = iter(
        [
            {"content": prose},
            {"content": "<<TOOL_"},
            {"content": frame[len("<<TOOL_") :]},
        ]
    )
    agent.model.llm_service.get_stream_usage.return_value = None
    agent.process_tool_calls = MagicMock(return_value=iter(()))
    agent.run = MagicMock(side_effect=lambda *_args, **_kwargs: agent._step_stream(memory_step))
    session = AgentSession(
        session_id="real-prefixed-control-sse",
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )

    payloads = [
        _sse_payload(event)
        async for event in agent_runner._run_basic_stream(session, "read", None)
    ]

    visible_content = "".join(
        str(payload.get("content") or "") for payload in payloads
    )
    assert prose.strip() in visible_content
    assert "<<TOOL_CALL>>" not in visible_content
    assert not any(payload.get("step_type") == "tool_call" for payload in payloads)
    agent.process_tool_calls.assert_called_once()


@pytest.mark.anyio
async def test_run_basic_stream_projects_raw_before_parsing_error_observation(agent_runner):
    raw = '<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{\"file_path\":\"/tmp/a\"}}'
    failed_step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.1),
        model_output=None,
        error=RuntimeError("missing standalone <<END_TOOL_CALL>> line"),
    )

    def _agent_stream():
        yield RawToolCallDebug(raw_tool_call=raw)
        yield failed_step

    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}
    agent.managed_agents = {}
    agent.run.side_effect = lambda *args, **kwargs: _agent_stream()
    session = AgentSession(
        session_id="raw-debug-invalid-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )

    payloads = [
        _sse_payload(event)
        async for event in agent_runner._run_basic_stream(session, "read", None)
    ]
    raw_index = next(i for i, item in enumerate(payloads) if item.get("raw_tool_call") == raw)
    error_index = next(
        i
        for i, item in enumerate(payloads)
        if item.get("step_type") == "observation" and "missing standalone" in item.get("content", "")
    )

    assert raw_index < error_index
    assert not any(item.get("step_type") == "tool_call" for item in payloads)


@pytest.mark.anyio
async def test_run_basic_stream_surfaces_action_step_errors_as_observations(
    agent_runner, caplog
):
    """Tool execution errors must be visible to the frontend agent timeline."""
    error_step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.0),
        model_output="I tried to call a tool.",
        error=RuntimeError(
            "Unknown tool expert_maths_solver, should be one of: read_file, dispatching_parallel_agent"
        ),
    )

    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}
    agent.managed_agents = {}
    agent.state = {}
    agent.stream_outputs = True
    agent._cli_stream_source = "basic_agent"
    agent._cli_live = None
    agent.run.return_value = iter([error_step])

    session = AgentSession(
        session_id="action-step-error-observation-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )

    with caplog.at_level(logging.WARNING):
        events = await _collect_stream_events(
            agent_runner._run_basic_stream(session, "continue", None)
        )
    payloads = [_sse_payload(event) for event in events]

    assert any(
        payload.get("type") == "agent_step"
        and payload.get("step_type") == "observation"
        and "Unknown tool expert_maths_solver" in payload.get("content", "")
        for payload in payloads
    )
    assert (
        "Agent action error: session=action-step-error-observation-session "
        "profile=basic_agent"
    ) in caplog.text


# ====================================================================
# Bug 1 regression tests (2026-06-12/13): backend SSE must not create
# Rich Live blocks. Web UI gets true SSE streaming; backend terminal
# should not token-render model output because ordinary logging interleaves
# with Live refreshes and leaves duplicated snapshots.
#
# Symptom (before fix): each `_step_stream` invocation created its
# own `with Live(...) as live:` block. Rich freezes the final
# markdown on `with` exit. A 50-step LLM-C loop (e.g. post-slim) thus
# produced 50 copies of the same content stacked vertically in the
# terminal.
#
# Fix: `_run_basic_stream` does not create a Live block. `_step_stream`
# only reuses a parent-provided `_cli_live` if a future dedicated CLI
# runner sets one; it must never create its own local Live fallback.
# ====================================================================


@pytest.mark.anyio
async def test_run_basic_stream_does_not_create_backend_cli_live(agent_runner):
    """Backend SSE runs must not create Rich Live terminal rendering."""
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}
    agent.managed_agents = {}
    agent.stream_outputs = True
    agent._cli_stream_source = "basic_agent"
    agent._cli_live = None  # initial state

    def _run(*args, **kwargs):
        assert agent._cli_live is None
        return iter([])

    agent.run.side_effect = _run

    session = AgentSession(
        session_id="cli-live-pinning-session",
        agent=agent,
        mode="tool_calling",
    )

    with patch("rich.live.Live") as mock_live:
        async for _ in agent_runner._run_basic_stream(session, "hi", None):
            pass

    assert agent._cli_live is None
    mock_live.assert_not_called()


@pytest.mark.anyio
async def test_run_basic_stream_skips_live_for_coordinator_routing(agent_runner):
    """coordinator_1 routing intentionally has no CLI Live (routing
    is invisible on the terminal — the dispatch_card is the UI).
    The fix must preserve that behavior (Bug 1 fix, 2026-06-12).
    """
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}
    agent.managed_agents = {}
    agent.stream_outputs = True
    agent._cli_stream_source = "coordinator_1"
    agent._cli_live = None

    live_construct_count = {"n": 0}

    class _SpyLive:
        def __init__(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def update(self, *a, **k):
            pass

    def _run(*args, **kwargs):
        return iter([])

    agent.run.side_effect = _run

    def _counting_live(*args, **kwargs):
        live_construct_count["n"] += 1
        return _SpyLive()

    session = AgentSession(
        session_id="cli-live-routing-skip-session",
        agent=agent,
        mode="tool_calling",
    )

    with patch("rich.live.Live", side_effect=_counting_live):
        async for _ in agent_runner._run_basic_stream(session, "hi", None):
            pass

    assert live_construct_count["n"] == 0
    assert agent._cli_live is None


@pytest.mark.anyio
async def test_run_basic_stream_releases_live_on_agent_exception(agent_runner):
    """If agent.run() raises, backend SSE still must not create Live."""
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}
    agent.managed_agents = {}
    agent.stream_outputs = True
    agent._cli_stream_source = "basic_agent"
    agent._cli_live = None

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated LLM backend failure")

    agent.run.side_effect = _boom

    session = AgentSession(
        session_id="cli-live-exception-session",
        agent=agent,
        mode="tool_calling",
    )

    with patch("rich.live.Live") as mock_live:
        events = []
        async for ev in agent_runner._run_basic_stream(session, "hi", None):
            events.append(ev)
    mock_live.assert_not_called()
    assert agent._cli_live is None


@pytest.mark.anyio
async def test_run_basic_stream_streams_sse_without_backend_live(agent_runner):
    """Disabling backend Live must not disable SSE content streaming."""
    agent = agent_runner._build_agent("tool_calling")
    agent.tools = {}
    agent.managed_agents = {}
    agent.stream_outputs = True
    agent._cli_stream_source = "basic_agent"
    agent.logger.console = MagicMock()

    agent.model.generate_stream = MagicMock(
        return_value=iter([ChatMessageStreamDelta(content="根据用户指令，输出第一条文本。")])
    )
    agent.model._last_stream_content = "根据用户指令，输出第一条文本。"
    agent.model._last_stream_reasoning = ""
    agent.model.llm_service.get_stream_usage.return_value = {
        "prompt_tokens": 1,
        "completion_tokens": 1,
    }

    session = AgentSession(
        session_id="sse-stream-without-backend-live",
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )

    with patch("rich.live.Live") as mock_live:
        events = [event async for event in agent_runner._run_basic_stream(session, "hi", None)]

    assert any('"type": "content"' in event for event in events)
    mock_live.assert_not_called()
    assert agent.model.llm_service.discard_stream_usage.call_count == 1
