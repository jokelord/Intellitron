"""
聊天 API 路由模块

实现 OpenAI 兼容的聊天 API 接口，支持：
- POST /v1/chat/completions: 对话补全（支持流式）
- GET /v1/models: 获取可用模型列表
"""

import json
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..config import get_config
from ..llm_service import LLMService, Message, get_llm_service
from ..utils.logger import DebugLogger, get_logger

# 创建路由器
router = APIRouter(prefix="/v1", tags=["chat"])

# 获取日志记录器
logger = get_logger(__name__)

# 获取配置
config = get_config()
debug = DebugLogger(config.debug)


def convert_tools_for_sglang(tools: List[dict]) -> List[dict]:
    """
    将工具列表转换为 SGLang 兼容格式。

    SGLang 在配置 --tool-call-parser 后支持标准 OpenAI function calling 格式：
    {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    直接透传即可。
    """
    if not tools:
        return []

    debug.api(
        f"原始工具列表 ({len(tools)} 个): {[t.get('name', t.get('type', 'unknown')) for t in tools]}"
    )
    converted = []

    for tool in tools:
        tool_type = tool.get("type")

        if tool_type == "function" and "function" in tool:
            # 标准 OpenAI function 格式，直接透传
            converted.append(tool)
            func_name = tool["function"].get("name", "unknown")
            debug.api(f"透传 function 工具: {func_name}")
        elif tool_type in ["web_search_preview", "code_interpreter"]:
            # SGLang 原生工具类型
            converted.append(tool)
            debug.api(f"保留原生工具: {tool_type}")
        else:
            tool_name = tool.get("name", tool_type or "unknown")
            debug.api(f"跳过不支持的工具格式: {tool_name}")

    debug.api(f"转换结果: {len(tools)} -> {len(converted)}")
    return converted


# ============================================================
# 请求/响应模型（OpenAI 兼容格式）
# ============================================================


class ChatMessage(BaseModel):
    """聊天消息"""

    role: str = Field(..., description="消息角色: system/user/assistant")
    content: Union[str, List[dict]] = Field(
        ..., description="消息内容 (字符串或列表，支持多模态)"
    )
    reasoning_content: Optional[str] = Field(default=None, description="推理过程内容")


class ChatCompletionRequest(BaseModel):
    """聊天补全请求（OpenAI 兼容）"""

    model: str = Field(default="qwen3-next", description="模型名称")
    messages: List[ChatMessage] = Field(..., description="对话消息列表")
    temperature: Optional[float] = Field(
        default=None, ge=0, le=2, description="采样温度"
    )
    top_p: Optional[float] = Field(default=None, ge=0, le=1, description="Top-P 采样")
    max_tokens: Optional[int] = Field(
        default=None, ge=1, description="最大生成 token 数"
    )
    stream: bool = Field(default=False, description="是否流式输出")
    tools: Optional[List[dict]] = Field(default=None, description="可用工具列表")
    tool_choice: Optional[str] = Field(default=None, description="工具选择策略")


