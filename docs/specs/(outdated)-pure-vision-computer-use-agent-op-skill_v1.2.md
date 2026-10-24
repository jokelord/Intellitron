# Pure-Vision Two-Stage Computer Use Grounding Skill

> **Superseded:** Use `docs/Pure-Vision-Two-Stage-Computer-Use-Grounding-Skill_v1.8.md` instead. This historical v1.2 document predates the same-Agent executable observation flow and current coordinate/grid rules; it must not be used to implement the current skill.

## 0. 文档状态

**版本：** MVP v1.2

**v1.2 修订（2026-09-14）：** 依据评审意见修订 —— Stage-1 越界 fail-closed（§9）；Stage-2 model-facing 整数坐标域（§15.1/§16.1/§24/§25/§33）；Model Coordinate Adapter（§60）与 Coordinate Calibration（§61）；统一 round_half_up_positive 取整规则（§62）；grid label 语义（§22.1）；convention 混淆风险修正与防御优先级（§34）；完整错误状态全集 + bounded retry budget（§35）。

**目标平台：**

* Ubuntu 24.04
* ARM64
* GNOME / Wayland 为主要目标
* True X11 兼容
* Python backend
* Next.js frontend
* 本地 VLM，例如 Qwen3-VL 8B 级模型

**核心路线：**

> Pure Vision Computer Use。

正确性不得依赖：

* AT-SPI / A11Y Tree
* Qt API
* Browser DOM
* OmniParser
* OCR
* 应用自身 automation API

桌面与应用统一视为：

> **可观察像素 + 可注入鼠标输入的黑盒系统。**

---

# 1. MVP 目标

构建一个：

> **高通用性、高鲁棒性、低额外开销、坐标变换完全可测试**

的 Linux Desktop Computer Use grounding subsystem。

必须适用于：

* GTK
* Qt
* Electron
* Chromium
* XWayland
* Canvas
* 自绘 UI
* OpenGL / Vulkan UI
* accessibility 信息缺失的应用

MVP 只实现：

* monitor screenshot observation
* two-stage visual grounding
* absolute pointer click
* left/right click
* Wayland
* X11
* single monitor

暂不实现：

* multi-monitor composite
* cross-monitor crop
* drag/drop
* scrolling
* keyboard automation
* OCR
* AT-SPI fast path
* OmniParser
* semantic element IDs

---

# 2. Major Monitor Scope

一个 Computer Use session 只绑定：

```text
one major monitor
```

session 建立后，该 monitor 在整个 session 生命周期中保持稳定。

以下变化必须使当前 observation / crop 全部失效：

* monitor disconnect
* resolution change
* scale change
* rotation change
* ScreenCast stream recreate
* libei region change
* X11 monitor geometry change

Backend 保存：

```text
geometry_version
```

任何 geometry change 都必须：

```text
geometry_version += 1
```

所有：

```text
frame_id
observation_id
crop_id
```

均绑定 geometry_version。

---

# 3. 最重要的坐标原则

## 3.1 `1000` 是 normalized coordinate domain

本系统中：

```text
1000
```

不是：

```text
1000 pixels
```

而是：

> **归一化参考长度。**

例如原始 monitor raster：

```text
3840 × 2160
```

某点实际 raster X 为：

```text
2880
```

对应 normalized X：

```text
Xn = 1000 × 2880 / 3840
   = 750
```

因此：

```text
relative_x = 1000 × raster_x / raster_width
relative_y = 1000 × raster_y / raster_height
```

反向：

```text
raster_x = relative_x / 1000 × raster_width
raster_y = relative_y / 1000 × raster_height
```

---

# 4. Stage-1 Global Normalized Space

Stage 1 永远使用：

```text
X ∈ [0,1000]
Y ∈ [0,1000]
```

分别表示：

```text
X / 1000 = horizontal ratio
Y / 1000 = vertical ratio
```

注意：

> Stage-1 的 X/Y normalized units 是两个独立的比例轴。

因此对于一个：

```text
3840 × 2160
```

的 monitor：

```text
500 horizontal units = 1920 raster pixels
500 vertical units   = 1080 raster pixels
```

Stage-1 normalized space 不要求：

```text
1 X unit
==
1 Y unit
```

具有相同物理视觉长度。

它只用于：

> **全屏 coarse relative localization。**

---

# 5. 图片绝不能为了坐标而变形

假设 monitor：

```text
3840 × 2160
```

Stage-1 overview 可以生成：

```text
1536 × 864
1280 × 720
1000 × 562
```

但禁止生成：

```text
1000 × 1000
```

如果这意味着非等比拉伸。

必须始终：

```text
overview_width / overview_height
≈
source_width / source_height
```

Stage-1 的 `[0,1000]²`：

> 只是 coordinate domain，不是 bitmap dimensions。

---

# 6. Two-Stage Grounding 总体流程

```text
Native Monitor Frame F
        │
        ├───────────────┐
        │               │
        ▼               │
Clean Overview          │
        │               │
        ▼               │
Stage-1 VLM             │
        │               │
inspect_region          │
(X,Y,zoom)              │
        │               │
        ▼               │
Normalized Crop Rect    │
        │               │
clip to monitor         │
        │               │
        ▼               │
Exact Native Crop ◄─────┘
        │
        ▼
Aspect-Ratio-Preserving Resize
        │
        ▼
Stage-2 Local Coordinate Space
        │
        ▼
Grid + Relative Ticks
        │
        ▼
Stage-2 VLM
        │
        ▼
click_detail(crop_id,u,v)
        │
        ▼
Deterministic inverse transform
        │
        ▼
Monitor ratio
        │
        ▼
Wayland/libei or X11/XTEST
        │
        ▼
CLICK
```

---

# 7. Immutable Observation

每次 Stage-1 observation 必须产生：

```text
observation_id
frame_id
geometry_version
```

例如：

