from __future__ import annotations

import json
import asyncio
import logging
import stat
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.asr.config import load_asr_config
from backend.asr.models import AsrResult, VadObservation


class FakeVad:
    def __init__(self):
        self.accept_calls = 0
        self.reset_count = 0

    def accept(self, samples):
        self.accept_calls += 1
        probability = 0.9 if float(np.max(np.abs(samples))) > 0.01 else 0.1
        return [VadObservation(probability, probability >= 0.5, False, probability >= 0.5)]

    def reset(self):
        self.reset_count += 1


class CapturingVad(FakeVad):
    """Retains the real post-gain PCM that reaches the VAD boundary."""

    def __init__(self):
        super().__init__()
        self.frames: list[np.ndarray] = []

    def accept(self, samples):
        self.frames.append(np.asarray(samples, np.float32).copy())
        return super().accept(samples)


class FakeAsrClient:
    def __init__(self):
        self.calls = 0
        self.wav_payloads: list[bytes] = []

    async def probe(self):
        return type("Status", (), {"available": True, "model_matches": True})()

    async def transcribe(self, wav_bytes, abort_event, on_delta=None):
        self.calls += 1
        assert wav_bytes.startswith(b"RIFF")
        self.wav_payloads.append(wav_bytes)
        return AsrResult(
            text="语音问题",
            language=None,
            duration_ms=1000,
            inference_latency_ms=5,
            error=None,
        )

    async def close(self):
        return None


class ControlledWebSocket:
    def __init__(self):
        self.incoming: asyncio.Queue[dict] = asyncio.Queue()
        self.sent: list[dict] = []
        self.sent_binary: list[bytes] = []
        self.outgoing: list[tuple[str, object]] = []
        self.accepted = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_code: int | None = None

    async def accept(self):
        self.accepted.set()

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, payload):
        self.sent.append(payload)
        self.outgoing.append(("json", payload))

    async def send_bytes(self, payload: bytes):
        self.sent_binary.append(payload)
        self.outgoing.append(("bytes", payload))

    async def close(self, code=1000):
        if self.closed.is_set():
            return
        self.close_code = code
        self.closed.set()
        await self.incoming.put({"type": "websocket.disconnect"})

    async def send_control(self, payload):
        await self.incoming.put(
            {"type": "websocket.receive", "text": json.dumps(payload)}
        )

    async def send_pcm(self, samples):
        await self.incoming.put(
            {"type": "websocket.receive", "bytes": np.asarray(samples, np.float32).tobytes()}
        )

    async def send_binary_from_client(self, payload: bytes):
        await self.incoming.put({"type": "websocket.receive", "bytes": payload})


async def _wait_for_event(websocket: ControlledWebSocket, event_type: str, timeout=1.0):
    async def wait():
        while True:
            for event in websocket.sent:
                if event.get("type") == event_type:
                    return event
            await asyncio.sleep(0)

    return await asyncio.wait_for(wait(), timeout=timeout)


async def _wait_for_event_count(
    websocket: ControlledWebSocket, event_type: str, count: int, timeout=1.0
):
    async def wait():
        while True:
            matches = [
                event for event in websocket.sent if event.get("type") == event_type
            ]
            if len(matches) >= count:
                return matches
            await asyncio.sleep(0)

    return await asyncio.wait_for(wait(), timeout=timeout)


async def _wait_for_frames(vad: CapturingVad, count: int, timeout=1.0) -> None:
    async def wait():
        while len(vad.frames) < count:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=timeout)


