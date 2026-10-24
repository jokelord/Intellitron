# OUTDATED — v2 Compact Prompt — Turn-Shape (historical design)

> **OUTDATED / DO NOT USE AS CURRENT DESIGN (2026-06-14):** This document is kept only as historical context. Its early tail-only / byte-equal prefix-cache assumptions conflict with the current principle of preserving reliable compact behavior first. Current work must follow `docs/superpowers/specs/2026-06-13-compact-prompt-shape-prefix-cache-prd.md` and `docs/superpowers/plans/2026-06-14-compact-prompt-shape-prefix-cache-plan.md`.

> 版本: v2 (replaces `2026-06-06-slash-commands-and-compact-design.md` 中 §8.4 "Compact prompt templates" 章节的 prompt 形态定义)
> 生效范围: `backend/compact/` · `backend/agent/agent_runner.py` · `backend/api/chat.py` · `backend/llm_service.py` · `config/config-agent-prompts.yaml` · `tests/test_compact_*.py`
> 配套踩坑记录: `踩坑记录4.md` §3-§5

> **2026-06-13 correction:** this document is now a historical record, not the source of truth for current compact prompt shape. The original "tail-only compact instruction + byte-equal active system/history" design was changed after E2E failures: long-context models ignored tail instructions, and agent system prompts caused compact to output routing XML/tool-call text instead of summaries. Current code uses a compact-specific system prompt in agent mode and inserts the compact instruction at position 1. See `docs/superpowers/specs/2026-06-13-compact-prompt-shape-prefix-cache-prd.md` for the current architecture direction.

## 1. 背景与本版本要解决的问题

### 1.1 v1 现状

v1 compact prompt (`backend/compact/prompt_builder.py::build_compact_messages`) 把整段对话**打包成单条 user message**：

```python
# v1 实际产出
[
    {"role": "system", "content": "<no_tools_preamble> + <main_prompt> + ... + <no_tools_trailer>"},
    {"role": "user", "content": "Conversation to summarize:\n\n[user] A 说：xxx\n[assistant] 回：yyy\n[user] B 说：zzz\n..."},
]
```

`build_compact_messages` 接收 `_session_memory_to_messages(session)` 的 flat pack 输出（body-only，不含 system），在内部用 `prompts.no_tools_preamble + main_prompt + no_tools_trailer` 拼一个**自己的** system message，整段对话作为单条 user 段。

### 1.2 v1 的两个根本问题

1. **SGLang prefix cache 几乎完全失效**。v1 送进 SGLang 的 token 序列 = `[system(compact 指令), user(整段对话)]`。这与之前 chat 调用的 token 序列 = `[system(agent profile), user, assistant, user, assistant, ...]` **完全不同**——`system` 段不同（一个是 agent profile + tools schema + runtime_context，~5-10k tokens；另一个是 compact 指令 ~500 tokens），`user` 段更不同（一个是单条大字符串，一个是多个独立消息）。SGLang radix tree 在 root 就分叉，**整个 prompt 都得重新 prefill**。这是 `踩坑记录4.md` §4.3 因果链 [6]-[8] 的核心：v1 compact 触发的 SGLang 调度器单次 prefill token 数 = `len(system_指令) + len(整段对话)` ≈ 5k + 40k = **45k tokens**。
2. **整段对话被打成一条 user 字符串，丢失了 role 切换边界**。chat template 渲染时这 45k tokens 都是同一个 `<|im_start|>user\n...<|im_end|>\n` 块，无法复用之前 chat 调用的 KV 块（之前 chat 调用的 KV 块按 `<|im_start|>user\nA<|im_end|>\n<|im_start|>assistant\nB<|im_end|>\n...` 这样的细粒度分段存储）。

### 1.3 当前实现的核心原则（2026-06-13 修正）

当前实现优先保证 compact 产出可靠 summary，而不是强行保持与上一轮 chat 调用 byte-equal。

实际形态如下：

- Agent 模式：`AgentRunner._build_compact_body_messages()` 用 `compaction.no_tools_preamble + main_prompt + no_tools_trailer` 覆盖 `body_messages[0]`，避免 active agent 的 routing/tool system prompt 压过 compact 指令。
- `build_compact_messages_v2()` 把 compact instruction 插入 position 1，即 `[body_messages[0], compact_instruction, ...body_messages[1:]]`，不是尾部追加。
- 因此当前 agent compact 与上一轮 agent/chat 请求从 token 0 或很早位置分叉，SGLang 日志里 `#cached-token: 0` 是符合现行 prompt shape 的结果。
- 未来若要恢复更高 prefix-cache 命中率，必须走 `2026-06-13-compact-prompt-shape-prefix-cache-prd.md` 中的 cache-first + validation + fallback 方案，而不是简单回滚到本文件早期的 tail-only 设计。

### 1.4 关键参考：SGLang radix tree 匹配行为

SGLang radix cache 匹配是**逐 token 整数相等**（`sglang/srt/mem_cache/radix_cache.py:191-211`），没有 hashing、没有 tolerance。匹配**不**是按 role 边界切分，**不**是按 message 边界切分，是按**整个 token 序列的最长公共前缀**切分。Hybrid Mamba+attention 模型用 `MambaRadixCache`（`sglang/srt/mem_cache/mamba_radix_cache.py:423-454`），**attention KV 与 mamba state 共用同一棵 radix tree**（`value` 和 `mamba_value` 在同一节点上），mamba 状态在 chunk-aligned 位置 checkpoint（`mamba_radix_cache.py:1035-1044`），匹配时**复用设备 KV + 复用设备 mamba state**（device-to-device copy），仅在 chunk 边界处有 ≤ 1 个 `mamba_cache_chunk_size`（通常 64）token 的 mamba state 重算。

**因此**：让 compact 调用与之前 chat 调用 token 序列**精确相同**，SGLang 会**精确**复用前 N 个 token 的 attention KV + 相应的 mamba state。

### 1.5 关键参考：smolagents `write_memory_to_messages` 已经是 turn-shape 输出

`SmartToolCallingAgent.write_memory_to_messages`（`backend/agent/agent_runner.py:1211-1645`）的输出**已经是** turn-shape 消息列表。它把 `memory.system_prompt.to_messages()` 渲染为 1 条 SYSTEM 消息，再把每个 `TaskStep` / `ActionStep` 渲染为对应的 USER / ASSISTANT 消息——**role 切换的 token 序列与之前 chat 调用完全一致**（因为每次 chat 调用也走 `write_memory_to_messages`，输出完全相同的 token 序列）。这正是**项目其它路径**（`routing_messages = list(coordinator_agent.write_memory_to_messages())` 在 `agent_runner.py:2886`、`memory_messages = self.write_memory_to_messages()` 在 `agent_runner.py:1809`）已经在用的 pattern。

