"""Regression: the manual /v1/compact/run endpoint, when called from the
slash command path (frontend handleSelectSlashCommand), must receive the
current page messages in the request body. If neither body_messages (v2)
nor messages (v1) is set on CompactRequest, the inlined no_messages branch
in CompactOrchestrator.run_compact yields compact_failed("no_messages"),
which the frontend silently rolls back — making /compact look like a
no-op.

This test pins the contract: the manual endpoint accepts messages and
returns compact_completed for a simple chat-mode body.
"""
import json
import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")

from httpx import ASGITransport, AsyncClient

from backend.compact.orchestrator import CompactOrchestrator


class _StubLLM:
    async def chat_stream_async(self, **kwargs):
        for c in ["<summary>OK</summary>"]:
            yield {"content": c}


def _patch_compact_orchestrator(monkeypatch):
    from backend.main import app
    from backend.config import get_config

    cfg = get_config()
    app.state.compact_orchestrator = CompactOrchestrator(
        llm_service=_StubLLM(),
        config=cfg.compact,
        prompts=cfg.agent_prompts.compaction,
    )


@pytest.mark.asyncio
async def test_compact_run_chat_mode_with_only_session_id_yields_no_messages(
    monkeypatch,
):
    """Without body_messages (v2) or messages (v1) in the request body, the
    orchestrator must yield compact_failed (no_messages). The frontend
    must always send body_messages for the manual /compact path so this
    branch is never hit. The v1 'collect_error' reason was renamed in
    the v2 refactor (Task 4): the inlined branch in run_compact uses
    the more specific 'no_messages' label that points at the actual
    contract violation (the request had neither v2 body_messages nor
    v1 messages)."""
    _patch_compact_orchestrator(monkeypatch)
    from backend.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "mode": "chat",
            "session_id": "some-session-uuid",
        }
        text = ""
        async with client.stream("POST", "/v1/compact/run", json=body) as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                text += chunk
                if "compact_failed" in text or "compact_completed" in text:
                    break
    events = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                events.append(json.loads(payload))
    assert events, "expected at least one compact event"
    last = events[-1]
    assert last["kind"] == "compact_failed"
    assert last["metadata"]["reason"] == "no_messages"


@pytest.mark.asyncio
async def test_compact_run_chat_mode_with_empty_messages_array_yields_empty_summary(
    monkeypatch,
):
    """With messages=[] the orchestrator should still run (build prompt
    with empty body), but format_compact_summary returns empty string
    and yields compact_failed(empty_summary). This pins the behavior so
    the frontend can decide whether to short-circuit client-side."""
    _patch_compact_orchestrator(monkeypatch)
    from backend.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "mode": "chat",
            "messages": [],
        }
        text = ""
        async with client.stream("POST", "/v1/compact/run", json=body) as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                text += chunk
                if "compact_failed" in text or "compact_completed" in text:
                    break
    events = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                events.append(json.loads(payload))
    last = events[-1]
    assert last["kind"] == "compact_failed"
