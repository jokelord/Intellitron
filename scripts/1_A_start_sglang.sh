#!/bin/bash
# SGLang 服务器启动脚本 - 简化版本
# 用于启动 SGLang 推理服务

# 获取项目根目录
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$PROJECT_ROOT/scripts/_workspace_paths.sh"
SHARED_WORKSPACE_ROOT="$(get_shared_workspace_root "$PROJECT_ROOT")"
SHARED_SGLANG_ACTIVATE="$(get_shared_sglang_activate "$PROJECT_ROOT")"

# 从 config-overall.yaml 读取实际配置文件路径
YAML_OVERALL="$PROJECT_ROOT/config/config-overall.yaml"
[[ ! -f "$YAML_OVERALL" ]] && { echo "❌ 配置文件不存在: $YAML_OVERALL"; exit 1; }

# 解析 overall.yaml 获取 infer 配置路径（model 键已移除不再引用；
# 旧 infer 缺 model 段时按 sibling 自动回退，新 infer 内嵌 model 段）。
OVERALL_MODEL=$(python3 -c "import yaml; print((yaml.safe_load(open('$YAML_OVERALL')) or {}).get('model', '') or '')")
OVERALL_INFER=$(python3 -c "import yaml; print((yaml.safe_load(open('$YAML_OVERALL')) or {}).get('infer', ''))")

# 配置引用中的 ~ 必须先展开；相对引用继续相对项目根目录解析。
YAML_INFER="$(resolve_project_path "$OVERALL_INFER" "$PROJECT_ROOT")"
if [ -n "$OVERALL_MODEL" ] && [ "$OVERALL_MODEL" != "None" ]; then
  YAML_MODEL="$(resolve_project_path "$OVERALL_MODEL" "$PROJECT_ROOT")"
else
  # sibling 回退：*-config-infer.yaml → *-config-model.yaml（旧文件兼容）
  SIBLING_MODEL="$(echo "$YAML_INFER" | sed 's/-config-infer\.yaml$/-config-model.yaml/')"
  if [ "$SIBLING_MODEL" != "$YAML_INFER" ] && [ -f "$SIBLING_MODEL" ]; then
    YAML_MODEL="$SIBLING_MODEL"
  else
    YAML_MODEL=""
  fi
fi

[[ ! -f "$YAML_INFER" ]] && { echo "❌ 配置文件不存在: $YAML_INFER"; exit 1; }
if [ -n "$YAML_MODEL" ] && [ ! -f "$YAML_MODEL" ]; then
  echo "❌ 配置文件不存在: $YAML_MODEL"; exit 1
fi

echo "📂 使用配置文件:"
if [ -n "$YAML_MODEL" ]; then
  echo "   Model (legacy sibling, 可选): $YAML_MODEL"
else
  echo "   Model: 内嵌于 Infer（新布局）"
fi
echo "   Infer: $YAML_INFER"
echo ""

# 使用 Python 一次性解析 YAML 并输出 shell 变量赋值；null 值输出为空字符串
# model 段优先读 infer 内嵌，缺失时回退读 legacy sibling 文件。
eval "$(python3 - "$YAML_MODEL" "$YAML_INFER" <<'PYEOF'
import yaml, sys, os

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

model_file = sys.argv[1]
infer_file = sys.argv[2]
m_file = {}
if model_file and os.path.isfile(model_file):
    with open(model_file) as f:
        m_file = (yaml.safe_load(f) or {}).get('model', {}) or {}
with open(infer_file) as f:
    infer = yaml.safe_load(f) or {}
m_infer = infer.get('model', {}) or {}
# infer 内嵌优先，非 null 覆盖；缺失键用 sibling 补齐
m = dict(m_file)
for k, v in m_infer.items():
    if v is not None:
        m[k] = v
sg = infer.get('sglang', {})

