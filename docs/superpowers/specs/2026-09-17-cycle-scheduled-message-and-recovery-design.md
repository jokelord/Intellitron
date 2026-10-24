# Cycle 定时 Message、断线恢复与可读性优化设计规格

> 状态：已由用户确认设计，待实现。
>
> 本文是实现规格，不是实现计划。实现前应依据本文再创建逐任务实施计划。

## 1. 背景与目标

当前 Cycle 功能把一个步骤树按顺序执行：`wait` 在前一个步骤完成后开始计时，普通 `message` 在轮到它时拍照并调用 LLM，`cycle` 负责有限次数地重复子步骤。这个模型适合“上一轮完成后再等待一段时间”的循环，但无法表达固定的日历时刻。

本变更增加一种与普通步骤树并行的定时轨道，支持用户在 `HH:mm:ss` 指定本地时刻发送文字和摄像头图片，并支持“仅1次”或“每天 N 天”。目标如下：

1. 保留普通轨道的串行语义和现有嵌套循环。
2. 让定时 message 不进入任何 `cycle` 或子循环，按绝对时刻独立触发。
3. 当定时 occurrence 在 Agent 推理或其他步骤期间到期时，精确入队；当前动作完成后立即执行，并在普通 message 的边界上优先执行定时任务。
4. 将 Cycle 的执行 producer 与浏览器 SSE subscriber 解耦，使浏览器刷新或 SSE 断开不影响 backend 继续运行，并允许重新连接和按游标恢复事件。
5. 对 Cycle 使用特殊的 run-level timeout 策略，避免 24 小时以上任务因连接空闲、Cycle 总时长或 Agent session TTL 被错误中断；单次工具和单次推理仍保留安全边界。
6. 使 Cycle 画布不再跟随 Web UI 页脚透明度，并修复浏览器浅色模式下的文字、控件和边框对比度。

## 2. 明确术语

| 术语 | 定义 |
| --- | --- |
| Cycle mode | 用户在 Web UI 中启动的自动化执行功能；不是 Agent session 的同义词。 |
| Cycle run | 一次点击 Start 产生的后端执行实例，拥有一个 Cycle run identity、两条轨道、一个调度仲裁器和一个事件 journal。现有 Cycle API 的 `session_id` 在此语境中表示 run identity；后续新字段优先使用 `cycle_run_id`。 |
| Cycle run identity | 标识一次 Cycle run 的 opaque/canonical ID，只用于找到该次 Cycle 的执行状态和事件，不代表 LLM 上下文。 |
| Agent session identity | 某一次普通或定时 message 调用所使用的 Agent 上下文 ID。`share_context=true` 时可在同一 Cycle run 中复用；`share_context=false` 时不同 message 可能使用不同 Agent session。规格不假设一个 Cycle run 只有一个 Agent session。 |
| 普通轨道（ordinary track） | 现有 `steps` 步骤树，允许 `wait`、普通 `message` 和有限深度的嵌套 `cycle`；其步骤仍按顺序执行。 |
| 定时轨道（scheduled track） | Cycle run 的独立顶层 `scheduled_messages` 列表；每条是一个定时 message 定义，只允许这一层，不允许出现在普通 `steps` 或任何子循环中。 |
| 定时 message | 定时轨道中的一条定义，包含 `HH:mm:ss`、重复模式、文字和可选摄像头索引。 |
| occurrence | 某条定时 message 在某个本地日期实际产生的一次触发；`每天 N 天` 最多产生 N 个 occurrence。 |
| 定时队列（due queue） | occurrence 已到期但调度仲裁器暂时不能发送时保存的 FIFO 队列。它保存 occurrence 状态，不创建无限的后台发送任务。 |
| 单时间轴调度仲裁器（dispatch arbiter） | Cycle run 内唯一允许启动拍照、Agent admission、LLM dispatch 的入口。它把两条逻辑轨道合并为一个实际发送顺序。 |
| timeline/journal | 按事件 sequence 记录 Cycle 的实际生命周期和发送结果，供当前 UI 和断线恢复使用；不是 LLM prompt，也不能写回 Agent memory。 |
| producer | backend 中持续执行 Cycle run 的 asyncio task/执行单元，与 HTTP/SSE subscriber 生命周期分离。 |
| subscriber | 连接 `/start` 或恢复事件 endpoint 的浏览器传输，仅负责接收 journal/live events；断开不等于 Stop。 |

