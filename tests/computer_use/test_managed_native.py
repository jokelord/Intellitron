import os
import socket
import sys

import pytest

from backend.security.environment_builder import ArgvExecutionPlan
from backend.security.process_supervisor import ProcessSupervisor


def test_managed_fd_eof_and_bounded_logs():
    parent, child = socket.socketpair()
    supervisor = ProcessSupervisor(max_output_bytes=64, termination_grace_seconds=.05)
    plan = ArgvExecutionPlan((sys.executable, '-c',
        'import os,sys; print("x"*1000,flush=True); '
        'os.read(int(sys.argv[1]),1)', str(child.fileno())), {}, '/tmp')
    process = supervisor.start_managed(plan, pass_fds=(child.fileno(),), owner_session_id='test-owner')
    child.close()
    parent.close()
    assert process.wait(3) == 0
    assert process.exited.is_set()
    assert len(process.result.stdout.encode()) <= 64
    assert process.result.truncated
    assert not supervisor._active


def test_managed_shutdown_and_reject_new_launches():
    supervisor = ProcessSupervisor(max_output_bytes=64, termination_grace_seconds=.05)
    plan = ArgvExecutionPlan((sys.executable, '-c', 'import time; time.sleep(60)'), {}, '/tmp')
    process = supervisor.start_managed(plan, pass_fds=(), owner_session_id='test-owner')
    supervisor.shutdown()
    assert process.wait(3) != 0
    assert process.exited.is_set()
    with pytest.raises(RuntimeError):
        supervisor.start_managed(plan, pass_fds=(), owner_session_id='test-owner')


def test_managed_abort_reaps_child():
    import threading
    abort = threading.Event()
    supervisor = ProcessSupervisor(max_output_bytes=64, termination_grace_seconds=.05)
    plan = ArgvExecutionPlan((sys.executable, '-c', 'import time; time.sleep(60)'), {}, '/tmp')
    process = supervisor.start_managed(plan, pass_fds=(), owner_session_id='test-owner', check_abort=abort.is_set)
    abort.set()
    process.wait(3)
    assert process.result.aborted
    assert not supervisor._process_group_exists(process.pid)


def test_desktop_helper_denies_fake_capability():
    from types import SimpleNamespace
    from backend.security.command_policy import CommandPolicy
    decision = CommandPolicy().evaluate_desktop_helper(('/tmp/helper', '--control-fd', '3'),
        SimpleNamespace(session_id='owner', allow_desktop_capture=True),
        configured_binary='/tmp/helper', owner_session_id='owner')
    assert not decision.allowed
