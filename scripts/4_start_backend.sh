#!/bin/bash
# 启动 FastAPI 后端服务脚本

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$PROJECT_ROOT/scripts/_workspace_paths.sh"
SHARED_WORKSPACE_ROOT="$(get_shared_workspace_root "$PROJECT_ROOT")"
SHARED_SGLANG_ACTIVATE="$(get_shared_sglang_activate "$PROJECT_ROOT")"
SHARED_SGLANG_PYTHON="$(get_shared_sglang_python "$PROJECT_ROOT")"
YAML_OVERALL="$PROJECT_ROOT/config/config-overall.yaml"

if [ ! -f "$YAML_OVERALL" ]; then
    echo "❌ 配置文件不存在: $YAML_OVERALL"
    exit 1
fi

cd "$PROJECT_ROOT"

if [ -f "$SHARED_SGLANG_ACTIVATE" ] && [ -x "$SHARED_SGLANG_PYTHON" ]; then
    echo "🐍 使用 uv 环境: $SHARED_WORKSPACE_ROOT/.sglang"
    # shellcheck disable=SC1091
    source "$SHARED_SGLANG_ACTIVATE"
else
    echo "❌ 未找到 .sglang 环境: $SHARED_WORKSPACE_ROOT/.sglang"
    exit 1
fi

SEARXNG_DEPLOY_DIR="$PROJECT_ROOT/deploy/searxng"
SEARXNG_ENV_FILE="$SEARXNG_DEPLOY_DIR/.env"
SEARXNG_SETTINGS_FILE="$SEARXNG_DEPLOY_DIR/core-config/settings.yml"
SEARXNG_BASE_URL_DEFAULT="http://127.0.0.1:8080"
SEARXNG_BASE_URL="${SEARXNG_BASE_URL_OVERRIDE:-$SEARXNG_BASE_URL_DEFAULT}"
SEARXNG_AUTOSTART="${SEARXNG_AUTOSTART:-1}"
SEARXNG_WORKER_PID=""
STARTUP_RUN_DIR=""
BACKEND_PID=""

if ! ASR_CONFIG_ASSIGNMENTS="$("$SHARED_SGLANG_PYTHON" - "$PROJECT_ROOT" "$YAML_OVERALL" <<'PYEOF'
import sys
from pathlib import Path

import yaml
from backend.asr.config import load_asr_config
from backend.retrieval.config import load_local_memory_config


def q(value):
    if value is None:
        value = ""
    value = str(value)
    return "'" + value.replace("'", "'\\''") + "'"


project_root = Path(sys.argv[1])
overall_path = Path(sys.argv[2])
sys.path.insert(0, str(project_root / "scripts"))
from path_utils import resolve_path

overall = yaml.safe_load(overall_path.read_text(encoding="utf-8")) or {}


server_path = resolve_path(overall.get("server", "config/config-server.yaml"), project_root)
infer_path = resolve_path(overall.get("infer", "config/qwen3-vl-config-infer.yaml"), project_root)

server_cfg = yaml.safe_load(server_path.read_text(encoding="utf-8")) or {}
infer_cfg = yaml.safe_load(infer_path.read_text(encoding="utf-8")) or {}

server = server_cfg.get("server", {}) or {}
frontend = server_cfg.get("frontend", {}) or {}
ocr = server_cfg.get("ocr", {}) or {}
asr_path = resolve_path(overall.get("asr", "config/config-asr.yaml"), project_root)
asr_cfg = load_asr_config(asr_path, project_root=project_root)
browser_security = asr_cfg.browser_security
memory_path = resolve_path(overall.get("local_memory", "config/config-local-memory.yaml"), project_root)
memory_cfg = load_local_memory_config(memory_path, project_root)
sglang = infer_cfg.get("sglang", {}) or {}
vllm = infer_cfg.get("vllm", {}) or {}
container = infer_cfg.get("container", {}) or {}

