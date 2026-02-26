"""
LLM 服务模块

"""

import asyncio
import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncGenerator, Generator, List, Optional, Union
import threading

from .config import AppConfig, get_config
from .utils.logger import DebugLogger, get_logger, get_memory_usage

# 获取模块日志记录器
logger = get_logger(__name__)


@dataclass
class Message:
    """
    对话消息数据类
    
    Attributes:
        role: 消息角色 (system/user/assistant)
        content: 消息内容
    """
    role: str
    content: str


@dataclass
class GenerationResult:
    """
    生成结果数据类
    
    Attributes:
        text: 生成的文本内容
        reasoning_content: 推理过程内容
        finish_reason: 结束原因 (stop/length/error)
        usage: token 使用统计
        generation_time: 生成耗时（秒）
    """
    text: str
    reasoning_content: Optional[str] = None
    finish_reason: str = "stop"
    usage: Optional[dict] = None
    generation_time: float = 0.0


class LLMProviderInterface(ABC):
    """
    LLM 提供者抽象接口
    
    定义 LLM 服务的标准接口，便于切换不同的推理后端。
    未来 Jetson Thor 部署可实现此接口的特定版本。
    """
    
    @abstractmethod
    def load_model(self) -> bool:
        """
        加载模型
        
        Returns:
            bool: 加载是否成功
        """
        pass
    
    @abstractmethod
    def generate(
        self,
        messages: List[Message],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        stream: bool = False,
        tools: Optional[List[dict]] = None,
    ) -> Union[GenerationResult, Generator[dict, None, None]]:
        """
        生成回复
        
        Args:
            messages: 对话消息列表
            max_new_tokens: 最大生成 token 数
            temperature: 采样温度
            top_p: Top-P 采样参数
            stream: 是否流式输出
            tools: 可用的工具列表
            
        Returns:
            生成结果或流式生成器 (字典形式 {"content": str, "reasoning": str})
        """
        pass
    
    @abstractmethod
    def is_loaded(self) -> bool:
        """检查模型是否已加载"""
        pass
    
    @abstractmethod
    def unload_model(self) -> None:
        """卸载模型释放资源"""
        pass


