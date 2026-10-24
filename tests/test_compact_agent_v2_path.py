"""Tests for the agent-mode auto-compact using the v2 turn-shape path.

Per spec §4.1, §5.4:
  - _run_compact_then_continue builds body_messages from
    session.agent.write_memory_to_messages() (not _session_memory_to_messages).
  - run_compact_for_session does the same.
  - _should_auto_compact_session uses the same source for token counting.
  - The body_messages list is passed to CompactRequest.body_messages (not .messages).
"""
from __future__ import annotations

import asyncio
from typing import List
from unittest.mock import MagicMock, patch

import pytest
from smolagents.memory import ActionStep, TaskStep
from smolagents.monitoring import Timing
import time as _time

from backend.compact.types import CompactEvent, CompactEventKind, CompactRequest
from backend.llm_service import Message


def _make_action_step(model_output: str, observations: str = "") -> ActionStep:
    """Build a valid ActionStep (smolagents requires step_number + timing)."""
    step = ActionStep(step_number=1, timing=Timing(start_time=_time.time()))
    step.model_output = model_output
    step.observations = observations
    return step


def _make_session_with_steps(steps):
    """Construct a minimal session-like object that AgentRunner methods can read."""
    session = MagicMock()
    session.session_id = "test-session"
    session.agent = MagicMock()
    session.agent.memory.steps = steps
    return session


async def _drain(agen):
    out = []
    async for ev in agen:
        out.append(ev)
    return out


def test_should_auto_compact_session_uses_write_memory_to_messages() -> None:
    """_should_auto_compact_session must use write_memory_to_messages() as its token-count source,
    matching the v2 compact body construction source (so token count is consistent with what LLM sees)."""
    session = _make_session_with_steps([
        TaskStep(task="user task 1"),
        _make_action_step("assistant text", "obs1"),
    ])

    # write_memory_to_messages returns N messages including the system
    write_memory_messages = [
        MagicMock(role="system", content="SYS"),
        MagicMock(role="user", content="New task:\nuser task 1"),
        MagicMock(role="assistant", content="assistant text"),
    ]
    session.agent.write_memory_to_messages = MagicMock(return_value=write_memory_messages)

    # Build a minimal runner-like object with just the attrs the method touches
    runner = MagicMock()
    runner.compact_orchestrator = MagicMock()  # non-None to skip the early return
    runner.config = MagicMock()
    runner.config.compact.enabled = True
    runner.config.compact.auto_trigger = True
    runner.llm_service = MagicMock()
    runner.llm_service.active_backend = "sglang"

    with patch("backend.compact.threshold.compute_threshold", return_value=100), \
         patch("backend.compact.threshold.should_compact", return_value=True), \
         patch("backend.llm_service.resolve_backend_context_length", return_value=8192), \
         patch("backend.compact.tokenizer.TokenCounter") as MockCounter:
        mock_counter_instance = MagicMock()
        mock_counter_instance.count_messages = MagicMock(return_value=200)
        MockCounter.get.return_value = mock_counter_instance

        from backend.agent.agent_runner import AgentRunner
        result = AgentRunner._should_auto_compact_session(runner, session)

    # Invariant 1: write_memory_to_messages is the source
    session.agent.write_memory_to_messages.assert_called_once()
    # Invariant 2: the flat _session_memory_to_messages is NOT called
    runner._session_memory_to_messages.assert_not_called()
    # Invariant 3: TokenCounter was given the write_memory output (3 messages)
    mock_counter_instance.count_messages.assert_called_once()
    called_args, _ = mock_counter_instance.count_messages.call_args
    assert len(called_args[0]) == 3
    # Invariant 4: the method returns True (because should_compact was mocked to True)
    assert result is True


