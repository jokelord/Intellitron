# Pure-Vision Two-Stage Computer Use Grounding Skill

## 0. 文档状态

**版本：** MVP v2.0，2026-09-29 实现勘误。Stage-1 把完整原生截图沿 X/Y 独立拉伸到配置边长 S 的正方形，随后叠加八等分半透明网格；Stage-2 使用Agent 选择的裁剪（支持 2×/4×/6×/8×，省略 zoom 时为 4×）、正方形 model canvas 和十六等分网格（偶数内线标签值沿奇数正交位置重复）。Stage-2 标签字体按每 1000 画布像素 12 px 缩放。自动模型探针默认关闭 thinking；用户可在真实 Agent 端到端测试中显式开启。真实模型校准与 live Agent 验收仍未完成，不能据旧样图或探针结果声称 `ready`。当前显式启用 `computer_use.operator_session_enabled` 的部署允许新 top-level web Agent context 与独立 operator CLI 执行普通桌面交互而暂不要求发布校准报告。授权仍绑定各自 context/capability，保留原生授权、几何、存活 binding 和至多一次输入发送检查，child Agent 不获桌面权限。后端不识别靶心、不评判落点、不读取应用结果；`ACTION_SENT` 仅表示原生输入已发送，结果由用户判断。此策略不创建校准报告，也不使发布 `ready` 为真。

**当前实现：** Stage-1 先把完整 monitor frame 独立沿 X/Y 拉伸成配置边长 S 的 S×S 无损 PNG，再叠加八等分网格；Stage-2 从同一 immutable frame 按 Agent 选择的 zoom 取得真实整数 crop（2/4/6/8；省略时固定为 4，YAML stage1_zoom 仅用于离线 fixture）、丢弃边缘 void，再把该 crop 的 X/Y 分别拉伸至 `S×S` model-facing detail，最后绘制完整 16×16 网格。Stage-1 只有八等分线与边框，不画文字；Stage-2 偶数内线标注 `125,250,375,500,625,750,875`，奇数线不标自己的值。placement 默认 edge（guide 端点）；当前 repeated 将每个 X 标签沿其竖线放在 Y=31.25,156.25,281.25,406.25,531.25,656.25,781.25,906.25，将每个 Y 标签沿其横线放在 X=31.25,156.25,281.25,406.25,531.25,656.25,781.25,906.25。每值八份，共 56 X 与 56 Y 标签、112 个文字标签。X 文字在竖线右侧，vertical bbox 中点对准 Y 锚点，逆时针 90°；Y 的 horizontal bbox 中点对准 X 锚点，位于其横线下方，水平；保留半透明黑描边。当前 YAML `S=1120`（配置模型默认 1024），只要求正整数；`S` 不是归一化坐标域。Stage-1/Stage-2 的 X/Y 仍各自使用 `[0,1000]` 相对轴，source/physical 坐标由后端按真实源宽高或 crop rect 分别反算。

**证据边界（用户勘误）：** 用户在既有模型探针中观察到：Stage-2 physical bitmap 越接近正方形，定位偏差越小；图片拉长的方向也是较大偏差的投影方向。这是对既有样本的经验观察，未证明因果；“模型训练或内部视觉预处理会强制转方形”仍只是尚未证实的可能解释。v2.0 把方形模型输入作为待验证的设计变更，须用 §58/§61 的同源、同目标、重复 A/B 试验确认效果，不能仅凭该观察通过校准或启用输入。

**继承与修订：** 沿用 v1.8 的 edge crop 不 padding、实际整数 crop rect 权威、双轴独立归一化；Stage-1 改为先将完整原图拉伸为配置的 S×S 正方形、再绘制八等分网格，Stage-2 保持方形画布并采用十六等分网格。canonical source 几何、crop 范围、input binding、身份与几何 fail-closed 规则保留；默认 binding_only 不以帧龄或像素变化否决输入。历史 v1.8 规范保留在同目录的 `_v1.8.md` 文件。
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

本系统 canonical geometry 中的 `1000` 是**归一化参考长度**，不是像素分辨率；历史离线 wire protocol 可以另行使用像素坐标（§60），v2.0 live 两阶段只能使用双轴归一化协议。例如原始 monitor raster：`3840 × 2160`。某点实际 raster X 为：`2880`。对应 normalized X：

```text
Xn = 1000 × 2880 / 3840
   = 750
```

因此对完整 source frame：`relative_x = 1000 × source_x / source_width`；`relative_y = 1000 × source_y / source_height`。反向：`source_x = relative_x / 1000 × source_width`；`source_y = relative_y / 1000 × source_height`。v2.0 的方形化只是 model-facing 像素映射；`source_width/source_height` 始终是未经拉伸的真实 monitor 尺寸，Stage-2 则使用真实整数 crop rect (§14–§18)。

---

# 4. Stage-1 Global Normalized Space

Stage 1 canonical geometry 永远使用：`X ∈ [0,1000]`；`Y ∈ [0,1000]`。分别表示：`X / 1000 = horizontal ratio`；`Y / 1000 = vertical ratio`。Stage-1 的 X/Y normalized units 是两个独立的比例轴。因此对于一个 `3840 × 2160` monitor：`500 horizontal units = 1920 source raster pixels`；`500 vertical units = 1080 source raster pixels`。Stage-1 模型画布固定为配置的 S×S 正方形，但其 X/Y 分别对应未经拉伸的 source 宽/高；同样的 normalized 距离在两个 source 物理轴上不一定占相同像素。Stage-1 坐标只用于全屏 coarse relative localization。

Stage-1 Agent 输出必须是符合当前 executable operation schema 的数值坐标，例如 `{"x":180,"y":740,"zoom":4}`。`x`、`y` 分别在已拉伸的正方形 overview 的横轴、纵轴 `[0,1000]` 上表示相对位置；不接受自然语言替代坐标。目标来自用户上下文；observe 不要求 target_description，若可选提供则须忠实于真实目标并正确 argv quoting。未提供时后端仅以中性 current desktop 填充内部非空字段，不猜应用目标。

---

# 5. Canonical Source 与方形 Model Canvas 必须分层

当前实现允许 Stage-1 overview 和 Stage-2 detail 各自在 model-facing 位图中非等比拉伸。后端保留未经拉伸的完整 canonical source frame `Wp×Hp`、真实整数 crop `[x0,x1)×[y0,y1)` 与输入区域。Stage-1 先把完整 `Wp×Hp` frame 拉伸至 `S×S`（`S=computer_use.model_square_side_px`），再绘制八等分网格；Stage-2 把 native crop 独立沿 X、Y resize 到正方形 `S×S`，再绘制十六等分网格。`S` 当前 YAML 为 1120，只要求正整数；不改变原始 source、crop 或输入几何。

| 层 | 示例尺寸 | 性质 |
|---|---:|---|
| canonical monitor frame | `3840×2400` | 真实源像素，不变形 |
| Stage-1 model-facing overview | `1680×1680` | 从完整 3840×2400 原图沿 X/Y 分别缩放到配置正方形，再叠加八等分半透明网格，PNG 无损编码 |
| Stage-2 native edge crop | `1920×1000` | 真实裁剪像素；不补白、不改 rect |
| Stage-2 model-facing detail | `1680×1680` | X 比例 `1680/1920`，Y 比例 `1680/1000`；先拉伸，再绘制十六等分网格 |
| model wire coordinate | `[0,1000]×[0,1000]` | 两轴各自的无量纲比例，不是 `S×S` 像素 |

