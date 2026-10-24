# 视觉自动化 PRD / Design Spec

**日期：** 2026-08-09  
**状态：** 已完成产品与设计评审，等待 implementation plan  
**目标页面：** `frontend/src/app/vision/page.tsx` (`/vision`)  
**目标平台：** Chromium 浏览器 + NVIDIA Jetson Thor 后端 + SGLang/vLLM 视觉模型

## 1. 摘要

在 Vision 页面的 `+` 操作区新增“视觉自动化”，并将现有“摄像头拍照”重命名为“手动拍照”。视觉自动化允许用户显式选择最多一个浏览器桌面共享源和多个浏览器摄像头，以统一、同步的 tick 采集多源画面。系统不保存所有历史帧，而是以完整 tick bundle 为单位执行有界采样：容量 `k>=2` 且字节预算允许时保留首个和最新 bundle，并随机淘汰中间 bundle；`k=1` 或字节预算只能容纳一个 bundle 时优先保留最新 bundle。

视觉自动化从 `Trigger Start` 开始持续 armed，跨越多次消息发送。每次发送冻结当前采集周期；根据 `capture_during_inference`，系统在消息提交后立即开始下一周期，或在模型/Agent 到达真实稳定终态后再开始。只有 `Abort`、页面离开或等价清理操作才永久解除 armed。采集期间面板可以折叠，`+` 按钮右上角持续显示状态环。

单条消息的真实图片数量由代码硬限制为 250，图片上传和手动拍照合计仍最多 9 张。自动化图片和手动图片共同受 250 张及 64 MiB Base64/Data URL 编码后图片预算约束。

为避免多轮自动化快速耗尽多模态上下文，本功能引入一项严格限定的 Prefix-Cache Covenant 例外：视觉自动化 armed 期间，以及 Abort 后的一次收尾发送中，每个新 turn 可以把更早 turns 的真实图片块永久替换为确定性汇总占位符。该例外只允许替换图片，不允许修改历史角色、文字、顺序、assistant 输出、tool calls 或 observations。它不关闭 auto-compact，也不改变 manual compact、cache-first、fallback 或 post-compact 行为。

## 2. 背景与现状

当前 `/vision` 页面已有三种 `+` 操作：上传图片、摄像头拍照、编程循环。浏览器摄像头组件已经支持权限获取、`enumerateDevices()`、多摄像头预览和单帧手动拍照，但没有桌面共享、连续同步采集、有界采样或跨消息自动重启。

当前图片链路大致为：浏览器选择或拍摄图片，调用 `/v1/process-image-binary`，后端按视觉配置压缩后返回 Data URL，前端再通过普通 Chat 或 Agent API 发送。Chat 和 Agent 使用不同 transport，但最终都把图片送入本地 VLM。

现状中的关键约束如下：

- 浏览器摄像头属于访问网页的客户端设备；Cycle 的 `/dev/video*` 摄像头属于 Jetson 后端设备。二者不是同一设备域。
- 仓库当前没有 `getDisplayMedia()` 桌面共享实现。
- 前端手动图片总数硬编码为 9，Backend 没有统一的 250 张或总字节校验。
- Next.js 同源代理会完整缓冲请求体；Base64 还会带来约 33% 膨胀。
- Agent canonical memory 会保留 task images；Chat 历史也可能继续携带旧图片。
- 当前 active vLLM infer 配置显式设置 `limit_mm_per_prompt.image = 1`，与多帧目标冲突。
- Prefix-cache 公约默认禁止任意改写历史 payload，因此视觉自动化的图片淘汰必须被精确记录为局部例外。

## 3. 产品目标

- 在 `/vision` 提供桌面共享和多浏览器摄像头的同步多帧采集。
- 使用有界 tick bundle 采样，运行时间不影响内存上限。
- 保留每个周期的首个 tick、最新 tick 和随机选中的中间 tick。
- 多源共享完全相同的保留 tick 集合。
- 每次发送自动切分采集周期，并在未来自动开始下一周期，直到 Abort。
- 支持配置模型运行期间是否继续采集，默认关闭。
- 复用现有视觉尺寸、格式、压缩、Base64 和 Chat/Agent 发送链路。
- 允许用户折叠运行中的面板，并从 `+` 状态环明确看见后台采样任务状态。
- 严格隔离不同 LLM/Agent session、不同 child-agent session、不同标签页和不同页面代次。
- 及时释放被淘汰、Abort、历史占位或页面离开后不再需要的图片资源。
- 保持现有 auto-compact、manual compact 和 Prefix-Cache Covenant 的其余规则。

## 4. 非目标

- 不支持多个桌面/窗口/标签页共享源并行采集。
- 不采集 Jetson 后端 `/dev/video*` 摄像头。
- 不录制或上传音频。
- 不生成视频文件。
- 不保存所有捕获帧。
- 不在页面刷新、关闭或跨页面导航后恢复 armed 状态或帧。
- 不让视觉自动化与编程循环同时运行。
- 不允许模型忙碌时发送或排队新的自动化大图消息。
- 不引入新的 Agent timeline event 类型；请求接受和稳定终态继续使用 provider-neutral transport lifecycle，不把浏览器采样状态写入 timeline。
- 不为视觉自动化关闭 auto-compact。
- 不把历史图片例外扩展到普通 Text、普通 Vision、Cycle、历史文本或工具轨迹。
- 首版不支持人工逐帧编辑自动化样本；采样和淘汰以完整 tick bundle 为单位。

## 5. 强制术语

| 术语 | 含义 |
|---|---|
| `session` | LLM/Agent context window，或 manager 创建的 child-agent context |
| 视觉自动化采样任务 | 从 `Trigger Start` 到 `Abort` 的浏览器本地自动化生命周期 |
| 采集周期 | 从一次新的 T=0 到下一次发送边界 |
| tick | 一次同步采集时刻 |
| tick bundle | 同一 tick 中所有活动视觉源的不可拆分图片集合 |
| request | 单次 Chat、Agent、compact、Stop 或 Answer Now 请求/推理生命周期 |
| stable terminal state | 相关前后端请求和 worker 已真正完成清理的稳定终态 |

