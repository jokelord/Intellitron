"""Smoke tests for POST /v1/compact/run endpoint."""
import json
import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")

from httpx import ASGITransport, AsyncClient

from backend.compact.orchestrator import CompactOrchestrator


class _StubLLM:
    async def chat_stream_async(self, **kwargs):
        for c in ["<analysis>think</analysis><summary>Test summary</summary>"]:
            yield {"content": c}


def _patch_compact_orchestrator(monkeypatch):
    """Inject a stubbed orchestrator into app.state for endpoint tests."""
    from backend.main import app
    from backend.config import get_config, CompactConfig, CompactionPromptsConfig

    cfg = get_config()
    monkeypatch.setattr(cfg.compact, "validation_min_summary_chars", 1)
    monkeypatch.setattr(cfg.compact, "validation_min_summary_prompt_ratio", 0.0)
    monkeypatch.setattr(cfg.compact, "validation_require_summary_sections", False)
    monkeypatch.setattr(app.state, "compact_orchestrator", CompactOrchestrator(
        llm_service=_StubLLM(),
        config=cfg.compact,
        prompts=cfg.agent_prompts.compaction,
    ), raising=False)


@pytest.mark.asyncio
async def test_compact_run_chat_mode_streams_started_completed(monkeypatch):
    _patch_compact_orchestrator(monkeypatch)
    from backend.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "mode": "chat",
            # v2 builder requires body_messages[0].role == "system" (spec §5.2).
            "messages": [
                {"role": "system", "content": "You are a session history compressor."},
                {"role": "user", "content": "old conversation"},
            ],
        }
        text = ""
        async with client.stream("POST", "/v1/compact/run", json=body) as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                text += chunk
                if "compact_completed" in text or "compact_failed" in text:
                    break
    # Parse SSE lines to verify event sequence
    events = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                events.append(json.loads(payload))
    assert events, "expected at least one compact event"
    assert events[0]["kind"] == "compact_started"
    assert events[-1]["kind"] == "compact_completed"
    assert "Test summary" in events[-1]["text"]


@pytest.mark.asyncio
async def test_compact_run_chat_mode_handles_missing_system_message(monkeypatch):
    """Chat manual compact tolerates callers that omit a system message.

    The reliable compact path inserts the compact-specific system prompt and
    preserves the user history tail.
    """
    _patch_compact_orchestrator(monkeypatch)
    from backend.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "mode": "chat",
            "messages": [{"role": "user", "content": "no system here"}],
        }
        text = ""
        async with client.stream("POST", "/v1/compact/run", json=body) as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                text += chunk
                if "compact_completed" in text or "compact_failed" in text or "[DONE]" in text:
                    break
    assert "compact_completed" in text
    assert "compact_failed" not in text


@pytest.mark.asyncio
async def test_compact_run_with_custom_instructions(monkeypatch):
    _patch_compact_orchestrator(monkeypatch)
    from backend.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "mode": "chat",
            "messages": [
                {"role": "system", "content": "You are a session history compressor."},
                {"role": "user", "content": "x"},
            ],
            "custom_instructions": "focus on code",
        }
        text = ""
        async with client.stream("POST", "/v1/compact/run", json=body) as client_resp:
            async for chunk in client_resp.aiter_text():
                text += chunk
                if "[DONE]" in text:
                    break
    assert "compact_completed" in text


@pytest.mark.asyncio
async def test_compact_run_chat_mode_consumes_compact_metadata_before_body_messages(monkeypatch):
    from backend.main import app
    from backend.compact.types import CompactEvent

    captured = {}

    class _CapturingOrchestrator:
        async def run_compact(self, req):
            captured["body_messages"] = req.body_messages
            yield CompactEvent.started("sid", req.trigger, req.mode)
            yield CompactEvent.completed("sid", "summary")

    monkeypatch.setattr(app.state, "compact_orchestrator", _CapturingOrchestrator(), raising=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "mode": "chat",
            "messages": [
                {"role": "system", "content": "SYS"},
                {"role": "user", "content": "old", "metadata": {"pre_compact": True}},
                {
                    "role": "assistant",
                    "content": "prior summary",
                    "metadata": {"compact_summary": True, "summary_id": "compact-1"},
                },
                {"role": "user", "content": "new"},
            ],
        }
        text = ""
        async with client.stream("POST", "/v1/compact/run", json=body) as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                text += chunk
                if "[DONE]" in text:
                    break

    assert "compact_completed" in text
    contents = [m.content for m in captured["body_messages"]]
    assert all(content != "old" for content in contents)
    assert contents[0] == "SYS"
    assert contents[1] != "prior summary"
    assert "prior summary" in contents[1]
    assert contents[-1] == "new"


