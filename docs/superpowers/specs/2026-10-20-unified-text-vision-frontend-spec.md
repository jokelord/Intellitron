# Text / Vision 前端统一架构 SPEC

- 日期：2026-10-20（America/Los_Angeles）
- 状态：**待用户审阅；尚未进入实施**
- 交付范围：本架构 SPEC；不包含实施计划、产品代码或测试修改。
- 决策依据：本次用户明确约束，以及仓库 `AGENTS.md`；两者优先于历史设计。

## 1. 目标与完成定义

将 `/` 与 `/vision` 的共同对话界面及应用控制逻辑收敛为一套实现，避免每次修改维护两份。
保留两个公开路由；Text 与 Vision 的能力边界保持明确，Chat 与 Agent 的请求链路继续独立。
共同逻辑只设一个状态所有者，页面差异通过显式能力和生命周期输入表达。
不以减少行数或把路由压缩成几行为完成标准，而以共同功能具有单一实现、所有权清楚、契约验证充分为标准。

本次只批准形成设计文档；文档审阅不等于已经实施，也不宣称现有测试通过。
实现前还需在用户审阅本 SPEC 后另行形成实施计划；本文仅定义架构切片，不列编码步骤。

## 2. 范围与硬性排除

纳入共享边界：消息展示、设置和配置初始化、皮肤、模板、语音/听写、斜杠命令、compact、Session Hub、工具配置、local memory、队列与 Cycle 页面衔接。
Vision 专属边界：上传图片、手动摄像头、浏览器屏幕/摄像头采样、Vision automation、图片 acceptance mutation 与相关资源管理。
复用现有 API client、wire builders、handler factories、reducers 和组件；不因命名不对称而另造实现或改名。
逻辑角色名称仅用于解释职责；具体文件名、hook 签名和参数形状由后续计划在依赖审计后确定。

以下项目明确不做：

- 不新增前端状态库、插件注册体系或通用 provider 框架。
- 不统一 Chat/Agent endpoint，不改变 SSE 协议、provider adapter、模型提示词或 tool schema。
- 不修改依赖包源码、部署链路、推理参数、compact 延迟参数或后端权限模型。
- 不补齐 Cycle 非 Agent 推理，也不承诺四种页面/交互组合具有不存在的功能。
- 不以本次前端 capability 判断替代后端模型能力和 OperationCapability 授权。

### 2.1 Answer-now 全面冻结

Answer-now 已废弃，本架构更新完全排除该功能；本条覆盖前期可行性报告中把它列为迁移目标的表述。
不得复制、迁移、重构、移动、改名、删除或恢复它的 feature handlers、state、replacement lifecycle、endpoint 或测试，也不改变现有调用语义。
不得把它列为受支持的功能、共享适配器或本次 parity 验收目标，也不得新增 Answer-now adapter。
名称包含 `answer-now` 的现有模块还提供活跃的 Stop/渲染 helpers，不能按文件名判定为死代码。
这些依赖及其签名保持冻结；共享 Stop/展示逻辑可原样调用，不借机整理其内部实现。
遗留回调依赖的 refs、输入和状态连接必须保留，不能因新 controller 接管普通请求而断开。
允许路由保留遗留区段和必要 glue；路由薄化服从冻结约束。
若某共享切片无法在不触碰冻结区的条件下提取，应隔离或延期该切片并明确报告，不做隐蔽搬移。
冻结测试不得为适配提取而改写；现有“无按钮”检查可原样执行，确保功能未重新出现。

冻结清单按符号/调用归属识别，不仅按文件名识别；同一文件内的冻结片段同样不能移动：