print('\n'.join([
    sh('MODEL_PATH',                   os.path.expanduser(yget(m, 'model_path'))),
    sh('EXTERNAL_MODEL_PACKAGE',       yget(sg, 'external_model_package')),
    sh('FLASHINFER_PREFILL_UNIFORM_Q_LEN_COMPAT', yget(sg, 'flashinfer_prefill_uniform_q_len_compat')),
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
    sh('MAMBA_RADIX_CACHE_STRATEGY',   yget(sg, 'mamba_radix_cache_strategy')),
    sh('MAMBA_SSM_DTYPE',              yget(sg, 'mamba_ssm_dtype')),
    sh('FP8_GEMM_BACKEND',             yget(sg, 'fp8_gemm_backend')),
    sh('FP4_GEMM_BACKEND',             yget(sg, 'fp4_gemm_backend')),
    sh('DISABLE_CUDA_GRAPH',           yget(sg, 'disable_cuda_graph')),
    sh('REASONING_PARSER',             yget(sg, 'reasoning_parser')),
    sh('CHAT_TEMPLATE',                os.path.expanduser(yget(sg, 'chat_template'))),
    sh('TOOL_CALL_PARSER',             yget(sg, 'tool_call_parser')),
    sh('SPECULATIVE_ALGORITHM',        yget(sg, 'speculative_algorithm')),
    sh('SPECULATIVE_NUM_STEPS',        yget(sg, 'speculative_num_steps')),
    sh('SPECULATIVE_EAGLE_TOPK',       yget(sg, 'speculative_eagle_topk')),
    sh('SPECULATIVE_NUM_DRAFT_TOKENS',  yget(sg, 'speculative_num_draft_tokens')),
    sh('ENABLE_LINEAR_REPLAYSSM_SPEC',  yget(sg, 'enable_linear_replayssm_spec')),
    sh('SPECULATIVE_DRAFT_MODEL_PATH', os.path.expanduser(yget(sg, 'speculative_draft_model_path'))),
    sh('SPECULATIVE_DRAFT_ATTENTION_BACKEND', yget(sg, 'speculative_draft_attention_backend')),
    sh('SPECULATIVE_DSPARK_BLOCK_SIZE', yget(sg, 'speculative_dspark_block_size')),
    sh('SPECULATIVE_DRAFT_MODEL_QUANTIZATION', yget(sg, 'speculative_draft_model_quantization')),
    sh('TRUST_REMOTE_CODE',            yget(sg, 'trust_remote_code')),
    sh('QUANTIZATION',                 yget(sg, 'quantization')),
    sh('KV_CACHE_DTYPE',               yget(sg, 'kv_cache_dtype')),
    sh('MOE_RUNNER_BACKEND',           yget(sg, 'moe_runner_backend')),
    sh('SCHEDULE_POLICY',              yget(sg, 'schedule_policy')),
    sh('ENABLE_CACHE_REPORT',          yget(sg, 'enable_cache_report')),
    sh('CUDA_GRAPH_MAX_BS',            yget(sg, 'cuda_graph_max_bs')),
    sh('MAX_MAMBA_CACHE_SIZE',         yget(sg, 'max_mamba_cache_size')),
    sh('SKIP_SERVER_WARMUP',           yget(sg, 'skip_server_warmup')),
    sh('COMPILE_MAX_JOBS',             yget(sg, 'compile_max_jobs')),
    sh('TRITON_COMPILE_THREADS',       yget(sg, 'triton_num_compile_threads')),
    sh('DTYPE',                        yget(sg, 'dtype')),
    sh('IMAGE_MAX_PIXELS',             yget(sg, 'image_max_pixels')),
    sh('IMAGE_MIN_PIXELS',             yget(sg, 'image_min_pixels')),
]))
PYEOF
)"

# Resolve configured model/template paths independently of the launcher cwd.
if [ -n "$CHAT_TEMPLATE" ]; then
  CHAT_TEMPLATE="$(resolve_project_path "$CHAT_TEMPLATE" "$PROJECT_ROOT")"
  [[ -f "$CHAT_TEMPLATE" ]] || { echo "❌ Chat template 不存在: $CHAT_TEMPLATE"; exit 1; }
fi

