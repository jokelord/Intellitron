import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.request_lifecycle import (
    RequestLifecycleCapacityError,
    RequestLifecycleConflict,
    RequestLifecycleRegistry,
    RequestLifecycleValidationError,
)


class _Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _fingerprint(value: str = "payload-1") -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reserve(
    registry: RequestLifecycleRegistry,
    request_id: str = "request-1",
    *,
    request_kind: str = "chat",
    context_identity: str | None = "conversation-1",
    payload_fingerprint: str = _fingerprint(),
):
    return registry.reserve(
        request_id=request_id,
        request_kind=request_kind,
        context_identity=context_identity,
        payload_fingerprint=payload_fingerprint,
    )


def test_same_request_reservation_grants_exactly_one_worker_start():
    registry = RequestLifecycleRegistry()

    first = _reserve(registry)
    duplicate = _reserve(registry)

    assert first.should_start is True
    assert first.registration_token is not None
    assert duplicate.should_start is False
    assert duplicate.registration_token is None
    assert duplicate.request_id == first.request_id
    assert registry.get_status("request-1") == "unknown"


@pytest.mark.parametrize(
    ("override", "expected_field"),
    [
        ({"payload_fingerprint": _fingerprint("different")}, "payload_fingerprint"),
        ({"context_identity": "different"}, "context_identity"),
        ({"request_kind": "agent"}, "request_kind"),
    ],
)
def test_same_request_id_with_different_identity_raises_conflict(
    override, expected_field
):
    registry = RequestLifecycleRegistry()
    _reserve(registry)

    with pytest.raises(RequestLifecycleConflict) as exc_info:
        _reserve(registry, **override)

    assert exc_info.value.status_code == 409
    assert str(exc_info.value) == "request ID conflicts with an existing request"
    assert expected_field not in str(exc_info.value)
    assert "request-1" not in str(exc_info.value)


def test_concurrent_reservations_grant_only_one_worker_start():
    registry = RequestLifecycleRegistry()

    with ThreadPoolExecutor(max_workers=16) as executor:
        reservations = list(executor.map(lambda _: _reserve(registry), range(64)))

    assert sum(reservation.should_start for reservation in reservations) == 1
    assert sum(
        reservation.registration_token is not None for reservation in reservations
    ) == 1


def test_registration_token_drives_monotonic_public_states():
    registry = RequestLifecycleRegistry()
    reservation = _reserve(registry)
    token = reservation.registration_token
    assert token is not None

    registry.mark_running(token)
    assert registry.get_status("request-1") == "unknown"

    registry.accept(token)
    assert registry.get_status("request-1") == "accepted"

    registry.mark_running(token)
    assert registry.get_status("request-1") == "running"

    registry.accept(token)
    assert registry.get_status("request-1") == "running"

    registry.mark_terminal(token, "completed")
    registry.mark_running(token)
    registry.mark_terminal(token, "late_error")
    assert registry.get_status("request-1") == "terminal"


def test_active_request_context_lookup_requires_accepted_matching_owner():
    registry = RequestLifecycleRegistry()
    reservation = _reserve(
        registry,
        request_kind="agent",
        context_identity="agent-session",
    )
    token = reservation.registration_token
    assert token is not None

    matches = lambda kind="agent", context="agent-session": (
        registry.is_active_request_for_context("request-1", kind, context)
    )
    assert not matches()
    registry.accept(token)
    assert matches()
    assert not matches(context="other-session")
    assert not matches(kind="chat")
    registry.mark_running(token)
    assert matches()
    registry.mark_terminal(token, "completed")
    assert not matches()


def test_active_records_never_expire_and_ttl_starts_at_terminal():
    clock = _Clock()
    registry = RequestLifecycleRegistry(clock=clock)
    reservation = _reserve(registry)
    token = reservation.registration_token
    assert token is not None

    clock.now = 10_000
    assert registry.cleanup_expired() == 0
    assert registry.get_status("request-1") == "unknown"

    registry.accept(token)
    clock.now = 20_000
    assert registry.cleanup_expired() == 0
    assert registry.get_status("request-1") == "accepted"

    registry.mark_terminal(token, "completed")
    clock.now = 20_599.999
    assert registry.cleanup_expired() == 0
    assert registry.get_status("request-1") == "terminal"

    clock.now = 20_600
    assert registry.cleanup_expired() == 1
    assert registry.get_status("request-1") == "unknown"


