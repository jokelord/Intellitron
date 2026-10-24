# Unified Multi-Agent Message Model Design

## Goal

为本项目的多 agent 架构建立一套可扩展到 4+ agents 的统一消息模型，使系统同时满足以下目标：

- `coordinator_1` 保留 LLM 智能路由能力，但必须极速选路。
- `coordinator_2` 在处理复杂但可能暂时无需工具的任务时，可以像非 agent 模式一样，把全量推理过程 SSE 流式展示在聊天气泡中。
- `basic_agent` 与未来新增 agents 继续承载工具调用与执行职责。
- Web UI 必须强制流式反馈，不能长时间无响应。
- CLI 流式是可选项，可读性优先。
- 给用户看的消息与给模型消费的上下文必须解耦，进入 Harness Engineering 所要求的“上下文精细化管理”。

本设计采用统一消息模型（Unified Message Model）与三层存储（Storage Tier 1/2/3），并在 Phase 1 中只使用 deterministic compiler，不引入 LLM 摘要器作为主链路依赖。

## Background

当前系统已经暴露出以下关键问题：

- coordinator 路由阶段过慢，`basic_agent` 首个思考框往往在 1-3 分钟后才出现。
- 本地模型在 routing 阶段会大量复述提示词，导致推理资源浪费严重。
- Web UI 的“任务调度”阶段虽然已有一定流式能力，但卡片滚动与扫光效果尚未和真实 SSE 节奏完全绑定。
- `coordinator_2` 被产品明确要求以普通聊天气泡方式显示完整推理，而不是继续渲染成 agent thought 卡片。
- 若后续继续新增 agents，继续沿用“每个 agent 发明一套消息格式”的做法将快速失控。
- 直接把前端可见 transcript 原样喂回模型，会让上下文窗口被 UI 噪音、长 routing transcript、重复中间态污染。

用户已经明确确认以下产品边界：

- 必须保留 `coordinator_1` 的 LLM 路由判断能力，不能退化为纯规则或 embedding 路由。
- `coordinator_1` 提示词必须极简，并显式要求它必须输出 agent 选择动作。
- `coordinator_1` 的 timeout 设为 10 秒，配置项放在 `misc yaml` 中。
- 若 10 秒内 `coordinator_1` 没有选择任何 agent，则系统自动 fallback 到 `basic_agent`。
- 若 `coordinator_1` 成功选择 agent，则不打断它的正常 handoff。
- `coordinator_2` 必须能够像非 agent 模式一样，把完整推理流式显示在普通聊天气泡中。
- `coordinator_2` 允许继续切换到 `basic_agent` 或未来其他 agents。
- Web UI 强制要求流式输出；CLI 是否流式可以灵活处理，以可读性优先。
- Phase 1 先采用 deterministic compiler；不做“每轮让主 LLM 重新摘要历史”的方案。

## Non-Goals

- 不在 Phase 1 中引入 per-turn 的 LLM 摘要任务。
- 不在 Phase 1 中把旧历史全部改成语义蒸馏压缩。
- 不强制 CLI 保持 token 级流式显示。
- 不在本轮设计中确定所有 future agents 的具体业务职责。
- 不在本轮设计中彻底删除现有 `agent_step` / `content` SSE 兼容层。

## Product Decision

采用如下产品架构：

1. 引入两阶段 coordinator：
   - `coordinator_1`: 极速 LLM 路由器。
   - `coordinator_2`: 复杂推理 agent，可继续切换。

2. 引入统一消息模型：
   - 所有 agent 事件先进入 canonical timeline。
   - 再由 projection 层分别生成：
     - 可见消息层（Web UI / CLI）
     - agent 行为层（Context Pack）

3. 引入三层存储：
   - Tier 1: Raw Timeline
   - Tier 2: Behavior Records
   - Tier 3: Context Pack

4. 引入 timeout fallback：
   - `coordinator_1` 10 秒未选路 -> 直接切 `basic_agent`
   - 该行为是显式 timeline event，而不是隐式内部逻辑。

5. `coordinator_2` 的可见流：
   - 必须使用普通聊天气泡式流式展示。
   - 不再沿用 `agent_step.thought` 的前端渲染语义。

