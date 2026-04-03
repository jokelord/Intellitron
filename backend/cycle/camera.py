"""
摄像头服务

通过 V4L2 ioctl 过滤真正的视频捕获设备，再用 OpenCV 拍照。
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

# ---- V4L2 constants ----
VIDIOC_QUERYCAP = 0x80685600
V4L2_CAP_VIDEO_CAPTURE = 0x00000001


def _check_v4l2_capture(device_path: str) -> tuple[bool, str]:
    """
    Open *device_path* and issue VIDIOC_QUERYCAP.

    Returns
        (is_capture_device, card_name)

    Raises OSError on any failure (caller decides how to handle).
    """
    fd = os.open(device_path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        buf = bytearray(104)
        fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf)
        # card name is bytes 16..48 (32 bytes, NUL-padded)
        card = buf[16:48].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        # capabilities at offset 84 (uint32 LE)
        capabilities = struct.unpack_from("<I", buf, 84)[0]
        is_capture = bool(capabilities & V4L2_CAP_VIDEO_CAPTURE)
        return is_capture, card
    finally:
        os.close(fd)


class CameraService:
    """摄像头管理与拍照服务"""

    def list_cameras(self) -> list[dict]:
        """枚举具有 V4L2_CAP_VIDEO_CAPTURE 能力的摄像头设备"""
        devices = sorted(glob.glob("/dev/video*"))
        cameras: list[dict] = []
        seq = 0
        for dev in devices:
            match = re.search(r"/dev/video(\d+)$", dev)
            if not match:
                continue
            try:
                is_capture, card_name = _check_v4l2_capture(dev)
            except OSError:
                logger.debug("跳过 %s: V4L2 查询失败", dev)
                continue
            if not is_capture:
                continue
            cameras.append(
                {
                    "index": seq,
                    "device": dev,
                    "name": card_name,
                }
            )
            seq += 1
        return cameras

    # ------------------------------------------------------------------

    def _resolve_device(self, index: int) -> str | None:
        """Map sequential *index* back to the actual ``/dev/videoN`` path."""
        cameras = self.list_cameras()
        for cam in cameras:
            if cam["index"] == index:
                return cam["device"]
        return None

    # ------------------------------------------------------------------

    def capture(self, indices: list[int]) -> list[dict]:
        """
        从指定摄像头拍照

        Args:
            indices: 摄像头索引列表，如 [0, 1]

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
        device_path = self._resolve_device(index)
        if device_path is None:
            return {
                "camera_index": index,
                "error": f"未找到索引 {index} 对应的摄像头设备",
            }

        cap = None
        try:
            cap = cv2.VideoCapture(device_path)
            if not cap.isOpened():
                return {
                    "camera_index": index,
                    "error": f"无法打开摄像头 {index}",
                }

            ret, frame = cap.read()
            if not ret or frame is None:
                return {
                    "camera_index": index,
                    "error": f"摄像头 {index} 读取失败",
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
