这是一份无需自造轮子、依靠开源库拼接的**“Agent Skills (静态知识) + Progressive MCP (动态本地手脚)”**具体技术细节与顶层规划。
备注：优先使用本仓库/.sglang虚拟环境（uv管理）
---

### 一、 核心技术栈与底层库 (Ubuntu 24 + Python 3.12+)

我们将使用以下 Python 库来拼装你的后端：

1.  **`smolagents` (核心调度与 Agent Loop)**：
    使用其 `CodeAgent` 引擎。它负责维持 ReAct 循环，解析 Skills，并生成直接调用本地基础工具的 Python 代码。
2.  **`litellm` (模型路由层)**：
    负责将 `smolagents` 的请求代理到你的自定义大语言模型后端。
3.  **`mcp` (Anthropic 官方 Python SDK)**：
    用于未来将这些基础工具标准化为 MCP Server。初期可以直接用 `smolagents` 的 `@tool` 装饰器，后续平滑迁移至标准 MCP 协议。
4.  **`unidiff` / `regex` (辅助库)**：
    专门用于实现高难度的 `edit`（精确修改代码）功能。

---

### 二、 目录结构规划 (Progressive Disclosure 层)

在你的 Python 后端项目中，建立静态的知识库目录。大模型初始只加载 `metadata.yaml`（几十个 Token），按需再加载 `instructions.md`。

```text
（以下树形结构所涉名称仅供参考，以本仓库实际的为准；如需新建，可也参考以下的名称）
/backend
  /skills                  # 静态知识库 (大脑)
    /git_workflow
      - metadata.yaml      # 摘要: "提供标准的 Git 提交流程和解决冲突规范"
      - instructions.md    # 详细的 Bash 命令模板和规范
    /data_analysis
      - metadata.yaml      # 摘要: "包含使用 Pandas 处理 Excel/CSV 的最佳实践"
      - instructions.md    # 具体的 Python 代码片段
  /tools                   # 本地执行器 (手脚 - 替代了云端 Sandbox)
    - file_ops.py          # 包含 read, write, edit
    - sys_ops.py           # 包含 exec (Bash/Python)
    - web_ops.py           # 包含 web_fetch, web_search (占位)
  - agent_loop.py          # smolagents 运行中枢
```

---

### 三、 基础工具 (Tools) 实施方案细节

这些是你需要用 Python 实现并注入给 Agent 的“手脚”。按照“多拼接”原则，以下是实现思路：

#### 1. 认知与学习工具 (核心骨架)
* **`list_skills()`**: 
    * **实现**：使用 Python 标准库 `os` 遍历 `/skills` 目录，用 `pyyaml` 解析所有 `metadata.yaml`，返回一个 JSON 或文本列表给大模型。
* **`read_skill(skill_name: str)`**: 
    * **实现**：读取 `/skills/{skill_name}/instructions.md`，将纯文本内容注入当前的 Context。

#### 2. 文件与系统操作 (Agent 基础四件套)
你可以直接查阅 `smolagents` 源码中的 `default_tools.py` 抄作业，无需手写。
* **`read_file(file_path: str)`**:
    * **实现**：标准的 `with open(path, 'r')`，注意添加行号，方便大模型后续执行 `edit` 操作。
* **`write_file(file_path: str, content: str)`**:
    * **实现**：标准的 `with open(path, 'w')` 覆盖写入。
* **`exec_command(command: str)`**:
    * **实现**：由于没有沙盒，直接使用 Python 的 `subprocess.run(command, shell=True, capture_output=True, text=True)`。注意设置一个合理的 `timeout`（比如 60 秒），防止死循环卡死后端。
* **`edit_file(file_path: str, search_string: str, replace_string: str)`**:
    * **技术细节（最难点）**：绝对不要让模型重写整个文件，Token 成本极高且容易丢代码。
    * **拼接方案**：直接借鉴 **`Aider-AI/aider`** 的 `SEARCH/REPLACE` 块匹配算法。使用 Python 的字符串查找或 `unidiff` 库，让大模型只输出需要被替换的几行上下文，和修改后的几行代码。

#### 3. Web 扩展操作 (占位)
目前先写好空函数并打上 `@tool` 装饰器，直接抛出 `NotImplementedError("正在开发中")`，大模型调用时会自动知道此路不通。
* **`web_search(query: str)`**: 预留给未来接入 DuckDuckGo 或 Tavily API。
* **`web_fetch(url: str)`**: 预留给未来使用 `beautifulsoup4` 或 `playwright` 抓取并转换为 Markdown。

---

### 四、 动态调度循环 (Agent Loop) 设计

在 `agent_loop.py` 中，你的组装逻辑如下：

1.  **实例化工具**：将上述所有 Python 函数（带有 `@tool` 装饰器）打包成一个 List。
2.  **配置 System Prompt**：
    “你是一个通过代码解决问题的 AI 助手。你运行在 Ubuntu 24 环境。你只有基础的读写执行能力。遇到复杂任务时，**必须**先调用 `list_skills` 查找是否有标准作业程序 (SOP)，如果有，调用 `read_skill` 学习它，然后再使用 `exec_command` 或 `edit_file` 执行具体动作。”
3.  **启动 CodeAgent**：
    将自定义的 LLM (通过 `litellm` 包装) 和工具 List 传给 `smolagents.CodeAgent`。大模型会在内部生成 Python 代码来按顺序调用这些工具。

---

### 五、 推荐的“拼接”源头 (GitHub 仓库)

不用自己从头构思边界情况（比如读取的文件过大怎么办、`exec` 报错怎么办），直接去以下仓库借鉴具体实现：

* **`huggingface/smolagents`**：直接复制它自带的 `LocalPythonExecutor` 类。它的AST（抽象语法树）解析写得非常好，能保证大模型生成的调用代码不会把你的 Python 进程搞崩溃。
* **`Aider-AI/aider`**：找到 `aider/coders/editblock_coder.py`。把里面的精准替换逻辑抽离出来，稍微改造一下，作为你自己的 `edit_file` 工具的核心代码。

---

这套方案建立了一个随时可以扩展为标准 MCP 架构的坚实底座。