6. Phase 1 的摘要与上下文管理：
   - 以 deterministic compiler 为主。
   - 所有摘要尽量由代码生成。
   - 仅对将来确实需要的超长旧历史，预留异步 distiller 扩展口。

## Architecture Overview

### High-level flow

一轮 turn 的核心执行流为：

1. 用户消息进入系统。
2. 系统创建 `turn_started` 与 `user_message` timeline 事件。
3. 请求进入 `coordinator_1`。
4. `coordinator_1` 持续产出 `routing_delta`，投影为“任务调度”卡片。
5. 分支：
   - 10 秒内成功选路：
     - 生成 `routing_decision`
     - 生成 `agent_switched`
     - 切换到目标 agent
   - 10 秒内未选路：
     - 生成 `routing_timeout_fallback(target=basic_agent)`
     - 生成 `agent_switched(target=basic_agent, reason=timeout_fallback)`
     - 进入 `basic_agent`
6. 若目标为 `coordinator_2`：
   - 使用 `message_started/message_delta/message_completed`
   - 普通聊天气泡流式显示
   - 必要时继续切换到其他 agent
7. 若目标为 `basic_agent` / future agents：
   - 继续使用 action / tool / observation 驱动的 agent 行为流
8. turn 结束时生成 `turn_completed` 或 `turn_aborted`

### Agent responsibilities

#### `coordinator_1`

- 输入：用户当前 turn + 必要最小历史背景
- 输出：必须选择一个 agent
- 限制：
  - 不承担长篇复杂推理
  - 不承担工具执行
  - 不承担最终复杂回答主体
- 可见层：
  - 全量推理可显示在“任务调度”卡片
- 关键配置：
  - `timeout_seconds = 10`，放入 `misc yaml`

#### `coordinator_2`

- 角色：复杂推理 agent
- 能力：
  - 可耐心处理复杂但暂时不需要工具的推导、算法、解释
  - 若中途发现需要工具，可继续切换到 `basic_agent` 或 future agents
- 可见层：
  - 全量 reasoning / content 进入普通聊天气泡流式展示

#### `basic_agent`

- 角色：执行型 agent
- 能力：
  - 工具调用
  - 文件操作
  - 浏览器
  - 搜索
  - future execution-oriented behaviors

#### Future agents

- 所有 future agents 必须接入同一 timeline 事件协议
- 不允许为某个新 agent 单独发明新的可见层协议形态

## Canonical Timeline

### Why timeline is the source of truth

timeline 是所有事件的唯一真相来源。未来新增 agent、修改 UI、优化行为层 context pack，都基于 timeline 进行投影和编译，而不是直接依赖当前某一版前端消息结构。

### Recommended event families

#### Turn events

- `turn_started`
- `user_message`
- `turn_completed`
- `turn_aborted`

#### Routing events

- `routing_started`
- `routing_delta`
- `routing_decision`
- `routing_timeout_fallback`

#### Visible message events

- `message_started`
- `message_delta`
- `message_completed`

#### Agent action events

- `agent_switched`
- `tool_call_started`
- `tool_call_completed`
- `observation_added`

### Example event shapes

```json
{
  "type": "timeline_event",
  "event": {
    "kind": "routing_delta",
    "source": "coordinator_1",
    "turn_id": "turn_001",
    "message_id": "routing-card-1",
    "text": "正在分析这个请求更适合哪一类 agent...",
    "partial": true,
    "timestamp": 1778600000
  }
}

{
  "type": "timeline_event",
  "event": {
    "kind": "message_delta",
    "source": "coordinator_2",
    "turn_id": "turn_001",
    "message_id": "assistant-msg-42",
    "text": "我们先从这个方程的边界条件开始...",
    "partial": true,
    "display": "chat_bubble"
  }
}

{
  "type": "timeline_event",
  "event": {
    "kind": "tool_call_started",
    "source": "basic_agent",
    "tool_name": "read_file",
    "message_id": "agent-step-99"
  }
}
```

## SSE Design

### Principle

SSE transport 层不再围绕“某个 agent 该怎么显示”来设计，而是围绕统一 timeline 事件来设计。

建议分层：

- 底层：canonical timeline event
- 中层：SSE transport event
- 上层：frontend projection reducer