def test_old_registration_token_cannot_mutate_reused_request_id():
    clock = _Clock()
    registry = RequestLifecycleRegistry(ttl_seconds=1, clock=clock)
    old = _reserve(registry)
    old_token = old.registration_token
    assert old_token is not None
    registry.accept(old_token)
    registry.mark_terminal(old_token, "completed")

    clock.now = 1
    assert registry.cleanup_expired() == 1
    new = _reserve(registry)
    new_token = new.registration_token
    assert new_token is not None
    registry.accept(new_token)
    registry.mark_running(new_token)

    registry.mark_terminal(old_token, "stale_callback")
    registry.accept(old_token)

    assert registry.get_status("request-1") == "running"


def test_abandon_releases_only_a_reserved_record_for_reuse():
    registry = RequestLifecycleRegistry(capacity=1)
    abandoned = _reserve(registry)
    token = abandoned.registration_token
    assert token is not None

    assert registry.abandon(token) is True
    assert registry.abandon(token) is False

    replacement = _reserve(registry, payload_fingerprint=_fingerprint("replacement"))
    assert replacement.should_start is True
    assert replacement.registration_token is not token


def test_abandon_cannot_remove_accepted_or_running_records():
    registry = RequestLifecycleRegistry(capacity=2)
    accepted = _reserve(registry, "accepted-request")
    registry.accept(accepted.registration_token)
    running = _reserve(registry, "running-request")
    registry.accept(running.registration_token)
    registry.mark_running(running.registration_token)

    assert registry.abandon(accepted.registration_token) is False
    assert registry.abandon(running.registration_token) is False
    assert registry.get_status("accepted-request") == "accepted"
    assert registry.get_status("running-request") == "running"


def test_stale_abandon_token_cannot_remove_reused_request_id():
    registry = RequestLifecycleRegistry(capacity=1)
    first = _reserve(registry)
    stale_token = first.registration_token
    assert registry.abandon(stale_token) is True

    replacement = _reserve(registry, payload_fingerprint=_fingerprint("replacement"))
    replacement_token = replacement.registration_token
    assert replacement_token is not None
    assert registry.abandon(stale_token) is False
    registry.accept(replacement_token)

    assert registry.get_status("request-1") == "accepted"


def test_concurrent_reuse_after_abandon_grants_one_new_worker():
    registry = RequestLifecycleRegistry()
    first = _reserve(registry)
    assert registry.abandon(first.registration_token) is True

    with ThreadPoolExecutor(max_workers=16) as executor:
        reservations = list(executor.map(lambda _: _reserve(registry), range(64)))

    assert sum(reservation.should_start for reservation in reservations) == 1


def test_waiting_duplicate_only_observes_abandon_before_live_retry_reserves():
    registry = RequestLifecycleRegistry()
    first = _reserve(registry)

    with ThreadPoolExecutor(max_workers=1) as executor:
        waiting = executor.submit(
            registry.wait_for_reservation_resolution,
            request_id="request-1",
            request_kind="chat",
            context_identity="conversation-1",
            payload_fingerprint=_fingerprint(),
            wait_token=first.wait_token,
        )
        assert waiting.done() is False
        assert registry.abandon(first.registration_token) is True
        resolution = waiting.result(timeout=1)

    assert resolution == "abandoned"
    replacement = _reserve(registry)
    assert replacement.should_start is True
    assert replacement.registration_token is not first.registration_token


def test_waiting_duplicate_returns_first_public_state_after_acceptance():
    registry = RequestLifecycleRegistry()
    first = _reserve(registry)

    with ThreadPoolExecutor(max_workers=1) as executor:
        waiting = executor.submit(
            registry.wait_for_reservation_resolution,
            request_id="request-1",
            request_kind="chat",
            context_identity="conversation-1",
            payload_fingerprint=_fingerprint(),
            wait_token=first.wait_token,
        )
        assert waiting.done() is False
        registry.accept(first.registration_token)
        resolution = waiting.result(timeout=1)

    assert resolution == "accepted"


def test_capacity_pressure_preserves_terminal_for_full_ttl():
    clock = _Clock()
    registry = RequestLifecycleRegistry(
        ttl_seconds=600,
        capacity=1,
        clock=clock,
    )
    terminal = _reserve(registry, "terminal-request")
    registry.accept(terminal.registration_token)
    registry.mark_terminal(terminal.registration_token, "completed")

    clock.now = 599.999
    with pytest.raises(RequestLifecycleCapacityError) as exc_info:
        _reserve(registry, "new-request")

    assert exc_info.value.status_code == 503
    duplicate = _reserve(registry, "terminal-request")
    assert duplicate.should_start is False
    assert duplicate.status == "terminal"
    assert registry.get_status("terminal-request") == "terminal"

    clock.now = 600
    replacement = _reserve(registry, "new-request")
    assert replacement.should_start is True
    assert registry.get_status("terminal-request") == "unknown"


