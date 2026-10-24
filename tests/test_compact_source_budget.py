import pytest

from backend.compact.source_budget import trim_messages_to_token_budget
from backend.config import CompactionPromptsConfig
from backend.llm_service import Message


class CharCounter:
    def count_messages(self, messages, image_count: int = 0) -> int:
        return sum(len(str(getattr(message, "content", ""))) for message in messages)


def test_trim_messages_to_token_budget_keeps_system_summary_and_recent_tail():
    prompts = CompactionPromptsConfig(
        summary_envelope_preface="This session is being continued",
    )
    messages = [
        Message(role="system", content="SYS"),
        Message(role="user", content="This session is being continued: prior compact summary"),
        Message(role="user", content="old user message that should be dropped"),
        Message(role="assistant", content="old assistant message that should be dropped"),
        Message(role="user", content="recent user"),
        Message(role="assistant", content="recent assistant"),
    ]

    trimmed = trim_messages_to_token_budget(
        messages,
        max_tokens=90,
        prompts=prompts,
        counter=CharCounter(),
    )

    assert [m.content for m in trimmed] == [
        "SYS",
        "This session is being continued: prior compact summary",
        "recent user",
        "recent assistant",
    ]


def test_trim_messages_to_token_budget_noops_when_under_budget():
    messages = [Message(role="system", content="SYS"), Message(role="user", content="short")]

    trimmed = trim_messages_to_token_budget(
        messages,
        max_tokens=100,
        prompts=CompactionPromptsConfig(),
        counter=CharCounter(),
    )

    assert trimmed == messages


def test_trim_messages_to_token_budget_omits_single_tail_message_that_exceeds_budget():
    messages = [Message(role="system", content="SYS"), Message(role="user", content="x" * 80)]

    trimmed = trim_messages_to_token_budget(
        messages,
        max_tokens=20,
        prompts=CompactionPromptsConfig(),
        counter=CharCounter(),
    )

    assert [m.content for m in trimmed] == ["SYS"]
    assert CharCounter().count_messages(trimmed) <= 20


def test_trim_messages_to_token_budget_does_not_keep_stale_tail_when_newest_message_exceeds_budget():
    messages = [
        Message(role="system", content="SYS"),
        Message(role="user", content="old small message"),
        Message(role="assistant", content="x" * 80),
    ]

    trimmed = trim_messages_to_token_budget(
        messages,
        max_tokens=25,
        prompts=CompactionPromptsConfig(),
        counter=CharCounter(),
    )

    assert [m.content for m in trimmed] == ["SYS"]


def test_trim_messages_to_token_budget_omits_everything_when_no_message_fits():
    messages = [Message(role="user", content="x" * 80)]

    trimmed = trim_messages_to_token_budget(
        messages,
        max_tokens=20,
        prompts=CompactionPromptsConfig(),
        counter=CharCounter(),
    )

    assert trimmed == []


def test_trim_messages_to_token_budget_keeps_summary_when_system_exceeds_budget():
    prompts = CompactionPromptsConfig(
        summary_envelope_preface="This session is being continued",
    )
    messages = [
        Message(role="system", content="S" * 80),
        Message(role="user", content="This session is being continued: compact summary"),
    ]

    trimmed = trim_messages_to_token_budget(
        messages,
        max_tokens=50,
        prompts=prompts,
        counter=CharCounter(),
    )

    assert [m.content for m in trimmed] == ["This session is being continued: compact summary"]


def test_trim_messages_to_token_budget_keeps_tool_observation_marker_when_observation_too_large():
    tool_trace = '''<<<TOOL_CALL>>> read_file
{
  "file_path": "/tmp/a"
}
<<<END_TOOL_CALL>>>'''
    messages = [
        Message(role="system", content="SYS"),
        Message(role="assistant", content=tool_trace),
        Message(role="user", content="Observation:\n" + "x" * 120),
        Message(role="assistant", content="Final answer summary"),
    ]

    trimmed = trim_messages_to_token_budget(
        messages,
        max_tokens=145,
        prompts=CompactionPromptsConfig(),
        counter=CharCounter(),
    )

    assert [m.role for m in trimmed] == ["system", "assistant", "user", "assistant"]
    assert trimmed[1].content == tool_trace
    assert trimmed[2].content.startswith("Observation omitted due compact source budget")
    assert trimmed[3].content == "Final answer summary"
    assert CharCounter().count_messages(trimmed) <= 145


def test_trim_messages_to_token_budget_inserts_tool_observation_marker_before_next_user_message():
    tool_trace = '''<<<TOOL_CALL>>> read_file
{
  "file_path": "/tmp/a"
}
<<<END_TOOL_CALL>>>'''
    messages = [
        Message(role="system", content="SYS"),
        Message(role="assistant", content=tool_trace),
        Message(role="user", content="Observation:\n" + "x" * 120),
        Message(role="user", content="next user"),
    ]

    trimmed = trim_messages_to_token_budget(
        messages,
        max_tokens=135,
        prompts=CompactionPromptsConfig(),
        counter=CharCounter(),
    )

    assert [m.role for m in trimmed] == ["system", "assistant", "user", "user"]
    assert trimmed[2].content.startswith("Observation omitted due compact source budget")
    assert trimmed[3].content == "next user"


def test_trim_messages_to_token_budget_drops_orphan_observation_when_tool_call_is_not_retained():
    messages = [
        Message(role="system", content="SYS"),
        Message(role="assistant", content="<<<TOOL_CALL>>> read_file\n" + "x" * 80),
        Message(role="user", content="Observation:\nsmall"),
        Message(role="assistant", content="Final answer summary"),
    ]

    trimmed = trim_messages_to_token_budget(
        messages,
        max_tokens=45,
        prompts=CompactionPromptsConfig(),
        counter=CharCounter(),
    )

    assert [m.content for m in trimmed] == ["SYS", "Final answer summary"]


def test_historical_legacy_trace_is_classified_only_for_observation_pairing():
    legacy_trace = 'Calling tools:\n[{"id":"immutable-history"}]'
    messages = [
        Message(role="system", content="SYS"),
        Message(role="assistant", content=legacy_trace),
        Message(role="user", content="Observation:\n" + "x" * 120),
        Message(role="assistant", content="Final answer summary"),
    ]

    trimmed = trim_messages_to_token_budget(
        messages,
        max_tokens=125,
        prompts=CompactionPromptsConfig(),
        counter=CharCounter(),
    )

    assert trimmed[1].role == "assistant"
    assert trimmed[1].content == legacy_trace
    assert trimmed[2].role == "user"
    assert trimmed[2].content == "Observation omitted due compact source budget."


@pytest.mark.parametrize(
    "tool_trace",
    [
        '<<TOOL_CALL>>\n{"name":"read_file","arguments":{}}\n<<END_TOOL_CALL>>',
        '<<<TOOL_CALL>>> read_file\n{}\n<<<END_TOOL_CALL>>',
    ],
)
def test_current_and_retired_traces_are_read_only_pairing_markers(tool_trace):
    messages = [
        Message(role="system", content="SYS"),
        Message(role="assistant", content=tool_trace),
        Message(role="user", content="Observation:\n" + "x" * 120),
        Message(role="assistant", content="Final answer summary"),
    ]

    trimmed = trim_messages_to_token_budget(
        messages,
        max_tokens=150,
        prompts=CompactionPromptsConfig(),
        counter=CharCounter(),
    )

    assert trimmed[1].content == tool_trace
    assert trimmed[2].content == "Observation omitted due compact source budget."
