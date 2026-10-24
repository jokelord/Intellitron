"""Session-owned Agent event journal and cursor subscriptions.

This module is transport-only. Journal payloads are never model memory and the
hub deliberately has no dependency on Agent prompt or message types.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any


def _immutable(*_args, **_kwargs):
    raise TypeError("session event payload is immutable")


class _FrozenJSONDict(dict):
    """An immutable dict subclass retained by the stdlib JSON encoder."""

    def __init__(self, items=()) -> None:
        dict.__init__(self, items)

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


class _FrozenJSONList(list):
    """An immutable list subclass retained by the stdlib JSON encoder."""

    def __init__(self, items=()) -> None:
        list.__init__(self, items)

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable


def _freeze_json_value(
    value: Any,
    *,
    active: set[int],
    memo: dict[int, Any],
) -> Any:
    """Detach and recursively freeze one JSON-compatible value exactly once."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        value_id = id(value)
        if value_id in active:
            raise ValueError("session event payload contains a cycle")
        if value_id in memo:
            return memo[value_id]
        active.add(value_id)
        try:
            frozen_items = []
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError("session event payload keys must be strings")
                frozen_items.append(
                    (key, _freeze_json_value(item, active=active, memo=memo))
                )
            frozen = _FrozenJSONDict(frozen_items)
            memo[value_id] = frozen
            return frozen
        finally:
            active.remove(value_id)
    if isinstance(value, (list, tuple)):
        value_id = id(value)
        if value_id in active:
            raise ValueError("session event payload contains a cycle")
        if value_id in memo:
            return memo[value_id]
        active.add(value_id)
        try:
            frozen = _FrozenJSONList(
                _freeze_json_value(item, active=active, memo=memo)
                for item in value
            )
            memo[value_id] = frozen
            return frozen
        finally:
            active.remove(value_id)
    raise TypeError(
        f"session event payload value is not JSON-compatible: {type(value).__name__}"
    )


def _freeze_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError("session event payload must be a dict")
    return _freeze_json_value(payload, active=set(), memo={})


@dataclass(frozen=True, slots=True)
class SessionEventRecord:
    """One immutable, session-local journal entry."""

    sequence: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PreparedSessionEventBatch:
    """A fully validated immutable payload batch awaiting one atomic commit."""

    payloads: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _ProducerClaim:
    generation: int


@dataclass(slots=True)
class _ProducerHandoff:
    expected_token: _ProducerClaim
    loop: asyncio.AbstractEventLoop
    notification: asyncio.Event
    claimed_token: _ProducerClaim | None = None


@dataclass(slots=True)
class _SubscriberRegistration:
    cursor: int
    generation: int
    release_epoch: int
    loop: asyncio.AbstractEventLoop
    notification: asyncio.Event


