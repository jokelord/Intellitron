# Multi-Agent Backend Design

> 日期: 2026-05-10
> 状态: 待评审

## 1. 概述

### 1.1 背景

当前仓库的 Agent 后端是单 `SmartToolCallingAgent` 架构：

1. 一个 session 对应一个 agent 实例
2. 一个 agent 实例维护一份 memory
3. 一套统一 prompt 决定角色设定、工具披露和行为规则
4. 最终答复继续沿用本项目现有“纯文本自动视为 final answer”的机制

这种架构已经能完成单 agent 工具调用，但不利于后续做“渐进式提示词披露”和“按 agent 模式精细维护上下文”。用户希望先落地一个最小可扩展版本：

1. `coordinator_agent`
2. `basic_agent`

这里的“多 agent”在本期并不是多个 agent 并行协作执行，而是：

1. 用不同 agent profile 渐进披露系统提示词
2. 用不同 agent mode 控制当前回合可见的工具集合
3. 为未来 `explorer_agent`、`auditor_agent`、`spawn/fork context` 预留稳定的后端状态机与配置结构

### 1.2 本期目标

本期目标是在**不修改现有 `/v1/agent/chat` 对外接口**的前提下，把单 agent 后端升级为“每轮先由 `coordinator_agent` 接待，再按需切到 `basic_agent`”的多 agent 路由系统，并同时兼容 text 与 vision 两条 agent 链路。

必须满足：

1. 每轮用户新消息都先经过 `coordinator_agent`
2. `coordinator_agent` 不调用任何工具
3. `coordinator_agent` 的 prompt 中**只提自己和 `basic_agent`**，不提 `explorer_agent` / `auditor_agent`
4. `basic_agent` 负责披露基础工具与 skills 的使用规则
5. session 继续使用**单 session + 主记忆共享 + 当前 agent 临时切换**模式
6. 主 memory 持久保存原始用户消息，不保存 coordinator 的包装文本作为历史主输入
7. 现有 final answer 适配机制不能回归
8. 现有 `/v1/agent/chat`、`/v1/agent/stop`、`/v1/agent/answer-now` 行为语义保持兼容
9. vision 模式下，`vlm_instructions` 仅追加给 `coordinator_agent`

### 1.3 本期非目标

本期明确不做：

1. 不实现 `explorer_agent` / `auditor_agent` 的真实运行能力
2. 不让 `coordinator_agent` 在 prompt 中披露 `explorer_agent` / `auditor_agent`
3. 不实现 `spawn` 子上下文
4. 不实现 `fork` 分支上下文
5. 不新增新的 multi-agent prompt 配置文件
6. 不重构前端交互协议
7. 不要求前端立即增加多 agent 专属 UI
8. 不把“模型跨 agent 记住旧工具定义”作为系统正确性的依赖前提

### 1.4 核心决策

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 运行时架构 | 单 session + 共享主 memory + 当前 agent 临时切换 | 最符合本期范围，侵入最小 |
| 每轮入口 | 固定从 `coordinator_agent` 进入 | 满足用户要求，路由边界清晰 |
| prompt 配置落点 | 继续使用 `config/config-agent-prompts.yaml` | 用户明确要求不新增文件 |
| agent prompt 结构 | `coordinator_agent` / `basic_agent` / `explorer_agent` / `auditor_agent` 同层级 | 满足后续扩展和当前占位需求 |
| vision 附加指令 | 仅追加到 `coordinator_agent` | 满足用户明确要求 |
| `switch_agent` 协议 | prompt 教一种 canonical 格式，后端解析尽量兼容多种写法 | 提高模型稳定性并保留容错 |
| 前端协议 | 默认保持不变 | 降低改动面，优先完成后端多 agent 核心能力 |
| 工具暴露策略 | coordinator 无工具，basic 暴露当前核心工具集合 | 兼顾新架构和现有能力不回归 |

## 2. 用户故事与成功标准

### 2.1 用户故事

