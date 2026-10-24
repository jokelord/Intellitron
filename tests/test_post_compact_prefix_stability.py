"""Post-compact calls retain the normal Agent prefix and tool schema."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from backend.agent.agent_runner import AgentRunner
from backend.agent.local_llm_model import LocalLLMModel
from smolagents.models import ChatMessage, MessageRole


def _service():
    service = MagicMock()
    service.chat.return_value = SimpleNamespace(text="ok", usage=None, tool_calls=None)
    return service


def _tool(name="search"):
    return SimpleNamespace(
        name=name,
        description="Search documents",
        inputs={"query": {"type": "string", "description": "Search query"}},
        output_type="string",
    )


def _text(role, text):
    return ChatMessage(role=role, content=[{"type": "text", "text": text}])


def test_first_and_later_post_compact_calls_keep_stable_prefix_and_schema():
    service = _service()
    session = SimpleNamespace(
        current_agent_name="basic_agent",
        last_llm_prompt_snapshot=None,
        last_compactable_llm_messages=None,
        last_compactable_agent_name=None,
    )
    model = LocalLLMModel(
        llm_service=service,
        runtime_context="PINNED RUNTIME",
        session=session,
    )
    tool = _tool()
    compact_envelope = _text(MessageRole.USER, "<summary>state</summary>")
    first_tail = _text(MessageRole.USER, "first post-compact turn")
    later_tail = _text(MessageRole.USER, "later turn")

    model.generate(
        [ChatMessage(role=MessageRole.SYSTEM, content="PROFILE"), compact_envelope, first_tail],
        tools_to_call_from=[tool],
    )
    first = service.chat.call_args.kwargs
    model.generate(
        [
            ChatMessage(role=MessageRole.SYSTEM, content="PROFILE"),
            compact_envelope,
            first_tail,
            _text(MessageRole.ASSISTANT, "first answer"),
            later_tail,
        ],
        tools_to_call_from=[tool],
    )
    later = service.chat.call_args.kwargs

    assert first["messages"][0].content == "PROFILE\n\nPINNED RUNTIME"
    assert later["messages"][: len(first["messages"])] == first["messages"]
    assert first["tools"] == later["tools"]
    assert first["tools"][0]["function"]["name"] == "search"
    assert "slim" not in first
    assert "slim" not in later


def test_replace_agent_memory_keeps_summary_envelope_and_has_run_without_transition_flag():
    runner = AgentRunner.__new__(AgentRunner)
    runner._session_disposal_tasks = {}
    session = MagicMock()
    session.compaction_archive = []
    session.has_run = False
    session.current_agent_name = "old_agent"
    session.active_agent = MagicMock()
    session.agent = MagicMock()
    session.agent.memory.steps = []
    session.last_compactable_llm_messages = []
    session.last_compactable_agent_name = "old_agent"
    del session.first_post_compact_turn

    runner.config = MagicMock()
    runner.config.agent_prompts.compaction.summary_envelope_preface = "PREFACE: "
    runner.config.agent_prompts.compaction.summary_envelope_suffix = " :SUFFIX"
    runner.config.agent.entry_agent_name = "coordinator_1"

    runner._replace_agent_memory(session, "summary text", "summary-id-1")

    assert session.has_run is True
    assert session.agent.memory.steps[0].task == "PREFACE: summary text :SUFFIX"
    assert "first_post_compact_turn" not in session.__dict__
