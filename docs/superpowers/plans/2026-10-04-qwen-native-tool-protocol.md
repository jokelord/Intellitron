# Qwen Native Tool Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 让仓库原生调用、特殊 agent actions 和工具结果使用 Qwen delimiters 与通用 canonical 消息，同时保持历史 prefix 与生命周期。

**Architecture:** 独立 schema-aware native parser，复用现有 legacy parser。仓库 canonical helper 保留调用/结果元数据；session pin 决定方言，原生调用与 provider calls 经相同 schema 校验后进入既有执行和路由路径。

**Tech Stack:** Python/FastAPI/smolagents、Jinja2、SGLang/NGC vLLM、pytest。

**Spec:** [设计](../specs/2026-10-04-qwen35-native-tool-protocol-design.md)

## Global Constraints

- 用户已明确要求计划后立即编码；不再等待额外确认。主 agent 规划/审阅，复用 subagent 执行具体代码，整个生命周期不超过 6 个 subagents。
- 无 Git、worktree/branch/commit、依赖源码修改、`.superpowers` ledgers 或每步总结；不开发 SFT/exporter/数据转换/registry。
- `qwen35-native` / `legacy_text` 配置选择；新 session pin，旧对象/无字段 snapshot 默认 legacy_text，不在线改写历史。
- native switch_agent 仅 `{"target_agent": string}`，additionalProperties=false，profile allowlist；提示与 schema 仅向 coordinator_1（首次路由）及 coordinator_2（二次 handoff）暴露，共享业务协议与其它原生 profiles 不教授此调用；保留已发送 legacy 前缀和历史。普通答案自然语言，不引入 final_answer。
- 内部 arguments 对象，HTTP arguments 字符串，模板 arguments 对象；历史工具/图像 provenance 与 acceptance commit 边界不变。
- compact cache-first/no-trim/source-budget/fallback 默认规则和性能参数不变；不更换模型及 basic_agent entry-agent。

## Review Focus

- 标准文本调用与 provider calls 重复编码必须拒绝，不能双执行（Task 2）。
- nullable/string 或 delimiter 字面值不能通过猜类型执行（Task 1）。
- coordinator 与 secondary switch 的 schema 在请求前暴露且历史有对应 receipt（Task 4）。
- 同名并行工具及最后 dispatch skipped 仍保留完整关联与单 blackboard（Task 3）。
- 连续工具 envelope、改变 thinking 与下一用户轮不得改旧 token prefix（Task 5/6）。

---

## 文件与接口分工

- `backend/agent/qwen_tool_protocol.py`：native grammar/codec/stream gate；返回现有 NaturalText/ToolCallSequence。
- `backend/agent/canonical_messages.py`：仓库 message adapter、clone/wire serializer、typed call results；不依赖 UI。
- `backend/agent/local_llm_model.py`：dialect selection、共享 validation、request schema 与 fallback。
- `backend/agent/agent_runner.py`：session pin、memory/result sidecar、switch routing receipt、compact/Answer Now 集成。
- `backend/llm_service.py` / `backend/api/chat.py` / compact diagnostics：保留完整消息与 schema。
- `config/chat-templates/qwen-native-cache-stable.jinja`：native 工具文本、稳定 reasoning、JSON scalars 和无损 observation。

### Task 1: native grammar 与 session 配置

**Files:** Create `backend/agent/qwen_tool_protocol.py`, `tests/test_qwen_tool_protocol.py`; Modify `backend/config.py`, `backend/agent/agent_runner.py`, `config/qwen3.8-radixArk-no-DSPARK-NVFP4-config-infer.yaml`; Test `tests/test_text_tool_protocol_v2.py`。

**Interfaces:** `parse_qwen_protocol(text: str, *, tool_schemas: Mapping[str, dict]) -> NaturalText | ToolCallSequence`; `format_qwen_tool_calls(calls: Sequence[TextToolCall], *, tool_schemas: Mapping[str, dict]) -> str`; `QwenStreamControlGate` 提供与 StreamControlGate 相同 `push(delta: str) -> str` / `finish() -> str` / `suppresses_visible_content` 接口。`InferenceConfig.tool_protocol: str = "legacy_text"`，配置 loader 校验仅两值；选中 infer YAML 设置 qwen35-native。`AgentSession.tool_protocol` 对新初始化取配置，旧对象读取用 getattr(...,"legacy_text")。

