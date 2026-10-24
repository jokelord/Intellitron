# Generic Transition UI Design

## Goal

把现有仅服务于 `coordinator_1` 路由阶段的 dispatch card，升级为一套可复用于所有 agent 切换动作的通用 transition UI，同时解决当前 `coordinator_2 -> basic_agent` secondary handoff 的两个体验问题：

- assistant 气泡内的展示顺序错误，导致 `basic_agent` 的步骤卡片压在 `coordinator_2` 的可见推理上方
- `switch_agent` canonical JSON 仍可能泄漏到用户可见正文中，破坏 UI 优雅度

本轮设计的目标展示顺序固定为：

1. A: `coordinator_1` routing dispatch card
2. C: `coordinator_2` 的“非 agent 模式”可见推理 / 正文
3. D: `coordinator_2` secondary handoff dispatch card（可见）
4. B: `basic_agent` 或 future execution agent 的 thought / tool_call / observation

## Background

当前实现已经完成了 `coordinator_2` secondary handoff 的 runner 级能力补齐，也修复了 canonical `switch_agent` array 在部分流式尾部路径中“被清洗但未真正触发 handoff”的问题。但从产品视角看，仍有两个残留断层：

1. assistant 气泡内部的渲染顺序仍然写死为：
   - `AgentStepDisplay`
   - `AssistantReasoningSection`
   - assistant 正文
   这会导致所有 `agent_step` 都天然排在 reasoning 上方。

2. `switch_agent` 目前虽然在大部分路径中已被 backend 识别为 secondary handoff，但它的“用户可见语义”仍未被正式定义为控制平面动作。部分路径仍可能让 canonical JSON 进入正文首行，导致 UI 回退到“用户先看到一条命令，再看到真正执行”的不优雅状态。

用户已经明确确认以下产品边界：

- 所有 `switch_agent` 动作都采用“弱可见”策略
- 不显示 JSON，不显示 `switch_agent` 命令字样
- 统一复用现有“任务调度”卡片作为切换状态可视化载体
- secondary handoff 的 dispatch card 必须是可见的
- dispatch card 需要瘦身：整体高度更低，去掉首行 `Coordinator Transition` / `TRANSITION` 文案
- `coordinator_2` 仍必须保持普通聊天气泡式流式展示，不改回 thought 卡片
- future agents 也必须复用同一套 transition UI，而不是为每个 agent 单独发明新的切换展示协议

## Non-Goals

- 不把 `coordinator_2` 改成多条 assistant 气泡分裂式展示
- 不把 `switch_agent` 直接显示给用户作为正文内容
- 不为 future agents 引入新的顶层 SSE transport 协议
- 不把 `coordinator_2` 的可见推理重新折叠回 `agent_step.thought`
- 不在本轮引入 per-turn LLM 摘要器或上下文压缩新机制

## Product Decision

采用“控制动作隐藏化 + 通用 dispatch card + assistant 气泡分区渲染”的方案：

1. 所有 `switch_agent` 都被定义为控制平面动作，而不是用户正文。
2. 所有 agent 切换动作都必须由 backend 解析为结构化 handoff / transition 事件，不能依赖前端从 transcript 猜测。
3. 用户可见的切换反馈统一复用 dispatch card，但从“`coordinator_1` 专属路由卡片”升级为“通用 transition UI”。
4. assistant 气泡内部改为分区渲染：
   - `before_body`: 顶部 transition 区
   - `body`: `coordinator_2` reasoning / 正文
   - `after_body`: secondary handoff dispatch + 执行型 agent 步骤区
5. 当前轮 `coordinator_2 -> basic_agent` secondary handoff 采用 A-C-D-B 排序；future agents 也沿用同一 slot 语义。

## UX Decision

### User-visible model

从用户视角，一条 assistant 消息允许同时包含三类可见内容：

1. transition card
2. 普通聊天推理 / 正文
3. 执行型 agent 步骤卡片

它们不再被视为“同一组步骤的简单时间顺序展开”，而是被视为一个带 slot 的组合消息。

### Required order

当本轮执行路径为 `coordinator_1 -> coordinator_2 -> basic_agent` 时，assistant 消息内的可见顺序必须固定为：

1. `before_body`:
   - `coordinator_1` routing dispatch card
2. `body`:
   - `coordinator_2` reasoning fold
   - `coordinator_2` 已可见正文
3. `after_body`:
   - `coordinator_2` secondary handoff dispatch card
   - `basic_agent` 的 thought / tool_call / observation

当路径为 `coordinator_1 -> basic_agent` 时：

1. `before_body`:
   - `coordinator_1` routing dispatch card
