"""Vectorized gain, level, and in-memory WAV primitives."""

from __future__ import annotations

import io
import math

import numpy as np
import soundfile as sf
from numpy.typing import NDArray

from backend.asr.models import GainResult


def _validated_samples(samples: NDArray[np.float32]) -> NDArray[np.float32]:
    pcm = np.asarray(samples)
    if pcm.ndim != 1:
        raise ValueError("samples must be one-dimensional mono PCM")
    if pcm.dtype != np.float32:
        raise ValueError("samples must use float32")
    if not np.isfinite(pcm).all():
        raise ValueError("samples must contain only finite values")
    return pcm


def apply_gain(
    samples: NDArray[np.float32], gain_db: float, clip_limit: float
) -> GainResult:
    """Apply a dB gain exactly once and report the fraction that was clipped."""
    pcm = _validated_samples(samples)
    if not math.isfinite(gain_db):
        raise ValueError("gain_db must be finite")
    if not math.isfinite(clip_limit) or not 0.0 < clip_limit <= 1.0:
        raise ValueError("clip_limit must be finite and in (0, 1]")

    amplified = pcm * np.float32(10.0 ** (gain_db / 20.0))
    clipping_ratio = (
        float(np.count_nonzero(np.abs(amplified) >= clip_limit)) / amplified.size
        if amplified.size
        else 0.0
    )
    clipped = np.clip(amplified, -clip_limit, clip_limit).astype(np.float32, copy=False)
    return GainResult(samples=clipped, clipping_ratio=clipping_ratio)


def rms_dbfs(samples: NDArray[np.float32]) -> float:
    """Return RMS dBFS, using negative infinity for empty or silent PCM."""
    pcm = _validated_samples(samples)
    if pcm.size == 0:
        return -math.inf
    rms = math.sqrt(float(np.mean(np.square(pcm, dtype=np.float64))))
    return 20.0 * math.log10(rms) if rms > 0.0 else -math.inf


def encode_wav(samples: NDArray[np.float32], sample_rate_hz: int) -> bytes:
    """Encode mono Float32 input as an in-memory PCM-16 WAV."""
    pcm = _validated_samples(samples)
    if sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be positive")
    payload = io.BytesIO()
    sf.write(payload, pcm, sample_rate_hz, format="WAV", subtype="PCM_16")
    return payload.getvalue()