class SGLangProvider(LLMProviderInterface):
    """
    SGLang 推理提供者
    
    通过 HTTP API 调用 SGLang 推理服务器。
    SGLang 是一个高效的 LLM 推理引擎，支持 NVFP4 等量化格式。
    
    参考文档: https://github.com/sgl-project/sglang
    """
    
    def __init__(self, config: Optional[AppConfig] = None):
        """
        初始化 SGLang 提供者
        
        Args:
            config: 应用配置，默认使用全局配置
        """
        self.config = config or get_config()
        self.debug = DebugLogger(self.config.debug)
        
        # SGLang 服务器配置 - 从配置文件读取
        sglang_host = self.config.sglang.host
        sglang_port = self.config.sglang.port
        self._api_url = os.environ.get("SGLANG_API_URL", f"http://{sglang_host}:{sglang_port}/v1")
        self._model_name = self.config.model.model_name
        self._timeout = self.config.inference.timeout  # 从配置读取超时时间
        self._loaded = False
    
    def load_model(self) -> bool:
        """
        验证 SGLang 服务器连接
        
        Returns:
            bool: 连接是否成功
        """
        import httpx
        
        self.debug.model_loading(f"连接 SGLang 服务: {self._api_url}")
        
        try:
            # 测试连接
            with httpx.Client(timeout=10) as client:
                # 优先尝试根路径健康检查
                base_url = self._api_url
                if base_url.endswith("/v1"):
                    base_url = base_url[:-3]
                
                try:
                    health_resp = client.get(f"{base_url}/health")
                    if health_resp.status_code == 200:
                        self.debug.model_loading(f"SGLang 健康检查通过: {base_url}/health")
                except Exception:
                    pass

                # 尝试获取模型列表验证连接
                response = client.get(f"{self._api_url}/models")
                
                if response.status_code == 200:
                    models = response.json()
                    self.debug.model_loading(f"SGLang 服务连接成功，可用模型: {models}")
                    logger.info(f"SGLang 服务连接成功: {self._api_url}")
                    self._loaded = True
                    return True
                else:
                    logger.error(f"SGLang 服务响应异常: {response.status_code}")
                    return False
                    
        except httpx.ConnectError as e:
            logger.error(f"无法连接到 SGLang 服务: {e}")
            logger.error(f"请确保 SGLang 服务已启动并监听 {self._api_url}")
            logger.error("启动命令: python3 -m sglang.launch_server --model-path <model> --host 0.0.0.0 --port 30000")
            return False
        except Exception as e:
            logger.error(f"SGLang 服务连接失败: {e}")
            return False
    
    def generate(
        self,
        messages: List[Message],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        stream: bool = False,
        tools: Optional[List[dict]] = None,
    ) -> Union[GenerationResult, Generator[dict, None, None]]:
        """
        调用 SGLang 服务生成回复
        """
        import httpx
        from .api.chat import convert_tools_for_sglang
        
        # 转换工具格式以兼容 SGLang
        converted_tools = None
        if tools:
            converted_tools = convert_tools_for_sglang(tools)
            self.debug.inference(f"转换工具格式: {len(tools)} 个工具 -> {len(converted_tools)} 个 SGLang 兼容工具")
        
        # 构建请求
        request_data = {
            "model": self._model_name,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature or self.config.inference.temperature,
            "top_p": top_p or self.config.inference.top_p,
            "max_new_tokens": max_new_tokens or self.config.inference.max_new_tokens,
            "stream": stream,
        }
        
        # 添加转换后的工具
        if converted_tools:
            request_data["tools"] = converted_tools
        
        self.debug.inference(f"发送请求到 SGLang: {json.dumps(request_data, indent=2)}")
        
        if stream:
            return self._stream_generate(request_data)
        else:
            return self._sync_generate(request_data)
    
    def _sync_generate(self, request_data: dict) -> GenerationResult:
        """同步生成"""
        import httpx
        import time
        
        start_time = time.perf_counter()
        
        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(
                f"{self._api_url}/chat/completions",
                json=request_data,
            )
            
            if response.status_code != 200:
                raise RuntimeError(f"SGLang 服务响应错误: {response.status_code} - {response.text}")
            
            result = response.json()
            
        elapsed = time.perf_counter() - start_time
        
        # 解析响应
        choice = result.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = message.get("content") or ""
        reasoning_content = message.get("reasoning_content")
            
        finish_reason = choice.get("finish_reason", "stop")
        usage = result.get("usage")
        
        self.debug.inference(f"SGLang 服务响应完成，耗时: {elapsed:.2f}s")
        
        return GenerationResult(
            text=content,
            reasoning_content=reasoning_content,
            finish_reason=finish_reason,
            usage=usage,
            generation_time=elapsed,
        )
    
    def _stream_generate(self, request_data: dict) -> Generator[dict, None, None]:
        """流式生成"""
        import httpx
        import json
        
        try:
            with httpx.Client(timeout=self._timeout) as client:
                with client.stream(
                    "POST",
                    f"{self._api_url}/chat/completions",
                    json=request_data,
                ) as response:
                    if response.status_code != 200:
                        error_text = response.text
                        self.debug.inference(f"SGLang 流式请求失败: {response.status_code} - {error_text}")
                        raise RuntimeError(f"SGLang 服务响应错误: {response.status_code} - {error_text}")
                    
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
                                    return
                                
                                try:
                                    parsed = json.loads(data)
                                    delta = parsed.get("choices", [{}])[0].get("delta", {})
                                    content = delta.get("content")
                                    reasoning = delta.get("reasoning_content")
                                    if content or reasoning:
                                        # 返回结构化数据而不是单纯字符串
                                        yield {"content": content, "reasoning": reasoning}
                                except json.JSONDecodeError as e:
                                    self.debug.inference(f"JSON 解析错误: {e}, 数据: {data}")
                                    pass
        except Exception as e:
            self.debug.inference(f"流式生成异常: {e}")
            # 返回错误信息作为流式内容
            yield {"content": f"\n[流式生成错误] {type(e).__name__}: {e}\n", "reasoning": None}
    
    def is_loaded(self) -> bool:
        """检查是否已连接"""
        return self._loaded
    
    def unload_model(self) -> None:
        """断开连接"""
        self._loaded = False
        logger.info("SGLang 服务连接已断开")