执行结构如下：

```text
Cycle run
├── 普通轨道：steps -> step cursor
├── 定时轨道：clock -> occurrence planner -> due queue
└── 单时间轴调度仲裁器
    ├── 普通 message dispatch
    └── 定时 message dispatch
        -> timeline journal / live subscribers
```

## 3. 范围与非目标

### 3.1 本次范围

- backend Cycle request model、执行器、定时计算、队列、事件 journal、恢复 endpoint 和 Cycle 专用 timeout policy。
- `config/config-misc.yaml`、`backend/config.py` 及配置 `/config` 返回值中的 Cycle 定时与恢复配置。
- `frontend/src/lib/cycle-types.ts`、共享 Cycle 编辑器、Cycle progress、LLMClient 和 text/vision 两页的启动、投影、恢复 glue。
- Cycle 画布的独立透明度变量和浅色/深色高对比样式。
- 真实拍照、Agent session、compact、canonical message history 继续复用现有普通 message 路径。

### 3.2 非目标

- backend 重启后自动恢复 Cycle；进程内 run record 和 journal 在 backend 重启时丢失，前端必须把该 run 标记为 expired/unavailable，不能重放或继续发送。
- 使用数据库或跨进程队列实现持久化。
- 让定时 message 进入普通 `cycle` 的遍历逻辑，或在每个普通步骤中轮询时刻。
- 用浏览器 `setTimeout` 作为定时真相来源。
- 允许任意浏览器上传图像成为定时任务 payload；本规格中的图片字段沿用普通 Cycle message 的摄像头索引语义。
- 修改全局 Chat/Agent 的 timeout、Session Hub 或 LLM prompt 规则。Cycle 的豁免必须通过明确的 Cycle scope 传递。

## 4. 用户可见行为

### 4.1 两条轨道

Cycle 编辑器展示两个明确区域：

1. “普通轨道”：继续编辑现有 `wait`、普通 `message`、`cycle`。普通 message 仍可被普通 `cycle` 重复调用。
2. “定时轨道”：使用独立列表编辑定时 message。列表项不能拖入普通步骤树，嵌套编辑器不能提供 `scheduled_message` 选项。

用户启动时，前端同时提交两条轨道和浏览器 IANA 时区。运行中编辑器不可修改，Stop 后可以重新编辑并启动新 run。

### 4.2 定时项控件

每条定时 message 提供：

- `time`：秒级 `HH:mm:ss` 控件，必须完整显示小时、分钟、秒。
- 重复开关：`仅1次` / `每天`。
- 仅选择 `每天` 时显示 `循环天数`，按条单独设置，最小值为 1，最大值来自 backend config。
- message 文字输入。
- vision 页面中的摄像头索引输入；text 页面保持现有“不提供拍照控件/显示已有 photo warning”的行为。
- 删除按钮。

UI 应显示本次请求发送的时区，例如 `Asia/Shanghai`；该值来自 `Intl.DateTimeFormat().resolvedOptions().timeZone`，不由用户手写，也不由 backend 主机时区推断。

### 4.3 时刻与 occurrence 规则

- Cycle start 的时刻等于定时值时，视为该时刻到达，可产生当天 occurrence。
- Cycle start 晚于当天定时值时，跳到下一天；`仅1次` 在下一天触发，`每天` 的第 1 次也从下一天开始计数。
- `每天` 的 N 次按同一个本地 `HH:mm:ss` 在连续本地日期触发，不因普通轨道的 wait、cycle 周期或 Agent 耗时重新计算周期。
- 每条定时项独立维护 occurrence index；一条项达到 day count 后结束，不影响其他定时项或普通轨道。
- 日期和时间按请求中的 IANA timezone 解释。夏令时跳过不存在的本地时刻；该日期不消耗 day count。重复的本地时刻使用该日期的第一次 occurrence（`fold=0`）。固定时区（如 `Asia/Shanghai`）不受此规则影响。
- 到期判定使用 wall-clock 日历时间；等待使用 monotonic/可取消的异步等待，系统时钟调整时重新计算下一次 occurrence，不用固定 sleep 累加漂移。

