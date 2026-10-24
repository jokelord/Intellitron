from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from backend.agent.agent_runner import AgentRunner, AgentSession
from backend.config import AgentConfig, AppConfig, ReadFileConfig, get_config
from backend.security.capabilities import build_session_capability
from backend.tools import sys_ops


def test_capability_carries_one_validated_command_runtime_snapshot(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    config = AppConfig(
        read_file=ReadFileConfig(allowed_roots=[str(root)], max_source_bytes=12345),
        agent=AgentConfig(
            search_whitelist_paths=[str(root)],
            work_dir=str(tmp_path),
            python_executable=str(Path(__file__).parents[1] / ".sglang/bin/python"),
            nvm_dir=str(tmp_path / "nvm"),
            command_mode="argv",
            allowed_executables=["printf"],
            command_cwd_allowlist=[str(root)],
            exec_timeout=3,
            max_exec_timeout_seconds=9,
            max_output_bytes=777,
            termination_grace_seconds=0.25,
            allow_browser=False,
        ),
    )

    capability = build_session_capability(
        config, session_id=str(uuid4()), mode="tool_calling"
    )
    runtime = capability.command_runtime_config()

    assert capability.command_mode == runtime.command_mode == "argv"
    assert capability.allowed_executables == runtime.allowed_executables == ("printf",)
    assert capability.max_exec_timeout_seconds == runtime.max_exec_timeout_seconds == 9
    assert capability.max_output_bytes == runtime.max_output_bytes == 777
    assert capability.max_file_bytes == 12345
    assert runtime.exec_timeout == 3
    assert runtime.termination_grace_seconds == 0.25
    assert runtime.allow_browser is False
    config.agent.command_cwd_allowlist.append("/tmp")
    assert capability.command_cwd_allowlist == runtime.command_cwd_allowlist == (str(root),)


def test_capability_carries_operation_specific_roots_and_file_defaults(tmp_path):
    read_root = tmp_path / "read"
    search_root = tmp_path / "search"
    file_root = tmp_path / "file"
    image_root = tmp_path / "image"
    session_temp = tmp_path / "session-temp"
    for root in (read_root, search_root, file_root, image_root, session_temp):
        root.mkdir()
    config = AppConfig(
        read_file=ReadFileConfig(
            allowed_roots=[str(read_root)], max_source_bytes=12345
        ),
        agent=AgentConfig(
            search_whitelist_paths=[str(search_root)],
            file_operation_roots=[str(file_root)],
            image_input_roots=[str(image_root)],
            session_temp_base_dir=str(session_temp),
            max_file_bytes=4321,
            allow_overwrite_existing=False,
            work_dir=str(tmp_path),
            python_executable=str(Path(__file__).parents[1] / ".sglang/bin/python"),
            nvm_dir=str(tmp_path / "nvm"),
        ),
    )

    capability = build_session_capability(
        config, session_id=str(uuid4()), mode="tool_calling"
    )

    assert capability.read_roots == (str(read_root.resolve()),)
    assert capability.search_roots == (str(search_root.resolve()),)
    assert capability.file_operation_roots == (str(file_root.resolve()),)
    assert capability.image_input_roots == (str(image_root.resolve()),)
    assert capability.session_temp_base_dir == str(session_temp)
    assert capability.max_file_bytes == 4321
    assert capability.allow_overwrite_existing is False


def test_all_runtime_profiles_bind_the_same_session_capability(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    config = AppConfig(
        read_file=ReadFileConfig(allowed_roots=[str(root)]),
        agent=AgentConfig(
            entry_agent_name="coordinator_1",
            enabled_agent_profiles=[
                "coordinator_1",
                "coordinator_2",
                "manager_agent",
                "basic_agent",
            ],
            search_whitelist_paths=[str(root)],
            work_dir=str(tmp_path),
            python_executable=str(Path(__file__).parents[1] / ".sglang/bin/python"),
            nvm_dir=str(tmp_path / "nvm"),
        ),
        agent_prompts=get_config().agent_prompts,
    )
    runner = AgentRunner(MagicMock(), config)
    session_id = str(uuid4())
    capability = build_session_capability(
        config, session_id=session_id, mode="tool_calling"
    )
    session = AgentSession(
        session_id=session_id,
        agent=SimpleNamespace(memory=SimpleNamespace(steps=[]), state={}),
        mode="tool_calling",
        capability=capability,
        tool_configuration_snapshot=runner.default_tool_configuration_snapshot,
    )

    for profile in config.agent.enabled_agent_profiles:
        agent = runner._build_profile_agent(profile, session=session)
        assert agent._operation_capability is capability
        assert all(
            tool._injected_capability is capability
            for tool in agent.tools.values()
        )
        assert all(
            internal not in tool.inputs
            for tool in agent.tools.values()
            for internal in ("session_id", "capability", "runtime_config")
        )


def test_actual_command_invocation_uses_capability_exec_timeout(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    config = AppConfig(
        read_file=ReadFileConfig(allowed_roots=[str(root)]),
        agent=AgentConfig(
            search_whitelist_paths=[str(root)],
            work_dir=str(tmp_path),
            python_executable=str(Path(__file__).parents[1] / ".sglang/bin/python"),
            nvm_dir=str(tmp_path / "nvm"),
            command_mode="argv",
            allowed_executables=["printf"],
            exec_timeout=7,
            max_exec_timeout_seconds=9,
        ),
    )
    capability = build_session_capability(
        config, session_id=str(uuid4()), mode="tool_calling"
    )
    captured = {}

    def capture_process(argv, **kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(sys_ops, "_run_process", capture_process)
    monkeypatch.setattr(
        sys_ops.EnvironmentBuilder,
        "build_argv",
        lambda self, argv: SimpleNamespace(
            argv=("/usr/bin/printf", *argv[1:]), env={}, cwd=str(tmp_path)
        ),
    )

    result = sys_ops.exec_command(
        "printf ok",
        session_id=capability.session_id,
        capability=capability,
    )

    assert result == "ok"
    assert captured["timeout"] == 7


def test_invalid_capability_field_reports_its_precise_name(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    config = AppConfig(
        read_file=ReadFileConfig(allowed_roots=[str(root)]),
        agent=AgentConfig(
            search_whitelist_paths=[str(root)],
            file_operation_roots="not-a-list",  # type: ignore[arg-type]
            work_dir=str(tmp_path),
            python_executable=str(Path(__file__).parents[1] / ".sglang/bin/python"),
            nvm_dir=str(tmp_path / "nvm"),
        ),
    )

    with pytest.raises(ValueError, match="agent.file_operation_roots"):
        build_session_capability(
            config, session_id=str(uuid4()), mode="tool_calling"
        )
