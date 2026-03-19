# Backend 变更记录

---

## 2026-03-13

### Bug Fix: `tools` 参数未传入推理调用链

**问题描述**

`POST /v1/chat/completions` 端点在收到 `tools` 字段时，会调用 `convert_tools_for_sglang()` 将工具转换为 SGLang 兼容格式，但转换结果 `converted_tools` 从未传入实际的推理调用，导致工具调用功能在该端点完全失效。
相同问题存在于流式和非流式两条路径，以及底层 `chat_stream_async` 方法。

**涉及文件**

- `backend/api/chat.py`
- `backend/llm_service.py`

**修复内容**

| 文件 | 位置 | 修改 |
|---|---|---|
| `api/chat.py` | `create_chat_completion()` 非流式分支 | `service.chat_async()` 调用添加 `tools=converted_tools` |
| `api/chat.py` | `create_chat_completion()` 流式分支 | `_generate_stream_response()` 调用添加 `tools=converted_tools` |
| `api/chat.py` | `_generate_stream_response()` 函数签名 | 添加 `tools: Optional[List[dict]] = None` 参数 |
| `api/chat.py` | `_generate_stream_response()` 内部 | `service.chat_stream_async()` 调用添加 `tools=tools` |
| `llm_service.py` | `LLMService.chat_stream_async()` 函数签名 | 添加 `tools: Optional[List[dict]] = None` 参数 |
| `llm_service.py` | `LLMService.chat_stream_async()` 内部 | `self.chat()` 调用添加 `tools=tools` |

**备注**

`POST /v1/responses`（ClawdBot 专用端点）的工具传递逻辑在修复前已正确实现，无需更改。

---

## 2026-03-17 ~ 03-18

### Feature: Agent Tool Calling 后端实现（Phase 1–3）

**背景**