### 4.4 排队、抢占和优先级

- 定时器只负责把到期 occurrence 放入 due queue，不直接调用 LLM，不并发创建 dispatch worker。
- 普通轨道的 `wait` 不会被定时 occurrence 打断，仍然从前一个普通步骤完成后开始计时。
- 普通 wait/message/cycle 子树每次完成后，调度仲裁器刷新到期时间并按优先级 drain/执行 due queue；在开始下一个普通步骤前再次刷新。
- 正在执行的普通或定时 message 不可抢占。若其执行期间到期，occurrence 入队；当前 dispatch 完成后立即执行队列。
- 定时 occurrence 与普通 message 启动边界重合时，定时 occurrence 先执行；普通 message 必须等待所有当时已到期的定时 occurrence 完成。
- 多条定时项同时到期时按 `(scheduled_for, editor_order, occurrence_index)` 排序；入队后不因后续普通步骤改变顺序。
- 定时 message 自身执行期间再次到期的 occurrence 按相同排序入队，禁止 duplicate occurrence。
- 普通轨道结束时，若定时轨道仍有未来 occurrence，Cycle run 不结束，转为等待定时轨道；若队列有已到期项，先执行队列。
- 两条轨道都完成后才发送 Cycle terminal event。

## 5. 请求与配置契约

### 5.1 Frontend/Backend request shape

推荐新增独立模型，而不是把定时字段塞入递归 `CycleStepRequest`：

```json
{
  "cycle_run_id": "<canonical cycle run id>",
  "steps": [],
  "scheduled_messages": [
    {
      "id": "schedule-1",
      "time": "14:30:00",
      "recurrence": "daily",
      "day_count": 3,
      "message": "请报告当前画面",
      "photo": [0]
    }
  ],
  "timezone": "Asia/Shanghai",
  "agent_mode": true,
  "share_context": true,
  "tool_configuration": {}
}
```

具体字段规则：

- 现有 `session_id` 请求字段保留兼容；如果 `cycle_run_id` 与旧字段同时存在，必须相同，否则返回 409。新前端发送 `cycle_run_id`，兼容客户端可继续发送 `session_id`。
- `id` 是用于事件关联的客户端 opaque key，不是 Agent session ID，不参与路径拼接；限制为 ASCII `[A-Za-z0-9_-]{1,64}`。
- `time` 只接受严格正则 `^(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$`。
- `recurrence` 只接受 `once` 或 `daily`。
- `once` 的 `day_count` 省略时按 1；显式提供时也必须为 1。
- `daily` 的 `day_count` 必填语义为正整数，并且不超过配置的 `max_scheduled_message_days`。
- `message` 沿用普通 message 的文本约束；空字符串的处理沿用普通 message，不在定时路径另造语义。
- `photo` 沿用普通 message 的摄像头索引列表语义；照片在 dispatch 时捕获，不在 start 时捕获。
- `timezone` 必须是 backend `zoneinfo` 可解析的 IANA timezone，不能接受任意 UTC offset 字符串或主机本地时区别名。
- `scheduled_messages` 只允许顶层数组；递归 `CycleStepRequest` 的 `type` 仍只允许 `wait`、`message`、`cycle`。

Start 必须在创建 producer 前完成所有静态校验，包括 timezone、时间格式、重复模式、天数上限、最大定时窗口和 request fingerprint。任何一项失败都返回 4xx，不得启动半个 run。

### 5.2 Cycle config

在既有 `cycle` 配置中增加配置化字段，名称和默认值应由 `backend/config.py` 建模，并通过 `/config` 暴露给前端：