| 位置 | 冻结对象 |
| --- | --- |
| 两个 route | Text `handleAnswerNow`（1627）、Vision `handleAnswerNow`（2470），对应 loading state、clicked refs、replacement request/attachment、状态清理与遗留调用连接 |
| `api.ts` | `LLMClient.answerNow` / `answerNowStream`（1639/1650）及导出 wrappers（2001/2005） |
| `answer-now-ui.ts` | 功能专属 types/helpers，以及同文件被活跃 Stop/渲染调用的 `captureAgentInterruptionIntent`、`finalizeStreamingAssistantMessage`、`getAssistantRenderableText`、`getStreamingTerminalBehavior` 等既有实现/签名/调用语义 |
| 共享类型/handler | `AgentRunState.answerNowWasClicked`，handler environment 的兼容字段、`settleKnownAnswerNowRejection` 及功能相关分支；不因提取普通路径删改 |
| 后端 | `/answer-now`、`/answer-now/stream` 路由及关联 runner、producer/replacement 生命周期，全部排除 |
| 测试 | `answer-now-ui.test.mjs`、`answer-now-session-dispatch.test.mts`、`answer-now-disabled.test.mts`，以及其他测试文件中的 Answer-now 契约，全部冻结 |

本清单不授权扫描后“顺手清理”其他关联项；发现遗漏仍按全面冻结处理。

## 3. 两条独立轴与架构职责

页面 capability 来自服务端配置及当前路由规则，不能由 `agentMode` 推断。
`model_type: vision` 的现有 Text 跳转语义继续保留，包括当前缓存和服务端确认的路由选择。
浏览器缓存只是现有路由提示，不成为媒体授权或模型能力的新来源。

| 轴 | 可选值 | 责任 |
| --- | --- | --- |
| 页面能力 | Text / Vision | 决定图片输入、摄像头、采样与对应输入 UI 是否存在 |
| 对话链路 | Chat / Agent | 决定请求 API、会话及工具上下文生命周期 |

共享会话界面承载消息列表、普通输入、设置、模板及公共状态展示。
共享应用 controller 负责共同状态、事件归属和用户意图到现有 API 的连接。
Chat/Agent 继续通过现有不同 client 方法和 handler factories 协作，不把两种传输压成一个万能请求。
Vision adapter 只在有 Vision capability 时挂载，提供图片输入和媒体生命周期接点。
通过组件挂载边界装配，禁止条件调用 hooks；若保留无条件 hook，其 disabled 分支必须显式惰性且无设备/持久化/transport 副作用。
Text 不挂载 Vision controller、automation terminal gate、媒体资源注册或设备探测逻辑。
Cycle controller 共享普通执行、恢复和事件归属，但通过 capability 决定是否允许相机请求。
路由保留路由选择、能力装配及冻结遗留连接；不强制所有状态都迁入同一个巨型 hook。

## 4. 现有可复用基础

现有 `createTextAgentSessionHandlers()` 已被两页的普通 Agent live/replay 使用；其名字不表示只能服务 Text。
保留这个共同投影来源，不建立新的 shadow replay reducer 或重复的 Agent callback 实现。
Chat recovery 继续复用 `createChatSessionHandlers()`，local memory 展示沿现有 handler 处理。
SSE 读取、顺序检查、heartbeat、attachment registry、bookmark transaction 与恢复 coordinator 已有共享实现。
消息/UI 类型、dispatch 投影、reasoning、compact 投影和 wire builders 均应作为现有契约使用。
语音 controller、dictation polling、Cycle agent projection、滚动跟随及多数展示组件亦已可复用。
提取重点是页面组合和所有权连接，不重写这些已承担契约的基础模块。
与冻结 Answer-now 共用的 factory/signature 保持兼容；若必须改动冻结调用方才能提取，停止该切片。

## 5. Vision 为主的差异裁决与已确认清单

仅对**已核实的共同路径差异**默认以当前 Vision 行为为主要参考；不整页复制，也不假定 Vision 没有缺陷。
能力差异单独保留，无法证明后果的差异记录为未决；用户新规则及 `AGENTS.md` covenant 优先于两页现状。
以下为静态源码确认；未运行服务或复现运行时故障，不能把推断写成已证实的线上根因。
每项迁移结果须报告证据、影响、对齐方式和验证；新发现须在相关具体结果提交前或同时报告，不能静默修复。