### Recommended SSE families

#### `timeline_event`

- 新的主干事件类型
- 用于承载 canonical timeline 的原子事件
- 所有 future agents 都优先走这一类

#### Compatibility events

继续保留：

- `content`
- `done`
- `error`
- `aborted`

在过渡期不立即移除现有 `agent_step` / `content` 兼容层。

### Key rule for `coordinator_2`

如果继续把 `coordinator_2` 的输出作为 `agent_step.thought` 暴露给前端，它会继续被渲染成思考框，而不是普通聊天气泡。

因此，`coordinator_2` 的可见输出必须走：

- `message_started`
- `message_delta`
- `message_completed`

并标注：

- `source = coordinator_2`
- `display = chat_bubble`

## Frontend Projection Design

### Projection principle

前端不再把 SSE payload 直接视为最终 UI 结构，而是先进入 projection reducer，再决定如何渲染。

### Dispatch card projection

“任务调度”卡片只消费 routing 相关 timeline 事件：

- `routing_started`
- `routing_delta`
- `routing_decision`
- `routing_timeout_fallback`

目标效果：

- 文本内容随 SSE 持续变化
- 卡片文本持续滚动
- 扫光动画持续循环
- 不能只替换一次文本后空转动画

### Chat bubble projection

普通聊天气泡消费：

- `message_started`
- `message_delta`
- `message_completed`

这将用于：

- `coordinator_2`
- 未来所有需要“assistant bubble style”展示的 visible agents

### Agent steps projection

agent steps 区域继续消费：

- `tool_call_started`
- `tool_call_completed`
- `observation_added`
- 以及需要保留的 agent 行为型 thought / action events

这会继续服务于：

- `basic_agent`
- future execution agents

### Text / vision parity

`frontend/src/app/page.tsx` 与 `frontend/src/app/vision/page.tsx` 必须共享同一套 projection 语义：

- dispatch card 行为一致
- `coordinator_2` bubble 流一致
- future agents 的消息投影一致

## Storage Tier Design

### Tier 1: Raw Timeline

职责：

- 全量事件归档
- 审计
- 回放
- 调试
- 前端流式真实来源

### Tier 2: Behavior Records

职责：

- 从 timeline 中沉淀出结构化行为记录
- 给 context compiler 提供稳定中间层

推荐记录类型：

- `route_receipt`
- `handoff_record`
- `tool_ledger`
- `unresolved_items`
- `working_memory`

### Tier 3: Context Pack

职责：

- 为当前目标 agent 编译真正可喂给模型的上下文包

推荐字段：

- `session_frame`
- `user_goal`
- `routing_receipt`
- `agent_contract`
- `recent_visible_history`
- `working_memory`
- `handoff_ledger`
- `tool_ledger`
- `unresolved_items`

## Behavior Layer / Harness Engineering

### Core principle

Raw timeline 不等于下一轮模型输入。

必须引入 `behavior compiler`：

- 输入：Raw Timeline + Behavior Records
- 输出：Per-Agent Context Pack

### Why not feed UI transcript directly

不能把以下内容直接原样塞回模型：

- 前端装饰文本
- 滚动文本和扫光 UI 文案
- dispatch card 视觉内容
- 大量 routing delta 原文
- 工具脏日志
- 已解决旧问题的低价值噪音

### What should be preserved

必须保留的高价值信息：

- 当前用户目标
- 最近 routing 决策及摘要
- 未完成事项
- 最近 agent 的关键结论
- 工具执行的结构化观察结果

## Summary / Compaction Strategy

### Decision

Phase 1 采用 deterministic compiler，**不让主 LLM 每轮都做摘要**。

### Why

如果每轮都让当前主模型重新提炼旧 turn，会显著拉长推理时间，投资回报率很低，违背本设计对低延迟 handoff 的目标。

### Recommended producers

#### 1. Deterministic compiler

默认主方案。

由代码从 timeline / behavior records 直接生成：

- route receipt
- handoff ledger
- tool ledger
- resolved / unresolved status
- fallback reason
- latest user goal

#### 2. Event-triggered distiller

仅在以下时机触发：

- turn 关闭
- agent 切换
- 窗口压力超过阈值

