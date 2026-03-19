#!/bin/bash
# SGLang 服务器启动脚本 - 简化版本
# 用于启动 SGLang 推理服务

# 获取项目根目录
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# 从配置文件读取配置
CONFIG_FILE="$PROJECT_ROOT/config/config.ini"
if [ -f "$CONFIG_FILE" ]; then
    # 读取模型路径
    MODEL_PATH_FROM_CONFIG=$(grep "^model_path" "$CONFIG_FILE" | cut -d'=' -f2 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    if [ -n "$MODEL_PATH_FROM_CONFIG" ]; then
        MODEL_PATH="${MODEL_PATH:-$MODEL_PATH_FROM_CONFIG}"
    fi
    
    # 读取 SGLang 配置（从 [sglang] 部分）
    SGLANG_CONFIG=$(sed -n '/^\[sglang\]/,/^\[/p' "$CONFIG_FILE" | grep -E '^(host|port|reasoning_parser|chat_template|tool_call_parser|disable_cuda_graph|fp8_gemm_backend|attention_backend|mm_attention_backend|tensor_parallel_size|max_context_length|mem_fraction_static|max_total_tokens|max_running_requests|max_prefill_tokens|chunked_prefill_size|mamba_full_memory_ratio|mamba_ssm_dtype|max_mamba_cache_size|skip_server_warmup|speculative_algorithm|speculative_draft_model_path|trust_remote_code|quantization|moe_runner_backend|dtype|image_max_pixels|image_min_pixels) =' | sed 's/^[[:space:]]*//')
    
    # 解析各个配置项
    SGLANG_HOST=$(echo "$SGLANG_CONFIG" | grep '^host =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    SGLANG_PORT=$(echo "$SGLANG_CONFIG" | grep '^port =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    REASONING_PARSER=$(echo "$SGLANG_CONFIG" | grep '^reasoning_parser =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    CHAT_TEMPLATE=$(echo "$SGLANG_CONFIG" | grep '^chat_template =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    TOOL_CALL_PARSER=$(echo "$SGLANG_CONFIG" | grep '^tool_call_parser =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    DISABLE_CUDA_GRAPH=$(echo "$SGLANG_CONFIG" | grep '^disable_cuda_graph =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    FP8_GEMM_BACKEND=$(echo "$SGLANG_CONFIG" | grep '^fp8_gemm_backend =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    ATTENTION_BACKEND=$(echo "$SGLANG_CONFIG" | grep '^attention_backend =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    MM_ATTENTION_BACKEND=$(echo "$SGLANG_CONFIG" | grep '^mm_attention_backend =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    TENSOR_PARALLEL_SIZE=$(echo "$SGLANG_CONFIG" | grep '^tensor_parallel_size =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    MAX_CONTEXT_LENGTH=$(echo "$SGLANG_CONFIG" | grep '^max_context_length =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    MEM_FRACTION_STATIC=$(echo "$SGLANG_CONFIG" | grep '^mem_fraction_static =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    MAX_TOTAL_TOKENS=$(echo "$SGLANG_CONFIG" | grep '^max_total_tokens =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    MAX_RUNNING_REQUESTS=$(echo "$SGLANG_CONFIG" | grep '^max_running_requests =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    MAX_PREFILL_TOKENS=$(echo "$SGLANG_CONFIG" | grep '^max_prefill_tokens =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    CHUNKED_PREFILL_SIZE=$(echo "$SGLANG_CONFIG" | grep '^chunked_prefill_size =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    MAMBA_FULL_MEMORY_RATIO=$(echo "$SGLANG_CONFIG" | grep '^mamba_full_memory_ratio =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    MAMBA_SSM_DTYPE=$(echo "$SGLANG_CONFIG" | grep '^mamba_ssm_dtype =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    SPECULATIVE_ALGORITHM=$(echo "$SGLANG_CONFIG" | grep '^speculative_algorithm =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    SPECULATIVE_DRAFT_MODEL_PATH=$(echo "$SGLANG_CONFIG" | grep '^speculative_draft_model_path =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    TRUST_REMOTE_CODE=$(echo "$SGLANG_CONFIG" | grep '^trust_remote_code =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    QUANTIZATION=$(echo "$SGLANG_CONFIG" | grep '^quantization =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    MOE_RUNNER_BACKEND=$(echo "$SGLANG_CONFIG" | grep '^moe_runner_backend =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    MAX_MAMBA_CACHE_SIZE=$(echo "$SGLANG_CONFIG" | grep '^max_mamba_cache_size =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    SKIP_SERVER_WARMUP=$(echo "$SGLANG_CONFIG" | grep '^skip_server_warmup =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    # Visual Encoder FP16 量化：降低 VLM 首 token 延迟（TTFT）
    DTYPE=$(echo "$SGLANG_CONFIG" | grep '^dtype =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    # Visual Token 预算控制：限制视觉编码器处理的像素数（TTFT 最关键参数）
    # IMAGE_MAX_PIXELS 和 IMAGE_MIN_PIXELS 已移除（当前版本 SGLang 不支持这两个参数）
    
    # 解析 KV cache 配置项 - 已废弃，不再使用
    # ENABLE_BLOCK_REUSE=$(echo "$KV_CACHE_CONFIG" | grep '^enable_block_reuse =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]')
    # MAX_CACHE_SIZE_MB=$(echo "$KV_CACHE_CONFIG" | grep '^max_cache_size_mb =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
fi

# 配置（使用环境变量或配置文件值）
MODEL_PATH="${MODEL_PATH:-/home/zk/Project/LLM-weight/Qwen3-30B-A3B-Thinking-2507-FP8/}"
HOST="${HOST:-${SGLANG_HOST}}"
PORT="${PORT:-${SGLANG_PORT}}"

# 参数验证和清理 - KV cache 参数已废弃，无需验证

# 确保 logs 目录存在
mkdir -p "$PROJECT_ROOT/logs"

# 生成日志文件名（带时间戳）
LOG_FILE="$PROJECT_ROOT/logs/sglang_$(date +%Y%m%d_%H%M%S).log"

# 激活虚拟环境
source /home/zk/Project/LLM-deployment/.sglang/bin/activate

# 设置 Triton PTXAS 路径
# export TRITON_PTXAS_PATH="$PROJECT_ROOT/patch/ptxas_wrapper.sh"
export TRITON_PTXAS_PATH=/usr/local/cuda-13.1/bin/ptxas
# 设置正确的 CUDA 架构列表
export TORCH_CUDA_ARCH_LIST=11.0a
# 设置库路径
export LD_LIBRARY_PATH=/usr/local/cuda-13.0/lib64:$LD_LIBRARY_PATH
export CPLUS_INCLUDE_PATH=/usr/local/cuda-13.0/targets/sbsa-linux/include:$CPLUS_INCLUDE_PATH
# 允许覆盖更长的上下文长度
export SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN=1
# cutlass FP8 内核启用 NVFP4
export SGLANG_USE_CUTLASS_NVFP4=

# VLM 启用 CUDA 图优化，激活ViTCudaGraphRunner，加速VLM首帧
# 要求每次传进来的图片分辨率都一样，否则会不断地进行 Graph Capture（捕获过程本身非常耗时，大约需要几百毫秒）
export SGLANG_VIT_ENABLE_CUDA_GRAPH=1

# ---- UMA / Jetson 内存优化 ----
# 禁用 DeepGemm JIT 编译（首因：DeepGemm 预编译会一次性编译所有 FP8 GEMM 内核变体，
# 在 UMA 上编译期间的临时 buffer 会无限吃系统内存直至 OOM。
# 且本模型 scale_fmt 非 ue8m0，DeepGemm 在 Blackwell 上可能精度下降。）
export SGLANG_ENABLE_JIT_DEEPGEMM=
# 如果希望保留 DeepGemm 但减少编译时间/内存，可改用快速预热（注释上行，取消注释下行）：
# export SGLANG_JIT_DEEPGEMM_FAST_WARMUP=1
# PyTorch CUDA 分配器优化（UMA 上无 VRAM 边界，需限制分配器行为）
export PYTORCH_ALLOC_CONF=
# export PYTORCH_ALLOC_CONF="expandable_segments:True,max_split_size_mb:256,garbage_collection_threshold:0.5"
# 延迟加载 CUDA 模块，减少驻留内存
export CUDA_MODULE_LOADING=
# export CUDA_MODULE_LOADING=LAZY
# 限制 Triton JIT 编译并发线程数（UMA 上编译器临时 buffer 也吃 GPU 内存）
export TRITON_NUM_COMPILE_THREADS=


# 启动 SGLang 服务，同时显示输出并保存到日志文件
ARGS=(python3 -m sglang.launch_server
    --model-path "$MODEL_PATH"
)

[ -n "$ATTENTION_BACKEND" ] && ARGS+=(--attention-backend "$ATTENTION_BACKEND")
[ -n "$MM_ATTENTION_BACKEND" ] && ARGS+=(--mm-attention-backend "$MM_ATTENTION_BACKEND")

[ -n "$SGLANG_HOST" ] && ARGS+=(--host "$SGLANG_HOST")
[ -n "$SGLANG_PORT" ] && ARGS+=(--port "$SGLANG_PORT")
[ -n "$MAX_CONTEXT_LENGTH" ] && ARGS+=(--context-length "$MAX_CONTEXT_LENGTH")
[ -n "$MEM_FRACTION_STATIC" ] && ARGS+=(--mem-fraction-static "$MEM_FRACTION_STATIC")
[ -n "$MAX_TOTAL_TOKENS" ] && ARGS+=(--max-total-tokens "$MAX_TOTAL_TOKENS")
[ -n "$MAX_RUNNING_REQUESTS" ] && ARGS+=(--max-running-requests "$MAX_RUNNING_REQUESTS")
[ -n "$MAX_PREFILL_TOKENS" ] && ARGS+=(--max-prefill-tokens "$MAX_PREFILL_TOKENS")
[ -n "$TENSOR_PARALLEL_SIZE" ] && ARGS+=(--tp-size "$TENSOR_PARALLEL_SIZE")
[ -n "$CHUNKED_PREFILL_SIZE" ] && ARGS+=(--chunked-prefill-size "$CHUNKED_PREFILL_SIZE")
[ -n "$MAMBA_FULL_MEMORY_RATIO" ] && ARGS+=(--mamba-full-memory-ratio "$MAMBA_FULL_MEMORY_RATIO")
[ -n "$MAMBA_SSM_DTYPE" ] && ARGS+=(--mamba-ssm-dtype "$MAMBA_SSM_DTYPE")
[ -n "$FP8_GEMM_BACKEND" ] && ARGS+=(--fp8-gemm-backend "$FP8_GEMM_BACKEND")
[ "$DISABLE_CUDA_GRAPH" = "true" ] && ARGS+=(--disable-cuda-graph)
[ -n "$REASONING_PARSER" ] && ARGS+=(--reasoning-parser "$REASONING_PARSER")
[ -n "$CHAT_TEMPLATE" ] && ARGS+=(--chat-template "$CHAT_TEMPLATE")
[ -n "$TOOL_CALL_PARSER" ] && ARGS+=(--tool-call-parser "$TOOL_CALL_PARSER")
[ -n "$SPECULATIVE_ALGORITHM" ] && ARGS+=(--speculative-algorithm "$SPECULATIVE_ALGORITHM")
[ -n "$SPECULATIVE_DRAFT_MODEL_PATH" ] && ARGS+=(--speculative-draft-model-path "$SPECULATIVE_DRAFT_MODEL_PATH")
[ "$TRUST_REMOTE_CODE" = "true" ] && ARGS+=(--trust-remote-code)
[ -n "$QUANTIZATION" ] && ARGS+=(--quantization "$QUANTIZATION")
[ -n "$MOE_RUNNER_BACKEND" ] && ARGS+=(--moe-runner-backend "$MOE_RUNNER_BACKEND")
[ -n "$MAX_MAMBA_CACHE_SIZE" ] && ARGS+=(--max-mamba-cache-size "$MAX_MAMBA_CACHE_SIZE")
[ "$SKIP_SERVER_WARMUP" = "true" ] && ARGS+=(--skip-server-warmup)
# --dtype：设置计算数据类型，float16 可使 visual encoder prefill 更快（相比默认 bfloat16 降低 TTFT 20-40%）
[ -n "$DTYPE" ] && ARGS+=(--dtype "$DTYPE")
# --image-max-pixels / --image-min-pixels：当前版本 SGLang 不支持，已禁用


echo "🚀 实际启动命令："
printf '  %q' "${ARGS[@]}"; echo

"${ARGS[@]}" 2>&1 | tee "$LOG_FILE"

echo ""
echo "=============================================================================="
echo "✅ SGLang 服务已停止"
echo "📝 日志已保存到: $LOG_FILE"
echo "=============================================================================="
