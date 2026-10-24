import asyncio
import json

import pytest
from fastapi import FastAPI, HTTPException, Request

from backend.agent.session_hub import AgentSessionHub
from backend.request_lifecycle import RequestLifecycleRegistry
from backend.session_hub_runtime import (
    SessionHubConflict,
    SessionHubRuntime,
    SessionProducerPublisher,
    encode_session_hub_record,
    subscribe_session_hub,
)


class _LifecycleRegistry:
    def __init__(self, hub: AgentSessionHub) -> None:
        self.hub = hub
        self.calls: list[tuple[str, object]] = []

    def accept(self, token: object) -> None:
        self.calls.append(("accept", token))

    def mark_running(self, token: object) -> None:
        self.calls.append(("mark_running", token))

    def mark_terminal(self, token: object, outcome: str) -> None:
        assert self.hub._journal[-1].payload["type"] == "done"
        assert self.hub.close_reason == outcome
        self.calls.append(("mark_terminal", (token, outcome)))


class _FaultLifecycleRegistry(_LifecycleRegistry):
    def __init__(
        self,
        hub: AgentSessionHub,
        *,
        fail_running_once: bool = False,
        fail_terminal_once: bool = False,
    ) -> None:
        super().__init__(hub)
        self.fail_running_once = fail_running_once
        self.fail_terminal_once = fail_terminal_once

    def mark_running(self, token: object) -> None:
        if self.fail_running_once:
            self.fail_running_once = False
            raise RuntimeError("injected mark_running failure")
        super().mark_running(token)

    def mark_terminal(self, token: object, outcome: str) -> None:
        if self.fail_terminal_once:
            self.fail_terminal_once = False
            raise RuntimeError("injected mark_terminal failure")
        super().mark_terminal(token, outcome)


class _FaultHub(AgentSessionHub):
    def __init__(self, *, fail_type_once: str | None = None) -> None:
        super().__init__()
        self.fail_type_once = fail_type_once
        self.fail_accept_batch_once = False
        self.fail_close_once = False

    def append(self, payload, *, producer_token=None):
        if self.fail_type_once == payload.get("type"):
            self.fail_type_once = None
            raise RuntimeError(f"injected {payload.get('type')} append failure")
        return super().append(payload, producer_token=producer_token)

    def append_batch(self, prepared, *, producer_token=None):
        payload_types = [payload.get("type") for payload in prepared.payloads]
        if self.fail_accept_batch_once and "request_accepted" in payload_types:
            self.fail_accept_batch_once = False
            raise RuntimeError("injected acceptance batch failure")
        if self.fail_type_once in payload_types:
            failed_type = self.fail_type_once
            self.fail_type_once = None
            raise RuntimeError(f"injected {failed_type} append failure")
        return super().append_batch(prepared, producer_token=producer_token)

    def close(self, reason, *, producer_token=None):
        if self.fail_close_once:
            self.fail_close_once = False
            return False
        return super().close(reason, producer_token=producer_token)


class _UnstringifiableError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("injected error stringification failure")


class _ReleaseDuringAcceptanceHub(AgentSessionHub):
    def append_batch(self, prepared, *, producer_token=None):
        if any(
            payload.get("type") == "request_accepted"
            for payload in prepared.payloads
        ):
            self.release()
        return super().append_batch(prepared, producer_token=producer_token)


class _ReplaceProducerDuringAcceptanceHub(AgentSessionHub):
    replacement_token: object | None = None

    def append_batch(self, prepared, *, producer_token=None):
        if (
            self.replacement_token is None
            and any(
                payload.get("type") == "request_accepted"
                for payload in prepared.payloads
            )
        ):
            self.close("superseded", producer_token=producer_token)
            assert self.release_producer(producer_token) is True
            self.replacement_token = self.claim_producer()
            raise RuntimeError("producer token is not current")
        return super().append_batch(prepared, producer_token=producer_token)


