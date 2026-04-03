"""
图片加载工具 (Image Operations)

允许 Agent 从本地文件系统加载图片，使 VLM 能在对话中分析图片内容。
加载的图片经由 store_tool_images → get_and_clear_tool_images 通道注入到
write_memory_to_messages 构造的消息列表中，供 VLM 在下一步推理时可见。
"""

import base64
import io
import logging
import os
import threading

from PIL import Image as PILImage

logger = logging.getLogger(__name__)

# ── 线程安全的会话级图片缓冲区 ──────────────────────────────
_tool_loaded_images: dict[str, list] = {}
_buffer_lock = threading.Lock()


def store_tool_images(session_id: str, images: list) -> None:
    """将 PIL 图片列表存入会话缓冲区（追加模式）。"""
    with _buffer_lock:
        if session_id not in _tool_loaded_images:
            _tool_loaded_images[session_id] = []
        _tool_loaded_images[session_id].extend(images)
        logger.info(
            f"store_tool_images: session={session_id}, "
            f"新增 {len(images)} 张, 缓冲区共 {len(_tool_loaded_images[session_id])} 张"
        )


def get_and_clear_tool_images(session_id: str) -> list:
    """取出并清空会话缓冲区中的图片列表。"""
    with _buffer_lock:
        images = _tool_loaded_images.pop(session_id, [])
        if images:
            logger.info(
                f"get_and_clear_tool_images: session={session_id}, 取出 {len(images)} 张"
            )
        return images


# ── 支持的图片扩展名 ─────────────────────────────────────────
_SUPPORTED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

# ── 文件大小上限 20 MB ───────────────────────────────────────
_MAX_FILE_SIZE = 20 * 1024 * 1024

# ── 单次最多加载 9 张 ────────────────────────────────────────
_MAX_IMAGE_COUNT = 9


