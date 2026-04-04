"""Tests for backend/skills/load_local_images/load_local_images.py standalone script."""

import base64
import json
import os
import shutil
import subprocess
import uuid
from io import BytesIO

import pytest
from PIL import Image as PILImage

SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "backend",
    "skills",
    "load_local_images",
    "load_local_images.py",
)
PYTHON = os.path.join(os.path.dirname(__file__), "..", ".sglang", "bin", "python")


def _run_script(session_id, files, max_dim=None):
    """Helper: run the script and return (returncode, stdout, stderr)."""
    cmd = [PYTHON, SCRIPT_PATH, "--session-id", session_id, "--files", files]
    if max_dim is not None:
        cmd.extend(["--max-dim", str(max_dim)])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result.returncode, result.stdout, result.stderr


@pytest.fixture
def session_id():
    sid = f"test-{uuid.uuid4().hex[:8]}"
    yield sid
    # Cleanup
    d = f"/tmp/agent_images/{sid}"
    if os.path.exists(d):
        shutil.rmtree(d)


@pytest.fixture
def tmp_jpeg(tmp_path):
    img = PILImage.new("RGB", (200, 150), color=(255, 0, 0))
    path = tmp_path / "test.jpg"
    img.save(str(path), format="JPEG")
    return str(path)


@pytest.fixture
def tmp_png_rgba(tmp_path):
    img = PILImage.new("RGBA", (100, 100), color=(0, 255, 0, 128))
    path = tmp_path / "test_rgba.png"
    img.save(str(path), format="PNG")
    return str(path)


@pytest.fixture
def tmp_large_image(tmp_path):
    img = PILImage.new("RGB", (3000, 2000), color=(0, 0, 255))
    path = tmp_path / "large.jpg"
    img.save(str(path), format="JPEG")
    return str(path)


class TestScriptBasic:
    def test_no_args_returns_error(self):
        result = subprocess.run(
            [PYTHON, SCRIPT_PATH], capture_output=True, text=True, timeout=10
        )
        assert result.returncode != 0

    def test_load_single_jpeg(self, session_id, tmp_jpeg):
        rc, stdout, stderr = _run_script(session_id, tmp_jpeg)
        assert rc == 0
        assert "1/1" in stdout
        assert "已加载" in stdout
        # Verify JSON output file exists
        images_dir = f"/tmp/agent_images/{session_id}"
        json_files = [f for f in os.listdir(images_dir) if f.endswith(".json")]
        assert len(json_files) == 1
        with open(os.path.join(images_dir, json_files[0]), "r") as f:
            data = json.load(f)
        assert len(data["images"]) == 1
        assert data["images"][0]["mime"] == "image/jpeg"
        assert len(data["images"][0]["base64"]) > 0

    def test_load_rgba_converts_to_rgb(self, session_id, tmp_png_rgba):
        rc, stdout, stderr = _run_script(session_id, tmp_png_rgba)
        assert rc == 0
        assert "1/1" in stdout

    def test_load_large_image_resized(self, session_id, tmp_large_image):
        rc, stdout, stderr = _run_script(session_id, tmp_large_image, max_dim=1280)
        assert rc == 0
        assert "1/1" in stdout
        # Verify resized in JSON
        images_dir = f"/tmp/agent_images/{session_id}"
        json_files = [f for f in os.listdir(images_dir) if f.endswith(".json")]
        with open(os.path.join(images_dir, json_files[0]), "r") as f:
            data = json.load(f)
        raw = base64.b64decode(data["images"][0]["base64"])
        img = PILImage.open(BytesIO(raw))
        assert max(img.size) <= 1280

    def test_pre_encoded_jpeg_optimization(self, session_id, tmp_jpeg):
        """Unmodified JPEG should use raw bytes (no re-encoding)."""
        rc, stdout, stderr = _run_script(session_id, tmp_jpeg)
        assert rc == 0
        images_dir = f"/tmp/agent_images/{session_id}"
        json_files = [f for f in os.listdir(images_dir) if f.endswith(".json")]
        with open(os.path.join(images_dir, json_files[0]), "r") as f:
            data = json.load(f)
        # The base64 should decode to valid JPEG
        raw = base64.b64decode(data["images"][0]["base64"])
        # Compare with original file bytes
        with open(tmp_jpeg, "rb") as f:
            original_bytes = f.read()
        assert raw == original_bytes


class TestScriptErrors:
    def test_file_not_found(self, session_id):
        rc, stdout, stderr = _run_script(session_id, "/nonexistent/image.jpg")
        assert rc == 1
        assert "文件不存在" in stdout

    def test_unsupported_format(self, session_id, tmp_path):
        txt_file = tmp_path / "test.txt"
        txt_file.write_text("not an image")
        rc, stdout, stderr = _run_script(session_id, str(txt_file))
        assert rc == 1
        assert "不支持的格式" in stdout

    def test_too_many_images(self, session_id, tmp_jpeg):
        paths = ",".join([tmp_jpeg] * 10)
        rc, stdout, stderr = _run_script(session_id, paths)
        assert rc == 1
        assert "最多" in stdout

    def test_empty_files_arg(self, session_id):
        rc, stdout, stderr = _run_script(session_id, " ")
        assert rc == 1

    def test_mixed_valid_invalid(self, session_id, tmp_jpeg):
        paths = f"{tmp_jpeg},/nonexistent.jpg"
        rc, stdout, stderr = _run_script(session_id, paths)
        assert rc == 0  # at least one succeeded
        assert "1/2" in stdout


class TestScriptMultiple:
    def test_multiple_images(self, session_id, tmp_jpeg, tmp_png_rgba):
        rc, stdout, stderr = _run_script(session_id, f"{tmp_jpeg},{tmp_png_rgba}")
        assert rc == 0
        assert "2/2" in stdout
        images_dir = f"/tmp/agent_images/{session_id}"
        json_files = [f for f in os.listdir(images_dir) if f.endswith(".json")]
        with open(os.path.join(images_dir, json_files[0]), "r") as f:
            data = json.load(f)
        assert len(data["images"]) == 2


class TestScriptCleanup:
    def test_json_output_directory_created(self, session_id, tmp_jpeg):
        images_dir = f"/tmp/agent_images/{session_id}"
        assert not os.path.exists(images_dir)
        _run_script(session_id, tmp_jpeg)
        assert os.path.isdir(images_dir)
