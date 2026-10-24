"""Tests for backend/provider selection overrides."""

from pathlib import Path
import sys
import types
import json
import base64
import io
import asyncio
import threading

import os
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import get_config, reload_config
from backend.llm_service import (
    LLMService,
    Message,
    OpenAICompatibleProxyProvider,
    SGLangProvider,
    VLLMProvider,
    abort_request_by_id,
    abort_request,
    active_upstream_request_ids,
    is_request_aborted,
    resolve_backend_context_length,
    resolve_effective_max_tokens,
    register_request,
    register_upstream_stream,
    unregister_request,
    unregister_upstream_stream,
)


def test_initialize_respects_forced_vllm_backend():
    config = get_config()

    with patch.dict(os.environ, {"LLM_BACKEND": "vllm"}, clear=False):
        with (
            patch("backend.llm_service.SGLangProvider") as sglang_cls,
            patch("backend.llm_service.VLLMProvider") as vllm_cls,
            patch("backend.llm_service.OpenAICompatibleProxyProvider") as container_cls,
        ):
            vllm_provider = MagicMock()
            vllm_provider.load_model.return_value = True
            vllm_cls.return_value = vllm_provider

            service = LLMService(config=config)

            assert service.initialize() is True
            sglang_cls.assert_not_called()
            vllm_cls.assert_called_once_with(config)
            container_cls.assert_not_called()
            assert service._active_backend == "vllm"


def test_sglang_local_health_client_ignores_environment_proxies():
    config = get_config()
    provider = SGLangProvider(config)
    models_response = MagicMock(status_code=200)
    models_response.json.return_value = {"data": [{"id": "local-model"}]}
    health_response = MagicMock(status_code=200)
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get.side_effect = [health_response, models_response]

    with patch("httpx.Client", return_value=client) as client_class:
        assert provider.load_model() is True

    client_class.assert_called_once_with(timeout=10, trust_env=False)


def test_openai_proxy_provider_accepts_tools_and_request_id():
    config = get_config()
    provider = OpenAICompatibleProxyProvider(config)

    with patch.object(
        provider, "_sync_generate", return_value=MagicMock()
    ) as sync_generate:
        provider.generate(
            messages=[Message(role="user", content="hello")],
            tools=[{"type": "function", "function": {"name": "noop"}}],
            request_id="req-123",
            stream=False,
        )

    request_data = sync_generate.call_args.args[0]
    assert request_data["request_id"] == "req-123"
    assert request_data["tools"] == [{"type": "function", "function": {"name": "noop"}}]


@pytest.mark.parametrize("provider_class", [SGLangProvider, VLLMProvider, OpenAICompatibleProxyProvider])
@pytest.mark.parametrize("preserve_prompt_shape", [False, True])
def test_service_centralizes_runtime_context_for_all_providers(
    provider_class, preserve_prompt_shape
):
    config = get_config()
    provider = provider_class(config)
    service = LLMService(config=config)
    service._provider = provider
    messages = [Message(role="system", content="SYSTEM"), Message(role="user", content="hello")]

    with patch.object(provider, "is_loaded", return_value=True), patch.object(
        provider, "_sync_generate", return_value=MagicMock()
    ) as sync_generate:
        service.chat(
            messages,
            runtime_context="RUNTIME",
            preserve_prompt_shape=preserve_prompt_shape,
        )

    request_messages = sync_generate.call_args.args[0]["messages"]
    expected = "SYSTEM" if preserve_prompt_shape else "SYSTEM\n\nRUNTIME"
    assert request_messages[0]["content"] == expected
    assert messages[0].content == "SYSTEM"


def test_sglang_provider_accepts_request_id_for_abortable_streams():
    config = get_config()
    provider = SGLangProvider(config)

    with patch.object(provider, "_stream_generate", return_value=iter(())) as stream_generate:
        list(
            provider.generate(
                messages=[Message(role="user", content="hello")],
                request_id="req-123",
                stream=True,
            )
        )

    request_data = stream_generate.call_args.args[0]
    assert request_data["request_id"] == "req-123"


