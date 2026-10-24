"""Tests for the agent-mode compact body construction (Bug 2, 2026-06-12).

Symptom (before fix): in agent mode, the compact LLM call used
`session.agent.write_memory_to_messages()` whose first element is the
**active agent's** system prompt (e.g. `coordinator_1`'s "你是极简路由
代理，只输出切换 marker" or `basic_agent`'s heavy tool-calling
instructions). The LLM saw the system prompt and the compact
instruction at user position 1 as **competing** instructions; the
system prompt won (system > user, especially on 40k+ context with
lost-in-the-middle), and the LLM emitted a routing JSON or tool-call
    format instead of the requested `<summary>`. The compact
card then displayed ~167 chars of routing XML instead of the actual
9-section summary.

Fix: `_build_compact_body_messages(session, prompts_cfg)` overrides
`body_messages[0]` with a **compact-specific** summarizer system prompt
built from `compaction.no_tools_preamble + compaction.main_prompt +
compaction.no_tools_trailer`. `body_messages[1:]` is byte-equal to
the agent's history.

Hard contracts pinned by these tests:
  * body_messages[0].role == "system" (v2 builder invariant)
  * body_messages[0].content is the compact system prompt (NOT the
    active agent's system prompt, NOT just "SYS")
  * body_messages[0].content does NOT contain tool-calling markers
    ("Calling tools:", "动作:", "switch_agent") — the LLM would
    otherwise fall back to its tool-calling behavior
  * body_messages[1:] is byte-equal to the agent's history
  * the helper works for both empty memory and short history
  * the helper is robust to None / empty prompt fields
"""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock, patch
from types import MethodType

import pytest
from smolagents.memory import ActionStep, TaskStep
from smolagents.monitoring import Timing

from backend.agent.agent_runner import (
    AgentRunner,
    AgentSession,
    _chat_messages_to_backend_messages,
)
from backend.llm_service import Message


def _make_action_step(model_output: str, observations: str = "") -> ActionStep:
    step = ActionStep(step_number=1, timing=Timing(start_time=0.0, end_time=0.1))
    step.model_output = model_output
    step.observations = observations
    step.tool_calls = []
    return step


def _make_session_with_steps(steps, *, system_prompt_text: str = "ACTIVE_AGENT_SYSTEM") -> AgentSession:
    """Build a session whose agent memory has the given steps and a
    specific system_prompt. The system's text is the discriminator
    between the active agent's prompt and the compact-specific
    prompt we expect after the fix.
    """
    agent = MagicMock()
    agent.memory.steps = list(steps)
    # `write_memory_to_messages` is the smolagents' canonical way to
    # build the messages list; it starts from
    # `self.memory.system_prompt.to_messages()` then appends each
    # step's `to_messages()`. We mock it to return a turn-shape list
    # with the given system prompt at index 0.
    expected = [MagicMock(role="system", content=system_prompt_text)]
    for step in steps:
        if isinstance(step, TaskStep):
            expected.append(MagicMock(role="user", content=f"New task:\n{step.task or ''}"))
        elif isinstance(step, _make_action_step.__globals__["ActionStep"]):
            if getattr(step, "model_output", None):
                expected.append(MagicMock(role="assistant", content=step.model_output))
            if getattr(step, "observations", None):
                expected.append(MagicMock(role="user", content=f"Observation:\n{step.observations}"))
    agent.write_memory_to_messages = MagicMock(return_value=expected)
    return agent, expected


def _make_prompts_cfg(
    *,
    preamble: str = "COMPACT: respond with TEXT ONLY, no tools.",
    main_prompt: str = "Your task: summarize the conversation that follows this instruction.",
    trailer: str = "REMINDER: respond with plain text only.",
) -> MagicMock:
    """Build a MagicMock that quacks like a CompactionPromptsConfig."""
    cfg = MagicMock()
    cfg.no_tools_preamble = preamble
    cfg.main_prompt = main_prompt
    cfg.no_tools_trailer = trailer
    return cfg


# ====================================================================
# Test 1: body_messages[0] is the compact system prompt, NOT the
# active agent's. This is the core Bug 2 fix.
# ====================================================================


def test_compact_body_messages_zero_is_compact_specific_system() -> None:
    """body_messages[0] must be a summarizer persona, not the active
    agent's persona. Otherwise the LLM responds with routing XML /
    tool calls instead of the requested 9-section summary (Bug 2,
    2026-06-12).
    """
    agent, _ = _make_session_with_steps(
        [TaskStep(task="user task 1"), _make_action_step("assistant text", "obs1")],
        system_prompt_text="ACTIVE_AGENT_SYSTEM: you are coordinator_1, output switch_agent JSON",
    )
    session = AgentSession(session_id="bug2-core", agent=agent, mode="tool_calling")

    prompts_cfg = _make_prompts_cfg()
    runner = MagicMock()

    body = AgentRunner._build_compact_body_messages(runner, session, prompts_cfg)

    assert body[0].role == "system"
    # The active agent's text is NOT in the compact system.
    assert "ACTIVE_AGENT_SYSTEM" not in body[0].content
    assert "switch_agent" not in body[0].content
    # The compact-specific text IS in the compact system.
    assert "COMPACT: respond with TEXT ONLY" in body[0].content
    assert "summarize the conversation" in body[0].content
    assert "REMINDER: respond with plain text only." in body[0].content


# ====================================================================
# Test 2: body_messages[1:] is byte-equal to the agent's history.
# This preserves the v2 spec's "byte-equal" ideal for the message
# body (only index 0 diverges).
# ====================================================================


