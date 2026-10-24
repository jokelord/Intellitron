from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock, patch
import asyncio

from backend.agent.agent_runner import AgentRunner, AgentSession
from backend.compact.types import CompactEvent
from backend.config import CompactConfig, CompactionPromptsConfig
from backend.llm_service import Message


def _prompts():
    return CompactionPromptsConfig(
        no_tools_preamble="COMPACT SYSTEM",
        main_prompt="MAIN",
        no_tools_trailer="TRAILER",
        compact_user_instruction="Summarize follows.",
        compact_control_user_instruction="[[SENTINEL]] summarize prior messages.",
    )


def _session():
    prompts = _prompts()
    agent = MagicMock()
    agent.memory.steps = []
    agent.write_memory_to_messages.return_value = [
        MagicMock(role="system", content="ACTIVE SYSTEM"),
        MagicMock(role="user", content="old task"),
    ]
    s = AgentSession(session_id="s1", agent=agent, mode="tool_calling")
    s.runtime_context = "Current local time: pinned"
    return s


async def _drain(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


def test_agent_cache_first_body_uses_active_system_plus_runtime_context():
    runner = MagicMock()
    runner.config = MagicMock()
    runner.config.compact = CompactConfig(prompt_control_enabled=True, prompt_shape="cache_first_tail_with_fallback")
    runner.config.agent_prompts.compaction = _prompts()
    runner._build_cache_first_compact_body_messages = MethodType(
        AgentRunner._build_cache_first_compact_body_messages, runner
    )

    body = runner._build_cache_first_compact_body_messages(_session())

    assert body is not None
    assert body[0].role == "system"
    assert "ACTIVE SYSTEM" in body[0].content
    assert "Current local time: pinned" in body[0].content
    assert "COMPACT SYSTEM" not in body[0].content
    assert body[1].content == "old task"


def test_agent_cache_first_body_prefers_last_compactable_payload_after_coordinator_reset():
    runner = MagicMock()
    runner.config = MagicMock()
    runner.config.compact = CompactConfig(prompt_control_enabled=True, prompt_shape="cache_first_tail_with_fallback")
    runner.config.agent_prompts.compaction = _prompts()
    runner._build_cache_first_compact_body_messages = MethodType(
        AgentRunner._build_cache_first_compact_body_messages, runner
    )

    session = _session()
    session.current_agent_name = "coordinator_1"
    session.last_compactable_agent_name = "basic_agent"
    session.last_compactable_llm_messages = [
        Message(role="system", content="BASIC SYSTEM WITH RUNTIME"),
        Message(role="user", content="large tool observation payload"),
    ]

    body = runner._build_cache_first_compact_body_messages(session)

    assert body is not None
    assert [m.content for m in body] == [
        "BASIC SYSTEM WITH RUNTIME",
        "large tool observation payload",
    ]
    assert body is not session.last_compactable_llm_messages


def test_agent_cache_first_body_disabled_when_prompt_control_disabled():
    runner = MagicMock()
    runner.config = MagicMock()
    runner.config.compact = CompactConfig(prompt_control_enabled=False, prompt_shape="cache_first_tail_with_fallback")
    runner.config.agent_prompts.compaction = _prompts()
    runner._build_cache_first_compact_body_messages = MethodType(
        AgentRunner._build_cache_first_compact_body_messages, runner
    )

    assert runner._build_cache_first_compact_body_messages(_session()) is None


def test_agent_cache_first_body_preserves_tool_trace_text_and_runtime_context():
    runner = MagicMock()
    runner.config = MagicMock()
    runner.config.compact = CompactConfig(prompt_control_enabled=True, prompt_shape="cache_first_tail_with_fallback")
    runner.config.agent_prompts.compaction = _prompts()
    runner._build_cache_first_compact_body_messages = MethodType(
        AgentRunner._build_cache_first_compact_body_messages, runner
    )

    agent = MagicMock()
    tool_trace = """<<TOOL_CALL>>
{
  "name": "write_file",
  "arguments": {
  "file_path": "/tmp/a.md",
  "content": "payload"
}
}
<<END_TOOL_CALL>>"""
    observation = "Observation:\nPath: /tmp/a.md\npayload"
    original_messages = [
        MagicMock(role="system", content="ACTIVE SYSTEM"),
        MagicMock(role="assistant", content=tool_trace),
        MagicMock(role="user", content=observation),
    ]
    agent.write_memory_to_messages.return_value = original_messages
    session = AgentSession(session_id="s-tool", agent=agent, mode="tool_calling")
    session.runtime_context = "Current local time: pinned"

    body = runner._build_cache_first_compact_body_messages(session)

    assert body is not None
    assert body[0].role == "system"
    assert body[0].content.endswith("Current local time: pinned")
    assert body[1].role == "assistant"
    assert body[1].content == tool_trace
    assert body[2].role == "user"
    assert body[2].content == observation
    assert [message.role for message in body] == ["system", "assistant", "user"]


def test_run_compact_then_continue_passes_both_reliable_and_cache_first_bodies():
    session = _session()
    runner = MagicMock()
    runner.config = MagicMock()
    runner.config.compact = CompactConfig(prompt_control_enabled=True, prompt_shape="cache_first_tail_with_fallback")
    runner.config.agent_prompts.compaction = _prompts()
    runner.llm_service = MagicMock()
    runner._replace_agent_memory = MagicMock()
    runner._compact_event_to_sse = AgentRunner._compact_event_to_sse
    runner._build_compact_body_messages = MethodType(AgentRunner._build_compact_body_messages, runner)
    runner._build_cache_first_compact_body_messages = MethodType(AgentRunner._build_cache_first_compact_body_messages, runner)

    captured = {}

    def capture_orchestrator(*args, **kwargs):
        async def fake_run(req):
            captured["body_messages"] = req.body_messages
            captured["cache_first_body_messages"] = req.cache_first_body_messages
            yield CompactEvent.started("sid", "auto", "agent")
            yield CompactEvent.completed("sid", "cleaned", metadata={"trigger": "auto", "mode": "agent"})
        inst = MagicMock()
        inst.run_compact = fake_run
        return inst

    with patch("backend.compact.orchestrator.CompactOrchestrator", side_effect=capture_orchestrator):
        asyncio.run(_drain(AgentRunner._run_compact_then_continue(runner, session, "auto")))

    assert captured["body_messages"][0].content.startswith("COMPACT SYSTEM")
    assert "ACTIVE SYSTEM" in captured["cache_first_body_messages"][0].content


def test_coordinator_2_profile_instructions_do_not_include_compact_control_clause():
    captured = {}
    cfg = SimpleNamespace(
        model=SimpleNamespace(model_name="qwen", model_type="text"),
        compact=CompactConfig(prompt_control_enabled=True),
        runtime_prompts=SimpleNamespace(enable_runtime_time_context=False),
        agent=SimpleNamespace(
            enabled_agent_profiles=["coordinator_2"],
            max_steps=3,
            python_executable="python",
        ),
        agent_prompts=SimpleNamespace(
            compaction=_prompts(),
            toolcalling_agent={},
            vlm_instructions=None,
            coordinator_2={"instructions": "COORDINATOR TWO"},
        ),
    )

    def fake_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(memory=SimpleNamespace(steps=[]), state={})

    with patch("backend.agent.agent_runner.get_tools_from_config", return_value=[]), patch(
        "backend.agent.agent_runner.SmartToolCallingAgent", side_effect=fake_agent
    ):
        runner = AgentRunner(llm_service=MagicMock(), config=cfg)
        runner._build_profile_agent("coordinator_2")

    assert captured["instructions"].startswith("COORDINATOR TWO")
    assert "[[SENTINEL]]" not in captured["instructions"]
    assert "INTERNAL COMPACT CONTROL RULE" not in captured["instructions"]
