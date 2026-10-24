#!/bin/bash
# Launch and supervise the dedicated Qwen3-ASR SGLang server.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$PROJECT_ROOT/scripts/_workspace_paths.sh"
SHARED_SGLANG_ACTIVATE="$(get_shared_sglang_activate "$PROJECT_ROOT")"
SHARED_SGLANG_PYTHON="$(get_shared_sglang_python "$PROJECT_ROOT")"
YAML_OVERALL="$PROJECT_ROOT/config/config-overall.yaml"

if [ ! -f "$YAML_OVERALL" ]; then
    echo "ASR overall config not found: $YAML_OVERALL"
    exit 1
fi
if [ ! -f "$SHARED_SGLANG_ACTIVATE" ] || [ ! -x "$SHARED_SGLANG_PYTHON" ]; then
    echo "ASR requires the shared .sglang environment: $SHARED_SGLANG_ACTIVATE"
    exit 1
fi
source "$SHARED_SGLANG_ACTIVATE"

if [ -n "${ASR_CONFIG_PATH:-}" ]; then
    ASR_CONFIG="$ASR_CONFIG_PATH"
else
    ASR_CONFIG="$($SHARED_SGLANG_PYTHON - "$PROJECT_ROOT" "$YAML_OVERALL" <<'PYEOF'
import sys
from pathlib import Path
import yaml

root = Path(sys.argv[1])
overall = yaml.safe_load(Path(sys.argv[2]).read_text(encoding="utf-8")) or {}
relative = overall.get("asr")
if not relative:
    raise SystemExit("config-overall.yaml has no asr route")
print(root / relative)
PYEOF
)"
fi

if ! PREFLIGHT_ASSIGNMENTS="$($SHARED_SGLANG_PYTHON "$PROJECT_ROOT/scripts/asr_preflight.py" --config "$ASR_CONFIG" --shell)"; then
    echo "ASR preflight failed; no process was started."
    exit 1
fi
eval "$PREFLIGHT_ASSIGNMENTS"
if [ "$ASR_ENABLED" != "true" ]; then
    echo "ASR is disabled in the typed configuration; no SGLang process was started."
    exit 0
fi

LOG_DIR="${ASR_LOG_DIR:-$PROJECT_ROOT/logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/asr_sglang_$(date +%Y%m%d_%H%M%S).log"

export TRITON_PTXAS_PATH="$PROJECT_ROOT/patch/ptxas_wrapper.sh"
export TRITON_PTXAS_BLACKWELL_PATH="$PROJECT_ROOT/patch/ptxas_wrapper.sh"
export TORCH_CUDA_ARCH_LIST=11.0a
export LD_LIBRARY_PATH="/usr/local/cuda-13.3/lib64:${LD_LIBRARY_PATH:-}"
export CPLUS_INCLUDE_PATH="/usr/local/cuda-13.3/targets/sbsa-linux/include:${CPLUS_INCLUDE_PATH:-}"
export SGLANG_ENABLE_JIT_DEEPGEMM=0

ARGS=(python3 -m sglang.launch_server
    --model-path "$MODEL_PATH"
    --host "$ASR_HOST"
    --port "$ASR_PORT"
    --served-model-name "$SERVED_MODEL_NAME"
    --dtype "$DTYPE"
    --mem-fraction-static "$MEM_FRACTION_STATIC"
    --context-length "$CONTEXT_LENGTH"
    --max-total-tokens "$MAX_TOTAL_TOKENS"
    --max-running-requests "$MAX_RUNNING_REQUESTS"
)
[ "$TRUST_REMOTE_CODE" = "true" ] && ARGS+=(--trust-remote-code)

CHILD_PID=""
CHILD_PGID=""
OWNED_GROUP_VERIFIED=0
STOPPING=0

child_is_running() {
    [ -n "$CHILD_PID" ] || return 1
    kill -0 "$CHILD_PID" 2>/dev/null || return 1
    local state
    state="$(ps -o stat= -p "$CHILD_PID" 2>/dev/null || true)"
    [ -n "$state" ] && [[ "$state" != Z* ]]
}

