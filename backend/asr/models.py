"""Value types shared by the ASR audio pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

import numpy as np
from numpy.typing import NDArray


Float32Array = NDArray[np.float32]


@dataclass(frozen=True, slots=True)
class GainResult:
    samples: Float32Array
    clipping_ratio: float


@dataclass(frozen=True, slots=True)
class IndexedAudioFrame:
    """A mono PCM span identified by global half-open sample indices."""

    samples: Float32Array
    start_sample_index: int
    end_sample_index: int

    def __post_init__(self) -> None:
        samples = np.asarray(self.samples)
        if samples.ndim != 1:
            raise ValueError("samples must be one-dimensional mono PCM")
        if samples.dtype != np.float32:
            raise ValueError("samples must use float32")
        if self.start_sample_index < 0:
            raise ValueError("start_sample_index must be non-negative")
        expected_end = self.start_sample_index + samples.size
        if self.end_sample_index != expected_end:
            raise ValueError(
                "end_sample_index must equal start_sample_index + sample count"
            )
        object.__setattr__(self, "samples", np.ascontiguousarray(samples))


class RecordingState(str, Enum):
    BUFFERING = "buffering"
    CANDIDATE = "candidate"
    RECORDING = "recording"
    FINALIZING = "finalizing"


@dataclass(frozen=True, slots=True)
class VadObservation:
    probability: float
    speech_started: bool
    speech_ended: bool
    is_speech: bool


@dataclass(frozen=True, slots=True)
class UtteranceMetadata:
    pretrigger_samples: int
    recorded_samples: int
    duration_ms: float
    trigger_dbfs: float
    max_dbfs: float
    silero_start_probability: float
    silero_confirmed: bool
    silero_end_reason: str
    max_duration_reached: bool


@dataclass(frozen=True, slots=True)
class CompletedUtterance:
    samples: Float32Array
    start_sample_index: int
    end_sample_index: int
    metadata: UtteranceMetadata

    @property
    def pretrigger_samples(self) -> int:
        return self.metadata.pretrigger_samples


@dataclass(frozen=True, slots=True)
class RecordingEvent:
    kind: Literal["state_changed", "completed", "rejected"]
    state: RecordingState
    previous_state: RecordingState | None = None
    utterance: CompletedUtterance | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AsrResult:
    text: str
    language: str | None
    duration_ms: int
    inference_latency_ms: int
    error: str | None
