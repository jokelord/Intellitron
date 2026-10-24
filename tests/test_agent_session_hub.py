import asyncio
import json
import threading

import pytest

from backend.agent.session_hub import AgentSessionHub


class _IndexOnlyJournal(list):
    """A test journal that fails any prefix iteration or slice allocation."""

    def __iter__(self):
        raise AssertionError("subscriber scanned the retained journal prefix")

    def __getitem__(self, index):
        if isinstance(index, slice):
            raise AssertionError("subscriber copied a retained journal slice")
        return super().__getitem__(index)


@pytest.mark.anyio
async def test_subscribe_replay_to_live_is_ordered_gap_free_and_single_copy():
    hub = AgentSessionHub()
    first = hub.append({"type": "literal", "value": "one"})
    second = hub.append({"type": "literal", "value": "two"})
    subscriber = hub.subscribe()

    received = [await anext(subscriber), await anext(subscriber)]
    assert received == [first, second]

    third = hub.append({"type": "literal", "value": "three"})
    received.append(await anext(subscriber))

    resumed = hub.subscribe(after_sequence=2)
    resumed_record = await anext(resumed)

    assert [record.sequence for record in received] == [1, 2, 3]
    assert [resumed_record.sequence] == [3]
    assert received[2] is resumed_record
    assert received[2].payload is resumed_record.payload

    await subscriber.aclose()
    await resumed.aclose()


@pytest.mark.anyio
async def test_close_wakes_followers_after_the_terminal_journal():
    hub = AgentSessionHub()
    terminal = hub.append({"type": "done"})
    subscriber = hub.subscribe(after_sequence=terminal.sequence)
    waiting = asyncio.create_task(anext(subscriber))

    for _ in range(100):
        if hub.subscriber_count == 1:
            break
        await asyncio.sleep(0)
    assert hub.subscriber_count == 1

    assert hub.close("completed") is True
    with pytest.raises(StopAsyncIteration):
        await waiting
    assert hub.close_reason == "completed"


def test_producer_claim_is_single_owner_and_stale_release_is_rejected():
    hub = AgentSessionHub()

    first_token = hub.claim_producer()
    assert first_token is not None
    assert hub.claim_producer() is None
    assert hub.release_producer(first_token) is True

    second_token = hub.claim_producer()
    assert second_token is not None
    assert second_token is not first_token
    assert hub.release_producer(first_token) is False
    assert hub.has_active_producer is True
    assert hub.release_producer(second_token) is True
    assert hub.has_active_producer is False


def test_payload_fields_query_returns_exact_locked_subset_under_concurrent_release():
    hub = AgentSessionHub()
    first = hub.append({"type": "assignment", "child_id": "child-a", "seq": 1})
    second = hub.append({"type": "assignment", "child_id": "child-a", "seq": 2})
    hub.append({"type": "assignment", "child_id": "child-b", "seq": 3})
    query_started = threading.Event()
    release_started = threading.Event()
    query_results = []

    def query() -> None:
        query_started.set()
        release_started.wait(2)
        query_results.append(
            hub.find_payloads_by_fields(
                {"type": "assignment", "child_id": "child-a"}
            )
        )

    def release() -> None:
        query_started.wait(2)
        release_started.set()
        hub.release()

    query_thread = threading.Thread(target=query)
    release_thread = threading.Thread(target=release)
    query_thread.start()
    release_thread.start()
    query_thread.join(2)
    release_thread.join(2)

    assert query_thread.is_alive() is False
    assert release_thread.is_alive() is False
    assert len(query_results) == 1
    result = query_results[0]
    assert len(result) in {0, 2}
    if result:
        assert result == (first, second)
        assert all(
            record.payload["child_id"] == "child-a" for record in result
        )


def test_append_batch_preflights_every_payload_before_atomic_commit():
    hub = AgentSessionHub()
    token = hub.claim_producer()
    assert token is not None
    cyclic = {"type": "turn_started"}
    cyclic["cycle"] = cyclic

    with pytest.raises(ValueError, match="cycle"):
        hub.prepare_append_batch(
            (
                {"type": "request_accepted"},
                cyclic,
            )
        )

    assert hub.journal_size == 0
    prepared = hub.prepare_append_batch(
        (
            {"type": "request_accepted"},
            {"type": "timeline_event", "event": {"kind": "turn_started"}},
        )
    )
    records = hub.append_batch(prepared, producer_token=token)

    assert [record.sequence for record in records] == [1, 2]
    assert [record.payload["type"] for record in records] == [
        "request_accepted",
        "timeline_event",
    ]


def test_append_batch_rejects_stale_owner_without_partial_commit():
    hub = AgentSessionHub()
    stale_token = hub.claim_producer()
    assert stale_token is not None
    prepared = hub.prepare_append_batch(({"type": "stale"}, {"type": "also-stale"}))
    assert hub.close("completed", producer_token=stale_token) is True
    assert hub.release_producer(stale_token) is True
    replacement_token = hub.claim_producer()
    assert replacement_token is not None

    with pytest.raises(RuntimeError, match="producer token"):
        hub.append_batch(prepared, producer_token=stale_token)

    assert hub.journal_size == 0
    assert hub.capture_active_producer() is replacement_token


@pytest.mark.anyio
async def test_release_clears_records_and_subscribers_and_wakes_waiters():
    hub = AgentSessionHub()
    hub.append({"type": "content", "content": "retained"})
    subscriber = hub.subscribe(after_sequence=hub.last_sequence)
    waiting = asyncio.create_task(anext(subscriber))

    for _ in range(100):
        if hub.subscriber_count == 1:
            break
        await asyncio.sleep(0)
    assert hub.subscriber_count == 1

    assert hub.release() is True
    with pytest.raises(StopAsyncIteration):
        await waiting
    assert hub.journal_size == 0
    assert hub.subscriber_count == 0
    assert hub.release() is False