Agent 只输出当前图像的归一化点；后端以原始 source/crop 各轴宽高分别作逆变换。Stage-1 拉伸会移动目标在模型位图中的像素位置，之后的网格仅改变颜色；Stage-2 方形拉伸改变显示形状，不改变 crop rect。若推理服务后续又裁剪、padding、旋转或改变可见区域而无法证明逆映射，必须 `CALIBRATION_REQUIRED` / fail closed。

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

一次 observe 失败只产生 fail-closed observation。Agent 可以重新 observe（live 无次数上限），或向用户报告 unavailable；不得假设桌面布局、默认应用入口或目标存在。

## 6.2 Executable observation protocol

skill 只能通过 backend 已登记的 allowlisted executable（默认命名为 `vision_desktop`）发送以下有限 argv/JSON envelope：`observe`、`inspect_region`、`interact_detail`。shell 解析、任意路径、任意子命令、native FD、binding ID、capability、crop ID 和 request ID 均由 runtime 拒绝或由可信 runtime 注入。Agent 只提供当前阶段允许的 target/action/坐标字段。

每个 envelope 必须包含 `protocol_version`、`context_nonce`、`operation` 与单调 `sequence`；裸 `observe` 不要求 action、target_description 或 reason；保留可选 target_description/action 兼容。未提供时内部 GroundingRequest 用中性 `current desktop` 满足非空字段，用户实际目标仍来自 Agent 上下文；提供时须正确 argv quoting。未显式预绑时只在 interact_detail 选择并 late-bind 有效 Action，使用 dataclasses.replace 保持 request/crop/frame ID 与几何；显式预绑 action 的旧调用仍要求一致。内部 capture-only left_click placeholder 不是模型动作限制。返回值必须包含 `state`、`observation_id`（若有图像）、`frame_id`、`geometry_version`、`provenance=computer_use_observation` 和结构化错误码。`provenance` 只能由 runtime 在验证 executable 身份、context/generation、sequence、返回码和受控图像句柄后赋予；模型输出、普通 stdout、其他 executable 或用户参数不得自称该 provenance。图像不得只作为普通 stdout 文本返回；runtime 必须从受控的 in-memory/memfd 句柄构造 multimodal observation，禁止把 base64 作为可重复的历史文本写入 Agent prompt。

`exec_command` 的公开 Agent schema 仍是现有的单个 `command: str` 参数，但当第一个 token 为 `vision_desktop` 时，runtime 必须走不启动 shell 的固定 argv parser：只接受上述四个 operation 及其白名单 flags，拒绝管道、重定向、命令替换、额外 executable、空白外的 shell 语法与未声明字段。Stage-specific operation schema 用于离线 adapter 的 prompt/schema/decode 闭环；live Agent 按稳定 SKILL/executable contract 调用，不得注册为新的 API tool，也不得替换 Agent 稳定 system/tool schema。

同一个 Agent tool-call batch 中不得同时提交相互依赖的 computer-use operations。runtime 必须串行化并绑定 `observe → inspect_region → interact_detail`；当一个 capture/inspect/observe 或其对应 Agent inference 正在进行时，另一个操作不得替换 active frame/crop。每个 Stage-1/Stage-2 model result 都带有其看到的 observation/crop generation；generation 不匹配、过期或来自另一批次时必须拒绝，不能只依赖 sequence number 猜测“最新 crop”。

所有 model-facing tool observations（成功、拒绝、异常）只报告状态、动作结果和简洁错误指引，不回显 Stage-1 或 Stage-2 的坐标值、坐标范围或 crop bounds。Stage-2 必须仅凭最新 detail image 与独立归一化轴定位；不得读取文本形式的 overview/global crop 提示。图像上的归一化 tick 标签与历史 assistant structured tool-call arguments 保持原样；禁止为此重写 canonical history。内部 source/crop/两阶段坐标映射只进入受限的 `logs/computer-use/vision-desktop.jsonl` 诊断记录，不进入 LLM prompt/observations；该日志按配置启停与有界轮转，不保存图片、base64、prompt、reasoning 或 target prose。

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
    zoom=4  # 示例；可选 2/4/6/8，省略时为 4
)
```

其中：

```text
x: normalized global X
   range [0,1000]
y: normalized global Y
   range [0,1000]
zoom:
   integer enum 2,4,6,8
   optional, default 4
```

live schema、executable parser 和 decoder 接受 Agent 自选的整数 2/4/6/8；省略 zoom 时固定为 4，不取 YAML 的 stage1_zoom（该项仅保留为离线 fixture 选择）。其他值返回 INVALID_ZOOM，不能猜测或静默转换。模型 wire 值先按其声明的 domain 验证；解码后的 canonical x/y 越界（例如 `x = 1200`、`x = -50`、`y = 1001`）：`INVALID_GLOBAL_COORDINATE`；`→ 不生成 crop`；`→ fail closed`。必须严格区分两类情形：

```text
合法中心点 + nominal 超出屏幕（显式配置 zoom=4 的几何示例）:
    x = 50, zoom = 4
    nominal crop = [-75, 175]
    → 合法 request，执行 §11 edge clipping
非法中心点:
    x = -50
    → 非法 request，直接 reject