| 编号/分类 | 证据与影响 | 本 SPEC 裁决与验收 |
| --- | --- | --- |
| D1 共同路径差异 | Text `/models` 与后续 `/config` 共用 try，前者失败会阻断配置；Vision 直接读取 config。来源 R2。 | 采用 Vision config-first；模型列表只作非阻塞补充。验证 models 失败时设置仍能初始化。 |
| D2 共同展示差异 | Text 对 `isPreCompact` 加 `opacity-60`，Vision 不加；历史 compact 设计曾要求变淡。来源 R3。 | 遵循用户当前 Vision-primary 选择，公共展示采用 Vision 当前不变淡的外观；保留历史语义标记、metadata 与 compact 边界。验证两页一致且 wire 不变。 |
| D3 共同生命周期差异 | Text 保存 Chat/Agent 设置时直接替换设置；Vision 还处理旧 transport 的 abort/ownership retirement。来源 R4。 | 普通模式切换采用 Vision 的捕获旧 owner、当前 token 检查和订阅清理；automation armed 阻止切换及 invalidation 仍仅 Vision。验证迟到 callback 不写新模式。 |
| D4 Text capability 缺口 | Cycle 编辑器只隐藏相机字段，缓存/default 仍可携带 photo，Text start 原样 POST，后端仍拍照；“will be skipped”提示无对应保障。来源 R5/R6。 | 按第 9 节整次拒绝 Text start，给出精确路径；不能依赖隐藏字段、静默删图或跳过消息。 |
| D5 Text 资源/启动差异 | Text start 无能力判断就销毁浏览器 camera streams；Vision 有 delayed-start token/cancel recheck。来源 R7。 | 共享通用启动 generation/cancel guard；硬件释放/等待只用于 Vision 拍照路径，Text 不调用媒体清理。 |
| D6 跨页面恢复缺口 | Cycle bookmark 无来源，两页均从同一指针恢复；Text 还投影 `photo_captured`。来源 R8。 | 新 bookmark 按 pageMode 隔离，Text 不接管 Vision/未知 legacy run；按第 10 节拒绝不明来源恢复。 |

以下差异不作为已确认 bug：

- Text 的模型跳转，Vision 的图片队列、acceptance 与 terminal busy gates：属于能力差异。
- Text recovery 开始时清空 active run ref，Vision 未同样清空：尚无用户可见故障证据，不能盲从 Vision 删除保护。
- Vision recovery 同步更新 `messagesRef` 服务于图片/compact 读路径：有现存消费者，不因 Text 无此 ref 而删除。
- 两页队列都通过 timer 再读 send ref：潜在时序风险需行为验证，不能称为单页漂移根因。
- 若 sticky Agent image omission 与 covenant 有疑似差异，须另列 reconciliation 议题；本次提取不授权更改该策略。

## 6. 请求、状态与所有权

必须区分 LLM/Agent session、request、subscriber attachment、page generation、automation sampling task、capture cycle。
同一 session 可有多个 request generation；同一 request 的订阅断开不代表后端 producer 已停止。
页面 hidden、unmount 或 subscriber detach 只按既有协议处理浏览器订阅与资源，不自动 Stop 或 DELETE backend session。
普通 Chat Stop 仍按 request ID；Agent Stop 保持 session 定位，沿用现有可选 `expected_request_id`，调用方提供时继续比较，不新增必填字段。
pending abort、response registration token、旧 worker cleanup 防护和单会话禁止 abort-all 均保持现状。
SGLang targeted abort 与 vLLM 关闭已登记 upstream response 的差异属于既有 provider 生命周期，不在前端重构中改写。

共享 controller 为普通状态提供唯一 owner，并保持 refs 与 React state 一致，不能依赖 render 闭包的新鲜度。
imperative admission、Stop、Clear、模式切换和异步恢复都需验证当前 attachment/request/generation。
旧 callback 不得 finalize 新请求、清空新 busy 或覆盖新会话；处理器和 teardown 必须幂等。
StrictMode 重挂载及 effect cleanup 要有明确资源归属；同一资源只释放一次，旧 cleanup 不释放新资源。
未知传输结果不视为明确拒绝；producer error 也不提前代替 canonical terminal 解锁。

保留 Text/Vision 分离的 Session Hub bookmark、legacy transcript migration 及其事务回滚语义。
Clear 只处理当前有归属的目标；detach 保留恢复入口，不把“用户暂离页面”转换为删除。
Agent paired canonical UUID、session 工具配置锁定、未知工具拒绝与 typed rejection 行为保持。
Agent tool schema/config 在既有 session admission 边界冻结，不能随共享组件重渲染改变。
冻结遗留回调依赖的 refs/flags 继续可用；它们不是本次删除状态的候选。

