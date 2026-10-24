# Three-Bubble Secondary Handoff Design

## Goal

把当前 `coordinator_1 -> coordinator_2 -> basic_agent` 路径中的 secondary handoff，从“第二个 assistant 气泡内部继续承载执行型 agent 步骤”的 A-C-D-B 同气泡模型，升级为真正的三气泡模型：

1. 气泡 1：`coordinator_1` routing transition UI
2. 气泡 2：`coordinator_2` 可见推理 / 正文 + secondary handoff transition UI
3. 气泡 3：`basic_agent` 或 future execution agent 的步骤卡片

核心要求是：`switch_agent` canonical JSON 永不进入用户可见正文；`coordinator_2` 已经流出的可见推理必须保留；handoff 卡片展示完后立即结束第二气泡；第三气泡专属于被切换的执行型 agent。

## Background

上一轮 generic transition UI 已完成两件事：

1. `switch_agent` 大部分路径已经不会直接作为 final answer 文本落到用户正文里。
2. secondary handoff 已经能显示第二张 transition UI 卡片。

但当前实现仍保留了一个与产品目标不一致的结构性问题：

- `basic_agent` 的工具步骤仍然继续追加到 `coordinator_2` 同一个 assistant 气泡中。

这会带来两个产品层面的缺陷：

1. 用户一眼看不出“`coordinator_2` 已经结束，后面是另一个 agent 在执行”。
2. 即使控制 JSON 被隐藏了，handoff 的生命周期仍然是“同气泡延长”，而不是“结束一个 agent，再开启下一个 agent 的新气泡”。

用户已经明确确认新的目标语义：

- `coordinator_2` 的已流出推理 / 正文要保留在第二气泡中。
- `switch_agent` 不得以 JSON、命令文本、类似 control transcript 的形式出现在第二气泡 final answer 中。
- secondary handoff 时第二气泡应显示 transition UI 卡片。
- transition UI 卡片显示后，第二气泡立即结束。
- 后端随后开启第三个气泡，供 `basic_agent` / future agent 继续执行。

## Non-Goals

- 不退回到“把 `coordinator_2` 变成 thought 卡片”的旧渲染语义。
- 不在 `[`、`{` 等早期流式片段出现时就打断 `coordinator_2` 正文流。
- 不把 secondary handoff 重新并回第二气泡的 `after_body` 执行步骤区。
- 不为 future agents 发明新的 SSE 顶层 transport family。
- 不在本轮引入新的上下文压缩 / LLM 摘要机制。

## Product Decision

采用“hidden control + second-bubble transition + explicit third-bubble handoff”的方案：

1. `switch_agent` 仍是 hidden control output，不属于用户正文。
2. `coordinator_2` 的 visible plane 只包含：
   - 已流出的 reasoning
   - 已流出的正文
   - secondary handoff transition UI
3. 一旦 secondary handoff 被识别并展示 transition UI：
   - 第二气泡结束
   - 后端 / 前端联合开启新的第三气泡
   - 后续 execution agent steps 只能进入第三气泡
4. future agents 进入时，沿用完全相同的“三气泡 handoff”生命周期，不允许回退到同气泡继续追加。

## UX Model

### Required visible order

对于 `coordinator_1 -> coordinator_2 -> basic_agent`：

1. 气泡 1：routing card
2. 气泡 2：
   - `coordinator_2` reasoning / 正文
   - secondary handoff transition UI
3. 气泡 3：
   - `basic_agent` thought / tool_call / observation

### Visibility policy

所有 `switch_agent` 一律：

- 不显示 canonical JSON
- 不显示 `switch_agent` 字样
- 不显示 `Calling tools:` / `Action:` 等控制 transcript
- 仅显示人话 transition 文案，例如：
  - `正在转交执行代理`
  - `已转交给 basic_agent 继续处理`

### Bubble ownership rule

1. routing card 只属于气泡 1。
2. `coordinator_2` reasoning / body 和 handoff card 只属于气泡 2。
3. `basic_agent` / future execution agent 的步骤只属于气泡 3。
4. 一个 execution agent 的步骤不得回写到 `coordinator_2` 气泡。

