"""Provider-neutral request acceptance and stable-terminal tracking."""

from __future__ import annotations

import heapq
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from enum import Enum
from threading import Condition, RLock
from typing import Callable, Literal

RequestStatus = Literal["unknown", "accepted", "running", "terminal"]
ReservationResolution = Literal["abandoned", "accepted", "running", "terminal"]

_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~:-]*\Z")
_CODE_RE = re.compile(r"[a-z][a-z0-9_-]*\Z")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}\Z")
_MIN_REQUEST_ID_LENGTH = 8
_MAX_REQUEST_ID_LENGTH = 128
_MAX_CODE_LENGTH = 32
_MAX_CONTEXT_IDENTITY_BYTES = 256


class RequestLifecycleState(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    TERMINAL = "terminal"


class RequestLifecycleConflict(ValueError):
    """A request ID was reused for a different logical request."""

    status_code = 409

    def __init__(self) -> None:
        super().__init__("request ID conflicts with an existing request")


class RequestLifecycleCapacityError(RuntimeError):
    """The bounded registry cannot accept another request yet."""

    status_code = 503

    def __init__(self) -> None:
        super().__init__("request lifecycle registry is at capacity")


class RequestLifecycleValidationError(ValueError):
    """Request lifecycle metadata failed canonical validation."""

    status_code = 422

    def __init__(self, field: str) -> None:
        super().__init__(f"invalid request lifecycle metadata: {field}")


@dataclass(frozen=True, slots=True)
class RequestLifecycleReservation:
    """Result of reserving an idempotency key.

    Only the first caller receives a registration token and permission to
    start a worker. Duplicate callers receive the existing public status.
    """

    request_id: str
    should_start: bool
    registration_token: object | None
    wait_token: object
    status: RequestStatus

    @property
    def is_new(self) -> bool:
        return self.should_start


@dataclass(frozen=True, slots=True)
class _RegistrationToken:
    request_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class _ReservationWaitToken:
    request_id: str
    generation: int


@dataclass(slots=True)
class _RequestRecord:
    request_id: str
    request_kind: str
    context_identity: str | None
    payload_fingerprint: str
    registration_token: _RegistrationToken
    state: RequestLifecycleState | None = None
    outcome: str | None = None
    terminal_expires_at: float | None = None


class RequestLifecycleRegistry:
    """Thread-safe request idempotency and lifecycle registry.

    Reserved, accepted, and running records are never evicted. Terminal
    records expire relative to their stable-terminal timestamp. Capacity
    pressure rejects new IDs rather than evicting active or unexpired terminal
    records, preserving idempotency for the full configured TTL.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 600,
        capacity: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)):
            raise ValueError("ttl_seconds must be a finite non-negative number")
        try:
            normalized_ttl_seconds = float(ttl_seconds)
        except OverflowError:
            raise ValueError(
                "ttl_seconds must be a finite non-negative number"
            ) from None
        if not math.isfinite(normalized_ttl_seconds) or normalized_ttl_seconds < 0:
            raise ValueError("ttl_seconds must be a finite non-negative number")
        if (
            isinstance(capacity, bool)
            or not isinstance(capacity, int)
            or capacity < 1
        ):
            raise ValueError("capacity must be a positive integer")
        self._ttl_seconds = normalized_ttl_seconds
        self._capacity = capacity
        self._clock = clock
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._records: dict[str, _RequestRecord] = {}
        self._terminal_expirations: list[tuple[float, int, str]] = []
        self._generation = 0

    def reserve(
        self,
        *,
        request_id: str,
        request_kind: str,
        context_identity: str | None,
        payload_fingerprint: str,
    ) -> RequestLifecycleReservation:
        """Reserve a request ID and grant at most one worker start."""

        self._validate_reservation_metadata(
            request_id=request_id,
            request_kind=request_kind,
            context_identity=context_identity,
            payload_fingerprint=payload_fingerprint,
        )
        with self._lock:
            self._cleanup_expired_locked(self._clock())
            existing = self._records.get(request_id)
            if existing is not None:
                if (
                    existing.request_kind != request_kind
                    or existing.context_identity != context_identity
                    or existing.payload_fingerprint != payload_fingerprint
                ):
                    raise RequestLifecycleConflict()
                return RequestLifecycleReservation(
                    request_id=request_id,
                    should_start=False,
                    registration_token=None,
                    wait_token=_ReservationWaitToken(
                        request_id, existing.registration_token.generation
                    ),
                    status=self._public_status(existing),
                )

            if len(self._records) >= self._capacity:
                raise RequestLifecycleCapacityError()
            self._generation += 1
            token = _RegistrationToken(request_id, self._generation)
            self._records[request_id] = _RequestRecord(
                request_id=request_id,
                request_kind=request_kind,
                context_identity=context_identity,
                payload_fingerprint=payload_fingerprint,
                registration_token=token,
            )
            return RequestLifecycleReservation(
                request_id=request_id,
                should_start=True,
                registration_token=token,
                wait_token=_ReservationWaitToken(request_id, token.generation),
                status="unknown",
            )

    def wait_for_reservation_resolution(
        self,
        *,
        request_id: str,
        request_kind: str,
        context_identity: str | None,
        payload_fingerprint: str,
        wait_token: object,
    ) -> ReservationResolution:
        """Observe one reservation generation without creating or transferring it."""

        self._validate_reservation_metadata(
            request_id=request_id,
            request_kind=request_kind,
            context_identity=context_identity,
            payload_fingerprint=payload_fingerprint,
        )
        if (
            not isinstance(wait_token, _ReservationWaitToken)
            or wait_token.request_id != request_id
        ):
            raise RequestLifecycleValidationError("wait_token")
        with self._condition:
            while True:
                existing = self._records.get(request_id)
                if (
                    existing is None
                    or existing.registration_token.generation != wait_token.generation
                ):
                    return "abandoned"
                if (
                    existing.request_kind != request_kind
                    or existing.context_identity != context_identity
                    or existing.payload_fingerprint != payload_fingerprint
                ):
                    raise RequestLifecycleConflict()
                if existing.state is not None:
                    return existing.state.value
                self._condition.wait()

    def abandon(self, reservation_token: object) -> bool:
        """Release an exact pre-accept reservation after setup failure."""

        with self._condition:
            record = self._record_for_token_locked(reservation_token)
            if record is None or record.state is not None:
                return False
            self._records.pop(record.request_id, None)
            self._condition.notify_all()
            return True

    def accept(self, reservation_token: object) -> None:
        """Publish acceptance after validation and worker registration."""

        with self._condition:
            record = self._record_for_token_locked(reservation_token)
            if record is not None and record.state is None:
                record.state = RequestLifecycleState.ACCEPTED
                self._condition.notify_all()

    def mark_running(self, reservation_token: object) -> None:
        """Mark an accepted request as running without regressing state."""

        with self._condition:
            record = self._record_for_token_locked(reservation_token)
            if record is not None and record.state is RequestLifecycleState.ACCEPTED:
                record.state = RequestLifecycleState.RUNNING
                self._condition.notify_all()

    def mark_terminal(self, reservation_token: object, outcome: str) -> None:
        """Record stable terminal only after acceptance has been published."""

        with self._condition:
            record = self._record_for_token_locked(reservation_token)
            if record is None or record.state not in (
                RequestLifecycleState.ACCEPTED,
                RequestLifecycleState.RUNNING,
            ):
                return
            self._validate_code("outcome", outcome)
            record.state = RequestLifecycleState.TERMINAL
            record.outcome = outcome
            record.terminal_expires_at = self._clock() + self._ttl_seconds
            heapq.heappush(
                self._terminal_expirations,
                (
                    record.terminal_expires_at,
                    record.registration_token.generation,
                    record.request_id,
                ),
            )
            self._condition.notify_all()

    def get_status(self, request_id: str) -> RequestStatus:
        with self._lock:
            self._cleanup_expired_locked(self._clock())
            record = self._records.get(request_id)
            return "unknown" if record is None else self._public_status(record)

    def is_active_request_for_context(
        self, request_id: str, request_kind: str, context_identity: str | None
    ) -> bool:
        """Check that a logical request still owns its context and is active."""

        with self._lock:
            self._cleanup_expired_locked(self._clock())
            record = self._records.get(request_id)
            return bool(
                record is not None
                and record.request_kind == request_kind
                and record.context_identity == context_identity
                and record.state in (
                    RequestLifecycleState.ACCEPTED,
                    RequestLifecycleState.RUNNING,
                )
            )

    def cleanup_expired(self, now: float | None = None) -> int:
        """Remove terminal records whose retention TTL has elapsed."""

        with self._lock:
            return self._cleanup_expired_locked(self._clock() if now is None else now)

    @staticmethod
    def _public_status(record: _RequestRecord) -> RequestStatus:
        return "unknown" if record.state is None else record.state.value

    def _record_for_token_locked(self, token: object) -> _RequestRecord | None:
        if not isinstance(token, _RegistrationToken):
            return None
        record = self._records.get(token.request_id)
        if record is None or record.registration_token is not token:
            return None
        return record

    def _cleanup_expired_locked(self, now: float) -> int:
        removed = 0
        while self._terminal_expirations and self._terminal_expirations[0][0] <= now:
            expires_at, generation, request_id = heapq.heappop(
                self._terminal_expirations
            )
            record = self._records.get(request_id)
            if (
                record is None
                or record.state is not RequestLifecycleState.TERMINAL
                or record.registration_token.generation != generation
                or record.terminal_expires_at != expires_at
            ):
                continue
            self._records.pop(request_id, None)
            removed += 1
        return removed

    @classmethod
    def _validate_reservation_metadata(
        cls,
        *,
        request_id: str,
        request_kind: str,
        context_identity: str | None,
        payload_fingerprint: str,
    ) -> None:
        if (
            not isinstance(request_id, str)
            or not _MIN_REQUEST_ID_LENGTH <= len(request_id) <= _MAX_REQUEST_ID_LENGTH
            or _REQUEST_ID_RE.fullmatch(request_id) is None
        ):
            raise RequestLifecycleValidationError("request_id")
        cls._validate_code("request_kind", request_kind)
        if context_identity is not None:
            if (
                not isinstance(context_identity, str)
                or not context_identity
                or any(
                    unicodedata.category(character).startswith("C")
                    for character in context_identity
                )
                or len(context_identity.encode("utf-8")) > _MAX_CONTEXT_IDENTITY_BYTES
            ):
                raise RequestLifecycleValidationError("context_identity")
        if (
            not isinstance(payload_fingerprint, str)
            or _FINGERPRINT_RE.fullmatch(payload_fingerprint) is None
        ):
            raise RequestLifecycleValidationError("payload_fingerprint")

    @staticmethod
    def _validate_code(field: str, value: str) -> None:
        if (
            not isinstance(value, str)
            or len(value) > _MAX_CODE_LENGTH
            or _CODE_RE.fullmatch(value) is None
        ):
            raise RequestLifecycleValidationError(field)