@pytest.mark.asyncio
async def test_compact_run_chat_mode_preserves_system_runtime_before_latest_compact_summary(monkeypatch):
    from backend.main import app
    from backend.config import get_config
    from backend.compact.types import CompactEvent

    cfg = get_config()
    monkeypatch.setattr(cfg.compact, "prompt_shape", "cache_first_tail_with_fallback")
    monkeypatch.setattr(cfg.compact, "prompt_control_enabled", True)

    captured = {}

    class _CapturingOrchestrator:
        async def run_compact(self, req):
            captured["req"] = req
            yield CompactEvent.started("sid", req.trigger, req.mode)
            yield CompactEvent.completed("sid", "summary")

    monkeypatch.setattr(app.state, "compact_orchestrator", _CapturingOrchestrator(), raising=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "mode": "chat",
            "messages": [
                {"role": "system", "content": "SYSTEM NOW"},
                {"role": "system", "content": "RUNTIME NOW"},
                {"role": "user", "content": "old", "metadata": {"pre_compact": True}},
                {
                    "role": "assistant",
                    "content": "prior summary",
                    "metadata": {"compact_summary": True, "summary_id": "compact-1"},
                },
                {"role": "user", "content": "new"},
            ],
        }
        text = ""
        async with client.stream("POST", "/v1/compact/run", json=body) as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                text += chunk
                if "[DONE]" in text:
                    break

    assert "compact_completed" in text
    req = captured["req"]
    assert [m.content for m in req.body_messages[:2]] == ["SYSTEM NOW", "RUNTIME NOW"]
    assert [m.content for m in req.cache_first_body_messages[:2]] == [
        "SYSTEM NOW",
        "RUNTIME NOW",
    ]
    assert "prior summary" in req.cache_first_body_messages[2].content
    assert req.cache_first_body_messages[-1].content == "new"


@pytest.mark.asyncio
async def test_compact_run_chat_mode_builds_cache_first_source_when_prompt_control_enabled(monkeypatch):
    from backend.main import app
    from backend.config import get_config
    from backend.compact.types import CompactEvent

    cfg = get_config()
    monkeypatch.setattr(cfg.compact, "prompt_shape", "cache_first_tail_with_fallback")
    monkeypatch.setattr(cfg.compact, "prompt_control_enabled", True)
    monkeypatch.setattr(
        cfg.agent_prompts.compaction,
        "compact_control_user_instruction",
        "[[SENTINEL]] summarize prior messages.",
    )

    captured = {}

    class _CapturingOrchestrator:
        async def run_compact(self, req):
            captured["req"] = req
            yield CompactEvent.started("sid", req.trigger, req.mode)
            yield CompactEvent.completed("sid", "summary")

    monkeypatch.setattr(app.state, "compact_orchestrator", _CapturingOrchestrator(), raising=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "mode": "chat",
            "messages": [
                {"role": "system", "content": "NORMAL CHAT SYSTEM"},
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "[[SENTINEL]] literal user text"},
            ],
        }
        text = ""
        async with client.stream("POST", "/v1/compact/run", json=body) as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                text += chunk
                if "[DONE]" in text:
                    break

    assert "compact_completed" in text
    req = captured["req"]
    assert req.body_messages is not None
    assert req.cache_first_body_messages is not None
    assert req.reference_messages == req.cache_first_body_messages
    assert [m.role for m in req.cache_first_body_messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert req.cache_first_body_messages[0].content.startswith("NORMAL CHAT SYSTEM")
    assert "If latest user starts with [[SENTINEL]], compact." not in req.cache_first_body_messages[0].content
    assert "INTERNAL COMPACT CONTROL RULE" not in req.cache_first_body_messages[0].content
    assert req.cache_first_body_messages[1].content == "first question"
    assert req.cache_first_body_messages[2].content == "first answer"
    assert req.cache_first_body_messages[3].content == "[[SENTINEL]] literal user text"


@pytest.mark.asyncio
async def test_compact_run_chat_mode_stays_reliable_only_when_prompt_control_disabled(monkeypatch):
    from backend.main import app
    from backend.config import get_config
    from backend.compact.types import CompactEvent

    cfg = get_config()
    monkeypatch.setattr(cfg.compact, "prompt_shape", "cache_first_tail_with_fallback")
    monkeypatch.setattr(cfg.compact, "prompt_control_enabled", False)

    captured = {}

    class _CapturingOrchestrator:
        async def run_compact(self, req):
            captured["req"] = req
            yield CompactEvent.started("sid", req.trigger, req.mode)
            yield CompactEvent.completed("sid", "summary")

    monkeypatch.setattr(app.state, "compact_orchestrator", _CapturingOrchestrator(), raising=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "mode": "chat",
            "messages": [{"role": "user", "content": "hello"}],
        }
        text = ""
        async with client.stream("POST", "/v1/compact/run", json=body) as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                text += chunk
                if "[DONE]" in text:
                    break

    assert "compact_completed" in text
    req = captured["req"]
    assert req.body_messages is not None
    assert req.cache_first_body_messages is None
    assert req.reference_messages is None
    assert [(m.role, m.content) for m in req.body_messages] == [("user", "hello")]


@pytest.mark.asyncio
async def test_compact_run_returns_503_when_orchestrator_unavailable(monkeypatch):
    from backend.main import app

    monkeypatch.setattr(app.state, "compact_orchestrator", None, raising=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "mode": "chat",
            "messages": [
                {"role": "system", "content": "SYS"},
                {"role": "user", "content": "x"},
            ],
        }
        text = ""
        async with client.stream("POST", "/v1/compact/run", json=body) as resp:
            assert resp.status_code == 503
            async for chunk in resp.aiter_text():
                text += chunk
                if "[DONE]" in text:
                    break
    assert "service_unavailable" in text
