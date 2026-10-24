# LLM-Deployment 项目 — Copilot 上下文指南

## 按任务定位源码

硬约束始终适用。先 `rg` 符号、限范围读，跨层则跟调用链；历史行号非锚点。展示改动无需通读 routes/`api.ts`/`agent_runner.py`/历史 specs/依赖。

下表 `C` = `frontend/src/lib/conversation/`，`L` = `frontend/src/lib/`，`U` = `frontend/src/components/`。

| 任务 | 入口（再跟相关调用） |
|------|--------------------|
| UI | `U/conversation/` 对应受控组件及叶子 |
| 状态/配置 | `C/{use-conversation-state,use-conversation-bootstrap,conversation-bootstrap}.ts` |
| send/Stop/Clear/mode | `C/{use-conversation-send,conversation-transport,conversation-actions,conversation-tool-admission}.ts` |
| Hub | `C/use-conversation-recovery.ts`；`L/{session-hub-recovery,chat-session-handlers,text-agent-session-handlers}.ts` |
| Cycle | `L/{cycle-capability,conversation-cycle,use-conversation-cycle,cycle-page}.ts`；`U/CycleEditor.tsx` / `CycleStepEditor.tsx` |
| Vision media | `frontend/src/app/vision/page.tsx`；`U/ImageUploader.tsx` / `CameraCapture.tsx`；相关 `L/vision-automation/` 模块 |
| voice/模板/slash/compact | `C/{use-conversation-voice,use-conversation-commands}.ts` |

SSE/recovery fixtures **须核对** `backend/session_hub_runtime.py` 的 `SessionProducerPublisher.accept/publish/finish` 和 `backend/api/chat.py` / `agent.py` 真实 producer。

## 项目架构

本项目是一个完整的 LLM 推理部署系统，运行在 **NVIDIA Jetson Thor** (SM110a) 上，包含三层：

| 层级 | 路径 | 技术栈 | 启动脚本 |
|------|------|--------|----------|
| 推理引擎 | SGLang 服务 | SGLang + Qwen3/3.5 系列模型 | `scripts/1_start_sglang.sh` |
| 后端 API | `backend/` | Python + FastAPI + smolagents | `scripts/2_start_backend.sh` |
| 前端 UI | `frontend/` | Next.js 14 + React + Tailwind | `scripts/3_start_frontend.sh` |

## Python Env
Path: ./.sglang
Note: 使用 uv 管理环境；不要擅自修改依赖包源码，如有必要修改，须先征得用户同意。

## vLLM Container Inference

- Jetson Thor 的受支持 vLLM 部署目标是 NVIDIA NGC container，不是本地 uv/source vLLM。Hugging Face cache/snapshot 布局使用 `scripts/1b_start_vllm_container_with_snapshots.sh`；普通独立模型目录使用 `scripts/1c_start_vllm_container.sh`。
- 模型与推理配置仍由 `config/config-overall.yaml` 选择的 model/infer YAML 驱动。vLLM API server 参数应沿 `config/*-infer.yaml -> container launch script -> vllm.entrypoints.openai.api_server` 生效，不要在 shell 中复制一套硬编码模型参数。
- 容器通常使用 host networking 和 infer YAML 中的 vLLM host/port（常见端口为 8000）。以 backend `/config` 返回的 `inference.active_backend` 作为实际 provider 诊断结果，不要仅根据端口或启动脚本猜测。
- vLLM 没有使用本项目的 SGLang `/abort_request`。Stop 依赖 request ID 定位并关闭已登记的 upstream HTTP response，让 vLLM 因客户端流断开而取消请求；SGLang 路径还会额外发送 targeted `/abort_request`。
- request abort 必须支持 response 注册前的 pending abort，并以 registration token 防止旧 worker cleanup 清掉同 ID 的新请求。单 session "Stop" 禁止 fallback 到 abort-all。


## Prefix-Cache Covenant

These rules are mandatory for every backend path that sends requests to the local SGLang or vLLM service, including normal chat/vision, all agent profiles and tool loops, routing/handoff, the turn after Stop, retries/fallbacks, `/compact`, auto-compact, prompt-shape, and memory serialization. Breaking them makes long-context inference slower and can also corrupt model context.

The global request shape is: session-pinned system/runtime/tool prefix + canonical serialized prior memory/messages + append-only current-turn tails.

1. Previously sent historical message roles, content, ordering, and tool traces must not be trimmed, merged, rewritten, summarized, or moved merely to construct a later turn. Once a stable prefix exists, new per-turn state is appended at the tail.
2. Dynamic controls belong in one self-contained final tail. Never insert per-turn controls, request/session IDs, route state, or other changing fields into system messages or the middle of history.
3. Do not feed frontend bubbles, raw timeline events, dispatch text, routing deltas, UI state, or usage diagnostics into LLM prompt bodies. Model input remains stable system/runtime/tool prefix plus canonical chat history or serialized `memory.steps` only.
4. Preserve historical tool traces from `SmartToolCallingAgent.write_memory_to_messages()`: structured assistant tool calls followed by their matching observations. Existing `legacy_text` contexts keep their historical `user`-role observations, including `read_file` text, unchanged. New contexts explicitly pinned to `qwen35-native` use canonical `tool`-role results with `name` and `tool_call_id`; the repository Qwen template renders these as `<tool_response>` inside its native user envelope. Canonical API roles and rendered template roles are distinct. A profile switch, Stop, or ordinary compact must not upgrade a context's pinned dialect or retag retained legacy history. Never strip `tool_calls`, trim observation text, rewrite observations as summaries, or emit synthetic empty tool calls just to chase cache hits.
5. Stop changes request lifecycle only; it must not rewrite canonical history. Backend-retained partial model output may be appended as an interrupted memory step after the old worker finishes.
7. Equivalent logical requests sent through SGLang and vLLM must use equivalent messages and tool-schema shape. Provider adapters may change transport fields, not prompt history. A retry/fallback for the same logical request must preserve the same prefix and schema unless an explicitly documented provider protocol makes that impossible.
8. Legitimate prefix boundaries are limited to first request initialization, successful compact memory replacement, an intentional agent-profile/tool-schema switch, a required multimodal representation change, an enabled reliable compact fallback, a different provider tokenizer/chat template, the narrowly scoped Vision Automation exception in item 9, the separately scoped Computer Use screenshot exception in item 10, and the separately scoped opt-in historical tool-text exception in item 11. After any boundary, append-only stability resumes. Report any other unavoidable byte difference to the user.
9. Vision Automation has one narrow historical-image exception. While a browser-local vision automation sampling task is armed, and for exactly one cleanup send after Abort, each newly sent turn may deterministically replace every image block in earlier turns with one aggregate textual placeholder per image block; only the current turn keeps real images. An image block means one continuous sequence/container of image content parts in a canonical user message (or one Agent TaskStep `task_images` container), not each individual image. The omission is staged and becomes canonical only when the same logical user turn is successfully accepted; pre-accept validation/transport failure must leave history unchanged. This is an intentional prefix boundary required to bound multimodal context and memory. It does not authorize changing historical roles, non-image text, ordering, assistant outputs, tool calls, tool observations, or any other prompt content. Chat and Agent paths must apply equivalent replacement semantics. Agent replacement is scoped only to the bound top-level Agent session and must never scan or modify child, sibling, or other manager sessions. Existing manual compact, auto-compact, cache-first, fallback, validation, and post-compact rules remain enabled and otherwise unchanged. After the one post-Abort cleanup send, normal Vision history policy resumes; already omitted images are never restored. Every such boundary must be reported as `vision_automation_historical_image_omission`. No other feature may reuse this exception.
10. Computer Use has a separate, top-level-Agent-only historical screenshot exception. It applies only to image blocks whose provenance is exactly `computer_use_observation`, created by the allowlisted `vision_desktop` executable (`observe`, `inspect_region`, `reobserve`, or a detail observation) and attached to the same Agent `ActionStep`. After a newer computer-use observation is successfully accepted, the next same-context tool turn may stage a projection replacing every earlier logical computer-use image block with one stable aggregate textual placeholder per block, while retaining the newest computer-use image block. Preserve historical roles, non-image text, ordering, assistant outputs, structured tool calls, tool observations, and all images with other provenance. The executable ActionStep is first written with the real image; the projection becomes canonical only after the next ordinary Agent inference request crosses its acceptance/transport barrier. Pre-accept validation, transport failure, empty-response fallback, Stop, or compact leaves the pending projection and canonical history unchanged. A single user message may contain multiple such tool turns and is not itself the commit boundary. A successful `reobserve` creates a new latest image and may stage omission; a failed or pre-accepted `reobserve` does not consume the permission. It must never scan or modify child, sibling, manager, Chat, user-upload, or ordinary Vision Automation images. Unrelated contexts do not consume this permission. Already omitted images are never restored; normal append-only stability resumes after the commit. Emit `computer_use_historical_image_omission` for every committed boundary. This exception is the only permitted computer-use violation of the general historical-image rule; no generic screenshot or tool may reuse it.
11. Historical tool-feedback text has one explicit opt-in exception, authorized by the user for `agent.tool_feedback_omission`. Default `enabled: false` preserves the existing prefix. When enabled, only after the next ordinary top-level Agent user request is successfully admitted may already completed historical tool text whose Unicode character count is strictly greater than `threshold_chars` be projected to a fixed omission placeholder. Current-task feedback stays complete until another ordinary user admission. Legacy observations remain indivisible combined blocks; native results are evaluated per call, retaining canonical metadata and all non-text content. Typed errors/parser feedback and unsuccessful result statuses are retained; a legacy block containing any unsuccessful result is retained whole. A native shared dispatch blackboard carrying an unsuccessful sibling result is also retained whole even when its owning dispatch completed. Error-like ordinary strings reported by a tool as completed remain eligible; do not infer status by matching text. Preserve calls/arguments/IDs, roles/order, user tasks, assistant output, every image/provenance, and original UI/timeline feedback. Never scan children/siblings or plain Chat. Stop, compact, profile switching, and serialization alone do not trigger omission. Pre-accept rejection leaves history unchanged. Committed projections are idempotent and must also govern compact sources; obsolete cached compact snapshots are invalidated so full feedback cannot reappear. Emit `tool_feedback_historical_text_omission` for each actual boundary. This exception authorizes no other historical rewriting; append-only stability resumes after the boundary.

