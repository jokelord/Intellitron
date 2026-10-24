#!/bin/bash
# 测试脚本：验证 sglang.max_context_length 参数解析

# 获取项目根目录
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# 从配置文件读取配置
CONFIG_FILE="$PROJECT_ROOT/config/config.ini"

if [ -f "$CONFIG_FILE" ]; then
    # 读取 SGLang 配置（从 [sglang] 部分）
    SGLANG_CONFIG=$(sed -n '/^\[sglang\]/,/^\[/p' "$CONFIG_FILE" | grep -E '^(host|port|reasoning_parser|chat_template|tool_call_parser|disable_cuda_graph|fp8_gemm_backend|tensor_parallel_size|max_context_length|mem_fraction_static) =' | sed 's/^[[:space:]]*//')

    # 解析 max_context_length
    MAX_CONTEXT_LENGTH=$(echo "$SGLANG_CONFIG" | grep '^max_context_length =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

    echo "sglang.max_context_length 解析结果："
    echo "MAX_CONTEXT_LENGTH = '$MAX_CONTEXT_LENGTH'"
    echo ""
    echo "传递到 SGLang 命令的参数："
    echo "--context-length $MAX_CONTEXT_LENGTH"
else
    echo "错误：找不到配置文件 $CONFIG_FILE"
fi