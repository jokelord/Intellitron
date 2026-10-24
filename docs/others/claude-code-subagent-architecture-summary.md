# Claude Code Sub-Agent / 任务调度架构精华总结

> 源码位置：`ref_src/Cld_Code/`
> 目的：提炼 Claude Code 子 agent 系统的架构设计精华，供本项目自研 agent 系统借鉴。
> 所有行号引用均指向 `ref_src/Cld_Code/` 下的源文件。

---

## 目录

1. [总体架构概览](#1-总体架构概览)
2. [Task 统一抽象层](#2-task-统一抽象层)
3. [Agent 定义体系（AgentDefinition）](#3-agent-定义体系agentdefinition)
4. [内置 Agent 设计](#4-内置-agent-设计)
5. [Agent 执行核心（runAgent）](#5-agent-执行核心runagent)
6. [AgentTool 调度入口](#6-agenttool-调度入口)
7. [Fork Subagent（上下文继承 fork）](#7-fork-subagent上下文继承-fork)
8. [Coordinator 编排模式](#8-coordinator-编排模式)
9. [异步 Agent 生命周期管理](#9-异步-agent-生命周期管理)
10. [Agent 恢复（Resume）](#10-agent-恢复resume)
11. [Agent 持久化记忆](#11-agent-持久化记忆)
12. [工具解析与权限控制](#12-工具解析与权限控制)
13. [Prompt 工程设计](#13-prompt-工程设计)
14. [Prompt Cache 优化策略](#14-prompt-cache-优化策略)
15. [对本项目的借鉴建议](#15-对本项目的借鉴建议)

---

## 1. 总体架构概览

Claude Code 的子 agent 系统是一个**多层级、可编排、可恢复**的自主任务执行框架，核心设计层次：

```
AgentTool.tsx        ← 调度入口（同步/异步/fork/worktree 路由）
  └─ runAgent.ts      ← 执行核心（上下文构建、工具解析、query 循环、转录记录）
       └─ query.ts    ← LLM 主循环（工具调用、流式输出）
  └─ agentToolUtils.ts ← 生命周期驱动（进度跟踪、通知、安全分类）
  └─ forkSubagent.ts   ← Fork 路径（上下文继承 + cache 共享）
  └─ resumeAgent.ts    ← 恢复路径（磁盘转录恢复）
  └─ loadAgentsDir.ts  ← Agent 定义加载（Markdown/JSON → AgentDefinition）
  └─ Task.ts           ← 统一 Task 抽象（kill 多态分发）
```

**核心设计哲学**：
- **统一消息模型**：所有 agent 交互通过 message 流传递，agent 结果以 `<task-notification>` XML 作为 user-role 消息回灌。
- **Prompt cache 优先**：大量设计决策围绕"保持 API 请求前缀字节一致"以最大化 cache 命中。
- **隔离与继承的平衡**：fork（继承上下文）vs subagent（全新上下文）vs worktree（文件隔离）三种模式覆盖不同场景。
- **安全分级**：权限模式、工具过滤、handoff 分类器多层防护。

---

## 2. Task 统一抽象层

**文件**：`Task.ts`

### 2.1 统一 TaskType 枚举

`Task.ts:6-13` 定义了 7 种任务类型，所有异步执行单元共用同一套状态机：

```typescript
export type TaskType =
  | 'local_bash'      // 后台 shell
  | 'local_agent'     // 本地子 agent
  | 'remote_agent'    // 远程 CCR agent
  | 'in_process_teammate' // 进程内队友（swarm）
  | 'local_workflow'  // 工作流脚本
  | 'monitor_mcp'     // MCP 监控
  | 'dream'           // 后台摘要
```

**优点**：将 shell 任务、agent 任务、远程任务、队友任务统一在同一调度框架下，`tasks.ts:22-32` 的 `getAllTasks()` 返回所有 Task 实现，`getTaskByType()` 做多态分发。

### 2.2 终态守卫

`Task.ts:27-29`：

```typescript
export function isTerminalTaskStatus(status: TaskStatus): boolean {
  return status === 'completed' || status === 'failed' || status === 'killed'
}
```

**优点**：防止向已死的 teammate 注入消息、防止重复清理已完成的任务。所有任务状态转换都需要先检查终态。

### 2.3 安全的 Task ID 生成

`Task.ts:79-106`：

- 类型前缀（`b`/`a`/`r`/`t`/`w`/`m`/`d`）便于人眼识别和日志排查。
- 36^8 ≈ 2.8 万亿组合，使用 `crypto.randomBytes` 生成，**抵御符号链接暴力攻击**。

### 2.4 极简 Task 接口

`Task.ts:72-76`：

```typescript
export type Task = {
  name: string
  type: TaskType
  kill(taskId: string, setAppState: SetAppState): Promise<void>
}
```

**优点**：注释（`Task.ts:69-71`）明确说明 spawn/render 从未被多态调用过，已在 #22546 中移除。`kill` 是唯一的多态接口，只依赖 `setAppState`，避免了对 `abortController` 的不必要依赖。**这是"接口最小化"的优秀实践——不为不存在的多态需求买单。**

---

## 3. Agent 定义体系（AgentDefinition）

**文件**：`loadAgentsDir.ts`

### 3.1 三种 Agent 来源的联合类型

`loadAgentsDir.ts:106-165`：

| 类型 | source | 特点 |
|------|--------|------|
| `BuiltInAgentDefinition` | `'built-in'` | 动态 prompt（`getSystemPrompt` 接收 `toolUseContext`），有 `callback` |
| `CustomAgentDefinition` | `'userSettings'`/`'projectSettings'`/`'policySettings'`/`'flagSettings'` | prompt 存于闭包，来自 Markdown/JSON |
| `PluginAgentDefinition` | `'plugin'` | 带插件元数据，`plugin` 字段标识来源 |

**优点**：统一接口 `AgentDefinition` 联合类型 + `isBuiltInAgent()`/`isCustomAgent()`/`isPluginAgent()` 类型守卫，让消费方可以安全区分来源。

### 3.2 丰富的 Agent 配置字段

`loadAgentsDir.ts:106-133`（`BaseAgentDefinition`）：

```typescript
{
  agentType: string           // 唯一标识
  whenToUse: string           // 何时使用（展示给 LLM 选路）
  tools?: string[]            // 工具白名单（支持 '*' 通配）
  disallowedTools?: string[]  // 工具黑名单
  skills?: string[]           // 预加载的 skill
  mcpServers?: AgentMcpServerSpec[]  // Agent 专属 MCP 服务器
  hooks?: HooksSettings       // 生命周期钩子
  model?: string              // 模型覆盖（支持 'inherit'）
  effort?: EffortValue        // 推理强度
  permissionMode?: PermissionMode  // 权限模式覆盖
  maxTurns?: number           // 最大轮次
  memory?: AgentMemoryScope   // 持久化记忆范围
  isolation?: 'worktree' | 'remote'  // 隔离模式
  background?: boolean        // 强制后台运行
  initialPrompt?: string      // 首轮追加 prompt
  color?: AgentColorName      // UI 颜色
  omitClaudeMd?: boolean      // 省略 CLAUDE.md 上下文（省 token）
  requiredMcpServers?: string[]  // MCP 依赖声明
  criticalSystemReminder_EXPERIMENTAL?: string  // 每轮注入的提醒
}
```

**优点**：
- **声明式配置**：Agent 行为通过 frontmatter 声明，不需要写代码。
- **工具白/黑名单**：`tools` + `disallowedTools` 双向控制，支持通配符和权限规则解析。
- **MCP 依赖声明**：`requiredMcpServers` 让框架在启动前验证依赖，避免运行时失败。
- **省 token 优化**：`omitClaudeMd` 为只读 agent 省略 CLAUDE.md（`loadAgentsDir.ts:128-132` 注释说每周省 5-15 Gtok）。

### 3.3 Agent 优先级与覆盖

`loadAgentsDir.ts:193-221`（`getActiveAgentsFromList`）：

```
built-in → plugin → user → project → flag → managed
```

后加载的同名 agent **覆盖**先加载的。**优点**：允许项目级 agent 覆盖内置 agent，策略级（managed）拥有最高优先级。

### 3.4 Markdown + JSON 双格式加载

- `parseAgentFromMarkdown()`（`loadAgentsDir.ts:541-755`）：从 `.md` 文件解析 frontmatter + 正文（正文即 system prompt）。
- `parseAgentFromJson()`（`loadAgentsDir.ts:445-516`）：从 JSON 配置解析，SDK 友好。
- 两者共用 Zod schema 验证（`loadAgentsDir.ts:73-99`），**统一校验逻辑**。
- 记忆启用时自动注入 Write/Edit/Read 工具（`loadAgentsDir.ts:456-467`、`662-674`）。

### 3.5 Memoized 加载 + 缓存清理

`loadAgentsDir.ts:296-398`：

```typescript
export const getAgentDefinitionsWithOverrides = memoize(async (cwd) => { ... })
export function clearAgentDefinitionsCache(): void { ... }
```

**优点**：memoize 避免重复加载，`clearAgentDefinitionsCache()` 在 `/reload-plugins` 或配置变更时调用，确保一致性。

---

## 4. 内置 Agent 设计

**文件**：`built-in/` 目录

### 4.1 Explore Agent（只读搜索专家）

`built-in/exploreAgent.ts:64-83`：

```typescript
export const EXPLORE_AGENT: BuiltInAgentDefinition = {
  agentType: 'Explore',
  disallowedTools: [AGENT_TOOL_NAME, EXIT_PLAN_MODE_TOOL_NAME, 
                    FILE_EDIT_TOOL_NAME, FILE_WRITE_TOOL_NAME, NOTEBOOK_EDIT_TOOL_NAME],
  model: process.env.USER_TYPE === 'ant' ? 'inherit' : 'haiku',  // 外部用户用 haiku 提速
  omitClaudeMd: true,  // 只读 agent 不需要 commit/PR/lint 规则
}
```

**优点**：
- **只读强制**：通过 `disallowedTools` 硬性禁止所有写工具，系统提示中重复强调 "READ-ONLY MODE"。
- **快速模型**：外部用户用 haiku（快），内部用 inherit（强）。
- **并行工具调用**：提示词明确鼓励并行 grep/read（`exploreAgent.ts:54-55`）。
- **省 token**：`omitClaudeMd: true`。

### 4.2 Plan Agent（架构规划专家）

`built-in/planAgent.ts:73-92`：

- 同样只读（`disallowedTools` 与 Explore 一致）。
- `model: 'inherit'`（需要强模型做架构推理）。
- 系统提示要求输出 "Critical Files for Implementation" 列表，**结构化输出**便于后续 agent 直接消费。

### 4.3 General Purpose Agent（通用执行 agent）

`built-in/generalPurposeAgent.ts:25-34`：

```typescript
export const GENERAL_PURPOSE_AGENT: BuiltInAgentDefinition = {
  agentType: 'general-purpose',
  tools: ['*'],  // 全工具
  // model 省略 → 使用默认 subagent 模型
}
```

**优点**：作为 fallback agent，拥有全部工具，适合需要写文件的多步骤任务。

### 4.4 Coordinator 模式下的 Agent 替换

`builtInAgents.ts:35-43`：当 `CLAUDE_CODE_COORDINATOR_MODE` 开启时，`getBuiltInAgents()` 返回 `getCoordinatorAgents()` 而非常规 agent 列表。**优点**：coordinator 模式有自己的 agent 体系，与普通模式完全隔离。

---

## 5. Agent 执行核心（runAgent）

**文件**：`tools/AgentTool/runAgent.ts`

### 5.1 AsyncGenerator 设计

`runAgent.ts:248-329`：`runAgent` 是一个 `AsyncGenerator<Message, void>`，逐条 yield message。

**优点**：
- **流式传递**：agent 产生的每条 message 实时 yield，支持前端流式渲染。
- **增量转录**：`runAgent.ts:792-805` 只记录新 message（O(1) per message），配合 `lastRecordedUuid` 维护父子链。

### 5.2 上下文继承与过滤

`runAgent.ts:368-378`：

```typescript
const contextMessages = forkContextMessages
  ? filterIncompleteToolCalls(forkContextMessages)
  : []
const initialMessages = [...contextMessages, ...promptMessages]
```

`filterIncompleteToolCalls()`（`runAgent.ts:866-904`）过滤掉没有对应 tool_result 的 tool_use，**防止 API 报错**。

**优点**：fork 路径继承父对话时自动清理不完整的工具调用，保证消息结构完整性。

### 5.3 只读 Agent 的上下文瘦身

`runAgent.ts:385-410`：

```typescript
// Explore/Plan 不需要 CLAUDE.md（commit/PR/lint 规则）
const shouldOmitClaudeMd = agentDefinition.omitClaudeMd && !override?.userContext
const { claudeMd: _omittedClaudeMd, ...userContextNoClaudeMd } = baseUserContext

// Explore/Plan 不需要 stale gitStatus（最多 40KB）
const { gitStatus: _omittedGitStatus, ...systemContextNoGit } = baseSystemContext
```

**优点**：根据 agent 角色裁剪上下文，每周省数 Gtok。**这不是"所有 agent 都给全量上下文"，而是"按需给"**。

### 5.4 权限模式覆盖逻辑

`runAgent.ts:412-498`（`agentGetAppState`）：

- Agent 可定义自己的 `permissionMode`，但**不能覆盖** parent 的 `bypassPermissions`/`acceptEdits`/`auto`。
- 异步 agent 设置 `shouldAvoidPermissionPrompts: true`（无法显示 UI）。
- `bubble` 模式的 agent 可以显示权限提示（冒泡到父终端）。
- 后台 agent 设置 `awaitAutomatedChecksBeforeDialog: true`（等待自动检查后再弹窗）。
- `allowedTools` 提供时**替换** session 级规则，但**保留** SDK 级 `cliArg` 规则。

**优点**：细粒度权限控制，防止父 agent 的权限泄漏到子 agent。

### 5.5 Agent 专属 MCP 服务器

`runAgent.ts:85-218`（`initializeAgentMcpServers`）：

- Agent frontmatter 可定义 `mcpServers`，**叠加到**父 context 的 MCP 客户端上。
- 引用式（字符串）共享父级 memoized 连接；内联式（对象）新建连接并在 agent 结束时清理。
- `plugin-only` 策略下，用户 agent 的 frontmatter MCP 被跳过，但 plugin/built-in agent 不受影响。

**优点**：Agent 可以有自己的工具生态，不污染父级，生命周期自动管理。

### 5.6 Skill 预加载

`runAgent.ts:577-646`：

- Agent frontmatter 的 `skills` 列表在 agent 启动时预加载。
- 三级名称解析：精确匹配 → 插件前缀限定 → 后缀匹配（`runAgent.ts:945-973`）。
- 加载的 skill 内容作为 `isMeta: true` 的 user message 注入 `initialMessages`。

**优点**：Agent 启动时即拥有 skill 知识，不需要运行时再触发。

### 5.7 Sync vs Async 上下文隔离

`runAgent.ts:524-528`、`runAgent.ts:697-714`：

```typescript
// AbortController:
// - Override 优先
// - Async agent 获得新的独立 controller
// - Sync agent 共享父 controller
const agentAbortController = override?.abortController
  ? override.abortController
  : isAsync ? new AbortController() : toolUseContext.abortController

// createSubagentContext:
// - Sync 共享 setAppState
// - Async 完全隔离
shareSetAppState: !isAsync,
```

**优点**：
- **Sync agent**：取消父 = 取消子，状态共享，适合需要结果才能继续的场景。
- **Async agent**：独立取消，独立状态，适合并行不阻塞的场景。
- `rootSetAppState`（`runAgent.ts:337-338`）解决嵌套 async→async 的 setAppState 穿透问题。

### 5.8 完善的 finally 清理

`runAgent.ts:816-859`：

```typescript
finally {
  await mcpCleanup()           // 清理 MCP 服务器
  clearSessionHooks(...)       // 清理生命周期钩子
  cleanupAgentTracking(...)    // 清理 prompt cache 跟踪
  agentToolUseContext.readFileState.clear()  // 释放文件状态缓存
  initialMessages.length = 0   // 释放 fork context 消息
  unregisterPerfettoAgent(...) // 释放 trace 注册
  clearAgentTranscriptSubdir(...)  // 释放转录目录映射
  // 释放 todos 条目（防止内存泄漏）
  rootSetAppState(prev => { ... })
  // 杀掉 agent 派生的后台 shell 任务
  killShellTasksForAgent(...)
}
```

**优点**：**每个资源都有明确的释放路径**。特别注意到 todos 清理和 shell 任务清理——"whale sessions spawn hundreds of agents; each orphaned key is a small leak that adds up"（`runAgent.ts:836-838`）。

### 5.9 onQueryProgress 心跳回调

`runAgent.ts:324-328`、`runAgent.ts:758`：

```typescript
onQueryProgress?: () => void  // 每次 query() yield 时调用
// ...
for await (const message of query({ ... })) {
  onQueryProgress?.()  // 检测长时间无 assistant message 的活跃度
```

**优点**：解决长时间 thinking 单块流式中"60s 无 assistant message"的活度检测问题。

---

## 6. AgentTool 调度入口

**文件**：`tools/AgentTool/AgentTool.tsx`

### 6.1 多路径调度

`AgentTool.tsx:239-250` 的 `call()` 方法是所有 agent spawn 的入口，路由逻辑：

```
subagent_type 指定 → 选择对应 AgentDefinition
subagent_type 省略 + fork 开启 → fork 路径（继承父上下文）
subagent_type 省略 + fork 关闭 → general-purpose
team_name + name → spawnTeammate（多 agent swarm）
isolation: 'remote' → 远程 CCR
isolation: 'worktree' → git worktree 隔离
```

### 6.2 Fork 递归守卫

`AgentTool.tsx:326-334`：

```typescript
// 双重检查：querySource（compaction-resilient）+ 消息扫描（fallback）
if (toolUseContext.options.querySource === `agent:builtin:${FORK_AGENT.agentType}` 
    || isInForkChild(toolUseContext.messages)) {
  throw new Error('Fork is not available inside a forked worker.')
}
```

**优点**：fork 子进程保留 Agent 工具（为 cache 一致），但在 call 时拦截递归 fork。querySource 存在 context.options 上，**survives autocompact**（autocompact 重写消息但不重写 context.options）。

### 6.3 Sync→Async 运行时转换

`AgentTool.tsx:808-833`：

```typescript
// Sync agent 注册为 foreground task
const registration = registerAgentForeground({
  agentId: syncAgentId,
  autoBackgroundMs: getAutoBackgroundMs() || undefined
})
foregroundTaskId = registration.taskId
backgroundPromise = registration.backgroundSignal.then(() => ({ type: 'background' }))
```

**优点**：sync agent 可以在运行中被"后台化"（用户点击或自动超时 `autoBackgroundMs`），实现**前台→后台无缝转换**。

### 6.4 Worker 工具池独立组装

`AgentTool.tsx:568-577`：

```typescript
// Worker 的工具池独立于父级组装
const workerPermissionContext = {
  ...appState.toolPermissionContext,
  mode: selectedAgent.permissionMode ?? 'acceptEdits'
}
const workerTools = assembleToolPool(workerPermissionContext, appState.mcp.tools)
```

**优点**：子 agent 不受父 agent 工具限制的影响，用自己的权限模式组装工具池。注释（`AgentTool.tsx:570-572`）说明这是为了避免 `runAgent` 导入 `tools.ts` 的循环依赖。

### 6.5 Worktree 隔离与自动清理

`AgentTool.tsx:582-593`、`AgentTool.tsx:644-685`：

- `createAgentWorktree(slug)` 创建临时 git worktree。
- agent 结束后检查是否有变更：无变更 → 自动删除；有变更 → 保留并返回路径和分支。
- `cleanupWorktreeIfNeeded` 返回 `{ worktreePath, worktreeBranch }` 供通知使用。

**优点**：**隔离的文件系统 + 自动生命周期管理**。agent 可以自由修改文件而不影响主工作区。

### 6.6 Name Registry

`AgentTool.tsx:700-712`：

```typescript
if (name) {
  rootSetAppState(prev => {
    const next = new Map(prev.agentNameRegistry)
    next.set(name, asAgentId(asyncAgentId))
    return { ...prev, agentNameRegistry: next }
  })
}
```

**优点**：异步 agent 可通过 `name` 被 `SendMessage({to: name})` 寻址，**支持具名 agent 通信**。

---

## 7. Fork Subagent（上下文继承 fork）

**文件**：`tools/AgentTool/forkSubagent.ts`

### 7.1 Fork vs Subagent 的本质区别

`forkSubagent.ts:18-39`：

- **Fork**：继承父的全量对话上下文 + 系统提示，**共享 prompt cache**。
- **Subagent**：全新开始，零上下文，需要完整 briefing。

**优点**：fork 适合"不需要保留中间工具输出在父上下文"的研究/实现任务，比新建 subagent 更便宜（cache 复用）。

### 7.2 Cache 一致的 Fork 消息构建

`forkSubagent.ts:107-169`（`buildForkedMessages`）：

```typescript
// 1. 克隆父的完整 assistant 消息（所有 tool_use、thinking、text）
// 2. 为每个 tool_use 构建相同的 placeholder tool_result
// 3. 追加 per-child directive（唯一不同的部分）
// 结果：[...history, assistant(all_tool_uses), user(placeholder_results..., directive)]
```

**优点**：所有 fork child 的 API 请求前缀**字节一致**，只有最后的 directive text block 不同，**最大化 prompt cache 命中**。

### 7.3 Fork 子进程的行为约束

`forkSubagent.ts:171-198`（`buildChildMessage`）：

```
STOP. READ THIS FIRST.
You are a forked worker process. You are NOT the main agent.
RULES (non-negotiable):
1. Do NOT spawn sub-agents; execute directly.
2. Do NOT converse, ask questions, or suggest next steps
3. Do NOT editorialize or add meta-commentary
4. USE your tools directly
5. If you modify files, commit your changes before reporting
6. Do NOT emit text between tool calls
7. Stay strictly within your directive's scope
...
Output format:
  Scope: <echo back your assigned scope>
  Result: <the answer or key findings>
  Key files: <relevant file paths>
  Files changed: <list with commit hash>
  Issues: <list>
```

**优点**：
- **结构化输出**：固定格式，便于父 agent 解析。
- **禁止递归 fork**：规则 1 明确禁止。
- **静默执行**：禁止元评论和中间文本，只在最后报告一次。
- **范围约束**：规则 7 防止 scope creep。

### 7.4 Worktree 路径翻译通知

`forkSubagent.ts:205-209`（`buildWorktreeNotice`）：

告知 fork 子进程它在隔离 worktree 中，需要翻译继承上下文中的路径，重新读取可能被父修改的文件。

**优点**：fork + worktree 组合时，子进程知道路径差异，避免错误操作。

### 7.5 Fork 互斥逻辑

`forkSubagent.ts:32-39`：

```typescript
export function isForkSubagentEnabled(): boolean {
  if (feature('FORK_SUBAGENT')) {
    if (isCoordinatorMode()) return false  // coordinator 有自己的委托模型
    if (getIsNonInteractiveSession()) return false  // SDK 模式不支持
    return true
  }
  return false
}
```

**优点**：明确互斥关系，避免两种编排模式冲突。

---

## 8. Coordinator 编排模式

**文件**：`coordinator/coordinatorMode.ts`

### 8.1 Coordinator 角色定义

`coordinatorMode.ts:111-369`（`getCoordinatorSystemPrompt`）：

```
You are a coordinator. Your job is to:
- Help the user achieve their goal
- Direct workers to research, implement and verify code changes
- Synthesize results and communicate with the user
- Answer questions directly when possible
```

**核心设计**：Coordinator 不直接执行工具，而是编排 worker agent。Coordinator 只拥有 `Agent`（spawn）、`SendMessage`（continue）、`TaskStop`（stop）三个工具。

### 8.2 四阶段任务工作流

`coordinatorMode.ts:198-210`：

| 阶段 | 执行者 | 目的 |
|------|--------|------|
| Research | Workers（并行） | 调查代码库、理解问题 |
| Synthesis | **Coordinator** | 阅读发现、理解问题、编写实现规格 |
| Implementation | Workers | 按规格做针对性修改 |
| Verification | Workers | 测试验证 |

**优点**：**Coordinator 必须自己做 Synthesis**——这是最关键的设计决策。`coordinatorMode.ts:255-259`：

> When workers report research findings, **you must understand them before directing follow-up work**.
> Never write "based on your findings" or "based on the research." These phrases delegate understanding to the worker instead of doing it yourself.

### 8.3 并发管理策略

`coordinatorMode.ts:212-218`：

```
- Read-only tasks (research) — run in parallel freely
- Write-heavy tasks (implementation) — one at a time per set of files
- Verification can sometimes run alongside implementation on different file areas
```

**优点**：按任务类型分级并发——读并行、写串行、验证可旁路。

### 8.4 Continue vs Spawn 决策矩阵

`coordinatorMode.ts:282-293`：

| 场景 | 机制 | 原因 |
|------|------|------|
| 研究恰好覆盖待编辑文件 | Continue | 已有文件上下文 + 清晰计划 |
| 研究广但实现窄 | Spawn fresh | 避免探索噪声 |
| 修正失败或延续近期工作 | Continue | 有错误上下文 |
| 验证不同 worker 写的代码 | Spawn fresh | 新鲜视角，不带实现假设 |
| 首次方法完全错误 | Spawn fresh | 错误上下文污染重试 |
| 完全无关任务 | Spawn fresh | 无可复用上下文 |

**优点**：**基于上下文重叠度的决策框架**，而非"总是 continue"或"总是 spawn"。

### 8.5 Worker 通知模型

`coordinatorMode.ts:142-164`：Worker 结果以 `<task-notification>` XML 作为 user-role 消息到达：

```xml
<task-notification>
<task-id>{agentId}</task-id>
<status>completed|failed|killed</status>
<summary>{human-readable status summary}</summary>
<result>{agent's final text response}</result>
<usage>
  <total_tokens>N</total_tokens>
  <tool_uses>N</tool_uses>
  <duration_ms>N</duration_ms>
</usage>
</task-notification>
```

**优点**：
- **统一消息模型**：worker 结果就是一条 user 消息，coordinator 不需要特殊协议。
- `task-id` 可直接用于 `SendMessage` 继续该 worker。
- `<usage>` 提供成本可观测性。
- Coordinator 被明确告知"不要预测/编造 worker 结果"（`prompt.ts:93`）。

### 8.6 Scratchpad 跨 worker 知识共享

`coordinatorMode.ts:104-107`：

```typescript
if (scratchpadDir && isScratchpadGateEnabled()) {
  content += `\n\nScratchpad directory: ${scratchpadDir}\nWorkers can read and write here without permission prompts. Use this for durable cross-worker knowledge.`
}
```

**优点**：通过共享文件系统目录实现 worker 间的持久化知识传递，**不需要把知识塞进 LLM prompt**。

---

## 9. 异步 Agent 生命周期管理

**文件**：`tools/AgentTool/agentToolUtils.ts`

### 9.1 runAsyncAgentLifecycle

`agentToolUtils.ts:508-686`：驱动后台 agent 从 spawn 到终态通知的完整生命周期：

```
spawn → progress tracking → completion/kill/error → notification
```

**优点**：
- 进度跟踪器（`createProgressTracker`）+ 活动描述解析器（`createActivityDescriptionResolver`）提供实时进度。
- Agent 摘要（`startAgentSummarization`）为长任务提供周期性进度摘要。
- 每条 message 实时追加到 AppState（`agentToolUtils.ts:559-570`），UI 可实时查看。

### 9.2 终态优先于装饰

`agentToolUtils.ts:599-603`：

```typescript
// Mark task completed FIRST so TaskOutput(block=true) unblocks immediately.
// classifyHandoffIfNeeded (API call) and getWorktreeResult (git exec) are
// notification embellishments that can hang — they must not gate the status transition.
completeAsyncAgent(agentResult, rootSetAppState)
```

**优点**：状态转换优先于可能挂起的装饰性操作（安全分类 API、git 操作），防止 gh-20236 类死锁。

### 9.3 Handoff 安全分类器

`agentToolUtils.ts:389-481`（`classifyHandoffIfNeeded`）：

- 当 `TRANSCRIPT_CLASSIFIER` feature 开启且权限模式为 `auto` 时，对 agent 的完整转录进行安全分类。
- 分类结果：`blocked`（安全警告）/ `unavailable`（分类器不可用，带警告放行）/ `allowed`。
- 安全警告以 `SECURITY WARNING:` 前缀注入最终消息。

**优点**：**多层安全防护**——agent 执行时有权限控制，handoff 时有转录审查。

### 9.4 Kill 时的部分结果保留

`agentToolUtils.ts:641-668`：

```typescript
if (error instanceof AbortError) {
  killAsyncAgent(taskId, rootSetAppState)
  const partialResult = extractPartialResult(agentMessages)
  enqueueAgentNotification({ status: 'killed', finalMessage: partialResult, ... })
}
```

`extractPartialResult()`（`agentToolUtils.ts:488-500`）从后往前找最后一条有文本内容的 assistant 消息。

**优点**：被 kill 的 agent 也能保留已完成的工作成果，不浪费已消耗的 token。

### 9.5 finalizeAgentTool 统一结果

`agentToolUtils.ts:276-357`：

- 提取最后一条 assistant 消息的 text content。
- 如果最后一条是纯 tool_use（循环中途退出），回退到最近有文本的消息。
- 统计 tool use 次数、token 数、持续时间。
- 发出 `tengu_cache_eviction_hint` 事件，**提示推理服务可以回收该 agent 的 cache chain**。

**优点**：**主动通知 cache 回收**，避免 agent 结束后 cache 长期占用。

### 9.6 finally 清理

`agentToolUtils.ts:682-685`：

```typescript
finally {
  clearInvokedSkillsForAgent(agentIdForCleanup)
  clearDumpState(agentIdForCleanup)
}
```

---

## 10. Agent 恢复（Resume）

**文件**：`tools/AgentTool/resumeAgent.ts`

### 10.1 从磁盘转录恢复

`resumeAgent.ts:63-79`：

```typescript
const [transcript, meta] = await Promise.all([
  getAgentTranscript(asAgentId(agentId)),    // 磁盘转录
  readAgentMetadata(asAgentId(agentId)),     // 元数据
])
const resumedMessages = filterWhitespaceOnlyAssistantMessages(
  filterOrphanedThinkingOnlyMessages(
    filterUnresolvedToolUses(transcript.messages),
  ),
)
```

**优点**：三层过滤确保恢复的消息结构完整——移除未解决的 tool_use、孤立的 thinking 消息、空白 assistant 消息。

### 10.2 Content Replacement 状态重建

`resumeAgent.ts:75-79`：

```typescript
const resumedReplacementState = reconstructForSubagentResume(
  toolUseContext.contentReplacementState,
  resumedMessages,
  transcript.contentReplacements,
)
```

**优点**：恢复 prompt cache 稳定性所需的 content replacement 状态，确保恢复后的 agent 请求前缀与之前一致。

### 10.3 Worktree 恢复与 mtime 保护

`resumeAgent.ts:82-97`：

```typescript
const resumedWorktreePath = meta?.worktreePath
  ? await fsp.stat(meta.worktreePath).then(
      s => (s.isDirectory() ? meta.worktreePath : undefined),
      () => undefined,  // 不存在则回退到父 cwd
    )
  : undefined
if (resumedWorktreePath) {
  // Bump mtime so stale-worktree cleanup doesn't delete a just-resumed worktree
  await fsp.utimes(resumedWorktreePath, now, now)
}
```

**优点**：**防御性恢复**——worktree 可能被外部删除，stat 检查 + mtime 更新防止清理误删。

### 10.4 Fork 恢复的特殊处理

`resumeAgent.ts:116-148`：

Fork agent 恢复时需要重建父系统提示。优先使用 `toolUseContext.renderedSystemPrompt`（字节精确），回退到重新构建。

**优点**：注释（`forkSubagent.ts:54-59`）解释——重新调用 `getSystemPrompt()` 可能因 GrowthBook 冷热状态不同而 diverge，threading 渲染后的字节是 byte-exact 的。

### 10.5 跳过权限重新检查

`resumeAgent.ts:99`：

```typescript
// Skip filterDeniedAgents re-gating — original spawn already passed permission checks
```

**优点**：恢复不需要重新检查权限，因为原始 spawn 已经过检查。避免因权限规则变更导致已运行的 agent 无法恢复。

---

## 11. Agent 持久化记忆

**文件**：`tools/AgentTool/agentMemory.ts`、`agentMemorySnapshot.ts`

### 11.1 三级记忆范围

`agentMemory.ts:12-13`：

```typescript
export type AgentMemoryScope = 'user' | 'project' | 'local'
```

| 范围 | 路径 | 特点 |
|------|------|------|
| user | `~/.claude/agent-memory/<agentType>/` | 跨项目通用 |
| project | `<cwd>/.claude/agent-memory/<agentType>/` | 随 VCS 共享 |
| local | `<cwd>/.claude/agent-memory-local/<agentType>/` | 本机本项目，不入 VCS |

**优点**：**按知识类型分级持久化**——通用经验 user 级、项目规范 project 级、机器特定 local 级。

### 11.2 记忆注入

`agentMemory.ts:138-177`（`loadAgentMemoryPrompt`）：

- Agent 启动时加载记忆目录的 `MEMORY.md`。
- 根据 scope 注入不同的指导语（user 级"保持通用"、project 级"针对项目"、local 级"针对项目和机器"）。
- `ensureMemoryDirExists` 是 fire-and-forget（在 sync `getSystemPrompt` 回调中调用，不能 async）。

### 11.3 快照初始化系统

`agentMemorySnapshot.ts`：

- 项目级快照（`.claude/agent-memory-snapshots/<agentType>/`）作为记忆模板。
- `checkAgentMemorySnapshot()`（`agentMemorySnapshot.ts:98-144`）：首次使用 → initialize（复制快照到本地）；快照更新 → prompt-update（提示用户）。
- `replaceFromSnapshot()`：替换前先删除旧 `.md` 文件，避免 orphan。
- 同步元数据 `.snapshot-synced.json` 记录最后同步的快照时间戳。

**优点**：**团队知识分发**——项目维护 agent 记忆快照，团队成员首次使用时自动初始化，更新时提示。

### 11.4 路径遍历防护

`agentMemory.ts:68-104`（`isAgentMemoryPath`）：

```typescript
// SECURITY: Normalize to prevent path traversal bypasses via .. segments
const normalizedPath = normalize(absolutePath)
```

**优点**：安全检查使用 normalize + prefix match，防止 `..` 绕过。

### 11.5 远程记忆支持

`agentMemory.ts:30-44`：

当 `CLAUDE_CODE_REMOTE_MEMORY_DIR` 设置时，local 记忆持久化到远程挂载，按项目命名空间隔离。

**优点**：支持容器化/远程开发环境的记忆持久化。

---

## 12. 工具解析与权限控制

**文件**：`tools/AgentTool/agentToolUtils.ts`

### 12.1 三层工具过滤

`agentToolUtils.ts:70-116`（`filterToolsForAgent`）：

```typescript
return tools.filter(tool => {
  if (tool.name.startsWith('mcp__')) return true          // MCP 工具始终允许
  if (ALL_AGENT_DISALLOWED_TOOLS.has(tool.name)) return false  // 全局禁用
  if (!isBuiltIn && CUSTOM_AGENT_DISALLOWED_TOOLS.has(tool.name)) return false  // 自定义 agent 禁用
  if (isAsync && !ASYNC_AGENT_ALLOWED_TOOLS.has(tool.name)) {
    // 异步 agent 只允许白名单工具
    if (isAgentSwarmsEnabled() && isInProcessTeammate()) {
      if (toolMatchesName(tool, AGENT_TOOL_NAME)) return true  // teammate 可 spawn sync subagent
      if (IN_PROCESS_TEAMMATE_ALLOWED_TOOLS.has(tool.name)) return true
    }
    return false
  }
  return true
})
```

**优点**：
- **三层过滤**：全局禁用 → 自定义 agent 限制 → 异步 agent 白名单。
- MCP 工具豁免（agent 专属 MCP 已在 `initializeAgentMcpServers` 管理）。
- 异步 agent 受限（无法显示权限 UI 的 agent 不应有危险工具）。
- In-process teammate 有额外白名单（可 spawn sync subagent、可协调任务列表）。

### 12.2 resolveAgentTools 通配符解析

`agentToolUtils.ts:122-225`：

- `tools: undefined` 或 `['*']` → 全工具（过滤 disallowed 后）。
- `tools: ['Bash', 'Read']` → 仅指定工具。
- Agent tool spec 可携带 `allowedAgentTypes`（如 `Agent(worker, researcher)`）。
- `disallowedTools` 与 `tools` 取交集后过滤。
- **主线程跳过过滤**（`isMainThread` 参数），因为主线程工具池已由 `useMergedTools()` 正确组装。

**优点**：**灵活的工具声明 + 严格的运行时解析**，支持通配符、黑名单、agent 类型限制。

### 12.3 权限规则解析

`agentToolUtils.ts:186-204`：

```typescript
const { toolName, ruleContent } = permissionRuleValueFromString(toolSpec)
// Special case: Agent tool carries allowedAgentTypes metadata in its spec
if (toolName === AGENT_TOOL_NAME) {
  if (ruleContent) {
    allowedAgentTypes = ruleContent.split(',').map(s => s.trim())
  }
}
```

**优点**：`Agent(worker, researcher)` 语法解析为 `allowedAgentTypes`，**限制子 agent 可以 spawn 哪些 agent 类型**。

---

## 13. Prompt 工程设计

**文件**：`tools/AgentTool/prompt.ts`

### 13.1 动态 Agent 列表注入

`prompt.ts:49-64`：

```typescript
export function shouldInjectAgentListInMessages(): boolean { ... }
```

当开启时，agent 列表通过 `agent_listing_delta` attachment 注入，而非内嵌在 tool description 中。

**优点**（`prompt.ts:52-57`注释）：动态 agent 列表占 fleet cache_creation tokens 的 ~10.2%。MCP 连接、`/reload-plugins`、权限模式变更都会改变列表 → description 变 → tool-schema cache bust。改为 attachment 注入后 **tool description 保持静态**，cache 稳定。

### 13.2 "Writing the prompt" 指导

`prompt.ts:99-113`：

```
Brief the agent like a smart colleague who just walked into the room
— it hasn't seen this conversation, doesn't know what you've tried,
doesn't understand why this task matters.
- Explain what you're trying to accomplish and why.
- Describe what you've already learned or ruled out.
- Give enough context about the surrounding problem.
- If you need a short response, say so.
- Lookups: hand over the exact command.
  Investigations: hand over the question.
```

**优点**：**区分 lookup（给命令）和 investigation（给问题）**——prescribed steps 在前提错误时变成死重。

### 13.3 "Never delegate understanding" 反模式

`prompt.ts:112`：

```
Never delegate understanding. Don't write "based on your findings, fix the bug"
or "based on the research, implement it." Those phrases push synthesis onto
the agent instead of doing it yourself.
```

**优点**：与 coordinator 模式的 Synthesis 阶段呼应——**理解必须由编排者完成，不能委托给执行者**。

### 13.4 Fork 指导

`prompt.ts:80-97`：

```
Fork yourself (omit subagent_type) when the intermediate tool output isn't
worth keeping in your context.
- Research: fork open-ended questions. Parallel forks in one message.
- Implementation: prefer to fork implementation work that requires more than a couple of edits.

Don't peek. The tool result includes an output_file path — do not Read or tail it.
Don't race. After launching, you know nothing about what the fork found.
Never fabricate or predict fork results.
```

**优点**：
- **"Don't peek"**：fork 的价值在于隔离工具噪声，偷看 output_file 就破坏了这个价值。
- **"Don't race"**：禁止在通知到达前编造 fork 结果。
- **并行 fork**：一条消息中多个 tool call 实现并行 fork。

### 13.5 When NOT to use Agent tool

`prompt.ts:234-240`：

```
When NOT to use the Agent tool:
- If you want to read a specific file path, use FileRead or Glob instead
- If you are searching for a specific class definition, use Grep instead
- If you are searching within 2-3 files, use FileRead instead
```

**优点**：明确 agent 工具的边界，避免对简单任务过度委托。

### 13.6 并发指导

`prompt.ts:245-249`、`prompt.ts:271`：

```
- Launch multiple agents concurrently whenever possible
- If the user specifies "parallel", you MUST send a single message with multiple tool uses
```

**优点**：**强制单消息多 tool call 实现真并行**，而非串行 spawn。

---

## 14. Prompt Cache 优化策略

Claude Code 在 prompt cache 方面的设计是其最有价值的工程实践之一，贯穿多个文件：

### 14.1 Fork 的字节一致前缀

`forkSubagent.ts:96-106`、`forkSubagent.ts:91-93`：

```typescript
// Placeholder text used for all tool_result blocks in the fork prefix.
// Must be identical across all fork children for prompt cache sharing.
const FORK_PLACEHOLDER_RESULT = 'Fork started — processing in background'
```

所有 fork child 共享相同的 placeholder tool_result，只有最后的 directive 不同。`forkSubagent.ts:57-59` 注释：重新调用 `getSystemPrompt()` 可能因 GrowthBook 冷热状态不同而 diverge，所以 threading 渲染后的字节。

### 14.2 Agent 列表外置

`prompt.ts:52-57`：动态 agent 列表占 ~10.2% cache_creation tokens → 改为 attachment 注入。

### 14.3 只读 Agent 上下文裁剪

`runAgent.ts:385-410`：Explore/Plan 省略 CLAUDE.md 和 gitStatus。

### 14.4 Fork 子进程继承父的 thinkingConfig

`runAgent.ts:679-684`：

```typescript
thinkingConfig: useExactTools
  ? toolUseContext.options.thinkingConfig  // Fork: 继承父的 thinking 配置
  : { type: 'disabled' as const },         // 普通 subagent: 关闭 thinking
```

**优点**：fork 路径继承父的 thinkingConfig 以匹配 API 请求前缀；普通 subagent 关闭 thinking 控制成本。

### 14.5 Cache Eviction Hint

`agentToolUtils.ts:337-346`：

```typescript
// Signal to inference that this subagent's cache chain can be evicted.
const lastRequestId = lastAssistantMessage.requestId
if (lastRequestId) {
  logEvent('tengu_cache_eviction_hint', { scope: 'subagent_end', last_request_id })
}
```

**优点**：agent 结束时主动通知推理服务可以回收 cache，**避免已结束 agent 的 cache 长期占用**。

### 14.6 useExactTools 路径

`runAgent.ts:500-502`、`runAgent.ts:309-314`：

```typescript
const resolvedTools = useExactTools
  ? availableTools  // Fork: 直接用父的工具池，不过滤
  : resolveAgentTools(agentDefinition, availableTools, isAsync).resolvedTools
```

**优点**：fork 路径使用父的完整工具池（`toolUseContext.options.tools`），而非重新解析的工具列表，确保 tool-def 序列化与父一致。

### 14.7 CacheSafeParams 回调

`runAgent.ts:721-730`：

```typescript
if (onCacheSafeParams) {
  onCacheSafeParams({
    systemPrompt: agentSystemPrompt,
    userContext: resolvedUserContext,
    systemContext: resolvedSystemContext,
    toolUseContext: agentToolUseContext,
    forkContextMessages: initialMessages,
  })
}
```

**优点**：暴露 cache-safe 参数给后台摘要服务，**让摘要服务可以 fork agent 的对话做周期性进度摘要**，共享 cache。

### 14.8 Prompt Cache Break Detection

`runAgent.ts:824-826`：

```typescript
if (feature('PROMPT_CACHE_BREAK_DETECTION')) {
  cleanupAgentTracking(agentId)
}
```

**优点**：专门的 cache break 检测 feature，agent 结束时清理跟踪状态。

---

## 15. 对本项目的借鉴建议

基于以上分析，以下是本项目（LLM-deployment agent 系统）可以借鉴的关键设计：

### 15.1 直接可借鉴的设计模式

| 模式 | Claude Code 实现 | 本项目对应 |
|------|-----------------|-----------|
| **统一消息模型** | 所有 agent 结果以 `<task-notification>` XML 作为 user 消息回灌 | 当前已有 timeline event + projection，可统一为 "agent 结果 = user 消息" |
| **Coordinator Synthesis 强制** | Coordinator 必须自己理解 worker 发现，禁止 "based on your findings" | coordinator_2 的 transcript 不应转写为系统提示，而应由 coordinator 自己综合 |
| **Continue vs Spawn 决策** | 基于上下文重叠度的 6 种场景决策表 | 当前的 secondary_handoff 机制可参考此框架 |
| **三层工具过滤** | 全局禁用 → 自定义限制 → 异步白名单 | 可增强当前的工具隐藏机制 |
| **finally 全面清理** | MCP/hooks/cache/todos/shell tasks 全部释放 | agent 结束时的资源清理 |
| **Kill 部分结果保留** | extractPartialResult 从后往前找最后有文本的 assistant 消息 | agent 被 stop 后保留已完成工作 |
| **结构化 fork 输出** | Scope/Result/Key files/Files changed/Issues 固定格式 | fork/subagent 结果可结构化 |
| **Agent 记忆三级范围** | user/project/local + 快照初始化 | 可为 agent 配置持久化记忆 |

### 15.2 与本项目当前架构的对照

| Claude Code | 本项目当前 | 差异/启发 |
|-------------|-----------|----------|
| Fork（继承上下文）+ Subagent（全新）| coordinator_1 → coordinator_2 → basic_agent | 本项目可考虑 fork 机制减少 briefing 成本 |
| Worktree 隔离 | 无文件隔离 | 可为写操作 agent 增加 worktree 隔离 |
| Agent frontmatter 声明式配置 | config-agent-prompts.yaml | 本项目已有类似设计，可增加 tools/disallowedTools/memory 字段 |
| `<task-notification>` 统一通知 | timeline event + projection | 本项目的统一 timeline 已是更先进的设计 |
| Prompt cache 字节一致 | Compact Prefix-Cache Covenant | 本项目已有完善的 cache covenant，可补充 fork 字节一致策略 |
| Agent 专属 MCP | 无 | 本项目暂无 MCP，但工具隔离机制可参考 |
| Sync→Async 运行时转换 | 无 | 可考虑 agent 运行中转为后台 |

### 15.3 最值得引入的 5 个设计

1. **Coordinator Synthesis 强制原则**：coordinator 必须自己理解 worker 发现，禁止委托理解。这直接对应本项目 AGENTS.md 中 "coordinator_2 的全量可见不等于原样回灌上下文" 的约束，但 Claude Code 的表述更清晰——**理解是编排者的责任，不是执行者的**。

2. **Continue vs Spawn 决策矩阵**：基于上下文重叠度的 6 种场景，而非"总是 continue"或"总是 spawn"。本项目的 secondary_handoff 可参考此框架做更智能的决策。

3. **Prompt cache 字节一致 fork**：所有 fork child 共享相同 placeholder tool_result + 只有最后 directive 不同。本项目的 compact cache-first 可参考这种"最大化共享前缀"的设计。

4. **finally 全面资源清理**：每个资源（MCP/hooks/cache/todos/shell）都有明确释放路径。本项目的 agent 结束清理可参考此清单。

5. **Kill 部分结果保留**：被中止的 agent 也能保留已完成工作。本项目当前 stop 后的行为可参考。

### 15.4 不建议直接照搬的部分

- **AgentTeams / InProcessTeammate**：多进程 swarm 模型复杂度高，本项目单进程不需要。
- **Remote Agent (CCR)**：远程执行环境依赖基础设施，本项目暂不需要。
- **GrowthBook A/B 测试**：Claude Code 的大量 feature flag 用于灰度发布，本项目规模不需要。
- **Perfetto trace**：Google 内部追踪系统，本项目可用更轻量的方案。

---

## 附录：关键文件索引

| 文件 | 行数 | 核心职责 |
|------|------|----------|
| `Task.ts` | 125 | 统一 Task 抽象、ID 生成、终态守卫 |
| `tasks.ts` | 39 | Task 注册表、多态分发 |
| `tools/AgentTool/runAgent.ts` | 973 | Agent 执行核心、上下文构建、转录记录 |
| `tools/AgentTool/AgentTool.tsx` | 1398 | 调度入口、sync/async/fork/worktree 路由 |
| `tools/AgentTool/forkSubagent.ts` | 210 | Fork 路径、cache 一致消息构建 |
| `tools/AgentTool/agentToolUtils.ts` | 686 | 生命周期驱动、工具解析、安全分类 |
| `tools/AgentTool/resumeAgent.ts` | 265 | Agent 恢复、转录过滤、worktree 重建 |
| `tools/AgentTool/loadAgentsDir.ts` | 755 | Agent 定义加载、Markdown/JSON 解析 |
| `tools/AgentTool/prompt.ts` | 287 | Prompt 工程、agent 列表注入、使用指导 |
| `tools/AgentTool/agentMemory.ts` | 177 | 持久化记忆、三级范围、路径安全 |
| `tools/AgentTool/agentMemorySnapshot.ts` | 197 | 快照初始化、团队知识分发 |
| `tools/AgentTool/builtInAgents.ts` | 72 | 内置 agent 注册、coordinator 模式切换 |
| `tools/AgentTool/built-in/exploreAgent.ts` | 83 | 只读搜索专家 |
| `tools/AgentTool/built-in/planAgent.ts` | 92 | 只读架构规划 |
| `tools/AgentTool/built-in/generalPurposeAgent.ts` | 34 | 通用执行 agent |
| `coordinator/coordinatorMode.ts` | 369 | Coordinator 编排、四阶段工作流、决策矩阵 |
| `tasks/InProcessTeammateTask/InProcessTeammateTask.tsx` | 126 | 进程内队友生命周期 |

---

*本文档基于 `ref_src/Cld_Code/` 源码分析生成，行号引用均指向该目录下的源文件。*
