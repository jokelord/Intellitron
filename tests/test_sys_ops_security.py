import stat
import sys
from pathlib import Path

import pytest

from backend.tools import sys_ops
from backend.security.capabilities import OperationCapability
from backend.security.environment_builder import CommandRuntimeConfig


class _SpawnStopped(RuntimeError):
    pass


def _runtime(tmp_path: Path, **overrides):
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    playwright = tools / "playwright-cli"
    playwright.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    playwright.chmod(playwright.stat().st_mode | stat.S_IXUSR)
    values = {
        "command_mode": "restricted_shell",
        "allowed_executables": ("playwright-cli", "printf", "python"),
        "command_blacklist": None,
        "exec_timeout": 60,
        "max_exec_timeout_seconds": 3600,
        "max_output_bytes": 4096,
        "termination_grace_seconds": 0.1,
        "allow_browser": True,
        "work_dir": str(tmp_path),
        "python_executable": sys.executable,
        "nvm_dir": str(tmp_path / "nvm"),
        "playwright_profile_dir": None,
        "environment_allowlist": (),
        "command_path_entries": (str(tools),),
    }
    values.update(overrides)
    return CommandRuntimeConfig(**values)


def _capability(
    tmp_path: Path,
    runtime: CommandRuntimeConfig | None = None,
    *,
    session_id: str = "safe-session",
) -> OperationCapability:
    runtime = runtime or _runtime(tmp_path)
    root = str(tmp_path.resolve())
    return OperationCapability(
        session_id=session_id,
        read_roots=(root,),
        search_roots=(root,),
        file_operation_roots=(root,),
        image_input_roots=(root,),
        session_temp_base_dir=str((tmp_path / "images").resolve()),
        command_mode=runtime.command_mode,
        allowed_executables=runtime.allowed_executables,
        max_exec_timeout_seconds=runtime.max_exec_timeout_seconds,
        max_output_bytes=runtime.max_output_bytes,
        max_file_bytes=4096,
        allow_overwrite_existing=True,
        allow_browser=runtime.allow_browser,
        exec_timeout=runtime.exec_timeout,
        termination_grace_seconds=runtime.termination_grace_seconds,
        work_dir=runtime.work_dir,
        python_executable=runtime.python_executable,
        nvm_dir=runtime.nvm_dir,
        playwright_profile_dir=runtime.playwright_profile_dir,
        environment_allowlist=runtime.environment_allowlist,
        command_path_entries=runtime.command_path_entries,
    )


def _capture_popen(monkeypatch, tmp_path, *, runtime=None):
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        raise _SpawnStopped("stop after process boundary")

    monkeypatch.setattr(sys_ops.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        sys_ops,
        "_get_command_runtime",
        lambda runtime_config=None, capability=None: runtime_config
        or runtime
        or _runtime(tmp_path),
    )
    return calls


@pytest.mark.parametrize(
    "session_id",
    [
        "",
        "bad session",
        "bad;session",
        "bad$(id)",
        "bad|session",
        "bad\nname",
        "bad\u2028name",
        "x" * 129,
    ],
)
def test_browser_rejects_invalid_session_before_process_creation(
    monkeypatch, tmp_path, session_id
):
    calls = _capture_popen(monkeypatch, tmp_path)

    result = sys_ops.browser_command("snapshot", session_id=session_id)

    assert calls == []
    assert "invalid_session_id" in result
    if session_id:
        assert session_id not in result


def test_browser_session_is_a_standalone_process_argument(monkeypatch, tmp_path):
    calls = _capture_popen(monkeypatch, tmp_path)

    capability = _capability(tmp_path, session_id="session-123")
    result = sys_ops.browser_command(
        "open https://example.com",
        session_id=capability.session_id,
        capability=capability,
    )

    assert result == "命令执行异常: _SpawnStopped: stop after process boundary"
    assert len(calls) == 1
    argv, kwargs = calls[0]
    session_index = argv.index("session-123")
    assert argv[session_index - 1] in {"-s", "--session"}
    assert argv.count("session-123") == 1
    assert all("eval" not in arg for arg in argv)
    assert kwargs.get("shell") is not True


def test_browser_defaults_remain_structured_without_a_session(monkeypatch, tmp_path):
    argv = sys_ops._build_browser_argv("open https://example.com", session_id=None)
    assert Path(argv[0]).name == "playwright-cli"
    assert "--browser=chromium" in argv
    assert "--headed" in argv
    assert "--persistent" in argv
    assert "-s" not in argv
    assert "--session" not in argv