禁止把视觉自动化采样任务称为 automation session。`session_id` 只能指现有 LLM/Agent 上下文标识。

## 6. 产品入口与功能边界

`+` 操作区按以下顺序显示：

1. 上传图片
2. 手动拍照
3. 视觉自动化
4. 编程循环，仅 Cycle 配置启用时显示

“摄像头拍照”必须在所有相关 Vision UI 文案和测试中改为“手动拍照”。视觉自动化只在 `model_type: vision` 且 `vision_automation.enabled=true` 时显示。

视觉自动化与编程循环互斥。任一功能处于运行状态时，另一项显示为禁用；系统不得为了启动另一项而静默 Stop 或 Abort 当前任务。

视觉自动化采集浏览器所在设备的摄像头，不采集 Backend/Jetson 摄像头。桌面共享由 `navigator.mediaDevices.getDisplayMedia()` 提供，最多一个；摄像头由 `getUserMedia()` 提供，可多选。所有请求必须设置 `audio: false`。

## 7. 总体架构

采用“浏览器持有采样状态机和有界帧池，后端提供配置、验证和推理”的架构。

| 单元 | 职责 |
|---|---|
| `VisionAutomationPanel` | 内联配置、源选择、Start/Abort、折叠、状态和缩略图矩阵 |
| `CaptureSource` | 统一抽象桌面和摄像头的启动、同步取帧、健康状态和释放 |
| `TickCoordinator` | 全局 tick、并行采集、同步 barrier、超时、失败计数和原子提交 |
| `TickBundleSampler` | 以完整 bundle 为单位执行有界随机采样和动态缩容 |
| 采样任务状态机 | 管理 armed、capture cycle、等待模型、源异常和 Abort |
| 图片资源层 | 管理上传版 Blob、缩略图、Object URL、Base64 和幂等清理 |
| Wire serializer | 图片排序、Base64、manifest、历史占位和 Chat/Agent 显式翻译 |
| Backend config/validation | YAML 默认值、`/config`、数量/字节/格式/尺寸校验 |

浏览器拥有 MediaStream、媒体权限、当前采样池和 localStorage 配置。Backend 不维护视觉自动化采样任务，不缓存连续 tick，只接收发送边界冻结后的最终图片。

采集、消息发送和 UI 状态不得继续全部堆入现有大型 `vision/page.tsx` 或 `ImageUploader.tsx`。实现计划应给这些职责建立可独立理解和测试的边界，但不做与本功能无关的重构。

## 8. 配置

新增 `config/config-vision.yaml`：

```yaml
vision_automation:
  enabled: true
  interval_seconds: 1.0
  max_frames_per_source: 16
  thumbnail_size_px: 32
  capture_during_inference: false
  tick_timeout_seconds: 10
  consecutive_tick_failure_limit: 3
  max_encoded_payload_mib: 64
  initial_jpeg_quality: 85
  minimum_jpeg_quality: 40
  minimum_image_dimension_px: 256
  base64_encode_concurrency: 4
  max_request_body_mib: 72
  request_status_ttl_seconds: 600
```

`config/config-overall.yaml` 使用 `vision` 路由引用独立的 `config/config-vision.yaml` 文件。Backend loader、AppConfig 和 `/config` 必须显式接入该配置。`/config` 在现有 `vision` 节点下返回 `automation`。

| 参数 | 默认值 | 约束 |
|---|---:|---|
| `enabled` | true | Backend/YAML 功能开关，不允许 localStorage 越权启用 |
| `interval_seconds` | 1.0 | 最小 0.1，无必要最大值 |
| `max_frames_per_source` | 16 | 1 到 250，250 为代码硬上限 |
| `thumbnail_size_px` | 32 | 16 到 128，128 为代码硬上限 |
| `capture_during_inference` | false | 布尔值 |
| `tick_timeout_seconds` | 10 | 最小 1 |
| `consecutive_tick_failure_limit` | 3 | 最小 1 |
| `max_encoded_payload_mib` | 64 | 浏览器可选择更低值，但不得高于 Backend YAML 上限 |
| `initial_jpeg_quality` | 85 | 40 到 95，server-controlled advanced default |
| `minimum_jpeg_quality` | 40 | 1 到 initial quality，server-controlled advanced floor |
| `minimum_image_dimension_px` | 256 | 64 到现有 `max_image_dimension`，server-controlled advanced floor |
| `base64_encode_concurrency` | 4 | 1 到 8，server-controlled advanced limit |
| `max_request_body_mib` | 72 | 必须大于图片预算，server-controlled proxy/backend ceiling |
| `request_status_ttl_seconds` | 600 | stable terminal 后的有界幂等/状态查询保留时间 |

以下限制必须写入程序代码并由前后端共同校验，不能只依赖 YAML、localStorage 或 HTML input：

- 单条消息真实图片总数最大 250。
- 每源配置 `n` 最大 250。
- 手动图片最大 9。
- 缩略图最大 128 px。
- 采集间隔最小 0.1 秒。

### 8.1 浏览器配置持久化

- 使用带 schema version 的专用 localStorage key。
- 首次加载且无本地数据时采用 `/config` 返回的 YAML 默认值。
- 用户在 UI 中修改有效参数后立即写入 localStorage。
- 后续加载优先使用有效本地参数。
- 非法 JSON、版本不兼容、字段缺失或越界时丢弃整份本地配置并回退 YAML。
- 不提供“恢复 YAML 默认值”按钮。
- 用户清除浏览器站点数据后自然恢复 YAML 默认值。
- 不持久化摄像头 deviceId、桌面选择、图片、Blob、运行状态或权限状态。
- Trigger Start 后所有配置和源选择锁定，直到 Abort。
- UI/localStorage 允许覆盖常规采样参数；上述 server-controlled advanced 字段只由 YAML 和 `/config` 提供，浏览器不得把它们提高到 Backend ceiling 以上。

## 9. UI / UX

采用输入区内联面板。点击“视觉自动化”展开源选择、配置和控制。面板处于 armed、等待模型或源异常时均允许折叠；折叠只改变展示，不改变 MediaStream、tick 或自动重启。