class VLLMProvider(LLMProviderInterface):
    """
    vLLM 推理提供者
    
    通过 HTTP API 调用 vLLM 推理服务器。
    vLLM 是一个高性能的 LLM 推理引擎，支持 PagedAttention 等优化。
    
    参考文档: https://docs.vllm.ai/
    """
    
    def __init__(self, config: Optional[AppConfig] = None):
        """
        初始化 vLLM 提供者
        
        Args:
            config: 应用配置，默认使用全局配置
        """
        self.config = config or get_config()
        self.debug = DebugLogger(self.config.debug)
        
        # vLLM 服务器配置 - 从配置文件读取
        vllm_host = self.config.vllm.host
        vllm_port = self.config.vllm.port
        self._api_url = os.environ.get("VLLM_API_URL", f"http://{vllm_host}:{vllm_port}/v1")
        self._model_name = self.config.vllm.served_model_name or self.config.model.model_name
        self._timeout = self.config.inference.timeout  # 从配置读取超时时间
        self._loaded = False
    
    def load_model(self) -> bool:
        """
        验证 vLLM 服务器连接
        
        Returns:
            bool: 连接是否成功
        """
        import httpx
        
        self.debug.model_loading(f"连接 vLLM 服务: {self._api_url}")
        
        try:
            # 测试连接
            with httpx.Client(timeout=10) as client:
                # 尝试健康检查
                base_url = self._api_url
                if base_url.endswith("/v1"):
                    base_url = base_url[:-3]
                
                try:
                    health_resp = client.get(f"{base_url}/health")
                    if health_resp.status_code == 200:
                        self.debug.model_loading(f"vLLM 健康检查通过: {base_url}/health")
                except Exception:
                    pass

                # 尝试获取模型列表验证连接
                response = client.get(f"{self._api_url}/models")
                
                if response.status_code == 200:
                    models = response.json()
                    self.debug.model_loading(f"vLLM 服务连接成功，可用模型: {models}")
                    logger.info(f"vLLM 服务连接成功: {self._api_url}")
                    self._loaded = True
                    return True
                else:
                    logger.error(f"vLLM 服务响应异常: {response.status_code}")
                    return False
                    
        except httpx.ConnectError as e:
            logger.error(f"无法连接到 vLLM 服务: {e}")
            logger.error(f"请确保 vLLM 服务已启动并监听 {self._api_url}")
            logger.error("启动命令: vllm serve <model> --host 0.0.0.0 --port 30000")
            return False
        except Exception as e:
            logger.error(f"vLLM 服务连接失败: {e}")
            return False
    
    def generate(
        self,
        messages: List[Message],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        stream: bool = False,
        tools: Optional[List[dict]] = None,
    ) -> Union[GenerationResult, Generator[dict, None, None]]:
        """
        调用 vLLM 服务生成回复
        """
        import httpx
        
        # 构建请求
        request_data = {
            "model": self._model_name,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature or self.config.inference.temperature,
            "top_p": top_p or self.config.inference.top_p,
            "max_tokens": max_new_tokens or self.config.inference.max_new_tokens,
            "stream": stream,
        }
        
        # 添加工具
        if tools:
            request_data["tools"] = tools
        
        self.debug.inference(f"发送请求到 vLLM: {json.dumps(request_data, indent=2)}")
        
        if stream:
            return self._stream_generate(request_data)
        else:
            return self._sync_generate(request_data)
    
    def _sync_generate(self, request_data: dict) -> GenerationResult:
        """同步生成"""
        import httpx
        import time
        
        start_time = time.perf_counter()
        
        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(
                f"{self._api_url}/chat/completions",
                json=request_data,
            )
            
            if response.status_code != 200:
                raise RuntimeError(f"vLLM 服务响应错误: {response.status_code} - {response.text}")
            
            result = response.json()
            
        elapsed = time.perf_counter() - start_time
        
        # 解析响应
        choice = result.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = message.get("content") or ""
        reasoning_content = message.get("reasoning_content")
            
        finish_reason = choice.get("finish_reason", "stop")
        usage = result.get("usage")
        
        self.debug.inference(f"vLLM 服务响应完成，耗时: {elapsed:.2f}s")
        
        return GenerationResult(
            text=content,
            reasoning_content=reasoning_content,
            finish_reason=finish_reason,
            usage=usage,
            generation_time=elapsed,
        )
    
    def _stream_generate(self, request_data: dict) -> Generator[dict, None, None]:
        """流式生成"""
        import httpx
        import json
        
        try:
            with httpx.Client(timeout=self._timeout) as client:
                with client.stream(
                    "POST",
                    f"{self._api_url}/chat/completions",
                    json=request_data,
                ) as response:
                    if response.status_code != 200:
                        error_text = response.text
                        self.debug.inference(f"vLLM 流式请求失败: {response.status_code} - {error_text}")
                        raise RuntimeError(f"vLLM 服务响应错误: {response.status_code} - {error_text}")
                    
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
                                    return
                                
                                try:
                                    parsed = json.loads(data)
                                    delta = parsed.get("choices", [{}])[0].get("delta", {})
                                    content = delta.get("content")
                                    reasoning = delta.get("reasoning_content")
                                    if content or reasoning:
                                        yield {"content": content, "reasoning": reasoning}
                                except json.JSONDecodeError as e:
                                    self.debug.inference(f"JSON 解析错误: {e}, 数据: {data}")
                                    pass
        except Exception as e:
            self.debug.inference(f"流式生成异常: {e}")
            yield {"content": f"\n[流式生成错误] {type(e).__name__}: {e}\n", "reasoning": None}
    
    def is_loaded(self) -> bool:
        """检查是否已连接"""
        return self._loaded
    
    def unload_model(self) -> None:
        """断开连接"""
        self._loaded = False
        logger.info("vLLM 服务连接已断开")


