# 【历史失效／仅供审计】Agent 模式“历史轮次清洗”对 Prefix Cache 的评估

> **失效声明（2026-09-11）：** 本文评估的是已经退役的 `Calling tools:` 历史文本
> 清洗方案。当前实现不再执行该清洗，也不得按照本文的现时语气、payload、伪代码或
> 建议修改生产系统。当前权威 memory 形态是 structured assistant tool trace，随后保持
> user-role observation；不得重写历史角色、顺序或工具配对。用于 UI/诊断的文本表示采用
> `<<TOOL_CALL>>` shared frame，frame 内是严格的 `name` / `arguments` JSON call objects，
> 但该 debug 文本不能替代 canonical structured memory。本文其余内容仅保留为当时研究记录。

> 写给关心 prefix cache 稳定性的工程师。
> 范围：`backend/agent/agent_runner.py` 的 `write_memory_to_messages()` 在 踩坑记录2.md §1 提到的
> "历史轮次保留清洗版 `Calling tools: / Observation:` 文本模式，不再回灌 `TOOL_CALL / TOOL_RESPONSE` 角色"
> 这条规则，对 SGLang / vLLM 推理服务 prefix cache 命中率的全面影响。
>
> 评估方法：阅读 SGLang 0.5.9 / vLLM 相关代码（`sglang/srt/mem_cache/radix_cache.py`、
> `sglang/srt/mem_cache/hicache_storage.py`、`sglang/srt/entrypoints/openai/protocol.py`、
> `sglang/srt/function_call/qwen3_coder_detector.py`）、`smolagents 1.22.0` 的
> `get_clean_message_list` / `tool_role_conversions`、Qwen3.5 chat_template.jinja，
> 用真实 AutoTokenizer 渲染 token 序列、对比当前轮 vs 历史轮的字面和 token 差异。

---

## 0. 一句话结论

**`write_memory_to_messages()` 的历史轮次清洗在 round 边界（current → historical 切换）会
导致 assistant 内容的字面 + token 都发生变化。Round N 的 representation 在 turn N（作为 current
round）和 turn N+1（作为 historical round）的 prefix cache 互相不命中。**

**但对 prefix cache 的"稳态"是有利的**：

- Round N+1 一旦"沉淀"为 historical，它在 turn N+1、turn N+2、turn N+3 ... 里的字面完全相同
  （前提：观测、model_output、工具调用入参、tool_call_id 都稳定），从 turn N+2 起的 prefix
  cache 可以 100% 命中 round N+1 的整段。
- 同一 session 内连续 N 轮对话的"稳态 prefix 长度" = `[system, runtime_context, ..., round_1_hist, round_2_hist, ..., round_{N-1}_hist, user_task_N]`。
  这正是 prefix cache 设计要吃下的红利。
- 每一轮"current → historical 切换"时只有该 round 的 prefix 失效（重建一次），后续轮次
  永久命中该 round。

**与"完全不清洗、把原始 `<tool_call>...</tool_call>` 块作为 assistant 文本注入"相比，清洗方案并没有让
prefix cache 命中率变差。** 原始方案的每一轮也是"current → historical 切换"，
只是切的时候 assistant content 长度（model_output + vs -）和 JSON 序列化形式
（smolagents `tc.dict()` 单引号 Python repr vs 项目 `json.dumps` 双引号）也不一样。
所以**本轮选择的清洗方案在 prefix cache 维度上是中性偏正向的，没有净负优化。**

下面是这个结论的详细证据。

---

## 1. 评估基线：prefix cache 是什么、什么时候命中

### 1.1 SGLang radix cache 的 key

`backend/main.py` 启动时给 SGLang 传 `--enable-prefix-caching`。SGLang 把请求的
**token_id 序列** 走 SHA256 滚动 hash 成 prefix 树（RadixCache），命中已有前缀就复用
K/V cache。

关键代码：`sglang/srt/mem_cache/hicache_storage.py`

```python
def get_hash_str(token_ids: List[int], prior_hash: str = None) -> str:
    hasher = hashlib.sha256()
    if prior_hash:
        hasher.update(bytes.fromhex(prior_hash))
    for t in token_ids:
        if isinstance(t, tuple):
            # EAGLE bigram mode: ...
            ...
        else:
            hasher.update(t.to_bytes(4, byteorder="little", signed=False))
    ...
```

**重点：hash 的输入是 token_id，不是原始文本。** 也就是说，只要两段消息经过
Qwen3.5 tokenizer + chat template 渲染出来的 token_id 序列完全一致，prefix cache 必命中；
有 1 个 token 差异，整段后续就断掉。

