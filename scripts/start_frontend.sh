#!/bin/bash
# 前端启动脚本
# 用于启动 Next.js 前端开发服务器

# 获取项目根目录
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "启动前端开发服务器..."
echo "项目根目录: $PROJECT_ROOT"
echo "=========================================="

# 切换到前端目录
cd "$PROJECT_ROOT/frontend" || {
    echo "❌ 错误: 无法切换到 frontend 目录"
    exit 1
}

# 检查 package.json 是否存在
if [ ! -f "package.json" ]; then
    echo "❌ 错误: package.json 文件不存在"
    echo "请确保 frontend 目录包含完整的 Next.js 项目文件"
    exit 1
fi

# 检查 node_modules 是否存在
if [ ! -d "node_modules" ]; then
    echo "📦 检测到未安装依赖，正在运行 npm install..."
    npm install
    if [ $? -ne 0 ]; then
        echo "❌ 错误: npm install 失败"
        exit 1
    fi
fi

# 启动开发服务器
echo "🚀 启动 Next.js 开发服务器..."
npm run dev