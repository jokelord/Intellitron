# Backend Prefix-Cache Audit - 2026-07-13 （后附中文总结）

## Scope and Method

This audit traced every backend path that can reach the local LLM from the API or Agent runtime through `LocalLLMModel`, `LLMService`, and the SGLang/vLLM/OpenAI-compatible providers. Correctness was checked from captured/canonical message structure, not token counts. The governing shape is:

```text
session-pinned system/runtime/tool prefix
+ canonical serialized memory/messages
+ append-only current-turn tails
```

UI bubbles, timeline events, dispatch text, routing deltas, session IDs, and usage diagnostics were separately traced and are not model-input sources. Session and request IDs remain transport/lifecycle fields only.

## Concrete Invocation Inventory

Locations below are the concrete call expressions, not endpoint groupings. `Stable` means the previous canonical request remains a structural prefix. Provider retries are listed only at the adapter expression that actually issues the repeated request, together with every request-field change.

| Invocation / exact location | Stable prefix source | Tail or changed transport | Boundary classification | Focused evidence |
|---|---|---|---|---|
| Chat non-stream, `backend/api/chat.py:280` | Filtered canonical request messages | Caller current-user tail | Stable; successful compact replacement or new image input is a multimodal boundary | `test_compact_chat_filter.py`, `test_compact_chat_streaming_filter.py`, `test_multimodal_request_preparation.py` |
| Chat stream, `backend/api/chat.py:625` | Same filtered request and deterministic multimodal preparation | Caller tail; usage remains diagnostics | Stable; new image is multimodal boundary | `test_compact_chat_streaming_filter.py`, `test_multimodal_request_preparation.py` |
| Responses API non-stream, `backend/api/chat.py:806` | Adapted canonical request | Adapted caller tail | Endpoint normalization before first call, then stable; new image input is a multimodal boundary | chat endpoint tests, `test_multimodal_request_preparation.py` |
| Normal Agent stream call, `backend/agent/agent_runner.py:1587` | Profile system + pinned runtime + canonical `memory.steps` | Current action loop tail | Stable within profile | `test_post_compact_prefix_stability.py`, `test_write_memory_tool_calls.py` |
| Normal Agent sync call, `backend/agent/agent_runner.py:1648` | Same canonical profile/memory/schema | Current action loop tail | Stable within profile | `test_write_memory_tool_calls.py` |
| Coordinator routing stream, `backend/agent/agent_runner.py:3574` | Coordinator profile + coordinator canonical memory | Routing user tail | Stable within coordinator profile; later profile switch is boundary | `test_prefix_cache_stability_v2.py`, `test_agent_session_context.py` |
| Coordinator routing sync, `backend/agent/agent_runner.py:3918` | Same coordinator prefix | Routing user tail | Stable within coordinator profile | `test_multi_agent_runner.py` |
| Answer Now initial stream, `backend/agent/agent_runner.py:4864` | Frozen active-profile canonical memory and unchanged schema | Self-contained no-tool user tail | Stable append-only | `test_agent_session_context.py`, `test_agent_stop_api.py` |
| Answer Now corrective retry, same expression `backend/agent/agent_runner.py:4864` | Exact initial Answer Now messages | One corrective user message | Stable append-only retry | `test_agent_session_context.py` |
| Local sync primary, `backend/agent/local_llm_model.py:429` | Converted canonical messages + pinned runtime; deterministic schema | None beyond caller tail | Stable | `test_post_compact_prefix_stability.py`, `test_local_llm_model_streaming.py` |
| Local sync unsupported-`extra_body` retry, `backend/agent/local_llm_model.py:442` | Byte-equivalent primary messages and schema | Removes transport `extra_body` only | Stable transport retry | `test_local_llm_model_streaming.py` |
| Local stream primary, `backend/agent/local_llm_model.py:555` | Same full profile/runtime messages | Stream transport uses `tools=None` for parser compatibility | Stable | `test_stream_tools_cycle.py` |
| Local stream unsupported-`extra_body` retry, `backend/agent/local_llm_model.py:594` | Exact stream-primary messages and schema shape | Removes transport `extra_body` only | Stable transport retry | `test_stream_tools_cycle.py` |
| Local empty-stream non-stream fallback, `backend/agent/local_llm_model.py:664` | Exact stream-primary messages and `tools=None` shape | Changes only `stream` transport mode; textual calls use the existing parser | Stable transport fallback | `test_stream_tools_cycle.py` |
| Compact cache-first attempt, `backend/compact/orchestrator.py:88` via first prompt plan | Exact untrimmed normal source, preferring `last_compactable_llm_messages` | Self-contained final user compact instruction | Stable append-only | `test_compact_orchestrator_cache_first.py`, `test_compact_agent_cache_first.py`, `test_compact_chat_cache_first.py` |
| Compact reliable fallback, `backend/compact/orchestrator.py:88` via fallback plan | Compact-specific system + budgeted canonical source | Position-1 compact instruction | Deliberate compact correctness boundary; config-gated | `test_compact_prompt_plan.py`, `test_compact_source_budget.py` |
| Compact transport dispatch, `backend/compact/orchestrator.py:88` | Exact selected compact plan | Dispatches the plan once through `LLMService.chat_stream_async`; this expression does not retry | Stable service pass-through; no failed cache-first delta reaches UI before validation | `test_compact_orchestrator_instrumentation.py` |
| Async non-stream service dispatch, `backend/llm_service.py:1864` | `chat_async` caller messages, preserving caller order and preparation policy | Thread-executor dispatch with `stream=False` | Stable service pass-through; any image-input boundary is established by the caller's multimodal preparation | `test_llm_backend_selection.py`, `test_multimodal_request_preparation.py` |
| Async stream service dispatch, `backend/llm_service.py:1901` | `chat_stream_async` caller messages, preserving caller order and preparation policy | Synchronous generator dispatch with `stream=True` | Stable service pass-through | `test_llm_stream_usage_propagation.py`, `test_multimodal_request_preparation.py` |
| Service-to-provider dispatch, `backend/llm_service.py:1832` | Caller-owned prepared messages, order preserved | Sampling/request IDs outside messages | Stable pass-through | `test_llm_backend_selection.py`, `test_llm_stream_usage_propagation.py` |
| SGLang sync provider request, `backend/llm_service.py:646-649`, `client.post(.../chat/completions, json=request_data)` | Prepared `messages`; converted tool schema; model and sampling fields | Sends `stream`, optional `top_k`, `repetition_penalty`, `extra_body` fields, `request_id`, and tools as assembled | Stable provider request; no retry at this adapter | provider/backend selection and multimodal tests |
| SGLang stream provider request, `backend/llm_service.py:704-708`, `client.stream("POST", .../chat/completions, json=request_data)` | Same assembled SGLang request and prepared messages | Streaming HTTP transport and upstream-close registration only | Stable provider request; no retry at this adapter | stream usage, abort, and multimodal tests |
| vLLM sync provider request, `backend/llm_service.py:1146-1149`, `client.post(.../chat/completions, json=request_data)` | Prepared `messages`, tools, model, sampling fields, optional extra fields and request ID | `max_tokens` may already be proactively capped before dispatch | Stable provider request | vLLM backend and context-budget tests |
| vLLM sync recursive max-limit retry, same request expression `backend/llm_service.py:1146-1149`, recursion selected at `1153-1159` | Preserves every request field, including `messages`, tools, model, sampling fields, `stream`, optional extra fields, and `request_id` | Replaces only `max_tokens` with the parsed provider limit; removes no field | Stable provider retry | vLLM max-token retry tests |
| vLLM sync recursive context-budget retry, same request expression `backend/llm_service.py:1146-1149`, recursion selected at `1160-1166` | Preserves every request field, including `messages`, tools, model, sampling fields, `stream`, optional extra fields, and `request_id` | Replaces only `max_tokens` with the estimated remaining-context budget; removes no field | Stable provider retry | vLLM context-budget tests |
| vLLM stream provider request, `backend/llm_service.py:1209-1213`, `client.stream("POST", .../chat/completions, json=request_data)` | Same assembled vLLM request, with `stream=True` | Streaming HTTP transport and upstream-close registration | Stable provider request | vLLM streaming, abort, and usage tests |
| vLLM stream recursive max-limit retry, same request expression `backend/llm_service.py:1209-1213`, recursion selected at `1222-1231` | Preserves every request field and the same `request_id`/registration arguments, including unchanged `messages`, tools, model, sampling fields, `stream`, and optional extra fields | Replaces only `max_tokens` with the parsed provider limit; removes no field | Stable provider retry | vLLM streaming retry tests |
| vLLM stream recursive context-budget retry, same request expression `backend/llm_service.py:1209-1213`, recursion selected at `1232-1241` | Preserves every request field and the same `request_id`/registration arguments, including unchanged `messages`, tools, model, sampling fields, `stream`, and optional extra fields | Replaces only `max_tokens` with the estimated remaining-context budget; removes no field | Stable provider retry | vLLM streaming context-budget tests |
| OpenAI-compatible proxy sync provider request, `backend/llm_service.py:1532-1535`, `client.post(.../chat/completions, json=request_data)` | Prepared `messages`, tools, model, sampling fields, optional extra fields and request ID | `max_tokens` may already be proactively capped before dispatch | Stable provider request | proxy backend and context-budget tests |
| OpenAI-compatible proxy sync recursive context-budget retry, same request expression `backend/llm_service.py:1532-1535`, recursion selected at `1539-1545` | Preserves every request field, including `messages`, tools, model, sampling fields, `stream`, optional extra fields, and `request_id` | Replaces only `max_tokens` with the estimated remaining-context budget; removes no field | Stable provider retry | proxy context-budget tests |
| OpenAI-compatible proxy stream provider request, `backend/llm_service.py:1586-1590`, `client.stream("POST", .../chat/completions, json=request_data)` | Same assembled proxy request, with `stream=True` | Streaming HTTP transport and upstream-close registration | Stable provider request | proxy streaming and abort tests |
| OpenAI-compatible proxy stream recursive context-budget retry, same request expression `backend/llm_service.py:1586-1590`, recursion selected at `1599-1608` | Preserves every request field and the same `request_id`/registration arguments, including unchanged `messages`, tools, model, sampling fields, `stream`, and optional extra fields | Replaces only `max_tokens` with the estimated remaining-context budget; removes no field | Stable provider retry | proxy streaming context-budget tests |