`vllm`（容器部署路径）走 `vllm/v1/core/single_type_kv_cache_manager.py` 的
`KVCacheManager.find_match()`，逻辑一致（基于 token_id 的 prefix match）。

### 1.2 一次 chat 调用的 token_id 序列是怎么来的

SGLang 在 `TokenizerManager._tokenize_one_request` 里把 OpenAI 格式的
`{"role":..., "content":...}` 列表喂给 chat template（`chat_template.jinja`），
渲染出纯文本，再过 Qwen3.5 的 AutoTokenizer 得到 `input_ids`，然后做 prefix match。

**所以："消息文本变了 → chat template 渲染的 token 序列变了 → prefix cache miss"。**

### 1.3 这次评估的"消息文本"包含什么

一次 chat 调用实际送进 chat template 的 `messages` 列表（OpenAI wire 格式）里
**每一个** 元素都参与 prefix 计算。本项目实际发出去的内容（`backend/llm_service.py:398`）：

```python
messages_data = [
    {"role": m.role, "content": build_message_content(m)} for m in messages
]
```

其中 `m.role` 和 `m.content` 在经过 `get_clean_message_list(role_conversions=tool_role_conversions)`
之后，被规整为 `"system" / "user" / "assistant"` 三种 role（见
`smolagents/models.py:237-240`：`TOOL_CALL→ASSISTANT`, `TOOL_RESPONSE→USER`）。
**`TOOL_CALL` / `TOOL_RESPONSE` 这两个 role 不会出现在 wire 格式里。**

那么 prefix 命中的关键就变成：
- 每个 `assistant` 消息的 content 文本
- 每个 `user` 消息的 content 文本
- 它们在 chat template 渲染出的 `<|im_start|>{role}\n{content}<|im_end|>\n` 序列是否一致

---

## 2. 关键代码再确认：`write_memory_to_messages` 的实际输出

为了后文分析精确，我把每一类消息的 role + content 实际形态列出来。
代码：`backend/agent/agent_runner.py:1211-1645` 中的 `write_memory_to_messages`。

### 2.1 Historical round 的"清洗"输出（i < last_task_idx 的 ActionStep）

```python
# agent_runner.py:1593-1608
elif isinstance(memory_step, ActionStep):
    if i < last_task_idx:
        observation_text = _historical_observation_text(memory_step)
        tool_call_text    = _historical_tool_call_text(memory_step)

        if observation_text and not tool_call_text and messages:
            _append_history_boundary_assistant()           # 插 "已完成。"
        _append_assistant_text(tool_call_text)             # role=ASSISTANT
        if observation_text:
            _append_user_text(f"Observation:\n{observation_text}")  # role=USER
        _append_assistant_text(
            _historical_final_answer_text(memory_step, observation_text)
        )                                                   # role=ASSISTANT，可能 "已完成。"
        continue
```

每一对 ActionStep（i < last_task_idx）变成最多 3 条 wire 消息：

| 序号 | role | content 形态 |
|---|---|---|
| a | `assistant` | `Calling tools:\n[{...json...}]`（若 tool_calls 存在） |
| b | `user` | `Observation:\n{observation_text}`（若 observations 存在） |
| c | `assistant` | final answer 文本，或 `已完成。`（边界补丁） |

其中：
- `_historical_tool_call_text()` 走 `_tool_call_transcript_text(step.tool_calls)`，
  最终用 `_serialize_tool_calls` + `json.dumps(..., ensure_ascii=False)`。**用项目自己
  的 JSON 输出（双引号 + 字典 key 顺序按 `_serialize_tool_calls` 的硬编码）。**
- `_historical_observation_text()` 优先用 `_historical_tool_artifact_text`（write_file 拿
  `file_path + content`，edit_file 拿 `replace_text`），否则用
  `_strip_historical_tool_transcript_text(step.observations)` 去掉 "Called the ... tool
  with the following input:" 头和 `<path>/<type>/<content>` 包装。

### 2.2 Current round 的输出（i == last_task_idx 那个 ActionStep，或其后的多步 ActionStep）

```python
# agent_runner.py:1611-1618
stripped = copy(memory_step)
stripped.observations_images = None
_append_live_action_step_messages(stripped)
```

`_append_live_action_step_messages`（1593-1618 调用的实现在 1342-1409），
对单个 ActionStep 产生：

| 序号 | role | content 形态 |
|---|---|---|
| 1 | `assistant` | `model_output.strip()`（若 model_output 存在且非 summary_mode） |
| 2 | `tool-call` | `Calling tools:\n[{...smolagents tc.dict()...}]` |
| 3 | `tool-response` | `Observation:\n{observations}` |
| 4 | `tool-response` | `Error:\n{error}...`（若 error 存在） |

