#!/bin/bash
# vLLM 服务器启动脚本
# 用于启动 vLLM 推理服务
# 使用独立的 .vllm uv 虚拟环境

set -uo pipefail

# ============================================================
# 1. 获取项目根目录 & 配置文件
# ============================================================
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_FILE="$PROJECT_ROOT/config/config.ini"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "❌ 配置文件不存在: $CONFIG_FILE"
    exit 1
fi

# ============================================================
# 2. 激活 .vllm uv 虚拟环境
# ============================================================
VLLM_VENV="$PROJECT_ROOT/.vllm"

if [ ! -d "$VLLM_VENV" ]; then
    echo "❌ .vllm 虚拟环境不存在，请先创建: uv venv .vllm --python 3.12"
    exit 1
fi

source "$VLLM_VENV/bin/activate"

echo "🐍 Python 环境: $(which python3)"
echo "📦 vLLM 版本: $(python3 -c 'import vllm; print(vllm.__version__)' 2>/dev/null || echo '<未安装>')"

# ============================================================
# 3. 从 config.ini 解析配置
# ============================================================

# 读取模型路径（从 [model] 部分）
MODEL_PATH=$(grep "^model_path" "$CONFIG_FILE" | head -1 | cut -d'=' -f2 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
MODEL_NAME=$(grep "^model_name" "$CONFIG_FILE" | head -1 | cut -d'=' -f2 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

# 读取 vLLM 配置（从 [vllm] 部分）
VLLM_CONFIG=$(sed -n '/^\[vllm\]/,/^\[/p' "$CONFIG_FILE" | grep ' = ' | sed 's/^[[:space:]]*//')

# ============================================================
# 3.1 参数有效性判断函数（过滤空值/null/none）
# ============================================================
trim_value() {
    echo "${1:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

safe_parse() {
    echo "$VLLM_CONFIG" | grep "^$1 =" | head -1 | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' || echo ""
}

is_set_value() {
    local value
    value="$(trim_value "${1:-}")"
    [[ -n "$value" && ! "$value" =~ ^([Nn][Uu][Ll][Ll]|[Nn][Oo][Nn][Ee])$ ]]
}

is_true() {
    local value
    value="$(trim_value "${1:-}")"
    [[ "$value" =~ ^([Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]|[Oo][Nn])$ ]]
}

# 解析各个配置项 - 基础配置
VLLM_HOST=$(safe_parse "host")
VLLM_PORT=$(safe_parse "port")
TENSOR_PARALLEL_SIZE=$(safe_parse "tensor_parallel_size")
PIPELINE_PARALLEL_SIZE=$(safe_parse "pipeline_parallel_size")
DISTRIBUTED_EXECUTOR_BACKEND=$(safe_parse "distributed_executor_backend")
MAX_MODEL_LEN=$(echo "$VLLM_CONFIG" | grep '^max_model_len =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
GPU_MEMORY_UTILIZATION=$(echo "$VLLM_CONFIG" | grep '^gpu_memory_utilization =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
DTYPE=$(echo "$VLLM_CONFIG" | grep '^dtype =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
QUANTIZATION=$(echo "$VLLM_CONFIG" | grep '^quantization =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

# 模型加载配置
TOKENIZER=$(echo "$VLLM_CONFIG" | grep '^tokenizer =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
REVISION=$(echo "$VLLM_CONFIG" | grep '^revision =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
TOKENIZER_REVISION=$(echo "$VLLM_CONFIG" | grep '^tokenizer_revision =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
LOAD_FORMAT=$(echo "$VLLM_CONFIG" | grep '^load_format =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
DOWNLOAD_DIR=$(echo "$VLLM_CONFIG" | grep '^download_dir =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
TRUST_REMOTE_CODE=$(echo "$VLLM_CONFIG" | grep '^trust_remote_code =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
ENFORCE_EAGER=$(echo "$VLLM_CONFIG" | grep '^enforce_eager =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

# 批处理与内存配置
MAX_NUM_SEQS=$(echo "$VLLM_CONFIG" | grep '^max_num_seqs =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
MAX_NUM_BATCHED_TOKENS=$(echo "$VLLM_CONFIG" | grep '^max_num_batched_tokens =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
BLOCK_SIZE=$(echo "$VLLM_CONFIG" | grep '^block_size =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
ENABLE_PREFIX_CACHING=$(echo "$VLLM_CONFIG" | grep '^enable_prefix_caching =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
KV_CACHE_DTYPE=$(echo "$VLLM_CONFIG" | grep '^kv_cache_dtype =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
SWAP_SPACE=$(echo "$VLLM_CONFIG" | grep '^swap_space =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
CPU_OFFLOAD_GB=$(echo "$VLLM_CONFIG" | grep '^cpu_offload_gb =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

# Attention 配置
ATTENTION_BACKEND=$(echo "$VLLM_CONFIG" | grep '^attention_backend =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
DISABLE_FLASH_ATTN=$(echo "$VLLM_CONFIG" | grep '^disable_flash_attn =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

# 性能优化配置
ENABLE_CHUNKED_PREFILL=$(echo "$VLLM_CONFIG" | grep '^enable_chunked_prefill =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
MAX_SEQ_LEN_TO_CAPTURE=$(echo "$VLLM_CONFIG" | grep '^max_seq_len_to_capture =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
DISABLE_SLIDING_WINDOW=$(echo "$VLLM_CONFIG" | grep '^disable_sliding_window =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
DISABLE_LOG_STATS=$(echo "$VLLM_CONFIG" | grep '^disable_log_stats =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
DISABLE_LOG_REQUESTS=$(echo "$VLLM_CONFIG" | grep '^disable_log_requests =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
MAX_LOGPROBS=$(echo "$VLLM_CONFIG" | grep '^max_logprobs =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
ENABLE_PROMPT_LOGPROBS=$(echo "$VLLM_CONFIG" | grep '^enable_prompt_logprobs =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
GUIDED_DECODING_BACKEND=$(echo "$VLLM_CONFIG" | grep '^guided_decoding_backend =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
DISABLE_CUSTOM_ALL_REDUCE=$(echo "$VLLM_CONFIG" | grep '^disable_custom_all_reduce =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

# 推理模式配置
ENABLE_REASONING=$(echo "$VLLM_CONFIG" | grep '^enable_reasoning =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
REASONING_PARSER=$(echo "$VLLM_CONFIG" | grep '^reasoning_parser =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
CHAT_TEMPLATE=$(echo "$VLLM_CONFIG" | grep '^chat_template =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
TOOL_CALL_PARSER=$(echo "$VLLM_CONFIG" | grep '^tool_call_parser =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
ENABLE_AUTO_TOOL_CHOICE=$(echo "$VLLM_CONFIG" | grep '^enable_auto_tool_choice =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
SERVED_MODEL_NAME=$(echo "$VLLM_CONFIG" | grep '^served_model_name =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

# LoRA 配置
ENABLE_LORA=$(echo "$VLLM_CONFIG" | grep '^enable_lora =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
MAX_LORAS=$(echo "$VLLM_CONFIG" | grep '^max_loras =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
MAX_LORA_RANK=$(echo "$VLLM_CONFIG" | grep '^max_lora_rank =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
LORA_EXTRA_VOCAB_SIZE=$(echo "$VLLM_CONFIG" | grep '^lora_extra_vocab_size =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
MAX_CPU_LORAS=$(echo "$VLLM_CONFIG" | grep '^max_cpu_loras =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
FULLY_SHARDED_LORAS=$(echo "$VLLM_CONFIG" | grep '^fully_sharded_loras =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

# 推测解码配置
SPECULATIVE_MODEL=$(echo "$VLLM_CONFIG" | grep '^speculative_model =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
NUM_SPECULATIVE_TOKENS=$(echo "$VLLM_CONFIG" | grep '^num_speculative_tokens =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
SPECULATIVE_MAX_MODEL_LEN=$(echo "$VLLM_CONFIG" | grep '^speculative_max_model_len =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
SPECULATIVE_DISABLE_BY_BATCH_SIZE=$(echo "$VLLM_CONFIG" | grep '^speculative_disable_by_batch_size =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
NGRAM_PROMPT_LOOKUP_MAX=$(echo "$VLLM_CONFIG" | grep '^ngram_prompt_lookup_max =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
NGRAM_PROMPT_LOOKUP_MIN=$(echo "$VLLM_CONFIG" | grep '^ngram_prompt_lookup_min =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

# 多模态配置
ENABLE_MULTI_MODAL=$(echo "$VLLM_CONFIG" | grep '^enable_multi_modal =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
DISABLE_MM_PREPROCESSOR_CACHE=$(echo "$VLLM_CONFIG" | grep '^disable_mm_preprocessor_cache =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

# 调度器配置
PREEMPTION_MODE=$(echo "$VLLM_CONFIG" | grep '^preemption_mode =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
SCHEDULER_DELAY_FACTOR=$(echo "$VLLM_CONFIG" | grep '^scheduler_delay_factor =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

# 其他配置
MODEL_LOADER_EXTRA_CONFIG=$(echo "$VLLM_CONFIG" | grep '^model_loader_extra_config =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
IGNORE_PATTERNS=$(echo "$VLLM_CONFIG" | grep '^ignore_patterns =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
MAX_PARALLEL_LOADING_WORKERS=$(echo "$VLLM_CONFIG" | grep '^max_parallel_loading_workers =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

# ============================================================
# 4. 确保 logs 目录存在 & 生成日志文件名
# ============================================================
mkdir -p "$PROJECT_ROOT/logs"
LOG_FILE="$PROJECT_ROOT/logs/vllm_$(date +%Y%m%d_%H%M%S).log"

# ============================================================
# 5. 环境变量设置 —— Jetson Thor (Blackwell sm_110) / UMA 平台优化
# ============================================================

# ---- CUDA 工具链路径 ----
# 设置正确的 CUDA 架构列表（sm_110 = compute capability 11.0a）
export TORCH_CUDA_ARCH_LIST=11.0a
export CUDA_HOME=/usr/local/cuda-13.1
export PATH=/usr/local/cuda-13.1/bin:$PATH

# ---- CUDA 内核说明 ----
# PyTorch 2.9.1+cu130 包含原生 sm_110 SASS，直接可用
# vLLM 自定义算子必须从源码编译才能获得 sm_110 SASS
# pip 预编译 wheel 仅包含 sm_75/80/87/89/90/100/120，不含 sm_110
# 因此 Jetson Thor 必须使用源码编译的 vLLM（参见上方第 2 节）
# ⚠️ 注意：不要设 CUDA_FORCE_PTX_JIT=1，它会强制所有代码走 PTX 路径，
#   反而绕过 PyTorch 的原生 sm_110 SASS，导致 kernel 加载失败
#export CUDA_FORCE_PTX_JIT=1

# ---- CUDA 环境（使用官方 cu130 wheel，无需兼容层）----

# PyTorch lib 路径（vLLM 的 .so 需要链接 libtorch*.so / libc10*.so）
TORCH_LIB_DIR=$(python3 -c "import torch, os; print(os.path.join(os.path.dirname(torch.__file__), 'lib'))")

# 库路径：torch/lib + CUDA 13.0
# export LD_LIBRARY_PATH="${TORCH_LIB_DIR}:/usr/local/cuda-13.0/lib64:${LD_LIBRARY_PATH:-}"
export CPLUS_INCLUDE_PATH=/usr/local/cuda-13.0/targets/sbsa-linux/include:${CPLUS_INCLUDE_PATH:-}

# ---- Triton 相关（vLLM 内部可能使用 Triton kernel）----
# export TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas
export TRITON_PTXAS_PATH="$PROJECT_ROOT/patch/ptxas_wrapper.sh"
export TRITON_PTXAS_BLACKWELL_PATH="$PROJECT_ROOT/patch/ptxas_wrapper.sh"
# 限制 Triton JIT 编译并发线程数（UMA 上编译器临时 buffer 也吃 GPU 内存）
#export TRITON_NUM_COMPILE_THREADS=1

# Triton 在 Blackwell 上需要特殊处理
export TRITON_BACKEND=CUDA
export PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True"

# ---- UMA / Jetson Thor 内存优化 ----
# 注意：不设置 CUDA_MODULE_LOADING=LAZY，在 sm_110 上可能导致 cuBLAS 延迟加载问题
# export CUDA_MODULE_LOADING=LAZY
# 同步 CUDA 调用，方便调试 kernel 错误（生产环境可注释掉）
# export CUDA_LAUNCH_BLOCKING=1
# PyTorch CUDA 分配器优化（UMA 上无 VRAM 边界，需限制分配器行为）
# export PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True,max_split_size_mb:256,garbage_collection_threshold:0.5"
# 允许 vLLM 在 Blackwell 上使用 FP4 量化感知
# (NVFP4 混合精度：MLP Experts=FP4(E2M1), Attention(MLA)=BF16)

# ---- vLLM 特定环境变量 ----
# 注意：vLLM >= 0.8.0 已移除 V0 引擎，VLLM_USE_V1 环境变量不再生效
# V1 引擎是唯一可用引擎，无需（也不能）切换回 V0

# 注意力后端通过 config.ini 中的 attention_backend 参数控制

# ============================================================
# 7. 构建 vLLM 启动命令
# ============================================================
# vLLM 使用 `vllm serve` 命令启动 OpenAI 兼容服务器
ARGS=(python3 -m vllm.entrypoints.openai.api_server
    --model "$MODEL_PATH"
)

# 基础配置
is_set_value "$VLLM_HOST" && ARGS+=(--host "$(trim_value "$VLLM_HOST")")
is_set_value "$VLLM_PORT" && ARGS+=(--port "$(trim_value "$VLLM_PORT")")
is_set_value "$TENSOR_PARALLEL_SIZE" && ARGS+=(--tensor-parallel-size "$(trim_value "$TENSOR_PARALLEL_SIZE")")
is_set_value "$PIPELINE_PARALLEL_SIZE" && [ "$(trim_value "$PIPELINE_PARALLEL_SIZE")" != "1" ] && ARGS+=(--pipeline-parallel-size "$(trim_value "$PIPELINE_PARALLEL_SIZE")")
is_set_value "$DISTRIBUTED_EXECUTOR_BACKEND" && ARGS+=(--distributed-executor-backend "$(trim_value "$DISTRIBUTED_EXECUTOR_BACKEND")")
is_set_value "$MAX_MODEL_LEN" && ARGS+=(--max-model-len "$(trim_value "$MAX_MODEL_LEN")")
is_set_value "$GPU_MEMORY_UTILIZATION" && ARGS+=(--gpu-memory-utilization "$(trim_value "$GPU_MEMORY_UTILIZATION")")
is_set_value "$DTYPE" && ARGS+=(--dtype "$(trim_value "$DTYPE")")
is_set_value "$QUANTIZATION" && ARGS+=(--quantization "$(trim_value "$QUANTIZATION")")

# 模型加载配置
is_set_value "$TOKENIZER" && ARGS+=(--tokenizer "$(trim_value "$TOKENIZER")")
is_set_value "$REVISION" && ARGS+=(--revision "$(trim_value "$REVISION")")
is_set_value "$TOKENIZER_REVISION" && ARGS+=(--tokenizer-revision "$(trim_value "$TOKENIZER_REVISION")")
is_set_value "$LOAD_FORMAT" && [ "$(trim_value "$LOAD_FORMAT")" != "auto" ] && ARGS+=(--load-format "$(trim_value "$LOAD_FORMAT")")
is_set_value "$DOWNLOAD_DIR" && ARGS+=(--download-dir "$(trim_value "$DOWNLOAD_DIR")")
is_true "$TRUST_REMOTE_CODE" && ARGS+=(--trust-remote-code)
is_true "$ENFORCE_EAGER" && ARGS+=(--enforce-eager)

# 批处理与内存配置
is_set_value "$MAX_NUM_SEQS" && ARGS+=(--max-num-seqs "$(trim_value "$MAX_NUM_SEQS")")
is_set_value "$MAX_NUM_BATCHED_TOKENS" && [ "$(trim_value "$MAX_NUM_BATCHED_TOKENS")" != "0" ] && ARGS+=(--max-num-batched-tokens "$(trim_value "$MAX_NUM_BATCHED_TOKENS")")
is_set_value "$BLOCK_SIZE" && ARGS+=(--block-size "$(trim_value "$BLOCK_SIZE")")
is_true "$ENABLE_PREFIX_CACHING" && ARGS+=(--enable-prefix-caching)
is_set_value "$KV_CACHE_DTYPE" && [ "$(trim_value "$KV_CACHE_DTYPE")" != "auto" ] && ARGS+=(--kv-cache-dtype "$(trim_value "$KV_CACHE_DTYPE")")
is_set_value "$SWAP_SPACE" && ARGS+=(--swap-space "$(trim_value "$SWAP_SPACE")")
is_set_value "$CPU_OFFLOAD_GB" && [ "$(trim_value "$CPU_OFFLOAD_GB")" != "0" ] && ARGS+=(--cpu-offload-gb "$(trim_value "$CPU_OFFLOAD_GB")")

# Attention 配置
is_set_value "$ATTENTION_BACKEND" && ARGS+=(--attention-backend "$(trim_value "$ATTENTION_BACKEND")")
# is_true "$DISABLE_FLASH_ATTN" && ARGS+=(--disable-flash-attn)

# 性能优化配置
is_true "$ENABLE_CHUNKED_PREFILL" && ARGS+=(--enable-chunked-prefill)
is_set_value "$MAX_SEQ_LEN_TO_CAPTURE" && [ "$(trim_value "$MAX_SEQ_LEN_TO_CAPTURE")" != "0" ] && ARGS+=(--max-seq-len-to-capture "$(trim_value "$MAX_SEQ_LEN_TO_CAPTURE")")
is_true "$DISABLE_SLIDING_WINDOW" && ARGS+=(--disable-sliding-window)
is_true "$DISABLE_LOG_STATS" && ARGS+=(--disable-log-stats)
is_true "$DISABLE_LOG_REQUESTS" && ARGS+=(--disable-log-requests)
is_set_value "$MAX_LOGPROBS" && ARGS+=(--max-logprobs "$(trim_value "$MAX_LOGPROBS")")
is_true "$ENABLE_PROMPT_LOGPROBS" && ARGS+=(--enable-prompt-logprobs)
is_set_value "$GUIDED_DECODING_BACKEND" && ARGS+=(--guided-decoding-backend "$(trim_value "$GUIDED_DECODING_BACKEND")")
is_true "$DISABLE_CUSTOM_ALL_REDUCE" && ARGS+=(--disable-custom-all-reduce)

# 推理模式配置
is_true "$ENABLE_REASONING" && ARGS+=(--enable-reasoning)
is_set_value "$REASONING_PARSER" && ARGS+=(--reasoning-parser "$(trim_value "$REASONING_PARSER")")
is_set_value "$CHAT_TEMPLATE" && ARGS+=(--chat-template "$(trim_value "$CHAT_TEMPLATE")")
is_set_value "$TOOL_CALL_PARSER" && ARGS+=(--tool-call-parser "$(trim_value "$TOOL_CALL_PARSER")")
is_true "$ENABLE_AUTO_TOOL_CHOICE" && ARGS+=(--enable-auto-tool-choice)
is_set_value "$SERVED_MODEL_NAME" && ARGS+=(--served-model-name "$(trim_value "$SERVED_MODEL_NAME")")

# LoRA 配置
is_true "$ENABLE_LORA" && ARGS+=(--enable-lora)
is_set_value "$MAX_LORAS" && [ "$(trim_value "$MAX_LORAS")" != "1" ] && ARGS+=(--max-loras "$(trim_value "$MAX_LORAS")")
is_set_value "$MAX_LORA_RANK" && [ "$(trim_value "$MAX_LORA_RANK")" != "16" ] && ARGS+=(--max-lora-rank "$(trim_value "$MAX_LORA_RANK")")
is_set_value "$LORA_EXTRA_VOCAB_SIZE" && [ "$(trim_value "$LORA_EXTRA_VOCAB_SIZE")" != "256" ] && ARGS+=(--lora-extra-vocab-size "$(trim_value "$LORA_EXTRA_VOCAB_SIZE")")
is_set_value "$MAX_CPU_LORAS" && [ "$(trim_value "$MAX_CPU_LORAS")" != "0" ] && ARGS+=(--max-cpu-loras "$(trim_value "$MAX_CPU_LORAS")")
is_true "$FULLY_SHARDED_LORAS" && ARGS+=(--fully-sharded-loras)

# 推测解码配置
is_set_value "$SPECULATIVE_MODEL" && ARGS+=(--speculative-model "$(trim_value "$SPECULATIVE_MODEL")")
is_set_value "$NUM_SPECULATIVE_TOKENS" && [ "$(trim_value "$NUM_SPECULATIVE_TOKENS")" != "0" ] && ARGS+=(--num-speculative-tokens "$(trim_value "$NUM_SPECULATIVE_TOKENS")")
is_set_value "$SPECULATIVE_MAX_MODEL_LEN" && [ "$(trim_value "$SPECULATIVE_MAX_MODEL_LEN")" != "0" ] && ARGS+=(--speculative-max-model-len "$(trim_value "$SPECULATIVE_MAX_MODEL_LEN")")
is_set_value "$SPECULATIVE_DISABLE_BY_BATCH_SIZE" && [ "$(trim_value "$SPECULATIVE_DISABLE_BY_BATCH_SIZE")" != "0" ] && ARGS+=(--speculative-disable-by-batch-size "$(trim_value "$SPECULATIVE_DISABLE_BY_BATCH_SIZE")")
is_set_value "$NGRAM_PROMPT_LOOKUP_MAX" && [ "$(trim_value "$NGRAM_PROMPT_LOOKUP_MAX")" != "0" ] && ARGS+=(--ngram-prompt-lookup-max "$(trim_value "$NGRAM_PROMPT_LOOKUP_MAX")")
is_set_value "$NGRAM_PROMPT_LOOKUP_MIN" && [ "$(trim_value "$NGRAM_PROMPT_LOOKUP_MIN")" != "0" ] && ARGS+=(--ngram-prompt-lookup-min "$(trim_value "$NGRAM_PROMPT_LOOKUP_MIN")")

# 多模态配置
is_true "$ENABLE_MULTI_MODAL" && ARGS+=(--enable-multi-modal)
is_true "$DISABLE_MM_PREPROCESSOR_CACHE" && ARGS+=(--disable-mm-preprocessor-cache)

# 调度器配置
is_set_value "$PREEMPTION_MODE" && ARGS+=(--preemption-mode "$(trim_value "$PREEMPTION_MODE")")
is_set_value "$SCHEDULER_DELAY_FACTOR" && [ "$(trim_value "$SCHEDULER_DELAY_FACTOR")" != "0.0" ] && ARGS+=(--scheduler-delay-factor "$(trim_value "$SCHEDULER_DELAY_FACTOR")")

# 其他配置
is_set_value "$MODEL_LOADER_EXTRA_CONFIG" && ARGS+=(--model-loader-extra-config "$(trim_value "$MODEL_LOADER_EXTRA_CONFIG")")
is_set_value "$IGNORE_PATTERNS" && ARGS+=(--ignore-patterns "$(trim_value "$IGNORE_PATTERNS")")
is_set_value "$MAX_PARALLEL_LOADING_WORKERS" && [ "$(trim_value "$MAX_PARALLEL_LOADING_WORKERS")" != "0" ] && ARGS+=(--max-parallel-loading-workers "$(trim_value "$MAX_PARALLEL_LOADING_WORKERS")")

echo "🚀 实际启动命令："
printf '  %q' "${ARGS[@]}"; echo
echo ""
echo "=============================================================================="
echo ""

# ============================================================
# 8. 启动 vLLM 服务
# ============================================================


"${ARGS[@]}" 2>&1 | tee "$LOG_FILE"

echo ""
echo "=============================================================================="
echo "✅ vLLM 服务已停止"
echo "📝 日志已保存到: $LOG_FILE"
echo "=============================================================================="