values = {
    "SEARXNG_ATTEMPT_TIMEOUT": memory_cfg.startup.searxng_attempt_timeout_seconds,
    "BACKEND_HOST": server.get("host", "0.0.0.0"),
    "BACKEND_PORT": server.get("port", 8090),
    "BACKEND_WORKERS": server.get("workers", 1),
    "OCR_ENABLED": str(bool(ocr.get("enabled", True))).lower(),
    "FRONTEND_DEV_PORT": frontend.get("dev_port", 3000),
    "FRONTEND_API_BASE_URL": frontend.get("api_base_url", "http://localhost:8090"),
    "OCR_SERVICE_URL": ocr.get("base_url", "http://127.0.0.1:8118"),
    "SGLANG_HOST": sglang.get("host", "127.0.0.1"),
    "SGLANG_PORT": sglang.get("port", 30000),
    "SGLANG_KV_CACHE_DTYPE": sglang.get("kv_cache_dtype", ""),
    "SGLANG_SCHEDULE_POLICY": sglang.get("schedule_policy", ""),
    "SGLANG_CUDA_GRAPH_MAX_BS": sglang.get("cuda_graph_max_bs", ""),
    "SGLANG_CHUNKED_PREFILL_SIZE": sglang.get("chunked_prefill_size", ""),
    "SGLANG_MEM_FRACTION_STATIC": sglang.get("mem_fraction_static", ""),
    "VLLM_HOST": vllm.get("host", "127.0.0.1"),
    "VLLM_PORT": vllm.get("port", 30000),
    "CONTAINER_ENABLED": str(bool(container.get("enabled", False))).lower(),
    "CONTAINER_API_URL": container.get("api_url", "http://localhost:8000/v1"),
    "CONTAINER_MODEL_NAME": container.get("model_name", ""),
    "ASR_HTTPS_ENABLED": str(browser_security.https_enabled).lower(),
    "ASR_CA_CERTIFICATE": resolve_path(browser_security.ca_certificate_path, project_root),
    "ASR_CERTIFICATE": resolve_path(browser_security.certificate_path, project_root),
    "ASR_PRIVATE_KEY": resolve_path(browser_security.private_key_path, project_root),
}

for key, value in values.items():
    print(f"{key}={q(value)}")
PYEOF
)"; then
    echo "❌ ASR typed/TLS config validation failed; backend was not started."
    exit 1
fi
eval "$ASR_CONFIG_ASSIGNMENTS"

BACKEND_HOST="${BACKEND_HOST_OVERRIDE:-$BACKEND_HOST}"
BACKEND_PORT="${BACKEND_PORT_OVERRIDE:-$BACKEND_PORT}"
BACKEND_WORKERS="${BACKEND_WORKERS_OVERRIDE:-$BACKEND_WORKERS}"
FRONTEND_API_BASE_URL="${FRONTEND_API_BASE_URL_OVERRIDE:-$FRONTEND_API_BASE_URL}"
BACKEND_SCHEME="http"
if [ "$ASR_HTTPS_ENABLED" = "true" ]; then
    BACKEND_SCHEME="https"
fi

if ! [[ "$BACKEND_WORKERS" =~ ^[0-9]+$ ]]; then
    echo "❌ server.workers must be an integer"
    exit 1
fi
if [ "$OCR_ENABLED" = "true" ]; then
    if [ "$BACKEND_WORKERS" -ne 1 ]; then
        echo "❌ OCR admission is process-local; server.workers must be 1 while Read File OCR is enabled"
        exit 1
    fi
fi

USER_LLM_BACKEND="${LLM_BACKEND:-}"
USER_SGLANG_API_URL="${SGLANG_API_URL:-}"
USER_VLLM_API_URL="${VLLM_API_URL:-}"
USER_CONTAINER_API_URL="${CONTAINER_API_URL:-}"

export TRITON_PTXAS_PATH="$SHARED_WORKSPACE_ROOT/patch/ptxas_wrapper.sh"
export TORCH_CUDA_ARCH_LIST=11.0
export LD_LIBRARY_PATH=/usr/local/cuda-13.0/lib64:${LD_LIBRARY_PATH:-}
export CPLUS_INCLUDE_PATH=/usr/local/cuda-13.0/targets/sbsa-linux/include:${CPLUS_INCLUDE_PATH:-}

normalize_api_url() {
    local base_url="$1"
    if [[ "$base_url" == */v1 ]]; then
        printf '%s' "$base_url"
    else
        printf '%s/v1' "$base_url"
    fi
}

http_ok() {
    local url="$1"
    curl -fsS --max-time 2 "$url" > /dev/null 2>&1
}

detect_engine() {
    local api_url="$1"
    local base_url="${api_url%/v1}"
    local body

    if http_ok "$base_url/server_info" || http_ok "$base_url/get_server_info"; then
        printf 'sglang'
        return 0
    fi

    body="$(curl -fsS --max-time 2 "$api_url/models" 2>/dev/null || true)"
    if [[ -n "$body" ]]; then
        if [[ "$body" == *"created"* ]]; then
            printf 'vllm'
        else
            printf 'openai-compatible'
        fi
        return 0
    fi

    return 1
}

