# Tool Orchestrator: 多工具编排执行系统设计

> 日期: 2026-04-11
> 状态: 待实现

## 1. 概述

### 1.1 问题

当前 SmartToolCallingAgent 每轮只处理一个工具调用（`config-agent-prompts.yaml:96` 明确限制"一次动作只能调用一个工具"）。LLM 需要分多轮对话、多次推理才能完成涉及多个工具的任务链（如"搜索信息 → 抓取网页"、"编辑文件 → 运行测试"）。这导致：
- 推理轮次多，延迟高
- 可并行的操作（如同时抓取多个网页）被迫串行
- 用户等待时间不必要地拉长

### 1.2 目标

让 LLM 在一次输出中返回多个 tool_calls，后端自动分析依赖关系、构建执行 DAG、并行/串行执行，大幅减少推理轮次和总耗时。

### 1.3 关键决策

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 编排逻辑位置 | 纯后端自动编排 | 对 LLM 能力要求最低，最可靠 |
| Web 搜索编排 | search_and_fetch / wikipedia_search_and_fetch 复合工具 | 对 LLM 透明，一步到位 |
| 前端展示 | 详细编排视图 | 用户能看到完整编排过程 |
| 错误处理 | 传播错误+继续 | 失败的工具不影响其他并行工具 |
| 依赖推断方式 | 静态规则（含只读并行优化） | 简单可靠，覆盖主要场景 |
| smolagents 集成 | SmartToolCallingAgent 内部 override process_tool_calls | 改动集中，不修改第三方库 |
| Timeout 管理 | LLM 自带 timeout 参数，后端 15s 默认值 | 不在 config yaml 配置，系统提示词中提及 |

### 1.4 技术前提

- Qwen3 模型原生支持一次返回多个 tool_calls（`<tool_call>` XML 标签格式，官方文档有示例）
- SGLang `qwen3_coder` parser 的 `re.findall()` 可解析多个 tool_call 块
- OpenAI API `tool_calls` 数组原生支持多元素（"Parallel Function Calling"）
- 项目现有文本解析路径 Format 1（XML `<tool_call>` 标签，`local_llm_model.py:667-692`）已支持 `re.findall()` 多匹配
- 结构化路径 `_parse_raw_tool_calls` (`local_llm_model.py:544-568`) 已遍历完整 tool_calls 数组

## 2. 架构设计

### 2.1 ToolOrchestrator 核心模块

新文件: `backend/agent/orchestrator.py`

```
输入: List[ChatMessageToolCall]  (LLM 一次输出的多个工具调用)
          ↓
    ┌─────────────────────┐
    │ DependencyAnalyzer   │  ← 静态规则表
    └────────┬────────────┘
             ↓
    ┌─────────────────────┐
    │ ExecutionPlan        │  ← stages = [[并行组1], [并行组2], ...]
    └────────┬────────────┘
             ↓
    ┌─────────────────────┐
    │ PlanExecutor         │  ← concurrent.futures.ThreadPoolExecutor
    └────────┬────────────┘
             ↓
输出: List[ToolResult]  (每个工具的结果/错误, 带 status)
```

### 2.2 静态依赖规则表

```python
TOOL_CATEGORIES = {
    "readonly":  ["read_file", "check_process", "list_directory", "list_skills", "read_skill"],
    "search":    ["web_search", "wikipedia_search", "wikipedia_summary"],
    "fetch":     ["web_fetch"],
    "write":     ["edit_file", "write_file"],
    "execute":   ["exec_command"],
}

# 只读类别集合（这些类别之间也可以并行）
READONLY_CATEGORIES = {"readonly", "search", "fetch"}
```

**规则**:
1. 同类只读工具（同一类别且属于 READONLY_CATEGORIES）→ 并行
2. **不同只读类别之间** → 也可并行（如 read_file + web_search 同时执行）
3. `write` 类工具之间 → 串行（避免写冲突）
4. `write` → `execute` → 必须串行（写完再跑）
5. 非只读类别工具 → 按 LLM 输出顺序串行
6. 未分类工具 → 按 LLM 输出顺序串行

### 2.3 执行流程示例

输入: `tool_calls = [read_file(A), read_file(B), edit_file(A), exec_command(test), check_process()]`

依赖分析结果:
- Stage 0: `[read_file(A), read_file(B)]` — 并行（同类 readonly）
- Stage 1: `[edit_file(A)]` — 串行（write 类，不可与上面并行）
- Stage 2: `[exec_command(test)]` — 串行（execute 类，依赖 stage 1）
- Stage 3: `[check_process()]` — 串行（readonly，但位于 execute 之后）

### 2.4 异步/同步桥接

smolagents 的 `process_tool_calls()` 是同步生成器。编排层的并行执行使用 `concurrent.futures.ThreadPoolExecutor`（而非 asyncio），因为：
- smolagents 的工具函数都是同步的
- 避免在同步代码中引入 asyncio 事件循环的复杂性
- ThreadPoolExecutor 天然适合 I/O 密集型并行（web_fetch、read_file 等）

