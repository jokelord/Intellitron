#!/bin/bash
# 测试脚本：验证 tool_call_parser 参数解析

# 获取项目根目录
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"

# 从配置文件读取配置
CONFIG_FILE="$PROJECT_ROOT/config/config.ini"

if [ -f "$CONFIG_FILE" ]; then
    # 读取 SGLang 配置（从 [sglang] 部分）
    SGLANG_CONFIG=$(sed -n '/^\[sglang\]/,/^\[/p' "$CONFIG_FILE" | grep -E '^(host|port|reasoning_parser|chat_template|tool_call_parser|disable_cuda_graph|fp8_gemm_backend|tensor_parallel_size|max_context_length|mem_fraction_static) =' | sed 's/^[[:space:]]*//')

    # 解析 tool_call_parser
    TOOL_CALL_PARSER=$(echo "$SGLANG_CONFIG" | grep '^tool_call_parser =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

    echo "解析结果："
    echo "TOOL_CALL_PARSER = '$TOOL_CALL_PARSER'"
else
    echo "错误：找不到配置文件 $CONFIG_FILE"
fi