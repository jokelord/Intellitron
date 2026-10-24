from __future__ import annotations

import json
import os
import pwd
import stat
import threading
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.agent_runner import get_tools_from_config
from backend.security.capabilities import OperationCapability
from backend.security.environment_builder import CommandRuntimeConfig, EnvironmentBuilder
from backend.tools import sys_ops


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="command execution isolation integration requires POSIX process groups",
)


def _runtime(tmp_path: Path, **overrides) -> CommandRuntimeConfig:
    values = {
        "command_mode": "restricted_shell",
        "allowed_executables": (
            "env",
            "false",
            "playwright-cli",
            "printf",
            "pwd",
            "python",
            "python3",
            "sleep",
        ),
        "command_blacklist": None,
        "exec_timeout": 3.0,
        "max_exec_timeout_seconds": 10.0,
        "max_output_bytes": 4096,
        "termination_grace_seconds": 0.1,
        "allow_browser": True,
        "work_dir": str(tmp_path),
        "python_executable": sys.executable,
        "nvm_dir": str(tmp_path / "nvm"),
        "environment_allowlist": ("LANG", "DISPLAY"),
        "command_path_entries": (),
    }
    values.update(overrides)
    return CommandRuntimeConfig(**values)


def _capability(
    tmp_path: Path,
    runtime: CommandRuntimeConfig | None = None,
    *,
    session_id: str = "integration-session",
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
        environment_allowlist=runtime.environment_allowlist,
        command_path_entries=runtime.command_path_entries,
        command_cwd_allowlist=runtime.command_cwd_allowlist,
        playwright_profile_dir=runtime.playwright_profile_dir,
    )