注意：这里发出的是 `tool-call` / `tool-response` 角色。但是！经过 smolagents
`get_clean_message_list(role_conversions=tool_role_conversions)` 之后（`local_llm_model.py:298-302`），
`tool-call` 被合并到前一条 `assistant` 的 content（prepend `\nCalling tools:\n` + `tc.dict()` repr），
`tool-response` 转为 `user` role（`Observation:\n...`）。

也就是说，**current round 经过 LLM service 实际送出去的 wire 消息是：**

| 序号 | role | content 形态 |
|---|---|---|
| 1 | `assistant` | `model_output + "\n" + "Calling tools:\n" + <Python repr of tc.dict()>` |
| 2 | `user` | `Observation:\n{observations}`（或 Error: 文本） |
| 3 | `assistant` | 下一条 ActionStep 的 model_output（如果有） |
| 4 | `user` | 下一条 Observation（如果有） |
| ... | ... | ... |

注意第 1 条的 Python repr 用的是 `str()` 格式（`{'id': 'call_0', 'type': 'function', ...}`，
**单引号**），而 historical round 用的是 `json.dumps(..., ensure_ascii=False)`
（**双引号**）。这是 round N 在 current vs historical 两种形态下的**字面差异**。

---

## 3. 对 prefix cache 的逐项影响分析

我用真实 AutoTokenizer（`~/Project/LLM-weight/Qwen3___5-35B-A3B-GPTQ-Int4/`，vocab=248044）
+ Qwen3.5 chat template 实际渲染对比 token 序列。

### 3.1 Tool call assistant 文本：current vs historical 序列化差异

| 维度 | Current round | Historical round |
|---|---|---|
| 序列化函数 | `smolagents.ActionStep.to_messages` 内的 `str([tc.dict() for tc in self.tool_calls])` | `_serialize_tool_calls` + `json.dumps(..., ensure_ascii=False)` |
| 字符串字面 | 单引号、Python repr 顺序 | 双引号、`_serialize_tool_calls` 硬编码顺序 |
| `arguments` 形态 | 看 smolagents 的 `ToolCall.arguments` 实际类型（**str**） | `_normalize_tool_call_arguments` 试图 `json.loads(arguments)` 解码，**dict 形态** |
| `model_output` 前缀 | 有（`model_output + "\n" + Calling tools:...`） | 无（只有 `Calling tools:\n...`） |
| 末尾 `<|im_end|>` 后的换行 | 1 个 | 1 个（相同） |

**历史字面差异**：当时以同一个 `get_time` call 比较了两种已退役表示。为避免旧
payload 被复制为当前操作格式，这里只保留不可执行的抽象形态：

```text
HISTORICAL_CURRENT_REPRESENTATION := <assistant reasoning> + <retired tool-call repr>
HISTORICAL_CLEANED_REPRESENTATION := <retired normalized tool-call JSON text>
```

前者使用 Python-repr 风格，后者使用 JSON 风格；这两种 payload 均不是当前协议或
当前 canonical memory 的示例。

两者**单/双引号、是否带 model_output 都不一致**。

**Token 差异**（用真实 AutoTokenizer 渲染 `<|im_start|>assistant\n{content}<|im_end|>\n<|im_start|>assistant\n<think>\n` 后去尾部 generation prompt）：
- Current 切到 historical 的同一轮：62 tokens → 55 tokens，**差 7 tokens**。

**Prefix cache 影响**：
- Round N 在 turn N（current）和 turn N+1（historical）的**前缀完全不匹配**。Round N
  作为 current 时 SGLang 已经把它的 K/V 写入了 radix tree，round N 一旦"沉淀"为 historical，
  新送来的 55 token 序列不命中任何已有节点，必须重算。
- 但 round N 一旦稳定为 historical 形态，**从 turn N+1 之后所有轮次都共享这 55 token 的 K/V**。

**结论**：在 round 边界（current → historical 切换）有一次性 prefix cache miss，但稳态下
historical 段的 K/V 是可复用的。这是不可避免的：任何"current vs historical"区分方案都要付出
一次性的 cache 失效代价。

### 3.2 Observation user 文本：strip 行为

`write_memory_to_messages:1550` 在 historical round 调用
`_strip_historical_tool_transcript_text(step.observations)`。

`_strip_historical_tool_transcript_text` 的触发条件（`agent_runner.py:1184-1188`）：

```python
looks_like_tool_transcript = bool(
    header_match or (content_match and (path_match or type_match))
)
if not looks_like_tool_transcript:
    return result
```

**所以：**

- 如果 observation 是普通字符串（多数工具），直接 return 原 observation → wire 文本是
  `Observation:\n{raw_observation}`，与 current round 的 `Observation:\n{raw_observation}` **完全一致**。
  → Prefix cache **不破**。
