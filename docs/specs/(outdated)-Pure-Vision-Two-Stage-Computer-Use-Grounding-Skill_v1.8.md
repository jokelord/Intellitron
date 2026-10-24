# Pure-Vision Two-Stage Computer Use Grounding Skill

## 0. 文档状态

**版本：** MVP v1.8。重复刻度须明确写出 `x=…` / `y=…`，采用更小的无描边、无阴影字体；离线图固定输出到仓库的 `computer-use-diagrams/`。
**v1.8 坐标与网格勘误（2026-09-25）：** Stage-2 的 X/Y 均独立归一化为 `[0,1000]`，与裁剪后 raster 的宽高比无关。两轴各八等分，完整刻度固定为 `0,125,250,375,500,625,750,875,1000`；同一刻度值沿正交网格线在八个格段各标一次，方便分区读取图像。图上网格可以是长方形，但不能出现尾端剩余的部分格。裁剪仍不补白，Stage-2 bitmap 保持实际 physical crop 的像素宽高比，例如 1920×1200 crop 可等比显示为 1000×625；其中 500×500 或 edge 的 500×260 是**相对裁剪画幅的描述，不是 bitmap 像素宽高**。Stage-2 的中心 `(500,500)` 映射到实际整数 crop 的宽、高各一半。此前 v1.6/v1.7 把 clipped 相对画幅误当作 raster 与坐标 domain，且 16 等分导致 63、313 等非预期标签；这些规则在 v1.8 失效。
**v1.7 修订（2026-09-25）：** 明确 Stage-1 必须由同一个具备视觉能力的 Agent 看当前 overview 并输出数值型 normalized 坐标；自然语言目标描述不能代替坐标输出。
**v1.4 背景（2026-09-22）：** 将 grounding 改为同一个 Agent context 内的 executable-driven observe → Stage-1 coarse → inspect_region → Stage-2 precise → dispatch；删除 backend 私有 VLM grounding call，加入 computer-use screenshot 的受限历史图片省略例外，并把 executable observation 作为 Agent multimodal observation 接入。immutable frame、geometry version、fail-closed、native lifecycle 与 dispatch 约束继续有效；坐标与网格以本 v1.8 勘误为准。
**v1.5 坐标修订（2026-09-25，已被 v1.6 修正）：** 曾把所有 Stage-2 crop 都误定为 1000×1000，遗漏 edge clipping 后的局部画幅比例。
**v1.6 坐标修订（2026-09-25，已被 v1.8 修正）：** 曾让短边坐标上界与 bitmap 高度都跟随 clipped 相对 extent 变化。v1.8 保留无 padding 的真实裁剪，以实际 physical crop 宽高比等比缩放 bitmap，并为两个坐标轴分别指定完整的 0–1000 domain。
**目标平台：** `Ubuntu 24.04`、`ARM64`、GNOME / Wayland 为主要目标、True X11 兼容、`Python backend`、`Next.js frontend`、本地 VLM（例如 Qwen3-VL 8B 级模型）。
**核心路线：** `Pure Vision Computer Use`。正确性不得依赖：`AT-SPI / A11Y Tree`、`Qt API`、`Browser DOM`、`OmniParser`、`OCR`、应用自身 automation API。桌面与应用统一视为：**可观察像素 + 可注入鼠标输入的黑盒系统。**。增加了6项鼠标交互规则。

---

# 1. MVP 目标

构建一个**高通用性、高鲁棒性、低额外开销、坐标变换完全可测试**的 Linux Desktop Computer Use grounding subsystem。必须适用于：`GTK`、`Qt`、`Electron`、`Chromium`、`XWayland`、`Canvas`、自绘 UI、`OpenGL / Vulkan UI`、accessibility 信息缺失的应用。MVP 只实现：`monitor screenshot observation`、`同一 Agent context 内的 two-stage visual grounding`、`absolute pointer interaction`、`left single click`、`right single click`、`left double click`、`right double click`、`scroll up 1 unit`、`scroll down 1 unit`、`Wayland`、`X11`、`single monitor`。第一步永远是观察当前桌面；没有有效 observation 时不得规划或发送交互。暂不实现：`multi-monitor composite`、`cross-monitor crop`、`drag/drop`、`keyboard automation`、`OCR`、`AT-SPI fast path`、`OmniParser`、`semantic element IDs`。

---

# 2. Major Monitor Scope

一个桌面 capture/input binding 只绑定 `one major monitor`，绑定建立后该 monitor 在整个生命周期中保持稳定；该绑定不是项目 LLM/Agent context session。以下变化必须使当前 observation / crop 全部失效：`monitor disconnect`、`resolution change`、`scale change`、`rotation change`、`ScreenCast stream recreate`、`libei region change`、`X11 monitor geometry change`、capture crop/transform 或 full-monitor correspondence 变化。Backend 保存 `geometry_version`；任何 geometry change 都必须执行 `geometry_version += 1`；所有 `frame_id`、`observation_id`、`crop_id` 均绑定 `geometry_version`。

---

# 3. 最重要的坐标原则

## 3.1 `1000` 是 normalized coordinate domain

本系统 canonical geometry 中的 `1000` 是**归一化参考长度**，不是像素分辨率；模型 wire protocol 可以另行使用像素坐标（§60）。 例如原始 monitor raster：`3840 × 2160`。某点实际 raster X 为：`2880`。对应 normalized X：

```text
Xn = 1000 × 2880 / 3840
   = 750
```

因此：`relative_x = 1000 × raster_x / raster_width`；`relative_y = 1000 × raster_y / raster_height`。反向：`raster_x = relative_x / 1000 × raster_width`；`raster_y = relative_y / 1000 × raster_height`。

---

# 4. Stage-1 Global Normalized Space

Stage 1 canonical geometry 永远使用：`X ∈ [0,1000]`；`Y ∈ [0,1000]`。分别表示：`X / 1000 = horizontal ratio`；`Y / 1000 = vertical ratio`。注意：Stage-1 的 X/Y normalized units 是两个独立的比例轴。因此对于一个：`3840 × 2160` 的 monitor：`500 horizontal units = 1920 raster pixels`；`500 vertical units   = 1080 raster pixels`。Stage-1 normalized space 不要求：`1 X unit`；`==`；`1 Y unit`，具有相同物理视觉长度。它只用于：**全屏 coarse relative localization。**

Stage-1 Agent 输出必须是符合当前 executable operation schema 的数值坐标，例如 `{"x":180,"y":740,"zoom":4}`。`x`、`y` 分别在 overview 横轴、纵轴 `[0,1000]` 上表示相对位置；不接受“点微信图标”“打开微信”等自然语言来替代坐标。自然语言 `target_description` 只作为同一视觉判断的目标条件。

---

# 5. 图片绝不能为了坐标而变形

假设 monitor：`3840 × 2160`。Stage-1 overview 可以生成：`1536 × 864`、`1280 × 720`、`1000 × 563`，不能为追求坐标域而拉成 1000×1000。Stage-2 detail 同样保持 **actual physical crop raster** 的像素宽高比；即便它的 X/Y 坐标域都是 0–1000，也不能把 1920×1200 的 crop 拉成方形 bitmap。Stage-1/Stage-2 的 `[0,1000]²` 都只是 coordinate domain，不是 bitmap dimensions。

---

# 6. Two-Stage Grounding 总体流程

```text
Agent task starts
        │
        ▼
exec_command → allowlisted `vision_desktop observe`
        │
        ▼
Same Agent receives multimodal desktop observation
        │
        ▼
Same Agent emits Stage-1 coarse relative coordinate
        │
        ▼
exec_command → `vision_desktop inspect_region(x,y,zoom)`
        │
        ▼
Same Agent receives ticked detail image from the same frozen frame
        │
        ▼
Same Agent emits Stage-2 precise local relative coordinate
        │
        ▼
exec_command → `vision_desktop interact_detail(u,v,action)`
        │
        ▼
Deterministic inverse transform → monitor ratio
        │
        ▼
Wayland/libei or X11/XTEST → MOVE → IMMEDIATE ACTION
```

`vision_desktop` 是受信任的固定 executable 协议入口，对应仓库内的
`backend/skills/computer-use-vision/bin/vision_desktop`。直接 shell 运行该
marker 必须返回 `BACKEND_BRIDGE_REQUIRED`；只有 session-bound backend
intercept 才能执行 live operation。它返回结构化 observation envelope；其中的
image block 由 Agent runtime 附着到当前 `ActionStep.observations_images`，所以同一个
Agent 的下一轮 LLM 请求可以直接看到截图。Stage-1 与 Stage-2 不是 backend 私有推理请求；
它们是同一个 Agent 根据当前 observation 的连续 tool turns。坐标 prompt/schema/grid labels
仍由 adapter 生成，raw output 仍必须先 decode。Agent 不得自行计算 local→global 或 physical 坐标。

Stage-1 坐标必须由这个 Agent 根据刚收到的 overview 图像视觉定位后产生；输出数值型 normalized 坐标，不是自然语言操作指令。Stage-2 也由同一 Agent 根据 detail crop 输出该 crop 坐标系中的最终数值坐标。

