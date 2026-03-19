# Plan: 后端升级为 Agent 工具调用后端

**TL;DR**：在不修改现有 `/v1/chat/completions` 的前提下，新增 `/v1/agent/chat` 端点，由 smolagents 框架驱动 ToolCallingAgent/CodeAgent 两种模式，配备 5 类工具（文件读写、精确编辑、Shell 执行、知识库查询），前端同步展示折叠的 Thought/Action/Observation 步骤。

---

## Phase 1 — 工具层 (Tools)

### 步骤 1 — 创建 `backend/tools/editblock_utils.py`
从 `aider-main/aider/coders/editblock_coder.py` 复制以下 7 个核心函数（attribution 注释置于文件头部），去除 aider 内部依赖，仅保留标准库（`difflib`、`re`、`math`）：
- `prep()` — 预处理入参（字符串 → 行列表）
- `replace_most_similar_chunk()` — 主入口，依次尝试 4 种策略
- `perfect_replace()` — 精确字节匹配
- `perfect_or_whitespace()` — 精确匹配 + 空白归一化
- `replace_part_with_missing_leading_whitespace()` — 缩进偏差修复
- `try_dotdotdots()` — `...` 省略行支持
- `replace_closest_edit_distance()` — SequenceMatcher 模糊匹配（阈值 0.8，±10% 长度容忍）

### 步骤 2 — 创建 `backend/tools/file_ops.py`
- `read_file(path, start_line=None, end_line=None)` — `with open` 读取，每行前加行号前缀（方便 LLM 后续定位）
- `write_file(path, content)` — 覆盖写入，借鉴 aider `io.py` 的指数退避重试（最多 5 次，应对文件锁）
- `edit_file(path, search, replace)` — 调用 `editblock_utils.replace_most_similar_chunk`；匹配失败时返回含最相似片段的错误提示，提示 LLM 修正搜索块

### 步骤 3 — 创建 `backend/tools/sys_ops.py`
- `exec_command(command, timeout=60)` — `subprocess.run(shell=True, capture_output=True, text=True, timeout=timeout)`
- 黑名单正则（不区分大小写）至少覆盖：
  - `rm\s+-rf\s+/` — 递归删除根目录
  - `sudo\s+` — 提权
  - `mkfs` — 格式化设备
  - `dd\s+.*of=/dev/` — 磁盘写入
  - `:(){ :|:& };:` — Fork Bomb
  - `shutdown|reboot|poweroff|halt` — 系统关机
  - `chmod\s+[0-7]*7[0-7]*\s+/` — 根目录权限扩大
- 命中黑名单时拒绝执行，返回 `"命令被安全策略阻断: {匹配的规则}"`

### 步骤 4 — 创建 `backend/tools/skill_ops.py`
- `list_skills()` — 遍历 `backend/skills/` 目录，用 `pyyaml` 解析所有 `metadata.yaml`，返回 JSON 列表（含 name/description/tags）
- `read_skill(skill_name: str)` — 读取 `backend/skills/{skill_name}/instructions.md`，内容直接注入上下文

### 步骤 5 — 创建样本知识库 `backend/skills/git_workflow/`
- `metadata.yaml` — 包含 `name`, `description`, `tags` 字段
- `instructions.md` — git 常用命令 SOP 示例（提交、分支、合并冲突）

---

## Phase 2 — Agent 层

### 步骤 6 — 安装 smolagents
在 `.sglang` 虚拟环境（uv 管理）中执行：
```bash
uv pip install smolagents
```
user注：已经在.sglang环境中安装完成，并且在 `requirements.txt` 中添加了 `smolagents` 依赖。  
**注意**：`smolagents-main/` 本地目录代码**不直接 import**，只从 PyPI 安装。

### 步骤 7 — 创建 `backend/agent/local_llm_model.py`
继承 smolagents 的 `Model` 基类，包装现有 `LLMService.chat()` 方法（**直接内部调用，不走 HTTP 回环**）：
- `LocalLLMModel(llm_service: LLMService, config: AppConfig)`
- `generate(messages, stop_sequences=None, tools_to_call_from=None)` 实现：
  1. 将 smolagents `ChatMessage` 列表转换为 LLMService 期望的 `dict` 格式
  2. 若有 `tools_to_call_from`，转换为 OpenAI tools JSON 格式，传给 LLMService
  3. 调用 `llm_service.chat()`（同步），获取原始响应
  4. 解析响应中的 `tool_calls`（如有），封装为 smolagents `ChatMessage` 对象返回
- 需兼容 `ToolCallingAgent`（结构化 tool_calls）和 `CodeAgent`（纯文本代码块）两种调用模式

### 步骤 8 — 创建 `backend/agent/agent_runner.py`
```python
@dataclass
class AgentSession:
    agent: MultiStepAgent   # ToolCallingAgent 或 CodeAgent
    last_activity: datetime
    mode: str               # "tool_calling" | "code"

class SessionStore:
    sessions: dict[str, AgentSession]
    def cleanup_expired(self, ttl_minutes: int): ...

class AgentRunner:
    def build_agent(self, session_id: str, mode: str) -> MultiStepAgent: ...
    async def run_stream(self, session_id: str, message: str) -> AsyncGenerator[str, None]: ...
```

SSE 事件格式（每行 `data: {json}\n\n`）：

| `type` 字段 | `step_type` 字段 | 含义 |
|---|---|---|
| `agent_step` | `thought` | LLM 思维过程文本 |
| `agent_step` | `tool_call` | 工具名 + 入参 JSON |
| `agent_step` | `observation` | 工具返回结果 |
| `content` | — | 最终答案文本块 |
| `done` | — | 流结束标志 |

---

