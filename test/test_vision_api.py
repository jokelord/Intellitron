#!/usr/bin/env python3
"""
视觉聊天 API 测试脚本

测试视觉聊天 API 端点是否正常工作。
"""

import sys
import os
import json

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_config_endpoint():
    """测试配置端点返回视觉配置"""
    print("=" * 60)
    print("测试: 配置端点")
    print("=" * 60)

    try:
        import httpx

        # 调用配置端点
        response = httpx.get("http://localhost:8000/v1/config", timeout=10)

        if response.status_code != 200:
            print(f"✗ 配置端点请求失败: {response.status_code}")
            return False

        config = response.json()

        print(f"\n[配置响应]")
        print(f"  model.model_type: {config.get('model', {}).get('model_type')}")
        print(f"  vision.enabled: {config.get('vision', {}).get('enabled')}")
        print(
            f"  vision.max_image_size: {config.get('vision', {}).get('max_image_size')}"
        )

        # 验证响应包含视觉配置
        assert "vision" in config, "响应缺少 vision 字段"
        assert "model_type" in config.get("model", {}), "响应缺少 model.model_type 字段"

        print("\n✓ 配置端点测试通过!")
        return True

    except Exception as e:
        print(f"✗ 测试失败: {e}")
        return False


def test_multimodal_message():
    """测试多模态消息格式"""
    print("\n" + "=" * 60)
    print("测试: 多模态消息格式")
    print("=" * 60)

    # 测试消息构建
    from backend.llm_service import Message

    # 文本消息
    text_msg = Message(role="user", content="你好")
    print(f"\n[文本消息]")
    print(f"  role: {text_msg.role}")
    print(f"  content: {text_msg.content}")

    # 多模态消息
    multimodal_content = [
        {"type": "text", "text": "描述这张图片"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}},
    ]
    multimodal_msg = Message(role="user", content=multimodal_content)
    print(f"\n[多模态消息]")
    print(f"  role: {multimodal_msg.role}")
    print(f"  content: {json.dumps(multimodal_msg.content, indent=2)}")

    # 验证消息类型
    assert isinstance(multimodal_msg.content, list), "多模态消息 content 应该是列表"
    assert multimodal_msg.content[0]["type"] == "text"
    assert multimodal_msg.content[1]["type"] == "image_url"

    print("\n✓ 多模态消息格式测试通过!")
    return True


if __name__ == "__main__":
    print("\n注意: 测试配置端点需要后端服务运行在 localhost:8000")
    print("如果后端未运行，将跳过部分测试\n")

    all_passed = True

    # 测试多模态消息格式
    if not test_multimodal_message():
        all_passed = False

    # 测试配置端点
    if not test_config_endpoint():
        all_passed = False

    if all_passed:
        print("\n" + "=" * 60)
        print("所有测试完成!")
        print("=" * 60)
    else:
        print("\n" + "=" * 60)
        print("部分测试失败!")
        print("=" * 60)
        sys.exit(1)
