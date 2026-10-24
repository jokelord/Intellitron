from backend.compact.prompt_diagnostics import (
    compare_prompt_snapshots,
    extract_cached_tokens,
    make_prompt_snapshot,
)
from backend.llm_service import Message


def test_make_prompt_snapshot_is_stable_for_same_messages():
    messages = [Message(role="system", content="SYS"), Message(role="user", content="hello")]
    a = make_prompt_snapshot("normal_agent", messages)
    b = make_prompt_snapshot("normal_agent", messages)

    assert a.prompt_shape == "normal_agent"
    assert a.message_count == 2
    assert a.prompt_chars == b.prompt_chars
    assert a.content_hash == b.content_hash


def test_make_prompt_snapshot_redacts_and_caps_large_image_payloads():
    data_url = "data:image/png;base64," + ("A" * 10000)
    snapshot = make_prompt_snapshot(
        "vision_agent",
        [Message(role="user", content=[{"type": "image_url", "image_url": {"url": data_url}}])],
    )

    assert snapshot.prompt_chars > len(snapshot.canonical_text)
    assert "data:image/png;base64," not in snapshot.canonical_text
    assert "<redacted-image-data>" in snapshot.canonical_text
    assert len(snapshot.canonical_text) <= 4096


def test_compare_prompt_snapshots_reports_first_divergence():
    reference = make_prompt_snapshot(
        "normal_agent",
        [Message(role="system", content="same"), Message(role="user", content="old")],
    )
    candidate = make_prompt_snapshot(
        "compact_candidate",
        [Message(role="system", content="same"), Message(role="user", content="new")],
    )

    comparison = compare_prompt_snapshots(reference, candidate)

    assert comparison.reference_shape == "normal_agent"
    assert comparison.candidate_shape == "compact_candidate"
    assert comparison.common_prefix_chars > 0
    assert comparison.first_divergence_char is not None
    assert comparison.same_hash is False


def test_extract_cached_tokens_handles_common_usage_shapes():
    assert extract_cached_tokens({"cached_tokens": 12}) == 12
    assert extract_cached_tokens({"prompt_tokens_details": {"cached_tokens": 34}}) == 34
    assert extract_cached_tokens({"cache_read_input_tokens": 56}) == 56
    assert extract_cached_tokens({"prompt_tokens": 100}) is None
