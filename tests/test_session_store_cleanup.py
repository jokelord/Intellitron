import asyncio
import threading
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from PIL import Image
from smolagents.memory import ActionStep, TaskStep
from smolagents.monitoring import Timing
from unittest.mock import AsyncMock, MagicMock, patch

from backend.agent.agent_runner import (
    AgentRunner,
    AgentSession,
    ChildAgentRun,
    SessionBusyError,
    SessionDisposalError,
    SessionStore,
    abort_manager,
    session_request_manager,
)
from backend.config import get_config
from backend.session_hub_runtime import SessionHubRuntime
from backend.security.capabilities import build_session_capability
from backend.security.path_policy import PathPolicyError


def _session_capability(session_id: str):
    """Build the same immutable authority required by production sessions."""
    return build_session_capability(
        get_config(), session_id=session_id, mode="tool_calling"
    )


def test_session_ids_returns_stable_snapshot():
    store = SessionStore()
    first = AgentSession(
        session_id="first",
        agent=None,
        mode="tool_calling",
        capability=_session_capability("first"),
    )
    store.put(first)

    snapshot = store.session_ids()
    store.put(
        AgentSession(
            session_id="second",
            agent=None,
            mode="tool_calling",
            capability=_session_capability("second"),
        )
    )

    assert snapshot == ("first",)


def test_put_if_absent_never_overwrites_existing_exact_session():
    store = SessionStore()
    existing = AgentSession(
        session_id="shared",
        agent=None,
        mode="tool_calling",
        capability=_session_capability("shared"),
    )
    replacement = AgentSession(
        session_id="shared",
        agent=None,
        mode="tool_calling",
        capability=_session_capability("shared"),
    )
    fresh = AgentSession(
        session_id="fresh",
        agent=None,
        mode="tool_calling",
        capability=_session_capability("fresh"),
    )
    store.put(existing)

    assert store.put_if_absent(replacement) is False
    assert store.get("shared") is existing
    assert store.put_if_absent(fresh) is True
    assert store.get("fresh") is fresh


def test_delete_cleans_validated_orphan_session_image_directory(tmp_path):
    base_dir = tmp_path / "agent-images"
    orphan = base_dir / "orphan-session"
    orphan.mkdir(parents=True)
    (orphan / "stale.json").write_text("{}", encoding="utf-8")
    store = SessionStore(session_base_dir=base_dir)

    store.delete("orphan-session")

    assert not orphan.exists()


def test_delete_rejects_invalid_orphan_id_without_touching_outside(tmp_path):
    base_dir = tmp_path / "agent-images"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    store = SessionStore(session_base_dir=base_dir)

    with pytest.raises(PathPolicyError, match="invalid_session_id"):
        store.delete("../outside")

    assert sentinel.read_text(encoding="utf-8") == "keep"


def _session(session_id: str, *, active: bool = False) -> AgentSession:
    agent = SimpleNamespace(memory=SimpleNamespace(steps=[]))
    session = AgentSession(
        session_id=session_id,
        agent=agent,
        mode="tool_calling",
        capability=_session_capability(session_id),
    )
    session.last_activity = datetime.now() - timedelta(minutes=120)
    session.run_in_progress = active
    session._run_depth = 1 if active else 0
    return session


def test_cleanup_expired_skips_active_sessions():
    store = SessionStore()
    expired_idle = _session("expired-idle")
    expired_active = _session("expired-active", active=True)
    store.put(expired_idle)
    store.put(expired_active)

    store.cleanup_expired(ttl_minutes=60)

    assert store.get("expired-idle") is None
    assert store.get("expired-active") is expired_active


def test_cycle_owner_pins_all_separate_contexts_until_run_cleanup():
    store = SessionStore()
    first, second, unrelated = (_session(name) for name in ('cycle-first', 'cycle-second', 'unrelated'))
    for session in (first, second, unrelated):
        store.put(session)
    assert hasattr(store, 'pin_cycle_session')
    store.pin_cycle_session('cycle-run', first.session_id)
    store.pin_cycle_session('cycle-run', second.session_id)
    assert store.cleanup_expired(60) == ('unrelated',)
    assert store.get(first.session_id) is first
    assert store.get(second.session_id) is second
    first.last_activity = second.last_activity = datetime.now() - timedelta(minutes=120)
    store.unpin_cycle_owner('cycle-run')
    assert set(store.cleanup_expired(60)) == {'cycle-first', 'cycle-second'}


def test_cycle_manager_pin_preserves_owned_child_and_retained_run_metadata():
    runner = _runner()
    manager, child = _session('cycle-manager'), _session('cycle-child')
    child.is_child_context = True
    runner.sessions.put(manager)
    runner.sessions.put(child)
    runner.sessions.pin_cycle_session('cycle', manager.session_id)
    runner.child_agent_sessions_by_key[(manager.session_id, 'worker')] = child.session_id
    run = ChildAgentRun(child_run_id='retained-run', manager_session_id=manager.session_id,
        dispatch_id='dispatch', child_name='worker', profile_name='basic_agent', assigned_task='task',
        status='completed', completed_at=datetime.now() - timedelta(minutes=120))
    run.terminal_event.set()
    runner.child_agent_runs[run.child_run_id] = run
    runner._retained_child_replay_ids.add(run.child_run_id)
    expired = runner.cleanup_expired_sessions(60)
    assert expired == ()
    assert set(runner.sessions.session_ids()) == {manager.session_id, child.session_id}
    assert run.child_run_id in runner.child_agent_runs
    runner.sessions.unpin_cycle_owner('cycle')
    assert set(runner.cleanup_expired_sessions(60)) == {manager.session_id, child.session_id}
    assert run.child_run_id not in runner.child_agent_runs


