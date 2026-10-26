#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/_workspace_paths.sh"

PADDLEX_PID=""
PADDLEX_PGID=""
PADDLEX_SID=""
PADDLEX_BOOT_ID=""
PADDLEX_START_TICKS=""
PADDLEX_EXE=""
PADDLEX_CMD_HASH=""
PADDLEX_GROUP_GONE=0
PADDLEX_MEMBER_IDENTITIES=""
SGLANG_PID=""
SGLANG_PGID=""
SGLANG_SID=""
SGLANG_BOOT_ID=""
SGLANG_START_TICKS=""
SGLANG_EXE=""
SGLANG_CMD_HASH=""
SGLANG_GROUP_GONE=0
SGLANG_MEMBER_IDENTITIES=""
LOCK_HELD=0
CLEANUP_RUNNING=0
CLEANUP_DONE=0

process_stat_fields() {
    local pid="$1" stat remainder
    [ -r "/proc/$pid/stat" ] || return 1
    stat="$(<"/proc/$pid/stat")"
    remainder="${stat##*) }"
    read -r -a PROCESS_STAT_FIELDS <<< "$remainder"
    [ "${#PROCESS_STAT_FIELDS[@]}" -ge 20 ]
}

process_command_hash() {
    local pid="$1" digest
    [ -r "/proc/$pid/cmdline" ] || return 1
    digest="$(sha256sum "/proc/$pid/cmdline")" || return 1
    printf '%s\n' "${digest%% *}"
}

record_component_identity() {
    local component="$1" pid="$2" prefix boot_id executable command_hash pgid sid start_ticks
    prefix="${component^^}"
    [ -d "/proc/$pid" ] || return 1
    process_stat_fields "$pid" || return 1
    pgid="${PROCESS_STAT_FIELDS[2]}"
    sid="${PROCESS_STAT_FIELDS[3]}"
    start_ticks="${PROCESS_STAT_FIELDS[19]}"
    boot_id="$(< /proc/sys/kernel/random/boot_id)"
    executable="$(readlink -f "/proc/$pid/exe")" || return 1
    command_hash="$(process_command_hash "$pid")" || return 1
    printf -v "${prefix}_PID" '%s' "$pid"
    printf -v "${prefix}_PGID" '%s' "$pgid"
    printf -v "${prefix}_SID" '%s' "$sid"
    printf -v "${prefix}_BOOT_ID" '%s' "$boot_id"
    printf -v "${prefix}_START_TICKS" '%s' "$start_ticks"
    printf -v "${prefix}_EXE" '%s' "$executable"
    printf -v "${prefix}_CMD_HASH" '%s' "$command_hash"
    printf -v "${prefix}_GROUP_GONE" '%s' "0"
}

record_or_report_early_exit() {
    local component="$1" pid="$2" prefix exited=0 status deadline
    prefix="${component^^}"
    printf -v "${prefix}_PID" '%s' "$pid"
    # setsid establishes PGID=SID=spawn PID; retain that ownership even if the
    # direct leader exits before /proc identity capture completes.
    printf -v "${prefix}_PGID" '%s' "$pid"
    printf -v "${prefix}_SID" '%s' "$pid"
    printf -v "${prefix}_GROUP_GONE" '%s' "0"
    printf -v "${prefix}_MEMBER_IDENTITIES" '%s' ""
    deadline=$((SECONDS + 1))
    while true; do
        if record_component_identity "$component" "$pid"; then
            snapshot_component_group_members_unchecked "$component" || true
            return 0
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            exited=1
            break
        elif process_stat_fields "$pid" && [ "${PROCESS_STAT_FIELDS[0]}" = "Z" ]; then
            exited=1
            break
        fi
        [ "$SECONDS" -lt "$deadline" ] || break
        sleep 0.02
    done
    if [ "$exited" = "1" ]; then
        snapshot_component_group_members_unchecked "$component" || true
        if wait "$pid"; then
            status=0
        else
            status=$?
        fi
        if ! component_group_has_members "$component"; then
            printf -v "${prefix}_PID" '%s' ""
        fi
        supervisor_message "${component^} exited before identity capture with status $status."
        [ "$status" -ne 0 ] && return "$status"
        return 1
    fi
    snapshot_component_group_members_unchecked "$component" || true
    supervisor_message "Failed to capture a stable identity for task-created $component PID $pid."
    return 1
}