class MockLLMProvider(LLMProviderInterface):
    """
    模拟 LLM 提供者（用于测试和演示）
    
    在没有 GPU 或 TensorRT-LLM 环境时使用，
    返回模拟的响应数据。
    """
    
    def __init__(self, config: Optional[AppConfig] = None):
        """初始化模拟提供者"""
        self.config = config or get_config()
        self.debug = DebugLogger(self.config.debug)
        self._loaded = False
    
    def load_model(self) -> bool:
        """模拟加载模型"""
        self.debug.model_loading("使用 MockLLMProvider（模拟模式）")
        logger.warning("使用模拟 LLM 提供者，仅用于测试")
        self._loaded = True
        return True
    
    def generate(
        self,
        messages: List[Message],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        stream: bool = False,
    ) -> Union[GenerationResult, Generator[dict, None, None]]:
        """生成模拟回复"""
        # 获取最后一条用户消息
        user_message = ""
        for msg in reversed(messages):
            if msg.role == "user":
                user_message = msg.content
                break
        
        # 生成模拟回复
        mock_response = (
            f"[模拟回复] 您说的是: \"{user_message[:50]}...\"\n\n"
            f"这是来自 {self.config.model.model_name} 的模拟响应。\n"
            "在实际部署中，这里将是模型生成的真实回复。"
        )
        
        if stream:
            return self._mock_stream(mock_response)
        else:
            return GenerationResult(
                text=mock_response,
                finish_reason="stop",
                generation_time=0.1,
            )
    
    def _mock_stream(self, text: str) -> Generator[dict, None, None]:
        """模拟流式输出"""
        import time
        words = text.split()
        for word in words:
            yield {"content": word + " ", "reasoning": None}
            time.sleep(0.05)  # 模拟生成延迟
    
    def is_loaded(self) -> bool:
        """检查是否已加载"""
        return self._loaded
    
    def unload_model(self) -> None:
        """卸载模型"""
        self._loaded = False