## 6.1 首次观察与同一上下文规则

每个新 computer-use 任务的第一个允许动作必须是 `vision_desktop observe`。在 observe 成功前，Agent 不得调用 `inspect_region`、`interact_detail`、任何鼠标动作或以自然语言目标替代截图。observe 返回的 `observation_id`、`frame_id`、`geometry_version` 与目标描述绑定到当前 top-level Agent context；Stage-1、Stage-2 和 dispatch 必须沿同一 context 继续，禁止另开隐藏 VLM context。

一次 observe 失败只产生 fail-closed observation。Agent 可以按预算重新 observe，或向用户报告 unavailable；不得假设桌面布局、默认应用入口或目标存在。

## 6.2 Executable observation protocol

skill 只能通过 backend 已登记的 allowlisted executable（默认命名为 `vision_desktop`）发送以下有限 argv/JSON envelope：`observe`、`inspect_region`、`interact_detail`、`reobserve`。shell 解析、任意路径、任意子命令、native FD、binding ID、capability、crop ID 和 request ID 均由 runtime 拒绝或由可信 runtime 注入。Agent 只提供当前阶段允许的 target/action/坐标字段。

每个 envelope 必须包含 `protocol_version`、`context_nonce`、`operation` 与单调 `sequence`；`observe` 还必须携带当前用户意图的 `target_description` 与可信 `requested_action`。返回值必须包含 `state`、`observation_id`（若有图像）、`frame_id`、`geometry_version`、`provenance=computer_use_observation` 和结构化错误码。`provenance` 只能由 runtime 在验证 executable 身份、context/generation、sequence、返回码和受控图像句柄后赋予；模型输出、普通 stdout、其他 executable 或用户参数不得自称该 provenance。图像不得只作为普通 stdout 文本返回；runtime 必须从受控的 in-memory/memfd 句柄构造 multimodal observation，禁止把 base64 作为可重复的历史文本写入 Agent prompt。

`exec_command` 的公开 Agent schema 仍是现有的单个 `command: str` 参数，但当第一个 token 为 `vision_desktop` 时，runtime 必须走不启动 shell 的固定 argv parser：只接受上述四个 operation 及其白名单 flags，拒绝管道、重定向、命令替换、额外 executable、空白外的 shell 语法与未声明字段。Stage-specific operation schema 只是附着在当前 observation 的动态调用说明，不得注册为新的 API tool，也不得替换 Agent 稳定 system/tool schema。

同一个 Agent tool-call batch 中不得同时提交相互依赖的 computer-use operations。runtime 必须串行化并绑定 `observe → inspect_region → interact_detail`；当一个 capture/inspect/reobserve 或其对应 Agent inference 正在进行时，另一个操作不得替换 active frame/crop。每个 Stage-1/Stage-2 model result 都带有其看到的 observation/crop generation；generation 不匹配、过期或来自另一批次时必须拒绝，不能只依赖 sequence number 猜测“最新 crop”。

---

# 7. Immutable Observation

每次 Stage-1 observation 必须产生：`observation_id`、`frame_id`、`geometry_version`。例如：`observation_id = obs_1042`；`frame_id       = frame_8812`；`geometry_version = 7`。Stage 1 与 Stage 2：**必须来自同一个 frozen native frame。** 正确：

```text
F42
 ├─ overview
 └─ detail crop
```

错误：`Stage 1 → F42`；`Stage 2 → F47`。因为真实桌面可能发生：`animation`、`popup`、`window movement`、`scroll`、`layout change`。

---

# 8. Frame Ownership 与 Capture Canonicalization

`capture.py → canonicalize_frame() → frame_store.py` 是强制边界。`freeze_frame()` 返回的是具有独立生命周期的 canonical raster，绝不能保存随后交还 PipeWire 的 buffer pointer。MVP 允许 copy；不要求 DMA-BUF zero-copy。