@pytest.mark.asyncio
async def test_silent_first_pcm_frame_emits_json_safe_level_event():
    from backend.asr.service import AsrService

    service = AsrService(
        _config(),
        sglang_client=FakeAsrClient(),
        vad_factory=lambda: FakeVad(),
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")

    await websocket.send_pcm(np.zeros(320, np.float32))
    level = await _wait_for_event(websocket, "level")

    assert np.isfinite(level["post_gain_dbfs"])
    json.dumps(level, allow_nan=False)

    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()


def _utterance_log_payloads(caplog):
    return [
        json.loads(record.args[0])
        for record in caplog.records
        if "asr_utterance" in str(record.msg) and record.args
    ]


def _config(
    *,
    audio=None,
    transport=None,
    recording=None,
    runtime=None,
    audio_trigger=None,
    vad=None,
    debug=None,
    observability=None,
):
    root = Path(__file__).resolve().parents[1]
    config = load_asr_config(root / "config/config-asr.yaml", project_root=root)
    updates = {}
    for section, values in (
        ("audio", audio),
        ("transport", transport),
        ("recording", recording),
        ("runtime", runtime),
        ("audio_trigger", audio_trigger),
        ("vad", vad),
        ("observability", observability),
    ):
        if values:
            updates[section] = getattr(config, section).model_copy(update=values)
    if debug is not None:
        configured_debug = getattr(config, "debug", None)
        updates["debug"] = (
            configured_debug.model_copy(update=debug)
            if configured_debug is not None
            else SimpleNamespace(**debug)
        )
    return config.model_copy(update=updates)


def _short_utterance_config(**kwargs):
    audio_trigger = {"min_trigger_duration_ms": 20}
    audio_trigger.update(kwargs.pop("audio_trigger", {}))
    recording = {
        "pretrigger_buffer_ms": 0,
        "posttrigger_tail_ms": 0,
        "max_utterance_ms": 100,
        "min_utterance_ms": 20,
        "candidate_confirmation_timeout_ms": 40,
        "end_no_voice_duration_ms": 20,
        # pad 等于无声窗：默认不裁剪，保持既有用例的载荷算术不变。
        "end_no_voice_trim_pad_ms": 20,
    }
    recording.update(kwargs.pop("recording", {}))
    return _config(
        recording=recording,
        audio_trigger=audio_trigger,
        **kwargs,
    )


def _app(fake_client: FakeAsrClient, config=None):
    from backend.api.asr import create_asr_router
    from backend.asr.service import AsrService

    config = config or _config()
    app = FastAPI()
    app.state.asr_service = AsrService(
        config,
        sglang_client=fake_client,
        vad_factory=lambda: FakeVad(),
    )
    app.include_router(create_asr_router(config.transport.websocket_path))
    return app


def _start_message():
    return {
        "type": "start",
        "protocol_version": 1,
        "sample_rate_hz": 16000,
        "channels": 1,
        "sample_format": "float32",
        "frame_samples": 320,
    }


def test_asr_websocket_requires_handshake_before_binary():
    client = TestClient(_app(FakeAsrClient()))
    with client.websocket_connect("/v1/asr/stream") as websocket:
        websocket.send_bytes(np.zeros(320, np.float32).tobytes())
        event = websocket.receive_json()
    assert event["type"] == "fatal_error"
    assert event["code"] == "handshake_required"


def test_asr_websocket_emits_final_transcript_after_completed_utterance():
    fake_client = FakeAsrClient()
    client = TestClient(
        _app(
            fake_client,
            _config(
                recording={
                    "end_no_voice_duration_ms": 20,
                    "posttrigger_tail_ms": 0,
                    "min_utterance_ms": 20,
                },
                debug={"replay": False},
            ),
        )
    )
    with client.websocket_connect("/v1/asr/stream") as websocket:
        websocket.send_text(json.dumps(_start_message()))
        assert websocket.receive_json()["type"] == "ready"

        speech = np.full(320, 0.2, np.float32).tobytes()
        silence = np.zeros(320, np.float32).tobytes()
        for _ in range(12):
            websocket.send_bytes(speech)
        for _ in range(55):
            websocket.send_bytes(silence)

        event_types = []
        transcript = None
        for _ in range(100):
            event = websocket.receive_json()
            event_types.append(event["type"])
            if event["type"] == "transcript":
                transcript = event["text"]
                break

    assert "transcribing" in event_types
    assert transcript == "语音问题"
    assert fake_client.calls == 1


def _gating_vad_overrides(**overrides):
    values = {
        "compute_gating_enabled": True,
        "compute_wake_threshold_dbfs": -50.0,
        "compute_suspend_after_quiet_ms": 80,
        "compute_warmup_context_ms": 40,
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_vad_compute_gating_suspends_inference_during_quiet_and_resumes_on_sound():
    from backend.asr.service import AsrService

    config = _config(
        audio_trigger={"min_trigger_duration_ms": 20},
        vad=_gating_vad_overrides(),
    )
    holder: dict[str, FakeVad] = {}

    def factory() -> FakeVad:
        holder.setdefault("vad", FakeVad())
        return holder["vad"]

    service = AsrService(
        config,
        sglang_client=FakeAsrClient(),
        vad_factory=factory,
    )
    websocket = ControlledWebSocket()

    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")

    silence = np.zeros(320, np.float32)
    for _ in range(12):
        await websocket.send_pcm(silence)
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)

    vad = holder["vad"]
    calls_at_suspend = vad.accept_calls
    assert calls_at_suspend > 0
    assert vad.reset_count >= 1

    for _ in range(10):
        await websocket.send_pcm(silence)
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    assert vad.accept_calls == calls_at_suspend

    loud = np.full(320, 0.5, np.float32)
    for _ in range(4):
        await websocket.send_pcm(loud)
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    assert vad.accept_calls > calls_at_suspend

    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()


@pytest.mark.asyncio
async def test_vad_compute_gating_stays_inactive_when_spl_trigger_disabled():
    from backend.asr.service import AsrService

    config = _config(
        audio_trigger={"enabled": False},
        vad=_gating_vad_overrides(),
    )
    holder: dict[str, FakeVad] = {}

    def factory() -> FakeVad:
        holder.setdefault("vad", FakeVad())
        return holder["vad"]

    service = AsrService(
        config,
        sglang_client=FakeAsrClient(),
        vad_factory=factory,
    )
    websocket = ControlledWebSocket()

    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")

    silence = np.zeros(320, np.float32)
    for _ in range(12):
        await websocket.send_pcm(silence)
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)

    vad = holder["vad"]
    calls_before_quiet = vad.accept_calls
    assert calls_before_quiet > 0

    for _ in range(10):
        await websocket.send_pcm(silence)
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    assert vad.accept_calls > calls_before_quiet

    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()


@pytest.mark.asyncio
async def test_compute_gated_session_still_transcribes_utterance():
    from backend.asr.service import AsrService

    config = _config(
        recording={
            "end_no_voice_duration_ms": 20,
            "posttrigger_tail_ms": 0,
            "min_utterance_ms": 20,
        },
        audio_trigger={"min_trigger_duration_ms": 20},
        vad=_gating_vad_overrides(),
        debug={"replay": False},
    )
    holder: dict[str, FakeVad] = {}

    def factory() -> FakeVad:
        holder.setdefault("vad", FakeVad())
        return holder["vad"]

    service = AsrService(
        config,
        sglang_client=FakeAsrClient(),
        vad_factory=factory,
    )
    websocket = ControlledWebSocket()

    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")

    speech = np.full(320, 0.2, np.float32)
    silence = np.zeros(320, np.float32)
    for _ in range(12):
        await websocket.send_pcm(speech)
        await asyncio.sleep(0)
    for _ in range(6):
        await websocket.send_pcm(silence)
        await asyncio.sleep(0)

    transcript = await _wait_for_event(websocket, "transcript")
    assert transcript["text"] == "语音问题"
    assert holder["vad"].reset_count >= 1

    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()


def test_main_config_exposes_only_browser_safe_asr_values():
    from backend.main import get_current_config

    configured = _config()
    payload = asyncio.run(get_current_config())
    assert payload["asr"]["enabled"] is configured.enabled
    assert payload["asr"]["input_gain_db"] == configured.audio.input_gain_db
    assert (
        payload["asr"]["input_gain_min_db"]
        == configured.audio.input_gain_min_db
    )
    assert (
        payload["asr"]["input_gain_max_db"]
        == configured.audio.input_gain_max_db
    )
    assert (
        payload["asr"]["input_gain_step_db"]
        == configured.audio.input_gain_step_db
    )
    assert payload["asr"]["auto_send"] is configured.transcription.auto_send
    assert payload["asr"]["websocket_path"] == configured.transport.websocket_path
    assert (
        payload["asr"]["abort_timeout_seconds"]
        == configured.runtime.shutdown_timeout_seconds
    )
    assert (
        payload["asr"]["max_buffered_frames"]
        == configured.transport.max_buffered_frames
    )
    assert payload["asr"]["dictation_magic_send"] == {
        "commands": configured.dictation_magic_send.commands,
        "poll_interval_ms": configured.dictation_magic_send.poll_interval_ms,
    }
    assert "model_path" not in payload["asr"]
    assert "private_key_path" not in payload["asr"]


def test_main_registers_asr_websocket_route():
    from backend.main import app

    paths = {getattr(route, "path", None) for route in app.routes}
    for included in app.routes:
        original_router = getattr(included, "original_router", None)
        if original_router is not None:
            paths.update(getattr(route, "path", None) for route in original_router.routes)
    assert "/v1/asr/stream" in paths


