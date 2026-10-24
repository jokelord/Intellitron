#!/bin/bash
# 日志文件管理脚本
# 用于清理旧的日志文件，保持日志目录整洁

set -e

# 获取项目根目录
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# 日志目录
LOG_DIR="$PROJECT_ROOT/logs"

# 确保日志目录存在
mkdir -p "$LOG_DIR"

echo "=============================================================================="
echo "🧹 日志文件管理"
echo "=============================================================================="
echo ""

# 显示当前日志文件统计
echo "📊 当前日志文件统计:"
echo "   总文件数: $(find "$LOG_DIR" -name "*.log" -type f | wc -l)"
echo "   总大小: $(du -sh "$LOG_DIR" 2>/dev/null | cut -f1)"
echo ""

# 清理7天前的日志文件
echo "🗑️  清理7天前的日志文件..."
if find "$LOG_DIR" -name "*.log" -type f -mtime +7 -exec rm -f {} \; 2>/dev/null; then
    echo "✅ 已清理旧日志文件"
else
    echo "ℹ️  没有找到需要清理的旧日志文件"
fi

echo ""
echo "📋 清理后的统计:"
echo "   总文件数: $(find "$LOG_DIR" -name "*.log" -type f | wc -l)"
echo "   总大小: $(du -sh "$LOG_DIR" 2>/dev/null | cut -f1)"
echo ""

# 显示最新的5个日志文件
echo "📄 最新的日志文件:"
find "$LOG_DIR" -name "*.log" -type f -printf '%T@ %p\n' | sort -n | tail -5 | while read -r line; do
    timestamp=$(echo "$line" | cut -d' ' -f1)
    filepath=$(echo "$line" | cut -d' ' -f2-)
    filename=$(basename "$filepath")
    date_str=$(date -d "@$timestamp" '+%Y-%m-%d %H:%M:%S')
    size=$(du -h "$filepath" | cut -f1)
    echo "   $date_str - $filename ($size)"
done

echo ""
echo "=============================================================================="
echo "✅ 日志管理完成"
echo "=============================================================================="