def test_session_store_registers_exact_top_level_and_child_hubs_in_shared_runtime():
    runtime = SessionHubRuntime()
    store = SessionStore(session_hub_runtime=runtime)
    top_level = _session("runtime-top-level")
    child = _session("runtime-child")
    child.is_child_context = True

    assert store.put_if_absent(top_level) is True
    assert store.put_if_absent(child) is True

    top_entry = runtime.resolve(top_level.session_id)
    child_entry = runtime.resolve(child.session_id)
    assert top_entry is top_level.session_hub_entry
    assert child_entry is child.session_hub_entry
    assert top_entry.hub is top_level.event_hub
    assert child_entry.hub is child.event_hub


def test_runtime_delete_releases_only_exact_agent_entry_and_projection_assets():
    runtime = SessionHubRuntime()
    store = SessionStore(session_hub_runtime=runtime)
    old = _session("runtime-replacement")
    old.vision_event_image_assets["old"] = "data:image/png;base64,old"
    assert store.put_if_absent(old) is True
    old_entry = old.session_hub_entry

    assert store.delete_if_same(old.session_id, old) is True
    assert runtime.resolve(old.session_id) is None
    assert old.event_hub.released is True
    assert old.vision_event_image_assets == {}

    replacement = _session("runtime-replacement")
    assert store.put_if_absent(replacement) is True
    replacement_entry = replacement.session_hub_entry
    assert replacement_entry is not old_entry
    assert store.delete_if_same(old.session_id, old) is False
    assert runtime.resolve(old.session_id) is replacement_entry
    assert replacement.event_hub.released is False


def test_runtime_registration_race_does_not_leak_rejected_agent_candidate():
    runtime = SessionHubRuntime()
    winner = _session("runtime-race")
    winner_entry = runtime.register(
        session_id=winner.session_id,
        kind="agent",
        hub=winner.event_hub,
    )
    loser = _session("runtime-race")
    store = SessionStore(session_hub_runtime=runtime)

    assert store.put_if_absent(loser) is False
    assert runtime.resolve(winner.session_id) is winner_entry
    assert loser.session_hub_entry is None
    assert loser.event_hub.released is False


def test_cleanup_expired_skips_session_with_active_hub_producer():
    store = SessionStore()
    producer_active = _session("expired-active-producer")
    producer_token = producer_active.event_hub.claim_producer()
    producer_active.event_hub.append(
        {"type": "content", "content": "still finalizing"},
        producer_token=producer_token,
    )
    store.put(producer_active)

    assert store.cleanup_expired(ttl_minutes=60) == ()

    assert store.get(producer_active.session_id) is producer_active
    assert producer_active.event_hub.released is False
    assert producer_active.event_hub.has_active_producer is True
    producer_active.event_hub.close("completed", producer_token=producer_token)
    producer_active.event_hub.release_producer(producer_token)


def test_cleanup_expired_releases_only_the_expired_session_hub():
    store = SessionStore()
    expired_idle = _session("expired-hub")
    expired_idle.event_hub.append({"type": "content", "content": "old"})
    active = _session("active-hub", active=True)
    active.event_hub.append({"type": "content", "content": "live"})
    store.put(expired_idle)
    store.put(active)

    assert store.cleanup_expired(ttl_minutes=60) == ("expired-hub",)

    assert expired_idle.event_hub.released is True
    assert expired_idle.event_hub.journal_size == 0
    assert active.event_hub.released is False
    assert active.event_hub.journal_size == 1


def test_delete_if_same_does_not_release_a_same_id_replacement_hub():
    store = SessionStore()
    old = _session("reused-hub")
    replacement = _session("reused-hub")
    replacement.event_hub.append({"type": "content", "content": "new"})
    store.put(old)
    store.put(replacement)

    assert store.delete_if_same(old.session_id, old) is False

    assert store.get(replacement.session_id) is replacement
    assert replacement.event_hub.released is False
    assert replacement.event_hub.journal_size == 1


def test_delete_if_same_releases_the_exact_removed_session_hub():
    store = SessionStore()
    session = _session("exact-hub-delete")
    session.event_hub.append({"type": "content", "content": "old"})
    store.put(session)

    assert store.delete_if_same(session.session_id, session) is True

    assert session.event_hub.released is True
    assert session.event_hub.journal_size == 0


def test_finish_run_does_not_release_session_journal():
    session = _session("finished-run")
    session.event_hub.append({"type": "done"})
    scope_id = session.begin_run()

    session.finish_run(scope_id)

    assert session.run_in_progress is False
    assert session.event_hub.released is False
    assert session.event_hub.journal_size == 1