```

clip 只允许作用于合法中心点产生的 nominal rectangle；它绝不是对非法模型输出的静默修复。
该 turn 只产生放大请求，不产生任何 physical input。若 Agent 没有看到本 request 的 `computer_use_observation`，或 observation 的 `geometry_version` 与当前 request 不一致，runtime 必须拒绝该 turn。

---

# 10. Zoom 的真实语义

`zoom` 表示 Stage-2 nominal field-of-view，不是 bitmap resize 的固定倍率，也不决定模型方形画布边长 `S`。几何定义 `C = 1000 / z`，其中 `z` 为 Agent 选择的 zoom，仅描述 crop span；以 `z=4` 为例，`C=250`；`z=2` 则为 `C=500`（每轴 nominal FOV 为 `500/1000`）。围绕 Stage-1 point `(X,Y)` 生成 nominal global normalized region：`L = X - C/2`、`R = X + C/2`、`T = Y - C/2`、`B = Y + C/2`，然后执行 edge clipping。live 支持 2/4/6/8，省略时为 4；该内部 region 不回显给模型。

---

# 11. Edge Crop Policy

MVP 使用 `clip-to-monitor-boundary`，不使用 `padding`、黑边、透明区、`duplicate edge pixels` 或其他 monitor 像素。执行：`L = max(X - C/2, 0)`、`R = min(X + C/2, 1000)`、`T = max(Y - C/2, 0)`、`B = min(Y + C/2, 1000)`。因此 edge crop 允许 `R-L != C`、`B-T != C`，这是预期行为；`C` 与方形画布边长 `S` 是不同量纲。

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

真实裁剪区域的尺寸仍由 `crop_width:crop_height` 决定，但发送给模型的 detail bitmap **有意沿两轴分别拉伸为 `S×S`**。例如在 3840×2400 monitor 上，显式配置 zoom 4 的示例中，中心 960×600 crop 与靠上边缘的 960×300 crop 均生成 `S×S` detail；两者每轴的拉伸系数不同，但 local X/Y 都分别映射到各自完整的 0–1000。绝不能把模型方形画布的像素宽高写回真实 crop rect。

## 15.1 独立的 Model-Facing Domain

`model_width=model_height=S` 与 canonical source/crop dimensions 分开保存，由双向 adapter（§60）定义。v2.0 的 live `relative_1000` / `custom_isotropic_1000` 两阶段均使用 1000×1000 wire domain；这里两个 1000 分别是比例尺，不表示方形 bitmap 必须为 1000×1000 像素。历史 `image_pixel` 协议仅可供离线对照，不得作为 v2.0 live Agent 的提示词、工具 schema 或网格标签。

adapter 先验证 raw 值，再转换为轴比例 `rx,ry`，最后生成 `u=rx*local_width`、`v=ry*local_height`。**不再整数化 canonical u/v。** 模型整数定位的量化误差与 raster/render rounding 是不同误差来源，见 §62。

# 16. Stage-2 裁剪例子

以下保留显式配置 zoom 4 的几何示例（当前部署配置为 4×）：zoom 4 在中心选出 `250×250` normalized crop。对于 3840×2400 source raster，physical crop 是 960×600；两个 local 坐标轴均为 0–1000。以 `S=3840` 为例，模型看到 3840×3840 detail，X 拉伸 4 倍、Y 拉伸 6.4 倍；原始 960×600 crop 本身没有改变。

靠近上边缘时，同一 zoom 可能实际裁出相对 extent `250×125`，对应 960×300 physical pixels。无 padding，丢弃 monitor 外的 void；模型看到的 detail 仍为 3840×3840，X 拉伸 4 倍、Y 拉伸 12.8 倍，**local 坐标域仍为 1000×1000**。`250×125` 只描述相对裁剪 extent，并非像素 raster。Stage-2 的 `(500,500)` 是实际 crop 的中心，反向映射到原始 crop 左上角加 `(480,150)` 的连续 source 点。图像拉伸与坐标比例分开保存；物理 crop 的整数边界始终是 inverse transform 的权威。

# 17. Stage-2 两轴独立归一化

Stage-1 使用全 monitor 的 `[0,1000]×[0,1000]` 百分比。Stage-2 使用实际 crop 的 `[0,1000]×[0,1000]` 比例坐标；X、Y 各有自己的 source baseline。模型看到的方形 raster 上，等份 X/Y 对应相同数量的**画布像素**，但 1 X unit 与 1 Y unit 通常对应不同数量的**原始 source 像素**。映射回 source 时，x/y 分别乘以实际 integer crop width/height。

---

# 18. 两阶段 Model-Facing Square Resize

使用显式配置的 `model_square_side_px=S`，当前 YAML `S=1120`（配置模型默认 1024），只要求正整数。Stage-1 先将完整 source `Wp×Hp` 沿两轴分别拉伸为 `S×S`（`S=computer_use.model_square_side_px`），然后叠加八等分网格；Stage-2 先依据 §13/§14 取 native crop、再 resize 到 `S×S`，最后绘制十六等分 `grid/ticks/labels`：

```text
S = computer_use.model_square_side_px
stage1_model_bitmap = overlay_8_grid(resize(full_source_frame, width=S, height=S))
native_crop = full_source_frame.crop([x0,x1) × [y0,y1))
stage2_model_bitmap = overlay_16_grid(resize(native_crop, width=S, height=S))
stage1_scale_x = S / Wp; stage1_scale_y = S / Hp
stage2_scale_x = S / (x1-x0); stage2_scale_y = S / (y1-y0)
```

Stage-2 方形化是受控的**非等比**模型输入变换，不是对 source/crop 几何进行重定义。中心 1920×1200 和 edge 1920×624 crop 均生成 S×S bitmap；不添加空白边缘。live Agent 输出双轴 normalized `[0,1000]`，backend 按真实 source/crop 宽高分别反算，**不可再额外做宽高比修正**。更改 `S`、网格、resize 算法或推理服务预处理会使旧 calibration fingerprint 失效，须重新校准。

这里 `mx,my` 是画布上的**连续几何位置**，范围 `[0,S]`，不是 RGB 数组的像素索引 `0..S-1`。精确正向/逆向几何关系如下，Stage-2 中 `x0,y0,x1,y1` 必须取 edge clipping 与整数 enclosure 后的实际 rect：

```text
Stage-1: mx = source_x; my = source_y
         X = 1000*mx/Wp = 1000*source_x/Wp
         Y = 1000*my/Hp = 1000*source_y/Hp
Stage-2: mx = (source_x-x0) * S/(x1-x0)
         my = (source_y-y0) * S/(y1-y0)
         u = 1000*mx/S; v = 1000*my/S
         source_x = x0 + u*(x1-x0)/1000
         source_y = y0 + v*(y1-y0)/1000
```

例：显式配置 `S=3840` 时，3840×2400 source 中心 `(1920,1200)` 对应 normalized `(500,500)`；若边缘 crop 是 `1920×1000`，显式配置 `S=1024` 时，拉伸后 detail 中心 `(512,512)` 对应真实 crop 左上角加 `(960,500)`。若真实 crop 是 `1920×624`，中心则为左上角加 `(960,312)`。真实 crop 尺寸可以不固定。PIL 重采样的离散像素中心偏差与连续几何映射分开计量。

---

# 19. Resize 顺序

Stage-1 必须：`canonical full frame → resize full frame to S×S → 8-grid/no-text-labels → lossless PNG encode`，其中 `S=computer_use.model_square_side_px`；Stage-2 必须：`canonical full frame → native crop → square resize(S,S) → 16-grid/ticks/labels → encode`。禁止先画网格再 resize。Stage-2 使用明确的 `PIL.Image.Resampling.LANCZOS`，纳入 fingerprint。

---

# 20. Stage-2 Grid

默认：`grid_enabled = true`；`grid_divisions = 16`。Stage-2 的 X、Y 两轴**各自**将完整的 `[0,1000]` 分成十六个等距 interval，guide spacing 为：

```text
grid_step = 1000 / 16
          = 62.5