def test_compact_body_messages_preserve_history_after_system_override() -> None:
    """body_messages[1:] is byte-equal to the agent's history. Only
    body_messages[0] diverges from the active agent's system prompt.
    """
    agent, expected_chat = _make_session_with_steps(
        [TaskStep(task="first user task"), _make_action_step("first assistant", "obs1")],
        system_prompt_text="AGENT_SYSTEM",
    )
    session = AgentSession(session_id="bug2-history", agent=agent, mode="tool_calling")

    prompts_cfg = _make_prompts_cfg()
    runner = MagicMock()

    body = AgentRunner._build_compact_body_messages(runner, session, prompts_cfg)

    assert len(body) == len(expected_chat)
    for i, src in enumerate(expected_chat[1:], start=1):
        assert body[i].role == src.role
        assert body[i].content == src.content


# ====================================================================
# Test 3: helper is robust to empty memory (no steps, no system).
# The v2 builder's invariant `body_messages[0].role == "system"`
# must still hold.
# ====================================================================


def test_compact_body_messages_handles_empty_agent_memory() -> None:
    """Defensive: if `write_memory_to_messages` returns an empty list
    (no system prompt AND no steps), the helper still emits a
    well-formed body_messages with a system message at index 0.
    Otherwise the v2 builder's `body_messages[0].role == "system"`
    invariant raises ValueError and the compact call fails-fast with
    `compact_failed(reason='build_error')`.
    """
    agent = MagicMock()
    agent.memory.steps = []
    agent.write_memory_to_messages = MagicMock(return_value=[])
    session = AgentSession(session_id="bug2-empty", agent=agent, mode="tool_calling")

    prompts_cfg = _make_prompts_cfg()
    runner = MagicMock()

    body = AgentRunner._build_compact_body_messages(runner, session, prompts_cfg)

    assert len(body) >= 1
    assert body[0].role == "system"
    assert "COMPACT: respond with TEXT ONLY" in body[0].content


# ====================================================================
# Test 4: helper is robust to None / empty prompt fields. The
# concatenation must not raise even if a prompt is missing.
# ====================================================================


def test_compact_body_messages_handles_empty_prompt_fields() -> None:
    """Defensive: prompts_cfg fields can be None or empty. The
    helper still produces a non-empty system message (the v2 builder
    requires `body_messages[0]` to be non-None and role='system').
    """
    agent, _ = _make_session_with_steps(
        [TaskStep(task="t")], system_prompt_text="AGENT_SYSTEM"
    )
    session = AgentSession(session_id="bug2-empty-prompts", agent=agent, mode="tool_calling")

    prompts_cfg = MagicMock()
    prompts_cfg.no_tools_preamble = None
    prompts_cfg.main_prompt = None
    prompts_cfg.no_tools_trailer = None
    runner = MagicMock()

    body = AgentRunner._build_compact_body_messages(runner, session, prompts_cfg)

    assert body[0].role == "system"
    # System content is some non-empty string (could be just
    # whitespace from None.rstrip() + "\n\n" joins; that's fine).
    assert isinstance(body[0].content, str)


# ====================================================================
# Test 5: end-to-end — the helper is called by both
# _run_compact_then_continue AND run_compact_for_session. Pinned
# here so a refactor that only patches one path doesn't slip
# through review.
# ====================================================================


@pytest.mark.anyio
async def test_run_compact_for_session_uses_compact_specific_system() -> None:
    """`run_compact_for_session` (manual /compact) must use the same
    compact-specific system override as `_run_compact_then_continue`
    (auto compact). Both must NOT pass the active agent's system
    prompt as body_messages[0] (Bug 2 fix, 2026-06-12).
    """
    agent, _ = _make_session_with_steps(
        [TaskStep(task="t")], system_prompt_text="ACTIVE_AGENT_X"
    )
    session = AgentSession(session_id="bug2-manual", agent=agent, mode="tool_calling")

    runner = MagicMock()
    runner.config = MagicMock()
    runner.config.agent_prompts.compaction = _make_prompts_cfg()
    runner.config.compact = MagicMock()
    runner.llm_service = MagicMock()
    runner.sessions = {session.session_id: session}
    runner._agent_admission_lock = threading.Lock()
    runner._pending_agent_admissions = set()
    runner._is_session_disposal_active = MagicMock(return_value=False)
    runner._replace_agent_memory = MagicMock()
    # Bind the real method so the runner's MagicMock doesn't shadow it.
    runner._build_compact_body_messages = MethodType(
        AgentRunner._build_compact_body_messages, runner
    )
    runner._build_cache_first_compact_body_messages = MagicMock(return_value=None)

    captured_request = {}

    def _capture_orchestrator(*args, **kwargs):
        async def _fake_run(req):
            captured_request["body_messages"] = req.body_messages
            from backend.compact.types import CompactEvent
            yield CompactEvent.started(summary_message_id="sid", trigger="manual", mode="agent")
            yield CompactEvent.completed("sid", "ok", metadata={"trigger": "manual", "mode": "agent"})
        instance = MagicMock()
        instance.run_compact = _fake_run
        return instance

    with patch(
        "backend.compact.orchestrator.CompactOrchestrator",
        side_effect=_capture_orchestrator,
    ):
        events = []
        async for ev in AgentRunner.run_compact_for_session(
            runner, session.session_id, mode="agent", trigger="manual"
        ):
            events.append(ev)

    assert captured_request["body_messages"] is not None
    body = captured_request["body_messages"]
    assert body[0].role == "system"
    assert "ACTIVE_AGENT_X" not in body[0].content
    assert "switch_agent" not in body[0].content
    assert "COMPACT: respond with TEXT ONLY" in body[0].content