```text
observation_id = obs_1042
frame_id       = frame_8812
geometry_version = 7
```

Stage 1 与 Stage 2：

> **必须来自同一个 frozen native frame。**

正确：

```text
F42
 ├─ overview
 └─ detail crop
```

错误：

```text
Stage 1 → F42
Stage 2 → F47
```

因为真实桌面可能发生：

* animation
* popup
* window movement
* scroll
* layout change

---

# 8. PipeWire Frame Ownership

所谓 immutable frame 不允许仅仅保存：

```text
pointer_to_pipewire_buffer
```

然后把该 buffer 重新交还 PipeWire。

一旦：

```text
freeze_frame()
```

完成，Frame 的图像内容必须保证在其生命周期内不会被下一帧覆盖。

MVP 允许直接：

```text
copy latest complete frame
```

正确性优先于 zero-copy。

未来可以优化：

* ref-counted buffers
* pinned buffers
* DMA-BUF lifetime management

但不属于 MVP correctness requirement。

---

# 9. Stage-1 Tool Contract

VLM 使用：

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

非法 zoom：

```text
3
6
"large"
```

必须返回：

```text
INVALID_ZOOM
```

不得自动猜测。

x/y 越界（例如 `x = 1200`、`x = -50`、`y = 1001`）：

```text
INVALID_GLOBAL_COORDINATE
→ 不生成 crop
→ fail closed
```

必须严格区分两类情形：

```text
合法中心点 + nominal 超出屏幕:
    x = 50, zoom = 2
    nominal crop = [-200, 300]
    → 合法 request，执行 §11 edge clipping

非法中心点:
    x = -50
    → 非法 request，直接 reject
```

clip 只允许作用于合法中心点产生的 nominal rectangle；
它绝不是对非法模型输出的静默修复。

---

# 10. Zoom 的真实语义

`zoom` 表示：

> **Stage-2 nominal field-of-view。**

不是：

> PIL resize 的固定倍率。

定义：

```text
S = 1000 / zoom
```

因此：

```text
2x  → nominal span = 500
4x  → nominal span = 250
8x  → nominal span = 125
16x → nominal span = 62.5
```

围绕 Stage-1 point：

```text
(X,Y)
```

生成 nominal global normalized region：

```text
L = X - S/2
R = X + S/2

T = Y - S/2
B = Y + S/2
```

然后执行 edge clipping。

---

# 11. Edge Crop Policy

MVP 使用：

> **clip-to-monitor-boundary**

不：

* padding
* 添加黑边
* 添加透明区
* duplicate edge pixels
* 从其他 monitor 补像素

执行：

```text
L = max(X - S/2, 0)
R = min(X + S/2, 1000)

T = max(Y - S/2, 0)
B = min(Y + S/2, 1000)
```

因此 edge crop 允许：

```text
R-L != S
B-T != S
```

这是预期行为。

---

# 12. Void Region 的原则

如果 nominal crop 超出 monitor：

```text
[-100, 400]
```

其中：

```text
[-100,0)
```

属于不存在的 void region。

本系统：

> **完全丢弃 void region。**

最终 crop：

```text
[0,400]
```

不存在对应 bitmap 的区域绝不进入 Stage-2 image。

因此：

```text
every Stage-2 image pixel
must originate from the frozen source frame
```

---

# 13. Normalized Crop → Native Raster

Normalized crop：

```text
L,T,R,B
```

转换为实际 source raster：

```text
x0 = floor(L / 1000 × Wp)
y0 = floor(T / 1000 × Hp)

x1 = ceil(R / 1000 × Wp)
y1 = ceil(B / 1000 × Hp)
```

然后 clamp：

```text
0 <= x0 < x1 <= Wp
0 <= y0 < y1 <= Hp
```

实际 crop 使用半开区间：

```text
[x0,x1)
[y0,y1)
```

---

# 14. Exact Integer Crop Rect 是权威几何

一旦：

```text
x0,y0,x1,y1
```

确定：

> 后续所有 Stage-2 inverse transform 必须基于这个实际 crop rect。

不得重新只根据：

```text
X
Y
zoom
```

计算。

原因：

```text
floor
ceil
integer raster rounding
```

会使理论 normalized crop 和最终实际 raster crop 产生极小差异。

因此保存：

```text
crop_width  = x1 - x0
crop_height = y1 - y0
```

---

# 15. Stage-2 Local Isotropic Coordinate Space

这是 Stage-2 与 Stage-1 最重要的区别。

Stage-2 必须基于：

> **实际最终 crop 的真实 H/W ratio。**

设最终 crop 的宽高在同一尺度下为：

```text
cw
ch
```

这里只使用它们的比例：

```text
cw : ch
```

定义：

```text
M = max(cw,ch)
```

然后：

```text
local_width  = 1000 × cw / M
local_height = 1000 × ch / M
```

因此永远：

```text
max(local_width,local_height) = 1000
```

同时：

```text
local_width / local_height
==
cw / ch
```

## 15.1 Model-Facing Integer Domain

local_width / local_height 的**几何定义**保持上述精确比例；
但暴露给模型的 coordinate domain 必须整数化：

```text
长轴 model-facing 值 = 1000
短轴 model-facing 值 = round_half_up_positive(1000 × short / long)
```

例如 exact `888.888...` → model-facing `889`。

因此：

* 模型 contract 永远是整数域，如 `[0,889] × [0,1000]`；
* backend 将短轴 model-facing 最大值视为该轴 100%；
* 逆变换（§25）使用 model-facing integer 值作为权威；
* 内部保留 `exact_crop_aspect = cw / ch` 仅供 tests 与
  diagnostics，绝不进入 prompt 或模型可见 schema。

不设 "max + 1" 式越界容忍带：合法整型域内的整数输出恒有效，
域外整数输出一律 reject（§24）。

整数化引入的映射偏差远小于一个 local unit，
属于 roundtrip 的 raster rounding 容差范围。

---

