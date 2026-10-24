# Coordinator Dispatch Status Design

## Goal

为多 agent 路由阶段增加一个前端可见、后端可观测的“任务调度”过渡态，解决 coordinator 在本地大模型上推理较慢时用户误以为前端卡死的问题，同时不向前端暴露 coordinator 的原始推理文本。

## Background

当前 `AgentRunner.run_stream()` 在进入 coordinator 路由时，会先同步执行 `_run_coordinator_routing_pass()`，直到拿到 direct-answer 或 switch 结果后才开始发第一条 SSE。对于本地部署的慢速模型，这意味着用户在几十秒到一分钟内看不到任何流式反馈。虽然系统并未卡死，但体验上等同于“无响应”。

本项目已经确认以下产品边界：

- 前端不显示 coordinator 原始推理文本。
- 前端需要一个新的、独立于现有 thought 卡片的状态组件，名称固定为“任务调度”。
- “任务调度”卡片采用专门视觉样式，不与现有蓝色思考框混用。
- 后端终端需要完整打印 coordinator 推理文本用于调试。
- coordinator 的提示词应放入 `config/config-agent-prompts.yaml`，不再继续把路由提示硬编码在 `agent_runner.py` 中。
- 若 coordinator 决定切换到执行代理，则提示词中应显式约束内部推理文本尽量短，目标小于 100 字。

## Non-Goals

- 不在前端显示 coordinator 的原始 reasoning / content 文本。
- 不修改 `/v1/agent/chat` 的基础协议形态，不新增独立的 SSE 顶层事件族。
- 不改变现有 basic agent 的 thought / tool_call / observation 展示逻辑。
- 不引入新的 prompt 配置文件。

## Product Decision

采用“混合式调度阶段”方案：

- 前端立即显示本地生成的“任务调度”卡片，避免首帧前完全空白。
- 后端将 coordinator 路由阶段改为可流式观测的执行过程。
- coordinator 的原始推理文本只打印到后端终端，用于 debug。
- 前端只接收抽象化的调度状态事件，不消费原始推理文本。

## UX Design

### User-visible behavior

当用户发送消息且 agent mode 开启时：

1. 聊天区立即插入 assistant 占位消息。
2. 占位消息顶部立即出现“任务调度”卡片。
3. 卡片进入活跃态，使用已确认的视觉样式：
   - 深色卡片背景
   - 标题“任务调度”
   - 橙色圆点及呼吸灯外晕
   - 文本内局部扫光动画，扫光范围贴合文字包围盒
   - 三条状态文案
4. 当 coordinator 完成 direct-answer 或 handoff 后，卡片退出活跃态。
5. 若后续进入 basic agent 执行，则继续显示现有步骤流。
6. 若 coordinator 直接回答用户，则卡片结束后直接进入最终回答展示。
7. 若路由失败或会话中止，卡片显示错误/中止态，避免用户误判为卡死。

### Dispatch card copy

前端默认展示的三条状态文案固定为：

- 检查当前任务是否需要扩展执行能力
- 评估是否应切换到具体执行代理
- 正在建立后续执行上下文

这些文案由前端本地动画驱动，不依赖后端逐字流式推送。

### Naming

- 前端展示名固定为：`任务调度`
- 内部 step type 使用：`dispatch`

## Backend Design

### Coordinator prompt source

coordinator 路由提示词从 `backend/agent/agent_runner.py::_build_coordinator_turn_message()` 中移出，改为放入 `config/config-agent-prompts.yaml` 的 `coordinator_agent.prompt_templates` 内。

建议新增模板键：

- `routing_turn_prompt`

模板负责定义以下约束：

- 若可直接回答，则输出用户可见的自然语言回复。
- 若需要 handoff，则仅输出 canonical switch JSON array。
- 若选择 handoff，内部推理文本应尽量简短，目标少于 100 字。
- 不输出混合格式，不输出额外 prose，不提及其他 agent。

Python 侧只负责模板渲染与变量注入，不再拼接大段路由提示文本。

### Stream lifecycle

`run_stream()` 的 coordinator 路由阶段改为：

1. 先获取/准备 session。
2. 立即发 `session_start` SSE。
3. 立即发一条 `agent_step`，其中 `step_type="dispatch"`，状态为 `running`。
4. 启动 coordinator 流式推理。
5. coordinator 的流式输出不透传给前端，但会被写入后端日志。
6. 流式结束后，对最终聚合文本执行 `switch_agent` / final answer 分类。
7. 根据分类结果，发 `dispatch` 完成态并进入下一分支：
   - `direct_answer`
   - `switch`
   - `error`
   - `aborted`

