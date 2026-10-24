"""Shared directory and lifecycle mechanics for recoverable Session Hubs.

This module is transport infrastructure only.  Domain adapters translate Chat
or Agent output into canonical payloads before publishing them here; Hub data
must never be used as model input.
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from backend.agent.session_hub import AgentSessionHub, SessionEventRecord

SessionKind = Literal["chat", "agent", "cycle"]
ProducerTerminalStatus = Literal["completed", "aborted", "error"]
PayloadResolver = Callable[[dict[str, Any]], dict[str, Any]]
ReleaseCallback = Callable[[], None]


class RequestLifecycle(Protocol):
    """The provider-neutral lifecycle methods used by a Hub producer."""

    def accept(self, reservation_token: object) -> None: ...

    def mark_running(self, reservation_token: object) -> None: ...

    def mark_terminal(self, reservation_token: object, outcome: str) -> None: ...

    def abandon(self, reservation_token: object) -> bool: ...


class SessionHubConflict(RuntimeError):
    """A different entry already owns the requested public session ID."""

    status_code = 409

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session hub already exists: {session_id}")


@dataclass(slots=True, eq=False)
class SessionHubEntry:
    """One exact directory registration and its session-owned resources."""

    session_id: str
    kind: SessionKind
    hub: AgentSessionHub
    resolve_payload: PayloadResolver | None = None
    release_resources: ReleaseCallback | None = None


class SessionHubRuntime:
    """Process-local directory for globally unique recoverable sessions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, SessionHubEntry] = {}

    def register(
        self,
        *,
        session_id: str,
        kind: SessionKind,
        hub: AgentSessionHub,
        resolve_payload: PayloadResolver | None = None,
        release_resources: ReleaseCallback | None = None,
    ) -> SessionHubEntry:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if kind not in ("chat", "agent", "cycle"):
            raise ValueError("kind must be 'chat', 'agent', or 'cycle'")
        if not isinstance(hub, AgentSessionHub):
            raise TypeError("hub must be an AgentSessionHub")
        if hub.released:
            raise ValueError("hub has already been released")
        if resolve_payload is not None and not callable(resolve_payload):
            raise TypeError("resolve_payload must be callable")
        if release_resources is not None and not callable(release_resources):
            raise TypeError("release_resources must be callable")

        entry = SessionHubEntry(
            session_id=session_id,
            kind=kind,
            hub=hub,
            resolve_payload=resolve_payload,
            release_resources=release_resources,
        )
        with self._lock:
            if session_id in self._entries:
                raise SessionHubConflict(session_id)
            self._entries[session_id] = entry
        return entry

    def resolve(self, session_id: str) -> SessionHubEntry | None:
        if not isinstance(session_id, str):
            return None
        with self._lock:
            return self._entries.get(session_id)

    def release_if_same(self, entry: SessionHubEntry) -> bool:
        """Unregister and release only the exact pinned directory entry."""

        if not isinstance(entry, SessionHubEntry):
            return False
        with self._lock:
            if self._entries.get(entry.session_id) is not entry:
                return False
            self._entries.pop(entry.session_id, None)

        try:
            entry.hub.release()
        finally:
            if entry.release_resources is not None:
                entry.release_resources()
        return True