```

两轴的 15 条内部 guide 位于 round_half_up(k*S/16)，k=1..15。偶数 k=2,4,...,14 标签值为 125,250,...,875；repeated 取 k=0,2,...,14；X 标签的 Y 锚点是 (k+0.5)*1000/16，即 31.25,156.25,...,906.25，Y 标签的 X 锚点同样为 (k+0.5)*1000/16，即 31.25,156.25,...,906.25。奇数 guide 不标自己的值，0/1000 也不是标签值。完整 normalized 十六格保留，像素格宽最多相差 1 px；右/下边框绘于 S-1。Stage-1 完整源图 resize 到 S×S 后只有八等分线与边框，不含文字。

---

# 21. Independent-Axis Grid

Stage-2 model raster 固定为 `S×S`；edge clipping 只改变真实 crop rect 与两轴拉伸系数，不改变画布尺寸或 X/Y local domain。横线和竖线按画布边长十六等分；同一 tick 值表示各自 source crop 轴的相同比例，**不**表示相同 source 像素距离。每格约对应 `(crop_width/16)×(crop_height/16)` source 像素；grid 不改变真实 crop 几何或 inverse transform。

---

# 22. Grid Rendering 要求

Stage-1 完整源图 resize 后绘制八等分线与边框，无文字。Stage-2 保留全部 15 条内线，偶数线标签值为 125,250,...,875。edge 在对应 guide 端点标注同组值；repeated 的 X 标签在 (标签 X,锚点 Y)，Y 标签在 (锚点 X,标签 Y)，每值八次，合计 56 X+56 Y。X 标签 Y 锚点为 31.25,156.25,281.25,406.25,531.25,656.25,781.25,906.25；Y 标签 X 锚点同样为 31.25,156.25,281.25,406.25,531.25,656.25,781.25,906.25。X 旋转 CCW90，在竖线右侧，vertical bbox 中点对准 Y；Y 水平，horizontal bbox 中点对准 X，位于横线下方；保留小间隙；repeated 用整数中心计算，两个轴的文字中心对准同一正交锚点系列。edge 的 bbox 检查不变，无 padding。标签值与重复锚点是两个不同坐标，不可混淆。工具 integer/number schema 不变。

两阶段格线及 Stage-2 白色文字和黑色细描边均以 `alpha=0.5` 合成；`grid_label_shadow_width_px` 是边长 1000 时的描边半径（默认 1），实际半径为 `max(1,round_half_up(width*S/1000))`，设置 0 则关闭。旋转 X mask 带描边 padding，Y bbox 包含描边，均无不透明矩形背景，不增加 canvas padding，不在模型提示或图中显示 source physical pixel 坐标。网格仅供视觉定位；后端反算仍以未画网格的 canonical source frame 和真实 crop rect 为准。Stage-1 模型图网格位置按 `round_half_up(k*S/8)`；Stage-2 分别按 `k*S/16`，线条 rasterization 的半入舍取整数与连续几何公式分开处理。

# 23. Grid 是 grounding aid，不是 geometry correctness

网格提高模型准确率仍需实测，不能靠规范保证。当前 live 路径固定使用 Stage-1 八等分、Stage-2 十六等分；此前的十等分、gridless 或 v1.8 结果只能做历史对照，不能当作当前 calibration。相同 source、目标、模型及 prompt 条件下的 A/B 结果才可比较。

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

这是 adapter 解码后的内部 canonical contract。`crop_id` 是可信 backend 的当前 crop 绑定字段，必须属于当前授权 request、context identity 与 capture/input binding；它不出现在 Agent 的 executable argv，也不能由模型回传或覆盖。Agent 只输出当前 `S×S` detail image 上的 `x/y/action`，runtime 将其绑定到最近一次成功的 `inspect_region` crop。`x/y` 是精确 canonical local coordinates，**两轴分别满足 `0<=x<=1000`、`0<=y<=1000`**，即使真实 crop 为 1920×624、方形画布为 3840×3840。模型 raw 值先由 §60 adapter 验证；要求有限实数，拒绝 NaN/Infinity、错误类型、越界值与未声明的额外字段；整数协议可接受数值等价的 `889.0`，这不是越界修复。浮点 epsilon 仅用于内部计算误差，不容忍模型输出超过域边界。

`action` 只能取 §24.1 的六项且必须等于 `GroundingRequest.requested_action`；否则 `INVALID_ACTION`。非法坐标返回 `INVALID_LOCAL_COORDINATE`，禁止 silent clamp。重复 request/crop dispatch 按 §35 返回已有状态，不能再次执行。

## 24.1 点击与滚动交互规则

Stage-2 local coordinate 完成逆变换并通过 pre-action validation 后，Backend 必须先把鼠标移动到转换后的目标坐标，再在同一 interaction transaction 中立即执行指定动作，不加入额外 dwell：
**（1）** `left_click`：鼠标挪动至转换后的坐标，然后立马左键单击；
**（2）** `right_click`：鼠标挪动至转换后的坐标，然后立马右键单击；
**（3）** `left_double_click`：鼠标挪动至转换后的坐标，然后立马左键双击；
**（4）** `right_double_click`：鼠标挪动至转换后的坐标，然后立马右键双击；
**（5）** `scroll_up`：鼠标挪动至转换后的坐标，然后立即向上发送 steps 个离散单位；
**（6）** `scroll_down`：鼠标挪动至转换后的坐标，然后立即向下发送 steps 个离散单位。
`double_click` 必须发送两个连续的同键 click pair，inter-click interval 使用可配置、经目标平台验证的值，落在可识别的 double-click 窗口内；这是动作内部必要间隔，不是 move 后的 dwell；steps 是可选整数 1..5（省略 1），bool/非整数/越界拒绝，点击不接受显式 steps；legacy scroll_up_1/down_1 仍仅一步。一个 InteractDetail 的 motion 后连续 N 个 signed ±1 wheel_step 共用同一 ledger reservation、transaction deadline、Stop/at-most-once 和 partial-failure unknown 状态；不扩大 native ±1 接口、不 replay uncertain 输入。step 为原生离散 vertical wheel unit，不承诺像素距离。

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

该例外只适用于绑定的 top-level Agent context，不能扫描或修改 child、sibling 或其他 session；不能用于普通用户上传图片、Vision Automation 图片或任何非 computer-use screenshot。事件名固定为 `computer_use_historical_image_omission`。省略后恢复普通 append-only 稳定性；不得恢复已经省略的截图。成功的 `observe` 会产生新的 latest image，因此可以按同一规则提交省略；失败或预接受的 `observe` 不消费该例外。compact、Stop、Answer Now 及其他 context 请求均不能消费或扩大该例外。

---

# 32. Stage-1 Prompt Contract

同一个 Agent 必须使用自身 VISION 能力查看当前 overview，并输出 normalized `[0,1000]×[0,1000]` 的 inspect_region(x,y,zoom)。Agent 自行选择 2/4/6/8 使 crop 能识别请求的交互点/特征；zoom 可省略并固定默认为 4。schema/prompt/parser/decoder 使用同一允许集合，不以 YAML stage1_zoom 限制。自然语言 target 只限定要找什么；只输出“打开微信”等自然语言而没有坐标属于无效模型输出，必须按 parser error 回灌纠正，不能由 backend 猜坐标或代替 Agent 规划。该输出不能交给隐藏的第二模型。

Stage-1 一律声明边界 `(0,0)..(1000,1000)` 及水平/垂直相对比例单位；禁止 Stage-1 使用 image-pixel wire domain。overview 是完整 monitor 经独立 X/Y 拉伸得到的 `S×S` 方形图（`S=computer_use.model_square_side_px`），保持全画面内容、随后叠加八等分网格、不 padding；坐标分别归一化两轴，不能把 source 或 canvas 像素原值直接作为输出。

Stage-1 prompt 必须明确按模型实际所见方形 overview 的两轴分别归一化：`x=px*1000/S`、`y=py*1000/S`，其中 `S=computer_use.model_square_side_px` 为两个阶段共享的 model canvas 边长。虽然两个分母在画布上恰好相同，X/Y 仍是独立的相对比例；backend 分别用原始 `Wp`、`Hp` 逆变换。模型不计算 source physical 坐标，也不试图恢复拉伸前的画面比例。

---

# 33. Stage-2 Prompt Contract

adapter 动态生成当前 crop 的可信 backend metadata、target、requested action、wire protocol、`model_width/model_height`、边界语义及对应 schema；`crop_id` 只留在 backend/runtime state，不放进模型可覆盖的 executable 参数。grid labels 必须使用同一协议实例。模型输出坐标按协议类型验证，不能把 canonical dimensions 无条件塞进所有模型 prompt。

共同语义：`You are viewing a new detail image derived from the same frozen desktop observation.`、`Use THIS image to determine the final interaction point.`、`Do not calculate global desktop coordinates.`、`Return the Stage-2 precise x,y and trusted requested action for the executable interact_detail operation; do not provide crop/session/native IDs.`、`For a user-requested interaction, if its point/feature is identifiable in the latest detail, the next call MUST be interact_detail. Only recover with observe if that point itself is absent, cut off or unidentifiable; full surrounding-object visibility, perfect centering, extra margins and another zoom are not prerequisites.` Live Agent 必须调用裸 `vision_desktop observe`，目标保留在用户上下文，动作到 interact_detail 再选择；target description/action 仅可选兼容；typed offline adapter 同样暴露零参数 `observe()` 作为 fresh-overview recovery。

Stage-1 与 Stage-2 的 live Agent prompt 均声明两个独立 normalized `[0,1000]` 轴。Stage-2 detail 始终是 `S×S` 方形图，只包含真实 crop 内容；edge clipping 改变源范围，却**不改变任一轴的坐标上界**。模型依据当前图的目标位置报局部比例，不自行“矫正”非等比拉伸、推算全屏/physical 坐标或把值强制吸附到网格线上。图上 Stage-1 八等分与 Stage-2 十六等分的 ticks、工具 schema 和解码器必须一致。历史 `image_pixel` adapter 可保留作离线诊断比较，不得进入 v2.0 live Agent 路径。

---

# 34. Stage-2 Coordinate Convention 风险

协议由 checkpoint、prompt/tool schema、adapter、renderer 与 serving preprocessing 共同决定，不能按模型名字推断。[Qwen3-VL 官方 ComputerUse utility](https://github.com/QwenLM/Qwen3-VL/blob/main/cookbooks/utils/agent_function_call.py) 会把 display width/height 注入 prompt，并描述该分辨率下的 pixel coordinates；这不证明所有 Qwen3-VL 部署都采用像素或固定 1000²。

## 34.1 固定 Canonical Geometry，可配置 Wire Protocol

canonical Stage-2 local domain 固定为 `[0,1000]²`，不是 A/B 配置项；实际 physical crop aspect 影响各轴拉伸系数和逆变换，**不影响方形 detail bitmap 尺寸**。v2.0 live wire protocol 的 prompt/schema/grid labels/decoder 必须统一使用两个独立归一化轴。历史 `image_pixel` 可用于隔离的离线对照，但不得暴露给 live Agent。所有 inverse transform 都分别按实际物理 crop width/height 映射，不允许改变 crop 几何来补偿协议。

## 34.2 Convention 混淆的不可检测误读

坐标值在 wire domain 内不能证明模型看对了图或采用了正确的 image preprocessing。v2.0 的方形 overview 仍可能被推理服务再次缩放、分块或裁剪；模型误把像素坐标当 normalized 值时，数值可能仍合法，单靠范围检查无法识别。例如在 `S=3840` 的 overview 上，目标位于纵轴 20%：

```text
target vertical ratio = 0.20
overview model height S = 3840 pixels
correct relative_1000 y = 0.20 * 1000 = 200
raw model-bitmap pixel y near target = 0.20 * 3840 = 768
mistaking pixel 768 for normalized y points to 76.8% of the monitor
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

