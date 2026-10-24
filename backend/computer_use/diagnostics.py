"""Bounded, best-effort internal diagnostics; never model observation text."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading

# Shared by every backend instance targeting the same normalized path. flock
# also serializes independent backend processes during write and rollover.
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FIELDS = frozenset({
    'session_id', 'context_nonce', 'sequence', 'operation', 'result', 'error_code',
    'error_type', 'error_field', 'elapsed_ms', 'request_id', 'binding_id', 'frame_id',
    'observation_id', 'geometry_version', 'crop_id', 'stage1_zoom', 'selected_zoom',
    'stage1_point', 'stage2_point', 'source_rect', 'source_size', 'source_point',
    'normalized_source_point', 'phase',
    'stream_generation', 'frame_sequence', 'captured_monotonic', 'arrived_monotonic',
    'logical_origin', 'logical_size', 'action', 'steps', 'calibration_fingerprint',
    'image_size', 'png_sha256', 'trusted_image_count', 'untrusted_image_count',
    'prepared_trusted_image_count', 'prepared_image_count', 'latest_image_present',
    'latest_image_match_count', 'omitted_image_count', 'omitted_block_count', 'fallback',
    'stream',
    'dispatch_state', 'dispatch_stage', 'cleanup_status', 'last_possible_event', 'freshness',
    'pre_action_validation',
    'debug_image_path',
})


class DiagnosticSink:
    def __init__(self, config):
        self.enabled = config.diagnostics_enabled
        path = Path(config.diagnostics_path).expanduser()
        self.path = (path if path.is_absolute() else _PROJECT_ROOT / path).resolve()
        self.max_bytes = config.diagnostics_max_bytes
        self.backup_count = config.diagnostics_backup_count
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(str(self.path), threading.RLock())

    def write(self, record: dict) -> None:
        if not self.enabled:
            return
        try:
            # Top-level allowlist is deliberately not a generic logging API.
            data = {key: value for key, value in record.items() if key in _FIELDS}
            data.update(schema_version='computer_use.diagnostics.v1',
                        recorded_at=datetime.now(timezone.utc).isoformat())
            payload = (json.dumps(data, separators=(',', ':'), allow_nan=False) + '\n').encode()
            if len(payload) > min(self.max_bytes, 16384):
                return
            import fcntl
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                lock_path = self.path.with_name(self.path.name + '.lock')
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    if self.path.exists() and self.path.stat().st_size + len(payload) > self.max_bytes:
                        oldest = self.path.with_name(self.path.name + f'.{self.backup_count}')
                        oldest.unlink(missing_ok=True)
                        for index in range(self.backup_count - 1, 0, -1):
                            source = self.path.with_name(self.path.name + f'.{index}')
                            if source.exists():
                                source.replace(self.path.with_name(self.path.name + f'.{index + 1}'))
                        self.path.replace(self.path.with_name(self.path.name + '.1'))
                    fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
                    try:
                        os.write(fd, payload)
                    finally:
                        os.close(fd)
                finally:
                    os.close(lock_fd)
        except Exception:
            # Disk full, read-only path, malformed metadata, and rollover errors
            # must never alter authorization, safety checks, or input delivery.
            return