probe_ocr_service() {
    local schema
    schema="$(curl -fsS --max-time 2 "${OCR_SERVICE_URL%/}/openapi.json" 2>/dev/null || true)"
    if [ -z "$schema" ]; then
        echo "WARNING: PaddleOCR-VL is unavailable at ${OCR_SERVICE_URL}. Start it with: bash scripts/2b_start_OCR_LM.sh or bash scripts/2a_start_OCR_LM_with_SGLang_backend.sh"
        return 0
    fi
    if printf '%s' "$schema" | "$SHARED_WORKSPACE_ROOT/.sglang/bin/python" -c \
        'import json,sys; schema=json.load(sys.stdin); raise SystemExit(0 if isinstance(schema.get("paths", {}).get("/layout-parsing", {}).get("post"), dict) else 1)'; then
        echo "PaddleOCR-VL service is healthy at ${OCR_SERVICE_URL} (POST /layout-parsing)."
    else
        echo "WARNING: ${OCR_SERVICE_URL} is occupied by an incompatible listener; OCR is disabled and no process was changed."
    fi
}

check_and_kill_port() {
    local port="$1"
    local pids
    pids="$(lsof -ti :"$port" 2>/dev/null || true)"
    if [ -n "$pids" ]; then
        echo "⚠️  检测到后端端口 $port 被以下进程占用: $(printf '%s' "$pids" | tr '\n' ' ')"
        while IFS= read -r pid; do
            [ -z "$pid" ] && continue
            kill -TERM "$pid" 2>/dev/null || true
        done <<< "$pids"

        local count=0
        while [ "$count" -lt 5 ]; do
            local any_alive=0
            while IFS= read -r pid; do
                [ -z "$pid" ] && continue
                if kill -0 "$pid" 2>/dev/null; then
                    any_alive=1
                    break
                fi
            done <<< "$pids"

            [ "$any_alive" -eq 0 ] && break
            sleep 1
            count=$((count + 1))
        done

        while IFS= read -r pid; do
            [ -z "$pid" ] && continue
            if kill -0 "$pid" 2>/dev/null; then
                echo "⚠️  强制终止进程 $pid"
                kill -KILL "$pid" 2>/dev/null || true
            fi
        done <<< "$pids"

        sleep 1
    fi
}

is_port_available() {
    local port="$1"
    ! lsof -Pi :"$port" -sTCP:LISTEN -t >/dev/null 2>&1
}

wait_for_backend_ready() {
    local curl_tls=()
    if [ "$ASR_HTTPS_ENABLED" = "true" ]; then
        curl_tls=(--cacert "$ASR_CA_CERTIFICATE")
    fi
    local url="${BACKEND_SCHEME}://127.0.0.1:${BACKEND_PORT}/health"
    local attempts=0
    while [ "$attempts" -lt 60 ]; do
        if curl -fsS "${curl_tls[@]}" --max-time 2 "$url" >/dev/null 2>&1; then
            return 0
        fi
        if [ -n "$BACKEND_PID" ] && ! kill -0 "$BACKEND_PID" 2>/dev/null; then
            return 1
        fi
        sleep 1
        attempts=$((attempts + 1))
    done
    return 1
}

LOCAL_SGLANG_API_URL="$(normalize_api_url "http://${SGLANG_HOST}:${SGLANG_PORT}")"
probe_ocr_service
LOCAL_VLLM_API_URL="$(normalize_api_url "http://${VLLM_HOST}:${VLLM_PORT}")"
CONFIGURED_CONTAINER_API_URL="$(normalize_api_url "$CONTAINER_API_URL")"

if [ -n "$USER_SGLANG_API_URL" ]; then
    LOCAL_SGLANG_API_URL="$(normalize_api_url "$USER_SGLANG_API_URL")"
fi

if [ -n "$USER_VLLM_API_URL" ]; then
    LOCAL_VLLM_API_URL="$(normalize_api_url "$USER_VLLM_API_URL")"
fi

if [ -n "$USER_CONTAINER_API_URL" ]; then
    CONFIGURED_CONTAINER_API_URL="$(normalize_api_url "$USER_CONTAINER_API_URL")"
fi

