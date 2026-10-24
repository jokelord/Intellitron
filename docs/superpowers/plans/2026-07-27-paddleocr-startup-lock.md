# PaddleOCR Startup Lock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make repeated PaddleOCR launcher invocations reuse a healthy matching service immediately while retaining serialized cold startup and safe intentional shutdown.

**Architecture:** `scripts/2_start_OCR_LM.sh` will perform a strict reuse probe before waiting on the startup lock, then repeat that probe after acquiring the lock. The owner releases the lock after canary and attestation success but records intentional shutdown with a sibling state marker and performs child termination while holding the startup lock.

**Tech Stack:** Bash, util-linux `flock`, Python subprocess integration tests, pytest.

## Global Constraints

- Keep PaddleOCR on `127.0.0.1:30001` with the official PaddleOCR-VL-1.6 full Pipeline, Transformers, and CUDA.
- Preserve current strict health, attestation, config, model-manifest, process-identity, timeout, and non-destructive ownership checks.
- Do not edit third-party dependency sources.
- Do not run Git commands or create a worktree.
- Implement inline in the current session without sub-agents.

## File Structure

- Modify `tests/test_paddle_ocr_startup.py`: add executable launcher regressions for legacy-lock bypass, locked double-check, and post-canary lock release; retain the existing shutdown race test.
- Modify `scripts/2_start_OCR_LM.sh`: add reusable-service helper, pre-lock and locked checks, bounded lock helpers, stopping marker, and startup-only lock release.
- Reference `docs/superpowers/specs/2026-07-27-paddleocr-startup-lock-design.md`: approved behavioral contract.

---

### Task 1: Lock Lifecycle Regression Tests and Launcher Fix

**Files:**
- Modify: `tests/test_paddle_ocr_startup.py:259-407`
- Modify: `scripts/2_start_OCR_LM.sh:15-158`
- Test: `tests/test_paddle_ocr_startup.py`

**Interfaces:**
- Consumes: existing `paddle_ocr_preflight.py` commands `check-env`, `reuse`, `probe`, and `canary`.
- Produces: launcher status 0 for strict reuse; `OCR_STOPPING_MARKER`, defaulting to `${OCR_START_LOCK}.stopping`; startup lock held only through reusable-service publication and intentional child stop.

- [x] **Step 1: Add a reusable fake launcher fixture and failing behavior tests**

Add a helper that copies the production launcher and workspace helper into a temporary project, symlinks the current Python executable as `.paddle-ocr/bin/python`, and installs these executable fakes:

```python
def _write_fake_launcher_project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    scripts = project / "scripts"
    ocr_bin = project / ".paddle-ocr/bin"
    scripts.mkdir(parents=True)
    ocr_bin.mkdir(parents=True)
    (project / "config").mkdir()
    (project / "config/paddleocr-vl-thor.yaml").write_text(
        "pipeline_name: PaddleOCR-VL-1.6\n"
    )
    (scripts / "2_start_OCR_LM.sh").write_text(START_SCRIPT.read_text())
    (scripts / "_workspace_paths.sh").write_text(WORKSPACE_HELPERS.read_text())
    os.symlink(sys.executable, ocr_bin / "python")
    (ocr_bin / "activate").write_text("")
    (scripts / "paddle_ocr_preflight.py").write_text(
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

command = sys.argv[1]
if command == "reuse":
    Path(os.environ["FAKE_REUSE_MARKER"]).write_text("reused")
    raise SystemExit(int(os.environ.get("FAKE_REUSE_STATUS", "0")))
if command == "canary":
    Path(os.environ["FAKE_CANARY_MARKER"]).write_text("ready")
raise SystemExit(0)
"""
    )
    paddlex = ocr_bin / "paddlex"
    paddlex.write_text(
        """#!/usr/bin/env python3
import os
import signal
import time
from pathlib import Path

Path(os.environ["FAKE_LAUNCH_MARKER"]).write_text("launched")
running = True

def stop(_signum, _frame):
    global running
    running = False

signal.signal(signal.SIGTERM, stop)
while running:
    time.sleep(0.05)
"""
    )
    paddlex.chmod(0o755)
    return scripts / "2_start_OCR_LM.sh", project


def _fake_launcher_env(tmp_path: Path, lock_path: Path, port: int) -> dict[str, str]:
    return {
        **os.environ,
        "OCR_PORT": str(port),
        "OCR_READINESS_TIMEOUT": "3",
        "OCR_SHUTDOWN_TIMEOUT": "3",
        "OCR_START_LOCK_TIMEOUT": "1",
        "OCR_START_LOCK": str(lock_path),
        "OCR_LOG": str(tmp_path / "service.log"),
        "OCR_ATTESTATION_PATH": str(tmp_path / "attestation.json"),
        "FAKE_REUSE_MARKER": str(tmp_path / "reuse-called"),
        "FAKE_CANARY_MARKER": str(tmp_path / "canary-called"),
        "FAKE_LAUNCH_MARKER": str(tmp_path / "launch-called"),
    }


def _unused_loopback_port() -> int:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


def _start_lock_holder(lock_path: Path) -> subprocess.Popen[str]:
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, sys, time; "
                "handle = open(sys.argv[1], 'w'); "
                "fcntl.flock(handle, fcntl.LOCK_EX); "
                "print('ready', flush=True); "
                "time.sleep(30)"
            ),
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline() == "ready\n"
    return holder
```

