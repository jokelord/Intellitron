"""Process-local Cycle ownership and bounded UI journals (never model memory).

The dedicated directory deliberately cannot resolve an Agent context ID. Its
bounded hub uses the shared Session Hub record/heartbeat serializer; retention
does not change the unbounded canonical Agent/Chat memory journals.
"""
from __future__ import annotations

import asyncio
import copy
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from backend.agent.session_hub import SessionEventRecord
from backend.session_hub_runtime import SessionHubEntry, subscribe_session_hub


class CycleRunConflict(ValueError):
    pass


class CycleRunNotFound(LookupError):
    pass


class CycleRunUnavailable(CycleRunNotFound):
    error_code = 'cycle_run_unavailable'


class CycleRunExpired(CycleRunNotFound):
    error_code = 'cycle_run_expired'


class CycleEventHub:
    """Event-loop-owned journal with bounded live buffers and cursor replay."""

    def __init__(self, run_id: str, retention_seconds: float, max_records: int, clock):
        self.run_id = run_id
        self.retention_seconds = retention_seconds
        self.max_records = max_records
        self.clock = clock
        self.records = deque()
        self.last_sequence = 0
        self.truncated_through = 0
        self.closed = False
        self._subscribers = {}

    def _prune(self):
        cutoff = self.clock() - self.retention_seconds
        while self.records and (len(self.records) > self.max_records or self.records[0][0] < cutoff):
            _, record = self.records.popleft()
            self.truncated_through = max(self.truncated_through, record.sequence)

    def append(self, payload: dict):
        if self.closed:
            raise RuntimeError('Cycle journal is terminal')
        self.last_sequence += 1
        data = copy.deepcopy({**payload, 'cycle_run_id': self.run_id})
        live = SessionEventRecord(self.last_sequence, data)
        if data.get('type') != 'llm_chunk':
            canonical = dict(data)
            if 'image_base64' in canonical:
                canonical.pop('image_base64')
                canonical['image_omitted'] = True
                canonical['omission_reason'] = 'live_thumbnail_only'
            self.records.append((self.clock(), SessionEventRecord(live.sequence, canonical)))
            self._prune()
        for pending, wake in self._subscribers.values():
            pending.append(live)
            wake.set()
        return live

    def close(self):
        self.closed = True
        for _, wake in self._subscribers.values():
            wake.set()

    async def subscribe(self, after_sequence=0):
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
            raise ValueError('after_sequence must be a non-negative integer')
        self._prune()
        pending = deque(maxlen=self.max_records)
        wake = asyncio.Event()
        token = object()
        self._subscribers[token] = (pending, wake)
        cursor = after_sequence
        try:
            if cursor < self.truncated_through:
                cursor = self.truncated_through
                yield self._truncation(cursor)
            # Snapshot is bounded and detached from future pruning.
            for _, record in tuple(self.records):
                if record.sequence > cursor:
                    cursor = record.sequence
                    yield record
            while True:
                if pending:
                    # A slow subscriber may lose live deltas. Explain its gap
                    # and recover canonical records where they still exist.
                    if pending[0].sequence > cursor + 1:
                        self._prune()
                        if cursor < self.truncated_through:
                            cursor = self.truncated_through
                            yield self._truncation(cursor)
                        for _, record in tuple(self.records):
                            if cursor < record.sequence < pending[0].sequence:
                                cursor = record.sequence
                                yield record
                    record = pending.popleft()
                    if record.sequence > cursor:
                        cursor = record.sequence
                        yield record
                    continue
                if self.closed:
                    return
                wake.clear()
                await wake.wait()
        finally:
            self._subscribers.pop(token, None)

    def _truncation(self, sequence):
        return SessionEventRecord(sequence, {
            'type': 'journal_truncated', 'cycle_run_id': self.run_id,
            'truncated_through': sequence,
            'reason': 'retention_limit',
        })


