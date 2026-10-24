from __future__ import annotations

from collections import OrderedDict
from threading import RLock
from typing import Any

from .contracts import CanonicalFrame
from .errors import ComputerUseError


def owned_rgb(source: Any, *, width: int, height: int) -> bytes:
    if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
        raise TypeError("width must be a positive integer")
    if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
        raise TypeError("height must be a positive integer")
    snapshot = bytes(source)
    if len(snapshot) != width * height * 3:
        raise ValueError("source length does not match packed RGB dimensions")
    return snapshot


class FrameStore:
    """Bounded immutable-frame storage with explicit request pins."""

    def __init__(self, max_frames: int = 4):
        if isinstance(max_frames, bool) or not isinstance(max_frames, int) or max_frames <= 0:
            raise TypeError("max_frames must be a positive integer")
        self._max_frames = max_frames
        self._frames: OrderedDict[str, CanonicalFrame] = OrderedDict()
        self._pins: dict[str, int] = {}
        self._invalid_bindings: set[str] = set()
        self._retired_ids: set[str] = set()
        self._lock = RLock()

    def put(self, frame: CanonicalFrame) -> None:
        if not isinstance(frame, CanonicalFrame):
            raise TypeError("frame must be CanonicalFrame")
        with self._lock:
            if frame.binding.binding_id in self._invalid_bindings:
                raise ComputerUseError("INVALID_CONTEXT_BINDING", "binding was invalidated")
            if frame.frame_id in self._frames or frame.frame_id in self._retired_ids:
                raise ComputerUseError("HELPER_PROTOCOL_ERROR", "frame_id must never be reused")
            self._frames[frame.frame_id] = frame
            self._evict_unpinned()

    def get(self, frame_id: str, binding_id: str, geometry_version: int) -> CanonicalFrame:
        with self._lock:
            if binding_id in self._invalid_bindings:
                raise ComputerUseError("INVALID_CONTEXT_BINDING", "binding was invalidated")
            frame = self._frames.get(frame_id)
            if frame is None:
                raise ComputerUseError("STALE_FRAME", "frame is unknown or expired")
            if frame.binding.binding_id != binding_id:
                raise ComputerUseError("INVALID_CONTEXT_BINDING", "frame belongs to another binding")
            if frame.binding.geometry_version != geometry_version:
                raise ComputerUseError("GEOMETRY_CHANGED", "frame geometry version is stale")
            return frame

    def pin(self, frame_id: str) -> CanonicalFrame:
        with self._lock:
            frame = self._frames.get(frame_id)
            if frame is None:
                raise ComputerUseError("STALE_FRAME", "cannot pin an expired frame")
            self._pins[frame_id] = self._pins.get(frame_id, 0) + 1
            return frame

    def release(self, frame_id: str) -> None:
        with self._lock:
            count = self._pins.get(frame_id, 0)
            if count <= 1:
                self._pins.pop(frame_id, None)
            else:
                self._pins[frame_id] = count - 1
            self._evict_unpinned()

    def invalidate(self, binding_id: str) -> None:
        with self._lock:
            self._invalid_bindings.add(binding_id)
            for frame_id, frame in tuple(self._frames.items()):
                if frame.binding.binding_id == binding_id:
                    self._frames.pop(frame_id)
                    self._pins.pop(frame_id, None)
                    self._retired_ids.add(frame_id)

    def _evict_unpinned(self) -> None:
        while len(self._frames) > self._max_frames:
            candidate = next((frame_id for frame_id in self._frames if not self._pins.get(frame_id)), None)
            if candidate is None:
                return
            self._frames.pop(candidate)
            self._retired_ids.add(candidate)
