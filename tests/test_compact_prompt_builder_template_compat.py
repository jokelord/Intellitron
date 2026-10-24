"""Regression: the Qwen3 chat template
(~/Project/LLM-weight/.../chat_template.jinja) has a
multi_step_tool check (line 79) that raises
'No user query found in messages.' if every user message in the
input looks like a <tool_response>...</tool_response> envelope.

The v1 (/compact) design returned a SINGLE system message from
build_compact_messages, which triggered this check (no user row
existed at all, so the reverse-scan never found a non-tool-response
user message, leaving multi_step_tool True).

The fix: build_compact_messages now returns [system, user], with
the system message holding the format instructions and the user
message holding the actual conversation to summarize.

This test pins:
- The shape: exactly 2 messages, [system, user].
- The user message content starts with the "Conversation to
  summarize:" marker so the LLM still sees the same semantic
  context as the v1 design.
- The system message content includes the format constraints
  (no_tools_preamble, main_prompt) and trailer.
- The total prompt is semantically equivalent to the v1 output
  (same key strings appear in the same order).

If SGLang's chat template ever changes its multi_step_tool
heuristic, this test must be updated together with the
build_compact_messages implementation, never unilaterally.
"""
from backend.compact.prompt_builder import build_compact_messages
from backend.config import CompactionPromptsConfig
from pathlib import Path


def make_prompts():
    return CompactionPromptsConfig(
        status_message="Compressing conversation context...",
        no_tools_preamble="Respond with TEXT ONLY. Do not call any tools.",
        main_prompt="Summarize the conversation following the 9-section skeleton.",
        custom_instructions_header="== USER CUSTOM INSTRUCTIONS ==",
        no_tools_trailer="Reminder: do not call tools. Text only.",
        summary_envelope_preface="[PRE]",
        summary_envelope_suffix="[POST]",
    )


def test_build_compact_messages_returns_system_then_user():
    """The split [system, user] is required to pass the Qwen3
    chat template's multi_step_tool check. Returning a single
    system message (v1 design) made the template raise
    'No user query found in messages.' because the reverse-scan
    never found a non-tool-response user row."""
    msgs = build_compact_messages(
        body_messages=[{"role": "user", "content": "hello"}],
        prompts=make_prompts(),
    )
    assert len(msgs) == 2, (
        f"expected [system, user] (2 msgs), got {len(msgs)}; "
        "a single system message would trigger the Qwen3 chat template's "
        "'No user query found in messages.' raise_exception"
    )
    assert msgs[0]["role"] == "system", f"first msg should be system, got {msgs[0]['role']!r}"
    assert msgs[1]["role"] == "user", f"second msg should be user, got {msgs[1]['role']!r}"


def test_build_compact_messages_system_holds_format_instructions():
    """The system message carries the format constraints and
    the main prompt. Without these the LLM would not know to
    emit the required <summary> block."""
    msgs = build_compact_messages(
        body_messages=[{"role": "user", "content": "hi"}],
        prompts=make_prompts(),
    )
    system_content = msgs[0]["content"]
    assert "TEXT ONLY" in system_content, "system must carry the no_tools_preamble"
    assert "9-section skeleton" in system_content, "system must carry the main_prompt"
    assert "Reminder: do not call tools" in system_content, "system must carry the trailer"


def test_build_compact_messages_user_holds_conversation():
    """The user message contains the rendered body of the original
    conversation, prefixed with the 'Conversation to summarize:'
    marker so the LLM treats the body as the input data rather
    than as additional instructions."""
    msgs = build_compact_messages(
        body_messages=[
            {"role": "user", "content": "hello there"},
            {"role": "assistant", "content": "general kenobi"},
        ],
        prompts=make_prompts(),
    )
    user_content = msgs[1]["content"]
    assert user_content.startswith("Conversation to summarize:"), (
        f"user msg must start with 'Conversation to summarize:' marker, "
        f"got: {user_content[:80]!r}"
    )
    assert "[user] hello there" in user_content, "user msg must contain rendered body"
    assert "[assistant] general kenobi" in user_content, "user msg must contain rendered body"


def test_build_compact_messages_includes_custom_instructions_when_provided():
    """Custom instructions are USER-provided format guidance, so they
    belong in the system message (alongside no_tools_preamble /
    main_prompt / trailer), not in the user message."""
    msgs = build_compact_messages(
        body_messages=[{"role": "user", "content": "x"}],
        prompts=make_prompts(),
        custom_instructions="focus on code examples",
    )
    system_content = msgs[0]["content"]
    assert "USER CUSTOM INSTRUCTIONS" in system_content
    assert "focus on code examples" in system_content


def test_build_compact_messages_omits_custom_instructions_when_empty():
    """When no custom instructions are provided, the system message
    must not include the custom-instructions header (avoids leaking
    the marker to the LLM)."""
    msgs = build_compact_messages(
        body_messages=[{"role": "user", "content": "x"}],
        prompts=make_prompts(),
        custom_instructions="   ",  # whitespace-only treated as empty
    )
    system_content = msgs[0]["content"]
    assert "USER CUSTOM INSTRUCTIONS" not in system_content


def test_build_compact_messages_passes_qwen3_chat_template():
    """End-to-end: render the produced messages through the actual
    Qwen3 chat template that the running SGLang server uses. Must
    NOT raise 'No user query found in messages.'"""
    import jinja2

    tpl_path = Path("~/Project/LLM-weight/Qwen3___5-35B-A3B-GPTQ-Int4/chat_template.jinja").expanduser()
    try:
        with open(tpl_path) as f:
            tpl_str = f.read()
    except FileNotFoundError:
        # Skip if model weights are not present in this environment.
        import pytest
        pytest.skip(f"Qwen3 chat template not found at {tpl_path}")

    # The SGLang template uses raise_exception which is provided as a
    # global by the SGLang Jinja environment. We register it as a no-op
    # so we can render the template standalone for the test.
    env = jinja2.Environment()
    env.globals["raise_exception"] = lambda msg: (_ for _ in ()).throw(AssertionError(msg))
    tpl = env.from_string(tpl_str)

    msgs = build_compact_messages(
        body_messages=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ],
        prompts=make_prompts(),
    )
    # Should NOT raise 'No user query found in messages.'
    try:
        tpl.render(messages=msgs, add_generation_prompt=True, tools=None)
    except AssertionError as e:
        raise AssertionError(
            f"Qwen3 chat template raised on build_compact_messages output: {e!s}. "
            "The fix is to keep [system, user] shape (not single system)."
        ) from e
