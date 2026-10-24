# Prefix Cache Stability v2 — Strict Append-Only Context Architecture

> 版本: v2 (replaces `2026-05-28-prefix-cache-stability-design.md`)
> 生效范围: `backend/agent/agent_runner.py` · `backend/agent/local_llm_model.py` · `backend/llm_service.py` · `backend/agent/orchestrator.py` · `backend/config.py` · `config/config-misc.yaml` · `config/config-agent-prompts.yaml` · `tests/`

## 1. 背景与本版本的差异

### 1.1 v1 的不足（已被本版本取代）

v1 SPEC (`2026-05-28-prefix-cache-stability-design.md`) 引入了 Tier 1/2/3 三层存储 + "Deterministic Compiler" + 一个预留的 "Phase 2: Event-triggered Distiller"。v1 设计的 Tier 2 字段（`working_memory` / `unresolved_items` / `handoff_ledger` / `tool_ledger`）被设计为 Per-Agent Context Pack 的输入，会被 LLM 消费。

**本版本审计发现，v1 的 Tier 2/3 设计存在三个根本问题：**

1. **当前 `_compile_context_pack` 产生的 `context_pack` 是死数据**。它在 `AgentRunner._activate_profile_agent()` 中被写入 `next_agent.state["context_pack"]`（`agent_runner.py:3448`），但全后端没有任何代码路径会读取 `state["context_pack"]` 再注入到 LLM 实际输入里（`grep "context_pack"` 在后端命中 4 处，全部是写入或 `pop`，零消费）。LLM 真正消费的输入只来自 `smolagents.memory.steps`（`write_memory_to_messages`）和 `LocalLLMModel._inject_runtime_context`。也就是说，v1 设计的"结构化 sidecar 喂给 LLM"在当前实现里**根本没有发生**。
2. **v1 描述的"按版本追加"语义没有任何执行机制**。`working_memory.append(...)`、`handoff_ledger.append(...)`、`unresolved_items` 字段在当前代码里是**简单 list.append**（`agent_runner.py:2629-2644`），没有版本号、没有冻结快照、可以被未来轮次覆盖式追加。一旦未来实现把 context_pack 真的注入 LLM，这些 list 就会随着轮次线性增长并改写前缀 token 序列，破坏 prefix cache。
3. **"Phase 2: Event-triggered Distiller" 在本地 LLM 上 ROI 极低**，会让主链路每 N 轮承担一次额外的 LLM 摘要推理，显著拉长首帧与 handoff 时延。`config-misc.yaml:97` 当前 `enable_runtime_time_context: true` 已经把 session-pinned 时间写进 system prefix 一次；进一步让 LLM 周期性总结旧历史是冗余的，应明确出范围。

### 1.2 本版本的核心原则

**前缀 = session-pinned 不可变头部 + append-only 动态尾部。** 任何不满足这两条性质之一的字段，禁止进入 LLM 实际输入。

具体地：

- 同一 session 内，**稳定前缀**在所有 LLM 调用之间字节级保持不变。
- 动态尾部只允许**追加**到尾部，**禁止重写、禁止覆盖、禁止前插**。
- 任何"会随轮次变化且放在前缀中部"的字段必须**删除**或**下移到动态尾部**。
- 不在本期引入任何 LLM 摘要/蒸馏器作为主链路依赖。

## 2. Goals

- 最大化 SGLang / vLLM prefix cache 命中率，**目标：同一 session 内的所有 LLM 调用，system 段 + 历史 message 段（不含最新一轮）字节级稳定**。
- 删除 v1 引入但当前并未实际喂给 LLM 的 Tier 2/3 抽象字段（`working_memory` / `unresolved_items` / `handoff_ledger` / `tool_ledger` / `agent_contract` / `recent_visible_history` / `user_goal` 在 prompt 主体中的位置），避免未来误用导致 prefix cache 命中失败。
- 维护"agent 切换"和"路由决策"这两类**仅用于审计/调试的事实记录**，但**不喂入 LLM prompt 主体**。
- 拒绝 Phase 2 LLM 摘要 / 蒸馏器作为主链路依赖；继续以 deterministic compiler 为唯一方式维护历史结构化状态。
- 显式禁止未来在 system 段或前缀中部引入"每轮会变"的字段。

