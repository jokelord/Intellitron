from __future__ import annotations

import os
import signal
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from backend.security.process_supervisor import ProcessResult, ProcessSupervisor


pytestmark = pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "killpg"),
    reason="process-group lifecycle tests require POSIX os.killpg support",
)


def _python_argv(source: str, *args: str) -> tuple[str, ...]:
    return (sys.executable, "-c", source, *args)


def _wait_for_pid_file(path: Path, count: int, timeout: float = 3.0) -> list[int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            values = [int(value) for value in path.read_text().splitlines()]
        except (FileNotFoundError, ValueError):
            values = []
        if len(values) == count:
            return values
        time.sleep(0.01)
    pytest.fail(f"process pid file did not contain {count} pids")


def _wait_until_process_gone(pid: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    pytest.fail(f"process {pid} survived supervised cleanup")


_PROCESS_TREE_SOURCE = r"""
import os
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    handle.write(f"{os.getpid()}\n{child.pid}\n")
    handle.flush()
    os.fsync(handle.fileno())
time.sleep(30)
"""


def test_timeout_terminates_the_spawned_process_group_and_descendant(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "timeout-pids"
    supervisor = ProcessSupervisor(max_output_bytes=4096, termination_grace_seconds=0.1)

    result = supervisor.run_argv(
        _python_argv(_PROCESS_TREE_SOURCE, str(pid_file)),
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout=0.2,
        check_abort=None,
    )

    parent_pid, child_pid = _wait_for_pid_file(pid_file, 2)
    assert result.timed_out is True
    assert result.aborted is False
    assert result.termination_state == "timed_out"
    assert result.returncode is not None
    _wait_until_process_gone(parent_pid)
    _wait_until_process_gone(child_pid)


def test_targeted_abort_terminates_the_spawned_process_group_and_descendant(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "abort-pids"
    supervisor = ProcessSupervisor(max_output_bytes=4096, termination_grace_seconds=0.1)
    started_at = time.monotonic()

    result = supervisor.run_argv(
        _python_argv(_PROCESS_TREE_SOURCE, str(pid_file)),
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout=10,
        check_abort=lambda: time.monotonic() - started_at >= 0.2,
    )

    parent_pid, child_pid = _wait_for_pid_file(pid_file, 2)
    assert result.timed_out is False
    assert result.aborted is True
    assert result.termination_state == "aborted"
    _wait_until_process_gone(parent_pid)
    _wait_until_process_gone(child_pid)


def test_output_capture_is_combined_bounded_and_reports_truncation() -> None:
    limit = 1024
    supervisor = ProcessSupervisor(max_output_bytes=limit)
    source = r"""
import os
os.write(1, b"stdout-prefix\n" + b"A" * 8192)
os.write(2, b"stderr-prefix\n" + b"B" * 8192)
"""

    result = supervisor.run_argv(
        _python_argv(source),
        cwd=None,
        env=os.environ.copy(),
        timeout=5,
        check_abort=None,
    )

    captured_size = len(result.stdout.encode("utf-8")) + len(
        result.stderr.encode("utf-8")
    )
    assert result.returncode == 0
    assert result.truncated is True
    assert captured_size <= limit
    assert result.stdout.startswith("stdout-prefix\n")
    assert result.stderr.startswith("stderr-prefix\n")


@pytest.mark.parametrize(
    ("exit_code", "expected_state"),
    [(0, "exited"), (7, "exited")],
)
def test_normal_and_nonzero_exit_codes_are_stable(
    exit_code: int, expected_state: str
) -> None:
    supervisor = ProcessSupervisor(max_output_bytes=4096)

    result = supervisor.run_argv(
        _python_argv(
            "import sys; print('out'); print('err', file=sys.stderr); "
            f"raise SystemExit({exit_code})"
        ),
        cwd=None,
        env=os.environ.copy(),
        timeout=5,
        check_abort=None,
    )

    assert isinstance(result, ProcessResult)
    assert result.returncode == exit_code
    assert result.termination_state == expected_state
    assert result.timed_out is False
    assert result.aborted is False
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"


def test_parent_exit_with_inherited_pipe_is_not_misreported_as_timeout(tmp_path: Path) -> None:
    supervisor = ProcessSupervisor(
        max_output_bytes=4096,
        termination_grace_seconds=0.1,
        poll_interval_seconds=0.01,
    )
    source = (
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "print(child.pid, flush=True); time.sleep(0.05)"
    )
    started_at = time.monotonic()

    result = supervisor.run_argv(
        _python_argv(source),
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout=5,
        check_abort=None,
    )
    elapsed = time.monotonic() - started_at

    child_pid = int(result.stdout.strip())
    assert result.returncode == 0
    assert result.timed_out is False
    assert result.aborted is False
    assert result.termination_state == "exited"
    assert elapsed < 1.5
    _wait_until_process_gone(child_pid)


def test_shell_compatibility_is_explicit_and_supervised() -> None:
    supervisor = ProcessSupervisor(max_output_bytes=4096)

    result = supervisor.run_shell_compat(
        "printf shell-compatible",
        cwd=None,
        env=os.environ.copy(),
        timeout=5,
        check_abort=None,
    )

    assert result.returncode == 0
    assert result.stdout == "shell-compatible"
    assert result.stderr == ""
    assert result.termination_state == "exited"


def test_shell_compatibility_timeout_cleans_its_process_tree(tmp_path: Path) -> None:
    pid_file = tmp_path / "shell-timeout-pids"
    supervisor = ProcessSupervisor(max_output_bytes=4096, termination_grace_seconds=0.1)

    result = supervisor.run_shell_compat(
        shlex.join(_python_argv(_PROCESS_TREE_SOURCE, str(pid_file))),
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout=0.2,
        check_abort=None,
    )

    parent_pid, child_pid = _wait_for_pid_file(pid_file, 2)
    assert result.timed_out is True
    assert result.termination_state == "timed_out"
    _wait_until_process_gone(parent_pid)
    _wait_until_process_gone(child_pid)


def test_configured_timeout_and_cleanup_grace_bound_forced_termination() -> None:
    supervisor = ProcessSupervisor(
        max_output_bytes=4096,
        termination_grace_seconds=0.15,
        poll_interval_seconds=0.01,
    )
    source = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(30)"
    )
    started_at = time.monotonic()

    result = supervisor.run_argv(
        _python_argv(source),
        cwd=None,
        env=os.environ.copy(),
        timeout=0.15,
        check_abort=None,
    )
    elapsed = time.monotonic() - started_at

    assert result.timed_out is True
    assert 0.25 <= elapsed < 1.5
    assert result.returncode == -signal.SIGKILL


def test_shutdown_aborts_a_running_process_and_prevents_descendant_leaks(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "shutdown-pids"
    supervisor = ProcessSupervisor(max_output_bytes=4096, termination_grace_seconds=0.1)
    holder: dict[str, ProcessResult] = {}

    thread = threading.Thread(
        target=lambda: holder.setdefault(
            "result",
            supervisor.run_argv(
                _python_argv(_PROCESS_TREE_SOURCE, str(pid_file)),
                cwd=tmp_path,
                env=os.environ.copy(),
                timeout=10,
                check_abort=None,
            ),
        )
    )
    thread.start()
    parent_pid, child_pid = _wait_for_pid_file(pid_file, 2)

    supervisor.shutdown()
    thread.join(timeout=3)

    assert thread.is_alive() is False
    assert holder["result"].aborted is True
    assert holder["result"].termination_state == "shutdown"
    _wait_until_process_gone(parent_pid)
    _wait_until_process_gone(child_pid)


def test_selector_setup_failure_closes_pipes_and_cleans_started_process() -> None:
    holder: dict[str, subprocess.Popen[bytes]] = {}

    def capture_popen(*args, **kwargs):
        process = subprocess.Popen(*args, **kwargs)
        holder["process"] = process
        return process

    def fail_selector_setup():
        raise RuntimeError("selector setup failed")

    supervisor = ProcessSupervisor(
        max_output_bytes=4096,
        termination_grace_seconds=0.1,
        popen_factory=capture_popen,
        selector_factory=fail_selector_setup,
    )

    with pytest.raises(RuntimeError, match="selector setup failed"):
        supervisor.run_argv(
            _python_argv("import time; time.sleep(30)"),
            cwd=None,
            env=os.environ.copy(),
            timeout=5,
            check_abort=None,
        )

    process = holder["process"]
    assert process.stdout is not None and process.stdout.closed
    assert process.stderr is not None and process.stderr.closed
    _wait_until_process_gone(process.pid)
