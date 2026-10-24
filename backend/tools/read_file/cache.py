from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .models import ReadFileResult


SCHEMA_VERSION = 1


def _safe_options(options: dict[str, Any]) -> dict[str, Any]:
    safe = {}
    for key, value in options.items():
        if key.lower() == "password":
            safe[key] = bool(value)
        else:
            safe[key] = value
    return safe


def build_cache_key(
    source_sha: str,
    parser_version: str,
    model_revision: str,
    config_hash: str,
    options: dict[str, Any],
) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_sha": source_sha,
        "parser_version": parser_version,
        "model_revision": model_revision,
        "config_hash": config_hash,
        "options": _safe_options(options),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ReadFileCache:
    def __init__(
        self,
        path: str | Path,
        *,
        success_ttl_seconds: int,
        partial_ttl_seconds: int,
        failure_ttl_seconds: int,
        clock: Callable[[], float] = time.time,
    ):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._key_path = self.path.with_suffix(self.path.suffix + ".key")
        self._hmac_key = self._load_or_create_hmac_key()
        self._clock = clock
        self._ttls = {
            "success": success_ttl_seconds,
            "partial": partial_ttl_seconds,
            "failure": failure_ttl_seconds,
        }
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS entries (
                    cache_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    result_json TEXT NOT NULL
                )"""
            )
            schema_row = self._connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if schema_row is not None and schema_row[0] != str(SCHEMA_VERSION):
                self._connection.execute("DELETE FROM entries")
            self._connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def _load_or_create_hmac_key(self) -> bytes:
        try:
            key = self._key_path.read_bytes()
        except FileNotFoundError:
            key = secrets.token_bytes(32)
            try:
                descriptor = os.open(
                    self._key_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                key = self._key_path.read_bytes()
            else:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(key)
        if len(key) != 32:
            raise ValueError("read_file cache HMAC key must be exactly 32 bytes")
        return key

    def password_digest(self, password: str | None) -> str | None:
        if password is None:
            return None
        return hmac.new(
            self._hmac_key, password.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    @staticmethod
    def _serialize(result: ReadFileResult) -> str:
        return json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":"))

    def get(self, key: str) -> ReadFileResult | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT expires_at, result_json FROM entries WHERE cache_key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            if float(row[0]) <= self._clock():
                with self._connection:
                    self._connection.execute(
                        "DELETE FROM entries WHERE cache_key = ?", (key,)
                    )
                return None
            try:
                return ReadFileResult.from_dict(json.loads(row[1]))
            except (TypeError, ValueError, KeyError):
                with self._connection:
                    self._connection.execute(
                        "DELETE FROM entries WHERE cache_key = ?", (key,)
                    )
                return None

    def put(self, key: str, result: ReadFileResult, status: str) -> None:
        if status not in self._ttls:
            raise ValueError(f"invalid cache status: {status}")
        serialized = self._serialize(result)
        expires_at = self._clock() + self._ttls[status]
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO entries(cache_key, status, expires_at, result_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                     status=excluded.status,
                     expires_at=excluded.expires_at,
                     result_json=excluded.result_json""",
                (key, status, expires_at, serialized),
            )

    def count(self) -> int:
        with self._lock:
            return int(self._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._connection.close()
