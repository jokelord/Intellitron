# 简化文本工具调用协议设计

> **2026-09-11 权威迁移说明：** 可执行 delimiter 已改为
> `<<TOOL_CALL>>`、`<<END_TOOL_CALL>>`、`<<SWITCH_AGENT>>`。工具 opener
> 必须独占真实物理行，后面不带工具名；frame body 是一个或多个顶层严格
> JSON call objects，schema 恰好为 `{"name":"glob","arguments":{"pattern":"**/*"}}`。
> formatter 对同轮 batch 推荐只输出一个共享 frame，parser 也兼容多个独立
> frames。旧 `<<<...>>>` 与 XML/HTML-like 文本均为 NaturalText，绝不会执行。
> 本说明覆盖下文保留的任何旧 grammar、XML rejection 或 per-call frame 描述。
> 下文 2026-08-30 内容现仅作为历史设计审计记录，不能作为当前实现说明；
> 当前行为以本迁移说明、`backend/agent/text_tool_protocol.py` 与对应测试为准。

日期：2026-08-30  
状态：历史草案（2026-09-11 双尖括号协议已实施）

## 1. 背景与目标

当前本地 LLM 需要生成带外层数组、调用 ID、`type`、`function`、`name` 和 `arguments` 多层包装的 canonical JSON。对于参数文本较长、包含数学公式、Shell 命令、代码片段或多个并行 child task 的场景，这种结构会显著增加漏括号、错引号、重复 key 和层级放错的概率。后端为兼容这些错误逐渐积累了多套解析、猜测和修复路径，也提高了普通 JSON 示例被误判为真实调用的风险。

本设计用一个有明确边界的文本控制协议替代所有旧 textual tool-call 和 handoff 格式。目标是：

- 让模型只生成“工具名 + 参数对象”，移除没有业务价值的外层数组及 wrapper 层。
- 多调用优先在一个共享 frame 内通过空白分隔的顶层 call objects 表达；也兼容连续独立 frames。
- 让包含 `:';[{}<\\|>`、中文、LaTeX、Shell、代码和嵌套 JSON 的参数仍可由标准 JSON 稳定承载。
- 使用整段响应级别的控制模式和 fail-closed 解析，避免自然语言、代码示例或公式中的 marker 误触发工具。
- 对格式错误给出精确的 `AgentParsingError` observation，让模型在下一轮自行纠正，不做智能猜测或内容修复。
- 保持现有 `SmartToolCallingAgent`、并行 orchestrator、结构化 memory traces、timeline/SSE 和前端协议不变。
- 为未来从 OpenAI Chat Completions 迁移到 Responses API 保留独立的 provider-native structured tool-call adapter。

## 2. 非目标

本次不做以下事项：

- 不改变任何工具自身的参数 JSON Schema、权限、执行语义或返回格式。
- 不改变同一 LLM turn 中调用必须相互独立、全部 accepted calls 组成一个并行 stage 的约束。
- 不改变 `dispatching_parallel_agent` 的参数契约；其参数仍严格且仅含 `child_id` 与 `task`。
- 不为 malformed JSON 增加引号修复、括号补全、尾随逗号删除、Python literal、heredoc 或另一套 raw-string 语法。
- 不继续兼容旧 textual formats，也不在新 parser 内保留旧格式的分支或死代码。
- 不改变 OpenAI/SGLang/vLLM 的 wire schema；这里定义的是本地模型 assistant content 中的文本协议。
- 不改变前端组件、SSE/timeline event 名称、payload schema 或 projection 逻辑。
- 不把 marker 配置成推理服务 stop sequence；否则 opener 一生成就可能截断后续 JSON。

## 3. 方案选择与理由

### 3.1 选定方案：有边界的 strict JSON block

选定格式由独占物理行的固定 opener、一个或多个 strict JSON call objects 和固定 closer 组成。工具名位于每个 call object 的 `name` 字段，参数位于 `arguments` object。边界可在流式输出中可靠识别，参数继续交给成熟的 JSON decoder，并且调用可以独立计数和报告错误。

### 3.2 未选择 NDJSON / 连续裸 JSON

NDJSON 可以省略 marker，但漂亮打印后的 JSON 本身跨行，不能再用“一行一个对象”确定调用边界；若改为单行 JSON，又违背可读、可 debug 和贴合 coding 训练语料的目标。连续裸 JSON 还会使普通回答中的 JSON demo 与真实工具调用难以区分。

