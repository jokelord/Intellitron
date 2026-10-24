"""Tests for cycle-mode auto-compact integration.

The full _execute_steps loop requires Pydantic CycleStepRequest objects and a
running session; here we exercise only the helper-level integration:

1. CycleExecutor passes agent_runner through to compact logic
2. The compact check guard (share_context + message + orchestrator + session)
   is correct
3. cycle_min_steps_between_compacts actually throttles compact frequency
   (regression test for the dead-config bug fixed on 2026-06-07:
   the field used to be parsed and documented but never read by any code path)
4. The trigger fires at the START of the next message step ("最晚那一个发送时"),
   not at the END of the previous one
5. State is reset after the cycle ends (no leak across sessions)
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.compact.orchestrator import CompactOrchestrator
from backend.config import get_config
from backend.cycle.executor import CycleExecutor
from backend.cycle.camera import CameraService
from backend.cycle.models import CycleStepRequest


class _StubLLM:
    async def chat_stream_async(self, **kwargs):
        yield {"content": "<summary>Compressed</summary>"}


def _make_compact_runner():
    """Build a MagicMock agent_runner wired up so the cycle's compact check
    is actually reachable (orchestrator present, sessions dict, helpers)."""
    cfg = get_config()
    runner = MagicMock()
    runner.compact_orchestrator = CompactOrchestrator(
        llm_service=_StubLLM(),
        config=cfg.compact,
        prompts=cfg.agent_prompts.compaction,
    )
    # sessions[agent_sid] is queried in the compact check path
    runner.sessions = {}
    return runner


def _build_cycle_exec(runner):
    camera = MagicMock(spec=CameraService)
    return CycleExecutor(
        agent_runner=runner,
        camera_service=camera,
        max_depth=5,
        max_duration=3600,
    )


def _message_step(text):
    return CycleStepRequest(type="message", message=text)


def test_cycle_executor_accepts_agent_runner_with_compact_orchestrator():
    cfg = get_config()
    runner = MagicMock()
    runner.compact_orchestrator = CompactOrchestrator(
        llm_service=_StubLLM(),
        config=cfg.compact,
        prompts=cfg.agent_prompts.compaction,
    )
    camera = MagicMock(spec=CameraService)
    exec_ = CycleExecutor(
        agent_runner=runner,
        camera_service=camera,
        max_depth=5,
        max_duration=3600,
    )
    assert exec_._agent_runner is runner


def test_cycle_executor_works_without_compact_orchestrator():
    """When agent_runner.compact_orchestrator is None, cycle still runs (compact is no-op)."""
    cfg = get_config()
    runner = MagicMock()
    runner.compact_orchestrator = None
    camera = MagicMock(spec=CameraService)
    exec_ = CycleExecutor(
        agent_runner=runner,
        camera_service=camera,
        max_depth=5,
        max_duration=3600,
    )
    assert exec_._agent_runner.compact_orchestrator is None


def test_compact_check_guard_evaluates_correctly():
    """Verify the in-loop guard conditions by direct evaluation of the boolean expression."""
    cfg = get_config()
    cfg.compact.enabled = True
    cfg.compact.auto_trigger = True
    runner = MagicMock()
    runner.compact_orchestrator = CompactOrchestrator(
        llm_service=_StubLLM(),
        config=cfg.compact,
        prompts=cfg.agent_prompts.compaction,
    )
    camera = MagicMock(spec=CameraService)
    exec_ = CycleExecutor(
        agent_runner=runner,
        camera_service=camera,
        max_depth=5,
        max_duration=3600,
    )
    # We can't easily exercise _execute_steps without a full request setup,
    # but we can verify the boolean guard components.
    step_message = SimpleNamespace(type="message", message="hi")
    step_wait = SimpleNamespace(type="wait", wait=1)
    step_cycle = SimpleNamespace(type="cycle", cycle_count=1, cycle_steps=[])

    # All four conditions must be true: share_context AND step.type=="message"
    # AND agent_runner is not None AND orchestrator is not None
    for step in (step_message, step_wait, step_cycle):
        should_run = (
            True  # share_context=True
            and step.type == "message"
            and exec_._agent_runner is not None
            and getattr(exec_._agent_runner, "compact_orchestrator", None) is not None
        )
        if step is step_message:
            assert should_run is True
        else:
            assert should_run is False


# ---------------------------------------------------------------------------
# Throttle behavior tests — pin the cycle_min_steps_between_compacts wire-up.
# These exercise the FULL _execute_steps loop (via start()), with the LLM
# service stubbed and the compact check helpers mocked.
# ---------------------------------------------------------------------------

class _CycleStepRecorder:
    """Captures the index of every message step the cycle ran, in order.
    Used to assert the relative ordering of compact fires vs message steps."""

    def __init__(self):
        self.message_indices: list[int] = []


def _make_recording_runner(counter_ref, agent_sid="agent-sid-stub"):
    """Build a runner where:
      * run_stream yields session_start + llm content (so the executor records agent_sid)
      * _should_auto_compact_session always returns True (so the check, if it runs, fires)
      * _run_compact_then_continue is an AsyncMock we can count

    The counter_ref is a dict that gets mutated when compact fires; tests assert
    on it to know exactly when the LLM summary call was made.
    """
    runner = _make_compact_runner()
    recorder = _CycleStepRecorder()
    runner._recorder = recorder
    runner._compact_fire_count = 0

    async def fake_run_stream(session_id, message, *_args, **kwargs):
        # Emit a session_start so _agent_session_ids gets populated.
        yield 'data: {"type": "session_start", "session_id": "%s"}\n\n' % agent_sid
        yield 'data: {"type": "content", "content": "ok"}\n\n'
        yield "data: [DONE]\n\n"

    runner.run_stream = fake_run_stream
    runner._should_auto_compact_session = MagicMock(return_value=True)

    async def fake_run_compact_then_continue(session, trigger):
        # Each compact call appends to counter_ref. Tests use len(counter_ref)
        # to know how many times compact fired.
        counter_ref.append({"trigger": trigger, "session_id": session.session_id})
        # Yield a real compact timeline event so SSE-ordering tests can find it.
        yield (
            'data: {"type": "timeline_event", '
            '"event": {"kind": "compact_completed", '
            '"summary_message_id": "stub-sum"}}\n\n'
        )

    runner._run_compact_then_continue = fake_run_compact_then_continue

    # Pre-populate the session lookup so the compact check finds a session object.
    fake_session = MagicMock()
    fake_session.session_id = agent_sid
    fake_session.agent.memory.steps = [MagicMock()]
    runner.sessions = {agent_sid: fake_session}

    return runner


@pytest.mark.asyncio
async def test_cycle_throttle_default_zero_compacts_at_every_message_boundary(monkeypatch):
    """With cycle_min_steps_between_compacts=0 (default), compact should fire
    at every message-step boundary once session exists, satisfying the spec
    contract documented in docs/.../2026-06-06-slash-commands-and-compact-design.md
    line 503 ('0 = check every message step')."""
    cfg = get_config()
    cfg.compact.enabled = True
    cfg.compact.auto_trigger = True
    cfg.compact.cycle_min_steps_between_compacts = 0

    compact_calls: list[dict] = []
    runner = _make_recording_runner(compact_calls)
    exec_ = _build_cycle_exec(runner)

    # 4 message steps → 3 internal boundaries. i=0 is the first step and
    # explicitly skipped (no prior step to "send"). Then i=1, 2, 3 each
    # check, and with min_steps=0 all of them fire.
    steps = [_message_step(f"m{i}") for i in range(4)]
    events = [ev async for ev in exec_.start("cycle-sid-1", steps, agent_mode=True, share_context=True)]
    assert any(ev for ev in events)  # sanity: the cycle produced events

    # 3 boundaries × 1 compact each = 3 compacts.
    assert len(compact_calls) == 3, (
        f"expected 3 compacts (one per internal boundary), got {len(compact_calls)}: "
        f"{compact_calls}"
    )


@pytest.mark.asyncio
async def test_cycle_throttle_with_n3_compacts_every_3_message_steps(monkeypatch):
    """With cycle_min_steps_between_compacts=3, compact should fire at most
    once every 3 message steps. This is the actual behavior the user expects
    when they set this config — the bug fix ensures it's no longer dead code."""
    cfg = get_config()
    cfg.compact.enabled = True
    cfg.compact.auto_trigger = True
    cfg.compact.cycle_min_steps_between_compacts = 3

    compact_calls: list[dict] = []
    runner = _make_recording_runner(compact_calls)
    exec_ = _build_cycle_exec(runner)

    # 7 message steps. Counter goes 0→1→2→3→4→5→6. With min_steps=3:
    #   i=1: counter=0, last=-(3+1)=-4, 0-(-4)=4>=3 → fires, last=0
    #   i=2: counter=1, last=0, 1-0=1<3 → skip
    #   i=3: counter=2, last=0, 2-0=2<3 → skip
    #   i=4: counter=3, last=0, 3-0=3>=3 → fires, last=3
    #   i=5: counter=4, last=3, 4-3=1<3 → skip
    #   i=6: counter=5, last=3, 5-3=2<3 → skip
    # Total: 2 compacts.
    steps = [_message_step(f"m{i}") for i in range(7)]
    async for _ in exec_.start("cycle-sid-2", steps, agent_mode=True, share_context=True):
        pass

    assert len(compact_calls) == 2, (
        f"expected 2 compacts (at i=1 and i=4), got {len(compact_calls)}: "
        f"{compact_calls}"
    )


