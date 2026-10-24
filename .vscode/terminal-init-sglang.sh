#!/bin/bash

WORKSPACE_ROOT="$(cd "${BASH_SOURCE%/*}/.." && pwd)"

source "$WORKSPACE_ROOT/scripts/_workspace_paths.sh"
SHARED_WORKSPACE_ROOT="$(get_shared_workspace_root "$WORKSPACE_ROOT")"

# 不记录 workspace terminal 历史
unset HISTFILE

# 加载正常 Bash 配置
# 包括 alias / PATH / update_proxy 等
if [[ -f "$HOME/.bashrc" ]]; then
    source "$HOME/.bashrc"
fi

# 激活项目 Python 虚拟环境
source "$SHARED_WORKSPACE_ROOT/.sglang/bin/activate"

# 安装 opencode（如果尚未安装）
if [[ ! -x "$HOME/.opencode/bin/opencode" ]]; then
    echo "Installing opencode..."
    curl -fsSL https://opencode.ai/install | bash
fi

# opencode PATH
export PATH="$PATH:$HOME/.opencode/bin"

# 清除当前 shell 历史
history -c
set +H