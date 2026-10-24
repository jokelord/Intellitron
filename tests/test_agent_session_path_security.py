from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.agent.agent_runner import (
    AgentRunner,
    AgentSession,
    SessionAdmissionError,
    SessionStore,
    _cleanup_session_images,
)
from backend.config import get_config
from backend.api.agent import AgentChatRequest
from backend.security.capabilities import OperationCapability, build_session_capability
from backend.security.path_policy import PathPolicy, PathPolicyError


def _session_policy(tmp_path: Path) -> PathPolicy:
    root = tmp_path / "root"
    root.mkdir()
    return PathPolicy(
        file_operation_roots=[root],
        session_base_dir=tmp_path / "agent-images",
    )


def _capability(policy: PathPolicy, session_id: str) -> OperationCapability:
    roots = tuple(str(item) for item in policy.file_operation_roots)
    return OperationCapability(
        session_id=session_id,
        read_roots=roots,
        search_roots=roots,
        file_operation_roots=roots,
        image_input_roots=tuple(str(item) for item in policy.image_input_roots),
        session_temp_base_dir=str(policy.session_base_dir),
        command_mode="argv",
        allowed_executables=("python",),
        max_exec_timeout_seconds=60,
        max_output_bytes=4096,
        max_file_bytes=policy.max_file_bytes,
        allow_overwrite_existing=policy.allow_overwrite_existing,
        allow_browser=False,
        exec_timeout=1,
        termination_grace_seconds=0,
        work_dir=str(policy.session_base_dir),
        python_executable="/usr/bin/python3",
        nvm_dir=str(policy.session_base_dir),
        environment_allowlist=(),
    )


def _runner() -> AgentRunner:
    return AgentRunner(SimpleNamespace(), get_config())


def test_cleanup_removes_only_valid_policy_session_directory(tmp_path):
    """Removing policy-owned directory validation would make cleanup target arbitrary paths."""
    policy = _session_policy(tmp_path)
    session_id = str(uuid4())
    directory = Path(policy.allocate_session_dir(session_id))
    (directory / "payload.json").write_text("{}", encoding="utf-8")

    assert _cleanup_session_images(
        session_id, capability=_capability(policy, session_id)
    ) is True
    assert not directory.exists()


def test_cleanup_rejects_invalid_id_without_creating_or_deleting_outside_directory(tmp_path):
    """Removing session-ID admission would allow traversal to reach cleanup."""
    policy = _session_policy(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    session_id = str(uuid4())

    assert _cleanup_session_images(
        "../outside", capability=_capability(policy, session_id)
    ) is False
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "agent-images").exists()


def test_session_store_rejects_malformed_identifiers_before_storage():
    """Removing SessionStore validation would retain a path-like session key."""
    store = SessionStore()
    with pytest.raises(PathPolicyError, match="invalid_session_id"):
        AgentSession(
            session_id="../../escape",
            agent=None,
            mode="tool_calling",
            capability=_capability(
                PathPolicy(file_operation_roots=["/tmp"]), "safe-session"
            ),
        )

    assert store.active_count == 0


@pytest.mark.parametrize("child_id", ["worker.v2", "child:alpha"])
def test_child_identity_with_dispatch_allowed_dot_or_colon_passes_session_validator(child_id):
    """A narrower session token regex rejects a child identity accepted by dispatch."""
    runner = _runner()
    child_session_id = runner._make_child_identity_id("manager.v2", child_id)

    assert PathPolicy.validate_session_id(child_session_id) == child_session_id


