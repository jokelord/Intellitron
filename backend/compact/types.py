"""Compact request and event types shared by the orchestrator and API."""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from typing import Any, List, Literal, Optional

from ..llm_service import Message


Mode = Literal["chat", "agent", "cycle"]
Trigger = Literal["manual", "auto"]


@dataclass
class CompactRequest:
    mode: Mode
    body_messages: Optional[List[Message]] = None  # v2: 含 system 的完整 messages 列表
    messages: Optional[List[Any]] = None          # v1: body-only 列表; deprecated, 1 release
    cache_first_body_messages: Optional[List[Message]] = None
    reference_messages: Optional[List[Message]] = None
    session_id: Optional[str] = None
    trigger: Trigger = "manual"
    custom_instructions: Optional[str] = None
    tools: Optional[list[dict]] = None


class CompactEventKind(str, enum.Enum):
    STARTED = "compact_started"
    DELTA = "compact_delta"
    COMPLETED = "compact_completed"
    FAILED = "compact_failed"


@dataclass
class CompactEvent:
    kind: CompactEventKind
    summary_message_id: str
    text: Optional[str] = None
    partial: bool = False
    metadata: dict = field(default_factory=dict)

    @staticmethod
    def new_summary_id() -> str:
        return f"compact-{uuid.uuid4()}"

    @staticmethod
    def started(
        summary_message_id: str,
        trigger: Trigger,
        mode: Mode,
        original_tokens: Optional[int] = None,
        threshold: Optional[int] = None,
        strategy: Optional[str] = None,
    ) -> "CompactEvent":
        meta: dict = {"trigger": trigger, "mode": mode}
        if original_tokens is not None:
            meta["original_tokens"] = original_tokens
        if threshold is not None:
            meta["threshold"] = threshold
        if strategy is not None:
            meta["strategy"] = strategy
        return CompactEvent(
            kind=CompactEventKind.STARTED,
            summary_message_id=summary_message_id,
            metadata=meta,
        )

    @staticmethod
    def delta(summary_message_id: str, text: str) -> "CompactEvent":
        return CompactEvent(
            kind=CompactEventKind.DELTA,
            summary_message_id=summary_message_id,
            text=text,
            partial=True,
        )

    @staticmethod
    def completed(
        summary_message_id: str,
        text: str,
        metadata: Optional[dict] = None,
    ) -> "CompactEvent":
        return CompactEvent(
            kind=CompactEventKind.COMPLETED,
            summary_message_id=summary_message_id,
            text=text,
            partial=False,
            metadata=metadata or {},
        )

    @staticmethod
    def failed(
        summary_message_id: str, reason: str, error: Optional[str] = None
    ) -> "CompactEvent":
        meta: dict = {"reason": reason}
        if error is not None:
            meta["error"] = error
        return CompactEvent(
            kind=CompactEventKind.FAILED,
            summary_message_id=summary_message_id,
            metadata=meta,
        )