def test_failed_top_level_candidate_claim_releases_only_candidate_hub():
    runner = _runner()
    existing = _resource_session("candidate-collision")
    existing.event_hub.append({"type": "content", "content": "existing"})
    runner.sessions.put(existing)
    candidates = []
    original_put_if_absent = runner.sessions.put_if_absent

    def capture_candidate(candidate):
        candidates.append(candidate)
        return original_put_if_absent(candidate)

    candidate_agent = SimpleNamespace(
        memory=SimpleNamespace(steps=[]),
        state={},
        tools={},
    )
    with (
        patch.object(runner, "_build_profile_agent", return_value=candidate_agent),
        patch.object(
            runner.sessions,
            "put_if_absent",
            side_effect=capture_candidate,
        ),
        pytest.raises(SessionBusyError),
    ):
        runner._create_session(existing.session_id, "tool_calling")

    assert len(candidates) == 1
    assert candidates[0] is not existing
    assert candidates[0].event_hub.released is True
    assert runner.sessions.get(existing.session_id) is existing
    assert existing.event_hub.released is False
    assert existing.event_hub.journal_size == 1


def test_runner_ttl_cleanup_releases_inactive_child_hub_and_metadata_only():
    runner = AgentRunner(MagicMock(), get_config())
    expired = _session("expired-child")
    active = _session("active-child", active=True)
    runner.sessions.put(expired)
    runner.sessions.put(active)
    for session, child_name in ((expired, "expired"), (active, "active")):
        session.event_hub.append({"type": "done", "child_name": child_name})
        run_id = session.session_id
        runner.child_agent_runs[run_id] = ChildAgentRun(
            child_run_id=run_id, manager_session_id="manager", dispatch_id="dispatch",
            child_name=child_name, child_session_identity=session,
            profile_name="basic_agent", assigned_task="Task",
            status="running" if session.run_in_progress else "completed",
        )
        runner.child_agent_sessions_by_key[("manager", child_name)] = session.session_id

    runner.cleanup_expired_sessions(60)

    assert "expired-child" not in runner.child_agent_runs
    assert ("manager", "expired") not in runner.child_agent_sessions_by_key
    assert expired.event_hub.released is True
    assert runner.sessions.get("active-child") is active
    assert "active-child" in runner.child_agent_runs
    assert active.event_hub.released is False


@pytest.mark.anyio
async def test_manager_ttl_cascades_to_newer_exact_child_and_all_indexes():
    runner = _runner()
    manager = _resource_session("ttl-cascade-manager")
    manager.last_activity = datetime.now() - timedelta(minutes=120)
    child = _resource_session("ttl-cascade-child")
    child.is_child_context = True
    child.last_activity = datetime.now()
    child.event_hub.append({"type": "done", "content": "owned terminal"})
    sibling_manager = _resource_session("ttl-sibling-manager")
    sibling_child = _resource_session("ttl-sibling-child")
    sibling_child.is_child_context = True
    for session in (manager, child, sibling_manager, sibling_child):
        runner.sessions.put(session)

    child_run = ChildAgentRun(
        child_run_id=child.session_id,
        manager_session_id=manager.session_id,
        dispatch_id="ttl-owned-dispatch",
        child_name="owned",
        profile_name="basic_agent",
        assigned_task="Release with the expired manager.",
        status="completed",
        completed_at=datetime.now(),
        child_session_identity=child,
    )
    child_run.terminal_event.set()
    sibling_run = ChildAgentRun(
        child_run_id=sibling_child.session_id,
        manager_session_id=sibling_manager.session_id,
        dispatch_id="ttl-sibling-dispatch",
        child_name="sibling",
        profile_name="basic_agent",
        assigned_task="Remain owned by the live manager.",
        status="completed",
        completed_at=datetime.now(),
        child_session_identity=sibling_child,
    )
    sibling_run.terminal_event.set()
    runner.child_agent_runs[child_run.child_run_id] = child_run
    runner.child_agent_runs[sibling_run.child_run_id] = sibling_run
    runner.child_agent_sessions_by_key[(manager.session_id, "owned")] = child.session_id
    runner.child_agent_sessions_by_key[(sibling_manager.session_id, "sibling")] = (
        sibling_child.session_id
    )
    expired = runner.cleanup_expired_sessions(60)

    assert expired == (manager.session_id,)
    assert runner.sessions.get(manager.session_id) is None
    assert runner.sessions.get(child.session_id) is None
    assert manager.event_hub.released is True
    assert child.event_hub.released is True
    assert child.agent.memory.steps == []
    assert child_run.child_run_id not in runner.child_agent_runs
    assert child_run.child_run_id not in runner._retained_child_replay_ids
    assert (manager.session_id, child_run.child_name) not in runner.child_agent_sessions_by_key
    assert runner.sessions.get(sibling_manager.session_id) is sibling_manager
    assert runner.sessions.get(sibling_child.session_id) is sibling_child
    assert sibling_child.event_hub.released is False
    assert sibling_run.child_run_id in runner.child_agent_runs


