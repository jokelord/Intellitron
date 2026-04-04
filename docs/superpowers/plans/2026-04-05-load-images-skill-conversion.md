# load_images 工具转 Skill 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `load_images` 工具从 smolagents 工具注册表中完全移除，转为 skill + exec_command + 独立脚本架构，使用文件 IPC 传递图片数据。

**Architecture:** 独立 Python 脚本处理图片并输出 JSON 到 `/tmp/agent_images/<session_id>/`，`write_memory_to_messages()` 从文件读取 base64 → 构造 PIL Image 注入消息。`read_skill()` 增加 `{{SESSION_ID}}` 模板变量替换。

**Tech Stack:** Python, PIL/Pillow, JSON file IPC, smolagents, pytest

**Design Spec:** `docs/superpowers/specs/2026-04-05-load-images-skill-conversion-design.md`

---

### Task 1: 创建独立脚本 `load_local_images.py`

**Files:**
- Create: `backend/skills/load_local_images/load_local_images.py`
- Test: `tests/test_load_images_script.py`

- [ ] **Step 1: Write the failing test — basic CLI invocation**

```python
# tests/test_load_images_script.py
"""Tests for backend/skills/load_local_images/load_local_images.py standalone script."""

import json
import os
import subprocess
import tempfile
import uuid

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
    return f"test-{uuid.uuid4().hex[:8]}"


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
        import base64 as b64
        from io import BytesIO
        raw = b64.b64decode(data["images"][0]["base64"])
        img = PILImage.open(BytesIO(raw))
        assert max(img.size) <= 1280


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
        rc, stdout, stderr = _run_script(session_id, "")
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.sglang/bin/python -m pytest tests/test_load_images_script.py -v`
Expected: FAIL — script file does not exist

- [ ] **Step 3: Write the standalone script**

