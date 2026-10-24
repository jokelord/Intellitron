# Cycle UI 四项优化 — 设计规范

**日期**: 2026-04-04
**范围**: 前端 UI 改造 + 后端摄像头过滤

## 背景

Nested Cycle 功能已完整实现（15 个任务全部完成），现需对以下 4 个方面进行优化。

## 优化 (3): UI 改造 — 水平按钮栏

**执行优先级最高，(1) 依赖 (3) 完成后修改。**

### Vision 模式 (`/vision`)

- 删除现有 `ActionPanel.tsx`
- 在 `ImageUploader` 的按钮区域，将原有的两个按钮（上传图片、摄像头拍照）扩展为三个中文按钮：`上传图片` | `摄像头拍照` | `编程循环`
- 三个按钮在同一水平行内，样式统一
- `上传图片`：点击 → 弹出文件选择器（行为不变，无展开区域）
- `摄像头拍照`：点击 → 展开/收起摄像头预览网格（CameraCapture 组件）
- `编程循环`：点击 → 展开/收起 CycleEditor + CycleProgress
- 三个按钮互不干扰，可以同时展开摄像头和循环编辑器

### Text 模式 (`/`)

- 只有一个 `编程循环` 按钮，点击展开/收起 CycleEditor + CycleProgress
- 无上传和摄像头按钮
- 按钮栏样式与 vision 模式保持一致

### 实现策略

- 将 `编程循环` 按钮和展开逻辑直接加入 `ImageUploader.tsx`（vision 模式）
- text 模式页面中直接渲染一个独立的按钮 + 展开区域（不经过 ImageUploader）
- 删除 `ActionPanel.tsx`，其功能分散到各页面

### 页面文件修改

- `frontend/src/app/vision/page.tsx`：移除 ActionPanel 引用，将 cycle props 传给 ImageUploader
- `frontend/src/app/page.tsx`：移除 ActionPanel 引用，直接渲染 cycle 按钮 + CycleEditor
- `frontend/src/components/ImageUploader.tsx`：增加第三个按钮 + cycle 展开区域
- `frontend/src/components/ActionPanel.tsx`：删除

## 优化 (1): Cycle 运行时 UI 折叠解锁

**依赖 (3) 完成后修改。**

### 问题

当 cycle 运行时，展开/折叠按钮被 disabled，与 Send 按钮的 isLoading 状态绑在一起。

### 修复

- `编程循环` 按钮的展开/收起功能在任何时候都可用（包括 cycle 运行中）
- CycleEditor 内部的步骤编辑控件在运行中禁用
- CycleEditor 内部的 Start 按钮在运行中变为 Stop
- 折叠按钮与 `isLoading` / `isCycleRunning` 完全独立

## 优化 (2): 默认配置加载

### 问题

首次打开 cycle UI 时，CycleEditor 不从后端加载默认配置，编辑器为空。

### 修复

- CycleEditor 组件的 `defaultSteps` prop 已经从后端 `/config` 获取并传入
- 问题在 CycleEditor 内部的 localStorage 加载逻辑：首次加载时 localStorage 为空，但 fallback 到 `defaultSteps` 的逻辑可能有 bug（如 `defaultSteps` 为空数组时被视为有效缓存）
- 确保当 localStorage 无数据或数据为空时，正确使用 `defaultSteps` 初始化
- 确保 `defaultSteps` 从后端配置正确解析并传入

## 优化 (4): 摄像头过滤 — V4L2 能力检查

### 问题

`CameraService.list_cameras()` 用 `glob("/dev/video*")` 列举所有 `/dev/videoN`，不区分实际摄像头和 metadata/IR 等非采集设备。

### 修复

- 使用 V4L2 `VIDIOC_QUERYCAP` ioctl（通过 Python `fcntl` + `ctypes`）检查每个设备能力
- 只保留具有 `V4L2_CAP_VIDEO_CAPTURE`（0x00000001）能力的设备
- 返回的 camera 列表重新编号：photo 0 = 第一个有效摄像头，photo 1 = 第二个有效摄像头
- 同时返回原始 `/dev/videoN` 路径和设备名称供参考
- 不需要额外依赖（`fcntl` 和 `ctypes` 是标准库）

### V4L2 VIDIOC_QUERYCAP 实现

```python
import fcntl
import ctypes
import struct

VIDIOC_QUERYCAP = 0x80685600  # _IOR('V', 0, struct v4l2_capability)
V4L2_CAP_VIDEO_CAPTURE = 0x00000001

def _check_v4l2_capture(device_path: str) -> tuple[bool, str]:
    """检查设备是否支持视频采集，返回 (is_capture, device_name)"""
    try:
        fd = os.open(device_path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            buf = bytearray(104)  # sizeof(struct v4l2_capability)
            fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf)
            # driver[16] + card[32] + bus_info[32] + version[4] + capabilities[4] + ...
            card = buf[16:48].split(b'\x00', 1)[0].decode('utf-8', errors='replace')
            capabilities = struct.unpack_from('<I', buf, 84)[0]
            return (capabilities & V4L2_CAP_VIDEO_CAPTURE) != 0, card
        finally:
            os.close(fd)
    except (OSError, IOError):
        return False, ""
```

### 返回格式变更

原来的返回:
```json
{"cameras": [{"index": 0, "device": "/dev/video0"}, {"index": 1, "device": "/dev/video1"}, ...]}
```

新的返回:
```json
{"cameras": [{"index": 0, "device": "/dev/video0", "name": "USB Camera"}, {"index": 1, "device": "/dev/video2", "name": "USB Camera 2"}]}
```

注意 `index` 是重新编号的（0, 1, 2...），`device` 是实际 `/dev/videoN` 路径。

## 执行顺序

1. **(4)** 后端摄像头过滤（独立，不影响前端）
2. **(2)** 默认配置加载修复（独立）
3. **(3)** UI 改造（主要工作量）
4. **(1)** 折叠解锁（在 (3) 基础上修改）

## 受影响文件

### 后端
- `backend/cycle/camera.py` — V4L2 过滤逻辑
- `tests/test_camera_service.py` — 更新测试

### 前端
- `frontend/src/components/ImageUploader.tsx` — 增加第三个按钮 + cycle 展开
- `frontend/src/components/CycleEditor.tsx` — 修复 defaultSteps 加载
- `frontend/src/components/ActionPanel.tsx` — 删除
- `frontend/src/app/vision/page.tsx` — 移除 ActionPanel，传 cycle props 给 ImageUploader
- `frontend/src/app/page.tsx` — 移除 ActionPanel，直接渲染 cycle 按钮