```yaml
cycle:
  enabled: true
  max_depth: 5
  max_duration: 36000
  show_thumbnails: true
  timeout_policy: exempt_cycle_run
  scheduled_track_max_duration_seconds: 2592000
  max_scheduled_message_days: 30
  event_journal_retention_seconds: 86400
  event_journal_max_records: 20000
  default_sequence: []
  default_scheduled_messages: []
```

字段语义：

- `timeout_policy=exempt_cycle_run` 是 Cycle 的默认特殊策略；`legacy_bounded` 仅用于明确要求旧 `max_duration` 行为的部署。
- `max_duration` 在 `legacy_bounded` 下保持旧语义；在 `exempt_cycle_run` 下不能成为 24 小时任务的隐式总时长杀手。普通轨道依靠有限步骤、单次操作 timeout 和显式 Stop 结束。
- `scheduled_track_max_duration_seconds` 是定时轨道的有限生命周期窗口，从 Cycle run start 计算；它不是 frontend/SSE idle timeout。定时 horizon 超出此窗口的 request 在 start 时拒绝，不能静默减少 day count。
- `max_scheduled_message_days` 是每条 daily 项的输入上限，不能用无限大或负数表达“无限”。
- `event_journal_retention_seconds` 和 `event_journal_max_records` 控制断线恢复资源；达到上限时应保留 terminal、最近的 canonical records 和可解释的 truncation record。
- `default_scheduled_messages` 只用于配置驱动的编辑器默认值，默认空数组，不改变现有默认普通序列。

如果部署需要超过上述建议默认窗口，必须修改 YAML，而不是在 shell、前端或 executor 中硬编码更大值。

## 6. Backend 执行架构

### 6.1 Run registry、producer 和 journal

`CycleExecutor` 继续作为 Cycle domain executor，但其生命周期拆成三个边界：

1. `CycleRunRegistry`/等价的 run store：按 Cycle run identity 保存 request fingerprint、固定 timezone、两条轨道快照、状态、occurrence state、当前 dispatch 和取消句柄。
2. 独立 producer：创建后使用 `asyncio.create_task` 或等价后台机制运行，不把 producer 放在 `StreamingResponse` generator 的 finally 中。subscriber 关闭不能取消 producer。
3. Cycle event hub/journal：使用现有 Session Hub 的 append-only、cursor、heartbeat 和 subscriber 机制；如需扩展 `SessionKind`，增加 `cycle` 领域，但不要把 Cycle 事件写入 Agent prompt 或 Agent memory。

`POST /v1/cycle/start` 的首个连接可以继续返回 SSE 以兼容现有客户端，但其职责变为：完成 admission、确保唯一 producer、订阅该 run 的 journal/live events。另增恢复入口：

```text
GET /v1/cycle/{cycle_run_id}
GET /v1/cycle/{cycle_run_id}/events?after_sequence=<n>
POST /v1/cycle/stop
```

同一个 active run 的重复 start/attach 必须使用 request fingerprint 做幂等判断：相同配置只附着已有 producer，不重复启动；不同配置返回 409。只允许精确的 Cycle run ID 解析，不能把它当作文件路径、Agent session 或任意 legacy token。

### 6.2 Journal retention 和 live delta 优化

为避免 24 小时以上任务因为每个 token delta 和每张 base64 图片无限堆积，事件分为两类：

- canonical replay records：`cycle_started`、`step_begin`、`photo_captured` 的轻量元数据、`llm_done`、`step_end`、queue/failure/terminal 等，用于刷新后重建聊天和 progress。
- live deltas：`llm_chunk`/思考增量仅用于当前 subscriber 的实时显示；重新连接不要求重放已经完成的每个 token，只要能从 canonical `llm_done` 继续一致地显示即可。

照片要遵守现有 `show_thumbnails` 与图片尺寸限制；journal 中不能无限保留重复大图。实现应优先使用 bounded asset/reference 或受 retention/record cap 约束的缩略图记录。发生裁剪必须发出可恢复 UI 能理解的 truncation record，不能假装所有历史图片仍然存在。

这一优化不改变 LLM 输入：真实 dispatch 仍把当前拍摄图片传给 LLM；journal 只是 UI/recovery 数据。