12. Vision Automation terminology is strict: `session` continues to mean an LLM/Agent context window or a manager-created child-agent context. The browser-local lifecycle from Trigger Start to Abort is a `vision automation sampling task`, and each T=0-to-send interval is a `capture cycle`; neither is an automation session. Sampling tasks and all asynchronous capture/restart callbacks must remain pinned to their starting page generation, Chat/Agent mode, and top-level context identity so images and lifecycle events cannot cross session IDs. The single post-Abort cleanup permission is a bound token inherited from that same page generation, mode, and top-level identity; only the first successfully accepted ordinary user turn that actually commits the omission consumes it. Compact, Stop, pre-accept failure, and another context's request do not consume it.
13. The following compact-specific rules are additional requirements:
13.1. Keep the fast path as `compact.prompt_shape=cache_first_tail_with_fallback` plus `compact.prompt_control_enabled=true`. Normal chat/agent prompts must not receive a compact-control system clause; compact cache-first preserves the normal prefix and adds one self-contained internal server/system compaction instruction as the final user-channel message.
13.2. Do not trim or rewrite cache-first source before the first attempt. The cache-first source must preserve the same system/runtime context and serialized prior messages as the normal request prefix. `source_token_budget` is not a cache optimization.
13.3. Never use `source_token_budget` as a cache-first eligibility gate. Cache-first must always get the first attempt when `compact.prompt_shape=cache_first_tail_with_fallback`, `compact.prompt_control_enabled=true`, and `cache_first_body_messages` is available, regardless of untrimmed source size. Use `source_token_budget` only to cap the budgeted `reliable_position1` fallback after cache-first validation/exception failure.
13.4. Keep reliable fallback available only behind `compact.reliable_fallback_enabled=true`; the project default is `false` so cache-first failures surface directly instead of silently selecting `reliable_position1`. When enabled, reliable fallback remains the correctness path: compact-specific system prompt + position-1 compact instruction + validation before memory replacement. Never expose failed cache-first deltas to the UI unless retraction semantics are explicitly designed.
13.5. Agent compact cache-first should prefer `session.last_compactable_llm_messages` captured from the last successful non-`coordinator_1` normal LLM call (`actual llm_messages + assistant output`) so auto-compact after new-turn coordinator reset can still reuse the latest long-text agent prefix. Clear this payload after successful memory replacement; never derive it from timeline/UI/context_pack.
13.6. Keep compact calls thinking-off via `compact.extra_body.chat_template_kwargs.enable_thinking=false` and summary-only output. Do not increase `compact.performance_ratio`, `max_summary_tokens`, or other latency knobs without user approval.
13.7. SGLang cache observability must be wired through config to launch: `config/*-infer.yaml -> scripts/1_start_sglang.sh -> sglang.launch_server`. For cache measurements, ensure `schedule_policy: lpm` and `enable_cache_report: true` are actually passed.
13.8. Stream usage chunks may update compact diagnostics/logs, but must not become chat text, reasoning text, compact summary text, or a new frontend protocol.
13.9. Cache-first failures often come from prefix drift, not source size: if auto-compact rebuilds source after coordinator reset, `coordinator_1` system prompt replaces the last long-text agent prompt and SGLang cannot reuse the large prefix. Also keep the tail internal compaction user-channel instruction self-contained with the full `<summary>` + 9-section format, no-tool/no-routing constraints, and multi-compact task-state rules; it cannot rely on a compact system prompt or a normal-prompt compact-control clause being present.

## git commands
强烈不建议使用 `git stash`. 
使用任何 git 命令前，要征得用户的许可；若使用了任何 git 命令，需要使用**加粗**文字向用户汇报。
本条原则的优先级 高过任何 skills. 

## multi-agents rules
针对正在看本文的你（AI）的要求：主 agent (normally that is you if you are not assigned to be a sub-agent explicitly) 负责信息收集、方案规划、任务分发，各 sub-agents 负责执行具体任务如 editing, coding. 
若多个 sub-agents 同时工作时，注意它们之间不要有业务冲突，否则会相互干涉。对于类似任务，尽量**复用**同一个 prior context of sub-agent, 原因是：一、节约 tokens (this is vital)；二、提高 context prefix 的命中率。
对于一个 coding plan，主 agent 不要调用超过6个以上的 sub-agents ，而是合理规划任务，并且让 同一个 sub-agent 串行做不同 且相似/相近 的任务，让 sub-agent 复用其 context；如果 sub-agent 触发其 context compact limit 的话，就让其自行做压缩，压缩后根据 summaries 自行继续任务；本条命令 不是“不允许 主 agent 只能调用1个 sub-agent”，而是 主 agent 需要根据 coding plan 来规划不同 sub-agents 的任务，内容跨度大的任务可以交给超过1个 sub-agent 来执行，但记住 整个 life cycle 中 主 agent 调用的 sub-agent 总计不应超过6个。
强制要求 主 agent 全程 主要使用英语跟各个 sub-agent 交流。

## Skills of Superpowers
欢迎使用 skills of Superpowers；但，如果你准备应用它们, 那么记住以下这条命令（overwrite those commands in skills ）：不要在 `/.superpowers` 里面记录 implementing plan 中的每个 coding step 小结（Detailed summaries, such as those prepared for each chapter, should not be included in any documents），那样太费 token 额度了。 不是说“不让写文档了”，对于大的框架变更 以及 较为复杂的新功能增添，最好是 要在 coding 之前写 spec/PRD and implementing plans 的，以上命令只是在说“不要写对每个 plan step 的阶段性小结”。

