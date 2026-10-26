#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/_workspace_paths.sh"

SHARED_WORKSPACE_ROOT="$(get_shared_workspace_root "$PROJECT_ROOT")"
OCR_PYTHON="$(get_shared_paddle_ocr_python "$PROJECT_ROOT")"
OCR_ACTIVATE="$(get_shared_paddle_ocr_activate "$PROJECT_ROOT")"
OCR_HOST="${OCR_HOST:-127.0.0.1}"
OCR_PORT="${OCR_PORT:-8118}"
OCR_URL="http://${OCR_HOST}:${OCR_PORT}"
PIPELINE_CONFIG="${PIPELINE_CONFIG:-$PROJECT_ROOT/config/paddleocr-vl-thor.yaml}"
ATTESTATION_PATH="${OCR_ATTESTATION_PATH:-$SHARED_WORKSPACE_ROOT/logs/paddleocr-runtime-attestation.json}"
OCR_LOG="${OCR_LOG:-$SHARED_WORKSPACE_ROOT/logs/paddleocr-service.log}"
READINESS_TIMEOUT="${OCR_READINESS_TIMEOUT:-900}"
OCR_EXIT_AFTER_CANARY="${OCR_EXIT_AFTER_CANARY:-0}"
OCR_SHUTDOWN_TIMEOUT="${OCR_SHUTDOWN_TIMEOUT:-30}"
OCR_ATTESTATION_MAX_AGE="${OCR_ATTESTATION_MAX_AGE:-86400}"
OCR_START_LOCK_TIMEOUT="${OCR_START_LOCK_TIMEOUT:-900}"
OCR_START_LOCK="${OCR_START_LOCK:-$SHARED_WORKSPACE_ROOT/logs/paddleocr-start.lock}"
OCR_STOPPING_MARKER="${OCR_STOPPING_MARKER:-${OCR_START_LOCK}.stopping}"
OCR_CUDA_MEMORY_LIMIT_GB="${OCR_CUDA_MEMORY_LIMIT_GB:-}"
OCR_MEMORY_REPORT="${OCR_MEMORY_REPORT:-$SHARED_WORKSPACE_ROOT/logs/paddleocr-cuda-memory-limit.json}"
OCR_SERVICE_WRAPPER="${OCR_SERVICE_WRAPPER:-$PROJECT_ROOT/scripts/paddle_ocr_service.py}"
PIPELINE_CONFIG="$(resolve_project_path "$PIPELINE_CONFIG" "$PROJECT_ROOT")"
ATTESTATION_PATH="$(expand_home_path "$ATTESTATION_PATH")"
OCR_LOG="$(expand_home_path "$OCR_LOG")"
OCR_START_LOCK="$(expand_home_path "$OCR_START_LOCK")"
OCR_STOPPING_MARKER="$(expand_home_path "$OCR_STOPPING_MARKER")"
OCR_MEMORY_REPORT="$(expand_home_path "$OCR_MEMORY_REPORT")"
OCR_SERVICE_WRAPPER="$(expand_home_path "$OCR_SERVICE_WRAPPER")"
OCR_PID=""
OCR_START_LOCK_HELD=0

if [ "$OCR_HOST" != "127.0.0.1" ]; then
    echo "ERROR: OCR_HOST must remain loopback-only (127.0.0.1)." >&2
    exit 1
fi
if [ ! -x "$OCR_PYTHON" ] || [ ! -f "$OCR_ACTIVATE" ]; then
    echo "ERROR: locked PaddleOCR environment is missing at $SHARED_WORKSPACE_ROOT/.paddle-ocr" >&2
    exit 1
fi
if [ ! -f "$PIPELINE_CONFIG" ]; then
    echo "ERROR: Pipeline config is missing: $PIPELINE_CONFIG" >&2
    exit 1