- [x] 写 `test_native_multiple_calls_roundtrip`、`test_native_schema_types_and_whitespace`、`test_native_rejects_invalid_batch`、`test_native_fenced_examples_are_inert`、`test_native_fragmented_eof`；断言 bool/null/objects/arrays、string `" true "`、长代码字符串精确往返，unknown/duplicate/missing/type/NaN/delimiter/歧义 union 均拒绝，legacy markers 不执行。
- [x] 运行 `.sglang/bin/python -m pytest tests/test_qwen_tool_protocol.py -q`，确认新增接口/行为导致失败。
- [x] 实现无副作用严格 parser/codec/gate；仅 schema 类型决定转换，不修格式。配置/session pin 测试覆盖新 native 与无字段 legacy；保留 text_tool_protocol.py 旧行为。
- [x] 运行 `.sglang/bin/python -m pytest tests/test_qwen_tool_protocol.py tests/test_text_tool_protocol_v2.py -q`，检查实际结果并单独列出已知 baseline，修复新增回归。

### Task 2: canonical transport 与 LocalLLM 适配

**Files:** Create `backend/agent/canonical_messages.py`, `tests/test_canonical_tool_messages.py`; Modify `backend/llm_service.py`, `backend/api/chat.py`, `backend/agent/local_llm_model.py`, `backend/compact/prompt_diagnostics.py`; Test `tests/test_local_llm_tool_protocol.py`, `tests/test_compact_message_dict_normalization.py`。

**Interfaces:** 扩展 `Message` 可选 `tool_calls: list[dict] | None`, `tool_call_id: str | None`, `name: str | None`, `reasoning_content: str | None`。helper `clone_message(message: Message, *, content: str | list[dict] | None = None) -> Message`；`message_to_wire(message: Message) -> dict`（arguments string）；`message_to_template(message: Message) -> dict`（arguments dict）；`canonicalize_model_messages(messages: Sequence[Any]) -> list[Message]` 保留 sidecar、不合并 role。LocalLLM 共享调用 schema builder 和 native/legacy resolver。`inference.capture_raw_tool_calls` 默认 false，选中 native 配置开启；有 exposed tools 的 native Agent 请求使用 opt-in raw passthrough，原消息/schema不变，SGLang 模板 alias 与 vLLM 仓库 parser plugin 返回真实 XML；兼容 API parser与 legacy/default请求。必要 transport flags 在 retry/fallback保留，raw UI sidecar 标明 model_text/provider_payload且不进模型历史。

- [x] 写 metadata round-trip/API Pydantic/multimodal clone/三个 provider serializer 测试；断言同名 tool ID/name、空 content 与 reasoning 保留，wire/template args 仅转一次，普通 Chat 不执行工具。
- [x] 写 sync/stream/empty fallback 请求捕获测试，断言等价 messages/tools/schema；raw+provider dual encoding 拒绝，provider 错误使用真实 schema path，不伪造 raw offsets。
- [x] 运行 `.sglang/bin/python -m pytest tests/test_canonical_tool_messages.py tests/test_local_llm_tool_protocol.py -q`，先确认新增行为失败。
- [x] 所有 Message 重建、API boundary、LocalLLM cleaner/画像准备复用 helper；native 方言使用 Task 1 parser/gate，legacy 继续旧路径；现有“stream 不传 tools”等 legacy 测试显式 pin legacy，新测试 pin native，不能放宽断言掩盖差异；捕获 fallback 时不可重新编号或双执行。diagnostics 包含完整 prompt fields/schema，metadata UI 字段不回灌。
- [x] 运行 `.sglang/bin/python -m pytest tests/test_canonical_tool_messages.py tests/test_local_llm_tool_protocol.py tests/test_compact_message_dict_normalization.py tests/test_compact_prompt_diagnostics.py -q`，检查实际结果并单独列出已知 baseline，修复新增回归。

### Task 3: memory 与真实工具/dispatch results

**Files:** Modify `backend/agent/agent_runner.py`, `backend/agent/orchestrator.py`, `backend/agent/canonical_messages.py`; Create `tests/test_native_agent_memory.py`; Test `tests/test_write_memory_tool_calls.py`, `tests/test_manager_agent_dispatch.py`。