## worktree/ worktrees/ branch
当 main 中没有 未提交的 代码文件（不包含各类文档）时，不要擅自创建 .worktree/ .worktrees/ .branch ，除非用户指定；
当 main 中有**大量**代码文件没有 commit 时，可以询问用户是否需要新建 .worktree/ .worktrees/ .branch ；
通常情况下，以 main 分支为主要工作分支。

## some working rules
When the user makes an obvious spelling mistake, ask them to confirm the intended
spelling before implementing or adopting it, especially in CLI/API names.

## 前端两种模式（重要！）

前端保留 **两个公开路由**，复用共享受控 UI、普通状态和应用控制逻辑。区分两条独立轴：**页面输入能力**（route + 后端配置确认的 Text/Vision capability）与 **传输模式**（Chat / Agent）；不能由 URL 推断当前模型一定具备视觉能力。普通逻辑在共享实现中修改，并验证两个路由，不再复制两份处理代码。

### 1. Text 模式 — `frontend/src/app/page.tsx`
- 路径: `/`
- 功能: 纯文本对话 + Agent 模式（可切换）
- 无图片上传能力
- Agent 模式下支持流式思考组件

### 2. Vision 模式 — `frontend/src/app/vision/page.tsx`
- 路径: `/vision`
- 功能: 视觉多模态对话 + Agent 模式（支持图片上传 / 摄像头拍照）
- 上传的图片在后端经 `max_image_dimension` 缩放后传给 VLM
- Agent 模式下同样支持流式思考组件
- **媒体启用条件**: 以 `/config` 返回值和 `hasVisionConversationCapability()` 为准，要求 `model.model_type === 'vision'` 且 `vision.enabled === true`；模型 YAML 由总配置选择，不能只盯固定 `config-model.yaml`。Text 路由仍按现有 `model_type` 语义重定向到 `/vision`，缓存 model type 仅是路由提示。
- 直接访问 `/vision` 但后端为 Text 时，保留普通文本发送、消息模板及无相机 Cycle；不得挂载上传、相机采集或自动采样输入。


### 共享前端的边界与生命周期契约

- Vision adapter = capability only; media/acceptance/terminal/generation/frozen glue stays in Vision route.
- Answer-now deprecated/frozen: no move/rename/refactor/delete/revival of state/handlers/API/tests without user scope change. Active Stop/render helpers callable unchanged; backend `final_answer` is separate.
- Cycle Text/no-photo: WHOLE-program reject for nonempty `photo` (ordinary/nested/scheduled) before POST/busy/bookmark/media/timers. Editor + start validate saved/default/programmatic/malformed input; `photo: []` allowed; no saved-program rewriting.
- `allowPhotos`: browser-only, never wire/fingerprint/bookmark. Page-owned Cycle operations: Text never adopts/clears/stops foreign/unknown legacy runs or projects photos.
- Clear cancels unsubmitted Cycle starts only; submitted producer/bookmark/subscriber/status/Stop survive. Vision-photo hardware wait rechecks generation/capability.
- live/recovery single owner: no Restore during busy/live/pending admission. Stale callbacks rejected; failure/Retry/Dismiss settlement BEFORE token retirement. Detach/mode ≠ Stop/delete (§39–40).
- Chat Stop: canonical `request_id`, else diagnose/retain attachment. Agent: session + optional `expected_request_id`; no abort-all. Capture/mark run + ALL tracked/execution IDs BEFORE detach.
- Current publisher `request_accepted`/`done` carry `request_id`; legacy/domain records may omit it (turn/content/timeline/memory/compact). Reject explicit foreign IDs only; no synthesized IDs/idless ID mutation/historical terminal clearing later requests.
- Keep Vision `disposeOnUnmount: false` + route delayed cleanup; no double dispose (§40–41/Covenant).
- Test Text/Vision × Chat/Agent mounted routes + real client/fake I/O: pending accept, live/replay, recovery failure/Retry, Stop/Clear/mode/pagehide/unmount. Real producer fixtures, not regex alone. See `C/conversation-routes.test.mts` / `L/session-hub-recovery.test.mts` (index aliases).

## Agent 系统架构

### 核心文件
| 文件 | 职责 |
|------|------|
| `backend/agent/agent_runner.py` | SmartToolCallingAgent、Session 管理、SSE 事件流 |
| `backend/agent/orchestrator.py` | ToolOrchestrator 多工具编排（DependencyAnalyzer + PlanExecutor） |
| `backend/agent/local_llm_model.py` | smolagents Model 适配器，桥接 LLMService |
| `backend/llm_service.py` | SGLang/vLLM 推理调用（同步+流式） |
| `backend/api/agent.py` | Agent API 端点 `/v1/agent/chat` |
| `backend/api/chat.py` | 普通聊天端点 `/v1/chat/completions` |
| `config/config-agent-prompts.yaml` | Agent 提示词模板（系统提示 + 指令） |

### 工具调用 与 agent hand-off 的 parser 准则
Local LLM 能通过输出 工具 schema 来 call tools or hand-off agents ， 有时它会输出错误的格式，此时你不应该让后端无限制 为其错误兜底（不能过度引入智能解析），而应该 通过 AgentParsingError Observation 回灌准确错误位置，feedback 相对应的错误给 local LLM， 让 local LLM 下一轮自行纠正。

仅coordinator可调用switch_agent。

### SmartToolCallingAgent 三层处理
1. **纯文本** → 自动包装为 `final_answer`（模型无需显式调用）
2. **final_answer 工具调用** → 静默拦截处理
3. **真实工具调用** → 正常执行工具并返回观察结果

### 多工具编排（ToolOrchestrator）

当 LLM 一次输出多个 tool_calls 时，`SmartToolCallingAgent.process_tool_calls()` 会调用 `ToolOrchestrator` 进行编排执行：

1. 一个 LLM turn 的全部 accepted tool calls 只组成一个 stage，并同时提交执行；backend 不再按工具类别推断依赖或创建串行 stage。
2. 同 turn 调用必须彼此独立；依赖前一个结果或可能发生写入/执行冲突的调用，必须由 LLM 等 observation 后放到 future turn。
3. manager 唯一 child dispatch 工具是 `dispatching_parallel_agent`，arguments 严格且仅含 `child_id` 与 `task`；一 call 只启动一个 child，多个 child 用同一 canonical array 中的多个 calls。
4. 同 turn 的所有 dispatch calls 共用一个 ordered shared blackboard；普通工具 observations 保持独立，`memory_step.tool_calls` 保留模型输出的全部结构化 calls。
5. `manager_agent_max_parallel_children` 只限制 dispatch calls：按模型输出顺序保留前 N 个，超限 calls 标为 skipped 并进入 blackboard；普通工具不受此限制。
6. 同 turn duplicate `child_id` 必须在任何 child 启动前拒绝；不同 `child_id` 不得被 manager-wide execution lock 串行化。
7. child 的文件范围、只读/写入约束全部写入 self-contained `task`，dispatch contract 不含 `subtask(s)`、`may_write` 或 `reserved_files`。
8. 编排结果继续通过 `orchestration_plan`/`orchestration_update`/`orchestration_done` SSE 事件推送前端。仅普通单工具可走原有父类路径；单个 dispatch call 仍必须走 preflight/blackboard 路径。
9. Dispatch 回归必须覆盖 canonical transcript parser -> `SmartToolCallingAgent.process_tool_calls()` 完整路径；只 mock 私有 child coroutine 不能证明生产路径有效。用户旧 plural transcript 已证实在 parser 层有效，因此无运行日志时不得声称唯一历史根因。

核心文件: `backend/agent/orchestrator.py`

### VLM + Agent 注意事项
- 当 `model_type: vision` 时，agent 系统需要在指令中明确告知模型"你是 VLM，能直接看到图片"
- VLM 指令在 `_build_agent()` 中根据 `config.model.model_type` 动态追加