def _install_fake_playwright(nvm_dir: Path) -> Path:
    node_bin = nvm_dir / "versions" / "node" / "v24.1.0" / "bin"
    node_bin.mkdir(parents=True)
    executable = node_bin / "playwright-cli"
    executable.write_text(
        "#!/bin/sh\nprintf '%s' \"$PLAYWRIGHT_CLI_TEST_PREFIX\"\nprintf '%s\\n' \"$@\"\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def _wait_for_pids(path: Path, count: int, timeout: float = 2.0) -> list[int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            pids = [int(value) for value in path.read_text().splitlines()]
        except (FileNotFoundError, ValueError):
            pids = []
        if len(pids) == count:
            return pids
        time.sleep(0.01)
    pytest.fail(f"expected {count} supervised process ids")


def _living_process_count(pids: list[int]) -> int:
    living = 0
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        living += 1
    return living


def _wait_for_process_count(pids: list[int], expected: int, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _living_process_count(pids) == expected:
            return
        time.sleep(0.01)
    pytest.fail(f"expected {expected} living processes, got {_living_process_count(pids)}")


def test_public_exec_argv_mode_runs_one_allowed_argv_without_a_shell(tmp_path):
    runtime = _runtime(tmp_path, command_mode="argv")
    capability = _capability(tmp_path, runtime)

    result = sys_ops.exec_command(
        "printf argv-isolated",
        session_id=capability.session_id,
        runtime_config=runtime,
        capability=capability,
    )

    assert result == "argv-isolated"


@pytest.mark.parametrize("mode", ["argv", "restricted_shell"])
@pytest.mark.parametrize("use_tilde", [False, True])
def test_public_exec_accepts_only_configured_python_absolute_path(
    tmp_path, monkeypatch, mode, use_tilde,
):
    agent_home = tmp_path / "account"
    interpreter = agent_home / ".agent" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    monkeypatch.setattr(EnvironmentBuilder, "_trusted_home", staticmethod(lambda: agent_home))
    # HOME from the caller must not affect the expansion of configured ~/ paths.
    monkeypatch.setenv("HOME", str(tmp_path / "untrusted-home"))
    script = tmp_path / "plot_paraboloid.py"
    script.write_text("print('agent-python-ok')\n", encoding="utf-8")
    runtime = _runtime(
        tmp_path, command_mode=mode,
        python_executable="~/.agent/bin/python" if use_tilde else str(interpreter),
    )
    capability = _capability(tmp_path, runtime)

    result = sys_ops.exec_command(
        f"{interpreter} {script}", session_id=capability.session_id, capability=capability,
    )

    assert result.strip() == "agent-python-ok"


@pytest.mark.parametrize("mode", ["argv", "restricted_shell"])
@pytest.mark.parametrize("relative_path", ["other/bin/python", "agent/bin/python-extra"])
def test_public_exec_does_not_extend_python_permission_to_other_paths(
    tmp_path, mode, relative_path,
):
    interpreter = tmp_path / "agent" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    other = tmp_path / relative_path
    other.parent.mkdir(parents=True, exist_ok=True)
    other.symlink_to(sys.executable)
    frozen_runtime = _runtime(tmp_path, command_mode=mode, python_executable=str(interpreter))
    capability = _capability(tmp_path, frozen_runtime)
    later_runtime = _runtime(tmp_path, command_mode=mode, python_executable=str(other))

    result = sys_ops.exec_command(
        f"{other} -c 'print(123)'", session_id=capability.session_id,
        capability=capability, runtime_config=later_runtime,
    )

    assert "命令被安全策略阻断" in result
    assert "123" not in result


def test_configured_python_path_still_obeys_shell_pipeline_policy(tmp_path):
    runtime = _runtime(tmp_path)
    capability = _capability(tmp_path, runtime)
    result = sys_ops.exec_command(
        f"printf payload | {sys.executable}",
        session_id=capability.session_id, capability=capability,
    )
    assert "interpreter_pipeline" in result


def test_public_exec_cd_uses_session_allowlist_and_does_not_persist_cwd(tmp_path):
    destination = tmp_path / "documents"
    destination.mkdir()
    capability = _capability(tmp_path, _runtime(tmp_path, command_cwd_allowlist=(str(destination),)))
    result = sys_ops.exec_command(
        f"cd {destination} && pwd", session_id=capability.session_id, capability=capability,
    )
    assert result.strip() == str(destination)
    result = sys_ops.exec_command("pwd", session_id=capability.session_id, capability=capability)
    assert result.strip() == str(tmp_path)
    result = sys_ops.exec_command(
        f"cd / && printf denied", session_id=capability.session_id, capability=capability,
    )
    assert "cwd_not_allowed" in result


def test_public_exec_argv_mode_rejects_shell_syntax_before_spawn(tmp_path):
    marker = tmp_path / "must-not-exist"
    runtime = _runtime(tmp_path, command_mode="argv")
    capability = _capability(tmp_path, runtime)

    result = sys_ops.exec_command(
        f"printf allowed && touch {marker}",
        session_id=capability.session_id,
        runtime_config=runtime,
        capability=capability,
    )

    assert "unsupported_shell_syntax" in result
    assert not marker.exists()


def test_public_shell_compat_uses_constant_boundary_without_eval(monkeypatch, tmp_path):
    captured = {}

    def stop_at_spawn(argv, **kwargs):
        captured["argv"] = tuple(argv)
        captured["kwargs"] = kwargs
        raise RuntimeError("spawn captured")

    monkeypatch.setattr(sys_ops.subprocess, "Popen", stop_at_spawn)
    command = "printf compatibility-shell"
    capability = _capability(tmp_path)

    result = sys_ops.exec_command(
        command,
        session_id=capability.session_id,
        runtime_config=capability.command_runtime_config(),
        capability=capability,
    )

    assert "spawn captured" in result
    assert captured["argv"][:4] == (
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-c",
    )
    assert captured["argv"][-1] == command
    assert captured["argv"].count(command) == 1
    assert all("eval" not in value for value in captured["argv"])


def test_public_exec_uses_minimal_environment_and_configured_cwd(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LLM_DEPLOYMENT_TEST_SECRET", "must-not-leak")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "must-not-leak")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/must-not-leak")
    monkeypatch.setenv("HOME", str(tmp_path / "untrusted-home"))
    monkeypatch.setenv("LANG", "C.UTF-8")
    runtime = _runtime(tmp_path)
    capability = _capability(tmp_path, runtime)
    source = (
        "import json, os; "
        "print(json.dumps({'cwd': os.getcwd(), "
        "'secret': os.getenv('LLM_DEPLOYMENT_TEST_SECRET'), "
        "'aws': os.getenv('AWS_ACCESS_KEY_ID'), 'ssh': os.getenv('SSH_AUTH_SOCK'), "
        "'home': os.getenv('HOME'), "
        "'lang': os.getenv('LANG'), 'venv': os.getenv('VIRTUAL_ENV'), "
        "'path': os.getenv('PATH'), 'nvm': os.getenv('NVM_DIR')}))"
    )

    result = sys_ops.exec_command(
        f"python -c {json.dumps(source)}",
        session_id=capability.session_id,
        runtime_config=runtime,
        capability=capability,
    )
    payload = json.loads(result)

    assert payload["cwd"] == str(tmp_path)
    assert payload["secret"] is None
    assert payload["aws"] is None
    assert payload["ssh"] is None
    assert payload["home"] == pwd.getpwuid(os.getuid()).pw_dir
    assert payload["lang"] == "C.UTF-8"
    assert payload["venv"] == str(Path(sys.executable).absolute().parent.parent)
    assert payload["path"].split(os.pathsep)[0] == str(
        Path(sys.executable).absolute().parent
    )
    assert payload["nvm"] == str(tmp_path / "nvm")


def test_public_exec_uses_runtime_default_timeout_and_grace(tmp_path):
    runtime = _runtime(
        tmp_path,
        exec_timeout=0.2,
        termination_grace_seconds=0.1,
    )
    started = time.monotonic()
    capability = _capability(tmp_path, runtime)

    result = sys_ops.exec_command(
        "sleep 30",
        session_id=capability.session_id,
        runtime_config=runtime,
        capability=capability,
    )
    elapsed = time.monotonic() - started

    assert "命令执行超时 (0.2 秒)" in result
    assert elapsed < 1.5


def test_public_exec_timeout_does_not_echo_command_arguments(tmp_path):
    runtime = _runtime(
        tmp_path,
        command_mode="argv",
        exec_timeout=0.05,
        termination_grace_seconds=0.05,
    )
    secret = "TOP_SECRET_TIMEOUT_ARGUMENT"
    capability = _capability(tmp_path, runtime)

    result = sys_ops.exec_command(
        f"python -c {json.dumps('import time; time.sleep(30)')} {secret}",
        runtime_config=runtime,
        session_id=capability.session_id,
        capability=capability,
    )

    assert "命令执行超时" in result
    assert secret not in result


def test_public_exec_uses_configured_forced_termination_grace(tmp_path):
    runtime = _runtime(
        tmp_path,
        command_mode="argv",
        exec_timeout=0.15,
        termination_grace_seconds=0.15,
    )
    source = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)"
    started = time.monotonic()
    capability = _capability(tmp_path, runtime)

    result = sys_ops.exec_command(
        f"python -c {json.dumps(source)}", runtime_config=runtime
        , session_id=capability.session_id, capability=capability
    )
    elapsed = time.monotonic() - started

    assert "命令执行超时 (0.15 秒)" in result
    assert 0.25 <= elapsed < 1.5


def test_public_exec_abort_and_nonzero_exit_are_reported(tmp_path):
    runtime = _runtime(tmp_path)
    capability = _capability(tmp_path, runtime)
    started = time.monotonic()

    aborted = sys_ops.exec_command(
        "sleep 30",
        check_abort=lambda: time.monotonic() - started > 0.1,
        runtime_config=runtime,
        session_id=capability.session_id,
        capability=capability,
    )
    nonzero = sys_ops.exec_command(
        "false",
        runtime_config=runtime,
        session_id=capability.session_id,
        capability=capability,
    )

    assert aborted == "命令已被用户中止"
    assert nonzero == "[退出码: 1]"


@pytest.mark.parametrize("trigger", ["timeout", "abort"])
def test_public_exec_cleans_parent_and_descendant_for_timeout_or_abort(
    tmp_path, trigger
):
    pid_file = tmp_path / f"{trigger}-pids"
    script = tmp_path / "process_tree.py"
    script.write_text(
        "import os,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
        "with open(sys.argv[1],'w',encoding='utf-8') as handle:\n"
        " handle.write(f'{os.getpid()}\\n{child.pid}\\n');handle.flush();os.fsync(handle.fileno())\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    runtime = _runtime(
        tmp_path,
        command_mode="argv",
        exec_timeout=0.4 if trigger == "timeout" else 5,
    )
    abort_event = threading.Event()
    capability = _capability(tmp_path, runtime)
    holder = {}

    thread = threading.Thread(
        target=lambda: holder.setdefault(
            "result",
            sys_ops.exec_command(
                    f"python {script} {pid_file}",
                    check_abort=abort_event.is_set,
                    runtime_config=runtime,
                    session_id=capability.session_id,
                    capability=capability,
            ),
        )
    )
    thread.start()
    pids = _wait_for_pids(pid_file, 2)
    assert _living_process_count(pids) == 2
    if trigger == "abort":
        abort_event.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    if trigger == "timeout":
        assert "命令执行超时" in holder["result"]
    else:
        assert holder["result"] == "命令已被用户中止"
    _wait_for_process_count(pids, 0)


def test_public_exec_applies_runtime_output_cap(tmp_path):
    runtime = _runtime(tmp_path, max_output_bytes=48)
    capability = _capability(tmp_path, runtime)

    result = sys_ops.exec_command(
        "printf '%02048d' 0",
        runtime_config=runtime,
        session_id=capability.session_id,
        capability=capability,
    )

    assert result.startswith("0" * 24)
    assert "[输出已截断]" in result
    assert len(result) < 100


def test_public_exec_fails_closed_for_unusable_configured_work_dir(tmp_path):
    unusable = tmp_path / "missing-parent" / "work"
    runtime = _runtime(tmp_path, work_dir=str(unusable))
    capability = _capability(tmp_path, runtime)

    result = sys_ops.exec_command(
        "printf never-runs",
        runtime_config=runtime,
        session_id=capability.session_id,
        capability=capability,
    )

    assert "invalid_work_dir" in result


def test_public_argv_rejects_user_selected_executable_path(tmp_path):
    runtime = _runtime(tmp_path, command_mode="argv")
    capability = _capability(tmp_path, runtime)

    result = sys_ops.exec_command(
        "/usr/bin/printf never-runs",
        runtime_config=runtime,
        session_id=capability.session_id,
        capability=capability,
    )

    assert "executable_not_resolved" in result


def test_public_browser_honors_enablement_and_session_binding(tmp_path):
    nvm_dir = tmp_path / "nvm"
    _install_fake_playwright(nvm_dir)
    enabled = _runtime(
        tmp_path,
        nvm_dir=str(nvm_dir),
        environment_allowlist=("PLAYWRIGHT_CLI_TEST_PREFIX",),
    )
    disabled = _runtime(tmp_path, allow_browser=False)
    enabled_capability = _capability(tmp_path, enabled, session_id="bound-session")
    disabled_capability = _capability(tmp_path, disabled, session_id="disabled-session")
    os.environ["PLAYWRIGHT_CLI_TEST_PREFIX"] = "browser:\n"
    try:
        output = sys_ops.browser_command(
            "snapshot", session_id="bound-session", runtime_config=enabled
            , capability=enabled_capability
        )
        denied = sys_ops.browser_command(
            "snapshot",
            session_id=disabled_capability.session_id,
            runtime_config=disabled,
            capability=disabled_capability,
        )
    finally:
        os.environ.pop("PLAYWRIGHT_CLI_TEST_PREFIX", None)

    assert output.splitlines()[:4] == [
        "browser:",
        "-s",
        "bound-session",
        "snapshot",
    ]
    assert "browser_disabled" in denied


def test_agent_tool_factory_binds_already_parsed_runtime_config(tmp_path):
    runtime = _runtime(tmp_path)
    capability = _capability(tmp_path, runtime, session_id="factory-session")
    agent = SimpleNamespace(**runtime.__dict__)
    config = SimpleNamespace(
        agent=agent,
        mcp=SimpleNamespace(servers=[]),
        web_tools=SimpleNamespace(enable_wikipedia=False),
    )
    tools = get_tools_from_config(config)
    exec_tool = next(tool for tool in tools if tool.name == "exec_command")
    exec_tool._injected_session_id = capability.session_id
    exec_tool._injected_capability = capability

    result = exec_tool.forward(command="pwd")

    assert result == f"{tmp_path}\n"
