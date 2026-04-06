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

# Import config to get camera_max_dimension
try:
    sys.path.append(os.path.join(os.path.dirname(__file__), "../../../backend"))
    from config import get_config

    config = get_config()
    DEFAULT_MAX_DIM = config.model.camera_max_dimension or 1280
except Exception:
    # Fallback if config loading fails
    DEFAULT_MAX_DIM = 1280

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
        # 无变换的 JPEG：直接使用原始文件字节，避免重编码
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
    parser.add_argument(
        "--max-dim",
        type=int,
        default=DEFAULT_MAX_DIM,
        help=f"图片最大边长像素，默认 {DEFAULT_MAX_DIM}",
    )
    args = parser.parse_args()

    raw_paths = [p.strip() for p in args.files.split(",") if p.strip()]
    if not raw_paths:
        print("错误: 未提供任何文件路径")
        sys.exit(1)
    if len(raw_paths) > MAX_IMAGE_COUNT:
        print(
            f"错误: 单次最多加载 {MAX_IMAGE_COUNT} 张图片，收到 {len(raw_paths)} 个路径"
        )
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