## 7. 模型历史、compact 与输入构造

canonical 模型历史与 UI bubbles/timeline 是不同层，不把 UI projection 当作完整模型上下文。
继续显式调用 `buildChatWireMessages()`、`buildAgentContextMessages()`、`buildCompactRequestBody()`。
`context_messages` 是把符合条件的可见文本历史导入 Agent memory 的桥，不是通用 canonical source，也不能重建工具轨迹。
跨层字段必须显式映射；禁止用通用对象 spread 假设 UI flags 已自动成为 wire metadata。
保留 compact 的 `pre_compact`、`compact_summary`、summary ID 与当前 transport refs 映射。
不新增历史截断、合并、改 role、移动或重写工具观察，不将 dispatch/UI/usage 当作提示正文。
保留匹配的 assistant `tool_calls` 与 observation metadata，以及 context 已固定的 `legacy_text` / `qwen35-native` dialect；前端提取不得升级保留上下文或给历史 tool results 改 role。
已有 wire 分支的兼容行为在提取中不顺带扩展；若发现违反 covenant 的行为，单列差异而非默许保留或静默修复。
session-pinned system/runtime/tool prefix 与 append-only tail 保持；动态 request/route 字段不得插入 system 或历史中段。
等价 SGLang/vLLM 请求的 messages/schema 保持等价，retry/fallback 不为 UI 提取改变前缀。

compact 的权威约束为 `AGENTS.md` Prefix-Cache Covenant 13.1–13.9：

- 保留 cache-first final user-channel 自包含内部指令；正常请求不预置 compact-control system clause。
- cache-first 首试保留未裁剪 source；`source_token_budget` 不能作为 eligibility gate，只约束允许的 reliable fallback。
- reliable fallback 继续仅在显式启用时可用；失败 cache-first 增量不提前进入 UI。
- Agent compact 继续采用既有最近成功非 coordinator_1 normal-call capture，不从 UI/context pack 重建。
- thinking-off、summary-only、9-section 格式及 no-tool/no-routing 约束保持，成功 replacement 后清理旧 capture。
- 不增加 performance ratio、summary token 等延迟参数，也不改 SGLang cache observability 配置链。
- Agent auto-compact summary 插入 streaming assistant anchor 之前；Chat 沿用 `projectChatMemory.getUserMessageId()` 的当前 user anchor；manual 路径保留既有插入规则与失败 rollback。
- usage chunk 仅更新现有诊断，不进入聊天/推理/摘要文本，不新增前端协议。

## 8. Vision 媒体、acceptance 与例外边界

手动图片、浏览器 automation source、voice replay 和 backend Cycle OpenCV capture 是不同资源所有者。
Text 不上传图片，不枚举、获取或拍摄摄像头，不挂载 automation，不借助 Vision busy gate 实现普通 busy。
这些限制针对输入能力及错误归属的 Cycle photo，不禁止 Text 展示合法历史/工具输出中已有的 display-only 图片。
Vision 当前图片/纯图发送、手动拍照、上传缩放配置与 automation 功能保持。
automation browser state 不进入 Hub bookmark、模型消息或 replay：stream、buffer、controller、cleanup secret、lease 均为 browser-local。
durable page generation allocator 仍在 commit 后 effect 中绑定，不在 render 初始化时读写 localStorage。
所有采样、重启与 cleanup callback 绑定开始时的 generation、Chat/Agent mode 和 top-level context；不得跨 session。
pending terminal gate 在稳定 terminal/status 前保留 busy；candidate completion、EOF、unknown status 均不能冒充 terminal。
明确 rejected 才能走对应 rollback；ambiguous transport 不丢弃应保留的 acceptance/恢复信息。

历史例外严格引用 `AGENTS.md` covenant 9–11，不在本文重新定义或扩权：