## Phase 3 — API 与配置层

### 步骤 9 — 创建 `backend/api/agent.py`
- `POST /v1/agent/chat`
  - 请求体：`{ session_id: str, message: str, stream: bool = true, mode: str | null = null }`
  - `mode` 为 null 时使用 config 默认值
  - 流式：`StreamingResponse` 返回 SSE
  - 非流式：等待所有步骤后返回 `{ steps: [...], answer: str }`
- `DELETE /v1/agent/session/{session_id}` — 清理指定 session

### 步骤 10 — 修改 `backend/main.py`
- 在 `lifespan` 中初始化 `AgentRunner` 并注册 `SessionStore`
- 当 `config.agent.enabled == True` 时，注册 `agent_router`（`include_router`）

### 步骤 11 — 修改 `backend/config.py`
新增 `AgentConfig` dataclass：
```python
@dataclass
class AgentConfig:
    enabled: bool = True
    agent_mode: str = "tool_calling"   # "tool_calling" | "code"
    max_steps: int = 20
    session_ttl_minutes: int = 60
    exec_timeout: int = 60
    work_dir: str | None = None        # None = os.getcwd()
```
在 `AppConfig` 中添加 `agent: AgentConfig` 字段，并在 YAML 加载逻辑中解析 `agent:` 配置段。

### 步骤 12 — 修改 `config/config-misc.yaml`
```yaml
agent:
  enabled: true
  agent_mode: tool_calling  # "tool_calling" | "code"
  max_steps: 20
  session_ttl_minutes: 60
  exec_timeout: 60
  work_dir: null
```

---

## Phase 4 — 前端展示

### 步骤 13 — 新建 `frontend/src/components/AgentStepDisplay.tsx`
折叠卡片组件，接收 `steps: AgentStep[]`：
- `thought` — 🤔 标题 + 文本内容，默认折叠
- `tool_call` — 🔧 标题 + tool name + 参数 JSON，默认折叠
- `observation` — 👁 标题 + 结果文本，默认折叠
- Tailwind 样式，支持点击展开/收起

```typescript
interface AgentStep {
  type: "agent_step";
  step_type: "thought" | "tool_call" | "observation";
  content?: string;
  name?: string;        // tool_call only
  input?: object;       // tool_call only
}
```

### 步骤 14 — 修改前端聊天组件
SSE 处理器中按 `type` 字段分流：
- `type === "agent_step"` → 追加到本条消息的 `agentSteps[]` 状态
- `type === "content"` → 追加到流式消息内容
- `type === "done"` → 终止流

消息对象增加：
```typescript
interface Message {
  // ...existing fields
  agentSteps?: AgentStep[];
}
```

Agent 模式下，发送请求改用 `POST /v1/agent/chat`，携带 `session_id`（UUID，存于组件 state）。

### 步骤 15 — 工具栏 Agent 模式开关（可选）
- 工具栏新增 Toggle：「普通对话 / Agent 模式」
- 选中状态存入 `localStorage`（key: `agentMode`）
- Agent 模式下显示当前 `session_id` 标识及「重置 Session」按钮

---

## 关键文件总览

| 文件 | 操作 | 备注 |
|------|------|------|
| `backend/tools/editblock_utils.py` | 新建 | 复制自 aider editblock_coder.py，attribution 注释 |
| `backend/tools/file_ops.py` | 新建 | 使用 editblock_utils |
| `backend/tools/sys_ops.py` | 新建 | subprocess + 黑名单 |
| `backend/tools/skill_ops.py` | 新建 | pyyaml |
| `backend/skills/git_workflow/metadata.yaml` | 新建 | 样本知识库 |
| `backend/skills/git_workflow/instructions.md` | 新建 | 样本知识库 |
| `backend/agent/local_llm_model.py` | 新建 | smolagents Model 子类，桥接 LLMService |
| `backend/agent/agent_runner.py` | 新建 | SessionStore + AgentRunner |
| `backend/api/agent.py` | 新建 | FastAPI router |
| `backend/main.py` | 修改 | 注册 agent_router，lifespan 初始化 |
| `backend/config.py` | 修改 | 增加 AgentConfig |
| `config/config-misc.yaml` | 修改 | 增加 agent: 配置段 |
| `frontend/src/components/AgentStepDisplay.tsx` | 新建 | 折叠步骤卡片 |
| `frontend/src/app/…` 聊天组件 | 修改 | SSE 分流 + agentSteps 状态 |

---

## 验证步骤

1. **工具单元测试**：直接 Python 调用 `read_file`、`write_file`、`edit_file`、`exec_command`
2. **editblock 三场景测试**：精确匹配、缩进偏差（少 4 空格）、模糊匹配（相似度 0.85）
3. **安全测试**：`rm -rf /` / `sudo whoami` 被拒绝；`ls -la` / `echo hello` 正常执行
4. **Agent API 集成测试**：`POST /v1/agent/chat` 发送「读取 /etc/hostname」任务，SSE 流返回 `agent_step` 事件
5. **Session 连续性测试**：同一 `session_id` 两次请求，确认 Agent 内存保持上下文
6. **前端集成测试**：UI 发送「读取 /etc/hostname 文件」，观察折叠思维链步骤渲染正确

---

## 范围边界（明确不含）

- `web_search` / `web_fetch` — 保留占位函数，调用时返回 `NotImplementedError("此工具尚未实现")`
- 数据库 — session 使用纯内存字典，重启后清空
- litellm — 不引入，用 `LocalLLMModel` 直接桥接 LLMService
- 现有 `/v1/chat/completions` — 完全不修改，两套端点并存
- `smolagents-main/` 本地目录 — 不直接 import，仅从 PyPI 安装 `smolagents`