LOCAL_ENGINE="$(detect_engine "$LOCAL_SGLANG_API_URL" || true)"
if [ -z "$LOCAL_ENGINE" ] && [ "$LOCAL_VLLM_API_URL" != "$LOCAL_SGLANG_API_URL" ]; then
    LOCAL_ENGINE="$(detect_engine "$LOCAL_VLLM_API_URL" || true)"
fi
CONTAINER_ENGINE="$(detect_engine "$CONFIGURED_CONTAINER_API_URL" || true)"

SELECTED_SCOPE=""
SELECTED_ENGINE=""
SELECTED_API_URL=""

if [ -n "$LOCAL_ENGINE" ]; then
    SELECTED_SCOPE="local"
    SELECTED_ENGINE="$LOCAL_ENGINE"
    if [ "$LOCAL_ENGINE" = "sglang" ]; then
        SELECTED_API_URL="$LOCAL_SGLANG_API_URL"
    else
        SELECTED_API_URL="$LOCAL_VLLM_API_URL"
    fi
fi

if [ -z "$SELECTED_ENGINE" ] && [ -n "$CONTAINER_ENGINE" ]; then
    SELECTED_SCOPE="container"
    SELECTED_ENGINE="$CONTAINER_ENGINE"
    SELECTED_API_URL="$CONFIGURED_CONTAINER_API_URL"
fi

if [ -n "$USER_LLM_BACKEND" ]; then
    export LLM_BACKEND="$USER_LLM_BACKEND"
else
    unset LLM_BACKEND || true
fi

if [ -n "$USER_SGLANG_API_URL" ]; then
    export SGLANG_API_URL="$LOCAL_SGLANG_API_URL"
else
    unset SGLANG_API_URL || true
fi

if [ -n "$USER_VLLM_API_URL" ]; then
    export VLLM_API_URL="$LOCAL_VLLM_API_URL"
else
    unset VLLM_API_URL || true
fi

if [ -n "$USER_CONTAINER_API_URL" ]; then
    export CONTAINER_API_URL="$CONFIGURED_CONTAINER_API_URL"
else
    unset CONTAINER_API_URL || true
fi

if [ -z "$USER_LLM_BACKEND" ] && [ -n "$SELECTED_ENGINE" ]; then
    case "$SELECTED_ENGINE" in
        sglang)
            export LLM_BACKEND="sglang"
            export SGLANG_API_URL="$SELECTED_API_URL"
            ;;
        vllm)
            export LLM_BACKEND="vllm"
            export VLLM_API_URL="$SELECTED_API_URL"
            ;;
        openai-compatible)
            export LLM_BACKEND="openai_proxy"
            export CONTAINER_API_URL="$SELECTED_API_URL"
            ;;
    esac
fi

echo "🚀 正在启动 LLM 后端服务..."
echo "📍 后端地址: ${BACKEND_SCHEME}://${BACKEND_HOST}:${BACKEND_PORT}"
echo "🌐 前端开发端口: ${FRONTEND_DEV_PORT}"
echo "🔗 前端 API Base URL: ${FRONTEND_API_BASE_URL}"
echo "🧭 本地推理端口探测: ${LOCAL_SGLANG_API_URL} / ${LOCAL_VLLM_API_URL} -> ${LOCAL_ENGINE:-未发现}"
echo "📦 容器推理端口探测: ${CONFIGURED_CONTAINER_API_URL} -> ${CONTAINER_ENGINE:-未发现}"
echo "🛠️  当前 SGLang 关键调优参数: kv_cache_dtype=${SGLANG_KV_CACHE_DTYPE:-unset}, schedule_policy=${SGLANG_SCHEDULE_POLICY:-unset}, cuda_graph_max_bs=${SGLANG_CUDA_GRAPH_MAX_BS:-unset}, chunked_prefill_size=${SGLANG_CHUNKED_PREFILL_SIZE:-unset}, mem_fraction_static=${SGLANG_MEM_FRACTION_STATIC:-unset}"

if [ -n "$USER_LLM_BACKEND" ]; then
    echo "🎛️  使用外部环境变量强制后端: ${USER_LLM_BACKEND}"
elif [ -n "$SELECTED_ENGINE" ]; then
    echo "✅ 选定推理后端: ${SELECTED_SCOPE}/${SELECTED_ENGINE} (${SELECTED_API_URL})"
else
    echo "⚠️  未探测到运行中的推理服务，后端将按自动检测逻辑启动"
fi

