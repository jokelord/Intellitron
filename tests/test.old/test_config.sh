#!/bin/bash

PROJECT_ROOT="${HOME}/Project/LLM-deployment"

CONFIG_FILE="$PROJECT_ROOT/config/config.ini"

if [ -f "$CONFIG_FILE" ]; then
    # 读取 SGLang 配置（从 [sglang] 部分）
    SGLANG_CONFIG=$(sed -n '/^\[sglang\]/,/^\[/p' "$CONFIG_FILE" | grep -E '^(host|port|reasoning_parser|tool_call_parser|disable_cuda_graph|fp8_gemm_backend|tensor_parallel_size|max_context_length) =' | sed 's/^[[:space:]]*//')
    
    # 解析各个配置项
    SGLANG_HOST=$(echo "$SGLANG_CONFIG" | grep '^host =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    SGLANG_PORT=$(echo "$SGLANG_CONFIG" | grep '^port =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    REASONING_PARSER=$(echo "$SGLANG_CONFIG" | grep '^reasoning_parser =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    TOOL_CALL_PARSER=$(echo "$SGLANG_CONFIG" | grep '^tool_call_parser =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    DISABLE_CUDA_GRAPH=$(echo "$SGLANG_CONFIG" | grep '^disable_cuda_graph =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    FP8_GEMM_BACKEND=$(echo "$SGLANG_CONFIG" | grep '^fp8_gemm_backend =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    TENSOR_PARALLEL_SIZE=$(echo "$SGLANG_CONFIG" | grep '^tensor_parallel_size =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    MAX_CONTEXT_LENGTH=$(echo "$SGLANG_CONFIG" | grep '^max_context_length =' | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
fi

echo "SGLANG_HOST: $SGLANG_HOST"
echo "SGLANG_PORT: $SGLANG_PORT"
echo "REASONING_PARSER: $REASONING_PARSER"
echo "TOOL_CALL_PARSER: $TOOL_CALL_PARSER"
echo "DISABLE_CUDA_GRAPH: $DISABLE_CUDA_GRAPH"
echo "FP8_GEMM_BACKEND: $FP8_GEMM_BACKEND"
echo "TENSOR_PARALLEL_SIZE: $TENSOR_PARALLEL_SIZE"
echo "MAX_CONTEXT_LENGTH: $MAX_CONTEXT_LENGTH"