`backend/cycle/executor.py` does not call an LLM directly; it delegates to `AgentRunner`. Manager child execution likewise enters the normal Agent expressions above with a newly initialized child profile/session.

A full re-scan of `backend/llm_service.py` found no outbound SDK `chat.completions` or `responses` inference call. The six HTTP expressions above are the complete model-inference set: one sync and one stream expression for each of SGLang, vLLM, and the OpenAI-compatible proxy. The other `.post` expressions at `backend/llm_service.py:274` and `backend/llm_service.py:2072` call SGLang `/abort_request`; they are lifecycle cancellation requests, not model inference, and therefore are not invocation rows.

## Fixed Violation

### Historical Agent tool traces were rewritten

`SmartToolCallingAgent.write_memory_to_messages()` previously changed completed historical `ActionStep`s when a later `TaskStep` appeared. It converted structured `TOOL_CALL`/`TOOL_RESPONSE` messages into sanitized assistant/user prose, rewrote file observations, synthesized artifacts and completion receipts, and could insert an artificial assistant boundary. Therefore a request sent during one turn was not a structural prefix of the next request.

The serializer now uses the same canonical ActionStep rendering for every round. It still strips only `observations_images`, which are duplicate task-image copies. It does not strip `tool_calls`, alter observation text, summarize `read_file` output, or change action/observation ordering.