def test_sglang_provider_preserves_zero_temperature_and_merges_extra_body():
    config = get_config()
    config.inference.top_k = 37
    config.inference.repetition_penalty = 1.0
    provider = SGLangProvider(config)

    with patch.object(provider, "_sync_generate", return_value=MagicMock()) as sync_generate:
        provider.generate(
            messages=[Message(role="user", content="hello")],
            temperature=0.0,
            max_new_tokens=150,
            stream=False,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

    request_data = sync_generate.call_args.args[0]
    assert request_data["temperature"] == 0.0
    assert request_data["top_p"] == config.inference.top_p
    assert request_data["top_k"] == 37
    assert request_data["repetition_penalty"] == 1.0
    assert request_data["max_new_tokens"] == 150
    assert request_data["chat_template_kwargs"] == {"enable_thinking": False}


def test_vllm_provider_preserves_zero_temperature_and_merges_extra_body():
    config = get_config()
    config.inference.top_k = 37
    config.inference.repetition_penalty = 1.0
    provider = VLLMProvider(config)

    with patch.object(provider, "_sync_generate", return_value=MagicMock()) as sync_generate:
        provider.generate(
            messages=[Message(role="user", content="hello")],
            temperature=0.0,
            max_new_tokens=150,
            stream=False,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

    request_data = sync_generate.call_args.args[0]
    assert request_data["temperature"] == 0.0
    assert request_data["top_p"] == config.inference.top_p
    assert request_data["top_k"] == 37
    assert request_data["repetition_penalty"] == 1.0
    assert request_data["max_tokens"] == 150
    assert request_data["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize(
    ("provider_class", "max_token_key", "other_max_token_key"),
    [
        (SGLangProvider, "max_new_tokens", "max_tokens"),
        (VLLMProvider, "max_tokens", "max_new_tokens"),
    ],
)
def test_provider_preserves_zero_temperature_and_zero_sampling_defaults(
    provider_class, max_token_key, other_max_token_key
):
    config = get_config()
    config.inference.top_p = 0.73
    config.inference.top_k = 0
    config.inference.repetition_penalty = 1.0
    provider = provider_class(config)

    with patch.object(provider, "_sync_generate", return_value=MagicMock()) as sync_generate:
        provider.generate(
            messages=[Message(role="user", content="hello")],
            temperature=0.0,
            max_new_tokens=150,
            stream=False,
        )

    request_data = sync_generate.call_args.args[0]
    assert request_data["temperature"] == 0.0
    assert request_data["top_p"] == 0.73
    assert request_data["top_k"] == 0
    assert request_data["repetition_penalty"] == 1.0
    assert request_data[max_token_key] == 150
    assert other_max_token_key not in request_data


@pytest.mark.parametrize(
    ("provider_class", "max_token_key", "other_max_token_key"),
    [
        (SGLangProvider, "max_new_tokens", "max_tokens"),
        (VLLMProvider, "max_tokens", "max_new_tokens"),
    ],
)
def test_provider_preserves_zero_temperature_and_allows_extra_body_sampling_overrides(
    provider_class, max_token_key, other_max_token_key
):
    config = get_config()
    config.inference.temperature = 0.61
    config.inference.top_p = 0.73
    config.inference.top_k = 37
    config.inference.repetition_penalty = 1.2
    config.inference.max_tokens = 150
    config.inference.max_new_tokens = 150
    provider = provider_class(config)
    extra_body = {
        "temperature": 0.0,
        "top_p": 0.25,
        "top_k": 0,
        "repetition_penalty": 1.0,
    }

    with patch.object(provider, "_sync_generate", return_value=MagicMock()) as sync_generate:
        provider.generate(
            messages=[Message(role="user", content="hello")],
            stream=False,
            extra_body=extra_body,
        )

    request_data = sync_generate.call_args.args[0]
    assert request_data["temperature"] == 0.0
    assert request_data["top_p"] == 0.25
    assert request_data["top_k"] == 0
    assert request_data["repetition_penalty"] == 1.0
    assert request_data[max_token_key] == 150
    assert other_max_token_key not in request_data
    assert request_data["model"] == provider._model_name
    assert request_data["messages"] == [{"role": "user", "content": "hello"}]
    assert request_data["stream"] is False


@pytest.mark.parametrize("provider_class", [SGLangProvider, VLLMProvider])
def test_provider_does_not_mutate_image_messages(provider_class):
    from PIL import Image

    config = get_config()
    config.model.max_image_dimension = 8
    provider = provider_class(config)

    def image_url(color):
        image = Image.new("RGB", (16, 16), color)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()

    historical_image_url = image_url("red")
    latest_image_url = image_url("blue")
    messages = [
        Message(
            role="user",
            content=[
                {"type": "text", "text": "historical image"},
                {"type": "image_url", "image_url": {"url": historical_image_url}},
            ],
        ),
        Message(role="assistant", content="I saw it."),
        Message(
            role="user",
            content=[
                {"type": "text", "text": "latest image"},
                {"type": "image_url", "image_url": {"url": latest_image_url}},
            ],
        ),
    ]

    with patch.object(provider, "_sync_generate", return_value=MagicMock()) as sync_generate:
        provider.generate(messages=messages, stream=False)

    request_messages = sync_generate.call_args.args[0]["messages"]
    assert messages[0].content[1]["image_url"]["url"] == historical_image_url
    assert messages[2].content[1]["image_url"]["url"] == latest_image_url
    assert request_messages[0]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": historical_image_url},
    }
    assert request_messages[2]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": latest_image_url},
    }