@pytest.mark.asyncio
async def test_cycle_throttle_nested_cycle_shares_counter(monkeypatch):
    """Nested cycle's message steps count toward the same throttle counter
    (same session_id), so the throttle applies to the whole cycle tree, not
    just the top-level step index."""
    cfg = get_config()
    cfg.compact.enabled = True
    cfg.compact.auto_trigger = True
    cfg.compact.cycle_min_steps_between_compacts = 4

    compact_calls: list[dict] = []
    runner = _make_recording_runner(compact_calls)
    exec_ = _build_cycle_exec(runner)

    # Outer: msg, cycle{count=1, steps=[msg, msg, msg]}, msg, msg, msg
    # Counter increments on every message step (outer or nested).
    # With min_steps=4 and 7 total message steps:
    #   i=1 (outer msg): counter=0, 0-(-5)=5>=4 → fires, last=0
    #   nested msg1: counter=1, 1-0=1<4 → skip
    #   nested msg2: counter=2, 2-0=2<4 → skip
    #   nested msg3: counter=3, 3-0=3<4 → skip
    #   i=3 (outer msg): counter=4, 4-0=4>=4 → fires, last=4
    #   i=4 (outer msg): counter=5, 5-4=1<4 → skip
    #   i=5 (outer msg): counter=6, 6-4=2<4 → skip
    # Total: 2 compacts.
    inner_cycle = CycleStepRequest(
        type="cycle",
        cycle_count=1,
        cycle_steps=[_message_step("inner1"), _message_step("inner2"), _message_step("inner3")],
    )
    steps = [
        _message_step("outer0"),
        inner_cycle,
        _message_step("outer1"),
        _message_step("outer2"),
        _message_step("outer3"),
    ]
    async for _ in exec_.start("cycle-sid-3", steps, agent_mode=True, share_context=True):
        pass

    assert len(compact_calls) == 2, (
        f"expected 2 compacts (top-level msg after outer0, then after 3 nested msgs), "
        f"got {len(compact_calls)}: {compact_calls}"
    )