1. 作为普通用户，当我提出简单闲聊或简单知识问答时，系统应直接回答，而不进入工具链。
2. 作为普通用户，当我提出文件读写、搜索、执行命令、复杂技能类任务时，系统应自动切到更合适的 agent 再完成任务。
3. 作为多轮对话用户，我期望历史上下文持续可用，但不希望 coordinator 的内部包装 prompt 污染后续主记忆。
4. 作为 vision 用户，我期望多 agent 改造后 `/vision` 页面仍然可工作，不因为路由层重写而丢失图片理解能力。
5. 作为开发者，我期望在不改前端主接口的情况下扩展 agent 体系，并给后续 `explorer_agent` / `auditor_agent` 留出落点。

### 2.2 成功标准

以下条件全部满足时，本期目标视为达成：

1. 同一 session 的每个新用户回合都从 `coordinator_agent` 入口开始
2. 对简单问答，coordinator 可直接给最终答案
3. 对工具任务，coordinator 可输出 `switch_agent` 命令并切到 `basic_agent`
4. 切到 `basic_agent` 后，现有工具链、stream/sync、final answer 机制正常工作
5. 主 memory 中不保留 coordinator 包装后的“加工文本”作为用户历史主消息
6. 历史 memory 中保留的是原始用户消息、必要的工具轨迹与最终答复
7. `config/config-agent-prompts.yaml` 已完成多 agent 化重构
8. text 与 vision 均至少通过一组端到端 smoke 测试

## 3. 总体架构

### 3.1 术语

1. **agent profile**：某个 agent 的角色提示词、工具可见性策略、运行时能力边界的组合。
2. **current agent**：当前正在代表 backend 与 LLM 对接的 agent profile。
3. **shared main memory**：同一 session 下共享的主记忆，用于保留用户原始消息、已执行工具轨迹和最终答复。
4. **routing pass**：coordinator 的轻量级路由判定过程，只决定“直接回答”还是“切换 agent”。
5. **switch command**：LLM 输出的切换 agent 命令，后端识别后切换当前 agent。

### 3.2 总体思路

本期采用：**单 session、共享主 memory、每轮临时切换 current agent mode**。

它不是“一个 session 内常驻多个彼此独立的 smolagents 实例”，而是：

1. session 内只有一份主 memory
2. 当前回合先激活 `coordinator_agent`
3. 若需要切换，则把 active profile 切到 `basic_agent`
4. 切换后继续使用同一份主 memory 完成本轮任务
5. 下一轮用户发新消息时，再重新从 `coordinator_agent` 进入

### 3.3 为什么不采用“多个独立 agent 实例长期并存”

如果让 `coordinator_agent` 和 `basic_agent` 各自长期持有一份独立 memory，再通过摘要同步，会立刻引入以下复杂度：

1. 两份 memory 的同步边界不清晰
2. reset / has_run / answer-now / abort 行为需要按 agent 分叉处理
3. 工具轨迹、图片上下文、流式中断恢复更容易不一致
4. 本期明明不做 spawn/fork，却提前引入了类似“多上下文同步”的复杂问题

因此本期不采用“双 memory / 双长期实例”方案。

### 3.4 运行时拓扑

```text
用户消息
   ↓
/v1/agent/chat
   ↓
AgentRunner
   ↓
SessionStore.get_or_create_session(...)
   ↓
Shared Session State
  - shared_main_memory
  - current_agent_name (per turn)
  - active_agent_instance
  - abort / answer-now state
   ↓
Turn entry = coordinator_agent
   ↓
Coordinator routing pass
   ├─ 直接自然语言 → 返回用户 + 提交原始用户消息/最终答复到 shared memory
   └─ switch_agent(basic_agent) → 切到 basic_agent
                                  ↓
                             basic_agent tool-calling run
                                  ↓
                             工具调用 / SSE / final answer
                                  ↓
                             提交到 shared memory
```

## 4. 配置设计

### 4.1 配置文件策略

不新增新的 multi-agent prompt 文件。继续使用：

`config/config-agent-prompts.yaml`

但是要把当前单 agent 结构重构成多 agent 结构。

### 4.2 目标 YAML 结构

本期目标结构如下：

