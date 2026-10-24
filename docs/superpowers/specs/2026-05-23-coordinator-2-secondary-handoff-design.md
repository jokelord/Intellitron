# Coordinator_2 Secondary Handoff Design

## Goal

让 `coordinator_2` 正式具备“运行中二次 handoff”能力：

- 当 `coordinator_2` 在推理过程中发现任务实际需要工具、外部搜索或其他执行能力时，能够稳定切换到 `basic_agent`
- `coordinator_2` 的普通推理内容继续以聊天气泡形式流式展示
- `coordinator_2` 触发工具调用时，前端不再把 canonical JSON 泄漏到聊天气泡，而是展示为工具调用卡片与观察卡片
- text 页面与 vision 页面保持一致行为

## Background

当前实现已经具备两段能力，但中间存在断层：

1. `coordinator_1` 已具备明确的路由契约与 handoff 执行路径，能够在 `coordinator_2` 与 `basic_agent` 之间做首轮选路。
2. `coordinator_2` 已被产品定义为“复杂推理 agent”，其推理过程必须像普通聊天一样在聊天气泡内流式展示，而不是渲染为 thought 卡片。
3. 后端已经能产生 `tool_call` / `observation` 类型的 `agent_step` 事件，前端也已经有现成的工具调用卡片与观察卡片组件。

但真实行为仍有两个关键问题：

1. `coordinator_2` 虽然提示词里提到“必要时可以切换到 basic_agent”，但实际上没有拿到完整、稳定、可执行的二次切换能力。
2. 当 `coordinator_2` 输出工具调用 transcript 或 canonical JSON 时，原始文本会先被流式投进聊天气泡，导致前端出现花括号与工具调用命令泄漏；真正的 `tool_call` / `observation` 事件虽然存在，却没有和这条聊天气泡形成正确的展示归属。

现状偏差主要来自三处：

- 能力暴露不完整：`coordinator_2` 没有像 `coordinator_1` 那样拿到清晰的 `switch_agent` 输出契约，也没有通过 `managed_agents` 获得稳定的 agent schema 能力。
- 执行链路不完整：`coordinator_2` 运行期间没有一个正式的“识别 secondary handoff 并执行 handoff”的 runner 级路径。
- 可见层清洗过晚：原始流式文本先进入 `message_delta/chat_bubble`，工具调用解析与清洗发生在后面，导致 UI 先看到脏文本。

## Non-Goals

- 不把 `coordinator_2` 改回 thought 卡片展示模式。
- 不让 `coordinator_2` 直接变成全功能执行 agent。
- 不为 future agents 发明新的 SSE 顶层协议。
- 不在本轮引入 per-turn LLM 摘要器。
- 不大规模重写现有 `agent_step` / `timeline_event` 兼容层。

## Product Decision

采用“正式 secondary handoff + bubble 清洗 + 现有卡片复用”的组合方案：

1. `coordinator_2` 仍然是复杂推理 agent，但要正式具备向 `basic_agent` 发起二次 handoff 的能力。
2. `coordinator_2` 的可见推理仍走 `message_started/message_delta/message_completed`，保持普通聊天气泡式流式展示。
3. `coordinator_2` 一旦进入工具调用，不再把原始 canonical JSON 作为聊天文本显示给用户；工具调用与观察继续走现有 `agent_step.tool_call` / `agent_step.observation`。
4. 前端不新造组件，直接复用现有 `AgentStepDisplay` 的工具调用卡片与观察卡片，但要把这些卡片挂到 `coordinator_2` 对应的 assistant 消息下，而不是挂到错误的占位消息上。

## Architecture Overview

### High-level runtime flow

目标执行流如下：

1. `coordinator_1` 选路到 `coordinator_2`
2. `coordinator_2` 开始普通聊天气泡流式推理
3. 若 `coordinator_2` 继续认为无需工具：
   - 直接完成该聊天气泡
   - 输出最终回答
4. 若 `coordinator_2` 在推理中发现需要外部搜索、工具执行或时效性信息：
   - 输出受约束的 `switch_agent -> basic_agent` 动作
   - runner 识别该动作并触发 `agent_switched`
   - 将 fresh `context_pack` 注入 `basic_agent`
   - `basic_agent` 继续执行工具调用
5. 若 `basic_agent` 在同一轮中产出工具调用：
   - 工具调用显示为工具调用卡片
   - 工具结果显示为观察卡片
   - 不把工具 canonical JSON 泄漏到用户可见聊天气泡