Regression evidence: `test_historical_read_file_keeps_structured_call_and_exact_user_observation` first failed because no historical `TOOL_CALL` existed, then passed after the fix. The exact `read_file` observation remains `TOOL_RESPONSE`, which `tool_role_conversions` sends to the provider as `user`; the preceding call remains `TOOL_CALL`, which converts to `assistant`.

Older tests that explicitly required historical rewriting were retired because they contradicted the approved covenant and the existing AGENTS hard contract.

## Negative Data-Flow Findings

- `timeline_events`, routing/dispatch deltas, and behavior records are emitted or retained for audit/UI only. `_activate_profile_agent()` shares `memory.steps` and does not inject `context_pack`.
- `session_id` and `request_id` are lifecycle/transport values. They are not interpolated into model message content.
- Stream `usage` is retained for diagnostics and compact metadata only. It is not appended to messages or model output.
- Answer Now snapshots backend Agent memory after the barrier. It does not consume frontend bubbles or timeline output.
- Tool images are the only tool-side dynamic payload appended to messages; their insertion is a required multimodal protocol change, not UI state.

## Legitimate Non-Byte-Identical Boundaries

1. First request: no earlier request exists to reuse.
2. Successful manual or automatic compact: canonical history is intentionally replaced by the validated summary envelope. Append-only behavior resumes from the replacement.
3. Intentional Agent profile or tool-schema switch: coordinator, coordinator_2, basic, manager, and future profiles can have different system prompts and schemas. Shared canonical memory remains unchanged; stability resumes inside the selected profile.
4. Multimodal representation change: a new image, resized image, or tool-loaded image necessarily changes message content. Historical image data is deterministically retained; stability resumes after encoding.
5. Reliable compact fallback, only when `reliable_fallback_enabled=true`: compact-specific system and position-1 instruction are the explicit correctness boundary after cache-first failure.
6. Provider deployment change: changing provider, tokenizer, model, or provider-owned chat template is an operational deployment boundary, not a per-turn exception.

