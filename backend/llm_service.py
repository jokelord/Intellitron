"""
LLM 服务模块

"""

import asyncio
import base64
import copy
from backend.agent.canonical_messages import message_to_wire
import io
import json
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncGenerator, Callable, Generator, List, Optional, Union
import threading
import uuid

from .config import AppConfig, get_config
from .utils.logger import DebugLogger, get_logger, get_memory_usage
from .vision.validation import redact_data_urls

# 获取模块日志记录器
logger = get_logger(__name__)


def _extract_error_text_from_response(response) -> str:
    """Safely read error bodies from streaming or non-streaming responses."""
    try:
        response.read()
    except Exception:
        pass

    try:
        text = response.text
        if text:
            return redact_data_urls(text)
    except Exception:
        pass

    try:
        payload = response.json()
        return redact_data_urls(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass

    return ""


def resolve_backend_context_length(
    config: AppConfig, backend_name: Optional[str]
) -> Optional[int]:
    """返回当前推理后端的实际上下文上限。"""
    backend = (backend_name or "").lower()
    if backend == "sglang":
        return config.sglang.max_context_length or config.sglang.max_total_tokens
    if backend in {"vllm", "openai_proxy"}:
        return config.vllm.max_model_len or config.model.max_context_length
    return config.model.max_context_length


def resolve_effective_max_tokens(
    config: AppConfig,
    backend_name: Optional[str],
    requested_max_tokens: Optional[int] = None,
) -> Optional[int]:
    """收敛默认输出长度，避免前端拿到超过后端实际能力的默认值。"""
    current = requested_max_tokens
    if current is None:
        current = config.inference.max_tokens
    if current is None:
        current = config.inference.max_new_tokens
    if current is None:
        return None

    context_length = resolve_backend_context_length(config, backend_name)
    if not context_length:
        return current

    return max(1, min(current, context_length))


def _inject_runtime_context(
    messages: List["Message"],
    runtime_context: Optional[str],
) -> List["Message"]:
    if not runtime_context:
        return messages

    injected = list(messages)
    for idx, message in enumerate(injected):
        if message.role != "system":
            continue
        existing = message.content or ""
        merged = f"{existing}\n\n{runtime_context}" if existing else runtime_context
        injected[idx] = Message(role="system", content=merged)
        return injected

    insert_at = len(injected)
    for idx in range(len(injected) - 1, -1, -1):
        if injected[idx].role == "user":
            insert_at = idx
            break
    injected.insert(insert_at, Message(role="system", content=runtime_context))
    return injected

# Active request tracking for abort mechanism
_active_requests: dict = {}
_requests_lock = threading.Lock()
_pending_request_aborts: set[str] = set()

# Upstream OpenAI-compatible streams are read by a worker thread. Merely
# aborting the browser/backend request does not interrupt that blocking HTTP
# read, so vLLM keeps decoding until the response naturally finishes. Keep a
# small, thread-safe registry of active upstream stream closers so the stop
# endpoint and async-generator cancellation can close the socket immediately.
_active_upstream_streams: dict[str, tuple[str, Callable[[], None], object]] = {}
_upstream_streams_lock = threading.Lock()


def create_request_id() -> str:
    """Create a unique request ID for tracking."""
    return str(uuid.uuid4())


def _request_log_summary(request_data: dict) -> str:
    roles: list[str] = []
    image_count = 0
    encoded_data_url_bytes = 0
    for message in request_data.get("messages") or []:
        if not isinstance(message, dict):
            continue
        roles.append(str(message.get("role", "unknown")))
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "image_url":
                continue
            image_count += 1
            image_url = item.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else None
            if isinstance(url, str) and url.startswith("data:"):
                encoded_data_url_bytes += len(url.encode("utf-8"))
    return (
        f"model={request_data.get('model')} stream={request_data.get('stream')} "
        f"roles={roles} images={image_count} "
        f"encoded_data_url_bytes={encoded_data_url_bytes}"
    )


def register_request(request_id: str) -> object:
    """Register a new active request."""
    registration = object()
    with _requests_lock:
        _active_requests[request_id] = {
            "aborted": request_id in _pending_request_aborts,
            "registration": registration,
        }
    return registration


def abort_request(request_id: str) -> None:
    """Mark a request as aborted and close any blocking upstream stream."""
    with _requests_lock:
        _pending_request_aborts.add(request_id)
        if request_id in _active_requests:
            _active_requests[request_id]["aborted"] = True

    # This is intentionally best-effort: the stream may not have registered
    # yet (for example, a stop can race request creation).
    _close_upstream_stream(request_id)


def is_request_aborted(request_id: str) -> bool:
    """Check if a request has been aborted."""
    with _requests_lock:
        return _active_requests.get(request_id, {}).get("aborted", False) or request_id in _pending_request_aborts


def unregister_request(request_id: str, registration: object | None = None) -> None:
    """Remove a request from tracking."""
    with _requests_lock:
        entry = _active_requests.get(request_id)
        if entry is not None and (
            registration is None or entry.get("registration") is registration
        ):
            _active_requests.pop(request_id, None)
            _pending_request_aborts.discard(request_id)


def active_request_ids() -> tuple[str, ...]:
    """Return a stable snapshot of all registered inference request IDs."""
    with _requests_lock:
        return tuple(_active_requests)


def register_upstream_stream(
    request_id: str, backend_name: str, closer: Callable[[], None]
) -> object | None:
    """Register the currently open upstream HTTP stream for cancellation."""
    if not request_id:
        return None
    registration = object()
    with _upstream_streams_lock:
        _active_upstream_streams[request_id] = (backend_name, closer, registration)
    if is_request_aborted(request_id):
        _close_upstream_stream(request_id, registration)
    return registration


def unregister_upstream_stream(
    request_id: str, registration: object | None = None
) -> None:
    """Forget an upstream stream after its context manager has closed."""
    if not request_id:
        return
    with _upstream_streams_lock:
        entry = _active_upstream_streams.get(request_id)
        if entry is not None and (registration is None or entry[2] is registration):
            _active_upstream_streams.pop(request_id, None)


def active_upstream_request_ids() -> tuple[str, ...]:
    """Return a stable snapshot of active provider request IDs."""
    with _upstream_streams_lock:
        return tuple(_active_upstream_streams)


def _close_upstream_stream(
    request_id: str, registration: object | None = None
) -> Optional[str]:
    """Close an active upstream stream and return its backend name."""
    if not request_id:
        return None
    with _upstream_streams_lock:
        entry = _active_upstream_streams.get(request_id)
        if entry is not None and (
            registration is None or entry[2] is registration
        ):
            _active_upstream_streams.pop(request_id, None)
        else:
            entry = None
    if entry is None:
        return None

    backend_name, closer, _registration = entry
    try:
        closer()
    except Exception as exc:
        logger.debug(
            "关闭 %s 上游流失败 request_id=%s: %s",
            backend_name,
            request_id,
            redact_data_urls(exc),
        )
    return backend_name


def _extract_reasoning_content(message: dict) -> Optional[str]:
    """Normalize OpenAI/vLLM reasoning field names in a full message."""
    reasoning = message.get("reasoning_content")
    if reasoning is None:
        reasoning = message.get("reasoning")
    return reasoning


def _openai_stream_payload_to_chunk(parsed: dict) -> dict | None:
    choices = parsed.get("choices") or []
    choice = choices[0] if choices else {}
    delta = choice.get("delta", {})
    content = delta.get("content")
    # vLLM 0.17.x's Qwen reasoning parser emits ``delta.reasoning`` while
    # older/OpenAI-compatible servers use ``reasoning_content``. Normalize
    # both spellings here; otherwise a thinking-only response is silently
    # dropped and the UI appears to have no streaming output.
    reasoning = delta.get("reasoning_content")
    if reasoning is None:
        reasoning = delta.get("reasoning")
    tool_calls = delta.get("tool_calls")
    usage = parsed.get("usage")
    finish_reason = choice.get("finish_reason")

    if not (content or reasoning or tool_calls or usage or finish_reason):
        return None

    chunk = {
        "content": content,
        "reasoning": reasoning,
        "tool_calls": tool_calls,
    }
    if usage:
        chunk["usage"] = usage
    if finish_reason:
        chunk["finish_reason"] = finish_reason
    return chunk


def _stream_termination_chunk(
    *, backend: str, request_id: Optional[str], done: bool, residual: str
) -> dict:
    residual = residual.strip()
    if residual:
        logger.warning(
            "[llm-stream-diag] backend=%s request_id=%s %s chars=%d tail=%r",
            backend,
            request_id,
            "bytes_after_done" if done else "non_newline_residual",
            len(residual),
            redact_data_urls(residual[-160:]),
        )
    return {
        "content": None,
        "reasoning": None,
        "stream_done": done,
        "stream_residual_chars": len(residual),
    }


def abort_sglang_all(sglang_url: str) -> bool:
    """
    Call SGLang's /abort_request endpoint with abort_all=True to stop all running requests.

    Args:
        sglang_url: Base URL of SGLang server (e.g., http://localhost:30000)

    Returns:
        bool: True if abort was successful, False otherwise
    """
    import httpx

    try:
        with httpx.Client(timeout=10, trust_env=False) as client:
            response = client.post(
                f"{sglang_url}/abort_request", json={"abort_all": True}
            )
            if response.status_code == 200:
                logger.info("Successfully sent abort_all to SGLang")
                return True
            else:
                logger.warning(
                    f"SGLang abort_all failed: {response.status_code} - {response.text}"
                )
                return False
    except Exception as e:
        logger.error(f"Failed to call SGLang abort_all endpoint: {e}")
        return False


@dataclass
class Message:
    """
    对话消息数据类

    Attributes:
        role: 消息角色 (system/user/assistant)
        content: 消息内容 (字符串或列表，支持多模态)
    """

    role: str
    content: Union[str, List[dict]]
    tool_calls: Optional[list] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    reasoning_content: Optional[str] = None


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
        tool_calls: SGLang 返回的结构化工具调用列表
    """

    text: str
    reasoning_content: Optional[str] = None
    finish_reason: str = "stop"
    usage: Optional[dict] = None
    generation_time: float = 0.0
    tool_calls: Optional[list] = None
    transport_error: Optional[str] = None


def prepare_multimodal_messages(
    messages: List[Message],
    *,
    max_image_dimension: Optional[int],
    preserve_image_urls: frozenset[str] = frozenset(),
    debug: Optional[DebugLogger] = None,
) -> List[Message]:
    """Copy multimodal messages and downscale oversized embedded images."""
    prepared = [
        copy.deepcopy(message)
        for message in messages
    ]

    if max_image_dimension is None or max_image_dimension <= 0:
        return prepared

    for message in prepared:
        if not isinstance(message.content, list):
            continue
        for item in message.content:
            if not (isinstance(item, dict) and item.get("type") == "image_url"):
                continue
            image_url = item.get("image_url")
            if not isinstance(image_url, dict):
                continue
            url = image_url.get("url")
            if not isinstance(url, str) or not url.startswith("data:"):
                continue
            if url in preserve_image_urls:
                continue

            try:
                header, encoded_image = url.split(",", 1)
                media_type = header[5:].split(";", 1)[0]
                if not (
                    media_type.lower().startswith("image/")
                    and len(media_type) > len("image/")
                    and header.lower().endswith(";base64")
                ):
                    continue
                image_bytes = base64.b64decode(encoded_image, validate=True)
                from PIL import Image

                with Image.open(io.BytesIO(image_bytes)) as source_image:
                    width, height = source_image.size
                    if (
                        width <= max_image_dimension
                        and height <= max_image_dimension
                    ):
                        continue

                    scale = min(
                        max_image_dimension / width,
                        max_image_dimension / height,
                    )
                    resized_size = (
                        max(1, int(width * scale)),
                        max(1, int(height * scale)),
                    )
                    resized_image = source_image.resize(
                        resized_size, Image.Resampling.LANCZOS
                    )
                    if resized_image.mode != "RGB":
                        resized_image = resized_image.convert("RGB")
                    output = io.BytesIO()
                    resized_image.save(
                        output, format="JPEG", quality=85, optimize=False
                    )
                image_url["url"] = (
                    "data:image/jpeg;base64,"
                    f"{base64.b64encode(output.getvalue()).decode()}"
                )
            except Exception as exc:
                if debug is not None:
                    debug.inference(
                        "[INFER-IMG] Failed to prepare embedded image "
                        f"({type(exc).__name__}: {exc}); leaving it unchanged"
                    )

    return prepared


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
        request_id: Optional[str] = None,
        extra_body: Optional[dict] = None,
        runtime_context: Optional[str] = None,
        preserve_prompt_shape: bool = False,
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
        self._api_url = os.environ.get(
            "SGLANG_API_URL", f"http://{sglang_host}:{sglang_port}/v1"
        )
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
            with httpx.Client(timeout=10, trust_env=False) as client:
                # 优先尝试根路径健康检查
                base_url = self._api_url
                if base_url.endswith("/v1"):
                    base_url = base_url[:-3]

                try:
                    health_resp = client.get(f"{base_url}/health")
                    if health_resp.status_code == 200:
                        self.debug.model_loading(
                            f"SGLang 健康检查通过: {base_url}/health"
                        )
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
            logger.error(
                "启动命令: python3 -m sglang.launch_server --model-path <model> --host 0.0.0.0 --port 30000"
            )
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
        request_id: Optional[str] = None,
        extra_body: Optional[dict] = None,
        runtime_context: Optional[str] = None,
        preserve_prompt_shape: bool = False,
    ) -> Union[GenerationResult, Generator[dict, None, None]]:
        """
        调用 SGLang 服务生成回复

        Args:
            request_id: Optional request ID for abort tracking. If provided and stream=True,
                        the stream can be aborted by calling abort_request(request_id).
        """
        import httpx
        from .api.chat import convert_tools_for_sglang

        # Register request for abort tracking if stream mode
        request_registration = None
        if stream and request_id:
            request_registration = register_request(request_id)
            logger.info(f"[ABORT] Registered request_id: {request_id}")

        # 转换工具格式以兼容 SGLang
        converted_tools = None
        if tools:
            converted_tools = convert_tools_for_sglang(tools)
            self.debug.inference(
                f"转换工具格式: {len(tools)} 个工具 -> {len(converted_tools)} 个 SGLang 兼容工具"
            )

        # 构建消息列表 - 支持多模态内容
        # 当 content 是字符串时，直接使用；当是列表时，保持多模态结构
        def build_message_content(msg: Message) -> Union[str, List[dict]]:
            content = msg.content
            if isinstance(content, str):
                return content
            elif isinstance(content, list):
                # 多模态消息，保持原始结构
                return content
            else:
                # 其他情况转为字符串
                return str(content)

        messages_data = [
            message_to_wire(m) for m in messages
        ]

        # 构建请求
        request_data = {
            "model": self._model_name,
            "messages": messages_data,
            "temperature": (
                self.config.inference.temperature if temperature is None else temperature
            ),
            "top_p": self.config.inference.top_p if top_p is None else top_p,
            "max_new_tokens": resolve_effective_max_tokens(
                self.config, "sglang", max_new_tokens
            ),
            "stream": stream,
        }

        if self.config.inference.top_k is not None:
            request_data["top_k"] = self.config.inference.top_k
        if self.config.inference.repetition_penalty is not None:
            request_data["repetition_penalty"] = self.config.inference.repetition_penalty

        if extra_body:
            request_data.update(extra_body)

        # stream_options 注入需在 extra_body 合并之后，保证统计字段不被调用方覆盖
        if stream and self.config.inference.stream_usage_stats:
            request_data["stream_options"] = {
                "include_usage": True,
                "continuous_usage_stats": True,  # SGLang 专有：每个 chunk 返回累计 usage
            }

        if request_id:
            request_data["request_id"] = request_id

        # 添加转换后的工具
        if converted_tools:
            request_data["tools"] = converted_tools

        self.debug.inference(
            f"发送请求到 SGLang: {_request_log_summary(request_data)}"
        )

        if stream:
            return self._stream_generate(request_data, request_id, request_registration)
        else:
            return self._sync_generate(request_data)

    def _sync_generate(self, request_data: dict) -> GenerationResult:
        """同步生成"""
        import httpx
        import time

        start_time = time.perf_counter()

        with httpx.Client(timeout=self._timeout, trust_env=False) as client:
            response = client.post(
                f"{self._api_url}/chat/completions",
                json=request_data,
            )

            if response.status_code != 200:
                raise RuntimeError(
                    f"SGLang 服务响应错误: {response.status_code} - "
                    f"{redact_data_urls(response.text)}"
                )

            result = response.json()

        elapsed = time.perf_counter() - start_time

        # 解析响应
        choice = result.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = message.get("content") or ""
        reasoning_content = _extract_reasoning_content(message)

        finish_reason = choice.get("finish_reason", "stop")
        usage = result.get("usage")

        # 提取结构化 tool_calls（SGLang tool_call_parser 解析结果）
        raw_tool_calls = message.get("tool_calls")

        self.debug.inference(f"SGLang 服务响应完成，耗时: {elapsed:.2f}s")

        return GenerationResult(
            text=content,
            reasoning_content=reasoning_content,
            finish_reason=finish_reason,
            usage=usage,
            generation_time=elapsed,
            tool_calls=raw_tool_calls,
        )

    def _stream_generate(
        self,
        request_data: dict,
        request_id: Optional[str] = None,
        request_registration: object | None = None,
    ) -> Generator[dict, None, None]:
        """流式生成

        Args:
            request_data: The request payload to send to SGLang
            request_id: Optional request ID for abort tracking
        """
        import httpx
        import json
        import time

        first_chunk_time = None
        upstream_registration = None
        try:
            with httpx.Client(timeout=self._timeout, trust_env=False) as client:
                request_start = time.perf_counter()
                with client.stream(
                    "POST",
                    f"{self._api_url}/chat/completions",
                    json=request_data,
                ) as response:
                    if request_id:
                        upstream_registration = register_upstream_stream(
                            request_id,
                            "sglang",
                            lambda: response.close(),
                        )
                    request_time = time.perf_counter() - request_start
                    self.debug.inference(
                        f"SGLang 请求耗时: {request_time:.2f}s (建立连接)"
                    )

                    if response.status_code != 200:
                        error_text = _extract_error_text_from_response(response)
                        self.debug.inference(
                            f"SGLang 流式请求失败: {response.status_code} - {error_text}"
                        )
                        raise RuntimeError(
                            f"SGLang 服务响应错误: {response.status_code} - {error_text}"
                        )

                    buffer = ""
                    for chunk in response.iter_text():
                        # Check abort flag before processing each chunk
                        if request_id and is_request_aborted(request_id):
                            self.debug.inference(f"请求 {request_id} 已中止")
                            # Yield abort signal before returning
                            yield {
                                "content": None,
                                "reasoning": None,
                                "aborted": True,
                            }
                            return

                        buffer += chunk

                        # 解析 SSE 数据
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()

                            if line.startswith("data:"):
                                data = line[5:].lstrip()

                                if data == "[DONE]":
                                    # Check abort before returning
                                    if request_id and is_request_aborted(request_id):
                                        self.debug.inference(
                                            f"请求 {request_id} 在 [DONE] 处中止"
                                        )
                                        yield {
                                            "content": None,
                                            "reasoning": None,
                                            "aborted": True,
                                        }
                                        return
                                    yield _stream_termination_chunk(
                                        backend="sglang",
                                        request_id=request_id,
                                        done=True,
                                        residual=buffer,
                                    )
                                    return

                                # 记录首次收到数据的时间
                                if first_chunk_time is None:
                                    first_chunk_time = time.perf_counter()
                                    total_time = first_chunk_time - request_start
                                    self.debug.inference(
                                        f"SGLang 首帧耗时: {total_time:.2f}s"
                                    )

                                try:
                                    parsed = json.loads(data)
                                    chunk_payload = _openai_stream_payload_to_chunk(parsed)
                                    if chunk_payload is not None:
                                        yield chunk_payload
                                except json.JSONDecodeError as e:
                                    self.debug.inference(
                                        f"JSON 解析错误: {e}, "
                                        f"数据: {redact_data_urls(data)}"
                                    )
                                    pass
                    yield _stream_termination_chunk(
                        backend="sglang",
                        request_id=request_id,
                        done=False,
                        residual=buffer,
                    )
        except Exception as e:
            safe_error = redact_data_urls(e)
            self.debug.inference(f"流式生成异常: {safe_error}")
            if not (request_id and is_request_aborted(request_id)):
                yield {
                    "content": f"\n[流式生成错误] {type(e).__name__}: {safe_error}\n",
                    "transport_error": f"{type(e).__name__}: {safe_error}",
                    "reasoning": None,
                }
        finally:
            if request_id:
                unregister_upstream_stream(request_id, upstream_registration)
                unregister_request(request_id, request_registration)

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
        self._api_url = os.environ.get(
            "VLLM_API_URL", f"http://{vllm_host}:{vllm_port}/v1"
        )
        self._model_name = (
            self.config.vllm.served_model_name or self.config.model.model_name
        )
        self._timeout = self.config.inference.timeout  # 从配置读取超时时间
        self._loaded = False

    @staticmethod
    def _estimate_message_tokens(content: Union[str, List[dict]]) -> int:
        """粗略估算单条消息 token 数，用于输出预算裁剪。"""
        if isinstance(content, str):
            return max(1, len(content) // 4)

        if not isinstance(content, list):
            return max(1, len(str(content)) // 4)

        total = 0
        for item in content:
            if not isinstance(item, dict):
                total += max(1, len(str(item)) // 4)
                continue

            if item.get("type") == "text":
                total += max(1, len(str(item.get("text", ""))) // 4)
            elif item.get("type") == "image_url":
                # Image tokenization is provider/model-specific. Never treat
                # encoded Base64 bytes as prompt text for output budgeting.
                total += 1024
            else:
                total += max(1, len(json.dumps(item, ensure_ascii=False)) // 4)

        return total

    def _estimate_prompt_tokens(self, messages_data: List[dict]) -> int:
        """粗略估算整个 prompt token 数。"""
        total = 0
        for msg in messages_data:
            total += 4  # chat template / role 开销的保守近似
            total += self._estimate_message_tokens(msg.get("content", ""))
        return total + 4

    def _cap_max_tokens_to_context_budget(self, request_data: dict) -> dict:
        """按剩余上下文预算主动裁剪 max_tokens，避免 vLLM 预处理阶段直接 400。"""
        context_length = resolve_backend_context_length(self.config, "vllm")
        current = request_data.get("max_tokens")
        messages = request_data.get("messages") or []

        if not context_length or not isinstance(current, int) or current <= 0:
            return request_data
        if not isinstance(messages, list) or not messages:
            return request_data

        prompt_tokens = self._estimate_prompt_tokens(messages)
        # 留少量余量给模板差异和 tokenizer 估算误差，避免卡在边界再次 400。
        safety_margin = min(512, max(32, context_length // 100))
        remaining = context_length - prompt_tokens - safety_margin

        if remaining <= 0:
            capped = 1
        else:
            capped = min(current, remaining)

        if capped >= current:
            return request_data

        retried_request = dict(request_data)
        retried_request["max_tokens"] = max(1, capped)
        self.debug.inference(
            "vLLM 请求输出预算按上下文自动收敛 "
            f"{current} -> {retried_request['max_tokens']} "
            f"(context={context_length}, prompt_est={prompt_tokens}, margin={safety_margin})"
        )
        return retried_request

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
            with httpx.Client(timeout=10, trust_env=False) as client:
                # 尝试健康检查
                base_url = self._api_url
                if base_url.endswith("/v1"):
                    base_url = base_url[:-3]

                try:
                    health_resp = client.get(f"{base_url}/health")
                    if health_resp.status_code == 200:
                        self.debug.model_loading(
                            f"vLLM 健康检查通过: {base_url}/health"
                        )
                except Exception:
                    pass

                # 尝试获取模型列表验证连接
                response = client.get(f"{self._api_url}/models")

                if response.status_code == 200:
                    models = response.json()
                    remote_model_name = self._resolve_remote_model_name(models)
                    if remote_model_name:
                        self._model_name = remote_model_name
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

    @staticmethod
    def _extract_error_text(response) -> str:
        """Safely read error bodies from streaming or non-streaming responses."""
        return _extract_error_text_from_response(response)

    @staticmethod
    def _resolve_remote_model_name(models_payload: dict) -> Optional[str]:
        data = models_payload.get("data")
        if not isinstance(data, list):
            return None

        for item in data:
            if isinstance(item, dict) and item.get("id"):
                return str(item["id"])

        return None

    @staticmethod
    def _extract_max_tokens_limit(error_text: str) -> Optional[int]:
        match = re.search(
            r"max_model_len=max_total_tokens=(\d+)|max_tokens\s*=\s*\d+\s*cannot be greater than\s*(\d+)",
            error_text,
        )
        if not match:
            return None

        for group in match.groups():
            if group is not None:
                return int(group)

        return None

    @staticmethod
    def _extract_context_length_from_error(error_text: str) -> Optional[int]:
        match = re.search(r"context length is only\s+(\d+)\s+tokens", error_text)
        if match:
            return int(match.group(1))
        return None

    def _maybe_retry_with_capped_max_tokens(
        self,
        request_data: dict,
        error_text: str,
        retry_fn,
    ):
        limit = self._extract_max_tokens_limit(error_text)
        current = request_data.get("max_tokens")

        if limit is None or not isinstance(current, int) or current <= limit:
            return None

        retried_request = dict(request_data)
        retried_request["max_tokens"] = limit
        self.debug.inference(
            f"vLLM 返回 max_tokens 超限，自动降级 {current} -> {limit} 后重试"
        )
        return retry_fn(retried_request)

    def _maybe_retry_with_context_budget(
        self,
        request_data: dict,
        error_text: str,
        retry_fn,
    ):
        """处理 prompt+output 总预算超限的报错。"""
        context_length = self._extract_context_length_from_error(error_text)
        current = request_data.get("max_tokens")

        if context_length is None or not isinstance(current, int) or current <= 1:
            return None

        updated_request = self._cap_max_tokens_to_context_budget(
            {**request_data, "max_tokens": min(current, context_length)}
        )
        new_max_tokens = updated_request.get("max_tokens")
        if not isinstance(new_max_tokens, int) or new_max_tokens >= current:
            return None

        self.debug.inference(
            "vLLM 返回上下文预算超限，自动按剩余上下文收敛输出预算 "
            f"{current} -> {new_max_tokens} 后重试"
        )
        return retry_fn(updated_request)

    def generate(
        self,
        messages: List[Message],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        stream: bool = False,
        tools: Optional[List[dict]] = None,
        request_id: Optional[str] = None,
        extra_body: Optional[dict] = None,
        runtime_context: Optional[str] = None,
        preserve_prompt_shape: bool = False,
    ) -> Union[GenerationResult, Generator[dict, None, None]]:
        """
        调用 vLLM 服务生成回复

        Runtime context and multimodal preparation are owned by LLMService.
        """
        import httpx

        # 构建消息列表 - 支持多模态内容
        def build_message_content(msg: Message) -> Union[str, List[dict]]:
            content = msg.content
            if isinstance(content, str):
                return content
            elif isinstance(content, list):
                return content
            else:
                return str(content)

        messages_data = [
            message_to_wire(m) for m in messages
        ]

        request_registration = None
        if stream and request_id:
            request_registration = register_request(request_id)

        # 构建请求
        request_data = {
            "model": self._model_name,
            "messages": messages_data,
            "temperature": (
                self.config.inference.temperature if temperature is None else temperature
            ),
            "top_p": self.config.inference.top_p if top_p is None else top_p,
            "max_tokens": resolve_effective_max_tokens(
                self.config, "vllm", max_new_tokens
            ),
            "stream": stream,
        }

        if self.config.inference.top_k is not None:
            request_data["top_k"] = self.config.inference.top_k
        if self.config.inference.repetition_penalty is not None:
            request_data["repetition_penalty"] = self.config.inference.repetition_penalty

        if extra_body:
            request_data.update(extra_body)

        # 注入需在 extra_body 合并之后；vLLM 的 StreamOptions 不支持
        # continuous_usage_stats，只请求最终 usage chunk
        if stream and self.config.inference.stream_usage_stats:
            request_data["stream_options"] = {"include_usage": True}

        request_data = self._cap_max_tokens_to_context_budget(request_data)

        if request_id:
            request_data["request_id"] = request_id

        # 添加工具
        if tools:
            request_data["tools"] = tools

        # 检查是否包含多模态图片内容
        def count_images_in_messages(msgs: List[dict]) -> int:
            count = 0
            for msg in msgs:
                content = msg.get("content")
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "image_url":
                            count += 1
            return count

        image_count = count_images_in_messages(messages_data)
        self.debug.inference(
            f"发送请求到 vLLM: {_request_log_summary(request_data)}"
        )

        if stream:
            return self._stream_generate(request_data, request_id, request_registration)
        else:
            return self._sync_generate(request_data)

    def _sync_generate(self, request_data: dict) -> GenerationResult:
        """同步生成"""
        import httpx
        import time

        start_time = time.perf_counter()

        with httpx.Client(timeout=self._timeout, trust_env=False) as client:
            response = client.post(
                f"{self._api_url}/chat/completions",
                json=request_data,
            )

            if response.status_code != 200:
                error_text = self._extract_error_text(response)
                retried = self._maybe_retry_with_capped_max_tokens(
                    request_data,
                    error_text,
                    self._sync_generate,
                )
                if retried is not None:
                    return retried
                retried = self._maybe_retry_with_context_budget(
                    request_data,
                    error_text,
                    self._sync_generate,
                )
                if retried is not None:
                    return retried
                raise RuntimeError(
                    f"vLLM 服务响应错误: {response.status_code} - {error_text}"
                )

            result = response.json()

        elapsed = time.perf_counter() - start_time

        # 解析响应
        choice = result.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = message.get("content") or ""
        reasoning_content = _extract_reasoning_content(message)
        tool_calls = message.get("tool_calls")

        finish_reason = choice.get("finish_reason", "stop")
        usage = result.get("usage")

        self.debug.inference(f"vLLM 服务响应完成，耗时: {elapsed:.2f}s")

        return GenerationResult(
            text=content,
            reasoning_content=reasoning_content,
            finish_reason=finish_reason,
            usage=usage,
            generation_time=elapsed,
            tool_calls=tool_calls,
        )

    def _stream_generate(
        self,
        request_data: dict,
        request_id: Optional[str] = None,
        request_registration: object | None = None,
    ) -> Generator[dict, None, None]:
        """流式生成，并支持通过 request_id 关闭上游 vLLM 连接。"""
        import httpx
        import json

        upstream_registration = None
        try:
            with httpx.Client(timeout=self._timeout, trust_env=False) as client:
                with client.stream(
                    "POST",
                    f"{self._api_url}/chat/completions",
                    json=request_data,
                ) as response:
                    if request_id:
                        upstream_registration = register_upstream_stream(
                            request_id,
                            "vllm",
                            lambda: response.close(),
                        )
                    if response.status_code != 200:
                        error_text = self._extract_error_text(response)
                        retried = self._maybe_retry_with_capped_max_tokens(
                            request_data,
                            error_text,
                            lambda data: self._stream_generate(
                                data, request_id, request_registration
                            ),
                        )
                        if retried is not None:
                            yield from retried
                            return
                        retried = self._maybe_retry_with_context_budget(
                            request_data,
                            error_text,
                            lambda data: self._stream_generate(
                                data, request_id, request_registration
                            ),
                        )
                        if retried is not None:
                            yield from retried
                            return
                        self.debug.inference(
                            f"vLLM 流式请求失败: {response.status_code} - {error_text}"
                        )
                        raise RuntimeError(
                            f"vLLM 服务响应错误: {response.status_code} - {error_text}"
                        )

                    buffer = ""
                    for chunk in response.iter_text():
                        buffer += chunk

                        # 解析 SSE 数据
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()

                            if line.startswith("data:"):
                                data = line[5:].lstrip()

                                if data == "[DONE]":
                                    yield _stream_termination_chunk(
                                        backend="vllm",
                                        request_id=request_id,
                                        done=True,
                                        residual=buffer,
                                    )
                                    return

                                try:
                                    parsed = json.loads(data)
                                    chunk_payload = _openai_stream_payload_to_chunk(parsed)
                                    if chunk_payload is not None:
                                        yield chunk_payload
                                except json.JSONDecodeError as e:
                                    self.debug.inference(
                                        f"JSON 解析错误: {e}, 数据: {redact_data_urls(data)}"
                                    )
                                    pass
                    yield _stream_termination_chunk(
                        backend="vllm",
                        request_id=request_id,
                        done=False,
                        residual=buffer,
                    )
        except Exception as e:
            if request_id and is_request_aborted(request_id):
                return
            safe_error = redact_data_urls(e)
            self.debug.inference(f"流式生成异常: {safe_error}")
            yield {
                "content": f"\n[流式生成错误] {type(e).__name__}: {safe_error}\n",
                "transport_error": f"{type(e).__name__}: {safe_error}",
                "reasoning": None,
            }
        finally:
            if request_id:
                unregister_upstream_stream(request_id, upstream_registration)
                unregister_request(request_id, request_registration)

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
        tools: Optional[List[dict]] = None,
        request_id: Optional[str] = None,
        extra_body: Optional[dict] = None,
        runtime_context: Optional[str] = None,
        preserve_prompt_shape: bool = False,
    ) -> Union[GenerationResult, Generator[dict, None, None]]:
        """生成模拟回复。"""
        # 获取最后一条用户消息
        user_message = ""
        for msg in reversed(messages):
            if msg.role == "user":
                user_message = msg.content
                break

        # 生成模拟回复
        mock_response = (
            f'[模拟回复] 您说的是: "{user_message[:50]}..."\n\n'
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

        self._api_url = os.environ.get(
            "CONTAINER_API_URL", self.config.container.api_url
        )
        self._model_name = (
            self.config.container.model_name or self.config.model.model_name
        )
        self._timeout = self.config.container.timeout
        self._loaded = False
        self._session = None

    def _cap_max_tokens_to_context_budget(self, request_data: dict) -> dict:
        """容器模式复用 vLLM 的预算裁剪逻辑。"""
        helper = VLLMProvider(self.config)
        return helper._cap_max_tokens_to_context_budget(request_data)

    def _maybe_retry_with_context_budget(
        self,
        request_data: dict,
        error_text: str,
        retry_fn,
    ):
        helper = VLLMProvider(self.config)
        return helper._maybe_retry_with_context_budget(request_data, error_text, retry_fn)

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
            with httpx.Client(timeout=10, trust_env=False) as client:
                # 尝试获取模型列表验证连接
                response = client.get(f"{self._api_url}/models")

                if response.status_code == 200:
                    models = response.json()
                    remote_model_name = self._resolve_remote_model_name(models)
                    if remote_model_name:
                        self._model_name = remote_model_name
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

    @staticmethod
    def _extract_error_text(response) -> str:
        """Read error body safely for both normal and streaming responses."""
        return _extract_error_text_from_response(response)

    @staticmethod
    def _resolve_remote_model_name(models_payload: dict) -> Optional[str]:
        data = models_payload.get("data")
        if not isinstance(data, list):
            return None

        for item in data:
            if isinstance(item, dict) and item.get("id"):
                return str(item["id"])

        return None

    def generate(
        self,
        messages: List[Message],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        stream: bool = False,
        tools: Optional[List[dict]] = None,
        request_id: Optional[str] = None,
        extra_body: Optional[dict] = None,
        runtime_context: Optional[str] = None,
        preserve_prompt_shape: bool = False,
    ) -> Union[GenerationResult, Generator[dict, None, None]]:
        """
        调用容器生成回复
        """
        # Runtime context and multimodal preparation are owned by LLMService.
        request_registration = None
        if stream and request_id:
            request_registration = register_request(request_id)

        request_data = {
            "model": self._model_name,
            "messages": [message_to_wire(m) for m in messages],
            "temperature": (
                self.config.inference.temperature if temperature is None else temperature
            ),
            "top_p": self.config.inference.top_p if top_p is None else top_p,
            "max_tokens": resolve_effective_max_tokens(
                self.config, "openai_proxy", max_new_tokens
            ),
            "stream": stream,
        }

        if extra_body:
            request_data.update(extra_body)

        request_data = self._cap_max_tokens_to_context_budget(request_data)

        if tools:
            request_data["tools"] = tools

        if request_id:
            request_data["request_id"] = request_id

        self.debug.inference(
            f"发送请求到容器: {_request_log_summary(request_data)}"
        )

        if stream:
            return self._stream_generate(request_data, request_id, request_registration)
        else:
            return self._sync_generate(request_data)

    def _sync_generate(self, request_data: dict) -> GenerationResult:
        """同步生成"""
        import httpx
        import json
        import time

        start_time = time.perf_counter()

        with httpx.Client(timeout=self._timeout, trust_env=False) as client:
            response = client.post(
                f"{self._api_url}/chat/completions",
                json=request_data,
            )

            if response.status_code != 200:
                error_text = self._extract_error_text(response)
                retried = self._maybe_retry_with_context_budget(
                    request_data,
                    error_text,
                    self._sync_generate,
                )
                if retried is not None:
                    return retried
                raise RuntimeError(
                    f"容器响应错误: {response.status_code} - {error_text}"
                )

            result = response.json()

        elapsed = time.perf_counter() - start_time

        # 解析响应
        choice = result.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = message.get("content", "")
        reasoning_content = _extract_reasoning_content(message)
        tool_calls = message.get("tool_calls")
        finish_reason = choice.get("finish_reason", "stop")
        usage = result.get("usage")

        self.debug.inference(f"容器响应完成，耗时: {elapsed:.2f}s")

        return GenerationResult(
            text=content,
            reasoning_content=reasoning_content,
            finish_reason=finish_reason,
            usage=usage,
            generation_time=elapsed,
            tool_calls=tool_calls,
        )

    def _stream_generate(
        self,
        request_data: dict,
        request_id: Optional[str] = None,
        request_registration: object | None = None,
    ) -> Generator[dict, None, None]:
        """流式生成，并支持通过 request_id 关闭上游连接。"""
        import httpx

        upstream_registration = None
        try:
            with httpx.Client(timeout=self._timeout, trust_env=False) as client:
                with client.stream(
                    "POST",
                    f"{self._api_url}/chat/completions",
                    json=request_data,
                ) as response:
                    if request_id:
                        upstream_registration = register_upstream_stream(
                            request_id,
                            "openai_proxy",
                            lambda: response.close(),
                        )
                    if response.status_code != 200:
                        error_text = self._extract_error_text(response)
                        retried = self._maybe_retry_with_context_budget(
                            request_data,
                            error_text,
                            lambda data: self._stream_generate(
                                data, request_id, request_registration
                            ),
                        )
                        if retried is not None:
                            yield from retried
                            return
                        raise RuntimeError(
                            f"容器响应错误: {response.status_code} - {error_text}"
                        )

                    buffer = ""
                    for chunk in response.iter_text():
                        buffer += chunk

                        # 解析 SSE 数据
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()

                            if line.startswith("data:"):
                                data = line[5:].lstrip()

                                if data == "[DONE]":
                                    yield _stream_termination_chunk(
                                        backend="openai_proxy",
                                        request_id=request_id,
                                        done=True,
                                        residual=buffer,
                                    )
                                    return

                                try:
                                    parsed = json.loads(data)
                                    chunk_payload = _openai_stream_payload_to_chunk(parsed)
                                    if chunk_payload is not None:
                                        yield chunk_payload
                                except json.JSONDecodeError:
                                    pass
                    yield _stream_termination_chunk(
                        backend="openai_proxy",
                        request_id=request_id,
                        done=False,
                        residual=buffer,
                    )
        except Exception as e:
            if request_id and is_request_aborted(request_id):
                return
            safe_error = redact_data_urls(e)
            self.debug.inference(f"容器流式生成异常: {safe_error}")
            yield {
                "content": f"\n[流式生成错误] {type(e).__name__}: {safe_error}\n",
                "transport_error": f"{type(e).__name__}: {safe_error}",
                "reasoning": None,
            }
        finally:
            if request_id:
                unregister_upstream_stream(request_id, upstream_registration)
                unregister_request(request_id, request_registration)

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
        self._active_backend: Optional[str] = None
        self._stream_usage_lock = threading.Lock()
        self._stream_usage_requests: dict[str, dict] = {}

    def initialize(self) -> bool:
        """
        初始化服务，加载模型

        推理后端自动检测优先级：
        1. 如果 use_mock=True，使用模拟模式
        2. 尝试连接 SGLang 推理服务器（主力后端）
        3. 尝试连接 vLLM 推理服务器
        4. 尝试 OpenAI 兼容服务代理（容器模式）
        5. 回退到模拟模式

        Returns:
            bool: 初始化是否成功
        """
        forced_backend = (os.environ.get("LLM_BACKEND") or "").strip().lower()

        # 1. 模拟模式（测试用）
        if self._use_mock:
            self._provider = MockLLMProvider(self.config)
            self._active_backend = "mock"
            return self._provider.load_model()

        if forced_backend:
            provider_map = {
                "sglang": (SGLangProvider, "sglang"),
                "vllm": (VLLMProvider, "vllm"),
                "openai": (OpenAICompatibleProxyProvider, "openai_proxy"),
                "openai_proxy": (OpenAICompatibleProxyProvider, "openai_proxy"),
                "container": (OpenAICompatibleProxyProvider, "openai_proxy"),
                "mock": (MockLLMProvider, "mock"),
            }
            provider_info = provider_map.get(forced_backend)
            if provider_info is None:
                logger.warning(
                    f"未知 LLM_BACKEND={forced_backend}，将回退到自动检测模式"
                )
            else:
                provider_cls, backend_name = provider_info
                self.debug.model_loading(
                    f"检测到环境变量 LLM_BACKEND={forced_backend}，强制使用 {backend_name}"
                )
                self._provider = provider_cls(self.config)
                if self._provider.load_model():
                    self._active_backend = backend_name
                    return True
                logger.error(f"强制指定的推理后端 {backend_name} 连接失败")
                return False

        # 2. SGLang（主推理后端，优先尝试）
        self.debug.model_loading("尝试连接 SGLang 推理后端...")
        self._provider = SGLangProvider(self.config)
        if self._provider.load_model():
            self._active_backend = "sglang"
            return True
        logger.info("SGLang 未运行，尝试 vLLM...")

        # 3. vLLM 推理后端
        self.debug.model_loading("尝试连接 vLLM 推理后端...")
        self._provider = VLLMProvider(self.config)
        if self._provider.load_model():
            self._active_backend = "vllm"
            return True
        logger.info("vLLM 未运行，尝试容器/代理模式...")

        # 4. OpenAI 兼容服务代理（用于对接其他服务）
        if self.config.container.enabled:
            self.debug.model_loading("尝试使用 OpenAI 兼容服务代理")
            self._provider = OpenAICompatibleProxyProvider(self.config)
            if self._provider.load_model():
                self._active_backend = "openai_proxy"
                return True
            logger.warning("容器/服务代理连接失败，尝试降级")

        # 5. 回退到模拟模式
        logger.warning("所有推理后端均不可用，回退到模拟模式")
        self._provider = MockLLMProvider(self.config)
        self._active_backend = "mock"
        return self._provider.load_model()

    def chat(
        self,
        messages: List[Message],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        stream: bool = False,
        tools: Optional[List[dict]] = None,
        request_id: Optional[str] = None,
        extra_body: Optional[dict] = None,
        runtime_context: Optional[str] = None,
        preserve_prompt_shape: bool = False,
        stream_usage_id: Optional[str] = None,
    ) -> Union[GenerationResult, Generator[dict, None, None]]:
        """
        对话接口

        Args:
            messages: 对话消息列表
            max_new_tokens: 最大生成 token 数
            temperature: 采样温度
            top_p: Top-P 采样参数
            stream: 是否流式输出
            request_id: Optional request ID for abort tracking (for stream mode)

        Returns:
            生成结果或流式生成器 (字典形式 {"content": str, "reasoning": str})
        """
        if self._provider is None or not self._provider.is_loaded():
            raise RuntimeError("服务未初始化，请先调用 initialize()")

        self.debug.inference(
            f"收到对话请求，消息数: {len(messages)}, stream: {stream}, request_id: {request_id}"
        )

        if preserve_prompt_shape:
            prepared_messages = messages
        else:
            runtime_messages = _inject_runtime_context(messages, runtime_context)
            prepared_messages = prepare_multimodal_messages(
                runtime_messages,
                max_image_dimension=self.config.model.max_image_dimension,
                debug=self.debug,
            )

        if stream and stream_usage_id is not None:
            request_snapshot = copy.deepcopy({
                "messages": [
                    message_to_wire(m) for m in prepared_messages
                ],
                "max_new_tokens": resolve_effective_max_tokens(
                    self.config, self._active_backend, max_new_tokens
                ),
                "temperature": (
                    self.config.inference.temperature
                    if temperature is None
                    else temperature
                ),
                "top_p": self.config.inference.top_p if top_p is None else top_p,
                # 权威 usage（来自推理后端 stream_options chunk），None 表示尚未捕获
                "usage": None,
            })
            with self._stream_usage_lock:
                self._stream_usage_requests[stream_usage_id] = request_snapshot

        result = self._provider.generate(
            messages=prepared_messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            stream=stream,
            tools=tools,
            request_id=request_id,
            extra_body=extra_body,
            runtime_context=runtime_context,
            preserve_prompt_shape=preserve_prompt_shape,
        )
        if stream and stream_usage_id is not None:
            return self._wrap_stream_usage_capture(result, stream_usage_id)
        return result

    def _wrap_stream_usage_capture(self, stream, stream_usage_id: str):
        """捕获推理后端返回的权威 usage chunk（last-write-wins）到 usage snapshot。

        continuous_usage_stats 的逐 chunk usage 是累计值，因此直接覆盖而非累加。
        """
        def wrapped():
            for chunk in stream:
                if isinstance(chunk, dict) and isinstance(chunk.get("usage"), dict):
                    with self._stream_usage_lock:
                        entry = self._stream_usage_requests.get(stream_usage_id)
                        if entry is not None:
                            entry["usage"] = chunk["usage"]
                yield chunk
        return wrapped()

    async def chat_async(
        self,
        messages: List[Message],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        tools: Optional[List[dict]] = None,
        extra_body: Optional[dict] = None,
        runtime_context: Optional[str] = None,
        preserve_prompt_shape: bool = False,
        request_id: Optional[str] = None,
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
                extra_body=extra_body,
                runtime_context=runtime_context,
                preserve_prompt_shape=preserve_prompt_shape,
                request_id=request_id,
            ),
        )
        # 类型断言：stream=False 时返回 GenerationResult
        return result  # type: ignore[return-value]

    async def chat_stream_async(
        self,
        messages: List[Message],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        tools: Optional[List[dict]] = None,
        request_id: Optional[str] = None,
        extra_body: Optional[dict] = None,
        runtime_context: Optional[str] = None,
        preserve_prompt_shape: bool = False,
    ) -> AsyncGenerator[dict, None]:
        """
        异步流式对话接口

        将同步流式生成器包装为异步生成器。

        Args:
            request_id: Optional request ID for abort tracking
        """
        # 注意：直接在事件循环里迭代同步生成器会阻塞整个 FastAPI worker。
        # 这里把同步生成器放到后台线程里跑，用 asyncio.Queue 转发，保证 SSE 流式不阻塞。
        generator = self.chat(
            messages=messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            stream=True,
            tools=tools,
            request_id=request_id,
            extra_body=extra_body,
            runtime_context=runtime_context,
            preserve_prompt_shape=preserve_prompt_shape,
        )

        q: asyncio.Queue[Optional[dict]] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        worker_stop = threading.Event()
        stream_finished = False
        abort_issued = False

        def _enqueue(item: Optional[dict]) -> None:
            if worker_stop.is_set():
                return
            try:
                loop.call_soon_threadsafe(q.put_nowait, item)
            except RuntimeError:
                # The request task/loop may already be gone after a client
                # disconnect. The upstream closer still handles GPU cleanup.
                return

        def _worker():
            try:
                for chunk in generator:  # type: ignore[union-attr]
                    _enqueue(chunk)
            except Exception as e:
                safe_error = redact_data_urls(e)
                _enqueue(
                    {
                        "content": f"\n[stream-error] {type(e).__name__}: {safe_error}\n",
                        "transport_error": f"{type(e).__name__}: {safe_error}",
                        "reasoning": None,
                    }
                )
            finally:
                _enqueue(None)

        t = threading.Thread(target=_worker, name="llm-stream-worker", daemon=True)
        t.start()

        try:
            while True:
                item = await q.get()
                if item is None:
                    stream_finished = True
                    break
                yield item
        except asyncio.CancelledError:
            # FastAPI cancels this async generator when the browser closes the
            # SSE connection. Propagate that cancellation to the synchronous
            # provider and close its blocking upstream HTTP response.
            worker_stop.set()
            if request_id:
                abort_request_by_id(request_id)
                abort_issued = True
            raise
        finally:
            worker_stop.set()
            if request_id and not stream_finished and not abort_issued:
                abort_request_by_id(request_id)
            if t.is_alive():
                await asyncio.to_thread(t.join)

    @property
    def active_backend(self) -> Optional[str]:
        """返回当前活跃的推理后端名称：sglang / vllm / openai_proxy / mock / None"""
        return self._active_backend

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
        with self._stream_usage_lock:
            self._stream_usage_requests.clear()

    def discard_stream_usage(self, request_id: Optional[str]) -> None:
        """Release a stream's diagnostic snapshot when its owner finishes."""
        if request_id is None:
            return
        with self._stream_usage_lock:
            self._stream_usage_requests.pop(request_id, None)

    def record_stream_usage(self, request_id: Optional[str], usage: Optional[dict]) -> None:
        """记录权威 usage（如非流式回退调用返回的 usage）到对应的流式统计快照。

        未知 request_id 或非 dict usage 时静默忽略（no-op）。
        """
        if request_id is None or not isinstance(usage, dict):
            return
        with self._stream_usage_lock:
            entry = self._stream_usage_requests.get(request_id)
            if entry is not None:
                entry["usage"] = usage

    def get_stream_usage(
        self, output_text: str, request_id: Optional[str] = None
    ) -> dict | None:
        """
        流式调用完成后获取 token 统计。

        推理后端返回的权威 usage 优先：SGLang（>= 0.5.16）/ vLLM 支持通过
        stream_options（include_usage 等）在流式响应中返回服务端统计，
        含 prompt_tokens_details 的 image/audio/video/cached_tokens 多模态细分。

        本地估算仅作为显式标注的回退路径（source="estimated"），用于 mock
        后端、流被中止（无 usage chunk）或 inference.stream_usage_stats 关闭
        的场景。估算委托给与 auto-compact 阈值检查相同的 TokenCounter
        (`backend.compact.tokenizer`)，使终端
        "[Step N: ... | Input tokens ... | Output tokens ...]" 日志与触发
        auto-compact 的口径一致，且 CJK/多语言文本按多语言启发式计数
        （CJK ≈ 1 token/字，非 CJK ≈ 1 token/4 字符），而非英语中心的
        `len // 4`。
        """
        if request_id is None or not hasattr(self, "_stream_usage_requests"):
            return None
        with self._stream_usage_lock:
            request_data = self._stream_usage_requests.pop(request_id, None)
        if request_data is None:
            return None

        # 权威 usage：由 _wrap_stream_usage_capture / record_stream_usage 回填
        server_usage = request_data.get("usage")
        if isinstance(server_usage, dict):
            details = server_usage.get("prompt_tokens_details") or {}
            prompt_tokens = int(server_usage.get("prompt_tokens") or 0)
            completion_tokens = int(server_usage.get("completion_tokens") or 0)
            # usage.prompt_tokens 已包含 image_tokens，total 缺失时用求和兜底
            total_tokens = int(server_usage.get("total_tokens") or 0)
            return {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens or (prompt_tokens + completion_tokens),
                "image_tokens": int(details.get("image_tokens") or 0),
                "audio_tokens": int(details.get("audio_tokens") or 0),
                "video_tokens": int(details.get("video_tokens") or 0),
                "cached_tokens": int(details.get("cached_tokens") or 0),
                "source": "server",
            }

        messages = request_data.get("messages", [])

        image_count = 0
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "image_url":
                        image_count += 1

        try:
            from .compact.tokenizer import TokenCounter
            counter = TokenCounter.get()
            prompt_tokens = counter.count_messages(messages, image_count=image_count)
            completion_tokens = counter.count_text(output_text or "")
        except Exception as exc:
            logger.debug(f"get_stream_usage: TokenCounter fallback ({exc})")
            prompt_tokens = 0
            for msg in messages:
                content = msg.get("content", "")
                if isinstance(content, str):
                    prompt_tokens += len(content) // 4
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                prompt_tokens += len(item.get("text", "")) // 4
                            elif item.get("type") == "image_url":
                                prompt_tokens += 512
            completion_tokens = len(output_text or "") // 4

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "image_tokens": image_count * 512,
            "audio_tokens": 0,
            "video_tokens": 0,
            "cached_tokens": 0,
            "source": "estimated",
        }


