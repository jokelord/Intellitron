# Frontend 变更记录

---

## 2026-03-17 ~ 03-18

### Feature: Agent Tool Calling 前端实现（Phase 4）

**背景**

配合后端新增的 `/v1/agent/chat` SSE 端点，前端需要：
1. 提供"Agent 模式"开关，让用户在普通对话与工具调用 Agent 之间切换
2. 用独立 API 方法接收并解析 Agent 专属 SSE 事件格式
3. 实时展示 Thought / Tool Call / Observation 三类步骤卡片

---

#### 新建 `src/components/AgentStepDisplay.tsx`

折叠卡片组件，接收 `steps: AgentStep[]`，按步骤类型渲染：

| step_type | 图标 | 颜色主题 | 默认状态 |
|---|---|---|---|
| `thought` | 🤔 | 蓝色 | 展示前 120 字，多余可点击展开 |
| `tool_call` | 🔧 | 琥珀色 | 显示工具名 badge + JSON 参数，多余可展开 |
| `observation` | 👁 | 绿色 | 展示前 120 字，多余可点击展开 |

- `StepCard` 子组件：折叠逻辑、预览截断（120 字）、`ChevronDown / ChevronRight` 图标
- 超过 120 字时点击整行展开，不超过则 `cursor-default` 禁止点击
- Tailwind 暗色模式适配（`dark:bg-*`、`dark:border-*`、`dark:text-*`）

---

#### 修改 `src/lib/api.ts`

**新增类型定义**

```typescript
// Agent 步骤（用于 AgentStepDisplay）
export interface AgentStep {
  step_type: 'thought' | 'tool_call' | 'observation';
  content?: string;
  name?: string;    // tool_call 专属：工具名称
  input?: string | Record<string, unknown>;  // tool_call 专属：参数
}

// Agent 对话请求体
export interface AgentChatRequest {
  session_id?: string;
  message: string;
  mode?: string;
}

// SSE 事件原始结构（内部使用）
interface AgentSSEEvent {
  type: 'agent_step' | 'content' | 'done' | 'error';
  step_type?: 'thought' | 'tool_call' | 'observation';
  content?: string;
  name?: string;
  input?: Record<string, unknown>;
  session_id?: string;
}
```

`ConfigResponse` 新增可选字段 `agent?: { enabled: boolean; mode: string }`，对应后端 `/config` 响应。

**新增方法 `agentChatStream()`**

```typescript
async agentChatStream(
  request: AgentChatRequest,
  onStep: (step: AgentStep) => void,
  onContent: (content: string) => void,
  onComplete?: (sessionId?: string) => void,
  onError?: (error: Error) => void,
): Promise<void>
```

- 请求目标：`POST /v1/agent/chat`，携带 `{ ...request, stream: true }`
- 超时时间设为 **10 分钟**（普通流式对话为 5 分钟，Agent 多步执行耗时更长）
- SSE 按 `event.type` 分流处理：
  - `agent_step` → 调用 `onStep()`，传入解析后的 `AgentStep`
  - `content` → 调用 `onContent()`，追加最终答案文本
  - `done` → 提取 `session_id` 后调用 `onComplete()`
  - `error` → 抛出异常由 `onError()` 捕获

---

#### 修改 `src/app/page.tsx`（主聊天页）

**消息类型扩展**

```typescript
interface Message {
  // ...原有字段
  agentSteps?: AgentStep[];   // 新增：Agent 步骤列表
  agentMode?: boolean;        // 新增：标识该消息是否由 Agent 模式产生
}
```

**状态新增**

```typescript
const [agentSessionId, setAgentSessionId] = useState<string | undefined>(undefined);
```

Agent session ID 存于组件 state，同一会话内复用，实现跨轮次的 Agent 记忆（smolagents `memory`）。

**Agent 模式开关**

- 从 `/config` 响应中读取 `config.agent.enabled`，决定是否显示"Agent 模式"Toggle
- 开关状态存入组件 state（`agentEnabled`），切换时不重置 session，仅下一次发送时生效
- 工具栏布局：`Agent 模式 [Toggle] [重置 Session]`

**发送逻辑分流**

```
发送消息
  └─ agentEnabled?
       ├─ YES → llmClient.agentChatStream(request, onStep, onContent, onComplete)
       │         onStep: 追加到当前消息 agentSteps[]
       │         onContent: 追加流式文本
       │         onComplete: 保存返回的 session_id
       └─ NO  → llmClient.chatStream(messages, options, onChunk)（原有逻辑不变）
```

**渲染逻辑**