class AgentSessionHub:
    """A single-copy append-only journal with generation-bound subscribers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._journal: list[SessionEventRecord] = []
        self._next_sequence = 1
        self._subscribers: dict[str, _SubscriberRegistration] = {}
        self._active_producer_token: _ProducerClaim | None = None
        self._producer_handoff: _ProducerHandoff | None = None
        self._generation = 0
        self._terminal_sequences: dict[int, int] = {}
        self._close_reason: str | None = None
        self._release_epoch = 0
        self._released = False

    def _wake_subscribers(
        self,
        subscribers: tuple[tuple[str, _SubscriberRegistration], ...],
    ) -> None:
        failed: list[tuple[str, _SubscriberRegistration]] = []
        for subscriber_id, subscriber in subscribers:
            try:
                subscriber.loop.call_soon_threadsafe(subscriber.notification.set)
            except RuntimeError:
                failed.append((subscriber_id, subscriber))
        if not failed:
            return
        with self._lock:
            for subscriber_id, subscriber in failed:
                if self._subscribers.get(subscriber_id) is subscriber:
                    self._subscribers.pop(subscriber_id, None)

    def _validate_append_owner_locked(self, producer_token: object | None) -> None:
        if self._active_producer_token is not None:
            if producer_token is not self._active_producer_token:
                raise RuntimeError("producer token is not current")
            return
        if producer_token is not None:
            raise RuntimeError("producer token is not current")
        if self._generation != 0:
            raise RuntimeError("producer token is required")

    def append(
        self,
        payload: dict[str, Any],
        *,
        producer_token: object | None = None,
    ) -> SessionEventRecord:
        """Append one canonical record for the current producer generation."""

        prepared = self.prepare_append_batch((payload,))
        return self.append_batch(
            prepared,
            producer_token=producer_token,
        )[0]

    @staticmethod
    def prepare_append_batch(
        payloads: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    ) -> PreparedSessionEventBatch:
        """Freeze every payload before any journal or sequence mutation."""

        if not isinstance(payloads, (tuple, list)) or not payloads:
            raise ValueError("session event batch must contain at least one payload")
        return PreparedSessionEventBatch(
            payloads=tuple(_freeze_payload(payload) for payload in payloads)
        )

    def append_batch(
        self,
        prepared: PreparedSessionEventBatch,
        *,
        producer_token: object | None = None,
    ) -> tuple[SessionEventRecord, ...]:
        """Atomically append one preflighted batch for the exact producer."""

        if not isinstance(prepared, PreparedSessionEventBatch):
            raise TypeError("prepared must be a PreparedSessionEventBatch")
        with self._lock:
            if self._released:
                raise RuntimeError("session hub has been released")
            self._validate_append_owner_locked(producer_token)
            if self._generation in self._terminal_sequences:
                raise RuntimeError("session hub execution is closed")
            records = tuple(
                SessionEventRecord(
                    sequence=self._next_sequence + offset,
                    payload=frozen_payload,
                )
                for offset, frozen_payload in enumerate(prepared.payloads)
            )
            self._next_sequence += len(records)
            self._journal.extend(records)
            subscribers = tuple(self._subscribers.items())
        self._wake_subscribers(subscribers)
        return records

    def find_exact_appended_batch(
        self,
        prepared: PreparedSessionEventBatch,
        *,
        producer_token: object,
        after_sequence: int,
    ) -> tuple[SessionEventRecord, ...] | None:
        """Find one exact prepared batch committed after a pinned sequence.

        This is a narrow fault-reconciliation query for callers whose
        ``append_batch`` raised after mutating the journal.  Payload identity
        anchors the match to the caller's frozen batch; sequence and content
        checks defend against unrelated writes.  The exact producer must
        still own the Hub generation.
        """

        if not isinstance(prepared, PreparedSessionEventBatch):
            raise TypeError("prepared must be a PreparedSessionEventBatch")
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
        ):
            raise ValueError("after_sequence must be a non-negative integer")
        with self._lock:
            if self._released or self._active_producer_token is not producer_token:
                return None
            batch_size = len(prepared.payloads)
            last_start = len(self._journal) - batch_size
            match: tuple[SessionEventRecord, ...] | None = None
            for start_index in range(after_sequence, last_start + 1):
                records = tuple(
                    self._journal[start_index + offset]
                    for offset in range(batch_size)
                )
                if all(
                    record.sequence == start_index + offset + 1
                    and record.payload is prepared.payloads[offset]
                    and record.payload == prepared.payloads[offset]
                    for offset, record in enumerate(records)
                ):
                    if match is not None:
                        raise RuntimeError(
                            "prepared session event batch was appended more than once"
                        )
                    match = records
            return match

    def find_payloads_by_fields(
        self, fields: Mapping[str, Any]
    ) -> tuple[SessionEventRecord, ...]:
        """Return one locked, read-only snapshot matching a payload subset."""

        if not isinstance(fields, Mapping):
            raise TypeError("fields must be a mapping")
        with self._lock:
            if self._released:
                return ()
            return tuple(
                record
                for record in self._journal
                if all(record.payload.get(key) == value for key, value in fields.items())
            )

    async def subscribe(
        self, after_sequence: int = 0
    ) -> AsyncIterator[SessionEventRecord]:
        """Replay incrementally, then follow one exact execution generation."""

        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
        ):
            raise ValueError("after_sequence must be a non-negative integer")

        subscriber_id = uuid.uuid4().hex
        with self._lock:
            if self._released:
                return
            registration = _SubscriberRegistration(
                cursor=after_sequence,
                generation=self._generation,
                release_epoch=self._release_epoch,
                loop=asyncio.get_running_loop(),
                notification=asyncio.Event(),
            )
            self._subscribers[subscriber_id] = registration

        try:
            while True:
                with self._lock:
                    if (
                        self._released
                        or registration.release_epoch != self._release_epoch
                    ):
                        return
                    terminal_sequence = self._terminal_sequences.get(
                        registration.generation
                    )
                    next_sequence = registration.cursor + 1
                    if (
                        terminal_sequence is not None
                        and next_sequence > terminal_sequence
                    ):
                        return
                    if next_sequence < self._next_sequence:
                        # Positive contiguous sequences map directly to the
                        # canonical list offset. Deliver exactly one reference
                        # so release is checked again before every record.
                        record = self._journal[next_sequence - 1]
                        registration.cursor = next_sequence
                    elif terminal_sequence is not None:
                        return
                    else:
                        record = None
                        registration.notification.clear()

                if record is not None:
                    with self._lock:
                        if (
                            self._released
                            or registration.release_epoch != self._release_epoch
                        ):
                            return
                    yield record
                    continue
                await registration.notification.wait()
        finally:
            with self._lock:
                if self._subscribers.get(subscriber_id) is registration:
                    self._subscribers.pop(subscriber_id, None)

    def _claim_producer_locked(
        self,
    ) -> tuple[
        _ProducerClaim,
        tuple[tuple[str, _SubscriberRegistration], ...],
    ]:
        previous_generation = self._generation
        if previous_generation not in self._terminal_sequences:
            self._terminal_sequences[previous_generation] = self._next_sequence - 1
            subscribers = tuple(
                (subscriber_id, subscriber)
                for subscriber_id, subscriber in self._subscribers.items()
                if subscriber.generation == previous_generation
            )
        else:
            subscribers = ()
        self._generation += 1
        token = _ProducerClaim(generation=self._generation)
        self._active_producer_token = token
        self._close_reason = None
        return token, subscribers

    def claim_producer(self) -> object | None:
        """Claim the sole producer and return an opaque generation token."""

        with self._lock:
            if self._released or self._active_producer_token is not None:
                return None
            token, subscribers = self._claim_producer_locked()
        self._wake_subscribers(subscribers)
        return token

    def capture_active_producer(self) -> object | None:
        """Capture the exact current producer for an internal handoff barrier."""

        with self._lock:
            return self._active_producer_token

    async def claim_producer_after(self, expected_token: object | None) -> object | None:
        """Atomically claim the generation immediately after one exact owner."""

        if expected_token is None:
            return self.claim_producer()
        if not isinstance(expected_token, _ProducerClaim):
            return None

        loop = asyncio.get_running_loop()
        handoff = _ProducerHandoff(
            expected_token=expected_token,
            loop=loop,
            notification=asyncio.Event(),
        )
        with self._lock:
            if self._released:
                return None
            if self._active_producer_token is expected_token:
                if self._producer_handoff is not None:
                    return None
                self._producer_handoff = handoff
                token = None
                subscribers = ()
            elif (
                self._active_producer_token is None
                and self._generation == expected_token.generation
            ):
                token, subscribers = self._claim_producer_locked()
            else:
                return None
        self._wake_subscribers(subscribers)
        if token is not None:
            return token

        try:
            await handoff.notification.wait()
        except asyncio.CancelledError:
            subscribers = ()
            with self._lock:
                if self._producer_handoff is handoff:
                    self._producer_handoff = None
                elif (
                    handoff.claimed_token is not None
                    and self._active_producer_token is handoff.claimed_token
                ):
                    generation = handoff.claimed_token.generation
                    self._terminal_sequences.setdefault(
                        generation, self._next_sequence - 1
                    )
                    self._active_producer_token = None
                    subscribers = tuple(
                        (subscriber_id, subscriber)
                        for subscriber_id, subscriber in self._subscribers.items()
                        if subscriber.generation == generation
                    )
            self._wake_subscribers(subscribers)
            raise
        return handoff.claimed_token

    def release_producer(self, token: object) -> bool:
        """Release producer ownership only for the exact current token."""

        with self._lock:
            if self._active_producer_token is not token:
                return False
            handoff = self._producer_handoff
            if handoff is not None and handoff.expected_token is token:
                self._active_producer_token = None
                claimed_token, subscribers = self._claim_producer_locked()
                handoff.claimed_token = claimed_token
                self._producer_handoff = None
            else:
                self._active_producer_token = None
                handoff = None
                subscribers = ()
        self._wake_subscribers(subscribers)
        if handoff is not None:
            try:
                handoff.loop.call_soon_threadsafe(handoff.notification.set)
            except RuntimeError:
                # The cancelled owner will be reclaimed by session release.
                pass
        return True

    def close(
        self,
        reason: str,
        *,
        producer_token: object | None = None,
    ) -> bool:
        """Close the current generation and wake its bound followers."""

        with self._lock:
            if self._released:
                return False
            if self._active_producer_token is not None:
                if producer_token is not self._active_producer_token:
                    return False
            elif producer_token is not None:
                return False
            if self._generation in self._terminal_sequences:
                return False
            self._terminal_sequences[self._generation] = self._next_sequence - 1
            self._close_reason = str(reason)
            subscribers = tuple(
                (subscriber_id, subscriber)
                for subscriber_id, subscriber in self._subscribers.items()
                if subscriber.generation == self._generation
            )
        self._wake_subscribers(subscribers)
        return True

    def release(self) -> bool:
        """Permanently release this exact Hub generation and its references."""

        with self._lock:
            if self._released:
                return False
            self._released = True
            self._release_epoch += 1
            if self._close_reason is None:
                self._close_reason = "released"
            self._active_producer_token = None
            handoff = self._producer_handoff
            self._producer_handoff = None
            self._journal.clear()
            self._terminal_sequences.clear()
            subscribers = tuple(self._subscribers.items())
            self._subscribers.clear()
        self._wake_subscribers(subscribers)
        if handoff is not None:
            try:
                handoff.loop.call_soon_threadsafe(handoff.notification.set)
            except RuntimeError:
                pass
        return True

    @property
    def last_sequence(self) -> int:
        with self._lock:
            return self._next_sequence - 1

    @property
    def journal_size(self) -> int:
        with self._lock:
            return len(self._journal)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    @property
    def has_active_producer(self) -> bool:
        with self._lock:
            return self._active_producer_token is not None

    @property
    def close_reason(self) -> str | None:
        with self._lock:
            return self._close_reason

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released