### 3.3 未选择 heredoc / raw-string

heredoc 能减少长文本内的转义，但会引入第二套字符串和 terminator 规则。模型需要同时掌握 JSON 与 heredoc，terminator 也可能与代码正文碰撞，parser 则必须重新实现嵌套和转义边界。严格 JSON 虽要求 `\"`、`\\`、`\n` 等标准转义，但规则单一、现有模型熟悉、用户也容易复现和调试。

## 4. 协议语法

以下 EBNF 仅描述 framing；`json-object` 必须由 strict JSON decoder 验证：

```text
control-response = [ prose, newline ],
                   (tool-call-sequence | switch-agent-command) ;

tool-call-sequence = tool-call-block,
                     { inter-block-whitespace, tool-call-block } ;

tool-call-block = "<<TOOL_CALL>>", newline,
                  call-object,
                  { protocol-whitespace, call-object }, newline,
                  closer-line ;

call-object = strict-json-object-with-exact-name-and-arguments-keys ;
closer-line = "<<END_TOOL_CALL>>", { any-character-except-physical-newline } ;

switch-agent-command = "<<SWITCH_AGENT>>", " ", agent-name ;

agent-name = identifier ;
identifier = (ALPHA | "_"), { ALPHA | DIGIT | "_" } ;
```

实现只把 LF 和 CRLF 当作物理行边界。opener 必须从物理行第 0 列开始且整行精确为 `<<TOOL_CALL>>`，其后不带工具名。closer 物理行只需以 `<<END_TOOL_CALL>>` 开始；同一行后缀被忽略并整行消费。bare CR、NEL、LS、PS 不会创建可执行行边界。工具名和 agent 名必须符合 identifier 语法。

### 4.1 单工具调用

提示词必须教模型以两个空格作为每层 JSON 缩进：

```text
<<TOOL_CALL>>
{
  "name": "read_file",
  "arguments": {
    "file_path": "/tmp/example.json"
  }
}
<<END_TOOL_CALL>>
```

JSON 顶层必须是 keys 恰好为 `name` 与 `arguments` 的 object。模型不输出 `id`、`type`、`function` 或 array wrapper。

### 4.2 多个并行调用

多个调用优先放入一个共享 frame；顶层 call objects 用真实空行分隔，没有开头 `[`、结尾 `]` 或对象间逗号：

```text
<<TOOL_CALL>>
{
  "name": "dispatching_parallel_agent",
  "arguments": {
    "child_id": "reader_a",
    "task": "分析公式、JSON 示例与符号 :' ; [ ] { } < > \\ |，只返回证据。"
  }
}

{
  "name": "dispatching_parallel_agent",
  "arguments": {
    "child_id": "reader_b",
    "task": "检查 Shell 命令并验证边界条件。"
  }
}
<<END_TOOL_CALL>>
```

objects/frame 的出现顺序就是 canonical call order。parser 同时兼容每个 call 一个完整 frame。文本协议不携带调用 ID；后端在全部 calls 成功解析后生成唯一 ID。ID 是内部实现细节，调用者只能依赖顺序与唯一性，不能依赖编号从 0 还是 1 开始。provider-native structured call 若已携带可信 ID 可以保留，否则同样由后端生成。

### 4.3 Agent 路由

`switch_agent` 不再伪装成 function call，也不使用 JSON：

```text
<<SWITCH_AGENT>> basic_agent
```

该命令必须是响应中唯一的控制行，前后只允许空白。它不能和 `TOOL_CALL` block 混用。目标 agent 必须匹配 identifier 语法，并属于当前 coordinator 的允许目标集合。

### 4.4 JSON 可读性与转义

系统提示、few-shot 示例和 canonical formatter 一律使用 UTF-8、`ensure_ascii=false` 和两空格缩进。parser 不因缩进宽度不同拒绝语义正确的 strict JSON；缩进是模型输出契约和可读性要求，不是额外的脆弱语法门槛。

`:`、`'`、`;`、`[`、`]`、`{`、`}`、`<`、`>` 和 `|` 可直接出现在 JSON string 中。双引号、反斜杠和控制字符仍必须使用标准 JSON 转义；真正的多行文本使用 `\n`，不能把原始换行直接放进 JSON string。嵌套 object 和 array 允许存在于参数 object 内。