```python
#!/usr/bin/env python3
"""
load_local_images.py — 加载本地图片文件，输出 JSON 供 Agent 框架注入到 VLM 对话。

用法:
    python load_local_images.py --session-id <id> --files "<逗号分隔路径>" [--max-dim 1280]

输出:
    1. JSON 文件写入 /tmp/agent_images/<session_id>/<uuid>.json
    2. stdout 打印人类可读摘要
"""

import argparse
import base64
import io
import json
import os
import sys
import uuid

from PIL import Image as PILImage

SUPPORTED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB
MAX_IMAGE_COUNT = 9


def _process_image(expanded_path: str, max_dimension: int) -> tuple:
    """处理单张图片: 打开 → 缩放 → 色彩转换 → base64 JPEG 编码。

    Returns:
        (base64_str, mime, size_info, mode) on success
        Raises Exception on failure
    """
    pil_img = PILImage.open(expanded_path)
    pil_img.load()
    original_fmt = (pil_img.format or "").lower()
    w, h = pil_img.size
    original_w, original_h = w, h

    # 缩放
    resized = False
    if max_dimension > 0 and (w > max_dimension or h > max_dimension):
        if w > h:
            nw, nh = max_dimension, int(h * max_dimension / w)
        else:
            nh, nw = max_dimension, int(w * max_dimension / h)
        pil_img = pil_img.resize((nw, nh), PILImage.Resampling.LANCZOS)
        resized = True
        w, h = nw, nh

    # 透明通道转换
    color_changed = False
    if pil_img.mode == "RGBA":
        pil_img.load()
        bg = PILImage.new("RGB", pil_img.size, (255, 255, 255))
        bg.paste(pil_img, mask=pil_img.split()[3])
        pil_img = bg
        color_changed = True
    elif pil_img.mode in ("P", "PA", "LA"):
        pil_img.load()
        rgba_tmp = pil_img.convert("RGBA")
        bg = PILImage.new("RGB", rgba_tmp.size, (255, 255, 255))
        bg.paste(rgba_tmp, mask=rgba_tmp.split()[3])
        pil_img = bg
        color_changed = True
    elif pil_img.mode not in ("RGB", "L"):
        pil_img = pil_img.convert("RGB")
        color_changed = True

    # base64 编码
    if not resized and not color_changed and original_fmt == "jpeg":
        with open(expanded_path, "rb") as f:
            raw_bytes = f.read()
        b64_str = base64.b64encode(raw_bytes).decode()
    else:
        buf = io.BytesIO()
        save_img = pil_img if pil_img.mode in ("RGB", "L") else pil_img.convert("RGB")
        save_img.save(buf, format="JPEG", quality=90)
        b64_str = base64.b64encode(buf.getvalue()).decode()

    size_info = f"{original_w}x{original_h}→{w}x{h}" if resized else f"{w}x{h}"
    return b64_str, "image/jpeg", size_info, pil_img.mode


def main():
    parser = argparse.ArgumentParser(description="加载本地图片文件")
    parser.add_argument("--session-id", required=True, help="会话 ID")
    parser.add_argument("--files", required=True, help="逗号分隔的文件路径")
    parser.add_argument("--max-dim", type=int, default=1280, help="图片最大边长像素")
    args = parser.parse_args()

    raw_paths = [p.strip() for p in args.files.split(",") if p.strip()]
    if not raw_paths:
        print("错误: 未提供任何文件路径")
        sys.exit(1)
    if len(raw_paths) > MAX_IMAGE_COUNT:
        print(f"错误: 单次最多加载 {MAX_IMAGE_COUNT} 张图片，收到 {len(raw_paths)} 个路径")
        sys.exit(1)

    images_data = []
    results = []

    for idx, raw_path in enumerate(raw_paths, 1):
        expanded = os.path.expanduser(os.path.expandvars(raw_path))

        if not os.path.isfile(expanded):
            results.append(f"[{idx}] {raw_path}: 文件不存在")
            continue

        ext = os.path.splitext(expanded)[1].lstrip(".").lower()
        if ext not in SUPPORTED_EXTENSIONS:
            results.append(
                f"[{idx}] {raw_path}: 不支持的格式 '{ext}' "
                f"(支持: {', '.join(sorted(SUPPORTED_EXTENSIONS))})"
            )
            continue

        file_size = os.path.getsize(expanded)
        if file_size > MAX_FILE_SIZE:
            results.append(
                f"[{idx}] {raw_path}: 文件过大 "
                f"({file_size / 1024 / 1024:.1f}MB > {MAX_FILE_SIZE / 1024 / 1024:.0f}MB)"
            )
            continue

        try:
            b64_str, mime, size_info, mode = _process_image(expanded, args.max_dim)
            images_data.append({"mime": mime, "base64": b64_str})
            results.append(f"[{idx}] {raw_path}: 已加载 ({size_info}, {mode})")
        except Exception as e:
            results.append(f"[{idx}] {raw_path}: 加载失败 ({e})")

    success_count = len(images_data)
    total_count = len(raw_paths)
    header = f"图片加载完成: {success_count}/{total_count} 张成功"
    summary = header + "\n" + "\n".join(results)

    # 写 JSON 到文件
    if images_data:
        output_dir = f"/tmp/agent_images/{args.session_id}"
        os.makedirs(output_dir, exist_ok=True)
        output_file = os.path.join(output_dir, f"{uuid.uuid4().hex}.json")
        output_data = {"images": images_data, "summary": summary}
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False)

    print(summary)
    sys.exit(0 if success_count > 0 else 1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.sglang/bin/python -m pytest tests/test_load_images_script.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/skills/load_local_images/load_local_images.py tests/test_load_images_script.py
git commit -m "feat: add standalone load_local_images.py script with tests"
```

---

### Task 2: 创建 skill metadata 和 instructions

**Files:**
- Create: `backend/skills/load_local_images/metadata.yaml`
- Create: `backend/skills/load_local_images/instructions.md`

- [ ] **Step 1: Create metadata.yaml**

```yaml
name: load_local_images
description: "加载本地磁盘图片文件，使 VLM 能在对话中直接看到并分析图片内容"
tags:
  - image
  - vlm
  - file-loading
```

- [ ] **Step 2: Create instructions.md**

```markdown
# 加载本地图片

## 适用场景

当用户要求查看或分析本地磁盘上的图片文件时，使用本技能。

## 操作步骤

本技能配备了现成的可执行脚本，**你不需要编写任何代码**，直接调用即可。

### 唯一步骤：调用 exec_command 执行脚本

\`\`\`bash
.sglang/bin/python {{SKILL_DIR}}/load_local_images.py --session-id {{SESSION_ID}} --files "<逗号分隔的文件路径>"
\`\`\`

### 可选参数

- `--max-dim <数值>`: 图片最大边长像素，默认 1280

### 示例

\`\`\`bash
.sglang/bin/python {{SKILL_DIR}}/load_local_images.py --session-id {{SESSION_ID}} --files "/tmp/photo1.jpg, ~/images/diagram.png" --max-dim 1024
\`\`\`

## 限制

- 单次最多 9 张图片
- 支持格式: png, jpg, jpeg, gif, webp
- 单文件不超过 20MB
- 加载成功后，图片会在下一步推理中自动出现在对话上下文中
- **不要自己写代码实现此功能，直接调用上述脚本**
```

