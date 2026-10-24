"""Lifecycle for one browser microphone WebSocket generation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect

from backend.asr.audio import apply_gain, encode_wav, rms_dbfs
from backend.asr.config import AsrConfig
from backend.asr.models import IndexedAudioFrame, RecordingState, VadObservation
from backend.asr.state_machine import RecordingStateMachine
from backend.utils.logger import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class QueuedAudioFrame:
    generation: int
    payload: bytes
    start_sample_index: int
    input_gain_db: float


class AsrSession:
    def __init__(
        self,
        websocket: WebSocket,
        config: AsrConfig,
        *,
        sglang_client,
        vad_factory: Callable[[], object],
        inference_semaphore: asyncio.Semaphore,
        inference_waiter_started: Callable[[], None] | None = None,
        inference_waiter_finished: Callable[[], None] | None = None,
    ) -> None:
        self.session_id = str(uuid.uuid4())
        self._websocket = websocket
        self._config = config
        self._client = sglang_client
        self._vad_factory = vad_factory
        self._vad = vad_factory()
        self._semaphore = inference_semaphore
        self._inference_waiter_started = inference_waiter_started or (lambda: None)
        self._inference_waiter_finished = inference_waiter_finished or (lambda: None)
        self._abort_event = asyncio.Event()
        self._abort_lock = asyncio.Lock()
        self._audio_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._run_finished = asyncio.Event()
        self._inference_task: asyncio.Task | None = None
        self._retiring_inference_tasks: set[asyncio.Task] = set()
        self._frame_worker_task: asyncio.Task | None = None
        self._debug_write_task: asyncio.Task | None = None
        self._active_utterance_id: str | None = None
        self._active_utterance = None
        self._active_utterance_started_at = 0.0
        self._input_gain_db = config.audio.input_gain_db
        self._frame_queue: asyncio.Queue[QueuedAudioFrame] = asyncio.Queue(
            maxsize=config.transport.max_buffered_frames
        )
        self._frame_generation = 0
        self._processing_generation = 0
        self._transcribing = False
        self._handshake_complete = False
        self._closed = False
        self._abort_cleanup_complete = False
        self._websocket_closed = False
        self._aborted_event_sent = False
        self._next_sample_index = 0
        self._last_committed_sample_index = 0
        self._latest_vad = VadObservation(0.0, False, False, False)
        self._vad_compute_suspended = False
        self._vad_quiet_samples = 0
        self._vad_warmup_remaining_samples = 0
        self._last_level_sent = 0.0
        self._machine = self._new_machine()

    def _new_machine(self) -> RecordingStateMachine:
        return RecordingStateMachine(
            audio_config=self._config.audio,
            recording_config=self._config.recording,
            trigger_config=self._config.audio_trigger,
            vad_config=self._config.vad,
            last_committed_sample_index=self._last_committed_sample_index,
        )

    async def run(self) -> None:
        await self._websocket.accept()
        try:
            while not self._closed:
                try:
                    if self._handshake_complete:
                        message = await self._websocket.receive()
                    else:
                        message = await asyncio.wait_for(
                            self._websocket.receive(),
                            timeout=self._config.transport.handshake_timeout_seconds,
                        )
                except asyncio.TimeoutError:
                    await self._fatal(
                        "handshake_timeout", "ASR start handshake timed out"
                    )
                    break
                if self._closed:
                    break
                message_type = message.get("type")
                if message_type == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    if not self._handshake_complete:
                        await self._fatal("handshake_required", "Start handshake required")
                        break
                    if not self._transcribing and self._inference_task is None:
                        await self._enqueue_binary(message["bytes"])
                    if self._closed:
                        break
                    continue
                text = message.get("text")
                if text is not None:
                    if not await self._accept_control(text):
                        break
        except WebSocketDisconnect:
            pass
        finally:
            await self.abort(send_event=False)
            self._run_finished.set()

    async def _accept_control(self, text: str) -> bool:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            await self._fatal("invalid_json", "Invalid ASR control message")
            return False
        if not isinstance(payload, dict):
            await self._fatal("invalid_control", "ASR control message must be an object")
            return False
        if payload.get("type") == "abort":
            await self.abort(send_event=True)
            return False
        if payload.get("type") == "set_input_gain":
            if not self._handshake_complete:
                await self._send_input_gain_error("Start handshake required")
                return True
            value = payload.get("input_gain_db")
            if not self._config.audio.accepts_input_gain_db(value):
                await self._send_input_gain_error(
                    "Input gain is outside the configured range or step"
                )
                return True
            self._input_gain_db = float(value)
            await self._send(
                {"type": "input_gain_changed", "input_gain_db": self._input_gain_db}
            )
            return True
        if payload.get("type") != "start" or self._handshake_complete:
            await self._fatal("invalid_control", "Expected one start handshake")
            return False
        expected = {
            "protocol_version": 1,
            "sample_rate_hz": self._config.audio.sample_rate_hz,
            "channels": self._config.audio.channels,
            "sample_format": self._config.audio.sample_format,
            "frame_samples": self._config.audio.frame_samples,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                await self._fatal("invalid_handshake", f"Invalid handshake field: {key}")
                return False
        self._handshake_complete = True
        self._frame_worker_task = asyncio.create_task(self._frame_worker())
        await self._send(
            {
                "type": "ready",
                "session_id": self.session_id,
                "protocol_version": 1,
                "state": "buffering",
            }
        )
        return True

    async def _send_input_gain_error(self, message: str) -> None:
        await self._send(
            {
                "type": "control_error",
                "code": "invalid_input_gain",
                "message": message,
                "input_gain_db": self._input_gain_db,
            }
        )

    async def _enqueue_binary(self, payload: bytes) -> None:
        expected_bytes = self._config.audio.frame_samples * 4
        if len(payload) != expected_bytes:
            await self._recover_audio_error("invalid_pcm", "Invalid PCM frame")
            return
        raw = np.frombuffer(payload, dtype="<f4").astype(np.float32, copy=False)
        if not np.isfinite(raw).all():
            await self._recover_audio_error("invalid_pcm", "Invalid PCM frame")
            return

        start_sample_index = self._next_sample_index
        self._next_sample_index += raw.size
        item = QueuedAudioFrame(
            generation=self._frame_generation,
            payload=payload,
            start_sample_index=start_sample_index,
            input_gain_db=self._input_gain_db,
        )
        try:
            self._frame_queue.put_nowait(item)
        except asyncio.QueueFull:
            self._invalidate_frame_queue()
            await self._recover_audio_error(
                "queue_full",
                "ASR frame queue is full",
                already_invalidated=True,
            )

    async def _frame_worker(self) -> None:
        while True:
            item = await self._frame_queue.get()
            try:
                if (
                    item.generation != self._frame_generation
                    or self._closed
                    or self._transcribing
                ):
                    continue
                self._processing_generation = item.generation
                try:
                    async with self._audio_lock:
                        if item.generation != self._frame_generation:
                            continue
                        await self._process_binary(
                            item.payload,
                            item.start_sample_index,
                            item.input_gain_db,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await self._recover_audio_error(
                        "timeline_error",
                        "ASR audio timeline processing failed",
                    )
            finally:
                self._frame_queue.task_done()

    async def _process_binary(
        self, payload: bytes, start_sample_index: int, input_gain_db: float
    ) -> None:
        raw = np.frombuffer(payload, dtype="<f4").astype(np.float32, copy=False)

        gain = apply_gain(
            raw, input_gain_db, self._config.audio.clip_limit
        )
        frame_dbfs = rms_dbfs(gain.samples)
        reported_dbfs = frame_dbfs if np.isfinite(frame_dbfs) else -100.0

        resume_pending, vad_should_run = self._update_vad_compute_gating(
            gain.samples.size, frame_dbfs
        )
        try:
            if resume_pending:
                self._resume_suspended_vad()
            observations = (
                self._vad.accept(gain.samples) if vad_should_run else []
            )
        except Exception:
            self._invalidate_frame_queue()
            self._reset_audio_state()
            await self._send(
                {
                    "type": "utterance_error",
                    "code": "vad_failed",
                    "message": "VAD processing failed",
                }
            )
            return
        if observations:
            if self._vad_warmup_remaining_samples > 0:
                # 预热期内 Silero 概率未校准，仅用于充注模型状态，不参与判定。
                self._vad_warmup_remaining_samples = max(
                    0,
                    self._vad_warmup_remaining_samples - gain.samples.size,
                )
            else:
                self._latest_vad = observations[-1]
        elif self._vad_warmup_remaining_samples > 0:
            self._vad_warmup_remaining_samples = max(
                0,
                self._vad_warmup_remaining_samples - gain.samples.size,
            )
        frame = IndexedAudioFrame(
            gain.samples,
            start_sample_index,
            start_sample_index + gain.samples.size,
        )
        if self._config.observability.log_vad_probability:
            logger.info(
                "asr_vad %s",
                json.dumps(
                    {
                        "session_id_hash": self._session_id_hash(),
                        "start_sample_index": frame.start_sample_index,
                        "probability": self._latest_vad.probability,
                        "is_speech": self._latest_vad.is_speech,
                    },
                    sort_keys=True,
                ),
            )
        events = self._machine.accept(
            frame,
            frame_dbfs,
            self._latest_vad.probability,
            self._latest_vad.is_speech,
        )

        now = time.monotonic()
        if (
            (now - self._last_level_sent) * 1000
            >= self._config.transport.level_event_interval_ms
        ):
            self._last_level_sent = now
            await self._send(
                {
                    "type": "level",
                    "post_gain_dbfs": reported_dbfs,
                    "clipping_ratio": gain.clipping_ratio,
                }
            )
            if self._config.observability.log_audio_level:
                logger.info(
                    "asr_audio_level %s",
                    json.dumps(
                            {
                                "session_id_hash": self._session_id_hash(),
                                "start_sample_index": frame.start_sample_index,
                                "post_gain_dbfs": reported_dbfs,
                                "clipping_ratio": gain.clipping_ratio,
                            },
                        sort_keys=True,
                    ),
                )
            if self._processing_generation != self._frame_generation:
                return
        for event in events:
            if event.kind == "state_changed":
                if self._config.observability.log_state_transitions:
                    logger.info(
                        "asr_state_transition %s",
                        json.dumps(
                            {
                                "session_id_hash": self._session_id_hash(),
                                "previous_state": (
                                    event.previous_state.value
                                    if event.previous_state is not None
                                    else None
                                ),
                                "state": event.state.value,
                            },
                            sort_keys=True,
                        ),
                    )
                await self._send({"type": "state_changed", "state": event.state.value})
            elif event.kind == "rejected":
                await self._send(
                    {"type": "utterance_error", "code": event.reason, "message": event.reason}
                )
            elif event.kind == "completed" and event.utterance is not None:
                if self._processing_generation != self._frame_generation:
                    return
                await self._start_transcription(event.utterance)

    def _samples_for_ms(self, duration_ms: int) -> int:
        return self._config.audio.sample_rate_hz * duration_ms // 1000

    def _update_vad_compute_gating(
        self, sample_count: int, frame_dbfs: float
    ) -> tuple[bool, bool]:
        """Track SPL level for VAD compute gating during idle.

        Returns ``(resume_pending, vad_should_run)``. Suspension only applies
        while BUFFERING with the SPL trigger enabled; CANDIDATE/RECORDING
        always require live VAD output.
        """
        gating_eligible = (
            self._config.vad.compute_gating_enabled
            and self._config.audio_trigger.enabled
        )
        if not gating_eligible or self._machine.state is not RecordingState.BUFFERING:
            self._vad_quiet_samples = 0
            if self._vad_compute_suspended:
                # Defensive: non-idle states always require live VAD output.
                return True, True
            return False, True

        if frame_dbfs >= self._config.vad.compute_wake_threshold_dbfs:
            self._vad_quiet_samples = 0
            return self._vad_compute_suspended, True

        self._vad_quiet_samples += sample_count
        if (
            not self._vad_compute_suspended
            and self._vad_quiet_samples
            >= self._samples_for_ms(
                self._config.vad.compute_suspend_after_quiet_ms
            )
        ):
            self._suspend_vad_for_idle()
        # Quiet frames never trigger a resume; inference just stops while suspended.
        return False, not self._vad_compute_suspended

    def _suspend_vad_for_idle(self) -> None:
        # 长时间 idle 时暂停 Silero 推理以节省 CPU；唤醒线以上的声音会重新拉起。
        self._vad_compute_suspended = True
        self._vad_quiet_samples = 0
        self._vad_warmup_remaining_samples = 0
        self._latest_vad = VadObservation(0.0, False, False, False)
        self._reset_vad_safely()

    def _resume_suspended_vad(self) -> None:
        self._vad_compute_suspended = False
        self._vad_quiet_samples = 0
        self._reset_vad_safely()
        warmup_ms = self._config.vad.compute_warmup_context_ms
        self._vad_warmup_remaining_samples = self._samples_for_ms(warmup_ms)
        lead_in = self._machine.recent_audio_samples(warmup_ms)
        if lead_in.size:
            # 用 ring 内真实音频充注 LSTM 状态；其概率仅用于预热，外层已屏蔽。
            self._vad.accept(lead_in)

    async def _recover_audio_error(
        self, code: str, message: str, *, already_invalidated: bool = False
    ) -> None:
        if not already_invalidated:
            self._invalidate_frame_queue()
        async with self._audio_lock:
            self._reset_audio_state()
            await self._send(
                {"type": "utterance_error", "code": code, "message": message},
                ignore_errors=True,
            )

    def _invalidate_frame_queue(self) -> None:
        self._frame_generation += 1
        while True:
            try:
                self._frame_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._frame_queue.task_done()

    def _reset_audio_state(self) -> None:
        self._reset_vad_safely()
        self._latest_vad = VadObservation(0.0, False, False, False)
        self._machine = self._new_machine()

    async def _start_transcription(self, utterance) -> None:
        utterance_id = str(uuid.uuid4())
        self._active_utterance_id = utterance_id
        self._active_utterance = utterance
        self._active_utterance_started_at = time.monotonic()
        self._transcribing = True
        self._invalidate_frame_queue()
        await self._send({"type": "transcribing"})
        wav_bytes = encode_wav(utterance.samples, self._config.audio.sample_rate_hz)
        await self._save_debug_audio(utterance_id, utterance, wav_bytes)

        async def worker() -> None:
            waiting = True
            self._inference_waiter_started()
            try:
                async with self._semaphore:
                    self._inference_waiter_finished()
                    waiting = False
                    result = await self._client.transcribe(
                        wav_bytes,
                        self._abort_event,
                        self._send_delta if self._config.transcription.streaming else None,
                    )
                if self._abort_event.is_set():
                    self._log_active_utterance(
                        asr_text_length=0,
                        error_code="cancelled",
                    )
                    return
                if result.error:
                    self._log_active_utterance(
                        asr_latency_ms=result.inference_latency_ms,
                        asr_text_length=0,
                        error_code="asr_failed",
                    )
                    await self._send(
                        {"type": "utterance_error", "code": "asr_failed", "message": result.error}
                    )
                    return
                self._machine.mark_committed(utterance)
                self._last_committed_sample_index = utterance.end_sample_index
                self._log_active_utterance(
                    asr_latency_ms=result.inference_latency_ms,
                    asr_text_length=len(result.text),
                    error_code=None,
                )
                await self._send_transcript(
                    utterance_id,
                    wav_bytes,
                    text=result.text,
                    language=result.language,
                )
            except asyncio.CancelledError:
                self._log_active_utterance(
                    asr_text_length=0,
                    error_code="cancelled",
                )
                raise
            except Exception:
                if not self._abort_event.is_set():
                    self._log_active_utterance(
                        asr_text_length=0,
                        error_code="asr_failed",
                    )
                    await self._send(
                        {
                            "type": "utterance_error",
                            "code": "asr_failed",
                            "message": "ASR transcription failed",
                        },
                        ignore_errors=True,
                    )
            finally:
                current_task = asyncio.current_task()
                try:
                    try:
                        if waiting:
                            self._inference_waiter_finished()
                        if not self._abort_event.is_set():
                            await self._reset_after_utterance()
                    finally:
                        if self._inference_task is current_task:
                            self._retiring_inference_tasks.add(current_task)
                            self._inference_task = None
                    if not self._closed and not self._abort_event.is_set():
                        await self._send(
                            {"type": "state_changed", "state": "buffering"},
                            ignore_errors=True,
                        )
                finally:
                    self._retiring_inference_tasks.discard(current_task)

        self._inference_task = asyncio.create_task(worker())

    async def _save_debug_audio(self, utterance_id, utterance, wav_bytes: bytes) -> None:
        observability = self._config.observability
        if not observability.save_debug_audio or not utterance.metadata.silero_confirmed:
            return
        directory = Path(observability.debug_audio_directory).expanduser()
        path = directory / f"{utterance_id}.wav"

        def write() -> None:
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            directory.chmod(0o700)

            def private_opener(filename, flags):
                return os.open(filename, flags, 0o600)

            with open(path, "xb", opener=private_opener) as debug_file:
                debug_file.write(wav_bytes)

        task = asyncio.create_task(asyncio.to_thread(write))
        self._debug_write_task = task
        task.add_done_callback(self._debug_write_finished)
        try:
            await asyncio.shield(task)
        except OSError:
            pass

    def _debug_write_finished(self, task: asyncio.Task) -> None:
        if self._debug_write_task is task:
            self._debug_write_task = None
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if isinstance(error, OSError):
            logger.warning("asr_debug_audio_write_failed")

    def _session_id_hash(self) -> str:
        return hashlib.sha256(self.session_id.encode("utf-8")).hexdigest()

    def _log_utterance(
        self,
        utterance_id,
        utterance,
        *,
        asr_latency_ms: int,
        asr_text_length: int,
        error_code: str | None,
    ) -> None:
        metadata = utterance.metadata
        samples = utterance.samples
        clipping_ratio = (
            float(
                np.count_nonzero(
                    np.abs(samples) >= self._config.audio.clip_limit
                )
            )
            / samples.size
            if samples.size
            else 0.0
        )
        fields = {
            "utterance_id": utterance_id,
            "session_id_hash": self._session_id_hash(),
            "start_sample_index": utterance.start_sample_index,
            "end_sample_index": utterance.end_sample_index,
            "pretrigger_samples": metadata.pretrigger_samples,
            "recorded_samples": metadata.recorded_samples,
            "duration_ms": metadata.duration_ms,
            "trigger_dbfs": metadata.trigger_dbfs,
            "max_dbfs": metadata.max_dbfs,
            "clipping_ratio": clipping_ratio,
            "silero_start_probability": metadata.silero_start_probability,
            "silero_confirmed": metadata.silero_confirmed,
            "silero_end_reason": metadata.silero_end_reason,
            "asr_latency_ms": asr_latency_ms,
            "asr_text_length": asr_text_length,
            "max_duration_reached": metadata.max_duration_reached,
            "error_code": error_code,
        }
        logger.info(
            "asr_utterance %s",
            json.dumps(fields, sort_keys=True, separators=(",", ":")),
        )

    def _log_active_utterance(
        self,
        *,
        asr_text_length: int,
        error_code: str | None,
        asr_latency_ms: int | None = None,
    ) -> None:
        utterance_id = self._active_utterance_id
        utterance = self._active_utterance
        if utterance_id is None or utterance is None:
            return
        if asr_latency_ms is None:
            asr_latency_ms = int(
                (time.monotonic() - self._active_utterance_started_at) * 1000
            )
        self._active_utterance_id = None
        self._active_utterance = None
        self._log_utterance(
            utterance_id,
            utterance,
            asr_latency_ms=asr_latency_ms,
            asr_text_length=asr_text_length,
            error_code=error_code,
        )

    async def _send_delta(self, text: str) -> None:
        await self._send({"type": "transcript_delta", "text": text})

    async def _reset_after_utterance(self) -> None:
        self._transcribing = False
        reset_succeeded = self._reset_vad_safely()
        self._latest_vad = VadObservation(0.0, False, False, False)
        self._machine = self._new_machine()
        if not reset_succeeded and not self._closed:
            await self._send(
                {
                    "type": "utterance_error",
                    "code": "vad_reset_failed",
                    "message": "VAD state reset failed",
                },
                ignore_errors=True,
            )

    def _reset_vad_safely(self) -> bool:
        try:
            self._vad.reset()
            return True
        except Exception:
            try:
                self._vad = self._vad_factory()
            except Exception:
                pass
            return False

    async def abort(
        self,
        *,
        send_event: bool,
        close_code: int = 1000,
        deadline: float | None = None,
    ) -> None:
        self._abort_event.set()
        self._closed = True
        if deadline is None:
            deadline = (
                asyncio.get_running_loop().time()
                + self._config.runtime.shutdown_timeout_seconds
            )
        async with self._abort_lock:
            if self._abort_cleanup_complete:
                return
            frame_task = self._frame_worker_task
            self._frame_worker_task = None
            self._invalidate_frame_queue()
            if (
                frame_task
                and frame_task is not asyncio.current_task()
                and not frame_task.done()
            ):
                frame_task.cancel()
                await self._wait_until_deadline(frame_task, deadline)
            debug_write_task = self._debug_write_task
            if debug_write_task is not None:
                await self._wait_until_deadline(debug_write_task, deadline)
            task = self._inference_task
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
                await self._wait_until_deadline(task, deadline)
            if task is not None and task.done() and self._inference_task is task:
                self._inference_task = None
            for retiring_task in tuple(self._retiring_inference_tasks):
                if retiring_task is not asyncio.current_task() and not retiring_task.done():
                    retiring_task.cancel()
                    await self._wait_until_deadline(retiring_task, deadline)
            self._log_active_utterance(
                asr_text_length=0,
                error_code="cancelled",
            )
            self._reset_vad_safely()
            self._transcribing = False
            self._next_sample_index = 0
            self._last_committed_sample_index = 0
            self._latest_vad = VadObservation(0.0, False, False, False)
            self._machine = self._new_machine()
            if send_event and not self._aborted_event_sent:
                self._aborted_event_sent = True
                await self._send({"type": "aborted"}, ignore_errors=True)
            await self._close_websocket(close_code)
            self._abort_cleanup_complete = True

    async def _wait_until_deadline(
        self, task: asyncio.Task, deadline: float
    ) -> bool:
        if task.done():
            return True
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        if remaining == 0.0:
            return False
        done, _ = await asyncio.wait({task}, timeout=remaining)
        return bool(done)

    async def wait_finished(self) -> None:
        await self._run_finished.wait()

    async def _fatal(self, code: str, message: str) -> None:
        await self._send(
            {"type": "fatal_error", "code": code, "message": message},
            ignore_errors=True,
        )
        await self.abort(send_event=False, close_code=1002)

    async def _close_websocket(self, code: int) -> None:
        if self._websocket_closed:
            return
        self._websocket_closed = True
        try:
            await self._websocket.close(code=code)
        except (RuntimeError, WebSocketDisconnect):
            pass

    async def _send(self, payload: dict, *, ignore_errors: bool = False) -> None:
        try:
            async with self._send_lock:
                await self._websocket.send_json(payload)
        except (RuntimeError, WebSocketDisconnect):
            if not ignore_errors:
                raise

    async def _send_transcript(
        self,
        utterance_id: str,
        wav_bytes: bytes,
        *,
        text: str,
        language: str | None,
    ) -> None:
        payload = {"type": "transcript", "text": text, "language": language}
        try:
            async with self._send_lock:
                if self._config.debug.replay:
                    await self._websocket.send_json(
                        {
                            "type": "replay_audio",
                            "utterance_id": utterance_id,
                            "mime_type": "audio/wav",
                            "byte_length": len(wav_bytes),
                        }
                    )
                    await self._websocket.send_bytes(wav_bytes)
                    payload["replay_utterance_id"] = utterance_id
                await self._websocket.send_json(payload)
        except (RuntimeError, WebSocketDisconnect):
            raise