# 全局服务实例
_llm_service: Optional[LLMService] = None


def abort_sglang_request(sglang_url: str, request_id: str) -> bool:
    """
    Call SGLang's /abort_request endpoint to actually stop the inference.

    Args:
        sglang_url: Base URL of SGLang server (e.g., http://localhost:30000)
        request_id: The request ID to abort

    Returns:
        bool: True if abort was successful, False otherwise
    """
    import httpx

    try:
        with httpx.Client(timeout=10, trust_env=False) as client:
            response = client.post(
                f"{sglang_url}/abort_request", json={"request_id": request_id}
            )
            if response.status_code == 200:
                logger.info(
                    f"Successfully sent abort to SGLang for request: {request_id}"
                )
                return True
            else:
                logger.warning(
                    f"SGLang abort failed: {response.status_code} - {response.text}"
                )
                return False
    except Exception as e:
        logger.error(f"Failed to call SGLang abort endpoint: {e}")
        return False


def abort_request_by_id(
    request_id: str,
    sglang_url: Optional[str] = None,
    fallback_to_abort_all: bool = False,
    require_provider_abort: bool = False,
) -> bool:
    """
    Abort a running request by its ID.

    Args:
        request_id: The request ID to abort
        sglang_url: Optional SGLang server URL. If provided, will also call SGLang's
                    /abort_request endpoint to actually stop GPU inference.
        require_provider_abort: For user-initiated Stop, keep the SGLang stream
                    active and report False if its targeted endpoint rejects the
                    abort, so the same request can be retried. Before upstream
                    registration, record a pending abort instead. Other
                    lifecycle paths retain best-effort local cancellation.

    Returns:
        bool: True when locally marked aborted; in strict SGLang mode, only
              when the targeted provider abort also succeeds.
    """
    with _upstream_streams_lock:
        upstream_entry = _active_upstream_streams.get(request_id)
        upstream_backend = upstream_entry[0] if upstream_entry is not None else None
    # vLLM/OpenAI-compatible servers abort when their upstream HTTP stream is
    # closed. They do not expose SGLang's /abort_request endpoint, so avoid a
    # misleading probe to the SGLang port for those requests. The stream may
    # already have unregistered by the time the UI's separate stop request
    # arrives; consult the active provider in that race as well.
    active_backend = None
    if _llm_service is not None:
        active_backend = _llm_service.active_backend
    request_backend = upstream_backend if upstream_backend is not None else active_backend
    if require_provider_abort and sglang_url and request_backend not in {"vllm", "openai_proxy"}:
        if upstream_entry is None:
            # A Stop can arrive after the Agent publishes its request ID but
            # before the blocking HTTP response is registered. Its pending
            # marker closes that response immediately when it appears.
            abort_request(request_id)
            abort_sglang_request(sglang_url, request_id)
            return is_request_aborted(request_id)
        if not abort_sglang_request(sglang_url, request_id):
            return False
        abort_request(request_id)
        return is_request_aborted(request_id)

    abort_request(request_id)
    if sglang_url and request_backend not in {"vllm", "openai_proxy"}:
        aborted = abort_sglang_request(sglang_url, request_id)
        if fallback_to_abort_all and not aborted:
            abort_sglang_all(sglang_url)

    return is_request_aborted(request_id)


def get_active_backend() -> Optional[str]:
    """Return the backend selected by the global LLM service, if initialized."""
    return _llm_service.active_backend if _llm_service is not None else None


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