owned_group_has_live_processes() {
    [ "$OWNED_GROUP_VERIFIED" -eq 1 ] || return 1
    local states
    local state
    states="$(ps -o stat= -g "$CHILD_PGID" 2>/dev/null || true)"
    while IFS= read -r state; do
        state="${state//[[:space:]]/}"
        [ -n "$state" ] && [[ "$state" != Z* ]] && return 0
    done <<< "$states"
    return 1
}

wait_for_owned_group() {
    local timeout_seconds="$1"
    local deadline
    deadline="$($SHARED_SGLANG_PYTHON - "$timeout_seconds" <<'PYEOF'
import sys
import time
print(time.monotonic() + float(sys.argv[1]))
PYEOF
)"
    while owned_group_has_live_processes; do
        if ! "$SHARED_SGLANG_PYTHON" - "$deadline" <<'PYEOF'
import sys
import time
raise SystemExit(0 if time.monotonic() < float(sys.argv[1]) else 1)
PYEOF
        then
            return 1
        fi
        sleep 0.05
    done
    return 0
}

reap_owned_leader_if_stopped() {
    [ -n "$CHILD_PID" ] || return 0
    if ! child_is_running; then
        wait "$CHILD_PID" 2>/dev/null || true
    fi
}

terminate_owned_process_group() {
    [ -n "$CHILD_PID" ] || return 0
    STOPPING=1
    if [ "$OWNED_GROUP_VERIFIED" -ne 1 ]; then
        echo "Refusing process-group cleanup because ASR ownership was not verified."
        return 1
    fi
    if owned_group_has_live_processes; then
        kill -s TERM -- "-$CHILD_PGID" 2>/dev/null || true
    fi
    if ! wait_for_owned_group "$SHUTDOWN_TIMEOUT_SECONDS"; then
        echo "Owned ASR process group did not stop within ${SHUTDOWN_TIMEOUT_SECONDS}s; sending KILL to that same group."
        kill -s KILL -- "-$CHILD_PGID" 2>/dev/null || true
        if ! wait_for_owned_group "$SHUTDOWN_TIMEOUT_SECONDS"; then
            echo "Owned ASR process group did not fully exit after bounded KILL cleanup."
        fi
    fi
    reap_owned_leader_if_stopped
    CHILD_PID=""
    CHILD_PGID=""
    OWNED_GROUP_VERIFIED=0
}

cleanup_on_exit() {
    [ "$STOPPING" -eq 1 ] || terminate_owned_process_group TERM
}

handle_signal() {
    local signal_name="$1"
    local exit_code="$2"
    trap - SIGINT SIGTERM SIGHUP
    echo "Received $signal_name; terminating the owned ASR SGLang process group."
    terminate_owned_process_group
    exit "$exit_code"
}

trap cleanup_on_exit EXIT
trap 'handle_signal INT 130' SIGINT
trap 'handle_signal TERM 143' SIGTERM
trap 'handle_signal HUP 129' SIGHUP

echo "Starting Qwen3-ASR SGLang on ${ASR_HOST}:${ASR_PORT}"
printf '  %q' "${ARGS[@]}"
printf '\n'
setsid "${ARGS[@]}" > >(tee "$LOG_FILE") 2>&1 &
CHILD_PID=$!
ACTUAL_PGID="$(ps -o pgid= -p "$CHILD_PID" 2>/dev/null || true)"
ACTUAL_PGID="${ACTUAL_PGID//[[:space:]]/}"
if [ "$ACTUAL_PGID" != "$CHILD_PID" ]; then
    echo "ASR SGLang process-group ownership verification failed; stopping the owned child."
    kill -s TERM "$CHILD_PID" 2>/dev/null || true
    wait "$CHILD_PID" 2>/dev/null || true
    CHILD_PID=""
    exit 1
fi
CHILD_PGID=$ACTUAL_PGID
OWNED_GROUP_VERIFIED=1

case "$ASR_HOST" in
    0.0.0.0) PROBE_HOST="127.0.0.1" ;;
    ::) PROBE_HOST="[::1]" ;;
    *:*) PROBE_HOST="[$ASR_HOST]" ;;
    *) PROBE_HOST="$ASR_HOST" ;;
