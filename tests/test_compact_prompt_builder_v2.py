"""Tests for the v2 compact prompt builder (turn-shape, position-1 instruction).

These tests pin the contract from spec §5.2 + the position-1 amendment:
  - len(final) == len(body_messages) + 1
  - final[0] is byte-equal to body_messages[0] (system, preserved for prefix cache)
  - final[1] is a user message with content starting with the compact_user_instruction
  - final[2:] is byte-equal to body_messages[1:] (the original history, unchanged)
  - body_messages must be non-empty
  - body_messages[0].role must be "system"

The instruction is placed at position 1 (right after system) rather than at the
tail to maximize model compliance at long contexts (40k+ tokens). At 40k context
the model's effective attention at the tail is ~0 (lost-in-the-middle effect),
and the model drifts into the user's language (Chinese in our E2E test payload)
producing free-form Chinese markdown summaries instead of the YAML-specified
<analysis> + 9-section <summary> English format. Putting the instruction at the
beginning treats it as the user's "opening question" and the conversation that
follows becomes the "data to apply the format to".

Trade-off: v2 spec's original byte-equal ideal (instruction at tail) is no
longer strictly byte-equal — body_messages[1..] is unchanged but a new user
message is inserted at index 1. In practice SGLang does not share KV across
distinct requests, so byte-equal was already a no-op in production; we trade
purity for actual model compliance.
"""
from __future__ import annotations

import pytest

from backend.compact.prompt_builder import build_compact_messages_v2
from backend.config import CompactionPromptsConfig
from backend.llm_service import Message


@pytest.fixture
def prompts() -> CompactionPromptsConfig:
    return CompactionPromptsConfig(
        no_tools_preamble="NO_TOOLS_PREAMBLE",
        main_prompt="MAIN_PROMPT",
        custom_instructions_header="== USER CUSTOM INSTRUCTIONS ==",
        no_tools_trailer="REMINDER: text only",
        summary_envelope_preface="PREFACE: ",
        summary_envelope_suffix="\nSUFFIX",
        compact_user_instruction=(
            "Your task is to create a detailed summary of the conversation "
            "that follows this message, paying close attention to the user's "
            "explicit requests and your previous actions."
        ),
    )


def test_returns_n_plus_one_messages(prompts: CompactionPromptsConfig) -> None:
    body = [
        Message(role="system", content="SYS"),
        Message(role="user", content="A 说：xxx"),
        Message(role="assistant", content="回：yyy"),
        Message(role="user", content="B 说：zzz"),
    ]
    out = build_compact_messages_v2(body, prompts)
    assert len(out) == len(body) + 1


def test_system_message_preserved_byte_equal_at_index_zero(
    prompts: CompactionPromptsConfig,
) -> None:
    body = [
        Message(role="system", content="SYS"),
        Message(role="user", content="A 说：xxx"),
        Message(role="assistant", content="回：yyy"),
    ]
    out = build_compact_messages_v2(body, prompts)
    # The system message is the v2 prefix-cache anchor: it MUST be byte-equal
    # to body_messages[0] and remain at index 0.
    assert out[0].role == body[0].role == "system"
    assert out[0].content == body[0].content


def test_instruction_is_at_position_one_not_tail(
    prompts: CompactionPromptsConfig,
) -> None:
    body = [
        Message(role="system", content="SYS"),
        Message(role="user", content="A"),
        Message(role="assistant", content="B"),
    ]
    out = build_compact_messages_v2(body, prompts)
    # The instruction is at index 1, NOT at the tail.
    assert out[1].role == "user"
    assert "create a detailed summary" in out[1].content
    # The last element is the original last body message (B), not the instruction.
    assert out[-1].role == "assistant"
    assert out[-1].content == "B"


def test_history_after_instruction_is_byte_equal_to_body_tail(
    prompts: CompactionPromptsConfig,
) -> None:
    body = [
        Message(role="system", content="SYS"),
        Message(role="user", content="A 说：xxx"),
        Message(role="assistant", content="回：yyy"),
        Message(role="user", content="B 说：zzz"),
    ]
    out = build_compact_messages_v2(body, prompts)
    # final[2:] must be byte-equal to body_messages[1:].
    assert len(out) == len(body) + 1
    assert len(out[2:]) == len(body[1:])
    for i, original in enumerate(body[1:]):
        assert out[2 + i].role == original.role
        assert out[2 + i].content == original.content


def test_instruction_takes_precedence_over_tail_with_long_history(
    prompts: CompactionPromptsConfig,
) -> None:
    """Even with 60 messages in body, the instruction must be at index 1,
    not appended at index 60. This is the core regression test for the
    position-1 amendment (lost-in-the-middle fix)."""
    body = [Message(role="system", content="SYS")]
    for i in range(60):
        if i % 2 == 0:
            body.append(Message(role="user", content=f"u{i}"))
        else:
            body.append(Message(role="assistant", content=f"a{i}"))
    assert len(body) == 61

    out = build_compact_messages_v2(body, prompts)
    assert len(out) == 62
    assert out[1].role == "user"
    assert "create a detailed summary" in out[1].content
    # The last body message is the last assistant (a59); the instruction must
    # NOT have displaced it to a later position.
    assert out[-1].role == "assistant"
    assert out[-1].content == "a59"
    # The instruction must NOT be the last message.
    assert "create a detailed summary" not in (out[-1].content or "")


