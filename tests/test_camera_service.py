"""
Tests for backend/cycle/camera.py
"""

import pytest
from unittest.mock import patch, MagicMock
import numpy as np

from backend.cycle.camera import CameraService


# ---------------------------------------------------------------------------
# New: V4L2 filtering tests
# ---------------------------------------------------------------------------


class TestListCamerasV4L2:
    """测试 V4L2 能力过滤"""

    @patch("backend.cycle.camera.glob.glob")
    @patch("backend.cycle.camera._check_v4l2_capture")
    def test_filters_non_capture_devices(self, mock_check, mock_glob):
        """mock 4 devices, only 2 have VIDEO_CAPTURE → only 2 returned"""
        mock_glob.return_value = [
            "/dev/video0",
            "/dev/video1",
            "/dev/video2",
            "/dev/video3",
        ]
        mock_check.side_effect = [
            (True, "Camera A", "usb-0000:00:14.0-1"),  # video0 – capture
            (False, "Metadata", "usb-0000:00:14.0-1"),  # video1 – not capture
            (True, "Camera B", "usb-0000:00:14.0-2"),  # video2 – capture
            (False, "Metadata", "usb-0000:00:14.0-2"),  # video3 – not capture
        ]

        service = CameraService()
        cameras = service.list_cameras()

        assert len(cameras) == 2
        assert cameras[0] == {"index": 0, "device": "/dev/video0", "name": "Camera A"}
        assert cameras[1] == {"index": 1, "device": "/dev/video2", "name": "Camera B"}

    @patch("backend.cycle.camera.glob.glob")
    @patch("backend.cycle.camera._check_v4l2_capture")
    def test_handles_ioctl_failure(self, mock_check, mock_glob):
        """When _check_v4l2_capture raises OSError, device is skipped → empty list"""
        mock_glob.return_value = ["/dev/video0"]
        mock_check.side_effect = OSError("Permission denied")

        service = CameraService()
        cameras = service.list_cameras()

        assert cameras == []

    @patch("backend.cycle.camera.glob.glob")
    @patch("backend.cycle.camera._check_v4l2_capture")
    def test_reindexes_cameras(self, mock_check, mock_glob):
        """/dev/video0,2,4 all capture-capable → indices are 0,1,2"""
        mock_glob.return_value = ["/dev/video0", "/dev/video2", "/dev/video4"]
        mock_check.side_effect = [
            (True, "Cam0", "usb-0000:00:14.0-1"),
            (True, "Cam2", "usb-0000:00:14.0-2"),
            (True, "Cam4", "usb-0000:00:14.0-3"),
        ]

        service = CameraService()
        cameras = service.list_cameras()

        assert len(cameras) == 3
        assert [c["index"] for c in cameras] == [0, 1, 2]
        assert [c["device"] for c in cameras] == [
            "/dev/video0",
            "/dev/video2",
            "/dev/video4",
        ]
        assert [c["name"] for c in cameras] == ["Cam0", "Cam2", "Cam4"]

    @patch("backend.cycle.camera.glob.glob")
    @patch("backend.cycle.camera._check_v4l2_capture")
    def test_deduplicates_by_bus_info(self, mock_check, mock_glob):
        """Same bus_info → same physical camera; only first node is kept."""
        mock_glob.return_value = [
            "/dev/video0",
            "/dev/video1",
            "/dev/video2",
            "/dev/video3",
        ]
        mock_check.side_effect = [
            (True, "Camera A", "usb-0000:00:14.0-1"),  # video0 – cam A raw
            (
                True,
                "Camera A",
                "usb-0000:00:14.0-1",
            ),  # video1 – cam A compressed (dup!)
            (True, "Camera B", "usb-0000:00:14.0-2"),  # video2 – cam B raw
            (
                True,
                "Camera B",
                "usb-0000:00:14.0-2",
            ),  # video3 – cam B compressed (dup!)
        ]

        service = CameraService()
        cameras = service.list_cameras()

        assert len(cameras) == 2
        assert cameras[0] == {"index": 0, "device": "/dev/video0", "name": "Camera A"}
        assert cameras[1] == {"index": 1, "device": "/dev/video2", "name": "Camera B"}


