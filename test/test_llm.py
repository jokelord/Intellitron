#!/usr/bin/env python3
"""
SGLang API 测试脚本

测试 SGLang 服务器是否正常工作，发送测试请求并验证响应。
"""

import argparse
import json
import sys
import time
import os
import configparser
from pathlib import Path
from typing import Optional

try:
    import httpx
except ImportError:
    print("错误: 需要安装 httpx 库")
    print("请运行: pip install httpx")
    sys.exit(1)


def get_config_value():
    """从配置文件读取服务器配置"""
    script_dir = Path(__file__).parent
    config_path = script_dir.parent / "config" / "config.ini"
    
    if config_path.exists():
        config = configparser.ConfigParser()
        config.read(config_path)
        
        if 'server' in config:
            host = config.get('server', 'host', fallback='localhost')
            port = config.get('server', 'port', fallback='8000')
            return f"http://{host}:{port}/v1"
    
    return "http://localhost:30000/v1"


class Colors:
    """终端颜色"""
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'
    BOLD = '\033[1m'


def print_section(title: str):
    """打印分隔标题"""
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'=' * 60}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{title}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'=' * 60}{Colors.RESET}\n")


def print_success(message: str):
    """打印成功消息"""
    print(f"{Colors.GREEN}✓ {message}{Colors.RESET}")


def print_error(message: str):
    """打印错误消息"""
    print(f"{Colors.RED}✗ {message}{Colors.RESET}")


def print_warning(message: str):
    """打印警告消息"""
    print(f"{Colors.YELLOW}⚠ {message}{Colors.RESET}")


def print_info(message: str):
    """打印信息消息"""
    print(f"{Colors.BLUE}ℹ {message}{Colors.RESET}")


def test_health_check(api_url: str, timeout: int = 10) -> bool:
    """
    测试服务器健康状态
    
    Args:
        api_url: API 基础 URL
        timeout: 超时时间（秒）
        
    Returns:
        bool: 测试是否通过
    """
    print_section("1. 健康检查")
    
    try:
        print_info(f"连接到: {api_url}")
        with httpx.Client(timeout=timeout) as client:
            # 1. 尝试根路径 /health (SGLang 标准)
            base_url = api_url
            if base_url.endswith("/v1"):
                base_url = base_url[:-3]
            
            try:
                response = client.get(f"{base_url}/health")
                if response.status_code == 200:
                    print_success(f"服务器健康检查通过 ({base_url}/health)")
                    return True
            except Exception:
                pass

            # 2. 尝试 API 路径 /health
            response = client.get(f"{api_url}/health")
            
            if response.status_code == 200:
                print_success("服务器健康检查通过")
                print(f"  响应: {response.text}")
                return True
            else:
                print_warning(f"健康检查返回非 200 状态码: {response.status_code}")
                return True  # SGLang 可能没有 /health 端点，继续测试
                
    except httpx.ConnectError:
        print_error(f"无法连接到 SGLang 服务器: {api_url}")
        print_error("请确保服务器已启动")
        return False
    except Exception as e:
        print_error(f"健康检查异常: {e}")
        return False


def test_models_endpoint(api_url: str, timeout: int = 10) -> Optional[str]:
    """
    测试模型列表端点
    
    Args:
        api_url: API 基础 URL
        timeout: 超时时间（秒）
        
    Returns:
        Optional[str]: 第一个可用的模型名称，如果失败则返回 None
    """
    print_section("2. 获取模型列表")
    
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{api_url}/models")
            
            if response.status_code == 200:
                models = response.json()
                print_success("成功获取模型列表")
                print(f"  响应: {json.dumps(models, indent=2, ensure_ascii=False)}")
                
                # 尝试提取模型名称
                if isinstance(models, dict) and "data" in models:
                    if models["data"]:
                        model_name = models["data"][0].get("id", "")
                        if model_name:
                            print_info(f"检测到模型: {model_name}")
                            return model_name
                        
                return None
            else:
                print_error(f"获取模型列表失败: {response.status_code}")
                print_error(f"  响应: {response.text}")
                return None
                
    except Exception as e:
        print_error(f"获取模型列表异常: {e}")
        return None


def test_chat_completion(api_url: str, model_name: Optional[str], timeout: int = 60) -> bool:
    """
    测试聊天补全端点（非流式）
    
    Args:
        api_url: API 基础 URL
        model_name: 模型名称
        timeout: 超时时间（秒）
        
    Returns:
        bool: 测试是否通过
    """
    print_section("3. 测试聊天补全（非流式）")
    
    # 如果没有模型名称，使用默认值
    if not model_name:
        model_name = "gpt-oss-120b-nvfp4"
        print_warning(f"使用默认模型名称: {model_name}")
    
    request_data = {
        "model": model_name,
        "messages": [
            {"role": "user", "content": "你好，请用一句话介绍你自己。"}
        ],
        "temperature": 0.7,
        "max_tokens": 100,
        "stream": False
    }
    
    print_info("发送请求...")
    print(f"  请求数据: {json.dumps(request_data, indent=2, ensure_ascii=False)}")
    
    try:
        start_time = time.time()
        
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{api_url}/chat/completions",
                json=request_data
            )
            
            elapsed = time.time() - start_time
            
            if response.status_code == 200:
                print_info("解析响应...")
                result = response.json()
                print_info(f"响应 JSON 长度: {len(json.dumps(result))}")
                print_success(f"聊天补全成功（耗时: {elapsed:.2f}s）")
                
                # 提取回复内容
                if "choices" in result and result["choices"]:
                    message = result["choices"][0].get("message", {})
                    content = message.get("content")
                    if content is None:
                        content = ""
                    reasoning_content = message.get("reasoning_content")
                    full_content = content
                    if reasoning_content:
                        full_content += f"\n\n推理过程:\n{reasoning_content}"
                    
                    if not full_content:
                        print_warning("回复内容为空 (content 和 reasoning_content 均为 None 或空)")
                        print(f"  完整响应: {json.dumps(result, indent=2, ensure_ascii=False)}")
                    else:
                        print(f"\n{Colors.BOLD}回复内容:{Colors.RESET}")
                        print(f"  {full_content}\n")
                    
                    # 显示 token 使用情况
                    if "usage" in result:
                        usage = result["usage"]
                        print_info(f"Token 使用:")
                        print(f"  输入: {usage.get('prompt_tokens', 0)}")
                        print(f"  输出: {usage.get('completion_tokens', 0)}")
                        print(f"  总计: {usage.get('total_tokens', 0)}")
                    
                    return True
                else:
                    print_error("响应格式异常，未找到 choices")
                    print(f"  完整响应: {json.dumps(result, indent=2, ensure_ascii=False)}")
                    return False
            else:
                print_error(f"聊天补全失败: {response.status_code}")
                print_error(f"  响应: {response.text}")
                return False
                
    except httpx.ReadTimeout:
        print_error(f"请求超时（>{timeout}秒）")
        print_warning("模型推理时间较长，可以尝试增加超时时间")
        return False
    except Exception as e:
        print_error(f"聊天补全异常: {e}")
        return False