### 9.1 启动前

面板显示：

- 一个桌面共享选项。
- 浏览器摄像头列表和刷新设备按钮。
- 当前视觉自动化配置。
- 安全上下文、权限和设备错误。
- `Trigger Start`。

至少选择一个视觉源后才能 Start。桌面共享 chooser 必须由 Start 的直接用户手势触发。页面不得在加载时根据 localStorage 自动启动采集或自动弹权限框。

### 9.2 运行中

面板显示：

- 当前状态：采集中、等待模型或等待视觉源。
- tick 尝试数、成功数和失败数。
- 当前图片数量及 250 上限。
- 预计编码字节数及 64 MiB 上限。
- 各活动源健康状态。
- 按 tick 列对齐的缩略图矩阵。
- 一条所有源共享的横向拖拽 bar。
- `Abort`。

每行对应一个视觉源，每列对应一个完整 tick bundle。桌面源排第一行，摄像头按启动时稳定顺序排列。随机删除一个 tick 时整列消失；新 tick 始终追加在最右侧。

所有行只使用一条总拖拽 bar，始终保持相同 tick 列对齐。默认跟随最新 tick；用户主动向左拖动后暂停自动跟随，并提供“回到最新”。触屏设备支持横向拖动。

点击缩略图打开上传版压缩图。放大层显示源别名、内部 tick index 和相对 T=0 时间。大图关闭后释放额外解码资源，但不删除采样池中的上传 Blob。

### 9.3 折叠指示器

Trigger Start 后，只要采样任务仍 armed，`+` 按钮右上角持续显示状态环：

- 蓝色旋转：正在采集。
- 灰色旋转：等待模型稳定终态。
- 琥珀色旋转：活动源异常，全组暂停。

Tooltip 和无障碍文本必须明确说明状态，不能只依赖颜色。`prefers-reduced-motion` 下使用静态状态环。

## 10. 采样任务与采集周期生命周期

### 10.1 状态

| 状态 | 行为 |
|---|---|
| `idle` | 未启动，无媒体流、定时器或自动帧 |
| `starting` | 获取权限、启动源并准备第一个同步 tick |
| `capturing` | 按全局 tick 持续采集 |
| `freezing` | 发送触发，停止新 tick 并冻结当前批次 |
| `waiting_for_model` | 开关关闭时保留 streams 但暂停取帧 |
| `capturing_during_model` | 开关开启时为下一条消息持续采集 |
| `waiting_for_sources` | 初次没有可用源，保持 armed 并等待显式 Retry Sources |
| `paused_source_error` | 活动源断开或连续失败，全组暂停 |
| `retryable_submit_error` | 提交前校验、编码或 transport 失败，冻结批次等待 Retry/Abort |
| `aborting` | 取消工作并释放资源 |

### 10.2 Trigger Start

1. 读取并冻结有效浏览器配置。
2. 锁定 Chat/Agent 模式、源选择和配置字段。
3. 启动桌面共享与所选摄像头。
4. 初次启动失败的源显示独立错误，并从本采样任务的活动同步组中排除。
5. 初次成功的源形成不可变活动组；失败源不能在当前任务中途加入，必须 Abort 后重新 Start。
6. 如果全部源失败，进入 `waiting_for_sources`，显示显式 `Retry Sources`。该按钮通过一次直接用户手势重新尝试全部原选中源；本次 attempt 的全部成功源共同建立不可变 cohort。若仍全部失败，可再次 Retry。
7. 活动组建立后立即创建新的采集周期，并执行 T=0。

运行中活动源故障时使用 `Retry Failed Source` 恢复同一冻结逻辑源；桌面 retry 按钮必须直接触发新的 chooser。运行中的失败源不能被其他新源替换，新增或更换源必须 Abort 后重新 Start。

### 10.3 发送

模型空闲时点击发送：

1. 禁止新 tick。
2. 等待当前 in-flight tick；超时或部分失败时整组舍弃。
3. 冻结采样池和手动图片。
4. 执行图片数、字节预算、压缩、manifest 和身份校验。
5. Staged 构建历史图片 omission，不修改 canonical history。
6. 使用唯一 request ID 原子提交 omission 与当前 Chat/Agent turn。
7. Backend acceptance 后结束当前采集周期并提交 UI omission，但保持采样任务 armed。

`capture_during_inference=true` 时，请求成功提交后立即建立新的 T=0，并在模型推理、Agent routing、handoff、tool step 和其他中间 step 期间采集。到达 final answer 时不重置这一周期。

`capture_during_inference=false` 时，模型忙碌期间不能新建视觉自动化采样任务；已 armed 的任务保持 streams 打开但停止取帧。普通 Chat 的逻辑完成或 Agent 产生 final answer 只表明候选终态；只有 Backend 明确确认绑定 request 的 worker/run scope 已全部退出后，前端才立即建立新的 T=0。

`capture_during_inference=true` 时，请求提交后建立的周期跨 final answer、模型错误、Stop 和 Answer Now 持续存在；任何 terminal callback 都不得重置、清空或重复建立该周期。只有发送下一条消息、Abort、context identity 变化或源错误状态转换能结束或暂停它。

模型忙碌期间不能发送自动化图片消息，也不能把自动化大图批次加入现有消息队列。

### 10.4 稳定终态

关闭运行期采集时，以下情况都必须等待相关请求/worker 真正完成清理后才能重启周期：

- Chat 正常完成。
- Agent final answer。
- 请求错误。
- Stop 后旧 worker 完成退出。
- Answer Now replacement 完成。
- SSE/网络断开后 Backend worker 完成。

Agent 中间 step、handoff、tool observation、coordinator 输出、final-answer 文本到达和仅仅关闭前端流都不是 worker 已退出的证明。Backend 的正常 stream completion acknowledgment 必须在对应 worker cleanup 和 run-scope release 后产生。若网络断开导致前端无法确认稳定终态，采样任务保持 `waiting_for_model`，不得猜测重启；UI 提供 Retry Status/Abort，或等待现有 provider-neutral lifecycle 查询确认。原始 SSE EOF 本身不构成稳定终态。

