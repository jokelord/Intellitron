from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from backend.agent.agent_runner import (
    AgentRunner,
    AgentSession,
    SessionAdmissionError,
)
from backend.config import AgentConfig, AppConfig, ReadFileConfig, get_config
from backend.security.capabilities import (
    CapabilityError,
    OperationCapability,
    build_session_capability,
)
from backend.security.path_policy import PathPolicyError


def _config(tmp_path: Path) -> AppConfig:
    read_root = tmp_path / "read"
    search_root = tmp_path / "search"
    read_root.mkdir(exist_ok=True)
    search_root.mkdir(exist_ok=True)
    return AppConfig(
        read_file=ReadFileConfig(allowed_roots=[str(read_root)], max_source_bytes=4096),
        agent=AgentConfig(
            agent_mode="tool_calling",
            search_whitelist_paths=[str(search_root)],
            work_dir=str(tmp_path),
            python_executable=str(Path(__file__).parents[1] / ".sglang/bin/python"),
            nvm_dir=str(tmp_path / "nvm"),
            allowed_executables=["printf"],
            exec_timeout=7,
            max_exec_timeout_seconds=11,
            max_output_bytes=2048,
            allow_browser=False,
        ),
        # AgentRunner startup now validates the complete selectable-tool
        # prompt catalog; this capability fixture only replaces policy roots.
        agent_prompts=get_config().agent_prompts,
    )


def test_build_session_capability_is_immutable_and_operation_roots_stay_separate(tmp_path):
    config = _config(tmp_path)
    session_id = str(uuid4())

    capability = build_session_capability(
        config, session_id=session_id, mode="tool_calling"
    )

    assert capability.session_id == session_id
    assert capability.read_roots == (str((tmp_path / "read").resolve()),)
    assert capability.file_operation_roots == capability.read_roots
    assert capability.image_input_roots == capability.read_roots
    assert capability.search_roots == (str((tmp_path / "search").resolve()),)
    assert capability.session_temp_base_dir == "/tmp/agent_images"
    assert capability.allow_overwrite_existing is True
    with pytest.raises(FrozenInstanceError):
        capability.allow_browser = True  # type: ignore[misc]


def test_build_session_capability_rejects_missing_policy_invalid_mode_and_bad_limits(tmp_path):
    session_id = str(uuid4())
    with pytest.raises(CapabilityError, match="capability_config_missing"):
        build_session_capability(
            AgentConfig(), session_id=session_id, mode="tool_calling"
        )
    with pytest.raises(CapabilityError, match="invalid_agent_mode"):
        build_session_capability(
            _config(tmp_path), session_id=session_id, mode="code"
        )

    config = _config(tmp_path)
    config.agent.max_output_bytes = 0
    with pytest.raises(CapabilityError, match="invalid_capability_config"):
        build_session_capability(
            config, session_id=session_id, mode="tool_calling"
        )


def test_agent_session_requires_matching_capability_and_valid_identity(tmp_path):
    capability = build_session_capability(
        _config(tmp_path), session_id=str(uuid4()), mode="tool_calling"
    )
    # Direct inspection records may lack authority; operation boundaries still
    # reject them instead of treating construction as permission to act.
    inspection = AgentSession(session_id=capability.session_id, agent=None, mode="tool_calling")
    assert inspection.capability is None
    from backend.security.capabilities import require_capability
    with pytest.raises(CapabilityError, match="missing_capability"):
        require_capability(inspection.capability, "desktop_capture")
    with pytest.raises(CapabilityError, match="capability_session_mismatch"):
        AgentSession(
            session_id=str(uuid4()),
            agent=None,
            mode="tool_calling",
            capability=capability,
        )
    with pytest.raises(PathPolicyError, match="invalid_session_id"):
        AgentSession(
            session_id="../escape",
            agent=None,
            mode="tool_calling",
            capability=capability,
        )


def test_runner_admission_generates_uuid_and_reuses_original_capability(tmp_path, monkeypatch):
    config = _config(tmp_path)
    runner = AgentRunner(SimpleNamespace(), config)
    created: list[AgentSession] = []

    def create(session_id: str, mode: str) -> AgentSession:
        session = AgentSession(
            session_id=session_id,
            agent=None,
            mode=mode,
            capability=build_session_capability(
                config, session_id=session_id, mode=mode
            ),
            tool_configuration_snapshot=runner.default_tool_configuration_snapshot,
        )
        runner.sessions.put(session)
        created.append(session)
        return session

    monkeypatch.setattr(runner, "_create_session", create)
    monkeypatch.setattr(
        runner, "prepare_session_for_new_turn", lambda session_id: runner.sessions.get(session_id)
    )
    generated = runner.get_or_create_session(None)
    assert str(UUID(generated.session_id)) == generated.session_id

    canonical_id = str(uuid4())
    canonical = runner.get_or_create_session(canonical_id)
    original_capability = canonical.capability
    config.agent.allowed_executables.append("python")
    assert runner.get_or_create_session(canonical_id).capability is original_capability

    with pytest.raises(SessionAdmissionError, match="legacy_session_not_found"):
        runner.get_or_create_session("legacy-safe-token")
    legacy_capability = build_session_capability(
        config, session_id="legacy-safe-token", mode="tool_calling"
    )
    legacy = AgentSession(
        session_id="legacy-safe-token",
        agent=None,
        mode="tool_calling",
        capability=legacy_capability,
        tool_configuration_snapshot=runner.default_tool_configuration_snapshot,
    )
    runner.sessions.put(legacy)
    assert runner.get_or_create_session("legacy-safe-token") is legacy


def test_invalid_session_id_is_rejected_before_store_lookup(tmp_path, monkeypatch):
    runner = AgentRunner(SimpleNamespace(), _config(tmp_path))
    monkeypatch.setattr(runner.sessions, "get", pytest.fail)

    with pytest.raises(PathPolicyError, match="invalid_session_id"):
        runner.get_or_create_session("../escape")
