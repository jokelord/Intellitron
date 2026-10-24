"""Tests for CompactOrchestrator v2 path (body_messages) + v1 fallback (with deprecation warning).

Per spec §5.1, §5.2, §6.1 I5:
  - When req.body_messages is provided (non-None), use build_compact_messages_v2.
  - When req.body_messages is None and req.messages is provided, use v1 build_compact_messages
    and log a deprecation warning.
  - In both cases, the resulting messages are streamed to LLMService.chat_stream_async.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, List, Optional

import pytest

from backend.compact.orchestrator import CompactOrchestrator
from backend.compact.types import CompactEvent, CompactEventKind, CompactRequest
from backend.config import CompactConfig, CompactionPromptsConfig
from backend.llm_service import Message


class FakeLLMService:
    """Captures the messages passed to chat_stream_async."""

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


async def _collect(agen):
    out = []
    async for ev in agen:
        out.append(ev)
    return out


@pytest.mark.asyncio
async def test_v2_path_uses_body_messages_directly(
    prompts: CompactionPromptsConfig, compact_config: CompactConfig
) -> None:
    llm = FakeLLMService()
    orch = CompactOrchestrator(llm_service=llm, config=compact_config, prompts=prompts)
    body = [
        Message(role="system", content="SYS"),
        Message(role="user", content="A 说：xxx"),
        Message(role="assistant", content="回：yyy"),
    ]
    req = CompactRequest(mode="agent", body_messages=body, trigger="manual")
    events = await _collect(orch.run_compact(req))
    # LLM received: [compact system, user<compact-instr>, A 说, 回 yyy].
    # The reliable path intentionally replaces the active system prompt.
    assert llm.captured_messages is not None
    assert len(llm.captured_messages) == len(body) + 1
    assert llm.captured_messages[0].role == "system"
    assert "PREAMBLE" in llm.captured_messages[0].content
    assert "SYS" not in llm.captured_messages[0].content
    # Second message is the compact instruction (position-1 amendment).
    assert llm.captured_messages[1].role == "user"
    assert "summarize the conversation" in llm.captured_messages[1].content
    # Remaining body messages are byte-equal to body[1:].
    for i, original in enumerate(body[1:]):
        assert llm.captured_messages[2 + i].role == original.role
        assert llm.captured_messages[2 + i].content == original.content
    # Compact completed successfully
    assert any(ev.kind == CompactEventKind.COMPLETED for ev in events)


@pytest.mark.asyncio
async def test_v1_path_logs_deprecation_warning(
    prompts: CompactionPromptsConfig, compact_config: CompactConfig, caplog
) -> None:
    llm = FakeLLMService()
    orch = CompactOrchestrator(llm_service=llm, config=compact_config, prompts=prompts)
    # v1 caller uses req.messages (no body_messages)
    body = [Message(role="user", content="legacy body")]
    req = CompactRequest(mode="agent", messages=body, trigger="manual")
    with caplog.at_level(logging.WARNING, logger="backend.compact.orchestrator"):
        events = await _collect(orch.run_compact(req))
    # LLM received a 2-message [system, user<legacy body>] shape (v1)
    assert llm.captured_messages is not None
    assert len(llm.captured_messages) == 2
    assert llm.captured_messages[0].role == "system"
    assert "PREAMBLE" in llm.captured_messages[0].content
    assert "MAIN" in llm.captured_messages[0].content
    # Anti-collision: guards against "PREAMBLE" + "MAIN" being joined into "PREMAINT".
    # The positive substring checks above would NOT catch this concatenation regression.
    assert "PREMAINT" not in llm.captured_messages[0].content
    assert llm.captured_messages[1].role == "user"
    assert "legacy body" in llm.captured_messages[1].content
    # Deprecation warning was logged
    assert any("deprecated" in rec.message.lower() for rec in caplog.records), (
        f"expected deprecation warning, got: {[r.message for r in caplog.records]}"
    )
    # Compact completed
    assert any(ev.kind == CompactEventKind.COMPLETED for ev in events)