```yaml
coordinator_agent:
  instructions: |-
    ...
  prompt_templates:
    system_prompt: |-
      ...

basic_agent:
  instructions: |-
    ...
  prompt_templates:
    system_prompt: |-
      ...
    planning:
      initial_plan: |-
        ...
      update_plan_pre_messages: |-
        ...
      update_plan_post_messages: |-
        ...
    managed_agent:
      task: |-
        ...
      report: |-
        ...
    final_answer:
      pre_messages: |-
        ...
      post_messages: |-
        ...

explorer_agent:
  instructions: |-
    ...
  prompt_templates:
    system_prompt: |-
      ...

auditor_agent:
  instructions: |-
    ...
  prompt_templates:
    system_prompt: |-
      ...

vlm_instructions: |-
  ...
```

### 4.3 配置迁移原则

当前文件中的：

1. `agent_instructions`
2. `toolcalling_agent`

要合并、吸收后迁移为：

`basic_agent.instructions + basic_agent.prompt_templates`

具体原则：

1. 原 `agent_instructions` 的行为规则迁移到 `basic_agent.instructions`
2. 原 `toolcalling_agent.system_prompt` 迁移到 `basic_agent.prompt_templates.system_prompt`
3. 原 `toolcalling_agent.planning`、`managed_agent`、`final_answer` 原样迁移到 `basic_agent.prompt_templates` 下对应节点
4. 原文件中和 basic 能力无关的单 agent 叙述，要改写成“basic agent 的工作规则”

### 4.4 coordinator prompt 约束

`coordinator_agent` 的 prompt 必须满足：

1. 明确说明自己是协调员
2. 明确说明它可以让 LLM 选择是否切到 `basic_agent`
3. 不披露 `explorer_agent` / `auditor_agent` 名字
4. 明确要求 LLM 的回复只能是两类之一：
   - 面向用户的最终自然语言答复
   - 一条纯粹的 `switch_agent` 指令
5. 明确禁止“在切换指令后附带长文本说明”
6. 明确提示：当任务涉及工具使用、文件操作、复杂执行、拿不准该选哪个 agent 时，优先切到 `basic_agent`

### 4.5 vision 指令注入规则

用户已明确要求：

`vlm_instructions` 只追加给 `coordinator_agent`

因此本期配置与运行时必须遵守：

1. text 模式下，不给 coordinator 追加 `vlm_instructions`
2. vision 模式下，只给 coordinator 追加 `vlm_instructions`
3. `basic_agent` 不额外追加这段全局 `vlm_instructions`

这意味着本期 vision 路径要特别确保：

1. coordinator 的路由判断能看到图像输入
2. 切到 basic 时，原始本轮图像仍能随当前任务进入 shared memory / 当前执行链

### 4.6 运行时多 agent 注册信息

除了 prompt YAML 之外，运行时仍需要一层 registry 描述每个 agent profile 的启用状态与工具范围。该信息建议进入现有 `agent` 配置段，而不是写死在 Python 逻辑中。

建议新增配置项：

```yaml
agent:
  entry_agent_name: coordinator_agent
  enabled_agent_profiles:
    - coordinator_agent
    - basic_agent
  max_agent_switches_per_turn: 2
```

说明：

1. `entry_agent_name` 本期固定为 `coordinator_agent`
2. `enabled_agent_profiles` 本期只启用 `coordinator_agent` 与 `basic_agent`
3. `explorer_agent` / `auditor_agent` 虽然在 prompt YAML 中有占位，但本期不启用
4. `max_agent_switches_per_turn` 用于防止模型切换循环，不宜硬编码在 Python 中

## 5. 数据模型与 Session 模型

### 5.1 Session 核心原则

一个 session 只维护：

1. 一份共享主 memory
2. 一份全局中止 / answer-now 状态
3. 一份当前激活 agent profile 信息
4. 一个当前 active agent 实例

不维护“多份长期并存、彼此独立的 agent memory”。

### 5.2 AgentSession 需要新增或重定义的状态

现有 `AgentSession` 以“一个 agent 对一个 session”为默认假设。多 agent 化后，需要把 session 的重心从“持有某个固定 agent 对象”改成“持有共享 memory 与当前激活 profile”。

建议新增或重定义下列运行时字段：

1. `shared_memory`
2. `current_agent_name`
3. `active_agent`
4. `turn_switch_count`
5. `has_shared_history`

语义说明：

