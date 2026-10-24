# Qwen 原生工具调用适配设计

日期：2026-10-04。状态：运行时实现已完成，相关自动化检查已执行；广覆盖测试尚未全绿，实际 UI、线上推理服务与 cache 命中尚未端到端验证。

## 1. 范围与目标

将本仓库的 `<<TOOL_CALL>>` / `<<END_TOOL_CALL>>` 以及 `<<SWITCH_AGENT>>` 替换为 Qwen 原生工具调用表示。普通最终回答仍是自然语言，模型不生成外层消息对象 JSON。

本次仅实现运行时协议适配：严格原生 parser、canonical 调用/结果消息、transport 与模板一致性、特殊 agent calls、旧 context 兼容。不开发 SFT exporter、数据转换系统、训练流程。训练兼容是采用通用结构化消息的理由。

不修改依赖源码，不执行 Git，不建立 worktree/branch，不记录 `.superpowers` coding-step 小结，不修改用户的模型选择、entry agent 或 compact 性能参数。

## 2. 三层契约

| 层 | 表示 |
|---|---|
| canonical history | OpenAI-style tools/schema、assistant.tool_calls、role: tool 的结果及 tool_call_id |
| 模型 completion | `<tool_call><function=...><parameter=...>...</parameter></function></tool_call>` 或自然语言 |
| HTTP API | tools/schema 原样保留；function.arguments 按接口要求序列化为 JSON 字符串 |

内部 `arguments` 为对象，进入模板前也是对象。后端分配调用 ID，不要求模型生成 ID。调用文本与 `tool_calls` 只编码一次，不在 assistant content 再保存同一次控制块。

```json
[
  {"role":"user","content":"读取 /tmp/demo.txt"},
  {"role":"assistant","content":"","tool_calls":[
    {"id":"call_001","type":"function","function":{
      "name":"read_file","arguments":{"file_path":"/tmp/demo.txt"}
    }}
  ]},
  {"role":"tool","name":"read_file","tool_call_id":"call_001","content":"hello"},
  {"role":"assistant","content":"文件内容是 hello。"}
]
```

模型对应调用：

```text
<tool_call>
<function=read_file>
<parameter=file_path>
/tmp/demo.txt
</parameter>
</function>
</tool_call>
```

Qwen 模板将 canonical tool 结果渲染为 user envelope 内的 `<tool_response>hello</tool_response>`。输入层 tool role 与模型文本层 user envelope 不矛盾；不要提前在 content 内包装 response tags。

