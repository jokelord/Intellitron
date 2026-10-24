# Manager Agent Parallel Dispatch Design

## Goal

新增一个与 `coordinator_2` 平级的 `manager_agent` profile，用于大型并发任务的管理、拆解、分发与汇总。

`manager_agent` 面向本项目的 local agentic assistant 场景，而不是 coding-agent platform。典型任务包括：并行读取新闻、整理资料、处理多个本地文件、让多个执行 agent 分头完成不同子任务，然后由 manager 综合结果。系统不引入 `.worktrees`、远程 worker、嵌套 manager 或多层管理树。

本设计的核心目标：

- `manager_agent` 是一个能使用工具的管理者，不是 coordinator，也不是只会路由的 router。
- `manager_agent` 拥有只读工具与专属工具 `dispatching_parallel_agents`。
- `dispatching_parallel_agents` 可以创建一个或多个新的 child `basic_agent` session。
- 每个 child `basic_agent` 使用新的 context window，不 fork manager 的历史上下文。
- child `basic_agent` 可以在同一个主 workspace 工作，但同一个 manager turn 内不能有多个 child 写同一个文件。
- 前端 manager 对话中必须展示 parallel dispatch UI card，并提供 child agent 链接。
- 用户点击 child 链接时，在新浏览器标签页打开 child 对话页面，先 replay 已有事件，再继续 live stream；用户不点击时 child 仍然后台运行，不阻塞。

## Background

当前项目已有多 agent profile 的基础结构：

- `coordinator_1`：极速路由 profile，默认可路由到 `coordinator_2` 或 `basic_agent`。
- `coordinator_2`：复杂推理/聊天 profile，可通过 managed-agent proxy handoff 到 `basic_agent`。
- `basic_agent`：通用执行 profile，拥有读写文件、搜索、执行命令、浏览器等工具能力。
- 前端 text 与 vision 页面各自维护 agent SSE 逻辑，必须同步修改。
- 统一 timeline/projection 是 future agents 的接入约束，不能为新 agent 发明完全独立的前端协议。

强制参考资料的设计要点：

- `dispatching-parallel-agents` skill 强调：每个并发 worker 必须有聚焦、独立、自包含的任务说明；多个独立任务应一次性并发 dispatch；manager 负责整合结果。
- Claude Code sub-agent architecture 强调：subagent 默认是全新上下文；manager/coordinator 必须自己做 synthesis，不能把理解责任甩给 worker；worker 结果应以结构化通知回灌；不要预测 worker 结果。

## Product Decisions

1. `manager_agent` 为独立 profile，与 `coordinator_2` 平级。

2. 默认配置将 `agent.entry_agent_name` 设置为 `manager_agent`，方便端到端测试。

3. `enabled_agent_profiles` 包含 `coordinator_1`、`coordinator_2`、`manager_agent`、`basic_agent`。

4. `coordinator_1` 后续可以路由到 `manager_agent`，但本设计不要求用户必须启用 coordinator 路由。

5. `manager_agent` 可以调用只读工具进行初步信息收集，也可以调用 `dispatching_parallel_agents` 分发子任务。

6. `manager_agent` 不直接拥有 `write_file`、`edit_file`、`exec_command`、`browser_command` 等执行/写入工具。

7. `manager_agent` 不能 dispatch 其他 `manager_agent`。所有 child 固定为 `basic_agent`。

8. child `basic_agent` 不继承 manager context，不使用 fork context，不共享 manager memory.steps。

9. child `basic_agent` 的初始 user message 是 manager 写出的英文任务 briefing。

10. child `basic_agent` 在同一主 workspace 运行，不创建 `.worktrees`。

11. 同一个 manager turn 中，多个 child 可以写不同文件，但不能写同一个文件。

12. child tab 采用 replay-then-live 体验：打开时先重放 child 已有 transcript/events，再继续订阅 live stream。

## Non-Goals