用途：

- 压缩超长 `coordinator_2` 推理
- 压缩超长 resolved turns

#### 3. Background cheap summarizer

只作为后续可选优化：

- 低优先级
- 低频
- 可缓存
- 后台执行
- 绝不阻塞主链路首帧与 handoff

### Recent unfinished turns

“最近未完成 turn：中高保真保留”的实现方式不是“每轮重新摘要”，而是：

- 持续维护 `working_memory`
- 持续维护 `unresolved_items`
- 持续维护 `latest_handoff_record`

也就是：增量维护结构化状态，而不是每轮全文压缩一次。

### Old resolved turns

“旧 turn：摘要化保留”的推荐方案：

- turn 完成后，先由 deterministic compiler 生成一个 summary skeleton
- 只有在将来确实需要更强语义压缩时，才异步补一个 distillation summary
- 结果应缓存、版本化，不重复计算

### Phase plan

#### Phase 1

- 仅 deterministic compiler
- 不引入 LLM 摘要器

#### Phase 2

- 仅对超长 resolved turns 增加异步 distiller
- 结果缓存、版本化

#### Phase 3

- 若 future agents 进一步增多，再考虑 agent-specific semantic compaction

## CLI Strategy

### Product rule

- Web UI：强制真实流式
- CLI：可读性优先，高自由度

### Recommendation

CLI 不强制保持 token 级流式。

可选策略：

1. routing 阶段只打状态摘要
2. `coordinator_2` / `basic_agent` 做节流流式
3. 若仍然太噪声，则仅输出阶段状态与最终结果

若 CLI 无法达到接近前端的可读流式效果，则宁可减少流式粒度，也不要保留“一 token 一行”的噪音输出。

## Config Design

新增或调整配置项应进入可配置文件，不能硬编码。

至少需要：

- `misc.coordinator_1_timeout_seconds = 10`

后续可扩展：

- dispatch card animation / text behavior tuning
- context compaction thresholds
- future distiller feature flags

## Documentation Deliverables

本设计要求同步交付以下文档：

1. 更新当前 worktree 的 `AGENTS.md`
   - 记录统一消息模型
   - 多阶段 coordinator
   - timeout fallback
   - canonical timeline
   - SSE timeline_event 方向
   - Storage Tier 1/2/3
   - Context Pack
   - 给下一个“不知情 AI”的注意事项

2. 在根目录新增 memo
   - 记录 Storage Tier 1/2/3 payload 示例
   - 记录 Per-Agent Context Pack 示例

## Risks and Mitigations

### Risk 1: `coordinator_2` 仍被错误渲染为 thought card

Mitigation:

- 强制 `coordinator_2` 走 `message_*` 事件族，而不是 `agent_step.thought`

### Risk 2: dispatch card 继续只有一次动画而非真实流式

Mitigation:

- dispatch card 只能消费持续的 `routing_delta`
- 文本更新和视觉动画都要绑定 reducer 最新状态

### Risk 3: 后续 future agents 再次发明独立前端协议

Mitigation:

- 统一要求所有 future agents 先落 timeline，再做 projection

### Risk 4: 行为层上下文被 UI transcript 污染

Mitigation:

- 使用 behavior compiler
- 明确区分 raw timeline 与 context pack

### Risk 5: 旧 turn 摘要带来新的主链路耗时

Mitigation:

- Phase 1 只使用 deterministic compiler
- 不做 per-turn LLM 摘要

## Acceptance Criteria

- `coordinator_1` 存在单独极速路由职责，且 timeout=10s 可配置。
- `coordinator_1` 在超时后会自动 fallback 到 `basic_agent`。
- `coordinator_2` 的全量 reasoning / content 能以普通聊天气泡方式 SSE 流式显示。
- `basic_agent` 与 future agents 继续使用统一 timeline + projection 架构，不再各自发明一套前端协议。
- Web UI 中 dispatch card 的文本更新与视觉滚动都建立在真实 SSE 流基础上。
- 行为层上下文不再直接等同于可见 transcript，而是由 Tier 1/2/3 与 context pack 编译生成。
- Phase 1 中不引入“每轮让主 LLM 总结旧 turn”的额外推理成本。