## 前端组件
| 组件 | 路径 | 功能 |
|------|------|------|
| 共享 conversation UI | `frontend/src/components/conversation/` | Header / SettingsDialog / MessageList / Composer / 无媒体输入操作；受控 props，不拥有请求生命周期 |
| 共享应用逻辑 | `frontend/src/lib/conversation/` | 状态、bootstrap、send/transport/actions、recovery、voice/commands；按上方任务索引定位 |
| AgentStepDisplay | `frontend/src/components/AgentStepDisplay.tsx` | 思考/工具/观察卡片渲染 |
| LLMClient | `frontend/src/lib/api.ts` | SSE 流式请求封装 |

## 配置文件
| 文件 | 内容 |
|------|------|
| `config/config-overall` | 总配置，可灵活调用以下几个 yaml 文件 |
| `config/config-model.yaml` 或由用户灵活调用的 `config/qwen3-vl-config-model.yaml` | 模型路径、model_type、图片参数 |
| `config/config-infer.yaml` 或由用户灵活调用的 `config/qwen3-vl-config-infer.yaml` | 推理参数、SGLang 启动参数、tool_call_parser |
| `config/config-agent-prompts.yaml` | Agent 提示词模板 |
| `config/config-server.yaml` | 后端服务器配置 |

### 参数配置要求
1. 对于较为重要的参数或者需要灵活调试的参数：尽量不要硬编码，Do not hardcode.
2. 参数的真正生效链路主要在 config/*-infer.yaml -> scripts/1_start_sglang.sh -> sglang.launch_server 。backend/config.py 主要负责配置建模与对外暴露，不直接启动 SGLang。
3. **Web 设置优先级规则（2026-08-24）**：web 设置面板展示的参数（如 Temperature、top_p、top_k、repetition_penalty、max_tokens 等），只要浏览器缓存（localStorage key `llm-chat-settings`）中存在该键，就**优先于后端 yaml 默认值**；yaml 仅为缓存中缺失的键兜底。键存在性以解析后的缓存对象是否含该键为准（值可以是 false/0/空串，不能做真值判断）。实现锚点：`frontend/src/lib/api.ts` 的 `parseCachedChatSettings()` + `mergeServerDefaultsWithCache()`；text/vision 两页在 `/config` 到达时都必须走该合并路径，禁止 `{ ...prev, ...serverDefaults }` 式的"yaml 覆盖用户值"回归。此规则只适用于 web 页面展示给用户的参数，不适用于其他链路（如 infer yaml -> 启动脚本链路）。

## 常见陷阱
1. **两个路由遗漏验证**: text/vision 共享普通 Agent 逻辑；优先修改共享实现，并验证两个 route 的 capability 与 Chat/Agent 装配，不能通过再复制一份逻辑解决差异。

3. **Qwen3-VL 在 SGLang 的 auto + stream 模式下可能返回空增量**: 已实际复现“非流式有 `<tool_call>` 文本，但流式只有空首帧和 stop，没有 content / reasoning_content / tool_calls”的情况。此时前端会看到空思考框，后端会把空字符串收敛成 final answer。修复点在 `backend/agent/local_llm_model.py`，需要对“流式空响应”回退到一次非流式解析。

4. **公开路由与共享实现要分清**: [Text route](frontend/src/app/page.tsx) 与 [Vision route](frontend/src/app/vision/page.tsx) 复用共享 UI/controller，但输入 capability、Vision 媒体/acceptance glue 与公开路径仍有区别。排障先区分能力、Chat/Agent transport、共享实现还是 route 装配问题。

5. **Agent 链路与普通聊天链路不同**: Agent 走 `/v1/agent/chat`，普通聊天走 `/v1/chat/completions`。vision/text 页面虽然都能进 agent 模式，但问题要先区分是“前端页面差异”还是“后端 agent/tool calling 差异”。

6. **smolagents `@tool` 装饰器 JSON Schema 生成失败**: 使用自定义 wrapper 包装工具时，`@tool` decorator 在装饰时（decoration time）就需要完整的 docstring。错误信息：`Cannot generate JSON schema for wrapper because the docstring has no description for the argument 'args'`。**关键点**：
   - docstring 必须在 `@tool` 装饰器**内部**（作为 wrapper 函数的文档字符串）
   - 不能用 `wrapper.__doc__ = ...` 在装饰**之后**赋值
   - Args 中的参数名要用 `args` 和 `kwargs`（无 `*` 前缀），不是 `*args` 和 `**kwargs`
   - 参考 `backend/agent/agent_runner.py` 中的 `_make_session_aware_wrapper` 和 `_create_tool_wrapper`

7. **Agent 工具 session_id 为 None**: `run_stream()` 的 `session_id` 参数可能为 `None`（前端首次请求），但 `get_or_create_session()` 会内部生成 UUID。如果不在调用后立即 `session_id = session.session_id` 回写，`_run_agent()` 闭包捕获的仍是 `None`，导致所有依赖 session_id 的工具（如 `load_images`）失败。同理 `run_sync()` 也需要回写。

8. **`write_memory_to_messages()` 中禁止 strip `tool_calls`**: `SmartToolCallingAgent` 覆写的 `write_memory_to_messages()` 只应 strip `observations_images`（任务图片副本去重），**绝不能** `stripped.tool_calls = None`。原因：smolagents 的 `ActionStep.to_messages()` 依赖 `tool_calls` 生成 `TOOL_CALL` 消息（→ASSISTANT 角色）。一旦缺失，`TOOL_RESPONSE`（→USER 角色）会变成孤立的观察消息，在多轮对话（`share_context=True`）中破坏 action→observation 配对，导致模型"发疯"（幻觉或无限重复工具调用）。详见`/lesson-learnt/踩坑记录.md` #39。

9. **smolagents 消息角色隐式转换**: `TOOL_CALL → ASSISTANT`、`TOOL_RESPONSE → USER`（定义在 `models.py:282-285` 的 `tool_role_conversions`）。`get_clean_message_list()` 会合并连续相同角色的消息。如果 `TOOL_CALL`（→ASSISTANT）缺失，相邻的 USER 消息会被合并，进一步破坏消息结构。修改 `write_memory_to_messages()` 时必须考虑这个合并行为。

   `qwen35-native` 新 context 使用仓库 canonical adapter 保留 assistant calls 与真正的 tool-role results，绕开该 legacy cleaner 的合并及 metadata 丢失；不修改依赖源码，也不把历史 legacy observations 改 role。Vision/CU 图片容器、provenance 和 acceptance 边界继续遵守全局 covenant。

10. final_answer 适配更改

本项目魔改了 smolagents 的 final_answer 工具原则：LLM 最终回复**不**使用 smolagents 的 final_answer 工具，而是返回纯文本，由代码自动包装为 final_answer。这导致了多处代码适配，以下是所有相关更改项：

10.1. 提示词 + API schema 双重过滤（agent_runner.py + local_llm_model.py）
- **不存在 `get_tools()` 方法**；实际过滤分两层（见陷阱 #12 的详细说明）：
  1. `SmartToolCallingAgent._HIDDEN_TOOL_NAMES`（`agent_runner.py:740`）——同时覆写 `initialize_system_prompt()`（`agent_runner.py:758`）和 `tools_and_managed_agents` property（`agent_runner.py:749`），确保系统提示文本和 API tool schema 都不含隐藏工具。
  2. `local_llm_model.py` 的 `__call__`（`local_llm_model.py:223-230`）和 `generate_stream`（`local_llm_model.py:315-321`）中保留同名集合作为防御性兜底。
- 目的：让模型直接返回纯文本，而非调用 final_answer 工具。

10.2. 纯文本自动包装（backend/agent/agent_runner.py）
- 在 `SmartToolCallingAgent.run_stream()` 和 `run_sync()` 中，如果响应是纯文本，自动视为 final_answer。
- 代码位置：`backend/agent/agent_runner.py:1106-1113`。
- 处理逻辑：纯文本响应 → `got_final_answer = True`，`final_answer = plain_text`。

10.3. final_answer 工具调用拦截（backend/agent/agent_runner.py）
- 静默拦截 final_answer 工具调用，不显示 "Calling tool: 'final_answer'" 消息。
- 代码位置：`backend/agent/agent_runner.py:1036-1050` 和 `backend/agent/agent_runner.py:1095-1103`。
- 处理逻辑：拦截 final_answer 调用，提取内容，静默完成。

10.4. 工具调用解析过滤（backend/agent/local_llm_model.py 和 agent_runner.py）
- 在解析工具调用时，过滤掉 final_answer，避免将其作为真实工具调用处理。
- 代码位置：
  - `backend/agent/local_llm_model.py:526-530` 和 `backend/agent/local_llm_model.py:536-540`（流式和非流式）。
  - `backend/agent/agent_runner.py:1009-1024`（文本解析）。
- 目的：仅剩 final_answer 时，返回 None 让上层视为纯文本响应。

10.5. 流式处理适配（backend/agent/agent_runner.py 和 local_llm_model.py）
- 在流式模式下，确保 final_answer 正确处理，避免空增量导致的纯文本 final_answer。
- 代码位置：`backend/agent/agent_runner.py:960-1024`（基于 content 部分解析）。
- 踩坑：Qwen3-VL 在 auto+stream 模式下可能返回空增量，需回退到非流式解析。

10.6. 避免重复文本渲染（frontend/src/CHANGE.md 和 backend/CHANGE.md）
- 防止 `FinalAnswerStep` 和 `ActionOutput(is_final_answer=True)` 同时触发，导致前端收到重复内容。
- 代码位置：`backend/agent/agent_runner.py:1627-1662`（跳过重复的 ActionOutput）。
- 解决：仅发送一次 final_answer 内容。

10.7. 提示词模板调整（config/config-agent-prompts.yaml）
- 在系统提示中提到 final_answer，但实际使用中被过滤。
- 代码位置：`config/config-agent-prompts.yaml:287` 和 `config/config-agent-prompts.yaml:537`。
- 注意：提示词中仍提及 final_answer，但模型不会看到工具定义。

10.8. 测试用例添加（tests/test_write_memory_tool_calls.py）
- 添加测试用例验证 final_answer 处理。
- 代码位置：`tests/test_write_memory_tool_calls.py:57-66`。
- 目的：确保纯文本自动包装为 final_answer。

11. **多工具编排 process_tool_calls 覆写**: `SmartToolCallingAgent.process_tool_calls()` 覆写了 smolagents 的父类方法。当 smolagents 升级时，需检查父类 `process_tool_calls` 的签名和行为是否有变化。编排事件通过 `yield {"_orchestration_event": True, ...}` 传递，在 `_run_agent()` 的事件循环中被识别并转发为 SSE 事件。

12. **向 LLM 隐藏工具必须同时切断两条路径**: LLM 通过两条独立路径"看到"工具，单独过滤任意一条**都不够**：

    **路径 1 — API schema（tool JSON）**：`model.__call__(tools_to_call_from=...)` 里的工具列表被序列化为 JSON schema 发给 LLM API。这条路径决定 LLM 的 tool_calls 输出中能填哪些工具名。

    **路径 2 — 系统提示（Jinja2 模板）**：`ToolCallingAgent.initialize_system_prompt()` 用 `self.tools`（完整字典）渲染模板中的 `{% for tool in tools.values() %} - {{ tool.to_tool_calling_prompt() }}` 块。工具名通过 `to_tool_calling_prompt()` 硬编码进系统提示文本——**仅靠修改 docstring 无法隐藏工具名**，因为该方法直接使用 `self.name`（即函数名）。

    `to_tool_calling_prompt()` 格式如下：
    ```python
    def to_tool_calling_prompt(self) -> str:
        return f"{self.name}: {self.description}\n    Takes inputs: {self.inputs}\n    Returns an output of type: {self.output_type}"
    ```

    **正确的完整隐藏方案**（见 `backend/agent/agent_runner.py`）：
    1. `_HIDDEN_TOOL_NAMES`（`agent_runner.py:740`）—— 声明隐藏工具名称集合
    2. 覆写 `initialize_system_prompt()`（`agent_runner.py:758`）—— 渲染系统提示前过滤 `tools` 字典，切断路径 2
    3. 覆写 `tools_and_managed_agents` property（`agent_runner.py:749`）—— 传给 `model.__call__` 时也过滤，切断路径 1
    4. `local_llm_model.py` 的 `__call__` / `generate_stream` 中保留同名过滤集合作为防御性兜底

    **根因教训**：本项目曾出现"只过滤了 API schema 却忘了系统提示"的情况，导致 LLM 仍在文本中看到 `final_answer` 工具定义，从而继续调用它。两条路径必须同时封堵。

13. **`coordinator_2` 的“全量可见”不等于“原样回灌上下文”**:
   - 对用户：允许展示 `coordinator_2` 的全量流式推理。
   - 对模型：不能简单把同样的长 transcript 原样拼回后续 agent 的 message history 主体。
   - 正确方向是：coordinator_2 的 transcript 仍以 `ActionStep.model_output` 追加到尾部（动态尾部），**不抽取成 `working_memory` / `unresolved_items` 写回 system 段或前缀中部**。
   - 任何把 `coordinator_2` 的 transcript 转写为系统提示或前缀中部字段的做法都是 v2 禁止的回归。

15. **future agents 必须先接入 timeline，再做 projection**:
   - 不允许每个新 agent 都发明新的前端协议。
   - 新 agent 必须先落统一 timeline event，再由 projection reducer 决定它渲染到聊天气泡、dispatch card、还是 agent steps。

16. **有关 multi-agent system 的注意事项**:
   - 不要假设“前端当前显示什么，就应该原样写回模型上下文”。
   - `coordinator_2` 聊天气泡的 `message_id` 必须按 **run/turn 唯一**，不能按 `session_id` 固定；否则同一 session 连续两轮都命中 `coordinator_2` 时，第二轮会继续写进上一轮气泡。
   - 不要把 dispatch card 只做成一次性动画；它必须和真实 `routing_delta` 绑定。
   - 前端当前仍是 **projection-first + delta-driven**：`timeline-projection.ts` 只显式投影 `routing_delta -> dispatch_update` 与 `coordinator_2 message_delta -> chat_chunk`；`message_started/message_completed` 不单独投影，bubble 生命周期还依赖页面侧 run bookkeeping 和 finalize helper。
   - 不要为 future agents 发明独立 SSE 协议；统一走 timeline event + projection。
   - `_activate_profile_agent()` 在 v2 起**不再**调用 `_compile_context_pack()`，也**不再**向 `next_agent.state` 注入 `context_pack`。`session.behavior_records` 中的 `route_receipt` / `handoff_ledger` 仍保留为 Tier 2 审计；`working_memory` / `unresolved_items` / `tool_ledger` 已被 v2 删除，禁止复活。
   - `_compile_context_pack()`（若仍存在）仅作为 Tier 2 审计快照工具，返回结构只含 `routing_receipt`；**禁止**把它的返回结果作为 LLM 输入。
   - `routing_timeout_fallback` timeline event 现在包含 `from_agent`；`_compile_context_pack()` 会优先读 `from_agent` 再回退到 `source`。改 timeout/fallback 相关逻辑时不要漏掉这个字段。
    - CLI 可读性与 Web UI SSE 已解耦：`_should_emit_cli_stream_chunk()` 只允许影响未来专用 CLI runner 的终端刷新，不得拿它去节流 Web SSE / timeline_event；backend SSE 路径不得创建 `rich.Live`。
    - 任何重要参数（例如 `coordinator_1_timeout_seconds`）都必须配置化，不要硬编码。

18. **Slash command framework** (introduced 2026-06-06):
   - Single source of truth: `frontend/src/lib/slash-commands.ts`
   - Both `page.tsx` and `vision/page.tsx` must maintain glue code in sync (per trap #1). The shared `SlashCommandMenu.tsx` and `slash-commands.ts` are the only shared pieces; each page owns its own useState and the small onChange/onKeyDown/handleSend modifications.
   - Trigger rule: popup is visible only when input starts with `/`. Other positions of `/` do not trigger. After the first space, the popup closes.
   - Each command is a `SlashCommand` object: `{ name, description, category?, disabledWhen?, run }`. Future commands (e.g. `/clear`, `/export`) are added as one-line registry entries.
   - Commands may consume the input (`kind='consumed'`) or fall through and send the resulting text as a normal user message.

21. **Frontend local state ≠ wire format. 必须显式翻译** (introduced 2026-06-06, after the post-/compact context-not-shrinking bug):
    - /compact 的 `isPreCompact: true` 和 `bubbleKind: 'compact_summary'` 是**前端 React state**，不进入 wire format。前端 `truncateMessagesForContext` (或任何把内部 `Message[]` 转成 API `ChatMessage[]` 的代码) 必须**显式**把这些字段翻译成 backend 能理解的 `metadata.pre_compact` / `metadata.compact_summary + summary_id`，否则 backend `filter_messages_for_llm` 是 no-op，LLM 永远看到完整未过滤历史。
    - **症状是"compact 时 token 数下降，compact 后下一条 chat token 数回升"**。SGLang 终端日志会显示 ~3k（compact 时）→ ~40k（下一条 chat 时）。这不是 SGLang 的 bug，是 backend 没收到 metadata。
    - **Defense-in-depth: backend filter 也必须成为默认值**。`backend/api/chat.py::_generate_stream_response` 的 `final_messages` 不能默认等于未过滤的 `messages`，必须默认等于 `filter_messages_for_llm(...)` 的结果。Auto-compact 分支是这个默认值的"覆盖者"，不是唯一使用者。
    - **"no metadata" 不是错误**。外部 OpenAI 客户端不发送 `metadata` 是合法的，filter 应该是 graceful no-op 而不是崩。设计 filter 默认值时，必须区分"filter 实际改变了列表"和"filter 是 passthrough"两种情况 —— 改了就用 filtered，没改就保持原样。详见 `tests/test_compact_chat_streaming_filter.py::test_chat_completions_streams_no_op_when_no_metadata`。
    - **永远不要把 spread 当成"自动翻译所有相关字段"**。`{role, content}` 的 spread 不会自动带上 `isPreCompact` / `bubbleKind` / `metadata`，这些字段名跟 wire format 不一样。**所有跨层字段翻译必须有显式函数**（如 `buildCompactMetadata`），不要隐式靠对象 spread。

22. **/compact 必须按 `agentMode` 选 `mode` (2026-06-06, agent 模式 /compact 路由丢失)**:
    硬编码 `mode='chat'` → chat 编排器不碰 `session.agent.memory.steps` → 下轮 `/v1/agent/chat` 仍 50k。前端统一走 `buildCompactRequestBody({ agentMode })`，共享 commands hook 调用它，并验证两条 route 的 wire body。**前端不应该知道后端有几个 mode，只声明"我是 agent 还是 chat"**。