### 6.3 Scheduler clock 和 due queue

每个 Cycle run 最多一个 scheduler clock task，未来 occurrences 存为有限的 next-occurrence 状态，不为每个日期创建一个 `sleep` task。clock 唤醒时：

1. 根据固定 timezone 和当前 wall clock 计算所有应到期 occurrence。
2. 以 occurrence key（`schedule_id`, `occurrence_index`, `scheduled_local_date`）做幂等检查。
3. 向 journal/live subscribers 发布 `scheduled_message_queued`，然后放入 due queue。
4. 唤醒 dispatch arbiter；若当前动作仍在执行，只等待 arbiter 的下一次 drain。

普通轨道和 scheduler clock 不共享一个全局锁。只有实际 dispatch 使用该 Cycle run 的 arbiter/lock，避免不同 schedule item 被不必要地串行化；同一 run 的真实发送仍然严格串行。

### 6.4 Dispatch 适配

现有 `_execute_message()` 应抽取为普通 message 和定时 message 共用的 dispatch 入口，入口参数包含 `track`、`schedule_id`、`occurrence_index` 和 optional `path`。该入口继续执行：

1. dispatch 时拍照并发布 `photo_captured`。
2. 根据 `share_context` 解析当前 Agent session identity；不得把 Cycle run ID 当作 Agent session ID。
3. 通过现有 Agent admission、`run_stream`、tool configuration 和 Stop 生命周期发送。
4. 把用户文字和真实图片按普通 message 的 canonical 输入写入 Agent/Chat 流程；调度元数据只在 timeline event 中出现。
5. 对 output 发布 live deltas 和 canonical `llm_done`。

Cycle agent path 必须继续遵守 Prefix-Cache Covenant：定时字段、queue 状态、scheduled_for、lateness 和 UI journal 不进入 system message、历史中部或上下文摘要；它们也不能作为普通用户文字拼进 LLM prompt。

## 7. Timeline/SSE 事件契约

所有 Cycle 业务事件通过一个按 sequence 排序的 timeline/journal 发送。现有事件保持兼容，并增加 `track` 和关联字段；事件不得另造一套与 Agent timeline 无关的模型输入协议。

新增或扩展事件至少包括：

```json
{
  "type": "scheduled_message_queued",
  "cycle_run_id": "...",
  "schedule_id": "schedule-1",
  "occurrence_index": 1,
  "scheduled_for": "2026-09-18T14:30:00+08:00",
  "queued_at": "2026-09-18T14:35:12+08:00",
  "reason": "ordinary_dispatch_busy"
}
```

`step_begin`、`photo_captured`、`llm_done`、`step_end` 对定时轨道增加：

- `track: "scheduled"`；普通轨道为 `track: "ordinary"`。
- `schedule_id`、`occurrence_index`、`scheduled_for`，仅定时轨道提供。
- `lateness_seconds` 在实际 dispatch/step end 事件中提供。

定时 message 真正开始 dispatch 时发布 `step_begin`，前端只能在这个事件时创建用户消息气泡；`scheduled_message_queued` 只更新 Cycle progress 的待处理状态，不能提前伪造用户发送。

单次 occurrence timeout/failure 发布结构化失败事件，包含 schedule identity、occurrence index、错误分类和是否继续。Cycle terminal event 必须区分 `completed`、`stopped`、`expired`、`error`，其中明确配置的定时窗口结束使用 `expired`，不能误报成浏览器 idle timeout。

## 8. Cycle 专用 timeout policy

实现前必须沿 Cycle path 审计下列 timeout，并在代码中让 Cycle scope 可诊断：