def test_manager_ttl_skips_cascade_while_owned_child_producer_is_active():
    runner = _runner()
    manager = _resource_session("ttl-active-child-manager")
    manager.last_activity = datetime.now() - timedelta(minutes=120)
    child = _resource_session("ttl-active-child")
    child.is_child_context = True
    child.last_activity = datetime.now()
    runner.sessions.put(manager)
    runner.sessions.put(child)
    token = child.event_hub.claim_producer()
    child_run = ChildAgentRun(
        child_run_id=child.session_id,
        manager_session_id=manager.session_id,
        dispatch_id="ttl-active-dispatch",
        child_name="active",
        profile_name="basic_agent",
        assigned_task="Stay active.",
        status="running",
        child_session_identity=child,
    )
    runner.child_agent_runs[child_run.child_run_id] = child_run
    runner.child_agent_sessions_by_key[(manager.session_id, "active")] = child.session_id

    assert runner.cleanup_expired_sessions(60) == ()

    assert runner.sessions.get(manager.session_id) is manager
    assert runner.sessions.get(child.session_id) is child
    assert manager.event_hub.released is False
    assert child.event_hub.released is False
    assert child_run.child_run_id in runner.child_agent_runs
    child.event_hub.close("completed", producer_token=token)
    child.event_hub.release_producer(token)


def test_manager_ttl_releases_pinned_old_child_without_touching_same_id_replacement():
    runner = _runner()
    manager = _resource_session("ttl-replacement-manager")
    manager.last_activity = datetime.now() - timedelta(minutes=120)
    old_child = _resource_session("ttl-reused-child")
    old_child.is_child_context = True
    old_child.event_hub.append({"type": "done", "content": "old"})
    replacement = _resource_session(old_child.session_id)
    replacement.is_child_context = True
    replacement.agent.memory.steps = [TaskStep(task="replacement history")]
    replacement.event_hub.append({"type": "content", "content": "replacement"})
    runner.sessions.put(manager)
    runner.sessions.put(replacement)
    child_run = ChildAgentRun(
        child_run_id=old_child.session_id,
        manager_session_id=manager.session_id,
        dispatch_id="ttl-replacement-dispatch",
        child_name="old-owned",
        profile_name="basic_agent",
        assigned_task="Release only the old exact context.",
        status="completed",
        completed_at=datetime.now(),
        child_session_identity=old_child,
    )
    child_run.terminal_event.set()
    runner.child_agent_runs[child_run.child_run_id] = child_run
    runner.child_agent_sessions_by_key[(manager.session_id, "old-owned")] = (
        old_child.session_id
    )

    assert runner.cleanup_expired_sessions(60) == (manager.session_id,)

    assert old_child.event_hub.released is True
    assert old_child.agent.memory.steps == []
    assert runner.sessions.get(replacement.session_id) is replacement
    assert replacement.event_hub.released is False
    assert replacement.event_hub.journal_size == 1
    assert replacement.agent.memory.steps[0].task == "replacement history"
    assert replacement.aborted is False
    assert child_run.child_run_id not in runner.child_agent_runs
    assert (manager.session_id, "old-owned") not in runner.child_agent_sessions_by_key


def _runner() -> AgentRunner:
    return AgentRunner(
        MagicMock(),
        deepcopy(get_config()),
        session_hub_runtime=SessionHubRuntime(),
    )


def _tracked_image():
    image = Image.new("RGB", (1, 1))
    image._pre_encoded_b64 = "cached"
    image._pre_encoded_mime = "image/png"
    image.close = MagicMock(wraps=image.close)
    return image


def _resource_session(session_id: str, image=None) -> AgentSession:
    task_images = [image] if image is not None else None
    observation_images = [image] if image is not None else None
    steps = [
        TaskStep(task="inspect", task_images=task_images),
        ActionStep(
            step_number=1,
            timing=Timing(start_time=0.0, end_time=0.1),
            observations_images=observation_images,
        ),
    ]
    agent = SimpleNamespace(
        memory=SimpleNamespace(steps=steps),
        state={"owned": "state"},
    )
    session = AgentSession(
        session_id=session_id,
        agent=agent,
        mode="tool_calling",
        capability=_session_capability(session_id),
    )
    session.last_compactable_llm_messages = [
        {"role": "user", "content": [{"type": "image", "image": image}]}
    ]
    session.last_compactable_agent_name = "basic_agent"
    session.last_llm_prompt_snapshot = object()
    session.compaction_archive = [{"summary": "old"}]
    session.vision_automation_binding = object()
    session.vision_automation_cleanup_token = object()
    session.vision_automation_image_omission_sticky = True
    session.vision_event_image_assets = {"asset-1": "data:image/jpeg;base64,retained"}
    session.vision_event_turn_assets = {1: ("asset-1",)}
    session.behavior_records = {"vision_automation_historical_image_omission": [{}]}
    return session


def test_resource_release_resets_sticky_vision_automation_omission_state():
    session = _resource_session("release-sticky")

    AgentRunner._release_session_resources(session, set())

    assert session.vision_automation_image_omission_sticky is False
    assert session.vision_event_image_assets == {}
    assert session.vision_event_turn_assets == {}