def _publisher(
    *,
    session_id: str = "session-a",
    hub: AgentSessionHub | None = None,
    lifecycle: _LifecycleRegistry | None = None,
) -> tuple[
    SessionHubRuntime,
    object,
    AgentSessionHub,
    object,
    object,
    _LifecycleRegistry,
    SessionProducerPublisher,
]:
    runtime = SessionHubRuntime()
    hub = hub or AgentSessionHub()
    entry = runtime.register(session_id=session_id, kind="chat", hub=hub)
    producer_token = hub.claim_producer()
    assert producer_token is not None
    reservation_token = object()
    lifecycle = lifecycle or _LifecycleRegistry(hub)
    publisher = SessionProducerPublisher(
        entry=entry,
        request_id="request-123",
        producer_token=producer_token,
        request_lifecycle_registry=lifecycle,
        reservation_token=reservation_token,
    )
    return (
        runtime,
        entry,
        hub,
        producer_token,
        reservation_token,
        lifecycle,
        publisher,
    )


def _turn_started() -> dict:
    return {
        "type": "timeline_event",
        "event": {
            "kind": "turn_started",
            "session_id": "session-a",
            "turn_index": 1,
            "mode": "chat",
        },
    }


def _real_registry_publisher(hub: AgentSessionHub):
    runtime = SessionHubRuntime()
    entry = runtime.register(session_id="session-real", kind="chat", hub=hub)
    producer_token = hub.claim_producer()
    assert producer_token is not None
    registry = RequestLifecycleRegistry()
    reservation = registry.reserve(
        request_id="request-real-123",
        request_kind="chat",
        context_identity="session-real",
        payload_fingerprint="0" * 64,
    )
    assert reservation.registration_token is not None
    publisher = SessionProducerPublisher(
        entry=entry,
        request_id=reservation.request_id,
        producer_token=producer_token,
        request_lifecycle_registry=registry,
        reservation_token=reservation.registration_token,
    )
    turn_started = _turn_started()
    turn_started["event"]["session_id"] = "session-real"
    return runtime, entry, producer_token, registry, publisher, turn_started


def test_directory_rejects_conflicts_and_releases_only_the_exact_entry():
    runtime = SessionHubRuntime()
    released_resources: list[str] = []
    old_hub = AgentSessionHub()
    old_entry = runtime.register(
        session_id="session-a",
        kind="chat",
        hub=old_hub,
        release_resources=lambda: released_resources.append("old"),
    )

    assert runtime.resolve("session-a") is old_entry
    with pytest.raises(SessionHubConflict):
        runtime.register(
            session_id="session-a",
            kind="agent",
            hub=AgentSessionHub(),
        )

    assert runtime.release_if_same(old_entry) is True
    assert runtime.resolve("session-a") is None
    assert old_hub.released is True
    assert released_resources == ["old"]

    replacement_hub = AgentSessionHub()
    replacement = runtime.register(
        session_id="session-a",
        kind="agent",
        hub=replacement_hub,
    )

    assert runtime.release_if_same(old_entry) is False
    assert runtime.resolve("session-a") is replacement
    assert replacement_hub.released is False
    assert released_resources == ["old"]


def test_release_callback_failure_still_releases_and_unregisters_exactly_once():
    runtime = SessionHubRuntime()
    hub = AgentSessionHub()
    calls: list[str] = []

    def fail_release() -> None:
        calls.append("called")
        raise RuntimeError("injected resource release failure")

    entry = runtime.register(
        session_id="session-a",
        kind="chat",
        hub=hub,
        release_resources=fail_release,
    )

    with pytest.raises(RuntimeError, match="resource release"):
        runtime.release_if_same(entry)

    assert runtime.resolve("session-a") is None
    assert hub.released is True
    assert runtime.release_if_same(entry) is False
    assert calls == ["called"]

@pytest.mark.parametrize("session_id", ["", "   ", None])
def test_directory_rejects_empty_session_ids(session_id):
    runtime = SessionHubRuntime()

    with pytest.raises(ValueError, match="session_id"):
        runtime.register(session_id=session_id, kind="chat", hub=AgentSessionHub())