### 10.5 Abort

`Abort` 是唯一永久解除 armed 和自动重启的 UI 控制：

- 立即停止新 tick。
- 废弃未提交的 in-flight bundle。
- 丢弃当前全部自动化帧。
- 保留上传图片和手动拍照产生的手动图片。
- 释放 automation-owned 的 camera/display tracks、定时器、in-flight/current-cycle Blob、Object URL、Base64、缩略图和异步引用。
- 不释放手动图片，也不释放已经提交且仍被 canonical/UI history 引用的图片。已提交图片在同一绑定 context 的历史 omission 成功提交时释放；clear、页面离开或 context 销毁时可以直接释放整个被销毁 context 的 UI 资源。
- 不停止已经提交的模型请求；Stop/Answer Now 使用现有控制。

刷新页面、关闭标签页、离开 `/vision`、清空对话或切换 context identity 必须执行 Abort 级清理。

## 11. 同步 Tick

每个 tick 同时触发活动组的全部源。各源可并行截图和压缩，但必须通过同步 barrier 原子提交。任一源失败或超时，整个 bundle 舍弃。

这是浏览器软件同步，不承诺硬件级同步。每个 bundle 的 canonical timestamp 是 `TickCoordinator.actual_start`；T=0 是该采集周期第一次 tick attempt 的 coordinator start，即使该 attempt 最终失败。排序和 LLM manifest 只使用 bundle canonical timestamp。实现可以额外记录各帧实际 capture time 诊断同一 tick 内偏差，但该诊断不进入 LLM prompt。

### 11.1 调度

配置间隔默认 1 秒，最小 0.1 秒。tick 不重叠、不追赶漏拍。

```text
actual_start[i+1] = max(actual_start[i] + x, completion_time[i])
```

如果 barrier、压缩或浏览器调度带来 `delta` 延迟，实际间隔为 `x+delta`，并以本次实际起点作为后续新基线。暂停恢复后同样使用恢复时刻作为新基线。

### 11.2 失败

- `tick_timeout_seconds` 默认 10 秒，最小 1 秒。
- 单次失败舍弃整个 bundle，后续继续。
- 每个源维护连续失败计数，成功一次清零。
- 同一源连续失败 3 次，或 track 发出 `ended`，全组暂停。
- 暂停时保留已有 bundle 和其他源 tracks。
- 只有恢复同一逻辑源后才继续同步 tick。
- 桌面共享恢复需要新的用户手势。

## 12. 图片数量和 Bundle 采样

设：

- `n` 为配置的每源最大帧数。
- `m` 为当前活动源数量，桌面共享计一个源。
- `M` 为当前手动图片数量，范围 0 到 9。
- 单条消息真实图片硬上限为 250。

活动组视觉源数量代码硬上限为 241。这样即使同时保留允许的 9 张手动图片，也能保证 `k>=1`。Start、Retry Sources、transport metadata 和 Backend 校验都必须拒绝超过 241 个活动源。

完整 tick 容量：

```text
k = min(n, floor((250 - M) / m))
```

每个活动源最多保留相同的 `k` 帧。不能用 250 除以 m 后的余数创建部分 tick。

### 12.1 采样规则

- `k=1` 时只保留最新 bundle。
- `k>=2` 时永久保留当前周期首个 bundle 和最新 bundle。
- 未满 k 时按时间追加。
- 已满 k 时先加入新 bundle，再从旧的中间 bundle 中等概率随机删除一个。
- 删除 bundle 时同时删除各源同一 tick 的图片。
- 最终 bundle 按 canonical bundle timestamp 升序排列。
- RNG 必须可注入，以支持确定性测试。

形式化为：已有 k 个 bundle，追加 newest 后临时长度 k+1；随机删除旧位置 2 到 k 中的一个 bundle，保留旧首个和 newest。

### 12.2 手动图片动态加入

视觉自动化运行中仍允许通过上传图片和手动拍照加入最多 9 张手动图片。手动图片优先保留。每次手动图片数量变化后重新计算 k。

若 k 缩小，以完整 bundle 缩容；缩容到 1 时只保留最新。若 k 增大，不恢复已删除帧，只由未来 tick 填充。

## 13. 64 MiB 图片预算

单次入站 Vision 请求携带的全部真实图片，包括历史 Chat 图片、当前手动图片和当前自动帧，Base64/Data URL 编码后不得超过 64 MiB。`64 MiB` 精确定义为 `67,108,864` 字节，按该 request body 中全部完整 Data URL 的 UTF-8 字节数求和，包含 `data:<mime>;base64,` header。Browser 可以为当前自动化 turn 选择更低预算，但不能高于 Backend YAML 上限。Backend 对收到的客户端编码 payload 重新计算同一口径；Backend 再缩放后的内部表示不用于放宽入口限制。

处理顺序：

1. 自动化图片统一编码为 JPEG；初始最大尺寸使用现有有效 `max_image_dimension`，初始质量使用 85。
2. 根据 Blob 大小加 Data URL header 计算精确 Base64 编码估值。
3. 超限时，所有自动帧按稳定图片顺序和同一 compression tier 重新编码。质量从配置的 initial quality 每次下降 10，并显式包含 minimum quality。默认序列为 85、75、65、55、45、40。
4. Minimum quality 仍超限时，尺寸从有效 `max_image_dimension` 开始每次乘 0.8 并向下取整；每个尺寸重新遍历同一 quality 序列，最低尺寸使用配置的 `minimum_image_dimension_px`，默认 256 px。
5. 同一 tick 的所有源必须使用相同 quality/dimension tier。
6. 最低 tier 后仍超限时，按 canonical bundle timestamp 删除最旧的中间完整 bundle。
7. 只剩首尾仍超限时删除首 bundle，只保留最新。
8. 最新 bundle 本身仍超限时阻止自动图片发送并显示错误。
9. 手动图片若在自动帧全部移除后仍导致超限，拒绝最新加入的手动图片，不静默删除已有手动图片。