**历史说明**：本版本最初要解决的问题是 compact v1 把对话打包成单条 user 字符串。当前代码已经改成 turn-shape history，但又因可靠性要求引入了 compact-specific system override 和 position-1 instruction。

### 1.6 runtime_context 处理（2026-06-13 修正）

compact 路径不应主动重新生成或写入 `session.runtime_context`。但 agent-mode compact 当前也**不**完全继承上一轮 active agent system prompt：`body_messages[0]` 会被 compact-specific system prompt 替换。压缩后下一轮普通 agent/chat 仍通过 session-pinned `runtime_context` 机制保持稳定；summary 不写入 `runtime_context`。

## 2. Goals

- [G1] **Superseded:** 原目标是让 compact 与上一轮 chat token 序列 byte-equal 并最大化 SGLang radix cache 命中。当前实现为保证 summary 可靠性已放弃该目标；未来优化见 `2026-06-13-compact-prompt-shape-prefix-cache-prd.md`。
- [G2] **Superseded:** 原目标是唯一分叉点为末尾 compact-instr user 消息。当前实现的分叉点包括 agent-mode compact system override 和 position-1 compact instruction。
- [G3] 沿用 v1 已有的 `_replace_agent_memory`、`format_compact_summary`、`token count threshold`、`SSE event flow`、`apply_compaction` 回调机制——**不**改变 compact 输出的 envelope 形态、不改变 compact 失败 / 完成的 SSE 事件流、不改变 threshold 策略。
- [G4] 兼容 v1 路径 1 个 release：旧 `build_compact_messages` 保留并标 `@deprecated`；`CompactRequest` 同时支持 `messages` (v1, body-only) 和 `body_messages` (v2, 含 system) 字段，orchestrator 优先用 `body_messages`，缺失时 fallback 到 v1 路径（带 deprecation warning 日志）。
- [G5] 措辞**完全复用** `ref_src/services/compact/prompt.ts`（Claude Code 参考实现）的英文措辞，**不**重新发明 prompt wording。中文翻译版本作为**主用**措辞（与现有 `config/config-agent-prompts.yaml::compaction::main_prompt` 一致），英文原文作为参考实现在该字段 docstring 中交叉引用。
- [G6] **同时重构** agent 模式和 chat 模式的 auto-compact 路径。Cycle 模式的预触发（`cycle/executor.py:160-198`）也同步走新 builder。
- [G7] 现有 20 个 `test_compact_*.py` 测试**全部保留并通过**（v1 路径不退化）。新增针对 v2 的测试套件（红绿 TDD），含 token-template 兼容性测试（用真实 Qwen3 chat_template 渲染）。

## 3. Non-Goals

- [N1] **不**修改 `_replace_agent_memory` 的 envelope 形态（`TaskStep(task=preface+summary+suffix, task_images=None)`）——这是 v2 prefix cache stability 设计的 §5.3 保留扩展路径（`docs/superpowers/specs/2026-06-02-prefix-cache-stability-v2-design.md`）。
- [N2] **不**修改 `format_compact_summary`（`<analysis>` strip + `<summary>` unwrap）—— 输出仍是纯文本 summary。
- [N3] **不**改 threshold 策略（`performance` vs `utilization`），不改 `compute_threshold` / `should_compact`。
- [N4] **不**改 token 计数策略（仍用 `TokenCounter.count_messages`）。
- [N5] **不**改 SSE 事件 schema（`compact_started` / `compact_delta` / `compact_completed` / `compact_failed` 字段保持不变）。
- [N6] **不**改前端协议（`runCompactSSE` 仍消费相同事件流，前端 `applyCompactProjection` 不变）。
- [N7] **不**在 v2 路径上做"分多轮 prefill 让每轮享受之前所有轮的 prefix cache"的多 pass 优化——这是单独的设计提案，本 spec 范围之外。
- [N8] **不**修改 `coordinator_1_routing_*` 相关参数（`coordinator_1` 走它自己的 routing 路径，与 compact 无关）。
- [N9] **不**修改 smolagents 库本身（`/home/zk/Project/LLM-deployment/.sglang/lib/python3.12/site-packages/smolagents/`）。
- [N10] **不**修改 SGLang 启动参数（`scripts/1_start_sglang.sh`、`config/qwen3__5-config-infer.yaml`）—— mamba / KV 调优已在 `踩坑记录4.md` 中落地。

## 4. 架构：组件、数据流、调用栈

### 4.1 核心组件

| 组件 | 文件 | 角色 |
|---|---|---|
| `CompactRequest` | `backend/compact/types.py` | 接受 `body_messages: List[Message]`（v2 新增）+ `messages: List[Any]`（v1 兼容，标 deprecated）+ `session_id: str` + `mode: str` + `trigger: str` + `custom_instructions: str` |
| `build_compact_messages_v2` | `backend/compact/prompt_builder.py` | 接收 `body_messages`（**已含 system**，来自调用者），在 position 1 插入 compact-instr user 消息，返回 `[body_messages[0], instruction, ...body_messages[1:]]`。 |
| `build_compact_messages` (v1) | 同上 | **保留** + `@deprecated` docstring。v1 路径 1 个 release 内仍可用 |
| `CompactOrchestrator.run_compact` | `backend/compact/orchestrator.py` | 优先用 `req.body_messages` 走 v2 路径；缺失时 fallback 到 v1 路径（用 `req.messages` + `build_compact_messages` v1）。**移除** v1 的 `_collect_messages`（v1 路径也直接用 `req.messages`，不再做兜底） |
| `AgentRunner._run_compact_then_continue` | `agent_runner.py:4515-4557` | **重构**：调用 `session.agent.write_memory_to_messages()`，转为 `List[Message]`，传给 `CompactRequest.body_messages` |
| `AgentRunner.run_compact_for_session` | `agent_runner.py:4559-4634` | **重构**：同上 |
| `AgentRunner._should_auto_compact_session` | `agent_runner.py:4451-4477` | **重构**：用 `session.agent.write_memory_to_messages()` 作为 messages source（与 v2 compact body 同源，确保 token count 与实际发送一致） |
| `AgentRunner._session_memory_to_messages` | `agent_runner.py:4428-4449` | **保留**但只用于 chat 模式 auto-compact 前的 token 估算（chat 模式不走 `write_memory_to_messages`，因为 chat 没有 agent session） |
| `_generate_stream_response` | `backend/api/chat.py` | Chat auto-compact 先对 `filter_messages_for_llm()` 后的历史做 compact。若最后一条是当前 user，则 compact body 使用 `filtered[:-1]`，compact 完成后 `final_messages = [summary_envelope, current_user]`。 |
| `_should_auto_compact_chat` | `api/chat.py:350-387` | **保留**：仍用 `_session_memory_to_messages` 逻辑把 `filtered` 摊平做 token count |
| `cycle/executor.py:160-200` | `cycle/executor.py` | **保留**：调用 `_agent_runner._run_compact_then_continue(session, trigger='auto')`（自动跟随 agent runner 的重构） |
| `format_compact_summary` | `backend/compact/summary_runner.py` | **不变** |
| `filter_messages_for_llm` | `backend/compact/chat_message_filter.py` | **不变**（trap #19 仍适用：metadata 标记 → envelope 重构） |
| `TokenCounter` | `backend/compact/tokenizer.py` | **不变**（v2 token count source 变化不需改 TokenCounter 自身） |