def test_service_preserve_prompt_shape_skips_image_preparation(monkeypatch):
    from PIL import Image

    class RecordingProvider:
        def __init__(self):
            self.calls = []

        def is_loaded(self):
            return True

        def generate(self, messages, preserve_prompt_shape=False, **kwargs):
            self.calls.append(
                {
                    "messages": messages,
                    "preserve_prompt_shape": preserve_prompt_shape,
                }
            )
            return MagicMock()

    config = get_config()
    monkeypatch.setattr(config.model, "max_image_dimension", 8)
    source_image = Image.new("RGB", (16, 16), "red")
    buffer = io.BytesIO()
    source_image.save(buffer, format="JPEG")
    original_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
    messages = [
        Message(
            role="user",
            content=[{"type": "image_url", "image_url": {"url": original_url}}],
        )
    ]
    provider = RecordingProvider()
    service = LLMService(config=config)
    service._provider = provider

    service.chat(messages, preserve_prompt_shape=False)
    resized_url = provider.calls[0]["messages"][0].content[0]["image_url"]["url"]

    service.chat(messages, preserve_prompt_shape=True)
    preserved_url = provider.calls[1]["messages"][0].content[0]["image_url"]["url"]

    assert resized_url != original_url
    assert messages[0].content[0]["image_url"]["url"] == original_url
    assert provider.calls[0]["preserve_prompt_shape"] is False
    assert preserved_url == original_url
    assert provider.calls[1]["preserve_prompt_shape"] is True


@pytest.mark.parametrize(
    "provider_class", [SGLangProvider, VLLMProvider, OpenAICompatibleProxyProvider]
)
def test_providers_never_inject_runtime_context(provider_class):
    config = get_config()
    provider = provider_class(config)

    with patch.object(provider, "_sync_generate", return_value=MagicMock()) as sync_generate:
        provider.generate(
            messages=[Message(role="system", content="BASE")],
            runtime_context="RUNTIME",
            preserve_prompt_shape=True,
            stream=False,
        )

    assert sync_generate.call_args.args[0]["messages"] == [
        {"role": "system", "content": "BASE"}
    ]

    with patch.object(provider, "_sync_generate", return_value=MagicMock()) as sync_generate:
        provider.generate(
            messages=[Message(role="system", content="BASE")],
            runtime_context="RUNTIME",
            preserve_prompt_shape=False,
            stream=False,
        )

    assert sync_generate.call_args.args[0]["messages"] == [
        {"role": "system", "content": "BASE"}
    ]


