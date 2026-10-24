from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable, Iterator

import psutil

from backend.config import ReadFileConfig

from .errors import ReadFileError, ReadFileErrorCode


class ResourceGuard:
    def __init__(self, config: ReadFileConfig):
        self.config = config
        self._condition = threading.Condition()
        self._active = 0
        self._queued = 0

    @property
    def active_count(self) -> int:
        with self._condition:
            return self._active

    @property
    def queue_depth(self) -> int:
        with self._condition:
            return self._queued

    def check_memory(self) -> None:
        reserve = self.config.main_llm_memory_reserve_gb * 1024**3
        if psutil.virtual_memory().available < reserve:
            raise ReadFileError(
                ReadFileErrorCode.MEMORY_RESERVE,
                "available UMA is below the configured document-processing floor",
            )

    @contextmanager
    def acquire(
        self, check_abort: Callable[[], bool] | None
    ) -> Iterator[None]:
        admitted = False
        queued = False
        with self._condition:
            if self._active >= self.config.ocr_concurrency:
                if self._queued >= self.config.ocr_queue_limit:
                    raise ReadFileError(
                        ReadFileErrorCode.RESOURCE_BUSY,
                        "OCR queue is full",
                    )
                self._queued += 1
                queued = True
            try:
                while self._active >= self.config.ocr_concurrency:
                    if check_abort and check_abort():
                        raise ReadFileError(
                            ReadFileErrorCode.ABORTED, "request was cancelled while queued"
                        )
                    self._condition.wait(timeout=0.05)
                if check_abort and check_abort():
                    raise ReadFileError(ReadFileErrorCode.ABORTED, "request was cancelled")
                self.check_memory()
                self._active += 1
                admitted = True
            finally:
                if queued:
                    self._queued -= 1
        try:
            yield
        finally:
            if admitted:
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()


_default_guard: ResourceGuard | None = None
_default_guard_lock = threading.Lock()


def get_default_resource_guard(config: ReadFileConfig) -> ResourceGuard:
    global _default_guard
    with _default_guard_lock:
        if _default_guard is None:
            _default_guard = ResourceGuard(config)
        return _default_guard