### 4.2 数据流（v2 完整路径）

```
                       ┌──────────────────────────────┐
                       │   session.agent              │  (agent 模式)
                       │     ├ memory.system_prompt   │
                       │     └ memory.steps           │
                       └──────────────┬───────────────┘
                                      │
                       write_memory_to_messages()
                                      │
                                      ▼
                       List[ChatMessage]  (smolagents 类型)
                                      │
                          smolagents ChatMessage
                            → backend.llm_service.Message
                                      │
                                      ▼
                       List[Message]  (含 system)
                                      │
                                      ▼
   ┌──────────────────────────────────┴───────────────────────────────────┐
   │                                                                      │
   ▼                                                                      ▼
[_should_auto_compact_session]                              [_run_compact_then_continue]
  TokenCounter.count_messages(msgs)                         CompactRequest(
        │                                                        mode="agent",
        ▼                                                        body_messages=msgs,
  compute_threshold + should_compact                             session_id=session.session_id,
        │                                                        trigger="auto"
        ▼                                                      )
   True / False                                                       │
        │ True                                                       ▼
        │                                              CompactOrchestrator.run_compact
        ▼                                                       │
   trigger auto-compact                                            ▼
                                                       build_compact_messages_v2(
                                                           body_messages=msgs,
                                                           prompts=prompts_cfg,
                                                           custom_instructions=req.custom_instructions,
                                                       )
                                                                │
                                                                ▼
                                                       末尾追加 1 条 user 消息
                                                       (compact-instr)
                                                                │
                                                                ▼
                                                       List[Message]  (含 system + history + compact-instr)
                                                                │
                                                                ▼
                                                       self.llm_service.chat_stream_async(
                                                           messages=final_messages,
                                                           max_new_tokens=...,
                                                           temperature=...,
                                                           top_p=...,
                                                           tools=None,
                                                           request_id=summary_id,
                                                       )
                                                                │
                                                                ▼
                                                       SGLang  ──> streaming deltas
                                                                │
                                                                ▼
                                                       format_compact_summary(accumulated)
                                                                │
                                                                ▼
                                                       _replace_agent_memory(session, cleaned, summary_id)
                                                       (envelope = TaskStep(preface+summary+suffix))
```

```
                       ┌──────────────────────────────┐
                       │  filtered (含 system/current)│  (chat 模式)
                       │  来自 _generate_stream_response
                       └──────────────┬───────────────┘
                                      │
                                      ▼
                       if last filtered msg is user:
                         compact_body = filtered[:-1]
                         current_user = filtered[-1]
                       else:
                         compact_body = filtered
                         current_user = None
                                      │
                                      ▼
                       _should_auto_compact_chat(compact_body)
                                      │
                                      ▼
                       True / False
                                      │
                                      ▼  True
                       CompactRequest(
                           mode="chat",
                           body_messages=compact_body,
                           session_id=...,  # 来自 raw_request (可选)
                           trigger="auto",
                       )
                                      │
                                      ▼  (与 agent 模式汇合)
                       build_compact_messages_v2 + chat_stream_async
                                      │
                                      ▼
                       rebuild final_messages =
                         [Message(role="user", content=envelope), ...filtered]
```

### 4.3 调用栈（与之前 chat 调用对比）

**之前 chat 调用**（agent 模式）：
```
session.agent.write_memory_to_messages()
  └─> List[ChatMessage]  (smolagents 标准序列化)
        └─> get_clean_message_list(..., role_conversions=tool_role_conversions)
              └─> List[dict]  (合并相邻同角色 + role 转换)
                    └─> Message(role=, content=)
                          └─> LLMService.chat(messages=[...])
                                └─> SGLangProvider.generate(stream=...)
                                      └─> httpx POST /v1/chat/completions
```

**之前 chat 调用**（chat 模式）：
```
filtered (前端发送 + 后端 filter_messages_for_llm)
  └─> Message(role=, content=)  (逐条)
        └─> LLMService.chat(messages=[...])
              └─> 同上
```

**v2 compact 调用**（agent 模式）：
```
session.agent.write_memory_to_messages()
  └─> List[ChatMessage]
        └─> Message(role=, content=)
              └─> AgentRunner._build_compact_body_messages()
                    └─> body_messages[0] = compact-specific system prompt
                          └─> CompactRequest(body_messages=[compact_system, ...history])
                                └─> build_compact_messages_v2: insert instruction at position 1
                                      └─> LLMService.chat_stream_async(messages=[compact_system, instruction, ...history])
                                └─> SGLangProvider.generate(stream=...)
                                      └─> httpx POST /v1/chat/completions  # **与之前 chat 调用同一个端点**
```

**v2 compact 调用**（chat 模式）：
```
filtered = filter_messages_for_llm(original_chat_messages)
  └─> if filtered[-1].role == "user": compact_body = filtered[:-1]
  └─> CompactRequest(body_messages=compact_body)
        └─> build_compact_messages_v2: insert instruction at position 1
              └─> chat_stream_async
```

**关键事实**：
- 当前 v2 compact 调用**不是** tail-only byte-equal 形态。
- Agent 模式会覆盖 system prompt；chat/agent 都会在 position 1 插入 compact instruction。
- 因此本文件早期的“仅末尾 compact 指令 miss、其余全部 cache hit”推断不再成立。
- 未来若要恢复 cache-first prompt shape，必须先实现观测、输出校验和 fallback。