component_identity_matches() {
    local component="$1" prefix current_boot current_exe current_hash current_pgid current_start
    prefix="${component^^}"
    local -n pid_ref="${prefix}_PID"
    local -n pgid_ref="${prefix}_PGID"
    local -n boot_ref="${prefix}_BOOT_ID"
    local -n start_ref="${prefix}_START_TICKS"
    local -n exe_ref="${prefix}_EXE"
    local -n hash_ref="${prefix}_CMD_HASH"
    [ -n "$pid_ref" ] && [ -d "/proc/$pid_ref" ] || return 1
    process_stat_fields "$pid_ref" || return 1
    [ "${PROCESS_STAT_FIELDS[0]}" != "Z" ] || return 1
    current_pgid="${PROCESS_STAT_FIELDS[2]}"
    current_start="${PROCESS_STAT_FIELDS[19]}"
    current_boot="$(< /proc/sys/kernel/random/boot_id)"
    current_exe="$(readlink -f "/proc/$pid_ref/exe")" || return 1
    current_hash="$(process_command_hash "$pid_ref")" || return 1
    [ "$current_pgid" = "$pgid_ref" ] \
        && [ "$current_pgid" = "$pid_ref" ] \
        && [ "$current_boot" = "$boot_ref" ] \
        && [ "$current_start" = "$start_ref" ] \
        && [ "$current_exe" = "$exe_ref" ] \
        && [ "$current_hash" = "$hash_ref" ]
}

component_leader_absent_or_zombie() {
    local component="$1" prefix
    prefix="${component^^}"
    local -n pid_ref="${prefix}_PID"
    [ -n "$pid_ref" ] || return 0
    [ -d "/proc/$pid_ref" ] || return 0
    process_stat_fields "$pid_ref" && [ "${PROCESS_STAT_FIELDS[0]}" = "Z" ]
}

component_group_has_members() {
    local component="$1" prefix stat_path stat remainder
    local -a fields
    prefix="${component^^}"
    local -n pgid_ref="${prefix}_PGID"
    local -n sid_ref="${prefix}_SID"
    local -n gone_ref="${prefix}_GROUP_GONE"
    [ "$gone_ref" = "0" ] && [ -n "$pgid_ref" ] && [ -n "$sid_ref" ] || return 1
    for stat_path in /proc/[0-9]*/stat; do
        [ -r "$stat_path" ] || continue
        stat="$(<"$stat_path")" || continue
        remainder="${stat##*) }"
        read -r -a fields <<< "$remainder"
        if [ "${#fields[@]}" -ge 4 ] \
            && [ "${fields[0]}" != "Z" ] \
            && [ "${fields[2]}" = "$pgid_ref" ] \
            && [ "${fields[3]}" = "$sid_ref" ]; then
            return 0
        fi
    done
    gone_ref=1
    return 1
}

snapshot_component_group_members_unchecked() {
    local component="$1" prefix stat_path stat remainder member_pid token
    local -a fields
    prefix="${component^^}"
    local -n pgid_ref="${prefix}_PGID"
    local -n sid_ref="${prefix}_SID"
    local -n identities_ref="${prefix}_MEMBER_IDENTITIES"
    for stat_path in /proc/[0-9]*/stat; do
        [ -r "$stat_path" ] || continue
        stat="$(<"$stat_path")" || continue
        remainder="${stat##*) }"
        read -r -a fields <<< "$remainder"
        if [ "${#fields[@]}" -ge 20 ] \
            && [ "${fields[0]}" != "Z" ] \
            && [ "${fields[2]}" = "$pgid_ref" ] \
            && [ "${fields[3]}" = "$sid_ref" ]; then
            member_pid="${stat_path#/proc/}"
            member_pid="${member_pid%/stat}"
            token="$member_pid:${fields[19]}"
            case " $identities_ref " in
                *" $token "*) ;;
                *) identities_ref="${identities_ref:+$identities_ref }$token" ;;
            esac
        fi
    done
    [ -n "$identities_ref" ]
}

component_group_has_verified_member() {
    local component="$1" prefix token member_pid expected_start
    prefix="${component^^}"
    local -n pgid_ref="${prefix}_PGID"
    local -n sid_ref="${prefix}_SID"
    local -n identities_ref="${prefix}_MEMBER_IDENTITIES"
    for token in $identities_ref; do
        member_pid="${token%%:*}"
        expected_start="${token#*:}"
        process_stat_fields "$member_pid" || continue
        if [ "${PROCESS_STAT_FIELDS[0]}" != "Z" ] \
            && [ "${PROCESS_STAT_FIELDS[2]}" = "$pgid_ref" ] \
            && [ "${PROCESS_STAT_FIELDS[3]}" = "$sid_ref" ] \
            && [ "${PROCESS_STAT_FIELDS[19]}" = "$expected_start" ]; then
            return 0
        fi
    done
    return 1
}