- Vision automation 历史 image block omission 及单次 Abort 后 cleanup token 保持既有绑定和成功接纳提交边界。
- Computer Use 只针对 allowlisted executable 所产生的 `computer_use_observation`，保留最新 block，限定 top-level Agent。
- tool-feedback omission 保持显式 opt-in、完成状态及长度阈值条件，不扫描 children/siblings 或 Chat。
- staged projection 只有跨越各自规定 admission/acceptance barrier 后才成为 canonical；Stop、compact、拒绝及不明结果不擅自消费权限或改历史。
- 对应 boundary event、provenance、原始工具 calls/results 与非图片文本均保留；已省略历史不恢复。
- 任何现有 sticky omission 行为疑义单独报告，本次不会以“统一两页”为由修改。

## 9. Cycle：Text 相机禁止与启动边界

禁止范围同时覆盖 `CycleMessageStep.photo`、任意深度 `cycle_steps` 中的 message，以及 `ScheduledMessage.photo`。
新建、已保存程序、server defaults、编辑器输入和 programmatic start 都受同一 capability 校验。
Text 的普通、嵌套、定时消息相机选择 UI 均不可用；隐藏 UI 不是唯一保护。

**裁决：Text 程序任一消息有非空 photo 请求，即整次拒绝启动。**
`photo: []` 表示无相机请求，不因此拒绝；空数组不能被真值判断误判为拍照。
错误指出精确路径，例如 `steps[1].cycle_steps[2].photo` 或 `scheduled_messages[0].photo`。
显式验证缓存/default/programmatic 输入的数组、step kind、递归容器和 photo 结构；非法结构返回本地路径错误，不能 crash、跳过检查或当作无图成功执行。
在 editor 与共享 start boundary 都做校验，后者防止 imperative/programmatic 调用绕过。
拒绝发生在 start POST、run ID/启动 generation 分配、busy/run 状态变更、bookmark 写入、媒体清理及启动 timer 之前。
允许展示本地验证错误；不触发设备访问、网络启动或保存程序的自动修正。
不静默 strip photo，不跳过整条 message，不改变文本/排序/defaults，也不重写现存保存程序。
编辑器须明确说明“Text 不允许该相机程序，整次未启动”，取代没有实现保障的 skipped 承诺。
程序可以保留并在 Vision 打开；只有用户明确编辑才改变保存的 program。

共享 Cycle start lifecycle 采用 generation/token guard，重复点击、取消、Clear 和迟到 promise 不启动旧 run。
Vision 相机程序继续由后端 OpenCV capture；浏览器 streams 的释放只是让出设备，不是实际 Cycle 拍照。
硬件释放/等待只发生于 Vision camera 路径，不扩大到 Text 或无相机程序。
等待后重新检查 start generation、busy 与 automation 归属，取消后的延迟回调不得 POST。
公共控制器复用生命周期 guard，不把 Vision 的 hardware cleanup、设备等待或 automation gates 搬进 Text。
设备释放遵守现有来源/资源所有权，不用一次 shared cleanup 销毁其他 context 的资源。

该保证针对本项目前端支持的入口；现有 backend models 没有 page capability，不宣称此为后端安全边界。
不新增 server capability 协议，也不改变现有后端权限或外部 API 客户端契约。
Cycle `agent_mode=false` 当前仍输出“非 Agent 模式暂未实现”占位；本次保留并明确限制，不实现新推理链路。

## 10. Cycle：页面归属、恢复与持久化

当前 `cycle-active-run` 仅含 run ID、fingerprint、cursor，没有 page origin；两页均自动恢复它。
新的 browser-local bookmark 使用 additive versioned 结构/命名空间，按 Text/Vision pageMode 分离。
pageMode 只存浏览器，不进入 POST body、fingerprint 或 run ID；不为迁移重算/改写后端身份。
保留 run ID、fingerprint、单调 cursor、generation guard、journal replay 与 scheduled occurrence 顺序。
元数据仅表达浏览器 UI ownership，不代表后端授权，也不修改 backend run identity 或 canonical memory。
共享 `cycle-editor-state` 与其 defaults 中的 program 保留；bookmark 迁移不清洗或改写程序内容。

