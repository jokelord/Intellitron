# Prefix Cache Stability Design

## Goal

最大化 SGLang / vLLM prefix cache 命中率，让同一 session 内的 prompt 前缀尽量保持字节级稳定。

## Background

当前系统已经具备多阶段 coordinator、session pinned runtime context、结构化 context pack、以及前端按 session 复用状态的基础能力。但现有链路里仍存在若干会破坏 prefix cache 的来源：

- 旧历史在重建时被重新序列化
- 可变时间信息若每轮刷新，会进入 system prompt 前缀
- 可见 transcript 若原样回灌，会把 UI 噪音带入模型输入
- 结构化摘要若覆盖式更新，会改写前缀 token 序列

本设计要求：

1. 每一轮新增的动态增量尽量按顺序追加到固定前缀后面。
2. 非新增但会随对话推进变化的部分，必须放在前缀末尾的动态区，并采用 append-only 或版本化追加。
3. 不允许把已封存历史 turn 重新格式化后写回前缀主体。
4. 不在 Phase 1 引入 per-turn LLM 摘要器作为主链路依赖。

## Non-Goals

- 不在本轮引入语义蒸馏式历史重写。
- 不把 raw transcript 直接作为模型主上下文主体。
- 不为 future agents 设计独立的 prompt 协议。
- 不强制把所有历史压成单一自然语言摘要。

## Product Decision

采用“稳定前缀 + 动态尾部 + 冻结历史快照 + 结构化 sidecar”的方案：

1. 稳定前缀只包含不会在同 session 内变化或只在新 session 初始化时变化的内容。
2. 动态内容统一放在前缀末尾，按 turn 追加，不回写旧段。
3. 历史摘要如果未来需要，只允许版本化追加，不允许覆盖旧版本。
4. `runtime_context` 只在新 session 首次生成一次，然后固定到该 session。
5. `session_id` 仅用于会话管理与路由，不进入 prompt 主体。

## Architecture Overview

### 1. Stable Prefix

稳定前缀是 prefix cache 需要尽量复用的部分，推荐包含：

- 固定 system prompt
- 固定工具 schema / tool allowlist
- session-pinned runtime context
- profile contract / agent role description

原则：这一段在同一 session 内不应随轮次变化。

### 2. Dynamic Tail

动态尾部是每轮追加的部分，推荐包含：

- 当前轮 user message
- 当前轮 assistant message / reasoning delta
- 当前轮 tool call / observation
- 当前轮 agent switch 结果
- 当前轮新增摘要版本条目

原则：只 append，不重写，不前插。

### 3. Frozen History Snapshot

已完成 turn 一旦封存，就以不可变 snapshot 的形式保留。后续轮次只读，不重写。

建议 snapshot 记录：

- turn_id
- sequence index
- 已完成的 prompt 片段
- 已完成的结构化摘要版本号

### 4. Structured Sidecars

像 `route_receipt`、`working_memory`、`handoff_ledger`、`unresolved_items` 这类字段，推荐作为结构化 sidecar 保存：

- 仅作为 context compiler 输入
- 不直接当作可见 transcript 回灌
- 不把旧值覆盖式写回 prompt 主体

## Data Rules

### Stable / Dynamic / Versioned classification

| 类别 | 归属 | 规则 |
|---|---|---|
| `system prompt` | Stable prefix | 同 session 内固定 |
| `tool schema` | Stable prefix | 仅 profile 变化时变化 |
| `runtime_context` | Stable prefix | 新 session 首次生成后固定 |
| `session_id` | Store only | 不进入 prompt 主体 |
| `current user goal` | Dynamic tail | 每轮可变，追加到尾部 |
| `raw transcript` | Frozen history / dynamic tail | 封存后不可重写 |
| `message_delta` | Dynamic tail | 只追加 |
| `summary` | Versioned sidecar | 只能 `summary_vN` 追加，不能覆盖 |
| `route_receipt` | Structured sidecar | append/update allowed in storage, not prompt rewrite |
| `working_memory` | Structured sidecar | append-first |
| `handoff_ledger` | Structured sidecar | append-only |
| `unresolved_items` | Structured sidecar | append/update in storage, prompt uses latest compiled view |

### Runtime time rule

- 新 session：第一次进入 `coordinator_2` / `basic_agent` 时生成一次 `Current local time: ...`
- 同 session 后续 turn：复用原值
- `coordinator_1`：永不注入 runtime time

### Historical summary rule

如果未来引入历史摘要：

- 只能产生新版本 `summary_vN`
- 旧版本保留用于审计或回放
- 当前 prompt 只消费最新版本的摘要指针
- 不允许直接改写旧摘要文本

## Implementation Constraints

1. `write_memory_to_messages()` 不得重排已封存历史 turn 的语义结构。
2. `prepare_session_for_new_turn()` 只能清理 transient state，不得破坏冻结历史。
3. `context_pack` 保持结构化，不得把 raw transcript 作为主体字段直接展开进前缀。
4. 前端 `systemPrompt` 在一轮会话内应视为不可变。
5. 前端 runtime time 只能在新 session 初始化时产生一次，不得在同 session 内重新生成。
6. 任何动态字段若会变化，必须放在稳定前缀之后的尾部区域。

## Risks and Mitigations

### Risk 1: 旧历史被重新序列化

Mitigation:

- 冻结已完成 turn 的序列化结果
- 新轮只 append delta

### Risk 2: runtime time 破坏 prefix reuse

Mitigation:

- session-pinned runtime context
- 新 session 才重新生成

### Risk 3: 摘要覆盖旧值导致前缀变化

Mitigation:

- 版本化摘要
- 只追加，不覆盖

### Risk 4: raw transcript 污染 prompt

Mitigation:

- 只保留结构化 sidecar + frozen snapshot
- 不直接回灌 UI transcript

## Acceptance Criteria

- 同一 session 内，除新增尾部外，稳定前缀保持不变。
- `Current local time` 在同 session 内不变化。
- 已封存历史 turn 不因后续轮次被重写。
- 摘要若未来启用，只能版本化追加。
- `session_id` 不进入模型 prompt 主体。
- `raw transcript / message_delta` 不进入稳定前缀主体。
