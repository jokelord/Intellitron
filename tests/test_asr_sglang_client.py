from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backend.asr.config import load_asr_config


@pytest.mark.asyncio
async def test_sglang_client_posts_configured_non_streaming_transcription():
    from backend.asr.sglang_client import SGLangAsrClient

    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": "测试文本", "usage": {"seconds": 1}})

    config = load_asr_config_from_repo()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SGLangAsrClient(config, http_client=http_client)
    try:
        result = await client.transcribe(b"RIFFfake-wav", asyncio.Event())
    finally:
        await http_client.aclose()

    assert result.text == "测试文本"
    assert result.error is None
    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/v1/audio/transcriptions"
    body = request.content
    assert b'name="model"' in body and b"Qwen3-ASR-1.7B" in body
    assert b'name="stream"' in body and b"false" in body
    assert b'name="file"; filename="utterance.wav"' in body


@pytest.mark.asyncio
async def test_sglang_client_probe_requires_matching_model_identity():
    from backend.asr.sglang_client import SGLangAsrClient

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, text="ok")
        return httpx.Response(200, json={"data": [{"id": "wrong-model"}]})

    config = load_asr_config_from_repo()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SGLangAsrClient(config, http_client=http_client)
    try:
        status = await client.probe()
    finally:
        await http_client.aclose()

    assert status.available is False
    assert status.model_matches is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"text": ["not", "text"]},
        {"text": "   "},
    ],
)
async def test_sglang_client_rejects_malformed_or_empty_json_results(payload):
    from backend.asr.sglang_client import SGLangAsrClient

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    config = load_asr_config_from_repo()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SGLangAsrClient(config, http_client=http_client)
    try:
        result = await client.transcribe(b"RIFFfake-wav", asyncio.Event())
    finally:
        await http_client.aclose()

    assert result.text == ""
    assert result.error is not None


@pytest.mark.asyncio
async def test_sglang_client_rejects_empty_text_result():
    from backend.asr.sglang_client import SGLangAsrClient

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=" \n ")

    config = _transcription_config(response_format="text")
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SGLangAsrClient(config, http_client=http_client)
    try:
        result = await client.transcribe(b"RIFFfake-wav", asyncio.Event())
    finally:
        await http_client.aclose()

    assert result.text == ""
    assert result.error is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        'data: []\n\ndata: [DONE]\n\n',
        'data: {"choices": "invalid"}\n\ndata: [DONE]\n\n',
        'data: {"choices": [{"delta": {"content": []}}]}\n\ndata: [DONE]\n\n',
        'data: {"choices": [{"delta": {"content": ""}}]}\n\ndata: [DONE]\n\n',
    ],
)
async def test_sglang_client_rejects_malformed_or_empty_stream_results(body):
    from backend.asr.sglang_client import SGLangAsrClient

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    config = _transcription_config(streaming=True)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SGLangAsrClient(config, http_client=http_client)
    try:
        result = await client.transcribe(b"RIFFfake-wav", asyncio.Event())
    finally:
        await http_client.aclose()

    assert result.text == ""
    assert result.error is not None


def load_asr_config_from_repo():
    from backend.config import PROJECT_ROOT

    return load_asr_config(
        PROJECT_ROOT / "config/config-asr.yaml", project_root=PROJECT_ROOT
    )


def _transcription_config(**updates):
    config = load_asr_config_from_repo()
    return config.model_copy(
        update={
            "transcription": config.transcription.model_copy(update=updates),
        }
    )
