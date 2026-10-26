"""Opt-in, in-process evidence for real top-level Agent computer-use turns.

This is a side channel, not a calibration pass or an authority grant. It sees
the finalized LLMService request only after the provider has returned an
accepted response, and pairs it with the Agent's parsed executable decision.
Normal sessions never instantiate this collector.
"""
from __future__ import annotations

import base64
from dataclasses import asdict, is_dataclass
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import Any

from .computer_use_history import computer_use_provenance
from backend.computer_use.executable_protocol import parse_vision_desktop_command


DIAGRAMS_ROOT = Path(__file__).resolve().parents[2] / 'logs' / 'computer-use' / 'calibration-evidence'
_SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
_DATA_URL = re.compile(r'data:[A-Za-z0-9.+/-]+;base64,[A-Za-z0-9+/=]+')


def _png(image: Any) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def trusted_observation_for_request(session: Any, trusted_urls: set[str] | frozenset[str]):
    """Match the provider PNG to a runtime-issued image in this exact context."""
    if session is None or getattr(session, 'is_child_context', False) or len(trusted_urls) != 1:
        return None
    context_id = getattr(session, 'session_id', None)
    if not isinstance(context_id, str) or not context_id:
        return None
    url = next(iter(trusted_urls))
    prefix = 'data:image/png;base64,'
    if not url.startswith(prefix):
        return None
    try:
        sent = base64.b64decode(url[len(prefix):], validate=True)
    except ValueError:
        return None
    steps = getattr(getattr(getattr(session, 'agent', None), 'memory', None), 'steps', ())
    matches = []
    for step in steps:
        for image in getattr(step, 'observations_images', ()) or ():
            provenance = computer_use_provenance(image, context_id)
            if provenance is not None and _png(image) == sent:
                matches.append(provenance)
    return matches[-1] if len(matches) == 1 else None


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _redact_data_urls(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact_data_urls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_data_urls(item) for item in value]
    if isinstance(value, str):
        def replace(match):
            token = match.group(0)
            mime = token[5:].split(';', 1)[0]
            encoded = token.split(',', 1)[1]
            try:
                digest = hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest()
            except ValueError:
                digest = hashlib.sha256(token.encode()).hexdigest()
            return f'[redacted {mime} data URL sha256={digest} chars={len(token)}]'
        return _DATA_URL.sub(replace, value)
    return value


class AgentCalibrationEvidence:
    """Save accepted same-Agent request/decision pairs; never score or enable."""

    def __init__(self, session_id: str, *, run_id: str, output_root: Path = DIAGRAMS_ROOT):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError('session_id is required')
        if not isinstance(run_id, str) or not _SAFE_ID.fullmatch(run_id):
            raise ValueError('run_id must be a safe relative name')
        self.session_id = session_id
        self.output_dir = Path(output_root).expanduser() / run_id
        self._lock = threading.Lock()
        self._sequence = 0

    def record_accepted_request(self, request_id: str, provider_request: dict,
                                *, trusted_urls: set[str] | frozenset[str],
                                observation_id: str, geometry_version: int) -> bool:
        if (not isinstance(request_id, str) or not _SAFE_ID.fullmatch(request_id)
                or not isinstance(observation_id, str) or not observation_id
                or type(geometry_version) is not int or geometry_version < 0
                or len(trusted_urls) != 1):
            return False
        url = next(iter(trusted_urls))
        prefix = 'data:image/png;base64,'
        if not url.startswith(prefix):
            return False
        try:
            image = base64.b64decode(url[len(prefix):], validate=True)
        except ValueError:
            return False
        snapshot = _jsonable(provider_request)
        if snapshot.get('request_id') != request_id:
            return False
        # The protected URL must occur in the finalized request, not merely in
        # a stale image still held by Agent memory.
        serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        if url not in serialized:
            return False
        with self._lock:
            directory = self.output_dir / request_id
            if directory.exists():
                return False
            record = {
                'session_id': self.session_id,
                'request_id': request_id,
                'turn_index': self._sequence + 1,
                'observation_id': observation_id,
                'geometry_version': geometry_version,
                'accepted': True,
                'image_sha256': hashlib.sha256(image).hexdigest(),
                'provider_request_sha256': hashlib.sha256(serialized.encode()).hexdigest(),
                'provider_request': _redact_data_urls(snapshot),
                'calibration_passed': False,
            }
            self.output_dir.mkdir(parents=True, exist_ok=True)
            pending = Path(tempfile.mkdtemp(prefix=f'.{request_id}-', dir=self.output_dir))
            try:
                (pending / 'model-visible.png').write_bytes(image)
                (pending / 'request.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
                pending.rename(directory)
                self._sequence += 1
            except BaseException:
                shutil.rmtree(pending, ignore_errors=True)
                raise
        return True

    def record_decision(self, request_id: str, session_id: str, tool_calls: list[dict]) -> bool:
        if session_id != self.session_id or not isinstance(request_id, str) or not _SAFE_ID.fullmatch(request_id):
            return False
        directory = self.output_dir / request_id
        if not (directory / 'request.json').is_file() or len(tool_calls) != 1:
            return False
        call = tool_calls[0]
        if call.get('name') != 'exec_command':
            return False
        arguments = call.get('arguments')
        if not isinstance(arguments, dict) or set(arguments) != {'command'}:
            return False
        try:
            parsed = parse_vision_desktop_command(arguments['command'])
        except (TypeError, ValueError):
            return False
        values = {name: value for name in ('x', 'y', 'zoom', 'action', 'reason', 'target_description')
                  if (value := getattr(parsed, name, None)) is not None}
        record = {'session_id': session_id, 'request_id': request_id,
                  'operation': parsed.operation.value, 'arguments': values,
                  'model_command': arguments['command'], 'calibration_passed': False}
        (directory / 'decision.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
        return True