## 5. 数据规则（Data Rules）

### 5.1 CompactRequest 字段契约

```python
@dataclass
class CompactRequest:
    mode: Mode                                          # "chat" | "agent" | "cycle"
    session_id: Optional[str] = None
    body_messages: Optional[List[Message]] = None       # v2 主路径 (含 system 的完整 messages)
    messages: Optional[List[Any]] = None                # v1 兼容 (body-only, 标 deprecated)
    trigger: Trigger = "manual"
    custom_instructions: Optional[str] = None
```

- 优先级：`body_messages` > `messages`（v1）。
- 校验：`body_messages` 非空且第一条 `role == "system"`，否则抛 `ValueError`。
- 校验：`body_messages` 每条 `Message.content` 必须是 `str`（不是 `list[dict]`，因为 `write_memory_to_messages` 输出 list content 在 compact 路径上要 flatten 成 str）。
- 校验：`messages` 字段已标 deprecated；调用者传 `messages` 时 orchestrator 打 warning 日志（"v1 path is deprecated since 2026-06-11, will be removed in next release; use body_messages"）。

### 5.2 build_compact_messages_v2 行为契约

```python
def build_compact_messages_v2(
    body_messages: List[Message],
    prompts: CompactionPromptsConfig,
    custom_instructions: Optional[str] = None,
) -> List[Message]:
    """v2 current: keep body_messages[0], insert compact-instr user message at position 1."""
    if not body_messages:
        raise ValueError("body_messages 不能为空")
    if body_messages[0].role != "system":
        raise ValueError("body_messages 第一条必须是 system 消息")

    parts: List[str] = [prompts.compact_user_instruction]
    if custom_instructions and custom_instructions.strip():
        parts.append(prompts.custom_instructions_header)
        parts.append(custom_instructions.strip())
    instruction = Message(role="user", content="\n\n".join(parts))

    return [body_messages[0], instruction] + list(body_messages[1:])
```

**关键不变式**：
- `len(final) == len(body_messages) + 1`
- `final[0] == body_messages[0]`
- `final[1].role == "user"`
- `final[1].content` 包含 `prompts.compact_user_instruction` 的全部文本
- `final[2:] == body_messages[1:]`

### 5.3 措辞契约（`compaction:` 块新增字段）

参考 `ref_src/services/compact/prompt.ts` 的措辞。在 `config/config-agent-prompts.yaml::compaction` 块新增：

```yaml
compaction:
  # 现有字段保留
  status_message: "Compressing conversation context..."
  no_tools_preamble: |     # 来自 ref_src/services/compact/prompt.ts:19-26
    CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

    - Do NOT use Read, Bash, Grep, Glob, Edit, Write, or ANY other tool.
    - You already have all the context you need in the conversation above.
    - Tool calls will be REJECTED and will waste your only turn — you will fail the task.
    - Your entire response must be plain text: an <analysis> block followed by a <summary> block.
  main_prompt: |           # 来自 ref_src/services/compact/prompt.ts:61-143 (BASE_COMPACT_PROMPT)
    Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
    This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.
    ...
    [详见 spec §5.3 完整文本]
  custom_instructions_header: "== USER CUSTOM INSTRUCTIONS =="
  no_tools_trailer: "REMINDER: Do NOT call any tools. Respond with plain text only — an <analysis> block followed by a <summary> block. Tool calls will be rejected and you will fail the task."
  summary_envelope_preface: |  # 来自 ref_src/services/compact/prompt.ts:345-347
    This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.
  summary_envelope_suffix: "Continue the conversation from where it left off without asking the user any further questions. Resume directly — do not acknowledge the summary, do not recap what was happening, do not preface with \"I'll continue\" or similar. Pick up the last task as if the break never happened."
  # 新增字段
  compact_user_instruction: |  # 来自 ref_src/services/compact/prompt.ts:61-143 + 末尾追加的 "above conversation" 锚定
    Please provide your summary based on the conversation above, following this structure and ensuring precision and thoroughness in your response.

    The "conversation above" refers to the messages that appear earlier in this message list, NOT including this current instruction message. Summarize those earlier messages only.

    There may be additional summarization instructions provided in the included context. If so, remember to follow these instructions when creating the above summary.
```

**措辞策略**：
- **完全复用** `ref_src/services/compact/prompt.ts` 的英文措辞（`BASE_COMPACT_PROMPT` 主段、`NO_TOOLS_PREAMBLE` 前置、`NO_TOOLS_TRAILER` 后置）。
- 中文翻译**不**做（与参考实现交叉验证，英文版更精确）。如有需要，二期再做翻译。
- `compact_user_instruction` 字段是从 `BASE_COMPACT_PROMPT` 末尾的"providing your summary based on the conversation so far"演化而来，加上一句**显式锚定 "The 'conversation above' refers to the messages that appear earlier in this message list, NOT including this current instruction message"**——这是 v2 设计的核心，明确告诉 LLM 压缩范围是"以上对话"，**不**包括本条 instruction 自身。

### 5.4 转换契约（smolagents ChatMessage → backend Message）

```python
def _smolagents_to_backend_message(chat_msg: ChatMessage) -> Message:
    """将 smolagents ChatMessage 转为 backend.llm_service.Message.

    关键点:
      - role: ChatMessage.role 是 MessageRole enum, .value 拿到 "user" / "assistant" / "system" / "tool_call" / "tool_response"
      - content: ChatMessage.content 是 list[dict] (multimodal shape), 转为 str
    """
    role_str = chat_msg.role.value if hasattr(chat_msg.role, "value") else str(chat_msg.role)
    if role_str in ("tool_call", "tool_response"):
        # write_memory_to_messages 已通过 role_conversions 把 TOOL_CALL → ASSISTANT / TOOL_RESPONSE → USER;
        # 但 write_memory_to_messages 之前可能 emit 一条 role=TOOL_CALL 的 ChatMessage.
        # 为安全起见, 显式归一化.
        role_str = "assistant" if role_str == "tool_call" else "user"

    content = chat_msg.content
    if isinstance(content, list):
        # 提取所有 text parts, 拼成 str. 忽略 image parts (compact 路径不携带图片).
        text_parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        content = "\n".join(p for p in text_parts if p)
    elif content is None:
        content = ""
    elif not isinstance(content, str):
        content = str(content)

    return Message(role=role_str, content=content)
```