仅在用户请求物理交互时，若其交互点/特征在最新 detail 可识别，下一 desktop call 必须 interact_detail；仅观察/检查任务不要求且不得自动点击或滚动；周围对象被裁剪、不够居中、缺少边距或想换 zoom 不构成恢复条件。不能在 observe/inspect 已成功时臆造缺少校准或权限，应处理实际返回的错误码。Stage-2 不允许直接 `inspect_region(global_x,global_y)`；detail 无法定位请求的交互点/特征本身时，live Agent 必须先调用裸 `vision_desktop observe`，目标保留在用户上下文且 target description/action 可选，收到 fresh overview 后才能再次 inspect_region。不得从 detail 推算 overview 坐标、复用旧 overview 的记忆坐标或猜测点击。前次 target-absent/wrong-region crop 是负证据；必须根据最新 overview 独立重新定位最接近目标中心的 rough point，不得连续重复同一失败 `(x,y)` 或重放同一 inspect_region 命令（改变 zoom 也不例外）。不得随机/任意偏移，无法定位则停止且不输入。typed offline adapter 的零参数 `observe()` 同样取得新 overview，收到新图前同样不得再次 crop。`ACTION_SENT` 与 `ACTION_DELIVERY_UNKNOWN` 都是本 request 的 terminal 状态，前者仅表示事件已提交，不表示应用完成目标动作。

## 35.1 错误与异常状态

输入/感知：`INVALID_GLOBAL_COORDINATE`、`INVALID_ZOOM`、`INVALID_LOCAL_COORDINATE`、`INVALID_ACTION`、`TARGET_NOT_VISIBLE`、`TARGET_AMBIGUOUS`。时效/绑定：`STALE_FRAME`、`STALE_CHECK_INDETERMINATE`、`CAPTURE_STALLED`、`GEOMETRY_CHANGED`、`INVALID_CROP_ID`（缺失、未知或已失效 ID）、`INVALID_CONTEXT_BINDING`、`UNSUPPORTED_CAPTURE_GEOMETRY`、`UNPROVEN_MONITOR_COVERAGE`、`INPUT_REGION_NOT_FOUND`、`AMBIGUOUS_INPUT_REGION`、`INPUT_REGION_MISMATCH`。环境/协议：`INPUT_CAPABILITY_MISSING`、`INPUT_DEVICE_NOT_READY`、`CALIBRATION_REQUIRED`、`PORTAL_CANCELLED`、`SESSION_CLOSED`。预算耗尽：`PREDISPATCH_BUDGET_EXHAUSTED`。每种异常必须映射为明确状态与审计记录，不能静默选用另一 region/protocol。

## 35.2 仅 Dispatch 前可重试

感知失败（目标不清、wrong crop、stale）使 crop 失效后 fresh observation；protocol failure 原样返回精确错误位置，允许有限 correction；配置/权限/校准错误需修复其原因，不能反复请求模型猜测。

物理输入在同一受控 seat 上跨 request 串行。持有 dispatch lock 后再次检查授权/context、geometry、region/device 状态和所选 pre_action_validation 策略，然后原子保留/消费 request-level `request_id` dispatch identity，同时绑定并消费当前 `crop_id`，才允许发出首个事件。同一 request 换一个 crop 也不能绕过 reservation/terminal state 再次 dispatch；重复请求返回已有状态。身份记录至少保留整个绑定生命周期。若无法可靠维护身份记录，绑定关闭并使旧 IDs 失效，禁止恢复重放。