snapshot_component_group_members() {
    local component="$1"
    if component_identity_matches "$component" \
        || component_group_has_verified_member "$component"; then
        snapshot_component_group_members_unchecked "$component"
        return
    fi
    return 1
}

signal_verified_component() {
    local component="$1" signal_name="$2" prefix
    prefix="${component^^}"
    local -n pid_ref="${prefix}_PID"
    local -n pgid_ref="${prefix}_PGID"
    [ -n "$pid_ref" ] || return 1
    if component_identity_matches "$component"; then
        :
    elif component_group_has_verified_member "$component"; then
        :
    else
        printf 'Refusing to signal %s: recorded process identity no longer matches.\n' "$component" >&2
        return 1
    fi
    kill "-$signal_name" -- "-$pgid_ref"
}

port_is_open() {
    local host="$1" port="$2"
    "$OCR_PYTHON" - "$host" "$port" <<'PYEOF'
import socket
import sys

with socket.socket() as sock:
    sock.settimeout(0.2)
    raise SystemExit(0 if sock.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)
PYEOF
}

prefix_stream() {
    local prefix="$1" log_path="$2" line
    while IFS= read -r line || [ -n "$line" ]; do
        printf '[%s] %s\n' "$prefix" "$line"
        printf '%s\n' "$line" >> "$log_path"
    done
}

supervisor_message() {
    local message="$1"
    if [ -n "${SUPERVISOR_LOG:-}" ]; then
        printf '%s\n' "$message" | tee -a "$SUPERVISOR_LOG"
    else
        printf '%s\n' "$message"
    fi
}

wait_for_component_readiness() {
    local component="$1" description="$2" prefix deadline status probe_output=""
    shift 2
    prefix="${component^^}"
    local -n pid_ref="${prefix}_PID"
    deadline=$((SECONDS + OCR_READINESS_TIMEOUT))
    while ! probe_output="$("$@" 2>&1)"; do
        if ! component_identity_matches "$component"; then
            if wait "$pid_ref"; then
                status=0
            else
                status=$?
            fi
            supervisor_message "${component^} exited before readiness with status $status."
            [ -z "$probe_output" ] || printf '%s\n' "$probe_output" >&2
            [ "$status" -ne 0 ] && return "$status"
            return 1
        fi
        if [ "$SECONDS" -ge "$deadline" ]; then
            supervisor_message "$description did not become ready within ${OCR_READINESS_TIMEOUT}s."
            [ -z "$probe_output" ] || printf '%s\n' "$probe_output" >&2
            return 1
        fi
        sleep 0.1
    done
    snapshot_component_group_members "$component" || true
}

release_lock() {
    if [ "$LOCK_HELD" = "1" ]; then
        flock -u 9
        LOCK_HELD=0
    fi
}

stop_component() {
    local component="$1" port="$2" prefix deadline escalated=0 should_stop=0
    prefix="${component^^}"
    local -n pid_ref="${prefix}_PID"
    [ -n "$pid_ref" ] || return 0

    if component_identity_matches "$component"; then
        snapshot_component_group_members_unchecked "$component" || true
        should_stop=1
    elif component_group_has_verified_member "$component"; then
        should_stop=1
    elif component_leader_absent_or_zombie "$component" \
        && ! component_group_has_members "$component"; then
        supervisor_message "Task-owned $component process group already exited."
    else
        printf 'Task-owned %s identity is no longer valid; no signal was sent.\n' "$component" >&2
        return 1
    fi

    if [ "$should_stop" = "1" ]; then
        supervisor_message "Stopping task-owned $component process group with TERM."
        signal_verified_component "$component" TERM || return 1
        deadline=$((SECONDS + OCR_SHUTDOWN_TIMEOUT))
        while component_group_has_members "$component"; do
            if [ "$SECONDS" -ge "$deadline" ]; then
                supervisor_message "Task-owned $component did not stop in time; sending scoped KILL."
                signal_verified_component "$component" KILL || return 1
                escalated=1
                break
            fi
            sleep 0.05
        done
        if [ "$escalated" = "1" ]; then
            deadline=$((SECONDS + OCR_SHUTDOWN_TIMEOUT))
            while component_group_has_members "$component"; do
                if [ "$SECONDS" -ge "$deadline" ]; then
                    printf 'Task-owned %s process group did not disappear after KILL.\n' "$component" >&2
                    return 1
                fi
                sleep 0.05
            done
        fi
    fi

    wait "$pid_ref" 2>/dev/null || true
    deadline=$((SECONDS + OCR_SHUTDOWN_TIMEOUT))
    while port_is_open 127.0.0.1 "$port"; do
        if [ "$SECONDS" -ge "$deadline" ]; then
            printf 'Task-owned %s listener on port %s did not disappear.\n' "$component" "$port" >&2
            return 1
        fi
        sleep 0.05
    done
    pid_ref=""
    supervisor_message "Task-owned $component listener on port $port disappeared."
    [ "$escalated" = "0" ] || printf 'Scoped KILL completed for task-owned %s.\n' "$component"
}