- [ ] **Step 3: Commit**

```bash
git add backend/skills/load_local_images/metadata.yaml backend/skills/load_local_images/instructions.md
git commit -m "feat: add load_local_images skill metadata and instructions"
```

---

### Task 3: 添加 `{{SESSION_ID}}` 模板变量到 `read_skill()`

**Files:**
- Modify: `backend/tools/skill_ops.py:91-96`
- Test: `tests/test_skill_session_id.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_skill_session_id.py
"""Tests for {{SESSION_ID}} template variable in read_skill()."""

import os
import tempfile
from unittest.mock import patch

import pytest

from backend.tools.skill_ops import read_skill
from backend.agent.agent_runner import current_session_context


@pytest.fixture(autouse=True)
def cleanup_context():
    yield
    current_session_context.clear()


@pytest.fixture
def mock_skill(tmp_path):
    """Create a temporary skill with {{SESSION_ID}} in instructions."""
    skill_dir = tmp_path / "test_skill"
    skill_dir.mkdir()
    (skill_dir / "metadata.yaml").write_text("name: test_skill\ndescription: test\ntags: []\n")
    (skill_dir / "instructions.md").write_text(
        "Run: script --session-id {{SESSION_ID}} --files {{SKILL_DIR}}/data"
    )
    return tmp_path


class TestSessionIdTemplate:
    def test_session_id_replaced_when_set(self, mock_skill):
        current_session_context.set("abc-123")
        with patch("backend.tools.skill_ops._get_skills_dir", return_value=mock_skill):
            result = read_skill("test_skill")
        assert "abc-123" in result
        assert "{{SESSION_ID}}" not in result

    def test_session_id_empty_when_not_set(self, mock_skill):
        # No session context set
        with patch("backend.tools.skill_ops._get_skills_dir", return_value=mock_skill):
            result = read_skill("test_skill")
        assert "{{SESSION_ID}}" not in result
        assert "--session-id " in result  # replaced with empty string

    def test_skill_dir_still_replaced(self, mock_skill):
        current_session_context.set("xyz-789")
        with patch("backend.tools.skill_ops._get_skills_dir", return_value=mock_skill):
            result = read_skill("test_skill")
        skill_dir_str = str(mock_skill / "test_skill")
        assert skill_dir_str in result
        assert "{{SKILL_DIR}}" not in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.sglang/bin/python -m pytest tests/test_skill_session_id.py -v`
Expected: FAIL — `{{SESSION_ID}}` still in output

- [ ] **Step 3: Modify `read_skill()` in `skill_ops.py`**

In `backend/tools/skill_ops.py`, after line 95 (`content = content.replace("{{SKILL_DIR}}", str(skill_dir))`), add:

```python
    # 将 {{SESSION_ID}} 占位符替换为当前会话 ID
    try:
        from backend.agent.agent_runner import current_session_context
        _session_id = current_session_context.get() or ""
        content = content.replace("{{SESSION_ID}}", _session_id)
    except Exception:
        content = content.replace("{{SESSION_ID}}", "")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.sglang/bin/python -m pytest tests/test_skill_session_id.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/tools/skill_ops.py tests/test_skill_session_id.py
git commit -m "feat: add {{SESSION_ID}} template variable to read_skill()"
```

---

### Task 4: 修改 `write_memory_to_messages()` — 文件 IPC 读取