一旦任何 motion/button/scroll 可能进入发送路径，出现异常均保守记录 `ACTION_DELIVERY_UNKNOWN`；不能回到 predispatch retry，不能声称“没有产生动作”。只有明确证明尚无输入事件发出的失败才可重试。`finally` 对本 transaction 可能按下的 button 做 best-effort release（单独 frame），不补发 click，不释放其他输入源的 button；记录 cleanup 的成功/不确定结果。fresh observation 可用于用户或上层重新判断，但不会自动重放原请求。无应用级 action verification，因此不承诺 exactly-once 应用效果。

ACTION_SENT 完成并消费当前 intent；任何后续显式 observe 只在 fresh screenshot 接受后创建新的 UUID intent。ACTION_DELIVERY_UNKNOWN 不得重放旧动作；新 observe 创建新 intent，之后根据新图重新选择点与动作，不自动 replay。捕获失败不替换旧 state，旧 detail 不可操作。

## 35.3 每张已接受 overview 的 detail 预算

默认 max_tool_contract_retries=1、max_pre_dispatch_attempts=3。live observe 无次数上限，仅有一个公共截图命令。detail 预算仅限制当前已接受 overview 的 inspect 次数；成功 observe 接受新图后 reset，不跨 refresh 累积；失败 refresh 不续预算、不恢复旧 crop 输入资格。isolated probe 的显式 max-reobserves 参数仅为诊断运行预算，不限制 live Agent。Stop/abort、geometry/native binding 与不重放 uncertain 输入仍保留。

---

# 36. 统一 `observe()` 捕获与刷新

Live/public executable 仅有 observe、inspect_region、interact_detail 三个基本操作。裸 observe 无需 reason、target_description 或 action，首次捕获和所有 refresh 使用同一调用；成功接受图像后创建 fresh UUID intent，reset 当前 overview 的 detail-inspection 预算，旧 crop 不可 replay。新 capture 失败不替换旧 state，旧 detail 不得输入；Stop/abort、授权和 binding/geometry 检查不变。detail 无法定位请求的交互点/特征本身时，必须先 observe 获得 fresh overview，再视觉重定位；不得直接 recrop 或复用旧 overview 点。typed offline adapter 暴露零参数 observe，内部可保留 Reobserve 作为 recovery marker，但不是模型工具名。可选旧 observe --action 仍显式预绑动作；普通裸 observe 到 interact_detail 才选择动作。成功观察按 §31 的 provenance/acceptance 规则 stage 历史图片省略，不扩大该例外。

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

# 48. Pre-Action Validation Policy

`computer_use.pre_action_validation` 默认且当前部署为 `binding_only`。dispatch 只验证 trusted frame/crop 的 binding、geometry、stream generation 与当前原生 input connection 身份；保留 session/capability、Portal、seat lock、Stop、至多一次发送和 unknown-delivery 禁止 replay。不为每次点击再次 `freeze`，不比较全屏/裁剪区像素，也不因截图年龄、静态 damage stream 或窗口/pointer/popup 变化拒绝模型给出的坐标。连接撤销、断开或几何变化仍阻止输入；observe 请求截图的行为不变。

`frame_and_binding` 是显式 opt-in 的旧版/诊断策略，才要求新帧、age/liveness 与 global/crop pixel diff。其验证完成后重查年龄并开始配置的 validation-to-dispatch interval；不能把默认 binding-only receipt 描述为已通过图像 freshness 检查。日志记录实际 policy，不读取或评判应用结果。

---

# 49. Frame / Crop Metadata

Frame 保存 `frame_id`、capture/input binding ID、context identity、monitor ID、geometry version、stream generation/sequence、monotonic timestamps、canonical source dimensions，以及 §8 canonicalization/coverage evidence。

DetailCrop 保存 `crop_id`、request/observation/frame IDs、同一绑定/version、`x0,y0,x1,y1`、真实整数 crop 宽高和 `exact_crop_aspect`、方形画布边长 `S` 与 `model_width=model_height=S`、各轴缩放系数 `S/crop_width` 和 `S/crop_height`、精确 canonical `local_width=local_height=1000`、renderer/resize/label policy、wire domain/type/extent/index semantics、protocol/calibration fingerprint、rough global coordinates、zoom、grid settings、创建时间和 dispatch identity/state。Stage-1 observation 也须保存 source 宽高、`S`、两轴缩放与同一 fingerprint。禁止 model-facing 整数上界覆盖 canonical dimensions；模型只收到当前调用所需的 adapter metadata，不暴露可替换授权身份。

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

至少 `10,000 randomized geometry cases`，覆盖 source W/H、合法/非法 Stage1 coordinates、Agent 选择 2×/4×/6×/8× 及非法 zoom、edge clipping、极细长/portrait crops、canonical local point、各 model protocol、region sizes 与 signed origins。验证合法 crop、无 padding、Stage-1 bitmap 从完整源图拉伸为配置的 S×S 正方形、Stage-2 bitmap 为 `S×S`、完整源内容未丢失、base raster 来自源 frame（不把 grid overlay 误判为源像素）、独立轴 ratio 范围与逆变换、单调性、无翻轴、version 不复用及 §62 分项误差预算。用已知非对称 landmark 测试两阶段的 source→model→normalized→source 闭环，尤其覆盖边缘 clipping。分别测试 interval floor/ceil 与 scalar half-up。

额外必须覆盖：

- 两阶段 prompt/schema/grid 标签与 decoder 的协议闭环；多个任意正整数 `S` 值、portrait/landscape 原始图、极窄真实 crop、pixel extent/index endpoints、无二次 canonical rounding、calibration fingerprint 失效。拒绝非正整数 S，验证 Stage-1 八格、Stage-2 十六格无残格，prompt/render/fingerprint 的 label placement 与 config 一致（Stage-1 无标签；Stage-2 edge 为端点，repeated 为X 值沿 Y=31.25..906.25 重复，Y 值沿 X=31.25..906.25 重复，各八份，56 X+56 Y），格线与标签均以 0.5 opacity 合成。
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

2×/4×/6×/8× crop 的每个 edge case 必须测试：`center`、`left edge`、`right edge`、`top edge`、`bottom edge`、four corners；`6x`、`8x` 同样属于 live executable 合法值。验证：`clip correct`；`no padding`；真实 crop rect/宽高不被方形化覆盖；Stage-1 模型图保留完整源内容并拉伸为配置的 S×S 正方形、Stage-2 为 `S×S`；`Stage-2 wire X/Y both 0..1000`；十六格完整、仅偶数内线标注 125/250/…/875，标签位置与配置一致（`edge` 模式验证四边端点位置；`repeated` 模式验证每个轴标签覆盖 八个正交锚点（X 标签的 Y=31.25..906.25，Y 标签的 X=31.25..906.25））；精确 rect 上的独立轴 inverse transform 正确。

---

# 56. Resolution Test Matrix

至少：`1920×1080`、`2560×1440`、`3840×2160`、`3840×2400`、`1920×1200`、`2560×1600`、`1280×1024`、`1080×1920`。无论原始比例如何，发送给模型的 Stage-1 图须保留完整源内容并拉伸为 `S×S`，Stage-2 图须包含完整裁剪内容并拉伸为 `S×S`；保留并验证各自的 source/画布映射系数。