@pytest.mark.asyncio
async def test_canonical_subscription_replays_sequences_and_resumes_from_cursor():
    runtime = SessionHubRuntime()
    hub = AgentSessionHub()
    entry = runtime.register(session_id="session-a", kind="chat", hub=hub)
    token = hub.claim_producer()
    assert token is not None
    hub.append(
        {
            "type": "request_accepted",
            "request_id": "request-123",
            "session_id": "session-a",
            "status": "accepted",
        },
        producer_token=token,
    )
    hub.append(_turn_started(), producer_token=token)
    hub.append(
        {
            "type": "done",
            "request_id": "request-123",
            "session_id": "session-a",
            "status": "completed",
        },
        producer_token=token,
    )
    assert hub.close("completed", producer_token=token) is True
    assert hub.release_producer(token) is True

    replay = [record async for record in subscribe_session_hub(entry, 0)]
    resumed = [record async for record in subscribe_session_hub(entry, 1)]

    assert [json.loads(record[6:])["event_sequence"] for record in replay] == [1, 2, 3]
    assert [json.loads(record[6:])["event_sequence"] for record in resumed] == [2, 3]


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [-1, True, "1"])
async def test_canonical_subscription_rejects_invalid_cursors(cursor):
    runtime = SessionHubRuntime()
    entry = runtime.register(
        session_id="session-a",
        kind="chat",
        hub=AgentSessionHub(),
    )

    with pytest.raises(ValueError, match="after_sequence"):
        await anext(subscribe_session_hub(entry, cursor))


@pytest.mark.asyncio
async def test_canonical_subscription_closes_inner_subscriber_on_resolver_failure():
    hub = AgentSessionHub()

    def fail_resolver(_payload):
        raise RuntimeError("injected resolver failure")

    entry = SessionHubRuntime().register(
        session_id="session-a",
        kind="chat",
        hub=hub,
        resolve_payload=fail_resolver,
    )
    hub.append({"type": "timeline_event"})
    subscriber = subscribe_session_hub(entry, 0)

    with pytest.raises(RuntimeError, match="resolver failure"):
        await anext(subscriber)

    assert hub.subscriber_count == 0


@pytest.mark.asyncio
async def test_canonical_subscription_closes_inner_subscriber_when_consumer_closes():
    hub = AgentSessionHub()
    entry = SessionHubRuntime().register(
        session_id="session-a",
        kind="chat",
        hub=hub,
    )
    hub.append({"type": "timeline_event"})
    subscriber = subscribe_session_hub(entry, 0)

    await anext(subscriber)
    assert hub.subscriber_count == 1
    await subscriber.aclose()

    assert hub.subscriber_count == 0


@pytest.mark.asyncio
async def test_canonical_subscription_heartbeats_during_silence_without_journal_mutation():
    hub = AgentSessionHub()
    producer_token = hub.claim_producer()
    assert producer_token is not None
    entry = SessionHubRuntime().register(
        session_id="session-a",
        kind="agent",
        hub=hub,
    )
    subscriber = subscribe_session_hub(entry, 0, heartbeat_seconds=0.01)

    assert await anext(subscriber) == ": ping\n\n"
    assert await anext(subscriber) == ": ping\n\n"
    assert hub.journal_size == 0
    assert hub.last_sequence == 0
    assert hub.capture_active_producer() is producer_token
    assert hub.subscriber_count == 1

    hub.append({"type": "timeline_event", "event": {"kind": "message_delta"}}, producer_token=producer_token)
    record = json.loads((await anext(subscriber))[6:])
    assert record["event_sequence"] == 1
    assert record["type"] == "timeline_event"

    hub.append({"type": "done"}, producer_token=producer_token)
    assert hub.close("completed", producer_token=producer_token) is True
    terminal = json.loads((await anext(subscriber))[6:])
    assert terminal == {"type": "done", "event_sequence": 2}
    with pytest.raises(StopAsyncIteration):
        await anext(subscriber)

    assert hub.journal_size == 2
    assert hub.last_sequence == 2
    assert hub.subscriber_count == 0
    assert hub.release_producer(producer_token) is True