1. `shared_memory`：同一 session 的唯一主 memory，跨 agent 共享
2. `current_agent_name`：当前回合正在运行的 agent 名字
3. `active_agent`：当前挂接到运行链路的 agent 实例
4. `turn_switch_count`：本轮切 agent 次数，用于防止无限切换
5. `has_shared_history`：表示 shared memory 是否已经有持久化历史，替代旧语义里偏向“某个固定 agent 是否 run 过”的 `has_run`

### 5.3 关于 active agent 实例

本期不建议长期保留多个 agent 实例并尝试做状态同步，而是：

1. 需要哪个 agent，就按 profile 构建哪个 agent 实例
2. 将 session 的 `shared_memory` 挂接给这个 active agent
3. active agent 完成本轮阶段后，session 只保留当前 active agent 引用
4. 发生 agent switch 时，替换 active agent，但继续复用同一份 `shared_memory`

这样可以避免多个 agent 内部状态逐渐漂移。

### 5.4 主 memory 的持久化内容

用户已明确要求：主 memory 持久保存**原始用户消息**。

因此本期要明确区分两类输入：

1. **transient routing input**：仅用于 coordinator 路由判断的包装文本
2. **persistent task input**：写入 shared memory 的原始用户消息

shared memory 允许持久保留：

1. 用户原始消息
2. 当前或历史轮次的工具调用轨迹
3. 工具观察结果
4. 最终答复
5. 当前轮图片输入

shared memory 不应持久保留：

1. coordinator 的包装模板全文
2. coordinator 的“切 basic”那条路由指令文本
3. 为了解析 switch 临时拼出的内部控制消息

### 5.5 coordinator 回合如何提交到主 memory

由于 coordinator 不应把包装后的用户消息直接 append 到主 memory，因此 coordinator 本轮必须分为两个阶段：

1. **routing pass**：内部临时执行，不直接写 shared memory
2. **commit pass**：根据路由结果，把应保留的信息写回 shared memory

分两种情况：

1. coordinator 直接回答用户
   - 向 shared memory 写入原始用户消息
   - 向 shared memory 写入最终答复
   - 不写入包装 prompt 与 switch 命令

2. coordinator 切到 basic
   - 不把 coordinator 的包装 prompt 写入 shared memory
   - basic 正式执行时，把原始用户消息作为当前任务写入 shared memory
   - 此后工具轨迹与 final answer 按 basic 正常流程进入 shared memory

额外硬约束：

1. `coordinator routing pass` 不能直接复用“会把当前任务自动写入 memory 的标准 `agent.run()` 提交流程”
2. coordinator 的路由判定必须先在**不污染 shared memory 的临时执行路径**中完成
3. 只有在路由结果确定后，才允许通过 commit pass 把原始用户消息与应保留的答复写回 shared memory

### 5.6 与现有 memory 结构的兼容要求

为了尽量复用现有 `write_memory_to_messages()`、history compression、image stripping 逻辑，coordinator-only 回合在提交 shared memory 时，仍应使用与现有 memory 兼容的 TaskStep / ActionStep 语义，而不是额外发明一套完全不同的历史对象结构。

要求：

1. coordinator-only 回合也要能在后续轮次被正常序列化为“用户消息 + 助手最终答复”
2. 不能因为 coordinator 是内部路由层，就让历史回合缺失用户/助手配对
3. 不能让历史写入破坏现有 image stripping 与 final answer 去重逻辑

## 6. `switch_agent` 命令协议

### 6.1 canonical 输出格式

虽然解析器会兼容多种写法，但 prompt 应该只教模型一种 canonical 格式，降低歧义。canonical 格式使用用户给出的样式：

```json
[{"id": "call_0", "type": "agent", "function": {"name": "switch_agent", "arguments": "basic_agent"}}]
```

### 6.2 后端兼容的写法

后端解析层要尽量兼容以下等价写法：

1. `type="agent"` + `arguments="basic_agent"`
2. `type="agent"` + `arguments={"agent_name":"basic_agent"}`
3. `type="function"` + `function.name="switch_agent"` + JSON string arguments
4. 纯 JSON 对象写法，而非数组写法
5. 文本前后带少量解释、但中间可提取出合法 switch payload 的情况

兼容不是为了鼓励模型乱写，而是为了降低小模型输出格式波动导致的失败率。

### 6.3 解析优先级

