#!/bin/bash
# 前端启动脚本
# 用于启动 Next.js 前端开发服务器

set -euo pipefail

# 获取项目根目录
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$PROJECT_ROOT/scripts/_workspace_paths.sh"
SHARED_WORKSPACE_ROOT="$(get_shared_workspace_root "$PROJECT_ROOT")"
SHARED_FRONTEND_ROOT="$SHARED_WORKSPACE_ROOT/frontend"
SHARED_SGLANG_PYTHON="$(get_shared_sglang_python "$PROJECT_ROOT")"
YAML_OVERALL="$PROJECT_ROOT/config/config-overall.yaml"

if [ ! -f "$YAML_OVERALL" ]; then
    echo "❌ 错误: 配置文件不存在: $YAML_OVERALL"
    exit 1
fi

if [ ! -x "$SHARED_SGLANG_PYTHON" ]; then
    echo "❌ 未找到共享 .sglang Python: $SHARED_SGLANG_PYTHON"
    exit 1
fi
cd "$PROJECT_ROOT"

if ! ASR_CONFIG_ASSIGNMENTS="$("$SHARED_SGLANG_PYTHON" - "$PROJECT_ROOT" "$YAML_OVERALL" <<'PYEOF'
import sys
from pathlib import Path

import yaml
from backend.asr.config import load_asr_config


def q(value):
    value = "" if value is None else str(value)
    return "'" + value.replace("'", "'\\''") + "'"


project_root = Path(sys.argv[1])
overall_path = Path(sys.argv[2])
overall = yaml.safe_load(overall_path.read_text(encoding="utf-8")) or {}
server_path = project_root / overall.get("server", "config/config-server.yaml")
server_cfg = yaml.safe_load(server_path.read_text(encoding="utf-8")) or {}
frontend = server_cfg.get("frontend", {}) or {}
asr_path = project_root / overall.get("asr", "config/config-asr.yaml")
asr_cfg = load_asr_config(asr_path, project_root=project_root)
security = asr_cfg.browser_security

def resolve(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else project_root / path

print(f"FRONTEND_DEV_PORT={q(frontend.get('dev_port', 3000))}")
print(f"FRONTEND_API_BASE_URL={q(frontend.get('api_base_url', 'http://localhost:8090'))}")
print(f"BACKEND_PORT={q(server_cfg.get('server', {}).get('port', 8090))}")
print(f"ASR_HTTPS_ENABLED={q(str(security.https_enabled).lower())}")
print(f"ASR_CA_CERTIFICATE={q(resolve(security.ca_certificate_path))}")
print(f"ASR_CERTIFICATE={q(resolve(security.certificate_path))}")
print(f"ASR_PRIVATE_KEY={q(resolve(security.private_key_path))}")
PYEOF
)"; then
    echo "❌ ASR typed/TLS config validation failed; frontend was not started."
    exit 1
fi
eval "$ASR_CONFIG_ASSIGNMENTS"

FRONTEND_DEV_PORT="${FRONTEND_DEV_PORT_OVERRIDE:-$FRONTEND_DEV_PORT}"
FRONTEND_API_BASE_URL="${FRONTEND_API_BASE_URL_OVERRIDE:-$FRONTEND_API_BASE_URL}"
if [ -z "${NEXT_ALLOWED_DEV_ORIGINS:-}" ]; then
    NEXT_ALLOWED_DEV_ORIGINS="$(hostname -I 2>/dev/null | tr ' ' ',' | sed 's/,$//')"
fi

echo "启动前端开发服务器..."
echo "项目根目录: $PROJECT_ROOT"
echo "前端端口: $FRONTEND_DEV_PORT"
echo "后端 API: $FRONTEND_API_BASE_URL"
echo "允许的开发来源: ${NEXT_ALLOWED_DEV_ORIGINS:-未设置}"
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
    if [ -d "$SHARED_FRONTEND_ROOT/node_modules" ]; then
        echo "📦 检测到 worktree 缺少依赖，复用共享依赖: $SHARED_FRONTEND_ROOT/node_modules"
        ln -s "$SHARED_FRONTEND_ROOT/node_modules" node_modules
    else
        echo "📦 检测到未安装依赖，正在运行 npm install..."
        npm install
        if [ $? -ne 0 ]; then
            echo "❌ 错误: npm install 失败"
            exit 1
        fi
    fi
fi

# 启动开发服务器
echo "🚀 启动 Next.js 开发服务器..."
NEXT_ARGS=(--port "$FRONTEND_DEV_PORT")
BACKEND_API_BASE_URL="$FRONTEND_API_BASE_URL"
if [ "$ASR_HTTPS_ENABLED" = "true" ]; then
    for name in ASR_CA_CERTIFICATE ASR_CERTIFICATE ASR_PRIVATE_KEY; do
        value="${!name/#\~/$HOME}"
        [[ "$value" = /* ]] || value="$PROJECT_ROOT/$value"
        printf -v "$name" '%s' "$value"
        [ -f "$value" ] || { echo "ASR HTTPS file missing: $value"; exit 1; }
    done
    NEXT_ARGS+=(--experimental-https --experimental-https-key "$ASR_PRIVATE_KEY" --experimental-https-cert "$ASR_CERTIFICATE" --experimental-https-ca "$ASR_CA_CERTIFICATE")
    BACKEND_API_BASE_URL="https://localhost:${BACKEND_PORT}"
    export NODE_EXTRA_CA_CERTS="$ASR_CA_CERTIFICATE"
    LAN_HOST="$(hostname -I 2>/dev/null || true)"
    LAN_HOST="${LAN_HOST%% *}"
    LAN_HOST="${LAN_HOST:-localhost}"
    echo "HTTPS Frontend: https://${LAN_HOST}:${FRONTEND_DEV_PORT}"
    echo "Android CA: $ASR_CA_CERTIFICATE"
fi
NEXT_ALLOWED_DEV_ORIGINS="$NEXT_ALLOWED_DEV_ORIGINS" \
NEXT_PUBLIC_API_BASE_URL="$FRONTEND_API_BASE_URL" \
BACKEND_API_BASE_URL="$BACKEND_API_BASE_URL" \
npm run dev -- "${NEXT_ARGS[@]}"