# 16. Stage-2 例子：500 × 260

假设经过：

```text
edge clipping
+
exact crop
```

后，实际 crop 的长宽比等效为：

```text
500 : 260
```

注意：

这里的：

```text
500
260
```

表示同一尺度下的**相对长度**。

不是要求 source bitmap 真的是：

```text
500 pixels × 260 pixels
```

则：

```text
M = 500

local_width  = 1000 × 500 / 500
             = 1000

local_height = 1000 × 260 / 500
             = 520
```

Stage-2 coordinate domain 因此是：

```text
X ∈ [0,1000]
Y ∈ [0,520]
```

Stage-2 raster 同样保持：

```text
1000 : 520
```

或任何与其等比例的 bitmap。

绝不能拉伸成：

```text
1000 × 1000
```

## 16.1 整数化例子：800 × 900

实际 crop 比例为 800 : 900（portrait）：

```text
M = 900

长轴 = 1000（Y）
短轴 exact = 1000 × 800 / 900
          = 888.888...

短轴 model-facing = round_half_up_positive(888.888...)
                  = 889
```

Stage-2 model-facing coordinate domain：

```text
X ∈ [0,889]
Y ∈ [0,1000]
```

模型 contract 中不出现 `888.888...`；
backend 内部保留 exact ratio 供 diagnostics。

---

# 17. 为什么 Stage-2 使用 isotropic coordinates

Stage-1：

```text
0..1000 × 0..1000
```

只表达全屏 percentage。

Stage-2 的目的则是：

> precise visual localization。

因此 Stage-2 使用：

```text
1 local unit horizontally
≈
1 local unit vertically
```

对应同等图像尺度。

这使：

* grid cell 为真正正方形
* horizontal / vertical visual distance 可直接比较
* grid 可以作为一致 spatial landmark

所以：

```text
Stage 1:
anisotropic percentage coordinate

Stage 2:
isotropic local relative coordinate
```

是有意设计。

---

# 18. Stage-2 Raster Resize

Stage-2 image：

```text
native crop
→ aspect-ratio-preserving resize
→ detail raster
```

默认：

```text
detail_long_edge_px = 1000
```

但注意：

```text
detail_long_edge_px
```

是 bitmap rendering 参数。

而：

```text
local_long_edge = 1000
```

是 model coordinate 参数。

两者必须在代码中独立存在。

默认情况下二者数值恰好相同，只是实现便利。

未来：

```text
detail_long_edge_px = 1536
```

时，local coordinate 仍然可以：

```text
long edge = 1000
```

但注意：修改 `detail_long_edge_px` 属于 detail renderer
尺度假设变更。在 coordinate protocol 未经 §61 calibration
确认前不得进行（MVP 硬性规定，见 §61）。

---

# 19. Resize 顺序

必须：

```text
native crop
→ resize
→ grid
→ ticks
→ labels
```

禁止：

```text
native crop
→ grid
→ resize
```

因为 interpolation 会导致：

* grid blur
* tick blur
* label blur
* overlay 边缘污染 GUI

默认 interpolation：

```text
PIL.Image.Resampling.LANCZOS
```

---

# 20. Stage-2 Grid

默认：

```text
grid_enabled   = true
grid_divisions = 16
```

精确定义：

> `grid_divisions=16` 表示完整 1000-unit 长轴被分为 16 个 cell。

因此：

```text
grid_step = 1000 / 16
          = 62.5
```

内部 guide lines：

```text
62.5
125
187.5
...
937.5
```

图片边缘单独绘制，不计入内部 guide line 数量。

---

# 21. Variable-Shape Grid

例如：

```text
local_width  = 1000
local_height = 520
```

则 vertical guide：

```text
x = 62.5,125,...,937.5
```

horizontal guide：

```text
y = 62.5,125,...,500
```

最后 image boundary：

```text
y = 520
```

必须标明真实 boundary。

不能伪装成：

```text
y = 1000
```

---

# 22. Grid Rendering 要求

必须：

* thin lines
* low/moderate opacity
* high enough contrast
* minimum text occlusion
* labels inside existing image canvas
* 不增加 canvas padding

推荐：

```text
major tick = 125
minor tick = 62.5
```

label 带：

* outline
* shadow

避免被浅色/深色 UI 吞没。

## 22.1 Grid/Tick Label 语义

Grid 与 tick 的数值 label **必须表达 Stage-2 local coordinate 值**：

* 不是 raster pixel coordinates
* 不是 percentage
* 与 prompt 中声明的 model-facing integer domain 完全一致

例如 `local = 1000 × 520`，长轴 major tick labels：

```text
0
125
250
375
500
625
750
875
1000
```

短轴 labels 到最后一个内部 guide 为止，
并**必须明确标出真实 boundary 数值**：

```text
... 375 500 [520]
```

（portrait 时如 `1000 × 889`，boundary label 为 889。）

这是对抗 §34.2 convention 混淆的主要 runtime 防御之一。

---

# 23. Grid 是 grounding aid，不是 geometry correctness

Two-stage architecture 的 correctness 不允许依赖：

```text
grid 一定提高模型准确率
```

因此实现必须允许：

```text
grid_enabled = false
```

MVP benchmark 至少比较：

```text
A. detail image without grid
B. detail + 8 divisions
C. detail + 16 divisions
```

默认实现仍按本 SPEC 使用：

```text
16 divisions
```

但 benchmark 结果必须记录。

---

# 24. Stage-2 Tool Contract

Stage-2 使用：

```text
click_detail(
    crop_id,
    x,
    y,
    button?
)
```

其中：

```text
crop_id:
    required

x:
    local isotropic normalized X

y:
    local isotropic normalized Y

button:
    enum ["left","right"]
    default "left"
```

Backend 根据 crop_id 获得：

```text
local_width
local_height
```

并验证（local_width / local_height 为 §15.1 定义的
model-facing integer 域上界，不出现小数）：

