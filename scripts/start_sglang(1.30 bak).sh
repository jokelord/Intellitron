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
    SGLANG_CONFIG=$(sed -n '/^\[sglang\]/,/^\[/p' "$CONFIG_FILE" | grep -E '^(host|port|reasoning_parser|chat_template|tool_call_parser|disable_cuda_graph|fp8_gemm_backend|tensor_parallel_size|max_context_length|mem_fraction_static|attention_backend|moe_runner_backend) =' | sed 's/^[[:space:]]*//')
    
    # 解析各个配置项
    SGLANG_HOST=$(echo "$SGLANG_CONFIG" | grep '^host =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    SGLANG_PORT=$(echo "$SGLANG_CONFIG" | grep '^port =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    REASONING_PARSER=$(echo "$SGLANG_CONFIG" | grep '^reasoning_parser =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    CHAT_TEMPLATE=$(echo "$SGLANG_CONFIG" | grep '^chat_template =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    TOOL_CALL_PARSER=$(echo "$SGLANG_CONFIG" | grep '^tool_call_parser =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    DISABLE_CUDA_GRAPH=$(echo "$SGLANG_CONFIG" | grep '^disable_cuda_graph =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    FP8_GEMM_BACKEND=$(echo "$SGLANG_CONFIG" | grep '^fp8_gemm_backend =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    TENSOR_PARALLEL_SIZE=$(echo "$SGLANG_CONFIG" | grep '^tensor_parallel_size =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    MAX_CONTEXT_LENGTH=$(echo "$SGLANG_CONFIG" | grep '^max_context_length =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    ATTENTION_BACKEND=$(echo "$SGLANG_CONFIG" | grep '^attention_backend =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    MOE_RUNNER_BACKEND=$(echo "$SGLANG_CONFIG" | grep '^moe_runner_backend =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
fi

# 配置（使用环境变量或配置文件值或默认值）
MODEL_PATH="${MODEL_PATH:-/home/zk/Project/LLM-weight/Qwen3-30B-A3B-Thinking-2507-FP8/}"
HOST="${HOST:-${SGLANG_HOST:-0.0.0.0}}"
PORT="${PORT:-${SGLANG_PORT:-30000}}"
REASONING_PARSER="${REASONING_PARSER:-qwen3-thinking}"
CHAT_TEMPLATE="${CHAT_TEMPLATE:-chatml}"
TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-qwen}"
DISABLE_CUDA_GRAPH="${DISABLE_CUDA_GRAPH:-true}"
FP8_GEMM_BACKEND="${FP8_GEMM_BACKEND:-triton}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-1}"
MAX_CONTEXT_LENGTH="${MAX_CONTEXT_LENGTH:-1048576}"
MEM_FRACTION_STATIC="${MEM_FRACTION_STATIC:-0.893}"
ATTENTION_BACKEND="${ATTENTION_BACKEND:-triton}"
MOE_RUNNER_BACKEND="${MOE_RUNNER_BACKEND:-triton}"

# 确保 logs 目录存在
mkdir -p "$PROJECT_ROOT/logs"

# 生成日志文件名（带时间戳）
LOG_FILE="$PROJECT_ROOT/logs/sglang_$(date +%Y%m%d_%H%M%S).log"

# 激活虚拟环境
source /home/zk/Project/LLM-deployment/.sglang/bin/activate

# 设置 Triton PTXAS 路径
export TRITON_PTXAS_PATH="$PROJECT_ROOT/patch/ptxas_wrapper.sh"
# 设置正确的 CUDA 架构列表
export TORCH_CUDA_ARCH_LIST=11.0
# 设置库路径
export LD_LIBRARY_PATH=/usr/local/cuda-13.0/lib64:$LD_LIBRARY_PATH
export CPLUS_INCLUDE_PATH=/usr/local/cuda-13.0/targets/sbsa-linux/include:$CPLUS_INCLUDE_PATH
# 允许覆盖更长的上下文长度
export SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN=1

# 输出日志信息
echo "=============================================================================="
echo "🚀 SGLang 服务器启动脚本"
echo "=============================================================================="
echo ""
echo "📝 日志文件: $LOG_FILE"
echo ""

# 启动 SGLang 服务，同时显示输出并保存到日志文件
python3 -m sglang.launch_server \
    --model-path "$MODEL_PATH" \
    --host "$HOST" \
    --port "$PORT" \
    --context-length "$MAX_CONTEXT_LENGTH" \
    --mem-fraction-static "$MEM_FRACTION_STATIC" \
    --reasoning-parser "$REASONING_PARSER" \
    --chat-template "$CHAT_TEMPLATE" \
    --tool-call-parser "$TOOL_CALL_PARSER" \
    --tensor-parallel-size "$TENSOR_PARALLEL_SIZE" \
    --attention-backend "$ATTENTION_BACKEND" \
    --moe-runner-backend "$MOE_RUNNER_BACKEND" \
    $([ "$DISABLE_CUDA_GRAPH" = "true" ] && echo "--disable-cuda-graph") \
    --fp8-gemm-backend "$FP8_GEMM_BACKEND" \
    2>&1 | tee "$LOG_FILE"

echo ""
echo "=============================================================================="
echo "✅ SGLang 服务已停止"
echo "📝 日志已保存到: $LOG_FILE"
echo "=============================================================================="
