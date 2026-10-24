"""Cancellable OpenAI-compatible client for the dedicated SGLang ASR server."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from backend.asr.config import AsrConfig
from backend.asr.models import AsrResult


DeltaCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class AsrUpstreamStatus:
    available: bool
    model_matches: bool
    error: str | None = None


class SGLangAsrClient:
    def __init__(
        self,
        config: AsrConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=config.sglang.base_url,
            timeout=httpx.Timeout(config.transcription.request_timeout_seconds),
            trust_env=False,
        )

    async def probe(self) -> AsrUpstreamStatus:
        try:
            timeout = self._config.sglang.health_timeout_seconds
            health = await self._client.get(
                f"{self._config.sglang.base_url}/health", timeout=timeout
            )
            health.raise_for_status()
            models = await self._client.get(
                f"{self._config.sglang.base_url}/v1/models", timeout=timeout
            )
            models.raise_for_status()
            payload = models.json()
            entries = payload.get("data") if isinstance(payload, dict) else None
            matches = isinstance(entries, list) and any(
                isinstance(item, dict)
                and item.get("id") == self._config.sglang.served_model_name
                for item in entries
            )
            return AsrUpstreamStatus(matches, matches, None if matches else "model mismatch")
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            return AsrUpstreamStatus(False, False, str(exc))

    async def transcribe(
        self,
        wav_bytes: bytes,
        abort_event: asyncio.Event,
        on_delta: DeltaCallback | None = None,
    ) -> AsrResult:
        started = time.monotonic()
        form = {
            "model": self._config.sglang.served_model_name,
            "response_format": self._config.transcription.response_format,
            "temperature": str(self._config.transcription.temperature),
            "stream": str(self._config.transcription.streaming).lower(),
        }
        if self._config.transcription.language:
            form["language"] = self._config.transcription.language
        files = {"file": ("utterance.wav", wav_bytes, "audio/wav")}
        url = f"{self._config.sglang.base_url}/v1/audio/transcriptions"

        if abort_event.is_set():
            raise asyncio.CancelledError
        try:
            async with self._client.stream(
                "POST", url, data=form, files=files
            ) as response:
                response.raise_for_status()
                if self._config.transcription.streaming:
                    text = await self._read_stream(response, abort_event, on_delta)
                    language = None
                elif self._config.transcription.response_format == "text":
                    text = (await response.aread()).decode("utf-8").strip()
                    language = None
                else:
                    payload = json.loads((await response.aread()).decode("utf-8"))
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("text"), str
                    ):
                        raise ValueError("malformed ASR response")
                    text = payload["text"].strip()
                    language_value = payload.get("language")
                    if language_value is not None and not isinstance(
                        language_value, str
                    ):
                        raise ValueError("malformed ASR response")
                    language = language_value or None
                if not text:
                    raise ValueError("empty ASR transcript")
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError:
            latency = int((time.monotonic() - started) * 1000)
            return AsrResult("", None, 0, latency, "ASR upstream request failed")
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
            latency = int((time.monotonic() - started) * 1000)
            return AsrResult("", None, 0, latency, "Invalid ASR upstream response")

        latency = int((time.monotonic() - started) * 1000)
        return AsrResult(text, language, 0, latency, None)

    async def _read_stream(
        self,
        response: httpx.Response,
        abort_event: asyncio.Event,
        on_delta: DeltaCallback | None,
    ) -> str:
        chunks: list[str] = []
        async for line in response.aiter_lines():
            if abort_event.is_set():
                raise asyncio.CancelledError
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise ValueError("malformed ASR streaming event")
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ValueError("malformed ASR streaming event")
            choice = choices[0]
            if not isinstance(choice, dict):
                raise ValueError("malformed ASR streaming event")
            delta_payload = choice.get("delta")
            if not isinstance(delta_payload, dict):
                raise ValueError("malformed ASR streaming event")
            delta = delta_payload.get("content", "")
            if not isinstance(delta, str):
                raise ValueError("malformed ASR streaming event")
            if delta:
                chunks.append(delta)
                if on_delta:
                    await on_delta(delta)
        return "".join(chunks).strip()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