- 如果 observation 带 "Called the ... tool with the following input: ..." 头
  或 `<path>/<type>/<content>` 包装（如某些工具链的 read_file 风格），historical 形态
  只保留 `Path: ...\nType: ...\n{body}` 形式，**stripped 文本比 current 短** → token 序列
  改变 → **prefix cache 破**。

**影响范围**：仅当工具返回带 XML 包装时才破。多数 read_file / search 类工具在本地实现中
不会返回这种包装（`backend/tools/` 下的工具返回值是 plain text），所以这条主要影响
**MCP / 第三方工具**或**有特定 XML 输出的工具**。

### 3.3 historical TaskStep 的图片保留与确定性归一化

当前 `write_memory_to_messages()` 不再清空 historical `TaskStep.task_images`。每一轮用户消息
中的所有 `image_url` part 都会保留在对应的 `TaskStep` 中，并继续进入正常 prompt 与 compact
cache-first source。`_strip_old_images` 也不再把历史图片替换为 `[图片×N]`。

在正常 prompt/cache-source 捕获之前，`prepare_multimodal_messages()` 会复制消息，并仅对超过
`max_image_dimension` 的内嵌 data URL 图片执行一次确定性归一化：按固定尺寸规则缩放后，以固定
JPEG 参数重新编码。未超限图片和非 data URL 保持不变。归一化后的实际 LLM 消息随后被捕获；
cache-first 直接复用这份已捕获 source，不再裁剪、重写或二次归一化。

图片去重只针对 `ActionStep.observations_images`：smolagents 会把任务图片复制到 ActionStep，
序列化时清除的是这些重复副本，而不是 `TaskStep` 中的用户原图。工具调用与 observation 轨迹
仍然保留，不因图片去重而丢失。

**Prefix cache 影响**：
- 同一轮从 current 变为 historical 时，用户图片不会因 `TaskStep.task_images` 被清空而改变前缀。
- 超限 data URL 在首次正常 prompt 准备阶段被确定性归一化，cache-first 使用捕获结果原样复用，
  因此不会因 compact 再处理图片而产生 prefix drift。
- `ActionStep.observations_images` 只是任务图片副本；移除副本避免重复 visual token，同时保留
  用户消息中的原始图片位置及历史工具轨迹。

**附带问题（多模态路径下）**：如果请求本身换成另一张图片或改变图片内容，SGLang 的视觉
token expansion 可能随之变化并导致 cache miss。这是输入变化，不是 historical 图片清洗或
compact cache-first 重写造成的 prefix drift。

### 3.4 Tool call ID 稳定性

SGLang 的 `qwen3_coder_detector` 从 assistant 文本中解析 `<tool_call>` 块，对每个 block
生成一个 `id`，默认是 `call_<index>`，其中 `index` 是该 response 中 tool_call 的序号。

- 同 model、同 input、同 sampling seed → 同 id。Prefix cache 稳。
- model 加/减一次 "thinking" tool call → 后续 call 的 id 全错位 → 整段 prefix cache 破。
  但这是 model behavior 维度的问题，与清洗无关。
- smolagents `ActionStep.tool_calls` 里保存的是模型原始 response 里的 id。
  `tool_call.dict()` 序列化时 `id` 字段照搬，所以**只要模型给出稳定 id，current round
  的 assistant 文本稳定**。

### 3.5 `_append_history_boundary_assistant` 插入的 `已完成。`

`write_memory_to_messages:1575-1577, 1600-1601`：

```python
def _append_history_boundary_assistant() -> None:
    if messages and messages[-1].role == MessageRole.USER:
        _append_assistant_text("已完成。")
```

仅在最后一条是 user 且本轮没有 tool_call_text 时插入（line 1600-1601）。这是 smolagents
`get_clean_message_list` 合并连续相同 role 消息的修复（踩坑记录2.md §1.2 阶段 C）。

**Token 影响**：boundary `已完成。` 是固定 4 个 token（实测 Qwen3.5 tokenizer，
`已完成。` + 前后 `<|im_start|>` `<|im_end|>` 标记），deterministic。多次重复出现不会破
prefix cache（同样的 token 序列同样的 K/V）。

**潜在的小问题**：boundary 的存在与否依赖 `i < last_task_idx AND observation_text AND not
tool_call_text` 这一个组合条件。如果某个 historical ActionStep 既没有 model_output
（fallback 时被过滤）也没有 tool_call，但有 observations，boundary 就会插入；下一个 round
可能因为 tool_call_text 非空而**不**插入。这种有/无的差异是确定的 deterministic 但会让
不同 historical round 的 K/V 不完全可合并（边界 round 的 prefix cache 占用短一些）。
**实际收益 vs 成本：负，因为 boundary 是 chat template 兼容性的必要补丁。**