## 5. 控制模式、消歧与原子性

### 5.1 进入控制模式

parser 只读取 assistant 的可见 content，不读取 `reasoning_content`。工具前可以有可见 prose；只有 Markdown fence 外、真实 LF/CRLF 后物理行第 0 列的精确 tool opener 可以开始可执行 tool sequence。switch opener 仍必须独占整条响应：

- `<<TOOL_CALL>>`
- `<<SWITCH_AGENT>>`

旧三尖括号和 XML/HTML-like 标签不会激活控制模式，始终作为 NaturalText。第 8 节的永久 rejection firewall 只针对响应开头的 `Calling tools:` / `调用工具:`。

除第 8 节 exact legacy-opener firewall 外，未以前述新 marker 开始的响应是自然语言响应，不扫描其中任何 JSON、`name`、`arguments`、花括号或 marker。因此：

- marker 出现在普通段落、数学推导、Shell 输出或 JSON demo 中间时，不调用工具，也不报协议错误。
- reasoning 中出现 marker 永远不触发解析。
- Markdown fenced code block 中的 marker 按普通正文处理，包括 backtick/tilde、run length 大于三、合法 info string 和未闭合 fence。
- 如果最终回答确实必须把 marker 作为第一个可见字符展示，模型必须使用 fenced code block，或先写一句自然语言说明。

进入工具控制模式后，第一个 opener 前的 prose 被忽略，但工具序列自身必须完全匹配 grammar。最后一个 closer 后新物理行上的 prose、Markdown fence、不完整 frame 或混合 route/tool 命令都使整段失败。

### 5.2 Fail-closed 与全有或全无

所有 block 必须先完成 framing、strict JSON、重复 key、顶层类型、工具名和基础参数 schema 校验，之后才能一次性转换为内部 calls 并提交 `SmartToolCallingAgent.process_tool_calls()`。任意一个 block 失败时：

- 本 turn 的全部 block 均被拒绝。
- 不执行任何工具或 child dispatch。
- 不发布代表已接受调用的 orchestration/tool-call 事件。
- 返回一个精确的 `AgentParsingError` observation，让 LLM 下一轮纠正。

这个原子性只负责“接受并提交调用”阶段；工具一旦通过现有 orchestrator preflight 并进入并行 stage，仍遵循现有执行、abort、timeout 和 observation 语义。duplicate `child_id`、child 上限及其他 manager preflight 继续在任何 child 启动前处理。

### 5.3 Marker 碰撞

closer 只有在物理行第 0 列以 `<<END_TOOL_CALL>>` 开始时才是 framing token；该物理行余下内容被兼容性忽略。合法 JSON string 中的 `<<END_TOOL_CALL>>` 不在物理行第 0 列，因此不会提前关闭 frame。parser 不使用“寻找下一个 `}`”或括号猜测，而是先按 marker 行取得 body，再用标准 decoder 顺序读取一个或多个由协议空白分隔的顶层 call objects。

## 6. Strict JSON 校验

每个工具参数 body 必须满足以下规则：

- 顶层值必须是一个 object，不能是 array、string、number、boolean 或 `null`。
- key 和 string 必须使用双引号。
- 拒绝重复 key，包括任意嵌套 object 中的重复 key。
- 拒绝 `NaN`、`Infinity` 和 `-Infinity`。
- 拒绝尾随逗号、注释、单引号、Python tuple/dict literal 和未转义控制字符。
- decoder 完成一个 call object 后只接受 space/tab/LF/CRLF，再读取下一 call object或结束；不能忽略 prose，也不把 JSON array 当作 batch。
- 不补全括号、不重写混合引号、不转义模型漏掉的引号、不把无法解析的 arguments 包装成 `{"raw": ...}`。
- JSON 成功后，再按已注册工具 schema 验证参数；未知工具名或参数不匹配是 parsing/preflight failure，不得执行部分调用。

duplicate key 使用 `object_pairs_hook` 或等价机制在 decoder 阶段拒绝；non-finite constant 使用 `parse_constant` 或等价机制拒绝。不能依赖 Python `json.loads` 对重复 key 的“后值覆盖”行为。

## 7. 错误反馈

