# Cycle UI 四项优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Optimize the Cycle feature across 4 areas: V4L2 camera filtering, default config loading, toolbar UI redesign, and collapse-during-run fix.

**Architecture:** Backend camera.py gets V4L2 ioctl filtering. Backend `/config` endpoint adds `default_sequence` serialization. Frontend replaces ActionPanel with inline toolbar buttons in both pages. ImageUploader gets a third "编程循环" button for vision mode.

**Tech Stack:** Python (fcntl/ctypes for V4L2), FastAPI, Next.js 14, React, TypeScript, Tailwind CSS

---

### Task 1: V4L2 Camera Filtering — Tests

**Files:**
- Modify: `tests/test_camera_service.py`

- [ ] **Step 1: Write failing tests for V4L2 filtering**

Add new test class to `tests/test_camera_service.py`:

```python
# Add to imports at top:
# (existing: import base64, pytest, from unittest.mock import patch, MagicMock, import numpy as np)

class TestListCamerasV4L2:
    """测试 V4L2 能力过滤"""

    @patch("backend.cycle.camera.os.open")
    @patch("backend.cycle.camera.os.close")
    @patch("backend.cycle.camera.fcntl.ioctl")
    @patch("backend.cycle.camera.glob.glob")
    def test_filters_non_capture_devices(self, mock_glob, mock_ioctl, mock_close, mock_open):
        """只保留具有 VIDEO_CAPTURE 能力的设备"""
        mock_glob.return_value = ["/dev/video0", "/dev/video1", "/dev/video2", "/dev/video3"]
        mock_open.return_value = 5  # fake fd

        def ioctl_side_effect(fd, request, buf):
            # Build fake v4l2_capability struct
            # card at offset 16, capabilities at offset 84
            device_path = mock_open.call_args_list[len(mock_ioctl.call_args_list) - 1][0][0]
            if device_path in ("/dev/video0", "/dev/video2"):
                # Capture-capable: set V4L2_CAP_VIDEO_CAPTURE (0x1) at offset 84
                import struct
                struct.pack_into("<I", buf, 84, 0x85200001)  # capabilities with VIDEO_CAPTURE
                buf[16:32] = b"USB Camera\x00\x00\x00\x00\x00\x00"
            else:
                # Metadata only: no VIDEO_CAPTURE
                import struct
                struct.pack_into("<I", buf, 84, 0x84200000)
                buf[16:32] = b"USB Meta\x00\x00\x00\x00\x00\x00\x00\x00"

        mock_ioctl.side_effect = ioctl_side_effect

        service = CameraService()
        cameras = service.list_cameras()

        assert len(cameras) == 2
        assert cameras[0]["index"] == 0
        assert cameras[0]["device"] == "/dev/video0"
        assert cameras[0]["name"] == "USB Camera"
        assert cameras[1]["index"] == 1
        assert cameras[1]["device"] == "/dev/video2"

    @patch("backend.cycle.camera.os.open")
    @patch("backend.cycle.camera.glob.glob")
    def test_handles_ioctl_failure(self, mock_glob, mock_open):
        """设备打开失败时跳过该设备"""
        mock_glob.return_value = ["/dev/video0", "/dev/video1"]
        mock_open.side_effect = OSError("Permission denied")

        service = CameraService()
        cameras = service.list_cameras()
        assert cameras == []

    @patch("backend.cycle.camera.os.open")
    @patch("backend.cycle.camera.os.close")
    @patch("backend.cycle.camera.fcntl.ioctl")
    @patch("backend.cycle.camera.glob.glob")
    def test_reindexes_cameras(self, mock_glob, mock_ioctl, mock_close, mock_open):
        """摄像头重新编号：index 是连续的 0,1,2..."""
        mock_glob.return_value = ["/dev/video0", "/dev/video2", "/dev/video4"]
        mock_open.return_value = 5

        def ioctl_side_effect(fd, request, buf):
            import struct
            struct.pack_into("<I", buf, 84, 0x00000001)  # VIDEO_CAPTURE
            buf[16:28] = b"Camera\x00\x00\x00\x00\x00\x00"

        mock_ioctl.side_effect = ioctl_side_effect

        service = CameraService()
        cameras = service.list_cameras()

        assert len(cameras) == 3
        assert cameras[0]["index"] == 0
        assert cameras[0]["device"] == "/dev/video0"
        assert cameras[1]["index"] == 1
        assert cameras[1]["device"] == "/dev/video2"
        assert cameras[2]["index"] == 2
        assert cameras[2]["device"] == "/dev/video4"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/zk/Project/LLM-deployment && .sglang/bin/python -m pytest tests/test_camera_service.py::TestListCamerasV4L2 -v`