def test_final_message_is_original_last_body_message(
    prompts: CompactionPromptsConfig,
) -> None:
    """The trailing element of the final list is the LAST original body
    message, NOT the compact instruction. The instruction is at position 1."""
    body = [
        Message(role="system", content="SYS"),
        Message(role="user", content="turn1"),
        Message(role="assistant", content="turn2"),
    ]
    out = build_compact_messages_v2(body, prompts)
    assert out[-1].role == "assistant"
    assert out[-1].content == "turn2"
    assert "create a detailed summary" not in out[-1].content


def test_custom_instructions_appended_to_instruction_message(
    prompts: CompactionPromptsConfig,
) -> None:
    """Custom instructions live in the SAME user message as the main
    instruction (final[1]), not in a separate message at the tail."""
    body = [
        Message(role="system", content="SYS"),
        Message(role="user", content="A"),
    ]
    out = build_compact_messages_v2(
        body, prompts, custom_instructions="Focus on file paths"
    )
    assert out[1].role == "user"
    assert "create a detailed summary" in out[1].content
    assert "USER CUSTOM INSTRUCTIONS" in out[1].content
    assert "Focus on file paths" in out[1].content
    # No extra message at the tail.
    assert len(out) == 3


def test_custom_instructions_blank_is_ignored(prompts: CompactionPromptsConfig) -> None:
    body = [
        Message(role="system", content="SYS"),
        Message(role="user", content="A"),
    ]
    out = build_compact_messages_v2(body, prompts, custom_instructions="   \n  ")
    assert "USER CUSTOM INSTRUCTIONS" not in out[1].content


def test_empty_body_messages_raises(prompts: CompactionPromptsConfig) -> None:
    with pytest.raises(ValueError, match="body_messages 不能为空"):
        build_compact_messages_v2([], prompts)


def test_first_message_must_be_system(prompts: CompactionPromptsConfig) -> None:
    with pytest.raises(ValueError, match="第一条必须是 system"):
        build_compact_messages_v2(
            [Message(role="user", content="hello")], prompts
        )


def test_language_lock_wording_present_in_instruction_content(
    prompts: CompactionPromptsConfig,
) -> None:
    """Guard against future YAML refactors that drop the LANGUAGE LOCK
    marker. Real E2E: at 40k context the model drifts into the user's
    language (Chinese), and the lock is what keeps the output in English."""
    # Read the real production prompt from the YAML to make sure the lock
    # markers are present in the canonical source of truth.
    import yaml
    from pathlib import Path

    yaml_path = (
        Path(__file__).resolve().parents[1] / "config" / "config-agent-prompts.yaml"
    )
    with open(yaml_path) as f:
        cfg = yaml.safe_load(f).get("compaction", {})
    compact_user_instruction = cfg.get("compact_user_instruction", "")
    assert "LANGUAGE LOCK" in compact_user_instruction, (
        "compact_user_instruction YAML must contain a LANGUAGE LOCK marker "
        "to keep the summary in English regardless of the conversation language. "
        "See AGENTS.md trap #27 (introduced 2026-06-11, position-1 amendment)."
    )
    assert "English" in compact_user_instruction, (
        "compact_user_instruction YAML must explicitly mention English."
    )


def test_structure_lock_contains_all_nine_section_names(
    prompts: CompactionPromptsConfig,
) -> None:
    """Guard against future YAML refactors that drop one of the 9 mandatory
    summary section names. The model drifted to free-form Chinese headers
    ('### 对话内容总结') at 40k context; the STRUCTURE LOCK is the
    defense-in-depth."""
    import yaml
    from pathlib import Path

    yaml_path = (
        Path(__file__).resolve().parents[1] / "config" / "config-agent-prompts.yaml"
    )
    with open(yaml_path) as f:
        cfg = yaml.safe_load(f).get("compaction", {})
    compact_user_instruction = cfg.get("compact_user_instruction", "")
    required_sections = (
        "Primary Request and Intent",
        "Key Technical Concepts",
        "Files and Code Sections",
        "Errors and fixes",
        "Problem Solving",
        "All user messages",
        "Pending Tasks",
        "Current Work",
        "Optional Next Step",
    )
    for section in required_sections:
        assert section in compact_user_instruction, (
            f"compact_user_instruction YAML must mention section {section!r}. "
            f"See AGENTS.md trap #27 (introduced 2026-06-11, position-1 amendment)."
        )
    assert "STRUCTURE LOCK" in compact_user_instruction
    assert "<analysis>" not in compact_user_instruction
    assert "<summary>" in compact_user_instruction