多 agent 化后，响应解析优先级应变为：

1. 先识别 `switch_agent` 命令
2. 再识别真实工具调用
3. 最后把剩余纯文本视为 final answer

理由：

1. `coordinator_agent` 没有工具，最核心动作是 switch
2. 本项目已经对“纯文本 = final answer”做了深度适配，不能被 switch 解析误伤
3. 先判 switch 能避免把 agent command 错当成普通 tool transcript

### 6.4 coordinator 输出约束

coordinator prompt 要显式要求：

1. 如果切 agent，只输出那条切换指令
2. 不要在切换指令前后追加与切换无关的长段说明
3. 不要输出工具调用 JSON
4. 不要同时输出“switch + 用户最终答案”混合文本

### 6.5 容错与冲突规则

后端解析层遵循以下规则：

1. 如果能稳定提取出唯一合法的 `switch_agent(target)`，则执行切换，并忽略外围少量装饰性文本
2. 如果同一响应里出现多个不同 target agent，视为无效输出
3. 如果同一响应同时包含有效 switch 与有效工具调用，视为冲突输出
4. 如果 target agent 未启用，返回受控错误，不悄悄降级成别的 agent
5. 如果 target agent 是占位但未启用的 `explorer_agent` / `auditor_agent`，明确报“未启用”

### 6.6 本期启用的 switch 范围

运行时 parser 可以做成通用，但本期真正启用的 target 只有：

1. `basic_agent`
2. `coordinator_agent`（可选支持，但不是常规路径）

本期 prompt surface 中，只有 coordinator 会主动教模型切到 `basic_agent`。`explorer_agent` / `auditor_agent` 不应出现在 coordinator 的 prompt 中，也不应成为本期正常可达路径。

## 7. Agent Prompt 设计

### 7.1 `coordinator_agent`

`coordinator_agent` 是“每轮接待员”，负责：

1. 告诉 LLM 当前可以直接与用户沟通，或者切到 `basic_agent`
2. 在简单闲聊、简单知识问答场景下直接回答
3. 在需要工具或更复杂执行时，输出 `switch_agent` 命令

它不负责：

1. 调用工具
2. 展示工具说明
3. 暴露其他未启用 agent

其 prompt 必须显式包含的规则：

1. 当前只能对接一个 agent
2. 如果任务只是普通情感聊天或简单知识问答，则直接回答
3. 如果任务需要工具、文件操作、系统操作、复杂技能或你拿不准，优先切到 `basic_agent`
4. 你的回复要么是给用户的最终自然语言，要么是一条纯 `switch_agent` 指令
5. 切换时不能附加任何与切换无关的长文本

### 7.2 `basic_agent`

`basic_agent` 是本期真正承担“工具型 agent”职责的 profile。

它的 prompt 由现有：

1. `agent_instructions`
2. `toolcalling_agent.system_prompt`
3. `toolcalling_agent.planning`
4. `toolcalling_agent.managed_agent`
5. `toolcalling_agent.final_answer`

合并、迁移、润色而来。

它需要保留的能力与规则包括：

1. 当前项目已有的 final answer 适配规则
2. 当前项目已有的工具调用与观察循环规则
3. 文件写入、编辑、读取、`glob`、`grep`、skills 的使用规范
4. 当前项目已有的命令执行安全约束
5. 当前项目已有的复合搜索工具使用规范

### 7.3 `explorer_agent` / `auditor_agent`

本期只做配置占位，不进入实际运行时披露。

它们存在的目的：

1. 让 prompt YAML 结构一次性过渡到多 agent 形态
2. 避免下期再做一次破坏性配置迁移
3. 给后续上下文隔离型 agent 留固定名字和配置节点

本期要求：

1. coordinator prompt 不提它们
2. runtime registry 不启用它们
3. session switch 不把它们作为正常路由目标

## 8. 工具暴露与可见性策略

### 8.1 profile 级工具策略

本期的工具暴露分两层：

1. **profile allowlist**：由当前 agent profile 决定本 profile 理论上可见哪些工具
2. **已有隐藏规则**：继续沿用当前 `SmartToolCallingAgent` 的 prompt/schema 过滤逻辑

最终可见工具集合 = `profile allowlist` ∩ `现有隐藏/暴露规则`