### Source-of-truth constraints

实现必须继续遵守统一消息模型：

- timeline 仍是唯一真相来源
- `coordinator_2` 的用户可见流仍以 `message_*` 事件为主
- 工具行为仍以 `agent_step` / tool 事件为主
- 不把前端当前可见 transcript 原样回灌为后续 agent 的主体上下文

## Backend Design

### 1. Expose secondary handoff capability to `coordinator_2`

`coordinator_2` 必须同时在两条路径上拿到“只能切到 `basic_agent`”的能力：

1. **系统提示路径**
   - 在 `config/config-agent-prompts.yaml` 的 `coordinator_2` 提示词中补充明确契约。
   - 提示词必须包含：
     - 什么时候必须切到 `basic_agent`
     - 切换时只允许输出哪一种 canonical JSON
     - 切换时不能再补充额外 prose
     - 未切换时才能继续以普通自然语言回答
   - 目标是让 `coordinator_2` 拥有和 `coordinator_1` 同等清晰的切换格式约束，但目标 agent 仅允许 `basic_agent`。

2. **API schema / managed agents 路径**
   - `_build_profile_agent()` 为 `coordinator_2` 注入受限的 `managed_agents`。
   - `coordinator_2` 只看到 `basic_agent` 作为可 handoff 目标；不能把 future agents 暴露进来。
   - `tools_and_managed_agents` 继续把 `managed_agents` 传给模型 schema。

### 2. Formalize runner-side secondary handoff execution

不能只依赖模型“知道有这个能力”，还必须由 runner 在执行层承接它。

建议在 `AgentRunner` 中增加显式的 secondary handoff 处理：

1. 当当前 profile 是 `coordinator_2` 时，允许它产出 `switch_agent -> basic_agent`
2. runner 识别该 agent action 后：
   - 记录 `routing_decision` / `agent_switched` 风格的 timeline 信息，来源标记为 `coordinator_2`
   - 使用已有 `context_pack` 编译路径为 `basic_agent` 生成下一跳上下文
   - 激活 `basic_agent` runtime agent
   - 继续当前 turn，而不是结束 `coordinator_2` 的 run 并把切换动作当普通文本返回

这条链路需要显式区分两类情况：

- `coordinator_2` 普通完成：结束聊天气泡并返回 final answer
- `coordinator_2` secondary handoff：结束当前 `coordinator_2` bubble 的 streaming，并切入 `basic_agent` 执行流

### 3. Enforce `coordinator_2` tool boundary

本轮不允许 `coordinator_2` 变成直接执行工具的 agent。

因此需要明确两件事：

1. `PROFILE_TOOL_ALLOWLIST` 中把 `coordinator_2` 约束为不直接暴露普通工具
2. `coordinator_2` 的出路是：
   - 直接答复
   - 切换到 `basic_agent`

这与设计文档中 `coordinator_2` 的角色一致，也能避免“coordinator_2 既想当 reasoning agent 又偷偷直接调工具”的语义混乱。

### 4. Clean chat bubble output before emission

`coordinator_2` 的聊天气泡不能再无条件透传原始 `ChatMessageStreamDelta.content`。

目标行为：

1. 普通自然语言推理文本继续流式进入聊天气泡
2. 若文本已经进入工具调用 transcript 区域，例如：
   - `Calling tools:`
   - canonical JSON 数组
   - `<tool_call>` 包裹文本
   - 其他已被现有解析器识别为工具调用前缀的结构
   则这些片段不应直接进入聊天气泡
3. 真正的工具调用展示由后续 `MemoryToolCall` 事件承担
4. 真正的观察结果展示由后续 `SmolToolOutput` 事件承担

最小实现方式是复用现有工具调用清洗逻辑，在 `coordinator_2` bubble 发射点前增加缓冲/剥离，而不是等到文本已经推送给前端之后再做补救。

### 5. Timeline semantics

本轮不新增新的顶层 SSE 协议。

继续使用：

- `timeline_event`：承载 `coordinator_2` 的 `message_started/message_delta/message_completed`
- `agent_step`：承载工具调用与观察卡片

但需要补足以下语义：

1. `coordinator_2 -> basic_agent` secondary handoff 时，timeline 中要留下明确的 handoff 记录
2. `coordinator_2` bubble 的 `message_id` 必须保持 run/turn 唯一
3. bubble 文本完成与 handoff 触发之间不能出现重复 final text 或残余 canonical JSON

