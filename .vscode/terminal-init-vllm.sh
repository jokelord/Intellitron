#!/bin/bash

WORKSPACE_ROOT="$(cd "${BASH_SOURCE%/*}/.." && pwd)"
source "$WORKSPACE_ROOT/scripts/_workspace_paths.sh"
SHARED_WORKSPACE_ROOT="$(get_shared_workspace_root "$WORKSPACE_ROOT")"

# 清除可能触发历史命令的变量
unset HISTFILE

# 加载基本的 .bashrc，但跳过 conda 初始化
if [ -f ~/.bashrc ]; then
    # 设置标志以阻止 conda 初始化
    __VSCODE_SKIP_CONDA=1
    source ~/.bashrc
    unset __VSCODE_SKIP_CONDA
fi

# 激活 .vllm 虚拟环境
source "$SHARED_WORKSPACE_ROOT/.vllm/bin/activate"

# 检查并安装 opencode 如果未安装
if [ ! -f "$HOME/.opencode/bin/opencode" ]; then
    echo "Installing opencode..."
    curl -fsSL https://opencode.ai/install | bash
fi

# 添加 opencode 到 PATH
export PATH="$PATH:$HOME/.opencode/bin"

# 确保 conda 命令不可用
unset -f conda 2>/dev/null
alias conda='echo "Conda is disabled in this workspace. Using .vllm environment."'

# 清理命令历史，防止自动执行旧命令
history -c
set +H