## Post-Compact Result

There is no first-post-compact transition exception. Compact replaces canonical memory with the validated summary envelope and keeps `has_run=True`; the first and every later normal Agent call retain the full selected profile system prompt, session-pinned runtime context, and deterministic tool schema. Subsequent turns differ only by canonical append-only memory tails. No compact latency knob or dependency source changed.

## 中文总结
这份审计报告是一份针对后端大模型（LLM）请求生成链路的**前缀缓存（Prefix-Cache）稳定性审计**。报告通过全量追踪代码路径、校验消息结构确定性，确认了后端在调用本地 LLM 时是否满足缓存复用条件。以下是核心内容的结构化总结：

### 🔍 一、审计目标与方法
- **核心目标**：验证从 API/Agent 运行时到 `LocalLLMModel`、`LLMService` 及底层 Provider（SGLang/vLLM/OpenAI 兼容）的完整调用链路，是否符合前缀缓存的结构稳定性要求。
- **判定标准**：不以 Token 数量为基准，而是严格校验消息结构的确定性。合规的请求必须遵循固定范式：
  `会话绑定的系统/运行时/工具前缀 + 规范化的历史消息序列化 + 仅追加（Append-only）的当前轮次尾部`
- **审计范围**：覆盖所有能触发模型推理的代码表达式，排除 UI 气泡、时间线事件、路由增量、Session/Request ID 等纯传输/诊断字段。

