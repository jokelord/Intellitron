from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class OneTimeObjectURL:
    url: str
    token: str
    expires_at: float


class TemporaryObjectServer:
    def __init__(self, port: int = 0):
        self._entries: dict[str, tuple[Path, float]] = {}
        self._lock = threading.Lock()
        registry = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                parts = parsed.path.split("/")
                if len(parts) != 3 or parts[1] != "objects" or not parts[2]:
                    self.send_error(404)
                    return
                token = parts[2]
                with registry._lock:
                    registry._cleanup_locked()
                    entry = registry._entries.get(token)
                    if entry is None:
                        self.send_error(404)
                        return
                    path, _ = entry
                    try:
                        stream = path.open("rb")
                    except OSError:
                        self.send_error(404)
                        return
                    registry._entries.pop(token, None)
                with stream:
                    size = path.stat().st_size
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(size))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    while chunk := stream.read(1024 * 1024):
                        self.wfile.write(chunk)

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _cleanup_locked(self) -> None:
        now = time.monotonic()
        for token, (_, expires_at) in list(self._entries.items()):
            if expires_at <= now:
                self._entries.pop(token, None)

    def register(self, path: str | Path, ttl_seconds: int) -> OneTimeObjectURL:
        resolved = Path(path).resolve(strict=True)
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        token = secrets.token_urlsafe(32)
        expires_at = time.monotonic() + min(max(1, ttl_seconds), 600)
        with self._lock:
            self._cleanup_locked()
            self._entries[token] = (resolved, expires_at)
        return OneTimeObjectURL(
            f"http://127.0.0.1:{self._server.server_port}/objects/{token}",
            token,
            expires_at,
        )

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        with self._lock:
            self._entries.clear()

    def __enter__(self) -> "TemporaryObjectServer":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
