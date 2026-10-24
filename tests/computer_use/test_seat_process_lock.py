from __future__ import annotations

import subprocess
import sys
import select
from pathlib import Path

import pytest


def test_probe_and_production_dispatcher_share_seat_lock_across_processes(tmp_path, monkeypatch):
    from backend.computer_use import dispatch
    from scripts.probe_computer_use_skill import ProbeError, _seat_transaction

    path = tmp_path / "seat.lock"
    monkeypatch.setattr(dispatch, "_seat_lock_path", lambda: path)
    project_root = Path(__file__).resolve().parents[2]
    child = """
import asyncio, sys
from pathlib import Path
from backend.computer_use import dispatch
dispatch._seat_lock_path = lambda: Path(sys.argv[1])
async def main():
    async with dispatch._SeatGuard():
        print("production-locked", flush=True)
        sys.stdin.readline()
asyncio.run(main())
"""
    process = subprocess.Popen(
        [sys.executable, "-c", child, str(path)], cwd=project_root,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # The probe owns the in-process and flock locks. Production in another
        # Python process must remain blocked until both are released.
        with _seat_transaction(timeout_seconds=1):
            assert not select.select([process.stdout], [], [], 0.1)[0]

        assert select.select([process.stdout], [], [], 5)[0]
        assert process.stdout.readline().strip() == "production-locked"
        # Reverse direction: the production async guard now owns the lock, so
        # the standalone probe must fail closed instead of interleaving.
        with pytest.raises(ProbeError, match="INPUT_SEAT_BUSY"):
            with _seat_transaction(timeout_seconds=0.05):
                pytest.fail("probe entered while production owns seat lock")
    finally:
        if process.poll() is None:
            if process.stdin is not None:
                process.stdin.write("release\n")
                process.stdin.flush()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        stderr = process.stderr.read() if process.stderr is not None else ""
        assert not stderr