def encode_session_hub_record(
    record: SessionEventRecord,
    entry: SessionHubEntry,
) -> str:
    """Encode one immutable journal record as a canonical SSE data frame."""

    payload = dict(record.payload)
    if entry.resolve_payload is not None:
        payload = entry.resolve_payload(payload)
        if not isinstance(payload, dict):
            raise TypeError("resolve_payload must return a dict")
    payload["event_sequence"] = record.sequence
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def subscribe_session_hub(
    entry: SessionHubEntry,
    after_sequence: int,
    *,
    heartbeat_seconds: float | None = None,
) -> AsyncIterator[str]:
    """Replay then follow one entry through the Hub's canonical cursor path."""

    if (
        isinstance(after_sequence, bool)
        or not isinstance(after_sequence, int)
        or after_sequence < 0
    ):
        raise ValueError("after_sequence must be a non-negative integer")
    if heartbeat_seconds is None:
        from backend.config import get_config

        heartbeat_seconds = get_config().server.session_hub_heartbeat_seconds
    if (
        isinstance(heartbeat_seconds, bool)
        or not isinstance(heartbeat_seconds, (int, float))
        or not math.isfinite(heartbeat_seconds)
        or heartbeat_seconds < 0
    ):
        raise ValueError("heartbeat_seconds must be finite and non-negative")

    subscription = entry.hub.subscribe(after_sequence=after_sequence)
    pending_record: asyncio.Task[SessionEventRecord] | None = None
    try:
        if heartbeat_seconds == 0:
            async for record in subscription:
                yield encode_session_hub_record(record, entry)
            return

        pending_record = asyncio.create_task(anext(subscription))
        while True:
            done, _ = await asyncio.wait(
                (pending_record,),
                timeout=heartbeat_seconds,
            )
            if not done:
                yield ": ping\n\n"
                continue
            try:
                record = pending_record.result()
            except StopAsyncIteration:
                pending_record = None
                return
            pending_record = None
            yield encode_session_hub_record(record, entry)
            pending_record = asyncio.create_task(anext(subscription))
    finally:
        try:
            if pending_record is not None:
                if not pending_record.done():
                    pending_record.cancel()
                try:
                    await pending_record
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
        finally:
            await subscription.aclose()


