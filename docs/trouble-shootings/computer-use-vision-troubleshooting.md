# Pure Vision Computer Use：当前状态与故障排查

Wayland Portal/PipeWire/EIS、sealed-frame IPC、NativeClient 与按 Agent context
懒创建的 Python runtime 已接入；X11 使用 XGetImage/XRandR/XTest 独立后端。**真实 Jetson 图形桌面端到端验收仍未完成**。
编译、fixture 测试、Portal 属性可见和 `ACTION_SENT` 都不能证明目标应用收到
真实点击。X11 的真实桌面验收须独立记录。配置类的 capture/input 默认值仍关闭；当前部署 YAML
显式启用了仅针对 archery target 的 operator test mode，release readiness 仍为 false。
这允许真实 Agent 测试，不代表通用桌面输入或发布验收。

当前运行中的旧 backend 不会热加载代码或 session capability。结束原图形登录
terminal 中的 backend 进程后运行 `bash scripts/4_start_backend.sh`，再打开**新**
web Agent context 并在 UI 开启 Thinking。运行原尺寸的
`computer_use_fixture/build/archery-input-target --computer-use-heartbeat`。
该 Qt6 程序仅为本地测试中间产物，保持忽略，不随仓库分发。
仓库附带的 helper 适用于 Linux ARM64，仍需匹配动态库；架构、权限和重新构建安装步骤见
[预编译程序说明](../bin/README.md)。默认 helper 路径为
`bin/linux-aarch64/computer-use-helper`，YAML 相对路径按仓库根目录解析。
重启后的 `/config` 中 `computer_use.operator_archery_test_active=true` 且
`restart_required=false` 才表示测试路径已安装；`release_ready=false` 是预期值。

## 当前边界

| 层 | 当前行为 | 不能据此推断 |
|---|---|---|
| session capability | 未授权时 `desktop_disabled`；combined Portal 需 capture+input | Portal 已被调用或 host 禁止输入 |
| X11 | XRandR primary/sole monitor + XGetImage frame + XTest root coordinates | DISPLAY 或 XTest 返回即证明应用收到点击 |
| Portal | 同一 session 选择 POINTER 和 MONITOR，再 Start 授权 | method 返回成功等于用户授权 |
| PipeWire | 使用 Portal fd/core，校验 buffer 并传 sealed RGB frame | node id 或像素相同证明绑定与活性 |
| EIS/libei | 需可用 absolute region 和可靠 mapping，输入按事务发送 | transport 完成证明 application 点击成功 |
| NativeClient | 认证 IPC、nonce/generation/sequence、fd/seal/hash/geometry 校验 | handshake 成功等于 live ready |
| Python runtime | session-bound helper、renderer、frame store、dispatcher、校准与 teardown | 配置 enabled 自动授予 capability |
| calibration | 重复多目标测量、误差/通过率核验、当前 fingerprint 与 monitor geometry 比对 | 本地 fingerprint 独立证明远端服务/compositor 版本 |

原先发现的 stub 是实现缺口，不是缺少 uinput 的证据。另两个独立 Python
问题也已修复：预 dispatch 曾把缓存 frame 与自己比较、不采集新帧；成功动作
后曾删除 backend state 却只允许 reobserve，造成恢复死路。现在使用 fresh
sample 检查全图/目标区域变化，动作结束后 reobserve，再以新 observe 建新意图。
重复 reobserve 不能复活旧 ledger。首次用于 geometry 校验的帧被首次 observe
消费一次，不会立即等待静态桌面产生第二帧。

## 错误与诊断方向

