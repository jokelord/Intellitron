"""Tests for chat-mode auto-compact trigger in /v1/chat/completions streaming path."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")

from httpx import ASGITransport, AsyncClient

from backend.compact.orchestrator import CompactOrchestrator
from backend.compact.types import CompactEvent
from backend.config import get_config, CompactConfig, CompactionPromptsConfig
from backend.llm_service import Message


class _StubLLM:
    """Returns one fixed chat chunk for the LLM call after auto-compact."""

    def __init__(self):
        self.stream_called_with = None
        self.stream_calls = 0

    async def chat_stream_async(self, messages, max_new_tokens=None, temperature=None,
                                top_p=None, tools=None, request_id=None, runtime_context=None,
                                extra_body=None):
        self.stream_calls += 1
        self.stream_called_with = list(messages)
        for c in ["Chat ", "response"]:
            yield {"content": c}


class _TriggeringLLM(_StubLLM):
    """compact LLM call returns a clean summary; main LLM call returns chat content."""

    async def chat_stream_async(self, messages, max_new_tokens=None, temperature=None,
                                top_p=None, tools=None, request_id=None, runtime_context=None,
                                extra_body=None):
        self.stream_calls += 1
        self.stream_called_with = list(messages)
        # Compact path carries a compact-* request id. The prompt shape may be
        # reliable position-1 or cache-first tail, so do not infer by message count.
        if request_id and str(request_id).startswith("compact-"):
            yield {"content": "<summary>Compressed summary</summary>"}
        else:
            for c in ["After-compact ", "reply"]:
                yield {"content": c}


def _patch_orchestrator(monkeypatch, llm_stub):
    from backend.main import app
    cfg = get_config()
    # auto_trigger is True by default; do not mutate enabled (tests may override).
    cfg.compact.auto_trigger = True
    cfg.compact.strategy = "performance"
    cfg.compact.performance_ratio = 0.5
    cfg.compact.validation_min_summary_chars = 1
    cfg.compact.validation_min_summary_prompt_ratio = 0.0
    cfg.compact.validation_require_summary_sections = False
    app.state.compact_orchestrator = CompactOrchestrator(
        llm_service=llm_stub,
        config=cfg.compact,
        prompts=cfg.agent_prompts.compaction,
    )


@pytest.fixture(autouse=True)
def _restore_config():
    """Save and restore config fields that tests mutate to avoid leaking into other tests."""
    cfg = get_config()
    saved = {
        "compact.enabled": cfg.compact.enabled,
        "compact.auto_trigger": cfg.compact.auto_trigger,
        "compact.strategy": cfg.compact.strategy,
        "compact.performance_ratio": cfg.compact.performance_ratio,
        "compact.validation_min_summary_chars": cfg.compact.validation_min_summary_chars,
        "compact.validation_min_summary_prompt_ratio": cfg.compact.validation_min_summary_prompt_ratio,
        "compact.validation_require_summary_sections": cfg.compact.validation_require_summary_sections,
        "sglang.max_context_length": cfg.sglang.max_context_length,
        "model.max_context_length": cfg.model.max_context_length,
    }
    yield
    for k, v in saved.items():
        section, field = k.split(".", 1)
        if section == "compact":
            setattr(cfg.compact, field, v)
        elif section == "sglang":
            setattr(cfg.sglang, field, v)
        elif section == "model":
            setattr(cfg.model, field, v)


def _parse_sse(text: str) -> list[dict]:
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            out.append(json.loads(payload))
        except Exception:
            pass
    return out


@pytest.mark.asyncio
async def test_chat_streaming_with_auto_compact_emits_compact_events_first(monkeypatch):
    """When token count exceeds threshold, compact events appear before chat chunks."""
    # Set max_context to a tiny number so a single short message exceeds threshold
    cfg = get_config()
    cfg.sglang.max_context_length = 1  # threshold = 0 (always trigger)
    cfg.model.max_context_length = 1
    _patch_orchestrator(monkeypatch, _TriggeringLLM())
    from backend.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "model": "qwen3-next",
            "messages": [
                {"role": "system", "content": "You are a test assistant."},
                {"role": "user", "content": "hello"},
            ],
            "stream": True,
        }
        text = ""
        async with client.stream("POST", "/v1/chat/completions", json=body) as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                text += chunk
                if text.count("[DONE]") >= 1:
                    break

    events = _parse_sse(text)
    compact_events = [e for e in events if isinstance(e, dict) and str(e.get("kind", "")).startswith("compact_")]
    chat_events = [e for e in events if isinstance(e, dict) and "choices" in e]

    assert compact_events, "expected at least one compact event"
    assert compact_events[0]["kind"] == "compact_started"
    assert compact_events[-1]["kind"] == "compact_completed"
    assert chat_events, "expected at least one chat completion chunk after compact"
    # Chat chunks should appear AFTER compact events
    first_compact_idx = events.index(compact_events[0])
    first_chat_idx = next(i for i, e in enumerate(events) if "choices" in e)
    assert first_compact_idx < first_chat_idx


@pytest.mark.asyncio
async def test_chat_streaming_no_compact_when_below_threshold(monkeypatch):
    """When token count is below threshold, no compact events; chat stream runs as usual."""
    cfg = get_config()
    cfg.sglang.max_context_length = 10_000_000  # huge -> threshold unreachable for short text
    cfg.model.max_context_length = 10_000_000
    _patch_orchestrator(monkeypatch, _StubLLM())
    from backend.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "model": "qwen3-next",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        text = ""
        async with client.stream("POST", "/v1/chat/completions", json=body) as resp:
            async for chunk in resp.aiter_text():
                text += chunk
                if text.count("[DONE]") >= 1:
                    break

    events = _parse_sse(text)
    compact_events = [e for e in events if str(e.get("kind", "")).startswith("compact_")]
    assert compact_events == []


@pytest.mark.asyncio
async def test_chat_auto_compact_disabled_when_config_disabled(monkeypatch):
    """When config.compact.enabled is False, no compact events even if tokens exceed threshold."""
    cfg = get_config()
    cfg.compact.enabled = False
    cfg.sglang.max_context_length = 1
    cfg.model.max_context_length = 1
    _patch_orchestrator(monkeypatch, _TriggeringLLM())
    from backend.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "model": "qwen3-next",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        text = ""
        async with client.stream("POST", "/v1/chat/completions", json=body) as resp:
            async for chunk in resp.aiter_text():
                text += chunk
                if text.count("[DONE]") >= 1:
                    break

    events = _parse_sse(text)
    compact_events = [e for e in events if str(e.get("kind", "")).startswith("compact_")]
    assert compact_events == []
    # restore for other tests
    cfg.compact.enabled = True


@pytest.mark.asyncio
async def test_chat_auto_compact_triggers_when_tokens_exceed_threshold(monkeypatch):
    """Regression (2026-06-06): auto-compact must trigger when real token
    count exceeds threshold. The pre-fix TokenCounter had two bugs that
    combined to make this never fire:

    (a) Heuristic was `len(content) // 4` (English-centric). For CJK text,
        the real tokenizer produces ~0.7-1.0 tokens/char, so the heuristic
        undercounted by 3-4x. A 30k-token Chinese conversation was reported
        as ~7k, well below any reasonable threshold.

    (b) The "real tokenizer" path (when AutoTokenizer was loaded) computed
        `len(apply_chat_template(tokenize=True))`, which on transformers
        >=4.43 returns a BatchEncoding whose `__len__` is the number of
        keys (2), not the token count. So the count was always 2.

    This test pins the contract: when count_messages returns a value above
    threshold, compact events MUST appear in the SSE stream. We use a mock
    counter to control the value deterministically (no real tokenizer load
    needed).
    """
    cfg = get_config()
    cfg.sglang.max_context_length = 1000
    cfg.model.max_context_length = 1000
    cfg.compact.performance_ratio = 0.5  # threshold = 500
    _patch_orchestrator(monkeypatch, _TriggeringLLM())
    from backend.main import app

    # Force TokenCounter to report a value above threshold (500).
    from backend.compact import tokenizer as tok_mod

    original_get = tok_mod.TokenCounter.get

    def _forced_get(*args, **kwargs):
        counter = original_get(*args, **kwargs)
        original_count = counter.count_messages

        def _count(messages, image_count: int = 0) -> int:
            return 1000  # above threshold of 500

        counter.count_messages = _count  # type: ignore[method-assign]
        return counter

    monkeypatch.setattr(tok_mod.TokenCounter, "get", staticmethod(_forced_get))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "model": "qwen3-next",
            "messages": [
                {"role": "system", "content": "You are a test assistant."},
                {"role": "user", "content": "hello"},
            ],
            "stream": True,
        }
        text = ""
        async with client.stream("POST", "/v1/chat/completions", json=body) as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                text += chunk
                if text.count("[DONE]") >= 1:
                    break

    events = _parse_sse(text)
    compact_events = [e for e in events if str(e.get("kind", "")).startswith("compact_")]
    assert compact_events, (
        "auto-compact did not trigger when token count (1000) exceeded "
        "threshold (500). This is the regression from 2026-06-06."
    )
    assert compact_events[0]["kind"] == "compact_started"
    assert compact_events[-1]["kind"] == "compact_completed"


@pytest.mark.asyncio
async def test_chat_auto_compact_passes_next_turn_focus_and_excludes_current_user(monkeypatch):
    from backend.api.chat import _generate_stream_response

    captured = {}

    class _CapturingOrchestrator:
        async def run_compact(self, req):
            captured["req"] = req
            yield CompactEvent.completed("compact-1", "Compressed summary")

    raw_request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(compact_orchestrator=_CapturingOrchestrator()))
    )
    original_chat_messages = [
        SimpleNamespace(role="system", content="SYS", metadata=None),
        SimpleNamespace(role="user", content="old task", metadata=None),
        SimpleNamespace(role="assistant", content="old answer", metadata=None),
        SimpleNamespace(role="user", content="new task after compact", metadata=None),
    ]
    messages = [Message(role=m.role, content=m.content) for m in original_chat_messages]

    with patch("backend.api.chat._should_auto_compact_chat", return_value=True):
        text = ""
        async for chunk in _generate_stream_response(
            service=_StubLLM(),
            messages=messages,
            model="qwen3-next",
            max_tokens=None,
            temperature=None,
            top_p=None,
            raw_request=raw_request,
            original_chat_messages=original_chat_messages,
        ):
            text += chunk
            if "[DONE]" in text:
                break

    req = captured["req"]
    assert req.custom_instructions is not None
    assert "Upcoming user instruction:" in req.custom_instructions
    assert "new task after compact" in req.custom_instructions
    assert all("new task after compact" not in str(m.content) for m in req.body_messages)
    assert all("new task after compact" not in str(m.content) for m in req.cache_first_body_messages)