### 8.2 coordinator 的工具策略

`coordinator_agent` 的工具集合必须为空。

这里必须同时切断两条路径：

1. API schema 中不传任何工具
2. system prompt 中不渲染任何工具列表

否则模型仍然可能通过某一条路径“看到”工具定义，破坏 coordinator 的路由纯度。

### 8.3 basic 的工具策略

`basic_agent` 需要暴露当前系统已有的核心工具能力，至少包括：

1. `read_file`
2. `write_file`
3. `edit_file`
4. `glob`
5. `grep`
6. `list_skills`
7. `read_skill`

为避免现有单 agent 能力回归，本期 `basic_agent` 可以继续承接当前单 agent 系统已有的其他基础工具集合，例如：

1. `exec_command`
2. `check_process`
3. `browser_command`（继续遵守现有 prompt/schema 显隐策略）
4. `search_and_fetch`
5. `wikipedia_search_and_fetch`
6. 其他当前单 agent 已稳定接入的 MCP 工具

原因是用户已明确说明：多 agent 的重点是“提示词渐进披露 + 上下文管理方式”，不是强制禁止模型跨 agent 记忆工具。

### 8.4 未来 agent 的工具边界

虽然 `explorer_agent` / `auditor_agent` 本期不启用，但本期架构不能把工具暴露逻辑写死成“只有 basic 有工具，未来没法扩展”。

因此工具过滤逻辑应按 profile 做成可扩展映射，而不是 if/else 硬编码死分支。

## 9. 执行流程设计

### 9.1 每轮入口规则

每次用户点击发送，后端都执行：

1. 载入 session
2. 把本轮 `current_agent_name` 重置为 `coordinator_agent`
3. 运行 coordinator routing pass

无论上一轮停在什么 agent，本轮都从 coordinator 开始。

### 9.2 coordinator routing pass

coordinator routing pass 是本期最关键的新增流程。它的设计目标是：

1. 让 coordinator 拿到包装后的用户需求文本
2. 不把包装文本直接写进 shared memory
3. 只让 coordinator 做“直接回答 or 切 basic”的轻量决策

执行步骤：

1. 读取 shared memory 的历史消息
2. 构造本轮 coordinator 专用的 transient user input：它包含 coordinator 说明 + 原始用户消息
3. 用 `coordinator_agent` 的 system prompt + transient user input 调用 LLM
4. 这一步必须通过**不自动提交 shared memory** 的临时执行路径完成，而不能直接走“标准 agent.run + 自动记忆追加”路径
4. 解析响应
5. 如果是自然语言最终答复，则提交原始用户消息与最终答复到 shared memory
6. 如果是 `switch_agent(basic_agent)`，则切换 active profile 到 `basic_agent`

### 9.3 coordinator 直接回答路径

如果 coordinator 判定该任务无需切 basic：

1. 不进入工具链
2. 直接把 coordinator 输出作为给用户的最终答复
3. 向 shared memory 提交：
   - 原始用户消息
   - 最终答复
4. 不把 coordinator 的包装 prompt、本轮 switch 解析中间产物写入 history

### 9.4 coordinator -> basic 切换路径

如果 coordinator 输出 `switch_agent(basic_agent)`：

1. 后端验证 target 是否已启用
2. 切换 session 的 `current_agent_name`
3. 构建 `basic_agent` 的 active agent 实例
4. 把 session 的 shared memory 挂接给该实例
5. 使用**原始用户消息**作为 basic 的当前任务输入
6. 若本轮有图片，则原始图片继续随本轮任务进入 basic
7. basic 继续按现有 stream/sync tool-calling 逻辑运行

### 9.5 stream 流程

`run_stream()` 的多 agent 化目标流程如下：

1. 获取或创建 session
2. 本轮初始化 `current_agent_name=coordinator_agent`
3. 运行 coordinator routing pass
4. 如果 coordinator 直接回答：
   - 输出 `session_start`
   - 流式输出 `content`
   - 输出 `done`
5. 如果 coordinator 切到 basic：
   - 不需要向前端强制新增协议
   - 后端内部切换 active agent
   - 继续沿用当前 basic 的 `agent_step` / `content` / `done` 事件流