## Architecture Overview

### Core principle

这轮的核心变化不是“继续赌 backend parser 永不漏出”，而是正式把 handoff 建模为 message lifecycle boundary，并给前端补上已落入 bubble 文本的回撤能力：

1. `coordinator_2` bubble 有明确结束点。
2. secondary handoff card 是第二气泡的最后一个可见元素。
3. execution agent 拥有新的 assistant bubble identity。
4. 若 canonical `switch_agent` JSON 已经因流式时序进入 bubble 2，frontend 可以在 handoff 确认后回撤该尾部控制文本，再由 transition card 替代其语义位置。

### High-level runtime flow

以 `coordinator_1 -> coordinator_2 -> basic_agent` 为例：

1. `coordinator_1` routing 卡片流式更新并结束气泡 1。
2. `coordinator_2` 开启气泡 2，流式输出 reasoning / 正文。
3. 若 `coordinator_2` 最终决定 `switch_agent`：
   - backend 在 control plane 识别并拦截 canonical JSON
   - 不把控制文本发送到 chat bubble
   - backend 发出 secondary handoff transition 更新
   - 前端把 transition card 渲染到气泡 2
4. transition card 收束后：
   - 气泡 2 结束
   - 系统创建气泡 3
5. `basic_agent` 的步骤流只追加到气泡 3。

## Canonical Event Model

### Existing transport kept

继续使用现有顶层 SSE `type`：

- `timeline_event`
- `agent_step`
- `content`
- `done`
- `error`
- `aborted`

本轮不新增新的顶层 transport family。

### Required timeline semantics

#### Bubble 2 (`coordinator_2`)

仍使用：

- `message_started`
- `message_delta`
- `message_completed`

但语义改为：

- `message_completed` 必须发生在 secondary handoff card 已经可见之后。
- `message_completed` 不得早于 second-bubble handoff card 的完成时刻。

#### Handoff card

secondary handoff 继续使用：

- `dispatch_delta`
- `agent_step(step_type="dispatch")`

但其归属 message 语义必须绑定到 bubble 2，而不是 bubble 1。

#### Bubble 3 creation

execution bubble 不强制要求 backend 发新的专属 transport；但系统必须有一个明确、结构化的 boundary 触发第三气泡创建。

Phase 1 允许两种实现：

1. backend 发出新的 bubble-level timeline event 再由 projection reducer 创建第三气泡
2. 前端根据结构化 `agent_switched(source="coordinator_2")` 创建第三气泡占位消息

无论采用哪种实现，都必须满足：

- 第三气泡的 identity 与第二气泡不同
- 后续 execution steps 只能进入第三气泡
- 不依赖解析 raw visible transcript

## Backend Design

### 1. Keep `switch_agent` hidden in all finalization paths

backend 必须继续在以下路径中兜底：

1. 流式 chunk 级检测
2. action flush 阶段检测
3. `_pending_final_content` 最终收束检测

任何命中的 `switch_agent` canonical JSON 或同类 control transcript：

- 不进入 `message_delta(display="chat_bubble")`
- 不进入 `content`
- 只转成 structured handoff signal

但需要明确：在真实流式 UI 中，backend parser 相对于已经落屏的前端 token 可能天然慢几步；因此 backend hidden-control parser 只作为第一层防线，不能再被视为“只靠后端就足以 100% 防泄漏”的唯一机制。

### 2. Secondary handoff card must complete before bubble 2 completes

当前 `_coordinator_stream_secondary_handoff()` 在部分路径中会先转发 `message_completed`，再发 completed dispatch step。新方案要求调整为：

1. 若已捕获 secondary handoff：
   - 先发 handoff dispatch 更新 / completed step
   - 再发 bubble 2 的 `message_completed`
2. 如果 trailing terminal event 是 `aborted` / `error`：
   - 不得错误地开启 bubble 3