| Scope | Cycle 行为 |
| --- | --- |
| Cycle run 总时长/旧 `max_duration` | `exempt_cycle_run` 下不作为隐式终止条件；普通轨道由有限步骤和显式 Stop 结束。定时轨道受 `scheduled_track_max_duration_seconds` 明确约束。 |
| frontend Cycle SSE idle timeout | Cycle subscriber 使用 heartbeat；不能因为没有业务事件而 abort。现有 `cycle_stream_idle_timeout_seconds` 不能覆盖这一豁免。 |
| backend HTTP/StreamingResponse 生命周期 | producer 独立于原始请求；SSE detach 只移除 subscriber。 |
| Agent session TTL cleanup | active Cycle run 持有的 Agent sessions 受保护；`share_context=false` 创建的多个 session 也逐个受保护。 |
| 单次 LLM HTTP timeout | 仍保留 provider transport 安全边界；timeout 被收敛为本次 dispatch failure，不能杀掉整个 Cycle。若要使用 Cycle-specific provider timeout，必须通过配置/显式 scope 传递。 |
| 单次工具、摄像头、子 Agent/coordinator timeout | 仍是单次操作边界；失败记录进入该 step/occurrence，arbiter 释放后继续后续可执行任务。 |
| Stop/disposal barrier | Stop 仍可取消当前 Cycle；必须等待 Cycle-owned agent/request cleanup 完成后才释放 run resources，避免旧 worker 影响新 run。 |

因此，“豁免”不是把所有 timeout 改成无限，而是阻止 subscriber/run/session 层 timeout 错误终止 Cycle，并把单次 operation timeout 限定在单次动作。所有 timeout 记录必须带 `scope` 和 `cycle_run_id`，便于诊断到底是哪一层触发。

## 9. 断线恢复与持久化边界

本规格中的持久化是 backend 进程生命周期内的 run record + event journal 持久化，不是跨 backend 重启的数据库持久化。

### 9.1 Browser refresh/SSE disconnect

- 前端在 start admission 前保存 `cycle_run_id`、模式（text/vision）和最后已消费的 event sequence；不保存 Agent prompt、tool trace 或图片作为恢复上下文。
- 页面加载后，如果 localStorage 有 active Cycle run，调用 status endpoint；状态为 active 时按 cursor 订阅 events；从 0 或保存 cursor 开始重放，再跟随新事件。
- 重新连接使用统一的 Cycle timeline projection 重建普通用户气泡、定时用户气泡、助手 `llm_done`、照片缩略图和 progress；不把 frontend bubble 或 journal 内容发送回 LLM。
- 允许多个 subscriber 观察同一个 producer，但只有一个 producer/arbiter；恢复订阅不能重复发送 occurrence。
- subscriber 在业务事件之间收到 heartbeat 时刷新 transport activity；heartbeat 不占用 journal sequence、不进入 timeline/projection、不成为前端业务协议。

### 9.2 Backend restart

- backend 重启时不加载旧 Cycle run，不恢复 scheduler clock，不继续执行 occurrence。
- 前端 status/events 得到 404/unavailable 后清除 active run 标记，并显示“Cycle 已因 backend 重启失效”；不能将其当作普通网络重连无限重试。
- backend 重启后使用同一个旧 ID 重新 Start 时，视为新 run，不能复用旧 journal 或旧 Agent session。

## 10. Frontend 适配与样式

### 10.1 共享类型和两页同步

更新 `frontend/src/lib/cycle-types.ts`：

- 增加 `CycleScheduledMessage`、`CycleRecurrence`、`scheduled_messages`、`timezone`、新事件字段和 recovery status 类型。
- `CycleEditorState` 增加 scheduled message 列表和 schema version；读取旧 localStorage 时无该字段按空数组迁移。
- `CycleStartRequest` 显式区分 `steps` 与 `scheduled_messages`，不能依赖 object spread 自动把 React state 翻译成 wire format。

更新共享 `CycleEditor`/`CycleStepEditor`/`CycleProgress`/`ImageUploader`：

- 普通轨道继续递归编辑；定时轨道使用独立的 top-level editor。
- Cycle progress 显示 queued、next scheduled time、sent count、failed count、lateness 和 terminal reason。
- text 与 vision 两页的 start callback、event handler、用户气泡 projection、cycle recovery 和 Stop 行为必须保持等价；vision 额外负责摄像头资源释放，不把 browser-local Vision Automation 状态写入 Cycle run。

更新 `frontend/src/lib/api.ts`：

