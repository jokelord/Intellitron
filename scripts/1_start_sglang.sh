#!/bin/bash
# SGLang 服务器启动脚本 - 简化版本
# 用于启动 SGLang 推理服务

# 获取项目根目录
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# 从 YAML 配置文件加载参数
YAML_MODEL="$PROJECT_ROOT/config/config-model.yaml"
YAML_INFER="$PROJECT_ROOT/config/config-infer.yaml"

[[ ! -f "$YAML_MODEL" ]] && { echo "❌ 配置文件不存在: $YAML_MODEL"; exit 1; }
[[ ! -f "$YAML_INFER" ]] && { echo "❌ 配置文件不存在: $YAML_INFER"; exit 1; }

# 使用 Python 一次性解析 YAML 并输出 shell 变量赋值；null 值输出为空字符串
eval "$(python3 - "$YAML_MODEL" "$YAML_INFER" <<'PYEOF'
import yaml, sys

def yget(d, *keys):
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return ''
        d = d[k]
    if d is None:
        return ''
    v = str(d)
    return v.lower() if v.lower() in ('true', 'false') else v

def sh(k, v):
    return "{}='{}'".format(k, v.replace("'", "'\\''"))

with open(sys.argv[1]) as f:
    m = yaml.safe_load(f).get('model', {})
with open(sys.argv[2]) as f:
    infer = yaml.safe_load(f)
sg = infer.get('sglang', {})

print('\n'.join([
    sh('MODEL_PATH',                   yget(m,  'model_path')),
    sh('SGLANG_HOST',                  yget(sg, 'host')),
    sh('SGLANG_PORT',                  yget(sg, 'port')),
    sh('ATTENTION_BACKEND',            yget(sg, 'attention_backend')),
    sh('MM_ATTENTION_BACKEND',         yget(sg, 'mm_attention_backend')),
    sh('MAX_CONTEXT_LENGTH',           yget(sg, 'max_context_length')),
    sh('MEM_FRACTION_STATIC',          yget(sg, 'mem_fraction_static')),
    sh('MAX_TOTAL_TOKENS',             yget(sg, 'max_total_tokens')),
    sh('MAX_RUNNING_REQUESTS',         yget(sg, 'max_running_requests')),
    sh('MAX_PREFILL_TOKENS',           yget(sg, 'max_prefill_tokens')),
    sh('TENSOR_PARALLEL_SIZE',         yget(sg, 'tensor_parallel_size')),
    sh('CHUNKED_PREFILL_SIZE',         yget(sg, 'chunked_prefill_size')),
    sh('MAMBA_FULL_MEMORY_RATIO',      yget(sg, 'mamba_full_memory_ratio')),
    sh('MAMBA_SSM_DTYPE',              yget(sg, 'mamba_ssm_dtype')),
    sh('FP8_GEMM_BACKEND',             yget(sg, 'fp8_gemm_backend')),
    sh('DISABLE_CUDA_GRAPH',           yget(sg, 'disable_cuda_graph')),
    sh('REASONING_PARSER',             yget(sg, 'reasoning_parser')),
    sh('CHAT_TEMPLATE',                yget(sg, 'chat_template')),
    sh('TOOL_CALL_PARSER',             yget(sg, 'tool_call_parser')),
    sh('SPECULATIVE_ALGORITHM',        yget(sg, 'speculative_algorithm')),
    sh('SPECULATIVE_DRAFT_MODEL_PATH', yget(sg, 'speculative_draft_model_path')),
    sh('TRUST_REMOTE_CODE',            yget(sg, 'trust_remote_code')),
    sh('QUANTIZATION',                 yget(sg, 'quantization')),
    sh('MOE_RUNNER_BACKEND',           yget(sg, 'moe_runner_backend')),
    sh('MAX_MAMBA_CACHE_SIZE',         yget(sg, 'max_mamba_cache_size')),
    sh('SKIP_SERVER_WARMUP',           yget(sg, 'skip_server_warmup')),
    sh('DTYPE',                        yget(sg, 'dtype')),
    sh('IMAGE_MAX_PIXELS',             yget(sg, 'image_max_pixels')),
    sh('IMAGE_MIN_PIXELS',             yget(sg, 'image_min_pixels')),
]))
PYEOF
)"

# 配置（YAML 已赋值；此处仅在 YAML 为空时提供保底默认值）
MODEL_PATH="${MODEL_PATH:-/home/zk/Project/LLM-weight/Qwen3-30B-A3B-Thinking-2507-FP8/}"

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