23. **post-compact `has_run=True`（否则 smolagents reset 抹 envelope，详见 踩坑记录3.md §8）**:
    `_replace_agent_memory` 后 `has_run` 必须 `True`（envelope = 刚跑完的 compact run）。`False` 让下轮 `agent.run(reset=True)` → smolagents `memory.reset()` 抹 envelope → LLM 回"全新开始"。重路由靠 `current_agent_name`+`active_agent`，**别复用 `has_run`**。

24. **lifespan wiring 顺序错位 = silent failure 温床（详见 踩坑记录3.md §9）**:
    `if A and B is not None: wire(A, B)` 在 `app.state` 互相依赖时静默跳过 → `agent_runner.compact_orchestrator` 永远是 None → auto-compact 永远不触发但 manual /compact 仍 work（独立路径）。**先就绪被赋值方再 wire**；manual 路径 work 会蒙混 unit test，必须有 lifespan 集成测试反向锁定 wiring 真的发生了。

25. **agent-mode auto-compact 的 compact_summary 必须插在 streaming 助手之前**（2026-06-08）: auto-compact 触发时 `compact_started` 是**第一个** timeline_event（在 routing/assistant content 之前）。如果 `applyCompactProjection` 把 `compact_summary` 追加到消息数组末尾，UI 顺序会变成"助手回答 → compact card"，违反直觉。**修复**：`applyCompactProjection` 接受 `options.insertBeforeMessageId`（agent 模式传 `agentRun.assistantMessageId`），插在 anchor 之前。manual `/compact` 路径不传 hint，回退到末尾追加。详见 `踩坑记录4.md §5.4`。