## 3. Non-Goals

- 不做 per-turn LLM 摘要 / 历史压缩 / 语义蒸馏。
- 不把 `state["context_pack"]` 重新设计为 LLM 输入。
- 不改变 frontend 可见 SSE/timeline 协议。
- 不改变 `coordinator_1` / `coordinator_2` / `basic_agent` 之间的 handoff 流程。
- 不修改 SGLang / vLLM 启动参数或 chat_template。

## 4. 实际 LLM 输入路径（v2 重新表述）

后端**实际**喂给 LLM 的输入，由以下三段拼接而成，**且仅由这三段拼接**：

### 4.1 稳定前缀段（Stable Prefix） — 同一 session 内字节级稳定

| 子段 | 来源 | 稳定性约束 |
|---|---|---|
| System prompt | `SmartToolCallingAgent.initialize_system_prompt()` 渲染的 `prompt_templates["system_prompt"]` | 同一 (profile_name, session.runtime_context) 下，**完全确定性**。任何"prompt_visible_tools"过滤集合、custom_instructions、agent_python_executable 都是 session 内不变。 |
| Runtime context（"Current local time" 等） | 由 `_build_runtime_context_for_profile()` 在新 session 首次生成，写入 `session.runtime_context` 后**冻结** | 同 session 内**冻结**。`enable_runtime_time_context` 关掉时整段不注入。 |
| 工具 schema | `tools_and_managed_agents` → `LocalLLMModel.generate[_stream]` 中 `get_tool_json_schema` | 同 profile 内稳定（profile 切换才会变）。 |

**关键不变量**：`session.runtime_context` 在新 session 首次生成后即冻结，`_build_profile_agent` 后续构造 agent 时**不会重新生成**（`agent_runner.py:2410-2413`）。这保证稳定前缀稳定。

### 4.2 动态尾部（Dynamic Tail） — 每轮 append-only

| 子段 | 来源 | 写入规则 |
|---|---|---|
| 当前轮 user task | `TaskStep.task` / `TaskStep.task_images` 序列化 | 只 append。历史 `image_url` part 保留在动态尾部且永不转换为文本；正常请求在捕获 source 前至多执行一次确定性的 data-URL resize。 |
| 当前轮 assistant message / reasoning delta | `ActionStep.model_output`（已清洗） | 只 append。`_strip_tool_call_text` 在第一步 LLM 调用后将原始 model_output 改写为清洗版；**改写一旦发生就稳定不变**，后续调用看到的是同一个清洗值（见 §6.1）。 |
| 当前轮 tool call / observation | `ActionStep.tool_calls` / `ActionStep.observations` | 只 append。 |
| 当前轮 agent switch | handoff 完成后由 `_activate_profile_agent` 切到新 agent，新 agent 的 system prompt 是"稳定前缀段"内的新版本（旧 agent 的 memory.steps 通过 `shared_steps` 共享保留）。 | append。 |
| 当前轮 routing decision 文本 | 仅在 `coordinator_1` 路由阶段作为 **最后一条 user 消息**追加到 `routing_messages`（`agent_runner.py:2932`） | append-only，仅在 routing 阶段出现。 |

### 4.3 显式**不**进入 LLM 输入的字段

以下字段在 v1 SPEC 中被设计为 context pack 的组成，但**v2 全部移出 LLM 输入路径**。它们可以保留在 `session.behavior_records` 作为 Tier 2 审计/调试记录，但**禁止任何路径把它们序列化到 system prompt 或 message 主体**：

- `working_memory`
- `unresolved_items`
- `handoff_ledger`
- `tool_ledger`
- `agent_contract` (per-agent contract 描述)
- `recent_visible_history` (UI 风格的历史摘录)
- 任何"v1 SPEC 表格中标记为 Structured sidecar / append/update in storage, prompt uses latest compiled view"的字段
- `user_goal` 字段本身（**注意**：user 的真实任务文本仍以 `TaskStep.task` 进入 message 主体；这里是删除"额外抽象的 user_goal 字段"）