如果现有有效最大尺寸小于 256 px，则该现有值同时成为起始值和 floor。Base64 转换并发读取 YAML，默认 4，代码校验范围 1 到 8。

前后端都必须重新计算数量和字节，不能信任客户端声明。

## 14. 图片处理和资源管理

### 14.1 Tick 图片处理

1. `TickCoordinator` 同时请求所有活动源帧。
2. 每个源使用独立 canvas 并行 `drawImage`。
3. 按 `/config` 返回的视觉尺寸及视觉自动化 JPEG quality 配置生成上传版 Blob。
4. 全部成功后原子建立 bundle。
5. 从上传版 Blob 生成缩略图。
6. bundle 进入有界采样器。
7. 释放 canvas 临时缓冲和原生帧引用。

自动化帧不在每个 tick 调用 `/v1/process-image-binary`，避免长期运行产生无界网络与 Jetson 处理负载。发送时仍经过现有 Chat/Agent Backend 图片校验和必要的再次缩放。

手动上传与手动拍照保持现有 `/v1/process-image-binary` 流程。

### 14.2 资源上限

- 采样池只保存上传版压缩 Blob，不保存原生 4K 帧。
- 放大预览展示上传版压缩图。
- 每张保留帧有独立小缩略图，默认 32×32，最大 128×128。
- 完整图片只在用户打开预览时解码。
- 淘汰 bundle 时立即 revoke Object URL 并释放 Blob、缩略图和元数据。
- Base64 只在发送冻结阶段生成。
- 折叠面板停止渲染矩阵，但不清除待发送采样池。
- Abort、页面离开和历史占位共用同一个幂等清理入口。
- 不得存在保存所有曾捕获帧的数组、日志、闭包或隐藏缓存。

## 15. 发送事务

发送事务顺序固定为：

```text
停止新 tick
→ 等待或舍弃 in-flight bundle
→ 冻结采样池
→ 校验 250 张
→ 校验 64 MiB
→ 自适应压缩或删除完整 bundle
→ 按配置并发转 Base64
→ 构造 manifest
→ 核验 context identity
→ staged 构建旧图片占位后的逻辑历史
→ 使用唯一 request_id 原子提交当前 turn 与 omission
→ 收到 Backend acceptance 后提交 Chat UI/history 和释放旧图
```

Base64 转换使用 `base64_encode_concurrency`，默认 4，不能同时为 250 张图片创建多份临时字符串。

历史 omission 在发送前只作为 staged transform，不得提前修改 canonical history 或释放旧图片。图片数、字节、manifest 和 context identity 全部校验成功后，当前 turn 与 omission 使用同一个唯一 `request_id` 提交。Backend 必须按 request ID 幂等接受；同一 ID 的重试不能重复 append turn、重复 omission 或重复启动 worker。

### 15.1 提交成功

- 普通 Chat 与 Agent 都以 Backend 返回 request acceptance 为提交边界。Acceptance 必须发生在 schema/预算/identity 验证完成且 worker/run scope 注册成功之后。Chat acceptance 确认 Backend 已接受 staged-omission wire history；Agent acceptance 还必须确认绑定 canonical memory 的 omission 已原子提交。
- 冻结图片成为最新用户 turn 的 canonical 图片。
- 根据 `capture_during_inference` 立即开始下一周期或等待稳定终态。
- Frontend 收到 acceptance 后才提交 Chat UI/history 的 staged omission，并释放对应旧图片。
- Agent omission 必须在 auto-compact source snapshot、compact eligibility、当前 turn append 和正常模型请求构建之前，在绑定顶层 canonical memory 中原子提交。

### 15.2 提交前失败

- 不开始下一周期。
- 保留冻结的自动帧和手动图片，进入可重试错误状态。
- Staged 历史 omission 不提交，原 canonical history 和旧图片保持不变。
- 某张自动图片转换失败时删除其完整 tick bundle。
- Abort 丢弃冻结自动帧，但保留手动图片。

请求已提交后的模型错误不回滚用户 turn，按稳定终态规则恢复未来采集。

若 Backend 已 acceptance 但 Frontend 未收到 acknowledgment，Frontend 必须先查询 `GET /v1/request-status/{request_id}`。该 provider-neutral endpoint 返回 `unknown | accepted | running | terminal`；`terminal` 只能在 worker cleanup 和 run-scope release 后写入。查询为 `unknown` 时，Frontend 才使用相同 request ID 幂等重试；查询为 accepted/running/terminal 时提交 UI omission，不重复请求。在消歧前不得释放旧图或启动下一周期。状态记录在 terminal 后保留 600 秒并有界清理；它是 request lifecycle metadata，不是视觉自动化 Backend session，也不得进入 LLM prompt。

## 16. LLM Session 与 Context Identity 隔离

| 标识 | 作用 |
|---|---|
| `agent_session_id` | 顶层 Agent/manager context window |
| `child_session_id` | 特定 child-agent context |
| `chat_conversation_generation` | 普通 Chat 页面本地对话代次 |
| `automation_task_id` | 浏览器本地采样任务标识，不是 session ID |
| `capture_cycle_id` | 单个采集周期标识 |
| `request_id` | 单次推理请求 |

### 16.1 绑定规则

- Trigger Start 时绑定当前页面实例、Chat/Agent 模式和 context identity。
- Agent 模式尚无 session ID 时，前端必须立即使用现有生成逻辑创建 `agent_session_id`，然后才启动采样。
- 普通 Chat 绑定页面本地 `chat_conversation_generation`。
- 采样任务只绑定顶层 manager session，不绑定或迁移到 child session。
- manager routing、handoff 或 child dispatch 不改变采样任务归属。
- armed 期间禁用 Chat/Agent 模式切换；用户必须先 Abort。

### 16.2 防串台

- capture、compression、send 和 terminal callback 都携带启动时 task/cycle/generation token。
- 回调提交前重新核验当前 identity。
- 旧 task callback 不能写入新采样池。
- 旧 request terminal callback 不能重启新 task。
- clear、新 context 或 Abort 递增 generation，使旧 callback 失效。
- Backend 回传 session ID 与绑定 ID 不同时，视为 identity violation，不得静默迁移。
- 冻结 payload 发送前再次核验绑定 ID。
- task/cycle/request/session ID 不进入 LLM prompt，只作为 lifecycle/transport 标识。
- manager 的历史图片替换不扫描或修改 child、sibling 或其他 manager session。

