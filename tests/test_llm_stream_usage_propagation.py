from backend.llm_service import _openai_stream_payload_to_chunk


def test_stream_payload_to_chunk_preserves_usage_only_payload():
    chunk = _openai_stream_payload_to_chunk({
        "choices": [],
        "usage": {"prompt_tokens": 123, "completion_tokens": 4, "cached_tokens": 100},
    })

    assert chunk == {
        "content": None,
        "reasoning": None,
        "tool_calls": None,
        "usage": {"prompt_tokens": 123, "completion_tokens": 4, "cached_tokens": 100},
    }


def test_stream_payload_to_chunk_preserves_content_and_usage_together():
    chunk = _openai_stream_payload_to_chunk({
        "choices": [{"delta": {"content": "hello"}}],
        "usage": {"prompt_tokens": 10},
    })

    assert chunk["content"] == "hello"
    assert chunk["usage"] == {"prompt_tokens": 10}


def test_stream_payload_to_chunk_returns_none_for_empty_payload():
    assert _openai_stream_payload_to_chunk({"choices": [{"delta": {}}]}) is None


def test_stream_payload_to_chunk_preserves_finish_reason_only_payload():
    chunk = _openai_stream_payload_to_chunk({
        "choices": [{"delta": {}, "finish_reason": "length"}],
    })

    assert chunk == {
        "content": None,
        "reasoning": None,
        "tool_calls": None,
        "finish_reason": "length",
    }


def test_stream_payload_to_chunk_accepts_vllm_reasoning_alias():
    chunk = _openai_stream_payload_to_chunk({
        "choices": [{"delta": {"reasoning": "thinking"}}],
    })

    assert chunk == {
        "content": None,
        "reasoning": "thinking",
        "tool_calls": None,
    }