class OpenAICompatibleProxyProvider(LLMProviderInterface):
    """OpenAI 兼容服务代理提供者。

    通过 HTTP API 调用一个 OpenAI 兼容的推理服务（例如 vLLM OpenAI server），
    本 Provider 作为代理转发请求并统一返回格式。
    """
    
    def __init__(self, config: Optional[AppConfig] = None):
        """
        初始化容器提供者
        
        Args:
            config: 应用配置，默认使用全局配置
        """
        self.config = config or get_config()
        self.debug = DebugLogger(self.config.debug)
        
        self._api_url = self.config.container.api_url
        self._model_name = self.config.container.model_name or self.config.model.model_name
        self._timeout = self.config.container.timeout
        self._loaded = False
        self._session = None
    
    def load_model(self) -> bool:
        """
        验证容器连接
        
        Returns:
            bool: 连接是否成功
        """
        import httpx
        
        self.debug.model_loading(f"连接 OpenAI 兼容服务: {self._api_url}")
        
        try:
            # 测试连接
            with httpx.Client(timeout=10) as client:
                # 尝试获取模型列表验证连接
                response = client.get(f"{self._api_url}/models")
                
                if response.status_code == 200:
                    models = response.json()
                    self.debug.model_loading(f"容器连接成功，可用模型: {models}")
                    logger.info(f"OpenAI 兼容服务连接成功: {self._api_url}")
                    self._loaded = True
                    return True
                else:
                    logger.error(f"容器响应异常: {response.status_code}")
                    return False
                    
        except httpx.ConnectError as e:
            logger.error(f"无法连接到 OpenAI 兼容服务: {e}")
            logger.error(f"请确保服务已启动并监听 {self._api_url}")
            return False
        except Exception as e:
            logger.error(f"容器连接失败: {e}")
            return False
    
    def generate(
        self,
        messages: List[Message],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        stream: bool = False,
    ) -> Union[GenerationResult, Generator[dict, None, None]]:
        """
        调用容器生成回复
        """
        import httpx
        
        # 构建请求
        request_data = {
            "model": self._model_name,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature or self.config.inference.temperature,
            "top_p": top_p or self.config.inference.top_p,
            "max_tokens": max_new_tokens or self.config.inference.max_new_tokens,
            "stream": stream,
        }
        
        self.debug.inference(f"发送请求到容器: model={self._model_name}, stream={stream}")
        
        if stream:
            return self._stream_generate(request_data)
        else:
            return self._sync_generate(request_data)
    
    def _sync_generate(self, request_data: dict) -> GenerationResult:
        """同步生成"""
        import httpx
        import time
        
        start_time = time.perf_counter()
        
        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(
                f"{self._api_url}/chat/completions",
                json=request_data,
            )
            
            if response.status_code != 200:
                raise RuntimeError(f"容器响应错误: {response.status_code} - {response.text}")
            
            result = response.json()
            
        elapsed = time.perf_counter() - start_time
        
        # 解析响应
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        finish_reason = result.get("choices", [{}])[0].get("finish_reason", "stop")
        usage = result.get("usage")
        
        self.debug.inference(f"容器响应完成，耗时: {elapsed:.2f}s")
        
        return GenerationResult(
            text=content,
            finish_reason=finish_reason,
            usage=usage,
            generation_time=elapsed,
        )
    
    def _stream_generate(self, request_data: dict) -> Generator[dict, None, None]:
        """流式生成"""
        import httpx
        import json
        
        with httpx.Client(timeout=self._timeout) as client:
            with client.stream(
                "POST",
                f"{self._api_url}/chat/completions",
                json=request_data,
            ) as response:
                if response.status_code != 200:
                    raise RuntimeError(f"容器响应错误: {response.status_code}")
                
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
                                return
                            
                            try:
                                parsed = json.loads(data)
                                delta = parsed.get("choices", [{}])[0].get("delta", {})
                                # 同时支持 content 和 reasoning_content
                                content = delta.get("content") or delta.get("reasoning_content")
                                if content:
                                    yield content
                            except json.JSONDecodeError:
                                pass
    
    def is_loaded(self) -> bool:
        """检查是否已连接"""
        return self._loaded
    
    def unload_model(self) -> None:
        """断开连接"""
        self._loaded = False
        logger.info("OpenAI 兼容服务连接已断开")