@pytest.mark.asyncio
async def test_canonical_subscription_aclose_cancels_pending_heartbeat_read():
    hub = AgentSessionHub()
    entry = SessionHubRuntime().register(
        session_id="session-a",
        kind="agent",
        hub=hub,
    )
    subscriber = subscribe_session_hub(entry, 0, heartbeat_seconds=0.01)

    assert await anext(subscriber) == ": ping\n\n"
    assert hub.subscriber_count == 1
    await subscriber.aclose()

    assert hub.subscriber_count == 0
    assert hub.journal_size == 0
    assert hub.last_sequence == 0


@pytest.mark.asyncio
async def test_canonical_subscription_zero_heartbeat_waits_for_real_record():
    hub = AgentSessionHub()
    entry = SessionHubRuntime().register(
        session_id="session-a",
        kind="chat",
        hub=hub,
    )
    subscriber = subscribe_session_hub(entry, 0, heartbeat_seconds=0)
    pending = asyncio.create_task(anext(subscriber))

    await asyncio.sleep(0.02)
    assert pending.done() is False
    hub.append({"type": "timeline_event"})
    record = json.loads((await pending)[6:])
    assert record["event_sequence"] == 1

    await subscriber.aclose()
    assert hub.subscriber_count == 0


def test_encoder_resolves_a_copy_and_preserves_the_frozen_journal_payload():
    runtime = SessionHubRuntime()
    hub = AgentSessionHub()

    def resolve_payload(payload):
        payload["asset"] = "resolved-image"
        return payload

    entry = runtime.register(
        session_id="session-a",
        kind="chat",
        hub=hub,
        resolve_payload=resolve_payload,
    )
    record = hub.append({"type": "timeline_event", "asset": "asset-1"})

    encoded = encode_session_hub_record(record, entry)

    assert json.loads(encoded[6:]) == {
        "type": "timeline_event",
        "asset": "resolved-image",
        "event_sequence": 1,
    }
    assert encoded.endswith("\n\n")
    assert record.payload["asset"] == "asset-1"


def test_publisher_enforces_acceptance_once_and_terminalizes_once():
    _, _, hub, _, reservation_token, lifecycle, publisher = _publisher()

    with pytest.raises(RuntimeError, match="accepted"):
        publisher.publish(
            {"type": "timeline_event", "event": {"kind": "message_delta"}}
        )

    publisher.accept(_turn_started())
    with pytest.raises(RuntimeError, match="already accepted"):
        publisher.accept(_turn_started())
    publisher.publish(
        {
            "type": "timeline_event",
            "event": {"kind": "message_delta", "delta": "hello"},
        }
    )
    publisher.finish("completed")
    publisher.finish("completed")

    payloads = [record.payload for record in hub._journal]
    assert payloads[0] == {
        "type": "request_accepted",
        "request_id": "request-123",
        "session_id": "session-a",
        "status": "accepted",
    }
    assert payloads[1] == _turn_started()
    assert [payload["type"] for payload in payloads] == [
        "request_accepted",
        "timeline_event",
        "timeline_event",
        "done",
    ]
    assert payloads[-1] == {
        "type": "done",
        "session_id": "session-a",
        "request_id": "request-123",
        "status": "completed",
    }
    assert lifecycle.calls == [
        ("accept", reservation_token),
        ("mark_running", reservation_token),
        ("mark_terminal", (reservation_token, "completed")),
    ]
    assert hub.close_reason == "completed"
    assert hub.has_active_producer is False


def test_publisher_failure_emits_one_error_then_one_done():
    _, _, hub, _, _, lifecycle, publisher = _publisher()
    publisher.accept(_turn_started())

    publisher.finish("error", ValueError("producer failed"))
    publisher.finish("error", ValueError("duplicate"))

    payloads = [record.payload for record in hub._journal]
    assert [payload["type"] for payload in payloads] == [
        "request_accepted",
        "timeline_event",
        "error",
        "done",
    ]
    assert payloads[-1]["status"] == "error"
    assert [call[0] for call in lifecycle.calls].count("mark_terminal") == 1