**Files:**
- Modify: `backend/agent/agent_runner.py:681-703`
- Test: `tests/test_write_memory_images.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_write_memory_images.py
"""Tests for file-based image IPC in write_memory_to_messages()."""

import base64
import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from PIL import Image as PILImage

from backend.agent.agent_runner import current_session_context


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
    import uuid

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
    import shutil
    d = Path(f"/tmp/agent_images/{session_id}")
    if d.exists():
        shutil.rmtree(d)


class TestFileBasedImageIPC:
    def test_json_files_consumed_and_deleted(self):
        sid = "test-ipc-consume"
        current_session_context.set(sid)
        try:
            json_file = _create_image_json(sid, [(100, 100)])
            assert json_file.exists()

            # Import and call the function that reads images
            from backend.agent.agent_runner import _read_and_consume_tool_images
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
        sid = "test-ipc-multi"
        current_session_context.set(sid)
        try:
            _create_image_json(sid, [(100, 100), (200, 200)])
            from backend.agent.agent_runner import _read_and_consume_tool_images
            images = _read_and_consume_tool_images(sid)
            assert len(images) == 2
        finally:
            _cleanup_dir(sid)

    def test_no_json_files_returns_empty(self):
        sid = "test-ipc-empty"
        current_session_context.set(sid)
        try:
            from backend.agent.agent_runner import _read_and_consume_tool_images
            images = _read_and_consume_tool_images(sid)
            assert images == []
        finally:
            _cleanup_dir(sid)

    def test_corrupted_json_skipped(self):
        sid = "test-ipc-corrupt"
        current_session_context.set(sid)
        try:
            images_dir = Path(f"/tmp/agent_images/{sid}")
            images_dir.mkdir(parents=True, exist_ok=True)
            corrupt_file = images_dir / "corrupt.json"
            corrupt_file.write_text("not valid json {{{")

            from backend.agent.agent_runner import _read_and_consume_tool_images
            images = _read_and_consume_tool_images(sid)
            assert images == []
            # Corrupt file should still be deleted
            assert not corrupt_file.exists()
        finally:
            _cleanup_dir(sid)

    def test_empty_directory_cleaned_up(self):
        sid = "test-ipc-cleanup"
        current_session_context.set(sid)
        try:
            json_file = _create_image_json(sid, [(50, 50)])
            images_dir = json_file.parent

            from backend.agent.agent_runner import _read_and_consume_tool_images
            _read_and_consume_tool_images(sid)

            # Directory should be removed after consuming all files
            assert not images_dir.exists()
        finally:
            _cleanup_dir(sid)

    def test_no_session_id_returns_empty(self):
        from backend.agent.agent_runner import _read_and_consume_tool_images
        images = _read_and_consume_tool_images(None)
        assert images == []

    def test_pre_encoded_b64_is_valid(self):
        sid = "test-ipc-b64"
        current_session_context.set(sid)
        try:
            _create_image_json(sid, [(100, 100)])
            from backend.agent.agent_runner import _read_and_consume_tool_images
            images = _read_and_consume_tool_images(sid)
            img = images[0]
            # Verify b64 decodes to valid JPEG
            raw = base64.b64decode(img._pre_encoded_b64)
            reopened = PILImage.open(io.BytesIO(raw))
            assert reopened.size == (100, 100)
        finally:
            _cleanup_dir(sid)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.sglang/bin/python -m pytest tests/test_write_memory_images.py -v`
Expected: FAIL — `_read_and_consume_tool_images` does not exist

- [ ] **Step 3: Implement `_read_and_consume_tool_images()` helper function**

Add this function in `backend/agent/agent_runner.py` (before the `SmartToolCallingAgent` class, around line 115):

```python
def _read_and_consume_tool_images(session_id: str | None) -> list:
    """从文件 IPC 读取并消费工具加载的图片。

    读取 /tmp/agent_images/<session_id>/*.json，解码 base64 为 PIL Image
    （带 _pre_encoded_b64/_pre_encoded_mime 属性），删除已消费的 JSON 文件，
    清理空目录。

    Args:
        session_id: 会话 ID，为 None 时返回空列表

    Returns:
        PIL Image 对象列表
    """
    if not session_id:
        return []

    import base64 as _b64
    import io as _io
    from pathlib import Path as _Path
    from PIL import Image as _PILImage

    images_dir = _Path(f"/tmp/agent_images/{session_id}")
    if not images_dir.exists():
        return []

    tool_images = []
    consumed_files = []

    for json_file in sorted(images_dir.glob("*.json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            for img_data in data.get("images", []):
                b64_str = img_data["base64"]
                mime = img_data.get("mime", "image/jpeg")
                raw_bytes = _b64.b64decode(b64_str)
                pil_img = _PILImage.open(_io.BytesIO(raw_bytes))
                pil_img.load()
                pil_img._pre_encoded_b64 = b64_str  # type: ignore[attr-defined]
                pil_img._pre_encoded_mime = mime  # type: ignore[attr-defined]
                tool_images.append(pil_img)
            consumed_files.append(json_file)
        except Exception as e:
            logger.warning(f"读取图片 JSON 失败: {json_file} -> {e}")
            consumed_files.append(json_file)

    for f in consumed_files:
        try:
            f.unlink()
        except OSError:
            pass

    try:
        if images_dir.exists() and not any(images_dir.iterdir()):
            images_dir.rmdir()
    except OSError:
        pass

    return tool_images
```

