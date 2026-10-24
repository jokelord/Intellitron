"""Tests for the compact-diag instrumentation log line in CompactOrchestrator.

Per docs/superpowers/reviews/2026-06-11-v2-compact-not-helping-review.md Step 1+2 (Section D),
the instrumentation must capture five metrics in a single INFO log line:
  1. path (v2 vs v1)
  2. prompt_tokens (SGLang-side, if exposed; chars as fallback)
  3. prefill wall time (start -> first content chunk)
  4. decode wall time (first chunk -> end of stream)
  5. completion tokens (SGLang-side, if exposed; chars as fallback)

These tests verify:
  - The log line is emitted on the success path with all 5 fields populated.
  - The log line is NOT emitted on the failure path (LLM error before any chunk).
  - The path= field is "v2" for the body_messages path and "v1" for the
    legacy req.messages fallback.
  - SGLang usage tokens (when present in a chunk) override the None defaults.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, List, Optional

import pytest

from backend.compact.orchestrator import CompactOrchestrator
from backend.compact.types import CompactEventKind, CompactRequest
from backend.config import CompactConfig, CompactionPromptsConfig
from backend.llm_service import Message


class _FakeLLMServiceBase:
    """Captures the messages passed to chat_stream_async. Subclasses control
    the stream body so we can test success / failure / usage paths."""


class FakeLLMServiceSuccess(_FakeLLMServiceBase):
    """Yields one content chunk, no usage. Simulates the typical SGLang stream."""

    def __init__(self, content: str = "<summary>OK</summary>") -> None:
        self.captured_messages: Optional[List[Message]] = None
        self._content = content

    async def chat_stream_async(
        self,
        messages: List[Message],
        max_new_tokens: int,
        temperature: float,
        top_p: float,
        tools: Any = None,
        request_id: Optional[str] = None,
    ) -> AsyncGenerator[dict, None]:
        self.captured_messages = messages
        yield {"content": self._content}


class FakeLLMServiceSuccessWithUsage(_FakeLLMServiceBase):
    """Yields a content chunk followed by a usage chunk (some backends do this)."""

    def __init__(
        self,
        content: str = "<summary>OK</summary>",
        prompt_tokens: int = 1234,
        completion_tokens: int = 56,
    ) -> None:
        self.captured_messages: Optional[List[Message]] = None
        self._content = content
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens

    async def chat_stream_async(
        self,
        messages: List[Message],
        max_new_tokens: int,
        temperature: float,
        top_p: float,
        tools: Any = None,
        request_id: Optional[str] = None,
    ) -> AsyncGenerator[dict, None]:
        self.captured_messages = messages
        yield {"content": self._content}
        yield {
            "usage": {
                "prompt_tokens": self._prompt_tokens,
                "completion_tokens": self._completion_tokens,
            }
        }


class FakeLLMServiceFailure(_FakeLLMServiceBase):
    """Raises before yielding any chunk (LLM unreachable / immediate error)."""

    def __init__(self) -> None:
        self.captured_messages: Optional[List[Message]] = None

    async def chat_stream_async(
        self,
        messages: List[Message],
        max_new_tokens: int,
        temperature: float,
        top_p: float,
        tools: Any = None,
        request_id: Optional[str] = None,
    ) -> AsyncGenerator[dict, None]:
        self.captured_messages = messages
        raise RuntimeError("simulated SGLang unreachable")
        yield  # pragma: no cover  -- makes this a generator


class FakeLLMServiceSequenced(_FakeLLMServiceBase):
    def __init__(self, outputs: list[str], cached_tokens: list[int | None] | None = None) -> None:
        self.calls: list[list[Message]] = []
        self._outputs = list(outputs)
        self._cached_tokens = list(cached_tokens or [])

    async def chat_stream_async(
        self,
        messages: List[Message],
        max_new_tokens: int,
        temperature: float,
        top_p: float,
        tools: Any = None,
        request_id: Optional[str] = None,
    ) -> AsyncGenerator[dict, None]:
        self.calls.append(messages)
        output = self._outputs.pop(0)
        yield {"content": output}
        cached_tokens = self._cached_tokens.pop(0) if self._cached_tokens else None
        usage = {"prompt_tokens": 123, "completion_tokens": 45}
        if cached_tokens is not None:
            usage["cached_tokens"] = cached_tokens
        yield {"usage": usage}


@pytest.fixture
def prompts() -> CompactionPromptsConfig:
    return CompactionPromptsConfig(
        no_tools_preamble="PREAMBLE",
        main_prompt="MAIN",
        custom_instructions_header="== USER CUSTOM INSTRUCTIONS ==",
        no_tools_trailer="TRAILER",
        summary_envelope_preface="PREFACE: ",
        summary_envelope_suffix="\nSUFFIX",
        compact_user_instruction="Please summarize the conversation above.",
    )


@pytest.fixture
def compact_config() -> CompactConfig:
    return CompactConfig(
        enabled=True,
        auto_trigger=True,
        max_summary_tokens=2000,
        temperature=0.3,
        top_p=0.9,
        strategy="performance",
        performance_ratio=0.1,
        validation_min_summary_chars=1,
        validation_min_summary_prompt_ratio=0.0,
        validation_require_summary_sections=False,
    )


def _cache_first_config() -> CompactConfig:
    return CompactConfig(
        enabled=True,
        auto_trigger=True,
        max_summary_tokens=2000,
        temperature=0.3,
        top_p=0.9,
        strategy="performance",
        performance_ratio=0.1,
        validation_min_summary_chars=1,
        validation_min_summary_prompt_ratio=0.0,
        validation_require_summary_sections=False,
        prompt_shape="cache_first_tail_with_fallback",
        prompt_control_enabled=True,
    )


async def _collect(agen):
    out = []
    async for ev in agen:
        out.append(ev)
    return out


def _diag_records(caplog) -> List[logging.LogRecord]:
    return [r for r in caplog.records if "[compact-diag]" in r.getMessage()]


def _route_records(caplog) -> List[logging.LogRecord]:
    return [r for r in caplog.records if "[compact-route]" in r.getMessage()]


class CharCounter:
    def count_messages(self, messages, image_count: int = 0) -> int:
        return sum(len(str(getattr(message, "content", ""))) for message in messages)


# ---------------------------------------------------------------------------
# Success path: log line is emitted with all expected fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diag_log_emitted_on_v2_success(
    prompts: CompactionPromptsConfig, compact_config: CompactConfig, caplog
) -> None:
    llm = FakeLLMServiceSuccess(content="<summary>OK</summary>")
    orch = CompactOrchestrator(llm_service=llm, config=compact_config, prompts=prompts)
    body = [
        Message(role="system", content="SYS"),
        Message(role="user", content="hello world"),
    ]
    req = CompactRequest(mode="agent", body_messages=body, trigger="manual")

    with caplog.at_level(logging.INFO, logger="backend.compact.orchestrator"):
        events = await _collect(orch.run_compact(req))

    diag = _diag_records(caplog)
    assert len(diag) == 1, (
        f"expected exactly one [compact-diag] line, got {len(diag)}: "
        f"{[r.getMessage() for r in diag]}"
    )
    msg = diag[0].getMessage()
    # path field reflects the v2 builder (body_messages is set)
    assert "path=v2" in msg
    # SGLang usage is None when the stream doesn't expose it
    assert "prompt_tokens_sglang=None" in msg
    assert "completion_tokens_sglang=None" in msg
    # chars are populated (sum of message content)
    assert "prompt_chars=" in msg
    assert "completion_chars=" in msg
    # timings are integers (ms) when at least one chunk arrived
    assert "prefill_ms=" in msg
    assert "prefill_ms=None" not in msg
    assert "decode_ms=" in msg
    assert "decode_ms=None" not in msg
    assert "total_ms=" in msg
    assert "max_summary_tokens=2000" in msg
    # Compact completed successfully
    assert any(ev.kind == CompactEventKind.COMPLETED for ev in events)


@pytest.mark.asyncio
async def test_diag_log_emitted_on_v1_fallback(
    prompts: CompactionPromptsConfig, compact_config: CompactConfig, caplog
) -> None:
    """v1 path (req.messages, no body_messages) must log path=v1."""
    llm = FakeLLMServiceSuccess(content="<summary>OK</summary>")
    orch = CompactOrchestrator(llm_service=llm, config=compact_config, prompts=prompts)
    body = [Message(role="user", content="legacy body")]
    req = CompactRequest(mode="agent", messages=body, trigger="manual")

    with caplog.at_level(logging.INFO, logger="backend.compact.orchestrator"):
        events = await _collect(orch.run_compact(req))

    diag = _diag_records(caplog)
    assert len(diag) == 1, (
        f"expected exactly one [compact-diag] line, got {len(diag)}: "
        f"{[r.getMessage() for r in diag]}"
    )
    msg = diag[0].getMessage()
    assert "path=v1" in msg
    assert "prefill_ms=None" not in msg
    assert "decode_ms=None" not in msg
    # Compact completed successfully
    assert any(ev.kind == CompactEventKind.COMPLETED for ev in events)


# ---------------------------------------------------------------------------
# SGLang usage chunk: when present, the SGLang token counts are recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diag_log_records_sglang_usage_tokens(
    prompts: CompactionPromptsConfig, compact_config: CompactConfig, caplog
) -> None:
    llm = FakeLLMServiceSuccessWithUsage(
        content="<summary>OK</summary>",
        prompt_tokens=1234,
        completion_tokens=56,
    )
    orch = CompactOrchestrator(llm_service=llm, config=compact_config, prompts=prompts)
    body = [
        Message(role="system", content="SYS"),
        Message(role="user", content="hello world"),
    ]
    req = CompactRequest(mode="agent", body_messages=body, trigger="manual")

    with caplog.at_level(logging.INFO, logger="backend.compact.orchestrator"):
        await _collect(orch.run_compact(req))

    diag = _diag_records(caplog)
    assert len(diag) == 1
    msg = diag[0].getMessage()
    assert "prompt_tokens_sglang=1234" in msg
    assert "completion_tokens_sglang=56" in msg
    # chars are still populated (chars and tokens are independent metrics)
    assert "prompt_chars=" in msg
    assert "completion_chars=" in msg


@pytest.mark.asyncio
async def test_route_log_shows_cache_first_result_and_cached_tokens(
    prompts: CompactionPromptsConfig, caplog
) -> None:
    llm = FakeLLMServiceSequenced(["<summary>OK</summary>"], cached_tokens=[99])
    orch = CompactOrchestrator(llm_service=llm, config=_cache_first_config(), prompts=prompts)
    req = CompactRequest(
        mode="chat",
        body_messages=[Message(role="system", content="COMPACT"), Message(role="user", content="history")],
        cache_first_body_messages=[Message(role="system", content="ACTIVE"), Message(role="user", content="history")],
        reference_messages=[Message(role="system", content="ACTIVE"), Message(role="user", content="history")],
        trigger="manual",
    )

    with caplog.at_level(logging.INFO, logger="backend.compact.orchestrator"):
        events = await _collect(orch.run_compact(req))

    assert any(ev.kind == CompactEventKind.COMPLETED for ev in events)
    route_messages = [r.getMessage() for r in _route_records(caplog)]
    assert any("plan" in msg and "route=cache_first_tail" in msg for msg in route_messages)
    assert any(
        "result" in msg
        and "selected_shape=cache_first_tail" in msg
        and "fallback_used=False" in msg
        and "cached_tokens_sglang=99" in msg
        and "prefix_cache_hit=yes" in msg
        for msg in route_messages
    )


@pytest.mark.asyncio
async def test_route_log_shows_fallback_reason_and_final_reliable_result(
    prompts: CompactionPromptsConfig, caplog
) -> None:
    llm = FakeLLMServiceSequenced(["bad", "<summary>OK long enough summary text</summary>"], cached_tokens=[0, 3])
    compact_cfg = _cache_first_config()
    compact_cfg.reliable_fallback_enabled = True
    compact_cfg.validation_min_summary_chars = 20
    orch = CompactOrchestrator(llm_service=llm, config=compact_cfg, prompts=prompts)
    req = CompactRequest(
        mode="chat",
        body_messages=[Message(role="system", content="COMPACT"), Message(role="user", content="history")],
        cache_first_body_messages=[Message(role="system", content="ACTIVE"), Message(role="user", content="history")],
        reference_messages=[Message(role="system", content="ACTIVE"), Message(role="user", content="history")],
        trigger="manual",
    )

    with caplog.at_level(logging.INFO, logger="backend.compact.orchestrator"):
        events = await _collect(orch.run_compact(req))

    assert any(ev.kind == CompactEventKind.COMPLETED for ev in events)
    route_messages = [r.getMessage() for r in _route_records(caplog)]
    assert any(
        "fallback" in msg
        and "from_shape=cache_first_tail" in msg
        and "to_shape=reliable_position1" in msg
        and "reason=too_short" in msg
        for msg in route_messages
    )
    assert any(
        "result" in msg
        and "selected_shape=reliable_position1" in msg
        and "fallback_used=True" in msg
        and "cached_tokens_sglang=3" in msg
        and "prefix_cache_hit=yes" in msg
        for msg in route_messages
    )


@pytest.mark.asyncio
async def test_route_log_shows_over_budget_cache_first_is_still_attempted(
    prompts: CompactionPromptsConfig, caplog, monkeypatch
) -> None:
    from backend.compact import source_budget as source_budget_module

    monkeypatch.setattr(source_budget_module.TokenCounter, "get", staticmethod(lambda: CharCounter()))

    llm = FakeLLMServiceSequenced(["<summary>OK</summary>"], cached_tokens=[None])
    compact_cfg = _cache_first_config()
    compact_cfg.source_token_budget = 20
    orch = CompactOrchestrator(llm_service=llm, config=compact_cfg, prompts=prompts)
    req = CompactRequest(
        mode="agent",
        body_messages=[Message(role="system", content="COMPACT"), Message(role="user", content="recent")],
        cache_first_body_messages=[
            Message(role="system", content="ACTIVE"),
            Message(role="user", content="x" * 80),
        ],
        reference_messages=[
            Message(role="system", content="ACTIVE"),
            Message(role="user", content="x" * 80),
        ],
        trigger="manual",
    )

    with caplog.at_level(logging.INFO, logger="backend.compact.orchestrator"):
        events = await _collect(orch.run_compact(req))

    assert any(ev.kind == CompactEventKind.COMPLETED for ev in events)
    route_messages = [r.getMessage() for r in _route_records(caplog)]
    assert any("plan" in msg and "route=cache_first_tail" in msg for msg in route_messages)
    assert any(
        "result" in msg
        and "selected_shape=cache_first_tail" in msg
        and "fallback_used=False" in msg
        and "cache_first_skipped_reason=None" in msg
        for msg in route_messages
    )


# ---------------------------------------------------------------------------
# Failure path: no [compact-diag] line is emitted (control flow short-circuits)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diag_log_not_emitted_on_llm_failure(
    prompts: CompactionPromptsConfig, compact_config: CompactConfig, caplog
) -> None:
    llm = FakeLLMServiceFailure()
    orch = CompactOrchestrator(llm_service=llm, config=compact_config, prompts=prompts)
    body = [
        Message(role="system", content="SYS"),
        Message(role="user", content="hello world"),
    ]
    req = CompactRequest(mode="agent", body_messages=body, trigger="manual")

    with caplog.at_level(logging.INFO, logger="backend.compact.orchestrator"):
        events = await _collect(orch.run_compact(req))

    # On failure, the orchestrator yields compact_failed and returns BEFORE
    # reaching the success-path log line.
    assert any(ev.kind == CompactEventKind.FAILED for ev in events)
    diag = _diag_records(caplog)
    assert diag == [], (
        f"no [compact-diag] line should be emitted on failure, got: "
        f"{[r.getMessage() for r in diag]}"
    )
