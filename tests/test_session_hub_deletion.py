import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

from backend.agent.session_hub import AgentSessionHub
from backend.api.chat_session_store import ChatSessionStore
from backend.api.session_hub import router
from backend.session_hub_runtime import SessionHubRuntime


def _app(runtime: SessionHubRuntime, store: ChatSessionStore) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.session_hub_runtime = runtime
    app.state.chat_session_store = store
    app.state.agent_runner = None
    return app


async def _delete(app: FastAPI, session_id: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.delete(f"/v1/session/{session_id}")


def _delete_config(timeout_seconds: float) -> SimpleNamespace:
    return SimpleNamespace(
        agent=SimpleNamespace(session_disposal_timeout_seconds=timeout_seconds),
        sglang=SimpleNamespace(host="127.0.0.1", port=8000),
    )


async def _inline_to_thread(function, /, *args, **kwargs):
    return function(*args, **kwargs)


@pytest.mark.anyio
async def test_delete_terminal_chat_releases_exact_journal_and_assets():
    runtime = SessionHubRuntime()
    store = ChatSessionStore()
    record = store.get_or_create("terminal-chat", runtime=runtime)
    record.hub.append({"type": "done", "status": "completed"})
    staged = record.stage_display_assets(
        turn_index=1,
        images=["data:image/jpeg;base64,retained"],
        evict_prior=False,
    )
    record.commit_display_assets(staged)

    response = await _delete(_app(runtime, store), record.session_id)

    assert response.status_code == 200
    assert response.json() == {
        "status": "deleted",
        "session_id": record.session_id,
        "kind": "chat",
    }
    assert store.get(record.session_id) is None
    assert runtime.resolve(record.session_id) is None
    assert record.hub.released is True
    assert record.hub.journal_size == 0
    assert record.resolve_payload(
        {
            "type": "timeline_event",
            "event": {"kind": "turn_started", "image_asset_ids": staged.asset_ids},
        }
    )["event"]["images"] == []


@pytest.mark.anyio
async def test_delete_active_chat_targets_exact_request_and_waits_for_cleanup():
    runtime = SessionHubRuntime()
    store = ChatSessionStore()
    record = store.get_or_create("active-chat", runtime=runtime)
    token = record.hub.claim_producer()
    assert token is not None
    turn_index = record.begin_turn(request_id="request-active-chat")
    finish_worker = threading.Event()

    async def producer():
        while not finish_worker.is_set():
            await asyncio.sleep(0)
        record.hub.close("aborted", producer_token=token)
        record.hub.release_producer(token)
        store.mark_finished_if_same(
            record,
            "aborted",
            request_id="request-active-chat",
            turn_index=turn_index,
        )

    record.produce_task = asyncio.create_task(producer())
    app = _app(runtime, store)

    def thread_safe_targeted_abort(*_args, **_kwargs):
        finish_worker.set()

    with (
        patch(
            "backend.llm_service.abort_request_by_id",
            side_effect=thread_safe_targeted_abort,
        ) as targeted,
        patch("backend.llm_service.abort_sglang_all") as abort_all,
    ):
        response = await asyncio.wait_for(_delete(app, record.session_id), timeout=2)

    assert response.status_code == 200
    targeted.assert_called_once()
    assert targeted.call_args.args[0] == "request-active-chat"
    assert targeted.call_args.kwargs["fallback_to_abort_all"] is False
    abort_all.assert_not_called()
    assert record.produce_task is None
    assert record.hub.released is True
    assert runtime.resolve(record.session_id) is None


@pytest.mark.anyio
async def test_delete_active_chat_timeout_is_stable_and_retains_exact_entry():
    runtime = SessionHubRuntime()
    store = ChatSessionStore()
    record = store.get_or_create("timeout-chat", runtime=runtime)
    token = record.hub.claim_producer()
    assert token is not None
    record.begin_turn(request_id="request-timeout-chat")
    never = asyncio.Event()
    record.produce_task = asyncio.create_task(never.wait())
    app = _app(runtime, store)

    with (
        patch("backend.llm_service.abort_request_by_id"),
        patch(
            "backend.api.session_hub.get_config",
            create=True,
            return_value=SimpleNamespace(
                agent=SimpleNamespace(session_disposal_timeout_seconds=0.01),
                sglang=SimpleNamespace(host="127.0.0.1", port=8000),
            ),
        ),
    ):
        response = await _delete(app, record.session_id)

    record.produce_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await record.produce_task

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "session_disposal_timeout"
    assert runtime.resolve(record.session_id) is record.entry
    assert record.hub.released is False


@pytest.mark.anyio
async def test_delete_agent_delegates_pinned_identity_and_ensures_runtime_release():
    runtime = SessionHubRuntime()
    hub = AgentSessionHub()
    entry = runtime.register(session_id="agent-delete", kind="agent", hub=hub)
    session = SimpleNamespace(
        session_id="agent-delete",
        event_hub=hub,
        session_hub_entry=entry,
        is_child_context=False,
    )
    runner = MagicMock()
    runner.sessions.get.return_value = session
    runner.dispose_session_exact = AsyncMock(
        return_value={"status": "disposed", "session_id": session.session_id}
    )
    app = _app(runtime, ChatSessionStore())
    app.state.agent_runner = runner

    response = await _delete(app, session.session_id)

    assert response.status_code == 200
    assert response.json() == {
        "status": "deleted",
        "session_id": session.session_id,
        "kind": "agent",
    }
    runner.dispose_session_exact.assert_awaited_once_with(
        session.session_id,
        expected_session=session,
    )
    assert runtime.resolve(session.session_id) is None
    assert hub.released is True


@pytest.mark.anyio
async def test_stale_chat_delete_completion_never_releases_same_string_replacement():
    runtime = SessionHubRuntime()
    store = ChatSessionStore()
    old = store.get_or_create("reused-chat", runtime=runtime)
    old_token = old.hub.claim_producer()
    assert old_token is not None
    old_turn = old.begin_turn(request_id="old-request")
    finish_old = asyncio.Event()
    abort_called = threading.Event()

    async def old_producer():
        await finish_old.wait()
        old.hub.release_producer(old_token)
        store.mark_finished_if_same(
            old,
            "aborted",
            request_id="old-request",
            turn_index=old_turn,
        )

    old.produce_task = asyncio.create_task(old_producer())
    app = _app(runtime, store)
    loop = asyncio.get_running_loop()

    def targeted_abort(*_args, **_kwargs):
        abort_called.set()

    with patch(
        "backend.llm_service.abort_request_by_id",
        side_effect=targeted_abort,
    ):
        deletion = asyncio.create_task(_delete(app, old.session_id))
        early, _ = await asyncio.wait({deletion}, timeout=0.05)
        if early:
            response = await deletion
            finish_old.set()
            await old.produce_task
            assert response.status_code == 200
        for _ in range(100):
            if abort_called.is_set():
                break
            await asyncio.sleep(0.01)
        assert abort_called.is_set()
        assert store.pop(old.session_id, force=True) is old
        replacement = store.get_or_create(old.session_id, runtime=runtime)
        replacement.hub.append({"type": "replacement", "content": "keep"})
        finish_old.set()
        response = await deletion

    assert response.status_code == 200
    assert store.get(replacement.session_id) is replacement
    assert runtime.resolve(replacement.session_id) is replacement.entry
    assert replacement.hub.released is False
    assert replacement.hub.journal_size == 1


@pytest.mark.anyio
async def test_delete_timeout_budget_includes_targeted_abort_and_retains_entry():
    runtime = SessionHubRuntime()
    store = ChatSessionStore()
    record = store.get_or_create("abort-budget-chat", runtime=runtime)
    token = record.hub.claim_producer()
    assert token is not None
    record.begin_turn(request_id="abort-budget-request")
    never = asyncio.Event()
    producer = asyncio.create_task(never.wait())
    record.produce_task = producer

    async def slow_abort(*_args, **_kwargs):
        await asyncio.sleep(0.05)

    with (
        patch("backend.api.session_hub.get_config", return_value=_delete_config(0.01)),
        patch(
            "backend.api.session_hub._targeted_abort_chat_request",
            new=AsyncMock(side_effect=slow_abort),
            create=True,
        ) as abort_request,
        patch("backend.llm_service.abort_request_by_id"),
        ):
            response = await _delete(_app(runtime, store), record.session_id)

    producer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await producer
    record.hub.release_producer(token)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "session_disposal_timeout"
    abort_request.assert_awaited_once_with(record.session_id, "abort-budget-request")
    assert runtime.resolve(record.session_id) is record.entry
    assert record.hub.released is False


@pytest.mark.anyio
async def test_delete_waits_exact_current_generation_not_stale_task():
    runtime = SessionHubRuntime()
    store = ChatSessionStore()
    record = store.get_or_create("second-turn-gap", runtime=runtime)
    stale_task = asyncio.create_task(asyncio.sleep(0))
    await stale_task
    record.produce_task = stale_task
    token, turn_index = record.claim_turn(request_id="second-turn-request")
    current_done = asyncio.Event()

    async def current_producer():
        await current_done.wait()
        record.hub.close("aborted", producer_token=token)
        record.hub.release_producer(token)
        store.mark_finished_if_same(
            record,
            "aborted",
            request_id="second-turn-request",
            turn_index=turn_index,
        )

    current_task = asyncio.create_task(current_producer())
    assert record.attach_producer_task(
        request_id="second-turn-request",
        turn_index=turn_index,
        task=current_task,
    ) is True
    abort_entered = asyncio.Event()

    async def targeted_abort(*_args, **_kwargs):
        abort_entered.set()

    with (
        patch("backend.api.session_hub.get_config", return_value=_delete_config(1)),
        patch(
            "backend.api.session_hub._targeted_abort_chat_request",
            new=AsyncMock(side_effect=targeted_abort),
            create=True,
        ),
    ):
        deletion = asyncio.create_task(_delete(_app(runtime, store), record.session_id))
        await asyncio.wait_for(abort_entered.wait(), timeout=1)
        await asyncio.sleep(0)
        assert deletion.done() is False
        current_done.set()
        response = await deletion

    assert response.status_code == 200
    assert runtime.resolve(record.session_id) is None
    assert record.hub.released is True


@pytest.mark.anyio
@pytest.mark.parametrize("producer_outcome", ["raised", "cancelled"])
async def test_delete_reconciles_failed_producer_after_exact_cleanup_completed(
    producer_outcome,
):
    runtime = SessionHubRuntime()
    store = ChatSessionStore()
    record = store.get_or_create(f"cleanup-{producer_outcome}", runtime=runtime)
    token, turn_index = record.claim_turn(request_id=f"request-{producer_outcome}")
    run = asyncio.Event()

    async def producer():
        try:
            await run.wait()
            if producer_outcome == "raised":
                raise RuntimeError("producer failed after cleanup")
            raise asyncio.CancelledError
        finally:
            record.hub.close("error", producer_token=token)
            record.hub.release_producer(token)
            store.mark_finished_if_same(
                record,
                "error",
                request_id=f"request-{producer_outcome}",
                turn_index=turn_index,
            )

    producer_task = asyncio.create_task(producer())
    record.produce_task = producer_task

    async def targeted_abort(*_args, **_kwargs):
        run.set()

    with (
        patch("backend.api.session_hub.get_config", return_value=_delete_config(1)),
        patch(
            "backend.api.session_hub._targeted_abort_chat_request",
            new=AsyncMock(side_effect=targeted_abort),
            create=True,
        ),
    ):
        response = await _delete(_app(runtime, store), record.session_id)

    with pytest.raises((RuntimeError, asyncio.CancelledError)):
        await producer_task
    assert response.status_code == 200
    assert runtime.resolve(record.session_id) is None
    assert record.hub.released is True


@pytest.mark.anyio
async def test_delete_producer_failure_without_cleanup_is_structured_and_retained():
    runtime = SessionHubRuntime()
    store = ChatSessionStore()
    record = store.get_or_create("failed-cleanup", runtime=runtime)
    token, turn_index = record.claim_turn(request_id="failed-cleanup-request")

    async def producer():
        raise RuntimeError("producer escaped before cleanup")

    producer_task = asyncio.create_task(producer())
    record.produce_task = producer_task
    assert record.attach_producer_task(
        request_id="failed-cleanup-request",
        turn_index=turn_index,
        task=producer_task,
    ) is True

    with (
        patch("backend.api.session_hub.get_config", return_value=_delete_config(1)),
        patch(
            "backend.api.session_hub._targeted_abort_chat_request",
            new=AsyncMock(),
            create=True,
        ),
    ):
        response = await _delete(_app(runtime, store), record.session_id)

    with pytest.raises(RuntimeError, match="escaped before cleanup"):
        await producer_task
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "session_disposal_failed"
    assert runtime.resolve(record.session_id) is record.entry
    assert record.hub.released is False
    assert record.hub.capture_active_producer() is token


@pytest.mark.anyio
async def test_delete_caller_cancellation_is_not_swallowed():
    runtime = SessionHubRuntime()
    store = ChatSessionStore()
    record = store.get_or_create("cancel-delete", runtime=runtime)
    token, turn_index = record.claim_turn(request_id="cancel-delete-request")
    producer = asyncio.create_task(asyncio.Event().wait())
    assert record.attach_producer_task(
        request_id="cancel-delete-request", turn_index=turn_index, task=producer
    ) is True

    with (
        patch("backend.api.session_hub.get_config", return_value=_delete_config(1)),
        patch(
            "backend.api.session_hub._targeted_abort_chat_request", new=AsyncMock()
        ),
    ):
        deletion = asyncio.create_task(_delete(_app(runtime, store), record.session_id))
        await asyncio.sleep(0)
        deletion.cancel()
        with pytest.raises(asyncio.CancelledError):
            await deletion

    producer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await producer
    record.hub.release_producer(token)