```text
0 <= x <= local_width
0 <= y <= local_height
```

明显越界：

```text
x = 1200
```

不得 silent clamp。

返回：

```text
INVALID_LOCAL_COORDINATE
```

仅允许针对 floating-point epsilon 做微小修正
（例如输出 `889.0` 而域上界为整数 `889`）。

不设 "max + 1" 式越界容忍带：
合法整型域内的整数输出恒有效（含 `u = local_width`
这类边界输出，见 §15.1），域外整数输出一律 reject。

---

# 25. Stage-2 Local → Crop Ratio

模型输出：

```text
u
v
```

计算（local_width / local_height 为 §15.1 定义的
model-facing integer 值；短轴最大值即该轴 100%）：

```text
rx = u / local_width
ry = v / local_height
```

得到：

```text
rx ∈ [0,1]
ry ∈ [0,1]
```

然后映射到 exact crop：

```text
source_x =
    x0 + rx × (x1-x0)

source_y =
    y0 + ry × (y1-y0)
```

这里：

```text
source_x
source_y
```

属于连续 image geometry。

它们不是数组 pixel index。

---

# 26. Continuous Geometry 与 Pixel Index 必须区分

归一化：

```text
X = 1000
```

语义是：

> image 的最右侧边界。

它不是：

```text
pixel index = width
```

因此系统必须区分：

```text
Normalized Continuous Coordinate
Raster Pixel Index
Backend Input Coordinate
```

Normalized edge：

```text
0..1000
```

允许包含 endpoint。

Raster sample：

```text
0..Wp-1
```

不允许：

```text
Wp
```

二者不得混用。

---

# 27. Monitor Global Ratio

得到 continuous source point 后：

```text
nx = source_x / Wp
ny = source_y / Hp
```

或者等价理解为：

```text
global_normalized_x / 1000
global_normalized_y / 1000
```

正式 Vision → Platform interface 只有：

```text
nx ∈ [0,1]
ny ∈ [0,1]
```

Vision subsystem 不知道：

* Wayland logical size
* fractional scale
* X11 root geometry
* compositor topology

---

# 28. Upper Boundary Safety

`nx = 1.0`：

语义上合法，代表 image right edge。

但是 OS input backend 必须保证：

> 最终 absolute pointer point 确实落在 backend advertised input region 内。

Wayland backend 应：

1. 计算 candidate logical point；
2. 使用对应 libei region 验证；
3. 如果恰好因为 upper-boundary endpoint 落在 region 外：

   * 只向内部做最小 epsilon 修正；
4. 再次验证；
5. 若仍不在 region 内，拒绝 click。

禁止：

```text
model 1200
→ clamp to 1000
→ click edge
```

这种 fail-open 行为。

---

# 29. Stage-2 必须重新进行 Vision Inference

Stage 2 必须：

> 根据新的 detail image 本身重新定位 target。

不能仅凭 Stage-1 reasoning memory：

```text
“它大概就在这里”
```

直接 extrapolate。

---

# 30. GroundingRequest Context

每次 Computer Use grounding request 必须显式保存：

```text
GroundingRequest {
    request_id
    target_description
    requested_action
}
```

例如：

```text
target_description =
"VS Code 左下角的 Settings 齿轮按钮"
```

Stage-1 inference：

```text
target_description
+
clean overview
```

Stage-2 inference：

```text
same target_description
+
detail image
+
crop coordinate metadata
```

---

# 31. Stage-2 不依赖上一张图片的上下文缓存

默认推荐：

```text
Stage-1:
1 image

Stage-2:
1 new image
```

Stage-2 不重复发送 full overview。

同时 Stage-2 不得依赖：

```text
previous image still exists in model context
```

如果 VLM server 使用 conversational history：

> 在 Stage-2 请求中必须避免旧 overview 被再次作为 image input 重新编码。

需要保留的是：

```text
target_description
```

而不是旧 image。

---

# 32. Stage-1 Prompt Contract

Stage-1 prompt 应明确：

```text
You are viewing a clean screenshot of one monitor.

Locate the requested target approximately.

The coordinate reference domain is:
top-left     = (0,0)
bottom-right = (1000,1000)

These numbers are relative coordinates, not pixels.

Do not perform the final click.

Call:
inspect_region(x,y,zoom)

Allowed zoom:
2,4,8,16.

Use the lowest zoom that is likely to make the
target clearly distinguishable.
```

---

# 33. Stage-2 Prompt Contract

Stage-2 prompt 必须动态插入：

```text
crop_id
local_width
local_height
target_description
```

其中 `local_width` / `local_height` 是 §15.1 的 model-facing
integer 值（如 `1000 × 520`、`1000 × 889`、`375 × 1000`）。

model-visible coordinate system 整体是 integer domain：

* prompt 中不出现小数（绝不出现 `888.888...`）
* 模型输出按整数坐标处理

语义：

```text
You are viewing a new zoomed-in detail image
derived from the same frozen desktop observation.

Target:
{target_description}

Use THIS image to determine the final click.

Coordinates are LOCAL and RELATIVE to this crop.

Top-left:
(0,0)

Bottom-right:
({local_width},{local_height})

The longest image axis is normalized to 1000.
The shorter axis preserves the image's true aspect ratio.

These are relative coordinates, not pixels.

Do not calculate global desktop coordinates.

Call:
click_detail(crop_id,x,y)

If the target is not clearly identifiable,
call reobserve() instead of guessing.
```

---

# 34. Stage-2 Coordinate Convention 风险

不同 VLM 的 grounding 输出约定不同，且同名模型不同版本也可能不同：

* Qwen2.5-VL 官方支持 absolute position coordinates
  （相对输入图像尺寸）；
* Qwen3-VL computer-use 官方采用固定 `1000 × 1000`
  relative grid，输入图片保持原始宽高比；