**Interfaces:** helper typed `CanonicalToolResult(call_id: str, name: str, content: str | list[dict], status: str)`；ActionStep 仓库 sidecar `canonical_tool_results` 保存 typed results，smolagents 字段不 monkeypatch。native `write_memory_to_messages()` adapter 产生 assistant.calls + 按序 tool results；legacy 分支完全保留。

- [x] 写 `test_native_memory_one_encoding`、`test_native_same_name_parallel_results`、`test_native_observation_whitespace_preserved`、`test_native_dispatch_last_skipped_single_blackboard`，从 native transcript parser 经真实 process_tool_calls 验证一次 stage/同名关联/全部 report/限额 skipped/单黑板；duplicate child_id 启动计数必须为零。
- [x] 运行 `.sglang/bin/python -m pytest tests/test_native_agent_memory.py -q`，确认失败。
- [x] 在执行结果源保存 typed records，不拆 combined observation；普通 call 各自原始正文，前面 dispatch 关联指向最后 result，最后 result 承载一次 ordered full blackboard；memory 保留 canonical tool_calls，raw 控制文本不重复编码。解析失败无 accepted call 使用内部 user 尾部，不造 orphan tool result；image sidecar/provenance 不变。
- [x] 运行 `.sglang/bin/python -m pytest tests/test_native_agent_memory.py tests/test_write_memory_tool_calls.py tests/test_manager_agent_dispatch.py -q`，检查实际结果并单独列出已知 baseline，修复新增回归。

### Task 4: switch_agent、coordinator 与 secondary handoff

**Files:** Modify `backend/agent/local_llm_model.py`, `backend/agent/agent_runner.py`, `config/config-agent-prompts.yaml`; Create `tests/test_native_agent_routing.py`。

**Interfaces:** `build_switch_agent_schema(allowed_agent_names: AbstractSet[str]) -> dict` 位于 canonical_messages.py；exact target_agent/required/additionalProperties=false/enum。native resolver 输出现有 `SwitchAgentCommand(target_agent, raw_payload)` 与已接受 canonical call。route 完成追加匹配 `CanonicalToolResult` receipt；不执行普通 function，也不清掉 accepted control memory。保留 SecondaryHandoffRequest/basic_agent proxy 的实际 schema。

- [x] 写 `test_native_coordinator_request_has_switch_schema`、`test_native_switch_target_strict`、`test_native_secondary_switch_keeps_receipt`、`test_native_switch_cannot_mix_tools`，覆盖 raw/provider、非法 target/alias/extra/missing、max switches、coordinator stream/sync 与 coordinator_2 secondary handoff。
- [x] 运行 `.sglang/bin/python -m pytest tests/test_native_agent_routing.py -q`，确认失败。
- [x] `_run_coordinator_routing_stream/pass` 请求前传 synthetic switch schema；`_classify_coordinator_output` 与 `_step_stream` 使用同 schema/allowlist，native 不调用旧 alias normalizer。receipt 存 memory，timeline 维持；prompt 用 native 标签与 exact target_agent，legacy 已建 session 的 pinned prompt 不重写。
- [x] 运行 `.sglang/bin/python -m pytest tests/test_native_agent_routing.py tests/test_local_llm_tool_protocol.py tests/test_manager_agent_dispatch.py -q`，检查实际结果并单独列出已知 baseline，修复新增回归。

### Task 5: cache-stable 模板与 launcher 配置

**Files:** Create `config/chat-templates/qwen-native-cache-stable.jinja`, `tests/test_qwen_native_template.py`; Modify `config/qwen3.8-radixArk-no-DSPARK-NVFP4-config-infer.yaml`, `scripts/1_A_start_sglang.sh`, `scripts/1_B_start_vllm_container_with_snapshots.sh`, `scripts/1_C_start_vllm_container.sh`（仅缺失的模板路径传递/容器挂载）；Test `tests/test_vllm_container_scripts.py`, `tests/test_sglang_config.py`。

**Interfaces:** template 消费 Task 2 template dicts、tools、enable_thinking；infer sglang/vllm.chat_template 指向仓库 artifact。已有 parser 配置保持符合 native XML 的实际支持值，不硬编码一套参数。容器内模板路径必须可读；不修改当前模型路径/entry-agent。