fi
if [ -z "$OCR_CUDA_MEMORY_LIMIT_GB" ]; then
    OCR_CUDA_MEMORY_LIMIT_GB="$($OCR_PYTHON - "$PROJECT_ROOT" <<'PYEOF'
import sys
from pathlib import Path

import yaml

root = Path(sys.argv[1])
overall = yaml.safe_load((root / "config/config-overall.yaml").read_text()) or {}
sys.path.insert(0, str(root / "scripts"))
from path_utils import resolve_path

server_path = resolve_path(overall.get("server", "config/config-server.yaml"), root)
server = yaml.safe_load(server_path.read_text()) or {}
value = (server.get("ocr") or {}).get("cuda_memory_limit_gb", 16)
if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
    raise SystemExit("ocr.cuda_memory_limit_gb must be a positive integer")
print(value)
PYEOF
)"
fi
if ! [[ "$OCR_CUDA_MEMORY_LIMIT_GB" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: OCR_CUDA_MEMORY_LIMIT_GB must be a positive integer." >&2
    exit 1
fi
if [ ! -f "$OCR_SERVICE_WRAPPER" ]; then
    echo "ERROR: PaddleOCR CUDA-limited service wrapper is missing: $OCR_SERVICE_WRAPPER" >&2
    exit 1
fi

mkdir -p "$(dirname "$OCR_LOG")" "$(dirname "$ATTESTATION_PATH")" "$(dirname "$OCR_START_LOCK")" "$(dirname "$OCR_MEMORY_REPORT")"
exec 9>"$OCR_START_LOCK"
source "$OCR_ACTIVATE"
PREFLIGHT="$PROJECT_ROOT/scripts/paddle_ocr_preflight.py"
PADDLEX="$SHARED_WORKSPACE_ROOT/.paddle-ocr/bin/paddlex"
if [ ! -x "$PADDLEX" ]; then
    echo "ERROR: PaddleX CLI is missing from the locked OCR environment." >&2
    exit 1
fi

"$OCR_PYTHON" "$PREFLIGHT" check-env --config "$PIPELINE_CONFIG"

port_is_open() {
    "$OCR_PYTHON" - "$OCR_HOST" "$OCR_PORT" <<'PYEOF'
import socket
import sys

with socket.socket() as sock:
    sock.settimeout(0.5)
    raise SystemExit(0 if sock.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)
PYEOF
}

reusable_service_is_active() {
    port_is_open || return 1
    "$OCR_PYTHON" "$PREFLIGHT" reuse \
        --url "$OCR_URL" \
        --attestation "$ATTESTATION_PATH" \
        --config "$PIPELINE_CONFIG" \
        --max-age-seconds "$OCR_ATTESTATION_MAX_AGE" \
        --memory-report "$OCR_MEMORY_REPORT" \
        --cuda-memory-limit-gb "$OCR_CUDA_MEMORY_LIMIT_GB"
}

acquire_start_lock() {
    [ "$OCR_START_LOCK_HELD" = "0" ] || return 0
    if ! flock -w "$OCR_START_LOCK_TIMEOUT" 9; then
        echo "ERROR: Timed out waiting for the serialized PaddleOCR startup lock: $OCR_START_LOCK" >&2
        return 1
    fi
    OCR_START_LOCK_HELD=1
}

release_start_lock() {
    if [ "$OCR_START_LOCK_HELD" = "1" ]; then
        flock -u 9
        OCR_START_LOCK_HELD=0
    fi
}

if [ ! -e "$OCR_STOPPING_MARKER" ] && reusable_service_is_active; then
    echo "Reusing the attested PaddleX layout Pipeline at $OCR_URL; no process was changed."
    exit 0
fi

acquire_start_lock

if reusable_service_is_active; then
    rm -f "$OCR_STOPPING_MARKER"
    release_start_lock
    echo "Reusing the attested PaddleX layout Pipeline at $OCR_URL; no process was changed."
    exit 0
fi
if port_is_open; then
    echo "ERROR: $OCR_URL is occupied by an incompatible or unattested listener; no process was changed." >&2
    exit 1
fi
rm -f "$OCR_STOPPING_MARKER"

owned_process_active() {
    local process_stat remainder state
    [ -n "$OCR_PID" ] || return 1
    kill -0 "$OCR_PID" 2>/dev/null || return 1
    [ -r "/proc/$OCR_PID/stat" ] || return 1
    process_stat="$(<"/proc/$OCR_PID/stat")"
    remainder="${process_stat##*) }"
    state="${remainder%% *}"
    [ "$state" != "Z" ]
}

stop_owned_service() {
    local shutdown_deadline shutdown_status=0
    if owned_process_active; then
        printf '%s\n' "$OCR_PID" >"$OCR_STOPPING_MARKER"
        if ! acquire_start_lock; then
            echo "ERROR: Cannot serialize shutdown of owned PaddleX process $OCR_PID." >&2
            shutdown_status=1
        fi
        echo "Stopping owned PaddleX service process $OCR_PID..."
        kill -TERM "$OCR_PID"
        shutdown_deadline=$((SECONDS + OCR_SHUTDOWN_TIMEOUT))
        while owned_process_active; do
            if [ "$SECONDS" -ge "$shutdown_deadline" ]; then
                echo "ERROR: Owned PaddleX process $OCR_PID did not stop within ${OCR_SHUTDOWN_TIMEOUT}s after TERM." >&2
                shutdown_status=1
                break
            fi
            sleep 0.2
        done
        if ! owned_process_active; then
            wait "$OCR_PID" 2>/dev/null || true
            OCR_PID=""
        fi
    fi
    rm -f "$OCR_STOPPING_MARKER"
    release_start_lock
    return "$shutdown_status"
}
trap stop_owned_service EXIT
trap 'exit 130' INT TERM HUP

echo "Starting official PaddleOCR-VL full Pipeline at $OCR_URL"
rm -f "$OCR_MEMORY_REPORT"
"$OCR_PYTHON" "$OCR_SERVICE_WRAPPER" \
    --cuda-memory-limit-gb "$OCR_CUDA_MEMORY_LIMIT_GB" \
    --memory-report "$OCR_MEMORY_REPORT" \
    --serve \
    --pipeline "$PIPELINE_CONFIG" \
    --engine transformers \
    --device gpu:0 \
    --host "$OCR_HOST" \
    --port "$OCR_PORT" \
    >"$OCR_LOG" 2>&1 9>&- &
OCR_PID=$!

deadline=$((SECONDS + READINESS_TIMEOUT))
until "$OCR_PYTHON" "$PREFLIGHT" probe --url "$OCR_URL" >/dev/null 2>&1; do
    if ! kill -0 "$OCR_PID" 2>/dev/null; then
        wait "$OCR_PID" || status=$?
        echo "ERROR: PaddleX exited during startup (status ${status:-unknown}). See $OCR_LOG" >&2
        exit 1
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
        echo "ERROR: PaddleX did not become ready within ${READINESS_TIMEOUT}s. See $OCR_LOG" >&2
        exit 1
    fi
    sleep 2
done

"$OCR_PYTHON" "$PREFLIGHT" canary \
    --url "$OCR_URL" \
    --attestation "$ATTESTATION_PATH" \
    --config "$PIPELINE_CONFIG" \
    --service-pid "$OCR_PID" \
    --memory-report "$OCR_MEMORY_REPORT" \
    --cuda-memory-limit-gb "$OCR_CUDA_MEMORY_LIMIT_GB"
echo "P0 canaries passed. Attestation: $ATTESTATION_PATH"
if [ "$OCR_EXIT_AFTER_CANARY" = "1" ]; then
    echo "Verification mode complete; stopping only the owned PaddleX service."
    exit 0
fi
release_start_lock
echo "PaddleX service PID $OCR_PID is running; log: $OCR_LOG"
wait "$OCR_PID"
OCR_PID=""