官方依据：[Qwen3.5 模板](https://huggingface.co/Qwen/Qwen3.5-4B/blob/main/chat_template.jinja)、[Transformers 工具消息](https://huggingface.co/docs/transformers/chat_extras)。

## 3. 严格解析与执行

新协议每个 tool_call 只有一个 function；多个块构成一个 ordered batch。原生 parser 依据暴露的工具 schema 解码：string 保留原始正文，非 string 使用严格 JSON 类型；拒绝重复/未知参数、缺 required、未知函数、非法 JSON、类型错误、NaN/Infinity、歧义联合类型和原生 delimiter 歧义。不要猜类型、补括号、改 escape 或猜别名。

完整 batch 经解析、schema validation 与 dispatch preflight 后才执行；后面块非法时前面工具也不能启动。literal/fenced/quoted 示例不执行；未闭合控制块在流式 EOF 时拒绝。流式控制内容缓冲，不泄漏半个调用文本。普通文本不要求工具或 final_answer。

真实原始工具调用指模型生成的 `<tool_call>` XML，而非 provider 已解析的 JSON。`inference.capture_raw_tool_calls` 默认 false；选中 native 配置开启后，仅 native Agent 且有 exposed tools 的请求附加 raw passthrough transport 标志，保留原消息与 tools schema。SGLang 使用 request-level none 绕过 parser，由仓库模板 `raw_request_tools` 恢复相同工具前缀；vLLM 使用仓库 opt-in parser plugin，未携带标志的 API clients 继续原 parser 行为。`skip_special_tokens=false` 保留原标签，retry/fallback 保留必要 raw 标志；Answer Now 仍使用同 tools/schema 与禁止调用尾部，compact 使用普通 transport。原 XML 标为 `model_text`；只有 provider payload 时标为 `provider_payload`，不合成 XML 或误称模型原文。显示 sidecar 不进入 canonical history。

provider 结构化调用和原生 raw XML 共用 validation。两种编码同时出现不能执行两遍，按当前严格 dual-encoding 规则拒绝。错误通过 AgentParsingError 反馈真实 block/parameter/offset；没有 raw text 时使用实际 JSON/schema path，不编造行列。

无 accepted call 的解析错误以独立内部 user 尾部反馈，不制造 orphan tool ID。已接受工具的执行失败是该 call 的 tool response。重试及 empty-stream fallback 必须至多执行一次。

## 4. 特殊调用

### switch_agent

新协议公开标准 function schema：

```json
{
  "type":"function",
  "function":{
    "name":"switch_agent",
    "description":"Switch to an allowed agent.",
    "parameters":{
      "type":"object",
      "properties":{"target_agent":{"type":"string","enum":["basic_agent"]}},
      "required":["target_agent"],
      "additionalProperties":false
    }
  }
}
```

enum 根据当前 profile 的实际 allowlist 生成，不能扩大权限。新协议只接受 `target_agent`，拒绝 `agent_name` / `target` / `name` 别名及裸字符串；legacy context 保留其旧逻辑。

switch_agent 的提示指导与 schema 只向 coordinator_1（首次路由）和 coordinator_2（二次 handoff）暴露；共享业务工具协议及其它原生 profiles 不教授或暴露此调用。保留既有 legacy system 前缀及历史，不因本提示范围调整重写或升级方言。

switch_agent 是标准调用表示、特殊路由执行：保持现有 allowed targets、单调用、不得与普通工具混用、switch-count 限制和 handoff timeline。调用及路由结果有 canonical 关联，不生成旧 SWITCH marker。coordinator 请求也必须携带实际 switch schema，不能只在响应后判断名称。

coordinator_2 的 managed `basic_agent` 仍按实际代理 schema 调用并产生 SecondaryHandoffRequest；任务和 additional_args 不丢失，不借协议迁移放宽原有冲突规则。

### dispatching_parallel_agent

manager 唯一 child dispatch 工具仍为 `dispatching_parallel_agent`，arguments 严格且仅含 child_id/task。同 turn duplicate child_id 在 child 启动前拒绝；max_parallel_children 只限制 dispatch，按模型顺序保留前 N，其余 skipped。普通工具不受限；单 dispatch 也经过 preflight/blackboard。

全部 accepted calls 仍为一个 stage 并发执行，不推断依赖。typed per-call results 从执行源保存，不靠 observation 展示字符串反向切分。

普通工具每 call 一个 tool result，按调用顺序保存原始正文。dispatch 每 call 也有关联结果：前面的 dispatch 保存 call_id/child_id/status 并指向本 batch 最后一个 dispatch result；最后一个保存自己的状态和完整 ordered shared blackboard 一次，包含所有 child report/error/skipped/限额信息。即使最后 call skipped，也承载该 aggregate；不得重复整个 blackboard 或丢失报告。现有 SSE/timeline 保持。

## 5. canonical serializer 与原生模板

新增仓库所有的 canonical helper，扩展 backend Message 与 API input schema，保留 tool_calls/tool_call_id/name/reasoning。smolagents 无真正 tool enum 且 ChatMessage 不含全部字段，因此通过仓库 adapter/sidecar 保存，不 monkeypatch 依赖，也不只修改 role conversion。

所有 clone/transport 路径共用 metadata-preserving 转换：多模态准备、同步/流式/provider、compact、diagnostics 和 benchmark。prompt diagnostics 覆盖所有实际 prompt-bearing 字段，而非只 role/content；UI/timeline/usage 不回灌模型正文。普通 Chat 可保存/转发结构化工具对话，但不执行 Agent 工具。

仓库模板保留官方 tool/function/parameter delimiters，只做必要适配：

- 已发送 assistant reasoning/空 think 的历史渲染固定，不根据未来用户轮次变化；历史策略与当前 enable_thinking 生成尾部独立，compact 仍 thinking-off。
- 官方 scalar 分支可能产生 True/None；本仓库 string 原样，非 string scalar/object/array 使用严格 JSON true/null，匹配 parser codec。
- observation 正文不 trim、不合并、不摘要；前后空白、代码缩进与换行无损保留，避免官方 render_content|trim 改写结果。

模板是明确的最小改编，不声称与官方逐字节相同。训练兼容要求输入与服务使用同一契约。

完整工具 batch 结果收集后才发送，按调用顺序编码；后续不能因晚到结果重开或合并既有 tool envelope。已闭合历史 token prefix 必须稳定，包括追加用户轮次和改变当前 thinking 设置。

## 6. 选择与旧上下文

配置明确选择 `qwen35-native` 或 `legacy_text`；新 context 初始化时 pin。旧 snapshot 或无字段对象默认 legacy_text。新 native context 不接受旧 markers 的兼容执行解析。

本次在选中的 Qwen infer 配置启用 native，不更换模型/entry-agent。旧 context 保持其旧 prompt、schema 和历史，不能在线替换标签、拆结果或改 role；compact/profile 切换不升级协议。新 native context 可用成功验证的 compact summary 初始化，但原 context 不变。

infer YAML -> 实际启动脚本 -> inference server 保持可配置。实际脚本为 scripts/1_A_start_sglang.sh、scripts/1_B_start_vllm_container_with_snapshots.sh、scripts/1_C_start_vllm_container.sh；vLLM 使用 NGC container。按实际 provider 版本验证 native parser 配置，provider 实际诊断以 /config 的 inference.active_backend 为准。

## 7. 生命周期、compact 与图像不变量

所有 normal/sync/stream/provider/fallback/路由/Answer Now 请求复用相同 canonical messages 与相同逻辑工具 schema。改变 transport 不授权改变历史。动态控制只在自包含尾部，system/runtime/tool prefix 固定。

Stop 仅影响 lifecycle；保留 targeted cancellation、pending abort 与 registration token，无单 session abort-all。old worker 完成后 retained partial output 可追加，不改旧历史。

Answer Now 保留原 agent schema/prefix/history，添加一次性禁工具、routing/handoff/进一步 planning 的 self-contained user tail，含 `Do not call or execute any tool in this Answer Now response` 与 `Use no tools for this response only`，明确不影响后续用户轮次。违规调用不执行，attempt 完整缓冲，最多一次 corrective retry。

compact 保持 cache_first_tail_with_fallback、prompt_control_enabled=true、reliable_fallback_enabled=false 默认值。cache-first 首次 source 不裁剪且不受 source_token_budget eligibility gate；source budget 只限制 enabled reliable fallback。保留 last_compactable_llm_messages、non-coordinator_1 来源、thinking-off、summary-only、自包含九段 summary/no-tool/no-routing tail。成功 validation 后才替换 memory；失败不泄漏 delta、不替换。latency knobs 不变。

保留历史 role、文本、顺序、assistant 输出、tool traces、图像 provenance；不把 coordinator_2 transcript 写进 system/context_pack。合法 compact/profile/template 边界按既有约定报告，不扩大例外。

Vision Automation 仅绑定的 sampling task armed 期间及一次 post-Abort cleanup，可替换早期 image blocks 为每块 aggregate placeholder；page generation/mode/top-level identity pin，stage 后同 logical user turn 成功接受才 commit，失败/compact/Stop/Answer Now/其他 context 不消费 permission。Chat/Agent 等价，仅 top-level，记录 vision_automation_historical_image_omission。

Computer Use 仅同 top-level Agent 的 trusted vision_desktop observation、provenance 恰为 computer_use_observation、同 ActionStep 适用；较新成功 observation 后下一 ordinary inference stage 旧 blocks，仅保留最新。接受 barrier 后 commit；pre-accept/transport/empty fallback/Stop/Answer Now/compact 不消费。成功 reobserve 可 stage，失败不可。不得扫描 child/manager/Chat/user upload/其他 provenance，记录 computer_use_historical_image_omission。role 迁移不重标图像。

## 8. 验收

- 原生 parser 全类型/空白/长字符串/EOF/fragment/fences/非法 schema/歧义 delimiter；整批校验，无提前执行。
- switch_agent 原生文本及 provider calls 走同严格 target_agent schema；非法 target、别名、混合调用拒绝；basic_agent 与 dispatch 的生产 parser -> process_tool_calls 全路径。
- 同名并行工具结果 ID/order 正确，黑板仅一次，skipped/failed 信息完整；普通 observation 无损。
- captured sync/stream/provider/fallback/Answer Now/compact 请求的消息与 schema 一致，不双执行或双编码。
- native/legacy pin，旧历史保持；API/multimodal/diagnostics 不丢字段；下一轮 token prefix 稳定。
- 既有 Stop、compact、Vision/CU acceptance 边界与 text/vision UI smoke checks；不添加新 SSE。

已知 baseline：test_multi_agent_config.py::test_real_config_files_use_multi_agent_prompt_layout 期望 manager_agent，但实际选中 entry-agent 为 basic_agent；此失败不得通过修改用户 entry-agent 修复。

AGENTS.md 定向澄清新 context canonical tool role / Qwen user envelope、旧 user observations 不改写。