def test_service_stream_usage_snapshots_are_deep_copied_and_request_scoped(monkeypatch):
    class RecordingProvider:
        def is_loaded(self):
            return True

        def generate(self, **kwargs):
            return iter(())

    class Counter:
        def count_messages(self, messages, image_count=0):
            return len(messages[0]["content"][0]["text"])

        def count_text(self, text):
            return len(text)

    config = get_config()
    service = LLMService(config=config)
    service._provider = RecordingProvider()
    monkeypatch.setattr(
        "backend.compact.tokenizer.TokenCounter.get", staticmethod(lambda: Counter())
    )
    first_messages = [
        Message(
            role="user",
            content=[
                {"type": "text", "text": "first"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
            ],
        )
    ]
    second_messages = [
        Message(role="user", content=[{"type": "text", "text": "second"}])
    ]

    service.chat(
        first_messages, stream=True, request_id="first", stream_usage_id="first"
    )
    first_messages[0].content[0]["text"] = "mutated"
    service.chat(
        second_messages, stream=True, request_id="second", stream_usage_id="second"
    )

    assert service.get_stream_usage("x") is None
    assert service.get_stream_usage("x", request_id="missing") is None
    assert service.get_stream_usage("x", request_id="first") == {
        "prompt_tokens": 5,
        "completion_tokens": 1,
        "total_tokens": 6,
        "image_tokens": 512,
        "audio_tokens": 0,
        "video_tokens": 0,
        "cached_tokens": 0,
        "source": "estimated",
    }
    assert service.get_stream_usage("x", request_id="second") == {
        "prompt_tokens": 6,
        "completion_tokens": 1,
        "total_tokens": 7,
        "image_tokens": 0,
        "audio_tokens": 0,
        "video_tokens": 0,
        "cached_tokens": 0,
        "source": "estimated",
    }


@pytest.mark.asyncio
async def test_chat_stream_async_releases_usage_snapshots_on_all_exit_paths():
    release_provider = __import__("threading").Event()

    class StreamProvider:
        def is_loaded(self):
            return True

        def generate(self, request_id=None, **kwargs):
            def chunks():
                if request_id == "error":
                    raise RuntimeError("provider failure")
                yield {"content": "first"}
                if request_id == "cancel":
                    release_provider.wait(timeout=1)
                yield {"content": "last"}

            return chunks()

    service = LLMService(config=get_config())
    service._provider = StreamProvider()

    async for _ in service.chat_stream_async([Message(role="user", content="normal")]):
        pass
    assert service._stream_usage_requests == {}
    assert not hasattr(service, "_last_stream_request")

    async for _ in service.chat_stream_async(
        [Message(role="user", content="error")], request_id="error"
    ):
        pass
    assert service._stream_usage_requests == {}

    stream = service.chat_stream_async(
        [Message(role="user", content="cancel")], request_id="cancel"
    )
    await anext(stream)
    await stream.aclose()
    assert service._stream_usage_requests == {}
    release_provider.set()


def test_openai_proxy_stream_does_not_store_request_snapshot():
    provider = OpenAICompatibleProxyProvider(get_config())

    with patch.object(provider, "_stream_generate", return_value=iter(())):
        provider.generate(
            messages=[Message(role="user", content="hello")],
            stream=True,
            request_id="proxy-stream",
        )

    assert not hasattr(provider, "_last_stream_request")


def test_direct_stream_usage_requires_explicit_usage_consumer_id():
    class RecordingProvider:
        def is_loaded(self):
            return True

        def generate(self, **kwargs):
            return iter(())

    service = LLMService(config=get_config())
    service._provider = RecordingProvider()

    service.chat(
        [Message(role="user", content="ordinary")],
        stream=True,
        request_id="ordinary",
    )
    assert service._stream_usage_requests == {}
    assert service.get_stream_usage("x", request_id="ordinary") is None

    service.chat(
        [Message(role="user", content="agent")],
        stream=True,
        request_id="agent",
        stream_usage_id="agent",
    )
    assert "agent" in service._stream_usage_requests
    assert service.get_stream_usage("x", request_id=None) is None
    assert "agent" in service._stream_usage_requests
    assert service.get_stream_usage("x", request_id="unknown") is None
    assert "agent" in service._stream_usage_requests
    assert service.get_stream_usage("x", request_id="agent") is not None
    assert service._stream_usage_requests == {}


def test_openai_proxy_provider_preserves_zero_temperature_and_merges_extra_body():
    config = get_config()
    provider = OpenAICompatibleProxyProvider(config)

    with patch.object(provider, "_sync_generate", return_value=MagicMock()) as sync_generate:
        provider.generate(
            messages=[Message(role="user", content="hello")],
            temperature=0.0,
            max_new_tokens=150,
            stream=False,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

    request_data = sync_generate.call_args.args[0]
    assert request_data["temperature"] == 0.0
    assert request_data["max_tokens"] == 150
    assert request_data["chat_template_kwargs"] == {"enable_thinking": False}


def test_abort_request_before_register_preserves_pending_stop():
    request_id = "req-pending-stop"

    abort_request(request_id)
    register_request(request_id)

    try:
        assert is_request_aborted(request_id) is True
    finally:
        unregister_request(request_id)


def test_register_request_clears_pending_abort_when_request_finishes():
    request_id = "req-cleanup-stop"

    abort_request(request_id)
    register_request(request_id)
    unregister_request(request_id)

    assert is_request_aborted(request_id) is False


def test_active_request_ids_include_request_before_upstream_response_registration():
    from backend.llm_service import active_request_ids

    request_id = "req-before-upstream-response"
    registration = register_request(request_id)

    try:
        assert request_id in active_request_ids()
    finally:
        unregister_request(request_id, registration)


def test_stale_request_unregister_does_not_clear_new_same_id_registration():
    request_id = "req-reused-registration"
    old_registration = register_request(request_id)
    new_registration = register_request(request_id)

    unregister_request(request_id, old_registration)
    abort_request(request_id)

    try:
        assert is_request_aborted(request_id) is True
    finally:
        unregister_request(request_id, new_registration)


def test_vllm_abort_closes_upstream_without_sglang_abort():
    request_id = "req-vllm-abort"
    closer = MagicMock()
    registration = register_upstream_stream(request_id, "vllm", closer)

    try:
        with patch("backend.llm_service.abort_sglang_request") as abort_sglang:
            assert abort_request_by_id(request_id, "http://sglang") is True

        closer.assert_called_once_with()
        abort_sglang.assert_not_called()
    finally:
        unregister_upstream_stream(request_id, registration)
        unregister_request(request_id)


def test_sglang_abort_closes_upstream_and_calls_targeted_abort():
    request_id = "req-sglang-abort"
    closer = MagicMock()
    registration = register_upstream_stream(request_id, "sglang", closer)

    try:
        def provider_abort(_url, _request_id):
            closer.assert_not_called()
            return True

        with patch(
            "backend.llm_service.abort_sglang_request", side_effect=provider_abort
        ) as abort_sglang:
            assert abort_request_by_id(
                request_id, "http://sglang", require_provider_abort=True
            ) is True

        closer.assert_called_once_with()
        abort_sglang.assert_called_once_with("http://sglang", request_id)
    finally:
        unregister_upstream_stream(request_id, registration)
        unregister_request(request_id)


def test_strict_vllm_stop_closes_upstream_without_sglang_probe():
    request_id = "req-vllm-strict-stop"
    closer = MagicMock()
    registration = register_upstream_stream(request_id, "vllm", closer)
    try:
        with patch("backend.llm_service.abort_sglang_request") as abort_sglang:
            assert abort_request_by_id(
                request_id, "http://sglang", require_provider_abort=True
            ) is True
        closer.assert_called_once_with()
        abort_sglang.assert_not_called()
    finally:
        unregister_upstream_stream(request_id, registration)
        unregister_request(request_id)


def test_sglang_abort_reports_targeted_endpoint_failure():
    request_id = "req-sglang-failed-abort"
    closer = MagicMock()
    registration = register_upstream_stream(request_id, "sglang", closer)
    try:
        with patch("backend.llm_service.abort_sglang_request", return_value=False):
            assert abort_request_by_id(
                request_id, "http://sglang", require_provider_abort=True
            ) is False
        assert not is_request_aborted(request_id)
        closer.assert_not_called()
    finally:
        unregister_upstream_stream(request_id, registration)
        unregister_request(request_id)


def test_strict_stop_before_upstream_registration_records_pending_abort():
    request_id = "req-sglang-pending-strict-stop"
    closer = MagicMock()
    with patch("backend.llm_service.abort_sglang_request", return_value=False):
        assert abort_request_by_id(
            request_id, "http://sglang", require_provider_abort=True
        ) is True
    registration = register_upstream_stream(request_id, "sglang", closer)
    try:
        closer.assert_called_once_with()
    finally:
        unregister_upstream_stream(request_id, registration)
        unregister_request(request_id)


def test_pending_abort_before_response_registration_closes_on_register():
    request_id = "req-pending-upstream"
    closer = MagicMock()
    abort_request(request_id)

    registration = register_upstream_stream(request_id, "vllm", closer)

    try:
        closer.assert_called_once_with()
    finally:
        unregister_upstream_stream(request_id, registration)
        unregister_request(request_id)


def test_register_racing_abort_closes_upstream_exactly_once():
    request_id = "req-register-abort-race"
    closer = MagicMock()
    barrier = threading.Barrier(2)

    def register():
        barrier.wait()
        register_upstream_stream(request_id, "vllm", closer)

    thread = threading.Thread(target=register)
    thread.start()
    barrier.wait()
    abort_request(request_id)
    thread.join()

    assert closer.call_count == 1
    unregister_request(request_id)


def test_registered_provider_is_authoritative_over_disagreeing_global_backend():
    request_id = "req-provider-disagreement"
    registration = register_upstream_stream(request_id, "sglang", MagicMock())

    try:
        with (
            patch("backend.llm_service._llm_service") as service,
            patch("backend.llm_service.abort_sglang_request", return_value=True) as abort_sglang,
        ):
            service.active_backend = "vllm"
            abort_request_by_id(request_id, "http://sglang")
        abort_sglang.assert_called_once_with("http://sglang", request_id)
    finally:
        unregister_upstream_stream(request_id, registration)
        unregister_request(request_id)


@pytest.mark.asyncio
async def test_chat_stream_cancellation_aborts_targeted_request_once():
    service = LLMService(get_config(), use_mock=True)
    request_id = "req-cancel-once"

    def blocking_stream():
        yield {"content": "started"}
        threading.Event().wait(1)

    with (
        patch.object(service, "chat", return_value=blocking_stream()),
        patch("backend.llm_service.abort_request_by_id") as abort_by_id,
    ):
        stream = service.chat_stream_async([], request_id=request_id)
        await anext(stream)
        task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    abort_by_id.assert_called_once_with(request_id)


def test_old_request_cleanup_does_not_clear_new_request():
    request_id = "req-reused"
    old_registration = register_upstream_stream(request_id, "vllm", MagicMock())
    new_registration = register_upstream_stream(request_id, "vllm", MagicMock())

    unregister_upstream_stream(request_id, old_registration)

    try:
        assert active_upstream_request_ids() == (request_id,)
    finally:
        unregister_upstream_stream(request_id, new_registration)


@pytest.mark.parametrize(
    "provider_class",
    [SGLangProvider, VLLMProvider, OpenAICompatibleProxyProvider],
)
def test_intentional_stream_close_emits_no_error_model_text(provider_class):
    request_id = f"req-intentional-close-{provider_class.__name__}"
    provider = provider_class(get_config())

    class FakeResponse:
        status_code = 200

        def close(self):
            pass

        def iter_text(self):
            raise RuntimeError("stream closed")

    response = FakeResponse()

    class FakeStreamContext:
        def __enter__(self):
            return response

        def __exit__(self, exc_type, exc, tb):
            return False

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client.stream.return_value = FakeStreamContext()
    fake_httpx = types.SimpleNamespace(Client=MagicMock(return_value=mock_client))
    abort_request(request_id)

    with patch.dict(sys.modules, {"httpx": fake_httpx}):
        chunks = list(provider._stream_generate({}, request_id=request_id))

    assert not any("流式生成错误" in (chunk.get("content") or "") for chunk in chunks)


@pytest.mark.parametrize(
    "provider_class",
    [SGLangProvider, VLLMProvider, OpenAICompatibleProxyProvider],
)
def test_stream_provider_surfaces_non_newline_terminated_residual(
    provider_class, caplog
):
    request_id = f"req-residual-{provider_class.__name__}"
    provider = provider_class(get_config())

    class FakeResponse:
        status_code = 200

        def close(self):
            pass

        def iter_text(self):
            return iter(['data: {"choices":[{"delta":{"content":"lost-tail"}}]}'])

    class FakeStreamContext:
        def __enter__(self):
            return FakeResponse()

        def __exit__(self, exc_type, exc, tb):
            return False

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client.stream.return_value = FakeStreamContext()
    fake_httpx = types.SimpleNamespace(Client=MagicMock(return_value=mock_client))

    with patch.dict(sys.modules, {"httpx": fake_httpx}), caplog.at_level(
        "WARNING", logger="backend.llm_service"
    ):
        chunks = list(provider._stream_generate({}, request_id=request_id))

    assert chunks[-1] == {
        "content": None,
        "reasoning": None,
        "stream_done": False,
        "stream_residual_chars": len(
            'data: {"choices":[{"delta":{"content":"lost-tail"}}]}'
        ),
    }
    assert "[llm-stream-diag]" in caplog.text
    assert f"request_id={request_id}" in caplog.text
    assert "non_newline_residual" in caplog.text


@pytest.mark.parametrize(
    "provider_class",
    [SGLangProvider, VLLMProvider, OpenAICompatibleProxyProvider],
)
def test_stream_provider_reports_bytes_buffered_after_done(provider_class, caplog):
    request_id = f"req-after-done-{provider_class.__name__}"
    provider = provider_class(get_config())
    trailing = 'data: {"unexpected":"after-done"}'

    class FakeResponse:
        status_code = 200

        def close(self):
            pass

        def iter_text(self):
            return iter([f"data: [DONE]\n{trailing}"])

    class FakeStreamContext:
        def __enter__(self):
            return FakeResponse()

        def __exit__(self, exc_type, exc, tb):
            return False

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client.stream.return_value = FakeStreamContext()
    fake_httpx = types.SimpleNamespace(Client=MagicMock(return_value=mock_client))

    with patch.dict(sys.modules, {"httpx": fake_httpx}), caplog.at_level(
        "WARNING", logger="backend.llm_service"
    ):
        chunks = list(provider._stream_generate({}, request_id=request_id))

    assert chunks[-1] == {
        "content": None,
        "reasoning": None,
        "stream_done": True,
        "stream_residual_chars": len(trailing),
    }
    assert "bytes_after_done" in caplog.text
    assert f"request_id={request_id}" in caplog.text


def test_openai_proxy_provider_uses_remote_model_id_from_models_endpoint():
    config = get_config()

    models_payload = {
        "data": [
            {"id": "/model", "object": "model"},
            {"id": "other-model", "object": "model"},
        ]
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = models_payload

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client.get.return_value = mock_response

    fake_httpx = types.SimpleNamespace(Client=MagicMock(return_value=mock_client))

    with patch.dict(sys.modules, {"httpx": fake_httpx}):
        provider = OpenAICompatibleProxyProvider(config)
        assert provider.load_model() is True

    assert provider._model_name == "/model"


def test_vllm_provider_uses_remote_model_id_from_models_endpoint():
    config = get_config()

    models_payload = {
        "data": [
            {"id": "/model", "object": "model", "created": 0},
            {"id": "other-model", "object": "model", "created": 1},
        ]
    }

    health_response = MagicMock()
    health_response.status_code = 200

    models_response = MagicMock()
    models_response.status_code = 200
    models_response.json.return_value = models_payload

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client.get.side_effect = [health_response, models_response]

    fake_httpx = types.SimpleNamespace(Client=MagicMock(return_value=mock_client))

    with patch.dict(sys.modules, {"httpx": fake_httpx}):
        provider = VLLMProvider(config)
        assert provider.load_model() is True

    assert provider._model_name == "/model"


def test_resolve_effective_max_tokens_uses_runtime_vllm_context_limit():
    config = get_config()
    config.model.max_context_length = 262144
    config.vllm.max_model_len = 160000
    config.inference.max_tokens = 262144
    config.inference.max_new_tokens = 262144

    assert resolve_backend_context_length(config, "vllm") == 160000
    assert resolve_effective_max_tokens(config, "vllm") == 160000


def test_config_keeps_max_tokens_and_max_new_tokens_in_sync():
    config = reload_config()

    assert config.inference.max_tokens == config.inference.max_new_tokens


def test_vllm_generate_caps_max_tokens_before_request():
    config = get_config()
    config.vllm.max_model_len = 160000
    provider = VLLMProvider(config)

    with patch.object(provider, "_sync_generate", return_value=MagicMock()) as sync_generate:
        provider.generate(
            messages=[Message(role="user", content="x" * 10133)],
            max_new_tokens=160000,
            stream=False,
        )

    request_data = sync_generate.call_args.args[0]
    assert request_data["max_tokens"] < 160000
    assert request_data["max_tokens"] >= 1


def test_openai_proxy_stream_error_reads_body_without_response_not_read():
    config = get_config()
    provider = OpenAICompatibleProxyProvider(config)

    class ResponseNotRead(Exception):
        pass

    class FakeResponse:
        def __init__(self):
            self.status_code = 404
            self._body = (
                '{"error": {"message": "The model `/bad-model` does not exist.", '
                '"type": "NotFoundError"}}'
            )
            self._read = False

        def read(self):
            self._read = True
            return self._body.encode()

        @property
        def text(self):
            if not self._read:
                raise ResponseNotRead(
                    "Attempted to access streaming response content, without having called `read()`."
                )
            return self._body

        def iter_text(self):
            return iter(())

    response = FakeResponse()

    class FakeStreamContext:
        def __enter__(self):
            return response

        def __exit__(self, exc_type, exc, tb):
            return False

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client.stream.return_value = FakeStreamContext()

    fake_httpx = types.SimpleNamespace(Client=MagicMock(return_value=mock_client))

    with patch.dict(sys.modules, {"httpx": fake_httpx}):
        chunks = list(
            provider._stream_generate(
                {
                    "model": "/bad-model",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                }
            )
        )

    assert len(chunks) == 1
    assert "ResponseNotRead" not in (chunks[0].get("content") or "")
    assert "NotFoundError" in (chunks[0].get("content") or "")


def test_sglang_stream_error_reads_body_without_response_not_read():
    config = get_config()
    provider = SGLangProvider(config)

    class ResponseNotRead(Exception):
        pass

    class FakeResponse:
        def __init__(self):
            self.status_code = 400
            self._body = (
                '{"error": {"message": "The model `/bad-model` does not exist.", '
                '"type": "NotFoundError"}}'
            )
            self._read = False

        def read(self):
            self._read = True
            return self._body.encode()

        @property
        def text(self):
            if not self._read:
                raise ResponseNotRead(
                    "Attempted to access streaming response content, without having called `read()`."
                )
            return self._body

        def iter_text(self):
            return iter(())

    response = FakeResponse()

    class FakeStreamContext:
        def __enter__(self):
            return response

        def __exit__(self, exc_type, exc, tb):
            return False

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client.stream.return_value = FakeStreamContext()

    fake_httpx = types.SimpleNamespace(Client=MagicMock(return_value=mock_client))

    with patch.dict(sys.modules, {"httpx": fake_httpx}):
        chunks = list(
            provider._stream_generate(
                {
                    "model": "/bad-model",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                }
            )
        )

    assert len(chunks) == 1
    assert "ResponseNotRead" not in (chunks[0].get("content") or "")
    assert "NotFoundError" in (chunks[0].get("content") or "")


def test_vllm_stream_error_reads_body_without_response_not_read():
    config = get_config()
    provider = VLLMProvider(config)

    class ResponseNotRead(Exception):
        pass

    class FakeResponse:
        def __init__(self):
            self.status_code = 404
            self._body = (
                '{"error": {"message": "The model `/bad-model` does not exist.", '
                '"type": "NotFoundError"}}'
            )
            self._read = False

        def read(self):
            self._read = True
            return self._body.encode()

        @property
        def text(self):
            if not self._read:
                raise ResponseNotRead(
                    "Attempted to access streaming response content, without having called `read()`."
                )
            return self._body

        def iter_text(self):
            return iter(())

    response = FakeResponse()

    class FakeStreamContext:
        def __enter__(self):
            return response

        def __exit__(self, exc_type, exc, tb):
            return False

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client.stream.return_value = FakeStreamContext()

    fake_httpx = types.SimpleNamespace(Client=MagicMock(return_value=mock_client))

    with patch.dict(sys.modules, {"httpx": fake_httpx}):
        chunks = list(
            provider._stream_generate(
                {
                    "model": "/bad-model",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                }
            )
        )

    assert len(chunks) == 1
    assert "ResponseNotRead" not in (chunks[0].get("content") or "")
    assert "NotFoundError" in (chunks[0].get("content") or "")


def test_vllm_sync_retries_when_max_tokens_exceeds_server_limit():
    config = get_config()
    provider = VLLMProvider(config)
    attempted_max_tokens = []

    class FakeErrorResponse:
        status_code = 400

        def __init__(self):
            self._read = False

        def read(self):
            self._read = True
            return b""

        @property
        def text(self):
            assert self._read is True
            return (
                '{"error":{"message":"max_tokens=262144 cannot be greater than '
                'max_model_len=max_total_tokens=32768. Please request fewer output tokens.",'
                '"type":"BadRequestError","param":"max_tokens","code":400}}'
            )

    class FakeSuccessResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json):
            attempted_max_tokens.append(json["max_tokens"])
            if json["max_tokens"] > 32768:
                return FakeErrorResponse()
            return FakeSuccessResponse()

    fake_httpx = types.SimpleNamespace(Client=MagicMock(return_value=FakeClient()))

    with patch.dict(sys.modules, {"httpx": fake_httpx}):
        result = provider._sync_generate(
            {
                "model": "/model",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 262144,
                "stream": False,
            }
        )

    assert attempted_max_tokens[0] == 262144
    assert 1 <= attempted_max_tokens[1] <= 32768
    assert result.text == "ok"


def test_vllm_stream_retries_when_max_tokens_exceeds_server_limit():
    config = get_config()
    provider = VLLMProvider(config)
    attempted_max_tokens = []

    class FakeErrorResponse:
        status_code = 400

        def __init__(self):
            self._read = False

        def read(self):
            self._read = True
            return b""

        @property
        def text(self):
            assert self._read is True
            return (
                '{"error":{"message":"max_tokens=262144 cannot be greater than '
                'max_model_len=max_total_tokens=32768. Please request fewer output tokens.",'
                '"type":"BadRequestError","param":"max_tokens","code":400}}'
            )

        def iter_text(self):
            return iter(())

    class FakeSuccessResponse:
        status_code = 200

        def iter_text(self):
            payload = json.dumps({"choices": [{"delta": {"content": "ok"}}]})
            return iter([f"data: {payload}\n", "data: [DONE]\n"])

    class FakeStreamContext:
        def __init__(self, response):
            self.response = response

        def __enter__(self):
            return self.response

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, json):
            attempted_max_tokens.append(json["max_tokens"])
            if json["max_tokens"] > 32768:
                return FakeStreamContext(FakeErrorResponse())
            return FakeStreamContext(FakeSuccessResponse())

    fake_httpx = types.SimpleNamespace(Client=MagicMock(return_value=FakeClient()))

    with patch.dict(sys.modules, {"httpx": fake_httpx}):
        chunks = list(
            provider._stream_generate(
                {
                    "model": "/model",
                    "messages": [{"role": "user", "content": "hello"}],
                    "max_tokens": 262144,
                    "stream": True,
                }
            )
        )

    assert attempted_max_tokens[0] == 262144
    assert 1 <= attempted_max_tokens[1] <= 32768
    assert [chunk for chunk in chunks if chunk.get("content") == "ok"]


def test_vllm_sync_retries_when_prompt_and_output_exceed_context_limit():
    config = get_config()
    provider = VLLMProvider(config)
    attempted_max_tokens = []

    class FakeErrorResponse:
        status_code = 400

        def __init__(self):
            self._read = False

        def read(self):
            self._read = True
            return b""

        @property
        def text(self):
            assert self._read is True
            return (
                '{"error":{"message":"You passed 10133 input characters and requested '
                '160000 output tokens. However, the model\'s context length is only 160000 '
                'tokens, resulting in a maximum input length of 0 tokens (at most 0 '
                'characters). Please reduce the length of the input prompt. '
                '(parameter=input_text, value=10133)","type":"BadRequestError",'
                '"param":"input_text","code":400}}'
            )

    class FakeSuccessResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json):
            attempted_max_tokens.append(json["max_tokens"])
            if len(attempted_max_tokens) == 1:
                return FakeErrorResponse()
            return FakeSuccessResponse()

    fake_httpx = types.SimpleNamespace(Client=MagicMock(return_value=FakeClient()))

    with patch.dict(sys.modules, {"httpx": fake_httpx}):
        result = provider._sync_generate(
            {
                "model": "/model",
                "messages": [{"role": "user", "content": "x" * 10133}],
                "max_tokens": 160000,
                "stream": False,
            }
        )

    assert attempted_max_tokens[0] == 160000
    assert 1 <= attempted_max_tokens[1] < 160000
    assert result.text == "ok"