- 不引入 `.worktrees` 或临时 git worktree 隔离。
- 不支持 manager 嵌套 manager。
- 不实现远程 agent、teammate swarm、MCP 专属 worker 或多进程 worker platform。
- 不把 child 的完整工具调用 transcript 注入 manager LLM prompt。
- 不把 frontend bubbles、raw timeline events、UI 状态或 dispatch card 文本喂给 LLM。
- 不要求用户打开 child tab 后 child 才开始执行。
- 不在第一版中实现跨浏览器重启后的持久 transcript 恢复；第一版以内存 session/event buffer 为准。

## Agent Responsibilities

### `manager_agent`

`manager_agent` 是管理者。它负责：

- 消化用户任务意图。
- 必要时用只读工具收集粗略信息。
- 判断任务是否值得并发拆分。
- 将大任务拆成一个或多个明确子任务。
- 为每个 child 写出准确、详尽、全英文的 task briefing。
- 为写入型任务声明文件归属，避免多个 child 在同一 turn 写同一个文件。
- 等待 shared blackboard 返回所有 child final reports。
- 自己阅读、理解、整合 child 汇报。
- 决定是否需要继续 dispatch、自己用只读工具补充信息，或最终回复用户。

`manager_agent` 不负责：

- 直接写文件。
- 直接执行 shell 命令。
- 直接操作浏览器。
- 指挥另一个 manager。
- 在 child 完成前预测 child 结果。
- 把“理解、综合、决策”责任转嫁给 child。

### Child `basic_agent`

child `basic_agent` 是执行者。它负责：

- 接收 manager 的英文 briefing。
- 在新 context window 中独立执行任务。
- 使用常规 `basic_agent` 工具提示词与完整工具集。
- 按 manager 要求完成搜索、文件处理、代码/文本写入、命令执行或其他操作。
- 最终给出面向 manager 的 final report。

child `basic_agent` 不负责：

- 再 dispatch manager。
- 假设自己看过 manager 的历史上下文。
- 修改未分配给自己的文件。
- 与同一 dispatch 中其他 child 协商文件冲突。

## Tool Permissions

### `manager_agent` allowlist

`manager_agent` 只允许以下工具：

- `read_file`
- `search_and_fetch`
- `web_fetch`
- `grep`
- `glob`
- `list_skills`
- `read_skill`
- `dispatching_parallel_agents`

第一版不建议给 `manager_agent` `web_search`，因为项目 prompt 已鼓励优先使用 `search_and_fetch`。如果后续确实需要单纯搜索 URL 列表，可单独配置化加入。

### `basic_agent` child tools

child `basic_agent` 使用常规 `basic_agent` 的完整工具集与提示词，不做额外裁剪。

### Hidden tool constraints

工具隐藏必须同时切断两条路径：

- API tool schema：`tools_to_call_from` 不包含 manager 禁止工具。
- system prompt 工具列表：渲染 prompt 前过滤 `self.tools`。

现有 `SmartToolCallingAgent` 已有隐藏 `final_answer` 的经验，新增 manager 工具权限时必须沿用同等严格度。

## `dispatching_parallel_agents` Tool Contract

### Tool name

`dispatching_parallel_agents`

### Visibility

仅 `manager_agent` 可见。

### Input schema

```json
{
  "subtasks": [
    {
      "name": "news-reading-agent",
      "task": "Read today's AI industry news from the assigned sources and produce a concise report...",
      "may_write": true,
      "reserved_files": ["/home/zk/Documents/news/ai-news.md"]
    }
  ],
  "max_parallel": 3
}
```

Fields:

- `subtasks`: required array, length `1..manager_agent_max_child_agents_per_dispatch`.
- `subtasks[].name`: required string, human-readable child name.
- `subtasks[].task`: required string, full English task briefing.
- `subtasks[].may_write`: optional boolean, default `false`. Must be `true` if the child is expected to create, edit or overwrite files.
- `subtasks[].reserved_files`: required when `may_write=true`; otherwise optional and normally empty. Contains absolute file paths or workspace-relative file paths the child may write in this dispatch.
- `max_parallel`: optional positive integer, capped by config.

### Child name validation

`name` must be convenient for UI and logs:

- Lowercase kebab-case is recommended, e.g. `news-reading-agent`.
- Spaces may be accepted and normalized for display.
- Must not be empty.
- Must not look like a UUID or opaque session id.
- Must be unique within one dispatch call after normalization.
- Must be length-limited, e.g. `3..64` characters.

