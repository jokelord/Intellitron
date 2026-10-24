from backend.compact.prompt_plan import (
    build_cache_first_tail_plan,
    build_reliable_position1_plan,
    build_reliable_source_with_compact_system,
)
from backend.config import CompactionPromptsConfig
from backend.llm_service import Message


def _prompts():
    return CompactionPromptsConfig(
        no_tools_preamble="PREAMBLE",
        main_prompt="MAIN",
        no_tools_trailer="TRAILER",
        compact_user_instruction="Summarize messages that follow.",
        custom_instructions_header="== USER CUSTOM INSTRUCTIONS ==",
        compact_control_user_instruction="[[SENTINEL]] summarize prior messages.",
    )


def test_reliable_source_replaces_system_and_preserves_history_tail():
    source = [Message(role="system", content="ACTIVE SYSTEM"), Message(role="user", content="Q1")]
    reliable = build_reliable_source_with_compact_system(source, _prompts())

    assert reliable[0].role == "system"
    assert "PREAMBLE" in reliable[0].content
    assert "MAIN" in reliable[0].content
    assert "TRAILER" in reliable[0].content
    assert "ACTIVE SYSTEM" not in reliable[0].content
    assert reliable[1].content == "Q1"


def test_reliable_source_handles_no_system_chat_history():
    source = [Message(role="user", content="Q1"), Message(role="assistant", content="A1")]
    reliable = build_reliable_source_with_compact_system(source, _prompts())

    assert reliable[0].role == "system"
    assert "PREAMBLE" in reliable[0].content
    assert [m.content for m in reliable[1:]] == ["Q1", "A1"]


def test_reliable_position1_plan_uses_current_builder_shape():
    source = build_reliable_source_with_compact_system(
        [Message(role="system", content="ACTIVE"), Message(role="user", content="Q1")],
        _prompts(),
    )
    plan = build_reliable_position1_plan(source, _prompts())

    assert plan.shape == "reliable_position1"
    assert plan.messages[0].role == "system"
    assert plan.messages[1].role == "user"
    assert "Summarize messages that follow" in plan.messages[1].content
    assert plan.messages[2].content == "Q1"


def test_cache_first_tail_plan_preserves_source_prefix_and_appends_internal_instruction_without_clause():
    source = [Message(role="system", content="ACTIVE WITHOUT CONTROL CLAUSE"), Message(role="user", content="Q1")]
    plan = build_cache_first_tail_plan(source, _prompts(), custom_instructions="Focus on files")

    assert plan.shape == "cache_first_tail"
    assert plan.messages[0] is source[0]
    assert plan.messages[0].content == "ACTIVE WITHOUT CONTROL CLAUSE"
    assert "compact" not in plan.messages[0].content.lower()
    assert plan.messages[1].content == "Q1"
    assert plan.messages[-1].role == "user"
    assert not plan.messages[-1].content.startswith("[[SENTINEL]]")
    assert "[[SENTINEL]]" not in plan.messages[-1].content
    assert "internal server/system instruction delivered through the user channel" in plan.messages[-1].content
    assert "not a real user request" in plan.messages[-1].content
    assert "Do not call tools" in plan.messages[-1].content
    assert "Do not route agents" in plan.messages[-1].content
    assert "Focus on files" in plan.messages[-1].content


def test_cache_first_custom_focus_keeps_source_messages_unchanged():
    source = [
        Message(role="system", content="SYS"),
        Message(role="user", content="old user"),
    ]

    plan = build_cache_first_tail_plan(
        source,
        _prompts(),
        custom_instructions="Focus on old user",
    )

    assert plan.messages[:-1] == source
    assert plan.messages[-1].role == "user"
    assert "Focus on old user" in plan.messages[-1].content
    assert "Focus on old user" not in str(plan.messages[:-1])
