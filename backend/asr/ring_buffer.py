"""Fixed-capacity indexed pretrigger audio ring."""

from __future__ import annotations

import numpy as np

from backend.asr.models import IndexedAudioFrame


class PretriggerRingBuffer:
    def __init__(self, capacity_samples: int) -> None:
        if capacity_samples < 0:
            raise ValueError("capacity_samples must be non-negative")
        self._buffer = np.empty(capacity_samples, dtype=np.float32)
        self._write_pointer = 0
        self._valid_length = 0
        self._next_sample_index: int | None = None

    def append(self, frame: IndexedAudioFrame) -> None:
        if (
            self._next_sample_index is not None
            and frame.start_sample_index != self._next_sample_index
        ):
            raise ValueError(
                "discontinuous audio frame: expected start "
                f"{self._next_sample_index}, got {frame.start_sample_index}"
            )

        capacity = self._buffer.size
        sample_count = frame.samples.size
        if capacity and sample_count >= capacity:
            self._buffer[:] = frame.samples[-capacity:]
            self._write_pointer = 0
            self._valid_length = capacity
        elif capacity and sample_count:
            first_count = min(sample_count, capacity - self._write_pointer)
            self._buffer[
                self._write_pointer : self._write_pointer + first_count
            ] = frame.samples[:first_count]
            remaining = sample_count - first_count
            if remaining:
                self._buffer[:remaining] = frame.samples[first_count:]
            self._write_pointer = (self._write_pointer + sample_count) % capacity
            self._valid_length = min(capacity, self._valid_length + sample_count)

        self._next_sample_index = frame.end_sample_index

    def snapshot(self, min_start_index: int | None = None) -> IndexedAudioFrame:
        end = self._next_sample_index or 0
        start = end - self._valid_length
        requested_start = start if min_start_index is None else max(start, min_start_index)
        requested_start = min(requested_start, end)

        if self._valid_length == 0 or requested_start == end:
            samples = np.empty(0, dtype=np.float32)
        else:
            capacity = self._buffer.size
            oldest = self._write_pointer if self._valid_length == capacity else 0
            ordered = np.concatenate(
                (self._buffer[oldest:], self._buffer[:oldest])
            ) if oldest else self._buffer[: self._valid_length]
            samples = ordered[requested_start - start :].copy()

        return IndexedAudioFrame(samples, requested_start, end)
