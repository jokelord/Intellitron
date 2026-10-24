import concurrent.futures
import os
import threading
import time

import pytest


def pool_class():
    from backend.tools.fetch_process_pool import FetchProcessPool
    return FetchProcessPool


@pytest.fixture
def pool(monkeypatch):
    helper = os.path.join(os.path.dirname(__file__), 'helpers')
    monkeypatch.setenv('PYTHONPATH', helper + os.pathsep + os.environ.get('PYTHONPATH', ''))
    instance = pool_class()(worker_count=1, cancel_grace_seconds=.15, handler_module='fetch_pool_handler')
    yield instance
    instance.close()


def test_calls_reuse_separate_interpreter(pool):
    a = pool.call('echo', {'value': 1}, 3)
    b = pool.call('echo', {'value': 2}, 3)
    assert a['pid'] == b['pid'] != os.getpid()
    assert b['value'] == 2


def test_crash_does_not_kill_parent_and_worker_is_replaced(pool):
    from backend.tools.fetch_process_pool import FetchWorkerError
    old = pool.call('echo', {}, 3)['pid']
    with pytest.raises(FetchWorkerError):
        pool.call('crash', {}, 3)
    assert pool.call('echo', {}, 3)['pid'] != old


def test_hung_worker_is_reaped_and_replaced(pool):
    old = pool.call('echo', {}, 3)['pid']
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        pool.call('hang', {}, .15)
    assert time.monotonic() - start < 2
    with pytest.raises(ProcessLookupError):
        os.kill(old, 0)
    assert pool.call('echo', {}, 3)['pid'] != old


def test_target_abort_preserves_other_task(pool):
    from backend.tools.fetch_process_pool import FetchAbortedError
    pool.call('echo', {}, 3)
    aborted = threading.Event()
    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        other = executor.submit(pool.call, 'echo', {'delay': .5, 'value': 'other'}, 3)
        target = executor.submit(pool.call, 'echo', {'delay': 5}, 3, aborted.is_set)
        time.sleep(.1)
        aborted.set()
        with pytest.raises(FetchAbortedError):
            target.result(2)
        assert other.result(2)['value'] == 'other'


def test_stdout_cannot_corrupt_protocol(pool):
    assert pool.call('noisy', {'value': 'ok'}, 3)['value'] == 'ok'


def test_checker_exception_cancels_and_pool_remains_usable(pool):
    def checker():
        raise LookupError('stop checker')
    with pytest.raises(LookupError, match='stop checker'):
        pool.call('echo', {'delay': 5}, 3, checker)
    assert pool.call('echo', {}, 3)


def test_close_unblocks_pending_and_rejects_calls(pool):
    from backend.tools.fetch_process_pool import FetchWorkerError
    with concurrent.futures.ThreadPoolExecutor(1) as executor:
        result = executor.submit(pool.call, 'echo', {'delay': 5}, 10)
        time.sleep(.15)
        pool.close()
        with pytest.raises(FetchWorkerError):
            result.result(2)
    with pytest.raises(FetchWorkerError):
        pool.call('echo', {}, 3)


def test_import_failure_is_bounded():
    from backend.tools.fetch_process_pool import FetchWorkerError
    instance = pool_class()(handler_module='nonexistent_fetch_handler')
    try:
        with pytest.raises(FetchWorkerError):
            instance.call('echo', {}, 2)
    finally:
        instance.close()


def test_timeout_waits_for_cleanup_ack(pool, tmp_path):
    pool.call('echo', {}, 3)
    marker = tmp_path / 'cleanup.txt'
    with pytest.raises(TimeoutError):
        pool.call('cleanup', {'marker': str(marker)}, .1)
    assert marker.read_text() == 'cleaned'