cleanup() {
    local status=0
    [ "$CLEANUP_DONE" = "0" ] || return 0
    [ "$CLEANUP_RUNNING" = "0" ] || return 0
    CLEANUP_RUNNING=1
    stop_component paddlex "$PADDLEX_PORT" || status=1
    stop_component sglang "$SGLANG_PORT" || status=1
    release_lock
    CLEANUP_DONE=1
    CLEANUP_RUNNING=0
    return "$status"
}

on_exit() {
    local status=$? cleanup_status=0
    trap - EXIT INT TERM HUP
    cleanup || cleanup_status=$?
    if [ "$cleanup_status" -ne 0 ] && [ "$status" -eq 0 ]; then
        status="$cleanup_status"
    fi
    exit "$status"
}

on_signal() {
    local signal_name="$1" status
    supervisor_message "Received $signal_name; stopping the task-owned OCR topology."
    case "$signal_name" in
        INT) status=130 ;;
        TERM) status=143 ;;
        HUP) status=129 ;;
        *) status=1 ;;
    esac
    exit "$status"
}

load_config() {
    local assignments
    if ! assignments="$("$OCR_PYTHON" - "$PIPELINE_CONFIG" "$PROJECT_ROOT" <<'PYEOF'
import shlex
import sys
from pathlib import Path

import yaml

path = Path(sys.argv[1]).resolve()
project_root = Path(sys.argv[2]).resolve()
payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
launch = payload.get("LLMDeploymentSGLangBackend")
recognition = ((payload.get("SubModules") or {}).get("VLRecognition") or {})
genai = recognition.get("genai_config") or {}
if not isinstance(launch, dict):
    raise SystemExit("LLMDeploymentSGLangBackend must be a mapping")

def positive_int(name):
    value = launch.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SystemExit(f"{name} must be a positive integer")
    return value

def required_bool(name):
    value = launch.get(name)
    if not isinstance(value, bool):
        raise SystemExit(f"{name} must be a boolean")
    return value

host = launch.get("host")
port = positive_int("port")
if host != "127.0.0.1" or port != 30002:
    raise SystemExit("OCR SGLang must use 127.0.0.1:30002")
model_dir = Path(str(launch.get("model_dir", ""))).expanduser()
chat_template = Path(str(launch.get("chat_template", ""))).expanduser()
if not model_dir.is_absolute() or not model_dir.is_dir():
    raise SystemExit("model_dir must be an existing absolute directory")
if not chat_template.is_absolute() or not chat_template.is_file():
    raise SystemExit("chat_template must be an existing absolute file")
served_model = launch.get("served_model_name")
if served_model != "PaddleOCR-VL-1.6-0.9B":
    raise SystemExit("served_model_name is invalid")
context_length = positive_int("context_length")
max_running = positive_int("max_running_requests")
max_prefill = positive_int("max_prefill_tokens")
shutdown_timeout = positive_int("shutdown_timeout_seconds")
readiness_timeout = positive_int("readiness_timeout_seconds")
memory_fraction = launch.get("mem_fraction_static")
if isinstance(memory_fraction, bool) or not isinstance(memory_fraction, (int, float)) or not 0 < memory_fraction < 1:
    raise SystemExit("mem_fraction_static must be between zero and one")
for boolean_name in ("trust_remote_code", "enable_metrics", "log_requests"):
    if required_bool(boolean_name) is not True:
        raise SystemExit(f"{boolean_name} must be enabled")
log_level = launch.get("log_requests_level")
if isinstance(log_level, bool) or not isinstance(log_level, int) or log_level != 0:
    raise SystemExit("log_requests_level must be zero")
if genai.get("backend") != "sglang-server" or genai.get("server_url") != "http://127.0.0.1:30002/v1":
    raise SystemExit("VLRecognition remote backend is inconsistent")
max_concurrency = genai.get("max_concurrency")
batch_size = recognition.get("batch_size")
if max_concurrency != max_running or isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < max_concurrency:
    raise SystemExit("VLRecognition concurrency is inconsistent")

overall = yaml.safe_load((project_root / "config/config-overall.yaml").read_text()) or {}
sys.path.insert(0, str(project_root / "scripts"))
from path_utils import resolve_path

def load_selected_config(key, fallback):
    selected = resolve_path(str(overall.get(key, fallback)), project_root)
    return yaml.safe_load(selected.read_text()) or {}

server = load_selected_config("server", "config/config-server.yaml")
agent_operation = load_selected_config(
    "agent_operation", "config/config-agent-operation.yaml"
)
cuda_memory_limit = (server.get("ocr") or {}).get("cuda_memory_limit_gb")
memory_reserve = (agent_operation.get("read_file") or {}).get(
    "main_llm_memory_reserve_gb"
)
for name, value in (
    ("ocr.cuda_memory_limit_gb", cuda_memory_limit),
    ("read_file.main_llm_memory_reserve_gb", memory_reserve),
):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SystemExit(f"{name} must be a positive integer")

values = {
    "CONFIG_SGLANG_HOST": host,
    "CONFIG_SGLANG_PORT": port,
    "MODEL_DIR": model_dir,
    "SERVED_MODEL_NAME": served_model,
    "CHAT_TEMPLATE": chat_template,
    "CONTEXT_LENGTH": context_length,
    "MAX_RUNNING_REQUESTS": max_running,
    "MAX_PREFILL_TOKENS": max_prefill,
    "MEM_FRACTION_STATIC": memory_fraction,
    "LOG_REQUESTS_LEVEL": log_level,
    "CONFIG_READINESS_TIMEOUT": readiness_timeout,
    "CONFIG_SHUTDOWN_TIMEOUT": shutdown_timeout,
    "CONFIG_OCR_CUDA_MEMORY_LIMIT_GB": cuda_memory_limit,
    "CONFIG_MAIN_LLM_MEMORY_RESERVE_GB": memory_reserve,
}
for key, value in values.items():
    print(f"{key}={shlex.quote(str(value))}")
PYEOF
)"; then
        return 1
    fi
    eval "$assignments"
}

