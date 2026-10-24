"""Tests for build_compact_messages prompt builder.

History: the v1 design returned a SINGLE system message. That shape
broke against the Qwen3 chat template's multi_step_tool check
(chat_template.jinja line 79) which raises 'No user query found in
messages.' if the input has no user row. The fix splits the prompt
into [system: format instructions, user: actual conversation].

These tests pin the v2 contract: exactly 2 messages, system first,
user second, with each side carrying the right content.
"""
from backend.compact.prompt_builder import build_compact_messages
from backend.config import CompactionPromptsConfig


def make_prompts():
    return CompactionPromptsConfig(
        status_message="",
        no_tools_preamble="NO_TOOLS_PREAMBLE",
        main_prompt="MAIN_PROMPT",
        custom_instructions_header="USER_INSTR_HEADER",
        no_tools_trailer="NO_TOOLS_TRAILER",
        summary_envelope_preface="",
        summary_envelope_suffix="",
    )


def test_returns_system_then_user():
    body = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "World"},
    ]
    out = build_compact_messages(body, prompts=make_prompts(), custom_instructions=None)
    assert len(out) == 2
    assert out[0]["role"] == "system"
    assert out[1]["role"] == "user"


def test_system_message_holds_format_instructions():
    body = [{"role": "user", "content": "x"}]
    out = build_compact_messages(body, prompts=make_prompts(), custom_instructions=None)
    system_content = out[0]["content"]
    assert "NO_TOOLS_PREAMBLE" in system_content
    assert "MAIN_PROMPT" in system_content
    assert "NO_TOOLS_TRAILER" in system_content


def test_user_message_holds_conversation_in_order():
    body = [
        {"role": "user", "content": "alpha-msg"},
        {"role": "assistant", "content": "beta-msg"},
        {"role": "user", "content": "gamma-msg"},
    ]
    out = build_compact_messages(body, prompts=make_prompts(), custom_instructions=None)
    user_content = out[1]["content"]
    assert user_content.startswith("Conversation to summarize:")
    # Use unique strings so the order check is unambiguous.
    a_pos = user_content.index("alpha-msg")
    b_pos = user_content.index("beta-msg")
    c_pos = user_content.index("gamma-msg")
    assert a_pos < b_pos < c_pos, (
        f"order not preserved: alpha at {a_pos}, beta at {b_pos}, gamma at {c_pos}"
    )


def test_custom_instructions_included_when_provided():
    body = [{"role": "user", "content": "x"}]
    out = build_compact_messages(body, prompts=make_prompts(), custom_instructions="focus on code")
    assert "USER_INSTR_HEADER" in out[0]["content"]
    assert "focus on code" in out[0]["content"]


def test_empty_custom_instructions_omitted():
    body = [{"role": "user", "content": "x"}]
    out = build_compact_messages(body, prompts=make_prompts(), custom_instructions="   ")
    assert "USER_INSTR_HEADER" not in out[0]["content"]


def test_handles_object_messages_with_role_and_content():
    from types import SimpleNamespace

    body = [SimpleNamespace(role="user", content="hello")]
    out = build_compact_messages(body, prompts=make_prompts(), custom_instructions=None)
    # Body is now in the user message (not system)
    assert "hello" in out[1]["content"]


def test_handles_multimodal_content_list():
    body = [{"role": "user", "content": [{"type": "text", "text": "see image"}]}]
    out = build_compact_messages(body, prompts=make_prompts(), custom_instructions=None)
    # Multimodal text parts are rendered into the user message
    assert "see image" in out[1]["content"]