# ---------------------------------------------------------------------------
# Updated: existing list_cameras tests (mock _check_v4l2_capture)
# ---------------------------------------------------------------------------


class TestListCameras:
    """测试摄像头枚举"""

    @patch("backend.cycle.camera.glob.glob")
    @patch("backend.cycle.camera._check_v4l2_capture")
    def test_list_cameras_found(self, mock_check, mock_glob):
        mock_glob.return_value = ["/dev/video0", "/dev/video1"]
        mock_check.side_effect = [
            (True, "Camera 0", "usb-0000:00:14.0-1"),
            (True, "Camera 1", "usb-0000:00:14.0-2"),
        ]
        service = CameraService()
        cameras = service.list_cameras()
        assert len(cameras) == 2
        assert cameras[0]["index"] == 0
        assert cameras[1]["index"] == 1

    @patch("backend.cycle.camera.glob.glob")
    @patch("backend.cycle.camera._check_v4l2_capture")
    def test_list_cameras_empty(self, mock_check, mock_glob):
        mock_glob.return_value = []
        service = CameraService()
        cameras = service.list_cameras()
        assert cameras == []

    @patch("backend.cycle.camera.glob.glob")
    @patch("backend.cycle.camera._check_v4l2_capture")
    def test_list_cameras_filters_non_numeric(self, mock_check, mock_glob):
        mock_glob.return_value = ["/dev/video0", "/dev/video1", "/dev/videoX"]
        mock_check.side_effect = [
            (True, "Cam0", "usb-0000:00:14.0-1"),
            (True, "Cam1", "usb-0000:00:14.0-2"),
            # videoX is filtered by regex before _check_v4l2_capture is called
        ]
        service = CameraService()
        cameras = service.list_cameras()
        assert len(cameras) == 2


# ---------------------------------------------------------------------------
# Updated: capture tests (mock _resolve_device)
# ---------------------------------------------------------------------------


class TestCapture:
    """测试摄像头拍照"""

    @patch("backend.cycle.camera.cv2")
    @patch.object(CameraService, "_resolve_device", return_value="/dev/video0")
    def test_capture_single(self, mock_resolve, mock_cv2):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        # 使用亮度足够的帧（> MIN_BRIGHTNESS=10.0），避免触发重试
        fake_frame = np.ones((100, 100, 3), dtype=np.uint8) * 128
        mock_cap.read.return_value = (True, fake_frame)
        mock_cv2.VideoCapture.return_value = mock_cap
        mock_cv2.imencode.return_value = (True, np.array([0xFF, 0xD8], dtype=np.uint8))

        service = CameraService()
        with patch.object(service, "_is_builtin_camera", return_value=False):
            results = service.capture([0])

        assert len(results) == 1
        assert "image_base64" in results[0]
        assert results[0]["camera_index"] == 0
        mock_cv2.VideoCapture.assert_called_with("/dev/video0")
        mock_cap.release.assert_called_once()

    @patch("backend.cycle.camera.cv2")
    @patch.object(CameraService, "_resolve_device", return_value="/dev/video0")
    def test_capture_multiple(self, mock_resolve, mock_cv2):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        fake_frame = np.zeros((100, 100, 3), dtype=np.uint8)
        mock_cap.read.return_value = (True, fake_frame)
        mock_cv2.VideoCapture.return_value = mock_cap
        mock_cv2.imencode.return_value = (True, np.array([0xFF, 0xD8], dtype=np.uint8))

        service = CameraService()
        results = service.capture([0, 1])
        assert len(results) == 2

    @patch("backend.cycle.camera.cv2")
    @patch.object(CameraService, "_resolve_device", return_value="/dev/video0")
    def test_capture_failed_open(self, mock_resolve, mock_cv2):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = False
        mock_cv2.VideoCapture.return_value = mock_cap

        service = CameraService()
        results = service.capture([0])
        assert len(results) == 1
        assert "error" in results[0]

    @patch("backend.cycle.camera.cv2")
    @patch.object(CameraService, "_resolve_device", return_value="/dev/video0")
    def test_capture_failed_read(self, mock_resolve, mock_cv2):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.read.return_value = (False, None)
        mock_cv2.VideoCapture.return_value = mock_cap

        service = CameraService()
        results = service.capture([0])
        assert len(results) == 1
        assert "error" in results[0]

    @patch("backend.cycle.camera.cv2")
    @patch.object(CameraService, "_resolve_device", return_value="/dev/video0")
    def test_capture_base64_format(self, mock_resolve, mock_cv2):
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

    @patch("backend.cycle.camera.cv2")
    @patch.object(CameraService, "_resolve_device", return_value=None)
    def test_capture_unknown_index(self, mock_resolve, mock_cv2):
        """When _resolve_device returns None, capture should return error"""
        service = CameraService()
        results = service.capture([99])
        assert len(results) == 1
        assert "error" in results[0]
        assert results[0]["camera_index"] == 99