| 状态 | 含义及下一步 |
|---|---|
| `desktop_disabled` | session 未获 capture/input 权限；尚不能推断 Portal 状态 |
| `TEST_TARGET_UNVERIFIED` / `TEST_TARGET_CLIPPED` / `TEST_TARGET_POINT_OUTSIDE` | operator archery mode 中可继续观察，但本次点按被目标见证、完整 crop 或中心点限制拒绝；`observe` 文本里的 `archery_test_witness` 与黄色形状/完整色环计数区分识别失败和旧帧。不要在同一 detail 上仅换坐标反复 `interact_detail`；重新观察一次，仍失败就停止。旧 marker 遮挡中心时可在 archery 应用里清除 marker 后重新观察。 |
| `INPUT_AUTHORITY_REQUIRED` | combined Portal 需要两种权限；capture-only 不请求 POINTER |
| `BACKEND_BRIDGE_REQUIRED` | 直接 shell 运行 marker；改由受控 Agent executable bridge 调用 |
| `HELPER_NOT_BUILT` / `UNAVAILABLE` | 检查 enabled、绝对 helper 路径和 startup/工具错误中的具体原因 |
| `SESSION_BUS_UNAVAILABLE` | 检查真实图形登录 D-Bus 环境；sandbox 失败不是 host 失败证据 |
| Portal denied/cancelled/timeout | 检查 Request response、用户授权、session close 与 owner 变化 |
| `PIPEWIRE_STREAM_ERROR_NO_INPUT_FORMAT` | Portal 授权已到视频流，但像素格式/尺寸提案没有交集；检查 helper 是否为最新构建与实际 Mutter/PipeWire 格式，不等于 EIS 失败 |
| `PIPEWIRE_FIRST_FRAME_TIMEOUT` | 流未交付可验证首帧；检查 PipeWire 状态、buffer 格式/覆盖及时间戳，不伪造帧活性 |
| `PIPEWIRE_STREAM_NOT_STREAMING` / `PIPEWIRE_FORMAT_NOT_CONFIGURED` | 启动超时前流未进入 streaming，或尚未收到可用的格式；检查 PipeWire 协商，不归咎于 EIS |
| `PIPEWIRE_NO_BUFFER` / `PIPEWIRE_ONLY_EMPTY_BUFFERS` | 流已协商，但没有收到 buffer，或仅收到无完整桌面内容的 buffer；保持 capture 未就绪 |
| `PIPEWIRE_BUFFER_BEFORE_FORMAT` / `PIPEWIRE_FRAME_LOST_ON_FORMAT_CHANGE` / `PIPEWIRE_BUFFER_NOT_PUBLISHED` | buffer 仅在最后一次格式协商前到达、初始帧在重协商时失效，或回调状态不符合预期；检查帧与格式事件次序，不能复用旧帧 |
| `PIPEWIRE_BUFFER_REQUEUE_FAILED` | 已取出的 buffer 无法归还 PipeWire；可能导致流停滞，关闭当前 binding 并检查生产端 |
| `PIPEWIRE_HEADER_MISSING` / `PIPEWIRE_PTS_MISSING` / `PIPEWIRE_PTS_BEFORE_CAPTURE_OPEN` / `PIPEWIRE_PTS_FUTURE` / `PIPEWIRE_PTS_NONADVANCING` | 收到图像但无法证明采样时间在当前会话内且递增；检查生产端时间戳与授权后首帧行为，不以到达时间替代 |
| `EIS_CONNECT_TIMEOUT` / `EIS_SEAT_TIMEOUT` / `EIS_DEVICE_TIMEOUT` | 分别卡在 libei 连接、seat 发布、设备发布；用图形登录终端的授权探针复现 |
| `EIS_DEVICE_RESUME_TIMEOUT` / `EIS_REQUIRED_CAPABILITY_MISSING` | 设备尚不能安全发送 absolute motion/button/scroll；保持输入关闭 |
| `EIS_MAPPING_NOT_FOUND` / `EIS_MAPPING_AMBIGUOUS` | EIS region 与 Portal stream 的 `mapping_id` 无可靠对应；禁止按尺寸、位置猜坐标 |
| `EIS_MAPPING_MULTIPLE_DEVICES_DIFFERENT_REGIONS` / `EIS_MAPPING_MULTIPLE_REGIONS` | 同一个 `mapping_id` 对应不同区域，或同一设备有多个匹配区域；保持输入关闭，检查 EIS 设备/区域公告 |
| `NATIVE_STARTUP_RENDEZVOUS_TIMEOUT` | 截止时 EIS 映射和帧各自可用，但未能在同一个有效状态中提交 binding；检查授权期间的设备/格式变化 |
| mapping/region/input unavailable | 需可靠 Portal stream/EIS region 对应，不能按截图宽高猜坐标 |
| `CALIBRATION_REQUIRED` | report、测量、fingerprint 或所选 monitor geometry 不符，重新核验 |
| `STALE_FRAME` / `FRAME_CHANGED` / `TARGET_CHANGED` | 旧图不可点击；重新 observe |
| `CAPTURE_STALLED` / `INDETERMINATE` | 无法证明新 sample 活性，拒绝发送 |
| `GEOMETRY_CHANGED` | 分辨率/scale/rotation/stream 变化，旧 binding/crop 无效 |
| `ACTION_DELIVERY_UNKNOWN` | 部分事件可能已发送；不重试旧动作，先观察结果 |
| `DESKTOP_SESSION_RESET_REQUIRED` | helper 已结束，销毁 Agent context 后新建；无隐式重新授权 |
| protocol/fd/credential error | nonce、generation、sequence、身份或 frame 校验失败，关闭 binding |

## 排查顺序

### 1. 真实图形登录环境