Add three tests:

```python
def test_healthy_service_bypasses_legacy_lifetime_start_lock(tmp_path: Path) -> None:
    launcher, _ = _write_fake_launcher_project(tmp_path)
    lock_path = tmp_path / "startup.lock"
    holder = _start_lock_holder(lock_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        port = listener.getsockname()[1]
        env = _fake_launcher_env(tmp_path, lock_path, port)
        result = subprocess.run(
            ["bash", launcher], env=env, capture_output=True, text=True, timeout=3
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert "Reusing the attested" in result.stdout
        assert Path(env["FAKE_REUSE_MARKER"]).exists()
        assert not Path(env["FAKE_LAUNCH_MARKER"]).exists()
    finally:
        listener.close()
        holder.terminate()
        holder.wait(timeout=5)


def test_lock_waiter_rechecks_reuse_before_launching(tmp_path: Path) -> None:
    launcher, _ = _write_fake_launcher_project(tmp_path)
    lock_path = tmp_path / "startup.lock"
    holder = _start_lock_holder(lock_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    env = _fake_launcher_env(tmp_path, lock_path, port)
    contender = subprocess.Popen(
        ["bash", launcher], env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True
    )
    try:
        time.sleep(0.2)
        listener.listen()
        holder.terminate()
        holder.wait(timeout=5)
        stdout, stderr = contender.communicate(timeout=5)
        assert contender.returncode == 0, (stdout, stderr)
        assert Path(env["FAKE_REUSE_MARKER"]).exists()
        assert not Path(env["FAKE_LAUNCH_MARKER"]).exists()
    finally:
        listener.close()
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=5)
        if contender.poll() is None:
            contender.terminate()
            contender.wait(timeout=5)


def test_launcher_releases_start_lock_after_canary(tmp_path: Path) -> None:
    launcher, _ = _write_fake_launcher_project(tmp_path)
    lock_path = tmp_path / "startup.lock"
    env = _fake_launcher_env(tmp_path, lock_path, _unused_loopback_port())
    owner = subprocess.Popen(
        ["bash", launcher], env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True
    )
    try:
        deadline = time.monotonic() + 5
        canary = Path(env["FAKE_CANARY_MARKER"])
        while not canary.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert canary.exists()
        assert owner.poll() is None
        probe = subprocess.run(["flock", "-n", str(lock_path), "true"])
        assert probe.returncode == 0
    finally:
        owner.terminate()
        owner.communicate(timeout=5)
```

- [x] **Step 2: Run the new and existing concurrency tests and verify RED**

Run:

```bash
.sglang/bin/python -m pytest \
  tests/test_paddle_ocr_startup.py::test_healthy_service_bypasses_legacy_lifetime_start_lock \
  tests/test_paddle_ocr_startup.py::test_lock_waiter_rechecks_reuse_before_launching \
  tests/test_paddle_ocr_startup.py::test_launcher_releases_start_lock_after_canary \
  tests/test_paddle_ocr_startup.py::test_two_launchers_do_not_reuse_child_being_stopped -q
```

