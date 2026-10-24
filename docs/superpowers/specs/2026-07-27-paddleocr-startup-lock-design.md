# PaddleOCR Startup Lock Design

## Problem

`scripts/2b_start_OCR_LM.sh` currently acquires
`logs/paddleocr-start.lock` before checking whether a reusable PaddleOCR service
already exists. The launcher then keeps file descriptor 9 open while waiting for
the PaddleX child process, so it holds the startup lock for the service's entire
lifetime. A second invocation cannot reach the reuse check and eventually times
out even though the service on `127.0.0.1:8118` is healthy and its attestation
matches the requested configuration.

The lock must serialize startup decisions, not service ownership.

## Goals

- Return status 0 quickly when a healthy, attested, configuration-matching OCR
  service is already running.
- Serialize genuinely concurrent startup attempts so that only one launcher
  starts PaddleX.
- Preserve all current health, canary, attestation, model-manifest, process
  ownership, and cleanup checks.
- Fix reuse while the current legacy launcher still holds the lifetime lock;
  restarting the healthy OCR service must not be required.

## Non-Goals

- Changing the PaddleOCR pipeline, model, Transformers/CUDA engine, address, or
  port.
- Replacing the launcher with systemd, a PID lease, or another service manager.
- Changing configured startup or health-check timeout values.
- Treating a merely open port or an unmatched attestation as reusable.

## Design

### Reuse Check Before Lock Acquisition

Before opening or waiting on the startup lock, the launcher performs the same
strict reusable-service validation used by the existing locked path. Reuse is
accepted only when the service is healthy and all existing attestation,
configuration, model-manifest, and process checks pass.

If validation passes, the launcher logs that it is reusing the service and exits
with status 0. It does not attach to, wait for, signal, or take ownership of the
existing PaddleX process.

This first check intentionally runs without the startup lock. It is an
optimization and is required to bypass a lock held by a legacy launcher. It is
not sufficient authorization to launch a new process.

The owner writes a sibling `stopping` state marker before intentionally
terminating its PaddleX child. A launcher that observes this marker skips the
unlocked reuse optimization and enters the serialized path. A stale marker is
safe: after acquiring an otherwise free lock, the contender performs the strict
reuse check and clears the stale marker before returning or starting.

### Serialized Double Check

If the first reuse check fails, the launcher waits for the existing bounded
startup lock. After acquiring the lock, it runs the strict reuse check again.
This closes the race where another launcher starts and validates PaddleX while
the contender waits.

If the second check passes, the contender releases the lock and exits with
status 0. If it fails, the lock owner proceeds through the existing stale or
incompatible service handling and startup flow.

### Lock Lifetime

The launcher that starts PaddleX keeps the lock while it:

1. Resolves any existing stale or incompatible process according to current
   policy.
2. Starts the PaddleX child process.
3. Waits for readiness and runs the existing canary validation.
4. Writes and validates the runtime attestation required for later reuse.

Once the service is fully reusable, the launcher explicitly releases file
descriptor 9. It then continues to wait for its PaddleX child exactly as it does
today, but no longer holds the startup lock during that lifetime wait.

Before an owner intentionally stops a live child, its exit cleanup writes the
`stopping` marker and acquires the startup lock if it no longer holds it. The
marker is removed only after the child has stopped. Verification mode keeps the
existing startup lock through its immediate stop, avoiding a release/reacquire
gap. This preserves the rule that another launcher cannot reuse a child already
being stopped.

Any failure before the service becomes reusable follows the existing cleanup
path. Shell exit also closes the descriptor, so unsuccessful startup cannot
leave the kernel lock held.

## Concurrency Outcomes

| State | Later invocation behavior |
| --- | --- |
| Matching service is already reusable | Pre-lock check exits 0 immediately |
| Another launcher is still starting | Wait for bounded lock, then recheck |
| Other launcher completed startup | Locked recheck exits 0 without duplicate launch |
| Other launcher failed startup | Lock waiter becomes the only startup owner |
| Current owner is intentionally stopping | Skip unlocked reuse and wait until stop cleanup releases the lock |
| Healthy service has mismatched or invalid attestation | Do not reuse; retain existing remediation policy |
| Lock wait exceeds configured timeout | Retain the current explicit timeout failure |

## Error Handling

- A failed pre-lock probe is not itself fatal because it may represent an
  absent service or a startup in progress.
- Failure to acquire the lock within the configured timeout remains fatal and
  reports the lock path.
- A failed locked probe does not weaken validation; it enters the existing
  startup/remediation path.
- Startup, canary, attestation, and child-process failures retain their current
  diagnostics and cleanup behavior.

## Testing

Regression coverage will exercise launcher behavior rather than relying only
on source-text assertions:

1. Simulate a matching healthy service while a separate process holds the
   startup lock. The launcher must return 0 within a short bound and must not
   attempt to launch or stop PaddleX.
2. Simulate two contenders where the first holds the lock during startup and
   makes the service reusable before releasing it. The second must wait,
   recheck under lock, return 0, and never launch a duplicate process.
3. Preserve the existing regression that a child in intentional shutdown is
   never reused by a second launcher.
4. Preserve existing startup preflight, attestation, canary, shell syntax, and
   failure-path tests.

## Acceptance Criteria

- Re-running `scripts/2b_start_OCR_LM.sh` against the current healthy service
  returns 0 without waiting for the legacy-held lock.
- A newly started launcher does not retain `paddleocr-start.lock` while waiting
  for the long-lived PaddleX child.
- Concurrent cold starts produce at most one new PaddleX service.
- A launcher never reuses an owned PaddleX child marked for intentional stop.
- Existing OCR health, attestation, and model identity guarantees remain
  unchanged.