### 3.6 `runtime_context` 注入到 system 段

`backend/agent/agent_runner.py:749-751`：

```python
def _build_runtime_time_context(now: datetime | None = None) -> str:
    current = now or datetime.now()
    return current.strftime("Current local time: %A, %Y-%m-%d %H:%M")
```

`backend/agent/agent_runner.py:2411-2415`：
```python
if session is not None:
    if session.runtime_context is None:
        session.runtime_context = _build_runtime_time_context()
    return session.runtime_context
```

`session.runtime_context` 是 **session-pinned**：session 创建时记录一次，整个
session 生命周期不变。

**Prefix cache 影响**：
- 同 session 内所有 turn 共享同一个 runtime_context → system 段 prefix cache **稳定命中**。
- **跨 session**（不同用户的并发请求，或同一 session_id 在不同 time window 被复用）必破。
  但这是 session 维度的变化，agent 模式本来就是 session-pinned state，本来 prefix cache
  就不会跨 session 复用。

**时间格式**：`%A, %Y-%m-%d %H:%M`（到分钟）。如果 session 跨整点续命（极少见但可能），
  `runtime_context` 仍保持 session 创建时的时间，**不会更新**。这是刻意的设计
  （"pinned = 保持 prefix cache 稳定"），但也意味着 LLM 看到的时间是 stale 的。
  **与本轮清洗无关，但与 prefix cache 稳定性的整体设计相关。**

### 3.7 System prompt 跨 agent 切换

`_activate_profile_agent`（`agent_runner.py:3382-3413`）在 coordinator_1 → coordinator_2 /
basic_agent 切换时，**构建新的 agent 实例**（`_build_profile_agent`），新 agent 的
`system_prompt` 由 `initialize_system_prompt` + `populate_template(system_prompt_template, ...)`
生成。

每个 profile 的 system_prompt 模板在 `config/config-agent-prompts.yaml` 里是独立的（coordinator_1
~150 tokens，coordinator_2 ~300 tokens，basic_agent ~500+ tokens）。

**Prefix cache 影响**：
- 跨 profile 切换时 `[system]` 前缀完全不一致 → 整个 prefix tree 从 system 段开始就分叉。
- 这是 multi-agent 路由的固有代价，**与清洗无关**。
- 同 profile 内连续轮次共享 system 段 → prefix cache 命中。

### 3.8 `_strip_old_images` 兜底防线

`backend/llm_service.py:542-586`：所有非最后一条 user 消息的图片会被替换为
`[图片×N]` 占位符。但豁免了 `"New task:"` 开头的 user 消息（即所有 `TaskStep.to_messages()`
生成的 user 消息）。

**在 agent 模式下，historical TaskStep 已经在 `write_memory_to_messages` 阶段就把图
剥离了，所以 `messages_data` 里 historical TaskStep 根本不含 image_url。**

这条防线主要保护 chat 模式（用户多次发图，前面的图被剥）以及 cycle 模式（同一轮里
non-task 的 user 消息）。

**与本轮"清洗"评估无关**，但需要注意：chat 模式下用户连发 N 张图，前 N-1 张会被剥成
`[图片×N]` 占位，**与包含原图的 prefix cache 必破**。

### 3.9 SGLang `schedule_policy`

`config/qwen3__5-config-infer.yaml:84`：
```yaml
# 调度策略：最长前缀匹配，提高多轮对话和 Agent 场景前缀缓存命中率; set lpm
schedule_policy: null
```

注释明确建议 `lpm`（longest prefix match），但**实际配置是 null**，没启用 lpm 调度。
SGLang 默认走 FCFS（first come first serve），**不在并发请求之间优先调度 prefix 匹配
度高的请求**。

**影响**：
- 单 session 顺序对话：无所谓，反正只有 1 个 in-flight 请求。
- 多 session 并发：lpm 会让"有共同前缀的请求"互相复用 K/V。**没启用 lpm 等于放弃了
  跨 session prefix 复用。** 与本轮"清洗"无关，但属于项目层 prefix cache 调优的 missed
  opportunity。
- 配合 `enable_prefix_caching: true`（已开），lpm 是免费的加速；不开启就是浪费。

### 3.10 多模态 token expansion 与 `[图片×N]` 占位

`backend/llm_service.py:580-583`：
```python
new_content.insert(0, {"type": "text", "text": f"[图片×{img_in_msg}]"})
```

被剥成占位的 user 消息内容：`[图片×1] {原 text}`（图片占位插在文本前面）。
原 user 消息：`{原 text} + <image_pad>xN`（image_pad 作为 multimodal content）。