Expected: the healthy-service bypass and post-canary release tests fail against the current lifetime-lock implementation; the existing shutdown test passes.

- [x] **Step 3: Implement strict reuse checks and bounded lock helpers**

In `scripts/2_start_OCR_LM.sh`, move environment activation, preflight setup, `check-env`, `port_is_open`, and reuse logic before the initial `flock`. Add:

```bash
OCR_STOPPING_MARKER="${OCR_STOPPING_MARKER:-${OCR_START_LOCK}.stopping}"

reusable_service_is_active() {
    port_is_open || return 1
    "$OCR_PYTHON" "$PREFLIGHT" reuse \
        --url "$OCR_URL" \
        --attestation "$ATTESTATION_PATH" \
        --config "$PIPELINE_CONFIG" \
        --max-age-seconds "$OCR_ATTESTATION_MAX_AGE"
}

acquire_start_lock() {
    [ "$OCR_START_LOCK_HELD" = "0" ] || return 0
    if ! flock -w "$OCR_START_LOCK_TIMEOUT" 9; then
        echo "ERROR: Timed out waiting for the serialized PaddleOCR startup lock: $OCR_START_LOCK" >&2
        return 1
    fi
    OCR_START_LOCK_HELD=1
}
```

Open file descriptor 9 before the fast check, but do not lock it. If no stopping marker exists and strict reuse succeeds, log and exit 0. Otherwise acquire the lock and repeat strict reuse. Under lock, clear a stale stopping marker after a successful strict reuse. Preserve the incompatible-listener error when the port is open but reuse validation fails.

- [x] **Step 4: Implement startup-only lock lifetime and serialized shutdown**

Keep the lock through startup, readiness, canary, and attestation. In verification mode, exit while still holding it. In normal mode, call `release_start_lock` immediately before logging the running PID and waiting for the child.

Update `stop_owned_service` so an active owned child is handled as follows:

```bash
printf '%s\n' "$OCR_PID" >"$OCR_STOPPING_MARKER"
if ! acquire_start_lock; then
    echo "ERROR: Cannot serialize shutdown of owned PaddleX process $OCR_PID." >&2
    return 1
fi
echo "Stopping owned PaddleX service process $OCR_PID..."
kill -TERM "$OCR_PID"
shutdown_deadline=$((SECONDS + OCR_SHUTDOWN_TIMEOUT))
while owned_process_active; do
    if [ "$SECONDS" -ge "$shutdown_deadline" ]; then
        echo "ERROR: Owned PaddleX process $OCR_PID did not stop within ${OCR_SHUTDOWN_TIMEOUT}s after TERM." >&2
        rm -f "$OCR_STOPPING_MARKER"
        return 1
    fi
    sleep 0.2
done
wait "$OCR_PID" 2>/dev/null || true
OCR_PID=""
rm -f "$OCR_STOPPING_MARKER"
release_start_lock
```

Ensure marker removal and lock release also occur when child shutdown reports an error. A stale marker remains conservative because another launcher skips the unlocked optimization, acquires the free lock, and performs the strict locked check.

- [x] **Step 5: Run focused tests and verify GREEN**

Run the four-test command from Step 2.

Expected: `4 passed`.

- [x] **Step 6: Run complete launcher regression and syntax verification**

Run:

```bash
.sglang/bin/python -m pytest tests/test_paddle_ocr_startup.py -q
bash -n scripts/2_start_OCR_LM.sh
```

Expected: all tests pass and `bash -n` exits 0 without output.

- [x] **Step 7: Verify the current live service bypasses its legacy lock**

Run the updated launcher with a short lock timeout while the current attested service remains running:

```bash
OCR_START_LOCK_TIMEOUT=2 bash scripts/2_start_OCR_LM.sh
```

Expected: status 0 and `Reusing the attested PaddleX layout Pipeline at http://127.0.0.1:30001; no process was changed.` No PaddleX process is stopped or launched.