### 📊 二、核心调用清单与稳定性验证
报告逐行梳理了约 30 处关键调用点，均被归类为**稳定前缀**或具有**明确边界**，主要结论如下：
- **Chat 与 Responses API**：非流/流式调用均基于过滤后的规范请求，新图片输入仅作为多模态边界，不破坏前缀。
- **Agent 运行器**：正常调用、协调器路由、Answer Now 初始/重试调用均保持 Profile 系统提示词与运行时上下文不变，尾部仅追加当前动作循环消息。
- **本地同步/流式调用**：主请求与重试请求（如移除不支持的 `extra_body`、空流降级）均保持消息主体与 Schema 不变，仅调整传输层参数。
- **Compact 编排层**：缓存优先策略直接复用未修剪的正常源；可靠回退策略仅在配置开启时触发，边界清晰。
- **LLM 服务层与 Provider 适配**：SGLang、vLLM、OpenAI 兼容代理的同步/流式请求均保持 `messages`、`tools`、`model`、采样字段不变。重试逻辑仅动态替换 `max_tokens` 或 `stream` 标志，不触碰消息结构。
- **结论**：所有出站推理调用均为稳定前缀，重试与传输降级不引入结构噪声。

### 🛠️ 三、已修复的关键缺陷
- **历史工具轨迹重写问题**：`SmartToolCallingAgent.write_memory_to_messages()` 曾在新任务出现时改写已完成的历史 `ActionStep`（如将结构化 `TOOL_CALL`/`TOOL_RESPONSE` 转为纯文本、篡改观察结果、插入虚假助手边界），导致请求无法复用前缀。
- **修复方案**：改为统一使用规范化的 ActionStep 渲染，仅剥离重复的 `observations_images`，不修改工具调用、不总结 `read_file` 输出、不改变动作顺序。
- **回归验证**：`test_historical_read_file_keeps_structured_call_and_exact_user_observation` 已验证修复生效，历史消息结构完全保留，符合协议契约。

### 🚫 四、负向数据流确认（不注入模型的内容）
明确排除了以下常见干扰项，确认它们仅用于审计、UI 或诊断，**不会混入模型请求体**：
- 时间线事件、路由/调度增量、行为记录
- Session ID / Request ID（仅生命周期字段）
- 流式 Usage 统计（仅用于诊断与 Compact 元数据）
- Answer Now 快照（仅捕获后端 Agent 内存，不消费前端气泡）
- 工具图像（唯一合法的多模态动态负载，属协议必需变更）

### ⚖️ 五、合法的字节差异边界（6 种预期场景）
报告明确指出，以下情况导致请求非字节级相同属于**合理边界**，不影响缓存有效性：
1. 首次请求（无前缀可复用）
2. 成功的手动/自动压缩（Compact）：规范历史被摘要信封替换，后续恢复仅追加
3. Agent 配置文件或工具 Schema 切换：系统提示词与 Schema 变化，共享记忆不变
4. 多模态变更：新图像/缩放图像/工具加载图像必然改变消息内容
5. 可靠压缩回退（Fallback）：仅在 `reliable_fallback_enabled=true` 时触发，位置 1 指令为明确边界
6. Provider 部署变更：更换模型、Tokenizer 或聊天模板属运维边界，非单轮异常

### ✅ 六、压缩后状态验证（Post-Compact Result）
- 无首次压缩过渡异常。Compact 将规范历史替换为验证通过的摘要信封，并标记 `has_run=True`。
- 后续正常 Agent 调用仍携带完整的 Profile 系统提示词、会话运行时上下文与确定性工具 Schema。
- 轮次间差异仅由仅追加（append-only）的尾部消息引起，未引入任何延迟旋钮或依赖源变更。

### 📌 总体结论
后端 LLM 请求生成链路结构清晰、前缀高度稳定，重试机制与压缩策略设计合理，严格遵循“系统/运行时前缀 + 规范化历史 + 仅追加尾部”的范式。历史工具轨迹重写缺陷已修复，非法数据注入已排除，所有合法边界均有明确定义。该实现完全满足 Prefix-Cache 的最佳实践，可保障缓存命中率与推理性能。