* GUI fine-tune checkpoint、system prompt、serving framework
  都可能改变实际 contract（不同 vLLM 版本已出现
  coordinate regression 的实际案例）。

因此**不得根据模型名字想当然认定坐标协议**；
VLM raw output 的解读必须由 Model Coordinate Adapter（§60）
按显式 protocol 处理，并经 §61 calibration 验证。

## 34.1 本 SPEC 的 custom Stage-2 contract

Stage-2 有意采用：

```text
longest axis = 1000
short axis   < 1000
```

原因是：

* preserve true crop H/W
* isotropic local units
* square grid landmarks

因此实现必须支持配置：

```text
stage2_coordinate_mode =
    "isotropic_long_edge_1000"
```

并建议同时保留实验模式：

```text
"qwen_square_1000"
```

用于 A/B benchmark。

注意：`stage2_coordinate_mode` 定义的是 canonical geometry domain；
`model_coordinate_protocol`（§60）定义的是 VLM raw output 的解读。
两者独立配置、独立测试。

MVP 默认实现本 SPEC 的：

```text
isotropic_long_edge_1000
```

不得未经测试擅自替换。

## 34.2 Convention 混淆的不可检测误读

isotropic 与 square 域存在合法值重叠，
单次 in-range 输出无法判断模型用的是哪种约定。
真实目标位于 50% vertical 时：

```text
isotropic 模型（local_height = 520）:
    y = 260        # 260/520 = 50%  ✓

square_1000 模型:
    y = 500        # 500/1000 = 50%  ✓

backend 若把 square 的 500 误按 isotropic 解读:
    500/520 = 96.15%   # 几乎点到底部
```

更隐蔽的是域内重叠：

```text
square 模型输出 y = 300
300 ∈ [0,520]，不会被 reject
30% 被误读为 300/520 = 57.7%
```

防御优先级（禁止 runtime 动态猜 convention）：

1. Model Adapter 显式声明 coordinate protocol（§60）；
2. 部署前 calibration（§61）；
3. prompt、grid labels 与该 protocol/domain 完全一致
   （§22.1、§33）；
4. audit log 做 convention drift detection —— 仅诊断告警，
   不作为 runtime correctness 机制。

---

# 35. Retry State Machine

MVP 状态机：

```text
FRESH_OBSERVATION
        │
        ▼
STAGE1_OVERVIEW
        │
        ▼
inspect_region
        │
        ▼
DETAIL_READY
    │         │
    │         └── target unclear
    │                │
    │                ▼
    │            reobserve
    │                │
    │                ▼
    │         FRESH_OBSERVATION
    │
    ▼
click_detail
    │
    ▼
PRECLICK_VALIDATE
    │
    ▼
CLICK_SENT
```

Stage-2 不允许直接调用：

```text
inspect_region(global_x,global_y)
```

因为 Stage-2 模型只有 local coordinate。

这避免迫使 VLM 进行：

```text
local → global
```

数学。

## 35.1 错误与异常状态全集

状态机必须显式处理以下状态：

```text
INVALID_GLOBAL_COORDINATE   (§9)
INVALID_ZOOM                (§9)
INVALID_LOCAL_COORDINATE    (§24)
STALE_FRAME                 (§48)
GEOMETRY_CHANGED            (§2)
TARGET_NOT_VISIBLE          (§36)
TARGET_AMBIGUOUS            (§36)
```

## 35.2 两类失败，两条处理路径

```text
Perception failure:
    target unclear / wrong crop / stale
    → 丢弃当前 crop
    → fresh observation（reobserve）

Protocol failure:
    invalid coordinate / invalid zoom
    → tool error 原样返回给模型
    → 允许 bounded correction retry
```

两类失败都不允许静默吞掉，
也不允许把 perception 失败当 protocol 错误回灌。

## 35.3 全局有限预算（必须配置化）

```text
max_tool_contract_retries = 1
max_grounding_reobserves  = 2
max_click_attempts        = 3
```

各分支自身可以没有 recursion，
但 invalid → retry → stale → reobserve → unclear → reobserve
这样的跨分支组合仍可能无限循环，
因此必须存在全局有限预算。

任何预算耗尽：

```text
→ abort 本次 grounding request
→ fail closed，不产生 click
→ 结果与原因写入 audit log
```

预算数值是默认值，必须可配置，不得硬编码绕过。

---

# 36. `reobserve()`

Tool：

```text
reobserve(reason?)
```

reason：

```text
target_not_visible
target_ambiguous
wrong_region
ui_changed
stale
other
```

调用后：

* 当前 crop 不再继续 click；
* 捕获 fresh observation；
* 回到 Stage 1。

禁止无限 recursive zoom。

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

不得 hot-path：

```text
observation
→ one-shot screenshot command
→ PNG file
```

应该：

```text
PipeWire stream
      ↓
latest complete frame slot
      ↓
freeze_frame() on demand
```

旧的未消费 frame：

```text
drop
```

不需要处理全部 60 FPS。

---

# 38. Wayland Portal Session Lifecycle

Wayland RemoteDesktop + ScreenCast 应尽量使用同一个 portal session。

逻辑顺序：

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

MVP monitor source：

```text
types = MONITOR
multiple = false
```

cursor：

```text
prefer HIDDEN
```

如果 portal 不支持 HIDDEN：

使用可用的最佳 cursor mode，并记录 capability。

---

# 39. Wayland Major Monitor 的定义

在标准 portal 路径中：

> major monitor = 当前 RemoteDesktop / ScreenCast session 实际授权并返回的唯一 monitor stream。

不要要求：

```text
通过私有 GNOME API 强行选择 primary monitor
```

用户显式选择或 restore token 恢复的 monitor：

就是当前 session 的 major monitor。

---

# 40. Wayland mapping_id

如果 PipeWire ScreenCast stream 提供：

```text
mapping_id
```

同时 libei region 提供对应：

```text
mapping_id
```

必须优先使用它建立：