class SessionProducerPublisher:
    """Publish one accepted producer generation and terminalize it exactly once."""

    _TERMINAL_STATUSES = frozenset({"completed", "aborted", "error"})
    _RESERVED_TYPES = frozenset({"request_accepted", "turn_started", "error", "done"})

    def __init__(
        self,
        *,
        entry: SessionHubEntry,
        request_id: str,
        producer_token: object,
        request_lifecycle_registry: RequestLifecycle,
        reservation_token: object,
    ) -> None:
        if not isinstance(entry, SessionHubEntry):
            raise TypeError("entry must be a SessionHubEntry")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")
        if producer_token is None:
            raise ValueError("producer_token is required")
        self.entry = entry
        self.request_id = request_id
        self._producer_token = producer_token
        self._request_lifecycle_registry = request_lifecycle_registry
        self._reservation_token = reservation_token
        self._state: Literal["new", "accepted", "finishing", "finished"] = "new"
        self._acceptance_prepared = None
        self._acceptance_lifecycle_accepted = False
        self._acceptance_lifecycle_running = False
        self._acceptance_failure: BaseException | None = None
        self._acceptance_appended = False
        self._terminal_status: ProducerTerminalStatus | None = None
        self._terminal_prepared = None
        self._terminal_records_appended = False
        self._hub_closed = False
        self._lifecycle_terminal = False
        self._producer_released = False
        self._lock = threading.RLock()

    @property
    def lifecycle_accepted(self) -> bool:
        with self._lock:
            return self._acceptance_lifecycle_accepted

    @property
    def acceptance_published(self) -> bool:
        with self._lock:
            return self._acceptance_appended

    def abandon_if_unaccepted(self) -> bool:
        """Abandon only this exact reservation before lifecycle acceptance.

        A stale producer never releases a replacement generation.  Once the
        lifecycle registry accepted the reservation, terminalization (not
        abandon) remains the only valid settlement.
        """

        with self._lock:
            if self._acceptance_lifecycle_accepted:
                return False
            abandoned = self._request_lifecycle_registry.abandon(
                self._reservation_token
            )
            if self.entry.hub.capture_active_producer() is self._producer_token:
                self._producer_released = self.entry.hub.release_producer(
                    self._producer_token
                )
            elif self._acceptance_owner_is_irrecoverable_locked():
                self._producer_released = True
            self._state = "finished"
            return abandoned

    def _acceptance_owner_is_irrecoverable_locked(self) -> bool:
        return (
            self.entry.hub.released
            or self.entry.hub.capture_active_producer() is not self._producer_token
        )

    def _terminalize_irrecoverable_acceptance_locked(self) -> None:
        """Settle only this request token when its Hub owner cannot be touched."""

        if not self._lifecycle_terminal:
            self._request_lifecycle_registry.mark_terminal(
                self._reservation_token,
                "error",
            )
            self._lifecycle_terminal = True
        self._terminal_status = "error"
        self._state = "finished"

    def _compensate_acceptance_locked(self, error: BaseException) -> None:
        prepared = self._acceptance_prepared
        if prepared is None:
            raise RuntimeError("session producer acceptance plan is missing")
        if not self._acceptance_appended:
            if self._acceptance_owner_is_irrecoverable_locked():
                raise RuntimeError("producer token is not current")
            self.entry.hub.append_batch(
                prepared,
                producer_token=self._producer_token,
            )
            self._acceptance_appended = True
        self._state = "accepted"
        self._prepare_terminal_locked("error", error)
        self._state = "finishing"
        self._advance_terminal_locked()

    def _recover_acceptance_failure_locked(self, error: BaseException) -> None:
        try:
            self._compensate_acceptance_locked(error)
        except BaseException:
            if not self._acceptance_owner_is_irrecoverable_locked():
                raise
            self._terminalize_irrecoverable_acceptance_locked()

    @staticmethod
    def _materialize_error(error: BaseException | None) -> str:
        if error is None:
            return "Session producer failed"
        try:
            message = str(error)
        except BaseException:
            message = type(error).__name__
        return message or type(error).__name__

    def _prepare_terminal_locked(
        self,
        status: ProducerTerminalStatus,
        error: BaseException | None,
    ) -> None:
        if self._terminal_status is not None:
            if self._terminal_status != status:
                raise RuntimeError("session producer terminal status already selected")
            return

        payloads: list[dict[str, Any]] = []
        if status == "error":
            payloads.append(
                {
                    "type": "error",
                    "session_id": self.entry.session_id,
                    "request_id": self.request_id,
                    "error": self._materialize_error(error),
                }
            )
        payloads.append(
            {
                "type": "done",
                "session_id": self.entry.session_id,
                "request_id": self.request_id,
                "status": status,
            }
        )
        prepared = self.entry.hub.prepare_append_batch(payloads)
        self._terminal_status = status
        self._terminal_prepared = prepared

    def _advance_terminal_locked(self) -> None:
        status = self._terminal_status
        prepared = self._terminal_prepared
        if status is None or prepared is None:
            raise RuntimeError("session producer terminal plan is missing")

        if not self._terminal_records_appended:
            if self.entry.hub.capture_active_producer() is not self._producer_token:
                raise RuntimeError("producer token is not current")
            self.entry.hub.append_batch(
                prepared,
                producer_token=self._producer_token,
            )
            self._terminal_records_appended = True

        if not self._hub_closed:
            if self.entry.hub.capture_active_producer() is not self._producer_token:
                raise RuntimeError("producer token is not current")
            closed = self.entry.hub.close(
                status,
                producer_token=self._producer_token,
            )
            if not closed:
                if (
                    self.entry.hub.capture_active_producer() is self._producer_token
                    and self.entry.hub.close_reason == status
                ):
                    self._hub_closed = True
                else:
                    raise RuntimeError("producer token is not current")
            else:
                self._hub_closed = True

        if not self._lifecycle_terminal:
            self._request_lifecycle_registry.mark_terminal(
                self._reservation_token,
                status,
            )
            self._lifecycle_terminal = True

        if not self._producer_released:
            if not self.entry.hub.release_producer(self._producer_token):
                raise RuntimeError("producer token is not current")
            self._producer_released = True
        self._state = "finished"

    def accept(
        self,
        turn_started: dict[str, Any],
        acceptance_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Publish lifecycle acceptance and the first canonical turn boundary."""

        with self._lock:
            if self._state != "new":
                raise RuntimeError("session producer was already accepted")
            if self._acceptance_failure is not None:
                original_failure = self._acceptance_failure
                try:
                    self._recover_acceptance_failure_locked(original_failure)
                except BaseException as compensation_error:
                    original_failure.add_note(
                        "Session Hub acceptance compensation failed: "
                        f"{type(compensation_error).__name__}"
                    )
                raise original_failure
            if self.entry.hub.capture_active_producer() is not self._producer_token:
                raise RuntimeError("producer token is not current")
            if not isinstance(turn_started, dict):
                raise TypeError("turn_started must be a dict")
            if (
                turn_started.get("type") != "timeline_event"
                or not isinstance(turn_started.get("event"), dict)
                or turn_started["event"].get("kind") != "turn_started"
            ):
                raise ValueError("turn_started must be a canonical timeline event")
            acceptance = {
                "type": "request_accepted",
                "request_id": self.request_id,
                "session_id": self.entry.session_id,
                "status": "accepted",
            }
            if acceptance_metadata is not None:
                if not isinstance(acceptance_metadata, dict):
                    raise TypeError("acceptance_metadata must be a dict")
                reserved = {"type", "request_id", "session_id", "status"}
                if reserved.intersection(acceptance_metadata):
                    raise ValueError("acceptance metadata contains reserved fields")
                acceptance.update(acceptance_metadata)
            prepared = self.entry.hub.prepare_append_batch(
                (acceptance, turn_started)
            )
            self._acceptance_prepared = prepared

            self._request_lifecycle_registry.accept(self._reservation_token)
            self._acceptance_lifecycle_accepted = True
            try:
                self._request_lifecycle_registry.mark_running(
                    self._reservation_token
                )
                self._acceptance_lifecycle_running = True
                self.entry.hub.append_batch(
                    prepared,
                    producer_token=self._producer_token,
                )
                self._acceptance_appended = True
                self._state = "accepted"
            except BaseException as exc:
                self._acceptance_failure = exc
                if self._acceptance_lifecycle_accepted:
                    try:
                        self._recover_acceptance_failure_locked(exc)
                    except BaseException as compensation_error:
                        exc.add_note(
                            "Session Hub acceptance compensation failed: "
                            f"{type(compensation_error).__name__}"
                        )
                raise

    def publish(self, payload: dict[str, Any]) -> SessionEventRecord:
        """Append one domain-translated record after canonical acceptance."""

        with self._lock:
            if self._state != "accepted":
                raise RuntimeError("session producer has not been accepted")
            if not isinstance(payload, dict):
                raise TypeError("payload must be a dict")
            payload_type = payload.get("type")
            event = payload.get("event")
            if payload_type in self._RESERVED_TYPES or (
                payload_type == "timeline_event"
                and isinstance(event, dict)
                and event.get("kind") == "turn_started"
            ):
                raise ValueError("lifecycle records must use accept() or finish()")
            return self.entry.hub.append(
                payload,
                producer_token=self._producer_token,
            )

    def finish(
        self,
        status: ProducerTerminalStatus,
        error: BaseException | None = None,
    ) -> None:
        """Publish error/done, close, mark terminal, and release exact ownership."""

        if status not in self._TERMINAL_STATUSES:
            raise ValueError("status must be completed, aborted, or error")
        if error is not None and status != "error":
            raise ValueError("error details require status='error'")

        with self._lock:
            if self._state == "finished":
                return
            if self._acceptance_failure is not None:
                self._recover_acceptance_failure_locked(
                    self._acceptance_failure
                )
                if self._state == "finished":
                    return
            if self._state not in ("accepted", "finishing"):
                raise RuntimeError("session producer has not been accepted")
            self._prepare_terminal_locked(status, error)
            self._state = "finishing"
            self._advance_terminal_locked()