if ! is_port_available "$BACKEND_PORT"; then
    echo "⚠️  后端端口 ${BACKEND_PORT} 已被占用，尝试清理..."
    check_and_kill_port "$BACKEND_PORT"
    if ! is_port_available "$BACKEND_PORT"; then
        echo "❌ 无法释放后端端口 ${BACKEND_PORT}，请手动检查"
        exit 1
    fi
fi

stop_process_bounded() {
    local pid="$1"
    local group="${2:-0}"
    [ -n "$pid" ] || return 0
    local target="$pid"
    [ "$group" = "1" ] && target="-$pid"
    kill -TERM -- "$target" 2>/dev/null || true
    for _ in {1..20}; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.05
    done
    kill -KILL -- "$target" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
}

cleanup() {
    local exit_code=$?
    trap - EXIT SIGINT SIGTERM SIGHUP
    stop_process_bounded "$SEARXNG_WORKER_PID" 1
    stop_process_bounded "$BACKEND_PID"
    if [ -n "$STARTUP_RUN_DIR" ]; then
        timeout --kill-after=1 "$SEARXNG_ATTEMPT_TIMEOUT" "$SHARED_SGLANG_PYTHON" -m backend.tools.backend_startup_support cleanup \
            --receipt "$STARTUP_RUN_DIR/searxng.json" --budget "$SEARXNG_ATTEMPT_TIMEOUT" || true
        rm -rf -- "$STARTUP_RUN_DIR"
    fi
    exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 0' SIGINT SIGTERM SIGHUP

# No gate variables are exported to Docker/probes or unrelated descendants.
unset LOCAL_MEMORY_STARTUP_GATE LOCAL_MEMORY_STARTUP_NONCE
STARTUP_RUN_DIR="$(mktemp -d "$PROJECT_ROOT/.backend-startup.XXXXXXXX")"
chmod 700 "$STARTUP_RUN_DIR"
eval "$("$SHARED_SGLANG_PYTHON" -m backend.tools.backend_startup_support init --directory "$STARTUP_RUN_DIR")"

UVICORN_ARGS=(python3 -m uvicorn backend.main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" --workers "$BACKEND_WORKERS")
if [ "$ASR_HTTPS_ENABLED" = "true" ]; then
    [ -f "$ASR_CA_CERTIFICATE" ] && [ -f "$ASR_CERTIFICATE" ] && [ -f "$ASR_PRIVATE_KEY" ] || { echo "ASR HTTPS CA/certificate/key missing"; exit 1; }
    UVICORN_ARGS+=(--ssl-keyfile "$ASR_PRIVATE_KEY" --ssl-certfile "$ASR_CERTIFICATE")
fi
LOCAL_MEMORY_STARTUP_GATE="$STARTUP_RUN_DIR/memory.gate" LOCAL_MEMORY_STARTUP_NONCE="$STARTUP_NONCE" "${UVICORN_ARGS[@]}" &
BACKEND_PID=$!

if wait_for_backend_ready; then
    echo "✅ 后端已启动: ${BACKEND_SCHEME}://127.0.0.1:${BACKEND_PORT}"
    echo "👉 现在可以去开启前端了。"
    echo "ℹ️ SearXNG 将在后端完全就绪后继续准备；首次启动可能需要较长时间下载镜像。"
    echo "ℹ️ 如果 SearXNG 尚未就绪，仅网络搜索功能受影响，非搜索功能保持可用。"

    # Dispatch the supervised attempt first; readiness never holds this gate.
    setsid "$SHARED_SGLANG_PYTHON" -m backend.tools.backend_startup_support attempt \
        --receipt "$STARTUP_RUN_DIR/searxng.json" --deploy "$SEARXNG_DEPLOY_DIR" \
        --base-url "$SEARXNG_BASE_URL" --budget "$SEARXNG_ATTEMPT_TIMEOUT" --enabled "$SEARXNG_AUTOSTART" &
    SEARXNG_WORKER_PID=$!
    printf '%s\n' "$SEARXNG_WORKER_PID" > "$STARTUP_RUN_DIR/worker.pid"
    "$SHARED_SGLANG_PYTHON" -m backend.tools.backend_startup_support release \
        --directory "$STARTUP_RUN_DIR" --nonce="$STARTUP_NONCE"

else
    echo "❌ 后端健康检查未通过，终止启动流程。"
    if [ -n "$BACKEND_PID" ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
        kill -TERM "$BACKEND_PID" 2>/dev/null || true
        stop_process_bounded "$BACKEND_PID"
    fi
    exit 1
fi

wait "$BACKEND_PID"