### 3. Bubble 3 must start after bubble 2 closes

当 `_activate_secondary_handoff()` 完成后，execution agent 的后续流不得继续复用 bubble 2 的 tracking id。

系统必须显式建立“execution bubble 已切换”的边界。最小实现可由前端基于 `agent_switched(source="coordinator_2")` 创建新占位 bubble，但 backend 仍需保证事件顺序：

1. bubble 2 handoff card
2. bubble 2 `message_completed`
3. `agent_switched(source="coordinator_2", target=<execution_agent>)`
4. execution steps

### 4. Dispatch identity remains explicit

继续保留：

- `dispatch_id`
- `dispatch_kind`
- `display_slot`

但 secondary handoff card 的目标 message 语义从“第二气泡的 after_body lane”提升为“第二气泡的 terminal transition lane”。

## Frontend Design

### 1. Bubble 1 and bubble 2 remain distinct

当前前端已经能在 `coordinator_2 message_id != assistantMessageId` 时创建新的 projected bubble。该能力继续保留：

- routing placeholder / bubble 1：本地占位 assistant
- `coordinator_2` bubble / bubble 2：由 `message_delta(message_id=...)` 创建

### 2. Add explicit execution-bubble tracking

当前 `getActiveTrackedAssistantMessageId()` 只会优先返回最后一个 projected assistant message id，导致 `basic_agent` steps 仍落回 bubble 2。

新方案要求新增一层显式 tracking state，例如：

- `executionAssistantMessageId`
- 或 `activeAssistantMessageId`

规则：

1. 默认 active bubble 是 bubble 1。
2. 当 `coordinator_2` projected bubble 出现时，active bubble 切到 bubble 2。
3. 当 secondary handoff 完成并收到 `agent_switched(source="coordinator_2")` 时：
   - 创建 bubble 3 占位消息
   - active bubble 切到 bubble 3
4. 后续 `onStep` / `onContent` / finalize 都走 active bubble，而不是盲目回退到最后一个 projected bubble。

### 3. Add explicit bubble-tail retraction after confirmed secondary handoff

当前 frontend 的 projected chat path 只有“append content”能力：`message_delta` 一旦经由 projection 写入 `messages[].content`，后续 dispatch card 无法再替换这段文本。

因此本轮必须新增一个共享 helper，用于在 **secondary handoff 已确认之后** 回撤 bubble 2 尾部已写入的 canonical `switch_agent` JSON。

约束：

1. helper 必须是通用 message-level 能力，而不是散落在页面组件里的临时正则。
2. helper 只处理 **assistant bubble 尾部** 的文本，不扫描整个正文历史中间位置。
3. helper 只在尾部存在“完整、平衡、canonical、并同时命中 `type":"agent"` 与 `switch_agent`”的 JSON span 时回撤。
4. helper 不得在看到单独的 `[` / `{` 时提前动作，避免打断数学公式、普通 JSON 示例或一般 prose。
5. helper 只在收到 structured secondary handoff signal 后触发；不在每个 `message_delta` token 上实时猜测。

推荐 helper 形态：

- `stripTrailingCanonicalSwitchAgentControl(text)`
- `retractTrackedAssistantTailText(messages, messageId, retractor)`

其中：

- `stripTrailingCanonicalSwitchAgentControl(text)` 负责返回 `strippedText` 与是否发生回撤。
- `retractTrackedAssistantTailText(...)` 负责按 tracked assistant `messageId` 更新消息数组。

### 4. Bubble 2 render contract changes

bubble 2 继续渲染：

- reasoning section
- Markdown body
- secondary handoff card

但不再承载 execution steps，且在 handoff 确认后允许回撤尾部 canonical `switch_agent` JSON。

### 5. Bubble 3 render contract

bubble 3 默认没有正文，主要承载：

- thought cards
- tool call cards
- observation cards
- future execution-oriented agent steps

若 execution agent 后续确有 `content`，也只能进入 bubble 3。

### 6. Handoff-triggered frontend order