@pytest.mark.anyio
async def test_dispose_idle_session_closes_pil_and_releases_canonical_state():
    runner = _runner()
    image = _tracked_image()
    session = _resource_session("dispose-idle", image)
    memory = session.agent.memory
    task_step, action_step = memory.steps
    runner.sessions.put(session)
    session.event_hub.append({"type": "content", "content": "dispose me"})
    session_request_manager.set(session.session_id, "request-idle")
    abort_manager.abort(session.session_id)

    with patch("backend.llm_service.abort_request_by_id") as targeted_abort:
        result = await runner.dispose_session(session.session_id)
    targeted_abort.assert_called_once_with(
        "request-idle",
        f"http://{runner.config.sglang.host}:{runner.config.sglang.port}",
        fallback_to_abort_all=False,
    )

    assert result == {"status": "disposed", "session_id": session.session_id}
    assert runner.sessions.get(session.session_id) is None
    image.close.assert_called_once_with()
    assert not hasattr(image, "_pre_encoded_b64")
    assert not hasattr(image, "_pre_encoded_mime")
    assert task_step.task_images is None
    assert action_step.observations_images is None
    assert memory.steps == []
    assert session.agent.state == {}
    assert session.last_compactable_llm_messages is None
    assert session.last_compactable_agent_name is None
    assert session.last_llm_prompt_snapshot is None
    assert session.vision_automation_image_omission_sticky is False
    assert session.compaction_archive == []
    assert session.vision_automation_binding is None
    assert session.vision_automation_cleanup_token is None
    assert session.behavior_records == {}
    assert session.event_hub.released is True
    assert session.event_hub.journal_size == 0
    assert session_request_manager.get(session.session_id) is None
    assert abort_manager.is_aborted(session.session_id) is False
    assert await runner.dispose_session(session.session_id) == {
        "status": "already_disposed",
        "session_id": session.session_id,
    }


@pytest.mark.anyio
async def test_active_disposal_targets_request_and_waits_for_all_answer_now_scopes():
    runner = _runner()
    image = _tracked_image()
    session = _resource_session("dispose-active", image)
    runner.sessions.put(session)
    first_scope = session.begin_run()
    answer_scope = session.begin_run()
    assert session.try_begin_answer_now() is True
    session.begin_answer_now()
    session_request_manager.set(session.session_id, "request-active")

    with (
        patch("backend.llm_service.abort_request_by_id") as targeted_abort,
        patch("backend.llm_service.abort_sglang_all") as abort_all,
    ):
        first = asyncio.create_task(runner.dispose_session(session.session_id))
        second = asyncio.create_task(runner.dispose_session(session.session_id))
        for _ in range(100):
            if session.aborted:
                break
            await asyncio.sleep(0.01)

        assert session.aborted is True
        assert first.done() is False
        assert second.done() is False
        assert runner.sessions.get(session.session_id) is session
        image.close.assert_not_called()
        targeted_abort.assert_called_once()
        assert targeted_abort.call_args.args[0] == "request-active"
        assert targeted_abort.call_args.kwargs["fallback_to_abort_all"] is False
        abort_all.assert_not_called()
        with pytest.raises(SessionBusyError):
            runner.get_or_create_session(session.session_id)

        session.finish_run(first_scope)
        session.finish_run(answer_scope)
        await asyncio.sleep(0.05)
        assert first.done() is False
        session.finish_answer_now()
        first_result, second_result = await asyncio.gather(first, second)

    assert first_result == second_result == {
        "status": "disposed",
        "session_id": session.session_id,
    }
    image.close.assert_called_once_with()
    assert runner.sessions.get(session.session_id) is None


@pytest.mark.anyio
async def test_disposal_timeout_is_bounded_and_retains_active_session_resources():
    runner = _runner()
    runner.config.agent.session_disposal_timeout_seconds = 0.05
    image = _tracked_image()
    session = _resource_session("dispose-timeout", image)
    runner.sessions.put(session)
    scope_id = session.begin_run()

    with pytest.raises(SessionDisposalError) as exc_info:
        await runner.dispose_session(session.session_id)

    assert exc_info.value.code == "session_disposal_timeout"
    assert exc_info.value.pending_scope_count == 1
    assert runner.sessions.get(session.session_id) is session
    image.close.assert_not_called()

    session.finish_run(scope_id)
    result = await runner.dispose_session(session.session_id)
    assert result["status"] == "disposed"
    image.close.assert_called_once_with()


@pytest.mark.anyio
async def test_disposal_publication_atomically_rejects_new_answer_now_acquisition():
    runner = _runner()
    session = _resource_session("dispose-wins-answer-now")
    runner.sessions.put(session)
    scope_id = session.begin_run()

    disposal = asyncio.create_task(runner.dispose_session(session.session_id))
    for _ in range(100):
        if session.session_id in runner._session_disposal_tasks:
            break
        await asyncio.sleep(0.01)

    try:
        status, acquired = runner.try_acquire_answer_now(
            session.session_id, expected_session=session
        )
    finally:
        session.finish_run(scope_id)
        await disposal

    assert status == "busy"
    assert acquired is None
    assert session._answer_now_in_progress is False