def test_chat_completion_stream(api_url: str, model_name: Optional[str], timeout: int = 60) -> bool:
    """
    测试聊天补全端点（流式）
    
    Args:
        api_url: API 基础 URL
        model_name: 模型名称
        timeout: 超时时间（秒）
        
    Returns:
        bool: 测试是否通过
    """
    print_section("4. 测试聊天补全（流式）")
    
    # 如果没有模型名称，使用默认值
    if not model_name:
        model_name = "gpt-oss-120b-nvfp4"
        print_warning(f"使用默认模型名称: {model_name}")
    
    request_data = {
        "model": model_name,
        "messages": [
            {"role": "user", "content": "数到10，每个数字单独一行。"}
        ],
        "temperature": 0.1,
        "max_tokens": 50,
        "stream": True
    }
    
    print_info("发送流式请求...")
    print(f"  请求数据: {json.dumps(request_data, indent=2, ensure_ascii=False)}")
    
    try:
        start_time = time.time()
        chunks_received = 0
        full_content = ""
        
        with httpx.Client(timeout=timeout) as client:
            with client.stream(
                "POST",
                f"{api_url}/chat/completions",
                json=request_data
            ) as response:
                
                if response.status_code != 200:
                    print_error(f"流式请求失败: {response.status_code}")
                    return False
                
                print(f"\n{Colors.BOLD}流式输出:{Colors.RESET}")
                print("  ", end="", flush=True)
                
                buffer = ""
                for chunk in response.iter_text():
                    buffer += chunk
                    
                    # 解析 SSE 数据
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        
                        if line.startswith("data: "):
                            data = line[6:]
                            
                            if data == "[DONE]":
                                break
                            
                            try:
                                parsed = json.loads(data)
                                delta = parsed.get("choices", [{}])[0].get("delta", {})
                                content = delta.get("content") or delta.get("reasoning_content")
                                if content:
                                    print(content, end="", flush=True)
                                    full_content += content
                                    chunks_received += 1
                            except json.JSONDecodeError:
                                pass
        
        elapsed = time.time() - start_time
        print("\n")
        
        print_success(f"流式补全成功（耗时: {elapsed:.2f}s）")
        print_info(f"接收到 {chunks_received} 个数据块")
        print_info(f"总输出长度: {len(full_content)} 字符")
        
        return chunks_received > 0
        
    except httpx.ReadTimeout:
        print_error(f"请求超时（>{timeout}秒）")
        return False
    except Exception as e:
        print_error(f"流式补全异常: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="测试 SGLang API 服务器")
    parser.add_argument(
        "--api-url",
        default=get_config_value(),
        help=f"SGLang API 基础 URL（默认: {get_config_value()}）"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="请求超时时间（秒，默认: 60）"
    )
    parser.add_argument(
        "--enable-stream",
        action="store_true",
        help="启用流式测试"
    )
    
    args = parser.parse_args()
    
    print_section("SGLang API 测试")
    print(f"API URL: {args.api_url}")
    print(f"超时时间: {args.timeout}秒")
    
    # 测试结果
    results = {}
    
    # 1. 健康检查
    results["health"] = test_health_check(args.api_url, timeout=10)
    if not results["health"]:
        print_error("\n服务器连接失败，终止测试")
        sys.exit(1)
    
    # 2. 获取模型列表
    model_name = test_models_endpoint(args.api_url, timeout=10)
    
    # 3. 非流式聊天补全
    results["chat"] = test_chat_completion(args.api_url, model_name, timeout=args.timeout)
    
    # 4. 流式聊天补全
    if args.enable_stream:
        results["stream"] = test_chat_completion_stream(args.api_url, model_name, timeout=args.timeout)
    
    # 总结
    print_section("测试总结")
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for test_name, result in results.items():
        status = "通过" if result else "失败"
        color = Colors.GREEN if result else Colors.RED
        symbol = "✓" if result else "✗"
        print(f"  {color}{symbol} {test_name}: {status}{Colors.RESET}")
    
    print(f"\n{Colors.BOLD}通过率: {passed}/{total}{Colors.RESET}")
    
    if passed == total:
        print_success("\n所有测试通过！SGLang 服务工作正常。")
        sys.exit(0)
    else:
        print_error("\n部分测试失败，请检查 SGLang 服务配置。")
        sys.exit(1)


if __name__ == "__main__":
    main()