协议层定义单一错误类型（例如 `TextToolProtocolError`），由 Agent 适配为现有 `AgentParsingError` observation。错误必须简短、可操作且不回显完整长参数，至少包含：

- 失败类别：framing、JSON、duplicate key、unknown tool、argument schema、route target 或 legacy protocol。
- 工具 block 的一基序号；route 错误不需要 block 序号。
- 标准 JSON decoder 可提供时的 JSON 行、列和原始错误摘要。
- duplicate key 的 key 名；位置无法可靠取得时不得伪造行列。
- 明确声明 `No tool was executed.`。
- 一条符合新协议、使用两空格缩进的最小纠正规则。

示例：

```text
Tool-call protocol error in item 2: invalid JSON at line 4, column 18: Expecting ',' delimiter. Output complete tool-call frames beginning with an exact `<<TOOL_CALL>>` line, use double-quoted strict JSON call objects with two-space indentation, and close with `<<END_TOOL_CALL>>`. No tool was executed.
```

非法 route 应同时返回目标值和允许目标列表，但不猜测拼写相近的 agent。解析失败由模型在后续 step 自行纠正，后端不进行 fuzzy matching 或自动修复。

## 8. Legacy textual format 删除策略

以下旧格式不再被提取、归一化、修复或执行：

- `Calling tools:` / `调用工具:` 加 canonical JSON array。
- `<tool_call>...</tool_call>`。
- `动作:` / `Action:` / `Coordinator decision:` 等 wrapper。
- 顶层裸 `{"name": ..., "arguments": ...}` 或数组。
- 正文尾部、正文中间的 JSON/tool-call 扫描。
- switch-agent shorthand、function wrapper、旧 canonical array，以及从可见正文中 split route JSON。
- Python `ast.literal_eval`、混合引号修复、尾随逗号修复、缺失括号补全、`write_file`/`exec_command` 专用 malformed payload 恢复。

必须永久保留一个极小的 active rejection firewall：仅当响应第一个非空白位置精确以历史 opener `Calling tools:` 或 `调用工具:` 开始时，直接产生 `unsupported legacy tool-call protocol` 的 `AgentParsingError`。该规则在 provider-native/text adapter 分派和任何执行之前运行；它不搜索正文、不定位数组、不调用 JSON decoder、不提取工具名或参数、不修复 payload，也绝不执行。因此它是永久有效的拒绝规则，不是 legacy parser、兼容层或以后可重新启用的旧代码。历史 opener 出现在正文中间或 fenced code block 内时仍按普通文字处理。

### 8.1 旧 helper 删除清单

在确认没有非 legacy callsite 后，删除 `backend/agent/local_llm_model.py` 中为旧协议服务的 helper 和分支，包括：

- `_normalize_tool_transcript_item`
- `_repair_tool_transcript_text`
- `_repair_single_missing_final_object_brace`
- `_extract_tool_transcript_array`
- `_iter_top_level_array_objects`
- `_duplicate_tool_call_top_level_key`
- `detect_malformed_tool_transcript`
- `_decode_malformed_json_string_value`
- `_try_parse_malformed_write_file_transcript`
- `_repair_truncated_json_object`
- `_extract_balanced_json_array`
- `_try_parse_exec_command_action_payload`
- `_normalize_switch_agent_arguments`
- `_extract_switch_agent_target`
- `_split_visible_text_and_trailing_switch_agent_command`
- `_split_visible_text_around_switch_agent_command`

现有 `_parse_switch_agent_command` 和 `_parse_tool_calls_from_text` 应由新协议入口替代，而不是在原函数内继续堆分支。若 `_extract_balanced_json`、`_try_parse_json` 等通用名 helper 经 callsite 审计后只服务旧容错 parser，也应删除；新模块直接使用严格 decoder，不能复用其“修复后再试”语义。

### 8.2 旧测试删除与替换清单

删除只证明旧格式“还能成功解析”或“能被智能修复”的测试，包括 XML、`动作:`、裸/行内 JSON、canonical array、Python literal、混合引号、尾随逗号、缺失 brace、malformed `write_file`/`exec_command`、route JSON wrapper/split 等用例。重点审计并更新：

- `tests/test_multi_tool_parsing.py`
- `tests/test_strip_tool_call_text.py`
- `tests/test_local_llm_compactable_payload.py`
- `tests/test_agent_session_context.py`
- `tests/test_multi_agent_runner.py`
- `tests/test_manager_agent_dispatch.py`
- `tests/test_multi_agent_config.py`
- compact/Answer Now 中嵌入旧 opener 的 fixtures