The backend still uses generated `session_id` for actual isolation. The child name is display metadata, not identity authority.

### Task briefing validation

`task` should be English and self-contained. The backend should not rely on perfect language detection, but the manager prompt must require English. Basic validation should reject empty or extremely short briefings.

A good briefing includes:

- Goal.
- Context known so far.
- Exact scope.
- Tools or sources to prefer, when relevant.
- Files the child may write, when relevant.
- Files the child must not touch, when relevant.
- Expected final report format.
- Whether the child should be concise or exhaustive.

### Reserved file validation

Within one `dispatching_parallel_agents` call:

- If a subtask may write files, `may_write` must be `true` and `reserved_files` must be non-empty.
- If `may_write=false`, the manager's child briefing must explicitly tell that child not to write files.
- Normalize all `reserved_files`.
- Reject duplicate file ownership across subtasks.
- Reject paths outside allowed workspace roots if existing file tools already enforce that boundary.
- If two child tasks need the same file, manager must split the workflow sequentially: one child writes first, then manager may dispatch a later child to verify or continue.

First version does not need to intercept every actual `write_file`/`edit_file` call at runtime. The hard contract is dispatch-time ownership declaration plus prompt enforcement. Runtime file-tool interception can be added later if conflicts appear in practice.

### Output schema to manager LLM

The tool returns a single blackboard observation only after all child tasks reach terminal state:

```text
Parallel child agents completed.

Child: news-reading-agent
Status: completed
Assigned task:
<exact manager English briefing>

Final report:
<child final answer exactly as produced>

---

Child: file-cleanup-agent
Status: failed
Assigned task:
...

Error:
...
```

Rules:

- The blackboard waits for all children in the dispatch call.
- The blackboard does not compress child final reports.
- The blackboard may add structure and separators.
- The blackboard must not include child raw tool traces unless a child included them in its final answer.
- The blackboard must include failed/aborted child terminal status.
- The blackboard must not include `session_id`, child URLs, frontend state or timeline metadata. Those are UI/runtime diagnostics, not manager LLM prompt content.

## Runtime Architecture

### Session model

Add manager-child metadata alongside existing `AgentSession` storage.

Conceptual data structures:

```python
@dataclass
class ChildAgentRun:
    child_run_id: str
    manager_session_id: str
    dispatch_id: str
    child_session_id: str
    child_name: str
    profile_name: str  # always "basic_agent" in v1
    assigned_task: str
    may_write: bool
    reserved_files: list[str]
    status: Literal["pending", "running", "completed", "failed", "aborted"]
    final_report: str | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
```

Each child session:

- Is created with `current_agent_name="basic_agent"`.
- Has its own `AgentSession` object.
- Has its own `memory.steps`.
- Has its own run scope id.
- Does not inherit manager `memory.steps`.
- Does not inherit manager `state`.
- May share global config, model, tool definitions and workspace.

### Dispatch execution flow

1. `manager_agent` calls `dispatching_parallel_agents` with one or more subtasks.
2. Backend validates input.
3. Backend creates a `manager_dispatch_*` timeline event for the manager session.
4. For each subtask, backend creates a child `AgentSession` with profile `basic_agent`.
5. Backend starts each child run in the background, subject to `max_parallel` and config caps.
6. Backend records child run metadata and emits child-created/started events to the manager session.
7. Each child run streams through the same event conversion path as normal agent mode, but events are also appended to a per-child event buffer.
8. If a user opens a child tab, the child tab subscribes to that buffer and live stream.
9. The dispatch tool awaits all child terminal states.
10. Backend assembles blackboard observation from child final reports.
11. Tool returns blackboard observation to `manager_agent`.
12. `manager_agent` decides next action or final answer.

### Non-blocking child tabs

Child runs must not depend on UI subscribers.

- If the user never clicks a child link, the child still runs to completion.
- If the user clicks while child is running, the page replays previous events and then receives live events.
- If the user clicks after child completion, the page replays the full transcript and shows terminal state.
- Closing the child tab does not stop the child run.
- Stopping the manager run should stop active child runs in that dispatch.

### Event buffer