30. **Backend SSE 路径不得创建 `rich.Live`；`_step_stream` 也不能 fallback 建本地 Live**（2026-06-12/13, Bug 1/1.1/1.2 fix, 终端重复 markdown 叠加）: `_step_stream` 每次被 smolagents 主循环调用（一次 agent turn 可能 50+ steps）若创建新的 `with Live(...) as live:`，Rich 会在每个 `with` 块退出时冻结当前 markdown；第一版修复把 Live promote 到 `_run_basic_stream` run 级别，但用户实测 backend logger 与 Live refresh 交错后，run-level Live 退出仍会再冻结一次最终 render，backend terminal 仍出现“流式内容 + 日志 + Final answer/Step + 完整内容”重复。**最终修复**：(1) `SmartToolCallingAgent.__init__` 保留 `self._cli_live = None` 作为未来专用 CLI runner 的 parent Live hook；(2) `_run_basic_stream`（backend SSE/web path）**不创建 `rich.Live`**，只负责把 deltas 推给 Web SSE；(3) `_step_stream` 只在 `self._cli_live` 已由外部 parent 设置时复用它，**无 shared Live 时不创建本地 Live，只 yield stream deltas**；(4) `coordinator_1` routing 同样不创建 Live。**Hard contracts**：`tests/test_agent_session_context.py` 锁 4 件事：(a) `test_run_basic_stream_does_not_create_backend_cli_live` —— backend SSE basic_agent 路径不构造 `Live` 且 `agent._cli_live` 保持 None；(b) `test_run_basic_stream_skips_live_for_coordinator_routing` —— routing 路径不构造 `Live`；(c) `test_run_basic_stream_releases_live_on_agent_exception` —— 异常路径也不构造 `Live`；(d) `test_step_stream_without_parent_live_does_not_create_per_step_live` —— 直接调用 `_step_stream` 且无 parent `_cli_live` 时 Live 构造计数 = 0；另有 `test_run_basic_stream_streams_sse_without_backend_live` 锁“不创建 backend Live 不影响 SSE content”。**回归风险**：未来 refactor 若在 `_run_basic_stream` 重新创建 Live、把 Live 创建挪回 `_step_stream`、或保留 local fallback，backend terminal 会再次重复显示模型输出。Web UI 流式与 backend terminal rendering 必须保持解耦。