launch_sglang() {
    TRITON_PTXAS_PATH="$TRITON_PTXAS_WRAPPER" \
    TRITON_PTXAS_BLACKWELL_PATH="$TRITON_PTXAS_WRAPPER" \
    TORCH_CUDA_ARCH_LIST="$OCR_TORCH_CUDA_ARCH_LIST" \
    setsid "$SGLANG_PYTHON" -m sglang.launch_server \
        --model-path "$MODEL_DIR" \
        --served-model-name "$SERVED_MODEL_NAME" \
        --host "$SGLANG_HOST" \
        --port "$SGLANG_PORT" \
        --chat-template "$CHAT_TEMPLATE" \
        --trust-remote-code \
        --context-length "$CONTEXT_LENGTH" \
        --max-running-requests "$MAX_RUNNING_REQUESTS" \
        --max-prefill-tokens "$MAX_PREFILL_TOKENS" \
        --mem-fraction-static "$MEM_FRACTION_STATIC" \
        --enable-metrics \
        --log-requests \
        --log-requests-level "$LOG_REQUESTS_LEVEL" \
        --log-requests-target stdout \
        > >(prefix_stream OCR-SGLANG "$SGLANG_LOG") 2>&1 &
    local pid=$!
    sleep 0.05
    record_or_report_early_exit sglang "$pid"
    supervisor_message "SGLang identity: pid=$SGLANG_PID pgid=$SGLANG_PGID boot=$SGLANG_BOOT_ID start=$SGLANG_START_TICKS exe=$SGLANG_EXE commandSha256=$SGLANG_CMD_HASH"
}