def test_deadline_covers_worker_queue(pool):
    pool.call('echo', {}, 3)
    with concurrent.futures.ThreadPoolExecutor(1) as executor:
        hung = executor.submit(pool.call, 'hang', {}, 10)
        time.sleep(.1)
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            pool.call('echo', {}, .1)
        assert time.monotonic() - start < 1
        from backend.tools.fetch_process_pool import FetchWorkerError
        with pytest.raises(FetchWorkerError):
            hung.result(2)


def test_large_payload_roundtrip_does_not_block_parent(pool):
    value = 'x' * 2000000
    assert pool.call('echo', {'value': value}, 5)['value'] == value


def test_zero_timeout_does_not_start_process(pool):
    with pytest.raises(TimeoutError):
        pool.call('echo', {}, 0)
    assert pool._workers == []


def test_retirement_kills_descendant_processes(pool):
    import psutil
    child = pool.call('child', {}, 3)['child_pid']
    with pytest.raises(TimeoutError):
        pool.call('hang', {}, .1)
    assert not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE


def test_cancel_before_coroutine_first_runs_is_acknowledged(pool):
    import json
    from backend.tools.fetch_process_pool import _Request
    pid = pool.call('echo', {}, 3)['pid']
    worker = pool._workers[0]
    request = _Request()
    worker.pending['prestart'] = request
    # One pipe write puts both lines in the StreamReader buffer. Processing the
    # cancel does not yield to the newly created handler coroutine first.
    lines = [{'id': 'prestart', 'operation': 'echo', 'payload': {'delay': 5}},
             {'id': 'prestart', 'cancel': True}]
    worker.outgoing.put((''.join(json.dumps(item) + '\n' for item in lines)).encode())
    assert request.done.wait(.5), 'pre-start cancellation must acknowledge cleanup'
    assert pool.call('echo', {}, 3)['pid'] == pid


@pytest.mark.parametrize('operation', ['crash', 'hang', 'close'])
def test_retirement_kills_detached_descendants_after_crash_or_timeout(pool, operation):
    import psutil
    from backend.tools.fetch_process_pool import FetchWorkerError
    child = pool.call('detached_child', {}, 3)['child_pid']
    assert os.getpgid(child) == child
    try:
        if operation == 'close':
            pool.close()
        else:
            with pytest.raises((FetchWorkerError, TimeoutError)):
                pool.call(operation, {}, .15)
        assert not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE
        if operation != 'close':
            assert pool.call('echo', {}, 3)
    finally:
        # Red-phase cleanup must never leave the intentionally detached fixture alive.
        try: os.kill(child, 9)
        except ProcessLookupError: pass


def test_detached_cleanup_does_not_touch_another_workers_processes(pool):
    import psutil
    from backend.tools.fetch_process_pool import FetchProcessPool, FetchWorkerError
    other = FetchProcessPool(worker_count=1, cancel_grace_seconds=.15,
                             handler_module='fetch_pool_handler')
    try:
        owned = pool.call('detached_child', {}, 3)['child_pid']
        unrelated = other.call('detached_child', {}, 3)['child_pid']
        with pytest.raises(FetchWorkerError):
            pool.call('crash', {}, 3)
        assert not psutil.pid_exists(owned) or psutil.Process(owned).status() == psutil.STATUS_ZOMBIE
        assert psutil.Process(unrelated).status() != psutil.STATUS_ZOMBIE
        assert other.call('echo', {'value': 'alive'}, 3)['value'] == 'alive'
    finally:
        other.close()


def test_proc_cleanup_error_still_notifies_pending_callers(pool, monkeypatch):
    import backend.tools.fetch_process_pool as supervisor
    def unavailable(*args):
        raise OSError('proc temporarily unavailable')
    pool.call('echo', {}, 3)
    monkeypatch.setattr(supervisor, '_kill_detached_descendants', unavailable)
    with pytest.raises(supervisor.FetchWorkerError, match='detached cleanup failed'):
        pool.call('crash', {}, 3)
    assert pool.call('echo', {'value': 'recovered'}, 3)['value'] == 'recovered'