### SSE shape

不新增新的顶层 SSE `type`。继续沿用现有：

- `type: "agent_step"`

新增 `step_type`：

- `dispatch`

新增 dispatch step payload 字段：

- `dispatch_status`: `running | completed | error | aborted`
- `dispatch_stage`: 可选，值为 `routing | handoff | direct_answer`
- `dispatch_target_agent`: 可选，仅在 handoff 成功时出现，例如 `basic_agent`
- `dispatch_message`: 可选，用于错误或状态提示

这样前端只需扩展 `AgentStep` 类型，不需要重写整个 SSE 消费器。

### Coordinator inference logging

后端终端默认完整打印 coordinator 推理文本，用于 debug。

要求：

- coordinator 流式阶段的中文日志必须可直接读，不出现 `\uXXXX`。
- 所有新增的 JSON 日志输出使用 `ensure_ascii=False`。
- 如果打印聚合文本或 delta 文本，也应保持原始中文可见。

## Frontend Design

### State model

扩展 `frontend/src/lib/api.ts` 中的 `AgentStep`：

- `step_type` 增加 `dispatch`
- 新增 dispatch 相关字段

前端页面在发送消息后，会本地插入一个 `dispatch` 步骤作为 optimistic placeholder。收到后端真实的 `dispatch` 事件后，用真实状态覆盖本地 placeholder，而不是重复追加第二张卡片。

### Rendering

`AgentStepDisplay` 增加 dispatch 专用渲染分支：

- 不复用 thought 卡片的蓝色配色
- 使用已确认的深色卡片视觉
- `running` 时展示呼吸灯和局部扫光
- `completed` 时静止收束
- `error` / `aborted` 时切换到相应状态样式

### Text and vision parity

`frontend/src/app/page.tsx` 与 `frontend/src/app/vision/page.tsx` 必须保持相同行为：

- 发送消息后都立即插入 `dispatch` placeholder
- 都能消费 dispatch step
- 都能在首次真实执行 step 或最终内容到来时结束 dispatch 活跃态

## Testing Strategy

### Backend tests

- `run_stream()` 在 coordinator 阶段必须先发 `session_start`
- `run_stream()` 在 coordinator 阶段必须发 `dispatch/running`
- coordinator direct-answer 路径必须发 `dispatch/completed` 后再发最终内容
- coordinator switch 路径必须发 `dispatch/completed` 后进入 basic stream
- coordinator invalid route / exception 路径必须发 `dispatch/error`
- abort 路径必须发 `dispatch/aborted`
- coordinator prompt 从 yaml 读取，而非硬编码
- 新增 prompt 文本中包含“切换时内部推理尽量短于 100 字”的约束

### Frontend tests

如现有测试基础不足，可先用 focused unit/state tests 覆盖：

- dispatch step 的类型消费与字段映射
- partial thought 到来后不会错误覆盖 dispatch step
- text / vision 页面状态更新逻辑一致

## Risks and mitigations

### Risk 1: coordinator 改为流式后分类逻辑退化

缓解：复用现有 `LocalLLMModel.generate_stream()` 的聚合思路，在流式完成后继续基于聚合文本执行 `_parse_switch_agent_command()` 与 final/direct-answer 分类。

### Risk 2: 前端出现重复 dispatch 卡片

缓解：页面状态逻辑中将 dispatch 视为单实例 placeholder，收到真实 dispatch 事件时优先更新最后一张 dispatch step，而不是追加。

### Risk 3: text / vision 两页再次分叉

缓解：两边同时修改，并在实现与验证中显式检查两页逻辑同步。

### Risk 4: 中文日志再次被转义

缓解：对新增 `json.dumps()` 全部显式使用 `ensure_ascii=False`；涉及 coordinator 推理日志的字符串输出尽量直接打印原文。

## Acceptance Criteria

- 用户发送消息后，前端立即出现“任务调度”卡片，不再存在长时间完全无反馈的空窗。
- coordinator 原始推理文本不出现在前端，但完整出现在后端终端。
- 后端终端中的中文推理/状态日志不出现 `\uXXXX` 转义文本。
- coordinator 路由提示词来自 `config/config-agent-prompts.yaml`。
- coordinator handoff 场景下，提示词明确要求内部推理文本尽量短，目标低于 100 字。
- text 页面和 vision 页面都具备一致的任务调度过渡体验。