class ChatCompletionChoice(BaseModel):
    """聊天补全选项"""

    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class ChatCompletionUsage(BaseModel):
    """Token 使用统计"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    """聊天补全响应（OpenAI 兼容）"""

    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:8]}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionChoice]
    usage: ChatCompletionUsage


class ChatCompletionChunkDelta(BaseModel):
    """流式响应增量"""

    role: Optional[str] = None
    content: Optional[str] = None
    reasoning_content: Optional[str] = None


class ChatCompletionChunkChoice(BaseModel):
    """流式响应选项"""

    index: int = 0
    delta: ChatCompletionChunkDelta
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    """流式响应块（OpenAI 兼容）"""

    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[ChatCompletionChunkChoice]


class ModelInfo(BaseModel):
    """模型信息"""

    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "local"
    context_length: Optional[int] = Field(
        default=None, description="模型支持的最大上下文长度"
    )


class ModelsResponse(BaseModel):
    """模型列表响应"""

    object: str = "list"
    data: List[ModelInfo]


# ============================================================
# API 端点
# ============================================================


@router.post("/chat/completions", response_model=ChatCompletionResponse)
async def create_chat_completion(request: ChatCompletionRequest):
    """
    创建聊天补全

    兼容 OpenAI Chat Completions API 格式。

    Args:
        request: 聊天补全请求

    Returns:
        ChatCompletionResponse 或 StreamingResponse（流式）
    """
    debug.api(
        f"收到聊天请求: model={request.model}, messages_count={len(request.messages)}"
    )
    debug.api(
        f"参数: temperature={request.temperature}, top_p={request.top_p}, stream={request.stream}"
    )

    # 转换工具格式（兼容SGLang）
    converted_tools = None
    if request.tools:
        converted_tools = convert_tools_for_sglang(request.tools)
        debug.api(f"转换工具: {len(request.tools)} -> {len(converted_tools)}")

    # 获取 LLM 服务
    service = get_llm_service()

    if not service.is_ready:
        debug.api("LLM 服务未就绪，尝试初始化...")
        if not service.initialize():
            raise HTTPException(status_code=503, detail="模型服务不可用")

    # 转换消息格式
    messages = [Message(role=msg.role, content=msg.content) for msg in request.messages]

    # 处理流式请求
    if request.stream:
        debug.api("使用流式输出模式")
        return StreamingResponse(
            _generate_stream_response(
                service=service,
                messages=messages,
                model=request.model,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                top_p=request.top_p,
                tools=converted_tools,
            ),
            media_type="text/event-stream",
        )

    # 非流式请求
    try:
        result = await service.chat_async(
            messages=messages,
            max_new_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            tools=converted_tools,
        )

        debug.api(
            f"生成完成: text_length={len(result.text)}, time={result.generation_time:.2f}s"
        )

        return ChatCompletionResponse(
            model=request.model,
            choices=[
                ChatCompletionChoice(
                    message=ChatMessage(
                        role="assistant",
                        content=result.text,
                        reasoning_content=result.reasoning_content,
                    ),
                    finish_reason=result.finish_reason,
                )
            ],
            usage=ChatCompletionUsage(
                prompt_tokens=usage.get("prompt_tokens", 0)
                if (usage := result.usage)
                else 0,
                completion_tokens=usage.get("completion_tokens", 0)
                if (usage := result.usage)
                else 0,
                total_tokens=usage.get("total_tokens", 0)
                if (usage := result.usage)
                else 0,
            ),
        )
    except Exception as e:
        logger.error(f"聊天生成失败: {e}")
        debug.api(f"生成异常: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"生成失败: {str(e)}")


async def _generate_stream_response(
    service: LLMService,
    messages: List[Message],
    model: str,
    max_tokens: Optional[int],
    temperature: Optional[float],
    top_p: Optional[float],
    tools: Optional[List[dict]] = None,
) -> AsyncGenerator[str, None]:
    """
    生成流式响应

    格式化为 SSE (Server-Sent Events) 格式，兼容 OpenAI 流式 API。
    """
    response_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
    created = int(time.time())

    # 发送角色信息（第一个块）
    first_chunk = ChatCompletionChunk(
        id=response_id,
        created=created,
        model=model,
        choices=[
            ChatCompletionChunkChoice(
                delta=ChatCompletionChunkDelta(role="assistant", content=""),
            )
        ],
    )
    yield f"data: {first_chunk.model_dump_json()}\n\n"

    # 流式生成内容
    try:
        async for chunk_data in service.chat_stream_async(
            messages=messages,
            max_new_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            tools=tools,
        ):
            chunk = ChatCompletionChunk(
                id=response_id,
                created=created,
                model=model,
                choices=[
                    ChatCompletionChunkChoice(
                        delta=ChatCompletionChunkDelta(
                            content=chunk_data.get("content"),
                            reasoning_content=chunk_data.get("reasoning"),
                        ),
                    )
                ],
            )
            yield f"data: {chunk.model_dump_json()}\n\n"
    except Exception as e:
        logger.error(f"流式生成异常: {e}")
        debug.api(f"流式生成异常: {type(e).__name__}: {e}")

    # 发送结束信号
    final_chunk = ChatCompletionChunk(
        id=response_id,
        created=created,
        model=model,
        choices=[
            ChatCompletionChunkChoice(
                delta=ChatCompletionChunkDelta(),
                finish_reason="stop",
            )
        ],
    )
    yield f"data: {final_chunk.model_dump_json()}\n\n"
    yield "data: [DONE]\n\n"


@router.get("/models", response_model=ModelsResponse)
async def list_models():
    """
    获取可用模型列表

    返回当前配置的模型信息。
    """
    debug.api("获取模型列表")

    # 根据活跃的推理后端确定上下文长度
    service = get_llm_service()
    if service.active_backend == "sglang":
        context_length = config.sglang.max_context_length
    elif service.active_backend == "vllm":
        context_length = config.vllm.max_model_len
    else:
        context_length = config.model.max_context_length

    return ModelsResponse(
        data=[
            ModelInfo(
                id=config.model.model_name,
                owned_by="nvidia",
                context_length=context_length,
            ),
            ModelInfo(
                id="qwen3-next",
                owned_by="nvidia",
                context_length=context_length,
            ),
        ]
    )


@router.get("/models/{model_id}", response_model=ModelInfo)
async def get_model(model_id: str):
    """
    获取指定模型信息

    Args:
        model_id: 模型 ID
    """
    debug.api(f"获取模型信息: {model_id}")

    # 根据活跃的推理后端确定上下文长度
    service = get_llm_service()
    if service.active_backend == "sglang":
        context_length = config.sglang.max_context_length
    elif service.active_backend == "vllm":
        context_length = config.vllm.max_model_len
    else:
        context_length = config.model.max_context_length

    if model_id in [config.model.model_name, "qwen3-next"]:
        return ModelInfo(
            id=model_id,
            owned_by="nvidia",
            context_length=context_length,
        )

    raise HTTPException(status_code=404, detail=f"模型不存在: {model_id}")


@router.post("/responses")
async def create_response(request: Request):
    """
    处理 /v1/responses 请求

    ClawdBot 使用的端点，支持灵活的请求格式
    自动适配并转换为标准的 Chat Completions 格式
    """
    try:
        # 获取原始请求体
        raw_body = await request.json()
        debug.api(
            f"收到 /responses 原始请求: {json.dumps(raw_body, indent=2, ensure_ascii=False)}"
        )

        # 尝试解析为标准的 ChatCompletionRequest
        try:
            chat_request = ChatCompletionRequest(**raw_body)
            debug.api(
                f"成功解析为标准格式: model={chat_request.model}, messages_count={len(chat_request.messages)}"
            )
        except Exception as parse_error:
            logger.warning(f"请求格式不符合标准 ChatCompletionRequest: {parse_error}")
            debug.api(f"解析错误详情: {parse_error}")

            # 尝试手动构建请求对象
            chat_request = _adapt_clawdbot_request(raw_body)
            debug.api(
                f"自适应解析完成: model={chat_request.model}, messages_count={len(chat_request.messages)}"
            )

        # 转换工具格式（兼容SGLang）
        converted_tools = None
        if chat_request.tools:
            converted_tools = convert_tools_for_sglang(chat_request.tools)
            debug.api(f"转换工具: {len(chat_request.tools)} -> {len(converted_tools)}")

        # 获取 LLM 服务
        service = get_llm_service()

        if not service.is_ready:
            debug.api("LLM 服务未就绪，尝试初始化...")
            if not service.initialize():
                logger.error("模型服务初始化失败")
                raise HTTPException(status_code=503, detail="模型服务不可用")

        # 转换消息格式
        messages = [
            Message(role=msg.role, content=msg.content) for msg in chat_request.messages
        ]

        # 处理流式请求
        if chat_request.stream:
            debug.api("使用流式输出模式")
            return StreamingResponse(
                _generate_stream_response(
                    service=service,
                    messages=messages,
                    model=chat_request.model,
                    max_tokens=chat_request.max_tokens,
                    temperature=chat_request.temperature,
                    top_p=chat_request.top_p,
                ),
                media_type="text/event-stream",
            )

        # 非流式请求
        debug.api("开始生成响应...")
        result = await service.chat_async(
            messages=messages,
            max_new_tokens=chat_request.max_tokens,
            temperature=chat_request.temperature,
            top_p=chat_request.top_p,
            tools=converted_tools,
        )

        debug.api(
            f"生成完成: tokens={result.usage.get('total_tokens', 'N/A') if result.usage else 'N/A'}"
        )

        # 转换 usage 为 ChatCompletionUsage 对象
        usage = ChatCompletionUsage()
        if result.usage:
            usage = ChatCompletionUsage(
                prompt_tokens=result.usage.get("prompt_tokens", 0),
                completion_tokens=result.usage.get("completion_tokens", 0),
                total_tokens=result.usage.get("total_tokens", 0),
            )

        # 构建响应，包含 reasoning_content（thinking 模型支持）
        response = ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex}",
            object="chat.completion",
            created=int(time.time()),
            model=chat_request.model,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatMessage(
                        role="assistant",
                        content=result.text,
                        reasoning_content=result.reasoning_content,
                    ),
                    finish_reason=result.finish_reason,
                )
            ],
            usage=usage,
        )

        debug.api(f"响应构建完成: finish_reason={result.finish_reason}")
        return response

    except HTTPException:
        # 重新抛出 HTTP 异常
        raise
    except Exception as e:
        logger.error(f"/responses 请求处理失败: {e}", exc_info=True)
        debug.api(f"错误: {e}")
        raise HTTPException(status_code=500, detail=f"生成失败: {str(e)}")


def _adapt_clawdbot_request(raw_body: dict) -> ChatCompletionRequest:
    """
    自适应解析 ClawdBot 的请求格式

    处理 ClawdBot 可能使用的非标准字段或格式
    """
    debug.api(f"ClawdBot 原始请求: {raw_body}")

    # 复制原始数据
    adapted = raw_body.copy()

    # 处理可能的字段映射
    # ClawdBot 可能使用不同的字段名

    # 确保必需字段存在
    if "model" not in adapted:
        adapted["model"] = "qwen3-next"  # 默认模型

    # 处理消息字段 - ClawdBot 可能使用不同的字段名或格式
    if "messages" not in adapted:
        # 尝试从其他字段提取消息
        messages = []

        # 检查是否有 prompt 或 content 字段
        if "prompt" in adapted:
            content = adapted["prompt"]
            if isinstance(content, list):
                # 如果 content 是列表，转换为字符串
                content = _extract_content_from_list(content)
            messages.append({"role": "user", "content": content})
        elif "content" in adapted:
            content = adapted["content"]
            if isinstance(content, list):
                content = _extract_content_from_list(content)
            messages.append({"role": "user", "content": content})
        elif "input" in adapted:
            content = adapted["input"]
            if isinstance(content, list):
                content = _extract_content_from_list(content)
            messages.append({"role": "user", "content": content})
        else:
            # 如果没有找到消息内容，创建一个默认的用户消息
            logger.warning(
                f"ClawdBot 请求中未找到消息内容，原始字段: {list(adapted.keys())}"
            )
            messages.append(
                {
                    "role": "user",
                    "content": "Hello",  # 默认消息
                }
            )

        adapted["messages"] = messages
    else:
        # 如果已有 messages，但格式可能不对，需要修复
        adapted["messages"] = _fix_messages_format(adapted["messages"])

    # 处理可能的工具字段
    if "tools" not in adapted:
        # ClawdBot 可能使用不同的字段名
        possible_tool_fields = [
            "functions",
            "tool_calls",
            "tools_list",
            "available_tools",
        ]
        for field in possible_tool_fields:
            if field in adapted:
                adapted["tools"] = adapted[field]
                debug.api(f"从字段 '{field}' 提取工具信息")
                break
        else:
            adapted["tools"] = None

    # 处理可能的参数字段
    for param in ["temperature", "top_p", "max_tokens", "stream"]:
        if param not in adapted:
            adapted[param] = None

    # 特殊处理：如果 stream 字段缺失，默认为 False
    if "stream" not in adapted:
        adapted["stream"] = False

    try:
        return ChatCompletionRequest(**adapted)
    except Exception as e:
        logger.error(f"自适应解析失败: {e}, 原始数据: {raw_body}")
        raise HTTPException(status_code=400, detail=f"无法解析请求格式: {str(e)}")


def _extract_content_from_list(content_list: list) -> str:
    """
    从内容列表中提取字符串内容

    ClawdBot 可能发送消息内容作为嵌套对象列表
    """
    if not isinstance(content_list, list):
        return str(content_list)

    extracted_parts = []
    for item in content_list:
        if isinstance(item, dict):
            # 如果是字典，尝试提取文本内容
            if "text" in item:
                text_content = item["text"]
                if isinstance(text_content, list):
                    # 递归处理嵌套列表
                    text_content = _extract_content_from_list(text_content)
                extracted_parts.append(str(text_content))
            elif "content" in item:
                content_value = item["content"]
                if isinstance(content_value, list):
                    # 递归处理嵌套列表
                    content_value = _extract_content_from_list(content_value)
                extracted_parts.append(str(content_value))
            elif "value" in item:
                value_content = item["value"]
                if isinstance(value_content, list):
                    # 递归处理嵌套列表
                    value_content = _extract_content_from_list(value_content)
                extracted_parts.append(str(value_content))
            else:
                # 如果没有找到文本字段，序列化整个对象
                extracted_parts.append(str(item))
        elif isinstance(item, list):
            # 如果是嵌套列表，递归处理
            nested_content = _extract_content_from_list(item)
            extracted_parts.append(nested_content)
        else:
            # 如果不是字典或列表，直接转换为字符串
            extracted_parts.append(str(item))

    return "\n".join(extracted_parts)


def _fix_messages_format(messages: list) -> list:
    """
    修复消息格式

    处理 ClawdBot 发送的消息格式问题，包括嵌套列表
    """
    fixed_messages = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue

        fixed_msg = msg.copy()

        # 修复 content 字段
        if "content" in msg:
            content = msg["content"]
            if isinstance(content, list):
                fixed_msg["content"] = _extract_content_from_list(content)
            elif not isinstance(content, str):
                fixed_msg["content"] = str(content)

        # 确保 role 字段存在
        if "role" not in fixed_msg:
            fixed_msg["role"] = "user"

        fixed_messages.append(fixed_msg)

    return fixed_messages


# ============================================================
# 配置相关 API
# ============================================================
# 图片处理 API
# ============================================================


class ImageProcessRequest(BaseModel):
    """图片处理请求"""

    image: str = Field(..., description="Base64 编码的图片数据 (data:image/...)")
    max_dimension: int = Field(default=768, description="最大边像素值")
    format: str = Field(default="webp", description="输出格式: webp, jpeg, png")


class ImageProcessResponse(BaseModel):
    """图片处理响应"""

    image: str = Field(..., description="处理后的 Base64 图片数据")
    width: int = Field(..., description="处理后的宽度")
    height: int = Field(..., description="处理后的高度")
    original_size: int = Field(..., description="原始数据大小 (bytes)")
    processed_size: int = Field(..., description="处理后数据大小 (bytes)")
    format: str = Field(..., description="输出格式")


@router.post("/process-image", response_model=ImageProcessResponse)
async def process_image(request: ImageProcessRequest):
    """
    处理图片：缩放、转换格式、压缩

    使用 Pillow 进行图片处理，支持：
    - 缩放到指定最大尺寸（保持宽高比）
    - 转换为 WebP/JPEG/PNG 格式
    - 质量压缩
    """
    from PIL import Image
    import base64
    import io

    debug.api(
        f"[process-image] 收到请求(旧Base64链路): max_dimension={request.max_dimension}, format={request.format}, "
        f"payload大小: {len(request.image)} chars"
    )

    import time as _time
    _t0 = _time.perf_counter()

    try:
        # 解析 base64 图片数据
        _t_b64dec_start = _time.perf_counter()
        if request.image.startswith("data:"):
            # 提取 data URL 中的 base64 部分
            header, b64_data = request.image.split(",", 1)
            image_bytes = base64.b64decode(b64_data)
        else:
            # 直接是 base64 字符串
            image_bytes = base64.b64decode(request.image)

        original_size = len(image_bytes)
        _t_b64dec = _time.perf_counter() - _t_b64dec_start

        debug.api(
            f"[process-image] Base64 解码: {len(request.image)} chars → {original_size} bytes, "
            f"耗时: {_t_b64dec*1000:.1f}ms"
        )

        # 使用 Pillow 处理图片
        _t_pil_start = _time.perf_counter()
        img = Image.open(io.BytesIO(image_bytes))

        # 获取原始尺寸
        original_width, original_height = img.size

        # 计算缩放后的尺寸（保持宽高比）
        max_dim = request.max_dimension
        if original_width > max_dim or original_height > max_dim:
            if original_width > original_height:
                new_width = max_dim
                new_height = int(original_height * max_dim / original_width)
            else:
                new_height = max_dim
                new_width = int(original_width * max_dim / original_height)

            # 使用高质量插值进行缩放
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        else:
            new_width = original_width
            new_height = original_height
        _t_pil = _time.perf_counter() - _t_pil_start

        debug.api(
            f"[process-image] PIL 解码+缩放: {original_width}x{original_height} → {new_width}x{new_height}, "
            f"耗时: {_t_pil*1000:.1f}ms"
        )

        # 转换格式并压缩
        _t_encode_start = _time.perf_counter()
        format_lower = request.format.lower()
        output_buffer = io.BytesIO()

        if format_lower == "webp":
            img.save(output_buffer, format="WEBP", quality=85, optimize=True)
            output_format = "webp"
        elif format_lower == "jpeg" or format_lower == "jpg":
            # 转换为 RGB 模式（JPEG 不支持 RGBA）
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(output_buffer, format="JPEG", quality=85, optimize=True)
            output_format = "jpeg"
        elif format_lower == "png":
            img.save(output_buffer, format="PNG", optimize=True)
            output_format = "png"
        else:
            # 默认使用 WebP
            img.save(output_buffer, format="WEBP", quality=85, optimize=True)
            output_format = "webp"

        processed_bytes = output_buffer.getvalue()
        processed_b64 = base64.b64encode(processed_bytes).decode("utf-8")
        _t_encode = _time.perf_counter() - _t_encode_start

        # 构建 data URL
        mime_type = f"image/{output_format}"
        processed_image = f"data:{mime_type};base64,{processed_b64}"

        _t_total = _time.perf_counter() - _t0

        debug.api(
            f"[process-image] ✅ 完成: {original_width}x{original_height} → {new_width}x{new_height} {output_format}, "
            f"原始 {original_size} → 处理后 {len(processed_bytes)} bytes, "
            f"总耗时: {_t_total*1000:.1f}ms "
            f"[b64解码={_t_b64dec*1000:.1f} + PIL={_t_pil*1000:.1f} + 转换+b64编码={_t_encode*1000:.1f}]"
        )

        return ImageProcessResponse(
            image=processed_image,
            width=new_width,
            height=new_height,
            original_size=original_size,
            processed_size=len(processed_bytes),
            format=output_format,
        )

    except Exception as e:
        _t_total = _time.perf_counter() - _t0
        logger.error(
            f"[process-image] ❌ 失败: 耗时 {_t_total*1000:.1f}ms, "
            f"错误: {type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(status_code=400, detail=f"图片处理失败: {str(e)}")


# ============================================================
# 二进制图片处理 API（跳过 Base64，直接接收原始字节）
# 优化目标：减少 TTFT（首 token 延迟）中的序列化/反序列化开销
#   旧链路：前端 Base64 编码 → HTTP 传输(+33%体积) → 后端 Base64 解码 → 处理 → Base64 编码 → 返回
#   新链路：前端直接发送原始字节 → HTTP 传输 → 后端 BytesIO → 处理 → 一次 Base64 编码 → 返回
# ============================================================

@router.post("/process-image-binary", response_model=ImageProcessResponse)
async def process_image_binary(
    file: UploadFile = File(..., description="原始图片文件（multipart/form-data）"),
    format: str = Form(default="auto", description="输出格式: auto, webp, jpeg, png (auto=智能选择)"),
):
    """
    处理图片（二进制上传版 v2）—— 针对 VLM TTFT 全链路优化。

    优化点：
    1. 跳过前端 Base64 编解码（直接接收原始字节）
    2. 智能缩放：根据 config.ini [model] max_image_dimension 缩放（默认 1280px）
       → 减少 Qwen3-VL visual token 数 → TTFT 从 100s 降到 ~25s
    3. 智能格式选择（auto 模式）：
       - JPEG 输入 → 直接透传原始字节，跳过 PIL 重编码（省 1.4s+）
       - PNG/其他 → 转 JPEG（最快编码速度）
    4. 全链路 debug timing 输出

    支持: multipart/form-data，字段名 "file"
    """
    from PIL import Image
    import base64
    import io as _io
    import time as _time

    TAG = "[process-image-binary]"
    _t0 = _time.perf_counter()

    # 从配置读取最大边长（0 或空 = 不缩放）
    max_dim = config.model.max_image_dimension or 0

    debug.api(
        f"{TAG} 收到请求: filename={file.filename}, "
        f"content_type={file.content_type}, format={format}, max_dim={max_dim}"
    )

    try:
        # ── 阶段 1: 读取原始字节 ──
        _t_read_start = _time.perf_counter()
        image_bytes = await file.read()
        original_size = len(image_bytes)
        _t_read = _time.perf_counter() - _t_read_start

        debug.api(
            f"{TAG} 读取原始字节: {original_size} bytes ({original_size/1024:.1f} KB), "
            f"耗时: {_t_read*1000:.1f}ms (无 Base64 解码)"
        )

        # ── 阶段 2: PIL 解码 + 获取原始尺寸 ──
        _t_decode_start = _time.perf_counter()
        img = Image.open(_io.BytesIO(image_bytes))
        original_width, original_height = img.size
        original_format = (img.format or "").lower()  # "jpeg", "png", "webp", ...
        _t_decode = _time.perf_counter() - _t_decode_start

        debug.api(
            f"{TAG} PIL 图片解码: {original_width}x{original_height}, "
            f"mode={img.mode}, format={original_format}, 耗时: {_t_decode*1000:.1f}ms "
            f"(lazy decode, 未 load 像素)"
        )

        # ── 阶段 3: 判断是否需要缩放 ──
        need_resize = (
            max_dim > 0
            and (original_width > max_dim or original_height > max_dim)
        )

        if need_resize:
            _t_resize_start = _time.perf_counter()
            img.load()  # 现在才解码像素
            # 保持宽高比缩放
            if original_width > original_height:
                new_width = max_dim
                new_height = int(original_height * max_dim / original_width)
            else:
                new_height = max_dim
                new_width = int(original_width * max_dim / original_height)
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            _t_resize = _time.perf_counter() - _t_resize_start
            resized = True

            debug.api(
                f"{TAG} 缩放: {original_width}x{original_height} → {new_width}x{new_height} "
                f"(max_dim={max_dim}), 耗时: {_t_resize*1000:.1f}ms, "
                f"像素数: {original_width*original_height} → {new_width*new_height} "
                f"(减少 {(1 - new_width*new_height/(original_width*original_height))*100:.0f}%)"
            )
        else:
            new_width, new_height = original_width, original_height
            _t_resize = 0.0
            resized = False
            debug.api(
                f"{TAG} 无需缩放: {original_width}x{original_height} "
                f"(max_dim={max_dim}, 已在范围内)"
            )

        # ── 阶段 4: 智能格式选择 + 编码 ──
        _t_encode_start = _time.perf_counter()
        format_lower = format.lower()

        # auto 模式：选择最快的方案
        if format_lower == "auto":
            if not resized:
                # 无需缩放 → 所有格式均直接透传原始字节, 零重编码!
                processed_bytes = image_bytes
                # 保留原始 MIME 类型
                _fmt_map = {"jpeg": "jpeg", "jpg": "jpeg", "webp": "webp",
                            "png": "png", "gif": "gif", "bmp": "jpeg"}
                output_format = _fmt_map.get(original_format, "jpeg")
                _t_encode = _time.perf_counter() - _t_encode_start
                debug.api(
                    f"{TAG} ⚡ 原格式透传 ({original_format}): 跳过 PIL 重编码, 直接使用原始字节 "
                    f"({original_size} bytes), 耗时: {_t_encode*1000:.1f}ms (几乎为零!)"
                )
            else:
                # 需要缩放 → 编码为 JPEG（最快编码速度，比 WebP 快 10-50 倍）
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                elif img.mode != "RGB":
                    img.load()
                    img = img.convert("RGB")
                output_buffer = _io.BytesIO()
                img.save(output_buffer, format="JPEG", quality=85, optimize=False)
                processed_bytes = output_buffer.getvalue()
                output_format = "jpeg"
                _t_encode = _time.perf_counter() - _t_encode_start
                debug.api(
                    f"{TAG} JPEG 编码 (auto, 缩放后): {len(processed_bytes)} bytes, "
                    f"耗时: {_t_encode*1000:.1f}ms"
                )
        elif format_lower in ("jpeg", "jpg"):
            if not resized and original_format in ("jpeg", "jpg"):
                processed_bytes = image_bytes
                output_format = "jpeg"
                _t_encode = _time.perf_counter() - _t_encode_start
                debug.api(
                    f"{TAG} ⚡ JPEG 透传: 直接使用原始字节, "
                    f"耗时: {_t_encode*1000:.1f}ms"
                )
            else:
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                output_buffer = _io.BytesIO()
                img.save(output_buffer, format="JPEG", quality=90, optimize=False)
                processed_bytes = output_buffer.getvalue()
                output_format = "jpeg"
                _t_encode = _time.perf_counter() - _t_encode_start
                debug.api(
                    f"{TAG} JPEG 编码: {len(processed_bytes)} bytes, "
                    f"耗时: {_t_encode*1000:.1f}ms"
                )
        elif format_lower == "webp":
            if not resized:
                img.load()
            output_buffer = _io.BytesIO()
            img.save(output_buffer, format="WEBP", quality=90, method=4)
            processed_bytes = output_buffer.getvalue()
            output_format = "webp"
            _t_encode = _time.perf_counter() - _t_encode_start
            debug.api(
                f"{TAG} WebP 编码: {len(processed_bytes)} bytes, "
                f"耗时: {_t_encode*1000:.1f}ms (注意: WebP 编码远慢于 JPEG)"
            )
        elif format_lower == "png":
            if not resized:
                img.load()
            output_buffer = _io.BytesIO()
            img.save(output_buffer, format="PNG", optimize=False)
            processed_bytes = output_buffer.getvalue()
            output_format = "png"
            _t_encode = _time.perf_counter() - _t_encode_start
            debug.api(
                f"{TAG} PNG 编码: {len(processed_bytes)} bytes, "
                f"耗时: {_t_encode*1000:.1f}ms"
            )
        else:
            # 未知格式 fallback 到 JPEG
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            output_buffer = _io.BytesIO()
            img.save(output_buffer, format="JPEG", quality=90, optimize=False)
            processed_bytes = output_buffer.getvalue()
            output_format = "jpeg"
            _t_encode = _time.perf_counter() - _t_encode_start
            debug.api(
                f"{TAG} JPEG fallback 编码: {len(processed_bytes)} bytes, "
                f"耗时: {_t_encode*1000:.1f}ms"
            )

        # ── 阶段 5: Base64 编码（唯一一次，为 SGLang data URL 服务）──
        _t_b64_start = _time.perf_counter()
        processed_b64 = base64.b64encode(processed_bytes).decode("utf-8")
        mime_type = f"image/{output_format}"
        processed_image = f"data:{mime_type};base64,{processed_b64}"
        _t_b64 = _time.perf_counter() - _t_b64_start

        _t_total = _time.perf_counter() - _t0

        debug.api(
            f"{TAG} Base64 编码(唯一一次): "
            f"{len(processed_b64)} chars ({len(processed_b64)/1024:.1f} KB), "
            f"耗时: {_t_b64*1000:.1f}ms"
        )

        # ── 总结 ──
        pixel_reduction = (
            f", 像素缩减 {(1 - new_width*new_height/(original_width*original_height))*100:.0f}%"
            if resized else ""
        )
        debug.api(
            f"{TAG} ✅ 完成: {original_width}x{original_height} → "
            f"{new_width}x{new_height} {output_format}, "
            f"原始 {original_size/1024:.1f}KB → 处理后 {len(processed_bytes)/1024:.1f}KB "
            f"(压缩比 {len(processed_bytes)/original_size*100:.1f}%{pixel_reduction}), "
            f"总耗时: {_t_total*1000:.1f}ms "
            f"[读取={_t_read*1000:.1f} + 解码={_t_decode*1000:.1f} + "
            f"缩放={_t_resize*1000:.1f} + 编码={_t_encode*1000:.1f} + b64={_t_b64*1000:.1f}]"
        )

        return ImageProcessResponse(
            image=processed_image,
            width=new_width,
            height=new_height,
            original_size=original_size,
            processed_size=len(processed_bytes),
            format=output_format,
        )

    except Exception as e:
        _t_total = _time.perf_counter() - _t0
        logger.error(
            f"{TAG} ❌ 失败: filename={file.filename}, "
            f"耗时: {_t_total*1000:.1f}ms, 错误: {type(e).__name__}: {e}",
            exc_info=True,
        )
        debug.api(
            f"{TAG} ❌ 失败: {type(e).__name__}: {e}, "
            f"耗时: {_t_total*1000:.1f}ms — "
            f"请检查图片文件是否损坏或格式不受支持"
        )
        raise HTTPException(status_code=400, detail=f"图片处理失败: {str(e)}")