32. 对 coordinator_2, **一个 `secondary_handoff` 只能开一个 execution bubble：dispatch step 开，`agent_switched` 只消费/兜底**（2026-06-13, Bug 3.1 fix, second handoff 修复过度导致双 bubble + 第一个永远“思考中...”）: Bug 3 的真实根因仍然是第二次 handoff 没换 `executionAssistantMessageId`，basic_agent 的 `agent_step: thought` / `content`（无 `message_id`）落进旧 execution buffer。但 2026-06-12 的第一版修复把 `onStep` (dispatch step) 和 `onTimelineEvent` (`agent_switched`) 两条路径都改成“每次都开新 bubble”，于是同一个 handoff 按 backend 顺序 `dispatch_step` → `agent_switched` 会开两个 bubble：第一个由 dispatch 创建，只显示 dispatch/“思考中...”；第二个由 `agent_switched` 创建并接收 basic_agent 文本。**正确修复**：(1) `onStep` 收到 `step_type='dispatch' && dispatch_kind='secondary_handoff'` 时开新 execution bubble，设置 `agentRun.secondaryHandoffExecutionBubblePendingAgentSwitch = true`；(2) 随后的 `agent_switched` from `coordinator_2` 若看到该 flag，只把它置回 false 并做幂等 tail retraction，**不再 generateId / 不再 append bubble**；(3) `agent_switched` 仅在没有 pending dispatch-created bubble 时作为 fallback 开 bubble；(4) 在共享 `text-agent-session-handlers.ts` 保持该语义，并验证 text/vision 两条 route。**Hard contracts**：`frontend/src/lib/timeline-projection.test.mts` 锁 `secondaryHandoffExecutionBubblePendingAgentSwitch` 初始化、dispatch path 设置 true、agent_switched path 消费 false、且 `newExecutionId = generateId()` 只能出现在 fallback branch。**回归风险**：重新加回 `!agentRun.executionAssistantMessageId` 会复现“第二次 handoff 不开 bubble”；让 dispatch 和 agent_switched 都无条件开 bubble 会复现“双 bubble + 第一个一直思考中”。详见 `踩坑记录4.md §5.11`。

33. **Stop 后不能提前释放 run scope，`search_and_fetch` timeout/abort 后必须等待 running fetch worker 收尾**（2026-06-13, stop 后 `free(): invalid pointer` / segfault）: 前端 stop 可能先关闭 SSE stream，再调用 `/v1/agent/stop`；若 `_run_basic_stream` 在 async generator 的 `finally` 中调用 `session.finish_run(run_scope_id)`，旧 `_run_agent` worker 仍在工具调用中时 session 已变成可复用，`run_abort_manager` scope 也被 cleanup，新一轮可能与旧 `curl_cffi`/libcurl worker 重叠。`backend/tools/search_and_fetch.py` 旧代码还用 `executor.shutdown(wait=False, cancel_futures=True)`，tool return 后 running fetch worker 继续跑，下一轮搜索容易触发 native crash。**修复**：(1) `_run_basic_stream` 的 run scope 由 worker thread 在 `_run_agent` `finally` 中释放；SSE generator 关闭不得 `finish_run`；(2) `search_and_fetch` 使用 `ThreadPoolExecutor.shutdown(wait=True, cancel_futures=True)`，timeout/abort 后等 running fetch worker 收尾再返回。**Hard contracts**：`tests/test_agent_session_context.py::test_run_basic_stream_generator_close_keeps_run_busy_until_worker_exits` 锁 generator close 后 worker 未退出时 `session.run_in_progress=True`；`tests/test_search_and_fetch.py::test_executor_shutdown_waits_for_fetch_workers_after_timeout_or_abort` 锁 timeout/abort 后 shutdown 等 running worker 完成。**回归风险**：把 `finish_run` 挪回 SSE generator `finally`、或把 search executor shutdown 改回 `wait=False`，都可能让 stop 后下一轮工具调用与旧 native worker 重叠并重新触发 `free(): invalid pointer`。

34. **Compact cache-first/speed path**（2026-06-14, updated 2026-06-16）: See the global `Prefix-Cache Covenant` and its compact-specific rules above. The short version: cache-first preserves the normal prefix and is attempted before reliable fallback whenever cache-first inputs are available; `source_token_budget` caps only the reliable fallback source, not cache-first eligibility; failed cache-first output is buffered until validation passes, historical tool traces must stay structured, compact thinking stays disabled, and latency knobs such as `compact.performance_ratio` must not be changed without user approval. 详见 `踩坑记录4.md §5.7.6`（其中关于 source budget 准入门槛的旧表述已过期，以本文件 covenant 为准）。

35. **Cache-first tail-only compact-control**：当前 cache-first tail 方案不再让 normal prompt 预埋 compact-control system clause。Compact 通过最终 user-channel message 中的自包含内部 server/system 指令触发；该指令必须明确“不是用户真实请求”、禁止正常聊天/工具调用/路由，并要求 9-section `<summary>` 与多轮 compact 任务状态更新。旧摘要中残留的 `LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL` 只作为 stale cleanup 目标处理，不再作为正常 prompt 触发机制。

37. **Compact 后首轮不得再做 slim prefix 切换**（2026-07-13）: successful compact memory replacement 本身是合法 prefix boundary；完成后应立即建立完整的新稳定前缀。不要在 compact 后首轮临时删除 runtime context、system 段或 tool schemas，再在第二轮恢复，这会制造额外且可避免的 cache miss，并改变模型能力。`has_run=True` 与 summary envelope 仍须保留。

38. **Child abort 是 targeted lifecycle 操作，不是 GPU/KV cache 诊断结论**（2026-07-15）: `/v1/agent/child/abort` 只中止指定的当前 child，禁止 fallback 到 abort-all，也不得影响 sibling child 或 manager。必须先捕获该 child 的 active scopes/request ID，发出 targeted abort，再等待 scope 与原 dispatch worker 的 terminal barrier；barrier 完成并完成 terminal settlement 后，在稳定 child `AgentSession` shell 内 reset 旧 Agent/model context；保留 deterministic child ID、Session Hub/journal/replay audit、URL 和 `(manager_session_id, child_id)` mapping。普通 completed dispatch 复用既有 child context。`ChildAgentRun` 与 bounded replay audit 继续保留；裁剪必须保持 replay `seq` 非递减，并保留 latest `manager_assigned_task` anchor、truncation notice 与 terminal event。TTL cleanup 必须清除 orphan child run/buffer/mapping/subscriber metadata，但保留仍活跃 session 的记录。模型输出退化本身不得误称为 KV cache 或 GPU corruption，除非有独立硬件/缓存证据。

39. **Session Hub 恢复的是 canonical journal/projection 与 subscriber attachment，不是浏览器运行时**（2026-08-27）: 发起请求的 browser subscriber 断开后，独立 backend producer 会继续运行并向 Hub journal 追加记录；因此“live UI 停住，但刷新恢复后出现后续结果”首先要诊断 subscriber transport，而不能认定 manager/child worker 停止。Child 执行期间其细节只写 child Hub，manager Hub 可能长时间无 application event；所有 Hub SSE 统一通过 `server.session_hub_heartbeat_seconds` 发 transport-only comment heartbeat。Heartbeat 禁止写 journal、占用 `event_sequence`、进入 timeline/projection 或成为前端协议；`readSessionHubStream()` 必须在收到任意非空 raw chunk（包括 comment）时刷新 inactivity timer，但 comment 不得触发 payload handler。实现 heartbeat 时只允许保留一个 pending `anext(subscription)`，禁止反复用 `wait_for(anext(...), timeout)` 取消底层 subscription。Live Agent detach 必须 retain bookmark、释放 `attachedRecoverySessionId`，让恢复入口无需刷新即可出现；不得因 subscriber detach 中止 producer 或删除 session。

40. **Hub restore 不得复活 Vision Automation 的 browser-local 状态**（2026-08-27）: Session Hub bookmark 只保存恢复会话所需的固定字段，不保存/回放媒体流、图片缓存、`VisionAutomationController`、sampling task/capture cycle、acceptance mutation、terminal gate、cleanup secret 或 page-generation lease；这些状态也禁止进入模型消息、timeline 或 context。保留的 Agent context 会继续持有 backend `vision_automation_binding`/cleanup tombstone，因此新页面和每次 lifecycle invalidation 必须通过独立 localStorage high-water allocator 取得严格递增的 `page_generation`，不能在 refresh 后重置为 0；持久化 generation claim 必须在 React commit 后的 effect 中绑定，render 阶段禁止读写 localStorage，也不能用 `useRef(new VisionPageLifecycle(...))` 制造构造副作用。旧页面/旧 generation 的异步 callback 必须继续被拒绝，不能为消除 422 而放松 backend 的 mode/context/task/generation 校验。localStorage high-water 只覆盖同一浏览器中的顺序关闭/恢复，不是跨 tab 的原子租约；如果要求多个 tab 并发恢复同一 Agent context，必须使用 backend-issued lease。若异常关闭发生在 cleanup token 已注册但未消费/取消的窗口，不能把 token 放进 Hub；正确扩展方向是 backend-issued lease 或“更高 generation 在 admission commit 时原子 supersede、rollback 时完整恢复旧 cleanup state”，不是从 replay 猜 token。

