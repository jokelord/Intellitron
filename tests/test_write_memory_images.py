"""Tests for file-based image IPC in _read_and_consume_tool_images()."""

import base64
import io
import json
import os
import shutil
import uuid
from pathlib import Path

import pytest
from PIL import Image as PILImage

from backend.agent.agent_runner import (
    _read_and_consume_tool_images,
    current_session_context,
)


@pytest.fixture(autouse=True)
def cleanup():
    yield
    current_session_context.clear()


def _create_image_json(session_id: str, images: list[tuple[int, int]]) -> Path:
    """Create a JSON file with base64-encoded test images.

    Args:
        session_id: session ID for directory path
        images: list of (width, height) tuples for test images to create

    Returns:
        Path to the created JSON file
    """
    images_dir = Path(f"/tmp/agent_images/{session_id}")
    images_dir.mkdir(parents=True, exist_ok=True)

    images_data = []
    for w, h in images:
        img = PILImage.new("RGB", (w, h), color=(255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        b64 = base64.b64encode(buf.getvalue()).decode()
        images_data.append({"mime": "image/jpeg", "base64": b64})

    output_file = images_dir / f"{uuid.uuid4().hex}.json"
    with open(output_file, "w") as f:
        json.dump({"images": images_data, "summary": "test"}, f)
    return output_file


def _cleanup_dir(session_id: str):
    """Remove session image directory."""
    d = Path(f"/tmp/agent_images/{session_id}")
    if d.exists():
        shutil.rmtree(d)


class TestReadAndConsumeToolImages:
    def test_json_files_consumed_and_deleted(self):
        sid = f"test-ipc-consume-{uuid.uuid4().hex[:6]}"
        try:
            json_file = _create_image_json(sid, [(100, 100)])
            assert json_file.exists()

            images = _read_and_consume_tool_images(sid)

            assert len(images) == 1
            assert hasattr(images[0], "_pre_encoded_b64")
            assert hasattr(images[0], "_pre_encoded_mime")
            assert images[0]._pre_encoded_mime == "image/jpeg"
            # JSON file should be deleted
            assert not json_file.exists()
        finally:
            _cleanup_dir(sid)

    def test_multiple_images_in_json(self):
        sid = f"test-ipc-multi-{uuid.uuid4().hex[:6]}"
        try:
            _create_image_json(sid, [(100, 100), (200, 200)])
            images = _read_and_consume_tool_images(sid)
            assert len(images) == 2
        finally:
            _cleanup_dir(sid)

    def test_no_json_files_returns_empty(self):
        sid = f"test-ipc-empty-{uuid.uuid4().hex[:6]}"
        images = _read_and_consume_tool_images(sid)
        assert images == []

    def test_corrupted_json_skipped(self):
        sid = f"test-ipc-corrupt-{uuid.uuid4().hex[:6]}"
        try:
            images_dir = Path(f"/tmp/agent_images/{sid}")
            images_dir.mkdir(parents=True, exist_ok=True)
            corrupt_file = images_dir / "corrupt.json"
            corrupt_file.write_text("not valid json {{{")

            images = _read_and_consume_tool_images(sid)
            assert images == []
            # Corrupt file should still be deleted
            assert not corrupt_file.exists()
        finally:
            _cleanup_dir(sid)

    def test_empty_directory_cleaned_up(self):
        sid = f"test-ipc-cleanup-{uuid.uuid4().hex[:6]}"
        try:
            json_file = _create_image_json(sid, [(50, 50)])
            images_dir = json_file.parent

            _read_and_consume_tool_images(sid)

            # Directory should be removed after consuming all files
            assert not images_dir.exists()
        finally:
            _cleanup_dir(sid)

    def test_none_session_id_returns_empty(self):
        images = _read_and_consume_tool_images(None)
        assert images == []

    def test_empty_session_id_returns_empty(self):
        images = _read_and_consume_tool_images("")
        assert images == []

    def test_pre_encoded_b64_is_valid(self):
        sid = f"test-ipc-b64-{uuid.uuid4().hex[:6]}"
        try:
            _create_image_json(sid, [(100, 100)])
            images = _read_and_consume_tool_images(sid)
            img = images[0]
            # Verify b64 decodes to valid JPEG
            raw = base64.b64decode(img._pre_encoded_b64)
            reopened = PILImage.open(io.BytesIO(raw))
            assert reopened.size == (100, 100)
        finally:
            _cleanup_dir(sid)