## 17. 当前 Turn 的图片序列与 Manifest

逻辑顺序固定为：

1. 手动图片，按用户添加顺序。
2. 桌面共享帧，按保留时间升序。
3. 各摄像头帧，按稳定源顺序分组，源内按同一套保留时间升序。
4. 最终 user-channel 文本，包含 manifest 和用户原始输入。

所有图片位于最终文本之前。Chat 和 Agent 必须使用等价的逻辑顺序。

Manifest 示例：

```text
<vision_automation_manifest>
Manual images: 2, image indexes 1-2
Source 1: Desktop, image indexes 3-6
Source 2: Camera A, image indexes 7-10
Relative times: 0.00s, 4.03s, 9.11s, 15.21s
</vision_automation_manifest>

用户原始消息……
```

Manifest 约束：

- 不输出 `Shared ticks: T0, T4...` 等冗余内部 tick 名称。
- Relative times 四舍五入到小数点后 2 位。
- 不包含 session ID、task ID、cycle ID、request ID、deviceId 或 UI 诊断。
- 摄像头使用稳定别名，不向模型暴露浏览器 deviceId。
- Manifest 是当前最终 user tail 中的模型相关视觉元数据，不进入 system 或历史中部。

## 18. Transport Metadata

普通 Chat 和 Agent 请求增加可选、等价的 transport metadata，并携带现有或新增的唯一 `request_id` 作为幂等 acceptance key：

```json
{
  "vision_automation": {
    "history_image_policy": "aggregate_prior_image_blocks",
    "manual_image_count": 2,
    "source_count": 2,
    "retained_tick_count": 4
  }
}
```

该 metadata 不进入 LLM prompt。Backend 必须从实际 payload 重新计算图片总数和字节数；声明字段只用于校验一致性和诊断。外部客户端不发送该字段是合法情况：它只禁用 automation-specific history policy 和计数一致性检查，所有 Vision 请求仍受全局 250 张及 64 MiB Backend 安全上限约束。

Chat 前端负责显式构建 staged-omission wire messages，并只在 Backend acceptance 后提交本地 canonical/UI history。Agent Backend 使用该 metadata，在绑定的顶层 Agent canonical memory 中执行旧 `task_images` 占位。不得从 UI bubbles、timeline events、routing deltas 或 context pack 重建 Agent memory。

Agent 历史 block 的 sources 和 relative range 只从该 canonical TaskStep 原始 task text 中由本项目生成的严格 `<vision_automation_manifest>` grammar 解析；解析器不得猜测或宽松修复格式。Manifest 缺失或不合法时使用 `sources=none; relative_range=unavailable`，仍按实际 `task_images` 数量生成占位。不得另建依赖前端 UI 状态的可变 provenance side channel。

## 19. 历史图片 Prefix 例外

### 19.1 生效范围

例外只在以下范围生效：

- 视觉自动化采样任务 armed 期间的每次新发送。
- Abort 后恰好一次、属于原绑定 identity 的普通用户 Chat/Agent 发送，用于收尾释放最近真实图片。

Abort 创建一个 `cleanup_pending` token。该 token 继承被 Abort 任务的 page generation、Chat/Agent mode 和顶层 context identity。只有同一绑定 identity 下第一个被 Backend 成功 acceptance、且实际提交 omission 的普通用户 turn 才消耗 token。Compact、Stop、Answer Now、pre-accept failure 和其他 context 的发送均不消耗。Clear、页面离开或 context 销毁会直接释放被销毁 context 的资源并取消 token。Abort 后若在同一 identity 重新 Trigger Start，则 armed 状态下下一次成功发送同时完成既有 cleanup，不再额外欠一次收尾发送。

收尾完成后，普通 Vision 恢复原有历史图片策略。已经占位的图片不会恢复。

### 19.2 替换规则

- 发送新 turn 前，只保留当前 turn 的真实图片。
- 所有更早 turns 的自动帧、上传图片和手动拍照图片都永久替换。
- 每个 canonical image block 只产生一个汇总占位符。这里的 image block 明确定义为同一 canonical user message/image container 中的一段连续 image content parts；不是每张单独图片。Agent `task_images` 视为该 TaskStep 的一个 image block。
- 保留原角色、文字、顺序、assistant 输出和工具轨迹。
- 不得 strip、合并或改写 Agent `tool_calls`。
- user-role tool observations 保持原角色和内容。
- 图片 Blob、Base64、Object URL 和缩略图在 omission 获得 Backend acceptance、成为 canonical 后立即释放。

占位符必须插回原 image block 位置，并使用固定 grammar：

```text
[historical images omitted: count=<decimal>; sources=<stable_alias_csv|none>; relative_range=<seconds_2dp>..<seconds_2dp>s|unavailable]
```

`sources` 使用固定顺序 `manual,desktop,camera_1,camera_2,...`，只使用内部稳定别名，不包含用户可控设备 label，因此不需要转义。没有自动化时间信息时使用 `relative_range=unavailable`。数值统一四舍五入到两位小数。

示例：

```text
[historical images omitted: count=48; sources=manual,desktop,camera_1; relative_range=0.00..15.21s]
```

UI 中旧图片矩阵同步变为汇总卡，只显示数量、来源和时间范围，不再允许打开原图。

### 19.3 Chat 与 Agent

- Chat 使用显式 history/wire 转换函数，不能依赖对象 spread 隐式翻译 UI metadata。
- Agent 只修改当前绑定顶层 session 的 canonical memory。
- Agent 替换只清除旧图片对象并添加确定性占位，不修改 tool calls 或 observations。
- Child session memory 不得因 manager 的视觉自动化请求被扫描或修改。
- Chat 和 Agent 对同一逻辑历史使用等价占位语义。

## 20. Auto-Compact 与 Prefix Cache

