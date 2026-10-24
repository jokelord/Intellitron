"""Exact ASGI body limits for inbound Vision-capable JSON routes."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

MAX_VISION_REQUEST_BODY_BYTES = 142_606_336
_PROTECTED_PATHS = {
    "/v1/chat/completions",
    "/v1/agent/chat",
    "/v1/responses",
}


class _RequestBodyTooLarge(Exception):
    pass


class VisionRequestBodyLimitMiddleware:
    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        max_body_bytes: int = MAX_VISION_REQUEST_BODY_BYTES,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: dict[str, Any], receive, send) -> None:
        if not self._is_protected(scope):
            await self.app(scope, receive, send)
            return

        content_length = self._content_length(scope)
        if content_length is not None and content_length > self.max_body_bytes:
            await self._send_too_large(send)
            return

        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if not response_started:
                await self._send_too_large(send)

    @staticmethod
    def _is_protected(scope: dict[str, Any]) -> bool:
        return (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") in _PROTECTED_PATHS
        )

    @staticmethod
    def _content_length(scope: dict[str, Any]) -> int | None:
        for name, value in scope.get("headers", ()):
            if name.lower() != b"content-length":
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed >= 0 else None
        return None

    @staticmethod
    async def _send_too_large(send) -> None:
        body = json.dumps({"detail": "request body too large"}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
