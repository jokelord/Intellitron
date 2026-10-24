"""JSON-only, exec-isolated workers for native web extraction and browsers.

Each worker runs concurrent asyncio requests. Cancellation acknowledges completed
cleanup, not merely receipt; an unresponsive worker and its process group are
reaped before its slot can be reused. No application objects cross this boundary.
"""
from __future__ import annotations

import atexit
import asyncio
import importlib
import io
import json
import logging
import os
import queue
import select
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


logger = logging.getLogger(__name__)
_WORKER_TOKEN_ENV = 'LLM_FETCH_WORKER_TOKEN'


def _kill_detached_descendants(token: str, cleanup_budget: float) -> None:
    """Retire this worker's exec descendants, including Playwright's setsid browser.

    The private token survives reparenting after SIGABRT. Inspect only processes
    of our UID, never log environment data, and signal through a pidfd whose
    /proc identity is verified *after* matching the token to avoid PID reuse.
    """
    expected = f'{_WORKER_TOKEN_ENV}={token}'.encode()
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise FetchWorkerError('safe detached browser cleanup requires Linux pidfd support')
    deadline = time.monotonic() + cleanup_budget
    while True:
        descriptors = []
        try:
            with os.scandir('/proc') as processes:
                entries = list(processes)
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                fd = None
                try:
                    if entry.stat().st_uid != os.getuid():
                        continue
                    pid = int(entry.name)
                    fd = os.pidfd_open(pid)
                    with open(f'{entry.path}/environ', 'rb') as source:
                        if expected not in source.read().split(b'\0'):
                            continue
                    with open(f'/proc/self/fdinfo/{fd}') as source:
                        identity = source.read().splitlines()
                    if not any(line.startswith('Pid:') and line.split()[1] == entry.name
                               for line in identity):
                        continue
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                    descriptors.append(fd)
                    fd = None
                except (ProcessLookupError, FileNotFoundError, PermissionError):
                    continue
                finally:
                    if fd is not None:
                        os.close(fd)
            if not descriptors:
                return
            # Wait for exit readiness, then rescan: a detached process may have
            # forked another marked descendant while this pass was collecting.
            select.select(descriptors, [], [], min(.02, max(0.0, deadline - time.monotonic())))
        finally:
            for fd in descriptors:
                os.close(fd)
        if time.monotonic() >= deadline:
            logger.warning('fetch worker descendants did not finish exiting within cleanup budget')
            return


class FetchWorkerError(RuntimeError):
    pass


class FetchAbortedError(FetchWorkerError):
    pass


@dataclass
class _Request:
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: BaseException | None = None
    operation: str = ''


