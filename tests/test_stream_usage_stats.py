"""Tests for authoritative stream token usage (stream_options injection + capture).

覆盖三条链路：
1. Provider 请求注入：SGLang（include_usage + continuous_usage_stats）/ vLLM
   （仅 include_usage），且注入优先于 extra_body、受 inference.stream_usage_stats 开关控制。
2. LLMService.chat(stream=True) 捕获后端返回的权威 usage chunk（last-write-wins），
   get_stream_usage 优先返回 source="server" 的统计，无 usage chunk 时回退到
   本地估算（source="estimated"）。
3. record_stream_usage：非流式回退调用可把权威 usage 回填到流式统计快照。
"""

from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import get_config
from backend.llm_service import LLMService, Message, SGLangProvider, VLLMProvider


def _make_provider(provider_class):
    config = get_config()
    return provider_class(config)


def _capture_stream_request(provider, **generate_kwargs):
    """调用 provider.generate(stream=True) 并捕获传给 _stream_generate 的 request_data。"""
    captured = []

    def recorder(request_data, *args, **kwargs):
        captured.append(request_data)
        return iter(())

    with patch.object(provider, "_stream_generate", side_effect=recorder):
        provider.generate(
            messages=[Message(role="user", content="hello")],
            stream=True,
            **generate_kwargs,
        )
    return captured[0]


def _capture_sync_request(provider, **generate_kwargs):
    """调用 provider.generate(stream=False) 并捕获传给 _sync_generate 的 request_data。"""
    captured = []

    def recorder(request_data, *args, **kwargs):
        captured.append(request_data)
        return MagicMock()

    with patch.object(provider, "_sync_generate", side_effect=recorder):
        provider.generate(
            messages=[Message(role="user", content="hello")],
            stream=False,
            **generate_kwargs,
        )
    return captured[0]


def test_sglang_provider_stream_injects_full_stream_options():
    provider = _make_provider(SGLangProvider)

    request_data = _capture_stream_request(provider)

    assert request_data["stream_options"] == {
        "include_usage": True,
        "continuous_usage_stats": True,
    }


def test_sglang_provider_non_stream_does_not_inject_stream_options():
    provider = _make_provider(SGLangProvider)

    request_data = _capture_sync_request(provider)

    assert "stream_options" not in request_data


def test_sglang_provider_extra_body_cannot_override_stream_options():
    provider = _make_provider(SGLangProvider)

    request_data = _capture_stream_request(
        provider, extra_body={"stream_options": {"include_usage": False}}
    )

    # 注入发生在 extra_body 合并之后，统计字段不允许被调用方覆盖
    assert request_data["stream_options"] == {
        "include_usage": True,
        "continuous_usage_stats": True,
    }


def test_vllm_provider_stream_injects_include_usage_only():
    provider = _make_provider(VLLMProvider)

    request_data = _capture_stream_request(provider)

    # vLLM 的 StreamOptions 不支持 continuous_usage_stats
    assert request_data["stream_options"] == {"include_usage": True}


def test_vllm_provider_non_stream_does_not_inject_stream_options():
    provider = _make_provider(VLLMProvider)

    request_data = _capture_sync_request(provider)

    assert "stream_options" not in request_data


@pytest.mark.parametrize("provider_class", [SGLangProvider, VLLMProvider])
def test_stream_options_injection_respects_config_flag(provider_class):
    config = get_config()
    original = config.inference.stream_usage_stats
    config.inference.stream_usage_stats = False
    try:
        provider = provider_class(config)
        assert "stream_options" not in _capture_stream_request(provider)
        assert "stream_options" not in _capture_sync_request(provider)
    finally:
        config.inference.stream_usage_stats = original


class _LoadedProvider:
    def is_loaded(self):
        return True


def test_chat_stream_captures_server_usage_and_pops_entry():
    # continuous_usage_stats 的逐 chunk usage 为累计值，最后一块为最终统计
    chunks = [
        {"content": "He", "reasoning": None},
        {
            "content": "",
            "reasoning": None,
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 15,
                "total_tokens": 135,
                "prompt_tokens_details": {"image_tokens": 256},
            },
        },
        {
            "content": "",
            "reasoning": None,
            "finish_reason": "stop",
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "prompt_tokens_details": {"image_tokens": 256, "cached_tokens": 64},
            },
        },
    ]

    class StreamingProvider(_LoadedProvider):
        def generate(self, **kwargs):
            return iter(chunks)

    service = LLMService(config=get_config())
    service._provider = StreamingProvider()

    messages = [Message(role="user", content=[{"type": "text", "text": "hi"}])]
    stream = service.chat(
        messages, stream=True, request_id="u1", stream_usage_id="u1"
    )
    collected = list(stream)

    # usage chunk 原样透传（chat 模式消费者只读 content/reasoning，自动忽略）
    assert len(collected) == 3

    usage = service.get_stream_usage("Hello", request_id="u1")
    assert usage == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "image_tokens": 256,
        "audio_tokens": 0,
        "video_tokens": 0,
        "cached_tokens": 64,
        "source": "server",
    }
    # one-shot 语义：读取后条目被弹出
    assert service.get_stream_usage("Hello", request_id="u1") is None


def test_get_stream_usage_falls_back_to_estimation_without_usage_chunk(monkeypatch):
    class Counter:
        def count_messages(self, messages, image_count=0):
            return 10

        def count_text(self, text):
            return 3

    class StreamingProvider(_LoadedProvider):
        def generate(self, **kwargs):
            return iter([{"content": "ok", "reasoning": None}])

    service = LLMService(config=get_config())
    service._provider = StreamingProvider()
    monkeypatch.setattr(
        "backend.compact.tokenizer.TokenCounter.get", staticmethod(lambda: Counter())
    )

    messages = [Message(role="user", content=[{"type": "text", "text": "hello"}])]
    stream = service.chat(
        messages, stream=True, request_id="u2", stream_usage_id="u2"
    )
    list(stream)

    usage = service.get_stream_usage("abc", request_id="u2")
    assert usage == {
        "prompt_tokens": 10,
        "completion_tokens": 3,
        "total_tokens": 13,
        "image_tokens": 0,
        "audio_tokens": 0,
        "video_tokens": 0,
        "cached_tokens": 0,
        "source": "estimated",
    }


def test_record_stream_usage_stores_server_usage_for_fallback_calls():
    class StreamingProvider(_LoadedProvider):
        def generate(self, **kwargs):
            return iter([{"content": "x", "reasoning": None}])

    service = LLMService(config=get_config())
    service._provider = StreamingProvider()

    messages = [Message(role="user", content="hello")]
    stream = service.chat(
        messages, stream=True, request_id="u3", stream_usage_id="u3"
    )
    list(stream)

    # 未知 request_id / 非 dict usage 均为 no-op
    service.record_stream_usage("unknown", {"prompt_tokens": 1})
    service.record_stream_usage("u3", "not-a-dict")
    service.record_stream_usage(None, {"prompt_tokens": 1})
    assert "u3" in service._stream_usage_requests
    assert service._stream_usage_requests["u3"]["usage"] is None

    service.record_stream_usage(
        "u3",
        {
            "prompt_tokens": 42,
            "completion_tokens": 7,
            "total_tokens": 49,
            "prompt_tokens_details": {"image_tokens": 128},
        },
    )
    usage = service.get_stream_usage("x", request_id="u3")
    assert usage == {
        "prompt_tokens": 42,
        "completion_tokens": 7,
        "total_tokens": 49,
        "image_tokens": 128,
        "audio_tokens": 0,
        "video_tokens": 0,
        "cached_tokens": 0,
        "source": "server",
    }