视觉自动化不禁用 auto-compact，也不禁用 manual compact。

- 现有 compact eligibility、cache-first、reliable fallback、validation 和 memory replacement 继续生效。
- 视觉自动化不修改 compact thinking、性能比、summary token 或 fallback 配置。
- 历史图片占位是独立、明确记录的 canonical prefix boundary，不依赖 compact 成功。
- Compact 若正常触发，应基于当时 canonical 形状工作，即“旧图占位 + 尚未被占位的当前真实图片 + 其余稳定历史”。
- Chat auto-compact source 使用 staged-omission wire history；Agent omission 必须在 auto-compact source snapshot 和 eligibility 之前原子提交。若 logical turn 未获 acceptance，staged Chat omission 不进入本地 canonical history。
- 除历史图片替换外，视觉自动化继续遵守 Prefix-Cache Covenant。
- 每次例外发生时记录原因 `vision_automation_historical_image_omission`。

## 21. Backend 验证和代理限制

Backend 必须独立验证：

- 最终逻辑 inference prompt 中的真实图片总数不超过 250。Chat 统计提交的完整 messages；Agent 统计 omission 后的 canonical memory 加当前 turn。视觉自动化因旧图先占位，当前 turn 可重新获得最多 250 的总配额。
- 自动化 transport 声明的手动图片数不超过 9。
- 自动化 transport 的活动 `source_count` 不超过 241。
- 实际图片总数符合 `manual_image_count + source_count * retained_tick_count`；不一致时拒绝自动化 metadata，而不是猜测。
- Data URL 合法且 Base64 可解码。
- MIME、实际图片格式、单图大小和尺寸符合现有视觉配置。
- 图片编码后总量不超过 64 MiB Backend 上限。
- Agent session ID 与目标 canonical memory 一致。
- 历史替换严格 scoped 到该顶层 session。

Next.js proxy 在调用 `request.arrayBuffer()` 前优先检查 `Content-Length`。Chat/Agent Vision request body ceiling 为 72 MiB，即 `75,497,472` 字节，以容纳 64 MiB 图片、JSON 和 manifest。缺失或伪造 Content-Length 时，FastAPI 仍按实际 payload 校验。超限使用明确的 `413 Payload Too Large`。

一张损坏图片不能让 Agent 静默丢弃整批图片。错误应指出失败图片位置或所属 tick，并返回可诊断的 4xx。

## 22. vLLM 配置

删除 active vLLM infer YAML 中显式的 `limit_mm_per_prompt.image = 1`。容器脚本在配置字段缺失时不得继续传 `--limit-mm-per-prompt` 的单图限制。

应用层继续使用 250 张硬上限。删除显式配置不等于假设 provider 一定支持 250；发布前必须用当前目标 vLLM、模型和容器进行多图实机验收。若 provider 默认仍低于需求，必须作为部署阻塞问题报告，不得静默把前端上限降回 1。

## 23. 错误处理

| 错误 | 行为 |
|---|---|
| 单个启动源权限拒绝 | 标记失败，其他成功源继续 |
| 全部启动源失败 | 保持 armed，等待人工恢复 |
| 单 tick 部分失败 | 舍弃完整 bundle，继续 |
| 同源连续失败达到阈值 | 暂停全组，保留已采样 bundle |
| Display track 被用户停止 | 暂停全组，要求用户手势恢复 |
| 最新 bundle 无法满足 64 MiB | 阻止自动图片发送并显示错误 |
| Context/session identity 不匹配 | 阻止发送、停止自动重启、清理未发送自动帧 |
| Backend 413/422 | 保留冻结批次供处理或重试 |
| 页面离开或 clear | Abort 级清理 |
| 编程循环冲突 | 阻止启动，不自动停止另一任务 |

## 24. 隐私与安全

- 摄像头和桌面共享只能由明确用户手势启动。
- 仅在 secure context 或 localhost 启用；否则显示可操作错误。
- 不保存或上传音频。
- 不在 localStorage、IndexedDB、日志或 Backend session 中持久化采集图片。
- 不记录 Base64、Blob 内容或截图像素。
- Manifest 不暴露 deviceId。
- 浏览器原生共享和摄像头指示仍可见。
- Abort 和页面卸载必须停止所有 tracks。
- Backend 防御非法 Base64、MIME 欺骗、图片解码炸弹和超大 payload。

## 25. 可观测性

UI 显示：

- 采样任务状态。
- 活动源数量和健康状态。
- tick 尝试、成功和失败数量。
- 当前保留 bundle 与图片数量。
- 当前预计编码字节。
- 最近错误和暂停原因。

Backend 日志可以记录顶层 session ID、图片数、总字节数、validation outcome 和 Prefix 例外原因。不得把 UI 诊断、日志或 session lifecycle 字段写入 LLM prompt。Child-agent 日志必须保留自己的 child session ID，不能误标为 manager 视觉请求。

## 26. 测试策略

### 26.1 前端纯逻辑

- k 公式所有边界。
- n=1、n=2、n>=3 的采样规则。
- 固定 RNG 的确定性结果。
- 完整 bundle 同步删除。
- 手动图片引起的动态缩容。
- 64 MiB 压缩和 bundle 淘汰。
- Relative times 两位小数。
- Manifest 不含冗余 tick 名称或身份字段。

### 26.2 状态机

- 所有状态转换。
- Trigger Start 的即时 T=0。
- 折叠不影响采集。
- 两种 `capture_during_inference` 行为。
- Agent 中间 step 不重启周期。
- Stop、Answer Now、错误和网络中断等待稳定终态。
- Abort 后旧 callback 不得重启。
- Cycle 互斥。

### 26.3 Media 与 Tick

- mocked getUserMedia/getDisplayMedia、video、canvas 和 fake timers。
- 同 tick 全源同时触发、并行工作、同步提交。
- 任一失败整组舍弃。
- tick 不重叠、不追赶、延迟后顺延。
- timeout、连续失败、track ended、暂停和人工恢复。

### 26.4 资源 Soak

