"""Bounded launch support; this module never opens the local-memory collection."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import signal
import subprocess
import tempfile
import time

from backend.tools.start_backend_searxng import (
    classify_searxng_failure_message,
    get_searxng_bind_port,
    should_manage_searxng_locally,
)


def atomic_json(path: Path, data: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def release_gate(path: Path, nonce: str) -> None:
    """Publish only in a private, launch-owned directory using atomic replacement."""
    directory = path.parent
    info = directory.lstat()
    if directory.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('startup gate requires a private owned directory')
    fd, temporary = tempfile.mkstemp(prefix='.gate-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(nonce + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _kill_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


class BoundedCommands:
    def __init__(self, receipt: Path, state: dict, budget: float):
        self.receipt = receipt
        self.state = state
        self.deadline = time.monotonic() + budget
        self.active: subprocess.Popen | None = None

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('SearXNG startup attempt exceeded its configured budget')
        return remaining

    def run(self, args: list[str], *, cwd: Path | None = None, capture=False) -> subprocess.CompletedProcess:
        budget = self.remaining()
        self.active = subprocess.Popen(args, cwd=cwd, start_new_session=True,
                                       stdout=subprocess.PIPE if capture else None,
                                       stderr=subprocess.PIPE if capture else None, text=True)
        self.state['active_pgid'] = self.active.pid
        atomic_json(self.receipt, self.state)
        try:
            stdout, stderr = self.active.communicate(timeout=budget)
            return subprocess.CompletedProcess(args, self.active.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            self.cancel()
            raise TimeoutError('SearXNG operation exceeded its configured budget') from None
        finally:
            self.active = None
            self.state.pop('active_pgid', None)
            atomic_json(self.receipt, self.state)

    def cancel(self) -> None:
        if self.active is None:
            return
        _kill_group(self.active.pid, signal.SIGTERM)
        try:
            self.active.wait(timeout=.2)
        except subprocess.TimeoutExpired:
            pass
        # Terminate descendants even when their immediate parent already exited.
        _kill_group(self.active.pid, signal.SIGKILL)
        try:
            self.active.wait(timeout=.2)
        except subprocess.TimeoutExpired:
            pass


def _compose_command(commands: BoundedCommands, deploy: Path) -> list[str]:
    if (deploy / 'dc').is_file() and os.access(deploy / 'dc', os.X_OK):
        return ['./dc']
    if commands.run(['docker', 'compose', 'version'], capture=True).returncode == 0:
        return ['docker', 'compose']
    if shutil.which('docker-compose'):
        return ['docker-compose']
    raise RuntimeError('Docker Compose unavailable')


def attempt_searxng(receipt: Path, deploy: Path, base_url: str, budget: float, enabled: bool) -> int:
    state = {'owned': False, 'status': 'dispatched', 'worker_pgid': os.getpgrp()}
    atomic_json(receipt, state)
    commands = BoundedCommands(receipt, state, budget)

    def terminate(_signal, _frame):
        commands.cancel()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        if not enabled or not should_manage_searxng_locally(base_url):
            state['status'] = 'skipped'
            return 0
        if commands.run(['curl', '-fsS', '--max-time', str(min(3, commands.remaining())), f'{base_url.rstrip("/")}/search?q=ping&format=json'], capture=True).returncode == 0:
            state['status'] = 'external'
            print(f'✅ 检测到已运行的 SearXNG 实例: {base_url}', flush=True)
            return 0
        if not shutil.which('docker') or commands.run(['docker', 'info'], capture=True).returncode != 0:
            raise RuntimeError('Docker unavailable')
        compose = _compose_command(commands, deploy)
        # A pre-existing project can be unhealthy; it is still externally owned.
        existing = commands.run(compose + ['ps', '-a', '-q'], cwd=deploy, capture=True)
        if existing.returncode != 0:
            raise RuntimeError('could not establish SearXNG ownership')
        if (existing.stdout or '').strip():
            state['status'] = 'external'
            return 0
        port = get_searxng_bind_port(base_url)
        listener = commands.run(['lsof', '-Pi', f':{port}', '-sTCP:LISTEN', '-t'], capture=True)
        if listener.returncode == 0:
            raise RuntimeError(f'SearXNG port {port} occupied')
        for template, target in [('.env.example', '.env'), ('core-config/settings.yml.example', 'core-config/settings.yml')]:
            if not (deploy / target).exists():
                shutil.copyfile(deploy / template, deploy / target)
        if not (deploy / 'docker-compose.yml').is_file():
            raise RuntimeError('SearXNG compose file unavailable')
        # Persist ownership before up so even partial creation is cleaned on failure.
        state.update(owned=True, compose=compose, deploy=str(deploy), status='starting')
        atomic_json(receipt, state)
        if commands.run(compose + ['up', '-d', '--pull', 'always'], cwd=deploy).returncode:
            raise RuntimeError('SearXNG container startup failed')
        while True:
            if commands.run(['curl', '-fsS', '--max-time', str(min(3, commands.remaining())), f'{base_url.rstrip("/")}/search?q=ping&format=json'], capture=True).returncode == 0:
                state['status'] = 'ready'
                print(f'✅ SearXNG 已可用: {base_url}', flush=True)
                return 0
            time.sleep(min(.5, commands.remaining()))
    except (OSError, RuntimeError, TimeoutError) as exc:
        state['status'] = 'degraded'
        print(classify_searxng_failure_message(str(exc)), flush=True)
        return 1
    finally:
        atomic_json(receipt, state)


def cleanup_searxng(receipt: Path, budget: float) -> None:
    try:
        state = json.loads(receipt.read_text())
    except (OSError, ValueError):
        return
    if not state.get('owned'):
        return
    commands = BoundedCommands(receipt, state, budget)
    try:
        commands.run(state['compose'] + ['down'], cwd=Path(state['deploy']), capture=True)
    except (OSError, RuntimeError, TimeoutError):
        print('⚠️ SearXNG cleanup exceeded its bounded attempt; inspect the local stack manually.', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['init', 'release', 'attempt', 'cleanup'])
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--deploy', type=Path)
    parser.add_argument('--base-url')
    parser.add_argument('--budget', type=float)
    parser.add_argument('--enabled', default='1')
    parser.add_argument('--nonce')
    args = parser.parse_args()
    if args.action == 'init':
        nonce = secrets.token_urlsafe(32)
        atomic_json(args.directory / 'searxng.json', {'owned': False, 'status': 'pending'})
        print('STARTUP_NONCE=' + shlex.quote(nonce))
    elif args.action == 'release':
        release_gate(args.directory / 'memory.gate', args.nonce)
    elif args.action == 'attempt':
        return attempt_searxng(args.receipt, args.deploy, args.base_url, args.budget, args.enabled == '1')
    else:
        cleanup_searxng(args.receipt, args.budget)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
