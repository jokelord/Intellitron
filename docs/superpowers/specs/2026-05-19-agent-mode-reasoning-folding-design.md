# Agent-Mode Reasoning Folding Design

## Goal

为 agent 模式下的 assistant 聊天气泡补齐与非 agent 模式一致的 reasoning/thinking 折叠体验，重点覆盖 `coordinator_2` 的聊天气泡流。

目标行为如下：

- `showReasoning=true` 时：
  - 若模型输出推理内容，UI 显示一个默认展开的推理折叠组件。
  - 推理内容在折叠组件内实时流式更新。
  - 主回答区只显示最终 answer，不重复显示推理正文。
- `showReasoning=false` 时：
  - 若模型输出推理内容，UI 仍创建同一个推理折叠组件，但默认收起。
  - 推理内容继续实时流式累积，只是默认不在屏幕上展开显示。
  - 用户手动展开后，可以看到已经累积的推理内容和后续流式更新。
  - 流式完成后，主回答区折叠为“只剩最终答案”。

## Scope

- 仅修改 frontend projection / reducer / render 层。
- 仅为 assistant 聊天气泡增加 reasoning split/folding。
- text 页面与 vision 页面必须保持同一语义与同一共享实现。
- 仅覆盖两类“常规聊天气泡”路径：
  - 非 agent 模式 assistant 聊天气泡
  - agent 模式下 `coordinator_2` 的 assistant 聊天气泡

## Non-Goals

- 不修改 backend SSE 协议。
- 不修改 canonical timeline event 结构。
- 不修改 `coordinator_2` 使用普通聊天气泡流式展示的产品决策。
- 不修改 `basic_agent` 的 `AgentStepDisplay` 思考、工具、观察卡片语义。
- 不引入新的 agent-mode 专属设置开关；继续复用 `showReasoning`。
- 不修改 `coordinator_1` routing-only overrides、timeout fallback、CLI gating、已有去重修复。
- 不把 `basic_agent` 思考/观察卡片中的 think 标签改造成折叠聊天气泡；它们继续按当前组件框语义展示。

## Existing Problem

当前非 agent 模式下，普通聊天流可以通过 `reasoning_content` 独立渲染推理内容，并由 `showReasoning` 控制显隐；agent 模式下，`coordinator_2` 的聊天气泡只通过 `timeline_event -> projectTimelineEvent() -> chat_chunk -> appendProjectedChatChunk()` 将文本直接追加到 `message.content`。

这导致：

- 若模型使用 `reasoning_content` 字段之外的路径输出推理（典型如 Qwen3.5 的 `<think>...</think>`），agent 模式 assistant 气泡无法折叠推理。
- 若直接把 `<think>...</think>` 原样塞进正文，`showReasoning` 不再具备“默认展开/收起推理”的能力。
- 若在错误层面修改（backend/SSE/schema），容易回归已经稳定的 `coordinator_2` 聊天气泡、去重、stop/answer-now 等能力。

## Design Decision

采用 **Projection-time incremental split**：

- 保持 backend 与 SSE 完全不变。
- 在前端共享 reducer/helper 中，对 agent 模式下 `coordinator_2` 的 `chat_chunk` 做增量解析。
- 解析器实时把 `<think>...</think>` 包裹的内容拆到 `reasoning_content`。
- 剩余正文拆到 `content`。
- 折叠组件只由 UI 层控制默认展开/收起，不影响数据累积。
- `<think>...</think>` fallback 解析只针对消息开头的 leading reasoning block；一旦可见 answer 已开始，后续出现的 `<think>` 一律按普通正文处理。

该方案的主要优势：

- 改动面最小，不触碰后端协议和稳定链路。
- 能处理 `<think>` / `</think>` 被分片拆开的流式 chunk。
- text / vision 可复用同一套 helper，避免双份逻辑漂移。
- 最终 `message.content` 只保留 answer，有利于保持当前 `coordinator_2` 去重逻辑稳定。

## Design Overview

### Data model

assistant message 在前端保留以下字段：

- `content?: string`
- `reasoning_content?: string`
- `reasoningCollapsed?: boolean`
- `reasoningDetected?: boolean`
- `reasoningParseState?: 'outside' | 'inside_think'`
- `reasoningBuffer?: string`

其中：

- `content` 表示主回答区可见正文。
- `reasoning_content` 表示可折叠推理区内容。
- `reasoningCollapsed` 是单条消息的 UI 状态；其初始值由全局 `showReasoning` 派生，但后续可被用户手动切换。
- `reasoningDetected` 用于判定当前消息是否需要渲染折叠组件。
- `reasoningParseState` / `reasoningBuffer` 仅用于流式增量解析，不参与后端协议。

### Parsing boundary

增量解析只发生在共享 helper 中，不在页面组件里做临时字符串判断。

推荐新增一个小型共享 helper，例如：

- `frontend/src/lib/assistant-reasoning.ts`

或者在 `frontend/src/lib/dispatch-message.ts` 中内聚实现，但必须保证 text / vision 两页共用。

### Parsing algorithm

解析器按 chunk 增量处理，目标是正确应对以下情况：

- 单个 chunk 中同时出现 `<think>reasoning</think>answer`
- `<think>` 被拆成多段，例如 `<thi` + `nk>`
- `</think>` 被拆成多段，例如 `</th` + `ink>`
- 推理区跨多个 chunk 连续流出
- 完全没有 think 标签的普通文本
- answer 已开始后正文里出现字面量 `<think>...</think>` 的普通文本