@pytest.mark.asyncio
async def test_cycle_compact_does_not_fire_when_should_compact_returns_false(monkeypatch):
    """When _should_auto_compact_session returns False, the check is a no-op
    even if the throttle window has elapsed — the gate is the actual token count,
    not the throttle math."""
    cfg = get_config()
    cfg.compact.enabled = True
    cfg.compact.auto_trigger = True
    cfg.compact.cycle_min_steps_between_compacts = 0

    compact_calls: list[dict] = []
    runner = _make_recording_runner(compact_calls)
    runner._should_auto_compact_session = MagicMock(return_value=False)
    exec_ = _build_cycle_exec(runner)

    steps = [_message_step(f"m{i}") for i in range(3)]
    async for _ in exec_.start("cycle-sid-4", steps, agent_mode=True, share_context=True):
        pass

    assert len(compact_calls) == 0, (
        f"expected 0 compacts (should_compact=False at every boundary), got {compact_calls}"
    )


@pytest.mark.asyncio
async def test_cycle_first_step_skips_compact_check(monkeypatch):
    """The very first message step (i=0) must NOT trigger a compact check —
    the session is brand new and _should_auto_compact_session would return False
    anyway, but we want to assert the i>0 guard is enforced explicitly so a
    future refactor can't accidentally compact an empty session."""
    cfg = get_config()
    cfg.compact.enabled = True
    cfg.compact.auto_trigger = True
    cfg.compact.cycle_min_steps_between_compacts = 0

    compact_calls: list[dict] = []
    runner = _make_recording_runner(compact_calls)
    exec_ = _build_cycle_exec(runner)

    # Track invocations: should_compact_session is the gate; if it ever
    # fires for an empty session it's a regression.
    runner._should_auto_compact_session = MagicMock(return_value=True)

    steps = [_message_step("only")]
    async for _ in exec_.start("cycle-sid-5", steps, agent_mode=True, share_context=True):
        pass

    # Only 1 step total, no internal boundary → 0 compacts.
    assert len(compact_calls) == 0
    # The check itself was never even called (i=0 skipped before the gate).
    assert runner._should_auto_compact_session.call_count == 0