2. `body`:
   - 可为空
3. `after_body`:
   - `basic_agent` 的步骤卡片

当路径为 `coordinator_1 -> coordinator_2 -> future_agent_x` 时：

1. `before_body`: routing dispatch
2. `body`: `coordinator_2` reasoning / 正文
3. `after_body`: handoff dispatch + `future_agent_x` 步骤卡片

### Dispatch card visual adjustments

通用 transition UI 继续复用现有 dispatch card 的主视觉语言，但必须做以下收敛：

1. 去掉顶部 `Coordinator Transition` / `TRANSITION` 文案，仅保留 `任务调度` 主标题。
2. 整体高度降低：
   - 减少 header padding
   - 减少 body padding
   - 收紧 live text 区域上下留白
3. 保留深色卡片底色、扫光效果、running/completed/error/aborted 状态。
4. secondary handoff card 与 routing card 使用同一组件，但文案根据 transition 类型切换。

### Visibility policy for `switch_agent`

所有 `switch_agent` 动作一律采用“弱可见”策略：

- 不显示 canonical JSON
- 不显示 `switch_agent` 字样
- 不显示“调用代理命令”等面向内部实现的措辞
- 只显示人话状态，例如：
  - `正在转交执行代理`
  - `已转交给 basic_agent 继续处理`
  - `已转交给 browser_agent 继续处理`

## Architecture Overview

### Core principle

本轮的核心不是“继续在更多地方做 regex 清洗”，而是正式区分两类输出：

1. **Control plane**
   - `switch_agent`
   - 其他 future agent switch actions
   - 只能进入结构化 timeline / dispatch / handoff 记录

2. **Visible plane**
   - 用户可见 reasoning
   - 用户可见正文
   - transition card 上的人话状态
   - 工具调用与观察卡片

控制平面动作不得再以原始文本进入 visible plane。

### High-level runtime flow

以 `coordinator_1 -> coordinator_2 -> basic_agent` 为例：

1. `coordinator_1` 发出 routing transition 的 running / delta / completed
2. 前端将其投影到 `before_body` dispatch card
3. `coordinator_2` 发出 `message_started/message_delta/message_completed`，前端将其投影到 `body`
4. `coordinator_2` 输出 `switch_agent` 控制动作时：
   - backend 识别并拦截
   - backend 从可见 transcript 中移除控制文本
   - backend 发出 structured secondary handoff dispatch
5. 前端将 secondary handoff dispatch 投影到 `after_body`
6. `basic_agent` 的 `agent_step` 继续追加到 `after_body`

## Canonical Event Model

### Transport rule

继续保留现有顶层 SSE `type`：

- `timeline_event`
- `agent_step`
- `content`
- `done`
- `error`
- `aborted`

本轮不新增新的顶层 transport family。

### Transition display contract

为了把 dispatch card 从 routing 专属升级为通用 transition UI，需要新增或正式化一套面向可见层的 generic dispatch contract。

建议的 timeline 事件族为：

- `dispatch_delta`
- `dispatch_state`

其中：

- `dispatch_delta` 负责卡片上的流式文本更新
- `dispatch_state` 负责 running / completed / error / aborted 等状态收束

推荐事件形态：

```json
{
  "type": "timeline_event",
  "event": {
    "kind": "dispatch_delta",
    "display": "dispatch_card",
    "message_id": "assistant-msg-run-42",
    "dispatch_id": "dispatch-run-42-routing",
    "dispatch_kind": "routing",
    "display_slot": "before_body",
    "source": "coordinator_1",
    "target_agent": "coordinator_2",
    "text": "正在分析是否需要切换执行代理",
    "partial": true
  }
}
```

```json
{
  "type": "timeline_event",
  "event": {
    "kind": "dispatch_delta",
    "display": "dispatch_card",
    "message_id": "assistant-msg-run-42",
    "dispatch_id": "dispatch-run-42-secondary",
    "dispatch_kind": "secondary_handoff",
    "display_slot": "after_body",
    "source": "coordinator_2",
    "target_agent": "basic_agent",
    "text": "正在转交执行代理",
    "partial": true
  }
}
```

```json
{
  "type": "agent_step",
  "step_type": "dispatch",
  "dispatch_kind": "secondary_handoff",
  "dispatch_status": "completed",
  "dispatch_target_agent": "basic_agent",
  "display_slot": "after_body"
}
```

### Compatibility rule

为减少一次性改动面，本轮兼容策略如下：