- start request 显式附带 `cycle_run_id` 和 `timezone`。
- 增加 status、events/cursor attach、stop 的恢复 API。
- Cycle 连接的 heartbeat 和 detached producer 不得被 `createInactivityTimeout` 误 abort；heartbeat 只刷新 transport，不产生业务消息。

### 10.2 Cycle 画布透明度和浅色模式

Cycle 画布必须使用独立的 `.cycle-canvas`/等价 class 和 CSS variables：

- 不读取 `--webui-footer-bg-alpha`，不使用 `webui-zone-footer-bg` 的 alpha 作为自己的透明度。
- 背景使用自己的低透明度（即高不透明度）稳定面板色；推荐浅色模式 `rgb(255 255 255 / 0.94)`，深色模式 `rgb(17 24 39 / 0.94)`，实际值必须集中在 Cycle 样式变量中。
- 不对整个画布使用 CSS `opacity`，避免文字、边框和控件一起变淡；只调整背景色 alpha。
- 浅色模式使用深色标题/正文/label、白色或近白色输入背景、`gray-300/400` 级别边框和明确 focus ring；深色模式使用当前深色控件和浅色文字。
- 普通轨道的深浅层背景、定时轨道边框、progress 状态色都必须在 light/dark 下有足够对比度。
- 画布设置 `isolation: isolate` 和适当 `color-scheme`，避免页脚的玻璃层或父级透明度改变文字可读性。

## 11. 错误、取消和兼容性

- Start validation 失败：返回 400/422，含字段路径和稳定错误 code，例如 `invalid_cycle_time`、`invalid_cycle_timezone`、`scheduled_day_count_exceeded`、`scheduled_horizon_exceeded`。
- 同 run ID 不同 request fingerprint：返回 409；相同 fingerprint：attach，不新建 producer。
- 摄像头单项失败：沿用普通 message 的 `photo_captured` error 行为，继续把没有成功图片的 message 交给现有 dispatch policy。
- 单次 dispatch timeout/error：发布 failure、释放 admission/run scope、继续仲裁；未要求的普通行为不应借此改变 Agent prompt 或 tool traces。
- 用户 Stop：取消 clock、标记 run stopped、清空未执行 due queue、定向终止当前 Agent/request、等待 cleanup barrier、发布 terminal event；不使用 abort-all 误伤其他 session。
- SSE 断开：只取消 subscriber，不调用 Cycle stop，不清空 queue，不释放 active Agent sessions。
- 已完成 run：在 retention 期内允许恢复查看 terminal journal；超过 retention 返回 expired。backend restart 后无论 retention 如何都返回 unavailable。
- 旧客户端只提交 `steps`/`session_id` 时，仍按普通轨道兼容运行；`scheduled_messages` 默认空数组，`session_id` 作为 Cycle run identity 使用，但响应中要明确它不是 Agent session。

## 12. 测试与验收标准

### 12.1 Backend unit tests

覆盖 scheduler/queue 的 fake clock 行为：

1. 严格接受/拒绝 `HH:mm:ss`、IANA timezone、`once`/`daily` 和 day count。
2. start 恰好在时刻、晚于时刻、跨午夜时的首次 occurrence。
3. daily N 次只生成 N 个 occurrence；once 永不重复；同一 occurrence 重入 clock 不重复入队。
4. occurrence 在普通 wait、普通 Agent stream、定时 Agent stream 期间到期后入队。
5. 普通动作完成后定时 queue 立即 drain；普通 message 边界重合时定时先执行。
6. 多条定时项和多次 occurrence 的排序稳定；真实 dispatch 串行且不存在并发 LLM 调用。
7. 普通轨道先结束、定时轨道仍有未来 occurrence 时 run 保持 active；两轨道都完成才 terminal。
8. 定时 horizon、day count、Stop、单次 timeout/failure 和不可恢复 validation error 的状态转换。
9. `share_context=true/false` 下 Cycle run identity 与 Agent session identity 分离，且不假设只有一个 Agent session。

### 12.2 Recovery/lifecycle integration tests

