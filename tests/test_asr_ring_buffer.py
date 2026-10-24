from __future__ import annotations

import numpy as np
import pytest


def frame(values: list[float], *, start: int):
    from backend.asr.models import IndexedAudioFrame

    samples = np.asarray(values, dtype=np.float32)
    return IndexedAudioFrame(samples, start, start + samples.size)


def test_ring_wraps_and_snapshot_is_ordered():
    from backend.asr.ring_buffer import PretriggerRingBuffer

    ring = PretriggerRingBuffer(capacity_samples=4)
    ring.append(frame([0, 1, 2], start=0))
    ring.append(frame([3, 4, 5], start=3))

    snap = ring.snapshot()

    assert snap.start_sample_index == 2
    assert snap.end_sample_index == 6
    assert snap.samples.tolist() == [2, 3, 4, 5]


def test_snapshot_crops_by_global_minimum_index():
    from backend.asr.ring_buffer import PretriggerRingBuffer

    ring = PretriggerRingBuffer(capacity_samples=8)
    ring.append(frame([10, 11, 12, 13], start=100))

    snap = ring.snapshot(min_start_index=102)

    assert snap.start_sample_index == 102
    assert snap.end_sample_index == 104
    assert snap.samples.tolist() == [12, 13]


def test_ring_rejects_discontinuity_without_mutating_existing_audio():
    from backend.asr.ring_buffer import PretriggerRingBuffer

    ring = PretriggerRingBuffer(capacity_samples=4)
    ring.append(frame([1, 2], start=10))

    with pytest.raises(ValueError, match="discontinuous"):
        ring.append(frame([9], start=13))

    snap = ring.snapshot()
    assert (snap.start_sample_index, snap.end_sample_index) == (10, 12)
    assert snap.samples.tolist() == [1, 2]


def test_oversized_append_keeps_only_tail_in_fixed_allocation():
    from backend.asr.ring_buffer import PretriggerRingBuffer

    ring = PretriggerRingBuffer(capacity_samples=3)
    allocation = ring._buffer
    ring.append(frame([0, 1, 2, 3, 4], start=20))

    snap = ring.snapshot()
    assert ring._buffer is allocation
    assert ring._buffer.size == 3
    assert snap.start_sample_index == 22
    assert snap.samples.tolist() == [2, 3, 4]


def test_zero_capacity_ring_tracks_timeline_without_retaining_audio():
    from backend.asr.ring_buffer import PretriggerRingBuffer

    ring = PretriggerRingBuffer(capacity_samples=0)
    ring.append(frame([1, 2], start=7))

    snap = ring.snapshot()
    assert (snap.start_sample_index, snap.end_sample_index) == (9, 9)
    assert snap.samples.size == 0