def test_maximum_dispatch_child_identity_is_bounded_unique_and_storable():
    """Appending a 128-character child ID must not create an overlong path component."""
    runner = _runner()
    prefix = "worker.v2:alpha-"
    child_id = prefix + "x" * (128 - len(prefix))
    same_prefix_different_child = child_id[:-1] + "y"
    task = "Investigate independent source details thoroughly."
    runner._validate_manager_child_dispatch(child_id, task)
    runner._validate_manager_child_dispatch(same_prefix_different_child, task)

    first = runner._make_child_identity_id("manager.v2", child_id)
    second = runner._make_child_identity_id("manager.v2", same_prefix_different_child)
    store = SessionStore()
    policy = PathPolicy(file_operation_roots=["/tmp"])
    store.put(
        AgentSession(
            session_id=first,
            agent=None,
            mode="tool_calling",
            capability=_capability(policy, first),
        )
    )
    store.put(
        AgentSession(
            session_id=second,
            agent=None,
            mode="tool_calling",
            capability=_capability(policy, second),
        )
    )

    assert len(first) <= 128
    assert len(second) <= 128
    assert PathPolicy.validate_session_id(first) == first
    assert PathPolicy.validate_session_id(second) == second
    assert first != second
    assert store.active_count == 2


@pytest.mark.parametrize("child_id", ["bad/name", r"bad\\name", "bad name", "bad;name", "bad$(id)"])
def test_dispatch_keeps_dangerous_child_id_characters_rejected(child_id):
    """Identity shortening must not relax dispatch's path and shell character contract."""
    with pytest.raises(ValueError):
        _runner()._validate_manager_child_dispatch(
            child_id, "Investigate independent source details thoroughly."
        )


@pytest.mark.parametrize(
    "session_id",
    [".", "..", "child/name", r"child\\name", "child name", "child;rm", "child$(id)"],
)
def test_session_validator_keeps_path_whitespace_and_shell_metacharacters_rejected(session_id):
    """Broadening session compatibility must not turn a token into a path or shell fragment."""
    with pytest.raises(PathPolicyError, match="invalid_session_id"):
        PathPolicy.validate_session_id(session_id)


def test_runner_only_creates_canonical_uuid_and_reuses_existing_legacy_token(monkeypatch):
    """Allowing any safe legacy token to create a session violates migration admission."""
    runner = _runner()
    created_ids: list[str] = []

    def create(session_id: str, mode: str):
        created_ids.append(session_id)
        return AgentSession(
            session_id=session_id,
            agent=None,
            mode=mode,
            capability=build_session_capability(
                runner.config, session_id=session_id, mode=mode
            ),
        )

    monkeypatch.setattr(runner, "_create_session", create)
    canonical = str(uuid4())
    assert runner.get_or_create_session(canonical).session_id == canonical
    assert created_ids == [canonical]

    with pytest.raises(SessionAdmissionError, match="legacy_session_not_found"):
        runner.get_or_create_session("legacy-safe-token")
    assert created_ids == [canonical]

    legacy = AgentSession(
        session_id="legacy-safe-token",
        agent=None,
        mode="tool_calling",
        capability=build_session_capability(
            runner.config, session_id="legacy-safe-token", mode="tool_calling"
        ),
        tool_configuration_snapshot=runner.default_tool_configuration_snapshot,
    )
    runner.sessions.put(legacy)
    assert runner.get_or_create_session("legacy-safe-token") is legacy


def test_staged_admission_rejects_unknown_legacy_token_before_pending_state():
    """Without staged admission validation, the async API path creates legacy sessions."""
    runner = _runner()

    with pytest.raises(SessionAdmissionError, match="legacy_session_not_found"):
        runner.stage_agent_request_admission(
            "legacy-safe-token", "tool_calling", None, inbound_image_count=0
        )

    assert runner._pending_agent_admissions == {}


@pytest.mark.parametrize("session_id", ["../escape", "bad/id", "bad id", ""])
def test_runner_rejects_invalid_session_ids_before_session_creation(monkeypatch, session_id):
    """Removing admission checks would create a session for an unsafe token."""
    runner = _runner()
    monkeypatch.setattr(runner, "_create_session", pytest.fail)

    with pytest.raises(PathPolicyError, match="invalid_session_id"):
        runner.get_or_create_session(session_id)


@pytest.mark.parametrize("session_id", ["../escape", "bad/id", "bad id"])
def test_agent_api_request_model_rejects_malformed_session_id(session_id):
    """Removing API admission would let malformed IDs reach session staging."""
    with pytest.raises(ValidationError, match="invalid session identifier"):
        AgentChatRequest(session_id=session_id, message="hello")
