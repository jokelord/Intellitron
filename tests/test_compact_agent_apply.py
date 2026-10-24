"""Tests for agent-mode auto-compact integration."""
import json
from types import SimpleNamespace

from backend.compact.orchestrator import CompactOrchestrator
from backend.config import get_config
from backend.agent.agent_runner import AgentSession


class _StubLLM:
    async def chat_stream_async(self, **kwargs):
        yield {"content": "<summary>Compressed</summary>"}


def _make_session_with_steps(steps_text: list[str]):
    """Build a minimal AgentSession with the given user/assistant memory steps."""
    from smolagents.memory import TaskStep, ActionStep
    from smolagents.monitoring import Timing
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
            self.memory = SimpleNamespace(steps=steps)
    agent = _Agent()
    return AgentSession(
        session_id="s-test",
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
        active_agent=agent,
    )


def test_replace_agent_memory_archives_lightweight_metadata_only():
    """Archive must not retain full old memory steps after compact."""
    from backend.agent.agent_runner import AgentRunner
    from backend.config import CompactConfig, CompactionPromptsConfig

    cfg = get_config()
    runner = AgentRunner(llm_service=_StubLLM(), config=cfg)
    session = _make_session_with_steps(["first", "second", "third"])
    session.last_compactable_llm_messages = [SimpleNamespace(role="system", content="OLD HUGE PAYLOAD")]
    session.last_compactable_agent_name = "basic_agent"

    old_steps = list(session.agent.memory.steps)
    cfg.agent.entry_agent_name = "coordinator_1"
    cfg.agent_prompts.compaction = CompactionPromptsConfig(
        status_message="",
        no_tools_preamble="",
        main_prompt="",
        custom_instructions_header="",
        no_tools_trailer="",
        summary_envelope_preface="[PRE]",
        summary_envelope_suffix="[/PRE]",
    )
    runner._replace_agent_memory(session, "compressed text", "compact-xyz")

    assert len(session.compaction_archive) == 1
    archive_entry = session.compaction_archive[0]
    assert archive_entry["summary_id"] == "compact-xyz"
    assert archive_entry["summary_text"] == "compressed text"
    assert archive_entry["archived_step_count"] == len(old_steps)
    assert "archived_steps" not in archive_entry
    assert all(step not in archive_entry.values() for step in old_steps)

    assert len(session.agent.memory.steps) == 1
    from smolagents.memory import TaskStep
    assert isinstance(session.agent.memory.steps[0], TaskStep)
    assert "[PRE]" in session.agent.memory.steps[0].task
    assert "compressed text" in session.agent.memory.steps[0].task

    # Routing reset
    # NB: as of 2026-06-06, has_run is True (not False) after compact.
    # The earlier code set has_run=False as a side-effect of "force
    # re-route via coordinator_1", but that was buggy: it caused
    # the next _run_basic_stream call to compute reset=True and
    # smolagents memory.reset() would wipe the envelope we just put
    # in place. The fix is to keep has_run=True (the envelope IS
    # the result of the just-completed compact) and rely on
    # current_agent_name + active_agent for the re-route.
    assert session.has_run is True
    assert session.current_agent_name == "coordinator_1"
    assert session.active_agent is None
    assert session.last_compactable_llm_messages is None
    assert session.last_compactable_agent_name is None


def test_replace_agent_memory_does_not_touch_runtime_context():
    """Stable prefix invariant: runtime_context is session-pinned and untouched."""
    from backend.agent.agent_runner import AgentRunner
    from backend.config import CompactionPromptsConfig

    cfg = get_config()
    runner = AgentRunner(llm_service=_StubLLM(), config=cfg)
    session = _make_session_with_steps(["hi"])
    session.runtime_context = "Current local time: 2026-06-06 00:00"
    cfg.agent_prompts.compaction = CompactionPromptsConfig(
        status_message="", no_tools_preamble="", main_prompt="",
        custom_instructions_header="", no_tools_trailer="",
        summary_envelope_preface="[", summary_envelope_suffix="]",
    )
    runner._replace_agent_memory(session, "summary", "compact-1")
    assert session.runtime_context == "Current local time: 2026-06-06 00:00"


def test_should_auto_compact_returns_false_without_orchestrator():
    """Without compact_orchestrator, _should_auto_compact_session returns False."""
    from backend.agent.agent_runner import AgentRunner

    cfg = get_config()
    cfg.compact.enabled = True
    cfg.compact.auto_trigger = True
    runner = AgentRunner(llm_service=_StubLLM(), config=cfg, compact_orchestrator=None)
    session = _make_session_with_steps(["hi"])
    assert runner._should_auto_compact_session(session) is False


def test_compact_event_to_sse_format():
    """_compact_event_to_sse yields a parseable data line wrapping a timeline_event."""
    from backend.agent.agent_runner import AgentRunner
    from backend.compact.types import CompactEvent, CompactEventKind

    ev = CompactEvent.started(
        summary_message_id="compact-x",
        trigger="auto",
        mode="agent",
    )
    line = AgentRunner._compact_event_to_sse(ev)
    assert line.startswith("data: ")
    payload = line[6:].strip()
    obj = json.loads(payload)
    assert obj["type"] == "timeline_event"
    assert obj["event"]["kind"] == "compact_started"
    assert obj["event"]["summary_message_id"] == "compact-x"