## Frontend Design

### 1. Reuse existing cards

`AgentStepDisplay` 已经支持：

- `tool_call`
- `observation`

本轮不新增新的卡片类型，也不改变工具调用卡片视觉语言。

### 2. Fix step ownership

核心问题不是“没有卡片”，而是卡片归属不对。

目标行为：

1. 若当前 assistant 消息来自 `coordinator_2` 的聊天气泡 timeline，则后续同一轮内由 secondary handoff 触发的 `tool_call` / `observation` 卡片，应挂到这条 assistant 消息下。
2. text 页面与 vision 页面必须都遵守同一归属规则。
3. 不允许继续把这类步骤卡片写回旧的 `assistantMessageId` 占位消息，导致用户看到“上面是 coordinator_2 bubble，下面工具卡片跑到别的消息里”。

### 3. Projection behavior

`timeline-projection.ts` 仍保持现有核心原则：

- `routing_delta -> dispatch_update`
- `coordinator_2 message_delta -> chat_chunk`

本轮不要求把 tool events 也塞进 timeline projection reducer；因为现有工具卡片通路已经存在。

真正需要修的点是：

1. 后端不要提前把 tool transcript 发进 `chat_chunk`
2. 页面状态管理把 `agent_step` 正确归属到当前 `coordinator_2` assistant bubble

### 4. Text and vision parity

`frontend/src/app/page.tsx` 与 `frontend/src/app/vision/page.tsx` 必须同步修改，保证：

- 同样的 bubble message id bookkeeping
- 同样的 `onStep` 归属逻辑
- 同样的 secondary handoff 展示效果

## Testing Strategy

### Backend tests

补充或更新以下测试：

1. `coordinator_2` profile 构建后：
   - 不暴露普通 tools
   - 仅暴露受限 `managed_agents` 目标 `basic_agent`
2. `coordinator_2` 提示词中包含明确的 secondary handoff 格式约束
3. `coordinator_2` 在运行中输出切换动作时：
   - runner 能识别并切换到 `basic_agent`
   - context pack / handoff 记录正确生成
4. `coordinator_2` 在含工具调用 transcript 的场景下：
   - 聊天气泡中不出现 canonical JSON
   - 工具调用仍通过 `agent_step.tool_call` 发出
   - 观察结果仍通过 `agent_step.observation` 发出
5. 现有 `coordinator_2` 普通聊天气泡流式测试继续通过，不退化为 thought 卡片路径

### Frontend tests

补充或更新以下测试：

1. 页面状态逻辑在 `coordinator_2` bubble 已开始后，收到 `tool_call` / `observation` 时，会把步骤挂到当前 bubble 对应消息下
2. text 页面与 vision 页面行为一致
3. `timeline-projection` 现有 `chat_chunk` / `dispatch_update` 语义不回归
4. `coordinator_2` 工具调用场景下，用户可见 bubble 文本不再出现 canonical JSON，而工具卡片与观察卡片照常渲染

## Risks and Mitigations

### Risk 1: secondary handoff breaks current `coordinator_2` direct-answer flow

缓解：secondary handoff 识别必须是显式分支，只在命中 `switch_agent -> basic_agent` 时触发；其余文本仍走现有 final-answer 路径。

### Risk 2: `coordinator_2` accidentally regains direct tool-call behavior

缓解：同时收紧 profile tool allowlist 与 managed-agent exposure，确保其职责边界明确。

### Risk 3: chat bubble text becomes over-filtered and loses useful reasoning

缓解：只剥离已知 tool transcript 结构，不对普通自然语言推理做激进过滤；新增针对典型 transcript 的回归测试。

### Risk 4: text and vision pages drift apart again

缓解：两页同步修改，并在验证阶段对两边的状态逻辑与类型行为做并行检查。

## Acceptance Criteria

- `coordinator_2` 在识别到时效性/外部信息/工具需求时，能够稳定切换到 `basic_agent`，而不是只在推理文本里“想切换”。
- `coordinator_2` 不直接暴露普通工具；其职责恢复为“直接回答或切换”。
- `coordinator_2` 的普通推理仍以聊天气泡流式展示。
- `coordinator_2` 触发工具调用时，聊天气泡中不再出现 canonical JSON / 花括号工具命令泄漏。
- 同一轮 secondary handoff 后的工具调用卡片与观察卡片显示在 `coordinator_2` 对应 assistant 消息下。
- text 页面与 vision 页面行为一致。