def test_accept_preflights_nested_payload_before_lifecycle_or_journal_mutation():
    _, _, hub, token, _, lifecycle, publisher = _publisher()
    invalid = _turn_started()
    invalid["event"]["cycle"] = invalid

    with pytest.raises(ValueError, match="cycle"):
        publisher.accept(invalid)

    assert lifecycle.calls == []
    assert hub.journal_size == 0
    assert hub.capture_active_producer() is token

    publisher.accept(_turn_started())
    assert [record.payload["type"] for record in hub._journal] == [
        "request_accepted",
        "timeline_event",
    ]


def test_accept_concurrent_hub_release_terminalizes_exact_real_lifecycle_token():
    hub = _ReleaseDuringAcceptanceHub()
    runtime, entry, _, registry, publisher, turn_started = _real_registry_publisher(hub)

    with pytest.raises(RuntimeError, match="released"):
        publisher.accept(turn_started)

    assert registry.get_status("request-real-123") == "terminal"
    assert hub.released is True
    assert hub.journal_size == 0
    assert hub.has_active_producer is False

    assert runtime.release_if_same(entry) is True
    replacement_hub = AgentSessionHub()
    replacement = runtime.register(
        session_id="session-real",
        kind="agent",
        hub=replacement_hub,
    )
    replacement_token = replacement_hub.claim_producer()
    assert replacement_token is not None

    publisher.finish("error", RuntimeError("duplicate finish"))
    with pytest.raises(RuntimeError, match="already accepted"):
        publisher.accept(turn_started)

    assert runtime.resolve("session-real") is replacement
    assert replacement_hub.capture_active_producer() is replacement_token
    assert replacement_hub.journal_size == 0


def test_accept_stale_generation_terminalizes_lifecycle_without_releasing_replacement():
    hub = _ReplaceProducerDuringAcceptanceHub()
    _, _, _, registry, publisher, turn_started = _real_registry_publisher(hub)

    with pytest.raises(RuntimeError, match="producer token"):
        publisher.accept(turn_started)

    assert registry.get_status("request-real-123") == "terminal"
    assert hub.replacement_token is not None
    assert hub.capture_active_producer() is hub.replacement_token
    assert hub.journal_size == 0

    publisher.finish("error", RuntimeError("duplicate finish"))
    with pytest.raises(RuntimeError, match="already accepted"):
        publisher.accept(turn_started)

    assert hub.capture_active_producer() is hub.replacement_token
    assert hub.journal_size == 0


@pytest.mark.parametrize("failure_stage", ["mark_running", "accept_batch"])
def test_accept_failure_after_lifecycle_acceptance_compensates_to_stable_terminal(
    failure_stage,
):
    hub = _FaultHub()
    lifecycle = _FaultLifecycleRegistry(
        hub,
        fail_running_once=failure_stage == "mark_running",
    )
    if failure_stage == "accept_batch":
        hub.fail_accept_batch_once = True
    _, _, _, _, _, _, publisher = _publisher(hub=hub, lifecycle=lifecycle)

    with pytest.raises(RuntimeError, match="injected"):
        publisher.accept(_turn_started())

    assert [record.payload["type"] for record in hub._journal] == [
        "request_accepted",
        "timeline_event",
        "error",
        "done",
    ]
    assert hub.close_reason == "error"
    assert hub.has_active_producer is False
    assert [call[0] for call in lifecycle.calls].count("mark_terminal") == 1


def test_finish_materializes_unstringifiable_error_without_stranding_terminalization():
    _, _, hub, _, _, lifecycle, publisher = _publisher()
    publisher.accept(_turn_started())

    publisher.finish("error", _UnstringifiableError())

    assert [record.payload["type"] for record in hub._journal][-2:] == [
        "error",
        "done",
    ]
    assert hub._journal[-2].payload["error"] == "_UnstringifiableError"
    assert hub.close_reason == "error"
    assert [call[0] for call in lifecycle.calls].count("mark_terminal") == 1


