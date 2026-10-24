"""Regression: the SSE event payload must expose trigger/mode/original_tokens/
threshold/strategy at the TOP LEVEL (not just nested inside metadata).

The frontend's projectTimelineEvent reads these from the top level. If the
backend only puts them in metadata, the panel renders with undefined
trigger/mode/etc.
"""
import json
import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")

from httpx import ASGITransport, AsyncClient

from backend.compact.orchestrator import CompactOrchestrator


class _StubLLM:
    async def chat_stream_async(self, **kwargs):
        for c in ["<summary>Test summary</summary>"]:
            yield {"content": c}


def _patch_compact_orchestrator(monkeypatch):
    from backend.main import app
    from backend.config import get_config

    cfg = get_config()
    cfg.compact.validation_min_summary_chars = 1
    cfg.compact.validation_min_summary_prompt_ratio = 0.0
    cfg.compact.validation_require_summary_sections = False
    app.state.compact_orchestrator = CompactOrchestrator(
        llm_service=_StubLLM(),
        config=cfg.compact,
        prompts=cfg.agent_prompts.compaction,
    )


@pytest.mark.asyncio
async def test_compact_started_event_flattens_trigger_and_mode_to_top_level(monkeypatch):
    _patch_compact_orchestrator(monkeypatch)
    from backend.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "mode": "chat",
            "messages": [{"role": "user", "content": "old"}],
        }
        text = ""
        async with client.stream("POST", "/v1/compact/run", json=body) as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                text += chunk
                if "compact_started" in text:
                    break
    started = None
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        ev = json.loads(payload)
        if ev.get("kind") == "compact_started":
            started = ev
            break
    assert started is not None
    # Top-level fields the frontend reads directly:
    assert started.get("trigger") == "manual"
    assert started.get("mode") == "chat"
    # They must also remain in metadata for backward compat:
    assert started["metadata"]["trigger"] == "manual"
    assert started["metadata"]["mode"] == "chat"


@pytest.mark.asyncio
async def test_compact_completed_event_flattens_metadata_to_top_level(monkeypatch):
    _patch_compact_orchestrator(monkeypatch)
    from backend.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "mode": "chat",
            # v2 builder requires body_messages[0].role == "system" (spec §5.2).
            "messages": [
                {"role": "system", "content": "You are a session history compressor."},
                {"role": "user", "content": "old"},
            ],
        }
        text = ""
        async with client.stream("POST", "/v1/compact/run", json=body) as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                text += chunk
                if "compact_completed" in text:
                    break
    completed = None
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        ev = json.loads(payload)
        if ev.get("kind") == "compact_completed":
            completed = ev
            break
    assert completed is not None
    assert completed.get("trigger") == "manual"
    assert completed.get("mode") == "chat"