每条 assistant 消息节点下，若 `message.agentSteps?.length > 0`，在消息正文**上方**渲染 `<AgentStepDisplay steps={message.agentSteps} />`；正文区展示最终答案文本流（与普通对话相同的 markdown 渲染路径）。

---

#### 修改 `src/app/vision/page.tsx`（Vision 页）

与 `page.tsx` 同步，新增相同的 Agent 模式逻辑：
- 引入 `AgentStep`、`AgentChatRequest`、`AgentStepDisplay`
- 状态 `agentSessionId` + Agent 模式开关
- 发送链路按 `agentEnabled` 分流到 `agentChatStream()` 或原 `chatStream()`
- 渲染层在消息正文上方插入 `<AgentStepDisplay>`

---

### Bug Fix: 观察结果为空 + 最终答案重复显示

**问题描述**

工具调用成功执行后，前端步骤卡片中 `#N 观察结果` 一栏始终为空；最终答案区域内容重复出现两次。

**根本原因（后端 `agent_runner.py`）**

smolagents 在 `stream=True` 时，`ToolOutput` 对象的实际观察数据存放在 `.observation` 属性而非 `.output`，早期代码读取了错误字段导致 observation 为空字符串。

同时，`FinalAnswerStep` 和 `ActionOutput(is_final_answer=True)` 均会被触发，若两者都向队列发送 `content` 事件，前端会收到两条内容，渲染出重复文本。

**修复内容（`backend/agent/agent_runner.py`）**

- `SmolToolOutput` 分支改为读取 `step_output.observation`（而非 `.output`）
- `FinalAnswerStep` 分支标记为"与 `ActionOutput` 重复，跳过"，不再发送额外 `content` 事件
- `MemoryActionStep` 分支仅发送 `thought`（读取 `model_output`），避免与上方专项事件重复

**涉及文件**

- `backend/agent/agent_runner.py` — `_run_agent()` 内部的 SSE 事件分发逻辑

---

### Bug Fix: React Hydration 错误（`<span>` 服务端/客户端不匹配）

**问题描述**

打开前端页面时控制台报错：

```
Warning: Expected server HTML to contain a matching <span> in <p>.
Uncaught Error: Hydration failed because the initial UI does not match what was rendered on the server.
```

错误定位在 `page.tsx:120`，即 Header 中条件渲染的 Agent 模式徽标：

```tsx
{settings.agentMode && (
  <span>...</span>
)}
```

**根本原因**

`settings` 状态原本通过惰性初始化（`useState(() => { ... })`）在组件挂载时直接读取 `localStorage`：

```ts
const [settings, setSettings] = useState<ChatSettings>(() => {
  if (typeof window !== 'undefined') {
    const saved = localStorage.getItem('llm-chat-settings');
    return saved ? { ...DEFAULT_SETTINGS, ...JSON.parse(saved) } : DEFAULT_SETTINGS;
  }
  return DEFAULT_SETTINGS;
});
```

- **服务端**：`window` 不存在，返回 `DEFAULT_SETTINGS`（`agentMode: false`），不渲染 `<span>`
- **客户端**：`localStorage` 中若存有 `agentMode: true`，首次渲染就带上 `<span>`

两端初始 HTML 结构不一致，触发 hydration 失败。

**修复方案（`src/app/page.tsx`）**

1. 将 `settings` 初始值统一为 `DEFAULT_SETTINGS`，确保服务端与客户端第一次渲染完全相同
2. 通过独立 `useEffect`（客户端挂载后执行一次）从 `localStorage` 恢复用户设置
3. 增加 `isHydratedRef` 标志，防止"保存 settings"的 `useEffect` 在加载完成前将默认值覆盖写回 `localStorage`

```ts
// 修复后
const [settings, setSettings] = useState<ChatSettings>(DEFAULT_SETTINGS);
const isHydratedRef = useRef(false);

// 客户端挂载后恢复存储的设置
useEffect(() => {
  const saved = localStorage.getItem('llm-chat-settings');
  if (saved) {
    try {
      setSettings({ ...DEFAULT_SETTINGS, ...JSON.parse(saved) });
    } catch { /* ignore malformed JSON */ }
  }
  isHydratedRef.current = true;
}, []);

// 仅在 hydrate 完成后才写入 localStorage
useEffect(() => {
  if (!isHydratedRef.current) return;
  localStorage.setItem('llm-chat-settings', JSON.stringify(settings));
}, [settings]);
```

**涉及文件**

- `src/app/page.tsx` — `settings` 状态初始化及 `localStorage` 读写逻辑
