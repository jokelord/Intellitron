"""Tests for the new CompactRequest.body_messages field.

Per spec §5.1:
  - body_messages is Optional[List[Message]], defaults to None.
  - messages (v1) is preserved for 1 release, defaults to None.
  - body_messages takes precedence over messages in the orchestrator (covered in Task 4).
"""
from __future__ import annotations

from backend.compact.types import CompactRequest, CompactEventKind, Mode, Trigger
from backend.llm_service import Message


def test_compact_request_body_messages_field_exists() -> None:
    req = CompactRequest(
        mode="agent",
        body_messages=[Message(role="system", content="S")],
    )
    assert req.body_messages is not None
    assert len(req.body_messages) == 1
    assert req.body_messages[0].role == "system"


def test_compact_request_v1_messages_field_still_present() -> None:
    """v1 path must keep working for 1 release."""
    req = CompactRequest(
        mode="agent",
        messages=[Message(role="user", content="legacy body")],
    )
    assert req.messages is not None
    assert len(req.messages) == 1


def test_compact_request_defaults_both_to_none() -> None:
    req = CompactRequest(mode="chat")
    assert req.body_messages is None
    assert req.messages is None
    assert req.session_id is None
    assert req.trigger == "manual"
    assert req.custom_instructions is None