复合工具 `search_and_fetch` 内部也使用 ThreadPoolExecutor 并发抓取。

### 2.5 与 SmartToolCallingAgent 的集成

在 `backend/agent/agent_runner.py` 中覆写 `process_tool_calls()`：

```python
class SmartToolCallingAgent(ToolCallingAgent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.orchestrator = ToolOrchestrator(max_parallel=5, global_timeout=60)

    def process_tool_calls(self, chat_message, memory_step):
        tool_calls = chat_message.tool_calls
        if len(tool_calls) <= 1:
            yield from super().process_tool_calls(chat_message, memory_step)
        else:
            yield from self.orchestrator.execute_orchestrated(
                tool_calls, self.tools, memory_step, self._event_callback
            )
```

## 3. 复合工具设计

### 3.1 search_and_fetch

新文件: `backend/tools/search_and_fetch.py`

```python
def search_and_fetch(query: str, max_results: int = 5, timeout: int = 15, check_abort=None) -> str:
```

内部流程:
1. 调用 `web_search(query)` 获取搜索结果列表
2. 用 `ThreadPoolExecutor` 并发抓取所有结果 URL
3. 用 `concurrent.futures.wait(timeout=timeout)` 实现全局超时
4. 超时后取消未完成任务，返回已有结果

返回格式:
```
=== 搜索结果: "{query}" ===
共找到 {n} 个结果，成功抓取 {m} 个，超时 {t} 个，失败 {f} 个

--- [1] {title} ---
URL: {url}
状态: 成功 / 超时 / 失败({error})
内容:
{truncated_text_content}

=== 搜索结束 ===
```

### 3.2 wikipedia_search_and_fetch

新文件: `backend/tools/wikipedia_search_and_fetch.py`

类似 search_and_fetch，但:
- 搜索阶段调用 `wikipedia_search_impl(query)`
- 抓取阶段并发调用 `wikipedia_summary_impl(title)` 对每个搜索结果
- 返回格式相同

## 4. 前端编排视图

### 4.1 新增 SSE 事件类型

在现有事件类型基础上新增:

| 事件类型 | 用途 |
|---------|------|
| `orchestration_plan` | 编排计划（stages + 工具列表） |
| `orchestration_update` | 单个工具状态变更（running/completed/failed/skipped/timeout） |
| `orchestration_done` | 编排完成（汇总信息） |

### 4.2 事件数据结构

```typescript
// orchestration_plan
{
  type: "orchestration_plan",
  stages: [
    { stage_id: 0, parallel: true, tools: [
      { call_id: "tc_0", name: "read_file", args: {...}, status: "pending" },
      { call_id: "tc_1", name: "read_file", args: {...}, status: "pending" }
    ]},
    { stage_id: 1, parallel: false, tools: [
      { call_id: "tc_2", name: "edit_file", args: {...}, status: "pending" }
    ]}
  ]
}

// orchestration_update
{
  type: "orchestration_update",
  call_id: "tc_0",
  status: "running" | "completed" | "failed" | "skipped" | "timeout",
  result_preview?: string,
  duration_ms?: number
}

// orchestration_done
{
  type: "orchestration_done",
  total: 5, completed: 4, failed: 1, skipped: 0, timeout: 0,
  duration_ms: 3200
}
```

### 4.3 AgentStep 类型扩展

在 `frontend/src/lib/api.ts` 的 `AgentStep` interface 中：
```typescript
export interface AgentStep {
  step_type: 'thought' | 'tool_call' | 'observation' | 'orchestration';
  content?: string;
  name?: string;
  input?: string | Record<string, unknown>;
  partial?: boolean;
  // 新增编排字段
  stages?: OrchestrationStage[];
  orchestration_update?: OrchestrationUpdate;
  orchestration_summary?: OrchestrationSummary;
}
```

### 4.4 AgentStepDisplay 扩展

在 `frontend/src/components/AgentStepDisplay.tsx` 中新增 `OrchestrationView` 子组件：
- 并行工具横向排列 (flex-row)，串行 stages 纵向排列
- 每个工具卡片: 工具名、关键参数、状态图标、耗时
- 可折叠/展开查看详细结果
- 单个工具调用时不显示编排视图，走原有渲染逻辑

## 5. 提示词与 local_llm_model.py 适配

### 5.1 提示词变更 (config/config-agent-prompts.yaml)

**移除** (line 96):
```
2. 一次动作只能调用一个工具；如调用多个工具，须分多步进行，每步调用一个工具并观察结果。
```

**替换为**:
```
2. 你可以在一次响应中调用多个工具。系统会自动分析依赖关系并优化执行顺序（独立工具并行、有依赖的工具串行）。你只需按逻辑顺序列出所有工具调用。
```