### 5.5 与 v1 的差异（Diff）

| 维度 | v1 | v2 |
|---|---|---|
| System 段来源 | compact 自己的 system 指令（`no_tools_preamble + main_prompt + no_tools_trailer`） | **当前 agent 模式仍使用 compact-specific system override**；chat 模式使用请求里的 system/history |
| History 形态 | 单条 user 字符串 `[user] A 说：xxx\n[assistant] 回：yyy\n...` | **多消息列表**，但 compact instruction 插在 position 1 |
| SGLang prefix cache 命中 | 几乎不命中上一轮 chat prefix | **当前实现不保证命中上一轮 chat prefix**；agent 模式从 compact system 起分叉，position-1 instruction 也会早分叉 |
| builder 输出 | 2 条消息 `[system, user<整段历史>]` | N+1 条消息 `[system, user<compact-instr>, ...history...]` |
| `_session_memory_to_messages` 调用方 | `_run_compact_then_continue` + `run_compact_for_session` + `_should_auto_compact_session` | **`_should_auto_compact_chat` 仍用**（chat 模式没有 agent session）；agent 模式改用 `write_memory_to_messages` |
| `_collect_messages` 兜底 | 存在（orchestrator 内部） | **移除**（v1 路径也直接用 `req.messages`，不再做兜底） |

## 6. 实现约束（Implementation Constraints）

### 6.1 必须遵守

- [I1] v2 builder 接收的 `body_messages` 不做 flatten/render；但它会在 position 1 插入 compact instruction。Agent 模式在进入 builder 前会由 `_build_compact_body_messages()` 覆盖 `body_messages[0]` 为 compact-specific system prompt。
- [I2] `Message.content` 在 v2 路径上**只**支持 `str`，**不**支持 `list[dict]`（图片）。`write_memory_to_messages` 输出的 `list[dict]` content 必须在调用 v2 builder 之前 flat 成 `str`。
- [I3] v2 path **不**主动注入 `runtime_context`——agent 原始 system_prompt 已含 session-pinned `runtime_context`（参考 `docs/superpowers/specs/2026-05-27-runtime-context-time-design.md`），compact 路径只需 1:1 复用，**不**需要也不应该二次注入。
- [I4] v2 path **不**主动注入 `tools` schema（`tools=None` 给 `chat_stream_async`）。Compact LLM 不调用工具，工具 schema 出现在 system 段是因为 system_prompt 是 agent 原始的，**不**额外注入。
- [I5] v1 path 与 v2 path 共用同一个 `CompactOrchestrator` 实例（**不**创建新类）。`run_compact` 入口处做分支：`body_messages` 非空走 v2；否则走 v1。
- [I6] v1 `build_compact_messages` 保留（**不**删除），docstring 加 `@deprecated since 2026-06-11`。
- [I7] v1 `req.messages` 字段保留（**不**删除），`CompactRequest.__post_init__` 不做强制校验；orchestrator 入口处打 warning 日志。
- [I8] `_should_auto_compact_session` 改用 `write_memory_to_messages()` 作为 messages source（agent 模式）；`_should_auto_compact_chat` 仍用 `_session_memory_to_messages` 转换 `filtered`（chat 模式没有 agent session）。
- [I9] 现有 20 个 `tests/test_compact_*.py` 测试**全部保留并通过**——v1 路径不退化。
- [I10] 现有 5 个 `踩坑记录2.md` §1 引用的 `write_memory_to_messages` 历史清洗逻辑**不变**——v2 builder 只是把 `write_memory_to_messages` 的输出原样转发给 LLM，**不**重新发明历史清洗。

### 6.2 不得做

- [D1] **不**修改 `_replace_agent_memory`（`agent_runner.py:4479-4513`）的 envelope 形态（`TaskStep(task=preface+summary+suffix, task_images=None)`）。
- [D2] **不**修改 `format_compact_summary`（`summary_runner.py`）的 `<analysis>` strip / `<summary>` unwrap 逻辑。
- [D3] **不**修改 `filter_messages_for_llm`（`chat_message_filter.py`）的 metadata 标记解释逻辑。
- [D4] **不**修改 `_HIDDEN_TOOL_NAMES` / `initialize_system_prompt` / `tools_and_managed_agents` 隐藏工具的相关逻辑（与 compact 路径无关，参考 AGENTS.md trap #12）。
- [D5] **不**在 v2 path 上做"分多轮 prefill"的多 pass 优化。
- [D6] **不**在 v2 builder 内部对 `body_messages` 做 `get_clean_message_list` 转换——`write_memory_to_messages` 已经应用了 `tool_role_conversions` 和 adjacent-role 合并（参考 `local_llm_model.py:298-302`），v2 builder 只接收 1:1 输出。
- [D7] **不**修改 SGLang 启动参数。
- [D8] **不**修改 smolagents 库。

## 7. 编码计划概要（详见配套 plan 文档）