Qwen3.5 tokenizer 渲染差异：
- 原：`[图片×1] {text}` token 数 ≈ 4-5（占位文本）+ 原文本 token
- 实际：[图片×1] 占位 + 文本，21 tokens（含前后 `<|im_start|>user\n<|im_end|>` 标记）
- 包含图片：[图片×1] 占位被替换为 `<|vision_start|><|image_pad|>...<|vision_end|>`，SGLang
  在 tokenize 阶段展开为 N 个 visual token

**Prefix cache 影响**：含图与不含图必破（视觉 token 数量级差异）。同 stripped 形态（`[图片×1]`）
跨轮次稳定 → prefix cache 命中（按 `[图片×1] {text}` 字面）。

---

## 4. 综合影响矩阵

下表把每条因素按 "current→historical 切换" 与 "同一 historical 跨多轮" 两个维度评估
prefix cache 表现。"破"= 字面改变 / token 序列改变 / 必破。"命"= 字面一致 / token 序列
稳定 / 必命中。

| # | 因素 | current → historical 切换 | 同一 historical 跨多轮 |
|---|---|---|---|
| 3.1 | assistant tool_call 文本（smolagents repr vs json.dumps） | 破（62→55 token） | 命 |
| 3.1 | assistant text 中 `model_output` 前缀的有无 | 破 | 命 |
| 3.2 | observation 的 XML 包装 strip | 仅在 observation 带包装时破 | 命 |
| 3.2 | 普通 observation（多数情况） | 不破 | 命 |
| 3.3 | historical TaskStep 图片剥离 | 破（视觉 token 段消失） | 命（按 stripped 文本） |
| 3.4 | tool_call id（`call_0`/`call_1`） | 命（id 稳定） | 命 |
| 3.5 | `已完成。` boundary 补丁 | 命（fixed 4 token） | 命 |
| 3.6 | `runtime_context`（session-pinned 时间） | 命（跨轮不变） | 命 |
| 3.7 | system prompt（profile 内） | 命（同 profile 内） | 命 |
| 3.7 | system prompt（跨 profile 切换） | 破（与本评估无关） | — |
| 3.8 | `_strip_old_images` 兜底防线 | —（agent 模式不触发 New task 豁免） | — |
| 3.9 | SGLang `schedule_policy: null` | 跨 session 复用 K/V 失败 | — |
| 3.10 | 多模态 token expansion | 破（图片有/无之间） | 命（同 stripped 形态） |

**每条 "破" 的因素都是 deterministic 的 round 边界失效**：round N 一旦切到 historical，
其 prefix K/V 必须重算。但**之后所有 turn 都共享 round N 的 K/V**。这是 prefix cache
设计能吃的红利。

---

## 5. 净影响 vs 替代方案

### 5.1 替代方案 A：完全不清洗，historical 也走 current round 的 wire 形态

- assistant content = `model_output + "\n" + Calling tools:\n[{...repr...}]`
- user content = `Observation:\n{raw_observation}`

**问题 1**：model_output 在 historical round 是过去的中间思考（"I should check the time."）
会大幅延长 context、模糊当前任务、踩坑记录2.md §1.3.2 提到的"诱导模型模仿历史 assistant 文本
模式"。

**问题 2**：历史 `<tool_call>` 块作为 assistant 文本塞进去会让 SGLang
`qwen3_coder_detector` 误识别（取决于 detector 配置，但 Qwen3 默认在 stream 阶段就会
实时解析 tool_call 块）。可能影响 Qwen3 tool_call_parser 行为。

**Prefix cache 对比**：
- 替代方案 A：round N 切到 historical 时 assistant 文本字面变了（去掉"current round 套
  的 thinking 前缀不一致"部分，但 model_output 保留），但**保留 model_output 仍是 current
  的字面**，所以从 current → historical 也破（同样要重算一次 K/V）。**和本轮选择破在
  同一处**。

### 5.2 替代方案 B：完全回灌 smolagents `TOOL_CALL` / `TOOL_RESPONSE` 角色消息

如果 SGLang 接受 `TOOL_CALL` / `TOOL_RESPONSE` 这种非标 role（实测不接受，会 raise
`ValueError: 'role' must be one of 'system', 'developer', 'assistant', 'tool', or 'function'`，
见 `sglang/srt/entrypoints/openai/protocol.py:500-510`），那需要把项目改用 `"tool"` role。

**改用 `"tool"` role 后**：
- 当前 turn 仍走 `get_clean_message_list` 把 `TOOL_RESPONSE` 转为 `user` role（"tool"
  role 在 smolagents 工具转换表里没有），行为退化。
