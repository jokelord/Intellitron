"""
LLM 服务模块

"""

import asyncio
import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncGenerator, Generator, List, Optional, Union, Union
import threading
import uuid

from .config import AppConfig, get_config
from .utils.logger import DebugLogger, get_logger, get_memory_usage

# 获取模块日志记录器
logger = get_logger(__name__)

# Active request tracking for abort mechanism
_active_requests: dict = {}
_requests_lock = threading.Lock()


def create_request_id() -> str:
    """Create a unique request ID for tracking."""
    return str(uuid.uuid4())


def register_request(request_id: str) -> None:
    """Register a new active request."""
    with _requests_lock:
        _active_requests[request_id] = {"aborted": False}


def abort_request(request_id: str) -> None:
    """Mark a request as aborted."""
    with _requests_lock:
        if request_id in _active_requests:
            _active_requests[request_id]["aborted"] = True


def is_request_aborted(request_id: str) -> bool:
    """Check if a request has been aborted."""
    with _requests_lock:
        return _active_requests.get(request_id, {}).get("aborted", False)


def unregister_request(request_id: str) -> None:
    """Remove a request from tracking."""
    with _requests_lock:
        _active_requests.pop(request_id, None)


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
        with httpx.Client(timeout=10) as client:
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
            with httpx.Client(timeout=10) as client:
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
        if stream and request_id:
            register_request(request_id)
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
            {"role": m.role, "content": build_message_content(m)} for m in messages
        ]

        # 构建请求
        request_data = {
            "model": self._model_name,
            "messages": messages_data,
            "temperature": temperature or self.config.inference.temperature,
            "top_p": top_p or self.config.inference.top_p,
            "max_new_tokens": max_new_tokens or self.config.inference.max_new_tokens,
            "stream": stream,
        }

        # 添加重复惩罚（防止模型生成重复内容）
        rep_penalty = self.config.inference.repetition_penalty
        if rep_penalty and rep_penalty != 1.0:
            request_data["repetition_penalty"] = rep_penalty

        # 添加转换后的工具
        if converted_tools:
            request_data["tools"] = converted_tools

        # ── 图片归一化：发给 SGLang 前的最后一道防线 ──
        # 扫描全部消息中的图片，对超出 max_image_dimension 的进行缩放重编码
        # 同时打印详细 per-image 尺寸/token 估算，便于排障
        def _normalize_and_log_images(msgs: List[dict]) -> int:
            """归一化消息中的图片尺寸，返回图片总数"""
            import base64 as _b64
            import io as _io

            try:
                from PIL import Image as _PIL
            except ImportError:
                return 0

            TAG = "[INFER-IMG]"
            max_dim = self.config.model.max_image_dimension or 0
            # Qwen3-VL visual token 估算: tokens ≈ pixels / 784
            PATCH_SQ = 784
            total_tokens_estimate = 0
            image_count = 0

            for i, msg in enumerate(msgs):
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for j, item in enumerate(content):
                    if not (isinstance(item, dict) and item.get("type") == "image_url"):
                        continue
                    url_data = item.get("image_url", {}).get("url", "")
                    if not url_data.startswith("data:"):
                        continue

                    image_count += 1
                    try:
                        # 解析 data URL
                        header, b64_str = url_data.split(",", 1)
                        # header 类似 "data:image/jpeg;base64"
                        mime = header.split(":")[1].split(";")[0]  # e.g. image/jpeg
                        raw_bytes = _b64.b64decode(b64_str)
                        kb = len(raw_bytes) / 1024

                        # PIL lazy open（仅读元数据，不解码像素）
                        img = _PIL.open(_io.BytesIO(raw_bytes))
                        w, h = img.size
                        orig_fmt = (img.format or "").lower()
                        pixels = w * h
                        tokens_est = pixels // PATCH_SQ
                        total_tokens_estimate += tokens_est

                        need_resize = max_dim > 0 and (w > max_dim or h > max_dim)

                        self.debug.inference(
                            f"{TAG} 消息{i}[{j}]: {w}x{h} {kb:.1f}KB "
                            f"({mime}) ≈ {tokens_est} visual tokens"
                            + (
                                f" ⚠️  超限 max_dim={max_dim}, 将缩放"
                                if need_resize
                                else ""
                            )
                        )

                        if need_resize:
                            import time as _t

                            _ts = _t.perf_counter()
                            img.load()
                            if w > h:
                                nw, nh = max_dim, int(h * max_dim / w)
                            else:
                                nh, nw = max_dim, int(w * max_dim / h)
                            img = img.resize((nw, nh), _PIL.Resampling.LANCZOS)
                            if img.mode in ("RGBA", "P"):
                                img = img.convert("RGB")
                            elif img.mode != "RGB":
                                img = img.convert("RGB")
                            out_buf = _io.BytesIO()
                            img.save(out_buf, format="JPEG", quality=85, optimize=False)
                            new_bytes = out_buf.getvalue()
                            new_b64 = _b64.b64encode(new_bytes).decode()
                            # 更新 messages_data in-place
                            item["image_url"]["url"] = (
                                f"data:image/jpeg;base64,{new_b64}"
                            )
                            new_tokens = (nw * nh) // PATCH_SQ
                            total_tokens_estimate = (
                                total_tokens_estimate - tokens_est + new_tokens
                            )
                            elapsed = (_t.perf_counter() - _ts) * 1000
                            self.debug.inference(
                                f"{TAG} 消息{i}[{j}]: 缩放归一化 {w}x{h}→{nw}x{nh} "
                                f"{kb:.1f}KB→{len(new_bytes) / 1024:.1f}KB "
                                f"tokens {tokens_est}→{new_tokens}, 耗时: {elapsed:.1f}ms"
                            )
                    except Exception as _e:
                        self.debug.inference(
                            f"{TAG} 消息{i}[{j}]: 图片归一化失败 ({type(_e).__name__}: {_e}), 跳过"
                        )

            if image_count > 0:
                self.debug.inference(
                    f"{TAG} 共 {image_count} 张图片, 估算 visual tokens: {total_tokens_estimate}"
                )
            return image_count

        # ── 历史图片剥离：仅保留最后一条 user 消息中的图片 ──
        # 适用场景：**普通多轮对话**（非 Agent）中，前轮 user 消息附带的图片不应在
        # 后续轮次重复上传，否则 visual token 随轮次线性增长。
        # 策略：将**非最后一条 user 消息**中的 image_url 替换为文字占位符 "[图片]"。
        #
        # 注意：Agent（smolagents）场景下，每个 step 的图片去重由
        # SmartToolCallingAgent.write_memory_to_messages() 在消息重建阶段完成，
        # 该函数在 Agent 调用链中起兜底防线作用（与 Agent 逻辑互不干扰）。
        def _strip_old_images(msgs: List[dict]) -> int:
            """剥离非最末 user 消息中的图片，返回被剥离的图片数"""
            TAG = "[INFER-IMG]"
            # 找到最后一条 user 消息的索引
            last_user_idx = -1
            for idx in range(len(msgs) - 1, -1, -1):
                if msgs[idx].get("role") == "user":
                    last_user_idx = idx
                    break
            stripped = 0
            for idx, msg in enumerate(msgs):
                if idx == last_user_idx:
                    continue  # 保留最新 user 消息的图片
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                # Agent 任务消息豁免：smolagents TaskStep.to_messages() 生成的消息
                # 第一个文本块固定以 "New task:" 开头。这类消息承载用户上传的任务图片，
                # 需要在整个 Agent 任务的所有步骤中保持可见（多步骤推理时 obs_text 会
                # 插在后面，导致 TaskStep 成为"非最末 user 消息"而被误删）。
                is_task_msg = any(
                    isinstance(item, dict)
                    and item.get("type") == "text"
                    and item.get("text", "").startswith("New task:")
                    for item in content
                )
                if is_task_msg:
                    continue  # 保留 Agent 任务消息中的图片
                new_content = []
                img_in_msg = 0
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "image_url":
                        img_in_msg += 1
                        stripped += 1
                    else:
                        new_content.append(item)
                if img_in_msg > 0:
                    # 在文本列表开头插入占位符
                    new_content.insert(
                        0, {"type": "text", "text": f"[图片×{img_in_msg}]"}
                    )
                    msg["content"] = new_content
            return stripped

        _strip_old_images(messages_data)
        image_count = _normalize_and_log_images(messages_data)

        # ── [MSG-DUMP] 完整消息结构诊断（用于对比 normal vs cycle 模式） ──
        # 打印每条消息的 role、content 类型、文本摘要、图片位置
        if image_count > 0:
            dump_lines = [
                f"[MSG-DUMP] === 发送到 SGLang 的完整消息结构 ({len(messages_data)} 条, {image_count} 张图片) ==="
            ]
            for mi, msg in enumerate(messages_data):
                role = msg.get("role", "?")
                content = msg.get("content")
                if isinstance(content, str):
                    preview = content[:120].replace("\n", "\\n")
                    dump_lines.append(
                        f'[MSG-DUMP]   msg[{mi}] role={role} content=str len={len(content)} preview="{preview}"'
                    )
                elif isinstance(content, list):
                    dump_lines.append(
                        f"[MSG-DUMP]   msg[{mi}] role={role} content=list ({len(content)} items):"
                    )
                    for ci, item in enumerate(content):
                        if isinstance(item, dict):
                            itype = item.get("type", "?")
                            if itype == "text":
                                txt = item.get("text", "")
                                preview = txt[:120].replace("\n", "\\n")
                                dump_lines.append(
                                    f'[MSG-DUMP]     [{ci}] type=text len={len(txt)} "{preview}"'
                                )
                            elif itype == "image_url":
                                url = item.get("image_url", {}).get("url", "")
                                if url.startswith("data:"):
                                    header = url.split(",", 1)[0]
                                    b64len = (
                                        len(url.split(",", 1)[1]) if "," in url else 0
                                    )
                                    dump_lines.append(
                                        f"[MSG-DUMP]     [{ci}] type=image_url header={header} b64_len={b64len}"
                                    )
                                else:
                                    dump_lines.append(
                                        f"[MSG-DUMP]     [{ci}] type=image_url url={url[:80]}"
                                    )
                            else:
                                dump_lines.append(
                                    f"[MSG-DUMP]     [{ci}] type={itype} keys={list(item.keys())}"
                                )
                        else:
                            dump_lines.append(
                                f"[MSG-DUMP]     [{ci}] raw={type(item).__name__}"
                            )
                else:
                    dump_lines.append(
                        f"[MSG-DUMP]   msg[{mi}] role={role} content={type(content).__name__}"
                    )
            dump_lines.append("[MSG-DUMP] === END ===")
            for line in dump_lines:
                logger.info(line)

        # 调试：非图片请求打印请求体
        if image_count > 0:
            self.debug.inference(f"发送请求到 SGLang: (含 {image_count} 张图片)")
        else:
            self.debug.inference(
                f"发送请求到 SGLang: {json.dumps(request_data, indent=2)}"
            )

        if stream:
            return self._stream_generate(request_data, request_id)
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
                raise RuntimeError(
                    f"SGLang 服务响应错误: {response.status_code} - {response.text}"
                )

            result = response.json()

        elapsed = time.perf_counter() - start_time

        # 解析响应
        choice = result.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = message.get("content") or ""
        reasoning_content = message.get("reasoning_content")

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
        self, request_data: dict, request_id: Optional[str] = None
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
        try:
            with httpx.Client(timeout=self._timeout) as client:
                request_start = time.perf_counter()
                with client.stream(
                    "POST",
                    f"{self._api_url}/chat/completions",
                    json=request_data,
                ) as response:
                    request_time = time.perf_counter() - request_start
                    self.debug.inference(
                        f"SGLang 请求耗时: {request_time:.2f}s (建立连接)"
                    )

                    if response.status_code != 200:
                        error_text = response.text
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

                            if line.startswith("data: "):
                                data = line[6:]

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
                                    delta = parsed.get("choices", [{}])[0].get(
                                        "delta", {}
                                    )
                                    content = delta.get("content")
                                    reasoning = delta.get("reasoning_content")
                                    tool_calls = delta.get("tool_calls")
                                    if content or reasoning or tool_calls:
                                        # 返回结构化数据而不是单纯字符串
                                        yield {
                                            "content": content,
                                            "reasoning": reasoning,
                                            "tool_calls": tool_calls,
                                        }
                                except json.JSONDecodeError as e:
                                    self.debug.inference(
                                        f"JSON 解析错误: {e}, 数据: {data}"
                                    )
                                    pass
        except Exception as e:
            self.debug.inference(f"流式生成异常: {e}")
            # 返回错误信息作为流式内容
            yield {
                "content": f"\n[流式生成错误] {type(e).__name__}: {e}\n",
                "reasoning": None,
            }
        finally:
            if request_id:
                unregister_request(request_id)

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
                        self.debug.model_loading(
                            f"vLLM 健康检查通过: {base_url}/health"
                        )
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
            {"role": m.role, "content": build_message_content(m)} for m in messages
        ]

        # 构建请求
        request_data = {
            "model": self._model_name,
            "messages": messages_data,
            "temperature": temperature or self.config.inference.temperature,
            "top_p": top_p or self.config.inference.top_p,
            "max_tokens": max_new_tokens or self.config.inference.max_new_tokens,
            "stream": stream,
        }

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
        if image_count > 0:
            self.debug.inference(f"发送请求到 vLLM: (含 {image_count} 张图片)")
        else:
            self.debug.inference(
                f"发送请求到 vLLM: {json.dumps(request_data, indent=2)}"
            )

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
                raise RuntimeError(
                    f"vLLM 服务响应错误: {response.status_code} - {response.text}"
                )

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

                            if line.startswith("data: "):
                                data = line[6:]

                                if data == "[DONE]":
                                    return

                                try:
                                    parsed = json.loads(data)
                                    delta = parsed.get("choices", [{}])[0].get(
                                        "delta", {}
                                    )
                                    content = delta.get("content")
                                    reasoning = delta.get("reasoning_content")
                                    if content or reasoning:
                                        yield {
                                            "content": content,
                                            "reasoning": reasoning,
                                        }
                                except json.JSONDecodeError as e:
                                    self.debug.inference(
                                        f"JSON 解析错误: {e}, 数据: {data}"
                                    )
                                    pass
        except Exception as e:
            self.debug.inference(f"流式生成异常: {e}")
            yield {
                "content": f"\n[流式生成错误] {type(e).__name__}: {e}\n",
                "reasoning": None,
            }

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

        self._api_url = self.config.container.api_url
        self._model_name = (
            self.config.container.model_name or self.config.model.model_name
        )
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

        self.debug.inference(
            f"发送请求到容器: model={self._model_name}, stream={stream}"
        )

        if stream:
            self._last_stream_request = request_data
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
                raise RuntimeError(
                    f"容器响应错误: {response.status_code} - {response.text}"
                )

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
                                content = delta.get("content") or delta.get(
                                    "reasoning_content"
                                )
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
        self._active_backend: Optional[str] = None

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
        # 1. 模拟模式（测试用）
        if self._use_mock:
            self._provider = MockLLMProvider(self.config)
            self._active_backend = "mock"
            return self._provider.load_model()

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

        if stream:
            self._last_stream_request = {
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                "max_new_tokens": max_new_tokens
                or self.config.inference.max_new_tokens,
                "temperature": temperature or self.config.inference.temperature,
                "top_p": top_p or self.config.inference.top_p,
            }

        return self._provider.generate(
            messages=messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            stream=stream,
            tools=tools,
            request_id=request_id,
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
        )

        q: asyncio.Queue[Optional[dict]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _worker():
            try:
                for chunk in generator:  # type: ignore[union-attr]
                    loop.call_soon_threadsafe(q.put_nowait, chunk)
            except Exception as e:
                loop.call_soon_threadsafe(
                    q.put_nowait,
                    {
                        "content": f"\n[stream-error] {type(e).__name__}: {e}\n",
                        "reasoning": None,
                    },
                )
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

    def get_stream_usage(self, output_text: str) -> dict | None:
        """
        流式调用完成后获取 token 统计。
        由于 SGLang 不在流式响应中返回 usage，需要本地估算。
        """
        if not hasattr(self, "_last_stream_request"):
            return None

        request_data = self._last_stream_request

        prompt_tokens = 0
        for msg in request_data.get("messages", []):
            content = msg.get("content", "")
            if isinstance(content, str):
                prompt_tokens += len(content) // 4
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text = item.get("text", "")
                            prompt_tokens += len(text) // 4
                        elif item.get("type") == "image_url":
                            url = item.get("image_url", {}).get("url", "")
                            if url.startswith("data:"):
                                b64_part = url.split(",", 1)[1] if "," in url else ""
                                prompt_tokens += len(b64_part) * 3 // 4 // 4

        completion_tokens = len(output_text) // 4

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
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
        with httpx.Client(timeout=10) as client:
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


def abort_request_by_id(request_id: str, sglang_url: Optional[str] = None) -> bool:
    """
    Abort a running request by its ID.

    Args:
        request_id: The request ID to abort
        sglang_url: Optional SGLang server URL. If provided, will also call SGLang's
                    /abort_request endpoint to actually stop GPU inference.

    Returns:
        bool: True if the request was found and marked as aborted, False otherwise
    """
    abort_request(request_id)

    if sglang_url:
        abort_sglang_request(sglang_url, request_id)
        # Also try abort_all as fallback to ensure GPU stops generating
        abort_sglang_all(sglang_url)

    return is_request_aborted(request_id)


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