**session_id 不进入 prompt 主体**（v1 规则保留）。

## 5. 数据规则（Data Rules）

### 5.1 字段分类（替换 v1 表格）

| 类别 | 归属 | 规则 |
|---|---|---|
| `system_prompt` | Stable prefix | 同 session 同 profile 内字节级稳定 |
| `runtime_context` (e.g. "Current local time") | Stable prefix | session-pinned，新 session 首次生成后冻结 |
| `tools schema` | Stable prefix | 同 profile 内稳定；profile 切换才会变 |
| `session_id` | Storage only | **禁止**进入 prompt 主体 |
| `TaskStep.task` (user 当前轮问题) | Dynamic tail | append-only |
| `TaskStep.task_images` | Dynamic tail | append-only；所有历史图片保持 `image_url` part，永不转换为文本。超过 `max_image_dimension` 时，正常请求在捕获 source 前至多执行一次确定性的 data-URL resize；cache-first compact 原样使用该 source 并绕过所有图片准备。 |
| `ActionStep.model_output` | Dynamic tail | append-only；首次写入后**冻结**（_strip_tool_call_text 清洗是一次性） |
| `ActionStep.tool_calls` | Dynamic tail | append-only |
| `ActionStep.observations` | Dynamic tail | append-only |
| `routing_decision` 用户层 routing 文本 | Dynamic tail (coordinator_1 only) | append-only |
| `routing_receipt` (Tier 2 审计) | Storage only | append/update 允许在 storage，但**禁止**进入 prompt 主体 |
| `handoff_ledger` (Tier 2 审计) | Storage only | append-only；不进入 prompt 主体 |
| `tool_ledger` | Storage only | append-only；不进入 prompt 主体 |
| `working_memory` | **删除** | 见 §7.1 |
| `unresolved_items` | **删除** | 见 §7.1 |
| `agent_contract` | **删除** | 见 §7.1 |
| `recent_visible_history` | **删除** | 见 §7.1 |
| `summary` (Phase 2 LLM 摘要) | **不存在** | 不在 v2 范围内 |

### 5.2 runtime time 规则（继承 v1）

- 新 session：第一次进入 `coordinator_2` / `basic_agent` 时由 `_build_runtime_context_for_profile` 生成一次 `"Current local time: ..."`。
- 同 session 后续 turn：复用同一字符串（`session.runtime_context` 在 `_build_profile_agent` 中显式传 `session=session`，命中 if 分支返回冻结值）。
- `coordinator_1`：永不需要 runtime time（routing prompt 不引用）；当前实现里 `_profile_uses_runtime_time_context("coordinator_1")` 返回 False，已经不会注入。
- 关闭开关：`runtime_prompts.enable_runtime_time_context = false` 时整段不注入（`agent_runner.py:2405-2406`）。

### 5.3 历史摘要规则（v2 修订）

- v2 **不**为 LLM 摘要预留 `summary_vN` 字段。如果未来某天确实需要历史压缩，必须满足：
  1. 旧历史必须**冻结**为不可变 snapshot，不再被改写；
  2. 新版本以新独立字段（例如 `summary_v2`）追加到前缀**末尾**，不覆盖 `summary_v1`；
  3. 当前 LLM 输入**只消费最新版本指针**，旧版本仅用于回放/审计。
- v2 当前不实现该机制；如要实现，**必须走新的 RFC + 新 SPEC**。

## 6. 实现约束（Implementation Constraints）

### 6.1 已知稳定性的边界

以下点当前实现满足 v2 约束，需在编码计划中以"不破坏"方式验证：