`canonicalize_frame()` 必须解释实际协商的 format、planes、stride、有效 buffer 范围与所有权，并按捕获后端已确认的 metadata 语义处理 `SPA_META_VideoCrop` / `SPA_META_VideoTransform`，输出与用户看到的方向一致的 raster。[VideoTransform 官方结构说明](https://docs.pipewire.org/structspa__meta__videotransform.html) 将其描述为已应用于 buffer 的 orientation transformation，因此不能无条件正向应用枚举；应依据 producer/consumer contract 确定所需正向、逆向或无需变换，以及 buffer-space crop 的处理顺序。不得把 compositor 已经应用的 rotation/flip 再做一次；每个变换必须且只能应用一次。未支持的格式、变换组合或无法确定的 metadata 坐标系返回 `UNSUPPORTED_CAPTURE_GEOMETRY`。

**可见 crop 不自动等于完整 monitor。** 只有证明 canonical raster 覆盖所绑定输入 region 的完整显示器，才能采用 §27/§41 的整屏比例映射；若 VideoCrop 代表 monitor 的局部内容，或覆盖范围不明，MVP fail closed，不能把局部图拉满整个 input region。保存源 buffer geometry、crop/transform、canonical dimensions、coverage evidence、stream generation、sequence 与 monotonic capture/arrival timestamps；映射改变时递增 `geometry_version`。测试需区分 buffer padding 裁除与真实 monitor 部分裁剪。

PipeWire 定义了 crop 与 transform metadata，具体枚举见 [官方 metadata 源码](https://docs.pipewire.org/meta_8h_source.html)。

---

# 9. Stage-1 Coordinate Turn

以下是同一 Agent 在 `vision_desktop inspect_region` 这一轮中产生的 canonical coordinate contract；Agent 的 raw tool-call wire schema 仍由 §60 对当前 overview 的协议生成，不保证直接使用以下数值域：

```text
inspect_region(
    x,
    y,
    zoom?
)
```

其中：

```text
x: normalized global X
   range [0,1000]
y: normalized global Y
   range [0,1000]
zoom:
   enum [2,4,8,16]
   optional
   default = 2
```

非法 zoom：`3`、`6`、`"large"`。必须返回：`INVALID_ZOOM`。不得自动猜测。模型 wire 值先按其声明的 domain 验证；解码后的 canonical x/y 越界（例如 `x = 1200`、`x = -50`、`y = 1001`）：`INVALID_GLOBAL_COORDINATE`；`→ 不生成 crop`；`→ fail closed`。必须严格区分两类情形：

```text
合法中心点 + nominal 超出屏幕:
    x = 50, zoom = 2
    nominal crop = [-200, 300]
    → 合法 request，执行 §11 edge clipping
非法中心点:
    x = -50
    → 非法 request，直接 reject
```

clip 只允许作用于合法中心点产生的 nominal rectangle；它绝不是对非法模型输出的静默修复。
该 turn 只产生放大请求，不产生任何 physical input。若 Agent 没有看到本 request 的 `computer_use_observation`，或 observation 的 `geometry_version` 与当前 request 不一致，runtime 必须拒绝该 turn。

---

# 10. Zoom 的真实语义

`zoom` 表示：`Stage-2 nominal field-of-view`。不是：`PIL resize 的固定倍率`。定义：`S = 1000 / zoom`。因此：`2x → nominal span = 500`、`4x → nominal span = 250`、`8x → nominal span = 125`、`16x → nominal span = 62.5`。围绕 Stage-1 point `(X,Y)` 生成 nominal global normalized region：`L = X - S/2`、`R = X + S/2`、`T = Y - S/2`、`B = Y + S/2`，然后执行 edge clipping。

---

# 11. Edge Crop Policy

MVP 使用 `clip-to-monitor-boundary`，不使用 `padding`、黑边、透明区、`duplicate edge pixels` 或其他 monitor 像素。执行：`L = max(X - S/2, 0)`、`R = min(X + S/2, 1000)`、`T = max(Y - S/2, 0)`、`B = min(Y + S/2, 1000)`。因此 edge crop 允许 `R-L != S`、`B-T != S`，这是预期行为。

---

# 12. Void Region 的原则

如果 nominal crop 超出 monitor，例如 `[-100,400]`，其中 `[-100,0)` 属于不存在的 void region。本系统**完全丢弃 void region**，最终 crop 为 `[0,400]`。不存在对应 bitmap 的区域绝不进入 Stage-2 image，即 overlay 前 base raster 的每个图像采样都必须来自 frozen source frame；§19–§22 明确生成的 grid/tick/label 是显示辅助叠层，不受此像素来源断言约束。

---

# 13. Normalized Crop → Native Raster

Normalized crop：`L,T,R,B`。转换为实际 source raster：`x0 = floor(L / 1000 × Wp)`；`y0 = floor(T / 1000 × Hp)`；`x1 = ceil(R / 1000 × Wp)`；`y1 = ceil(B / 1000 × Hp)`。然后 clamp：`0 <= x0 < x1 <= Wp`；`0 <= y0 < y1 <= Hp`。实际 crop 使用半开区间：`[x0,x1)`、`[y0,y1)`。

---

# 14. Exact Integer Crop Rect 是权威几何

一旦 `x0,y0,x1,y1` 确定，后续所有 Stage-2 inverse transform 必须基于这个实际 crop rect，不得重新只根据 `X`、`Y`、`zoom` 计算。原因是 `floor`、`ceil`、`integer raster rounding` 会使理论 normalized crop 与最终实际 raster crop 产生极小差异。因此保存：`crop_width = x1 - x0`、`crop_height = y1 - y0`。

---

# 15. Stage-2 双轴独立的 Local Coordinate Space

Stage-1 给出全屏 `[0,1000]×[0,1000]` 百分比。zoom 的 nominal rectangle 分别映射到 source width/height，clip 后以实际整数 crop `x0,y0,x1,y1` 为权威。**Stage-2 的 local X 与 local Y 始终各有 `[0,1000]` 的完整坐标域**；它们分别表示实际 crop 宽度和高度的比例，与 Stage-2 bitmap 的像素尺寸、宽高比相互独立：

```text
local_width = 1000
local_height = 1000
source_x = x0 + local_x / 1000 * (x1-x0)
source_y = y0 + local_y / 1000 * (y1-y0)
```

裁剪后的 **bitmap 显示比例** 单独由实际 physical crop 的整数宽高 `crop_width:crop_height` 决定，等比缩放到配置的 raster 长边；不得把 x/y 分别归一化后的相对 extent 当成 bitmap 像素尺寸。3840×2400 monitor 上，中心 zoom 2 的 crop 是 1920×1200，可等比显示为 1000×625，也可在更大配置下显示为 3840×2400。靠上边缘裁成 1920×624 时，显示为 1000×325（取整后）。这两个图的 X/Y 仍各自映射到 0–1000。

## 15.1 独立的 Model-Facing Domain

`model_width` / `model_height` 与 canonical dimensions 分开保存，由双向 adapter（§60）定义。v1.8 的 live `relative_1000` / `custom_isotropic_1000` 两阶段均使用 1000×1000 wire domain；两个方向各自是比例尺，不能把 bitmap 的实际像素高度（例如 625 或 325）写作 Y 上界。历史 `image_pixel` 协议仅可供离线对照，不得作为 v1.8 live Agent 的提示词、工具 schema 或网格标签。

adapter 先验证 raw 值，再转换为轴比例 `rx,ry`，最后生成 `u=rx*local_width`、`v=ry*local_height`。**不再整数化 canonical u/v。** 模型整数定位的量化误差与 raster/render rounding 是不同误差来源，见 §62。

# 16. Stage-2 裁剪例子

zoom 2 在中心选出 `500×500` normalized crop。对于 3840×2400 source raster，理想 physical crop 是 1920×1200；两个 local 坐标轴均为 0–1000，而 detail bitmap 保持 1920:1200 的物理宽高比，例如长边设为 1000 时得到 1000×625。`500×500` 从未要求方形像素图。

靠近上边缘时，同一 zoom 可能实际裁出相对 extent `500×260`，对应 1920×624 physical pixels。无 padding，丢弃 monitor 外的 void；detail bitmap 等比显示为 1000×325，**但 local 坐标域仍为 1000×1000**。`500×260` 只描述相对裁剪 extent，并非像素 raster。Stage-2 的 `(500,500)` 是这张图的中心，反向映射到 physical crop 的 `(960,312)` 偏移处。图像比例与坐标比例分开保存；物理 crop 的整数边界始终是 inverse transform 的权威。

# 17. Stage-2 两轴独立归一化

Stage-1 使用全 monitor 的 `[0,1000]×[0,1000]` 百分比。Stage-2 使用 cropped image 的 `[0,1000]×[0,1000]` 比例坐标；X、Y 各有自己的 baseline。1 X unit 与 1 Y unit 不必对应相同 physical 像素距离，因而 raster 上的等分网格可以是长方形。映射回 source 时，x/y 分别乘以实际 integer crop width/height。

---

# 18. Stage-2 Raster Resize

Stage-2 image：`native rectangular crop`；按下式 resize，再绘制 `grid/ticks/labels`：

```text
M = max(crop_width, crop_height)
raster_width  = round_half_up(detail_long_edge_px * crop_width  / M)
raster_height = round_half_up(detail_long_edge_px * crop_height / M)
```

`detail_long_edge_px` 是 raster 长边；raster aspect **匹配实际 physical crop 的像素宽高比**，不能为了 normalized 坐标拉伸图像。中心 1920×1200 crop 生成 1000×625；1920×624 edge crop 生成 1000×325。不得 padding，亦不得强制短边到 1000。**两种 raster 的 x/y 坐标 domain 都是 `[0,1000]`**。坐标 inverse transform 仍基于 exact integer physical crop rect。改变长边会使旧 calibration fingerprint 失效，须重新通过 §61 后才能启用 interaction。

---

# 19. Resize 顺序

必须：`native crop`；`→ resize`；`→ grid`；`→ ticks`；`→ labels`。禁止：`native crop`；`→ grid`；`→ resize`。因为 interpolation 会导致：`grid blur`、`tick blur`、`label blur`、overlay 边缘污染 GUI 默认 interpolation：`PIL.Image.Resampling.LANCZOS`。

---

# 20. Stage-2 Grid

默认：`grid_enabled = true`；`grid_divisions = 8`。Stage-2 的 X、Y 两轴**各自**将完整的 `[0,1000]` 分成八个等距 interval，guide spacing 为：

```text
grid_step = 1000 / 8
          = 125
```

两轴完整刻度都严格为 `0,125,250,375,500,625,750,875,1000`。每一条内部竖线跨越整张 raster 的高度，每一条内部横线跨越整张 raster 的宽度；边界线位于首尾。八个 X cell 和八个 Y cell 都没有余下的部分格。1000×625 的 raster 每个 cell 约为 125×78 像素，是合法长方形；不得为了做方格而补白、拉伸或改变源 crop。

---

# 21. Independent-Axis Grid

Stage-2 raster 的宽高比保持实际 physical crop 的宽高比，并可随 edge clipping 变化；X/Y local domain 仍各为 0–1000。同一 tick 值在不同轴上表示各自 image extent 的相同比例，**不**表示相同物理像素距离。横线按 raster height 八等分，竖线按 raster width 八等分；grid 不改变原始 crop 几何或 inverse transform。

---

# 22. Grid Rendering 要求

必须：`thin lines`、`low/moderate opacity`、`high enough contrast`、`minimum text occlusion`、`labels inside existing image canvas`、不增加 canvas padding。每轴只用八等分的 125-unit major ticks；不得显示 62.5/63/313 等旧版分格或取整标签。刻度字体比先前默认字体更小，绘制时 `stroke_width=0`，不加黑色描边或阴影。为让分区处理图像的模型也能读到局部刻度，每个 X 刻度值沿对应竖线在八个 row cell 各重复一次，写作 `x=0`、`x=125`、…、`x=1000`；每个 Y 刻度值沿对应横线在八个 column cell 各重复一次，写作 `y=0`、`y=125`、…、`y=1000`。包括边界值 `0`、`1000`，每轴每个值恰好出现八次；例如 `x=125` 和 `y=125` 各出现八次。X label 放在各竖线格段靠近上端的位置，Y label 放在各横线格段中点；避免交点叠字，并尽量把格段中部留给细小目标。重复标签只提供局部坐标参照，不产生额外坐标轴或新的格线。

## 22.1 Grid/Tick Label 语义

guide 的位置由 X/Y 各自 `tick/1000` 乘以当前 raster width/height 得出。v1.8 live Agent 的 prompt、tool schema 和图上 labels 都必须声明两个独立的 0–1000 相对轴；每个轴的边界分别标 0 和 1000，内部为 125 至 875。不能把 1000×625 raster 的 625 像素高度写成 Y 坐标上界，亦不能显示 physical pixel ticks。线条与标签表示同一归一化位置；不得把相同位置标成另一套单位。

125-unit 刻度无需取整。标签必须清晰可见且不能彼此重叠；边界 X/Y 标签也按上述八格段重复，并限制在原有 canvas 内，不得省略某个完整刻度或改写数值。模型按目标所处的格段读最近的 `x=…` / `y=…` 标签；同值的八个副本是同一轴刻度，不表示八个不同坐标。

---

# 23. Grid 是 grounding aid，不是 geometry correctness

Two-stage architecture 的 correctness 不允许依赖：`grid 一定提高模型准确率`。因此实现仍允许 `grid_enabled = false`，MVP benchmark 比较同一输入的无 grid 与八等分 grid，并记录结果。v1.8 的 live grid 固定为两轴各八等分；历史 16 等分结果不能冒充本版本校准。

---

# 24. Stage-2 Tool Contract

Stage-2 使用：

```text
interact_detail(
    crop_id,
    x,
    y,
    action
)
```

这是 adapter 解码后的内部 canonical contract。`crop_id` 是可信 backend 的当前 crop 绑定字段，必须属于当前授权 request、context identity 与 capture/input binding；它不出现在 Agent 的 executable argv，也不能由模型回传或覆盖。Agent 只输出当前 detail image 上的 `x/y/action`，runtime 将其绑定到最近一次成功的 `inspect_region` crop。`x/y` 是精确 canonical local coordinates，**两轴分别满足 `0<=x<=1000`、`0<=y<=1000`**，即使可见 detail image 是 1000×325。模型 raw 值先由 §60 adapter 验证；要求有限实数，拒绝 NaN/Infinity、错误类型、越界值与未声明的额外字段；整数协议可接受数值等价的 `889.0`，这不是越界修复。浮点 epsilon 仅用于内部计算误差，不容忍模型输出超过域边界。

`action` 只能取 §24.1 的六项且必须等于 `GroundingRequest.requested_action`；否则 `INVALID_ACTION`。非法坐标返回 `INVALID_LOCAL_COORDINATE`，禁止 silent clamp。重复 request/crop dispatch 按 §35 返回已有状态，不能再次执行。

## 24.1 六种鼠标交互规则

Stage-2 local coordinate 完成逆变换并通过 pre-action validation 后，Backend 必须先把鼠标移动到转换后的目标坐标，再在同一 interaction transaction 中立即执行指定动作，不加入额外 dwell：
**（1）** `left_click`：鼠标挪动至转换后的坐标，然后立马左键单击；
**（2）** `right_click`：鼠标挪动至转换后的坐标，然后立马右键单击；
**（3）** `left_double_click`：鼠标挪动至转换后的坐标，然后立马左键双击；
**（4）** `right_double_click`：鼠标挪动至转换后的坐标，然后立马右键双击；
**（5）** `scroll_up_1`：鼠标挪动至转换后的坐标，然后立马滚轮向上滚一个单位；
**（6）** `scroll_down_1`：鼠标挪动至转换后的坐标，然后立马滚轮向下滚一个单位。
`double_click` 必须发送两个连续的同键 click pair，inter-click interval 使用可配置、经目标平台验证的值，落在可识别的 double-click 窗口内；这是动作内部必要间隔，不是 move 后的 dwell；`scroll_*_1` 的“一个单位”定义为 backend 抽象层的一个离散 vertical wheel step。

---

# 25. Stage-2 Local → Crop Ratio

adapter 输出 canonical `u`、`v`。计算：`rx = u / 1000`；`ry = v / 1000`。得到：`rx ∈ [0,1]`；`ry ∈ [0,1]`。然后映射到 exact crop：

```text
source_x =
    x0 + rx × (x1-x0)
source_y =
    y0 + ry × (y1-y0)
```

这里：`source_x`、`source_y`，属于连续 image geometry。它们不是数组 pixel index。

---

# 26. Continuous Geometry 与 Pixel Index 必须区分

归一化：`X = 1000`。语义是：image 的最右侧边界。它不是：`pixel index = width`。因此系统必须区分：`Normalized Continuous Coordinate`、`Raster Pixel Index`、`Backend Input Coordinate`。Normalized edge：`0..1000`，允许包含 endpoint。Raster sample：`0..Wp-1`。不允许：`Wp`。二者不得混用。

---

# 27. Monitor Global Ratio

得到 continuous source point 后：`nx = source_x / Wp`；`ny = source_y / Hp`。或者等价理解为：`global_normalized_x / 1000`、`global_normalized_y / 1000`。正式 Vision → Platform interface 只有：`nx ∈ [0,1]`；`ny ∈ [0,1]`。Vision subsystem 不知道：`Wayland logical size`、`fractional scale`、`X11 root geometry`、`compositor topology`。

---

# 28. Upper Boundary Safety

`nx = 1.0` 在语义上合法，代表 image right edge，但 OS input backend 必须保证最终 absolute pointer point 确实落在 backend advertised input region 内。Wayland backend 应依次执行：
1. 计算 candidate logical point；
2. 使用对应 libei region 验证；
3. 若恰好因 upper-boundary endpoint 落在 region 外，修正为 backend/EI wire 精度实际可表示的最邻近内部点；不能只在本机 double 上减一个在序列化后消失的 epsilon；
4. 按实际发送精度量化/序列化后再次验证 region containment；
5. 若仍不在 region 内，拒绝本次 interaction。禁止 `model 1200 → clamp to 1000 → execute at edge` 这种 fail-open 行为。

---

# 29. Stage-2 必须重新进行 Vision Inference

Stage 2 必须：根据新的 detail image 本身重新定位 target。不能仅凭 Stage-1 reasoning memory：`“它大概就在这里”`。直接 extrapolate。

---

# 30. GroundingRequest Context

每次 Computer Use grounding request 必须显式保存：

```text
GroundingRequest {
    request_id
    target_description
    requested_action
    trusted_context_identity
    capture_input_binding_id
    pinned_protocol_descriptor_and_calibration_fingerprint
    request_dispatch_state
}
```

上述身份、binding、协议 descriptor 与状态由可信调用链/后端管理，不是模型可以覆盖的 executable arguments；同一 request 的两个 stage 使用绑定的协议配置与各自 image metadata，且必须留在同一个 top-level Agent context。

例如 `target_description = "VS Code 左下角的 Settings 齿轮按钮"`，`requested_action = "left_click"`。Agent 首先通过 executable 获得 overview；同一个具备视觉能力的 Agent 依据该图像输出数值型 normalized Stage-1 `(x,y,zoom)`；inspect executable 返回 detail；同一 Agent 再依据 detail image 输出数值型 Stage-2 局部坐标与可信 requested action。

---

# 31. Same-Agent Grounding 与 Prefix Covenant 例外

Stage-1 与 Stage-2 是同一个 Agent context 的连续 tool turns，不是 backend 私有 inference call。Stage-1 只允许依据当前 `observe` observation 输出粗略相对坐标；Stage-2 必须依据 executable 返回的、来自同一 frozen frame 的新 detail image 重新定位，不能仅凭 Stage-1 reasoning 或 server 侧隐藏记忆外推。Stage-2 输出只包含 precise local relative coordinate 和可信 requested action；Agent 不做 local→global 数学。

本 computer-use 流程新增一个严格限定的 prefix exception：为了控制截图 tokens，只有 provenance 为 `computer_use_observation` 的历史图像块可以被省略。每次成功接收一个新的 computer-use observation 后，runtime 在下一次同一 top-level Agent tool turn 提交前，把更早的 computer-use 图像块按逻辑连续 image container 聚合替换为一个稳定文本占位符；当前最新 observation 的 image URL 保留。所有角色、文本、tool calls、tool observations、顺序及其他 provenance 图片必须原样保留。替换采用 staged/commit 状态机：executable observation 先作为带真实图像的 ActionStep 写入 canonical Agent memory；构造下一次普通 Agent inference request 时生成带占位符的候选消息；只有该 request 通过接受/发送屏障后才提交 omission projection，预接受校验、传输失败、空响应 fallback、Stop、Answer Now 或 compact 不提交且不消费 pending omission。一次普通用户轮次可以包含多个这样的 tool turns，不能把“用户轮次”误解为新用户消息。

该例外只适用于绑定的 top-level Agent context，不能扫描或修改 child、sibling 或其他 session；不能用于普通用户上传图片、Vision Automation 图片或任何非 computer-use screenshot。事件名固定为 `computer_use_historical_image_omission`。省略后恢复普通 append-only 稳定性；不得恢复已经省略的截图。成功的 `reobserve` 会产生新的 latest image，因此可以按同一规则提交省略；失败或预接受的 `reobserve` 不消费该例外。compact、Stop、Answer Now 及其他 context 请求均不能消费或扩大该例外。

---

# 32. Stage-1 Prompt Contract

同一个 Agent 必须使用自身 VISION 能力查看当前 overview，并在稳定的 Agent context 中按当前 executable operation schema 输出数值型 `inspect_region(x,y,zoom)`。Stage-1 坐标固定为 normalized `[0,1000]×[0,1000]`，不随 Stage-2 wire protocol 改变。共同语义：`You are viewing the latest clean screenshot of one monitor.`、`Locate the requested target approximately.`、`Do not perform the final interaction.`、`Return numeric normalized x and y plus zoom for inspect_region.` zoom 仅为 `2,4,8,16`，选择足以辨认目标的最低 zoom。自然语言 target 只限定要找什么；只输出“打开微信”等自然语言而没有坐标属于无效模型输出，必须按 parser error 回灌纠正，不能由 backend 猜坐标或代替 Agent 规划。该输出不能交给隐藏的第二模型。

Stage-1 一律声明边界 `(0,0)..(1000,1000)` 及水平/垂直相对比例单位；禁止 Stage-1 使用 image-pixel wire domain。overview 与 raster 保持长宽比，坐标分别归一化两轴，不能把像素原值直接作为输出。

Stage-1 prompt 必须明确按实际 overview 尺寸分别归一化：`x=px*1000/image_width`、`y=py*1000/image_height`。overview 保持物理屏幕长宽比，所以两轴公式不能共用一个像素基准。

---

# 33. Stage-2 Prompt Contract

adapter 动态生成当前 crop 的可信 backend metadata、target、requested action、wire protocol、`model_width/model_height`、边界语义及对应 schema；`crop_id` 只留在 backend/runtime state，不放进模型可覆盖的 executable 参数。grid labels 必须使用同一协议实例。模型输出坐标按协议类型验证，不能把 canonical dimensions 无条件塞进所有模型 prompt。

共同语义：`You are viewing a new detail image derived from the same frozen desktop observation.`、`Use THIS image to determine the final interaction point.`、`Do not calculate global desktop coordinates.`、`Return the Stage-2 precise x,y and trusted requested action for the executable interact_detail operation; do not provide crop/session/native IDs.`、`If the target is not clearly identifiable, call reobserve() instead of guessing.`

Stage-1 与 Stage-2 的 live Agent prompt 均声明两个独立 normalized `[0,1000]` 轴。Stage-2 raster aspect 可随 crop clipping 改变，**不会改变任一轴的坐标上界**。图上 0、125、…、1000 的 ticks、工具 schema 和解码器必须一致。历史 `image_pixel` adapter 可保留作离线诊断比较，不得进入 v1.8 live Agent 路径。

---

# 34. Stage-2 Coordinate Convention 风险

协议由 checkpoint、prompt/tool schema、adapter、renderer 与 serving preprocessing 共同决定，不能按模型名字推断。[Qwen3-VL 官方 ComputerUse utility](https://github.com/QwenLM/Qwen3-VL/blob/main/cookbooks/utils/agent_function_call.py) 会把 display width/height 注入 prompt，并描述该分辨率下的 pixel coordinates；这不证明所有 Qwen3-VL 部署都采用像素或固定 1000²。

## 34.1 固定 Canonical Geometry，可配置 Wire Protocol

canonical Stage-2 local domain 固定为 `[0,1000]²`，不是 A/B 配置项；实际 physical crop aspect 只影响等比缩放后的 detail bitmap 宽高比。v1.8 live wire protocol 的 prompt/schema/grid labels/decoder 必须统一使用两个独立归一化轴。历史 `image_pixel` 可用于隔离的离线对照，但不得暴露给 live Agent。所有 inverse transform 都分别按实际物理 crop width/height 映射，不允许改变 crop 几何来补偿协议。

## 34.2 Convention 混淆的不可检测误读

坐标值在 wire domain 内不能证明模型看对了图或采用了正确的 image preprocessing。真实目标位于 70% vertical 时，模型若直接返回 1000×563 overview 的像素 y=394，而当前协议要求 normalized y=700，这个错误值仍然合法，不会被范围校验发现。因此：

```text
target vertical ratio = 0.70
overview visible height = 563 pixels
correct relative_1000 y = 0.70 * 1000 = 700
raw image pixel y near target = 0.70 * 563 = 394
mistaking pixel y for normalized y points to 39.4% of the monitor
```

防御优先级（禁止 runtime 动态猜 convention）：
1. Model Adapter 显式声明 coordinate protocol（§60）；
2. 部署前 calibration（§61）；
3. prompt、grid labels 与该 protocol/domain 完全一致 （§22.1、§33）；
4. audit log 做 convention drift detection —— 仅诊断告警，不作为 runtime correctness 机制。

---

# 35. Predispatch Retry 与 At-Most-Once Dispatch

```text
FRESH_OBSERVATION → STAGE1_OVERVIEW → DETAIL_READY
    → PREACTION_VALIDATE → DISPATCH_RESERVED → MOVE → ACTION_SENT
                                   └─可能已发送后失败 → ACTION_DELIVERY_UNKNOWN
```

Stage-2 不允许 `inspect_region(global_x,global_y)`；重新定位必须 `reobserve()`。`ACTION_SENT` 与 `ACTION_DELIVERY_UNKNOWN` 都是本 request 的 terminal 状态，前者仅表示事件已提交，不表示应用完成目标动作。

## 35.1 错误与异常状态

输入/感知：`INVALID_GLOBAL_COORDINATE`、`INVALID_ZOOM`、`INVALID_LOCAL_COORDINATE`、`INVALID_ACTION`、`TARGET_NOT_VISIBLE`、`TARGET_AMBIGUOUS`。时效/绑定：`STALE_FRAME`、`STALE_CHECK_INDETERMINATE`、`CAPTURE_STALLED`、`GEOMETRY_CHANGED`、`INVALID_CROP_ID`（缺失、未知或已失效 ID）、`INVALID_CONTEXT_BINDING`、`UNSUPPORTED_CAPTURE_GEOMETRY`、`UNPROVEN_MONITOR_COVERAGE`、`INPUT_REGION_NOT_FOUND`、`AMBIGUOUS_INPUT_REGION`、`INPUT_REGION_MISMATCH`。环境/协议：`INPUT_CAPABILITY_MISSING`、`INPUT_DEVICE_NOT_READY`、`CALIBRATION_REQUIRED`、`PORTAL_CANCELLED`、`SESSION_CLOSED`。预算耗尽：`PREDISPATCH_BUDGET_EXHAUSTED`。每种异常必须映射为明确状态与审计记录，不能静默选用另一 region/protocol。

## 35.2 仅 Dispatch 前可重试

感知失败（目标不清、wrong crop、stale）使 crop 失效后 fresh observation；protocol failure 原样返回精确错误位置，允许有限 correction；配置/权限/校准错误需修复其原因，不能反复请求模型猜测。

物理输入在同一受控 seat 上跨 request 串行。持有 dispatch lock 后再次检查授权/context、geometry、region/device 状态和 freshness，然后原子保留/消费 request-level `request_id` dispatch identity，同时绑定并消费当前 `crop_id`，才允许发出首个事件。同一 request 换一个 crop 也不能绕过 reservation/terminal state 再次 dispatch；重复请求返回已有状态。身份记录至少保留整个绑定生命周期。若无法可靠维护身份记录，绑定关闭并使旧 IDs 失效，禁止恢复重放。

一旦任何 motion/button/scroll 可能进入发送路径，出现异常均保守记录 `ACTION_DELIVERY_UNKNOWN`；不能回到 predispatch retry，不能声称“没有产生动作”。只有明确证明尚无输入事件发出的失败才可重试。`finally` 对本 transaction 可能按下的 button 做 best-effort release（单独 frame），不补发 click，不释放其他输入源的 button；记录 cleanup 的成功/不确定结果。fresh observation 可用于用户或上层重新判断，但不会自动重放原请求。无应用级 action verification，因此不承诺 exactly-once 应用效果。

## 35.3 有限预算（配置化）

默认 `max_tool_contract_retries=1`、`max_grounding_reobserves=2`、`max_pre_dispatch_attempts=3`。第三项计数整个 request 的 grounding attempt（首次与每次 correction/reobserve 均消耗一次），确保跨分支有全局上界。预算耗尽只发生在首个可能输入之前，返回 `PREDISPATCH_BUDGET_EXHAUSTED`，无动作；dispatch 后不使用此预算。

---

# 36. `reobserve()`

Executable operation：`reobserve(reason?)`，只在 predispatch grounding 中自动使用；terminal dispatch 后仅允许独立 fresh observation，不重放旧动作。`reason` 可为 `target_not_visible`、`target_ambiguous`、`wrong_region`、`ui_changed`、`stale`、`other`。调用后：当前 crop 不再执行 interaction、捕获 fresh observation、回到 Stage 1，并由同一个 Agent 接收新图像。禁止无限 recursive zoom；reobserve 不会消费或扩大历史图片省略例外。

---

# 37. Wayland Capture Backend

Wayland MVP 使用：

```text
XDG RemoteDesktop / ScreenCast Portal
        ↓
PipeWire
        ↓
persistent monitor stream
```

不得 hot-path：`observation`；`→ one-shot screenshot command`；`→ PNG file`。应该：

```text
PipeWire stream
      ↓
latest complete buffer slot
      ↓
freeze + canonicalize_frame() on demand
      ↓
immutable full-monitor canonical raster
```

旧的未消费 frame：`drop`。不需要处理全部 60 FPS。

---

# 38. Wayland Portal Session Lifecycle

Wayland RemoteDesktop + ScreenCast 应尽量使用同一个 portal session。逻辑顺序：

```text
RemoteDesktop.CreateSession
        ↓
RemoteDesktop.SelectDevices
        ↓
ScreenCast.SelectSources
        ↓
RemoteDesktop.Start
        ↓
ScreenCast.OpenPipeWireRemote
        ↓
RemoteDesktop.ConnectToEIS
```

返回 Request handle 的异步方法必须收到成功 Response 才进入下一步；OpenPipeWireRemote / ConnectToEIS 等直接返回 FD 的方法检查直接返回结果/错误，不等待不存在的 Request Response。处理 cancellation、失败、超时、Session Closed，并关闭关联 stream/EIS 使 frame/crop 与授权失效。combined session 的 persistence/restore 参数只由 RemoteDesktop.SelectDevices 管理，不在 ScreenCast.SelectSources 重复设置；Start 后才可 ConnectToEIS。EIS 建立后输入只走该通路，不 fallback 到 Portal Notify*；EIS 断开使 input binding 失效，重新建立授权/绑定前禁止输入。见 [RemoteDesktop 官方协议](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html)。

MVP monitor source：`types = MONITOR`；`multiple = false`。cursor：`prefer HIDDEN`。如果 portal 不支持 HIDDEN：使用可用的最佳 cursor mode，并记录 capability。

---

# 39. Wayland Major Monitor 的定义

在标准 portal 路径中：major monitor = 当前 RemoteDesktop / ScreenCast session 实际授权并返回的唯一 monitor stream。不要要求：`通过私有 GNOME API 强行选择 primary monitor`。用户显式选择或 restore token 恢复的 monitor：就是当前 session 的 major monitor。

---

# 40. Wayland Stream/Input Region Binding

对当前已授权 capture/input binding 中所有 eligible absolute device/region 建立候选集合。双方都提供 `mapping_id` 时必须 exact match 且结果唯一；已知 ID mismatch 返回 `INPUT_REGION_MISMATCH`，不得退回尺寸相似或唯一 monitor 猜测。mapping_id 是 device 内标识，不假设跨 device 全局唯一；多个 eligible matching pairs 返回 `AMBIGUOUS_INPUT_REGION`。

任一端缺少 optional ID 时，仅可在 session-scoped eligible device/region pair 唯一、且另有可靠证据确立 canonical raster 与该 region 完整 monitor 对应时 fallback。唯一数量本身不是覆盖范围证明。零候选返回 `INPUT_REGION_NOT_FOUND`，覆盖未证明返回 `UNPROVEN_MONITOR_COVERAGE`。禁止 first-match。保存 match evidence；region/device 重建使旧绑定失效。MVP 的单 device 限制见 §45，不代表 libei 本身仅支持一个设备或 region。见 [ScreenCast stream properties](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html)。

---

# 41. Wayland Fractional Scaling

绝对禁止：`input_x = screenshot_x / 1.5`。或：`input_x = screenshot_x × 1.5`。Vision 输出：`nx`、`ny`。假设 matching libei region：`origin = (ox,oy)`；`size   = (Rw,Rh)`。candidate：`input_x = ox + nx × Rw`；`input_y = oy + ny × Rh`。然后进行：`region containment validation`。fractional scale 由 compositor / EIS mapping 自己处理。

---

# 42. Wayland Absolute Pointer Interaction

路径：`RemoteDesktop → ConnectToEIS → libei`。发送前设备必须处于 resumed 状态、能力齐备并已 `start_emulating`；paused/removed、session 关闭或 region 变化后立即停止 dispatch。resume 后重新检查绑定与能力并按 libei lifecycle 恢复 emulation。使用 `CLOCK_MONOTONIC` 的微秒 timestamp。

每个 button 状态跃迁使用独立 device frame；同一 frame 不得对同一 button 重复转换：

```text
move:         motion_absolute(x,y) → frame(t0)
single click: button_down → frame(t1) → button_up → frame(t2)
double click: first down/frame/up/frame
              → validated configured inter-click interval
              → second down/frame/up/frame
wheel step:   scroll_discrete(vertical=±120) → frame(t1)
```

motion/frame 后不加 dwell，立即开始所选 action；双击内部间隔遵循 §24.1。`frame` 划分 logical hardware event，**不是 transport flush 的别名**；transport flush/readiness 按 libei API 驱动，不能合并上述 frame。离散 wheel 的一单位为 120，up/down 符号须由 backend conformance test 确认；不得再发同一动作的 smooth scroll 造成重复。孤立 discrete wheel step 不普遍要求 `scroll_stop`；若所选 scroll sequence 需要 stop，单独 `scroll_stop → frame`，不与 scroll event 放在同一 frame。

遵循 §35 的锁、去重与异常 cleanup，最多报告 `ACTION_SENT`，不报告 `ACTION_SUCCEEDED`。协议依据：[device frame](https://libinput.pages.freedesktop.org/libei/interfaces/ei_device/index.html)、[scroll](https://libinput.pages.freedesktop.org/libei/interfaces/ei_scroll/index.html)、[sender API](https://libinput.pages.freedesktop.org/libei/api/group__libei-sender.html)。

---

# 43. X11 Backend

True X11 session 的 Capture 使用 `XShm / MIT-SHM`，Input 使用 `XTEST`。如果 major monitor root rect 为 `origin = (ox,oy)`、`width = Rw`、`height = Rh`，则先按 §62 对非负 monitor-local offset 取整：`ix=min(Rw-1, round_half_up_positive(nx*Rw))`、`iy=min(Rh-1, round_half_up_positive(ny*Rh))`，再加允许为负的整数 origin：`root_x=ox+ix`、`root_y=oy+iy`。前提是 ratio 已合法；这里的 upper-edge 限制不能修复非法模型坐标。校验 monitor/root 可寻址范围；实际 X server 不支持的 topology 必须拒绝。六种动作与 §24.1 保持同一语义：先把 pointer 移到 `(root_x,root_y)`，随后立即执行 `left/right single click`、`left/right double click` 或一个单位的 `scroll up/down`；X11 backend 只负责把 canonical action 映射成对应 XTEST 事件序列，不得改变 action 语义。

---

# 44. XWayland

如果：`desktop session = Wayland`。但 target app 是 XWayland：仍然使用：`Wayland capture` + `Wayland input`。Backend 根据：desktop session 选择。不是根据：target application toolkit。

---

# 45. Backend Capability Detection

`doctor.py` 检查真实 capability，而非仅 `$WAYLAND_DISPLAY`。Wayland 包含 portals、PipeWire、ConnectToEIS、cursor mode、capture canonicalization/coverage、mapping evidence，以及 libei device 的 `POINTER_ABSOLUTE`、`BUTTON`、`SCROLL`、resumed/emulation 状态。

click 可用至少要求所选设备同时具备 `POINTER_ABSOLUTE + BUTTON`；scroll 还需 `SCROLL`。MVP 六动作完整 ready 必须三者齐备；缺能力只能报告明确 degraded/unavailable，不能对不支持动作宣称 ready。MVP 要求唯一选定设备同时提供这些能力，跨 device 协作属于未实现范围；libei 协议本身允许多设备/region。X11 检查 DISPLAY、MIT-SHM、XTEST、monitor geometry、坐标可寻址范围。输出 machine-readable diagnosis 和缺失原因；设备暂停/移除即时撤销 ready。

---

# 46. Image Transport

默认：`PNG`。原因：GUI 包含：`small text`、`sharp edges`、`thin lines`、small icons 允许：`data:image/png;base64,...`。但内部不得：`save PNG`；`→ reopen`；`→ base64`。必须：`memory buffer`；`→ crop`；`→ resize`；`→ overlay`；`→ in-memory encode`；`→ VLM`。

---

# 47. Demand-Driven Processing

PipeWire 可以持续 capture。但：`freeze`、`resize`、`crop`、`grid`、`PNG encode`、`base64`。只在 agent 请求 observation 时进行。禁止：`encode every PipeWire frame`。

---

# 48. Pre-Action Staleness Check

执行前比较 frozen observation 与当前 canonical frame 的 `global coarse comparison + target neighborhood comparison`（可用 downscaled grayscale diff、perceptual hash、SSIM-like score），并检查 observation age、stream generation、frame sequence、capture/arrival monotonic age。相同 sequence 的旧帧不能仅因像素相同就判为新鲜；damage-driven stream 允许静态内容不更新，但必须有后端活性/同步证据，否则按配置的最大年龄 fail closed。断流、冻结或无法获得可信 fresh sample 返回 `CAPTURE_STALLED` / `STALE_CHECK_INDETERMINATE`。

结果 `FRESH`、`STALE`、`INDETERMINATE`；MVP 默认仅 FRESH 可 dispatch，后两者均阻止动作。阈值、最大 observation age、最大 sample age、允许验证到发出首个事件的间隔必须配置化并通过测试，不凭某个低像素差异忽略过期时间。持有 seat dispatch lock 后重查 geometry/device/binding 与 freshness；若锁等待使验证过期，重新验证。staleness check 只能缓解竞态，不能保证 UI 检查与系统输入原子化。

---

# 49. Frame / Crop Metadata

Frame 保存 `frame_id`、capture/input binding ID、context identity、monitor ID、geometry version、stream generation/sequence、monotonic timestamps、canonical source dimensions，以及 §8 canonicalization/coverage evidence。

DetailCrop 保存 `crop_id`、request/observation/frame IDs、同一绑定/version、`x0,y0,x1,y1`、整数 crop 与 detail raster dimensions、精确 canonical `local_width/local_height`、`exact_crop_aspect`、独立 `model_width/model_height`、wire domain/type/extent/index semantics、protocol/calibration fingerprint、rough global coordinates、zoom、grid settings、创建时间和 dispatch identity/state。禁止 model-facing 整数上界覆盖 canonical dimensions；模型只收到当前调用所需的 adapter metadata，不暴露可替换授权身份。

---

# 50. Audit Log

每次 interaction attempt 记录：`request_id`、`observation_id`、`frame_id`、`crop_id`、`geometry_version`、`monitor_id`、`target_description`、`requested_action`、`rough_x`、`rough_y`、`requested_zoom`、`crop_x0`、`crop_y0`、`crop_x1`、`crop_y1`、`local_width`、`local_height`、`final_local_x`、`final_local_y`、`native_source_x`、`native_source_y`、`monitor_ratio_x`、`monitor_ratio_y`、`backend_type`、`backend_input_x`、`backend_input_y`、`mapping_id`、`model_coordinate_protocol`、`stale_check_result`、`executed_action`、`action_sent`。另记录 raw model values、wire domain 与 canonical values、protocol/calibration fingerprint、canonicalization/coverage evidence、device/region IDs 与匹配依据、capture/arrival/validation/dispatch monotonic times、freshness age/sequence、dispatch identity/terminal state、最后一个可能发出的 event/frame、cleanup 结果；`action_sent` 必须能表达 unknown，不能用 false 隐藏部分发送。目标是区分 `VLM grounding error`、adapter error 与 `coordinate/backend/action mapping error`。

---

# 51. Coordinate Module Ownership

`coordinates.py` 是 correctness-critical module。它必须成为以下数学转换的唯一实现：`Stage1 normalized`；`→ crop geometry`；`normalized crop`；`→ native raster rect`；`Stage2 local`；`→ crop ratio`；`crop ratio`；`→ source continuous coordinate`；`source coordinate`；`→ monitor ratio`；`monitor ratio`；`→ backend input coordinate`。禁止在：`tool handler`、`agent logic`、`Wayland backend`、X11 backend 复制一套不同公式。`coordinates.py` 只处理 canonical coordinate 数学，完全不感知模型 coordinate convention；模型 prompt/schema/grid-label 编码与 raw output 解码唯一归双向 Model Coordinate Protocol Adapter（§60）；grid renderer 只负责绘图，不自行解释 protocol。

---

# 52. 仓库目录与责任边界

```text
backend/skills/computer-use-vision/
├── SKILL.md
└── bin/vision_desktop                 # session-bound executable marker

backend/computer_use/
├── contracts.py                        # immutable frame/crop/protocol records
├── coordinates.py                      # canonical geometry math only
├── model_adapter.py                    # model wire encode/decode
├── render.py                           # overview/detail/grid artifacts
├── grounding.py                        # same-Agent state transitions
├── executable_protocol.py              # fixed argv + trusted provenance
├── executable_backend.py               # protocol → engine adapter
├── computer_use_history.py             # top-level screenshot projection
├── dispatch.py                          # at-most-once physical input
├── native_client.py                     # authenticated helper IPC
└── native/                              # C helper; readiness remains fail-closed

tests/computer_use/
├── test_executable_observation_protocol.py
├── test_executable_backend.py
├── test_agent_executable_attachment.py
├── test_same_agent_two_stage.py
├── test_computer_use_image_history.py
├── test_coordinate_roundtrip.py
├── test_capture_canonicalization.py
└── ...
```

---

# 53. Property-Based 与协议集成测试

至少 `10,000 randomized geometry cases`，覆盖 source W/H、合法/非法 Stage1 coordinates、zoom、edge clipping、极细长/portrait crops、canonical local point、各 model protocol、region sizes 与 signed origins。验证合法 crop、无 padding、base raster 来自源 frame（不把 grid overlay 误判为源像素）、精确 canonical isotropy、ratio 范围、单调性、无翻轴、version 不复用及 §62 分项误差预算。分别测试 interval floor/ceil 与 scalar half-up，不能使用“所有取整同一规则”的断言。

额外必须覆盖：

- 两阶段 prompt/schema/grid 标签与 decoder 的协议闭环；1000/1536 renderer、portrait/landscape、short-domain min 1、pixel extent/index endpoints、无二次 canonical rounding、calibration fingerprint 失效。
- buffer 复用/stride/planes、使用非对称标记图验证 crop/rotation/flip 单次 canonicalization、完整 monitor 证明与局部 crop 拒绝、transform 变化 invalidation。
- mapping ID missing/mismatch/跨 device 重复、多 region、唯一但 coverage 未证明、各 capability 缺失、paused/removed、Start/Response/cancel/Closed lifecycle。
- 六动作真实 event trace：motion/frame 先行、down/up 独立 frame、双击间隔、离散 120 单位且无重复 smooth、可选 stop 独立 frame、monotonic 微秒。
- 并发 request 序列化、重复 dispatch ID、同 request 更换 crop 仍不能二次 dispatch、validation 后 geometry 改变、freeze/stale stream、首次事件前失败可重试；在每个可能发送点注入异常，证明无自动 replay、状态 unknown 与 best-effort release。
- 同一 Agent 的 executable observation 会进入 canonical ActionStep observation；验证历史截图省略只作用于 `computer_use_observation` provenance，必须保留其他 image/tool traces、角色、文本、顺序与稳定 prefix。

geometry 测试不依赖 VLM；libei event mock 之外仍需目标 GNOME compositor 的 conformance test。

---

# 54. Endpoint Tests

必须专门测试：`(0,0)`、`(1000,0)`、`(0,1000)`、`(1000,1000)`、`(1,500)`、`(999,500)`、`(500,1)`、`(500,999)`。并证明：`normalized endpoint`。永远不会错误地被当成：`out-of-range pixel index`。同时最终 Wayland pointer interaction 必须满足：`target libei region containment`。

---

# 55. Edge / Zoom Test Matrix

每个 edge case：`2x`、`4x`、`8x`、`16x`。必须测试：`center`、`left edge`、`right edge`、`top edge`、`bottom edge`、four corners 验证：`clip correct`；`no padding`；`raster aspect matches the exact physical crop`；`Stage-2 wire X/Y both 0..1000`；`both grid axes have eight complete cells and labels 0,125,...,1000`；`each tick label appears once in each of eight orthogonal cell segments per axis`；`inverse transform correct`。

---

# 56. Resolution Test Matrix

至少：`1920×1080`、`2560×1440`、`3840×2160`、`1920×1200`、`2560×1600`、`1280×1024`、`1080×1920`。

---

# 57. Fractional Scaling Test Matrix

Wayland：`100%`、`125%`、`150%`、`175%`、`200%`。测试必须证明：`Vision normalized ratio`；`→ matching libei logical region`。而不是：`screenshot pixels / scale`。

---

# 58. Model Grounding Benchmark

Geometry correctness 和 VLM accuracy 必须分开测试。至少建立 GUI benchmark：`GTK`、`Qt`、`Electron`、`Chromium`、`Canvas`、`custom rendered UI`；包含 `large button`、`normal button`、`small toolbar icon`、`tiny icon`、`textless icon`、`edge target`、`corner target`、`dense toolbar`、`dark theme`、`light theme`、`high-DPI`。比较：`Stage1 direct interaction baseline`、`Two-stage without grid`、`Two-stage + 8-grid`；历史 16-grid/image-pixel 结果只能作为隔离对照，不能代替 v1.8 验收。至少记录：`Stage1 target-region recall`、`Stage2 point hit rate`、`end-to-end interaction success rate`、`reobserve rate`、`false-action rate`，并分别覆盖六种鼠标 action。

---

# 59. Performance Benchmark

至少记录：`capture freeze latency`、`frame copy latency`、`overview resize latency`、`native crop latency`、`detail resize latency`、`grid render latency`、`PNG encode latency`、`observation image attach latency`、`historical-image omission latency`、`Agent observe-turn latency`、`Agent Stage1-turn latency`、`Agent Stage2-turn latency`、`pointer move latency`、`action dispatch latency`、`total interaction latency`、`peak RSS`。MVP 不要求处理每帧。性能原则：必要的 Agent/VLM turns 是主要推理成本；截图 plumbing 与历史图片省略不得重复编码或写临时文件。

---

# 60. Model Coordinate Protocol Adapter（双向）

`model_adapter.py` 是唯一模型坐标协议边界，**同时覆盖两个 stage 的输入与输出**：

```text
Canonical geometry + actual model-visible image
    → adapter encode(prompt, tool schema, grid labels)
    → VLM raw tool output
    → strict domain validation + adapter decode(axis ratios)
    → canonical Stage1 [0,1000]² / exact Stage2 local coordinates
    → deterministic geometry → OS backend
```

显式配置协议，禁止 runtime auto-guess：

Stage-1 协议固定为 normalized `relative_1000`：两轴均输出 `[0,1000]` 数值。Stage-2 可使用下表中经校准的 wire protocol；Stage-2 的协议选择不得改变 Stage-1 坐标约定。

| protocol | 模型 wire domain | 解码到比例 |
|---|---|---|
| `relative_1000` | Stage1/Stage2 均为两个独立的 `[0,1000]` 轴 | `rx=x/1000`, `ry=y/1000` |
| `custom_isotropic_1000`（旧名称兼容） | Stage1/Stage2 同上；名称不再表示方形物理格 | `rx=x/1000`, `ry=y/1000` |
| `image_pixel` + `extent` | 历史离线对照；v1.8 live Agent 不可选 | `rx=x/W`, `ry=y/H` |
| `image_pixel` + `index_center` | 历史离线对照；v1.8 live Agent 不可选 | `rx=(x+0.5)/W`, `ry=(y+0.5)/H` |

协议必须声明输出类型（integer 或 finite float）、端点语义及精度；MVP custom/relative wire 默认 integer。`image_pixel` 必须显式选择语义，不能把 W 当合法 index，也不能把 index 隐式除以 W-1。其他 checkpoint convention 只能新增显式、经测试和校准的 adapter，不启发式兼容错误格式。

axis ratios 转成 Stage1 或 Stage2 的 `1000*rx,1000*ry`；无二次取整。server preprocessing 若改变坐标所指图像、增加 padding 或裁剪，adapter 必须有可验证的 inverse mapping；未知则 `CALIBRATION_REQUIRED`/fail closed。每次 encode/decode 绑定同一协议实例、图尺寸、stage 与 calibration fingerprint。

更换模型可更换 adapter/config 并重新运行测试，不能修改 crop/libei/XTEST 几何以补偿模型协议错误。

---

# 61. Model Coordinate Calibration

上线前由配置明确选择 protocol，再用 calibration suite **验证该协议**，不是从单点输出自动猜测或改写 protocol。失败可输出候选诊断，但必须修正配置并重测后才能启用输入。

两个 stage 均测试多比例位置、角落、靠边目标。Stage-1 overview 包含非正方形 portrait/landscape 图；Stage-2 raster 覆盖物理中心 crop 与 edge clipping 后的非方形 crop，逐例验证等比缩放的 physical aspect、双轴 0–1000 wire 范围和八等分标签。也测试不同 renderer 长边（例如 1000 与 1536）。Stage-1 以目标完整落在选定 crop 中的比例评价粗定位；Stage-2 以最终点命中率和误差评价精定位。重复样本测协议通过率与失败率；阈值配置化并在 deployment 报告记录。校准减少 convention mismatch 风险，不证明模型每次都能正确 grounding。

结果绑定 fingerprint：checkpoint/revision、system prompt/tool schema、adapter version/protocol、overview/detail renderer 参数、grid/label policy、serving stack 及实际 image preprocessing。任何相关项改变使旧结果失效，返回 `CALIBRATION_REQUIRED`，重新通过后才可 dispatch。允许离线调整 renderer 来校准，但未经新校准不得执行鼠标输入。离线样图、模型点标注、contact sheets 与探针报告统一写到仓库内稳定的 `computer-use-diagrams/`，不再将图像输出写到 `/tmp`；运行时截屏仍只在受控内存/IPC 中处理。

---

# 62. Deterministic Rounding 与误差预算

区分三类边界，禁止语言默认 bankers rounding：

1. **Crop interval enclosure**：§13 左/上 `floor`，右/下 `ceil`，clamp 到 source bounds；每边 enclosure 扩张小于 1 source raster unit。actual integer rect 一旦生成即为权威，不再按 nominal rect 回算。
2. **非负 scalar/dimension quantization**：`round_half_up_positive(x)=floor(x+0.5)`。crop interval 的 floor/ceil enclosure 与 model wire coordinates 的量化按各自协议执行。Stage-2 raster 尺寸按实际 physical crop 的宽高比将长边缩放至配置值；不能用 clipped relative extent 拉伸 bitmap，也不能把短边强制成正方形。canonical X/Y 都保持 0–1000；u/v 不取整。八等分 guide label 均为整数 125 的倍数。
3. **X11 point quantization**：先对合法非负 `nx*Rw, ny*Rh` 取 half-up 并限制到 `[0,Rw-1]` / `[0,Rh-1]`，再加 signed integer origin；不对负的 global coordinate 调用 positive rounding。Wayland 保留连续输入，仅对合法 upper endpoint 做 §28 的最小内部修正。

误差必须分项度量。整数 wire 域每轴上界 `D` 若代表均匀比例刻度，nearest quantization 最多约 `crop_axis/(2D)` source units；4K 不能统称“小于一个 raster pixel”。Stage-2 renderer 的 x/y resize 是两个独立线性映射，需纳入 renderer fingerprint 与校准；模型 grounding 误差单独统计。X11 内部点 rounding 不超过0.5 input unit，最外边界安全限制可接近1 input unit。

geometry roundtrip 测试在未量化 canonical/ratio 层应仅有数值精度误差；跨 wire/render/backend 的测试按上述各自尺度预算，不能拿 source pixels、input units 与 normalized units 相加而不换算。

---

# 63. MVP Acceptance Criteria

- **Platform**：GNOME Wayland 持续 capture 与六动作可用，libei lifecycle/capabilities、event-frame 顺序、120-unit wheel 与双击间隔经实际平台验证；映射唯一且完整 monitor coverage 成立。True X11 正确处理 signed origin 与 endpoints；XWayland 使用桌面所属 backend。
- **Geometry**：同一 canonical immutable frame 派生 overview/crop，无 padding；exact integer physical crop 权威；Stage-2 raster aspect 与该 physical crop 的像素宽高比匹配，local X/Y domain 始终各为 0..1000；八等分网格各轴均有完整的 0、125、…、1000 刻度且无部分格；inverse transform 分别映射回物理宽高；双向闭环、floor/ceil 与 half-up 分工、分项误差预算通过 property tests。
- **VLM/Agent**：任务第一步必须是 executable observe；同一个具备视觉能力的 Agent 必须从 overview 输出数值型 normalized Stage-1 坐标，并从 fresh detail 输出数值型 Stage-2 局部坐标；自然语言指令不能替代坐标，模型不做 local→global 数学。prompt/schema/labels/decoder 一致、protocol 显式选择并通过当前 fingerprint 校准；不清楚则拒绝。只按 §31 的 provenance 例外省略旧 computer-use screenshots。
- **Robustness**：ownership/transform/coverage、geometry version、capture freshness、default INDETERMINATE failclosed、上下文授权验证均有效；锁内复查、request 去重、仅 predispatch retry、partial dispatch unknown/cleanup 与禁止 replay 通过故障注入。
- **Performance**：hot path 无临时截图文件，不 encode 每帧；crop/resize/overlay 按需；提供 §58/§59 benchmark。

---

# 64. SKILL.md 职责

最终给 Agent 使用的 `SKILL.md` 不应塞入全部数学，只应说明：每个新任务第一步必须通过 allowlisted executable `observe`，之后什么时候 `inspect_region`、什么时候 `interact_detail`、什么时候 `reobserve`、如何传递 `requested_action`、target unclear 时绝不猜测。skill 不注册或调用 `computer_use` tool，也不允许后台私有 VLM grounding；它通过受控 `exec_command` argv 取得 multimodal observation。详细数学归 `backend/computer_use/coordinates.py`、`render.py` 与 `model_adapter.py`；详细平台实现归 `backend/computer_use/native/`、`native_client.py`；通过 tests 保证 correctness。项目接入必须走已有 session-bound wrapper / `OperationCapability` 授权链，skill、模型参数或 crop ID 本身不能授予桌面输入权限；上下文身份由可信调用链绑定。若向前端报告进展，沿统一 timeline event/projection，不另造 SSE 协议。

---

# 65. 最终核心 Invariants

1. Stage1 看完整、干净且比例保真的 monitor overview；两个 stage 来自同一 canonical immutable frame。
2. Stage-1 与 Stage-2 的 live wire coordinate 都固定为两个独立的 normalized `[0,1000]` 轴；canonical `1000` 是归一化参考长度，不是 bitmap pixel 尺寸。历史 pixel 协议只供离线对照。
3. crop 丢弃 void、不 padding，actual integer rect 是唯一权威；Stage2 两轴各自归一化到 0..1000，物理 crop width/height 分别决定 inverse transform。
4. 双向 adapter 统一两个 stage 的 prompt/schema/grid labels 与 raw decoder；模型不做 global 数学，禁止 runtime 猜 convention。
5. continuous geometry、wire coordinate、raster index 与 OS input coordinate 分层；crop floor/ceil 与 scalar half-up 不混用。
6. capture 先 canonicalize，已证明覆盖完整 monitor 才能按比例映射 input region；metadata/geometry 改变立即失效。
7. 非法坐标/action、过期/不确定帧、绑定或校准缺失均在发送前 fail closed。
8. input backend 仅接受 monitor ratio 与可信绑定；按协议分隔 motion/button frames，六动作保持 move→立即开始 action。
9. 物理输入串行、身份先消费后发送；任何可能发送后的失败都是 delivery unknown，禁止自动 replay，不声称应用动作成功。
10. current fingerprint 校准是 dispatch 前置条件；同一 Agent 的 grounding observations 进入 canonical history，只有 §31 明确的 computer-use screenshot provenance 例外可以在接受新 observation 时被占位符替换。

---

# 66. 核心一句话

整个 Skill 始终遵循：**Executable 负责“先把当前状态给同一个 Agent 看”，Agent/VLM 负责“看哪里”，Geometry Backend 负责“怎么算”，Platform Backend 负责“移动并执行什么鼠标动作”。** 即：

```text
Allowlisted Executable Observation
      ↓
Same Agent Context
      ↓
Adapter-Encoded Prompt / Schema / Grid Labels
      ↓
Visual Perception
      ↓
Raw Model Coordinate
      ↓
Protocol Adapter Decode
      ↓
Canonical Relative Coordinate
      ↓
Deterministic Geometry
      ↓
Normalized Monitor Ratio
      ↓
OS-specific Input Mapping
      ↓
Move → Immediate Mouse Action
```

这是 Pure-Vision Computer Use MVP 的核心边界。