在不修改现有 `/v1/chat/completions` 端点的前提下，新增独立的 `/v1/agent/chat` 端点，由 [smolagents](https://github.com/huggingface/smolagents) 框架驱动 `ToolCallingAgent`，配备文件读写、精确编辑、Shell 执行、技能库查询等工具，前端同步以折叠卡片展示 Thought / Action / Observation 三步骤。

---

#### Phase 1 — 工具层

**新建 `backend/tools/editblock_utils.py`**

从 `aider` 的 `editblock_coder.py` 提取 7 个核心函数（文件头部保留 attribution 注释），去除 aider 内部依赖，仅保留标准库（`difflib`、`re`、`math`）：

| 函数 | 策略 |
|---|---|
| `prep()` | 预处理：字符串 → 行列表 |
| `replace_most_similar_chunk()` | 主入口，依次尝试以下 4 种策略 |
| `perfect_replace()` | 精确字节匹配 |
| `perfect_or_whitespace()` | 精确匹配 + 空白归一化 |
| `replace_part_with_missing_leading_whitespace()` | 缩进偏差容错修复 |
| `try_dotdotdots()` | `...` 省略行支持 |
| `replace_closest_edit_distance()` | SequenceMatcher 模糊匹配（阈值 0.8，长度容忍 ±10%） |

**新建 `backend/tools/file_ops.py`**

- `read_file(path, start_line, end_line)` — 读取文件，每行前加行号前缀（便于 LLM 定位）
- `write_file(path, content)` — 覆盖写入，借鉴 aider 的指数退避重试（最多 5 次，应对文件锁）
- `edit_file(path, search_text, replace_text)` — 调用 `editblock_utils`；匹配失败时返回含最相似片段的提示，引导 LLM 修正搜索块

**新建 `backend/tools/sys_ops.py`**

- `exec_command(command, timeout)` — `subprocess.run(shell=True, capture_output=True, text=True, timeout=timeout)`
- 黑名单正则（不区分大小写）覆盖：`rm -rf /`、`sudo`、`mkfs`、`dd of=/dev/`、Fork Bomb `:(){ :|:& };:`、`shutdown/reboot/poweroff/halt`、根目录权限扩大 `chmod 777 /`
- 命中黑名单时拒绝执行，返回 `"命令被安全策略阻断: {匹配的规则}"`

**新建 `backend/tools/skill_ops.py`**

- `list_skills()` — 遍历 `backend/skills/` 目录，用 `pyyaml` 解析 `metadata.yaml`，返回 JSON 列表（name/description/tags）
- `read_skill(skill_name)` — 读取 `backend/skills/{skill_name}/instructions.md`，直接注入上下文

同时新建样本知识库 `backend/skills/git_workflow/`（`metadata.yaml` + `instructions.md`）。

---

#### Phase 2 — Agent 层

**新建 `backend/agent/local_llm_model.py`**

继承 smolagents `Model` 基类，将 `LLMService.chat()`（同步）包装为 smolagents 兼容的 `generate()` 接口，**直接内部调用，不走 HTTP 回环**：

1. 用 `get_clean_message_list()` 将 smolagents `ChatMessage` 列表规范化
2. 用 `get_tool_json_schema()` 将工具列表转换为 OpenAI function 格式
3. 调用 `LLMService.chat()` 同步获取 `GenerationResult`
4. 从 `result.raw.choices[0].message.tool_calls` 解析结构化工具调用，封装为 `ChatMessageToolCall` 返回
5. 回退：若无结构化字段，尝试从文本中解析 JSON 格式工具调用

**新建 `backend/agent/agent_runner.py`**

核心组件：

- `AgentSession` — dataclass，持有 `agent`、`mode`、`created_at`、`last_activity`
- `SessionStore` — 纯内存字典，提供 `get / put / delete / cleanup_expired`
- `AgentRunner` — 调度器，核心方法：
  - `_build_agent(mode)` — 构建 `ToolCallingAgent`（或 `CodeAgent`），注入 9 个工具
  - `get_or_create_session(session_id, mode)` — 复用或新建 session
  - `run_stream(session_id, message)` — 在后台线程运行 smolagents，通过 `asyncio.Queue` 将步骤推送为 SSE 事件流
  - `run_sync(session_id, message)` — 非流式运行，运行完后从 `agent.memory.steps` 提取步骤

**SSE 事件格式**

| `type` | `step_type` | 含义 |
|---|---|---|
| `agent_step` | `thought` | LLM 思维过程文本（`model_output`） |
| `agent_step` | `tool_call` | 工具名 + 入参 JSON（`ToolCall`） |
| `agent_step` | `observation` | 工具执行结果（`ToolOutput.observation`） |
| `content` | — | 最终答案文本块（`ActionOutput.is_final_answer`） |
| `done` | — | 流结束标志 |
| `error` | — | 异常信息 |

smolagents `stream=True` 模式下按事件类型分流处理：`MemoryToolCall` → tool_call、`SmolToolOutput` → observation、`ActionOutput(is_final_answer=True)` → content、`MemoryActionStep` → thought（从 `model_output` 提取）。

系统提示（`AGENT_SYSTEM_PROMPT`）要求模型在 `final_answer` 中包含完整实际数据，因为用户只能看到最终答案，看不到中间步骤。

---

#### Phase 3 — API 与配置层

**新建 `backend/api/agent.py`**

- `POST /v1/agent/chat` — 请求体 `{session_id, message, stream, mode}`；流式返回 `StreamingResponse(SSE)`；非流式等待所有步骤后返回 `{session_id, steps, answer}`
- `DELETE /v1/agent/session/{session_id}` — 清理指定 session

**修改 `backend/config.py`**

新增 `AgentConfig` dataclass：

```python
@dataclass
class AgentConfig:
    enabled: bool = True
    agent_mode: str = "tool_calling"   # "tool_calling" | "code"
    max_steps: int = 20
    session_ttl_minutes: int = 60
    exec_timeout: int = 60
    work_dir: str | None = None
```

`AppConfig` 新增 `agent: AgentConfig` 字段；YAML 加载逻辑中解析 `agent:` 配置段。

**修改 `backend/main.py`**

- `lifespan` 中当 `config.agent.enabled` 为 `True` 时初始化 `AgentRunner` 并注册 `agent_router`
- `/config` 端点响应中新增 `agent` 字段（`enabled`、`mode`）

**修改 `config/config-misc.yaml`**

新增：

```yaml
agent:
  enabled: true
  agent_mode: tool_calling
  max_steps: 20
  session_ttl_minutes: 60
  exec_timeout: 60
  work_dir: null
```

---

### Bug Fix: SGLang 工具格式 400 错误

**问题描述**

Agent 工具调用激活后，`AgentRunner` 通过 `LocalLLMModel.generate()` 将 smolagents 工具列表（经 `get_tool_json_schema()` 生成，格式为标准 OpenAI `{"type": "function", "function": {...}}`）传入 `LLMService.chat()`，后者内部调用 `convert_tools_for_sglang()`。

原 `convert_tools_for_sglang()` 实现针对 ClawdBot 的 `web_search_preview` 等自定义工具类型设计，对 smolagents 生成的全部 `type="function"` 工具无法正确透传：
- 大部分工具（`agent_read_file`、`agent_write_file` 等）被识别为"不支持的工具格式"而丢弃，日志输出 `🌐 [API] 跳过不支持的函数工具: agent_xxx`
- `agent_web_search` / `agent_web_fetch` 被错误地转换为 `{"type": "web_search_preview", "parameters": {...}}` 格式（缺少 `"function"` wrapper）

SGLang 端报 422 / 400 错误：

```
{'type': 'missing', 'loc': ('body', 'tools', 0, 'function'), 'msg': 'Field required'}
```

**修复内容**

重写 `backend/api/chat.py` 中的 `convert_tools_for_sglang()`：

- `type="function"` 且包含 `"function"` wrapper 的工具 → **直接透传**（此为 smolagents `get_tool_json_schema()` 输出的标准格式）
- `type="web_search_preview"` / `type="code_interpreter"` → 保留（SGLang 原生类型）
- 其他格式 → 跳过并写入 debug 日志

修复后 9 个 smolagents 工具全部通过验证，SGLang 正确解析工具定义并返回结构化 `tool_calls`。

**涉及文件**

- `backend/api/chat.py` — 重写 `convert_tools_for_sglang()`
=== File end ===

## 2026-03-19

### Bug Fix: Agent Step 2+ 重复上传用户原始图片

**问题描述**

smolagents Agent 在 Step 2+ 时重复上传用户原始图片，导致 visual token 随 step 数线性增长。
日志显示 Step 2 的消息 3[0]、Step 3 的消息 5[0] 等位置出现与 Step 1 相同的图片。

**根本原因**

smolagents 存在两条路径都会把用户原始图片注入历史消息：

| 路径 | 位置 | 旧修复是否覆盖 |
|------|------|---------------|
| `TaskStep.task_images` | `TaskStep.to_messages()` 每次重建消息时附加图片 | ✅ 已修复 |
| `ActionStep.observations_images` | `_run_stream` 创建**每个** ActionStep 时都传 `observations_images=images`（原始task图片） | ❌ 未修复 |

消息 3/5/... 最后那条 user 消息 = `observations_images` + 工具文本合并后的 USER 消息，`_strip_old_images` 发现它是"最后一条 user 消息"而保留了它，导致图片逃过了 `llm_service.py` 层的防线。

**涉及文件**

- `backend/agent/agent_runner.py` — 扩展 `SmartToolCallingAgent.write_memory_to_messages()` 方法

**修复内容**

在 `write_memory_to_messages()` 中，step 2+ 时对历史 ActionStep 也做 `observations_images = None`。
经代码库确认，`observations_images` 在 smolagents 中**只有 `_run_stream` L573 这一处赋值**，从不存放工具真正返回的图片（工具图片走 `AgentImage → state['image.png']` 路径），所以清空安全无副作用。

**备注**