@dataclass
class CycleRunRecord:
    cycle_run_id: str
    request_fingerprint: str
    hub: CycleEventHub
    created_at: float
    status: str = 'running'
    finished_at: float | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    producer_task: asyncio.Task | None = None
    scheduler_task: asyncio.Task | None = None
    agent_session_ids: set[str] = field(default_factory=set)
    active_agent_session_id: str | None = None
    schedule: object | None = None
    request: object | None = None
    agent_identity_seed: str = ''
    scheduled_expires_at: datetime | None = None
    scheduled_remaining: int = 0
    schedule_expired: bool = False
    active_track: str | None = None
    on_finished: Callable | None = field(default=None, repr=False)
    expiry_handle: asyncio.TimerHandle | None = field(default=None, repr=False)

    def publish(self, payload):
        data = dict(payload)
        if self.schedule is not None and 'next_scheduled_for' not in data:
            deadline = getattr(self.schedule, 'next_deadline', None)
            data['next_scheduled_for'] = deadline.isoformat() if deadline else None
        return self.hub.append(data)

    def abandoned_scheduled_due(self) -> int:
        if self.schedule is None:
            return 0
        abandoned = self.schedule.abandon_due()
        if abandoned:
            self.wake.set()
        return len(abandoned)

    def finish(self, status: str, payload: dict):
        if self.finished_at is not None:
            return
        self.status = status
        self.publish({**payload, 'status': status})
        self.finished_at = self.hub.clock()
        self.hub.close()
        if self.on_finished is not None:
            self.on_finished(self)

    async def events(self, after_sequence=0, *, heartbeat_seconds=None):
        entry = SessionHubEntry(self.cycle_run_id, 'cycle', self.hub)
        stream = subscribe_session_hub(entry, after_sequence, heartbeat_seconds=heartbeat_seconds)
        try:
            async for frame in stream:
                yield frame
            yield 'data: [DONE]\n\n'
        finally:
            await stream.aclose()

    def snapshot(self):
        deadline = getattr(self.schedule, 'next_deadline', None) if self.schedule is not None else None
        return {
            'cycle_run_id': self.cycle_run_id, 'session_id': self.cycle_run_id,
            'status': self.status, 'request_fingerprint': self.request_fingerprint,
            'event_sequence': self.hub.last_sequence,
            'timezone': getattr(self.request, 'timezone', None),
            'next_scheduled_for': deadline.isoformat() if deadline else None,
        }


class CycleRunRegistry:
    def __init__(self, *, retention_seconds=86400, max_records=20000, clock: Callable = time.monotonic):
        if retention_seconds <= 0 or max_records <= 0:
            raise ValueError('Cycle journal bounds must be positive')
        self.retention_seconds = retention_seconds
        self.max_records = max_records
        self.clock = clock
        self._records = {}
        self._expired_ids = deque(maxlen=max_records)

    def prune(self):
        now = self.clock()
        for key, record in tuple(self._records.items()):
            record.hub._prune()
            if record.finished_at is not None and now - record.finished_at >= self.retention_seconds:
                self._release(record)

    def _release(self, record):
        if self._records.get(record.cycle_run_id) is not record:
            return
        self._records.pop(record.cycle_run_id)
        if record.cycle_run_id not in self._expired_ids:
            self._expired_ids.append(record.cycle_run_id)
        if record.expiry_handle is not None:
            record.expiry_handle.cancel()
            record.expiry_handle = None
        record.hub.records.clear()

    def _schedule_release(self, record):
        # Exact-record callback cannot evict a later run reusing the same ID.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        record.expiry_handle = loop.call_later(
            self.retention_seconds, self._release, record,
        )

    def resolve(self, run_id: str) -> CycleRunRecord:
        self.prune()
        try:
            return self._records[run_id]
        except KeyError:
            if run_id in self._expired_ids:
                raise CycleRunExpired('cycle_run_expired') from None
            raise CycleRunUnavailable('cycle_run_unavailable') from None

    def admit(self, run_id: str, fingerprint: str):
        self.prune()
        if run_id in self._records:
            record = self._records[run_id]
            if record.request_fingerprint != fingerprint:
                raise CycleRunConflict('cycle_run_fingerprint_mismatch')
            return record, False
        try:
            self._expired_ids.remove(run_id)
        except ValueError:
            pass
        record = CycleRunRecord(run_id, fingerprint, CycleEventHub(
            run_id, self.retention_seconds, self.max_records, self.clock,
        ), self.clock())
        record.on_finished = self._schedule_release
        self._records[run_id] = record
        return record, True