- [x] 写 template tests，断言工具定义仅一次、native 调用/response 一次，true/null 为 JSON、string 原空白、observation 代码/前后空白不 trim；历史 reasoning/空 think 在新用户轮和当前 enable_thinking 改变后不变。
- [x] 运行 `.sglang/bin/python -m pytest tests/test_qwen_native_template.py -q`，确认失败。
- [x] 以官方 Qwen native Jinja 为基线，只适配历史 thinking、scalar JSON 和 observation 内容保留。完整 tools 组闭合后不重开；launcher 从 infer YAML 取模板，SGLang host 与 vLLM container path 传递到服务。未实际启动服务不宣称线上 parser/cache 已验证。
- [x] 运行 `.sglang/bin/python -m pytest tests/test_qwen_native_template.py tests/test_vllm_container_scripts.py tests/test_sglang_config.py -q`，检查实际结果并单独列出已知 baseline，修复新增回归；`bash -n` 分别检查三个实际启动脚本。

### Task 6: lifecycle/compact/image parity 与收尾验证

**Files:** Modify `backend/agent/agent_runner.py`, `backend/compact/orchestrator.py`, `backend/compact/prompt_plan.py`, `backend/compact/chat_message_filter.py`, `AGENTS.md`（仅所需契约澄清）；Create `tests/test_native_tool_lifecycle.py`; Test `tests/test_prefix_cache_stability_v2.py`, `tests/test_post_compact_prefix_stability.py`, `tests/test_local_llm_compactable_payload.py`, `tests/test_agent_stop_api.py`。

**Interfaces:** Answer Now 和 compact 使用 Task 2 canonical/messages/schema；last_compactable payload 包含完整调用 metadata，成功 replacement 清除。native/legacy session 分支贯穿 all request builders，既有 image omission sidecars/acceptance tokens 不变。

- [x] 写 captured-request parity tests：normal/stream/fallback/Answer Now/compact、profile/legacy pin、下一用户轮历史 prefix；Answer Now 禁工具两句完整，最多一次 corrective retry，失败无执行；Stop targeted/pending abort/token cleanup。
- [x] 写 Vision/CU regression assertions：canonical role 转换不改变 provenance/绑定/placeholder/acceptance 及 token 消费；失败、Stop、Answer Now、compact/empty fallback 不误 commit omission。
- [x] 运行 `.sglang/bin/python -m pytest tests/test_native_tool_lifecycle.py -q`，确认新增失败。
- [x] 修补实际遗漏的 canonical/schema 转换，保留 cache-first first attempt/no trim/source budget/fallback 默认/latency 参数；AGENTS 定向澄清新 tool role 与渲染 user、旧历史不改写。
- [x] 运行 `.sglang/bin/python -m pytest tests/test_qwen_tool_protocol.py tests/test_canonical_tool_messages.py tests/test_native_agent_memory.py tests/test_native_agent_routing.py tests/test_qwen_native_template.py tests/test_native_tool_lifecycle.py tests/test_text_tool_protocol_v2.py tests/test_local_llm_tool_protocol.py tests/test_write_memory_tool_calls.py tests/test_manager_agent_dispatch.py tests/test_stream_tools_cycle.py tests/test_prefix_cache_stability_v2.py tests/test_post_compact_prefix_stability.py tests/test_local_llm_compactable_payload.py tests/test_agent_stop_api.py tests/test_compact_orchestrator_cache_first.py tests/test_compact_chat_filter.py -q`，检查实际结果并单独列出已知 baseline，修复新增回归。
- [x] 运行 `.sglang/bin/python -m pytest tests/test_multi_agent_config.py -q`，单独报告 baseline entry-agent assertion；不为消除旧失败改 basic_agent 默认。完成现有 text/vision UI smoke 或明确未验证范围，读实际 diff（无 Git）检查没有依赖修改/模型选择漂移/旧 marker 新协议执行/额外文档 ledger。

验收范围：相关检查 311 passed；legacy/lifecycle 28 项失败与原 baseline 一致。frontend type-check 与三个 launcher 的 bash syntax 检查通过。实际 text/vision UI smoke、线上 provider inference 和 cache 命中尚未验证。

最终广覆盖命令使用 `.sglang/bin/python -m pytest -c tests/pytest.ini --ignore=tests/test.old -m 'not paddle_ocr_integration' -q`，对照 baseline 分别报告新失败与旧失败；默认 pytest 会因 test.old 重名模块 collection 失败，不修无关测试基础设施。

本计划仅跟踪任务 checkbox，不另写每步小结。执行方式沿用用户指定主/subagent 分工与复用 context；计划保存后立即进入代码实现。
