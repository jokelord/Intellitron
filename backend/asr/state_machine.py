"""Four-state indexed utterance recorder."""

from __future__ import annotations

import math

import numpy as np

from backend.asr.config import (
    AudioConfig,
    AudioTriggerConfig,
    RecordingConfig,
    VadConfig,
)
from backend.asr.models import (
    CompletedUtterance,
    IndexedAudioFrame,
    RecordingEvent,
    RecordingState,
    UtteranceMetadata,
)
from backend.asr.ring_buffer import PretriggerRingBuffer


class RecordingStateMachine:
    def __init__(
        self,
        *,
        audio_config: AudioConfig,
        recording_config: RecordingConfig,
        trigger_config: AudioTriggerConfig,
        vad_config: VadConfig,
        last_committed_sample_index: int = 0,
    ) -> None:
        if last_committed_sample_index < 0:
            raise ValueError("last_committed_sample_index must be non-negative")
        self._audio = audio_config
        self._recording = recording_config
        self._trigger = trigger_config
        self._vad = vad_config
        self.state = RecordingState.BUFFERING
        self.last_committed_sample_index = last_committed_sample_index
        self.end_no_voice_samples = 0

        self._ring = PretriggerRingBuffer(
            self._samples_for_ms(recording_config.pretrigger_buffer_ms)
        )
        max_storage = self._samples_for_ms(
            recording_config.pretrigger_buffer_ms
            + recording_config.max_utterance_ms
            + recording_config.posttrigger_tail_ms
        )
        self._utterance_buffer = np.empty(max_storage, dtype=np.float32)
        self._utterance_length = 0
        self._utterance_start = 0
        self._next_sample_index: int | None = None
        self._trigger_samples = 0
        self._candidate_samples = 0
        self._finalizing_samples = 0
        self._pretrigger_samples = 0
        self._trigger_dbfs = -math.inf
        self._max_dbfs = -math.inf
        self._silero_start_probability = 0.0
        self._silero_confirmed = False
        self._max_duration_reached = False
        self._terminal = False
        self._trim_keep_length: int | None = None
        self._trim_discard_samples = 0

    def accept(
        self,
        frame: IndexedAudioFrame,
        dbfs: float,
        vad_probability: float,
        vad_is_speech: bool,
    ) -> list[RecordingEvent]:
        if self._terminal:
            return []
        if frame.samples.size != self._audio.frame_samples:
            raise ValueError(
                f"frame must contain exactly {self._audio.frame_samples} samples"
            )
        if math.isnan(dbfs):
            raise ValueError("dbfs must not be NaN")
        if not math.isfinite(vad_probability) or not 0.0 <= vad_probability <= 1.0:
            raise ValueError("vad_probability must be finite and in [0, 1]")
        if (
            self._next_sample_index is not None
            and frame.start_sample_index != self._next_sample_index
        ):
            raise ValueError(
                "discontinuous audio frame: expected start "
                f"{self._next_sample_index}, got {frame.start_sample_index}"
            )

        events: list[RecordingEvent] = []
        if self.state is RecordingState.BUFFERING:
            vad_trigger_active = (
                vad_is_speech or vad_probability >= self._vad.threshold
            )
            trigger_active = (
                dbfs >= self._trigger.start_threshold_dbfs
                if self._trigger.enabled
                else vad_trigger_active
            )
            if trigger_active:
                self._trigger_samples += frame.samples.size
            else:
                self._trigger_samples = 0
            if self._trigger_samples >= self._samples_for_ms(
                self._trigger.min_trigger_duration_ms
            ):
                snapshot = self._ring.snapshot(
                    min_start_index=self.last_committed_sample_index
                )
                if snapshot.samples.size == 0:
                    snapshot = IndexedAudioFrame(
                        np.empty(0, dtype=np.float32),
                        frame.start_sample_index,
                        frame.start_sample_index,
                    )
                self._begin_candidate(snapshot, dbfs, vad_probability)
                events.append(self._transition(RecordingState.CANDIDATE))
                max_samples = self._samples_for_ms(
                    self._recording.max_utterance_ms
                )
                self._append_utterance(frame, max_new_samples=max_samples)
                body_samples = self._utterance_length - self._pretrigger_samples
                if body_samples >= max_samples:
                    self._append_tail_from_frame(frame)
                vad_confirmed = vad_is_speech or (
                    not self._trigger.enabled and vad_trigger_active
                )
                if not self._trigger.require_vad_confirmation or vad_confirmed:
                    self._silero_confirmed = vad_confirmed
                    events.append(self._transition(RecordingState.RECORDING))
                    if body_samples >= max_samples:
                        events.extend(
                            self._begin_max_duration_finalizing(frame)
                        )

        elif self.state is RecordingState.CANDIDATE:
            self._max_dbfs = max(self._max_dbfs, dbfs)
            max_samples = self._samples_for_ms(self._recording.max_utterance_ms)
            body_samples = (
                self._utterance_length
                - self._pretrigger_samples
                - self._finalizing_samples
            )
            remaining_body = max(0, max_samples - body_samples)
            if vad_is_speech:
                if remaining_body:
                    self._append_utterance(
                        frame, max_new_samples=remaining_body
                    )
                self._silero_confirmed = True
                self._silero_start_probability = vad_probability
                events.append(self._transition(RecordingState.RECORDING))
                body_samples = (
                    self._utterance_length
                    - self._pretrigger_samples
                    - self._finalizing_samples
                )
                if body_samples >= max_samples:
                    events.extend(self._begin_max_duration_finalizing(frame))
            else:
                timeout_samples = self._samples_for_ms(
                    self._recording.candidate_confirmation_timeout_ms
                )
                remaining_candidate = max(
                    0, timeout_samples - self._candidate_samples
                )
                append_limit = min(remaining_body, remaining_candidate)
                if append_limit:
                    self._append_utterance(
                        frame,
                        max_new_samples=append_limit,
                    )
                self._candidate_samples += min(
                    frame.samples.size, remaining_candidate
                )
                if self._candidate_samples >= timeout_samples:
                    events.append(self._reject("candidate_timeout"))

        elif self.state is RecordingState.RECORDING:
            max_samples = self._samples_for_ms(self._recording.max_utterance_ms)
            body_samples = self._utterance_length - self._pretrigger_samples
            self._append_utterance(
                frame, max_new_samples=max(0, max_samples - body_samples)
            )
            self._max_dbfs = max(self._max_dbfs, dbfs)
            body_samples = self._utterance_length - self._pretrigger_samples
            if body_samples >= max_samples:
                events.extend(self._begin_max_duration_finalizing(frame))
            else:
                # 结束判定只依赖 Silero VAD 人声概率，不使用绝对静音(SPL)判断：
                # 嘈杂环境下 RMS 长期高于阈值也能正常收尾，避免无限录音。
                no_voice = vad_probability < self._vad.threshold
                if no_voice:
                    self.end_no_voice_samples += frame.samples.size
                else:
                    self.end_no_voice_samples = 0
                if self.end_no_voice_samples >= self._samples_for_ms(
                    self._recording.end_no_voice_duration_ms
                ):
                    self._arm_no_voice_trim()
                    events.append(self._transition(RecordingState.FINALIZING))
                    if self._recording.posttrigger_tail_ms == 0:
                        events.extend(self._finish(max_duration_reached=False))

        elif self.state is RecordingState.FINALIZING:
            tail_samples = self._samples_for_ms(
                self._recording.posttrigger_tail_ms
            )
            appended = self._append_utterance(
                frame,
                max_new_samples=max(0, tail_samples - self._finalizing_samples),
            )
            self._max_dbfs = max(self._max_dbfs, dbfs)
            self._finalizing_samples += appended
            if self._finalizing_samples >= tail_samples:
                events.extend(
                    self._finish(
                        max_duration_reached=self._max_duration_reached
                    )
                )

        self._ring.append(frame)
        self._next_sample_index = frame.end_sample_index
        return events

    def mark_committed(self, utterance: CompletedUtterance) -> None:
        if utterance.end_sample_index < self.last_committed_sample_index:
            raise ValueError("committed sample boundary cannot move backward")
        self.last_committed_sample_index = utterance.end_sample_index

    def recent_audio_samples(self, duration_ms: int) -> np.ndarray:
        """Return up to duration_ms of the most recently appended PCM samples."""
        if duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")
        snapshot = self._ring.snapshot()
        take = min(snapshot.samples.size, self._samples_for_ms(duration_ms))
        return snapshot.samples[snapshot.samples.size - take :]

    def _samples_for_ms(self, duration_ms: int) -> int:
        return self._audio.sample_rate_hz * duration_ms // 1000

    def _begin_candidate(
        self,
        snapshot: IndexedAudioFrame,
        dbfs: float,
        vad_probability: float,
    ) -> None:
        self._utterance_length = 0
        self._utterance_start = snapshot.start_sample_index
        self._append_utterance(snapshot)
        self._pretrigger_samples = snapshot.samples.size
        self._trigger_dbfs = dbfs
        self._max_dbfs = dbfs
        self._silero_start_probability = vad_probability
        self._silero_confirmed = False
        self._max_duration_reached = False
        self._candidate_samples = 0
        self._finalizing_samples = 0
        self._trim_keep_length = None
        self._trim_discard_samples = 0
        self.end_no_voice_samples = 0

    def _append_utterance(
        self, frame: IndexedAudioFrame, *, max_new_samples: int | None = None
    ) -> int:
        expected_index = self._utterance_start + self._utterance_length
        if frame.end_sample_index <= expected_index:
            return 0
        if frame.start_sample_index > expected_index:
            raise ValueError("utterance audio contains a sample-index gap")
        crop = max(0, expected_index - frame.start_sample_index)
        samples = frame.samples[crop:]
        if max_new_samples is not None:
            samples = samples[:max_new_samples]
        new_length = self._utterance_length + samples.size
        if new_length > self._utterance_buffer.size:
            raise ValueError("utterance exceeded fixed recording capacity")
        self._utterance_buffer[self._utterance_length : new_length] = samples
        self._utterance_length = new_length
        return samples.size

    def _transition(self, state: RecordingState) -> RecordingEvent:
        previous = self.state
        self.state = state
        return RecordingEvent(
            kind="state_changed", state=state, previous_state=previous
        )

    def _begin_max_duration_finalizing(
        self, frame: IndexedAudioFrame
    ) -> list[RecordingEvent]:
        self._max_duration_reached = True
        # max_duration 结束不经过无声窗验证，不做尾部裁剪。
        self._trim_keep_length = None
        self._trim_discard_samples = 0
        events = [self._transition(RecordingState.FINALIZING)]
        self._append_tail_from_frame(frame)
        tail_samples = self._samples_for_ms(self._recording.posttrigger_tail_ms)
        if self._finalizing_samples >= tail_samples:
            events.extend(self._finish(max_duration_reached=True))
        return events

    def _append_tail_from_frame(self, frame: IndexedAudioFrame) -> None:
        tail_samples = self._samples_for_ms(self._recording.posttrigger_tail_ms)
        remaining_tail = max(0, tail_samples - self._finalizing_samples)
        if remaining_tail:
            self._finalizing_samples += self._append_utterance(
                frame, max_new_samples=remaining_tail
            )

    def _reject(self, reason: str) -> RecordingEvent:
        previous = self.state
        self.state = RecordingState.BUFFERING
        self._utterance_length = 0
        self._trigger_samples = 0
        self._candidate_samples = 0
        self._max_duration_reached = False
        self._trim_keep_length = None
        self._trim_discard_samples = 0
        self.end_no_voice_samples = 0
        return RecordingEvent(
            kind="rejected",
            state=RecordingState.BUFFERING,
            previous_state=previous,
            reason=reason,
        )

    def _arm_no_voice_trim(self) -> None:
        """Capture the verified-silent tail span to drop before ASR submission."""
        window_samples = min(
            self.end_no_voice_samples, self._utterance_length
        )
        pad_samples = min(
            self._samples_for_ms(self._recording.end_no_voice_trim_pad_ms),
            window_samples,
        )
        self._trim_discard_samples = window_samples - pad_samples
        self._trim_keep_length = (
            self._utterance_length - self._trim_discard_samples
            if self._trim_discard_samples > 0
            else None
        )

    def _finish(self, *, max_duration_reached: bool) -> list[RecordingEvent]:
        # 最短时长按未裁剪的完整录音判定，保持既有接受行为不变。
        minimum_samples = self._samples_for_ms(self._recording.min_utterance_ms)
        if self._utterance_length < minimum_samples:
            return [self._reject("too_short")]

        if (
            not max_duration_reached
            and self._trim_discard_samples > 0
            and self._trim_keep_length is not None
        ):
            keep = self._trim_keep_length
            drop_end = keep + self._trim_discard_samples
            samples = np.concatenate(
                (
                    self._utterance_buffer[:keep],
                    self._utterance_buffer[drop_end : self._utterance_length],
                )
            )
        else:
            samples = self._utterance_buffer[: self._utterance_length].copy()

        end_sample_index = self._utterance_start + samples.size
        metadata = UtteranceMetadata(
            pretrigger_samples=self._pretrigger_samples,
            recorded_samples=samples.size - self._pretrigger_samples,
            duration_ms=samples.size * 1000.0 / self._audio.sample_rate_hz,
            trigger_dbfs=self._trigger_dbfs,
            max_dbfs=self._max_dbfs,
            silero_start_probability=self._silero_start_probability,
            silero_confirmed=self._silero_confirmed,
            silero_end_reason=(
                "max_duration" if max_duration_reached else "no_voice_timeout"
            ),
            max_duration_reached=max_duration_reached,
        )
        utterance = CompletedUtterance(
            samples=samples,
            start_sample_index=self._utterance_start,
            end_sample_index=end_sample_index,
            metadata=metadata,
        )
        self._terminal = True
        return [
            RecordingEvent(
                kind="completed",
                state=RecordingState.FINALIZING,
                utterance=utterance,
            )
        ]