Expected: FAIL — `backend.cycle.camera` has no `os`, `fcntl` attributes

---

### Task 2: V4L2 Camera Filtering — Implementation

**Files:**
- Modify: `backend/cycle/camera.py`

- [ ] **Step 1: Implement V4L2 filtering in CameraService**

Replace the entire `backend/cycle/camera.py` with:

```python
"""
摄像头服务

通过 OpenCV 直接访问 /dev/videoN 设备进行拍照。
使用 V4L2 ioctl 过滤出真正的视频采集设备（排除 metadata 节点等）。
"""

import base64
import fcntl
import glob
import logging
import os
import re
import struct

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# V4L2 constants
VIDIOC_QUERYCAP = 0x80685600  # _IOR('V', 0, struct v4l2_capability)
V4L2_CAP_VIDEO_CAPTURE = 0x00000001


def _check_v4l2_capture(device_path: str) -> tuple[bool, str]:
    """
    检查 /dev/videoN 设备是否支持视频采集。

    通过 V4L2 VIDIOC_QUERYCAP ioctl 读取设备能力标志，
    只有具有 V4L2_CAP_VIDEO_CAPTURE 能力的设备才是真正的摄像头。

    Returns:
        (is_capture_capable, device_name)
    """
    try:
        fd = os.open(device_path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            buf = bytearray(104)  # sizeof(struct v4l2_capability)
            fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf)
            # struct v4l2_capability layout:
            #   driver[16] + card[32] + bus_info[32] + version[4] + capabilities[4] + ...
            card = buf[16:48].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
            capabilities = struct.unpack_from("<I", buf, 84)[0]
            return (capabilities & V4L2_CAP_VIDEO_CAPTURE) != 0, card
        finally:
            os.close(fd)
    except (OSError, IOError):
        return False, ""


class CameraService:
    """摄像头管理与拍照服务"""

    def list_cameras(self) -> list[dict]:
        """
        枚举可用摄像头设备。

        使用 V4L2 VIDIOC_QUERYCAP 检查每个 /dev/videoN 设备，
        只返回具有 VIDEO_CAPTURE 能力的真正摄像头。
        返回列表中 index 从 0 开始连续编号。
        """
        devices = sorted(glob.glob("/dev/video*"))
        cameras = []
        for dev in devices:
            match = re.search(r"/dev/video(\d+)$", dev)
            if not match:
                continue
            is_capture, name = _check_v4l2_capture(dev)
            if is_capture:
                cameras.append(
                    {
                        "index": len(cameras),  # 连续编号
                        "device": dev,
                        "name": name,
                    }
                )
        return cameras

    def _resolve_device(self, index: int) -> str | None:
        """将连续编号的 index 解析为实际 /dev/videoN 设备路径"""
        cameras = self.list_cameras()
        for cam in cameras:
            if cam["index"] == index:
                return cam["device"]
        return None

    def capture(self, indices: list[int]) -> list[dict]:
        """
        从指定摄像头拍照

        Args:
            indices: 摄像头索引列表（连续编号），如 [0, 1]

        Returns:
            list[dict]: 每个元素包含 camera_index + image_base64 或 error
        """
        results = []
        for idx in indices:
            result = self._capture_one(idx)
            results.append(result)
        return results

    def _capture_one(self, index: int) -> dict:
        """从单个摄像头拍一张照片"""
        device = self._resolve_device(index)
        if device is None:
            return {
                "camera_index": index,
                "error": f"摄像头 {index} 不存在��不可用",
            }

        cap = None
        try:
            cap = cv2.VideoCapture(device)
            if not cap.isOpened():
                return {
                    "camera_index": index,
                    "error": f"无法打开摄像头 {index} ({device})",
                }

            ret, frame = cap.read()
            if not ret or frame is None:
                return {
                    "camera_index": index,
                    "error": f"摄像头 {index} ({device}) 读取失败",
                }

            success, buffer = cv2.imencode(".jpg", frame)
            if not success:
                return {
                    "camera_index": index,
                    "error": f"摄像头 {index} JPEG 编码失败",
                }

            image_base64 = base64.b64encode(buffer.tobytes()).decode("utf-8")
            return {
                "camera_index": index,
                "image_base64": f"data:image/jpeg;base64,{image_base64}",
            }

        except Exception as e:
            logger.error(f"摄像头 {index} 拍照异常: {e}")
            return {
                "camera_index": index,
                "error": str(e),
            }
        finally:
            if cap is not None:
                cap.release()
```