```text
capture stream
↔
absolute input region
```

关联。

不能仅因为：

```text
目前只有一个 monitor
```

就完全忽略这层绑定。

---

# 41. Wayland Fractional Scaling

绝对禁止：

```text
input_x = screenshot_x / 1.5
```

或：

```text
input_x = screenshot_x × 1.5
```

Vision 输出：

```text
nx
ny
```

假设 matching libei region：

```text
origin = (ox,oy)
size   = (Rw,Rh)
```

candidate：

```text
input_x = ox + nx × Rw
input_y = oy + ny × Rh
```

然后进行：

```text
region containment validation
```

fractional scale 由 compositor / EIS mapping 自己处理。

---

# 42. Wayland Absolute Click

推荐：

```text
RemoteDesktop
→ ConnectToEIS
→ libei
→ absolute pointer
```

transaction：

```text
absolute motion
button down
button up
frame / flush
```

Backend 成功最多表示：

```text
CLICK_SENT
```

不得自动宣称：

```text
ACTION_SUCCEEDED
```

---

# 43. X11 Backend

True X11 session：

Capture：

```text
XShm / MIT-SHM
```

Input：

```text
XTEST
```

如果 major monitor root rect：

```text
origin = (ox,oy)
width  = Rw
height = Rh
```

则：

```text
root_x = ox + nx × Rw
root_y = oy + ny × Rh
```

同样必须处理 upper boundary。

---

# 44. XWayland

如果：

```text
desktop session = Wayland
```

但 target app 是 XWayland：

仍然使用：

```text
Wayland capture
+
Wayland input
```

Backend 根据：

> desktop session

选择。

不是根据：

> target application toolkit。

---

# 45. Backend Capability Detection

不得只检查：

```text
$WAYLAND_DISPLAY
```

`doctor.py` 必须检查实际 capability。

Wayland：

```text
ScreenCast portal
RemoteDesktop portal
PipeWire
ConnectToEIS
absolute pointer capability
cursor modes
mapping_id availability
```

X11：

```text
DISPLAY
MIT-SHM
XTEST
monitor geometry
```

输出 machine-readable diagnosis。

---

# 46. Image Transport

默认：

```text
PNG
```

原因：

GUI 包含：

* small text
* sharp edges
* thin lines
* small icons

允许：

```text
data:image/png;base64,...
```

但内部不得：

```text
save PNG
→ reopen
→ base64
```

必须：

```text
memory buffer
→ crop
→ resize
→ overlay
→ in-memory encode
→ VLM
```

---

# 47. Demand-Driven Processing

PipeWire 可以持续 capture。

但：

```text
freeze
resize
crop
grid
PNG encode
base64
```

只在 agent 请求 observation 时进行。

禁止：

```text
encode every PipeWire frame
```

---

# 48. Pre-Click Staleness Check

Stage 1/2 虽然绑定 frozen frame：

```text
F42
```

但 inference 期间真实 UI 仍可能变化。

click 前应：

```text
read current latest frame
```

并做 best-effort staleness check。

至少包含：

```text
global coarse comparison
+
target neighborhood comparison
```

可采用：

* downscaled grayscale diff
* perceptual hash
* lightweight SSIM-like score

结果：

```text
FRESH
STALE
INDETERMINATE
```

默认：

```text
STALE → abort click + reobserve
```

如果启用严格模式：

```text
INDETERMINATE → abort + reobserve
```

staleness check 只是：

> race mitigation

不是 atomic desktop transaction。

---

# 49. Crop Metadata

每个 DetailCrop 至少保存：

```text
crop_id

request_id
observation_id
frame_id
monitor_id
geometry_version

source_width_px
source_height_px

x0
y0
x1
y1

crop_width_px
crop_height_px

detail_width_px
detail_height_px

local_width
local_height
exact_crop_aspect

rough_global_x
rough_global_y

requested_zoom

grid_enabled
grid_divisions

created_timestamp
```

VLM 只看到必要字段。

`local_width` / `local_height` 为 §15.1 的 model-facing integer 值；
`exact_crop_aspect = cw / ch` 仅供 tests 与 diagnostics，
不进入模型上下文。

---

# 50. Audit Log

每次 click attempt 记录：

```text
request_id

observation_id
frame_id
crop_id
geometry_version
monitor_id

target_description

rough_x
rough_y
requested_zoom

crop_x0
crop_y0
crop_x1
crop_y1

local_width
local_height

final_local_x
final_local_y

native_source_x
native_source_y

monitor_ratio_x
monitor_ratio_y

backend_type
backend_input_x
backend_input_y

mapping_id

model_coordinate_protocol

stale_check_result

click_sent
```

目标：

能够快速区分：

```text
VLM grounding error
```

和：

```text
coordinate/backend mapping error
```

---

# 51. Coordinate Module Ownership

`coordinates.py` 是 correctness-critical module。

它必须成为以下数学转换的唯一实现：

```text
Stage1 normalized
→ crop geometry

normalized crop
→ native raster rect

Stage2 local
→ crop ratio

crop ratio
→ source continuous coordinate

source coordinate
→ monitor ratio

monitor ratio
→ backend input coordinate
```

禁止在：

* tool handler
* agent logic
* Wayland backend
* X11 backend

复制一套不同公式。

`coordinates.py` 只处理 canonical coordinate 数学，
完全不感知模型 coordinate convention；
"VLM raw output → canonical coordinate" 的解读
唯一归 Model Coordinate Adapter（§60）所有。

---

# 52. 推荐目录

