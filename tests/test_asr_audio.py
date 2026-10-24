from __future__ import annotations

import io

import numpy as np
import pytest
import soundfile as sf


def test_gain_is_applied_once_and_reports_clipping():
    from backend.asr.audio import apply_gain

    result = apply_gain(np.array([0.1, 0.5], np.float32), 12.0, 0.999)

    np.testing.assert_allclose(result.samples[0], 0.398107, rtol=1e-5)
    assert result.samples[1] == pytest.approx(0.999)
    assert result.clipping_ratio == pytest.approx(0.5)
    assert result.samples.dtype == np.float32


def test_gain_rejects_non_finite_pcm():
    from backend.asr.audio import apply_gain

    with pytest.raises(ValueError, match="finite"):
        apply_gain(np.array([0.0, np.nan], np.float32), 0.0, 0.999)


def test_gain_counts_samples_at_clip_limit_as_clipping():
    from backend.asr.audio import apply_gain

    result = apply_gain(np.array([-0.999, 0.999], np.float32), 0.0, 0.999)

    assert result.clipping_ratio == pytest.approx(1.0)


def test_rms_dbfs_is_stable_for_silence_and_known_level():
    from backend.asr.audio import rms_dbfs

    assert rms_dbfs(np.zeros(320, np.float32)) == -np.inf
    assert rms_dbfs(np.full(320, 0.5, np.float32)) == pytest.approx(
        20.0 * np.log10(0.5)
    )


def test_wav_is_mono_16khz_pcm():
    from backend.asr.audio import encode_wav

    payload = encode_wav(np.zeros(320, np.float32), 16000)
    info = sf.info(io.BytesIO(payload))

    assert (info.samplerate, info.channels, info.frames) == (16000, 1, 320)
    assert info.subtype == "PCM_16"


def test_indexed_frame_requires_matching_half_open_indices():
    from backend.asr.models import IndexedAudioFrame

    with pytest.raises(ValueError, match="end_sample_index"):
        IndexedAudioFrame(
            samples=np.zeros(3, np.float32),
            start_sample_index=10,
            end_sample_index=12,
        )