Text 只接纳明确归属于 Text 的新版本恢复指针；Vision/未知 legacy 指针不得自动导入 Text。
legacy 来源模糊时 Text fail closed，不 resume 或投影该 run，并提供到 Vision 查看/恢复的入口。
保留 legacy pointer 及 program，不用猜测来源的迁移把它改成 Text，也不自动删除未知 run。
Vision 保留现有兼容恢复体验，不新增点击或审批门槛；status/attachment 成功确认后才提交本地 ownership 迁移，此前保留原指针。
旧 status snapshot 缺少足以证明原始 program 无图及 page origin 的权威信息，不能据此宣称 Text 恢复安全。
本次不扩展 server snapshot/protocol 来补此信息，也不根据 replay 到目前还未出现图片推断未来无图片。

Text Clear/Stop 只作用于其当前明确 owned run 和对应页面 bookmark。
所有 Cycle read/write/clear/status/reconnect/Stop 入口都须先确定 page/run owner，包含错误、过期和 cleanup 分支。
不得因为发现共享 legacy/Vision 指针而 stop/delete 其 producer、清除 Vision bookmark 或取消它的 scheduled track。
Text cleanup 必须保留 foreign/legacy bookmark；未知来源不能靠 cleanup 顺手迁移或删除。
不属于当前 page/run/generation 的 live/replay 事件均不得修改 Text 状态；未知来源的 `photo_captured` 不插入图片或 thumbnail。
对已声明 Text-owned 的 run 若出现不允许的 photo 事件，也应阻止图片投影并显式报告 ownership/capability 异常；不擅自停止外部 producer。
保留 timezone、独立 scheduled track、occurrence index、排队/交付顺序、fingerprint 冲突和过期/可重试分类。

## 11. 配置、语音、队列与工具

共同 bootstrap 采用 Vision config-first，`/models` 补充失败不阻塞设置/ASR/Cycle config。
服务端配置到浏览器状态的映射保持显式；Vision options 独立装配，Text redirect 保留现有规则。
缓存按键存在性优先于 YAML，`false`、`0`、空字符串不得当作缺失；复用现有 parse/merge helpers。
现有 maxTokens 验证/上限约束、hydration 保存防护、runtime context pinning 及设置重置语义保持。
模板插入、立即发送、slash command 与 compact 自定义指令继续走同一普通发送入口，不另建旁路。

voice mode/controller phase 分开建模，启动失败仍展示可重试面板；reactive visibility/resume 条件保持可诊断。
页面 hidden 不自动销毁 MediaStream/voice；恢复继续遵守共同 busy、dictation pending 和 Vision pending acceptance 条件。
听写只插入文本，full-auto voice 按既有 final transcript 发送；模式切换等待 cleanup，防止旧音频回调进入新上下文。
voice replay/object URL 各有 owner，发送、丢弃、Clear 与 unmount 只释放自己拥有的资源一次。
冻结遗留 callbacks 所需的 replay/ref 连接不因公共 hook 提取而移除。
imperative send 仍通过 ref-backed busy admission 防止同一时刻重复 POST。
队列保留每轮捕获的文本及 Vision image snapshot，不从未来草稿借用图片；Text 无图片 payload。
不把通用 UI transcript/payload spread 当作输入构造；Agent 工具与 local memory 配置继续使用既有 session admission 语义。

## 12. 验证与验收契约

实施后的验证按已有功能事实设计，不要求不存在的 Cycle 非 Agent 推理；本文没有执行以下检查。
普通对话覆盖 Text/Vision × Chat/Agent；每条链路覆盖 live/replay、accepted/rejected/unknown/detached/terminal。
覆盖 Stop、Clear、Chat/Agent 切换、compact、工具与配置锁定、local memory、模板、slash、voice/dictation 与队列。
比较共同逻辑的消息/事件结果及资源所有权，不只比较 DOM 字符串或代码长度。
Answer-now feature 不进入 parity 目标，冻结测试不改写；原有无按钮检查可原样运行。