def load_images(
    file_paths: str, max_dimension: int = 1280, session_id: str = ""
) -> str:
    """加载本地图片文件，使 VLM 能在对话中直接看到并分析这些图片。

    Args:
        file_paths: 逗号分隔的文件路径列表。支持 ~ 和环境变量。例如 "/tmp/a.png, ~/b.jpg"
        max_dimension: 图片最大边长（像素），超过则等比缩放。默认 1280
        session_id: 当前会话 ID（由框架自动注入，工具调用时无需传入）

    Returns:
        加载结果摘要文本，包含每张图片的状态信息
    """
    if not session_id:
        # 兼容回退：尝试从 thread-local 获取
        try:
            from backend.agent.agent_runner import current_session_context

            session_id = current_session_context.get() or ""
        except Exception:
            pass
    if not session_id:
        return "错误: 无法获取当前会话 ID，图片加载失败"

    # 解析逗号分隔的路径
    raw_paths = [p.strip() for p in file_paths.split(",") if p.strip()]
    if not raw_paths:
        return "错误: 未提供任何文件路径"
    if len(raw_paths) > _MAX_IMAGE_COUNT:
        return f"错误: 单次最多加载 {_MAX_IMAGE_COUNT} 张图片，收到 {len(raw_paths)} 个路径"

    loaded_images: list = []
    results: list[str] = []

    for idx, raw_path in enumerate(raw_paths, 1):
        # 路径展开
        expanded = os.path.expanduser(os.path.expandvars(raw_path))

        # 文件存在性检查
        if not os.path.isfile(expanded):
            results.append(f"[{idx}] {raw_path}: 文件不存在")
            continue

        # 扩展名检查
        ext = os.path.splitext(expanded)[1].lstrip(".").lower()
        if ext not in _SUPPORTED_EXTENSIONS:
            results.append(
                f"[{idx}] {raw_path}: 不支持的格式 '{ext}' "
                f"(支持: {', '.join(sorted(_SUPPORTED_EXTENSIONS))})"
            )
            continue

        # 文件大小检查
        file_size = os.path.getsize(expanded)
        if file_size > _MAX_FILE_SIZE:
            results.append(
                f"[{idx}] {raw_path}: 文件过大 "
                f"({file_size / 1024 / 1024:.1f}MB > {_MAX_FILE_SIZE / 1024 / 1024:.0f}MB)"
            )
            continue

        try:
            pil_img = PILImage.open(expanded)
            pil_img.load()  # 确保像素已加载到内存
            original_pil_fmt = pil_img.format  # e.g. "JPEG", "PNG"
            detected_fmt = (pil_img.format or "").lower()  # e.g. "jpeg", "png"
            w, h = pil_img.size
            original_w, original_h = w, h

            # ── 按 max_dimension 缩放 ──
            _resized = False
            if max_dimension > 0 and (w > max_dimension or h > max_dimension):
                if w > h:
                    nw, nh = max_dimension, int(h * max_dimension / w)
                else:
                    nh, nw = max_dimension, int(w * max_dimension / h)
                pil_img = pil_img.resize((nw, nh), PILImage.Resampling.LANCZOS)
                _resized = True
                logger.info(
                    f"load_images[{idx}]: 缩放 {w}x{h} → {nw}x{nh} (max_dim={max_dimension})"
                )
                w, h = nw, nh

            # ── 透明通道转换 ──
            _orig_mode = pil_img.mode
            _color_changed = False
            if pil_img.mode == "RGBA":
                pil_img.load()
                _bg = PILImage.new("RGB", pil_img.size, (255, 255, 255))
                _bg.paste(pil_img, mask=pil_img.split()[3])
                pil_img = _bg
                _color_changed = True
                logger.info(f"load_images[{idx}]: RGBA→RGB (白色背景合成)")
            elif pil_img.mode in ("P", "PA", "LA"):
                pil_img.load()
                _rgba_tmp = pil_img.convert("RGBA")
                _bg = PILImage.new("RGB", _rgba_tmp.size, (255, 255, 255))
                _bg.paste(_rgba_tmp, mask=_rgba_tmp.split()[3])
                pil_img = _bg
                _color_changed = True
                logger.info(f"load_images[{idx}]: {_orig_mode}→RGB (白色背景合成)")
            elif pil_img.mode not in ("RGB", "L"):
                pil_img = pil_img.convert("RGB")
                _color_changed = True
                logger.info(f"load_images[{idx}]: {_orig_mode}→RGB")

            # 将 format 挂回 PIL 对象（resize/convert 后会置 None）
            pil_img.format = original_pil_fmt  # type: ignore[attr-defined]

            # ── 预编码优化 ──
            if not _resized and not _color_changed and detected_fmt == "jpeg":
                # 无任何变换，直接使用原始文件的 JPEG 字节
                with open(expanded, "rb") as f:
                    raw_bytes = f.read()
                pil_img._pre_encoded_b64 = base64.b64encode(raw_bytes).decode()  # type: ignore[attr-defined]
                pil_img._pre_encoded_mime = "image/jpeg"  # type: ignore[attr-defined]
                logger.info(
                    f"load_images[{idx}]: ⚡ 使用原始 JPEG 字节（无变换）, "
                    f"base64 {len(pil_img._pre_encoded_b64)} chars"  # type: ignore[attr-defined]
                )
            else:
                # 经过变换（缩放/色彩/非JPEG），重新编码为 JPEG
                _enc_buf = io.BytesIO()
                _save_img = pil_img
                if _save_img.mode not in ("RGB", "L"):
                    _save_img = _save_img.convert("RGB")
                _save_img.save(_enc_buf, format="JPEG", quality=90)
                pil_img._pre_encoded_b64 = base64.b64encode(  # type: ignore[attr-defined]
                    _enc_buf.getvalue()
                ).decode()
                pil_img._pre_encoded_mime = "image/jpeg"  # type: ignore[attr-defined]
                reason = (
                    "缩放"
                    if _resized
                    else ("色彩转换" if _color_changed else "非JPEG格式")
                )
                logger.info(
                    f"load_images[{idx}]: 预编码 JPEG {len(_enc_buf.getvalue())} bytes, "
                    f"base64 {len(pil_img._pre_encoded_b64)} chars (原因: {reason})"  # type: ignore[attr-defined]
                )

            loaded_images.append(pil_img)

            # 构建摘要行
            size_info = f"{w}x{h}"
            if _resized:
                size_info = f"{original_w}x{original_h}→{w}x{h}"
            results.append(f"[{idx}] {raw_path}: 已加载 ({size_info}, {pil_img.mode})")

        except Exception as e:
            results.append(f"[{idx}] {raw_path}: 加载失败 ({e})")
            logger.warning(f"load_images[{idx}] 加载失败: {expanded} -> {e}")

    # 存入会话缓冲区
    if loaded_images:
        store_tool_images(session_id, loaded_images)

    # 汇总
    success_count = len(loaded_images)
    total_count = len(raw_paths)
    header = f"图片加载完成: {success_count}/{total_count} 张成功"
    return header + "\n" + "\n".join(results)