def test_vllm_stream_retries_when_prompt_and_output_exceed_context_limit():
    config = get_config()
    provider = VLLMProvider(config)
    attempted_max_tokens = []

    class FakeErrorResponse:
        status_code = 400

        def __init__(self):
            self._read = False

        def read(self):
            self._read = True
            return b""

        @property
        def text(self):
            assert self._read is True
            return (
                '{"error":{"message":"You passed 10133 input characters and requested '
                '160000 output tokens. However, the model\'s context length is only 160000 '
                'tokens, resulting in a maximum input length of 0 tokens (at most 0 '
                'characters). Please reduce the length of the input prompt. '
                '(parameter=input_text, value=10133)","type":"BadRequestError",'
                '"param":"input_text","code":400}}'
            )

        def iter_text(self):
            return iter(())

    class FakeSuccessResponse:
        status_code = 200

        def iter_text(self):
            payload = json.dumps({"choices": [{"delta": {"content": "ok"}}]})
            return iter([f"data: {payload}\n", "data: [DONE]\n"])

    class FakeStreamContext:
        def __init__(self, response):
            self.response = response

        def __enter__(self):
            return self.response

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, json):
            attempted_max_tokens.append(json["max_tokens"])
            if len(attempted_max_tokens) == 1:
                return FakeStreamContext(FakeErrorResponse())
            return FakeStreamContext(FakeSuccessResponse())

    fake_httpx = types.SimpleNamespace(Client=MagicMock(return_value=FakeClient()))

    with patch.dict(sys.modules, {"httpx": fake_httpx}):
        chunks = list(
            provider._stream_generate(
                {
                    "model": "/model",
                    "messages": [{"role": "user", "content": "x" * 10133}],
                    "max_tokens": 160000,
                    "stream": True,
                }
            )
        )

    assert attempted_max_tokens[0] == 160000
    assert 1 <= attempted_max_tokens[1] < 160000
    assert [chunk for chunk in chunks if chunk.get("content") == "ok"]
