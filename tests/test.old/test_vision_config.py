#!/usr/bin/env python3
"""
视觉配置测试脚本

测试视觉模型配置加载是否正确。
"""

import sys
import os

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import get_config


def test_vision_config():
    """测试视觉配置加载"""
    print("=" * 60)
    print("测试: 视觉模型配置加载")
    print("=" * 60)

    config = get_config()

    # 测试基础配置
    print(f"\n[基础配置]")
    print(f"  model_name: {config.model.model_name}")
    print(f"  model_type: {config.model.model_type}")

    # 测试视觉配置
    print(f"\n[视觉配置]")
    print(f"  max_image_size: {config.model.max_image_size}")
    print(f"  supported_formats: {config.model.supported_formats}")
    print(f"  max_image_tokens: {config.model.max_image_tokens}")

    # 断言
    assert config.model.model_type in ["text", "vision"], (
        "model_type 必须是 text 或 vision"
    )
    assert config.model.max_image_size > 0, "max_image_size 必须大于 0"
    assert config.model.supported_formats is not None, "supported_formats 不能为空"
    assert config.model.max_image_tokens > 0, "max_image_tokens 必须大于 0"

    print("\n✓ 所有测试通过!")
    return True


def test_vision_enabled_detection():
    """测试视觉模式检测"""
    print("\n" + "=" * 60)
    print("测试: 视觉模式检测")
    print("=" * 60)

    config = get_config()

    is_vision = config.model.model_type == "vision"
    print(f"\n[检测结果]")
    print(f"  当前模型类型: {config.model.model_type}")
    print(f"  是否为视觉模式: {is_vision}")

    print("\n✓ 视觉模式检测测试通过!")
    return True


if __name__ == "__main__":
    try:
        test_vision_config()
        test_vision_enabled_detection()
        print("\n" + "=" * 60)
        print("所有测试完成!")
        print("=" * 60)
    except Exception as e:
        print(f"\n✗ 测试失败: {e}")
        sys.exit(1)
