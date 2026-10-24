"""Regression: agent-mode /compact MUST update the agent session's memory.

Bug history (2026-06-06):
  Vision/agent-mode user reported that /compact appears to succeed
  (compact_summary bubble appears in UI, SGLang terminal shows the
  ~4k token compact LLM call) but the NEXT /v1/agent/chat request
  still sees the full 40k context.

Two combined bugs:

1. Frontend (page.tsx, vision/page.tsx) hardcoded
       mode: 'chat'
   in llmClient.runCompact, regardless of settings.agentMode.
   So agent-mode /compact went to the CHAT compact orchestrator,
   which generates a summary in the chat context but does NOT touch
   session.agent.memory.steps.

2. The agent runner's own _run_compact_then_continue path
   (used for auto-compact AND the natural manual /compact path
   in agent mode) built CompactRequest with body_messages=None AND
   messages=None, which makes the inlined no_messages branch in
   CompactOrchestrator.run_compact yield compact_failed("no_messages").
   So even when a caller DID route correctly to the agent compact
   logic, the orchestrator would yield compact_failed.

These tests pin the contract:

A. AgentRunner.run_compact_for_session(session_id, trigger) exists
   and yields CompactEvents; on COMPLETED, session.agent.memory.steps
   has been replaced with the envelope step.

B. AgentRunner._run_compact_then_continue(session, trigger) NO LONGER
   raises during the orchestrator's collect phase (it builds the
   body from memory.steps and passes it as req.messages).

C. /v1/compact/run with mode='agent' + session_id routes to the
   agent runner (so manual /compact in agent mode ACTUALLY affects
   the session), and the resulting session.agent.memory.steps has
   the envelope.

D. (Frontend) page-level handleSelectSlashCommand emits mode='agent'
   when settings.agentMode is true, mode='chat' otherwise.
   This is a contract test on the request body construction.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _restore_global_compact_config():
    cfg = __import__("backend.config", fromlist=["get_config"]).get_config()
    saved_compaction = cfg.agent_prompts.compaction
    saved_validation = {
        "validation_min_summary_chars": cfg.compact.validation_min_summary_chars,
        "validation_min_summary_prompt_ratio": cfg.compact.validation_min_summary_prompt_ratio,
        "validation_require_summary_sections": cfg.compact.validation_require_summary_sections,
    }
    yield
    cfg.agent_prompts.compaction = saved_compaction
    for key, value in saved_validation.items():
        setattr(cfg.compact, key, value)


def _relax_compact_validation(cfg) -> None:
    cfg.compact.validation_min_summary_chars = 1
    cfg.compact.validation_min_summary_prompt_ratio = 0.0
    cfg.compact.validation_require_summary_sections = False


# =========================================================================
# Shared test fixtures
# =========================================================================


class _StubLLM:
    """Minimal LLM service stub that returns one fixed summary chunk."""

    def __init__(self, summary: str = "<summary>Compressed</summary>"):
        self.summary = summary
        self.stream_calls: list[dict] = []

    async def chat_stream_async(self, **kwargs):
        self.stream_calls.append(kwargs)
        # yield a single complete chunk so the orchestrator moves on
        yield {"content": self.summary}


def _make_session_with_steps(steps_text: list[str], session_id: str = "s-test"):
    from smolagents.memory import TaskStep, ActionStep
    from smolagents.monitoring import Timing
    from smolagents.models import ChatMessage, MessageRole
    import time as _time

    steps = []
    for i, text in enumerate(steps_text):
        if i % 2 == 0:
            steps.append(TaskStep(task=text, task_images=None))
        else:
            s = ActionStep(step_number=i, timing=Timing(start_time=_time.time()))
            s.model_output = text
            steps.append(s)

    class _Agent:
        def __init__(self):
            # system_prompt is a duck-typed object whose .to_messages()
            # returns the [system] ChatMessage that smolagents would build
            # for this profile. We return a single system message so the
            # resulting list mirrors what a real ToolCallingAgent would
            # produce (and what the v2 compact body path consumes).
            system_prompt = SimpleNamespace(
                to_messages=lambda summary_mode=False: [
                    ChatMessage(role=MessageRole.SYSTEM, content="[SYS]"),
                ]
            )
            self.memory = SimpleNamespace(
                steps=steps,
                system_prompt=system_prompt,
            )

        def write_memory_to_messages(self, summary_mode: bool = False):
            """Mimic smolagents.MultiStepAgent.write_memory_to_messages (v2 path)."""
            messages = list(self.memory.system_prompt.to_messages(summary_mode=summary_mode))
            for memory_step in self.memory.steps:
                messages.extend(memory_step.to_messages(summary_mode=summary_mode))
            return messages

    agent = _Agent()
    from backend.agent.agent_runner import AgentSession
    return AgentSession(
        session_id=session_id,
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
        active_agent=agent,
    )


# =========================================================================
# A. AgentRunner.run_compact_for_session
# =========================================================================


@pytest.mark.asyncio
async def test_run_compact_for_session_replaces_memory_on_completion(monkeypatch):
    """When the public run_compact_for_session completes, the session's
    memory.steps has been replaced with the envelope step.
    """
    from backend.agent.agent_runner import AgentRunner
    from backend.config import CompactionPromptsConfig
    from backend.compact.types import CompactEventKind

    cfg = __import__("backend.config", fromlist=["get_config"]).get_config()
    _relax_compact_validation(cfg)
    cfg.agent_prompts.compaction = CompactionPromptsConfig(
        status_message="",
        no_tools_preamble="",
        main_prompt="",
        custom_instructions_header="",
        no_tools_trailer="",
        summary_envelope_preface="[PRE]",
        summary_envelope_suffix="[/PRE]",
    )

    runner = AgentRunner(llm_service=_StubLLM(), config=cfg, compact_orchestrator=object())
    session = _make_session_with_steps(["first", "second", "third"])
    old_step_count = len(session.agent.memory.steps)
    assert old_step_count == 3

    # The runner has to be able to look up the session by id
    runner.sessions.put(session)

    events = []
    async for ev in runner.run_compact_for_session(session.session_id, trigger="manual"):
        events.append(ev)

    kinds = [ev.kind for ev in events]
    assert CompactEventKind.STARTED in kinds, "expected compact_started event"
    assert CompactEventKind.COMPLETED in kinds, "expected compact_completed event"
    assert CompactEventKind.FAILED not in kinds, "compact must not fail"

    # CRITICAL: session.agent.memory.steps has been replaced with envelope
    assert len(session.agent.memory.steps) == 1, (
        f"session memory not replaced; still {len(session.agent.memory.steps)} steps"
    )
    from smolagents.memory import TaskStep
    assert isinstance(session.agent.memory.steps[0], TaskStep)
    assert "[PRE]" in session.agent.memory.steps[0].task
    assert "Compressed" in session.agent.memory.steps[0].task


@pytest.mark.asyncio
async def test_run_compact_for_session_yields_failed_when_no_session():
    """Calling run_compact_for_session with an unknown session_id
    yields a compact_failed event (no silent success).
    """
    from backend.agent.agent_runner import AgentRunner
    from backend.compact.types import CompactEventKind

    cfg = __import__("backend.config", fromlist=["get_config"]).get_config()
    runner = AgentRunner(llm_service=_StubLLM(), config=cfg, compact_orchestrator=object())

    events = []
    async for ev in runner.run_compact_for_session("does-not-exist", trigger="manual"):
        events.append(ev)

    kinds = [ev.kind for ev in events]
    assert CompactEventKind.FAILED in kinds
    failed = next(ev for ev in events if ev.kind == CompactEventKind.FAILED)
    assert failed.metadata.get("reason") == "no_session", (
        f"expected reason='no_session', got {failed.metadata!r}"
    )


@pytest.mark.asyncio
async def test_run_compact_for_session_passes_custom_instructions_to_compact_request(monkeypatch):
    from backend.agent.agent_runner import AgentRunner
    from backend.compact.types import CompactEvent

    cfg = __import__("backend.config", fromlist=["get_config"]).get_config()
    runner = AgentRunner(llm_service=_StubLLM(), config=cfg, compact_orchestrator=object())
    session = _make_session_with_steps(["first", "second"], session_id="agent-custom")
    runner.sessions.put(session)
    captured = {}

    def _capture_orchestrator(*args, **kwargs):
        async def _fake_run(req):
            captured["req"] = req
            yield CompactEvent.completed("compact-1", "summary")

        return SimpleNamespace(run_compact=_fake_run)

    monkeypatch.setattr(
        "backend.compact.orchestrator.CompactOrchestrator",
        _capture_orchestrator,
    )

    async for _ in runner.run_compact_for_session(
        "agent-custom",
        trigger="manual",
        custom_instructions="focus on manual agent details",
    ):
        pass

    assert captured["req"].custom_instructions == "focus on manual agent details"


@pytest.mark.asyncio
async def test_manual_compact_rejects_pending_admission_and_active_run():
    from backend.agent.agent_runner import AgentRunner
    from backend.compact.types import CompactEventKind

    cfg = __import__("backend.config", fromlist=["get_config"]).get_config()
    llm = _StubLLM()
    runner = AgentRunner(llm_service=llm, config=cfg, compact_orchestrator=object())
    session = _make_session_with_steps(["first", "second"], session_id="compact-busy")
    runner.sessions.put(session)

    admission = runner.stage_agent_request_admission(
        session.session_id,
        "tool_calling",
        None,
    )
    pending_events = [
        event
        async for event in runner.run_compact_for_session(session.session_id)
    ]
    assert pending_events[0].kind == CompactEventKind.FAILED
    assert pending_events[0].metadata["reason"] == "session_busy"
    assert llm.stream_calls == []
    runner.rollback_agent_request_stage(admission)

    scope_id = session.begin_run()
    active_events = [
        event
        async for event in runner.run_compact_for_session(session.session_id)
    ]
    session.finish_run(scope_id)
    assert active_events[0].kind == CompactEventKind.FAILED
    assert active_events[0].metadata["reason"] == "session_busy"
    assert llm.stream_calls == []


@pytest.mark.asyncio
async def test_manual_compact_run_scope_blocks_concurrent_agent_admission(monkeypatch):
    from backend.agent.agent_runner import AgentRunner, SessionBusyError
    from backend.compact.types import CompactEvent

    cfg = __import__("backend.config", fromlist=["get_config"]).get_config()
    runner = AgentRunner(llm_service=_StubLLM(), config=cfg, compact_orchestrator=object())
    session = _make_session_with_steps(["first", "second"], session_id="compact-race")
    runner.sessions.put(session)
    entered = asyncio.Event()
    release = asyncio.Event()

    def _blocking_orchestrator(*args, **kwargs):
        async def _run(req):
            entered.set()
            yield CompactEvent.started("compact-race-summary", "manual", "agent")
            await release.wait()
            yield CompactEvent.failed(
                "compact-race-summary",
                "test_complete",
                "test release",
            )

        return SimpleNamespace(run_compact=_run)

    monkeypatch.setattr(
        "backend.compact.orchestrator.CompactOrchestrator",
        _blocking_orchestrator,
    )

    async def _consume():
        return [
            event
            async for event in runner.run_compact_for_session(session.session_id)
        ]

    compact_task = asyncio.create_task(_consume())
    await entered.wait()
    assert session.capture_active_run_scopes()
    with pytest.raises(SessionBusyError):
        runner.stage_agent_request_admission(
            session.session_id,
            "tool_calling",
            None,
        )
    release.set()
    await compact_task
    assert session.capture_active_run_scopes() == frozenset()


# =========================================================================
# B. _run_compact_then_continue no longer raises
# =========================================================================


@pytest.mark.asyncio
async def test_run_compact_then_continue_passes_messages_from_memory(monkeypatch):
    """The auto-compact path inside AgentRunner must build the body
    from session.agent.memory.steps and pass it as req.body_messages
    (v2) or req.messages (v1);
    otherwise the inlined no_messages branch in
    CompactOrchestrator.run_compact fires and the path silently
    produces compact_failed.

    _run_compact_then_continue yields SSE strings (not CompactEvent
    objects) because it is consumed by the /v1/agent/chat SSE loop.
    We verify the contract by:
      1. Reading the yielded SSE lines for compact_failed
      2. Asserting no compact_failed appears (i.e. the ValueError
         path was fixed)
      3. Asserting compact_started + compact_completed appear
      4. Asserting the session's memory.steps was actually replaced
    """
    from backend.agent.agent_runner import AgentRunner
    from backend.config import CompactionPromptsConfig

    cfg = __import__("backend.config", fromlist=["get_config"]).get_config()
    _relax_compact_validation(cfg)
    cfg.agent_prompts.compaction = CompactionPromptsConfig(
        status_message="",
        no_tools_preamble="",
        main_prompt="",
        custom_instructions_header="",
        no_tools_trailer="",
        compact_user_instruction="CONTROL",
        compact_control_user_instruction="TAIL_CONTROL",
        summary_envelope_preface="[",
        summary_envelope_suffix="]",
    )

    stub = _StubLLM()
    runner = AgentRunner(llm_service=stub, config=cfg, compact_orchestrator=object())
    session = _make_session_with_steps(["q1", "a1", "q2", "a2"])
    initial_step_count = len(session.agent.memory.steps)
    assert initial_step_count == 4

    sse_lines: list[str] = []
    async for sse_line in runner._run_compact_then_continue(session, trigger="auto"):
        sse_lines.append(sse_line)
        # Lines look like: data: {"type": "timeline_event", "event": {"kind": "...", ...}}
        if '"kind": "compact_completed"' in sse_line:
            break

    sse_blob = "\n".join(sse_lines)
    assert "compact_started" in sse_blob, sse_blob
    assert "compact_completed" in sse_blob, sse_blob
    assert "compact_failed" not in sse_blob, (
        f"auto-compact must not fail; got SSE: {sse_blob!r}"
    )

    # The LLM was actually called (orchestrator reached the LLM call)
    assert len(stub.stream_calls) == 1, (
        "orchestrator never reached the LLM call; the no_messages branch probably fired"
    )
    call = stub.stream_calls[0]
    # Default compact path is cache-first: preserve the prior system+history
    # prefix and append the compact-control user turn at the tail.
    # For a 4-step session ([q1, a1, q2, a2]) the body is:
    #   [system, u<q1>, a<a1>, u<q2>, a<a2>, compact-control]
    assert len(call["messages"]) == 6, (
        f"expected 6 msgs to LLM (cache-first v2 path: body=5 + compact-control=1), "
        f"got {len(call['messages'])}: "
        f"{[(m.role, m.content) for m in call['messages']]}"
    )
    # First message is the normal system message; final user message is the
    # compact-control instruction, expanded with the self-contained cache-first
    # format requirements.
    assert call["messages"][0].role == "system"
    assert call["messages"][-1].role == "user"
    tail_instruction = call["messages"][-1].content
    assert tail_instruction.startswith("This message is an internal server/system instruction delivered through the user channel")
    assert "TAIL_CONTROL" in tail_instruction
    assert "not a real user request" in tail_instruction
    assert "Do not call tools" in tail_instruction
    assert "Do not route agents" in tail_instruction
    assert "CACHE-FIRST COMPACT FORMAT REQUIREMENTS" in tail_instruction
    assert "Primary Request and Intent" in tail_instruction
    assert "Optional Next Step" in tail_instruction
    roles = [m.role for m in call["messages"]]
    assert roles == ["system", "user", "assistant", "user", "assistant", "user"], roles

    # session.agent.memory.steps was replaced with the envelope step
    from smolagents.memory import TaskStep
    assert len(session.agent.memory.steps) == 1
    assert isinstance(session.agent.memory.steps[0], TaskStep)
    assert "[" in session.agent.memory.steps[0].task  # envelope preface
    assert "Compressed" in session.agent.memory.steps[0].task  # summary text


# =========================================================================
# C. /v1/compact/run with mode=agent + session_id routes to agent runner
# =========================================================================


@pytest.mark.asyncio
async def test_compact_run_routes_agent_mode_to_agent_runner(monkeypatch):
    """POST /v1/compact/run with mode='agent' and a session_id must
    call AgentRunner.run_compact_for_session (so the session's
    memory.steps is actually compacted) and yield the resulting
    CompactEvents as SSE data lines.
    """
    from httpx import ASGITransport, AsyncClient

    # Spy on AgentRunner.run_compact_for_session
    from backend.agent import agent_runner as ar_module
    captured = {"called_with": None, "session": None, "custom_instructions": None}

    async def _fake_run_compact_for_session(
        self,
        session_id,
        mode=None,
        trigger="manual",
        custom_instructions=None,
    ):
        captured["called_with"] = (session_id, trigger)
        captured["custom_instructions"] = custom_instructions
        # Reuse the real method semantics: pull session, replace memory
        session = self.sessions.get(session_id)
        captured["session"] = session
        if session is None:
            from backend.compact.types import CompactEvent, CompactEventKind
            yield CompactEvent.failed("x", "no_session", f"no {session_id}")
            return
        from backend.compact.types import CompactEvent, CompactEventKind
        sid = CompactEvent.new_summary_id()
        yield CompactEvent.started(summary_message_id=sid, trigger=trigger, mode="agent")
        # Simulate the replacement
        self._replace_agent_memory(session, "Compressed text", sid)
        yield CompactEvent.completed(sid, "Compressed text")

    monkeypatch.setattr(
        ar_module.AgentRunner,
        "run_compact_for_session",
        _fake_run_compact_for_session,
    )

    # Build a session and register it with the runner
    session = _make_session_with_steps(["a", "b", "c"], session_id="agent-xyz")

    from backend.main import app
    # Reset and inject a fresh AgentRunner
    from backend.agent.agent_runner import AgentRunner
    cfg = __import__("backend.config", fromlist=["get_config"]).get_config()
    runner = AgentRunner(llm_service=_StubLLM(), config=cfg, compact_orchestrator=object())
    runner.sessions.put(session)
    app.state.agent_runner = runner
    # Also make sure the chat compact orchestrator is set (defense)
    if getattr(app.state, "compact_orchestrator", None) is None:
        from backend.compact.orchestrator import CompactOrchestrator
        app.state.compact_orchestrator = CompactOrchestrator(
            llm_service=_StubLLM(),
            config=cfg.compact,
            prompts=cfg.agent_prompts.compaction,
        )

    body = {
        "mode": "agent",
        "session_id": "agent-xyz",
        "messages": None,  # agent mode builds body from session
        "custom_instructions": "focus on agent memory",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("POST", "/v1/compact/run", json=body) as resp:
            assert resp.status_code == 200
            text = ""
            async for chunk in resp.aiter_text():
                text += chunk
                if "[DONE]" in text:
                    break

    # The agent runner was actually called (not the chat compact)
    assert captured["called_with"] is not None, (
        "AgentRunner.run_compact_for_session was NOT called for mode=agent"
    )
    assert captured["called_with"][0] == "agent-xyz"
    assert captured["custom_instructions"] == "focus on agent memory"

    # The SSE stream contained compact_started and compact_completed
    assert "compact_started" in text
    assert "compact_completed" in text


@pytest.mark.asyncio
async def test_compact_run_chat_mode_unchanged(monkeypatch):
    """Defense: /v1/compact/run with mode='chat' still uses the
    chat compact orchestrator (this was the working path; must not
    regress).
    """
    from httpx import ASGITransport, AsyncClient

    from backend.main import app
    cfg = __import__("backend.config", fromlist=["get_config"]).get_config()

    # Spy on the chat compact orchestrator's run_compact
    from backend.compact.orchestrator import CompactOrchestrator
    chat_called = {"yes": False}

    async def _spy(self, req):
        chat_called["yes"] = True
        from backend.compact.types import CompactEvent, CompactEventKind
        sid = CompactEvent.new_summary_id()
        yield CompactEvent.started(summary_message_id=sid, trigger=req.trigger, mode=req.mode)
        yield CompactEvent.completed(sid, "summary")

    monkeypatch.setattr(CompactOrchestrator, "run_compact", _spy)

    app.state.compact_orchestrator = CompactOrchestrator(
        llm_service=_StubLLM(),
        config=cfg.compact,
        prompts=cfg.agent_prompts.compaction,
    )

    body = {
        "mode": "chat",
        "session_id": None,
        "messages": [{"role": "user", "content": "hi"}],
        "custom_instructions": None,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("POST", "/v1/compact/run", json=body) as resp:
            assert resp.status_code == 200
            text = ""
            async for chunk in resp.aiter_text():
                text += chunk
                if "[DONE]" in text:
                    break

    assert chat_called["yes"], "chat compact orchestrator should still be invoked for mode=chat"