# 配置（YAML 已赋值；model_path 优先由 infer 内嵌 model 段提供，旧文件回退 sibling）
if [ -n "$YAML_MODEL" ]; then
  MODEL_SRC="$YAML_INFER (+ $YAML_MODEL)"
else
  MODEL_SRC="$YAML_INFER"
fi
[ -n "$MODEL_PATH" ] || { echo "❌ model 配置缺少 model_path，请在 $MODEL_SRC 的 model 段中指定"; exit 1; }

# 确保 logs 目录存在
mkdir -p "$PROJECT_ROOT/logs"

# 生成日志文件名（带时间戳）
LOG_FILE="$PROJECT_ROOT/logs/sglang_$(date +%Y%m%d_%H%M%S).log"

# 激活虚拟环境
source "$SHARED_SGLANG_ACTIVATE"

# 按模型配置选择外部适配包，确保 server 与 worker 均可从任意 cwd 导入项目包。详见：lesson-learnt/踩坑记录8.md
if [ -n "$EXTERNAL_MODEL_PACKAGE" ]; then
  export SGLANG_EXTERNAL_MODEL_PACKAGE="$EXTERNAL_MODEL_PACKAGE"
  export PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"
fi

# 只允许当前 infer YAML 明确启用；清除父进程残留开关，避免影响其他模型。详见：lesson-learnt/踩坑记录8.md
if [ "$FLASHINFER_PREFILL_UNIFORM_Q_LEN_COMPAT" = "true" ]; then
  export LLM_DEPLOYMENT_FLASHINFER_PREFILL_UNIFORM_Q_LEN_COMPAT=1
else
  unset LLM_DEPLOYMENT_FLASHINFER_PREFILL_UNIFORM_Q_LEN_COMPAT
fi

# 设置 Triton PTXAS 路径（普通 ptxas + Blackwell 专用 ptxas-blackwell）
# 注意：ptxas patch 中有显式调用 CUDA 13.x
export TRITON_PTXAS_PATH="$PROJECT_ROOT/patch/ptxas_wrapper.sh"
export TRITON_PTXAS_BLACKWELL_PATH="$PROJECT_ROOT/patch/ptxas_wrapper.sh"
#export TRITON_PTXAS_PATH=/usr/local/cuda-13.3/bin/ptxas
# 设置正确的 CUDA 架构列表
export TORCH_CUDA_ARCH_LIST=11.0a
# 设置库路径
export LD_LIBRARY_PATH=/usr/local/cuda-13.3/lib64:$LD_LIBRARY_PATH
export CPLUS_INCLUDE_PATH=/usr/local/cuda-13.3/targets/sbsa-linux/include:$CPLUS_INCLUDE_PATH
# 允许覆盖更长的上下文长度
export SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN=1

# cutlass FP8 内核启用 NVFP4
#export SGLANG_USE_CUTLASS_NVFP4=

# 开启针对 MoE 层 NVFP4 格式的调度支持（需配合 flashinfer_cutlass 的 moe_runner_backend 后端使用）
# export SGLANG_MOE_NVFP4_DISPATCH="true"

# 避免底层库检查冲突
# export SGLANG_DISABLE_CUDNN_CHECK=1

# VLM 启用 CUDA 图优化，激活ViTCudaGraphRunner，加速VLM首帧
# 要求每次传进来的图片分辨率都一样，否则会不断地进行 Graph Capture（捕获过程本身非常耗时，大约需要几百毫秒）
# 临时禁用：导致摄像头拍照后VLM状态损坏（投毒）
# export SGLANG_VIT_ENABLE_CUDA_GRAPH=1

