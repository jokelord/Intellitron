"""Tests for CompactRequest and CompactEvent types."""
from backend.compact.types import (
    CompactEvent,
    CompactEventKind,
    CompactRequest,
)


def test_compact_request_defaults():
    req = CompactRequest(mode="chat")
    assert req.mode == "chat"
    assert req.messages is None
    assert req.session_id is None
    assert req.trigger == "manual"
    assert req.custom_instructions is None


def test_compact_request_full_construction():
    req = CompactRequest(
        mode="agent",
        messages=[{"role": "user", "content": "hi"}],
        session_id="s1",
        trigger="auto",
        custom_instructions="focus on code",
    )
    assert req.mode == "agent"
    assert req.session_id == "s1"
    assert req.trigger == "auto"
    assert req.custom_instructions == "focus on code"


def test_compact_event_started_factory():
    ev = CompactEvent.started(
        summary_message_id="compact-x",
        trigger="auto",
        mode="agent",
        original_tokens=51234,
        threshold=50000,
        strategy="performance",
    )
    assert ev.kind == CompactEventKind.STARTED
    assert ev.summary_message_id == "compact-x"
    assert ev.metadata["trigger"] == "auto"
    assert ev.metadata["mode"] == "agent"
    assert ev.metadata["original_tokens"] == 51234
    assert ev.metadata["threshold"] == 50000
    assert ev.metadata["strategy"] == "performance"


def test_compact_event_delta_factory():
    ev = CompactEvent.delta("compact-x", "Hello world")
    assert ev.kind == CompactEventKind.DELTA
    assert ev.text == "Hello world"
    assert ev.partial is True


def test_compact_event_completed_factory():
    ev = CompactEvent.completed("compact-x", "Summary: ...", metadata={"summary_tokens": 100})
    assert ev.kind == CompactEventKind.COMPLETED
    assert ev.text == "Summary: ..."
    assert ev.partial is False
    assert ev.metadata["summary_tokens"] == 100


def test_compact_event_failed_factory():
    ev = CompactEvent.failed("compact-x", "llm_unreachable", "Connection refused")
    assert ev.kind == CompactEventKind.FAILED
    assert ev.metadata["reason"] == "llm_unreachable"
    assert ev.metadata["error"] == "Connection refused"


def test_compact_event_new_summary_id_unique():
    a = CompactEvent.new_summary_id()
    b = CompactEvent.new_summary_id()
    assert a != b
    assert a.startswith("compact-")


def test_compact_request_cache_first_fields_default_to_none():
    req = CompactRequest(mode="agent")
    assert req.body_messages is None
    assert req.cache_first_body_messages is None
    assert req.reference_messages is None
