"""Bounded subprocess lifecycle management for agent command execution.

The supervisor deliberately exposes separate argv and shell-compatibility
entry points.  Both launch without ``shell=True``, create a fresh process
session on POSIX, bound captured output, and terminate the complete process
group on timeout, abort, shutdown, or exceptional cleanup.
"""

from __future__ import annotations

import errno
import math
import os
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence


AbortCheck = Callable[[], bool]
PopenFactory = Callable[..., subprocess.Popen[bytes]]
SelectorFactory = Callable[[], selectors.BaseSelector]


@dataclass(frozen=True)
class ProcessResult:
    """Stable result returned for every successfully started process."""

    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool = False
    aborted: bool = False
    truncated: bool = False
    termination_state: str = "exited"
    cleanup_failed: bool = False


@dataclass
class _ActiveProcess:
    process: subprocess.Popen[bytes]
    stop_requested: threading.Event = field(default_factory=threading.Event)
    cleanup_lock: threading.Lock = field(default_factory=threading.Lock)
    termination_reason: str | None = None
    cleanup_failed: bool = False


class _BoundedOutput:
    """Keep useful stream prefixes within one shared byte budget."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.truncated = False

    def add(self, stream_name: str, data: bytes) -> None:
        if not data:
            return
        target = self.stdout if stream_name == "stdout" else self.stderr
        other = self.stderr if stream_name == "stdout" else self.stdout
        available = self.limit - len(self.stdout) - len(self.stderr)

        # If the first stream consumed the budget, reclaim only its tail so the
        # second stream can retain a diagnostic prefix too.  Total retained
        # bytes never exceed the configured combined limit.
        if available == 0 and not target and other:
            desired = min(len(data), max(1, self.limit // 2))
            reclaimed = min(desired, len(other))
            if reclaimed:
                del other[-reclaimed:]
                available = reclaimed
                self.truncated = True

        kept = data[:available]
        target.extend(kept)
        if len(kept) != len(data):
            self.truncated = True

    def decode(self) -> tuple[str, str]:
        return (
            bytes(self.stdout).decode("utf-8", errors="replace"),
            bytes(self.stderr).decode("utf-8", errors="replace"),
        )


class ProcessSupervisor:
    """Run commands inside fresh, bounded process-group lifecycles."""

    def __init__(
        self,
        *,
        max_output_bytes: int,
        termination_grace_seconds: float = 2.0,
        poll_interval_seconds: float = 0.05,
        shell_path: str = "/bin/bash",
        popen_factory: PopenFactory = subprocess.Popen,
        selector_factory: SelectorFactory = selectors.DefaultSelector,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(max_output_bytes, bool)
            or not isinstance(max_output_bytes, int)
            or max_output_bytes <= 0
        ):
            raise ValueError("max_output_bytes must be a positive integer")
        self.max_output_bytes = max_output_bytes
        self.termination_grace_seconds = self._finite_nonnegative(
            termination_grace_seconds, "termination_grace_seconds"
        )
        self.poll_interval_seconds = self._finite_positive(
            poll_interval_seconds, "poll_interval_seconds"
        )
        if not isinstance(shell_path, str) or not shell_path or "\x00" in shell_path:
            raise ValueError("shell_path must be non-empty text")
        self.shell_path = shell_path
        self._popen_factory = popen_factory
        self._selector_factory = selector_factory
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._active: dict[int, _ActiveProcess] = {}
        self._shutdown = False

    @staticmethod
    def _finite_positive(value: object, name: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be finite and positive")
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{name} must be finite and positive") from None
        if not math.isfinite(parsed) or parsed <= 0:
            raise ValueError(f"{name} must be finite and positive")
        return parsed

    @staticmethod
    def _finite_nonnegative(value: object, name: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be finite and non-negative")
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{name} must be finite and non-negative") from None
        if not math.isfinite(parsed) or parsed < 0:
            raise ValueError(f"{name} must be finite and non-negative")
        return parsed

    @staticmethod
    def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
        if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or not argv:
            raise ValueError("argv must be a non-empty string sequence")
        normalized = tuple(argv)
        if any(
            not isinstance(value, str)
            or "\x00" in value
            or (index == 0 and not value)
            for index, value in enumerate(normalized)
        ):
            raise ValueError("argv must be a non-empty string sequence")
        return normalized

    def run_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None,
        env: Mapping[str, str] | None,
        timeout: float,
        check_abort: AbortCheck | None,
    ) -> ProcessResult:
        """Execute an already-authorized argv without a shell parser."""
        return self._run(
            self._validate_argv(argv),
            cwd=cwd,
            env=env,
            timeout=timeout,
            check_abort=check_abort,
        )

    def run_shell_compat(
        self,
        command: str,
        *,
        cwd: str | os.PathLike[str] | None,
        env: Mapping[str, str] | None,
        timeout: float,
        check_abort: AbortCheck | None,
    ) -> ProcessResult:
        """Execute explicitly approved text through a constant shell boundary.

        The first shell parses only this module-owned trampoline.  The approved
        command remains one positional argv value and is interpreted by a clean
        non-login shell.  This is intentionally still shell compatibility, not
        argv isolation.
        """
        if not isinstance(command, str) or not command or "\x00" in command:
            raise ValueError("shell compatibility command must be non-empty text")
        return self._run(
            (
                self.shell_path,
                "--noprofile",
                "--norc",
                "-c",
                'exec "$1" --noprofile --norc -c "$2"',
                "shell-compat-boundary",
                self.shell_path,
                command,
            ),
            cwd=cwd,
            env=env,
            timeout=timeout,
            check_abort=check_abort,
        )

    def shutdown(self) -> None:
        """Reject future launches and terminate all currently supervised groups."""
        with self._lock:
            self._shutdown = True
            active = tuple(self._active.values())
            for entry in active:
                if entry.termination_reason is None:
                    entry.termination_reason = "shutdown"
                entry.stop_requested.set()
        for entry in active:
            self._terminate_process_group(entry)

    def start_managed(self, plan, *, pass_fds: tuple[int, ...], owner_session_id: str,
                      check_abort: AbortCheck | None = None) -> "ManagedProcess":
        """Start an already-authorized persistent plan; caller owns passed FDs.

        This is an internal lifecycle primitive, not a command authorization API.
        Desktop callers must first use the fixed-helper policy and environment builder.
        """
        argv = self._validate_argv(plan.argv)
        if not owner_session_id or not isinstance(owner_session_id, str):
            raise ValueError("owner_session_id is required")
        if not isinstance(pass_fds, tuple) or any(type(fd) is not int or fd < 0 for fd in pass_fds):
            raise ValueError("pass_fds must be a tuple of valid descriptors")
        with self._lock:
            if self._shutdown:
                raise RuntimeError("ProcessSupervisor is shut down")
            process = self._popen_factory(argv, cwd=plan.cwd, env=dict(plan.env),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=False, bufsize=0, start_new_session=True, pass_fds=pass_fds)
            entry = _ActiveProcess(process)
            self._active[id(entry)] = entry
        handle = ManagedProcess(self, entry, owner_session_id, check_abort)
        try:
            handle._thread.start()
        except BaseException:
            self._terminate_process_group(entry)
            self._close_pipe(process.stdout)
            self._close_pipe(process.stderr)
            with self._lock:
                self._active.pop(id(entry), None)
            raise
        return handle

    def _run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str | os.PathLike[str] | None,
        env: Mapping[str, str] | None,
        timeout: float,
        check_abort: AbortCheck | None,
    ) -> ProcessResult:
        timeout_seconds = self._finite_positive(timeout, "timeout")
        with self._lock:
            if self._shutdown:
                raise RuntimeError("ProcessSupervisor is shut down")

        process = self._popen_factory(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.fspath(cwd) if isinstance(cwd, Path) else cwd,
            env=dict(env) if env is not None else None,
            text=False,
            bufsize=0,
            start_new_session=True,
        )
        entry = _ActiveProcess(process=process)
        with self._lock:
            self._active[id(entry)] = entry
            if self._shutdown:
                entry.termination_reason = "shutdown"
                entry.stop_requested.set()

        selector: selectors.BaseSelector | None = None
        capture = _BoundedOutput(self.max_output_bytes)
        deadline = self._monotonic() + timeout_seconds
        drain_deadline: float | None = None
        parent_exit_deadline: float | None = None

        try:
            selector = self._selector_factory()
            self._register_pipe(selector, process.stdout, "stdout")
            self._register_pipe(selector, process.stderr, "stderr")

            while True:
                now = self._monotonic()
                parent_returncode = process.poll()
                if parent_returncode is not None and entry.termination_reason is None:
                    # A descendant can keep the capture pipe open after the
                    # supervised parent has exited.  That is cleanup work,
                    # not a timeout of the command itself.
                    if parent_exit_deadline is None:
                        parent_exit_deadline = now + self.termination_grace_seconds
                    if (
                        selector.get_map()
                        and now >= parent_exit_deadline
                        and drain_deadline is None
                    ):
                        self._terminate_process_group(entry)
                        drain_deadline = self._monotonic() + max(
                            0.1, self.poll_interval_seconds * 2
                        )
                elif entry.termination_reason is None:
                    if check_abort is not None and check_abort():
                        entry.termination_reason = "aborted"
                        entry.stop_requested.set()
                    elif now >= deadline:
                        entry.termination_reason = "timed_out"
                        entry.stop_requested.set()

                if entry.stop_requested.is_set() and drain_deadline is None:
                    self._terminate_process_group(entry)
                    drain_deadline = self._monotonic() + max(
                        0.1, self.poll_interval_seconds * 2
                    )

                wait_for = self.poll_interval_seconds
                if entry.termination_reason is None:
                    boundaries = [deadline - now]
                    if parent_exit_deadline is not None:
                        boundaries.append(parent_exit_deadline - now)
                    if drain_deadline is not None:
                        boundaries.append(drain_deadline - now)
                    wait_for = min(wait_for, max(0.0, min(boundaries)))
                elif drain_deadline is not None:
                    wait_for = min(
                        wait_for, max(0.0, drain_deadline - self._monotonic())
                    )

                events = selector.select(wait_for) if selector.get_map() else []
                for key, _ in events:
                    self._read_pipe(selector, key.fileobj, key.data, capture)

                if process.poll() is not None and not selector.get_map():
                    break
                if drain_deadline is not None and self._monotonic() >= drain_deadline:
                    break

            returncode = self._reap_process(process)
            if entry.termination_reason is None and self._process_group_exists(process.pid):
                self._terminate_process_group(entry)
            stdout, stderr = capture.decode()
            reason = entry.termination_reason
            return ProcessResult(
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                timed_out=reason == "timed_out",
                aborted=reason in {"aborted", "shutdown"},
                truncated=capture.truncated,
                termination_state=reason or "exited",
                cleanup_failed=entry.cleanup_failed,
            )
        except BaseException:
            if entry.termination_reason is None:
                entry.termination_reason = "aborted"
            entry.stop_requested.set()
            self._terminate_process_group(entry)
            raise
        finally:
            if selector is not None:
                self._close_selector(selector)
            self._close_pipe(process.stdout)
            self._close_pipe(process.stderr)
            self._reap_process(process)
            with self._lock:
                self._active.pop(id(entry), None)

    @staticmethod
    def _register_pipe(
        selector: selectors.BaseSelector,
        pipe: object | None,
        stream_name: str,
    ) -> None:
        if pipe is None:
            return
        file_descriptor = pipe.fileno()  # type: ignore[attr-defined]
        os.set_blocking(file_descriptor, False)
        selector.register(pipe, selectors.EVENT_READ, stream_name)

    @staticmethod
    def _read_pipe(
        selector: selectors.BaseSelector,
        pipe: object,
        stream_name: str,
        capture: _BoundedOutput,
    ) -> None:
        try:
            data = os.read(pipe.fileno(), 65_536)  # type: ignore[attr-defined]
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if data:
            capture.add(stream_name, data)
            return
        try:
            selector.unregister(pipe)
        except (KeyError, ValueError):
            pass
        ProcessSupervisor._close_pipe(pipe)

    @staticmethod
    def _close_selector(selector: selectors.BaseSelector) -> None:
        try:
            selector.close()
        except Exception:
            pass

    @staticmethod
    def _close_pipe(pipe: object | None) -> None:
        if pipe is None:
            return
        try:
            pipe.close()  # type: ignore[attr-defined]
        except Exception:
            pass

    @staticmethod
    def _reap_process(process: subprocess.Popen[bytes]) -> int | None:
        try:
            return process.wait(timeout=0)
        except subprocess.TimeoutExpired:
            return process.poll()
        except (ChildProcessError, OSError):
            return process.returncode

    @staticmethod
    def _process_group_exists(process_group_id: int) -> bool:
        if os.name != "posix" or not hasattr(os, "killpg"):
            return False
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _signal_process_group(
        self,
        entry: _ActiveProcess,
        sig: signal.Signals,
    ) -> None:
        process = entry.process
        try:
            if os.name == "posix" and hasattr(os, "killpg"):
                os.killpg(process.pid, sig)
            elif sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except ProcessLookupError:
            return
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return
            entry.cleanup_failed = True

    def _terminate_process_group(self, entry: _ActiveProcess) -> None:
        with entry.cleanup_lock:
            process = entry.process
            self._signal_process_group(entry, signal.SIGTERM)
            graceful_deadline = self._monotonic() + self.termination_grace_seconds
            while self._monotonic() < graceful_deadline:
                if not self._process_group_exists(process.pid):
                    break
                time.sleep(
                    min(
                        self.poll_interval_seconds,
                        max(0.0, graceful_deadline - self._monotonic()),
                    )
                )
            if self._process_group_exists(process.pid):
                self._signal_process_group(entry, signal.SIGKILL)
            try:
                process.wait(timeout=max(0.1, self.poll_interval_seconds * 2))
            except subprocess.TimeoutExpired:
                entry.cleanup_failed = True
            except (ChildProcessError, OSError):
                pass


class ManagedProcess:
    """Persistent child with bounded logs and process-group lifetime ownership."""

    def __init__(self, supervisor, entry, owner_session_id, check_abort):
        self._supervisor, self._entry = supervisor, entry
        self.owner_session_id = owner_session_id
        self.pid = entry.process.pid
        self.exited = threading.Event()
        self.result: ProcessResult | None = None
        self._check_abort = check_abort
        self._thread = threading.Thread(target=self._monitor, daemon=True,
                                        name=f"managed-process-{self.pid}")

    def _monitor(self):
        supervisor, entry = self._supervisor, self._entry
        capture = _BoundedOutput(supervisor.max_output_bytes)
        selector = None
        try:
            selector = supervisor._selector_factory()
            supervisor._register_pipe(selector, entry.process.stdout, "stdout")
            supervisor._register_pipe(selector, entry.process.stderr, "stderr")
            while entry.process.poll() is None:
                if self._check_abort is not None and self._check_abort():
                    entry.termination_reason = "aborted"
                    entry.stop_requested.set()
                if entry.stop_requested.is_set():
                    supervisor._terminate_process_group(entry)
                    break
                for key, _ in selector.select(supervisor.poll_interval_seconds):
                    supervisor._read_pipe(selector, key.fileobj, key.data, capture)
            # Parent exit must also retire descendants, even when they close logs.
            supervisor._terminate_process_group(entry)
            for key, _ in selector.select(0):
                supervisor._read_pipe(selector, key.fileobj, key.data, capture)
        except BaseException:
            entry.termination_reason = "aborted"
            supervisor._terminate_process_group(entry)
        finally:
            if selector is not None:
                supervisor._close_selector(selector)
            supervisor._close_pipe(entry.process.stdout)
            supervisor._close_pipe(entry.process.stderr)
            stdout, stderr = capture.decode()
            self.result = ProcessResult(stdout, stderr, supervisor._reap_process(entry.process),
                aborted=entry.termination_reason in {"aborted", "shutdown"},
                truncated=capture.truncated, termination_state=entry.termination_reason or "exited",
                cleanup_failed=entry.cleanup_failed)
            with supervisor._lock:
                supervisor._active.pop(id(entry), None)
            self.exited.set()

    def wait(self, timeout: float | None = None) -> int | None:
        if not self.exited.wait(timeout):
            raise subprocess.TimeoutExpired(str(self.pid), timeout)
        return self.result.returncode

    def terminate(self) -> None:
        if not self.exited.is_set():
            self._entry.termination_reason = self._entry.termination_reason or "aborted"
            self._entry.stop_requested.set()
            self.wait(self._supervisor.termination_grace_seconds + 5)

    close = terminate


__all__ = ["ProcessResult", "ProcessSupervisor", "ManagedProcess"]
