"""Regression: the auto-compact rebuild path in backend.api.chat
builds final_messages as:
  [Message(role='user', content=envelope), Message(role=last.role, content=last.content)]

where `last` is the LAST item in `filtered` (the messages that pass
filter_messages_for_llm). If the chat history had a system message
(compact summary) BEFORE the user's current message, and the filter
returns all messages including that system message, the chat
template's strict check (system must be at the beginning) would
fail downstream.

The correct behavior: rebuilt messages should contain only
[envelope, current_user_message] — NOT the prior system summary
or other old messages. This is what the v1 (/compact) design calls
"replace history with summary envelope + current user query".

These tests pin the contract so future refactors cannot regress to
"include the prior system summary" or "include all old messages".
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")


class _StubOrchestrator:
    """Returns one compact_completed event with a fixed summary text."""

    def __init__(self, summary_text: str = "Test summary"):
        self.summary_text = summary_text
        self.last_request = None

    async def run_compact(self, req):
        from backend.compact.types import CompactEvent, CompactEventKind

        self.last_request = req
        summary_id = "compact-test-uuid"
        yield CompactEvent(
            kind=CompactEventKind.STARTED,
            summary_message_id=summary_id,
            metadata={"trigger": "auto", "mode": "chat"},
        )
        yield CompactEvent(
            kind=CompactEventKind.COMPLETED,
            summary_message_id=summary_id,
            text=self.summary_text,
            partial=False,
            metadata={"trigger": "auto", "mode": "chat"},
        )


def _patch_orchestrator(monkeypatch, stub):
    from backend.main import app
    monkeypatch.setattr(app.state, "compact_orchestrator", stub, raising=False)


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
        "sglang.max_context_length": cfg.sglang.max_context_length,
        "model.max_context_length": cfg.model.max_context_length,
    }
    yield
    for k, v in saved.items():
        section, field = k.split(".", 1)
        setattr(getattr(cfg, section), field, v)


@pytest.mark.asyncio
async def test_auto_compact_rebuilds_with_only_envelope_and_current_user(
    monkeypatch, _restore_config
):
    """When auto-compact fires, the next LLM call should see ONLY:
    [envelope(summary), current_user_message].
    Old messages must NOT be re-included, and any prior system summary
    in the page state must NOT be carried over (it would violate
    the chat template's 'system message at the beginning' rule)."""
    _patch_orchestrator(monkeypatch, _StubOrchestrator("Prior conversation summarized."))
    from backend.config import get_config
    from backend.main import app
    cfg = get_config()
    cfg.compact.enabled = True
    cfg.compact.auto_trigger = True
    cfg.compact.strategy = "performance"
    cfg.compact.performance_ratio = 0.0  # force trigger
    cfg.sglang.max_context_length = 1000  # tiny context to force trigger

    from httpx import ASGITransport, AsyncClient

    # Capture what the LLM service sends by monkey-patching the SGLang
    # provider's chat path. We intercept the LLMService.chat method to
    # capture final_messages without actually calling SGLang.
    captured = {}
    from backend.llm_service import LLMService

    orig_chat = LLMService.chat

    def spy_chat(self, *args, messages=None, **kwargs):
        # If this is the second call (the actual chat after compact),
        # capture the messages and return a tiny generator.
        if messages and len(messages) <= 2 and messages[0].role == "user":
            # This is the rebuilt chat call
            captured["final_messages"] = [
                {"role": m.role, "content": m.content} for m in messages
            ]
            def gen():
                yield {"content": "OK", "reasoning": None}
            return gen()
        # Otherwise pass through (the compact call uses huge prompts).
        return orig_chat(self, *args, messages=messages, **kwargs)

    monkeypatch.setattr(LLMService, "chat", spy_chat)

    # The request body mirrors what the frontend sends: old messages,
    # then a current user message. NO pre_compact metadata (frontend
    # does not send it for this code path).
    request_body = {
        "model": "qwen3-next",
        "messages": [
            {"role": "user", "content": "old user message 1"},
            {"role": "assistant", "content": "old assistant reply 1"},
            {"role": "user", "content": "old user message 2"},
            {"role": "assistant", "content": "old assistant reply 2"},
            {"role": "user", "content": "current user query (the trigger)"},
        ],
        "stream": True,
        "max_tokens": 50,
        "temperature": 0.7,
        "top_p": 0.9,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("POST", "/v1/chat/completions", json=request_body) as resp:
            assert resp.status_code == 200
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if b"compact_completed" in body and b"[DONE]" in body:
                    break

    # The 2nd LLM call should have exactly 2 messages: envelope + current
    assert "final_messages" in captured, (
        "auto-compact did not fire; final_messages was not captured. "
        "Check that compact.enabled and performance_ratio=0.0 took effect."
    )
    final = captured["final_messages"]
    assert len(final) == 2, (
        f"expected exactly 2 messages (envelope + current user), "
        f"got {len(final)}: {final!r}"
    )
    assert final[0]["role"] == "user", (
        f"first message should be the envelope (role=user), got role={final[0]['role']!r}"
    )
    assert "summary" in final[0]["content"].lower() or "summar" in final[0]["content"].lower(), (
        f"first message content should be the summary envelope, got: {final[0]['content'][:200]!r}"
    )
    assert final[1]["role"] == "user", (
        f"second message should be the current user query, got role={final[1]['role']!r}"
    )
    assert final[1]["content"] == "current user query (the trigger)", (
        f"second message should be the trigger, got: {final[1]['content']!r}"
    )