```text
computer-use-vision/
├── SKILL.md
│
├── scripts/
│   ├── session.py
│   ├── capture.py
│   ├── frame_store.py
│   ├── overview.py
│   ├── detail_crop.py
│   ├── grid_overlay.py
│   ├── coordinates.py
│   ├── stale_check.py
│   ├── doctor.py
│   ├── model_adapter.py
│   ├── calibration.py
│   │
│   └── backends/
│       ├── base.py
│       ├── wayland.py
│       └── x11.py
│
└── tests/
    ├── test_stage1_mapping.py
    ├── test_crop_math.py
    ├── test_edge_crop.py
    ├── test_local_coordinates.py
    ├── test_coordinate_roundtrip.py
    ├── test_coordinate_endpoints.py
    ├── test_fractional_scaling.py
    ├── test_wayland_mapping.py
    ├── test_x11_mapping.py
    ├── test_frame_immutability.py
    ├── test_stale_frame.py
    ├── test_geometry_version.py
    ├── test_model_adapter.py
    ├── test_coordinate_calibration.py
    └── test_rounding_rules.py
```

---

# 53. Property-Based Tests

至少运行：

```text
10,000 randomized geometry cases
```

随机：

```text
native W/H
Stage1 X/Y
zoom
edge clipping
crop shapes
Stage2 local point
Wayland region sizes
region origins
```

验证：

```text
all crops valid

no padding

all detail pixels originate from source

aspect ratio preserved

local coordinates isotropic

all ratios inside [0,1]

mapping monotonic

no axis reversal

no geometry-version reuse

roundtrip error within raster rounding tolerance

model-facing local dims integer-valued

single rounding rule (round_half_up_positive) everywhere
```

---

# 54. Endpoint Tests

必须专门测试：

```text
(0,0)
(1000,0)
(0,1000)
(1000,1000)

(1,500)
(999,500)

(500,1)
(500,999)
```

并证明：

```text
normalized endpoint
```

永远不会错误地被当成：

```text
out-of-range pixel index
```

同时最终 Wayland click：

```text
must satisfy target libei region containment
```

---

# 55. Edge / Zoom Test Matrix

每个 edge case：

```text
2x
4x
8x
16x
```

必须测试：

* center
* left edge
* right edge
* top edge
* bottom edge
* four corners

验证：

```text
clip correct
no padding
actual ratio correct
Stage2 longest side = 1000
short side proportional
inverse transform correct
```

---

# 56. Resolution Test Matrix

至少：

```text
1920×1080
2560×1440
3840×2160

1920×1200
2560×1600

1280×1024

1080×1920
```

---

# 57. Fractional Scaling Test Matrix

Wayland：

```text
100%
125%
150%
175%
200%
```

测试必须证明：

```text
Vision normalized ratio
→ matching libei logical region
```

而不是：

```text
screenshot pixels / scale
```

---

# 58. Model Grounding Benchmark

Geometry correctness 和 VLM accuracy 必须分开测试。

至少建立 GUI benchmark：

```text
GTK
Qt
Electron
Chromium
Canvas
custom rendered UI
```

包含：

```text
large button
normal button
small toolbar icon
tiny icon
textless icon
edge target
corner target
dense toolbar
dark theme
light theme
high-DPI
```

比较：

```text
1. Stage1 direct click baseline

2. Two-stage without grid

3. Two-stage + 8-grid

4. Two-stage + 16-grid

5. isotropic Stage2 coordinate

6. optional Qwen-native square coordinate
```

至少记录：

```text
Stage1 target-region recall

Stage2 click hit rate

end-to-end hit rate

reobserve rate

false-click rate
```

---

# 59. Performance Benchmark

至少记录：

```text
capture freeze latency

frame copy latency

overview resize latency

native crop latency

detail resize latency

grid render latency

PNG encode latency

base64 overhead

Stage1 VLM latency

Stage2 VLM latency

total click latency

peak RSS
```

MVP 不要求处理每帧。

性能原则：

> 大部分时间成本应该来自必要的 VLM inference，而不是 screenshot plumbing。

---

# 60. Model Coordinate Adapter

VLM raw output 不得直接进入 geometry 计算。

架构边界：

```text
VLM raw output
      ↓
Model Coordinate Adapter
      ↓
Canonical Stage1 / Stage2 coordinate
      ↓
Deterministic Geometry
      ↓
OS Backend
```

`model_adapter.py` 是唯一知道
"当前 checkpoint 到底输出 0–1000 relative、
image pixels 还是其他 convention" 的模块。

职责：

```text
- 解析并归一化 Stage-1 raw (x,y)
  → canonical [0,1000]²
- 解析并归一化 Stage-2 raw (u,v)
  → canonical Stage-2 local integer domain（§15.1）
- INVALID_GLOBAL_COORDINATE /
  INVALID_LOCAL_COORDINATE 等错误码的输入侧判定
- 将生效的 model_coordinate_protocol 写入 audit log
```

protocol 显式声明（配置化，禁止 auto-guess）：

```text
model_coordinate_protocol:
    qwen3_relative_1000
    # 0..1000 × 0..1000 relative grid，
    # 输入图保持原始宽高比

    image_pixel
    # 相对输入图像尺寸的绝对像素坐标

    custom
    # 经 §61 calibration 确认的自定义映射
```

硬性约束：

* geometry（crop / libei / XTEST / fractional scaling）
  完全不感知 protocol；
* 更换 GUI VLM 或 serving stack 时只更换 adapter，
  crop、Wayland、X11、audit、property tests 全部不动；
* 禁止 runtime 从输出分布自动猜测 convention
  （audit drift detection 仅用于诊断告警，见 §34.2）。

---

# 61. Model Coordinate Calibration

模型安装或切换（包括 serving stack 升级）后、
首次 click 之前，必须运行一次小型 calibration suite：

* 生成若干人工图，目标位于已知比例位置，例如：

```text
25% width / 50% height
50% width / 50% height
75% width / 25% height
```

* 记录 raw 输出并判定其更接近：

```text
[250, 500]
  → relative_1000 类 protocol

[image_width × 0.25, image_height × 0.5]
  → image_pixel 类 protocol
```

* 判定结果一次性固定写入 adapter protocol。

MVP 硬性规定：

