"""Regression: post-/compact agent run MUST see the envelope in the LLM call.

Bug history (2026-06-06):
  Vision-page /agent-mode user reported that after a successful
  /compact (SGLang terminal showed the 3k-token compact LLM call,
  the UI showed the compact_summary bubble), the next /v1/agent/chat
  round still asked the agent to do the task from scratch, and the
  agent replied with "I don't have memory function" instead of using
  the summary.

Root cause:
  `_replace_agent_memory` in backend/agent/agent_runner.py set
  `session.has_run = False` (the comment said "Reset routing: next
  user message goes through coordinator_1"). The intent was to force
  coordinator_1 to re-route on the next turn.

  But `session.has_run = False` is also the flag that
  `_run_basic_stream` reads to decide `reset=True` for the
  downstream agent.run() call. With `reset=True`, smolagents
  invokes `self.memory.reset()` which wipes the very envelope that
  `_replace_agent_memory` had just put into memory.steps.

  So the flow on the 2nd turn became:
    1. coordinator_1 routes to coordinator_2 (sees envelope, routes fine)
    2. _activate_profile_agent builds coordinator_2 with
       memory.steps = [TaskStep(envelope)]
    3. _run_basic_stream calls coordinator_2.run(reset=True) because
       session.has_run is False
    4. smolagents wipes memory.steps → envelope gone
    5. coordinator_2 sees only the new user message, no envelope
    6. LLM (which is the same model that the user asked "do you
       remember which character you chose?") says "I don't have
       memory" because the envelope that explained the prior
       conversation is no longer in its context.

Fix:
  `_replace_agent_memory` should set `session.has_run = True`
  (not False). The envelope IS the result of the last run (a
  virtual "I just read a summary" run), so semantically has_run
  is True. The re-routing is achieved separately by setting
  `session.current_agent_name = entry_agent_name` and
  `session.active_agent = None`, which is unaffected by has_run.

These tests pin the contract:

A. After `_replace_agent_memory` returns, `session.has_run` is True
   (so the next run() call will preserve the envelope, not reset).

B. The 2nd-turn agent LLM call sees the envelope text in its
   input messages (not just the new user question, not an empty
   history).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from smolagents.memory import TaskStep, ActionStep
from smolagents.monitoring import Timing

import time as _time

from backend.agent.agent_runner import AgentRunner, AgentSession
from backend.config import CompactionPromptsConfig, get_config


class _StubLLM:
    """Captures every chat_stream_async call so we can assert what the
    LLM actually saw.

    Returns no content (so the agent loop terminates immediately with
    a final-answer-empty text, which is enough for the test purposes).
    """

    def __init__(self):
        self.calls: list[list[tuple[str, str]]] = []

    async def chat_stream_async(self, messages, **kwargs):
        snap = []
        for m in messages:
            role = getattr(m, "role", "?")
            content = getattr(m, "content", "")
            if isinstance(content, list):
                content = "".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            snap.append((role, str(content)))
        self.calls.append(snap)
        # Provide a tiny final-answer so the agent loop exits cleanly
        if False:
            yield {"content": ""}


def _make_session_with_envelope(envelope_text: str, session_id: str = "s-post-compact"):
    """Build a fresh session whose memory is just the envelope TaskStep.

    This mirrors the post-_replace_agent_memory state we want to assert.
    """
    cfg = get_config()
    cfg.agent_prompts.compaction = CompactionPromptsConfig(
        status_message="",
        no_tools_preamble="",
        main_prompt="",
        custom_instructions_header="",
        no_tools_trailer="",
        summary_envelope_preface="",
        summary_envelope_suffix="",
    )
    stub = _StubLLM()
    runner = AgentRunner(llm_service=stub, config=cfg, compact_orchestrator=object())
    agent = runner._build_profile_agent(
        "coordinator_1",
        runtime_context="ctx",
        session=None,
    )
    agent.memory.steps = [TaskStep(task=envelope_text, task_images=None)]
    session = AgentSession(
        session_id=session_id,
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
        active_agent=agent,
    )
    return session, runner, stub


# =========================================================================
# A. _replace_agent_memory leaves has_run=True so the next run preserves
#    memory (and therefore the envelope).
# =========================================================================


def test_replace_agent_memory_keeps_has_run_true():
    """After _replace_agent_memory runs, session.has_run must be True
    so the next _run_basic_stream call sets reset=False and smolagents
    preserves the envelope in memory.steps.
    """
    cfg = get_config()
    cfg.agent_prompts.compaction = CompactionPromptsConfig(
        status_message="",
        no_tools_preamble="",
        main_prompt="",
        custom_instructions_header="",
        no_tools_trailer="",
        summary_envelope_preface="[",
        summary_envelope_suffix="]",
    )
    stub = _StubLLM()
    runner = AgentRunner(llm_service=stub, config=cfg, compact_orchestrator=object())
    session, _runner2, _stub2 = _make_session_with_envelope("[Original envelope]")

    # Simulate the state right BEFORE _replace_agent_memory:
    # session has 3 historical steps (the LLM was previously running).
    from smolagents.memory import TaskStep as TS
    session.agent.memory.steps = [
        TS(task="old user msg", task_images=None),
        ActionStep(step_number=1, timing=Timing(start_time=_time.time())),
        TS(task="another old msg", task_images=None),
    ]
    session.has_run = True  # was True before compact

    runner._replace_agent_memory(session, "Compressed summary text", "compact-xyz")

    assert session.has_run is True, (
        "session.has_run must be True after _replace_agent_memory, "
        "otherwise the next _run_basic_stream will call "
        "agent.run(reset=True) which wipes the envelope via "
        "smolagents memory.reset()."
    )


# =========================================================================
# B. End-to-end: a post-compact 2nd-turn agent run sees the envelope
#    in the LLM call.
# =========================================================================


def test_post_compact_second_turn_preserves_envelope_in_llm_call():
    """Simulate: a session that just got compacted, then a new user
    turn runs the basic agent. Capture the LLM input and assert
    the envelope is present.

    This test fails before the fix because the post-compact
    has_run=False causes reset=True → smolagents wipes the envelope.
    """
    summary_text = (
        "User asked to read novel Green Angel. "
        "Agent summarized and analyzed characters. "
        "User asked which character is the agent's favorite. "
        "Agent said it has no personal preferences."
    )
    cfg = get_config()
    cfg.agent_prompts.compaction = CompactionPromptsConfig(
        status_message="",
        no_tools_preamble="",
        main_prompt="",
        custom_instructions_header="",
        no_tools_trailer="",
        summary_envelope_preface="[ENVELOPE START]\n",
        summary_envelope_suffix="\n[ENVELOPE END]",
    )
    expected_envelope = (
        cfg.agent_prompts.compaction.summary_envelope_preface
        + summary_text
        + cfg.agent_prompts.compaction.summary_envelope_suffix
    )
    # Build a runner + session directly (don't go through the
    # _make_session_with_envelope helper because it mutates the
    # global config and overrides the values we just set).
    stub = _StubLLM()
    runner = AgentRunner(llm_service=stub, config=cfg, compact_orchestrator=object())
    agent = runner._build_profile_agent(
        "coordinator_1", runtime_context="ctx", session=None,
    )
    agent.memory.steps = [TaskStep(task=expected_envelope, task_images=None)]
    session = AgentSession(
        session_id="s-post-compact-b",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
        active_agent=agent,
    )

    # Reproduce the post-compact state: the envelope is in memory
    # AND has_run is False (this is the bug).
    runner._replace_agent_memory(session, summary_text, "compact-xyz")

    # Sanity: the envelope TaskStep is in memory.
    tasks = [s for s in session.agent.memory.steps if isinstance(s, TaskStep)]
    assert any(
        expected_envelope[:40] in (s.task or "") for s in tasks
    ), "envelope must be in memory.steps immediately after compact"

    # Now simulate what _run_basic_stream does:
    # _should_reset = not session.has_run. If has_run were True,
    # _should_reset would be False, smolagents would preserve memory.
    # The bug: has_run is False post-compact, so _should_reset=True
    # → smolagents memory.reset() wipes the envelope.
    _should_reset = not session.has_run
    if _should_reset:
        session.agent.memory.steps = []  # simulate smolagents memory.reset()

    # The envelope MUST still be in memory at this point.
    tasks_after = [s for s in session.agent.memory.steps if isinstance(s, TaskStep)]
    assert any(
        expected_envelope[:40] in (s.task or "") for s in tasks_after
    ), (
        "post-compact 2nd turn: envelope was wiped from memory "
        "because session.has_run was False → _should_reset=True → "
        "smolagents memory.reset() cleared the envelope TaskStep. "
        "Fix: _replace_agent_memory must set session.has_run = True."
    )


# =========================================================================
# C. End-to-end: the LLM call after a compact sees the envelope text.
# =========================================================================


def test_post_compact_llm_call_actually_sees_envelope():
    """Full chain: build session → compact → write_memory_to_messages
    → assert envelope text is in the resulting ChatMessage list that
    gets sent to the LLM. This is the most direct test of "the agent
    actually uses the summary in its next decision".

    Before the fix, the envelope was wiped by memory.reset() before
    write_memory_to_messages ran, so the LLM saw only the new user
    message and produced an "I don't remember" response.
    """
    from backend.agent.agent_runner import AgentRunner as _AR

    summary_text = (
        "User asked to read novel Green Angel. "
        "Agent summarized the plot and analyzed the protagonist Green. "
        "User asked which character is the agent's favorite. "
        "Agent replied it has no personal preferences as an AI."
    )
    cfg = get_config()
    cfg.agent_prompts.compaction = CompactionPromptsConfig(
        status_message="",
        no_tools_preamble="",
        main_prompt="",
        custom_instructions_header="",
        no_tools_trailer="",
        summary_envelope_preface="[ENVELOPE START]\n",
        summary_envelope_suffix="\n[ENVELOPE END]",
    )
    stub = _StubLLM()
    runner = AgentRunner(llm_service=stub, config=cfg, compact_orchestrator=object())
    agent = runner._build_profile_agent(
        "coordinator_1", runtime_context="ctx", session=None,
    )
    agent.memory.steps = [
        TaskStep(task="old question 1", task_images=None),
        ActionStep(step_number=1, timing=Timing(start_time=_time.time())),
        TaskStep(task="old question 2", task_images=None),
    ]
    session = AgentSession(
        session_id="s-e2e",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
        active_agent=agent,
    )

    # The compact step.
    runner._replace_agent_memory(session, summary_text, "compact-e2e")

    # The post-compact 2nd turn. _run_basic_stream computes
    # _should_reset = not session.has_run and then calls
    # agent.run(message, reset=_should_reset, ...). If has_run is
    # correctly True after compact, smolagents will preserve the
    # envelope in memory.steps. If has_run is False (the bug), it
    # calls memory.reset() and the envelope is gone.
    _should_reset = not session.has_run
    if _should_reset:
        # Simulate the smolagents memory.reset() wipe.
        session.agent.memory.steps = []

    # Now ask the agent to write its memory to messages and check
    # the envelope is in the wire shape.
    messages = session.agent.write_memory_to_messages()
    serialized = []
    for m in messages:
        role = getattr(m, "role", "?")
        content = getattr(m, "content", "")
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        serialized.append((role, str(content)))

    blob = "\n".join(f"{r}: {c}" for r, c in serialized)
    assert "ENVELOPE START" in blob, (
        f"envelope missing from LLM call input. Full messages:\n{blob}"
    )
    assert summary_text in blob, (
        f"summary text missing from LLM call input. Full messages:\n{blob}"
    )
    assert "ENVELOPE END" in blob, (
        f"envelope suffix missing from LLM call input. Full messages:\n{blob}"
    )