launch_paddlex() {
    rm -f "$OCR_MEMORY_REPORT"
    env \
        -u ALL_PROXY -u all_proxy \
        -u HTTP_PROXY -u http_proxy \
        -u HTTPS_PROXY -u https_proxy \
        NO_PROXY="${NO_PROXY:+$NO_PROXY,}$LOCAL_NO_PROXY" \
        no_proxy="${no_proxy:+$no_proxy,}$LOCAL_NO_PROXY" \
    setsid "$OCR_PYTHON" "$OCR_SERVICE_WRAPPER" \
        --cuda-memory-limit-gb "$OCR_CUDA_MEMORY_LIMIT_GB" \
        --memory-report "$OCR_MEMORY_REPORT" \
        --serve \
        --pipeline "$PIPELINE_CONFIG" \
        --device gpu:0 \
        --host "$PADDLEX_HOST" \
        --port "$PADDLEX_PORT" \
        > >(prefix_stream PADDLEX "$PADDLEX_LOG") 2>&1 &
    local pid=$!
    sleep 0.05
    record_or_report_early_exit paddlex "$pid"
    supervisor_message "PaddleX identity: pid=$PADDLEX_PID pgid=$PADDLEX_PGID boot=$PADDLEX_BOOT_ID start=$PADDLEX_START_TICKS exe=$PADDLEX_EXE commandSha256=$PADDLEX_CMD_HASH"
}