class _Worker:
    def __init__(self, handler_module: str, cleanup_budget: float):
        self.lock = threading.RLock()
        self.retire_lock = threading.Lock()
        self.pending: dict[str, _Request] = {}
        self.dead = False
        self.outgoing = queue.SimpleQueue()
        self.token = uuid.uuid4().hex
        self.cleanup_budget = cleanup_budget
        environment = os.environ.copy()
        environment[_WORKER_TOKEN_ENV] = self.token
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), '--worker', handler_module],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, bufsize=0,
            env=environment,
            cwd=str(Path(__file__).resolve().parents[2]),
        )
        self.writer = threading.Thread(target=self._write, daemon=True, name='fetch-worker-writer')
        self.reader = threading.Thread(target=self._read, daemon=True, name='fetch-worker-reader')
        self.writer.start()
        self.reader.start()

    def _write(self):
        try:
            while True:
                message = self.outgoing.get()
                if message is None:
                    return
                raw = memoryview(message)
                while raw:
                    sent = self.process.stdin.write(raw)
                    if not sent:
                        raise BrokenPipeError('fetch worker pipe closed')
                    raw = raw[sent:]
        except (OSError, ValueError) as exc:
            self.retire(f'fetch worker input failed: {exc}')

    def _read(self):
        try:
            # Popen pipes are raw for cancellable writes; buffer reads separately
            # to avoid one system call per byte for large extracted documents.
            for line in io.BufferedReader(self.process.stdout):
                message = json.loads(line)
                with self.lock:
                    request = self.pending.pop(message['id'], None)
                    if request is None:
                        continue
                    if message.get('error'):
                        request.error = FetchWorkerError(message['error'])
                    else:
                        request.result = message.get('result')
                    request.done.set()
        except (OSError, ValueError, KeyError) as exc:
            reason = f'fetch worker protocol failed: {exc}'
        else:
            reason = 'fetch worker exited or closed its output'
        self.retire(reason)

    def send(self, message):
        self.outgoing.put((json.dumps(message, ensure_ascii=True, allow_nan=False) + '\n').encode())

    def retire(self, reason):
        # Serialize process-group kill/reap, including races between EOF and timeout.
        with self.retire_lock:
            with self.lock:
                if self.dead:
                    return
                self.dead = True
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()
            try:
                _kill_detached_descendants(self.token, self.cleanup_budget)
            except Exception as exc:
                # /proc or pidfd failures must not strand callers waiting for an
                # ACK forever. Report cleanup failure without an unsafe PID kill.
                reason = f'{reason}; detached cleanup failed: {type(exc).__name__}: {exc}'
                logger.error('fetch worker detached cleanup failed pid=%s error=%s',
                             self.process.pid, exc)
            with self.lock:
                operations = sorted({request.operation for request in self.pending.values()})
            logger.info('fetch worker retired pid=%s returncode=%s operations=%s reason=%s',
                        self.process.pid, self.process.returncode, operations, reason)
            self.outgoing.put(None)
            for pipe in (self.process.stdin, self.process.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass
            with self.lock:
                for request in self.pending.values():
                    request.error = FetchWorkerError(
                        f'{reason} (pid={self.process.pid}, exit={self.process.returncode})')
                    request.done.set()
                self.pending.clear()


class FetchProcessPool:
    def __init__(self, worker_count: int = 4, cancel_grace_seconds: float = 1.0,
                 handler_module: str = 'backend.tools.fetch_worker_runtime'):
        self.worker_count = max(1, int(worker_count))
        self.cancel_grace_seconds = max(0.0, float(cancel_grace_seconds))
        self.handler_module = handler_module
        self._lock = threading.Lock()
        self._workers: list[_Worker] = []
        self._closed = False

    def call(self, operation: str, payload: dict, timeout: float,
             check_abort: Callable | None = None):
        deadline = time.monotonic() + max(0.0, float(timeout))

        def check():
            if check_abort is not None and check_abort():
                raise FetchAbortedError('fetch request aborted')
            if time.monotonic() >= deadline:
                raise TimeoutError('fetch request deadline exceeded')

        check()
        # Validate JSON before reserving a slot; closures/native objects never cross IPC.
        request_id = uuid.uuid4().hex
        message = {'id': request_id, 'operation': operation, 'payload': payload}
        json.dumps(message, allow_nan=False)
        while not self._lock.acquire(timeout=min(.05, max(0.0, deadline - time.monotonic()))):
            check()
        try:
            check()
            if self._closed:
                raise FetchWorkerError('fetch process pool is closed')
            self._workers = [worker for worker in self._workers if not worker.dead]
            if len(self._workers) < self.worker_count:
                worker = _Worker(self.handler_module, self.cancel_grace_seconds)
                self._workers.append(worker)
            else:
                worker = min(self._workers, key=lambda item: len(item.pending))
            request = _Request(operation=operation)
            with worker.lock:
                if worker.dead:
                    raise FetchWorkerError('fetch worker exited during startup')
                worker.pending[request_id] = request
                worker.send(message)
        finally:
            self._lock.release()
        try:
            while True:
                check()
                if request.done.wait(min(.05, max(0.0, deadline - time.monotonic()))):
                    check()
                    if request.error:
                        raise request.error
                    return request.result
        except BaseException:
            if not request.done.is_set():
                worker.send({'id': request_id, 'cancel': True})
                if not request.done.wait(self.cancel_grace_seconds):
                    worker.retire('fetch worker did not acknowledge cancellation cleanup')
            raise

    def close(self):
        with self._lock:
            self._closed = True
            workers, self._workers = self._workers, []
        # Ask healthy workers to close their shared runtime first; then reap all
        # process groups (including descendants surviving normal Python exit).
        for worker in workers:
            worker.send({'shutdown': True})
        deadline = time.monotonic() + self.cancel_grace_seconds
        for worker in workers:
            try:
                worker.process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
            worker.retire('fetch process pool closed')


_pool_lock = threading.Lock()
_pool: FetchProcessPool | None = None


def get_fetch_pool(cfg: dict) -> FetchProcessPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = FetchProcessPool(
                worker_count=cfg.get('fetch_worker_processes', 4),
                cancel_grace_seconds=cfg.get('fetch_worker_cancel_grace_seconds', 1.0),
            )
        return _pool


def shutdown_fetch_pool():
    global _pool
    with _pool_lock:
        instance, _pool = _pool, None
    if instance is not None:
        instance.close()


atexit.register(shutdown_fetch_pool)


async def _serve(handler_module: str, output):
    handler = importlib.import_module(handler_module)
    tasks = {}
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)

    def emit(message):
        output.write((json.dumps(message, ensure_ascii=True, allow_nan=False) + '\n').encode())
        output.flush()

    def completed(request_id, task):
        # A task cancelled before its first instruction never enters a coroutine
        # try/finally. The callback covers that case as well as actual cleanup.
        tasks.pop(request_id, None)
        if task.cancelled():
            emit({'id': request_id, 'error': 'fetch request cancelled'})
        elif task.exception() is not None:
            exc = task.exception()
            emit({'id': request_id, 'error': f'{type(exc).__name__}: {exc}'})
        else:
            emit({'id': request_id, 'result': task.result()})

    try:
        while line := await reader.readline():
            message = json.loads(line)
            if message.get('shutdown'):
                break
            if message.get('cancel'):
                task = tasks.get(message['id'])
                if task is not None:
                    task.cancel()
                continue
            request_id = message['id']
            task = asyncio.create_task(handler.handle(message['operation'], message['payload']))
            tasks[request_id] = task
            task.add_done_callback(lambda done, rid=request_id: completed(rid, done))
    finally:
        for task in list(tasks.values()):
            task.cancel()
        await asyncio.gather(*list(tasks.values()), return_exceptions=True)
        await handler.shutdown()
        transport.close()


if __name__ == '__main__':
    # Save the protocol descriptor before redirecting Python AND native stdout.
    control = os.fdopen(os.dup(sys.stdout.fileno()), 'wb')
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    asyncio.run(_serve(sys.argv[2], control))