- 虚拟运行 10,000 tick 后无无界引用增长。
- 每个 Object URL 恰好 revoke 一次。
- Abort 幂等。
- 折叠/展开不重复创建 MediaStream 或 Blob。
- 历史占位释放 Base64、Blob、缩略图和 Object URL。
- 最多存在一个已发送批次和一个当前采样池，不允许自动化大图消息队列增长。

### 26.5 配置

- YAML、overall loader、AppConfig、`/config` 和 localStorage 全链路。
- 首次 fallback、本地优先、非法值 fallback 和 schema version。
- 代码硬限制。
- Trigger Start 后配置锁定。
- 不持久化设备或图片。
- `/config` 使用当前配置对象，避免热重载旧引用。

### 26.6 Chat/Agent Contract

- 图片顺序和 manifest 等价。
- 当前图片最多 250。
- 历史图片块汇总占位。
- 历史文字、角色、顺序和工具轨迹不变。
- Normal Vision 无 metadata 时保持原行为。
- Abort 后一次收尾，随后恢复 normal policy。
- Auto-compact 仍可触发。

### 26.7 Session 隔离

- 顶层 Session A/B 不串台。
- Manager A 替换不修改 child A1/A2 或 sibling manager。
- clear 后旧 terminal callback 不重启新页面代次采集。
- Backend 回传 session ID 不匹配时拒绝迁移。
- 两个 `/vision` 标签页运行状态和帧完全隔离。
- localStorage 只共享配置。

### 26.8 Backend 与 Provider

- 251 张返回 4xx。
- 超过 64 MiB 返回 413。
- 伪造客户端计数不能绕过验证。
- 非法 Base64、MIME、图片炸弹和超尺寸图片被拒绝。
- Direct FastAPI 与 Next proxy 等价校验。
- SGLang/vLLM 逻辑消息等价。
- vLLM 删除单图 flag 后接受多于一张图片和 250 张小图协议验收。

无 automation metadata 的普通 Vision fixture 也必须覆盖 250 张与 64 MiB 全局安全上限；metadata 缺失只保持 normal history policy，不跳过安全校验。

### 26.9 真实浏览器

- Chromium secure context 权限流程。
- 一个桌面源加多个摄像头。
- 原生停止共享和摄像头热拔插。
- 后台标签页 tick 顺延。
- 0.1 秒配置下的实际资源表现。
- 250 张小图和接近 64 MiB 请求。
- 桌面端、移动端、触屏拖动、键盘和 reduced-motion。

## 27. 发布门槛

以下条件全部满足后才能默认启用：

1. 配置链路测试通过。
2. Chat/Agent 消息形状等价。
3. 250 张和 64 MiB 双限制前后端生效。
4. 同步 tick bundle 和随机采样测试通过。
5. 10,000 tick soak 无无界增长。
6. Abort、页面退出和历史占位完整释放资源。
7. 顶层 Agent、child、sibling 和标签页无串台。
8. Normal Vision、Text、Cycle、compact、Stop、Answer Now 回归通过。
9. vLLM 多图实机通过。
10. 桌面共享和多摄像头手工验收通过。
11. Jetson Thor 接近 64 MiB 请求无不可接受 UMA 峰值或进程崩溃。
12. Prefix audit 只出现本文允许的视觉历史图片边界。

## 28. 已知限制和风险

- 浏览器只能软件同步，不能保证硬件同步。
- 后台标签页可能节流 timer；系统顺延而不追赶。
- `x=0.1s` 是允许配置，不是吞吐保证。
- 多源和高复杂度画面可能降低实际 tick 数或图片质量。
- 首帧/最新帧固定保留，因此算法不是标准均匀 reservoir sampling。
- 桌面共享恢复需要用户手势。
- 浏览器、驱动和 USB 带宽可能限制同时打开的摄像头数量。
- 250 张模型声明不等于目标 provider 能以可接受资源运行，实机结果优先。
- Next proxy、FastAPI 和 provider 仍可能为同一 64 MiB payload 创建多份内存副本。
- 历史真实图片占位后不可恢复。
- Abort 不撤回已经提交给模型的图片。
- 历史图片例外会制造明确的 prefix boundary；这是为控制多模态上下文和内存而接受的局部取舍。

## 29. 预计影响区域

- `config/config-overall.yaml`
- 新增 `config/config-vision.yaml`
- `backend/config.py`
- Backend `/config`
- Chat/Agent 请求 schema 与图片校验
- Agent canonical memory 历史图片占位
- Next.js backend proxy 请求体限制
- `frontend/src/app/vision/page.tsx`
- `frontend/src/components/ImageUploader.tsx`
- 浏览器摄像头 stream registry
- 新的 capture source、tick coordinator、sampler 和状态机模块
- Chat/Agent wire serializer
- UI Message 图片汇总卡
- vLLM infer 配置和容器参数测试
- `AGENTS.md`
- 前后端单元、集成、契约和浏览器测试

Text 页面 `/` 不新增视觉 UI。

## 30. 完成定义

该功能只有在产品行为、同步采样、双重预算、资源释放、Chat/Agent parity、session 隔离、历史图片例外、auto-compact 保持启用、Provider 实机和所有发布门槛都得到验证后才算完成。

本文是设计与产品规格，不是 implementation plan；具体文件级实施顺序、测试先后和提交拆分应在用户批准本文后单独编写。

## 31. Implementation Plan 分解要求

该 PRD 是一个产品规格，但实施范围不应压缩成一个无法审阅的巨型执行批次。后续 writing-plans 阶段必须拆成四个有依赖顺序、各自可验证的计划或里程碑：

1. 配置、wire contract、Backend/proxy 安全校验、request acceptance/idempotency 与 vLLM 参数移除。
2. CaptureSource、同步 TickCoordinator、bundle sampler、图片资源层和内联 UI。
3. Chat/Agent canonical historical-image omission、auto-compact ordering、stable-terminal barrier 和 session 隔离。
4. 浏览器 E2E、10,000 tick soak、vLLM 多图与 Jetson Thor UMA 发布验收。

后一个里程碑只有在前一个里程碑的契约测试通过后才能开始集成；不得在未建立 request/session isolation 测试前启用默认功能。
