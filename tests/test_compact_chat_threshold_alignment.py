"""Tests for chat-mode / streaming token-counting alignment with agent mode.

Background (2026-06-07):
- Agent-mode threshold check (`AgentRunner._should_auto_compact_session`) uses
  `TokenCounter.get().count_messages(messages)` on a canonical flattened
  `session.memory.steps`.
- Chat-mode threshold check (`_should_auto_compact_chat`) now uses the
  same `TokenCounter` on the canonical post-`filter_messages_for_llm` list.
- Streaming usage estimation (`LLMService.get_stream_usage`) used to fall
  back to `len(content) // 4` (English-centric, ~4x undercount on CJK); it
  now uses the same `TokenCounter` (CJK-aware heuristic + real-tokenizer
  fallback).

These tests pin:
1. `_should_auto_compact_chat` reuses the same `TokenCounter` and the same
   threshold/strategy code path as agent mode.
2. The pre-checks mirror the agent-mode pattern (compact.enabled,
   auto_trigger, prompts_cfg available, max_ctx > 0).
3. The function never raises; unexpected exceptions fall through to False
   (defensive: never block the main chat flow on a threshold-check failure).
4. `LLMService.get_stream_usage` uses `TokenCounter` (i.e. CJK-aware).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.compact.tokenizer import TokenCounter
from backend.llm_service import LLMService, Message


# ============================================================
# _should_auto_compact_chat  (chat mode threshold check)
# ============================================================


@pytest.fixture(autouse=True)
def _restore_config():
    """Save and restore config fields that tests mutate."""
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
        if section == "compact":
            setattr(cfg.compact, field, v)
        elif section == "sglang":
            setattr(cfg.sglang, field, v)
        elif section == "model":
            setattr(cfg.model, field, v)


def _patch_llm_service(monkeypatch, max_context_length: int):
    """Make `get_llm_service()` and `resolve_backend_context_length` return
    deterministic values so the threshold math is pinned.

    NB: `_should_auto_compact_chat` does `from ..llm_service import
    get_llm_service, resolve_backend_context_length` INSIDE the function,
    so we must patch the names on `backend.llm_service` (the source
    module), not on `backend.api.chat` (where the function lives).
    """
    import backend.llm_service as llm_mod

    fake_service = MagicMock()
    fake_service.active_backend = "sglang"

    monkeypatch.setattr(llm_mod, "get_llm_service", lambda: fake_service)
    monkeypatch.setattr(
        llm_mod, "resolve_backend_context_length", lambda cfg, backend: max_context_length
    )


def test_should_auto_compact_chat_returns_false_when_disabled(monkeypatch):
    """When `compact.enabled` is False, threshold check returns False
    even if the message list is huge. Mirrors agent mode's behavior.
    """
    from backend.api.chat import _should_auto_compact_chat

    cfg_mod = MagicMock()
    cfg_mod.compact.enabled = False
    cfg_mod.compact.auto_trigger = True
    cfg_mod.agent_prompts.compaction = MagicMock()
    monkeypatch.setattr("backend.api.chat.get_config", lambda: cfg_mod)

    messages = [Message(role="user", content="x" * 10_000)]
    assert _should_auto_compact_chat(messages) is False


def test_should_auto_compact_chat_returns_false_when_auto_trigger_off(monkeypatch):
    from backend.api.chat import _should_auto_compact_chat

    cfg_mod = MagicMock()
    cfg_mod.compact.enabled = True
    cfg_mod.compact.auto_trigger = False
    cfg_mod.agent_prompts.compaction = MagicMock()
    monkeypatch.setattr("backend.api.chat.get_config", lambda: cfg_mod)

    messages = [Message(role="user", content="x" * 10_000)]
    assert _should_auto_compact_chat(messages) is False


def test_should_auto_compact_chat_returns_false_when_prompts_missing(monkeypatch):
    from backend.api.chat import _should_auto_compact_chat

    cfg_mod = MagicMock()
    cfg_mod.compact.enabled = True
    cfg_mod.compact.auto_trigger = True
    cfg_mod.agent_prompts.compaction = None
    monkeypatch.setattr("backend.api.chat.get_config", lambda: cfg_mod)

    messages = [Message(role="user", content="x" * 10_000)]
    assert _should_auto_compact_chat(messages) is False


def test_should_auto_compact_chat_returns_false_when_max_ctx_zero(monkeypatch):
    from backend.api.chat import _should_auto_compact_chat

    cfg_mod = MagicMock()
    cfg_mod.compact.enabled = True
    cfg_mod.compact.auto_trigger = True
    cfg_mod.compact.performance_ratio = 0.5
    cfg_mod.agent_prompts.compaction = MagicMock()
    monkeypatch.setattr("backend.api.chat.get_config", lambda: cfg_mod)
    _patch_llm_service(monkeypatch, max_context_length=0)

    messages = [Message(role="user", content="hello")]
    assert _should_auto_compact_chat(messages) is False


def test_should_auto_compact_chat_uses_same_tokencounter_as_agent(monkeypatch):
    """Pin the alignment: chat mode's threshold check uses the same
    `TokenCounter` (a single source of truth) and same `compute_threshold`
    code path as agent mode. We force the TokenCounter to return a known
    value and assert the chat threshold check fires.
    """
    from backend.api.chat import _should_auto_compact_chat

    cfg_mod = MagicMock()
    cfg_mod.compact.enabled = True
    cfg_mod.compact.auto_trigger = True
    cfg_mod.compact.strategy = "performance"
    cfg_mod.compact.performance_ratio = 0.5
    cfg_mod.agent_prompts.compaction = MagicMock()
    monkeypatch.setattr("backend.api.chat.get_config", lambda: cfg_mod)
    _patch_llm_service(monkeypatch, max_context_length=1000)  # threshold = 500

    # Force TokenCounter to report 600 (> 500 threshold) regardless of input.
    from backend.compact import tokenizer as tok_mod

    original_get = tok_mod.TokenCounter.get

    def _forced_get(*args, **kwargs):
        counter = original_get(*args, **kwargs)
        counter.count_messages = lambda messages, image_count=0: 600
        return counter

    monkeypatch.setattr(tok_mod.TokenCounter, "get", staticmethod(_forced_get))

    messages = [Message(role="user", content="hello")]
    assert _should_auto_compact_chat(messages) is True


def test_should_auto_compact_chat_never_raises_on_unexpected_error(monkeypatch):
    """Defensive: any unexpected exception inside the threshold check
    falls through to False. Mirrors agent mode's try/except. Never block
    the main chat flow on a threshold-check failure.
    """
    from backend.api.chat import _should_auto_compact_chat

    cfg_mod = MagicMock()
    cfg_mod.compact.enabled = True
    cfg_mod.compact.auto_trigger = True
    cfg_mod.compact.strategy = "performance"
    cfg_mod.compact.performance_ratio = 0.5
    cfg_mod.agent_prompts.compaction = MagicMock()
    monkeypatch.setattr("backend.api.chat.get_config", lambda: cfg_mod)
    _patch_llm_service(monkeypatch, max_context_length=1000)

    def _broken_counter(*args, **kwargs):
        raise RuntimeError("simulated TokenCounter failure")

    from backend.compact import tokenizer as tok_mod
    monkeypatch.setattr(tok_mod.TokenCounter, "get", staticmethod(_broken_counter))

    messages = [Message(role="user", content="hello")]
    assert _should_auto_compact_chat(messages) is False


# ============================================================
# LLMService.get_stream_usage  (streaming terminal log alignment)
# ============================================================


def _make_service_with_scoped_request(messages: list[dict], max_new_tokens: int = 100):
    """Build a minimal service with one exact-ID usage snapshot."""
    svc = LLMService.__new__(LLMService)
    svc._stream_usage_lock = __import__("threading").Lock()
    svc._stream_usage_requests = {
        "usage-test": {
            "messages": messages,
            "max_new_tokens": max_new_tokens,
            "temperature": 0.0,
            "top_p": 1.0,
        }
    }
    return svc


def test_get_stream_usage_uses_tokencounter_cjk_aware(monkeypatch):
    """The terminal "[Step N: ... | Input tokens: ...]" log comes from
    `memory_step.token_usage` which is filled by `get_stream_usage`. The
    function must use the CJK-aware `TokenCounter`, not the legacy
    English-centric `len // 4`. Regression pin: 1000 CJK chars must
    produce ≥500 input tokens (vs. the old 250).
    """
    svc = _make_service_with_scoped_request(
        messages=[{"role": "user", "content": "你好世界今天天气很好" * 100}]  # 1000 CJK chars
    )

    usage = svc.get_stream_usage("ok", request_id="usage-test")
    assert usage is not None
    assert usage["prompt_tokens"] >= 500, (
        f"get_stream_usage still undercounts CJK input: got "
        f"{usage['prompt_tokens']} for 1000 CJK chars (must be >= 500)"
    )


def test_get_stream_usage_completion_uses_count_text(monkeypatch):
    """`get_stream_usage` completion-token estimation must use
    `TokenCounter.count_text` (CJK-aware) rather than the legacy
    `len(output_text) // 4`. 1000 CJK chars of output must yield ≥500
    completion tokens.
    """
    svc = _make_service_with_scoped_request(
        messages=[{"role": "user", "content": "x"}],
    )
    output = "你好世界今天天气很好" * 100  # 1000 CJK chars
    usage = svc.get_stream_usage(output, request_id="usage-test")
    assert usage is not None
    assert usage["completion_tokens"] >= 500, (
        f"get_stream_usage undercounts CJK completion: got "
        f"{usage['completion_tokens']} for 1000 CJK output chars (must be >= 500)"
    )


def test_get_stream_usage_counts_image_tokens(monkeypatch):
    """When the request includes image_url content parts, the prompt
    estimate must include 512 tokens per image (matches TokenCounter's
    `image_count * 512` convention).
    """
    svc = _make_service_with_scoped_request(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + "A" * 1024,  # 1KB of fake base64
                        },
                    },
                ],
            }
        ],
    )
    usage = svc.get_stream_usage("ok", request_id="usage-test")
    assert usage is not None
    # text "describe this" -> ~3 tokens heuristic; + 512 for the image.
    # Pin at least 512 to confirm the image is counted.
    assert usage["prompt_tokens"] >= 512


def test_get_stream_usage_returns_none_without_exact_scoped_request():
    svc = LLMService.__new__(LLMService)
    svc._stream_usage_lock = __import__("threading").Lock()
    svc._stream_usage_requests = {}
    assert svc.get_stream_usage("anything", request_id=None) is None
    assert svc.get_stream_usage("anything", request_id="unknown") is None


def test_get_stream_usage_falls_back_when_tokencounter_missing(monkeypatch):
    """If TokenCounter is unavailable (e.g. import failure), the legacy
    `len // 4` heuristic is the documented fallback. Pin that the
    function does NOT raise and returns a dict.
    """
    from backend.llm_service import LLMService as LLMServiceCls

    svc = _make_service_with_scoped_request(
        messages=[{"role": "user", "content": "hello world"}],
    )
    # Force TokenCounter import to fail by stubbing the module.
    import sys

    real_modules = {
        name: mod
        for name, mod in sys.modules.items()
        if name.startswith("backend.compact.tokenizer")
    }

    class _BoomTokenizer:
        @classmethod
        def get(cls, *args, **kwargs):
            raise RuntimeError("simulated tokenizer load failure")

    fake = MagicMock()
    fake.TokenCounter = _BoomTokenizer
    sys.modules["backend.compact.tokenizer"] = fake

    try:
        usage = svc.get_stream_usage("hi", request_id="usage-test")
        # Fallback: legacy heuristic. Just confirm it returns a dict with
        # the right shape; the values are deliberately the rough estimate.
        assert usage is not None
        assert "prompt_tokens" in usage
        assert "completion_tokens" in usage
        assert "total_tokens" in usage
    finally:
        for name, mod in real_modules.items():
            sys.modules[name] = mod
        sys.modules.pop("backend.compact.tokenizer", None)