- [ ] **Step 2: Update existing tests to match new API**

In `tests/test_camera_service.py`, update the `TestListCameras` class. The old tests mocked `glob.glob` but now need V4L2 mocks too. Replace the entire `TestListCameras` class:

```python
class TestListCameras:
    """测试摄像头枚举"""

    @patch("backend.cycle.camera._check_v4l2_capture")
    @patch("backend.cycle.camera.glob.glob")
    def test_list_cameras_found(self, mock_glob, mock_v4l2):
        mock_glob.return_value = ["/dev/video0", "/dev/video1"]
        mock_v4l2.return_value = (True, "Camera")
        service = CameraService()
        cameras = service.list_cameras()
        assert len(cameras) == 2
        assert cameras[0]["index"] == 0
        assert cameras[1]["index"] == 1

    @patch("backend.cycle.camera.glob.glob")
    def test_list_cameras_empty(self, mock_glob):
        mock_glob.return_value = []
        service = CameraService()
        cameras = service.list_cameras()
        assert cameras == []

    @patch("backend.cycle.camera._check_v4l2_capture")
    @patch("backend.cycle.camera.glob.glob")
    def test_list_cameras_filters_non_numeric(self, mock_glob, mock_v4l2):
        mock_glob.return_value = ["/dev/video0", "/dev/video1", "/dev/videoX"]
        mock_v4l2.return_value = (True, "Camera")
        service = CameraService()
        cameras = service.list_cameras()
        assert len(cameras) == 2
```

Also update `TestCapture` to mock `_resolve_device`:

```python
class TestCapture:
    """测试摄像头拍照"""

    @patch.object(CameraService, "_resolve_device", return_value="/dev/video0")
    @patch("backend.cycle.camera.cv2")
    def test_capture_single(self, mock_cv2, mock_resolve):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        fake_frame = np.zeros((100, 100, 3), dtype=np.uint8)
        mock_cap.read.return_value = (True, fake_frame)
        mock_cv2.VideoCapture.return_value = mock_cap
        mock_cv2.imencode.return_value = (True, np.array([0xFF, 0xD8], dtype=np.uint8))

        service = CameraService()
        results = service.capture([0])

        assert len(results) == 1
        assert "image_base64" in results[0]
        assert results[0]["camera_index"] == 0
        mock_cap.release.assert_called_once()

    @patch.object(CameraService, "_resolve_device", return_value="/dev/video0")
    @patch("backend.cycle.camera.cv2")
    def test_capture_multiple(self, mock_cv2, mock_resolve):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        fake_frame = np.zeros((100, 100, 3), dtype=np.uint8)
        mock_cap.read.return_value = (True, fake_frame)
        mock_cv2.VideoCapture.return_value = mock_cap
        mock_cv2.imencode.return_value = (True, np.array([0xFF, 0xD8], dtype=np.uint8))

        service = CameraService()
        results = service.capture([0, 1])
        assert len(results) == 2

    @patch.object(CameraService, "_resolve_device", return_value="/dev/video0")
    @patch("backend.cycle.camera.cv2")
    def test_capture_failed_open(self, mock_cv2, mock_resolve):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = False
        mock_cv2.VideoCapture.return_value = mock_cap

        service = CameraService()
        results = service.capture([0])
        assert len(results) == 1
        assert "error" in results[0]

    @patch.object(CameraService, "_resolve_device", return_value="/dev/video0")
    @patch("backend.cycle.camera.cv2")
    def test_capture_failed_read(self, mock_cv2, mock_resolve):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.read.return_value = (False, None)
        mock_cv2.VideoCapture.return_value = mock_cap

        service = CameraService()
        results = service.capture([0])
        assert len(results) == 1
        assert "error" in results[0]

    @patch.object(CameraService, "_resolve_device", return_value="/dev/video0")
    @patch("backend.cycle.camera.cv2")
    def test_capture_base64_format(self, mock_cv2, mock_resolve):
        """验证返回的 base64 格式正确"""
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        fake_frame = np.zeros((100, 100, 3), dtype=np.uint8)
        mock_cap.read.return_value = (True, fake_frame)
        mock_cv2.VideoCapture.return_value = mock_cap
        mock_cv2.imencode.return_value = (True, np.array([0xFF, 0xD8], dtype=np.uint8))

        service = CameraService()
        results = service.capture([0])
        assert results[0]["image_base64"].startswith("data:image/jpeg;base64,")

    @patch.object(CameraService, "_resolve_device", return_value=None)
    def test_capture_unknown_index(self, mock_resolve):
        """不存在的摄像头 index 返回错误"""
        service = CameraService()
        results = service.capture([99])
        assert len(results) == 1
        assert "error" in results[0]
```

- [ ] **Step 3: Run all camera tests**

Run: `cd /home/zk/Project/LLM-deployment && .sglang/bin/python -m pytest tests/test_camera_service.py -v`

Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add backend/cycle/camera.py tests/test_camera_service.py
git commit -m "feat: add V4L2 capability filtering to CameraService"
```

---

### Task 3: Backend — Add default_sequence to /config Response

**Files:**
- Modify: `backend/main.py:277-281`

- [ ] **Step 1: Add default_sequence serialization to /config endpoint**

In `backend/main.py`, the `get_current_config()` function's `"cycle"` section (line 277-281) needs to include `default_sequence`. Add a helper function and update the cycle response.

Add this helper function before `get_current_config()` (around line 228):

```python
def _serialize_cycle_steps(steps) -> list[dict]:
    """将 CycleStepConfig 列表序列化为前端可用的格式"""
    result = []
    for step in steps:
        if step.step_type == "wait":
            result.append({"type": "wait", "wait": step.wait_seconds})
        elif step.step_type == "message":
            item: dict = {"type": "message", "message": step.message}
            if step.photo:
                item["photo"] = step.photo
            result.append(item)
        elif step.step_type == "cycle":
            result.append({
                "type": "cycle",
                "cycle_count": step.cycle_count,
                "cycle_steps": _serialize_cycle_steps(step.cycle_steps),
            })
    return result
```

Then update the cycle section in the response dict (line 277-281) from:

```python
        "cycle": {
            "enabled": config.cycle.enabled,
            "max_depth": config.cycle.max_depth,
            "max_duration": config.cycle.max_duration,
        },
```

to:

```python
        "cycle": {
            "enabled": config.cycle.enabled,
            "max_depth": config.cycle.max_depth,
            "max_duration": config.cycle.max_duration,
            "default_sequence": _serialize_cycle_steps(config.cycle.default_sequence),
        },
```

- [ ] **Step 2: Run existing tests to ensure nothing breaks**

Run: `cd /home/zk/Project/LLM-deployment && .sglang/bin/python -m pytest tests/ -v`

Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add backend/main.py
git commit -m "feat: include default_sequence in /config cycle response"
```

---

### Task 4: Frontend — Update cycle-types.ts Camera interface

**Files:**
- Modify: `frontend/src/lib/cycle-types.ts:37-40`

- [ ] **Step 1: Add `name` field to Camera interface**

In `frontend/src/lib/cycle-types.ts`, update the `Camera` interface (lines 37-40) from:

```typescript
export interface Camera {
  index: number;
  device: string;
}
```

to:

```typescript
export interface Camera {
  index: number;
  device: string;
  name?: string;
}
```

- [ ] **Step 2: Verify TypeScript compilation**

Run: `cd /home/zk/Project/LLM-deployment/frontend && npx tsc --noEmit`

Expected: 0 errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/cycle-types.ts
git commit -m "feat: add name field to Camera type for V4L2 device names"
```

---

### Task 5: Frontend — Remove ActionPanel, Add Cycle Button to ImageUploader (Vision Mode)

**Files:**
- Modify: `frontend/src/components/ImageUploader.tsx`
- Delete: `frontend/src/components/ActionPanel.tsx`

- [ ] **Step 1: Add cycle props and state to ImageUploader**

In `frontend/src/components/ImageUploader.tsx`, update the imports, interface, and component:

First, update imports (line 10-14) — add CycleEditor, CycleProgress, and cycle types:

```typescript
import React, { useRef, useState, useCallback } from 'react';
import { Upload, Image as ImageIcon, Camera, X, AlertCircle, Loader2, RefreshCw } from 'lucide-react';
import ImagePreview from './ImagePreview';
import CameraCapture from './CameraCapture';
import CycleEditor from './CycleEditor';
import CycleProgress from './CycleProgress';
import { llmClient } from '@/lib/api';
import {
  CycleStep,
  CycleSSEEvent,
  CycleExecutionStatus,
} from '@/lib/cycle-types';
```

Then update the props interface (lines 16-23):

```typescript
interface ImageUploaderProps {
  onImagesChange: (images: string[] | ((prev: string[]) => string[])) => void;
  images?: string[];
  maxImages?: number;
  maxFileSize?: number;
  acceptedFormats?: string[];
  cameraDimension?: number;
  // Cycle props
  cycleEnabled?: boolean;
  cycleMaxDepth?: number;
  cycleDefaultSteps?: CycleStep[];
  cycleExecutionStatus?: CycleExecutionStatus;
  cycleEvents?: CycleSSEEvent[];
  onCycleStart?: (steps: CycleStep[], shareContext: boolean) => void;
  onCycleStop?: () => void;
}
```

Update the component function signature to destructure the new props:

```typescript
export default function ImageUploader({
  onImagesChange,
  images: externalImages,
  maxImages = 9,
  maxFileSize = 10 * 1024 * 1024,
  acceptedFormats = ['png', 'jpg', 'jpeg', 'gif', 'webp'],
  cameraDimension = 768,
  cycleEnabled = false,
  cycleMaxDepth = 5,
  cycleDefaultSteps = [],
  cycleExecutionStatus = 'idle',
  cycleEvents = [],
  onCycleStart,
  onCycleStop,
}: ImageUploaderProps) {
```

Add cycle expand state after existing `showCamera` state (after line 57):

```typescript
  const [showCycle, setShowCycle] = useState(false);
```

- [ ] **Step 2: Add the third button and cycle expand area to the JSX**

Replace the button area (lines 202-236, the `{canAddMore && (...)}` block) with a version that always shows and includes three buttons:

```typescript
      {/* 工具按钮栏 */}
      <div className="flex gap-2">
        {/* 文件选择按钮 */}
        {canAddMore && (
          <button
            onClick={() => fileInputRef.current?.click()}
            className="flex-1 flex items-center justify-center gap-2 
              px-4 py-2 border-2 border-dashed border-gray-300 dark:border-gray-600 
              rounded-lg hover:border-nvidia-500 hover:bg-gray-50 dark:hover:bg-gray-800 
              transition-colors"
          >
            <ImageIcon className="w-5 h-5 text-gray-500" />
            <span className="text-sm text-gray-600 dark:text-gray-400">
              上传图片
            </span>
            <Upload className="w-4 h-4 text-gray-400" />
          </button>
        )}

        {/* 摄像头按钮 */}
        {canAddMore && (
          <button
            onClick={() => setShowCamera(!showCamera)}
            className={`flex-1 flex items-center justify-center gap-2 
              px-4 py-2 border-2 border-dashed rounded-lg transition-colors
              ${showCamera
                ? 'border-nvidia-500 bg-nvidia-500/10'
                : 'border-gray-300 dark:border-gray-600 hover:border-nvidia-500 hover:bg-gray-50 dark:hover:bg-gray-800'
              }`}
          >
            <Camera className={`w-5 h-5 ${showCamera ? 'text-nvidia-500' : 'text-gray-500'}`} />
            <span className={`text-sm ${showCamera ? 'text-nvidia-500' : 'text-gray-600 dark:text-gray-400'}`}>
              {showCamera ? '关闭摄像头' : '摄像头拍照'}
            </span>
          </button>
        )}

        {/* 编程循环按钮 */}
        {cycleEnabled && (
          <button
            onClick={() => setShowCycle(!showCycle)}
            className={`flex-1 flex items-center justify-center gap-2 
              px-4 py-2 border-2 border-dashed rounded-lg transition-colors
              ${showCycle
                ? 'border-nvidia-500 bg-nvidia-500/10'
                : cycleExecutionStatus === 'running'
                  ? 'border-blue-500 bg-blue-500/10'
                  : 'border-gray-300 dark:border-gray-600 hover:border-nvidia-500 hover:bg-gray-50 dark:hover:bg-gray-800'
              }`}
          >
            <RefreshCw className={`w-5 h-5 ${
              showCycle ? 'text-nvidia-500' 
              : cycleExecutionStatus === 'running' ? 'text-blue-400 animate-spin' 
              : 'text-gray-500'
            }`} />
            <span className={`text-sm ${
              showCycle ? 'text-nvidia-500' 
              : cycleExecutionStatus === 'running' ? 'text-blue-400'
              : 'text-gray-600 dark:text-gray-400'
            }`}>
              {cycleExecutionStatus === 'running' ? '循环运行中' : '编程循环'}
            </span>
          </button>
        )}
      </div>
```

After the camera capture area (after line 258, the `{showCamera && canAddMore && (...)}` block), add the cycle expand area:

```typescript
      {/* 编程循环编辑区域 */}
      {showCycle && cycleEnabled && (
        <div className="mt-2 bg-gray-800/30 border border-gray-700/50 rounded-lg p-3 space-y-3">
          <CycleEditor
            isVisionMode={true}
            maxDepth={cycleMaxDepth}
            defaultSteps={cycleDefaultSteps}
            executionStatus={cycleExecutionStatus}
            onStart={onCycleStart || (() => {})}
            onStop={onCycleStop || (() => {})}
          />
          {cycleExecutionStatus !== 'idle' && (
            <CycleProgress status={cycleExecutionStatus} events={cycleEvents} />
          )}
        </div>
      )}
```

- [ ] **Step 3: Delete ActionPanel.tsx**

Delete the file: `frontend/src/components/ActionPanel.tsx`

- [ ] **Step 4: Verify TypeScript compilation**

Run: `cd /home/zk/Project/LLM-deployment/frontend && npx tsc --noEmit`

Expected: Errors in `page.tsx` and `vision/page.tsx` (they still import ActionPanel) — that's OK, we'll fix them in next tasks.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ImageUploader.tsx
git rm frontend/src/components/ActionPanel.tsx
git commit -m "feat: add cycle button to ImageUploader, remove ActionPanel"
```

---

### Task 6: Frontend — Update Vision Page to Use New ImageUploader

**Files:**
- Modify: `frontend/src/app/vision/page.tsx`

- [ ] **Step 1: Remove ActionPanel import and usage, pass cycle props to ImageUploader**

In `frontend/src/app/vision/page.tsx`:

1. Remove the ActionPanel import (line 20):
   Remove: `import ActionPanel from '@/components/ActionPanel';`

2. Remove the ActionPanel usage in the footer (lines 1168-1178):
   Remove this entire block:
   ```tsx
           {/* Cycle 编辑器面板 */}
           {cycleConfig.enabled && (
             <ActionPanel
               isVisionMode={true}
               maxDepth={cycleConfig.max_depth}
               defaultSteps={cycleConfig.default_sequence}
               executionStatus={cycleStatus}
               onStart={handleCycleStart}
               onStop={handleCycleStop}
             />
           )}
   ```

3. Update the ImageUploader component (lines 1181-1189) to pass cycle props:
   ```tsx
           <ImageUploader
             key={`uploader-${imagesKey}`}
             images={images}
             onImagesChange={handleImagesChange}
             maxImages={9}
             maxFileSize={visionConfig?.maxImageSize || 10 * 1024 * 1024}
             acceptedFormats={visionConfig?.supportedFormats || ['png', 'jpg', 'jpeg', 'gif', 'webp']}
             cameraDimension={visionConfig?.cameraDimension || 768}
             cycleEnabled={cycleConfig.enabled}
             cycleMaxDepth={cycleConfig.max_depth}
             cycleDefaultSteps={cycleConfig.default_sequence}
             cycleExecutionStatus={cycleStatus}
             cycleEvents={cycleEvents}
             onCycleStart={handleCycleStart}
             onCycleStop={handleCycleStop}
           />
   ```

4. Remove the CycleProgress rendering in main area (lines 1159-1161):
   Remove:
   ```tsx
           {cycleStatus !== 'idle' && (
             <CycleProgress status={cycleStatus} events={cycleEvents} />
           )}
   ```
   (CycleProgress is now shown inside ImageUploader's cycle expand area)

5. Remove unused CycleProgress import (line 21):
   Remove: `import CycleProgress from '@/components/CycleProgress';`

- [ ] **Step 2: Verify TypeScript compilation**

Run: `cd /home/zk/Project/LLM-deployment/frontend && npx tsc --noEmit`

Expected: Errors only from `page.tsx` (still uses ActionPanel) — OK for now

- [ ] **Step 3: Commit**

```bash
git add frontend/src/app/vision/page.tsx
git commit -m "feat: vision page uses ImageUploader toolbar instead of ActionPanel"
```

---

### Task 7: Frontend — Update Text Page with Inline Cycle Button

**Files:**
- Modify: `frontend/src/app/page.tsx`

- [ ] **Step 1: Replace ActionPanel with inline cycle button and editor**

In `frontend/src/app/page.tsx`:

1. Remove ActionPanel import (line 13):
   Remove: `import ActionPanel from '@/components/ActionPanel';`

2. Add CycleEditor import:
   Add: `import CycleEditor from '@/components/CycleEditor';`

3. Keep CycleProgress import (line 14) — it's used.

4. Add `RefreshCw` to lucide imports (line 5):
   Change to: `import { Send, Trash2, Settings, Bot, User, Loader2, Zap, StopCircle, RefreshCw } from 'lucide-react';`

5. Add cycle expand state. Find the cycle state block (around lines 190-199) and add after the cycleConfig state:

   ```typescript
   const [showCycle, setShowCycle] = useState(false);
   ```

6. Replace the ActionPanel block in the footer (lines 1187-1197) with the inline cycle button and expand area:

   ```tsx
           {/* 编程循环按钮 */}
           {cycleConfig.enabled && (
             <div>
               <div className="flex gap-2">
                 <button
                   onClick={() => setShowCycle(!showCycle)}
                   className={`flex-1 flex items-center justify-center gap-2 
                     px-4 py-2 border-2 border-dashed rounded-lg transition-colors
                     ${showCycle
                       ? 'border-nvidia-500 bg-nvidia-500/10'
                       : cycleStatus === 'running'
                         ? 'border-blue-500 bg-blue-500/10'
                         : 'border-gray-300 dark:border-gray-600 hover:border-nvidia-500 hover:bg-gray-50 dark:hover:bg-gray-800'
                     }`}
                 >
                   <RefreshCw className={`w-5 h-5 ${
                     showCycle ? 'text-nvidia-500' 
                     : cycleStatus === 'running' ? 'text-blue-400 animate-spin' 
                     : 'text-gray-500'
                   }`} />
                   <span className={`text-sm ${
                     showCycle ? 'text-nvidia-500' 
                     : cycleStatus === 'running' ? 'text-blue-400'
                     : 'text-gray-600 dark:text-gray-400'
                   }`}>
                     {cycleStatus === 'running' ? '循环运行中' : '编程循环'}
                   </span>
                 </button>
               </div>
               {showCycle && (
                 <div className="mt-2 bg-gray-800/30 border border-gray-700/50 rounded-lg p-3 space-y-3">
                   <CycleEditor
                     isVisionMode={false}
                     maxDepth={cycleConfig.max_depth}
                     defaultSteps={cycleConfig.default_sequence}
                     executionStatus={cycleStatus}
                     onStart={handleCycleStart}
                     onStop={handleCycleStop}
                   />
                   {cycleStatus !== 'idle' && (
                     <CycleProgress status={cycleStatus} events={cycleEvents} />
                   )}
                 </div>
               )}
             </div>
           )}
   ```

7. Remove the CycleProgress rendering in main area (lines 1178-1180):
   Remove:
   ```tsx
           {cycleStatus !== 'idle' && (
             <CycleProgress status={cycleStatus} events={cycleEvents} />
           )}
   ```

- [ ] **Step 2: Verify TypeScript compilation**

Run: `cd /home/zk/Project/LLM-deployment/frontend && npx tsc --noEmit`

Expected: 0 errors

- [ ] **Step 3: Verify Next.js build**

Run: `cd /home/zk/Project/LLM-deployment/frontend && npx next build`

Expected: Build succeeds for both `/` and `/vision` routes

- [ ] **Step 4: Commit**

```bash
git add frontend/src/app/page.tsx
git commit -m "feat: text page uses inline cycle button instead of ActionPanel"
```

---

### Task 8: Frontend — Fix CycleEditor Default Steps Loading (Bug Fix)

**Files:**
- Modify: `frontend/src/components/CycleEditor.tsx:36-50`

- [ ] **Step 1: Fix the localStorage loading to properly fallback to defaultSteps**

The issue: On first load, `localStorage.getItem(STORAGE_KEY)` returns `null`, so it falls to `setSteps(defaultSteps)`. But `defaultSteps` is `[]` on mount because the `/config` API hasn't returned yet. The `useEffect` runs once on mount with `[]` as dependency, and `defaultSteps` changing later doesn't trigger re-run.

Fix: Make the effect depend on `defaultSteps` and only skip loading defaults when localStorage actually has steps.

In `frontend/src/components/CycleEditor.tsx`, replace the load effect (lines 37-50):

```typescript
  // Load from localStorage on mount, fallback to defaultSteps
  useEffect(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved) {
        const state: CycleEditorState = JSON.parse(saved);
        if (state.steps && state.steps.length > 0) {
          setSteps(state.steps);
          setShareContext(state.shareContext);
          return;
        }
      }
    } catch {
      // Fall through to defaults
    }
    // Use defaults only if we have some (API may not have loaded yet)
    if (defaultSteps.length > 0) {
      setSteps(defaultSteps);
    }
  }, [defaultSteps]);
```

- [ ] **Step 2: Verify TypeScript compilation**

Run: `cd /home/zk/Project/LLM-deployment/frontend && npx tsc --noEmit`

Expected: 0 errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/CycleEditor.tsx
git commit -m "fix: CycleEditor properly loads default steps from backend config"
```

---

### Task 9: Verification — Full Test Suite and Build

**Files:** None (verification only)

- [ ] **Step 1: Run all backend tests**

Run: `cd /home/zk/Project/LLM-deployment && .sglang/bin/python -m pytest tests/ -v`

Expected: All tests pass

- [ ] **Step 2: Run TypeScript compilation check**

Run: `cd /home/zk/Project/LLM-deployment/frontend && npx tsc --noEmit`

Expected: 0 errors

- [ ] **Step 3: Run Next.js build**

Run: `cd /home/zk/Project/LLM-deployment/frontend && npx next build`

Expected: Build succeeds for both `/` and `/vision` routes

- [ ] **Step 4: Final commit (if any remaining changes)**

```bash
git add -A && git status
# Only commit if there are changes
```