1. `agent_step(step_type="dispatch")` 继续保留，用于驱动 dispatch card 的实例化与状态合并。
2. 新前端 projection 优先消费 `dispatch_delta`。
3. 旧的 `routing_delta` 在迁移期可以继续被 frontend 接受为 fallback，但不再作为通用 transition UI 的长期语义基础。
4. `routing_started/routing_decision/routing_timeout_fallback` 等 behavior records 仍可保留，用于上下文编译和行为记录；但 dispatch card 的用户可见流应逐步统一到 `dispatch_*` display contract。

原因是：

- `routing_delta` 从名字上只适合 `coordinator_1` 首轮选路
- secondary handoff 与 future agents handoff 都不应继续强行套用 routing 语义

## Backend Design

### 1. Treat all `switch_agent` as hidden control actions

backend 必须把所有 agent switch 动作正式定义为 hidden control，不局限于 `coordinator_2 -> basic_agent`。

要求：

1. 任何可被 `_parse_switch_agent_command()` 识别的文本，都不能直接进入用户正文。
2. 无论其形式是：
   - canonical array
   - canonical object
   - 行内 control JSON
   - `Action:` / `动作:` 包裹形式
   - future agent switch payload
   都必须优先走 control-plane 解析。
3. 若解析成功：
   - 生成 structured handoff / transition event
   - 生成或更新 dispatch step
   - 从 chat bubble visible text 中剥离控制文本
4. 若解析失败但明显命中“控制命令前缀”：
   - 继续缓冲
   - 直到流式结束再决定是 false positive prose 还是 control action
5. 若为无效 `switch_agent` 命令：
   - 不把原始 JSON 暴露给用户
   - 以 runner error / dispatch error 处理

### 2. Introduce generic dispatch display emission

`coordinator_1` routing 与 `coordinator_2` secondary handoff 都要发出统一语义的 dispatch display 更新。

建议：

1. `coordinator_1`：
   - 继续保留 behavior-level `routing_*` 记录
   - 同时发出 display-level `dispatch_delta` 与 `dispatch` step 更新
   - `display_slot = before_body`

2. `coordinator_2` secondary handoff：
   - 在识别到 handoff 时发出 `dispatch_delta`
   - 在真正完成 handoff 激活时发出 `dispatch` completed step
   - `display_slot = after_body`

3. future agents handoff：
   - 遵守与 `coordinator_2` secondary handoff 相同的 display contract

### 3. Add slot metadata to dispatch and execution steps

所有用户可见 `agent_step` 都需要具备明确的渲染 slot。

推荐字段：

- `display_slot: "before_body" | "after_body"`

规则：

1. `coordinator_1` routing dispatch：`before_body`
2. `coordinator_2` secondary handoff dispatch：`after_body`
3. `basic_agent` / future execution agents 的 thought / tool_call / observation：`after_body`
4. 未显式标注的 legacy execution steps，在迁移期默认视为 `after_body`

### 4. Keep `coordinator_2` body stream pure

`coordinator_2` 的 `message_delta(display="chat_bubble")` 只负责两类内容：

1. 用户可见 reasoning
2. 用户可见正文

以下内容不得再走 chat bubble：

- `switch_agent` canonical JSON
- secondary handoff control transcript
- future agent switch control transcript

这样才能从语义上保证：

- C 只代表 `coordinator_2` 的可见思考 / 正文
- D 只代表切换状态
- B 只代表执行型 agent 步骤

### 5. Message and dispatch identity

为了避免 future reuse 再次混乱，identity 语义必须明确：

1. assistant 气泡 `message_id` 继续保持 run/turn 唯一。
2. dispatch card 需要独立的 `dispatch_id`，而不是只靠最后一个 dispatch step 猜测。
3. `dispatch_id` 也必须 run/turn 唯一，不能使用 session 级固定 id。

## Frontend Design

### 1. Split assistant message rendering into slots

assistant 消息渲染从当前的线性顺序：

1. `AgentStepDisplay`
2. `AssistantReasoningSection`
3. Markdown 正文

调整为：

1. `before_body` transition section
2. reasoning section
3. Markdown 正文
4. `after_body` transition + execution section

### 2. Keep one message, partition the visible lanes

本轮不拆成多条 assistant 消息。

同一 assistant message 继续承载：

- `coordinator_2` body stream
- secondary handoff dispatch
- `basic_agent` 步骤流

但渲染时要按 slot 分桶，而不是按 `agentSteps` 原始数组一次性整块渲染。

推荐做法：

1. `message.agentSteps` 继续保留为单数组，按时间追加。
2. 页面层按 `display_slot` 过滤成：
   - `beforeBodySteps`
   - `afterBodySteps`
3. 分别渲染两个 section。

### 3. Dispatch card reuse rules

`AgentStepDisplay` 中的 dispatch card 继续复用，但要升级为通用 transition card：

