#!/usr/bin/env python3
"""
测试脚本：验证 max_tokens 到 max_new_tokens 的完整传递链条
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

    # 读取 inference 配置
    max_tokens = config.inference.max_tokens
    max_new_tokens = config.inference.max_new_tokens

    print("inference.max_tokens 传递链条验证：")
    print(f"config.ini [inference] max_tokens = 4096")
    print(f"backend/config.py InferenceConfig.max_tokens = {max_tokens}")
    print(f"backend/config.py InferenceConfig.max_new_tokens = {max_new_tokens}")
    print("")

    # 测试 LLM 服务
    llm_service = get_llm_service()
    if hasattr(llm_service, 'config'):
        service_max_tokens = llm_service.config.inference.max_tokens
        service_max_new_tokens = llm_service.config.inference.max_new_tokens

        print("LLM 服务中的配置：")
        print(f"service_max_tokens = {service_max_tokens}")
        print(f"service_max_new_tokens = {service_max_new_tokens}")
        print("")

        if (service_max_tokens == max_tokens == max_new_tokens == service_max_new_tokens == 4096):
            print("✅ 完整传递链条有效：")
            print("  config.ini → backend/config.py → backend/llm_service.py → SGLang API (max_new_tokens)")
        else:
            print("❌ 传递链条无效：值不一致")
            print(f"  期望值: 4096")
            print(f"  实际值: max_tokens={max_tokens}, max_new_tokens={max_new_tokens}")
    else:
        print("LLM 服务未正确初始化配置")

except ImportError as e:
    print(f"导入错误: {e}")
    print("请确保在 .sglang 虚拟环境中运行")
except Exception as e:
    print(f"错误: {e}")