"""ASR admission, health, and shutdown service."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import WebSocket

from backend.asr.config import AsrConfig
from backend.asr.session import AsrSession
from backend.asr.sglang_client import SGLangAsrClient
from backend.asr.vad import SileroVadAdapter


class AsrService:
    def __init__(
        self,
        config: AsrConfig,
        *,
        sglang_client=None,
        vad_factory: Callable[[], object] | None = None,
    ) -> None:
        self.config = config
        self._client = sglang_client or SGLangAsrClient(config)
        self._vad_factory = vad_factory or (
            lambda: SileroVadAdapter(config.vad, sample_rate_hz=config.audio.sample_rate_hz)
        )
        self._semaphore = asyncio.Semaphore(config.runtime.asr_worker_concurrency)
        self._sessions: dict[str, AsrSession] = {}
        self._session_tasks: dict[str, asyncio.Task] = {}
        self._queued_utterances = 0
        self._lock = asyncio.Lock()
        self._closing = False
        self._shutdown_complete = asyncio.Event()

    def validate_vad(self) -> None:
        """Load and reset one adapter so enabled startup fails on bad VAD setup."""
        adapter = self._vad_factory()
        adapter.reset()

    async def run_websocket(self, websocket: WebSocket) -> None:
        async with self._lock:
            if self._closing:
                await websocket.accept()
                await websocket.send_json(
                    {
                        "type": "fatal_error",
                        "code": "shutting_down",
                        "message": "ASR service is shutting down",
                    }
                )
                await websocket.close(code=1013)
                return
            if len(self._sessions) >= self.config.runtime.max_active_sessions:
                await websocket.accept()
                await websocket.send_json(
                    {"type": "fatal_error", "code": "busy", "message": "ASR session limit reached"}
                )
                await websocket.close(code=1013)
                return
            session = AsrSession(
                websocket,
                self.config,
                sglang_client=self._client,
                vad_factory=self._vad_factory,
                inference_semaphore=self._semaphore,
                inference_waiter_started=self._inference_waiter_started,
                inference_waiter_finished=self._inference_waiter_finished,
            )
            self._sessions[session.session_id] = session
            current_task = asyncio.current_task()
            if current_task is not None:
                self._session_tasks[session.session_id] = current_task
        try:
            await session.run()
        finally:
            async with self._lock:
                self._sessions.pop(session.session_id, None)
                self._session_tasks.pop(session.session_id, None)

    def _inference_waiter_started(self) -> None:
        self._queued_utterances += 1

    def _inference_waiter_finished(self) -> None:
        self._queued_utterances = max(0, self._queued_utterances - 1)

    async def health(self) -> dict:
        status = await self._client.probe()
        return {
            "status": "healthy" if status.available else "degraded",
            "vad_ready": True,
            "sglang_ready": status.available,
            "model_name": self.config.sglang.served_model_name,
            "active_sessions": len(self._sessions),
            "queued_utterances": self._queued_utterances,
        }

    async def shutdown(self) -> None:
        deadline = (
            asyncio.get_running_loop().time()
            + self.config.runtime.shutdown_timeout_seconds
        )
        async with self._lock:
            if self._closing:
                owns_shutdown = False
                sessions = []
                tasks = []
            else:
                self._closing = True
                owns_shutdown = True
                sessions = list(self._sessions.values())
                tasks = list(self._session_tasks.values())
        if not owns_shutdown:
            await self._shutdown_complete.wait()
            return

        try:
            await asyncio.gather(
                *(
                    session.abort(send_event=False, deadline=deadline)
                    for session in sessions
                ),
                return_exceptions=True,
            )
            if tasks:
                remaining = max(
                    0.0, deadline - asyncio.get_running_loop().time()
                )
                _, pending = await asyncio.wait(
                    tasks, timeout=remaining
                )
                for task in pending:
                    task.cancel()
                if pending:
                    remaining = max(
                        0.0, deadline - asyncio.get_running_loop().time()
                    )
                    if remaining:
                        await asyncio.wait(pending, timeout=remaining)
                    else:
                        await asyncio.sleep(0)
            async with self._lock:
                self._sessions.clear()
                self._session_tasks.clear()
            await self._client.close()
        finally:
            self._shutdown_complete.set()