**移除** (line 313 类似限制):
```
注意：一次只能调用一个工具；如需调用多个工具，分多步进行，每步调用一个工具并观察结果。
```

**替换为**:
```
注意：你可以在一次响应中调用多个工具，系统会自动编排执行。
```

**新增复合工具说明** (在工具描述区域):
```
- search_and_fetch: 搜索互联网并自动并发抓取所有搜索结果页面内容。优先使用此工具而非分步调用 web_search + web_fetch。
- wikipedia_search_and_fetch: 搜索维基百科并自动获取所有匹配条目的摘要内容。优先使用此工具而非分步调用 wikipedia_search + wikipedia_summary。
- 网络工具支持 timeout 参数（默认15秒），超时后会返回已获取的部分结果。
```

### 5.2 local_llm_model.py 适配

1. **流式模式保持不变**: `tools=None` 绕过 SGLang parser，文本解析 Format 1 已支持多匹配
2. **`_extract_tool_calls()` final_answer 处理调整**: 当 LLM 输出 `[tool_a, tool_b, final_answer]` 时，保留 `[tool_a, tool_b]` 正常执行

## 6. 内存/消息管理

### 6.1 ActionStep 多 tool_calls

编排模式下，一个 ActionStep 携带多个 tool_calls。smolagents 的 `ActionStep.to_messages()` 会为每个 tool_call 生成一对 `TOOL_CALL(ASSISTANT) → TOOL_RESPONSE(USER)` 消息，保持角色交替，不会触发 `get_clean_message_list()` 的合并问题。

### 6.2 关键约束（复用现有保护机制）

- `step.tool_calls` 必须保留，不能设为 None（AGENTS.md 陷阱 #8）
- `write_memory_to_messages()` 中只 strip `observations_images`，保留 `tool_calls`

## 7. 错误处理

### 7.1 Stage 内部（并行组）
- 每个工具独立 try/catch，失败记录到 observation
- 其他并行工具不受影响

### 7.2 Stage 之间
- 某 stage 工具失败后，检查后续 stage 中依赖该工具的工具
- 有依赖的标记为 `skipped`，无依赖的正常执行

### 7.3 全局超时
- 编排执行硬编码 60s 全局超时

### 7.4 边界情况

| 情况 | 处理方式 |
|------|---------|
| LLM 只输出 1 个 tool_call | 不走编排，走原有 `super().process_tool_calls()` |
| LLM 输出 tool_calls + final_answer | 执行 real tools，完毕后处理 final_answer |
| LLM 输出纯文本 | 走现有纯文本 → final_answer 逻辑 |
| 编排中所有工具都失败 | 整理错误信息返回给 LLM |
| search_and_fetch 内部部分超时 | 返回已获取的部分结果 |
| 用户取消 (/stop) | 取消所有执行中的工具 |
| 同一文件并行读写 | 静态规则保证 write 串行 |

## 8. 文件变更清单

| 文件 | 变更类型 | 内容 |
|------|---------|------|
| `backend/agent/orchestrator.py` | 新增 | ToolOrchestrator 核心模块（DependencyAnalyzer + ExecutionPlan + PlanExecutor） |
| `backend/agent/agent_runner.py` | 修改 | 新增 `__init__`、覆写 `process_tool_calls()`、注册新工具、新增编排 SSE 事件 |
| `backend/agent/local_llm_model.py` | 修改 | `_extract_tool_calls()` 调整 final_answer 过滤逻辑 |
| `backend/tools/search_and_fetch.py` | 新增 | search_and_fetch 复合工具 |
| `backend/tools/wikipedia_search_and_fetch.py` | 新增 | wikipedia_search_and_fetch 复合工具 |
| `config/config-agent-prompts.yaml` | 修改 | 放开多工具限制、新增复合工具说明 |
| `frontend/src/lib/api.ts` | 修改 | 扩展 AgentStep 类型、新增编排类型定义 |
| `frontend/src/components/AgentStepDisplay.tsx` | 修改 | 新增 OrchestrationView 子组件 |
| `frontend/src/app/page.tsx` | 修改 | SSE 事件处理适配编排事件 |
| `frontend/src/app/vision/page.tsx` | 修改 | SSE 事件处理适配编排事件 |
| `tests/test_orchestrator.py` | 新增 | ToolOrchestrator 单元测试 |
| `tests/test_search_and_fetch.py` | 新增 | 复合工具测试 |
| `AGENTS.md` | 修改 | 更新架构文档 |

## 9. 风险与缓解

| 风险 | 缓解措施 |
|------|---------|
| Qwen3 输出多 tool_calls 的质量不稳定 | 单个 tool_call 走原有逻辑；渐进放开 |
| 静态规则覆盖不了所有依赖场景 | 未分类工具默认串行；后续可扩展 |
| 并行执行增加系统资源消耗 | max_parallel=5 限制并发数 |
| smolagents 升级可能破坏覆写 | 锁定版本；覆写方法有注释 |