仍有价值的执行、streaming、memory、orchestration 和 Answer Now 行为测试应改用新 transcript，而不是整组删除。代码库中不得残留声称模型“仍可输出旧格式”的 prompt、注释、fixture 或断言。

## 9. Provider-native structured calls 与 Responses 迁移

文本协议和 provider-native structured tool call 是两个独立输入 adapter：

```text
local assistant content -> text protocol parser --+
                                                  +-> internal ChatMessageToolCall -> Agent/orchestrator
provider tool_calls/function_call items -> adapter+
```

保留 `_parse_raw_tool_calls` 或重命名后的等价 adapter，用于 Chat Completions/SGLang/vLLM 返回的原生 `tool_calls`。未来 Responses API 的 `function_call` output item 也直接映射到同一个内部 call type。provider adapter 不扫描 `output_text`，文本 adapter 不解释 provider item；只有明确启用 local text protocol 的路径才解析可见 content。

每个响应必须按以下固定顺序选择唯一数据源：

1. 先运行第 8 节的 exact legacy-opener rejection firewall；命中时原子拒绝整个响应。
2. 若存在 provider-native structured calls，它们是 authoritative source，普通 visible/output text 不送入文本 parser。
3. 但若存在 native calls 的同时，visible content 第一个非空白位置又是 `<<TOOL_CALL>>` 或 `<<SWITCH_AGENT>>`，则属于 ambiguous dual encoding：原子拒绝整个响应，native/text 两边都不执行，并返回 protocol error。
4. 若不存在 native calls，才对 visible content 应用本地文本协议 parser。

这条规则禁止把 native calls 与文本 calls 合并，也禁止在其中一边失败后回退执行另一边。普通伴随文本只要不以新 control opener 或 legacy opener 开始，就不会影响 authoritative native calls。

`<<<...>>>` 对 Chat Completions 和 Responses 都只是普通文本，没有使用常见的 tokenizer/chat-template 特殊 token 形式 `<|...|>`。未来采用 Responses 原生 calls 时，可以停用 local text adapter，而不改变 Agent、orchestrator 或前端协议。

## 10. Streaming 与 UI 隐藏

streaming parser 使用一个小型前缀状态机：

1. 在仅收到开头空白或 marker 的可能前缀时暂存 content delta。
2. 一旦确认前缀不可能成为任一控制 marker，立即把暂存内容作为普通可见文本释放，并按正常方式继续 streaming。
3. 一旦完整识别 `TOOL_CALL` 或 `SWITCH_AGENT` opener，缓冲该 assistant content 的剩余全部 delta，直至响应结束。
4. 完整响应通过原子校验后，仅向既有 tool-call、routing 和 orchestration 事件链提交内部对象；原始 marker/JSON 不进入用户聊天 bubble。
5. 校验失败时不释放已缓冲的协议文本，也不执行调用；只走既有 parsing-error/observation 路径。

marker 可以跨任意多个 delta 拆分。`reasoning_content` 继续按现有 thinking 展示规则处理，但不能送入文本协议 parser。流式空响应的非流式 fallback 仍保留；fallback content 也必须使用同一新 parser，不能回到旧格式。

## 11. 现有系统集成

### 11.1 `LocalLLMModel`

- 新增独立且无执行副作用的协议模块 `backend/agent/text_tool_protocol.py`。
- 文本 parsing 统一经该模块产生 typed result：natural text、tool calls、switch command 或 protocol error。
- provider-native structured calls 继续走独立 adapter。
- `final_answer` 仍对模型隐藏；自然语言仍由 `SmartToolCallingAgent` 自动包装成 final answer。

### 11.2 `SmartToolCallingAgent` 与 orchestrator

- 新 parser 生成现有 `ChatMessageToolCall`/等价内部对象后，再进入 `SmartToolCallingAgent.process_tool_calls()`。
- 多个新 block 保持模型输出顺序，形成一个 stage；现有 ordered shared blackboard、parallel child limit、duplicate child ID preflight 和 orchestration events 不变。
- 单个 dispatch 仍必须走 manager preflight/blackboard 路径；普通单工具可保持现有父类路径。
- `switch_agent` 解析结果仍进入现有 routing/handoff 流程，timeline event 与 SSE projection 不变。