# ---- UMA / Jetson 内存优化 ----
# 禁用 DeepGemm JIT 编译（首因：DeepGemm 预编译会一次性编译所有 FP8 GEMM 内核变体，
# 在 UMA 上编译期间的临时 buffer 会无限吃系统内存直至 OOM。
# 且本模型 scale_fmt 非 ue8m0，DeepGemm 在 Blackwell 上可能精度下降。）
export SGLANG_ENABLE_JIT_DEEPGEMM=0
# 如果希望保留 DeepGemm 但减少编译时间/内存，可改用快速预热（注释上行，取消注释下行）：
# export SGLANG_JIT_DEEPGEMM_FAST_WARMUP=1
# PyTorch CUDA 分配器优化（UMA 上无 VRAM 边界，需限制分配器行为）
export PYTORCH_ALLOC_CONF=
# export PYTORCH_ALLOC_CONF="expandable_segments:True,max_split_size_mb:256,garbage_collection_threshold:0.5"
# 延迟加载 CUDA 模块，减少驻留内存
export CUDA_MODULE_LOADING=
# export CUDA_MODULE_LOADING=LAZY
# 将各编译缓存显式固定在持久化 home 下，避免临时目录或不同启动入口造成缓存漂移。
# 可在启动前用 SGLANG_COMPILE_CACHE_BASE 覆盖，但切换路径会造成一次 cache miss。
SGLANG_COMPILE_CACHE_BASE="$(expand_home_path "${SGLANG_COMPILE_CACHE_BASE:-$HOME}")"
export FLASHINFER_WORKSPACE_BASE="$(expand_home_path "${FLASHINFER_WORKSPACE_BASE:-$SGLANG_COMPILE_CACHE_BASE}")"
export SGLANG_CACHE_DIR="$(expand_home_path "${SGLANG_CACHE_DIR:-$SGLANG_COMPILE_CACHE_BASE/.cache/sglang}")"
export TORCH_EXTENSIONS_DIR="$(expand_home_path "${TORCH_EXTENSIONS_DIR:-$SGLANG_COMPILE_CACHE_BASE/.cache/torch_extensions}")"
export TRITON_CACHE_DIR="$(expand_home_path "${TRITON_CACHE_DIR:-$SGLANG_COMPILE_CACHE_BASE/.triton/cache}")"
export CUDA_CACHE_PATH="$(expand_home_path "${CUDA_CACHE_PATH:-$SGLANG_COMPILE_CACHE_BASE/.nv/ComputeCache}")"
export SGLANG_DG_CACHE_DIR="$(expand_home_path "${SGLANG_DG_CACHE_DIR:-$SGLANG_COMPILE_CACHE_BASE/.cache/deep_gemm}")"
mkdir -p "$SGLANG_CACHE_DIR" "$TORCH_EXTENSIONS_DIR" "$TRITON_CACHE_DIR" \
    "$CUDA_CACHE_PATH" "$SGLANG_DG_CACHE_DIR"

# PyTorch C++/CUDA extension 使用 Ninja；不限制时默认 CPU 数 + 2 个 worker，
# 每个 nvcc/cicc 在 Jetson UMA 上都可能占用 1GB 以上内存。
[ -z "$COMPILE_MAX_JOBS" ] || [[ "$COMPILE_MAX_JOBS" =~ ^[1-9][0-9]*$ ]] \
    || { echo "❌ compile_max_jobs 必须是正整数"; exit 1; }
[ -z "$TRITON_COMPILE_THREADS" ] || [[ "$TRITON_COMPILE_THREADS" =~ ^[1-9][0-9]*$ ]] \
    || { echo "❌ triton_num_compile_threads 必须是正整数"; exit 1; }
[ -n "$COMPILE_MAX_JOBS" ] && export MAX_JOBS="$COMPILE_MAX_JOBS"
[ -n "$TRITON_COMPILE_THREADS" ] && export TRITON_NUM_COMPILE_THREADS="$TRITON_COMPILE_THREADS"

echo "🧱 编译缓存与并发:"
echo "   FlashInfer: $FLASHINFER_WORKSPACE_BASE/.cache/flashinfer"
echo "   SGLang:    $SGLANG_CACHE_DIR"
echo "   MAX_JOBS:  ${MAX_JOBS:-library default}"
echo "   Triton:    ${TRITON_NUM_COMPILE_THREADS:-library default} compile thread(s)"
echo ""