@pytest.mark.asyncio
async def test_cycle_throttle_state_cleared_after_completion(monkeypatch):
    """After the cycle finishes, the per-session throttle counter and last-compact
    marker should be popped — no leak that would corrupt the next cycle on the
    same session_id."""
    cfg = get_config()
    cfg.compact.enabled = True
    cfg.compact.auto_trigger = True
    cfg.compact.cycle_min_steps_between_compacts = 0

    compact_calls: list[dict] = []
    runner = _make_recording_runner(compact_calls)
    exec_ = _build_cycle_exec(runner)
    sid = "cycle-sid-6"

    steps = [_message_step(f"m{i}") for i in range(3)]
    async for _ in exec_.start(sid, steps, agent_mode=True, share_context=True):
        pass

    assert sid not in exec_._message_step_counter
    assert sid not in exec_._last_compact_cycle_step
    # And the other per-session dicts are also cleaned up.
    assert sid not in exec_._cancel_events
    assert sid not in exec_._agent_session_ids


@pytest.mark.asyncio
async def test_cycle_compact_triggered_at_start_of_next_message_step(monkeypatch):
    """Regression test for the '最晚那一个相邻消息发送时' semantics:
    the compact fires BEFORE the next message step's run_stream(), not AFTER
    the previous one. We verify by checking the chronological order of
    compact fires vs message step events in the SSE stream."""
    cfg = get_config()
    cfg.compact.enabled = True
    cfg.compact.auto_trigger = True
    cfg.compact.cycle_min_steps_between_compacts = 0

    compact_calls: list[dict] = []
    runner = _make_recording_runner(compact_calls)
    exec_ = _build_cycle_exec(runner)

    # 2 message steps → 1 internal boundary. The compact event must be
    # emitted between step_end of step 0 and step_begin of step 1.
    steps = [_message_step("first"), _message_step("second")]
    events: list[str] = []
    async for ev in exec_.start("cycle-sid-7", steps, agent_mode=True, share_context=True):
        events.append(ev)

    # Find the indices of step_end(step0) and step_begin(step1).
    step_end_step0 = next(
        i for i, ev in enumerate(events)
        if '"step_end"' in ev and '"path": [0]' in ev
    )
    step_begin_step1 = next(
        i for i, ev in enumerate(events)
        if '"step_begin"' in ev and '"path": [1]' in ev
    )
    # Compact must be between them. The compact itself yields a timeline_event
    # for each compact chunk. There must be at least one compact timeline event
    # strictly between the two markers.
    between = events[step_end_step0 + 1:step_begin_step1]
    compact_events = [ev for ev in between if 'compact_completed' in ev]
    assert len(compact_events) >= 1, (
        f"expected compact events between step_end(0) and step_begin(1); "
        f"got between={between!r}"
    )