对 `coordinator_2 -> basic_agent` 的 frontend 实际处理顺序要求如下：

1. `coordinator_2` 的 `message_delta` 正常追加到 bubble 2。
2. 一旦 structured `secondary_handoff` / `agent_switched(source="coordinator_2")` 边界被确认：
   - 先对 bubble 2 执行尾部回撤
   - 再确保 secondary handoff dispatch card 留在 bubble 2
   - 再创建 bubble 3 execution placeholder
3. 之后的 execution steps / content 只进入 bubble 3。

该顺序保证：

- 不提前打断 `coordinator_2` 的流式正文
- 已经落进 bubble 2 的 canonical switch JSON 可被移除
- transition card 真正取代被回撤的控制文本位置

### 7. Page-state parity

text / vision 页面必须同步实现：

- active bubble tracking
- third-bubble placeholder creation
- second-bubble tail retraction helper 调用
- second-bubble finalize timing
- stop / answer-now / abort 时对 bubble 2 与 bubble 3 的一致收尾

## Testing Strategy

### Backend regressions

新增或更新测试覆盖：

1. `coordinator_2` secondary handoff 时，`switch_agent` JSON 不进入 visible chat bubble。
2. second-bubble handoff card 在 `message_completed` 之前已经出现。
3. `message_completed` 发生在 `agent_switched(source="coordinator_2")` 之前。
4. trailing abort / error 时不会启动 bubble 3 对应的 execution stream。

同时允许清理那些只为多轮 parser 缝补而引入、但在“frontend confirmed-handoff retraction”方案下已不再承担主行为验证价值的死测试；前提是保留 structured handoff 顺序和 hidden-control 基础兜底的核心回归。

### Frontend regressions

新增或更新测试覆盖：

1. 第二气泡和第三气泡使用不同 assistant message id / tracking state。
2. `agent_switched(source="coordinator_2")` 会创建新的 execution bubble 占位消息。
3. 后续 `basic_agent` steps 只会进入第三气泡。
4. 第二气泡仍保留已流出的 `coordinator_2` reasoning / 正文与 transition card。
5. handoff 确认后，第二气泡尾部 canonical `switch_agent` JSON 被回撤。
6. 数学公式、普通 `[`、普通 JSON 示例不被误回撤。
7. text / vision 页面保持完全一致。

## Risks and Mitigations

### Risk 1: 第二气泡未及时结束，第三气泡步骤仍串到旧 bubble

缓解：引入显式 active bubble tracking，而不是继续复用 `projectedAssistantMessageIds` 的“最后一个 id”启发式。

### Risk 2: secondary handoff card 因事件顺序问题出现在 `message_completed` 之后

缓解：调整 `_coordinator_stream_secondary_handoff()` 的缓冲与释放顺序，确保 dispatch step 先于 bubble completion。

### Risk 3: stop / answer-now 只 finalize 一个 bubble，留下另一个 bubble 悬挂

缓解：收尾逻辑按 tracked bubble ids 全量 finalize，而不是只 finalize 初始 placeholder。

### Risk 4: 前端尾部回撤误伤数学公式或普通 JSON 示例

缓解：只在 structured handoff 已确认后触发回撤，且只处理尾部完整平衡 span，并要求同时命中 `type":"agent"` 与 `switch_agent`。

## Acceptance Criteria

- `coordinator_1` routing 仍显示在独立的第一个气泡中。
- `coordinator_2` 的 reasoning / 正文显示在第二个气泡中。
- secondary handoff card 显示在第二个气泡中，且在第二个气泡结束前可见。
- 第二个气泡结束后，execution agent 使用第三个新气泡继续输出。
- 如果 canonical `switch_agent` JSON 曾短暂进入第二气泡流式正文，handoff 确认后它会被前端回撤，不留在最终 UI 中。
- `switch_agent` canonical JSON 不会出现在任何用户可见正文中。
- `basic_agent` / future execution agent 的步骤不会再追加到第二气泡中。
- text 页面与 vision 页面行为一致。
