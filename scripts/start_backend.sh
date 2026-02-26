#!/bin/bash
# 启动 FastAPI 后端服务脚本 (Jetson Thor 优化版)

# 获取项目根目录
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# 从配置文件读取服务器配置
CONFIG_FILE="$PROJECT_ROOT/config/config.ini"
HOST="0.0.0.0"
PORT="8000"

if [ -f "$CONFIG_FILE" ]; then
    # 从 [server] 部分读取配置
    SERVER_CONFIG=$(sed -n '/^\[server\]/,/^\[/p' "$CONFIG_FILE" | grep -E '^(host|port) =' | sed 's/^[[:space:]]*//')
    
    HOST_FROM_CONFIG=$(echo "$SERVER_CONFIG" | grep '^host =' | cut -d'=' -f2 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    PORT_FROM_CONFIG=$(echo "$SERVER_CONFIG" | grep '^port =' | cut -d'=' -f2 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    
    if [ -n "$HOST_FROM_CONFIG" ]; then
        HOST="$HOST_FROM_CONFIG"
    fi
    if [ -n "$PORT_FROM_CONFIG" ]; then
        PORT="$PORT_FROM_CONFIG"
    fi
fi

# 1. 激活 .sglang (uv) 环境
if [ -d "$PROJECT_ROOT/.sglang" ]; then
    echo "使用 uv 环境: .sglang"
    source "$PROJECT_ROOT/.sglang/bin/activate"
else
    echo "错误: 未找到 .sglang 环境，请先使用 uv 创建"
    exit 1
fi

# 2. 设置环境变量 (应用 Jetson Thor 踩坑经验)
# 设置 Triton PTXAS Wrapper 路径
export TRITON_PTXAS_PATH=/home/zk/ptxas_wrapper.sh
# 设置正确的 CUDA 架构列表
export TORCH_CUDA_ARCH_LIST=11.0
# 设置库路径
export LD_LIBRARY_PATH=/usr/local/cuda-13.0/lib64:$LD_LIBRARY_PATH
export CPLUS_INCLUDE_PATH=/usr/local/cuda-13.0/targets/sbsa-linux/include:$CPLUS_INCLUDE_PATH

# 3. 检查并清理占用端口的进程
check_and_kill_port() {
    local port=$1
    local pid=$(lsof -ti :$port 2>/dev/null)
    if [ -n "$pid" ]; then
        echo "⚠️  检测到端口 $port 被进程 $pid 占用，正在终止..."
        kill -TERM $pid 2>/dev/null
        
        # 等待进程终止，最多等待5秒
        local count=0
        while [ $count -lt 5 ] && kill -0 $pid 2>/dev/null; do
            sleep 1
            count=$((count + 1))
        done
        
        # 如果进程还在，强制终止
        if kill -0 $pid 2>/dev/null; then
            echo "⚠️  强制终止进程 $pid..."
            kill -KILL $pid 2>/dev/null
        fi
        
        # 再等1秒确保端口释放
        sleep 1
    fi
}

# 检查端口是否可用
is_port_available() {
    local port=$1
    if lsof -Pi :$port -sTCP:LISTEN -t >/dev/null 2>&1; then
        return 1  # 端口被占用
    else
        return 0  # 端口可用
    fi
}

# 4. 启动 uvicorn 服务
echo "🚀 正在启动 LLM 后端服务..."
echo "📍 地址: http://$HOST:$PORT"

# 检查端口是否被占用
if ! is_port_available $PORT; then
    echo "⚠️  端口 $PORT 已被占用，尝试清理..."
    check_and_kill_port $PORT
    
    # 再次检查
    if ! is_port_available $PORT; then
        echo "❌ 无法释放端口 $PORT，请手动检查并终止占用进程"
        exit 1
    fi
fi

# 设置信号处理，确保子进程被正确终止
cleanup() {
    echo ""
    echo "🛑 接收到终止信号，正在关闭服务..."
    # 终止所有子进程
    pkill -P $$ 2>/dev/null
    exit 0
}

# 捕获常见终止信号
trap cleanup SIGINT SIGTERM SIGHUP

# 生产环境建议移除 --reload
python3 -m uvicorn backend.main:app --host "$HOST" --port "$PORT"
