#!/usr/bin/env python3
"""
测试脚本：验证 inference.max_tokens 参数传递
"""

import sys
import os
from pathlib import Path

# 确保项目根目录在 Python 路径中
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

try:
    from backend.config import get_config
    from backend.llm_service import get_llm_service

    # 获取配置
    config = get_config()

    # 读取 inference.max_tokens
    max_tokens = config.inference.max_tokens

    print("inference.max_tokens 解析结果：")
    print(f"max_tokens = {max_tokens}")
    print("")

    # 测试 LLM 服务是否能获取到配置
    llm_service = get_llm_service()
    if hasattr(llm_service, 'config'):
        service_max_tokens = llm_service.config.inference.max_tokens
        print("LLM 服务中的 max_tokens：")
        print(f"service_max_tokens = {service_max_tokens}")
        print("")
        if service_max_tokens == max_tokens:
            print("✅ 参数传递有效：配置值与服务值一致")
        else:
            print("❌ 参数传递无效：配置值与服务值不一致")
    else:
        print("LLM 服务未正确初始化配置")

except ImportError as e:
    print(f"导入错误: {e}")
    print("请确保在 .sglang 虚拟环境中运行")
except Exception as e:
    print(f"错误: {e}")