| Step | 描述 | 验证 |
|---|---|---|
| 1 | **红**：写 `test_compact_prompt_builder_v2.py` + `test_compact_prompt_builder_v2_template_compat.py`（v2 builder 单元测试 + 真实 Qwen3 chat_template 渲染测试）；运行，**期望失败**（v2 builder 还不存在） | `pytest tests/test_compact_prompt_builder_v2.py tests/test_compact_prompt_builder_v2_template_compat.py -q` 应 fail |
| 2 | **绿**：实现 `build_compact_messages_v2`（`prompt_builder.py`）；运行红测试，**期望通过** | `pytest ...v2.py` 应 pass |
| 3 | **红**：写 `test_compact_request_v2_field.py`（`CompactRequest.body_messages` 字段、v1 兼容、校验）；运行，**期望失败** | fail |
| 4 | **绿**：扩展 `CompactRequest`（`types.py`）新增 `body_messages` 字段；运行红测试 pass | pass |
| 5 | **红**：写 `test_compact_orchestrator_v2.py`（orchestrator 优先用 `body_messages`，缺失时 fallback v1 路径并打 warning 日志） | fail |
| 6 | **绿**：重构 `CompactOrchestrator.run_compact`（`orchestrator.py`）：优先 `body_messages`，缺失 fallback v1 + warning | pass |
| 7 | **红**：写 `test_compact_agent_v2_path.py`（`_run_compact_then_continue` + `run_compact_for_session` + `_should_auto_compact_session` 用 `write_memory_to_messages`） | fail |
| 8 | **绿**：重构 `agent_runner.py` 三处使用 v2 builder | pass |
| 9 | **红**：写 `test_compact_chat_v2_path.py`（`_generate_stream_response` auto-compact 分支排除最后的当前 user，compact 后再绑定 `[summary_envelope, current_user]`） | fail |
| 10 | **绿**：重构 `api/chat.py` auto-compact 分支 | pass |
| 11 | **红**：写 `test_compact_v1_v2_equivalence.py`（v1 + v2 在等价输入下覆盖度等价：包含相同的 system 段、包含所有 history token） | fail |
| 12 | **绿**：实现 v1/v2 等价性 helper（不修改现有 v1 builder） | pass |
| 13 | **红**：写 `test_compact_yaml_v2_prompts.py`（`config/config-agent-prompts.yaml::compaction` 含 `compact_user_instruction` 字段，措辞与 ref_src 一致） | fail |
| 14 | **绿**：在 `config/config-agent-prompts.yaml` 添加 `compact_user_instruction` 字段 | pass |
| 15 | **回归**：跑所有现有 `tests/test_compact_*.py` + `tests/test_prefix_cache_stability_v2.py` + `tests/test_write_memory_tool_calls.py` + `tests/test_cycle_executor.py` + 14 个新 v2 测试，**期望全过** | all pass |
| 16 | **类型检查 + 集成测试**：`PYTHONPATH=. ./.sglang/bin/python -m mypy backend/compact/` (如果项目有 mypy) + `pytest tests/test_main_lifespan_wiring.py` | 0 错 |
| 17 | **更新文档**：`AGENTS.md` 添加 trap #26（"compact v2 prompt builder 必须 1:1 复用 body_messages"）；更新 `踩坑记录4.md` §5（把"不可避免的 re-prefill"修正为"v2 通过 turn-shape 复用 80-95% KV"） | 文档可读 |
| 18 | **plan 文档**：写 `docs/superpowers/plans/outdated-2026-06-11-v2-compact-prompt-turn-shape-plan.md`（按 `2026-06-02-prefix-cache-stability-v2-plan.md` 风格） | 文件存在 |

## 8. 风险与缓解

| 风险 | 可能性 | 影响 | 缓解 |
|---|---|---|---|
| `write_memory_to_messages` 接口在 smolagents 升级时变化（v1.23+ 可能改 `ChatMessage.role` 枚举、content 形态、`to_messages()` 返回结构） | 中 | Compact v2 path 全坏 | 锁定 smolagents 到 1.22.0（项目当前版本）；接口变化时单元测试立即 fail（`test_compact_prompt_builder_v2.py::test_smolenagents_to_backend_message_role_normalization` 锁定转换契约） |
| `_inject_runtime_context` 在 compact 调用时被意外调用 | 低 | compact prompt shape 变得不稳定 | v2 compact 直接调 `llm_service.chat_stream_async`，不经过 `LocalLLMModel.generate[_stream]`。当前 agent compact 还会覆盖 `body_messages[0]` 为 compact-specific system，所以这里不再声称与上一轮 active system byte-equal。 |
| position-1 compact instruction 与 agent-mode compact system override 降低 prefix-cache 命中 | 高 | SGLang 可能显示 `#cached-token: 0`，compact 仍需 prefill 长历史 | 当前实现接受该代价换取 summary 可靠性。未来若要恢复 cache-first prompt shape，必须按 `2026-06-13-compact-prompt-shape-prefix-cache-prd.md` 增加观测、输出校验和 fallback。 |
| `body_messages` 经 `Message` 转换后丢失图片（list content → str） | 中 | Vision 场景下 compact 看不到图片 | Compact 路径**不**携带图片（图片占 token 多、对 summary 价值低）。`session.agent.memory.steps` 里的 `task_images` 不在 compact 路径中传递——这与 v1 行为**完全一致**。在 `test_compact_agent_v2_path.py` 加 assertion：构造有 `task_images` 的 session，验证 v2 path **不**传图片给 SGLang |
| `runtime_context` 二次注入风险 | 低 | compact prompt shape 变得不稳定 | compact 调用不要重新生成或写入 `session.runtime_context`。若未来要对 compact LLM request 传 `runtime_context`，必须单独设计并测试。 |
| chat 模式 `filtered` 已含当前 user turn | 中 | auto-compact 会把当前请求一起总结，违反“先压缩旧历史，再绑定当前 user”顺序 | 当前修正：若最后一条 filtered message 是 user，则 compact body 使用 `filtered[:-1]`，compact 完成后最终 LLM call 使用 `[summary_envelope, current_user]`。 |
| `_should_auto_compact_session` 改用 `write_memory_to_messages()` 后 token count 行为变化 | 中 | threshold 触发时机可能微调 | v1 用 `_session_memory_to_messages` 算的 token 数是 body-only；v2 用 `write_memory_to_messages` 算的 token 数**含 system**。两者差 ≈ 5k-10k tokens。**预期**：v2 阈值检查的 token 数**更高**（因为多了 system），可能更频繁触发 auto-compact。**缓解**：把 `compute_threshold` 调成 `max_context_length * 0.85`（参考 v1 已有 `performance_ratio: 0.1` 但那是占总 max_ctx 的比例—— v1 算 token 时不含 system，所以 0.1 实际触发点约 24k；v2 含 system，0.1 实际触发点约 14k）— **需要在测试中验证触发时机的合理性**，必要时调 `performance_ratio` |
| 20 个现有 compact 测试在 v2 重构后**不**通过 | 中 | 回归 | 严格执行 §7 step 15 的回归测试；任何失败立即定位。`test_compact_prompt_builder.py::test_v1_two_message_shape` 仍需 pass（v1 builder 保留）；`test_compact_prompt_builder_template_compat.py::test_v1_passes_qwen3_chat_template` 仍需 pass（v1 builder 渲染 [system, user] 仍合法） |
| 新 v2 测试覆盖度不足 | 中 | 漏掉边界 case | 每个测试对应一个 spec 章节；`test_compact_prompt_builder_v2_template_compat.py` 用**真实** Qwen3 chat_template 渲染（不是 mock）—— 锁住 multi_step_tool check 不 raise（参考 `踩坑记录3` trap #20 教训） |
| 仓库里"现成的措辞"在 ref_src 与本项目语义不完全对齐（如 `BASE_COMPACT_PROMPT` 末尾的"may be additional summarization instructions provided in the included context"） | 低 | LLM 行为偏离 | 措辞**完全复用** ref_src，仅添加 1 句 "The 'conversation above' refers to the messages that appear earlier in this message list, NOT including this current instruction message" 作为 v2 适配。如果实际跑出来 LLM 行为有问题，再迭代措辞（保留 ref_src 主干） |

