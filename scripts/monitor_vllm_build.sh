#!/bin/bash
# vLLM 编译进度监控脚本
# 实时显示编译日志的最后 50 行，并高亮显示关键进度信息

set -euo pipefail

# 获取项目根目录
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# 查找最新的编译日志文件
LATEST_LOG=$(ls -t "$PROJECT_ROOT/logs/vllm_build_"*.log 2>/dev/null | head -1)

if [ -z "$LATEST_LOG" ]; then
    echo "❌ 未找到编译日志文件，请先启动编译"
    exit 1
fi

echo "📊 监控编译进度: $LATEST_LOG"
echo "🔄 实时显示最后 50 行，按 Ctrl+C 退出"
echo "=================================================="

# 使用 tail -f 显示实时进度，并用 grep 高亮关键信息
tail -f "$LATEST_LOG" | grep --line-buffered -E "(Compiling|Linking|Building|cmake|ninja|error|Error|warning|Warning|completed|success|failed|FAILED)" --color=always || tail -f "$LATEST_LOG"