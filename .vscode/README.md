# VS Code 配置说明

本目录包含 LLM-deployment 项目的 VS Code 工作区配置。

## settings.json

主要配置内容：

### Python 环境配置

- **默认解释器**：自动使用 uv 管理的虚拟环境 `.sglang` (Python 版本根据环境而定)
- **终端自动激活**：每次打开新终端时自动激活相应虚拟环境
- **语言服务器**：使用 Pylance 提供智能感知和类型检查

### 路径配置说明

默认配置使用项目内的 uv 虚拟环境。如果需要自定义，请修改 `settings.json` 中的路径：

```json
// 使用 .sglang 环境（默认）
"python.defaultInterpreterPath": "${workspaceFolder}/.sglang/bin/python"

// 使用 .vllm 环境
"python.defaultInterpreterPath": "${workspaceFolder}/.vllm/bin/python"

// 自定义 uv 环境路径
"python.defaultInterpreterPath": "/path/to/your/uv/env/bin/python"
```

### 终端配置

配置了两个终端配置文件：

- `sglang (workspace)`：自动激活 `.sglang` 虚拟环境
- `vllm (workspace)`：自动激活 `.vllm` 虚拟环境

每次打开新终端时会自动：

1. 激活指定的 uv 虚拟环境
2. 进入项目根目录

### 代码格式化

- **Python**：使用 Black 格式化
- **TypeScript/JavaScript**：使用 Prettier 格式化
- **保存时自动格式化**：已启用

## uv 环境管理

### 切换 uv 环境的方法

由于 VS Code 的终端初始化脚本只在终端启动时运行，无法在已运行的终端中动态切换环境。要切换到不同环境，请按以下步骤操作：

1. **创建新终端**：
   - 按 `Ctrl+Shift+`` 打开新终端
   - 在终端下拉菜单中选择 `sglang (workspace)` 或 `vllm (workspace)`

2. **手动切换（在现有终端中）**：
   ```bash
   # 切换到 .sglang 环境
   source ${workspaceFolder}/.sglang/bin/activate

   # 切换到 .vllm 环境
   source ${workspaceFolder}/.vllm/bin/activate
   ```

### 为什么不能在同一个终端内动态切换？

VS Code 的终端 profile 配置是静态的，初始化脚本（如 `terminal-init-sglang.sh`）仅在创建终端时执行一次。一旦终端运行，无法重新运行初始化脚本或动态更改环境。这是因为：

- 终端的环境变量和激活状态在启动后固定
- VS Code 不支持在运行中终端重新初始化
- 手动 `source` 命令可以临时切换，但不会持久

如果需要频繁切换，建议使用多个终端窗口，每个对应一个环境。

## 推荐的 VS Code 扩展

安装以下扩展以获得最佳开发体验：

### Python 开发
- `ms-python.python` - Python 支持
- `ms-python.vscode-pylance` - Pylance 语言服务器
- `ms-python.black-formatter` - Black 代码格式化

### 前端开发
- `dbaeumer.vscode-eslint` - ESLint
- `esbenp.prettier-vscode` - Prettier 格式化
- `bradlc.vscode-tailwindcss` - Tailwind CSS 智能感知

### 通用工具
- `ms-vscode.makefile-tools` - Makefile 支持
- `redhat.vscode-yaml` - YAML 支持

## 故障排查

### 终端未自动激活 uv 环境

1. 检查 uv 是否安装：
   ```bash
   uv --version
   ```

2. 重启 VS Code

3. 手动检查虚拟环境路径：
   ```bash
   ls -la ${workspaceFolder}/.sglang/bin/python
   ls -la ${workspaceFolder}/.vllm/bin/python
   ```

### Python 解释器未找到

1. 确认虚拟环境已创建：
   ```bash
   ls -la ${workspaceFolder}/.sglang
   ls -la ${workspaceFolder}/.vllm
   ```

2. 在 VS Code 中手动选择解释器：
   - 按 `Ctrl+Shift+P`
   - 输入 "Python: Select Interpreter"
   - 选择相应虚拟环境的 Python

3. 检查 `settings.json` 中的路径是否正确

### uv 环境管理问题

1. 创建新虚拟环境：
   ```bash
   uv venv .newenv
   source .newenv/bin/activate
   ```

2. 安装依赖：
   ```bash
   uv pip install -r requirements.txt
   ```

3. 同步依赖：
   ```bash
   uv sync
   ```