1. start 后关闭首个 subscriber，producer 继续执行，occurrence 仍按时入队和 dispatch。
2. 新 subscriber 使用 `after_sequence` 能重放 canonical records 并跟随后续事件；不能重复发送 occurrence。
3. heartbeat 不写 journal、不增加 event sequence、不触发业务 projection。
4. subscriber detach 不触发 `finish_run`、Agent TTL disposal 或 Cycle stop。
5. Stop 等待当前 worker cleanup，旧 worker 不能修改新同 ID run。
6. 模拟 backend restart/空 registry 返回 unavailable，前端不自动恢复或继续发送。
7. journal 达到 record/retention 上限时保留 terminal 和 truncation record，恢复 UI 能显示历史被裁剪而不是静默丢失。

### 12.3 Timeout tests

覆盖 Cycle scope 下的 run-level、frontend idle、backend subscriber、Agent TTL、单次 LLM/tool timeout：

- 24 小时以上的 fake wall-clock gap 不产生 generic timeout error。
- 没有业务事件但持续收到 heartbeat 时，Cycle subscriber 不 abort。
- 单次 operation timeout 只失败当前 dispatch，后续普通/定时任务仍能继续。
- 明确达到 `scheduled_track_max_duration_seconds` 时产生 `expired` terminal，而不是误报浏览器 idle timeout。

### 12.4 Frontend tests

- 旧 Cycle editor localStorage 可迁移到含空 scheduled list 的新 state。
- `CycleStartRequest` 显式携带 `scheduled_messages`、`cycle_run_id` 和浏览器 IANA timezone。
- 定时 editor 只在顶层出现，nested editor 不出现 scheduled option；daily day count 按项保存。
- queued event 不创建用户气泡，scheduled `step_begin` 创建一次气泡，重放同一 sequence 不重复。
- text/vision Cycle start、event projection、recovery 和 stop glue 保持同步；vision 资源释放仍受现有 lifecycle guard 约束。
- light/dark 样式源码或浏览器验收确认 Cycle canvas 不引用页脚 alpha，文字和输入控件在浅色背景上有明确对比。
- 运行 frontend type-check 和现有 Cycle/API 测试。

## 13. 目标文件与实现边界

预期修改点如下，实施时以实际代码结构为准：

- `backend/cycle/models.py`：请求/响应与定时 message 校验模型。
- `backend/cycle/executor.py`：run registry 接入、scheduler clock、due queue、dispatch arbiter、timeout scope 和 terminal 生命周期。
- `backend/api/cycle.py`：start attach、status、cursor events、stop 与稳定错误响应。
- `backend/session_hub_runtime.py`、`backend/agent/session_hub.py`：若复用现有 Hub，扩展 Cycle kind、canonical replay/live delta、retention 或精确 release 逻辑。
- `backend/config.py`、`config/config-misc.yaml`、`config/config-server.yaml`：Cycle timeout、scheduled horizon、day count、journal 和 heartbeat 配置链路。
- `backend/main.py`：Cycle run registry/Hub/producer 生命周期 wiring，确保 producer 不依赖请求 generator。
- `frontend/src/lib/cycle-types.ts`、`frontend/src/lib/api.ts`：wire 类型、timezone、attach/recovery 和 SSE 事件解析。
- `frontend/src/components/CycleEditor.tsx`、`CycleStepEditor.tsx`、`CycleProgress.tsx`、`ImageUploader.tsx`：两轨道编辑和 progress。
- `frontend/src/app/page.tsx`、`frontend/src/app/vision/page.tsx`：两套 Cycle start/stop、timeline projection、bubble/recovery glue 同步修改。
- `frontend/src/app/globals.css`：Cycle canvas 独立视觉变量、浅色高对比和页脚透明度隔离。
- `tests/test_cycle_executor.py`、`tests/test_cycle_config.py`、`tests/test_cycle_api.py` 以及新增 scheduler/recovery/frontend tests：锁定本文的时序、恢复、timeout 和样式契约。

实现不得通过修改 smolagents 依赖源码、向普通 prompt 添加调度控制 clause、或在前端创建独立 timer 发送路径来绕过上述边界。