41. **全自动语音恢复必须由 reactive visibility 驱动，UI mode 与 controller phase 必须可诊断**（2026-08-27）: LLM terminal 若发生在 hidden 页面，单次读取 `document.visibilityState` 的 effect 会错过回到 visible 的时刻并永久停在 `paused_for_response`；text/vision 两页必须通过共享 `visibilitychange` 状态重新评估 resume，Vision 仍需同时满足 model idle 与无 pending acceptance。页面 hidden 只触发恢复条件更新，禁止因此 stop MediaStream、deactivate Voice 或销毁后台采集。自动 `activateVoice()` 失败会让 controller 回到 keyboard phase，但 page-level `inputMode` 可能仍为 `voice`；`VoiceInputPanel` 的显示应由 `inputMode === 'voice'` 决定，以暴露 recoverable error/Retry，不能用 `voiceController.isVoiceMode` 隐藏错误并制造“语音仍选中但完全不工作”的假挂起。共享 `use-conversation-voice.ts` 持有此逻辑，两条 route 均须验证。

## 新工具与新 Skill 的安全接入须知

本项目的 Agent 工具已经默认处于 capability/enforce 模式。新增工具、Skill 或 wrapper 时，不能把“模型能看到参数”误认为“模型获得了权限”；所有副作用都必须经过后端已解析的策略和会话绑定。以下规则是接入门槛，不得由 prompt、task 文本、UI 状态或 Skill 内容绕过。

1. **先绑定 OperationCapability，再暴露工具**：
   - `AgentSession` 必须持有 immutable `OperationCapability`；配置只在会话准入时解析一次，工具调用不得自行重读 YAML、拼接更宽的 roots 或从用户/模型参数推导权限。
   - 文件、命令、浏览器、图片等副作用工具必须通过现有 session-aware wrapper/context 注入 capability；不要把 capability、`may_write`、`reserved_files` 或权限开关加入模型可见 schema。
   - child capability 只能继承或收窄父 capability；manager 的 `dispatching_parallel_agent` contract 仍只能有 `child_id` 和 `task`，task 文本不是授权载体。策略拒绝后应返回稳定 observation，禁止无策略重试或回退到 unrestricted 路径。
   - 新的外部 Agent session ID 必须是后端接受的 canonical UUID。历史 `agent-<UUID>` 只允许严格迁移/复用已有上下文；不要放宽为任意 legacy token。

2. **文件工具必须走 PathPolicy**：
   - `write_file`/`edit_file` 只能在 capability-bound `PathPolicy` 下执行；必须同时检查 resolved containment、所有父级和最终组件的 no-follow、普通文件类型、内容字节上限及竞态窗口。
   - read、search、file operation、image input 使用各自的 resolved roots；不要把它们 union，也不要把“当前工作目录”当作授权根。根目录范围的改变必须是独立、明确的配置决策。
   - 写入使用临时文件、安全的新文件权限、flush/fsync、同一安全 parent descriptor 下的原子替换，并在替换前重新检查目标。覆盖授权目录内普通文件默认允许，但 `allow_overwrite_existing=false` 必须拒绝；不能因覆盖开关跳过其他校验。覆盖已有文件要保留原 mode。
   - 图片 IPC 目录只能由经过验证的 session ID 选择；孤儿清理也必须通过固定 `session_temp_base_dir` 的安全策略，严禁对未经验证的字符串调用 `shutil.rmtree`。

3. **命令工具必须经过 CommandPolicy + EnvironmentBuilder + ProcessSupervisor**：
   - 默认使用 argv；浏览器参数必须保持结构化 argv，session token 只能作为一个 argv 元素。禁止 `shell=True`、`eval`、字符串拼接用户参数，禁止把浏览器/GUI Skill 当作命令安全边界。
   - `restricted_shell` 是显式受限兼容模式，不是 unrestricted shell：shell 嵌套（`bash -lc`/`sh -c`）、`source`、`eval`、重定向、管道、命令替换和后台语法会被拒绝。prompt 不应指导模型包裹 `bash -lc`；若确需新语法，先修改 policy/spec/test，再评估安全影响。
   - allowlist 只是 Python 层调用准入，不是 OS 沙箱。`cp`、`mv`、`touch`、`tar` 等允许的程序仍可能读写 file-operation roots 之外；需要真正文件系统隔离时必须另行引入容器、namespace、seccomp 或专用 worker，不能声称 capability 已提供 OS 隔离。
   - 命令执行环境必须由同一 runtime snapshot 构造：最小环境变量、受控 PATH/agent Python/Node/nvm、配置化 cwd、timeout、termination grace 和 stdout/stderr 上限。不要直接继承完整 `os.environ`，也不要硬编码覆盖配置。
   - 所有子进程通过 supervisor 创建 fresh process group/session；timeout、abort、shutdown 和异常路径都要清理整个进程组。新工具不得直接使用 `subprocess.Popen/run` 绕过 supervisor。

4. **Skill 不能授予额外权限**：
   - Skill 文档只描述操作方法，不改变 capability、allowlist、roots、session binding 或命令 policy；Skill 中的命令仍会经过相同的后端检查。
   - GUI launcher、Playwright/browser skill 与普通 shell 命令是不同边界：不要手写/覆盖 `DISPLAY`、`WAYLAND_DISPLAY`、`XAUTHORITY`、`XDG_RUNTIME_DIR` 或 D-Bus 环境变量，也不要把 GUI launcher 当成 shell 注入修复。
   - 新 Skill 如需写文件、执行命令、访问浏览器或消费图片，必须明确列出所需副作用，接入现有 wrapper，并为缺失 capability、session mismatch、越界路径和拒绝命令添加真实副作用测试。

5. **前端和多模式接入**：
   - text 与 vision 是两个公开路由，复用共享 UI/controller；新增 Agent 工具 glue、session 生命周期或 wire 字段应修改共享 owner，并验证两条 route 的能力与传输模式组合。前端生成的新 Agent ID 使用纯 canonical UUID，旧书签只能严格迁移 `agent-<UUID>`。
   - 新 agent/skill 的事件先写入统一 timeline，再由 projection reducer 决定聊天气泡、dispatch card 或 agent step；不要发明新的 SSE 协议，也不要把 UI transcript、timeline、routing 状态或权限描述回灌进模型历史。
   - 跨层字段必须显式翻译为 wire format；不要依赖 object spread 把前端 state 当成后端请求，也不要把前端显示内容当作授权或模型上下文。

6. **新增工具的最小验收清单**：
   - 测试 missing capability、capability/session mismatch、绑定 policy mismatch、非法/旧 session ID，以及直接库调用是否 fail-closed。
   - 文件类测试越界、父级/最终符号链接、目录/FIFO/device/socket、大小上限、覆盖开关、原子替换竞态、权限保留和失败临时文件清理。
   - 命令类测试 allow/deny、shell 元字符、环境 secret 不继承、cwd/timeout/grace/output limit、浏览器 session argv 完整性、父子进程清理和 targeted abort。
   - 同时覆盖实际 public wrapper、text/vision 页面路径和需要的 frontend type-check；不要只测试私有 policy 函数或 mock 掉真正的 child/process launch。
   - 运行与改动相称的 `.sglang/bin/python -m pytest`、`py_compile` 和 `frontend` type-check；若有失败或超时，先定位原因，不能以放宽安全策略换取测试通过。