本期默认不要求前端感知“coordinator 先想了一次”，因此 coordinator routing pass 可作为后端内部步骤，不必强制渲染为新的前端卡片。

### 9.6 sync 流程

`run_sync()` 的流程与 stream 相同，只是最终返回 JSON：

1. coordinator 路由
2. 直接答复则返回最终 answer
3. 切到 basic 则返回 basic 执行后的 answer 和 steps

### 9.7 同轮多次切换保护

虽然本期正常路径只预期 `coordinator -> basic` 一次切换，但运行时仍要防止模型进入切换循环。因此本轮应统计 `turn_switch_count`，一旦超过 `max_agent_switches_per_turn`，则终止切换并返回受控错误。

## 10. API 与 SSE 兼容要求

### 10.1 HTTP API

本期保持以下接口不变：

1. `POST /v1/agent/chat`
2. `DELETE /v1/agent/session/{session_id}`
3. `POST /v1/agent/stop`
4. `POST /v1/agent/stop-all`
5. `POST /v1/agent/answer-now`
6. `POST /v1/agent/answer-now/stream`

请求字段和响应主体不做破坏性调整。

### 10.2 SSE 事件

本期不要求新增前端必须消费的新事件类型。

现有事件保持为主：

1. `session_start`
2. `agent_step`
3. `content`
4. `done`
5. `error`
6. `aborted`

如实现阶段确实需要增加调试型事件，也必须是**可选、非破坏性**的新增，且当前前端在忽略它们时不能出错。

### 10.3 stop / answer-now 兼容性

多 agent 化后，这些接口仍需工作在“当前 active request”语义下：

1. 如果当前在 coordinator routing pass，则应中止 routing request
2. 如果当前在 basic 工具链执行，则应中止 basic request
3. `answer-now` 只能基于当前已保留的 shared memory 和可见历史生成最终答复

## 11. Vision 兼容设计

### 11.1 基本要求

本期多 agent 必须同时兼容：

1. text 页面 `/`
2. vision 页面 `/vision`

### 11.2 coordinator 的 vision 责任

当 `model_type=vision` 时：

1. coordinator 要收到 `vlm_instructions`
2. coordinator routing pass 需要能看见本轮图片输入
3. coordinator 不能误判为“自己看不到图片”

### 11.3 切到 basic 后的 vision 保真要求

由于用户明确要求 `vlm_instructions` 只追加给 coordinator，本期实现必须至少保证：

1. coordinator -> basic 切换时，不丢失原始图片输入
2. basic 接手当前任务时，本轮图片仍然作为当前任务的一部分可用
3. 对 vision 页面至少做一组 smoke 测试，验证“带图任务可从 coordinator 成功切到 basic 并继续执行”

### 11.4 风险说明

本项目已有经验表明：VLM 身份指令对模型“愿不愿承认自己能看图”很关键。由于本期按用户要求只给 coordinator 追加 `vlm_instructions`，vision 路径存在一项需要重点验证的风险：

1. 部分模型在 basic 阶段仍可能弱化对图像的显式自我认知

因此这一点必须进入验收测试，而不能仅凭设计假设通过。

## 12. 错误处理与降级策略

### 12.1 switch 相关错误

以下情况都应返回受控错误，而不是悄悄走错路径：

1. target agent 不存在
2. target agent 存在但未启用
3. 同一响应里出现多个冲突 switch target
4. switch 与工具调用混在同一响应中且无法确定主意图
5. 本轮 switch 次数超限

### 12.2 coordinator 无效输出

如果 coordinator 输出既不是可解析的 switch，也不是合理的自然语言最终答复，处理原则如下：

1. 优先尝试容错提取唯一合法 switch
2. 若失败，但存在清晰自然语言，则按 final answer 处理
3. 若既无有效 switch，又无可用自然语言，则返回 `coordinator_invalid_response` 类型错误

### 12.3 占位 agent 请求

如果模型试图切到 `explorer_agent` 或 `auditor_agent`：

1. 不执行
2. 明确返回“agent 未启用”
3. 不自动偷偷改成 `basic_agent`

### 12.4 memory 污染防护

以下内容不得写入 shared memory：

1. coordinator 包装 prompt
2. switch 命令原文
3. backend 内部控制用的 transient routing 文本