在 Jetson 图形登录用户的 terminal 执行，而不是仅在 coding sandbox 中执行：

```bash
printf 'XDG_SESSION_TYPE=%s\n' "$XDG_SESSION_TYPE"
printf 'WAYLAND_DISPLAY=%s\n' "$WAYLAND_DISPLAY"
printf 'XDG_RUNTIME_DIR=%s\n' "$XDG_RUNTIME_DIR"
printf 'DBUS_SESSION_BUS_ADDRESS=%s\n' "$DBUS_SESSION_BUS_ADDRESS"
loginctl session-status
systemctl --user --no-pager --full status xdg-desktop-portal pipewire wireplumber
```

不要把 root/system service 当作普通用户的 Portal client。EnvironmentBuilder
已允许必要的图形 session 变量；不要用继承整个 server 环境代替受控环境。

### 2. Portal 接口与授权（仅 Wayland）

```bash
busctl --user introspect org.freedesktop.portal.Desktop \
  /org/freedesktop/portal/desktop org.freedesktop.portal.RemoteDesktop
busctl --user introspect org.freedesktop.portal.Desktop \
  /org/freedesktop/portal/desktop org.freedesktop.portal.ScreenCast
```

检查 RemoteDesktop 版本与 POINTER、ScreenCast MONITOR 支持。正确 live 顺序：
CreateSession → SelectDevices(POINTER) → SelectSources(MONITOR, 同 session) →
RemoteDesktop.Start → OpenPipeWireRemote → ConnectToEIS。用户 Request response
才是授权结果。EIS 建立后不混用 NotifyPointer。

mapping_id 和 pipewire-serial 是版本相关的可选 metadata。优先使用可用 serial，
缺少可靠映射时拒绝 input；不能把 node id、截图宽高或 Portal position 直接当
EIS region。用户取消、超时、Session.Closed、Portal owner 更换均需关闭旧绑定。
若多台已恢复的虚拟设备同时声明同一 `mapping_id`、相同完整区域以及
absolute/button/scroll 能力，可固定使用其中一台；发布 binding 后所选设备
失效或任一候选区域分歧时立即撤销，不自动切换设备。

### 3. 构建与只读诊断

按 [运维文档](computer-use-vision-operations.md) 构建、运行 CTest、helper
`--probe` 和 doctor。包发现、Portal 接口、live frame、真实 input outcome 和
calibration 是独立证据。`SO_PASSCRED` skip 必须在 host 重测，不能记为通过。
Doctor 的 `not_run` 表示没有执行对应 live 检查。

### 4. 配置与校准

设置绝对 helper/report 路径，保持默认 capability 不变，只对受控 top-level
Agent 授权。readiness 要求六个实测数值阈值加一个 fingerprint，不是七个数值。
不能填任意常数或伪造 `passed:true`。

初次模型校准可以运行 `python -m scripts.calibrate_computer_use
--generate-fixtures <目录>`，用 production renderer 生成离线已知目标 overview/
detail 与 stage prompts。通过普通 Vision upload 在同一 top-level Agent 中
收集重复输出，保持 desktop capability 关闭，不给模型看 ground-truth manifest。
按运维文档转换/评分真实输出；这不需要先伪造 calibration report，也不放开
未校准输入。普通上传与 production attachment 的有效 preprocessing 必须一致。
离线 fixture 不证明 live monitor geometry 或输入结果。

当前 live `inspect_region` 固定要求 `zoom=4`；prompt schema、decoder 和
executable parser 对其他值返回 `INVALID_ZOOM`。4× 表示 nominal crop 每轴取
`250/1000` 视野，不代表模型有四倍更大的误差余量。校准 `max_axis_error` 衡量
monitor-normalized 的目标中心误差，不包含目标边界或可点击区域大小。Stage-2
方形拉伸只改变各轴像素比例，不能证明点命中小图标；没有每个样本的真实目标
边界与 hit/miss 证据时，不应按“使用 4×”放宽全局阈值。当前 live pure-vision
调用没有可信 target bounds，因此离线按尺寸分桶只能用于衡量模型，不能授权
未知尺寸的 live 点击。