---

# 57. Fractional Scaling Test Matrix

Wayland：`100%`、`125%`、`150%`、`175%`、`200%`。测试必须证明：`Vision normalized ratio`；`→ matching libei logical region`。而不是：`screenshot pixels / scale`。

---

# 58. Model Grounding Benchmark

Geometry correctness 和 VLM accuracy 必须分开测试。至少建立 GUI benchmark：`GTK`、`Qt`、`Electron`、`Chromium`、`Canvas`、`custom rendered UI`；包含 `large button`、`normal button`、`small toolbar icon`、`tiny icon`、`textless icon`、`edge target`、`corner target`、`dense toolbar`、`dark theme`、`light theme`、`high-DPI`。当前验收路径是完整源图先拉伸为正方形后绘制八等分 Stage-1 概览、模型自选 zoom crop（2/4/6/8，省略时为 4），以及正方形十六等分 gridded Stage-2。`Stage1 direct interaction baseline`、`Two-stage without grid`、`Two-stage + 10-grid` 和 v1.8 等比输入只作为可选历史对照，不代替当前验收；对照时须固定原始帧、目标、模型/checkpoint、推理参数与采样次数，并可固定同一个 Stage-1 rough point/crop 来单独测 Stage-2 变化。旧版 16-grid/image-pixel 报告也只能作为隔离对照。至少记录：`Stage1 target-region recall`、`Stage2 point hit rate`、原始 source X/Y 轴绝对误差 `|Δx|,|Δy|`、各自除以真实 crop 宽高后的相对误差、真实 crop 的 `|log(crop_width/crop_height)|`、`end-to-end interaction success rate`、`overview refresh rate`、`false-action rate`，并分别覆盖六种鼠标 action。这样可以检验用户观察的“接近正方形”与长轴误差规律，同时避免仅因长轴像素数较多就把同样的相对误差误判为模型偏差。模型内部是否转方形不能从误差相关性直接推断。

---

# 59. Performance Benchmark

至少记录：`capture freeze latency`、`frame copy latency`、`full-frame square-resize plus overview grid/PNG latency`、`native crop latency`、`detail square-resize latency`、`grid render latency`、`PNG encode latency`、`observation image attach latency`、`historical-image omission latency`、`Agent observe-turn latency`、`Agent Stage1-turn latency`、`Agent Stage2-turn latency`、`pointer move latency`、`action dispatch latency`、`total interaction latency`、`peak RSS`、实际发送/预处理后的图像尺寸。`S=3840` 的每张未压缩 RGB 方形图约 42.2 MiB，须验证峰值内存和端到端推理开销后才能选定部署 `S`。MVP 不要求处理每帧；截图 plumbing 与历史图片省略不得重复编码或写临时文件；仅显式 debug_save_images 允许保存已经编码的最终 PNG。

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
| `custom_isotropic_1000`（旧名称兼容） | Stage1/Stage2 同上；旧名称不决定 source/模型画布形状 | `rx=x/1000`, `ry=y/1000` |
| `image_pixel` + `extent` | 历史离线对照；v2.0 live Agent 不可选 | `rx=x/W`, `ry=y/H` |
| `image_pixel` + `index_center` | 历史离线对照；v2.0 live Agent 不可选 | `rx=(x+0.5)/W`, `ry=(y+0.5)/H` |

协议必须声明输出类型（integer 或 finite float）、端点语义及精度；MVP custom/relative wire 默认 integer。`image_pixel` 必须显式选择语义，不能把 W 当合法 index，也不能把 index 隐式除以 W-1。其他 checkpoint convention 只能新增显式、经测试和校准的 adapter，不启发式兼容错误格式。

axis ratios 转成 Stage1 或 Stage2 的 `1000*rx,1000*ry`；无二次取整。明确区分：`W,H` 在历史 `image_pixel` 离线模式中表示**实际模型可见画布尺寸**，而 v2.0 live 路径只用方形图的双轴相对值，并分别用真实 source/crop 尺寸反算。server preprocessing 若再次改变坐标所指图像、增加 padding 或裁剪，adapter 必须有可验证的 inverse mapping；未知则 `CALIBRATION_REQUIRED`/fail closed。每次 encode/decode 绑定同一协议实例、`S`、真实 source/crop 尺寸、stage 与 calibration fingerprint。

更换模型可更换 adapter/config 并重新运行测试，不能修改 crop/libei/XTEST 几何以补偿模型协议错误。

---

# 61. Model Coordinate Calibration

上线前由配置明确选择 protocol，再用 calibration suite **验证该协议**，不是从单点输出自动猜测或改写 protocol。失败可输出候选诊断，但必须修正配置并重测后才能启用输入。

两个 stage 均测试多比例位置、角落、靠边目标。Stage-1 使用来自非正方形 portrait/landscape source 的完整图先独立拉伸到配置的 S×S 正方形、再绘制八等分网格；Stage-2 使用来自中心和边缘不同真实宽高比 crop 的方形 detail 与十六等分网格，逐例验证尺寸、无内容丢失/补白、各轴反向映射、双轴 0–1000 wire 范围和标签。也测试不同正整数 `S`，并检查推理服务实际接收的图及预处理没有不可逆裁剪、padding 或旋转。Stage-1 以目标完整落在选定 crop 中的比例评价粗定位；Stage-2 以最终点命中率和原始 source X/Y 误差评价精定位。重复样本测协议通过率与失败率；阈值配置化并在 deployment 报告记录。校准减少 convention mismatch 风险，不证明模型每次都能正确 grounding。

**配置 zoom 与目标尺寸门槛：** Stage-1 的 z 由 Agent 选择，支持 2/4/6/8，省略时固定为 4；nominal FOV 每轴为 `(1000/z)/1000`（2× 为 `500/1000`，4× 为 `250/1000`）；它只决定 crop 范围。Stage-2 将该 crop 拉伸为 `S×S`，不是模型定位误差的安全余量。校准中的 `max_axis_error` 是 monitor-normalized 最大轴误差，同一个误差比例在不同 monitor 分辨率上对应不同 physical 像素数；它也不随目标尺寸变化。因此，不能因为用了 2× 或 4× crop 就放宽全局误差阈值，或据此声称小目标命中安全。可靠的 target-size-aware 证据至少需要每个样本的已知 target bounding box、最终点是否落入可点击区域（最好含明确边缘余量）的 hit 结果，以及按目标尺寸分桶的命中率；仅有中心点误差不够。由于 live pure-vision operation 没有可信的 target bounds，离线小目标命中结果也不能自动证明任意 live target 尺寸可安全点击。除非 live path 能获得并验证实际 hit bounds，否则不采用按目标尺寸放宽 precision/dispatch gate 的 concession。

