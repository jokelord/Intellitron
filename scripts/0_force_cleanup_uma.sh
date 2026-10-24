#!/bin/bash
# UMA 内存强制清理脚本（Jetson Thor）
# 这个脚本会尝试释放尽可能多的内存：
#   1. 终止 SGLang/vLLM/backend Python 进程
#   2. 停止 SGLang/vLLM NGC Docker 容器
#   3. 清理系统页面缓存

set -euo pipefail

echo "=============================================================================="
echo "🚀 UMA 内存强制清理（Jetson Thor）"
echo "=============================================================================="
echo ""

# 获取项目根目录（subshell，不改变当前工作目录的副作用）
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

source "$PROJECT_ROOT/scripts/_workspace_paths.sh"
SHARED_SGLANG_ACTIVATE="$(get_shared_sglang_activate "$PROJECT_ROOT")"

# 激活 Python 虚拟环境
if [ -f "$SHARED_SGLANG_ACTIVATE" ]; then
    # shellcheck disable=SC1091
    source "$SHARED_SGLANG_ACTIVATE"
else
    echo "❌ 未找到 Python 虚拟环境: $SHARED_SGLANG_ACTIVATE"
    exit 1
fi

# 1. 终止 Python 进程并停止 Docker 容器
echo "=============================================================================="
echo "📋 步骤 1: 清理 Python 进程和 Docker 容器"
echo "   目标: sglang / vllm / backend"
echo "=============================================================================="
echo ""
python scripts/cleanup_gpu_memory.py --force

echo ""
echo "=============================================================================="
echo "📋 步骤 2: 清理系统页面缓存"
echo "=============================================================================="
echo ""

# 2. 同步文件系统
echo "🔄 同步文件系统..."
sync

# 3. 清理页面缓存、目录项和 inode
echo "🧹 清理系统缓存（需要 sudo 权限）..."
echo "   这会释放文件系统缓存，但不会影响程序运行"
echo ""

if sudo -n true 2>/dev/null; then
    # 有免密 sudo 权限
    sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches' && echo "✅ 系统缓存已清理" || echo "❌ 清理失败（权限不足）"
else
    # 需要密码
    echo "⚠️  需要输入 sudo 密码来清理系统缓存:"
    sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches' && echo "✅ 系统缓存已清理" || echo "❌ 清理失败或用户取消"
fi

echo ""
echo "=============================================================================="
echo "📋 步骤 3: 最终内存状态"
echo "=============================================================================="
echo ""

# 4. 显示最终状态
python scripts/cleanup_gpu_memory.py --status

echo ""
echo "=============================================================================="
echo "✅ 强制清理完成！"
echo "=============================================================================="
echo ""
echo "💡 如果仍然内存不足:"
echo "   1. 检查是否有其他大型程序在运行（浏览器、IDE 等）"
echo "   2. 考虑重启系统以完全释放内存"
echo "   3. 降低 GPU 利用率参数（在配置文件中设置更低的 gpu_memory_utilization）"
echo ""
