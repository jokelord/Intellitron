"""
图片透传诊断测试 — 验证 SGLang 是否正确接收和解析图片数据。

使用方法：
  python3 test/test_image_passthrough.py <image_path>

此脚本绕过所有后端逻辑，直接：
1. 读取本地图片文件
2. 编码为 base64 data URL
3. 发送到 SGLang /chat/completions API
4. 打印模型对图片的描述

如果模型在这里也无法正确识别图片，说明是 SGLang/模型层面的问题；
如果这里识别正确，说明是后端数据流的问题。
"""
import base64
import io
import json
import sys

import httpx

SGLANG_URL = "http://127.0.0.1:30000"
MODEL_NAME = "default"


def load_and_encode_image(image_path: str) -> tuple[str, int, int, str]:
    """加载图片并编码为 base64 data URL，返回 (data_url, width, height, info)"""
    from PIL import Image

    with open(image_path, "rb") as f:
        raw_bytes = f.read()

    img = Image.open(io.BytesIO(raw_bytes))
    w, h = img.size
    fmt = (img.format or "UNKNOWN").upper()
    mode = img.mode
    info = f"{w}x{h} {mode} {fmt} ({len(raw_bytes)} bytes)"

    # 如果是 RGBA，合成白色背景后转 JPEG（与后端逻辑一致）
    if mode == "RGBA":
        img.load()
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        encoded_bytes = buf.getvalue()
        mime = "image/jpeg"
        info += f" → JPEG RGB {len(encoded_bytes)} bytes (RGBA→RGB 白色背景)"
    elif mode in ("P", "PA", "LA"):
        img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        encoded_bytes = buf.getvalue()
        mime = "image/jpeg"
        info += f" → JPEG RGB {len(encoded_bytes)} bytes"
    elif fmt == "JPEG":
        # JPEG 直接使用原始字节
        encoded_bytes = raw_bytes
        mime = "image/jpeg"
        info += " (原始 JPEG 字节直传)"
    else:
        # 其他格式转 JPEG
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        encoded_bytes = buf.getvalue()
        mime = "image/jpeg"
        info += f" → JPEG {len(encoded_bytes)} bytes"

    b64 = base64.b64encode(encoded_bytes).decode()
    data_url = f"data:{mime};base64,{b64}"
    return data_url, w, h, info


def test_direct_sglang(image_path: str):
    """直接向 SGLang 发送图片，验证图片是否被正确解析"""
    print(f"=== 图片透传诊断测试 ===\n")
    print(f"图片路径: {image_path}")

    data_url, w, h, info = load_and_encode_image(image_path)
    print(f"图片信息: {info}")
    print(f"Base64 长度: {len(data_url)} chars")

    # 同时保存编码后的图片到临时文件，供人工比对
    header, b64_str = data_url.split(",", 1)
    img_bytes = base64.b64decode(b64_str)
    debug_path = "/tmp/sglang_debug/test_direct.jpg"
    import os
    os.makedirs("/tmp/sglang_debug", exist_ok=True)
    with open(debug_path, "wb") as f:
        f.write(img_bytes)
    print(f"编码后图片已保存: {debug_path} ({len(img_bytes)} bytes)")

    # 构造请求
    request = {
        "model": MODEL_NAME,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": "请详细描述这张图片中的内容。"},
                ],
            }
        ],
        "max_new_tokens": 1024,
        "temperature": 0.3,
        "stream": False,
    }

    print(f"\n发送请求到 SGLang ({SGLANG_URL})...")

    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(
                f"{SGLANG_URL}/v1/chat/completions",
                json=request,
            )

        if response.status_code != 200:
            print(f"❌ SGLang 返回错误: {response.status_code}")
            print(response.text[:500])
            return

        result = response.json()
        choice = result.get("choices", [{}])[0]
        msg = choice.get("message", {})
        content = msg.get("content", "")
        reasoning = msg.get("reasoning_content", "")
        usage = result.get("usage", {})

        print(f"\n=== SGLang 返回 ===")
        if reasoning:
            print(f"[思考] {reasoning[:500]}")
        print(f"[回答] {content}")
        print(f"\nToken 使用: prompt={usage.get('prompt_tokens', '?')}, "
              f"completion={usage.get('completion_tokens', '?')}")

    except httpx.ConnectError:
        print(f"❌ 无法连接到 SGLang ({SGLANG_URL})")
        print("请确保 SGLang 服务已启动")
    except Exception as e:
        print(f"❌ 请求失败: {type(e).__name__}: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 test/test_image_passthrough.py <image_path>")
        print("示例: python3 test/test_image_passthrough.py /path/to/image.png")
        sys.exit(1)

    test_direct_sglang(sys.argv[1])