@pytest.mark.anyio
async def test_live_subscription_uses_direct_incremental_cursor_lookup():
    hub = AgentSessionHub()
    hub._journal = _IndexOnlyJournal()
    subscriber = hub.subscribe()

    for index in range(256):
        record = hub.append({"type": "delta", "index": index})
        assert await anext(subscriber) is record

    await subscriber.aclose()


@pytest.mark.anyio
async def test_payload_is_deeply_detached_immutable_and_json_encodable():
    source = {
        "type": "observation",
        "nested": {"items": [{"text": "canonical"}]},
    }
    hub = AgentSessionHub()
    record = hub.append(source)

    source["nested"]["items"][0]["text"] = "producer mutation"
    source["nested"]["items"].append({"text": "producer append"})

    assert record.payload["nested"]["items"] == [{"text": "canonical"}]
    with pytest.raises(TypeError):
        record.payload["extra"] = "subscriber mutation"
    with pytest.raises(TypeError):
        record.payload["nested"]["items"][0]["text"] = "subscriber mutation"
    with pytest.raises(TypeError):
        record.payload["nested"]["items"].append({"text": "subscriber append"})

    replay = hub.subscribe()
    replayed = await anext(replay)
    assert replayed is record
    assert json.loads(json.dumps(replayed.payload)) == {
        "type": "observation",
        "nested": {"items": [{"text": "canonical"}]},
    }
    await replay.aclose()


@pytest.mark.anyio
async def test_old_generation_subscriber_stops_if_next_claim_precedes_wakeup():
    hub = AgentSessionHub()
    old_token = hub.claim_producer()
    terminal = hub.append({"type": "done"}, producer_token=old_token)
    subscriber = hub.subscribe(after_sequence=terminal.sequence)
    waiting = asyncio.create_task(anext(subscriber))

    for _ in range(100):
        if hub.subscriber_count == 1:
            break
        await asyncio.sleep(0)
    assert hub.subscriber_count == 1

    assert hub.close("completed", producer_token=old_token) is True
    assert hub.release_producer(old_token) is True
    new_token = hub.claim_producer()
    assert new_token is not None
    hub.append({"type": "next_turn"}, producer_token=new_token)

    with pytest.raises(StopAsyncIteration):
        await waiting


def test_stale_producer_cannot_append_or_close_new_generation():
    hub = AgentSessionHub()
    old_token = hub.claim_producer()
    hub.append({"type": "old"}, producer_token=old_token)
    assert hub.close("old_completed", producer_token=old_token) is True
    assert hub.release_producer(old_token) is True
    new_token = hub.claim_producer()

    with pytest.raises(RuntimeError, match="producer token"):
        hub.append({"type": "stale"}, producer_token=old_token)
    with pytest.raises(RuntimeError, match="producer token"):
        hub.append({"type": "tokenless bypass"})
    assert hub.close("stale_close", producer_token=old_token) is False

    current = hub.append({"type": "current"}, producer_token=new_token)
    assert current.sequence == 2
    assert hub.close_reason is None


@pytest.mark.anyio
async def test_exact_producer_handoff_claims_next_generation_without_gap():
    hub = AgentSessionHub()
    old_token = hub.claim_producer()
    assert hub.capture_active_producer() is old_token
    handoff_task = asyncio.create_task(hub.claim_producer_after(old_token))
    await asyncio.sleep(0)

    assert handoff_task.done() is False
    assert hub.claim_producer() is None

    hub.append({"type": "done", "status": "aborted"}, producer_token=old_token)
    hub.close("aborted", producer_token=old_token)
    assert hub.release_producer(old_token) is True
    new_token = await asyncio.wait_for(handoff_task, timeout=1)

    assert new_token is not None
    assert hub.capture_active_producer() is new_token
    assert hub.claim_producer() is None
    hub.close("completed", producer_token=new_token)
    assert hub.release_producer(new_token) is True


@pytest.mark.anyio
async def test_stale_handoff_token_cannot_claim_after_unrelated_generation():
    hub = AgentSessionHub()
    old_token = hub.claim_producer()
    hub.close("completed", producer_token=old_token)
    assert hub.release_producer(old_token) is True
    unrelated_token = hub.claim_producer()
    hub.close("completed", producer_token=unrelated_token)
    assert hub.release_producer(unrelated_token) is True

    assert await hub.claim_producer_after(old_token) is None
    assert hub.has_active_producer is False


@pytest.mark.anyio
async def test_release_between_replay_deliveries_drops_unsent_records():
    hub = AgentSessionHub()
    first = hub.append({"type": "content", "value": "first"})
    hub.append({"type": "content", "value": "must not leak"})
    subscriber = hub.subscribe()

    assert await anext(subscriber) is first
    assert hub.release() is True

    with pytest.raises(StopAsyncIteration):
        await anext(subscriber)


@pytest.mark.anyio
async def test_failed_subscriber_loop_wakeup_detaches_registration():
    hub = AgentSessionHub()
    subscriber = hub.subscribe()
    waiting = asyncio.create_task(anext(subscriber))

    for _ in range(100):
        if hub.subscriber_count == 1:
            break
        await asyncio.sleep(0)
    assert hub.subscriber_count == 1

    class _ClosedLoop:
        def call_soon_threadsafe(self, *_args):
            raise RuntimeError("loop closed")

    with hub._lock:
        registration = next(iter(hub._subscribers.values()))
        registration.loop = _ClosedLoop()

    hub.append({"type": "content", "content": "wake"})
    assert hub.subscriber_count == 0

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