- [ ] **Step 4: Replace image injection in `write_memory_to_messages()`**

Replace lines 681-703 in `agent_runner.py`:

Old:
```python
        # ── 注入工具加载的图片（load_images 工具存入的缓冲区） ──
        session_id = current_session_context.get()
        if session_id:
            tool_images = get_and_clear_tool_images(session_id)
            if tool_images:
                ...
```

New:
```python
        # ── 注入工具加载的图片（从文件 IPC 读取） ──
        session_id = current_session_context.get()
        tool_images = _read_and_consume_tool_images(session_id)
        if tool_images:
            image_content = []
            for img in tool_images:
                image_content.append({"type": "image", "image": img})
            image_content.append(
                {
                    "type": "text",
                    "text": "[工具加载的图片已注入，请直接分析这些图片内容]",
                }
            )
            messages.append(
                ChatMessage(
                    role=MessageRole.USER,
                    content=image_content,
                )
            )
            logger.info(
                f"write_memory_to_messages: 注入 {len(tool_images)} 张工具加载图片"
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.sglang/bin/python -m pytest tests/test_write_memory_images.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add backend/agent/agent_runner.py tests/test_write_memory_images.py
git commit -m "feat: replace in-memory image buffer with file-based IPC in write_memory_to_messages()"
```

---

### Task 5: 移除 `load_images` 工具并清理 import

**Files:**
- Modify: `backend/agent/agent_runner.py:140, 552, 565`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_get_tools_no_load_images.py
"""Test that _get_tools() no longer includes load_images."""

from unittest.mock import patch, MagicMock
from backend.agent.agent_runner import _get_tools


class TestGetToolsExclusion:
    def test_load_images_not_in_tools(self):
        config = MagicMock()
        config.agent.python_authorized_imports = []
        config.agent.enable_wikipedia = False
        config.agent.mcp_servers = None

        tools = _get_tools(config)
        tool_names = [t.name for t in tools]
        assert "load_images" not in tool_names

    def test_standard_tools_still_present(self):
        config = MagicMock()
        config.agent.python_authorized_imports = []
        config.agent.enable_wikipedia = False
        config.agent.mcp_servers = None

        tools = _get_tools(config)
        tool_names = [t.name for t in tools]
        assert "read_file" in tool_names
        assert "exec_command" in tool_names
        assert "list_skills" in tool_names
        assert "read_skill" in tool_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.sglang/bin/python -m pytest tests/test_get_tools_no_load_images.py -v`
Expected: FAIL — `load_images` is still in the tools list

- [ ] **Step 3: Remove load_images from `_get_tools()` and clean up import**

In `backend/agent/agent_runner.py`:

1. Delete line 140: `from backend.tools.image_ops import load_images, get_and_clear_tool_images`
2. Delete line 552: `load_images_tool = _create_tool_wrapper(load_images, "load_images")`
3. Delete line 565: `load_images_tool,` from tools list

- [ ] **Step 4: Run test to verify it passes**

Run: `.sglang/bin/python -m pytest tests/test_get_tools_no_load_images.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agent/agent_runner.py tests/test_get_tools_no_load_images.py
git commit -m "refactor: remove load_images tool from _get_tools() and clean up imports"
```

---

### Task 6: 删除 `image_ops.py` 和旧测试

**Files:**
- Delete: `backend/tools/image_ops.py`
- Delete: `tests/test_image_ops.py`

- [ ] **Step 1: Delete `backend/tools/image_ops.py`**

```bash
git rm backend/tools/image_ops.py
```

- [ ] **Step 2: Delete `tests/test_image_ops.py`**

```bash
git rm tests/test_image_ops.py
```

- [ ] **Step 3: Verify no remaining references to `image_ops`**

```bash
grep -rn "image_ops" backend/ tests/ config/ --include="*.py" --include="*.yaml"
```

Expected: No matches (or only in design docs which are fine)

- [ ] **Step 4: Run full test suite to verify nothing is broken**

Run: `.sglang/bin/python -m pytest tests/ -v`
Expected: All tests pass (count reduced by 18 from old tests, increased by new tests)

- [ ] **Step 5: Commit**

```bash
git commit -m "refactor: remove image_ops.py and its 18 tests (replaced by skill + file IPC)"
```

---

### Task 7: 添加会话清理逻辑

**Files:**
- Modify: `backend/agent/agent_runner.py` (SessionStore class, around line 996-1009)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_write_memory_images.py`:

```python
class TestSessionImageCleanup:
    def test_cleanup_session_images_removes_directory(self):
        sid = "test-cleanup-session"
        images_dir = Path(f"/tmp/agent_images/{sid}")
        images_dir.mkdir(parents=True, exist_ok=True)
        (images_dir / "test.json").write_text('{"images":[]}')

        from backend.agent.agent_runner import _cleanup_session_images
        _cleanup_session_images(sid)
        assert not images_dir.exists()

    def test_cleanup_session_images_nonexistent_dir(self):
        from backend.agent.agent_runner import _cleanup_session_images
        # Should not raise
        _cleanup_session_images("nonexistent-session-id")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.sglang/bin/python -m pytest tests/test_write_memory_images.py::TestSessionImageCleanup -v`
Expected: FAIL — `_cleanup_session_images` does not exist

- [ ] **Step 3: Add `_cleanup_session_images()` function and wire into SessionStore**

Add function to `backend/agent/agent_runner.py` (near `_read_and_consume_tool_images`):

```python
def _cleanup_session_images(session_id: str) -> None:
    """清理会话的图片 IPC 文件目录。"""
    import shutil
    from pathlib import Path as _Path

    images_dir = _Path(f"/tmp/agent_images/{session_id}")
    if images_dir.exists():
        try:
            shutil.rmtree(images_dir)
            logger.info(f"已清理会话图片目录: {images_dir}")
        except OSError as e:
            logger.warning(f"清理会话图片目录失败: {images_dir} -> {e}")
```

In `SessionStore.delete()` (line 996-997), add cleanup call:

```python
    def delete(self, session_id: str):
        self._sessions.pop(session_id, None)
        _cleanup_session_images(session_id)
```

In `SessionStore.cleanup_expired()` (line 999-1009), add cleanup call in the loop:

```python
        for sid in expired:
            del self._sessions[sid]
            _cleanup_session_images(sid)
            logger.info(f"Session {sid} 已过期清理")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.sglang/bin/python -m pytest tests/test_write_memory_images.py::TestSessionImageCleanup -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agent/agent_runner.py tests/test_write_memory_images.py
git commit -m "feat: add session image directory cleanup on session delete/expire"
```

---

### Task 8: 更新 `vlm_instructions`

**Files:**
- Modify: `config/config-agent-prompts.yaml:71`

- [ ] **Step 1: Update vlm_instructions line 71**

Replace:
```yaml
  当用户要求你查看或分析本地磁盘上的图片文件时，使用 load_images 工具加载这些图片，加载成功后你将在下一步直接看到图片内容并进行分析。
```

With:
```yaml
  当用户要求你查看或分析本地磁盘上的图片文件时，先调用 list_skills 查找图片加载相关技能，然后调用 read_skill 阅读操作步骤并按指示执行。加载成功后你将在下一步直接看到图片内容并进行分析。
```

- [ ] **Step 2: Run full test suite**

Run: `.sglang/bin/python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add config/config-agent-prompts.yaml
git commit -m "refactor: update vlm_instructions to reference skill system instead of load_images tool"
```

---

### Task 9: 最终验证

- [ ] **Step 1: Run the full test suite**

```bash
.sglang/bin/python -m pytest tests/ -v
```

Expected: All tests pass. Count should be approximately: 66 - 18 (old image_ops) + N (new tests) = ~60+ tests

- [ ] **Step 2: Verify no remaining references to old code**

```bash
grep -rn "from backend.tools.image_ops" backend/ tests/
grep -rn "store_tool_images\|get_and_clear_tool_images" backend/ tests/
grep -rn "_tool_loaded_images" backend/ tests/
```

Expected: No matches

- [ ] **Step 3: Verify skill files exist and are well-formed**

```bash
ls -la backend/skills/load_local_images/
cat backend/skills/load_local_images/metadata.yaml
```

- [ ] **Step 4: Build frontend to verify no breakage**

```bash
cd frontend && npm run build
```

Expected: Build succeeds (frontend has no dependency on backend image_ops)