解析原则：

1. 维护 `reasoningParseState` 和 `reasoningBuffer`。
2. `outside` 状态下，未进入 `<think>` 前的文本追加到 `content`。
3. 进入 `<think>` 后，文本追加到 `reasoning_content`。
4. 直到识别到完整 `</think>` 才回到 `outside`。
5. 若标签被 chunk 边界截断，暂存到 `reasoningBuffer`，等待下一个 chunk 补齐后再解析。
6. `<think>...</think>` fallback 只识别消息开头的 leading reasoning block；若可见 answer 已经开始，后续出现的 `<think>` 视为普通正文。
7. 对完全不含 think 标签的消息，退化为当前行为：全文进入 `content`。

### UI behavior

当 assistant 消息首次检测到推理内容时：

- `reasoningDetected = true`
- `reasoningCollapsed = !settings.showReasoning`

渲染行为：

- `showReasoning=true`
  - 推理折叠区默认展开。
  - 推理流式更新立即可见。
- `showReasoning=false`
  - 推理折叠区默认收起。
  - 推理内容继续累积，但默认不展开。
  - 用户展开后，可看到历史已累积内容和后续流式更新。

主回答区行为：

- 始终只显示 `content`。
- 当推理仍在流式、answer 尚未开始时，主回答区继续沿用当前空态语义，而不是把推理正文回灌到正文区。
- 流式完成后，若存在 answer，则主回答区显示最终 answer；若没有 answer，则保留当前空态/停止/错误语义，不额外发明新展示协议。

### User interaction

assistant 聊天气泡新增一个 reasoning 折叠组件：

- 对检测到推理的消息渲染。
- 默认展开/收起由 `showReasoning` 控制。
- 用户可单独切换当前消息的展开状态。
- 折叠组件状态仅影响展示，不影响推理数据继续流式累积。

该组件只用于“常规 assistant 聊天气泡”。

- 非 agent 模式：适用于普通 assistant 聊天气泡。
- agent 模式：仅适用于 `coordinator_2` 的普通聊天气泡。
- `basic_agent` 的 thought / observation / tool card 不接入这个折叠组件。
- 若 `basic_agent` 的 thought 内容自身包含 think 标签，也继续留在现有思考卡片中显示，不做额外折叠改造。

## Compatibility Constraints

本功能是锦上添花，不允许影响已稳定的重要能力。必须满足：

1. 不改变 backend SSE payload。
2. 不改变 `timeline_event` 投影入口与 `coordinator_2` 聊天气泡生命周期。
3. 不改变 per-run assistant `message_id` 绑定语义。
4. 不改变 stop / error / answer-now 的 finalize 路径。
5. 不改变 `basic_agent` 的 thought / observation / tool card。
6. 不改变 `coordinator_2` 已修复的最终答案去重逻辑。

特别要求：

- 最终 `message.content` 必须稳定为 answer-only，而不是 `reasoning + answer` 混合值。
- 但在流式过程中，reasoning parser state 不能因 finalize/stop/error 被清空到无法回看；否则用户展开时会看到残缺 reasoning。

## Affected Files

- `frontend/src/lib/dispatch-message.ts`
  - 调整 `appendProjectedChatChunk()` 或把其委托给新的 reasoning helper。
- `frontend/src/lib/answer-now-ui.ts`
  - 如有必要，仅微调 assistant 空态文本判定，确保“只有推理、无 answer”时不错误展示正文。
- `frontend/src/app/page.tsx`
  - assistant bubble 渲染新增 reasoning 折叠组件；继续复用共享 helper。
- `frontend/src/app/vision/page.tsx`
  - 同步同一渲染语义，不允许复制分叉实现。
- `frontend/src/lib/timeline-projection.ts`
  - 原则上不需要改协议；仅当需要极小类型扩展时才改，且不得改变现有 `chat_chunk` 契约。

## Testing Strategy

### Unit tests

为共享 parsing helper 添加测试，至少覆盖：

- 单 chunk：`<think>abc</think>final`
- 多 chunk：`<thi` + `nk>abc` + `</th` + `ink>final`
- 只有 reasoning、尚无 answer
- 无 think 标签时全文进入 `content`

### Projection / reducer tests

覆盖 agent 模式 `coordinator_2` 的 `chat_chunk` 处理：

- `reasoning_content` 正确累积
- `content` 仅保留答案部分
- 默认折叠状态与 `showReasoning` 一致

### UI regression tests

至少覆盖：

- text / vision 渲染语义一致
- `showReasoning=true` 时默认展开
- `showReasoning=false` 时默认收起，但用户展开后可看到已流出的 reasoning

### Protection regressions

必须验证不回归：

- `coordinator_2` 去重修复
- `basic_agent` thought card
- stop / error / answer-now finalize 行为

## Rollout Notes

- 该功能优先遵循“最小正确改动”，不为 future agents 预埋额外协议。
- 若未来后端原生提供 agent-mode `reasoning_content` 字段，可在保持当前 UI 契约的前提下，把 parser 降级为兜底逻辑。
- 在当前阶段，`<think>...</think>` 增量解析是为兼容 Qwen3.5 默认推理输出而加的前端兜底层。

## Verification

实现完成后，至少重新执行与本功能直接相关的前端测试与类型检查，并补跑本分支的关键回归基线，重点关注：

- timeline projection tests
- text / vision 页面一致性
- `npm run type-check`
- `npm run build`
- 与 `coordinator_2` 去重、stop/answer-now 相关的 backend/frontend 回归片段