class LLMService:
    """
    LLM 服务管理类
    
    提供高层次的 LLM 服务接口，自动选择合适的推理后端，
    处理模型生命周期管理。
    
    使用示例:
        service = LLMService()
        service.initialize()
        
        messages = [Message(role="user", content="你好")]
        result = service.chat(messages)
        print(result.text)
    """
    
    def __init__(self, config: Optional[AppConfig] = None, use_mock: bool = False):
        """
        初始化 LLM 服务
        
        Args:
            config: 应用配置
            use_mock: 是否使用模拟提供者（用于测试）
        """
        self.config = config or get_config()
        self.debug = DebugLogger(self.config.debug)
        self._use_mock = use_mock
        self._provider: Optional[LLMProviderInterface] = None
    
    def initialize(self) -> bool:
        """
        初始化服务，加载模型
        
        推理后端选择优先级：
        1. 如果 use_mock=True，使用模拟模式
        2. 根据 config.model.backend 选择后端：
           - sglang: SGLang 推理服务器（默认）
           - openai_proxy: OpenAI 兼容服务代理
        3. 回退到模拟模式
        
        Returns:
            bool: 初始化是否成功
        """
        # 1. 模拟模式（测试用）
        if self._use_mock:
            self._provider = MockLLMProvider(self.config)
            return self._provider.load_model()
        
        backend = (self.config.model.backend or "sglang").strip().lower()

        # 2. SGLang（主推理后端）
        if backend == "sglang":
            self.debug.model_loading("使用 SGLang 推理后端")
            self._provider = SGLangProvider(self.config)
            if self._provider.load_model():
                return True
            logger.warning("SGLang 初始化失败，尝试降级")

        # 3. vLLM 推理后端
        if backend == "vllm":
            self.debug.model_loading("使用 vLLM 推理后端")
            self._provider = VLLMProvider(self.config)
            if self._provider.load_model():
                return True
            logger.warning("vLLM 初始化失败，尝试降级")

        # 4. OpenAI 兼容服务代理（用于对接其他服务）
        if self.config.container.enabled or backend == "openai_proxy":
            self.debug.model_loading("尝试使用 OpenAI 兼容服务代理")
            self._provider = OpenAICompatibleProxyProvider(self.config)
            if self._provider.load_model():
                return True
            logger.warning("容器/服务代理连接失败，尝试降级")
        
        # 4. 回退到模拟模式
        logger.warning("回退到模拟模式")
        self._provider = MockLLMProvider(self.config)
        return self._provider.load_model()
    
    def chat(
        self,
        messages: List[Message],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        stream: bool = False,
        tools: Optional[List[dict]] = None,
    ) -> Union[GenerationResult, Generator[dict, None, None]]:
        """
        对话接口
        
        Args:
            messages: 对话消息列表
            max_new_tokens: 最大生成 token 数
            temperature: 采样温度
            top_p: Top-P 采样参数
            stream: 是否流式输出
            
        Returns:
            生成结果或流式生成器 (字典形式 {"content": str, "reasoning": str})
        """
        if self._provider is None or not self._provider.is_loaded():
            raise RuntimeError("服务未初始化，请先调用 initialize()")
        
        self.debug.inference(f"收到对话请求，消息数: {len(messages)}")
        
        return self._provider.generate(
            messages=messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            stream=stream,
            tools=tools,
        )
    
    async def chat_async(
        self,
        messages: List[Message],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        tools: Optional[List[dict]] = None,
    ) -> GenerationResult:
        """
        异步对话接口（非流式）
        
        将同步调用包装为异步，便于在 FastAPI 中使用。
        """
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self.chat(
                messages=messages,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                top_p=top_p,
                stream=False,
                tools=tools,
            )
        )
        # 类型断言：stream=False 时返回 GenerationResult
        return result  # type: ignore[return-value]
    
    async def chat_stream_async(
        self,
        messages: List[Message],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
    ) -> AsyncGenerator[dict, None]:
        """
        异步流式对话接口
        
        将同步流式生成器包装为异步生成器。
        """
        # 注意：直接在事件循环里迭代同步生成器会阻塞整个 FastAPI worker。
        # 这里把同步生成器放到后台线程里跑，用 asyncio.Queue 转发，保证 SSE 流式不阻塞。
        generator = self.chat(
            messages=messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            stream=True,
        )

        q: asyncio.Queue[Optional[dict]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _worker():
            try:
                for chunk in generator:  # type: ignore[union-attr]
                    loop.call_soon_threadsafe(q.put_nowait, chunk)
            except Exception as e:
                loop.call_soon_threadsafe(q.put_nowait, {"content": f"\n[stream-error] {type(e).__name__}: {e}\n", "reasoning": None})
            finally:
                loop.call_soon_threadsafe(q.put_nowait, None)

        t = threading.Thread(target=_worker, name="llm-stream-worker", daemon=True)
        t.start()

        while True:
            item = await q.get()
            if item is None:
                break
            yield item
    
    @property
    def is_ready(self) -> bool:
        """检查服务是否就绪"""
        return self._provider is not None and self._provider.is_loaded()
    
    def shutdown(self) -> None:
        """关闭服务，释放资源"""
        if self._provider is not None:
            self._provider.unload_model()
            self._provider = None
            logger.info("LLM 服务已关闭")


# 全局服务实例
_llm_service: Optional[LLMService] = None


def get_llm_service(use_mock: bool = False) -> LLMService:
    """
    获取全局 LLM 服务实例
    
    Args:
        use_mock: 是否使用模拟模式
        
    Returns:
        LLMService: LLM 服务实例
    """
    global _llm_service
    
    if _llm_service is None:
        _llm_service = LLMService(use_mock=use_mock)
    
    return _llm_service


if __name__ == "__main__":
    # 测试服务
    service = LLMService(use_mock=True)
    service.initialize()
    
    messages = [
        Message(role="system", content="你是一个有帮助的助手。"),
        Message(role="user", content="你好，请介绍一下自己。"),
    ]
    
    result = service.chat(messages, stream=False)
    if isinstance(result, GenerationResult):
        print(f"回复: {result.text}")
        print(f"耗时: {result.generation_time:.2f}s")