def test_unstaged_run_admission_cannot_start_after_disposal_completes():
    runner = _runner()
    runner.config.agent.session_ttl_minutes = 1
    session = _resource_session("dispose-race-unstaged-admission")
    replacement = _resource_session(session.session_id)
    runner.sessions.put(session)
    admission_paused = threading.Event()
    allow_admission = threading.Event()
    disposal_done = threading.Event()
    events = []
    errors = []
    original_begin_run = AgentSession.begin_run
    original_cleanup = runner.cleanup_expired_sessions

    def gated_cleanup(ttl_minutes):
        admission_paused.set()
        if not allow_admission.wait(2):
            raise TimeoutError("admission cleanup gate timed out")
        return original_cleanup(ttl_minutes)

    def create_replacement(session_id, resolved_mode):
        runner.sessions.put(replacement)
        return replacement

    def observed_begin_run(candidate):
        scope_id = original_begin_run(candidate)
        events.append("run_started")
        return scope_id

    async def fake_active_agent(*args, **kwargs):
        return {
            "session_id": session.session_id,
            "answer": "not reached after disposal",
            "steps": [],
        }

    def run_admission():
        try:
            asyncio.run(
                runner.run_sync(session.session_id, "must not run after disposal")
            )
            events.append("admission_returned")
        except BaseException as exc:
            errors.append(exc)

    def run_disposal():
        try:
            asyncio.run(runner.dispose_session(session.session_id))
            events.append("disposal_completed")
        except BaseException as exc:
            errors.append(exc)
        finally:
            disposal_done.set()

    with (
        patch.object(AgentSession, "begin_run", observed_begin_run),
        patch.object(runner, "cleanup_expired_sessions", side_effect=gated_cleanup),
        patch.object(runner, "_create_session", side_effect=create_replacement),
        patch.object(runner, "prepare_session_for_new_turn", return_value=session),
        patch.object(runner, "_bootstrap_session_from_context_messages"),
        patch.object(runner, "_decode_request_images", return_value=None),
        patch.object(runner, "_entry_uses_coordinator_routing", return_value=False),
        patch.object(
            runner,
            "_sync_active_agent",
            new=AsyncMock(side_effect=fake_active_agent),
        ),
    ):
        admission_thread = threading.Thread(target=run_admission, daemon=True)
        admission_thread.start()
        assert admission_paused.wait(2)

        admission_lock_was_released = runner._agent_admission_lock.acquire(
            blocking=False
        )
        if admission_lock_was_released:
            runner._agent_admission_lock.release()

        disposal_thread = threading.Thread(target=run_disposal, daemon=True)
        disposal_thread.start()
        if admission_lock_was_released:
            assert disposal_done.wait(2)
        allow_admission.set()
        admission_thread.join(2)
        disposal_thread.join(2)

    assert admission_thread.is_alive() is False
    assert disposal_thread.is_alive() is False
    assert errors == []
    assert events.index("run_started") < events.index("disposal_completed")
    assert events.index("admission_returned") < events.index("disposal_completed")


@pytest.mark.anyio
async def test_answer_now_acquisition_first_is_captured_by_disposal_barrier():
    runner = _runner()
    session = _resource_session("answer-now-wins-disposal")
    runner.sessions.put(session)
    status, acquired = runner.try_acquire_answer_now(session.session_id)
    assert status == "acquired"
    assert acquired is session
    session.begin_answer_now()
    answer_scope = session.begin_run()
    session_request_manager.set(session.session_id, "answer-now-provider-request")

    with patch("backend.llm_service.abort_request_by_id") as targeted_abort:
        disposal = asyncio.create_task(runner.dispose_session(session.session_id))
        for _ in range(100):
            if session.aborted:
                break
            await asyncio.sleep(0.01)

        assert disposal.done() is False
        targeted_abort.assert_called_once()
        assert targeted_abort.call_args.args[0] == "answer-now-provider-request"
        assert targeted_abort.call_args.kwargs["fallback_to_abort_all"] is False
        session.finish_run(answer_scope)
        await asyncio.sleep(0.05)
        assert disposal.done() is False
        session.finish_answer_now()
        assert (await disposal)["status"] == "disposed"


@pytest.mark.anyio
async def test_answer_now_expected_identity_rejects_stale_same_string_session():
    runner = _runner()
    old = _resource_session("answer-now-reused-id")
    runner.sessions.put(old)
    assert (await runner.dispose_session(old.session_id))["status"] == "disposed"
    replacement = _resource_session(old.session_id)
    runner.sessions.put(replacement)

    stale_status, stale_session = runner.try_acquire_answer_now(
        old.session_id, expected_session=old
    )
    fresh_status, fresh_session = runner.try_acquire_answer_now(
        replacement.session_id, expected_session=replacement
    )

    assert stale_status == "busy"
    assert stale_session is None
    assert fresh_status == "acquired"
    assert fresh_session is replacement
    assert old._answer_now_in_progress is False
    replacement.finish_answer_now()


