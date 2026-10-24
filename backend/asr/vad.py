"""Session-local Silero VAD chunking adapter with lazy dependency loading."""

from __future__ import annotations

import importlib.resources
import math
from typing import Any

import numpy as np
from numpy.typing import NDArray

from backend.asr.config import VadConfig
from backend.asr.models import VadObservation


class SileroVadAdapter:
    def __init__(
        self,
        config: VadConfig,
        *,
        sample_rate_hz: int = 16000,
        model: Any | None = None,
    ) -> None:
        self._config = config
        self._sample_rate_hz = sample_rate_hz
        self._injected_model = model is not None
        self._model = model if model is not None else _load_silero_model(config.model_path)
        self._pending = np.empty(0, dtype=np.float32)
        self._is_speech = False
        self._silence_samples = 0

    def accept(self, samples: NDArray[np.float32]) -> list[VadObservation]:
        pcm = np.asarray(samples)
        if pcm.ndim != 1 or pcm.dtype != np.float32:
            raise ValueError("Silero input must be one-dimensional float32 PCM")
        if not np.isfinite(pcm).all():
            raise ValueError("Silero input must contain only finite values")
        if pcm.size:
            self._pending = np.concatenate((self._pending, pcm))

        observations: list[VadObservation] = []
        chunk_size = self._config.frame_samples
        while self._pending.size >= chunk_size:
            chunk = self._pending[:chunk_size]
            self._pending = self._pending[chunk_size:]
            probability = self._predict(chunk)
            speech_started = False
            speech_ended = False

            if not self._is_speech and probability >= self._config.threshold:
                self._is_speech = True
                self._silence_samples = 0
                speech_started = True
            elif self._is_speech:
                if probability < self._config.threshold:
                    self._silence_samples += chunk_size
                    silence_required = (
                        self._sample_rate_hz
                        * (
                            self._config.min_silence_duration_ms
                            + self._config.speech_pad_ms
                        )
                        // 1000
                    )
                    if self._silence_samples >= silence_required:
                        self._is_speech = False
                        self._silence_samples = 0
                        speech_ended = True
                else:
                    self._silence_samples = 0

            observations.append(
                VadObservation(
                    probability=probability,
                    speech_started=speech_started,
                    speech_ended=speech_ended,
                    is_speech=self._is_speech,
                )
            )
        return observations

    def reset(self) -> None:
        self._pending = np.empty(0, dtype=np.float32)
        self._is_speech = False
        self._silence_samples = 0
        reset_states = getattr(self._model, "reset_states", None)
        if reset_states is not None:
            reset_states()

    def _predict(self, chunk: NDArray[np.float32]) -> float:
        if self._injected_model:
            prediction = self._model(chunk, self._sample_rate_hz)
        else:
            import torch

            prediction = self._model(torch.from_numpy(chunk), self._sample_rate_hz)
        probability = float(prediction.item())
        if not math.isfinite(probability):
            raise ValueError("Silero returned a non-finite probability")
        return probability


def _load_silero_model(model_path: str) -> Any:
    if not model_path.startswith("package://"):
        raise ValueError("Silero model_path must use package://")
    package_path = model_path.removeprefix("package://")
    package, separator, resource_path = package_path.partition("/")
    if not separator or not package or not resource_path:
        raise ValueError("invalid package:// Silero model path")

    try:
        resource = importlib.resources.files(package).joinpath(resource_path)
        if not resource.is_file():
            raise RuntimeError(f"Silero model resource does not exist: {model_path}")
        from silero_vad import load_silero_vad
    except (ImportError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            "silero-vad is required when ASR VAD is initialized"
        ) from exc

    return load_silero_vad(onnx=True)