1. **`_strip_tool_call_text` 的清洗一次性**：在 `_step_stream` 内（`agent_runner.py:2045-2052`）第一次清洗后写回 `memory_step.model_output`，后续 LLM 调用看到的就是这个稳定值。但**第一次 LLM 调用**（即本 step 内的第一次调用）看到的是原始 `model_output`，写回清洗值后，**同一 turn 内下一次 LLM 调用**会看到清洗值。这是**单 step 内**的"首次 vs 后续"差异，但 `model_output` 属于"动态尾部"（见 §4.2），**不属于稳定前缀**，因此不会破坏 prefix cache 的复用前提（prefix cache 复用的是"上一次完整调用的输入"，下一次调用的输入里这一段已经 stable）。前提是"清清洗后该字段不再变"——这一点成立。
2. **图片 base64 一致性**：用户上传图片经 `_decode_request_images` 解码后，若未触发 resize，**保留原始 `_pre_encoded_b64` / `_pre_encoded_mime`**（`agent_runner.py:4436-4451`）；`_pre_encode_images` 命中这两个属性时**不重新编码**，直接 pass-through。**前提**：同一 session 同一图片输入走同一 resize 决策路径——这由 `max_image_dimension` / `max_image_size` / `supported_formats` 配置决定，配置不漂移则 base64 字节级稳定。
3. **多模态规范化在 provider 之前完成**：历史 `image_url` part 保留在动态尾部，永不转换为文本。正常 chat/agent 请求在形成可缓存 source 前，对超出 `max_image_dimension` 的 data URL 至多做一次确定性尺寸归一化；历史 `image_url` 不得删除、替换或重排。cache-first compact 原样使用捕获的 source，并在 `LLMService`/provider 路径中绕过所有图片准备，不得执行第二次变换，保证前缀与最近正常请求一致。reliable fallback 仍走正常准备路径。

### 6.2 绝对禁止的写法

下列操作在编码计划落地后必须被视为**回归**，应被任何 review 拒绝：

- 任何在 `initialize_system_prompt` 中加入"会随调用次数变化"的字段（时间戳、计数器、UUID、随机数）。
- 任何在 `session.runtime_context` 已有非空值后再次调 `_build_runtime_time_context()` 覆盖写入。
- 任何把 `state["context_pack"]` 或其子字段序列化进 system prompt / message 主体。
- 任何把 `working_memory` / `unresolved_items` 重新写回 system prompt 或 message。
- 任何为旧 history 添加"重新格式化后写回前缀主体"的代码（即"对已封存 turn 重新序列化"）。
- 任何让 LLM 在每轮开始前重新总结旧 turn 的代码（"per-turn LLM summarization"）。

### 6.3 调试与观测的边界

- `agent_runner.py` 的 `[CYCLE-DIAG]` 日志、`llm_service.py` 的多模态归一化诊断日志、`agent_runner.py` 的 `_log_handoff_debug` 都可以继续保留——它们不进入 prompt。**但**：这些日志若以 debug 级别全量打印 messages 全文，可能在日志中泄露 prompt 内容（隐私），应仅在排障时临时开启。
- `dispatch card` 的文本（routing_delta 等）**仅用于前端 SSE**，**不进入 LLM 输入**，不受 prefix cache 约束。

## 7. 编码计划概要（详见配套 plan 文档）

> 完整任务分解在 `2026-06-02-prefix-cache-stability-v2-plan.md`。

### 7.1 必须删除（Data + Code 双删）

| 删除项 | 位置 | 删除理由 |
|---|---|---|
| `state["context_pack"]` 写入 | `agent_runner.py:3430-3449` (`_activate_profile_agent`) | 死数据；从未被 LLM 消费 |
| `state["context_pack"]` 删除防护 | `agent_runner.py:2906` (`_strip_transient_state_fields`) | 同上；写入删除后此行失去意义 |
| `working_memory` 维护 | `agent_runner.py:2636-2644` (`_activate_secondary_handoff`) | 死数据；v2 明确删除 |
| `handoff_ledger` 维护 | `agent_runner.py:2628-2634` | **保留**为 Tier 2 审计，但**不再注入 context_pack** |
| `_compile_context_pack` 的 `working_memory` / `unresolved_items` / `agent_contract` / `recent_visible_history` / `user_goal` 字段 | `agent_runner.py:2853-2891` | v2 明确删除 |
| `AgentSession.behavior_records` 中 `working_memory` / `unresolved_items` / `tool_ledger` 的写入点（如有） | 全文搜索 | 删除 |
| 测试中对 `working_memory` / `unresolved_items` / `tool_ledger` / `recent_visible_history` 的断言 | `tests/test_timeline_projection.py` 全文、`tests/test_multi_agent_runner.py:1203-1218` 等 | 与新 SPEC 对齐 |
| 任何 `_run_agent` 内/外对 `state["context_pack"]` 的消费尝试 | 全后端 | 删除——v2 不再注入 |