结果绑定 fingerprint：checkpoint/revision、system prompt/tool schema、adapter version/protocol、`S`、square-resize 算法与两阶段 renderer 参数、grid/label policy、serving stack 及实际 image preprocessing。v1.8 的等比图校准报告不能用于 v2.0；任何相关项改变使旧结果失效，返回 `CALIBRATION_REQUIRED`，重新通过后才可 dispatch。允许离线调整 renderer 来校准，但未经新校准不得执行鼠标输入。离线样图、模型点标注、contact sheets 与探针报告统一写到仓库内稳定的 `logs/computer-use/`，不再将图像输出写到 `/tmp`；运行时截屏仍只在受控内存/IPC 中处理。native helper 的构建中间产物位于 `build/computer-use-native/`，供仓库分发和默认运行的 Linux ARM64 程序位于 `bin/linux-aarch64/computer-use-helper`（通过显式 CMake install 更新，详见 `bin/README.md`）；YAML 相对路径按仓库根目录解析，不依赖证据/样图目录存在；旧样图目录不是配置或运行前提。

---

# 62. Deterministic Rounding 与误差预算

区分三类边界，禁止语言默认 bankers rounding：

1. **Crop interval enclosure**：§13 左/上 `floor`，右/下 `ceil`，clamp 到 source bounds；每边 enclosure 扩张小于 1 source raster unit。actual integer rect 一旦生成即为权威，不再按 nominal rect 回算。
2. **非负 scalar/dimension quantization**：`round_half_up_positive(x)=floor(x+0.5)`。crop interval 的 floor/ceil enclosure 与 model wire coordinates 的量化按各自协议执行。Stage-1 bitmap 从完整原生 `Wp×Hp` 拉伸至 `S×S`，其中 `S=computer_use.model_square_side_px`；Stage-2 bitmap 两轴分别拉伸至 `S×S`，不依据 clipped relative extent 重算真实 crop rect。Stage-2 guide 位于 `round_half_up(k*S/16)`，交点锚定使用相同 guide 取整位置，normalized 十六等分不变；1000 端点仅绘制于最后一个可用像素索引 `S-1`。canonical X/Y 都保持 0–1000；u/v 不取整。
3. **X11 point quantization**：先对合法非负 `nx*Rw, ny*Rh` 取 half-up 并限制到 `[0,Rw-1]` / `[0,Rh-1]`，再加 signed integer origin；不对负的 global coordinate 调用 positive rounding。Wayland 保留连续输入，仅对合法 upper endpoint 做 §28 的最小内部修正。

误差必须分项度量。整数 wire 域每轴上界 `D` 若代表均匀比例刻度，nearest quantization 最多约 `crop_axis/(2D)` source units；4K 不能统称“小于一个 raster pixel”。Stage-2 renderer 的 x/y resize 是两个独立线性映射，需纳入 renderer fingerprint 与校准；模型 grounding 误差单独统计。X11 内部点 rounding 不超过0.5 input unit，最外边界安全限制可接近1 input unit。

geometry roundtrip 测试在未量化 canonical/ratio 层应仅有数值精度误差；跨 wire/render/backend 的测试按上述各自尺度预算，不能拿 source pixels、input units 与 normalized units 相加而不换算。

---

# 63. MVP Acceptance Criteria

- **Platform**：GNOME Wayland 持续 capture 与六动作可用，libei lifecycle/capabilities、event-frame 顺序、120-unit wheel 与双击间隔经实际平台验证；映射唯一且完整 monitor coverage 成立。True X11 正确处理 signed origin 与 endpoints；XWayland 使用桌面所属 backend。
- **Geometry**：同一 canonical immutable frame 派生完整源图的正方形 overview/真实 crop，无 padding；exact integer physical crop 权威；Stage-2 model-facing bitmap 为 `S×S`，原始 source/crop 宽高与拉伸系数独立保存，local X/Y domain 始终各为 0..1000；Stage-1 八等分与 Stage-2 十六等分无残格，线条和标签按 0.5 opacity 合成；inverse transform 分别映射回原始宽高；双向闭环、floor/ceil 与 half-up 分工、分项误差预算通过 property tests。
- **VLM/Agent**：任务第一步必须是 executable observe；同一个具备视觉能力的 Agent 必须从 overview 输出数值型 normalized Stage-1 坐标，并从 fresh detail 输出数值型 Stage-2 局部坐标；自然语言指令不能替代坐标，模型不做 local→global 数学。prompt/schema/labels/decoder 一致、protocol 显式选择并通过当前 fingerprint 校准；不清楚则拒绝。只按 §31 的 provenance 例外省略旧 computer-use screenshots。
- **Robustness**：ownership/transform/coverage、geometry version、capture freshness、default INDETERMINATE failclosed、上下文授权验证均有效；锁内复查、request 去重、仅 predispatch retry、partial dispatch unknown/cleanup 与禁止 replay 通过故障注入。
- **Performance**：hot path 无临时截图文件，不 encode 每帧；crop/resize/overlay 按需；提供 §58/§59 benchmark。

---

# 64. SKILL.md 职责

最终给 Agent 使用的 `SKILL.md` 不应塞入全部数学，只应说明：每个新任务第一步必须通过 allowlisted executable `observe`，之后什么时候 `inspect_region`、什么时候 `interact_detail`、什么时候重新 `observe`、如何传递 `requested_action`、target unclear 时绝不猜测。skill 不注册或调用 `computer_use` tool，也不允许后台私有 VLM grounding；它通过受控 `exec_command` argv 取得 multimodal observation。详细数学归 `backend/computer_use/coordinates.py`、`render.py` 与 `model_adapter.py`；详细平台实现归 `backend/computer_use/native/`、`native_client.py`；通过 tests 保证 correctness。项目接入必须走已有 session-bound wrapper / `OperationCapability` 授权链，skill、模型参数或 crop ID 本身不能授予桌面输入权限；上下文身份由可信调用链绑定。若向前端报告进展，沿统一 timeline event/projection，不另造 SSE 协议。

---

# 65. 最终核心 Invariants

1. Stage1 看完整 monitor 先拉伸成正方形、再叠加八等分半透明网格的 overview；两个 stage 来自同一未拉伸的 canonical immutable frame。
2. Stage-1 与 Stage-2 的 live wire coordinate 都固定为两个独立的 normalized `[0,1000]` 轴；canonical `1000` 是归一化参考长度，不是 bitmap pixel 尺寸。历史 pixel 协议只供离线对照。
3. crop 丢弃 void、不 padding，actual integer rect 是唯一权威；Stage2 把真实 crop 各轴分别拉伸至 `S×S` 后绘制完整十六等分网格（偶数内线标签值沿奇数正交位置重复），两轴各自归一化到 0..1000，原始 crop width/height 分别决定 inverse transform。
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

`computer_use.debug_save_images` 默认关闭。启用时仅把两阶段最终带网格的原样 PNG 保存到项目 `logs/computer-use/vision/`，不修改模型消息；`debug_image_max_files` 与 `debug_image_max_bytes` 限制本功能文件的总量，磁盘失败不影响观察或输入。


当前部署 grid_label_placement=repeated；Stage-1 无文字，Stage-2 偶数值 125..875 分别在 Y=31.25,156.25,...,906.25（X 标签）及 X=31.25,156.25,...,906.25（Y 标签）重复，共 56 X 与 56 Y 标签。edge 在端点标注同组偶数值。奇数 guide 不标自己的值；黑描边与 placement 进入 renderer/deployment fingerprint。