esac
BASE_URL="http://${PROBE_HOST}:${ASR_PORT}"
POLL_INTERVAL_SECONDS="${ASR_STARTUP_POLL_INTERVAL_SECONDS:-1}"
STARTUP_DEADLINE="$($SHARED_SGLANG_PYTHON - "$STARTUP_TIMEOUT_SECONDS" "$POLL_INTERVAL_SECONDS" <<'PYEOF'
import math
import sys
import time

timeout = float(sys.argv[1])
interval = float(sys.argv[2])
if not math.isfinite(interval) or interval <= 0:
    raise SystemExit("ASR_STARTUP_POLL_INTERVAL_SECONDS must be positive and finite")
print(time.monotonic() + timeout)
PYEOF
)"
PROBE_BODY_FILE="$(mktemp)"
trap 'rm -f "$PROBE_BODY_FILE"; cleanup_on_exit' EXIT

remaining_seconds() {
    "$SHARED_SGLANG_PYTHON" - "$STARTUP_DEADLINE" "$1" <<'PYEOF'
import sys
import time

remaining = float(sys.argv[1]) - time.monotonic()
cap = float(sys.argv[2])
if remaining <= 0:
    raise SystemExit(1)
print(max(0.001, min(remaining, cap)))
PYEOF
}

probe_http() {
    local url="$1"
    local status
    local curl_status
    local max_time
    max_time="$(remaining_seconds "$HEALTH_TIMEOUT_SECONDS")" || return 1
    set +e
    status="$(curl -sS -o "$PROBE_BODY_FILE" -w '%{http_code}' --max-time "$max_time" "$url" 2>/dev/null)"
    curl_status=$?
    set -e
    if [ "$curl_status" -ne 0 ]; then
        return 1
    fi
    if [ "$status" = "503" ]; then
        echo "ASR SGLang is still starting (HTTP status 503 from $url); retrying."
        return 1
    fi
    if [[ ! "$status" =~ ^2[0-9][0-9]$ ]]; then
        echo "ASR SGLang returned unhealthy HTTP status $status from $url."
        return 2
    fi
    return 0
}

models_identity_report() {
    "$SHARED_SGLANG_PYTHON" - "$SERVED_MODEL_NAME" "$PROBE_BODY_FILE" <<'PYEOF'
import json
import sys
from pathlib import Path

expected = sys.argv[1]
try:
    payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    print("<invalid models response>")
    raise SystemExit(1)
models = payload.get("data") if isinstance(payload, dict) else None
ids = (
    [item.get("id") for item in models if isinstance(item, dict) and isinstance(item.get("id"), str)]
    if isinstance(models, list)
    else []
)
print(", ".join(ids) if ids else "<none>")
raise SystemExit(0 if expected in ids else 1)
PYEOF
}

while remaining_seconds "$POLL_INTERVAL_SECONDS" >/dev/null; do
    if ! child_is_running; then
        set +e
        wait "$CHILD_PID"
        child_status=$?
        set -e
        echo "ASR SGLang exited before readiness (status $child_status)."
        terminate_owned_process_group
        exit 1
    fi

    health_status=0
    probe_http "$BASE_URL/health" || health_status=$?
    if [ "$health_status" -eq 2 ]; then
        exit 1
    fi
    if [ "$health_status" -eq 0 ]; then
        models_status=0
        probe_http "$BASE_URL/v1/models" || models_status=$?
        if [ "$models_status" -eq 2 ]; then
            exit 1
        fi
        if [ "$models_status" -eq 0 ]; then
            set +e
            OBSERVED_MODEL_IDS="$(models_identity_report)"
            identity_status=$?
            set -e
            if [ "$identity_status" -eq 0 ]; then
                echo "ASR SGLang is ready: ${BASE_URL} serves exact model ${SERVED_MODEL_NAME}."
                set +e
                wait "$CHILD_PID"
                child_status=$?
                set -e
                terminate_owned_process_group
                exit "$child_status"
            fi
            echo "ASR SGLang model mismatch: expected exact id ${SERVED_MODEL_NAME}; observed IDs: ${OBSERVED_MODEL_IDS}. Log: ${LOG_FILE}"
            exit 1
        fi
    fi

    remaining_sleep="$(remaining_seconds "$POLL_INTERVAL_SECONDS")" || break
    sleep "$remaining_sleep"
done

echo "ASR SGLang startup timed out after ${STARTUP_TIMEOUT_SECONDS} seconds."
exit 1