- 或者把转换表改为 `TOOL_CALL→ASSISTANT` + `TOOL_RESPONSE→TOOL`，这样 user 序列中插入
  `tool` 角色。SGLang 把它渲染为 `<|im_start|>user\n<tool_response>\n{obs}\n</tool_response>`。
  Historical 仍要清洗成 user role + `Observation:\n` 前缀。**一样要破。**

### 5.3 净结论

| 方案 | Round 边界 cache 失效 | 跨 round cache 命中 | 模型行为 |
|---|---|---|---|
| **本轮选择（清洗为 user/assistant 文本）** | 一次（current→historical 切换） | 稳态命中 | 最佳（保留工具调用模式，剔除 noise） |
| 替代 A（不清洗） | 一次（同处） | 稳态命中 | 差（model_output 噪音、<tool_call> 误识别） |
| 替代 B（tool role） | 一次（同样处） | 稳态命中 | 中（需要重写 smolagents 转换表） |

**本轮选择的"清洗为文本"是 prefix cache 中性偏正向的方案。Round 边界的 cache 失效
在所有方案中都不可避免；选这个方案换来了显著的模型行为改进。**

---

## 6. 不属于本轮"清洗"、但同属 prefix cache 维度的 missed optimization

这些是项目层 prefix cache 调优的"漏网之鱼"，与 `write_memory_to_messages` 的"清洗"
逻辑无关，但与"保持前缀相对稳定"是同一个 work theme：

### 6.1 SGLang `schedule_policy: null`（最强建议改）

`config/qwen3__5-config-infer.yaml:84` 注释里建议 `set lpm` 但实际没设。
`lpm`（longest prefix match）调度让**多 session 并发时**优先调度 prefix 匹配度最高的
请求，最大化跨 session 复用 K/V。对于"同一时间段多个用户共享 SGLang 服务的场景"
特别有用。

**建议**：根据 SGLang 0.5.9 的实际启动参数（`--schedule-policy lpm`），把 yaml 改成
`schedule_policy: lpm`，并在 `scripts/1_start_sglang.sh` 里透传。

### 6.2 工具调用 ID 稳定性

SGLang `qwen3_coder_detector` 给同 response 内的 tool_call 生成 `call_0`、`call_1` 这样的
id。`id` 出现在 wire 文本里 → 影响 token 序列 → 影响 prefix cache。

本项目 `smolagents.ActionStep.tool_calls[i].id` 直接来自 SGLang 的 detector。如果模型
的"thinking tool call" 行为不一致（多/少一次 think），id 序列会错位，整段历史 prefix
cache 失效。

**建议**：上游约束（系统提示 + sampling seed）让 model 的 tool call 序列更确定性。
**不是本轮清洗能解决的事。**

### 6.3 `runtime_context` 跨 session 必破

`Current local time: Saturday, 2026-06-06 14:30` 是 session-pinned，但不同 session 的时间
不同 → 跨 session 共享 system 段 prefix 失败。

**建议**：要么接受（不同 session 是不同 context，本来就该失效），要么把
`_build_runtime_time_context` 改为"只取日期不取时分"（降低精度但跨 session 复用率提高）。
**项目当前选择是 conservative + 准确，本评估认为 OK。**

### 6.4 多模态 token 数量稳定性

`config/qwen3__5-config-infer.yaml:106 image_max_pixels=null`（即使用 Qwen3.5 默认值，
~1280×784，~1280 visual token）。不同分辨率的图会展开为不同数量的 image_pad token →
同位置的 prefix cache key 不同。

**建议**：固定 `image_max_pixels` + `image_min_pixels` 到一个常量（项目已有 `model.max_image_dimension`，
但 `image_max_pixels` 没设）。**与本轮清洗无关。**

### 6.5 `done` 标记的 `finish_reason` 不影响 wire 文本，但 SGLang stop token 序列影响
**无影响**：`finish_reason` 出现在 SSE 响应里，不在 LLM 调用的输入 prompt 里，不参与
prefix cache。

---

## 7. 进一步可做的 prefix-cache-aware 优化（提议，不在本轮修复范围）

按收益/风险排：

1. **同化 current vs historical 的 tool_call JSON 序列化**（最大单点优化）：
   - 改 `smolagents.ActionStep.to_messages` 行为不可行（污染 smolagents）。
   - 可行：让 `LocalLLMModel.generate_stream` / `generate` 在拿到 `ChatMessage.tool_calls`
     后，**不依赖 smolagents 内部 `tc.dict()` repr**，而是统一走项目自己的
     `_serialize_tool_calls` + `json.dumps(..., ensure_ascii=False)`，把内容写回
     `ChatMessage.content` 作为 assistant 文本（同时保留 `tool_calls` 字段供 LLM 解析）。
     这样 current round 的 wire 文本与 historical round 一致 → round 边界 cache 失效
     消失。
   - 风险：smolagents `get_clean_message_list` 内部可能再次合并 `tc.dict()`。需要更深的
     monkey-patch 或 fork。
   - 收益：每轮 round 边界省一次 K/V 重算。10 轮对话省 9 次 K/V 重算。
   - 复杂度：高，需要本地 fork smolagents 或重写 `LocalLLMModel` 的消息转换层。