### 7.2 必须保留（但限缩用途）

| 保留项 | 保留用途 |
|---|---|
| `session.behavior_records["route_receipt"]` | Tier 2 审计、调试日志、Tier 3 future agents 的入口契约；**不进入 prompt 主体** |
| `session.behavior_records["handoff_ledger"]` | Tier 2 审计：哪些 handoff 发生过；**不进入 prompt 主体** |
| `session.timeline_events` 全量 | Tier 1 raw timeline：审计/回放/前端流；**不进入 prompt 主体** |
| `_activate_profile_agent` 的"切到目标 agent"机制 | 切到目标 profile 后，新 agent 的 system prompt 是新的"稳定前缀段"前缀——这是 profile 维度的合理切换，不视为"重写"。 |

### 7.3 编码计划必须强化的不变量

1. **`session.runtime_context` 一旦非空就不再覆盖**：`_build_runtime_context_for_profile` 的 `if session is not None: if session.runtime_context is None: ...; return session.runtime_context` 路径不可被绕过。
2. **稳定前缀不引入"调用计数器"**：禁止在 system prompt 模板中加入 `{{ turn_count }}` / `{{ timestamp }}` 等随调用次数/时间变化的占位符。
3. **`_compile_context_pack` 若被保留（作为审计工具），其返回结构必须**不再被任何代码当作 LLM 输入**；如彻底删除可走 §7.1 路径。
4. **图片编码 byte-stable**：`agent_runner._decode_request_images` 必须保留 `_pre_encoded_b64` / `_pre_encoded_mime` 路径，不引入"重新 JPEG 编码"分支。

## 8. 与 v1 SPEC 的逐条差异（Diff）

| v1 章节 | v2 处理 |
|---|---|
| v1 §Architecture Overview: 1. Stable Prefix | **保留**，但增加"v2 §4.1 实际路径"明确哪些字段是 stable prefix。 |
| v1 §Architecture Overview: 2. Dynamic Tail | **保留**，但裁掉"routing decision 文本"以外的任何"中间态"。 |
| v1 §Architecture Overview: 3. Frozen History Snapshot | **保留**（smolagents `memory.steps` 不可变 list）。 |
| v1 §Architecture Overview: 4. Structured Sidecars | **删除 v1 含义**。v2 仅保留 `route_receipt` / `handoff_ledger` 作为审计，禁止注入 prompt。 |
| v1 Data Rules 中 `working_memory` / `unresolved_items` 等标记为 "append-first / append-only / prompt uses latest compiled view" | **删除全部**，不再进入 prompt。 |
| v1 Historical summary rule（版本化追加） | **降级**：v2 不预留该机制，需要时再走 RFC。 |
| v1 Risks and Mitigations 全部 | **保留风险 1、2、4**；**删除风险 3**（v2 不再讨论摘要覆盖）。 |
| AGENTS.md 陷阱 #13 "`coordinator_2` 全量可见 ≠ 原样回灌上下文" | **保留**。仍成立，但 v2 进一步收紧到"coordinator_2 的 transcript 也不进入 prefix 中部，仅作为 smolagents ActionStep.model_output 追加到尾部"。 |
| AGENTS.md 陷阱 #14 "Phase 1 禁止引入每轮 LLM 总结旧历史" | **保留**，并扩展为 v2 永久原则。 |
| Unified Message Model MEMO 中 `working_memory` / `unresolved_items` 章节 | **删除**（v2 不再维护这两个 sidecar）。`route_receipt` / `handoff_ledger` 保留为 Tier 2 审计示例。 |

## 9. 风险与缓解

