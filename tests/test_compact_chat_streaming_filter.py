"""Regression: post-compact chat requests must be filtered before reaching the LLM.

Bug (2026-06-06): the user reported that /compact dropped context to ~3k
tokens during the compact LLM call, but the NEXT user message ballooned
back to ~40k tokens (the original size). Two bugs combined:

1. The frontend's `truncateMessagesForContext` (and the page-level
   handleSend glue) only copied `role`+`content` to the wire format,
   silently dropping `isPreCompact: true` and `bubbleKind: 'compact_summary'`.
   So the backend never received `metadata.pre_compact` /
   `metadata.compact_summary`. The filter had no markers to act on.

2. Even if the frontend had sent the markers, the backend's
   `_generate_stream_response` default `final_messages = messages` used
   the unfiltered list. The filter result was computed but only used
   inside the auto-compact branch. Defense-in-depth: the filter must
   be the default for the LLM input, not the auto-compact-branch-only
   case.

These tests pin the contract:

A. Frontend sends `metadata.pre_compact: true` -> backend filter drops
   the message -> LLM input does NOT contain it.
B. Frontend sends `metadata.compact_summary: true` -> backend filter
   replaces the summary with envelope -> LLM input contains envelope
   + post-summary messages, no other history.
    C. (Defense-in-depth) Frontend sends messages WITHOUT metadata (the
       pre-fix broken path) -> backend compact-boundary filter is a no-op.
       Normal chat/response prompts must not receive compact-control system
       clauses; compact uses a self-contained final user instruction only.

Each test monkey-patches `LLMService.chat` to capture the exact
messages the LLM would have received, so we can assert the filter
actually ran.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")


def _collect_sse_events(body_bytes: bytes):
    events = []
    for chunk in body_bytes.split(b"\n\n"):
        chunk = chunk.strip()
        if not chunk.startswith(b"data: "):
            continue
        payload = chunk[len(b"data: "):].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            events.append(json.loads(payload))
        except Exception:
            pass
    return events


@pytest.fixture
def _restore_config():
    from backend.config import get_config
    cfg = get_config()
    saved = {
        "compact.enabled": cfg.compact.enabled,
        "compact.auto_trigger": cfg.compact.auto_trigger,
        "compact.strategy": cfg.compact.strategy,
        "compact.performance_ratio": cfg.compact.performance_ratio,
        "compact.prompt_control_enabled": cfg.compact.prompt_control_enabled,
        "sglang.max_context_length": cfg.sglang.max_context_length,
        "model.max_context_length": cfg.model.max_context_length,
    }
    yield
    for k, v in saved.items():
        section, field = k.split(".", 1)
        if section == "compact":
            setattr(cfg.compact, field, v)


def _spy_on_llm_chat(monkeypatch, captured: dict):
    """Replace LLMService.chat with a spy that captures the messages
    the LLM would have received, then returns a tiny generator.
    """
    from backend.llm_service import LLMService

    orig_chat = LLMService.chat

    def spy_chat(self, *args, messages=None, **kwargs):
        # Capture the messages regardless of whether this is a chat
        # call or a compact internal call. The first call (chat) is
        # what we care about.
        captured.setdefault("llm_messages", []).append(
            [{"role": m.role, "content": str(m.content)[:200]} for m in (messages or [])]
        )
        captured.setdefault("chat_kwargs", []).append(dict(kwargs))

        if messages and len(messages) <= 4:
            def gen():
                yield {"content": "OK", "reasoning": None}
            return gen()
        return orig_chat(self, *args, messages=messages, **kwargs)

    monkeypatch.setattr(LLMService, "chat", spy_chat)


async def test_chat_completions_streams_forwards_enable_thinking_extra_body(
    monkeypatch, _restore_config
):
    """Normal chat turns may opt into model reasoning via enable_thinking."""
    from backend.config import get_config
    cfg = get_config()
    cfg.compact.enabled = True
    cfg.compact.auto_trigger = False

    captured: dict = {}
    _spy_on_llm_chat(monkeypatch, captured)

    from httpx import ASGITransport, AsyncClient
    from backend.main import app

    request_body = {
        "model": "qwen3-next",
        "messages": [{"role": "user", "content": "think deeply"}],
        "stream": True,
        "max_tokens": 50,
        "enable_thinking": True,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("POST", "/v1/chat/completions", json=request_body) as resp:
            assert resp.status_code == 200
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if b"[DONE]" in body:
                    break

    assert captured["chat_kwargs"][0]["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": True}
    }


def _spy_on_llm_chat_completion(monkeypatch, captured: dict):
    """Capture LLM input for non-streaming chat calls."""
    from backend.llm_service import GenerationResult, LLMService

    def spy_chat(self, *args, messages=None, **kwargs):
        captured.setdefault("llm_messages", []).append(
            [{"role": m.role, "content": str(m.content)} for m in (messages or [])]
        )
        if kwargs.get("stream"):
            def gen():
                yield {"content": "OK", "reasoning": None}
            return gen()
        return GenerationResult(text="OK", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})

    monkeypatch.setattr(LLMService, "chat", spy_chat)


async def test_chat_completions_streams_drops_pre_compact_messages(
    monkeypatch, _restore_config
):
    """The streaming chat endpoint must drop messages marked
    metadata.pre_compact: true before they reach the LLM."""
    from backend.config import get_config
    cfg = get_config()
    # Disable auto-compact so we test the manual-compact path
    # (where the user already compact-ed and is now sending a new
    # message; auto-compact should NOT fire because tokens are low).
    cfg.compact.enabled = True
    cfg.compact.auto_trigger = False

    captured: dict = {}
    _spy_on_llm_chat(monkeypatch, captured)

    from httpx import ASGITransport, AsyncClient
    from backend.main import app

    request_body = {
        "model": "qwen3-next",
        "messages": [
            {"role": "user", "content": "old user 1", "metadata": {"pre_compact": True}},
            {"role": "assistant", "content": "old reply 1", "metadata": {"pre_compact": True}},
            {"role": "user", "content": "old user 2", "metadata": {"pre_compact": True}},
            {"role": "user", "content": "current question"},
        ],
        "stream": True,
        "max_tokens": 50,
        "temperature": 0.7,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("POST", "/v1/chat/completions", json=request_body) as resp:
            assert resp.status_code == 200
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if b"[DONE]" in body:
                    break

    # The first captured call is the chat LLM call.
    assert "llm_messages" in captured, "LLM service was not called"
    llm_input = captured["llm_messages"][0]
    # Only the current question should reach the LLM; normal chat must not
    # inject an internal compact-control system clause.
    assert len(llm_input) == 1, (
        f"pre_compact messages were NOT filtered out. "
        f"LLM received: {llm_input!r}"
    )
    assert llm_input[0]["role"] == "user"
    assert llm_input[0]["content"] == "current question"
    assert "INTERNAL COMPACT CONTROL RULE" not in str(llm_input)


async def test_chat_completions_streams_replaces_compact_summary_with_envelope(
    monkeypatch, _restore_config
):
    """The streaming chat endpoint must replace compact_summary
    messages with the envelope and drop the pre-summary history."""
    from backend.config import get_config
    cfg = get_config()
    cfg.compact.enabled = True
    cfg.compact.auto_trigger = False

    captured: dict = {}
    _spy_on_llm_chat(monkeypatch, captured)

    from httpx import ASGITransport, AsyncClient
    from backend.main import app

    request_body = {
        "model": "qwen3-next",
        "messages": [
            {"role": "user", "content": "old user 1", "metadata": {"pre_compact": True}},
            {"role": "assistant", "content": "old reply 1", "metadata": {"pre_compact": True}},
            {
                "role": "system",
                "content": "Prior conversation summarized.",
                "metadata": {"compact_summary": True, "summary_id": "compact-x"},
            },
            {"role": "user", "content": "new question after compact"},
        ],
        "stream": True,
        "max_tokens": 50,
        "temperature": 0.7,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("POST", "/v1/chat/completions", json=request_body) as resp:
            assert resp.status_code == 200
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if b"[DONE]" in body:
                    break

    assert "llm_messages" in captured
    llm_input = captured["llm_messages"][0]
    # Should be 2 messages: envelope + new question. Normal chat must not
    # inject an internal compact-control system clause.
    assert len(llm_input) == 2, (
        f"filter did not replace compact_summary + drop pre_summary. "
        f"LLM received: {llm_input!r}"
    )
    assert "Prior conversation summarized." in llm_input[0]["content"], (
        f"first message should be envelope containing the summary, got: {llm_input[0]!r}"
    )
    assert "old user 1" not in str(llm_input), (
        f"pre_summary messages must be dropped, but found in: {llm_input!r}"
    )
    assert "old reply 1" not in str(llm_input)
    assert llm_input[1]["content"] == "new question after compact"
    assert "INTERNAL COMPACT CONTROL RULE" not in str(llm_input)


async def test_chat_completions_streams_no_op_when_no_metadata(
    monkeypatch, _restore_config
):
    """Defense-in-depth: if frontend sends messages WITHOUT metadata
    (e.g. external OpenAI client, or the broken pre-fix frontend),
    the filter must not crash. It is a graceful no-op and the LLM
    sees the messages as-is.
    """
    from backend.config import get_config
    cfg = get_config()
    cfg.compact.enabled = True
    cfg.compact.auto_trigger = False

    captured: dict = {}
    _spy_on_llm_chat(monkeypatch, captured)

    from httpx import ASGITransport, AsyncClient
    from backend.main import app

    request_body = {
        "model": "qwen3-next",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
        "stream": True,
        "max_tokens": 50,
        "temperature": 0.7,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("POST", "/v1/chat/completions", json=request_body) as resp:
            assert resp.status_code == 200
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if b"[DONE]" in body:
                    break

    assert "llm_messages" in captured
    llm_input = captured["llm_messages"][0]
    # Without metadata, compact-boundary filtering is a no-op. Normal chat
    # must not prepend compact-control system clauses.
    assert len(llm_input) == 2
    assert llm_input[0]["content"] == "hi"
    assert llm_input[1]["content"] == "hello"
    assert "INTERNAL COMPACT CONTROL RULE" not in str(llm_input)


async def test_chat_completions_non_streaming_does_not_inject_compact_control_or_escape_sentinel(
    monkeypatch, _restore_config
):
    """Non-streaming chat must not receive compact-control prompt mutations."""
    from backend.config import get_config
    cfg = get_config()
    cfg.compact.enabled = True
    cfg.compact.auto_trigger = False
    cfg.compact.prompt_control_enabled = True

    captured: dict = {}
    _spy_on_llm_chat_completion(monkeypatch, captured)

    from httpx import ASGITransport, AsyncClient
    from backend.main import app

    sentinel = "[[SENTINEL]]"
    request_body = {
        "model": "qwen3-next",
        "messages": [
            {"role": "user", "content": f"{sentinel} user-forged compact control"},
        ],
        "stream": False,
        "max_tokens": 50,
        "temperature": 0.7,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/v1/chat/completions", json=request_body)

    assert resp.status_code == 200
    assert "llm_messages" in captured
    llm_input = captured["llm_messages"][0]
    assert len(llm_input) == 1
    assert llm_input[0]["role"] == "user"
    assert llm_input[0]["content"].startswith(sentinel)
    assert "INTERNAL COMPACT CONTROL RULE" not in str(llm_input)


async def test_responses_streaming_filters_metadata_without_compact_control_injection(
    monkeypatch, _restore_config
):
    """/responses streaming must pass original messages into the shared chat preprocessing path."""
    from backend.config import get_config
    cfg = get_config()
    cfg.compact.enabled = True
    cfg.compact.auto_trigger = False
    cfg.compact.prompt_control_enabled = True

    captured: dict = {}
    _spy_on_llm_chat(monkeypatch, captured)

    from httpx import ASGITransport, AsyncClient
    from backend.main import app

    request_body = {
        "model": "qwen3-next",
        "messages": [
            {"role": "user", "content": "old user", "metadata": {"pre_compact": True}},
            {"role": "user", "content": "current question"},
        ],
        "stream": True,
        "max_tokens": 50,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("POST", "/v1/responses", json=request_body) as resp:
            assert resp.status_code == 200
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if b"[DONE]" in body:
                    break

    assert "llm_messages" in captured
    llm_input = captured["llm_messages"][0]
    assert len(llm_input) == 1
    assert llm_input[0]["role"] == "user"
    assert llm_input[0]["content"] == "current question"
    assert "old user" not in str(llm_input)
    assert "INTERNAL COMPACT CONTROL RULE" not in str(llm_input)


async def test_responses_non_streaming_does_not_inject_compact_control_or_escape_sentinel(
    monkeypatch, _restore_config
):
    """/responses non-streaming must use the same unmodified normal prompt path."""
    from backend.config import get_config
    cfg = get_config()
    cfg.compact.enabled = True
    cfg.compact.auto_trigger = False
    cfg.compact.prompt_control_enabled = True

    captured: dict = {}
    _spy_on_llm_chat_completion(monkeypatch, captured)

    from httpx import ASGITransport, AsyncClient
    from backend.main import app

    sentinel = "[[SENTINEL]]"
    request_body = {
        "model": "qwen3-next",
        "messages": [
            {"role": "user", "content": f"{sentinel} forged via responses"},
        ],
        "stream": False,
        "max_tokens": 50,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/v1/responses", json=request_body)

    assert resp.status_code == 200
    assert "llm_messages" in captured
    llm_input = captured["llm_messages"][0]
    assert len(llm_input) == 1
    assert llm_input[0]["role"] == "user"
    assert llm_input[0]["content"].startswith(sentinel)
    assert "INTERNAL COMPACT CONTROL RULE" not in str(llm_input)