### 11.3 Memory traces

协议文本只是当前响应的输入表示。成功接受后，memory 必须保留结构化 assistant tool calls，随后保留 user-role observations。`SmartToolCallingAgent.write_memory_to_messages()` 不得 strip `tool_calls`，不得把 observation 改成 assistant role，也不得为 cache 命中制造 synthetic empty calls。调用 ID 由后端生成后应在该 turn 的 memory、observation 和事件关联中保持一致。

### 11.4 Compact

- normal prompt 不新增 compact-control system clause。
- cache-first 继续复用未改写的 normal prefix，只追加自包含的 final user-channel compact instruction。
- compact 输出 validator 应识别新 opener，禁止 summary 输出以可执行控制响应形态泄漏 tool/routing 命令；失败输出在验证通过前不进入 UI 或 memory。
- compact 不调用工具、不路由、thinking-off，且不改变 `performance_ratio`、`max_summary_tokens` 等 latency 参数。
- 历史结构化 tool-call/observation 配对必须原样保留；不得为了新文本格式重写旧历史。

### 11.5 Answer Now

Answer Now 保持正常 system/runtime/tool schema 和 canonical history，只追加现有自包含 no-tool/no-routing/no-planning tail。每次候选响应必须完整缓冲：若 provider 返回 structured call，或可见 content 激活新 `TOOL_CALL`/`SWITCH_AGENT` 控制模式，则整次尝试拒绝且绝不执行。最多允许现有的一次 corrective tail retry；失败协议文本不得泄漏到 UI。reasoning 中的 marker 不作为调用。

### 11.6 Prefix-cache covenant 与迁移边界

部署新协议会改变 agent prompt/tool-instruction schema，属于一次明确、可审计的 intentional agent-profile/tool-schema switch，因此是合法 prefix boundary。部署时应让活跃 Agent runtime 采用一致的新 prompt 与 parser 版本，避免同一 runtime 半新半旧；可以通过 backend 重启/agent runtime 重建或显式 protocol-version mismatch 重建完成。

迁移不得重写已发送历史消息的 role、content、顺序或结构化 tool traces。若旧 session 无法在不改写历史的前提下安全升级，应开始新的 Agent context，而不是就地转换旧 transcript。边界之后立即建立完整的新稳定 prefix；不能先使用临时 slim prefix 再恢复 schema。SGLang 与 vLLM 对等逻辑请求必须看到等价的新 prompt 和 tool schema。

## 12. 预计影响文件与组件

主要代码和配置：

- `backend/agent/text_tool_protocol.py`：新 framing、strict JSON、typed result 与错误信息。
- `backend/agent/local_llm_model.py`：接入新 parser，删除 legacy parser/repair，保留 provider adapter。
- `backend/agent/agent_runner.py`：stream buffering、control result 分派、Answer Now 拒绝和 parsing feedback。
- `config/config-agent-prompts.yaml`：coordinator、manager、basic/VLM 相关说明及 examples 全部切换为新协议和两空格 JSON。
- `backend/compact/*` 中实际负责 output validation/prompt control 的模块：加入新 marker 的 no-tool/anti-leak 约束，但不改变 prompt shape。
- 相关 backend tests：替换 transcript fixtures，并新增端到端 production-path 回归。

`backend/agent/orchestrator.py` 原则上无需改变业务逻辑；若仅为接入 typed result 做极小适配，也不得改变 stage/blackboard/SSE 语义。前端文件和 SSE/timeline schema 不在修改范围内。

## 13. 测试矩阵

### 13.1 Parser 单元测试

- 单 tool block、多个连续 block、block 间空行、LF/CRLF、首尾空白。
- 参数 object 含嵌套 object/array、中文、emoji、LaTeX、Shell 和 `:';[{}<\\|>`。
- JSON string 中包含转义双引号、反斜杠、`\n` 和 marker 字面量。
- prompt/canonical formatter 输出两空格缩进。
- 后端生成的调用 ID 唯一，block 顺序不变。
- 顶层 array/scalar/null、重复 key（包括嵌套）、NaN/Infinity、尾随逗号、单引号、注释、原始换行、第二个 JSON value 均拒绝。
- 缺 opener/closer、marker 不独占规定位置、未知工具、额外 prose、route/tool 混合均拒绝。
- 第二个或后续 block malformed 时返回正确 block index，并且结果中没有部分 calls。