@pytest.mark.parametrize("failure_stage", ["error", "done", "close", "lifecycle"])
def test_finish_failure_is_retryable_without_duplicate_terminal_records(failure_stage):
    hub = _FaultHub(
        fail_type_once=failure_stage if failure_stage in {"error", "done"} else None
    )
    hub.fail_close_once = failure_stage == "close"
    lifecycle = _FaultLifecycleRegistry(
        hub,
        fail_terminal_once=failure_stage == "lifecycle",
    )
    _, _, _, token, _, _, publisher = _publisher(hub=hub, lifecycle=lifecycle)
    publisher.accept(_turn_started())

    with pytest.raises(RuntimeError, match="injected|producer token"):
        publisher.finish("error", ValueError("failure"))

    assert hub.capture_active_producer() is token
    publisher.finish("error", ValueError("failure"))

    terminal_types = [
        record.payload["type"]
        for record in hub._journal
        if record.payload["type"] in {"error", "done"}
    ]
    assert terminal_types == ["error", "done"]
    assert hub.close_reason == "error"
    assert hub.has_active_producer is False
    assert [call[0] for call in lifecycle.calls].count("mark_terminal") == 1


def test_publisher_rejects_lifecycle_records_from_domain_publishers():
    _, _, _, _, _, _, publisher = _publisher()
    publisher.accept(_turn_started())

    for payload_type in ("request_accepted", "turn_started", "error", "done"):
        with pytest.raises(ValueError, match="lifecycle"):
            publisher.publish({"type": payload_type})


def test_stale_publisher_cannot_publish_or_release_a_replacement_producer():
    _, _, hub, old_token, _, lifecycle, publisher = _publisher()
    publisher.accept(_turn_started())
    assert hub.close("superseded", producer_token=old_token) is True
    assert hub.release_producer(old_token) is True
    replacement_token = hub.claim_producer()
    assert replacement_token is not None

    with pytest.raises(RuntimeError, match="producer token"):
        publisher.publish(
            {"type": "timeline_event", "event": {"kind": "message_delta"}}
        )
    with pytest.raises(RuntimeError, match="producer token"):
        publisher.finish("completed")

    assert hub.capture_active_producer() is replacement_token
    assert hub.has_active_producer is True
    assert [call[0] for call in lifecycle.calls].count("mark_terminal") == 0
    assert hub.close("completed", producer_token=replacement_token) is True
    assert hub.release_producer(replacement_token) is True


@pytest.mark.asyncio
async def test_canonical_endpoint_resolves_both_kinds_through_the_shared_runtime():
    from backend.api.session_hub import router, session_hub_events

    runtime = SessionHubRuntime()
    app = FastAPI()
    app.state.session_hub_runtime = runtime
    app.include_router(router)
    assert any(getattr(route, "original_router", None) is router for route in app.routes)
    assert any(
        route.path == "/v1/session/{session_id}/events" for route in router.routes
    )

    for session_id, kind in (("chat-session", "chat"), ("agent-session", "agent")):
        hub = AgentSessionHub()
        runtime.register(session_id=session_id, kind=kind, hub=hub)
        token = hub.claim_producer()
        assert token is not None
        hub.append(
            {"type": "done", "session_id": session_id, "status": "completed"},
            producer_token=token,
        )
        assert hub.close("completed", producer_token=token) is True
        assert hub.release_producer(token) is True

        raw_request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/v1/session/{session_id}/events",
                "query_string": b"after_sequence=0",
                "headers": [],
                "app": app,
            }
        )
        response = await session_hub_events(session_id, raw_request, "0")
        payloads = [
            json.loads(frame[6:])
            async for frame in response.body_iterator
        ]
        assert payloads == [
            {
                "type": "done",
                "session_id": session_id,
                "status": "completed",
                "event_sequence": 1,
            }
        ]

    raw_request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/session/missing/events",
            "query_string": b"after_sequence=0",
            "headers": [],
            "app": app,
        }
    )
    with pytest.raises(HTTPException) as missing:
        await session_hub_events("missing", raw_request, "0")
    with pytest.raises(HTTPException) as invalid:
        await session_hub_events("chat-session", raw_request, "-1")
    assert missing.value.status_code == 404
    assert "unavailable or expired" in missing.value.detail
    assert invalid.value.status_code == 422


def test_module_level_app_installs_the_shared_runtime():
    from backend.main import app

    assert isinstance(app.state.session_hub_runtime, SessionHubRuntime)