当前 v2.0 路径先把完整 monitor overview 沿双轴拉伸成较长边正方形，再叠加
八等分 Stage-1 网格并无损编码；Stage-1 rough point 选择固定 4× 的真实整数 source
crop。crop 在边缘精确裁剪、无 padding，Stage-2 也将其 X/Y 独立拉伸为
`S×S` model-facing bitmap；source frame 和 actual integer crop rect 不变。
3840×2400 monitor 上中心 4× crop 为 960×600，rough point 在上边缘时
可裁为 960×300；二者的 Stage-2 图均为 `S×S`，但后端按各自真实 crop
宽、高分别 inverse map。两阶段 X/Y 均独立使用归一化 0..1000。
Stage-2 网格每轴十六等分，仅偶数内线标注 125、250、…、875；Stage-1
八等分网格标注全部七条内线。`computer_use.grid_label_placement` 默认是
`edge`：X 标签放在竖线与图像上、下边缘的交点，Y 标签放在横线与左、右
边缘的交点。旧布局 `repeated` 会将每个 X/Y 标签分别重复到该轴所有格段
（Stage-1 每值八份，Stage-2 每值十六份）。两种布局的 X 标签逆时针旋转
90°，Y 标签保持水平；线和标签以 0.5 opacity 合成。布局会进入 calibration
fingerprint，切换布局后旧报告失效，需重新校准。
Gridless、10-grid 和旧等比输入仅供历史对照，不能作为当前 renderer 的
校准证据。

当前 fingerprint 覆盖本地配置、代码、processor/tokenizer 版本、模型 metadata
hash 和 weight 文件身份（路径/大小/mtime）。它不是 weight 全内容 hash，也
不能独立证明实际远端/container 推理服务或 compositor build。部署时另行核验
这些版本，变化后重新测量。runtime 授权后另外比对 report 的 EIS 或 XRandR monitor logical
origin/size 与 full-monitor pixel size，geometry 改变后拒绝旧 crop。

### 5. 同 Agent 两阶段与失效路径

`observe → inspect_region → interact_detail` 的两个坐标决策必须来自同一
Agent。crop/render 在 Python；native 只负责 capture/input。detail 绑定原始
immutable frame、binding 和 geometry。dispatch 前获取新的 current sample，
校验全图与 target region、活性与年龄、validation-to-first-event 时间。
后续事件和 cleanup 也有有界 transaction deadline，多个 context 共用 seat 锁。

动作结束后先 reobserve，再新 observe 明确新的 target/action；不自动重放旧
request。helper 结束时销毁旧 Agent context 后重新开始。Stop、helper crash、
Portal revoke 或 EIS 中断不能把不确定状态当成功。保留原有 screenshot provenance
和普通 Agent inference acceptance barrier，不修改用户上传图或其他上下文。

## Live 验收

必须完成：受控 capability、Portal 用户授权、完整 monitor frame、可信 EIS
mapping、authenticated sealed-frame IPC、当前校准、真实同 Agent click/scroll
目标效果，以及 resolution/scale/hotplug、Portal revoke/restart、stream
recreation、capture stall、EIS 断连、Stop、helper crash 的 fail-closed 测试。
`ACTION_SENT` 只表示 transport 提交结果；libei 没有 application 物理点击 ACK。
acceptance manifest 不会把未运行测试标为通过，Wayland 不要求 X11 验收。
日志记录必要的身份/尺寸/hash/时序/错误码，避免保存私密桌面内容。

无需为了 Portal/EIS 默认添加 `/dev/uinput`、input group 或 CAP_SYS_ADMIN。
Sunshine X11/uinput/NVENC/EGL 的问题与本项目 Wayland 输入没有直接因果关系。


## X11 独立诊断

明确 XDG_SESSION_TYPE=x11 选择 X11；wayland 即使有 XWayland DISPLAY
也不切换后端。没有 session type 时 Wayland 环境优先。禁止因 Portal 失败而
fallback 到 XWayland。X11 需要真实 X 连接、XRandR primary（或唯一 monitor）
与 XTest 扩展；多 monitor 无 primary 时 fail closed。不要手写替换图形环境变量。

Doctor 的依赖按后端报告：X11 使用 json-glib 与 x11/xext/xtst/xrandr，不依赖 Wayland 开发包；
Wayland 使用 gio-unix/json-glib 与 PipeWire/SPA/libei。X11 probes 的 Portal/EIS
标为 not_applicable，分别报告 x11_connection、xrandr_mapping、xtest_input。
没有发送输入的 probe 不能宣称 XTest outcome 已验证。X11_* / XRANDR_* /
XTEST_* 错误分别检查 X 连接/截图、monitor 选择与拓扑、XTest 扩展与输入。

截图实现是 XGetImage，不使用 MIT-SHM/XShm。monitor/root/output 身份和
origin/size 变化使旧 binding/crop 失效；坐标包含 monitor root origin，不能只按
截图宽高推断绝对位置。X11 验收覆盖真实 frame、应用 button/scroll callbacks、
monitor/root 变化、X 连接中断、Stop 与 helper crash。受控测试结果不能替代
模型校准或通用桌面输入验收。