## 9. 验收标准

### 9.1 自动化测试

- [V1] 现有 20 个 `tests/test_compact_*.py` 测试**全部通过**（v1 路径不退化）
- [V2] 新增 14 个 v2 测试**全部通过**：
  - `test_compact_prompt_builder_v2.py`（系统必须为第一条、position-1 instruction、custom_instructions 拼接、空 body_messages 报错、非 system 第一条报错）
  - `test_compact_prompt_builder_v2_template_compat.py`（2 个测试：v2 输出渲染真实 Qwen3 chat_template 不 raise；多 messages 数量 ≥ 2 渲染合法）
  - `test_compact_request_v2_field.py`（3 个测试：body_messages 字段存在、messages 字段保留 deprecated、v2 优先）
  - `test_compact_orchestrator_v2.py`（2 个测试：body_messages 路径走 v2；fallback v1 打 warning 日志）
  - `test_compact_agent_v2_path.py`（3 个测试：`_run_compact_then_continue` 用 `write_memory_to_messages`、`run_compact_for_session` 同源、`_should_auto_compact_session` 同源）
  - `test_compact_chat_v2_path.py`（chat auto-compact body 排除当前 user；rebuild `final_messages` 使用 `[summary_envelope, current_user]`）
  - `test_compact_v1_v2_equivalence.py`（1 个测试：v1 + v2 覆盖度等价）
  - `test_compact_yaml_v2_prompts.py`（1 个测试：`config/config-agent-prompts.yaml` 含 `compact_user_instruction` 字段，措辞与 ref_src 关键短语一致）
- [V3] `tests/test_prefix_cache_stability_v2.py` 全部通过（不破坏 v2 prefix cache 稳定性约束）
- [V4] `tests/test_write_memory_tool_calls.py` + `tests/test_cycle_executor.py` 全部通过（不破坏 cycle 路径）
- [V5] `tests/test_main_lifespan_wiring.py` 全部通过（lifespan 集成）

### 9.2 端到端验证

- [V6] 启动 SGLang + backend + frontend，跑一次 manual `/compact` slash command：UI 显示 `compact_summary` panel，流式输出 summary，session memory 被替换
- [V7] 跑一次 agent-mode auto-compact（session 触发阈值）：summary 正常产生，不出现 routing XML/tool-call 格式；若要验证 cache 命中率，按 `2026-06-13-compact-prompt-shape-prefix-cache-prd.md` 先补观测。
- [V8] 跑一次 chat-mode auto-compact：UI 显示 compact 卡片，下一次 chat 调用的 token 数**显著下降**（与 v1 行为对比，trap #21）
- [V9] 跑 14 个 v2 测试 + 全部现有测试 = 132+ 个测试全过

### 9.3 性能验证

- [V10] SGLang 终端日志监控（手动）：不要再期待当前 agent compact 出现“仅末尾 instruction miss”的 cache 形态；当前 prompt shape 可能显示 `#cached-token: 0`。未来 cache-first 方案需单独观测和验收。
- [V11] Compact LLM 调用性能验收以实际 `[compact-diag]` / SGLang 日志为准，不再用本历史 spec 的 tail-only cache-hit 假设作为成功标准。

## 10. Position-1 修订（2026-06-11）

### 10.1 原始 v2 设计（tail-placed instruction）

§5.2 描述的 v2 builder 在 `body_messages` **末尾**追加 1 条 user 消息（compact-instr），前缀段的 N-1 条消息与之前 chat 调用 1:1 byte-equal：

```
[system, ...history..., user<compact-instr>]   ← 原始 v2 设计
 ^body[0]                  ^body[N]            ← 末尾追加
```

这一设计的理论假设是：radix tree 会复用前 N-1 条对应的 KV，仅末尾第 N 条新 user 消息需要重新 prefill。**SGLang 端是对的**（`radix_cache.py:191-211` 逐 token 整数匹配），**但 LLM 端是错的**。

### 10.2 E2E 问题（2026-06-11）

在 40k+ token 长上下文跑 `/tmp/fixed_compact_payload.json`（Chinese sci-fi Q&A 对话），LLM 输出了：

```
### 对话内容总结
#### 1. 对话概况
[... 5-section 中文自由格式 markdown ...]
```

**完全忽略了** YAML `main_prompt` 规定的 `<analysis>` + 9-section `<summary>` English 格式。

### 10.3 根因

LLM 注意力分布的 **lost-in-the-middle effect**：在 40k token 上下文里，模型对**首尾**的 attention 较强，对**中部**的 attention 接近 0。**末尾的 instruction 收到的 attention 极弱**，模型既没有读到 instruction 的格式约束，也没有读到 LANGUAGE LOCK 的英文要求，而是被对话主体的中文"吸过去"了，输出了中文自由 markdown 总结。

### 10.4 修复（position-1 amendment）

把 instruction 从**末尾**挪到 **position 1**（system 之后第一条 user）：

```
[system, user<compact-instr>, ...history...]
 ^body[0] ^body+1[0]           ^body+1[1:]
```

- LLM 视角：position 1 是 user 的"开场问题"，模型**严格遵循**这条 instruction 的格式约束
- 对话主体变成"要应用该格式的数据"
- LLM 在 attention 强的头部看到 instruction，在 attention 强的尾部看到对话尾段，**两头都强**

### 10.5 Trade-off 与新契约

v2 spec 的 byte-equal 理想**被打破**：
- 原设计：`final[:-1] == body_messages`（仅末尾追加 1 条）
- 修订后：`final[0] == body_messages[0]`（system byte-equal），`final[2:] == body_messages[1:]`（history byte-equal），`final[1]` 是**新插入**的 user 消息

**2026-06-13 correction:** 这个 trade-off 不能再描述成“免费”。当前 position-1 instruction 改善了模型遵循度，但确实会降低与上一轮请求的最长公共前缀命中。是否值得恢复 cache-first shape，必须按 `2026-06-13-compact-prompt-shape-prefix-cache-prd.md` 增加观测、校验和 fallback 后再判断。