main() {
    SHARED_WORKSPACE_ROOT="$(get_shared_workspace_root "$PROJECT_ROOT")"
    SGLANG_PYTHON="$(get_shared_sglang_python "$PROJECT_ROOT")"
    OCR_PYTHON="$(get_shared_paddle_ocr_python "$PROJECT_ROOT")"
    PIPELINE_CONFIG="${OCR_PIPELINE_CONFIG:-$PROJECT_ROOT/config/paddleocr-vl-thor-sglang.yaml}"
    PIPELINE_CONFIG="$(resolve_project_path "$PIPELINE_CONFIG" "$PROJECT_ROOT")"
    OCR_SERVICE_WRAPPER="${OCR_SERVICE_WRAPPER:-$PROJECT_ROOT/scripts/paddle_ocr_service.py}"
    PREFLIGHT="${OCR_PREFLIGHT:-$PROJECT_ROOT/scripts/paddle_ocr_preflight.py}"
    OCR_LOG_DIR="${OCR_LOG_DIR:-$SHARED_WORKSPACE_ROOT/logs}"
    OCR_ATTESTATION_PATH="${OCR_ATTESTATION_PATH:-$OCR_LOG_DIR/paddleocr-runtime-attestation.json}"
    OCR_MEMORY_REPORT="${OCR_MEMORY_REPORT:-$OCR_LOG_DIR/paddleocr-cuda-memory-limit.json}"
    OCR_TOPOLOGY_LOCK="${OCR_TOPOLOGY_LOCK:-$OCR_LOG_DIR/paddleocr-sglang-topology.lock}"
    OCR_EXIT_AFTER_CANARY="${OCR_EXIT_AFTER_CANARY:-0}"
    OCR_START_LOCK_TIMEOUT="${OCR_START_LOCK_TIMEOUT:-900}"
    OCR_PROBE_TIMEOUT="${OCR_PROBE_TIMEOUT:-5}"
    TRITON_PTXAS_WRAPPER="${OCR_TRITON_PTXAS_WRAPPER:-$SHARED_WORKSPACE_ROOT/patch/ptxas_wrapper.sh}"
    OCR_SERVICE_WRAPPER="$(expand_home_path "$OCR_SERVICE_WRAPPER")"
    PREFLIGHT="$(expand_home_path "$PREFLIGHT")"
    OCR_LOG_DIR="$(expand_home_path "$OCR_LOG_DIR")"
    OCR_ATTESTATION_PATH="$(expand_home_path "$OCR_ATTESTATION_PATH")"
    OCR_MEMORY_REPORT="$(expand_home_path "$OCR_MEMORY_REPORT")"
    OCR_TOPOLOGY_LOCK="$(expand_home_path "$OCR_TOPOLOGY_LOCK")"
    TRITON_PTXAS_WRAPPER="$(expand_home_path "$TRITON_PTXAS_WRAPPER")"
    OCR_TORCH_CUDA_ARCH_LIST="${OCR_TORCH_CUDA_ARCH_LIST:-11.0a}"
    LOCAL_NO_PROXY="127.0.0.1,localhost"
    PADDLEX_HOST=127.0.0.1
    PADDLEX_PORT=8118

    [ -x "$SGLANG_PYTHON" ] || { printf 'Missing shared SGLang Python: %s\n' "$SGLANG_PYTHON" >&2; return 1; }
    [ -x "$OCR_PYTHON" ] || { printf 'Missing shared PaddleOCR Python: %s\n' "$OCR_PYTHON" >&2; return 1; }
    [ -x "$TRITON_PTXAS_WRAPPER" ] || { printf 'Missing executable Thor PTXAS wrapper: %s\n' "$TRITON_PTXAS_WRAPPER" >&2; return 1; }
    [ "$OCR_TORCH_CUDA_ARCH_LIST" = "11.0a" ] || { printf 'OCR_TORCH_CUDA_ARCH_LIST must be 11.0a on Jetson Thor.\n' >&2; return 1; }
    [ -f "$PIPELINE_CONFIG" ] || { printf 'Missing Pipeline config: %s\n' "$PIPELINE_CONFIG" >&2; return 1; }
    [ -f "$OCR_SERVICE_WRAPPER" ] || { printf 'Missing PaddleX wrapper: %s\n' "$OCR_SERVICE_WRAPPER" >&2; return 1; }
    [ -f "$PREFLIGHT" ] || { printf 'Missing preflight program: %s\n' "$PREFLIGHT" >&2; return 1; }
    [[ "$OCR_EXIT_AFTER_CANARY" =~ ^[01]$ ]] || { printf 'OCR_EXIT_AFTER_CANARY must be zero or one.\n' >&2; return 1; }
    [[ "$OCR_PROBE_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || { printf 'OCR_PROBE_TIMEOUT must be a positive integer.\n' >&2; return 1; }

    load_config
    OCR_CUDA_MEMORY_LIMIT_GB="${OCR_CUDA_MEMORY_LIMIT_GB:-$CONFIG_OCR_CUDA_MEMORY_LIMIT_GB}"
    MAIN_LLM_MEMORY_RESERVE_GB="${MAIN_LLM_MEMORY_RESERVE_GB:-$CONFIG_MAIN_LLM_MEMORY_RESERVE_GB}"
    SGLANG_PORT="$CONFIG_SGLANG_PORT"
    SGLANG_HOST="$CONFIG_SGLANG_HOST"
    OCR_READINESS_TIMEOUT="${OCR_READINESS_TIMEOUT:-$CONFIG_READINESS_TIMEOUT}"
    OCR_SHUTDOWN_TIMEOUT="${OCR_SHUTDOWN_TIMEOUT:-$CONFIG_SHUTDOWN_TIMEOUT}"
    [[ "$PADDLEX_PORT" =~ ^[1-9][0-9]*$ && "$SGLANG_PORT" =~ ^[1-9][0-9]*$ ]] || return 1
    [ "$PADDLEX_PORT" -le 65535 ] && [ "$SGLANG_PORT" -le 65535 ] || return 1
    [[ "$OCR_CUDA_MEMORY_LIMIT_GB" =~ ^[1-9][0-9]*$ ]] || { printf 'Invalid OCR CUDA memory limit.\n' >&2; return 1; }
    [[ "$MAIN_LLM_MEMORY_RESERVE_GB" =~ ^[1-9][0-9]*$ ]] || { printf 'Invalid main LLM memory reserve.\n' >&2; return 1; }
    for timeout_name in OCR_READINESS_TIMEOUT OCR_SHUTDOWN_TIMEOUT OCR_START_LOCK_TIMEOUT OCR_PROBE_TIMEOUT; do
        local -n timeout_ref="$timeout_name"
        [[ "$timeout_ref" =~ ^[1-9][0-9]*$ ]] || {
            printf '%s must be a positive integer.\n' "$timeout_name" >&2
            return 1
        }
    done

    RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
    mkdir -p "$OCR_LOG_DIR" "$(dirname "$OCR_ATTESTATION_PATH")" "$(dirname "$OCR_MEMORY_REPORT")" "$(dirname "$OCR_TOPOLOGY_LOCK")"
    SGLANG_LOG="$OCR_LOG_DIR/paddleocr-sglang-$RUN_ID.log"
    PADDLEX_LOG="$OCR_LOG_DIR/paddlex-$RUN_ID.log"
    SUPERVISOR_LOG="$OCR_LOG_DIR/paddleocr-supervisor-$RUN_ID.log"
    : > "$SGLANG_LOG"
    : > "$PADDLEX_LOG"
    printf 'Starting OCR topology run %s.\n' "$RUN_ID" | tee -a "$SUPERVISOR_LOG"

    trap on_exit EXIT
    trap 'on_signal INT' INT
    trap 'on_signal TERM' TERM
    trap 'on_signal HUP' HUP

    "$SGLANG_PYTHON" "$PREFLIGHT" check-sglang-env \
        --python "$SGLANG_PYTHON" \
        --config "$PIPELINE_CONFIG"

    exec 9>"$OCR_TOPOLOGY_LOCK"
    if ! flock -w "$OCR_START_LOCK_TIMEOUT" 9; then
        printf 'Timed out acquiring OCR topology lock: %s\n' "$OCR_TOPOLOGY_LOCK" >&2
        return 1
    fi
    LOCK_HELD=1
    if port_is_open "$PADDLEX_HOST" "$PADDLEX_PORT" || port_is_open "$SGLANG_HOST" "$SGLANG_PORT"; then
        printf 'OCR topology port is already occupied; no process was changed.\n' >&2
        return 1
    fi
    "$OCR_PYTHON" "$PREFLIGHT" check-memory-reserve \
        --minimum-gb "$MAIN_LLM_MEMORY_RESERVE_GB"

    launch_sglang
    SGLANG_URL="http://$SGLANG_HOST:$SGLANG_PORT"
    wait_for_component_readiness sglang SGLang \
        "$SGLANG_PYTHON" "$PREFLIGHT" probe-sglang \
            --url "$SGLANG_URL" \
            --served-model-name "$SERVED_MODEL_NAME" \
            --timeout "$OCR_PROBE_TIMEOUT"
    supervisor_message "SGLang readiness passed for $SGLANG_URL."

    launch_paddlex
    PADDLEX_URL="http://$PADDLEX_HOST:$PADDLEX_PORT"
    wait_for_component_readiness paddlex PaddleX \
        "$OCR_PYTHON" "$PREFLIGHT" probe \
            --url "$PADDLEX_URL" \
            --timeout "$OCR_PROBE_TIMEOUT"
    supervisor_message "PaddleX readiness passed for $PADDLEX_URL."
    SGLANG_LOG_OFFSET="$(wc -c < "$SGLANG_LOG")"
    "$OCR_PYTHON" "$PREFLIGHT" canary-sglang \
        --paddlex-url "$PADDLEX_URL" \
        --sglang-url "$SGLANG_URL" \
        --served-model-name "$SERVED_MODEL_NAME" \
        --attestation "$OCR_ATTESTATION_PATH" \
        --config "$PIPELINE_CONFIG" \
        --pipeline-pid "$PADDLEX_PID" \
        --sglang-pid "$SGLANG_PID" \
        --memory-report "$OCR_MEMORY_REPORT" \
        --cuda-memory-limit-gb "$OCR_CUDA_MEMORY_LIMIT_GB" \
        --sglang-log "$SGLANG_LOG" \
        --sglang-log-offset "$SGLANG_LOG_OFFSET" \
        --sglang-python "$SGLANG_PYTHON" \
        --main-llm-memory-reserve-gb "$MAIN_LLM_MEMORY_RESERVE_GB" \
        --timeout "$OCR_READINESS_TIMEOUT"
    supervisor_message "PaddleOCR remote canary passed."

    release_lock
    if [ "$OCR_EXIT_AFTER_CANARY" = "1" ]; then
        printf 'Canary passed; verification mode is stopping both task-owned services.\n'
        return 0
    fi

    printf 'OCR topology is ready. PaddleX PID=%s, SGLang PID=%s.\n' "$PADDLEX_PID" "$SGLANG_PID"
    local exited_pid child_status
    if wait -n -p exited_pid "$PADDLEX_PID" "$SGLANG_PID"; then
        child_status=0
    else
        child_status=$?
    fi
    if [ "$exited_pid" = "$PADDLEX_PID" ]; then
        supervisor_message "PaddleX exited unexpectedly with status $child_status."
    elif [ "$exited_pid" = "$SGLANG_PID" ]; then
        supervisor_message "SGLang exited unexpectedly with status $child_status."
    else
        supervisor_message "OCR topology wait ended without a recognized child PID (status $child_status)."
    fi
    [ "$child_status" -ne 0 ] && return "$child_status"
    return 1
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