def test_browser_keeps_configured_profile_as_one_argument(monkeypatch, tmp_path):
    calls = _capture_popen(
        monkeypatch,
        tmp_path,
        runtime=_runtime(tmp_path, playwright_profile_dir="/tmp/profile with spaces"),
    )

    capability = _capability(
        tmp_path,
        _runtime(tmp_path, playwright_profile_dir="/tmp/profile with spaces"),
        session_id="session-profile",
    )
    sys_ops.browser_command(
        "open https://example.com",
        session_id=capability.session_id,
        capability=capability,
    )

    argv = calls[0][0]
    assert "--profile=/tmp/profile with spaces" in argv
    assert "--persistent" not in argv


@pytest.mark.parametrize(
    "command",
    [
        "snapshot; touch /tmp/not-authorized",
        "snapshot $(touch /tmp/not-authorized)",
        "snapshot `touch /tmp/not-authorized`",
        "snapshot | sh",
    ],
)
def test_browser_rejects_shell_syntax_before_process_creation(
    monkeypatch, tmp_path, command
):
    calls = _capture_popen(monkeypatch, tmp_path)

    capability = _capability(tmp_path)
    result = sys_ops.browser_command(
        command, session_id=capability.session_id, capability=capability
    )

    assert calls == []
    assert "命令被安全策略阻断" in result


def test_exec_command_rejects_legacy_playwright_cli_without_spawning(
    monkeypatch, tmp_path
):
    calls = _capture_popen(monkeypatch, tmp_path)

    result = sys_ops.exec_command(
        "playwright-cli snapshot",
        session_id="exec-browser-session",
        capability=_capability(tmp_path, session_id="exec-browser-session"),
    )

    assert calls == []
    assert "legacy_browser_cli_disabled" in result
    assert "playwright-browser" in result


def test_browser_policy_checks_session_bound_argv_before_spawn(monkeypatch, tmp_path):
    calls = _capture_popen(
        monkeypatch,
        tmp_path,
        runtime=_runtime(tmp_path, allowed_executables=("rg",)),
    )

    capability = _capability(tmp_path, session_id="safe-session")
    result = sys_ops.browser_command(
        "open https://example.com",
        session_id="safe-session",
        capability=capability,
    )

    assert calls == []
    assert "executable_not_allowed" in result


def test_restricted_shell_denial_happens_before_process_creation(monkeypatch, tmp_path):
    calls = _capture_popen(monkeypatch, tmp_path)

    capability = _capability(tmp_path)
    result = sys_ops.exec_command(
        "printf payload | python", session_id=capability.session_id, capability=capability
    )

    assert calls == []
    assert "interpreter_pipeline" in result


def test_restricted_shell_cannot_override_protected_runtime_environment(
    monkeypatch, tmp_path
):
    calls = _capture_popen(monkeypatch, tmp_path)

    capability = _capability(tmp_path)
    result = sys_ops.exec_command(
        "env PATH=/tmp printf payload",
        session_id=capability.session_id,
        capability=capability,
    )

    assert calls == []
    assert "protected_environment_override" in result


def test_configured_work_dir_rejects_symlinked_parent_before_spawn(
    monkeypatch, tmp_path
):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked-parent"
    link.symlink_to(outside, target_is_directory=True)
    calls = _capture_popen(monkeypatch, tmp_path)

    capability = _capability(
        tmp_path, _runtime(tmp_path, work_dir=str(link / "work"))
    )
    result = sys_ops.exec_command(
        "printf never-runs",
        session_id=capability.session_id,
        runtime_config=capability.command_runtime_config(),
        capability=capability,
    )

    assert calls == []
    assert "invalid_work_dir" in result


def test_ordinary_exec_success_output_is_preserved(tmp_path):
    capability = _capability(tmp_path)
    assert (
        sys_ops.exec_command(
            "printf ordinary-output",
            session_id=capability.session_id,
            capability=capability,
        )
        == "ordinary-output"
    )


def test_exec_command_translates_supervisor_truncation_without_unbounded_output(
    tmp_path,
):
    capability = _capability(tmp_path, _runtime(tmp_path, max_output_bytes=64))
    result = sys_ops.exec_command(
        "printf '%02048d' 0",
        session_id=capability.session_id,
        capability=capability,
    )

    assert result.startswith("0" * 32)
    assert "[输出已截断]" in result
    assert len(result) < 128