1. 不再硬编码 `Coordinator Transition` 文案
2. 文案由 `dispatch_kind + dispatch_status + dispatch_target_agent` 决定
3. running / completed / error / aborted 都继续沿用现有视觉状态机
4. 卡片默认高度收窄，适用于夹在 C 和 B 之间的 secondary handoff 场景

### 4. Copy rules

推荐 copy：

#### `dispatch_kind = routing`

- running: `正在分配执行代理`
- completed + direct answer: `已完成响应路径判定`
- completed + handoff: `已分配执行代理`

#### `dispatch_kind = secondary_handoff`

- running: `正在转交执行代理`
- completed: `已转交给 <agent> 继续处理`
- error: `执行代理转交失败`
- aborted: `执行代理转交已中止`

future agents 一律沿用 `secondary_handoff` 文案模板，仅替换目标 agent 名。

### 5. Projection rules

`timeline-projection.ts` 需要从“只认 `routing_delta` 的特殊逻辑”升级为“通用 transition display reducer”。

推荐规则：

1. `dispatch_delta(display="dispatch_card") -> dispatch_update`
2. 迁移期保留：`routing_delta -> dispatch_update` fallback
3. `message_delta(source="coordinator_2", display="chat_bubble") -> chat_chunk`
4. 其他 timeline event 默认 `noop`

### 6. Page state rules

text 页面与 vision 页面必须同步遵守以下规则：

1. optimistic routing placeholder 仍插入到当前 assistant message 的 `before_body`
2. secondary handoff dispatch 更新追加到同一 assistant message 的 `after_body`
3. `basic_agent` / future execution agent 的步骤也追加到同一 assistant message 的 `after_body`
4. 不允许 secondary handoff dispatch 覆盖 routing card
5. 不允许 `after_body` 的步骤写回到 `before_body`

## Testing Strategy

### Backend tests

新增或更新以下测试：

1. 所有可识别的 `switch_agent` 输出形式都不会进入 chat bubble visible text
2. `coordinator_2` 命中 secondary handoff 时：
   - 会发出 structured dispatch update
   - 会发出 `display_slot = after_body` 的 dispatch completed step
   - 后续 `basic_agent` 步骤也带 `display_slot = after_body`
3. `coordinator_1` routing dispatch 带 `display_slot = before_body`
4. `dispatch_id` 为 run/turn 唯一，而不是 session 固定 id
5. future agent switch path 若接入，同样不会把 control JSON 暴露给用户

### Frontend tests

新增或更新以下测试：

1. assistant message 在存在 `before_body` / `after_body` 时，渲染顺序固定为 A-C-D-B
2. text 与 vision 页面都按相同 slot 规则归属 dispatch 与 execution steps
3. `dispatch_delta` projection 能驱动同一 dispatch card 的流式文案更新
4. legacy `routing_delta` 在迁移期仍可被接收
5. secondary handoff 场景下，正文第一行不再出现 canonical switch JSON
6. dispatch card 样式已去掉 `Coordinator Transition` 文案并收窄高度

## Risks and Mitigations

### Risk 1: routing 与 secondary handoff 共用卡片后，状态合并串台

缓解：引入独立 `dispatch_id` 与 `display_slot`，避免用“最后一张 dispatch step”进行脆弱猜测。

### Risk 2: 只改前端顺序但未正式隐藏 control-plane，未来还会再次漏 JSON

缓解：把所有 `switch_agent` 都纳入 backend hidden-control contract，前端不再从 transcript 猜测切换动作。

### Risk 3: transition UI 通用化后，`coordinator_1` 现有 routing 行为回归

缓解：迁移期保留 `routing_delta` fallback，并为 routing path 补 dedicated regression tests。

### Risk 4: text / vision 页面再次分叉

缓解：两页同步修改，并增加 source-level parity tests。

## Acceptance Criteria

- dispatch card 不再是 `coordinator_1` 专属，而是通用 transition UI。
- 所有 `switch_agent` 动作都不再以 JSON / 命令文本形式展示给用户。
- `coordinator_2 -> basic_agent` secondary handoff 场景下，assistant message 的可见顺序稳定为 A-C-D-B。
- D（secondary handoff dispatch card）对用户可见，且位于 C 与 B 之间。
- dispatch card 去掉 `Coordinator Transition` / `TRANSITION` 顶部文案，并整体瘦身。
- `coordinator_2` 的可见推理继续保持普通聊天气泡语义，不退化为 thought 卡片。
- text 页面与 vision 页面行为一致。
- future agent switch actions 可直接复用同一套 transition UI 语义，而不需要再次发明新的展示协议。
