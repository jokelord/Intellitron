"""Tests for filter_messages_for_llm (chat mode compact marker interpretation)."""
from types import SimpleNamespace

import pytest

from backend.compact.chat_message_filter import filter_messages_for_llm
from backend.config import CompactionPromptsConfig


def make_msg(role, content, **meta):
    m = SimpleNamespace(role=role, content=content)
    m.metadata = meta if meta else None
    return m


def make_prompts():
    return CompactionPromptsConfig(
        status_message="",
        no_tools_preamble="",
        main_prompt="",
        custom_instructions_header="",
        no_tools_trailer="",
        summary_envelope_preface="[PRE]",
        summary_envelope_suffix="[POST]",
    )


def test_passthrough_when_no_markers():
    msgs = [make_msg("user", "hi"), make_msg("assistant", "hello")]
    out = filter_messages_for_llm(msgs, make_prompts())
    assert len(out) == 2
    assert out[0].content == "hi"
    assert out[1].content == "hello"


def test_drops_pre_compact_messages_when_no_summary():
    """Orphan pre_compact markers are dropped as graceful degradation."""
    msgs = [
        make_msg("user", "old1", pre_compact=True),
        make_msg("user", "current"),
    ]
    out = filter_messages_for_llm(msgs, make_prompts())
    assert len(out) == 1
    assert out[0].content == "current"


def test_replaces_compact_summary_with_envelope():
    msgs = [
        make_msg("user", "old1", pre_compact=True),
        make_msg("user", "Summary text", compact_summary=True, summary_id="compact-x"),
        make_msg("user", "new question"),
    ]
    out = filter_messages_for_llm(msgs, make_prompts())
    assert len(out) == 2
    assert out[0].role == "user"
    assert "[PRE]" in out[0].content
    assert "Summary text" in out[0].content
    assert "[POST]" in out[0].content
    assert out[1].content == "new question"


def test_uses_latest_compact_summary_only():
    msgs = [
        make_msg("user", "old1", pre_compact=True),
        make_msg("user", "First summary", compact_summary=True, summary_id="a"),
        make_msg("user", "more old", pre_compact=True),
        make_msg("user", "Second summary", compact_summary=True, summary_id="b"),
        make_msg("user", "now"),
    ]
    out = filter_messages_for_llm(msgs, make_prompts())
    assert len(out) == 2
    assert "First summary" not in out[0].content
    assert "Second summary" in out[0].content
    assert out[1].content == "now"


def test_drops_pre_compact_after_summary_defensively():
    """pre_compact after a summary shouldn't normally appear, but if it does, drop it."""
    msgs = [
        make_msg("user", "Summary", compact_summary=True, summary_id="x"),
        make_msg("user", "should not be here", pre_compact=True),
        make_msg("user", "now"),
    ]
    out = filter_messages_for_llm(msgs, make_prompts())
    assert len(out) == 2
    assert "should not be here" not in [m.content for m in out]
    assert out[-1].content == "now"


def test_empty_input_returns_empty():
    assert filter_messages_for_llm([], make_prompts()) == []


def test_chatmessage_metadata_field_accepted():
    """api/chat.py ChatMessage now accepts an optional metadata dict."""
    from backend.api.chat import ChatMessage
    msg = ChatMessage(role="user", content="hi", metadata={"pre_compact": True})
    assert msg.metadata == {"pre_compact": True}
    msg2 = ChatMessage(role="user", content="hi")
    assert msg2.metadata is None
