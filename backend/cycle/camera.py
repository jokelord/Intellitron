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
import time

import cv2
import numpy as np

from backend.config import get_config


logger = logging.getLogger(__name__)

# ---- V4L2 constants ----
VIDIOC_QUERYCAP = 0x80685600
V4L2_CAP_VIDEO_CAPTURE = 0x00000001


def _check_v4l2_capture(device_path: str) -> tuple[bool, str, str]:
    """
    Open *device_path* and issue VIDIOC_QUERYCAP.

    Returns
        (is_capture_device, card_name, bus_info)

    *bus_info* identifies the physical connection (e.g. ``usb-0000:00:14.0-1``).
    The same physical camera may expose multiple ``/dev/video*`` nodes (raw,
    compressed, metadata); they all share the same *bus_info*.

    Raises OSError on any failure (caller decides how to handle).
    """
    fd = -1
    try:
        fd = os.open(device_path, os.O_RDONLY | os.O_NONBLOCK)
        buf = bytearray(104)
        fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf)
        # card name is bytes 16..48 (32 bytes, NUL-padded)
        card = buf[16:48].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        # bus_info is bytes 48..80 (32 bytes, NUL-padded)
        bus = buf[48:80].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        # capabilities at offset 84 (uint32 LE)
        capabilities = struct.unpack_from("<I", buf, 84)[0]
        is_capture = bool(capabilities & V4L2_CAP_VIDEO_CAPTURE)
        return is_capture, card, bus
    finally:
        if fd >= 0:
            os.close(fd)


