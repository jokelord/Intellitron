#!/bin/bash
# 【跑任何 vLLM 之前，记得先清空显存】
# Start a model directory without Hugging Face snapshot indirection.

set -euo pipefail

CONTAINER_MODEL_PATH="/model"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
YAML_OVERALL="${CONFIG_OVERALL_PATH:-$PROJECT_ROOT/config/config-overall.yaml}"
DOCKER_BIN="${VLLM_DOCKER_BIN:-docker}"
CONTAINER_TAG="nvcr.io/nvidia/vllm:26.06-py3"

fail() {
    printf '❌ %s\n' "$1" >&2
    exit 1
}

require_absolute_dir_path() {
    [[ "$2" == /* ]] || fail "$1 必须是绝对路径: $2"
}

is_set_value() {
    local value="${1:-}"
    [[ -n "$value" && "$value" != "null" && "$value" != "None" ]]
}

is_true() {
    [[ "${1:-}" = "true" ]]
}

append_boolean_arg() {
    local value="$1"
    local true_flag="$2"
    local false_flag="$3"

    if [ "$value" = "true" ]; then
        VLLM_ARGS+=("$true_flag")
    elif [ "$value" = "false" ]; then
        VLLM_ARGS+=("$false_flag")
    fi
}

reject_legacy_value() {
    local config_key="$1"
    local value="$2"

    if is_set_value "$value"; then
        fail "vllm.${config_key} 在 vLLM 26.06 中没有可安全映射的 CLI 参数；请移除该值或改用 vLLM 26.06 的对应配置。"
    fi
}

reject_legacy_boolean() {
    local config_key="$1"
    local value="$2"

    if is_set_value "$value"; then
        fail "vllm.${config_key} 在 vLLM 26.06 中没有可安全映射的 CLI 参数；请移除该值或改用 vLLM 26.06 的对应配置。"
    fi
}

supports_api_flag() {
    local expected_flag="$1"
    local listed_flag

    while IFS= read -r listed_flag; do
        if [ "$listed_flag" = "$expected_flag" ]; then
            return 0
        fi
    done < <(printf '%s\n' "$API_SERVER_HELP" | grep -oE -- '--[A-Za-z0-9][A-Za-z0-9-]*')

    return 1
}

load_api_server_help() {
    local help_stderr
    local diagnostic

    help_stderr="$(mktemp "${TMPDIR:-/tmp}/vllm-api-server-help.XXXXXX")" || fail "无法创建 vLLM 参数校验临时文件"
    if ! API_SERVER_HELP="$("$DOCKER_BIN" run --rm --entrypoint python3 "$CONTAINER_TAG" -m vllm.entrypoints.openai.api_server --help 2>"$help_stderr")"; then
        diagnostic="$(<"$help_stderr")"
        rm -f "$help_stderr"
        if [ -n "$diagnostic" ]; then
            printf '%s\n' "$diagnostic" >&2
        fi
        fail "无法读取 vLLM 26.06 目标镜像的 api_server --help"
    fi
    rm -f "$help_stderr"
}

[ -f "$YAML_OVERALL" ] || fail "配置文件不存在: $YAML_OVERALL"

# 旧版（26.03）引入的持久化编译缓存方案暂时禁用，供 26.06 端到端实验使用。
# 如需恢复，重新启用以下变量、目录创建、Docker 挂载和环境变量。
# CACHE_TAG="${CONTAINER_TAG##*:}"
# DEFAULT_CACHE_ROOT="${XDG_CACHE_HOME:-${HOME}/.cache}/vllm-thor/${CACHE_TAG}"
# VLLM_CACHE_ROOT_HOST="${VLLM_CACHE_ROOT_HOST:-$DEFAULT_CACHE_ROOT}"
# require_absolute_dir_path "VLLM_CACHE_ROOT_HOST" "$VLLM_CACHE_ROOT_HOST"
# VLLM_CACHE_HOST_PATH="${VLLM_CACHE_ROOT_HOST}/vllm"
# TRITON_CACHE_HOST_PATH="${VLLM_CACHE_ROOT_HOST}/triton"
# TORCHINDUCTOR_CACHE_HOST_PATH="${VLLM_CACHE_ROOT_HOST}/torchinductor"

CONFIG_VALUES="$(python3 - "$PROJECT_ROOT" "$YAML_OVERALL" <<'PYEOF'
import json
import re
import shlex
import sys
from pathlib import Path

import yaml

project_root = Path(sys.argv[1]).resolve()
overall_path = Path(sys.argv[2]).resolve()
overall = yaml.safe_load(overall_path.read_text(encoding="utf-8")) or {}

def selected_path(key, *, required=True):
    value = overall.get(key)
    if not value:
        if required:
            raise SystemExit(f"config-overall 未配置 {key}")
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_root / path
    path = path.resolve()
    if not path.is_file():
        raise SystemExit(f"配置文件不存在: {path}")
    return path

def sibling_model_path(infer_path):
    name = infer_path.name
    if "-config-infer.yaml" not in name:
        return None
    sibling = infer_path.parent / name.replace("-config-infer.yaml", "-config-model.yaml")
    return sibling if sibling.is_file() else None

infer_path = selected_path("infer")
# model 键已移除：优先读 infer 内嵌 model 段，缺失时回退 sibling（旧文件兼容）。
model_path = selected_path("model", required=False)
if model_path is None:
    model_path = sibling_model_path(infer_path)
infer_data = yaml.safe_load(infer_path.read_text(encoding="utf-8")) or {}
model_infer = infer_data.get("model", {}) or {}
model_file = {}
if model_path is not None:
    model_file = (yaml.safe_load(model_path.read_text(encoding="utf-8")) or {}).get("model", {}) or {}
model = dict(model_file)
for _k, _v in model_infer.items():
    if _v is not None:
        model[_k] = _v
vllm = infer_data.get("vllm", {}) or {}
if model_path is None:
    model_path = infer_path

model_location = model.get("model_path")
if not model_location:
    raise SystemExit("model.model_path 未配置（请在 infer 内嵌 model 段中指定）")
model_location = Path(model_location).expanduser()
if not model_location.is_absolute():
    model_location = model_path.parent / model_location
model_name = str(model.get("model_name") or "vllm")
docker_name = re.sub(r"[^a-z0-9_.-]+", "-", model_name.lower()).strip("-.") or "vllm"

scalar_keys = (
    "host", "port", "tensor_parallel_size", "pipeline_parallel_size",
    "distributed_executor_backend", "max_model_len", "gpu_memory_utilization",
    "dtype", "quantization", "tokenizer", "revision", "tokenizer_revision",
    "load_format", "download_dir", "trust_remote_code", "enforce_eager",
    "max_num_seqs", "max_num_batched_tokens", "block_size",
    "enable_prefix_caching", "kv_cache_dtype", "cpu_offload_gb",
    "attention_backend", "flash_attn_version", "enable_chunked_prefill",
    "disable_sliding_window", "disable_log_stats", "disable_log_requests",
    "max_logprobs", "disable_custom_all_reduce", "reasoning_parser",
    "chat_template", "tool_call_parser", "tool_parser_plugin", "enable_auto_tool_choice",
    "served_model_name", "enable_lora", "max_loras", "max_lora_rank",
    "max_cpu_loras", "fully_sharded_loras", "skip_mm_profiling",
    "model_loader_extra_config", "ignore_patterns", "max_parallel_loading_workers",
    "disable_flash_attn", "enable_reasoning", "enable_multi_modal",
    "disable_mm_preprocessor_cache", "guided_decoding_backend", "preemption_mode",
    "scheduler_delay_factor", "speculative_model", "num_speculative_tokens",
    "speculative_max_model_len", "speculative_disable_by_batch_size",
    "ngram_prompt_lookup_max", "ngram_prompt_lookup_min", "swap_space",
    "max_seq_len_to_capture", "enable_prompt_logprobs", "lora_extra_vocab_size",
)

def shell_value(value, *, as_json=False):
    if value is None:
        return ""
    if as_json:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)

values = {
    "MODEL_PATH": shell_value(model_location.resolve()),
    "CONTAINER_NAME": shell_value(docker_name),
}
for key in scalar_keys:
    name = f"VLLM_{key.upper()}" if key in {"host", "port"} else key.upper()
    values[name] = shell_value(vllm.get(key))
values["HF_OVERRIDES"] = shell_value(vllm.get("hf_overrides"), as_json=True)
values["LIMIT_MM_PER_PROMPT"] = shell_value(vllm.get("limit_mm_per_prompt"), as_json=True)
model_loader_extra_config = vllm.get("model_loader_extra_config")
values["MODEL_LOADER_EXTRA_CONFIG"] = shell_value(
    model_loader_extra_config,
    as_json=isinstance(model_loader_extra_config, (dict, list)),
)
flash_attn_version = vllm.get("flash_attn_version")
values["ATTENTION_CONFIG"] = (
    shell_value({"flash_attn_version": flash_attn_version}, as_json=True)
    if flash_attn_version is not None else ""
)

for name, value in values.items():
    print(f"{name}={shlex.quote(value)}")
PYEOF
)" || fail "无法读取 config-overall 选定的 model/infer 配置"
eval "$CONFIG_VALUES"

# Resolve repository-relative template paths before mapping the file into NGC.
CHAT_TEMPLATE_HOST=""
if is_set_value "$CHAT_TEMPLATE"; then
    CHAT_TEMPLATE_HOST="$CHAT_TEMPLATE"
    [[ "$CHAT_TEMPLATE_HOST" = /* ]] || CHAT_TEMPLATE_HOST="$PROJECT_ROOT/$CHAT_TEMPLATE_HOST"
    [[ -f "$CHAT_TEMPLATE_HOST" ]] || { echo "❌ Chat template 不存在: $CHAT_TEMPLATE_HOST"; exit 1; }
    CHAT_TEMPLATE="/opt/llm-chat-template.jinja"
fi

# Plugins are repository artifacts, mounted read-only without modifying vLLM.
TOOL_PARSER_PLUGIN_HOST=""
if is_set_value "$TOOL_PARSER_PLUGIN"; then
    TOOL_PARSER_PLUGIN_HOST="$TOOL_PARSER_PLUGIN"
    [[ "$TOOL_PARSER_PLUGIN_HOST" = /* ]] || TOOL_PARSER_PLUGIN_HOST="$PROJECT_ROOT/$TOOL_PARSER_PLUGIN_HOST"
    [[ -f "$TOOL_PARSER_PLUGIN_HOST" ]] || { echo "❌ Tool parser plugin 不存在: $TOOL_PARSER_PLUGIN_HOST"; exit 1; }
    TOOL_PARSER_PLUGIN="/opt/llm-tool-parser-plugin.py"
fi

[ -d "$MODEL_PATH" ] || fail "模型目录不存在: $MODEL_PATH"
HOST_MODEL_PATH="$MODEL_PATH"

# These settings were removed from the vLLM 26.06 image and have no direct
# equivalent, so every configured value is rejected.
reject_legacy_boolean "disable_flash_attn" "$DISABLE_FLASH_ATTN"
reject_legacy_boolean "enable_reasoning" "$ENABLE_REASONING"
reject_legacy_boolean "enable_multi_modal" "$ENABLE_MULTI_MODAL"
reject_legacy_boolean "disable_mm_preprocessor_cache" "$DISABLE_MM_PREPROCESSOR_CACHE"
reject_legacy_boolean "enable_prompt_logprobs" "$ENABLE_PROMPT_LOGPROBS"
reject_legacy_value "guided_decoding_backend" "$GUIDED_DECODING_BACKEND"
reject_legacy_value "preemption_mode" "$PREEMPTION_MODE"
reject_legacy_value "scheduler_delay_factor" "$SCHEDULER_DELAY_FACTOR"
reject_legacy_value "speculative_model" "$SPECULATIVE_MODEL"
reject_legacy_value "num_speculative_tokens" "$NUM_SPECULATIVE_TOKENS"
reject_legacy_value "speculative_max_model_len" "$SPECULATIVE_MAX_MODEL_LEN"
reject_legacy_value "speculative_disable_by_batch_size" "$SPECULATIVE_DISABLE_BY_BATCH_SIZE"
reject_legacy_value "ngram_prompt_lookup_max" "$NGRAM_PROMPT_LOOKUP_MAX"
reject_legacy_value "ngram_prompt_lookup_min" "$NGRAM_PROMPT_LOOKUP_MIN"
reject_legacy_value "swap_space" "$SWAP_SPACE"
reject_legacy_value "max_seq_len_to_capture" "$MAX_SEQ_LEN_TO_CAPTURE"
reject_legacy_value "lora_extra_vocab_size" "$LORA_EXTRA_VOCAB_SIZE"

VLLM_ARGS=(
    python3 -m vllm.entrypoints.openai.api_server
    --model "$CONTAINER_MODEL_PATH"
)

if is_set_value "$VLLM_HOST"; then
    VLLM_ARGS+=(--host "$VLLM_HOST")
fi
if is_set_value "$VLLM_PORT"; then
    VLLM_ARGS+=(--port "$VLLM_PORT")
fi
if is_set_value "$TENSOR_PARALLEL_SIZE"; then
    VLLM_ARGS+=(--tensor-parallel-size "$TENSOR_PARALLEL_SIZE")
fi
if is_set_value "$PIPELINE_PARALLEL_SIZE"; then
    VLLM_ARGS+=(--pipeline-parallel-size "$PIPELINE_PARALLEL_SIZE")
fi
if is_set_value "$DISTRIBUTED_EXECUTOR_BACKEND"; then
    VLLM_ARGS+=(--distributed-executor-backend "$DISTRIBUTED_EXECUTOR_BACKEND")
fi
if is_set_value "$MAX_MODEL_LEN"; then
    VLLM_ARGS+=(--max-model-len "$MAX_MODEL_LEN")
fi
if is_set_value "$GPU_MEMORY_UTILIZATION"; then
    VLLM_ARGS+=(--gpu-memory-utilization "$GPU_MEMORY_UTILIZATION")
fi
if is_set_value "$DTYPE"; then
    VLLM_ARGS+=(--dtype "$DTYPE")
fi
if is_set_value "$QUANTIZATION"; then
    VLLM_ARGS+=(--quantization "$QUANTIZATION")
fi
if is_set_value "$TOKENIZER"; then
    VLLM_ARGS+=(--tokenizer "$TOKENIZER")
fi
if is_set_value "$REVISION"; then
    VLLM_ARGS+=(--revision "$REVISION")
fi
if is_set_value "$TOKENIZER_REVISION"; then
    VLLM_ARGS+=(--tokenizer-revision "$TOKENIZER_REVISION")
fi
if is_set_value "$LOAD_FORMAT"; then
    VLLM_ARGS+=(--load-format "$LOAD_FORMAT")
fi
if is_set_value "$DOWNLOAD_DIR"; then
    VLLM_ARGS+=(--download-dir "$DOWNLOAD_DIR")
fi
append_boolean_arg "$TRUST_REMOTE_CODE" --trust-remote-code --no-trust-remote-code
append_boolean_arg "$ENFORCE_EAGER" --enforce-eager --no-enforce-eager
if is_set_value "$MAX_NUM_SEQS"; then
    VLLM_ARGS+=(--max-num-seqs "$MAX_NUM_SEQS")
fi
if is_set_value "$MAX_NUM_BATCHED_TOKENS"; then
    VLLM_ARGS+=(--max-num-batched-tokens "$MAX_NUM_BATCHED_TOKENS")
fi
if is_set_value "$BLOCK_SIZE"; then
    VLLM_ARGS+=(--block-size "$BLOCK_SIZE")
fi
append_boolean_arg "$ENABLE_PREFIX_CACHING" --enable-prefix-caching --no-enable-prefix-caching
if is_set_value "$KV_CACHE_DTYPE"; then
    VLLM_ARGS+=(--kv-cache-dtype "$KV_CACHE_DTYPE")
fi
if is_set_value "$CPU_OFFLOAD_GB"; then
    VLLM_ARGS+=(--cpu-offload-gb "$CPU_OFFLOAD_GB")
fi
if is_set_value "$ATTENTION_BACKEND"; then
    VLLM_ARGS+=(--attention-backend "$ATTENTION_BACKEND")
fi
if is_set_value "$ATTENTION_CONFIG"; then
    VLLM_ARGS+=(--attention-config "$ATTENTION_CONFIG")
fi
append_boolean_arg "$ENABLE_CHUNKED_PREFILL" --enable-chunked-prefill --no-enable-chunked-prefill
append_boolean_arg "$DISABLE_SLIDING_WINDOW" --disable-sliding-window --no-disable-sliding-window
if is_true "$DISABLE_LOG_STATS"; then
    VLLM_ARGS+=(--disable-log-stats)
fi
append_boolean_arg "$DISABLE_LOG_REQUESTS" --no-enable-log-requests --enable-log-requests
if is_set_value "$MAX_LOGPROBS"; then
    VLLM_ARGS+=(--max-logprobs "$MAX_LOGPROBS")
fi
append_boolean_arg "$DISABLE_CUSTOM_ALL_REDUCE" --disable-custom-all-reduce --no-disable-custom-all-reduce
if is_set_value "$REASONING_PARSER"; then
    VLLM_ARGS+=(--reasoning-parser "$REASONING_PARSER")
fi
if is_set_value "$CHAT_TEMPLATE"; then
    VLLM_ARGS+=(--chat-template "$CHAT_TEMPLATE")
fi
if is_set_value "$TOOL_CALL_PARSER"; then
    VLLM_ARGS+=(--tool-call-parser "$TOOL_CALL_PARSER")
fi
if is_set_value "$TOOL_PARSER_PLUGIN"; then
    VLLM_ARGS+=(--tool-parser-plugin "$TOOL_PARSER_PLUGIN")
fi
append_boolean_arg "$ENABLE_AUTO_TOOL_CHOICE" --enable-auto-tool-choice --no-enable-auto-tool-choice
if is_set_value "$SERVED_MODEL_NAME"; then
    VLLM_ARGS+=(--served-model-name "$SERVED_MODEL_NAME")
fi
append_boolean_arg "$ENABLE_LORA" --enable-lora --no-enable-lora
if is_set_value "$MAX_LORAS"; then
    VLLM_ARGS+=(--max-loras "$MAX_LORAS")
fi
if is_set_value "$MAX_LORA_RANK"; then
    VLLM_ARGS+=(--max-lora-rank "$MAX_LORA_RANK")
fi
if is_set_value "$MAX_CPU_LORAS"; then
    VLLM_ARGS+=(--max-cpu-loras "$MAX_CPU_LORAS")
fi
append_boolean_arg "$FULLY_SHARDED_LORAS" --fully-sharded-loras --no-fully-sharded-loras
if is_set_value "$HF_OVERRIDES"; then
    VLLM_ARGS+=(--hf-overrides "$HF_OVERRIDES")
fi
if is_set_value "$LIMIT_MM_PER_PROMPT"; then
    VLLM_ARGS+=(--limit-mm-per-prompt "$LIMIT_MM_PER_PROMPT")
fi
append_boolean_arg "$SKIP_MM_PROFILING" --skip-mm-profiling --no-skip-mm-profiling
if is_set_value "$MODEL_LOADER_EXTRA_CONFIG"; then
    VLLM_ARGS+=(--model-loader-extra-config "$MODEL_LOADER_EXTRA_CONFIG")
fi
if is_set_value "$IGNORE_PATTERNS"; then
    VLLM_ARGS+=(--ignore-patterns "$IGNORE_PATTERNS")
fi
if is_set_value "$MAX_PARALLEL_LOADING_WORKERS"; then
    VLLM_ARGS+=(--max-parallel-loading-workers "$MAX_PARALLEL_LOADING_WORKERS")
fi

load_api_server_help
for argument in "${VLLM_ARGS[@]}"; do
    if [[ "$argument" == --* ]] && ! supports_api_flag "$argument"; then
        fail "目标镜像不支持 vLLM 参数: $argument"
    fi
done

# 持久化编译缓存目录创建（26.03 方案）暂时禁用。
# mkdir -p "$VLLM_CACHE_HOST_PATH" "$TRITON_CACHE_HOST_PATH" "$TORCHINDUCTOR_CACHE_HOST_PATH"

echo "----------------------------------------"
echo "正在启动 Jetson Thor 优化推理服务..."
echo "正在清理旧容器并启动 Jetson Thor 优化推理服务..."
echo "----------------------------------------"

DOCKER_TTY_ARGS=(-i)
if [ -t 0 ] && [ -t 1 ]; then
    DOCKER_TTY_ARGS=(-it)
fi

docker_args=(
    --rm
    --gpus all
    "${DOCKER_TTY_ARGS[@]}"
    --ipc=host
    --network host
    -v "${HOST_MODEL_PATH}:${CONTAINER_MODEL_PATH}"
    # 持久化编译缓存挂载（26.03 方案）暂时禁用。
    # -v "${VLLM_CACHE_HOST_PATH}:/root/.cache/vllm"
    # -v "${TRITON_CACHE_HOST_PATH}:/root/.triton"
    # -v "${TORCHINDUCTOR_CACHE_HOST_PATH}:/tmp/torchinductor_root"
    --name "$CONTAINER_NAME"
    --ulimit memlock=-1
    --ulimit stack=67108864
    # 持久化编译缓存环境变量（26.03 方案）暂时禁用。
    # -e VLLM_CACHE_ROOT=/root/.cache/vllm
    # -e TORCHINDUCTOR_CACHE_DIR=/tmp/torchinductor_root
    # -e TRITON_CACHE_DIR=/root/.triton/cache
)

if [ -n "$CHAT_TEMPLATE_HOST" ]; then
    docker_args+=(-v "$CHAT_TEMPLATE_HOST:$CHAT_TEMPLATE:ro")
fi
if [ -n "$TOOL_PARSER_PLUGIN_HOST" ]; then
    docker_args+=(-v "$TOOL_PARSER_PLUGIN_HOST:$TOOL_PARSER_PLUGIN:ro")
fi

printf '实际启动命令:'
printf ' %q' "$DOCKER_BIN" run "${docker_args[@]}" "$CONTAINER_TAG" "${VLLM_ARGS[@]}"
printf '\n'
"$DOCKER_BIN" rm -f "$CONTAINER_NAME" > /dev/null 2>&1 || true
"$DOCKER_BIN" run "${docker_args[@]}" "$CONTAINER_TAG" "${VLLM_ARGS[@]}"