| 维度 | 必须验证 |
| --- | --- |
| 会话生命周期 | 重连/重放幂等、heartbeat 只刷新 transport、迟到 generation 被拒绝、detach 不 Stop producer、Clear 精确目标 |
| 输入与配置 | config-first 失败隔离、缓存键优先含 false/0/空串、maxTokens 上限、工具 paired UUID 与 typed rejection |
| 模型输入 | canonical history 与 UI 分离、builders metadata、provider/request shape 不漂移、工具轨迹保留 |
| compact | cache-first 首试、可选 fallback、thinking-off、正确 anchor/rollback、failed delta 隔离与 post-compact 映射 |
| voice | 模式切换 cleanup、final transcript、听写光标插入、恢复 gates、启动失败重试、replay 资源不重复释放 |
| Vision | 手动图片/纯图、图片队列 snapshot、automation generation/context pinning、unknown acceptance、terminal gate、限定 omission |
| Cycle 既有行为 | 普通/嵌套/scheduled、start cancel token、恢复 fingerprint/cursor、timezone/occurrence、Agent 投影；非 Agent 保持明确占位 |

Text 相机负例必须覆盖普通/任意嵌套/scheduled、saved/default/programmatic start，以及 foreign/legacy recovery 与迟到 live events。
断言整次拒绝时没有 start POST、没有相机 enumerate/acquire/capture/cleanup/硬件等待、没有 run/busy/bookmark/timer 副作用。
覆盖 `photo: []` 合法、缓存递归结构非法时安全拒绝、未知 bookmark status 不能被当作来源证明。
同时断言 program/defaults 未被 strip 或保存改写，Vision producer/bookmark 未被 Text Clear/Stop 影响，Text 不插入 photo 图片。
Vision 正例证明相机能力保留，硬件等待后取消不会启动；共享 lifecycle 不误伤其他资源 owner。

现有测试有大量读取两页源码的 regex；只迁移本次范围内测试为共享行为测试加薄 route wiring 检查。
不得通过删除 attachment/generation/compact/voice/camera 安全断言获得通过，也不得改写冻结 Answer-now 测试适配移动。
后续实施应运行 type-check、build 与有意义的 scoped Node/Python 集成测试；不在本 SPEC 猜测命令或声称已通过。
完成需提供对应差异清单、兼容性证据与已执行验证；无法验证的范围明确列出。

## 13. 架构切片顺序、风险与边界

架构切片顺序为：共同展示/配置设置 → 普通对话 controller/ownership → Cycle 与 Vision adapter 边界 → 行为 parity。
这仅说明依赖方向，不是编码任务表；每个切片都受 Answer-now 冻结及 covenant 限制。
未能无侵入共享的遗留耦合允许暂留路由，必须报告边界，不能借“最终薄路由”越过冻结。
主要风险是 refs 与闭包不同步、StrictMode cleanup、unknown admission、源代码 regex 测试耦合及 legacy Cycle ownership 缺失。
控制方式是复用现有 factories/registries、显式事件归属、分离 browser/backend owner，以及上述正负行为验收。
普通 backend 接口保持；如果发现确实需要协议变更，本次不自行扩围，应另提设计差异。
Vision-primary 只裁决明确共同漂移；Text 相机禁止、历史 covenant 和冻结约束对两页均有更高优先级。

## 14. 精简源码索引

以下行号对应本次只读审计快照，实施时需重新确认；不据行号代替契约。