@pytest.mark.anyio
async def test_manager_disposal_cleans_owned_child_resources_only_after_child_barrier():
    runner = _runner()
    manager = _resource_session("dispose-manager")
    child_image = _tracked_image()
    child = _resource_session("dispose-manager-child", child_image)
    mapped_only_image = _tracked_image()
    mapped_only_child = _resource_session(
        "dispose-manager-mapped-only-child", mapped_only_image
    )
    sibling_manager = _resource_session("sibling-manager")
    sibling_child = _resource_session("sibling-child")
    for session in (
        manager,
        child,
        mapped_only_child,
        sibling_manager,
        sibling_child,
    ):
        runner.sessions.put(session)
        session.event_hub.append({"type": "content", "content": session.session_id})

    child_scope = child.begin_run()
    session_request_manager.set(child.session_id, "request-child")
    child_run = ChildAgentRun(
        child_run_id=child.session_id,
        manager_session_id=manager.session_id,
        dispatch_id="dispatch-owned",
        child_name="owned",
        profile_name="basic_agent",
        assigned_task="Inspect owned child resources.",
        event_hub_producer_token=child.event_hub.claim_producer(),
        status="running",
        child_session_identity=child,
    )
    sibling_run = ChildAgentRun(
        child_run_id=sibling_child.session_id,
        manager_session_id=sibling_manager.session_id,
        dispatch_id="dispatch-sibling",
        child_name="sibling",
        profile_name="basic_agent",
        assigned_task="Retain sibling resources.",
        status="completed",
        child_session_identity=sibling_child,
    )
    runner.child_agent_runs[child_run.child_run_id] = child_run
    runner.child_agent_runs[sibling_run.child_run_id] = sibling_run
    runner.child_agent_sessions_by_key[(manager.session_id, "owned")] = child.session_id
    runner.child_agent_sessions_by_key[(manager.session_id, "mapped-only")] = (
        mapped_only_child.session_id
    )
    runner.child_agent_sessions_by_key[(sibling_manager.session_id, "sibling")] = (
        sibling_child.session_id
    )

    with patch("backend.llm_service.abort_request_by_id") as targeted_abort:
        disposal = asyncio.create_task(runner.dispose_session(manager.session_id))
        for _ in range(100):
            if child.aborted:
                break
            await asyncio.sleep(0.01)

        assert child.aborted is True
        assert disposal.done() is False
        child_image.close.assert_not_called()
        targeted_abort.assert_called_once()
        assert targeted_abort.call_args.args[0] == "request-child"
        assert targeted_abort.call_args.kwargs["fallback_to_abort_all"] is False

        child.finish_run(child_scope)
        child_run.status = "aborted"
        child_run.error = "The manager session is being disposed."
        child_run.terminal_event.set()
        result = await asyncio.wait_for(disposal, timeout=1)

    assert result["status"] == "disposed"
    child_image.close.assert_called_once_with()
    mapped_only_image.close.assert_called_once_with()
    assert runner.sessions.get(manager.session_id) is None
    assert runner.sessions.get(child.session_id) is None
    assert runner.sessions.get(mapped_only_child.session_id) is None
    assert child.agent.memory.steps == []
    assert mapped_only_child.agent.memory.steps == []
    assert manager.event_hub.released is True
    assert child.event_hub.released is True
    assert mapped_only_child.event_hub.released is True
    assert session_request_manager.get(child.session_id) is None
    assert abort_manager.is_aborted(child.session_id) is False
    assert child_run.child_run_id not in runner.child_agent_runs
    assert child_run.child_run_id not in runner._retained_child_replay_ids
    assert (manager.session_id, "owned") not in runner.child_agent_sessions_by_key
    assert (
        manager.session_id,
        "mapped-only",
    ) not in runner.child_agent_sessions_by_key
    assert runner.sessions.get(sibling_manager.session_id) is sibling_manager
    assert runner.sessions.get(sibling_child.session_id) is sibling_child
    assert sibling_manager.event_hub.released is False
    assert sibling_child.event_hub.released is False
    assert sibling_run.child_run_id in runner.child_agent_runs
    assert (sibling_manager.session_id, "sibling") in runner.child_agent_sessions_by_key


@pytest.mark.anyio
async def test_disposed_manager_child_journal_and_metadata_release_immediately():
    runner = _runner()
    manager = _resource_session("replay-manager")
    child = _resource_session("replay-child")
    runner.sessions.put(manager)
    runner.sessions.put(child)
    child_run = ChildAgentRun(
        child_run_id=child.session_id,
        manager_session_id=manager.session_id,
        dispatch_id="replay-dispatch",
        child_name="reader",
        profile_name="basic_agent",
        assigned_task="Read the retained terminal transcript.",
        event_hub_producer_token=child.event_hub.claim_producer(),
        call_id="replay-call",
        status="completed",
        final_report="retained report",
        completed_at=datetime.now(),
        child_session_identity=child,
    )
    child_run.terminal_event.set()
    runner.child_agent_runs[child_run.child_run_id] = child_run
    runner.child_agent_sessions_by_key[(manager.session_id, child_run.child_name)] = (
        child.session_id
    )

    await runner.dispose_session(manager.session_id)

    with pytest.raises(KeyError):
        runner.get_child_agent_run(child_run.child_run_id)
    assert child.event_hub.released is True
    assert child.event_hub.journal_size == 0