### 12.5 回归防护

以下现有能力必须保持不回归：

1. final answer 纯文本自动包装
2. final_answer 工具调用静默拦截
3. 历史 tool_calls 不可被错误 strip
4. 多工具编排逻辑
5. answer-now / stop 的现有语义

## 13. 测试与验收标准

### 13.1 单元测试

建议新增测试：

1. `switch_agent` canonical 格式解析
2. 多种兼容写法解析为统一 `SwitchAgentCommand`
3. 解析优先级：switch > tool > plain text
4. coordinator prompt 中不出现 `explorer_agent` / `auditor_agent`
5. coordinator schema 中不暴露工具
6. basic 继续暴露必需工具
7. 占位 agent 未启用时返回受控错误

### 13.2 Session / Memory 测试

建议新增测试：

1. 每轮入口强制回到 coordinator
2. shared memory 保留原始用户消息
3. coordinator 包装文本不进入 shared memory
4. coordinator 直接回答时，历史轮次仍能正常回放
5. coordinator 切到 basic 时，basic 接手后 memory 连贯
6. has_run / history state 以 shared memory 语义为准，而不是以某个旧 agent 实例为准

### 13.3 Stream / Sync 测试

建议新增测试：

1. stream 下简单问答走 coordinator 直接返回
2. stream 下工具任务由 coordinator 切到 basic，并正确返回工具步骤和最终答案
3. sync 下同样覆盖上述两条路径
4. stop / answer-now 在 coordinator routing pass 和 basic 执行阶段都能工作

### 13.4 Vision 测试

建议新增测试：

1. vision 简单问答由 coordinator 直接处理
2. vision 工具任务由 coordinator 切到 basic，且图片不丢失
3. 老的图片泄漏防护逻辑仍然成立

### 13.5 验收清单

上线前必须逐项确认：

1. text 与 vision 都能启动并完成一轮普通对话
2. 简单问答不会误切到 basic
3. 文件/执行类任务能切到 basic 并正常完成
4. `/v1/agent/chat` 对外接口无破坏性变化
5. shared memory 不含 coordinator 包装 prompt
6. `config/config-agent-prompts.yaml` 已完成结构化迁移

## 14. 实施范围与分期建议

### 14.1 本期必做

1. prompt YAML 重构为多 agent 结构
2. `AgentPromptsConfig` / 配置解析升级
3. session 共享 memory + current agent 状态模型升级
4. coordinator routing pass
5. `switch_agent` 解析与路由
6. coordinator -> basic 切换执行链
7. text / vision smoke tests

### 14.2 本期只留扩展点、不落地

1. `explorer_agent`
2. `auditor_agent`
3. spawn context
4. fork context
5. agent 专属前端展示

## 15. 风险与后续扩展

### 15.1 本期主要风险

1. coordinator routing pass 若直接复用现有 `agent.run()`，容易把包装 prompt 误写入 shared memory
2. shared memory 语义与旧 `has_run` 语义冲突，若处理不当会造成 reset 错误
3. vision 模式下只给 coordinator 注入 `vlm_instructions`，可能导致 basic 阶段对图片理解的稳定性下降
4. 若 switch 命令解析和现有 tool parsing 顺序处理不当，会把 agent command 错当 tool call 或 plain text

### 15.2 后续扩展方向

本期设计完成后，后续可以在不推翻核心结构的前提下扩展：

1. 启用 `explorer_agent`
2. 启用 `auditor_agent`
3. 为不同 agent 配置不同上下文隔离策略
4. 引入 spawn / fork 子上下文
5. 为前端增加可选的“当前 agent”可视化反馈

## 16. 最终结论

本期多 agent 改造的正确落点不是“把单 agent 生硬复制成多个长期独立实例”，而是：

1. 以 session 为主轴
2. 共享一份主 memory
3. 每轮先由 `coordinator_agent` 做内部路由
4. 按需切到 `basic_agent` 执行工具链
5. 保持外部 API 不变
6. 把 prompt 配置升级为同层级多 agent 结构

这条路径改动足够大，能够真正把系统从“单 agent”升级为“多 agent 状态机”；同时又足够克制，不会提前引入本期不需要的多上下文同步复杂度。