- R1 权威约束：[AGENTS.md](/home/zk/Project/LLM-deployment/AGENTS.md:26)，以 Prefix-Cache Covenant 及其中 13.x compact 条款为准。
- R2 bootstrap：[Text](/home/zk/Project/LLM-deployment/frontend/src/app/page.tsx:463)、[Vision](/home/zk/Project/LLM-deployment/frontend/src/app/vision/page.tsx:771)；共同 merge [api.ts](/home/zk/Project/LLM-deployment/frontend/src/lib/api.ts:331)。
- R3 opacity：[Text](/home/zk/Project/LLM-deployment/frontend/src/app/page.tsx:2336)、[Vision](/home/zk/Project/LLM-deployment/frontend/src/app/vision/page.tsx:3465)；[历史设计](/home/zk/Project/LLM-deployment/docs/superpowers/specs/2026-06-06-slash-commands-and-compact-design.md:440)。
- R4 模式切换：[Text](/home/zk/Project/LLM-deployment/frontend/src/app/page.tsx:2268)、[Vision](/home/zk/Project/LLM-deployment/frontend/src/app/vision/page.tsx:3366)、[transport helper](/home/zk/Project/LLM-deployment/frontend/src/lib/vision-automation/page-integration.ts:225)。
- R5 Cycle UI/缓存：[CycleEditor](/home/zk/Project/LLM-deployment/frontend/src/components/CycleEditor.tsx:30)、校验 66/提示 96/scheduled gate 113；[嵌套传递](/home/zk/Project/LLM-deployment/frontend/src/components/CycleStepEditor.tsx:242)；[defaults/state](/home/zk/Project/LLM-deployment/frontend/src/lib/cycle-types.ts:162)。
- R6 Text POST/后端：[Text start](/home/zk/Project/LLM-deployment/frontend/src/app/page.tsx:1870)、[photo models](/home/zk/Project/LLM-deployment/backend/cycle/models.py:16)、[capture](/home/zk/Project/LLM-deployment/backend/cycle/executor.py:542)、[非 Agent 占位](/home/zk/Project/LLM-deployment/backend/cycle/executor.py:694)。
- R7 Cycle start：[Text cleanup](/home/zk/Project/LLM-deployment/frontend/src/app/page.tsx:1844)、[Vision token/等待](/home/zk/Project/LLM-deployment/frontend/src/app/vision/page.tsx:2800)。
- R8 Cycle 恢复：[bookmark](/home/zk/Project/LLM-deployment/frontend/src/lib/cycle-page.ts:40)、[Text](/home/zk/Project/LLM-deployment/frontend/src/app/page.tsx:1791)、[Vision](/home/zk/Project/LLM-deployment/frontend/src/app/vision/page.tsx:2749)、[Text photo 投影](/home/zk/Project/LLM-deployment/frontend/src/app/page.tsx:1747)、[status snapshot](/home/zk/Project/LLM-deployment/backend/cycle/run_registry.py:191)。
- R9 共享底座：[Agent handlers](/home/zk/Project/LLM-deployment/frontend/src/lib/text-agent-session-handlers.ts:379)、[Chat handlers](/home/zk/Project/LLM-deployment/frontend/src/lib/chat-session-handlers.ts:105)、[Hub reader](/home/zk/Project/LLM-deployment/frontend/src/lib/session-hub-client.ts:166)、[bookmark transactions](/home/zk/Project/LLM-deployment/frontend/src/lib/session-hub-recovery.ts:131)。
- R10 输入与媒体：[wire builders](/home/zk/Project/LLM-deployment/frontend/src/lib/message-types.ts:436)、[compact builder](/home/zk/Project/LLM-deployment/frontend/src/lib/compact-client.ts:143)、[voice](/home/zk/Project/LLM-deployment/frontend/src/lib/voice-input.ts:662)、[Vision lifecycle](/home/zk/Project/LLM-deployment/frontend/src/lib/vision-automation/page-integration.ts:550)。
- R11 测试耦合：[route lifecycle](/home/zk/Project/LLM-deployment/frontend/src/lib/text-agent-session-handlers.test.mts:446)、[voice wiring](/home/zk/Project/LLM-deployment/frontend/src/lib/voice-input-pages.test.mts:58)、[compact](/home/zk/Project/LLM-deployment/frontend/src/lib/compact-projection.test.mts:414)、[Vision generation](/home/zk/Project/LLM-deployment/frontend/src/lib/vision-automation/page-integration.test.mts:855)。
- R12 冻结接口：[Text feature](/home/zk/Project/LLM-deployment/frontend/src/app/page.tsx:1627)、[Vision feature](/home/zk/Project/LLM-deployment/frontend/src/app/vision/page.tsx:2470)、[API client](/home/zk/Project/LLM-deployment/frontend/src/lib/api.ts:1639)、[活跃通用 helper 所在文件](/home/zk/Project/LLM-deployment/frontend/src/lib/answer-now-ui.ts:27)、[后端路由](/home/zk/Project/LLM-deployment/backend/api/agent.py:1933)。

本文件停留在书面设计审阅阶段；尚未创建实施计划或修改产品行为。