> coordinate protocol 尚未经过 calibration 前，
> 不允许修改 detail renderer 的尺度假设
> （`detail_long_edge_px`），也不允许上线 click。

原因：若模型实际输出 image pixels 而后端按 0–1000 解读，
会产生无法从单点 click 检测的系统性 scale error；
`detail_long_edge_px` 一旦改变，该偏差随之放大。

---

# 62. Deterministic Rounding Rules

所有 quantization 只允许一个定义：

```text
round_half_up_positive(x) = floor(x + 0.5)
```

几何长度与坐标均为非负数；
禁止依赖语言自带 bankers rounding
（如 Python `round(2.5) == 2`）。

统一应用于：

**A. Stage-2 model-facing short dimension（§15.1）**

```text
local_short = round_half_up_positive(1000 × short / long)
```

**B. detail raster（§18）**

```text
detail_long_px  = configured value
detail_short_px = max(1, round_half_up_positive(
                       detail_long_px × short / long))
```

**C. 最终 X11/XTEST integer coordinate（§43）**

```text
root_x = round_half_up_positive(continuous root_x)
root_y = round_half_up_positive(continuous root_y)
```

随后 clamp / validate 到 monitor integer domain。

prompt 与 model-facing domain 一律显示整数
（`1000 × 520`、`1000 × 889`、`375 × 1000`），
绝不显示小数；backend 内部保留 exact ratio
仅供 tests 与 diagnostics（§15.1）。

---

# 63. MVP Acceptance Criteria

## Platform

必须：

* GNOME Wayland 可以持续 PipeWire capture
* Wayland absolute click 可工作
* ScreenCast stream 与 libei region 可以正确关联
* fractional scaling 不产生系统性 click offset
* True X11 capture/input 可工作
* XWayland app 无需特殊 backend

## Geometry

必须：

* Stage1 normalized mapping 正确
* Stage2 crop 来自同一个 immutable frame
* edge crop 无 padding
* actual crop ratio 始终保留
* Stage2 long axis = 1000
* short axis 按真实 ratio 缩放
* endpoint 不发生 off-by-one
* property tests 无 invariant violation
* coordinate roundtrip 误差只来自 raster rounding
* Stage-2 model-facing domain 为整数
  （长边 1000，短边 round_half_up_positive）
* 所有 quantization 仅使用 round_half_up_positive

## VLM

必须：

* Stage1 只做 coarse location
* Stage2 根据 detail image 重新判断
* VLM 不进行 local→global 数学
* target unclear 时可以拒绝点击
* Stage2 只有当前 detail image，target description 显式传递
* model_coordinate_protocol 显式声明并通过 calibration
* VLM raw output 只经 Model Coordinate Adapter 进入 geometry

## Robustness

必须：

* frame 真正 immutable
* geometry_version 生效
* stale frame 能阻止明显错误 click
* invalid coordinates fail closed
* audit log 可区分 grounding 与 mapping error
* Stage-1 越界返回 INVALID_GLOBAL_COORDINATE 且不生成 crop
* invalid/stale/geometry-changed 分支受 bounded retry budget 约束

## Performance

必须：

* hot path 无 temporary screenshot file
* 不 encode 每个 PipeWire frame
* crop/resize/overlay demand-driven
* 输出完整 benchmark report

---

# 64. SKILL.md 职责

最终给 Agent 使用的 `SKILL.md` 不应塞入全部数学。

它只应说明：

```text
什么时候 observe

什么时候 inspect_region

什么时候 click_detail

什么时候 reobserve

target unclear 时绝不猜测
```

详细数学：

```text
coordinates.py
detail_crop.py
```

详细平台实现：

```text
backends/wayland.py
backends/x11.py
```

通过 tests 保证 correctness。

---

# 65. 最终核心 Invariants

### Invariant 1

> Stage 1 永远看完整、干净、不变形的 monitor overview。

### Invariant 2

> `1000` 永远代表 normalized reference coordinate，不代表 pixel resolution。

### Invariant 3

> Stage 1 与 Stage 2 永远来自同一个 immutable native frame。

### Invariant 4

> Stage-2 crop 超出 monitor 时直接舍弃 void region，不 padding。

### Invariant 5

> Stage-2 使用真实 crop H/W；最长边 normalized 为 1000，短边同比例缩放。

### Invariant 6

> Stage-2 的 local coordinate 与 detail image 的真实 H/W 一致，因此 local X/Y unit isotropic。

### Invariant 7

> VLM 只回答“当前图片里的目标在哪里”；所有坐标转换均由程序确定性完成。

### Invariant 8

> normalized continuous coordinate、raster pixel index、OS backend input coordinate 永远是三个不同概念。

### Invariant 9

> Wayland screenshot raster 不直接转换成鼠标 logical pixels；Vision 与 backend 只通过 monitor ratio 通信。

### Invariant 10

> 任何不确定、stale、geometry mismatch 或非法坐标都必须 fail closed，不允许猜测 click。

### Invariant 11

> VLM raw output 必须先经 Model Coordinate Adapter 归一为 canonical coordinate 才能进入 geometry；adapter 是唯一感知模型 coordinate convention 的模块。

### Invariant 12

> Stage-2 model-facing coordinate domain 必须是整数域；所有 quantization 只使用 round_half_up_positive；prompt 与 grid labels 不出现小数。

### Invariant 13

> coordinate protocol 未经 calibration 确认前，不得上线 click，也不得修改 detail renderer 尺度假设。

---

# 66. 核心一句话

整个 Skill 始终遵循：

> **VLM 负责“看哪里”，Geometry Backend 负责“怎么算”，Platform Backend 负责“点哪里”。**

即：

```text
Visual Perception
      ↓
Relative Coordinate
      ↓
Deterministic Geometry
      ↓
Normalized Monitor Ratio
      ↓
OS-specific Input Mapping
      ↓
Safe Click
```

这是 Pure-Vision Computer Use MVP 的核心边界。