Maintain an in-memory append-only event buffer per child run/session.

Buffer contents:

- `session_start`
- manager assigned task event
- `agent_step` thought/tool_call/observation events
- `orchestration_*` events
- `content` deltas
- `timeline_event` events relevant to that child session
- `done`, `error`, or `aborted`

The buffer should preserve order. Each event should have a monotonic sequence number for replay boundary handling.

First version may keep buffers in process memory with session TTL cleanup. Persistent transcript storage is a future extension.

### Child subscription endpoint

Add this endpoint for child conversation tabs:

`GET /v1/agent/child/stream?child_run_id=...`

Required behavior:

- Validate that the requested `child_run_id` exists.
- Replay buffered events in order.
- If child is still active, keep connection open and stream new events.
- If child is terminal, close after replay with `done`.
- Do not create a new run from this endpoint.

The URL exposed to the frontend must use `child_run_id`. `session_id` remains backend operational identity and must not be required in browser links.

## Frontend Design

### Manager dispatch card

In the main manager conversation, show a parallel dispatch card.

Card content:

- Dispatch title, e.g. `Parallel agents`.
- Manager-generated child names.
- Child status badges: `pending`, `running`, `completed`, `failed`, `aborted`.
- Manager's English instruction preview for each child.
- Link/button for each child.
- Completion count, e.g. `2 / 3 completed`.

Click behavior:

- Opens a new browser tab.
- URL includes `child_run_id`.
- Does not interrupt manager run.
- Does not start child run; child run already started by backend.

### Child conversation page

Add a route for child agent conversations:

- `/agent/child/[childRunId]`

The child page must display:

- Child display name.
- Parent manager session id or link back if available.
- Manager assigned English task as the first visible user/command block.
- The child `basic_agent` stream exactly like a normal agent conversation:
  - thoughts
  - tool calls
  - observations
  - orchestration events
  - final answer content
  - terminal state

The child page should be read-only in v1. It is a live transcript viewer, not a second user input surface. Allowing the user to continue the child conversation is a future feature and would require explicit session ownership and conflict rules.

### Text and vision page sync

Both pages must support manager dispatch projection:

- `frontend/src/app/page.tsx`
- `frontend/src/app/vision/page.tsx`

Shared pieces should be placed in reusable frontend modules where practical, but each page currently owns its own state glue. Both must be updated consistently.

### Timeline projection

Extend `TimelineEvent` and `TimelineProjection` to represent parallel dispatch.

Possible events:

```ts
type ManagerChildStatus = 'pending' | 'running' | 'completed' | 'failed' | 'aborted';

interface ManagerChildAgentEvent {
  kind: 'manager_child_agent_update';
  source: 'manager_agent';
  dispatch_id: string;
  child_run_id: string;
  child_session_id?: string;
  child_name: string;
  status: ManagerChildStatus;
  assigned_task?: string;
  href?: string;
  final_preview?: string;
  error?: string;
}
```

Projection should update a single dispatch card rather than append a new card per event.

Add a dedicated manager projection type instead of overloading existing `dispatch_kind` values. `routing` and `secondary_handoff` behavior should remain unchanged.

## Backend SSE and Timeline Events

Manager session should emit timeline events rather than a one-off custom frontend protocol.

Recommended event kinds:

- `manager_dispatch_started`
- `manager_child_agent_update`
- `manager_dispatch_completed`
- `manager_dispatch_failed`

Example:

```json
{
  "kind": "manager_child_agent_update",
  "source": "manager_agent",
  "dispatch_id": "manager-dispatch-...",
  "child_run_id": "child-news-reading-agent-...",
  "child_name": "news-reading-agent",
  "status": "running",
  "assigned_task": "Read today's AI industry news...",
  "href": "/agent/child/child-news-reading-agent-..."
}
```

Child page SSE can reuse normal agent event payloads instead of manager timeline projection. This keeps the child page visually equivalent to a normal `basic_agent` run.

## Prompt Design

### `manager_agent` instructions

The manager prompt must include these rules in English:

- You are `manager_agent`, a manager for large parallel tasks.
- Your job is to understand the user's goal, optionally gather broad read-only context, plan strategy, dispatch one or more `basic_agent` children, and synthesize their reports.
- You can use read-only tools yourself, but you do not perform write/execute/browser actions directly.
- Use `dispatching_parallel_agents` when independent subtasks can proceed in parallel or when a fresh context window helps execution.
- You may dispatch only `basic_agent` children. Never dispatch or ask for another manager.
- Each child receives a brand-new context window. Brief it as a smart colleague who has not seen the current conversation.
- All child task briefings must be in English, precise, detailed, and self-contained.
- For write tasks in one dispatch, set `may_write=true` and assign exclusive `reserved_files`. Do not assign the same file to multiple children in the same turn.
- For read-only tasks, set `may_write=false` and explicitly tell the child not to create, edit or overwrite files.
- Do not over-delegate simple tasks.
- Do not predict child results. Wait for the blackboard observation.
- After child reports arrive, you must read and understand them yourself before deciding next steps.
- Never write phrases like "based on your findings, fix the issue" as a substitute for your own synthesis.

### Child briefing output format recommendation

Manager should ask each child to return:

```text
Scope: <what you were assigned>
Result: <what you completed or found>
Files changed: <paths, if any>
Key observations: <important evidence>
Issues: <blockers or risks>
```

This is guidance, not a parser dependency.

## Configuration

Add/modify config under `agent`:

```yaml
agent:
  entry_agent_name: manager_agent
  enabled_agent_profiles: [coordinator_1, coordinator_2, manager_agent, basic_agent]
  manager_agent_max_child_agents_per_dispatch: 6
  manager_agent_default_max_parallel_children: 3
  manager_agent_max_parallel_children: 4
  manager_agent_child_event_buffer_limit: 5000
```

Config semantics:

- `manager_agent_max_child_agents_per_dispatch`: hard cap on subtask count in one tool call.
- `manager_agent_default_max_parallel_children`: default concurrency when tool call omits `max_parallel`.
- `manager_agent_max_parallel_children`: hard cap for concurrent child runs.
- `manager_agent_child_event_buffer_limit`: max buffered SSE events per child before applying safe truncation policy.

Buffer truncation policy must preserve terminal status and final answer. If old events are dropped, the child page should show a visible note that earlier events were truncated.

## Error Handling

### Tool validation errors

Return a normal tool observation to `manager_agent` when:

- `subtasks` is empty.
- Too many subtasks are requested.
- Child names are duplicate or invalid.
- `task` is missing or too short.
- `reserved_files` conflict within one dispatch.
- `max_parallel` is invalid.

The manager can then revise and call the tool again.

### Child failure

If one child fails:

- Mark that child `failed`.
- Preserve error text.
- Continue waiting for other children unless manager/session abort is requested.
- Return blackboard observation containing both successes and failures.

### Abort/stop

If the user stops the manager run:

- Abort active child runs launched by the current dispatch.
- Mark children `aborted` where applicable.
- Preserve partial child transcript buffers.
- Do not release run scope before child worker cleanup completes.

This must respect the existing stop-safety lessons: SSE generator close must not prematurely mark a session reusable while worker threads are still active.

### Child tab disconnect

If a child tab disconnects:

- Do not abort the child run.
- Keep buffering until terminal state or TTL cleanup.

## Prefix Cache and Prompt Stability

This feature must obey the existing prefix-cache constraints:

- Do not inject frontend UI state into LLM prompt bodies.
- Do not inject raw manager timeline events into child prompts.
- Do not inject child raw tool traces into manager prompts.
- Child input is its own `basic_agent` system/runtime prompt plus its own `memory.steps`.
- Manager receives only the `dispatching_parallel_agents` tool observation containing child final reports.
- `session_id` must not be used as meaningful prompt context.
- Runtime context remains pinned per session.

Because child agents use fresh contexts, this design prioritizes execution isolation and UI observability over fork-style prompt cache reuse.

## Implementation Areas

Expected files/modules:

- `config/config-agent-prompts.yaml`: add `manager_agent` prompt bundle.
- `config/config-misc.yaml`: default `entry_agent_name: manager_agent`; add manager caps.
- `backend/config.py`: model new manager config fields.
- `backend/agent/agent_runner.py`: add profile allowlist, build manager tool, child session creation, child event buffer, child subscription support.
- `backend/api/agent.py`: expose child transcript/stream endpoint if routing belongs at API layer.
- `frontend/src/lib/api.ts`: add manager child event types and child stream client.
- `frontend/src/lib/timeline-projection.ts`: project manager dispatch events.
- `frontend/src/components/*`: add parallel child dispatch card or extend existing dispatch rendering.
- `frontend/src/app/page.tsx`: integrate manager dispatch card and child links.
- `frontend/src/app/vision/page.tsx`: keep vision page in sync.
- New frontend child route: implement child transcript viewer.
- Tests under `tests/` and frontend test files for projection behavior.

## Testing Plan

Backend tests:

- `manager_agent` profile is enabled and default entry profile can be `manager_agent`.
- `manager_agent` tool allowlist contains only allowed read-only tools plus `dispatching_parallel_agents`.
- `manager_agent` prompt/tool schema does not expose write/execute/browser tools.
- `dispatching_parallel_agents` accepts one child and creates a child `basic_agent` session.
- Multiple children get distinct `session_id`, distinct `memory.steps`, and distinct child metadata.
- Child sessions do not inherit manager memory.
- Duplicate child names are rejected.
- `may_write=true` without `reserved_files` is rejected.
- Duplicate `reserved_files` across subtasks are rejected.
- `max_parallel` is capped by config.
- Blackboard waits for all child terminal states before returning.
- Child failure is included in blackboard without cancelling successful siblings.
- Manager abort stops active child runs and preserves partial buffers.
- Child event buffer replays events in order and then streams live events.
- Completed child replay returns transcript and terminal `done` without starting a new run.

Frontend tests:

- `TimelineEvent` projection handles manager child status updates.
- Parallel dispatch card merges updates by `dispatch_id` and `child_run_id`.
- Child link opens a route with `child_run_id`.
- Text and vision pages both render manager dispatch updates.
- Child page renders assigned task, tool calls, observations, streamed content and terminal status from replay/live stream.

Manual E2E test:

1. Start backend/frontend with `entry_agent_name: manager_agent`.
2. Ask for a parallel local assistant task, e.g. reading two independent news topics and saving each to different files.
3. Verify manager creates a parallel dispatch card.
4. Click one child link while it is still running.
5. Verify new tab replays the manager command and continues live streaming child `basic_agent` tool activity.
6. Do not click another child link until completion.
7. Open it after completion and verify full replay is shown.
8. Verify manager waits for all child reports before synthesizing final answer.
9. Verify no `.worktrees` directory is created.
10. Verify two children were not assigned the same writable file in one dispatch.

## Acceptance Criteria

The feature is complete when:

- Default agent mode starts at `manager_agent` for E2E testing.
- `manager_agent` can call read-only tools and `dispatching_parallel_agents`.
- `manager_agent` cannot see or call write/execute/browser tools.
- `dispatching_parallel_agents` can run one or more child `basic_agent` sessions without forked context.
- Child runs proceed without requiring the user to open child tabs.
- Main UI shows a manager parallel dispatch card with child links and statuses.
- Child tab replays old events and continues live streaming if the child is active.
- Child tab shows manager's English instruction and normal `basic_agent` tool/output stream.
- Blackboard returns all child final reports to manager only after all children complete/fail/abort.
- Manager final response is based on its own synthesis of child reports.
- Same-turn duplicate writable file assignment is rejected before child launch.
- Writable child tasks must declare `may_write=true` and exclusive `reserved_files` before launch.
- Text and vision pages remain behaviorally consistent.

## Open Implementation Notes

- Prefer `child_run_id` as the browser-facing identifier and keep `session_id` as backend operational identity.
- Use in-memory transcript buffers in v1; persistent child transcript storage can be added later.
- Keep child page read-only in v1 to avoid multi-tab write/session ownership ambiguity.
- If future runtime file conflict enforcement is needed, add per-run file ownership checks to `write_file` and `edit_file` wrappers using current run scope context.
- If coordinator routing is re-enabled, update `coordinator_1` route choices to include `manager_agent` for large parallel tasks.