### 风险 1: 旧历史被重新序列化（v1 Risk 1）

**保留**。当前实现以 `write_memory_to_messages` 的"历史 turn 压缩为文本"路径实现（`agent_runner.py:1411-1609`）。这条路径**只读取 memory.steps 后生成新 message list**，不修改 memory.steps。**前提**：`memory.steps` 在 turn 关闭后冻结——本实现通过 `_step_stream` 在每 step 结束后 append 而不是 in-place mutate 满足。

### 风险 2: runtime time 破坏 prefix reuse（v1 Risk 2）

**保留**。由 §5.2 约束保证 session-pinned。

### 风险 3（v1）: 摘要覆盖旧值导致前缀变化

**删除**。v2 不再讨论该风险，因为 v2 删除了相关字段。

### 风险 4: raw transcript 污染 prompt（v1 Risk 4）

**保留**。当前实现把 raw transcript 通过 `_emit_chat_bubble_delta` 推到前端 SSE，**不进入 prompt**。`_run_basic_stream` 内 `_buf_tool_calls` / `_buf_observations` 是 SSE 事件缓冲，**不会回写**到 memory.steps。前提：所有新代码不能把前端 transcript 写回 `state["context_pack"]` 或 `memory.steps`。

### 风险 5 (新增): v2 删除 working_memory / unresolved_items 后，下游行为有依赖

**缓解**：在落地前**全后端 grep `working_memory` / `unresolved_items`**，确认除 v1 维护点外无消费点；测试侧 grep `test_*` 中所有 `.context_pack[...]` 引用，按需改写或删除。

## 10. 验收标准

落地后必须满足以下可观测验收：

- 同一 session 内，所有 LLM 调用（`LocalLLMModel.generate` / `generate_stream` 入口）传入的 `messages` 列表中：
  - 第 0 条（system message）的 `content` 在该 session 全部调用中**字节级相同**。
  - 第 1 ~ N-1 条 user/assistant/tool 消息在该 session 同 profile 下的同一前缀切片上**字节级相同**（仅最后一条 user 消息因 dynamic tail 增长而变化）。
- 任意两次同一 (session, profile) 内的 LLM 调用，**system message 的 token 序列完全相同**。
- 同一 session 第 N 轮 (N≥2) 的 LLM 调用与第 1 轮的 LLM 调用比对，**前 (当前轮之前的) 所有 message 字节级相同**（包括图片 base64 字节）。
- 关闭 `runtime_prompts.enable_runtime_time_context` 后，system message 不再含 `"Current local time"` 字样；打开后，同 session 内该字样不变。
- 关闭 `config.agent.coordinator_1_timeout_seconds` 时不出现（仅验证可配置化）。
- `state["context_pack"]` 在新 session 中**首次写入后永不被读取**（grep 验证零消费）。
- `session.behavior_records` 不再写入 `working_memory` / `unresolved_items` / `tool_ledger`（grep 验证零写入）。
- `_compile_context_pack`（若保留）返回结构中不含 `working_memory` / `unresolved_items` / `agent_contract` / `recent_visible_history` / `user_goal` 字段；或该方法整体删除。
- 全部现有 `tests/test_multi_agent_runner.py` / `tests/test_timeline_projection.py` / `tests/test_agent_session_context.py` / `tests/test_multi_tool_parsing.py` 测试在 v2 改动后**通过**（必要时按 v2 新契约改写 mock 与断言）。

## 11. 参考

- 上一版 SPEC: `docs/superpowers/specs/2026-05-28-prefix-cache-stability-design.md`（v2 取代）
- 多阶段协调器基础: `docs/superpowers/specs/2026-05-13-unified-multi-agent-message-model-design.md`
- 统一消息模型 memo: `UNIFIED_MESSAGE_MODEL_MEMO.md`（v2 同步裁剪 `working_memory` / `unresolved_items` 章节）
- 项目协作契约: `AGENTS.md`（v2 落地后须同步更新相关章节）
- 配套编码计划: `docs/superpowers/specs/2026-06-02-prefix-cache-stability-v2-plan.md`
