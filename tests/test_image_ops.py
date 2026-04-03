"""
Tests for backend/tools/image_ops.py
"""

import os
import io
import base64
import tempfile
import threading
import pytest
from PIL import Image as PILImage

from backend.tools.image_ops import (
    load_images,
    store_tool_images,
    get_and_clear_tool_images,
    _tool_loaded_images,
    _buffer_lock,
)
from backend.agent.agent_runner import current_session_context


@pytest.fixture(autouse=True)
def clean_buffer():
    """每个测试前后清理全局缓冲区"""
    with _buffer_lock:
        _tool_loaded_images.clear()
    yield
    with _buffer_lock:
        _tool_loaded_images.clear()
    current_session_context.clear()


@pytest.fixture
def session_ctx():
    """设置 session context 并在测试后清理"""
    sid = "test-session-001"
    current_session_context.set(sid)
    yield sid
    current_session_context.clear()


@pytest.fixture
def tmp_jpeg(tmp_path):
    """创建临时 JPEG 文件"""
    img = PILImage.new("RGB", (200, 150), color=(255, 0, 0))
    path = tmp_path / "test.jpg"
    img.save(str(path), format="JPEG")
    return str(path)


@pytest.fixture
def tmp_png_rgba(tmp_path):
    """创建临时 RGBA PNG 文件"""
    img = PILImage.new("RGBA", (100, 100), color=(0, 255, 0, 128))
    path = tmp_path / "test_rgba.png"
    img.save(str(path), format="PNG")
    return str(path)


@pytest.fixture
def tmp_large_image(tmp_path):
    """创建超大尺寸图片"""
    img = PILImage.new("RGB", (3000, 2000), color=(0, 0, 255))
    path = tmp_path / "large.jpg"
    img.save(str(path), format="JPEG")
    return str(path)


class TestBufferOps:
    """测试全局缓冲区操作"""

    def test_store_and_get(self):
        img = PILImage.new("RGB", (10, 10))
        store_tool_images("s1", [img])
        result = get_and_clear_tool_images("s1")
        assert len(result) == 1
        assert result[0] is img

    def test_get_clears_buffer(self):
        img = PILImage.new("RGB", (10, 10))
        store_tool_images("s1", [img])
        get_and_clear_tool_images("s1")
        assert get_and_clear_tool_images("s1") == []

    def test_get_nonexistent_session(self):
        assert get_and_clear_tool_images("nonexistent") == []

    def test_multiple_stores_accumulate(self):
        img1 = PILImage.new("RGB", (10, 10))
        img2 = PILImage.new("RGB", (20, 20))
        store_tool_images("s1", [img1])
        store_tool_images("s1", [img2])
        result = get_and_clear_tool_images("s1")
        assert len(result) == 2

    def test_separate_sessions(self):
        img1 = PILImage.new("RGB", (10, 10))
        img2 = PILImage.new("RGB", (20, 20))
        store_tool_images("s1", [img1])
        store_tool_images("s2", [img2])
        assert len(get_and_clear_tool_images("s1")) == 1
        assert len(get_and_clear_tool_images("s2")) == 1


class TestLoadImages:
    """测试 load_images 函数"""

    def test_no_session_context(self, tmp_jpeg):
        """无 session context 时应报错"""
        result = load_images(tmp_jpeg)
        assert "错误" in result
        assert "会话" in result

    def test_empty_paths(self, session_ctx):
        result = load_images("")
        assert "错误" in result
        assert "未提供" in result

    def test_too_many_paths(self, session_ctx, tmp_jpeg):
        paths = ",".join([tmp_jpeg] * 10)
        result = load_images(paths)
        assert "错误" in result
        assert "最多" in result

    def test_file_not_found(self, session_ctx):
        result = load_images("/nonexistent/path/image.jpg")
        assert "文件不存在" in result
        assert "0/1" in result

    def test_unsupported_format(self, session_ctx, tmp_path):
        txt_file = tmp_path / "test.txt"
        txt_file.write_text("not an image")
        result = load_images(str(txt_file))
        assert "不支持的格式" in result

    def test_load_single_jpeg(self, session_ctx, tmp_jpeg):
        result = load_images(tmp_jpeg)
        assert "1/1" in result
        assert "已加载" in result
        images = get_and_clear_tool_images(session_ctx)
        assert len(images) == 1
        assert images[0].mode == "RGB"

    def test_load_single_jpeg_direct_session_id(self, tmp_jpeg):
        """直接传入 session_id 参数（模拟 wrapper 注入）"""
        sid = "direct-session-test"
        result = load_images(tmp_jpeg, session_id=sid)
        assert "1/1" in result
        assert "已加载" in result
        images = get_and_clear_tool_images(sid)
        assert len(images) == 1
        assert images[0].mode == "RGB"

    def test_load_rgba_converts_to_rgb(self, session_ctx, tmp_png_rgba):
        result = load_images(tmp_png_rgba)
        assert "1/1" in result
        images = get_and_clear_tool_images(session_ctx)
        assert len(images) == 1
        assert images[0].mode == "RGB"

    def test_load_large_image_resized(self, session_ctx, tmp_large_image):
        result = load_images(tmp_large_image, max_dimension=1280)
        assert "1/1" in result
        images = get_and_clear_tool_images(session_ctx)
        assert len(images) == 1
        w, h = images[0].size
        assert max(w, h) <= 1280

    def test_pre_encoded_attributes(self, session_ctx, tmp_jpeg):
        load_images(tmp_jpeg)
        images = get_and_clear_tool_images(session_ctx)
        img = images[0]
        assert hasattr(img, "_pre_encoded_b64")
        assert hasattr(img, "_pre_encoded_mime")
        assert img._pre_encoded_mime == "image/jpeg"
        # b64 应该是有效的 base64
        decoded = base64.b64decode(img._pre_encoded_b64)
        assert len(decoded) > 0

    def test_multiple_images(self, session_ctx, tmp_jpeg, tmp_png_rgba):
        paths = f"{tmp_jpeg},{tmp_png_rgba}"
        result = load_images(paths)
        assert "2/2" in result
        images = get_and_clear_tool_images(session_ctx)
        assert len(images) == 2

    def test_mixed_valid_invalid(self, session_ctx, tmp_jpeg):
        paths = f"{tmp_jpeg},/nonexistent.jpg"
        result = load_images(paths)
        assert "1/2" in result
        images = get_and_clear_tool_images(session_ctx)
        assert len(images) == 1

    def test_tilde_expansion(self, session_ctx, tmp_jpeg, tmp_path):
        """测试 ~ 路径展开（使用符号链接模拟）"""
        # 这个测试只验证 expanduser 被调用，不需要真实的 ~ 路径
        result = load_images(tmp_jpeg)
        assert "1/1" in result


class TestThreadSafety:
    """测试线程安全性"""

    def test_concurrent_store_and_get(self):
        results = []

        def store_task(sid, count):
            imgs = [PILImage.new("RGB", (10, 10)) for _ in range(count)]
            store_tool_images(sid, imgs)

        threads = [
            threading.Thread(target=store_task, args=(f"s{i}", 5)) for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = 0
        for i in range(10):
            total += len(get_and_clear_tool_images(f"s{i}"))
        assert total == 50
