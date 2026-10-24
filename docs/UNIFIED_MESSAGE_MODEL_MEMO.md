# Unified Message Model Memo

本备忘用于快速对齐多阶段 coordinator + 统一消息模型的关键 payload 形态。

适用范围：

- `coordinator_1`
- `coordinator_2`
- `basic_agent`
- future agents

当前阶段：Phase 1

- 只使用 deterministic compiler
- 不引入 per-turn LLM 摘要

## 0. Pinned Runtime Prompt

- `session-sticky` here means `会话级固定`：同一 session 内复用同一份 runtime prompt，不在每次请求时重新生成。
- 对这一 session 来说，时间提示词是 pinned/fixed 的历史，不会随着钟表时间推进而刷新。
- 这样 LLM 看到的时间信息保持稳定，旧轮次不会被改写，也更利于 prefix cache 命中。

## 1. Storage Tier 1: Raw Timeline

Tier 1 保存全量事件，是系统回放、审计、调试、前端真实流式投影的来源。

### 示例 1：routing delta

```json
{
  "kind": "routing_delta",
  "event_id": "evt_000101",
  "session_id": "sess_001",
  "turn_id": "turn_014",
  "source": "coordinator_1",
  "message_id": "routing-card-1",
  "text": "正在分析当前请求应该交给哪一个 agent...",
  "partial": true,
  "timestamp": 1778600000.125
}
```

### 示例 2：routing timeout fallback

```json
{
  "kind": "routing_timeout_fallback",
  "event_id": "evt_000117",
  "session_id": "sess_001",
  "turn_id": "turn_014",
  "source": "system",
  "from_agent": "coordinator_1",
  "target_agent": "basic_agent",
  "reason": "timeout_fallback",
  "timeout_seconds": 10,
  "timestamp": 1778600010.004
}
```

### 示例 3：coordinator_2 可见消息流

```json
{
  "kind": "message_delta",
  "event_id": "evt_000130",
  "session_id": "sess_001",
  "turn_id": "turn_014",
  "source": "coordinator_2",
  "message_id": "assistant-msg-42",
  "display": "chat_bubble",
  "text": "我们先从方程的边界条件开始讨论。",
  "partial": true,
  "timestamp": 1778600012.441
}
```

### 示例 4：basic_agent tool call

```json
{
  "kind": "tool_call_started",
  "event_id": "evt_000149",
  "session_id": "sess_001",
  "turn_id": "turn_014",
  "source": "basic_agent",
  "message_id": "agent-step-7",
  "tool_name": "read_file",
  "arguments": {
    "path": "/tmp/demo.txt"
  },
  "timestamp": 1778600018.930
}
```

## 2. Storage Tier 2: Behavior Records

Tier 2 是从 Raw Timeline 中归纳出的结构化行为记录，用于后续编译 Context Pack。

### 示例 1：route receipt

```json
{
  "record_type": "route_receipt",
  "record_id": "route_014",
  "session_id": "sess_001",
  "turn_id": "turn_014",
  "decider": "coordinator_1",
  "decision": "coordinator_2",
  "reason_summary": "该请求需要复杂推理，暂不需要工具调用。",
  "timeout_fallback": false,
  "raw_transcript_ref": {
    "start_event_id": "evt_000101",
    "end_event_id": "evt_000116"
  },
  "created_at": 1778600009.998
}
```

### 示例 2：handoff record

```json
{
  "record_type": "handoff_record",
  "record_id": "handoff_014_2",
  "session_id": "sess_001",
  "turn_id": "turn_014",
  "from_agent": "coordinator_2",
  "to_agent": "basic_agent",
  "why": "推导到中途发现需要读取本地文件验证参数。",
  "carry_forward": [
    "已确认目标方程形式正确。",
    "下一步需要读取 /tmp/demo.txt 中的参数值。"
  ],
  "created_at": 1778600021.221
}
```

### 示例 3：tool ledger item

```json
{
  "record_type": "tool_ledger_item",
  "record_id": "tool_014_1",
  "session_id": "sess_001",
  "turn_id": "turn_014",
  "agent": "basic_agent",
  "tool_name": "read_file",
  "arguments": {
    "path": "/tmp/demo.txt"
  },
  "result_summary": "成功读取参数文件，提取到 alpha=0.73, beta=1.22。",
  "observation_ref": {
    "start_event_id": "evt_000149",
    "end_event_id": "evt_000153"
  },
  "status": "completed",
  "created_at": 1778600024.002
}
```

### 示例 4：（v2 删除）unresolved_items 字段不再维护

> 从 v2 版本 Prefix Cache Stability 起，`unresolved_items` / `working_memory` / `tool_ledger` /
> `recent_visible_history` / `agent_contract` 不再作为 Tier 2 字段维护，也绝不进入
> LLM prompt 主体。如需历史压缩或待办列表，必须走新 SPEC + RFC。

## 3. Storage Tier 3: Context Pack

Tier 3 仅为 **Tier 2 审计快照**。从 v2 起，其内容**不进入** LLM prompt 主体，详见
`docs/superpowers/specs/2026-06-02-prefix-cache-stability-v2-design.md`。

`AgentRunner._compile_context_pack()` 当前仅返回 `{"routing_receipt": ...}` 一项。
历史 v1 字段（`user_goal` / `agent_contract` / `recent_visible_history` / `working_memory` /
`unresolved_items` / `tool_ledger` / `handoff_ledger`）已全部从返回值中删除。

## 4. Per-Agent Context Pack 示例 (Tier 3)

### 4.1 `coordinator_1` Context Pack（v2 模板）

```json
{
  "routing_receipt": {
    "decider": "coordinator_1"  // 或 system (timeout_fallback)
    "decision": "basic_agent"  // 路由目标
    "reason_summary": "...",
    "timeout_fallback": false
  }
}
```

### 4.2 `coordinator_2` Context Pack（v2 模板）

```json
{
  "routing_receipt": {
    "decider": "coordinator_1",
    "decision": "coordinator_2",
    "reason_summary": "判断为复杂推理任务",
    "timeout_fallback": false
  }
}
```

### 4.3 `basic_agent` Context Pack（v2 模板）

```json
{
  "routing_receipt": {
    "decider": "coordinator_1",
    "decision": "basic_agent",
    "reason_summary": "...",
    "timeout_fallback": false
  }
}
```

### 4.4 future agent Context Pack（v2 模板）

```json
{
  "routing_receipt": {
    "decider": "coordinator_1 or coordinator_2",
    "decision": "future_agent_x",
    "reason_summary": "..."
  }
}
```

> v2 强调：上述 Context Pack **仅作为 Tier 2 审计快照**，不喂入 LLM prompt 主体。
> LLM 实际输入 = `system_prompt`（含 `runtime_context`）+ `write_memory_to_messages()` 序列化结果。

## 5. 摘要策略说明（v2 永久原则）

- **永久禁止** per-turn LLM 摘要。
- 历史压缩只能走新 SPEC + RFC，不得在主链路偷偷启用。
- `working_memory` / `unresolved_items` 等 v1 摘要类 sidecar 已被 v2 删除；不要在 v2 之后
  重新引入，除非走完整 RFC 流程。

## 6. 实施提示

- 可见消息层和 agent 行为层必须解耦。
- `coordinator_2` 的全量可见流，不等于后续必须把其全文原样塞回模型上下文。
- future agents 必须优先适配 Tier 1/2/3 （含 Context Pack），不要绕开统一消息模型直接读前端 transcript。