class CameraService:
    """摄像头管理与拍照服务"""

    def __init__(self):
        self.config = get_config().camera

    def list_cameras(self) -> list[dict]:
        """枚举具有 V4L2_CAP_VIDEO_CAPTURE 能力的摄像头设备

        同一物理摄像头可能暴露多个 /dev/video* 节点（原始流、压缩流、元数据）。
        通过 bus_info 去重，只保留每个物理设备的首个（编号最小的）捕获节点。
        """
        devices = sorted(glob.glob("/dev/video*"))
        cameras: list[dict] = []
        seen_buses: set[str] = set()
        seq = 0
        for dev in devices:
            match = re.search(r"/dev/video(\d+)$", dev)
            if not match:
                continue
            try:
                is_capture, card_name, bus_info = _check_v4l2_capture(dev)
            except OSError:
                logger.debug("跳过 %s: V4L2 查询失败", dev)
                continue
            if not is_capture:
                continue
            # Deduplicate: same physical camera → same bus_info
            if bus_info in seen_buses:
                continue
            seen_buses.add(bus_info)
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

    def _is_builtin_camera(self, index: int) -> bool:
        """检查指定索引的摄像头是否为内置摄像头

        通过检查 bus_info 来判断摄像头类型：
        - 内置摄像头通常包含 "platform" 或特定硬件标识符
        - USB摄像头通常包含 "usb-"
        """
        cameras = self.list_cameras()
        for cam in cameras:
            if cam["index"] == index:
                device_path = cam["device"]
                try:
                    is_capture, card_name, bus_info = _check_v4l2_capture(device_path)
                    if is_capture:
                        # 检查 bus_info 是否包含平台相关标识符（内置摄像头）
                        # 常见模式：platform:xxxxx 或不含 "usb-"
                        if (
                            "platform" in bus_info.lower()
                            or "usb-" not in bus_info.lower()
                        ):
                            logger.debug(
                                f"摄像头 {index} ({device_path}) 检测为内置摄像头: bus_info='{bus_info}'"
                            )
                            return True
                        else:
                            logger.debug(
                                f"摄像头 {index} ({device_path}) 检测为USB摄像头: bus_info='{bus_info}'"
                            )
                            return False
                except OSError:
                    logger.warning(f"无法检查摄像头 {index} 的类型，默认视为USB摄像头")
                    return False
        return False

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

    # 摄像头预热帧数：USB 摄像头刚打开时前几帧通常全黑或曝光不足
    # （自动曝光/白平衡需要几帧来调整），丢弃这些帧后再取真正的照片。
    @property
    def WARMUP_FRAMES(self):
        return self.config.usb_warmup_frames

    # 亮度验证阈值：帧的平均像素值低于此值视为黑帧/损坏帧
    @property
    def MIN_BRIGHTNESS(self):
        return self.config.min_brightness

    # 最大重试次数（含首次尝试）：每次重试增加预热帧数并延长等待
    @property
    def MAX_CAPTURE_ATTEMPTS(self):
        return self.config.max_capture_attempts

    # 打开设备后的初始等待时间（秒），让硬件完成初始化
    @property
    def DEVICE_OPEN_DELAY(self):
        return self.config.usb_device_open_delay

    def _capture_one(self, index: int) -> dict:
        """从单个摄像头拍一张照片

        打开设备后先等待硬件初始化，再丢弃 WARMUP_FRAMES 帧让摄像头
        自动曝光/白平衡稳定，最后读取最终帧。

        如果拍到的帧亮度过低（黑帧），会自动重试：每次重试增加预热帧数
        并延长等待时间，最多尝试 MAX_CAPTURE_ATTEMPTS 次。
        """
        device_path = self._resolve_device(index)
        if device_path is None:
            return {
                "camera_index": index,
                "error": f"未找到索引 {index} 对应的摄像头设备",
            }

        # 根据摄像头类型调整参数
        # 内置摄像头（通常是0号）可能需要更长的初始化时间
        is_builtin_camera = self._is_builtin_camera(index)
        if is_builtin_camera:
            # 内置摄像头：更长的延迟和warmup
            device_delay = self.config.builtin_device_open_delay
            warmup_frames = self.config.builtin_warmup_frames
            logger.info(
                f"摄像头 {index} 检测为内置摄像头，使用延长参数: delay={device_delay}s, warmup={warmup_frames}"
            )
        else:
            # USB摄像头：标准参数
            device_delay = self.config.usb_device_open_delay
            warmup_frames = self.config.usb_warmup_frames

        for attempt in range(1, self.MAX_CAPTURE_ATTEMPTS + 1):
            cap = None
            try:
                cap = cv2.VideoCapture(device_path)
                if not cap.isOpened():
                    return {
                        "camera_index": index,
                        "error": f"无法打开摄像头 {index}",
                    }

                # 设置 V4L2 内核缓冲区为最小值（1-2 帧），减少残留旧帧。
                # 默认 V4L2 缓冲区通常为 4-5 帧，在循环拍照场景中会导致
                # 拿到几秒前的旧帧。CAP_PROP_BUFFERSIZE 可能不被所有驱动支持，
                # 失败时静默忽略。
                try:
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                except Exception:
                    pass

                # 等待硬件初始化（USB 摄像头切换后需要时间）
                delay = device_delay * attempt
                time.sleep(delay)

                # 丢弃预热帧，让自动曝光/白平衡稳定。
                # 使用 grab() 而非 read()：grab() 只从驱动取帧不解码像素，
                # 速度快约 3-5 倍，能更快地排空 V4L2 缓冲区中的旧帧。
                warmup = warmup_frames * attempt
                for _ in range(warmup):
                    cap.grab()

                ret, frame = cap.read()
                if not ret or frame is None:
                    if attempt < self.MAX_CAPTURE_ATTEMPTS:
                        logger.warning(
                            f"摄像头 {index} 第 {attempt} 次读取失败，"
                            f"将重试 (warmup={warmup}, delay={delay:.1f}s)"
                        )
                        continue
                    return {
                        "camera_index": index,
                        "error": f"摄像头 {index} 读取失败",
                    }

                # 亮度验证：检测黑帧/曝光不足
                brightness = float(np.mean(frame))
                if brightness < self.MIN_BRIGHTNESS:
                    if attempt < self.MAX_CAPTURE_ATTEMPTS:
                        logger.warning(
                            f"摄像头 {index} 第 {attempt} 次拍照亮度过低 "
                            f"({brightness:.1f} < {self.MIN_BRIGHTNESS})，"
                            f"将重试 (warmup={warmup}, delay={delay:.1f}s)"
                        )
                        continue
                    logger.warning(
                        f"摄像头 {index} 亮度仍然过低 ({brightness:.1f})，"
                        f"已达最大重试次数，使用当前帧"
                    )

                # 图片缩放 step1：按 capture_dimension 缩放至最大边长（保持宽高比）
                cap_dim = self.config.capture_dimension
                if cap_dim and cap_dim > 0:
                    h, w = frame.shape[:2]
                    if max(w, h) > cap_dim:
                        if w >= h:
                            new_w = cap_dim
                            new_h = int(h * cap_dim / w)
                        else:
                            new_h = cap_dim
                            new_w = int(w * cap_dim / h)
                        frame = cv2.resize(
                            frame, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4
                        )
                        logger.debug(
                            f"摄像头 {index} 采集缩放: {w}x{h} → {new_w}x{new_h}"
                        )

                # 图片缩放 step2：按 model 配置的 camera_max_dimension 缩放后再发给 VLM
                max_dim = get_config().model.camera_max_dimension
                if max_dim and max_dim > 0:
                    h, w = frame.shape[:2]
                    if w > max_dim or h > max_dim:
                        if w > h:
                            new_w = max_dim
                            new_h = int(h * max_dim / w)
                        else:
                            new_h = max_dim
                            new_w = int(w * max_dim / h)
                        frame = cv2.resize(
                            frame, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4
                        )
                        logger.debug(
                            f"摄像头 {index} VLM 缩放: {w}x{h} → {new_w}x{new_h}"
                        )

                success, buffer = cv2.imencode(".jpg", frame)
                if not success:
                    return {
                        "camera_index": index,
                        "error": f"摄像头 {index} JPEG 编码失败",
                    }

                image_base64 = base64.b64encode(buffer.tobytes()).decode("utf-8")
                logger.info(
                    f"摄像头 {index} ({device_path}) 拍照成功: "
                    f"帧大小 {frame.shape[1]}x{frame.shape[0]}, "
                    f"JPEG {len(buffer)} bytes, "
                    f"亮度 {brightness:.1f}, "
                    f"尝试次数 {attempt}"
                )
                return {
                    "camera_index": index,
                    "image_base64": f"data:image/jpeg;base64,{image_base64}",
                }

            except Exception as e:
                logger.error(f"摄像头 {index} 拍照异常 (尝试 {attempt}): {e}")
                if attempt >= self.MAX_CAPTURE_ATTEMPTS:
                    return {
                        "camera_index": index,
                        "error": str(e),
                    }
            finally:
                if cap is not None:
                    cap.release()

        # Should not reach here, but just in case
        return {
            "camera_index": index,
            "error": f"摄像头 {index} 拍照失败（已耗尽重试次数）",
        }