def test_run_compact_then_continue_uses_write_memory_to_messages() -> None:
    """_run_compact_then_continue must call write_memory_to_messages() and pass the result as
    CompactRequest.body_messages (not as the deprecated .messages field)."""
    session = _make_session_with_steps([
        TaskStep(task="user task 1"),
        _make_action_step("assistant text", "obs1"),
    ])

    # write_memory_to_messages returns a turn-shape list (system + u + a + u + a + ...)
    expected_chat_messages = [
        MagicMock(role="system", content="SYS"),
        MagicMock(role="user", content="New task:\nuser task 1"),
        MagicMock(role="assistant", content="assistant text"),
        MagicMock(role="user", content="Observation:\nobs1"),
    ]
    session.agent.write_memory_to_messages = MagicMock(return_value=expected_chat_messages)

    runner = MagicMock()
    runner.config = MagicMock()
    runner.config.compact.enabled = True
    runner.config.auto_trigger = True
    runner.compact_orchestrator = MagicMock()  # non-None so the path enters
    runner._session_memory_to_messages = MagicMock()
    runner._replace_agent_memory = MagicMock()
    # _run_compact_then_continue calls self._compact_event_to_sse(ev) on each
    # event before yielding. Use the real static method so the events end
    # up as real SSE strings ("data: {...}") and we can assert against the
    # JSON payload.
    from backend.agent.agent_runner import AgentRunner
    runner._compact_event_to_sse = AgentRunner._compact_event_to_sse
    # Bug 2 fix (2026-06-12): _run_compact_then_continue delegates body
    # construction to _build_compact_body_messages (which then calls
    # write_memory_to_messages). Bind the real method via MethodType so
    # `self` is properly set; assigning the raw function to a MagicMock
    # attribute makes `runner._build_compact_body_messages(...)` a no-arg
    # call (no implicit self binding) and the method would be called
    # with the wrong arity.
    from types import MethodType
    runner._build_compact_body_messages = MethodType(
        AgentRunner._build_compact_body_messages, runner
    )

    # Capture the CompactRequest constructed inside _run_compact_then_continue
    captured_request = {}

    def _capture_request_ctor(*args, **kwargs):
        async def _fake_run(req):
            captured_request["body_messages"] = req.body_messages
            captured_request["messages"] = req.messages
            yield CompactEvent.started(summary_message_id="sid", trigger="auto", mode="agent")
            yield CompactEvent.completed("sid", "cleaned summary", metadata={"trigger": "auto", "mode": "agent"})
        instance = MagicMock()
        instance.run_compact = _fake_run
        return instance

    with patch("backend.compact.orchestrator.CompactOrchestrator", side_effect=_capture_request_ctor):
        events = asyncio.run(_drain(AgentRunner._run_compact_then_continue(runner, session, "auto")))

    # Invariant 1: write_memory_to_messages was called
    session.agent.write_memory_to_messages.assert_called_once()
    # Invariant 2: _session_memory_to_messages was NOT called (the v1 path is gone)
    runner._session_memory_to_messages.assert_not_called()
    # Invariant 3: CompactRequest.body_messages was set (not .messages)
    assert captured_request["body_messages"] is not None, (
        "CompactRequest.body_messages should be set in the v2 path"
    )
    assert captured_request["messages"] is None, (
        "CompactRequest.messages should NOT be set in the v2 path (deprecated)"
    )
    # Invariant 4: body_messages is a List[Message] (not a List[ChatMessage])
    assert isinstance(captured_request["body_messages"], list)
    assert len(captured_request["body_messages"]) == len(expected_chat_messages)
    assert all(isinstance(m, Message) for m in captured_request["body_messages"])
    # Invariant 5 (Bug 2 fix, 2026-06-12): body_messages[0] is now
    # the **compact-specific** system prompt (overrides the active
    # agent's system prompt, which is `expected_chat_messages[0]` =
    # "SYS"). body_messages[1:] is byte-equal to the agent's history.
    compact_msg = captured_request["body_messages"][0]
    assert compact_msg.role == "system"
    assert compact_msg.content != expected_chat_messages[0].content, (
        "body_messages[0] must be the compact system prompt, NOT the "
        "active agent's system prompt (Bug 2 fix, 2026-06-12). "
        "Otherwise the LLM sees the routing/tool-calling instructions "
        "and emits routing XML instead of <summary>."
    )
    # body_messages[1:] is byte-equal to the agent's history.
    for i, src in enumerate(expected_chat_messages[1:], start=1):
        assert captured_request["body_messages"][i].role == src.role
        assert captured_request["body_messages"][i].content == src.content
    # Invariant 6: events include started and completed
    # _run_compact_then_continue yields SSE strings (data: {...}), not raw
    # CompactEvent objects, because the consumer is the /v1/agent/chat
    # SSE loop. The compact_started / compact_completed kinds should
    # therefore appear as substrings inside the data payloads.
    sse_blob = "\n".join(events)
    assert "compact_started" in sse_blob, (
        f"expected compact_started in SSE stream, got: {sse_blob!r}"
    )
    assert "compact_completed" in sse_blob, (
        f"expected compact_completed in SSE stream, got: {sse_blob!r}"
    )


def test_run_compact_then_continue_passes_next_turn_focus_without_adding_to_source() -> None:
    session = _make_session_with_steps([
        TaskStep(task="user task 1"),
        _make_action_step("assistant text", "obs1"),
    ])
    expected_chat_messages = [
        MagicMock(role="system", content="SYS"),
        MagicMock(role="user", content="New task:\nuser task 1"),
        MagicMock(role="assistant", content="assistant text"),
    ]
    session.agent.write_memory_to_messages = MagicMock(return_value=expected_chat_messages)

    runner = MagicMock()
    runner.config = MagicMock()
    runner.compact_orchestrator = MagicMock()
    runner._replace_agent_memory = MagicMock()
    from backend.agent.agent_runner import AgentRunner
    runner._compact_event_to_sse = AgentRunner._compact_event_to_sse
    from types import MethodType
    runner._build_compact_body_messages = MethodType(
        AgentRunner._build_compact_body_messages, runner
    )
    runner._build_cache_first_compact_body_messages = MagicMock(return_value=None)

    captured_request = {}

    def _capture_request_ctor(*args, **kwargs):
        async def _fake_run(req):
            captured_request["req"] = req
            yield CompactEvent.started(summary_message_id="sid", trigger="auto", mode="agent")
            yield CompactEvent.completed("sid", "cleaned summary", metadata={"trigger": "auto", "mode": "agent"})
        instance = MagicMock()
        instance.run_compact = _fake_run
        return instance

    with patch("backend.compact.orchestrator.CompactOrchestrator", side_effect=_capture_request_ctor):
        asyncio.run(
            _drain(
                AgentRunner._run_compact_then_continue(
                    runner,
                    session,
                    "auto",
                    next_turn_user_message="new agent task",
                )
            )
        )

    req = captured_request["req"]
    assert req.custom_instructions is not None
    assert "Upcoming user instruction:" in req.custom_instructions
    assert "new agent task" in req.custom_instructions
    assert all("new agent task" not in str(m.content) for m in req.body_messages)