# 启动 SGLang 服务，同时显示输出并保存到日志文件
# （全部启动参数均由 config/*-infer.yaml / *-model.yaml 驱动，此处不再硬编码模型相关参数）
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
[ -n "$MAMBA_RADIX_CACHE_STRATEGY" ] && ARGS+=(--mamba-radix-cache-strategy "$MAMBA_RADIX_CACHE_STRATEGY")
[ -n "$MAMBA_SSM_DTYPE" ] && ARGS+=(--mamba-ssm-dtype "$MAMBA_SSM_DTYPE")
[ -n "$FP8_GEMM_BACKEND" ] && ARGS+=(--fp8-gemm-backend "$FP8_GEMM_BACKEND")
[ -n "$FP4_GEMM_BACKEND" ] && ARGS+=(--fp4-gemm-backend "$FP4_GEMM_BACKEND")
[ "$DISABLE_CUDA_GRAPH" = "true" ] && ARGS+=(--disable-cuda-graph)
[ -n "$REASONING_PARSER" ] && ARGS+=(--reasoning-parser "$REASONING_PARSER")
[ -n "$CHAT_TEMPLATE" ] && ARGS+=(--chat-template "$CHAT_TEMPLATE")
[ -n "$TOOL_CALL_PARSER" ] && ARGS+=(--tool-call-parser "$TOOL_CALL_PARSER")
[ -n "$SPECULATIVE_ALGORITHM" ] && ARGS+=(--speculative-algorithm "$SPECULATIVE_ALGORITHM")
[ -n "$SPECULATIVE_NUM_STEPS" ] && ARGS+=(--speculative-num-steps "$SPECULATIVE_NUM_STEPS")
[ -n "$SPECULATIVE_EAGLE_TOPK" ] && ARGS+=(--speculative-eagle-topk "$SPECULATIVE_EAGLE_TOPK")
[ -n "$SPECULATIVE_NUM_DRAFT_TOKENS" ] && ARGS+=(--speculative-num-draft-tokens "$SPECULATIVE_NUM_DRAFT_TOKENS")
[ "$ENABLE_LINEAR_REPLAYSSM_SPEC" = "true" ] && ARGS+=(--enable-linear-replayssm-spec)
[ -n "$SPECULATIVE_DRAFT_MODEL_PATH" ] && ARGS+=(--speculative-draft-model-path "$SPECULATIVE_DRAFT_MODEL_PATH")
[ -n "$SPECULATIVE_DRAFT_ATTENTION_BACKEND" ] && ARGS+=(--speculative-draft-attention-backend "$SPECULATIVE_DRAFT_ATTENTION_BACKEND")
[ -n "$SPECULATIVE_DSPARK_BLOCK_SIZE" ] && ARGS+=(--speculative-dspark-block-size "$SPECULATIVE_DSPARK_BLOCK_SIZE")
[ -n "$SPECULATIVE_DRAFT_MODEL_QUANTIZATION" ] && ARGS+=(--speculative-draft-model-quantization "$SPECULATIVE_DRAFT_MODEL_QUANTIZATION")
[ "$TRUST_REMOTE_CODE" = "true" ] && ARGS+=(--trust-remote-code)
[ -n "$QUANTIZATION" ] && ARGS+=(--quantization "$QUANTIZATION")
[ -n "$KV_CACHE_DTYPE" ] && ARGS+=(--kv-cache-dtype "$KV_CACHE_DTYPE")
[ -n "$MOE_RUNNER_BACKEND" ] && ARGS+=(--moe-runner-backend "$MOE_RUNNER_BACKEND")
[ -n "$SCHEDULE_POLICY" ] && ARGS+=(--schedule-policy "$SCHEDULE_POLICY")
[ "$ENABLE_CACHE_REPORT" = "true" ] && ARGS+=(--enable-cache-report)
[ -n "$CUDA_GRAPH_MAX_BS" ] && ARGS+=(--cuda-graph-max-bs "$CUDA_GRAPH_MAX_BS")
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