@pytest.mark.anyio
async def test_retained_orphan_ttl_ignores_unrelated_live_session_with_same_id():
    runner = _runner()
    manager = _resource_session("ttl-generation-manager")
    child = _resource_session("shared-child-session-id")
    runner.sessions.put(manager)
    runner.sessions.put(child)
    child_run = ChildAgentRun(
        child_run_id=child.session_id,
        manager_session_id=manager.session_id,
        dispatch_id="ttl-generation-dispatch",
        child_name="reader",
        profile_name="basic_agent",
        assigned_task="Retain this completed replay for one TTL generation.",
        event_hub_producer_token=child.event_hub.claim_producer(),
        status="completed",
        final_report="old retained replay",
        completed_at=datetime.now(),
        child_session_identity=child,
    )
    child_run.terminal_event.set()
    runner.child_agent_runs[child_run.child_run_id] = child_run
    runner.child_agent_sessions_by_key[(manager.session_id, child_run.child_name)] = (
        child.session_id
    )

    await runner.dispose_session(manager.session_id)
    unrelated = AgentSession(
        session_id=child.session_id,
        agent=SimpleNamespace(memory=SimpleNamespace(steps=["unrelated history"])),
        mode="tool_calling",
        capability=_session_capability(child.session_id),
    )
    runner.sessions.put(unrelated)
    child_run.completed_at = datetime.now() - timedelta(minutes=61)

    runner.cleanup_expired_sessions(60)

    assert runner.sessions.get(unrelated.session_id) is unrelated
    assert runner.child_agent_runs.get(child_run.child_run_id) is None
    assert child_run.child_run_id not in runner._retained_child_replay_ids


@pytest.mark.anyio
async def test_disposed_session_stale_callbacks_cannot_mutate_reused_id():
    runner = _runner()
    old = _resource_session("reused-session")
    runner.sessions.put(old)
    old_request_id = "request-old"
    session_request_manager.set(old.session_id, old_request_id)

    assert (await runner.dispose_session(old.session_id))["status"] == "disposed"
    replacement = _resource_session(old.session_id)
    runner.sessions.put(replacement)
    session_request_manager.set(replacement.session_id, "request-new")

    old.agent.memory.steps.append(TaskStep(task="stale callback"))
    session_request_manager.clear(old.session_id, old_request_id)

    assert runner.sessions.delete_if_same(old.session_id, old) is False
    assert runner.sessions.get(old.session_id) is replacement
    assert replacement.agent.memory.steps[0].task == "inspect"
    assert session_request_manager.get(old.session_id) == "request-new"
    session_request_manager.cleanup(old.session_id)


@pytest.mark.anyio
async def test_explicit_delete_releases_top_level_hub_exactly_once():
    runner = _runner()
    session = _resource_session("single-release-top-level")
    runner.sessions.put(session)
    session.event_hub.release = MagicMock(wraps=session.event_hub.release)

    assert (await runner.dispose_session(session.session_id))["status"] == "disposed"

    session.event_hub.release.assert_called_once_with()


@pytest.mark.anyio
async def test_manager_delete_releases_each_exact_hub_once():
    runner = _runner()
    manager = _resource_session("single-release-manager")
    child = _resource_session("single-release-child")
    child.is_child_context = True
    runner.sessions.put(manager)
    runner.sessions.put(child)
    child_run = ChildAgentRun(
        child_run_id=child.session_id,
        manager_session_id=manager.session_id,
        dispatch_id="single-release-dispatch",
        child_name="worker",
        profile_name="basic_agent",
        assigned_task="Verify exact cleanup ownership.",
        event_hub_producer_token=child.event_hub.claim_producer(),
        status="completed",
        completed_at=datetime.now(),
        child_session_identity=child,
    )
    child_run.terminal_event.set()
    runner.child_agent_runs[child_run.child_run_id] = child_run
    runner.child_agent_sessions_by_key[(manager.session_id, child_run.child_name)] = (
        child.session_id
    )
    manager.event_hub.release = MagicMock(wraps=manager.event_hub.release)
    child.event_hub.release = MagicMock(wraps=child.event_hub.release)

    assert (await runner.dispose_session(manager.session_id))["status"] == "disposed"

    manager.event_hub.release.assert_called_once_with()
    child.event_hub.release.assert_called_once_with()


def test_stale_child_disposal_callback_cannot_delete_same_id_replacement():
    runner = _runner()
    manager = _resource_session("stale-child-manager")
    old_child = _resource_session("stale-child")
    replacement = _resource_session(old_child.session_id)
    runner.sessions.put(manager)
    runner.sessions.put(old_child)
    child_run = ChildAgentRun(
        child_run_id=old_child.session_id,
        manager_session_id=manager.session_id,
        dispatch_id="stale-dispatch",
        child_name="worker",
        profile_name="basic_agent",
        assigned_task="Complete the stale child task.",
        status="aborted",
        child_session_identity=old_child,
    )
    runner.child_agent_sessions_by_key[(manager.session_id, "worker")] = (
        old_child.session_id
    )
    assert runner.sessions.delete_if_same(old_child.session_id, old_child) is True
    assert old_child.event_hub.released is True
    runner.sessions.put(replacement)
    session_request_manager.set(replacement.session_id, "replacement-request")

    runner._dispose_child_context(child_run, old_child)

    assert runner.sessions.get(old_child.session_id) is replacement
    assert old_child.event_hub.released is True
    assert replacement.event_hub.released is False
    assert session_request_manager.get(replacement.session_id) == "replacement-request"
    assert runner.child_agent_sessions_by_key[(manager.session_id, "worker")] == (
        replacement.session_id
    )
    session_request_manager.cleanup(replacement.session_id)
