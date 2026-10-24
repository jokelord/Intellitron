"""Tests for chat-mode auto-compact using the v2 turn-shape path.

Per the 2026-06-13 chat auto-compact ordering fix:
  - The auto-compact branch in _generate_stream_response must pass the previous
    history as CompactRequest.body_messages, excluding the final current user turn.
  - The auto-compact rebuild of final_messages is unchanged:
      [Message(role="user", content=envelope), current_user]
  - When no auto-compact is triggered, the behavior is unchanged (regression).
"""
from __future__ import annotations

import asyncio
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from backend.api.chat import ChatMessage
from backend.compact.types import CompactEvent, CompactEventKind
from backend.llm_service import Message


def _collect_async_gen(agen):
    """Drain an async generator into a list."""
    async def _drain():
        out = []
        async for x in agen:
            out.append(x)
        return out
    return asyncio.run(_drain())


async def _fake_post_compact_stream(messages, **kwargs):
    """Tiny stream used by the post-compact chat LLM call."""
    yield {"content": "ok"}


def _expected_chat_messages() -> List[ChatMessage]:
    """The chat request shape: prior history plus a final current user turn."""
    return [
        ChatMessage(role="system", content="SYS from chat"),
        ChatMessage(role="user", content="Q1"),
        ChatMessage(role="assistant", content="A1"),
        ChatMessage(role="user", content="Q2"),
    ]


def test_chat_auto_compact_excludes_current_user_from_compact_body() -> None:
    """Chat auto-compact summarizes prior history only.

    The current user turn is bound to the new compacted context after the
    summary is produced, but it must not be part of the compact LLM input.
    """
    from backend.api.chat import _generate_stream_response

    chat_messages = _expected_chat_messages()

    # Mock the raw_request that holds app.state.compact_orchestrator
    raw_request = MagicMock()
    raw_request.app.state.compact_orchestrator = MagicMock()

    # The orchestrator's run_compact pins the v2 contract: it captures
    # the CompactRequest fields so the test can assert reliable body_messages
    # use compact system fallback while still excluding the current user.
    captured_request = {}

    async def _fake_run(req):
        captured_request["body_messages"] = req.body_messages
        captured_request["cache_first_body_messages"] = req.cache_first_body_messages
        captured_request["messages"] = req.messages
        yield CompactEvent.started(summary_message_id="sid", trigger="auto", mode="chat")
        yield CompactEvent.completed("sid", "cleaned", metadata={"trigger": "auto", "mode": "chat"})

    raw_request.app.state.compact_orchestrator.run_compact = _fake_run

    # Build a service mock with chat_stream_async that returns a tiny stream.
    captured_final = {}

    async def _capture_post_compact_stream(messages, **kwargs):
        captured_final["messages"] = list(messages)
        yield {"content": "ok"}

    service = MagicMock()
    service.chat_stream_async = _capture_post_compact_stream

    # Mock _should_auto_compact_chat to return True so the auto-compact branch fires.
    with patch("backend.api.chat._should_auto_compact_chat", return_value=True):
        sse_chunks = _collect_async_gen(_generate_stream_response(
            service=service,
            messages=[Message(role=m.role, content=m.content) for m in chat_messages],
            model="qwen3-next",
            max_tokens=None,
            temperature=None,
            top_p=None,
            tools=None,
            request_id=None,
            raw_request=raw_request,
            original_chat_messages=chat_messages,
        ))

    # Invariant: CompactRequest.body_messages is the reliable compact path:
    # compact-specific system prompt plus previous history tail, excluding
    # the final current user turn.
    assert captured_request.get("body_messages") is not None, (
        "CompactRequest.body_messages should be set to the prior history in chat mode v2 path"
    )
    history_messages = chat_messages[:-1]
    assert len(captured_request["body_messages"]) == len(history_messages)
    assert captured_request["body_messages"][0].role == "system"
    assert captured_request["body_messages"][0].content != history_messages[0].content
    for i, m in enumerate(history_messages[1:], start=1):
        assert captured_request["body_messages"][i].role == m.role
        assert captured_request["body_messages"][i].content == m.content
    assert all(m.content != "Q2" for m in captured_request["body_messages"]), (
        "current user turn must not be summarized by chat auto-compact"
    )
    # Invariant: CompactRequest.messages is NOT set (the deprecated v1 field)
    assert captured_request.get("messages") is None, (
        "CompactRequest.messages should NOT be set in v2 path (deprecated)"
    )
    # The post-compact chat call receives the new context: summary envelope + current user.
    final_messages = captured_final["messages"]
    assert len(final_messages) == 2
    assert final_messages[0].role == "user"
    assert "cleaned" in final_messages[0].content
    assert final_messages[1].role == "user"
    assert final_messages[1].content == "Q2"


def test_chat_no_auto_compact_unchanged_behavior() -> None:
    """When _should_auto_compact_chat returns False, the request should NOT call
    compact_orchestrator.run_compact at all (regression guard for the v1→v2 refactor)."""
    from backend.api.chat import _generate_stream_response

    chat_messages = [
        ChatMessage(role="system", content="SYS"),
        ChatMessage(role="user", content="Q1"),
    ]

    raw_request = MagicMock()
    raw_request.app.state.compact_orchestrator = MagicMock()

    service = MagicMock()
    service.chat_stream_async = _fake_post_compact_stream

    with patch("backend.api.chat._should_auto_compact_chat", return_value=False):
        _collect_async_gen(_generate_stream_response(
            service=service,
            messages=[Message(role=m.role, content=m.content) for m in chat_messages],
            model="qwen3-next",
            max_tokens=None,
            temperature=None,
            top_p=None,
            tools=None,
            request_id=None,
            raw_request=raw_request,
            original_chat_messages=chat_messages,
        ))

    # Invariant: compact_orchestrator.run_compact was NOT called
    raw_request.app.state.compact_orchestrator.run_compact.assert_not_called()