def test_capacity_pressure_never_overrides_conflict_with_generic_409():
    registry = RequestLifecycleRegistry(capacity=1)
    _reserve(registry)

    with pytest.raises(RequestLifecycleConflict):
        _reserve(registry, payload_fingerprint=_fingerprint("different"))


def test_concurrent_terminal_and_cleanup_operations_are_safe():
    clock = _Clock()
    registry = RequestLifecycleRegistry(ttl_seconds=1, capacity=128, clock=clock)
    tokens = []
    for index in range(128):
        reservation = _reserve(registry, f"request-{index}")
        registry.accept(reservation.registration_token)
        tokens.append(reservation.registration_token)

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [
            executor.submit(registry.mark_terminal, token, "completed")
            for token in tokens
        ]
        futures.extend(
            executor.submit(registry.cleanup_expired, 1.0) for _ in range(64)
        )
        for future in futures:
            future.result()

    registry.cleanup_expired(now=1.0)
    assert all(
        registry.get_status(f"request-{index}") == "unknown"
        for index in range(128)
    )


@pytest.mark.parametrize(
    "ttl_seconds",
    [True, float("nan"), float("inf"), -float("inf"), -1, 10**1000],
)
def test_registry_rejects_nonfinite_or_negative_ttl(ttl_seconds):
    with pytest.raises(ValueError):
        RequestLifecycleRegistry(ttl_seconds=ttl_seconds)


@pytest.mark.parametrize("capacity", [True, False, 0, -1, 1.5, "10", None])
def test_registry_requires_non_boolean_positive_integer_capacity(capacity):
    with pytest.raises(ValueError):
        RequestLifecycleRegistry(capacity=capacity)


def test_registry_accepts_zero_ttl_and_positive_integer_capacity():
    registry = RequestLifecycleRegistry(ttl_seconds=0, capacity=1)
    reservation = _reserve(registry)
    registry.accept(reservation.registration_token)
    registry.mark_terminal(reservation.registration_token, "completed")

    assert registry.get_status("request-1") == "unknown"


@pytest.mark.parametrize(
    "override",
    [
        {"request_id": "x" * 129},
        {"request_id": "request id with spaces"},
        {"request_kind": "x" * 33},
        {"request_kind": "Chat"},
        {"context_identity": "x" * 257},
        {"context_identity": "context\nidentity"},
        {"payload_fingerprint": "f" * 63},
        {"payload_fingerprint": "g" * 64},
        {"payload_fingerprint": "F" * 64},
    ],
)
def test_reserve_rejects_unbounded_or_noncanonical_metadata(override):
    registry = RequestLifecycleRegistry()

    with pytest.raises(RequestLifecycleValidationError) as exc_info:
        _reserve(registry, **override)

    assert exc_info.value.status_code == 422


@pytest.mark.parametrize(
    "context_identity",
    [
        "context\u0000identity",
        "context\u200bidentity",
        "context\u202eidentity",
        "context\ue000identity",
        "context\ud800identity",
    ],
)
def test_context_identity_rejects_unicode_control_format_and_other_categories(
    context_identity,
):
    registry = RequestLifecycleRegistry()

    with pytest.raises(RequestLifecycleValidationError) as exc_info:
        _reserve(registry, context_identity=context_identity)

    assert exc_info.value.status_code == 422


def test_high_entropy_request_id_shapes_are_accepted():
    registry = RequestLifecycleRegistry(capacity=3)

    for request_id in (
        "018f6f44-75f4-7def-b746-01a31bc3c001",
        "01J5YQXW9P1Q0JH8VQ6J1RZZZZ",
        "Wm9pRXA4QW8xNEZQdF9Cd2c4TQ",
    ):
        assert _reserve(registry, request_id).should_start is True


@pytest.mark.parametrize("outcome", ["", "Completed", "x" * 33, "bad outcome"])
def test_terminal_outcome_must_be_a_small_canonical_code(outcome):
    registry = RequestLifecycleRegistry()
    reservation = _reserve(registry)
    registry.accept(reservation.registration_token)
    registry.mark_running(reservation.registration_token)

    with pytest.raises(RequestLifecycleValidationError) as exc_info:
        registry.mark_terminal(reservation.registration_token, outcome)

    assert exc_info.value.status_code == 422
    assert registry.get_status("request-1") == "running"