### 13.2 消歧测试

- marker 位于普通 prose、JSON demo、公式、Shell 输出或 Markdown fence 内时，不触发。
- reasoning 包含完整 control response，而 visible content 是自然答案时，不触发。
- visible content 第一个非空白字符是 opener 时进入严格控制模式。
- 真实回答需要展示协议时，以 fence 或前置说明安全展示。
- exact legacy opener 在第一个非空白位置时必须返回 rejection-only error 且绝不执行；测试须证明没有调用任何旧 JSON helper。正文中间和 fenced code block 中的 legacy opener 不触发 firewall。

### 13.3 Streaming 测试

- opener、工具名、JSON 和 closer 分散在多个 delta 中仍能正确解析。
- 可能的 marker 前缀后来发生分歧时，普通文本完整且按序释放。
- 确认进入控制模式后，原始 marker/JSON 不出现在 chat content。
- malformed/incomplete control response 结束时零执行、零 raw-protocol UI leakage。
- Qwen3-VL empty-stream 非流式 fallback 使用同一个新 parser。

### 13.4 集成与端到端测试

- 新 transcript parser → `SmartToolCallingAgent.process_tool_calls()` → 普通工具 observation 的完整路径。
- 多个 `dispatching_parallel_agent` block → preflight → ordered shared blackboard → parallel stage 的完整生产路径；不能只 mock 私有 child coroutine。
- duplicate child ID 在任何 child 启动前拒绝；parallel child limit 行为不变。
- 合法和非法 `SWITCH_AGENT` 从 coordinator 到 routing timeline/SSE 的完整路径。
- provider-native structured calls 不经文本 parser，并映射到同一内部 object。
- native calls 与普通 visible text 共存时以 native calls 为 authoritative source；visible text 不被扫描。
- native calls 与以新 reserved opener 开始的 visible content 共存时视为 dual encoding，整段拒绝且零执行。
- legacy opener rejection firewall 在 native/text adapter 分派前同样生效。
- memory 序列化仍为 assistant tool call → user observation，compactable payload 行为不回归。
- Compact cache-first、reliable fallback gating、summary validation 和 prefix shape 不回归。
- Answer Now 对 text/native calls 均零执行、完整缓冲、最多一次纠正，普通答案照常显示。
- Text/Vision 的 Agent API 行为一致；无需新增前端特例。

### 13.5 静态清理测试

- prompt、注释和运行时代码中不再宣称支持 canonical outer array、XML、`动作:`、裸 JSON 或 Python literal。
- legacy helper 无定义、无引用；不得用 unreachable branch 保留。
- 测试 fixtures 不再把旧 transcript 当成功调用输入。
- 前端类型、SSE/timeline schema snapshot 不发生变化。

## 14. 验收标准

实现满足以下全部条件才算完成：

1. 单工具调用只需要 opener + 工具名 + strict indented JSON object + closer。
2. 多工具调用通过连续 block 表达，模型不生成 outer array、wrapper 或 call ID。
3. `switch_agent` 只使用 `<<SWITCH_AGENT>> target`，旧 route JSON 不再执行。
4. 只有 whole-response control mode 可以触发调用；prose、fence、demo 和 reasoning 无误触发。
5. 任意 malformed block 导致该 turn 零工具执行，并返回准确、可操作的 parsing feedback。
6. 所有 JSON repair 与 legacy textual parser 代码和成功兼容测试被删除，不遗留死代码；exact legacy opener 的永久 firewall 只拒绝，永不解析、提取、修复或执行 payload。
7. provider-native structured adapter 独立保留，并可作为未来 Responses `function_call` item 的入口。
8. 数据源选择是确定性的：native calls authoritative；无 native calls 才使用文本 parser；native calls 与新 control opener 双重编码时原子拒绝、零执行。
9. streaming 中 marker/JSON 不泄漏到聊天 bubble；自然文本 prefix false alarm 不丢字符。
10. orchestrator 并行语义、memory tool traces、Compact、Answer Now、prefix-cache covenant 全部通过回归。
11. 前端和 SSE/timeline schema 无变化，SGLang 与 vLLM 的逻辑 prompt/tool shape 等价。