# ---------------------------------------------------------------------------
# Warm-up frame tests
# ---------------------------------------------------------------------------


class TestCaptureWarmup:
    """测试摄像头预热帧丢弃行为"""

    @patch("backend.cycle.camera.cv2")
    @patch.object(CameraService, "_resolve_device", return_value="/dev/video0")
    def test_warmup_frames_discarded(self, mock_resolve, mock_cv2):
        """grab() should be called WARMUP_FRAMES times for warmup, read() once for real capture"""
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        # 使用亮度足够的帧，避免触发重试
        fake_frame = np.ones((100, 100, 3), dtype=np.uint8) * 128
        mock_cap.read.return_value = (True, fake_frame)
        mock_cv2.VideoCapture.return_value = mock_cap
        mock_cv2.imencode.return_value = (True, np.array([0xFF, 0xD8], dtype=np.uint8))

        service = CameraService()
        warmup_count = service.WARMUP_FRAMES  # property 访问
        with patch.object(service, "_is_builtin_camera", return_value=False):
            results = service.capture([0])

        # warmup 使用 grab()（不解码，更快），拍照使用 read()
        assert mock_cap.grab.call_count == warmup_count
        assert mock_cap.read.call_count == 1
        assert "image_base64" in results[0]

    @patch("backend.cycle.camera.cv2")
    @patch.object(CameraService, "_resolve_device", return_value="/dev/video0")
    def test_warmup_black_frames_then_good(self, mock_resolve, mock_cv2):
        """Warmup frames are handled by grab(), final read() frame is valid → capture succeeds"""
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True

        good_frame = np.ones((100, 100, 3), dtype=np.uint8) * 128

        # warmup 阶段使用 grab()（不返回帧），拍照使用 read()
        mock_cap.read.return_value = (True, good_frame)
        mock_cv2.VideoCapture.return_value = mock_cap
        mock_cv2.imencode.return_value = (True, np.array([0xFF, 0xD8], dtype=np.uint8))

        service = CameraService()
        with patch.object(service, "_is_builtin_camera", return_value=False):
            results = service.capture([0])

        assert "image_base64" in results[0]
        # imencode should be called with the good frame
        actual_frame = mock_cv2.imencode.call_args[0][1]
        assert np.array_equal(actual_frame, good_frame)

    @patch("backend.cycle.camera.cv2")
    @patch.object(CameraService, "_resolve_device", return_value="/dev/video0")
    def test_warmup_frames_configurable(self, mock_resolve, mock_cv2):
        """WARMUP_FRAMES is now a config-backed property, can be changed via config"""
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        fake_frame = np.ones((100, 100, 3), dtype=np.uint8) * 128
        mock_cap.read.return_value = (True, fake_frame)
        mock_cv2.VideoCapture.return_value = mock_cap
        mock_cv2.imencode.return_value = (True, np.array([0xFF, 0xD8], dtype=np.uint8))

        service = CameraService()
        # 通过修改 config 对象来改变 warmup 帧数
        service.config.usb_warmup_frames = 10
        with patch.object(service, "_is_builtin_camera", return_value=False):
            service.capture([0])

        # warmup 使用 grab()，10 帧预热 + 1 次 read
        assert mock_cap.grab.call_count == 10
        assert mock_cap.read.call_count == 1