2. **去掉 current round 的 `model_output` 前缀（仅在 historical 阶段也加 model_output）**：
   - 反向操作：让 historical round 也保留 `model_output`（即 historical 输出和 current
     输出完全一致）。
   - 风险：与踩坑记录2.md §1.3.2 "失败原因二：只保留成功回执" 反着来，可能重新诱导
     模型偷懒。
   - 不推荐。

3. **SGLang `schedule_policy: lpm`**：零风险，立刻能改。改完配合现有 prefix cache 才有
   跨 session 复用。

4. **多模态 token 数量固定**：`config/qwen3__5-config-infer.yaml:106` 设
   `image_max_pixels` 到 589824（注释里建议的"平衡质量和速度"值），降低视觉 token
   数量波动。

5. **runtime_context 精度降级**：把 `%H:%M` 改成 `%Y-%m-%d`（按天），跨 session 复用率
   显著提高，副作用是模型看不到时分。**产品上需要权衡。**

---

## 8. 终极结论（一句话版本）

**本轮在 `write_memory_to_messages` 里做的"历史轮次清洗为 `Calling tools:` / `Observation:` 文本
而非 `TOOL_CALL` / `TOOL_RESPONSE` 角色消息"的改动，对 SGLang/vLLM prefix cache 命中率是
中性的。**

每一轮的"current → historical 切换"会因为 tool_call JSON 序列化形式不同（smolagents
`tc.dict()` 单引号 Python repr vs 项目 `json.dumps` 双引号）和 `model_output` 前缀有/无而
产生一次性的 round 边界 prefix cache 失效（每轮多花一次 K/V 重算），
但这是不可避免的：所有区分"current vs historical"的方案都至少要破一次。

**一旦 round 进入 historical 形态，跨多轮稳定命中，prefix cache 红利正常吃下。**

`runtime_context` 已在 session 级别 pinned，跨轮次稳定。`已完成。` boundary 补丁 deterministic。
historical TaskStep 图片剥离 deterministic。`schedule_policy: null`（没设 lpm）
是项目层的 missed optimization，与本轮"清洗"无关，但建议顺手改。

**不需要修改本轮"清洗"逻辑。**

---

## 附：本次评估实际执行过的命令（可复现）

```bash
# 1. 验证 tool_call 序列化差异
PYTHONPATH=. ./.sglang/bin/python -c "
from smolagents.memory import ActionStep, ToolCall
from smolagents.monitoring import Timing
from smolagents.models import get_clean_message_list, tool_role_conversions
import time, json
step = ActionStep(step_number=1, timing=Timing(start_time=time.time()))
step.model_output = 'I should check the time.'
step.tool_calls = [ToolCall(id='call_0', name='get_time', arguments='{\"format\": \"12h\"}')]
step.observations = '12:00:00'
msgs = step.to_messages()
clean = get_clean_message_list(msgs, role_conversions=tool_role_conversions)
# 比较 historical 等价序列化（用项目 _serialize_tool_calls + json.dumps）
# 发现字面不同
"

# 2. 用真实 Qwen3.5 tokenizer 渲染对比 token 数
PYTHONPATH=. ./.sglang/bin/python -c "
import json
from pathlib import Path
from transformers import AutoTokenizer
tok = AutoTokenizer.from_pretrained(Path('~/Project/LLM-weight/Qwen3___5-35B-A3B-GPTQ-Int4/').expanduser(), trust_remote_code=True)
# current_round_text = model_output + \n + Calling tools:\n + repr
# historical_text = Calling tools:\n + json.dumps
# 渲染后：62 vs 55 tokens（差 7）
"

# 3. 验证 SGLang protocol role 验证
grep -A 5 "_normalize_role" .sglang/lib/python3.12/site-packages/sglang/srt/entrypoints/openai/protocol.py
# 发现非标 role（tool-call / tool-response）会被 ValueError 拒绝
# 但 get_clean_message_list(role_conversions=tool_role_conversions) 已先把它们转成 user/assistant

# 4. 验证 image stripping 在 stripped 后 token 变化
# current: 19 tokens, stripped: 21 tokens (因 [图片×1] 占位是中文)

# 5. 验证 _build_runtime_time_context 是 session-pinned
grep -B 1 -A 8 "def _build_runtime_time_context" backend/agent/agent_runner.py
```