def test_asr_router_factory_registers_configured_websocket_path():
    from backend.api.asr import create_asr_router

    app = FastAPI()
    app.include_router(create_asr_router("/custom/asr/socket"))

    paths = {getattr(route, "path", None) for route in app.routes}
    for included in app.routes:
        original_router = getattr(included, "original_router", None)
        if original_router is not None:
            paths.update(getattr(route, "path", None) for route in original_router.routes)
    assert "/custom/asr/socket" in paths
    assert "/v1/asr/stream" not in paths


@pytest.mark.asyncio
async def test_service_shutdown_closes_websocket_and_waits_for_receive_loop():
    from backend.asr.service import AsrService

    fake_client = FakeAsrClient()
    fake_client.closed = False

    async def close():
        fake_client.closed = True

    fake_client.close = close
    service = AsrService(
        _config(transport={"handshake_timeout_seconds": 30}),
        sglang_client=fake_client,
        vad_factory=lambda: FakeVad(),
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()

    await asyncio.wait_for(service.shutdown(), timeout=1.0)
    await asyncio.wait_for(run_task, timeout=1.0)

    assert websocket.closed.is_set()
    assert fake_client.closed is True
    assert (await service.health())["active_sessions"] == 0


@pytest.mark.asyncio
async def test_shutdown_awaits_active_transcription_terminal_barrier():
    from backend.asr.service import AsrService

    class CancellableClient(FakeAsrClient):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.worker_finished = asyncio.Event()
            self.closed_after_worker = False

        async def transcribe(self, wav_bytes, abort_event, on_delta=None):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                self.worker_finished.set()
                raise

        async def close(self):
            self.closed_after_worker = self.worker_finished.is_set()

    client = CancellableClient()
    service = AsrService(
        _short_utterance_config(),
        sglang_client=client,
        vad_factory=lambda: FakeVad(),
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")
    await websocket.send_pcm(np.full(320, 0.2, np.float32))
    await websocket.send_pcm(np.zeros(320, np.float32))
    await asyncio.wait_for(client.started.wait(), timeout=1.0)

    await asyncio.wait_for(service.shutdown(), timeout=1.0)
    await asyncio.wait_for(run_task, timeout=1.0)

    assert client.worker_finished.is_set()
    assert client.closed_after_worker is True
    assert websocket.closed.is_set()


@pytest.mark.asyncio
async def test_handshake_timeout_is_fatal_and_releases_session_admission():
    from backend.asr.service import AsrService

    service = AsrService(
        _config(
            transport={"handshake_timeout_seconds": 1},
            runtime={"max_active_sessions": 1},
        ),
        sglang_client=FakeAsrClient(),
        vad_factory=lambda: FakeVad(),
    )
    idle = ControlledWebSocket()
    idle_task = asyncio.create_task(service.run_websocket(idle))

    await asyncio.wait_for(idle_task, timeout=2.0)

    assert idle.closed.is_set()
    assert any(
        event.get("type") == "fatal_error" and event.get("code") == "handshake_timeout"
        for event in idle.sent
    )
    assert (await service.health())["active_sessions"] == 0

    replacement = ControlledWebSocket()
    replacement_task = asyncio.create_task(service.run_websocket(replacement))
    await replacement.accepted.wait()
    await replacement.send_control(_start_message())
    await _wait_for_event(replacement, "ready")
    await replacement.send_control({"type": "abort"})
    await asyncio.wait_for(replacement_task, timeout=1.0)
    assert replacement.closed.is_set()
    await service.shutdown()


@pytest.mark.asyncio
async def test_handshake_timeout_does_not_apply_after_ready():
    from backend.asr.service import AsrService

    service = AsrService(
        _config(transport={"handshake_timeout_seconds": 1}),
        sglang_client=FakeAsrClient(),
        vad_factory=lambda: FakeVad(),
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")

    await asyncio.sleep(1.1)

    assert not websocket.closed.is_set()
    assert not run_task.done()
    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_payload",
    [b"\x00\x00\x00", np.full(320, np.nan, np.float32).tobytes()],
)
async def test_invalid_pcm_is_recoverable_for_later_utterance(invalid_payload):
    from backend.asr.service import AsrService

    vad = FakeVad()
    client = FakeAsrClient()
    service = AsrService(
        _short_utterance_config(),
        sglang_client=client,
        vad_factory=lambda: vad,
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")
    await websocket.send_binary_from_client(invalid_payload)
    error = await _wait_for_event(websocket, "utterance_error")

    assert error == {
        "type": "utterance_error",
        "code": "invalid_pcm",
        "message": "Invalid PCM frame",
    }
    assert not websocket.closed.is_set()
    assert (await service.health())["queued_utterances"] == 0
    assert vad.accept_calls == 0

    await websocket.send_pcm(np.full(320, 0.2, np.float32))
    while vad.accept_calls < 1:
        await asyncio.sleep(0)
    await websocket.send_pcm(np.zeros(320, np.float32))
    transcript = await _wait_for_event(websocket, "transcript")

    assert transcript["text"] == "语音问题"
    assert client.calls == 1
    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()


@pytest.mark.asyncio
async def test_replay_debug_sends_wav_before_linked_transcript():
    from backend.asr.service import AsrService

    client = FakeAsrClient()
    service = AsrService(
        _short_utterance_config(debug={"replay": True}),
        sglang_client=client,
        vad_factory=lambda: FakeVad(),
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")

    await websocket.send_pcm(np.full(320, 0.2, np.float32))
    await websocket.send_pcm(np.zeros(320, np.float32))
    transcript = await _wait_for_event(websocket, "transcript")

    replay_events = [
        event for event in websocket.sent if event.get("type") == "replay_audio"
    ]
    assert replay_events, "debug.replay must emit replay metadata"
    replay = replay_events[0]
    assert replay["mime_type"] == "audio/wav"
    assert replay["byte_length"] > 44
    assert transcript["replay_utterance_id"] == replay["utterance_id"]
    assert len(websocket.sent_binary) == 1
    assert websocket.sent_binary[0].startswith(b"RIFF")
    assert len(websocket.sent_binary[0]) == replay["byte_length"]
    assert websocket.sent_binary[0] == client.wav_payloads[0]

    replay_index = websocket.outgoing.index(("json", replay))
    binary_index = websocket.outgoing.index(("bytes", websocket.sent_binary[0]))
    transcript_index = websocket.outgoing.index(("json", transcript))
    assert replay_index < binary_index < transcript_index

    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()


@pytest.mark.asyncio
async def test_replay_debug_disabled_keeps_transcript_protocol_text_only():
    from backend.asr.service import AsrService

    service = AsrService(
        _short_utterance_config(debug={"replay": False}),
        sglang_client=FakeAsrClient(),
        vad_factory=lambda: FakeVad(),
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")

    await websocket.send_pcm(np.full(320, 0.2, np.float32))
    await websocket.send_pcm(np.zeros(320, np.float32))
    transcript = await _wait_for_event(websocket, "transcript")

    assert websocket.sent_binary == []
    assert not any(event.get("type") == "replay_audio" for event in websocket.sent)
    assert "replay_utterance_id" not in transcript

    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()


@pytest.mark.asyncio
async def test_timeline_fault_is_recoverable_for_later_utterance():
    from backend.asr.service import AsrService

    class InvalidOnceVad(FakeVad):
        def accept(self, samples):
            self.accept_calls += 1
            if self.accept_calls == 1:
                return [VadObservation(2.0, False, False, False)]
            probability = 0.9 if float(np.max(np.abs(samples))) > 0.01 else 0.1
            return [
                VadObservation(
                    probability,
                    probability >= 0.5,
                    False,
                    probability >= 0.5,
                )
            ]

    vad = InvalidOnceVad()
    client = FakeAsrClient()
    service = AsrService(
        _short_utterance_config(),
        sglang_client=client,
        vad_factory=lambda: vad,
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")

    await websocket.send_pcm(np.full(320, 0.2, np.float32))
    error = await _wait_for_event(websocket, "utterance_error")

    assert error == {
        "type": "utterance_error",
        "code": "timeline_error",
        "message": "ASR audio timeline processing failed",
    }
    assert not websocket.closed.is_set()

    await websocket.send_pcm(np.full(320, 0.2, np.float32))
    while vad.accept_calls < 2:
        await asyncio.sleep(0)
    await websocket.send_pcm(np.zeros(320, np.float32))
    transcript = await _wait_for_event(websocket, "transcript")

    assert transcript["text"] == "语音问题"
    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()


@pytest.mark.asyncio
async def test_frame_queue_processes_pcm_in_receive_order():
    from backend.asr.service import AsrService

    class OrderingVad(FakeVad):
        def __init__(self):
            super().__init__()
            self.means = []

        def accept(self, samples):
            self.accept_calls += 1
            self.means.append(float(np.mean(samples)))
            return [VadObservation(0.1, False, False, False)]

    vad = OrderingVad()
    service = AsrService(
        _config(transport={"max_buffered_frames": 8}),
        sglang_client=FakeAsrClient(),
        vad_factory=lambda: vad,
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")

    for value in (0.01, 0.02, 0.03):
        await websocket.send_pcm(np.full(320, value, np.float32))
    while vad.accept_calls < 3:
        await asyncio.sleep(0)

    assert vad.means == sorted(vad.means)
    assert len(set(vad.means)) == 3
    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()


@pytest.mark.asyncio
async def test_runtime_input_gain_changes_following_pcm_without_rewriting_previous_frame():
    from backend.asr.service import AsrService

    vad = CapturingVad()
    service = AsrService(
        _config(audio={"input_gain_db": 0.0}),
        sglang_client=FakeAsrClient(),
        vad_factory=lambda: vad,
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")

    pcm = np.full(320, 0.05, np.float32)
    await websocket.send_pcm(pcm)
    await _wait_for_frames(vad, 1)
    await websocket.send_control({"type": "set_input_gain", "input_gain_db": 20.0})
    acknowledgement = await _wait_for_event(websocket, "input_gain_changed")
    await websocket.send_pcm(pcm)
    await _wait_for_frames(vad, 2)

    assert acknowledgement == {"type": "input_gain_changed", "input_gain_db": 20.0}
    assert np.allclose(vad.frames[0], 0.05)
    assert np.allclose(vad.frames[1], 0.5)

    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()


@pytest.mark.asyncio
async def test_runtime_input_gain_can_change_after_recording_started():
    from backend.asr.service import AsrService

    vad = CapturingVad()
    service = AsrService(
        _short_utterance_config(audio={"input_gain_db": 0.0}),
        sglang_client=FakeAsrClient(),
        vad_factory=lambda: vad,
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")

    for _ in range(2):
        await websocket.send_pcm(np.full(320, 0.05, np.float32))
    await _wait_for_event_count(websocket, "state_changed", 2)
    assert any(event.get("state") == "recording" for event in websocket.sent)

    await websocket.send_control({"type": "set_input_gain", "input_gain_db": 20.0})
    assert (await _wait_for_event(websocket, "input_gain_changed"))["input_gain_db"] == 20.0
    prior_count = len(vad.frames)
    await websocket.send_pcm(np.full(320, 0.05, np.float32))
    await _wait_for_frames(vad, prior_count + 1)
    assert np.allclose(vad.frames[-1], 0.5)
    assert not websocket.closed.is_set()

    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()


@pytest.mark.asyncio
async def test_invalid_runtime_input_gain_is_nonfatal_and_preserves_last_value():
    from backend.asr.service import AsrService

    service = AsrService(
        _config(audio={"input_gain_db": 0.0}),
        sglang_client=FakeAsrClient(),
        vad_factory=lambda: CapturingVad(),
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")
    await websocket.send_control({"type": "set_input_gain", "input_gain_db": 20.0})
    await _wait_for_event(websocket, "input_gain_changed")
    await websocket.send_control({"type": "set_input_gain", "input_gain_db": 20.25})
    error = await _wait_for_event(websocket, "control_error")

    assert error["code"] == "invalid_input_gain"
    assert error["input_gain_db"] == 20.0
    assert not websocket.closed.is_set()
    await websocket.send_pcm(np.full(320, 0.05, np.float32))
    await _wait_for_event(websocket, "level")

    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()


@pytest.mark.asyncio
async def test_runtime_input_gain_is_isolated_per_websocket_session():
    from backend.asr.service import AsrService

    vads: list[CapturingVad] = []

    def factory() -> CapturingVad:
        vad = CapturingVad()
        vads.append(vad)
        return vad

    service = AsrService(
        _config(audio={"input_gain_db": 0.0}),
        sglang_client=FakeAsrClient(),
        vad_factory=factory,
    )
    first = ControlledWebSocket()
    second = ControlledWebSocket()
    first_task = asyncio.create_task(service.run_websocket(first))
    second_task = asyncio.create_task(service.run_websocket(second))
    await first.accepted.wait()
    await second.accepted.wait()
    for websocket in (first, second):
        await websocket.send_control(_start_message())
        await _wait_for_event(websocket, "ready")
    await first.send_control({"type": "set_input_gain", "input_gain_db": 20.0})
    await _wait_for_event(first, "input_gain_changed")
    pcm = np.full(320, 0.05, np.float32)
    await first.send_pcm(pcm)
    await second.send_pcm(pcm)
    await _wait_for_frames(vads[0], 1)
    await _wait_for_frames(vads[1], 1)

    assert np.allclose(vads[0].frames[0], 0.5)
    assert np.allclose(vads[1].frames[0], 0.05)

    for websocket in (first, second):
        await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(first_task, timeout=1.0)
    await asyncio.wait_for(second_task, timeout=1.0)
    await service.shutdown()


@pytest.mark.asyncio
async def test_legacy_client_uses_yaml_default_without_gain_control():
    from backend.asr.service import AsrService

    vad = CapturingVad()
    service = AsrService(
        _config(audio={"input_gain_db": 12.0}),
        sglang_client=FakeAsrClient(),
        vad_factory=lambda: vad,
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")
    await websocket.send_pcm(np.full(320, 0.05, np.float32))
    await _wait_for_frames(vad, 1)

    assert np.allclose(vad.frames[0], 0.1990536, rtol=1e-5)

    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()


@pytest.mark.asyncio
async def test_queued_pcm_captures_input_gain_at_receive_time():
    from backend.asr.session import AsrSession

    session = AsrSession(
        ControlledWebSocket(),
        _config(audio={"input_gain_db": 0.0}),
        sglang_client=FakeAsrClient(),
        vad_factory=lambda: FakeVad(),
        inference_semaphore=asyncio.Semaphore(1),
    )
    payload = np.full(320, 0.05, np.float32).tobytes()

    await session._enqueue_binary(payload)
    session._input_gain_db = 20.0
    await session._enqueue_binary(payload)
    first = session._frame_queue.get_nowait()
    second = session._frame_queue.get_nowait()

    assert first.input_gain_db == 0.0
    assert second.input_gain_db == 20.0
    session._frame_queue.task_done()
    session._frame_queue.task_done()


@pytest.mark.asyncio
async def test_frame_queue_overflow_resets_and_same_session_recovers():
    from backend.asr.service import AsrService

    vad = FakeVad()
    client = FakeAsrClient()
    service = AsrService(
        _short_utterance_config(transport={"max_buffered_frames": 1}),
        sglang_client=client,
        vad_factory=lambda: vad,
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")

    frame = np.full(320, 0.2, np.float32).tobytes()
    await websocket.incoming.put({"type": "websocket.receive", "bytes": frame})
    await websocket.incoming.put({"type": "websocket.receive", "bytes": frame})
    error = await _wait_for_event(websocket, "utterance_error")

    assert error == {
        "type": "utterance_error",
        "code": "queue_full",
        "message": "ASR frame queue is full",
    }
    assert not websocket.closed.is_set()
    assert (await service.health())["queued_utterances"] == 0

    baseline_calls = vad.accept_calls
    await websocket.send_pcm(np.full(320, 0.2, np.float32))
    while vad.accept_calls == baseline_calls:
        await asyncio.sleep(0)
    await websocket.send_pcm(np.zeros(320, np.float32))
    transcript = await _wait_for_event(websocket, "transcript")

    assert transcript["text"] == "语音问题"
    assert client.calls == 1
    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()


@pytest.mark.asyncio
async def test_abort_cancels_and_awaits_frame_worker_barrier():
    from backend.asr.session import AsrSession

    websocket = ControlledWebSocket()
    session = AsrSession(
        websocket,
        _config(),
        sglang_client=FakeAsrClient(),
        vad_factory=lambda: FakeVad(),
        inference_semaphore=asyncio.Semaphore(1),
    )
    worker_started = asyncio.Event()
    worker_finished = asyncio.Event()

    async def blocked_processor(payload, start_sample_index, input_gain_db):
        assert start_sample_index == 0
        assert input_gain_db == 0.0
        worker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            worker_finished.set()

    session._process_binary = blocked_processor
    run_task = asyncio.create_task(session.run())
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")
    await websocket.send_pcm(np.zeros(320, np.float32))
    await asyncio.wait_for(worker_started.wait(), timeout=1.0)

    await asyncio.wait_for(session.abort(send_event=False), timeout=1.0)
    await asyncio.wait_for(run_task, timeout=1.0)

    assert worker_finished.is_set()
    assert websocket.closed.is_set()


@pytest.mark.asyncio
async def test_vad_reset_failure_after_inference_recovers_session():
    from backend.asr.service import AsrService

    class ResetFailsOnceVad(FakeVad):
        def __init__(self):
            super().__init__()
            self.reset_calls = 0

        def reset(self):
            self.reset_calls += 1
            if self.reset_calls == 1:
                raise RuntimeError("private reset detail")

    vad = ResetFailsOnceVad()
    client = FakeAsrClient()
    service = AsrService(
        _short_utterance_config(),
        sglang_client=client,
        vad_factory=lambda: vad,
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")

    await websocket.send_pcm(np.full(320, 0.2, np.float32))
    await asyncio.sleep(0)
    await websocket.send_pcm(np.zeros(320, np.float32))
    await _wait_for_event_count(websocket, "transcript", 1)
    errors = await _wait_for_event_count(websocket, "utterance_error", 1)

    assert errors[-1] == {
        "type": "utterance_error",
        "code": "vad_reset_failed",
        "message": "VAD state reset failed",
    }
    assert "private reset detail" not in errors[-1]["message"]

    await websocket.send_pcm(np.full(320, 0.2, np.float32))
    await asyncio.sleep(0)
    await websocket.send_pcm(np.zeros(320, np.float32))
    await _wait_for_event_count(websocket, "transcript", 2)

    assert client.calls == 2
    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_worker_blocked_sending_vad_reset_error():
    from backend.asr.service import AsrService

    class ResetFailsOnceVad(FakeVad):
        def __init__(self):
            super().__init__()
            self.reset_calls = 0

        def reset(self):
            self.reset_calls += 1
            if self.reset_calls == 1:
                raise RuntimeError("private reset detail")

    class BlockingResetErrorWebSocket(ControlledWebSocket):
        def __init__(self):
            super().__init__()
            self.reset_error_send_started = asyncio.Event()
            self.reset_error_send_finished = asyncio.Event()
            self.release_reset_error_send = asyncio.Event()

        async def send_json(self, payload):
            if payload.get("code") == "vad_reset_failed":
                self.reset_error_send_started.set()
                try:
                    await self.release_reset_error_send.wait()
                finally:
                    self.reset_error_send_finished.set()
            await super().send_json(payload)

    service = AsrService(
        _short_utterance_config(),
        sglang_client=FakeAsrClient(),
        vad_factory=lambda: ResetFailsOnceVad(),
    )
    websocket = BlockingResetErrorWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")
    await websocket.send_pcm(np.full(320, 0.2, np.float32))
    await asyncio.sleep(0)
    await websocket.send_pcm(np.zeros(320, np.float32))
    await _wait_for_event(websocket, "transcript")
    await asyncio.wait_for(websocket.reset_error_send_started.wait(), timeout=1.0)
    session = next(iter(service._sessions.values()))

    await asyncio.wait_for(service.shutdown(), timeout=1.0)
    await asyncio.wait_for(run_task, timeout=1.0)
    try:
        assert websocket.reset_error_send_finished.is_set()
        assert not session._retiring_inference_tasks
    finally:
        websocket.release_reset_error_send.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_vad_reset_failure_during_abort_does_not_break_barrier():
    from backend.asr.service import AsrService

    class ResetAlwaysFailsVad(FakeVad):
        def reset(self):
            raise RuntimeError("private abort reset detail")

    vad = ResetAlwaysFailsVad()
    service = AsrService(
        _config(),
        sglang_client=FakeAsrClient(),
        vad_factory=lambda: vad,
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")

    await asyncio.wait_for(service.shutdown(), timeout=1.0)
    await asyncio.wait_for(run_task, timeout=1.0)

    assert websocket.closed.is_set()


@pytest.mark.asyncio
async def test_abort_during_transcribing_send_logs_cancelled_once(caplog):
    from backend.asr.service import AsrService

    class BlockingTranscribingWebSocket(ControlledWebSocket):
        def __init__(self):
            super().__init__()
            self.transcribing_send_started = asyncio.Event()

        async def send_json(self, payload):
            if payload.get("type") == "transcribing":
                self.transcribing_send_started.set()
                await asyncio.Event().wait()
            await super().send_json(payload)

    service = AsrService(
        _short_utterance_config(),
        sglang_client=FakeAsrClient(),
        vad_factory=lambda: FakeVad(),
    )
    websocket = BlockingTranscribingWebSocket()
    with caplog.at_level(logging.INFO, logger="backend.asr.session"):
        run_task = asyncio.create_task(service.run_websocket(websocket))
        await websocket.accepted.wait()
        await websocket.send_control(_start_message())
        await _wait_for_event(websocket, "ready")
        await websocket.send_pcm(np.full(320, 0.2, np.float32))
        await asyncio.sleep(0)
        await websocket.send_pcm(np.zeros(320, np.float32))
        await asyncio.wait_for(
            websocket.transcribing_send_started.wait(), timeout=1.0
        )

        await asyncio.wait_for(service.shutdown(), timeout=1.0)
        await asyncio.wait_for(run_task, timeout=1.0)

    diagnostics = _utterance_log_payloads(caplog)
    assert len(diagnostics) == 1
    assert diagnostics[0]["error_code"] == "cancelled"


@pytest.mark.asyncio
async def test_abort_bounds_debug_write_with_one_shutdown_deadline(
    tmp_path, caplog, monkeypatch
):
    from backend.asr.service import AsrService
    from backend.asr import session as session_module

    write_started = threading.Event()
    release_write = threading.Event()
    write_finished = threading.Event()
    original_open = open

    def blocked_open(path, *args, **kwargs):
        write_started.set()
        release_write.wait()
        try:
            return original_open(path, *args, **kwargs)
        finally:
            write_finished.set()

    monkeypatch.setattr(session_module, "open", blocked_open, raising=False)
    config = _short_utterance_config(
        runtime={"shutdown_timeout_seconds": 1},
        observability={
            "save_debug_audio": True,
            "debug_audio_directory": str(tmp_path / "debug-audio"),
            "log_vad_probability": False,
            "log_audio_level": False,
            "log_state_transitions": False,
        }
    )
    service = AsrService(
        config,
        sglang_client=FakeAsrClient(),
        vad_factory=lambda: FakeVad(),
    )
    websocket = ControlledWebSocket()

    with caplog.at_level(logging.INFO, logger="backend.asr.session"):
        run_task = asyncio.create_task(service.run_websocket(websocket))
        await websocket.accepted.wait()
        await websocket.send_control(_start_message())
        await _wait_for_event(websocket, "ready")
        await websocket.send_pcm(np.full(320, 0.2, np.float32))
        await asyncio.sleep(0)
        await websocket.send_pcm(np.zeros(320, np.float32))
        assert await asyncio.to_thread(write_started.wait, 1.0)

        started = asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(service.shutdown(), timeout=2.0)
            elapsed = asyncio.get_running_loop().time() - started
            assert 0.8 <= elapsed < 1.5
            assert not write_finished.is_set()
        finally:
            release_write.set()
        await asyncio.wait_for(run_task, timeout=1.0)

    assert await asyncio.to_thread(write_finished.wait, 1.0)
    diagnostics = _utterance_log_payloads(caplog)
    assert len(diagnostics) == 1
    assert diagnostics[0]["error_code"] == "cancelled"


@pytest.mark.asyncio
async def test_completed_utterance_logs_safe_fields_and_saves_debug_wav(
    tmp_path, caplog
):
    from backend.asr.service import AsrService

    debug_directory = tmp_path / "debug-audio"
    debug_directory.mkdir(mode=0o777)
    debug_directory.chmod(0o777)
    config = _short_utterance_config(
        audio={"input_gain_db": 12.0},
        observability={
            "save_debug_audio": True,
            "debug_audio_directory": str(debug_directory),
            "log_vad_probability": False,
            "log_audio_level": False,
            "log_state_transitions": False,
        }
    )
    client = FakeAsrClient()
    service = AsrService(
        config,
        sglang_client=client,
        vad_factory=lambda: FakeVad(),
    )
    websocket = ControlledWebSocket()

    with caplog.at_level(logging.INFO, logger="backend.asr.session"):
        run_task = asyncio.create_task(service.run_websocket(websocket))
        await websocket.accepted.wait()
        await websocket.send_control(_start_message())
        ready = await _wait_for_event(websocket, "ready")
        await websocket.send_pcm(np.full(320, 0.5, np.float32))
        await asyncio.sleep(0)
        await websocket.send_pcm(np.zeros(320, np.float32))
        await _wait_for_event(websocket, "transcript")
        await websocket.send_control({"type": "abort"})
        await asyncio.wait_for(run_task, timeout=1.0)
        await service.shutdown()

    records = [
        record.args[0]
        for record in caplog.records
        if "asr_utterance" in str(record.msg) and record.args
    ]
    assert len(records) == 1
    diagnostics = json.loads(records[0])
    assert set(diagnostics) == {
        "utterance_id",
        "session_id_hash",
        "start_sample_index",
        "end_sample_index",
        "pretrigger_samples",
        "recorded_samples",
        "duration_ms",
        "trigger_dbfs",
        "max_dbfs",
        "clipping_ratio",
        "silero_start_probability",
        "silero_confirmed",
        "silero_end_reason",
        "asr_latency_ms",
        "asr_text_length",
        "max_duration_reached",
        "error_code",
    }
    assert diagnostics["session_id_hash"] != ready["session_id"]
    assert diagnostics["silero_confirmed"] is True
    assert diagnostics["clipping_ratio"] == pytest.approx(0.5)
    assert diagnostics["asr_latency_ms"] == 5
    assert diagnostics["asr_text_length"] == len("语音问题")
    assert diagnostics["error_code"] is None

    files = list(debug_directory.glob("*.wav"))
    assert len(files) == 1
    assert stat.S_IMODE(debug_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    uuid.UUID(files[0].stem)
    assert files[0].read_bytes().startswith(b"RIFF")
    assert "语音问题" not in caplog.text
    assert "语音问题" not in files[0].name


@pytest.mark.asyncio
@pytest.mark.parametrize("silero_confirmed", [True, False])
async def test_debug_audio_disabled_or_unconfirmed_creates_no_file(
    tmp_path, silero_confirmed
):
    from backend.asr.service import AsrService

    debug_directory = tmp_path / "must-not-exist"
    config = _short_utterance_config(
        audio_trigger={
            "min_trigger_duration_ms": 20,
            "require_vad_confirmation": silero_confirmed,
        },
        observability={
            "save_debug_audio": not silero_confirmed,
            "debug_audio_directory": str(debug_directory),
            "log_vad_probability": False,
            "log_audio_level": False,
            "log_state_transitions": False,
        },
    )

    class ConfiguredVad(FakeVad):
        def accept(self, samples):
            self.accept_calls += 1
            if silero_confirmed:
                return super().accept(samples)
            return [VadObservation(0.1, False, False, False)]

    service = AsrService(
        config,
        sglang_client=FakeAsrClient(),
        vad_factory=lambda: ConfiguredVad(),
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")
    await websocket.send_pcm(np.full(320, 0.2, np.float32))
    await asyncio.sleep(0)
    await websocket.send_pcm(np.zeros(320, np.float32))
    await _wait_for_event(websocket, "transcript")
    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()

    assert not debug_directory.exists()


@pytest.mark.asyncio
async def test_transient_vad_failure_recovers_for_later_utterance():
    from backend.asr.service import AsrService

    class FailsOnceVad(FakeVad):
        def __init__(self):
            super().__init__()
            self.failed = False
            self.reset_count = 0

        def accept(self, samples):
            if not self.failed:
                self.failed = True
                raise RuntimeError("private VAD detail")
            return super().accept(samples)

        def reset(self):
            self.reset_count += 1

    vad = FailsOnceVad()
    client = FakeAsrClient()
    service = AsrService(
        _short_utterance_config(),
        sglang_client=client,
        vad_factory=lambda: vad,
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")

    await websocket.send_pcm(np.full(320, 0.2, np.float32))
    error = await _wait_for_event(websocket, "utterance_error")

    assert error == {
        "type": "utterance_error",
        "code": "vad_failed",
        "message": "VAD processing failed",
    }
    assert not websocket.closed.is_set()
    assert "private VAD detail" not in error["message"]

    await websocket.send_pcm(np.full(320, 0.2, np.float32))
    await websocket.send_pcm(np.zeros(320, np.float32))
    transcript = await _wait_for_event(websocket, "transcript")

    assert transcript["text"] == "语音问题"
    assert client.calls == 1
    assert vad.reset_count >= 1
    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["malformed_result", "unexpected_exception"])
async def test_transcription_failure_recovers_for_second_utterance(first_failure):
    from backend.asr.service import AsrService

    class RecoveringClient(FakeAsrClient):
        async def transcribe(self, wav_bytes, abort_event, on_delta=None):
            self.calls += 1
            if self.calls == 1:
                if first_failure == "unexpected_exception":
                    raise RuntimeError("private upstream detail")
                return AsrResult(
                    "", None, 0, 1, "Invalid ASR upstream response"
                )
            return AsrResult("second succeeds", None, 20, 1, None)

    client = RecoveringClient()
    service = AsrService(
        _short_utterance_config(),
        sglang_client=client,
        vad_factory=lambda: FakeVad(),
    )
    websocket = ControlledWebSocket()
    run_task = asyncio.create_task(service.run_websocket(websocket))
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")

    for samples in (np.full(320, 0.2, np.float32), np.zeros(320, np.float32)):
        await websocket.send_pcm(samples)
    error = await _wait_for_event(websocket, "utterance_error")
    assert error["code"] == "asr_failed"
    assert "private upstream detail" not in error["message"]

    for samples in (np.full(320, 0.2, np.float32), np.zeros(320, np.float32)):
        await websocket.send_pcm(samples)
    transcript = await _wait_for_event(websocket, "transcript")

    assert transcript["text"] == "second succeeds"
    assert client.calls == 2
    await websocket.send_control({"type": "abort"})
    await asyncio.wait_for(run_task, timeout=1.0)
    await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", [False, True])
async def test_utterance_readiness_retires_worker_before_next_pcm(first_failure):
    from backend.asr.session import AsrSession

    class Client(FakeAsrClient):
        async def transcribe(self, wav_bytes, abort_event, on_delta=None):
            self.calls += 1
            return AsrResult(
                f"phrase {self.calls}", None, 20, 1,
                "first failed" if first_failure and self.calls == 1 else None,
            )

    class ReadyWebSocket(ControlledWebSocket):
        def __init__(self):
            super().__init__()
            self.readiness_count = 0

        async def send_json(self, payload):
            if payload == {"type": "state_changed", "state": "buffering"}:
                assert session._inference_task is None
                assert not session._transcribing
                assert session._machine.state.value == "buffering"
                self.readiness_count += 1
                if self.readiness_count == 1:
                    await self.send_pcm(np.full(320, 0.2, np.float32))
                    await self.send_pcm(np.zeros(320, np.float32))
            await super().send_json(payload)

    websocket = ReadyWebSocket()
    client = Client()
    session = AsrSession(
        websocket, _short_utterance_config(), sglang_client=client,
        vad_factory=lambda: FakeVad(), inference_semaphore=asyncio.Semaphore(1),
    )
    run_task = asyncio.create_task(session.run())
    try:
        await websocket.accepted.wait()
        await websocket.send_control(_start_message())
        await _wait_for_event(websocket, "ready")
        await websocket.send_pcm(np.full(320, 0.2, np.float32))
        await websocket.send_pcm(np.zeros(320, np.float32))
        await _wait_for_event_count(websocket, "transcript", 1 if first_failure else 2)
        async def wait_ready():
            while websocket.readiness_count < 2:
                await asyncio.sleep(0)
        await asyncio.wait_for(wait_ready(), 1)
        assert client.calls == 2
    finally:
        await websocket.send_control({"type": "abort"})
        await asyncio.wait_for(run_task, 1)


@pytest.mark.asyncio
async def test_abort_cancels_a_blocked_utterance_readiness_send():
    from backend.asr.session import AsrSession

    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockedReadyWebSocket(ControlledWebSocket):
        async def send_json(self, payload):
            if payload == {"type": "state_changed", "state": "buffering"}:
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            await super().send_json(payload)

    websocket = BlockedReadyWebSocket()
    session = AsrSession(
        websocket, _short_utterance_config(), sglang_client=FakeAsrClient(),
        vad_factory=lambda: FakeVad(), inference_semaphore=asyncio.Semaphore(1),
    )
    run_task = asyncio.create_task(session.run())
    await websocket.accepted.wait()
    await websocket.send_control(_start_message())
    await _wait_for_event(websocket, "ready")
    await websocket.send_pcm(np.full(320, 0.2, np.float32))
    await websocket.send_pcm(np.zeros(320, np.float32))
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(session.abort(send_event=False), 0.2)
    assert cancelled.is_set(), "a retiring worker must remain owned by session cleanup"
    await asyncio.wait_for(run_task, 1)


@pytest.mark.asyncio
async def test_health_reports_sessions_waiting_for_inference_semaphore():
    from backend.asr.service import AsrService

    class BlockingClient(FakeAsrClient):
        def __init__(self):
            super().__init__()
            self.first_started = asyncio.Event()
            self.release = asyncio.Event()

        async def transcribe(self, wav_bytes, abort_event, on_delta=None):
            self.calls += 1
            self.first_started.set()
            await self.release.wait()
            return AsrResult(f"result {self.calls}", None, 20, 1, None)

    client = BlockingClient()
    service = AsrService(
        _short_utterance_config(),
        sglang_client=client,
        vad_factory=lambda: FakeVad(),
    )
    sockets = [ControlledWebSocket(), ControlledWebSocket()]
    tasks = [asyncio.create_task(service.run_websocket(socket)) for socket in sockets]
    for socket in sockets:
        await socket.accepted.wait()
        await socket.send_control(_start_message())
        await _wait_for_event(socket, "ready")

    speech = np.full(320, 0.2, np.float32)
    silence = np.zeros(320, np.float32)
    await sockets[0].send_pcm(speech)
    await sockets[0].send_pcm(silence)
    await client.first_started.wait()
    await sockets[1].send_pcm(speech)
    await sockets[1].send_pcm(silence)

    async def wait_for_queue():
        while True:
            health = await service.health()
            if health["queued_utterances"] == 1:
                return health
            await asyncio.sleep(0)

    health = await asyncio.wait_for(wait_for_queue(), timeout=1.0)
    assert health["active_sessions"] == 2

    client.release.set()
    for socket in sockets:
        await _wait_for_event(socket, "transcript")
        await socket.send_control({"type": "abort"})
    await asyncio.gather(*tasks)
    assert (await service.health())["queued_utterances"] == 0
    await service.shutdown()


@pytest.mark.asyncio
async def test_cancelling_queued_utterance_decrements_queue_depth():
    from backend.asr.service import AsrService

    class BlockingClient(FakeAsrClient):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def transcribe(self, wav_bytes, abort_event, on_delta=None):
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return AsrResult("first result", None, 20, 1, None)

    client = BlockingClient()
    service = AsrService(
        _short_utterance_config(),
        sglang_client=client,
        vad_factory=lambda: FakeVad(),
    )
    sockets = [ControlledWebSocket(), ControlledWebSocket()]
    tasks = [asyncio.create_task(service.run_websocket(socket)) for socket in sockets]
    for socket in sockets:
        await socket.accepted.wait()
        await socket.send_control(_start_message())
        await _wait_for_event(socket, "ready")

    speech = np.full(320, 0.2, np.float32)
    silence = np.zeros(320, np.float32)
    await sockets[0].send_pcm(speech)
    await sockets[0].send_pcm(silence)
    await client.started.wait()
    await sockets[1].send_pcm(speech)
    await sockets[1].send_pcm(silence)

    async def wait_for_queue_depth(expected):
        while (await service.health())["queued_utterances"] != expected:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_queue_depth(1), timeout=1.0)
    await sockets[1].send_control({"type": "abort"})
    await asyncio.wait_for(tasks[1], timeout=1.0)
    await asyncio.wait_for(wait_for_queue_depth(0), timeout=1.0)

    assert client.calls == 1
    client.release.set()
    await _wait_for_event(sockets[0], "transcript")
    await sockets[0].send_control({"type": "abort"})
    await asyncio.wait_for(tasks[0], timeout=1.0)
    await service.shutdown()


@pytest.mark.asyncio
async def test_shutdown_gate_rejects_racing_admission_and_is_idempotent():
    from backend.asr.service import AsrService

    class BlockingCloseClient(FakeAsrClient):
        def __init__(self):
            super().__init__()
            self.close_started = asyncio.Event()
            self.release_close = asyncio.Event()
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1
            self.close_started.set()
            await self.release_close.wait()

    client = BlockingCloseClient()
    vad_factory_calls = 0

    def vad_factory():
        nonlocal vad_factory_calls
        vad_factory_calls += 1
        return FakeVad()

    service = AsrService(
        _config(),
        sglang_client=client,
        vad_factory=vad_factory,
    )
    first_shutdown = asyncio.create_task(service.shutdown())
    await asyncio.wait_for(client.close_started.wait(), timeout=1.0)
    second_shutdown = asyncio.create_task(service.shutdown())
    websocket = ControlledWebSocket()

    await asyncio.wait_for(service.run_websocket(websocket), timeout=1.0)

    assert websocket.closed.is_set()
    assert websocket.sent == [
        {
            "type": "fatal_error",
            "code": "shutting_down",
            "message": "ASR service is shutting down",
        }
    ]
    assert vad_factory_calls == 0
    assert client.calls == 0
    assert not second_shutdown.done()

    client.release_close.set()
    await asyncio.gather(first_shutdown, second_shutdown)
    assert client.close_calls == 1