**新契约**（`backend/compact/prompt_builder.py::build_compact_messages_v2`）：
- `body_messages[0].role == "system"`（不变量，违反则 `ValueError`）
- `final[0] == body_messages[0]`（byte-equal，prefix-cache anchor）
- `final[1].role == "user"`，content = `prompts.compact_user_instruction` (+ optional custom)
- `final[2:] == body_messages[1:]`（byte-equal，history turns）
- `len(final) == len(body_messages) + 1`

**回归测试**（任一失败即视为 v2 设计破坏）：
- `tests/test_compact_prompt_builder_v2.py::test_instruction_takes_precedence_over_tail_with_long_history`：60 消息 body，instruction 必须在 index 1，不在末尾
- `tests/test_compact_prompt_builder_v2.py::test_final_message_is_original_last_body_message`：末尾元素 = 最后一条原始 body 消息，**不是** instruction
- `tests/test_compact_prompt_builder_v2.py::test_history_after_instruction_is_byte_equal_to_body_tail`：`final[2:]` 与 `body_messages[1:]` byte-equal
- `tests/test_compact_prompt_builder_v2.py::test_system_message_preserved_byte_equal_at_index_zero`：`final[0]` 与 `body_messages[0]` byte-equal
- `tests/test_compact_prompt_builder_v2.py::test_custom_instructions_appended_to_instruction_message`：custom instructions 与主 instruction **同一条** user 消息，不在末尾

### 10.6 措辞加固（YAML `compact_user_instruction`）

为防止未来 LLM 在长上下文里再次漂移，`config/config-agent-prompts.yaml::compaction::compact_user_instruction` 加上两层 LOCK：

- **LANGUAGE LOCK**：Write the entire summary in **English**, regardless of the language used in the conversation being summarized.
- **STRUCTURE LOCK**：exactly two blocks in this order — `<analysis>` + `<summary>` with 9 mandatory English section names（Primary Request and Intent / Key Technical Concepts / Files and Code Sections / Errors and fixes / Problem Solving / All user messages / Pending Tasks / Current Work / Optional Next Step）；禁止 `### / ####` Chinese headers、自由分节、`<analysis>` 之前的前缀。

**回归测试**：
- `tests/test_compact_prompt_builder_v2.py::test_language_lock_wording_present_in_instruction_content`：YAML 必须含 `LANGUAGE LOCK` 标记和 `English` 字眼
- `tests/test_compact_prompt_builder_v2.py::test_structure_lock_contains_all_nine_section_names`：YAML 必须含 `STRUCTURE LOCK` 标记、`<analysis>`、`<summary>`、全部 9 个 section 名称

### 10.7 关联

- AGENTS.md trap #26：v2 builder 总体规则（更新表述，承认 position-1 修订）
- AGENTS.md trap #27：**新增**，专门讲 position-1 修订的 lost-in-the-middle 动因与 trade-off
- `踩坑记录4.md` §5.5：v2 修复后再加一个 §5.6 记录这次 position-1 修订

## 11. 参考

- `docs/superpowers/specs/2026-06-02-prefix-cache-stability-v2-design.md`：v2 prefix cache stability 硬性约束（system 段 byte-stable、`runtime_context` session-pinned、append-only 动态尾部）
- `docs/superpowers/specs/2026-06-06-slash-commands-and-compact-design.md`：v1 /compact 整体设计（前端协议、metadata 标记、envelope 重构、cycle 预触发）
- `docs/superpowers/specs/2026-05-27-runtime-context-time-design.md`：`runtime_context` 注入与 freeze 机制
- `docs/superpowers/specs/2026-05-13-unified-multi-agent-message-model-design.md`：统一消息模型（message level 而不是 step level）
- `ref_src/services/compact/prompt.ts`：Claude Code 参考实现的 compact 措辞源（`NO_TOOLS_PREAMBLE` / `BASE_COMPACT_PROMPT` / `NO_TOOLS_TRAILER` / `getCompactUserSummaryMessage`）
- `踩坑记录3.md` §20：v1 /compact E2E 调试教训（chat template `multi_step_tool` check、Qwen3 chat_template 渲染测试必跑）
- `踩坑记录4.md`：v1 auto-compact latency 调查 + SGLang mamba / KV 调优（`mamba_full_memory_ratio: 0.5`、`chunked_prefill_size: 4096`）
- AGENTS.md：
  - trap #8：`write_memory_to_messages` 中禁止 strip `tool_calls`（v2 path 仍遵守）
  - trap #12：向 LLM 隐藏工具必须同时切断 API schema + 系统提示两条路径（与 compact 无关，但相关）
  - trap #14：永久禁止引入"每轮 LLM 总结旧历史"（v2 compact 是 event-triggered only，遵守）
  - trap #17：/compact 是 event-triggered only（v2 遵守）
  - trap #19：ChatMessage.metadata 字段（chat 模式 auto-compact 仍用）
  - trap #20：/compact E2E 调试教训（v2 测试覆盖 chat template 渲染）
  - trap #21：frontend local state ≠ wire format（chat 模式 metadata 翻译仍用）
  - trap #22：/compact 必须按 `agentMode` 选 `mode`（v2 遵守）
  - trap #23：post-compact `has_run=True`（v2 仍遵守）
- SGLang 源码（`/home/zk/Project/LLM-deployment/.sglang/lib/python3.12/site-packages/sglang/`）：
  - `srt/mem_cache/radix_cache.py:191-211`：`_key_match_*` 逐 token 整数相等
  - `srt/mem_cache/radix_cache.py:374-444`：`match_prefix` 入口
  - `srt/mem_cache/mamba_radix_cache.py:423-454`：`MambaRadixCache` 与 `RadixCache` 共享 `key_match_fn`
  - `srt/mem_cache/mamba_radix_cache.py:1035-1044`：mamba state 在 chunk-aligned 位置 checkpoint
- Qwen3.5 chat_template：`/home/zk/Project/LLM-weight/Qwen3___5-35B-A3B-GPTQ-Int4/chat_template.jinja:67-80`（`multi_step_tool` check，至少一条非 `<tool_response>` user 行）
- smolagents 1.22.0 源码（`/home/zk/Project/LLM-deployment/.sglang/lib/python3.12/site-packages/smolagents/`）：
  - `agents.py:752-764`：`write_memory_to_messages` 父类方法
  - `memory.py:24-211`：`ToolCall` / `ActionStep` / `TaskStep` / `SystemPromptStep` 数据类
  - `models.py:237-240`：`tool_role_conversions` (TOOL_CALL→ASSISTANT, TOOL_RESPONSE→USER)
  - `models.py:272-337`：`get_clean_message_list` (合并相邻同角色)
