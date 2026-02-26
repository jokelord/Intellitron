"""
聊天 API 路由模块

实现 OpenAI 兼容的聊天 API 接口，支持：
- POST /v1/chat/completions: 对话补全（支持流式）
- GET /v1/models: 获取可用模型列表
"""

import json
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
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
    将OpenAI格式工具转换为SGLang兼容格式
    
    SGLang只支持 'web_search_preview' 和 'code_interpreter'
    """
    if not tools:
        return []
    
    debug.api(f"原始工具列表 ({len(tools)} 个): {[t.get('name', t.get('type', 'unknown')) for t in tools]}")
    converted = []
    
    # 检查是否有web_search相关的工具
    has_web_tools = any('web_search' in str(tool).lower() or 'web_fetch' in str(tool).lower() for tool in tools)
    
    for tool in tools:
        tool_type = tool.get('type')
        tool_name = tool.get('name', 'unknown')
        
        if tool_type == 'function':
            func_name = tool.get('function', {}).get('name', tool_name)
            if 'web_search' in func_name or 'web_fetch' in func_name:
                # 转换为SGLang支持的web_search_preview
                converted_tool = {
                    'type': 'web_search_preview',
                    'parameters': tool.get('function', {}).get('parameters', tool.get('parameters', {}))
                }
                converted.append(converted_tool)
                debug.api(f"转换工具: {func_name} -> web_search_preview")
            else:
                debug.api(f"跳过不支持的函数工具: {func_name}")
        elif tool_type in ['web_search_preview', 'code_interpreter']:
            # 保留SGLang原生支持的工具
            converted.append(tool)
            debug.api(f"保留原生工具: {tool_type}")
        else:
            # 尝试从工具名称或描述中检测web工具
            tool_str = str(tool).lower()
            if 'web' in tool_str and ('search' in tool_str or 'fetch' in tool_str):
                converted_tool = {
                    'type': 'web_search_preview',
                    'parameters': tool.get('parameters', {})
                }
                converted.append(converted_tool)
                debug.api(f"从描述检测到web工具，转换: {tool_name} -> web_search_preview")
            else:
                debug.api(f"跳过不支持的工具类型: {tool_type} ({tool_name})")
    
    if not converted and has_web_tools:
        debug.api("警告: 检测到web工具但转换失败")
    elif not converted:
        debug.api("警告: 没有工具能转换为SGLang格式，所有工具将被忽略")
    
    debug.api(f"转换结果: {len(tools)} -> {len(converted)}")
    return converted


# ============================================================
# 请求/响应模型（OpenAI 兼容格式）
# ============================================================

class ChatMessage(BaseModel):
    """聊天消息"""
    role: str = Field(..., description="消息角色: system/user/assistant")
    content: str = Field(..., description="消息内容")
    reasoning_content: Optional[str] = Field(default=None, description="推理过程内容")


class ChatCompletionRequest(BaseModel):
    """聊天补全请求（OpenAI 兼容）"""
    model: str = Field(default="qwen3-next", description="模型名称")
    messages: List[ChatMessage] = Field(..., description="对话消息列表")
    temperature: Optional[float] = Field(default=None, ge=0, le=2, description="采样温度")
    top_p: Optional[float] = Field(default=None, ge=0, le=1, description="Top-P 采样")
    max_tokens: Optional[int] = Field(default=None, ge=1, description="最大生成 token 数")
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
    context_length: Optional[int] = Field(default=None, description="模型支持的最大上下文长度")


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
    debug.api(f"收到聊天请求: model={request.model}, messages_count={len(request.messages)}")
    debug.api(f"参数: temperature={request.temperature}, top_p={request.top_p}, stream={request.stream}")
    
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
        )
        
        debug.api(f"生成完成: text_length={len(result.text)}, time={result.generation_time:.2f}s")
        
        return ChatCompletionResponse(
            model=request.model,
            choices=[
                ChatCompletionChoice(
                    message=ChatMessage(
                        role="assistant", 
                        content=result.text,
                        reasoning_content=result.reasoning_content
                    ),
                    finish_reason=result.finish_reason,
                )
            ],
            usage=ChatCompletionUsage(
                prompt_tokens=usage.get("prompt_tokens", 0) if (usage := result.usage) else 0,
                completion_tokens=usage.get("completion_tokens", 0) if (usage := result.usage) else 0,
                total_tokens=usage.get("total_tokens", 0) if (usage := result.usage) else 0,
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
        ):
            chunk = ChatCompletionChunk(
                id=response_id,
                created=created,
                model=model,
                choices=[
                    ChatCompletionChunkChoice(
                        delta=ChatCompletionChunkDelta(
                            content=chunk_data.get("content"),
                            reasoning_content=chunk_data.get("reasoning")
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
    
    # 根据后端类型确定上下文长度
    if config.model.backend == "sglang":
        context_length = config.sglang.max_context_length
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
    
    # 根据后端类型确定上下文长度
    if config.model.backend == "sglang":
        context_length = config.sglang.max_context_length
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
        debug.api(f"收到 /responses 原始请求: {json.dumps(raw_body, indent=2, ensure_ascii=False)}")
        
        # 尝试解析为标准的 ChatCompletionRequest
        try:
            chat_request = ChatCompletionRequest(**raw_body)
            debug.api(f"成功解析为标准格式: model={chat_request.model}, messages_count={len(chat_request.messages)}")
        except Exception as parse_error:
            logger.warning(f"请求格式不符合标准 ChatCompletionRequest: {parse_error}")
            debug.api(f"解析错误详情: {parse_error}")
            
            # 尝试手动构建请求对象
            chat_request = _adapt_clawdbot_request(raw_body)
            debug.api(f"自适应解析完成: model={chat_request.model}, messages_count={len(chat_request.messages)}")
        
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
        messages = [Message(role=msg.role, content=msg.content) for msg in chat_request.messages]
        
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
        
        debug.api(f"生成完成: tokens={result.usage.get('total_tokens', 'N/A') if result.usage else 'N/A'}")
        
        # 转换 usage 为 ChatCompletionUsage 对象
        usage = ChatCompletionUsage()
        if result.usage:
            usage = ChatCompletionUsage(
                prompt_tokens=result.usage.get('prompt_tokens', 0),
                completion_tokens=result.usage.get('completion_tokens', 0),
                total_tokens=result.usage.get('total_tokens', 0),
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
    if 'model' not in adapted:
        adapted['model'] = 'qwen3-next'  # 默认模型
    
    # 处理消息字段 - ClawdBot 可能使用不同的字段名或格式
    if 'messages' not in adapted:
        # 尝试从其他字段提取消息
        messages = []
        
        # 检查是否有 prompt 或 content 字段
        if 'prompt' in adapted:
            content = adapted['prompt']
            if isinstance(content, list):
                # 如果 content 是列表，转换为字符串
                content = _extract_content_from_list(content)
            messages.append({
                'role': 'user',
                'content': content
            })
        elif 'content' in adapted:
            content = adapted['content']
            if isinstance(content, list):
                content = _extract_content_from_list(content)
            messages.append({
                'role': 'user', 
                'content': content
            })
        elif 'input' in adapted:
            content = adapted['input']
            if isinstance(content, list):
                content = _extract_content_from_list(content)
            messages.append({
                'role': 'user',
                'content': content
            })
        else:
            # 如果没有找到消息内容，创建一个默认的用户消息
            logger.warning(f"ClawdBot 请求中未找到消息内容，原始字段: {list(adapted.keys())}")
            messages.append({
                'role': 'user',
                'content': 'Hello'  # 默认消息
            })
        
        adapted['messages'] = messages
    else:
        # 如果已有 messages，但格式可能不对，需要修复
        adapted['messages'] = _fix_messages_format(adapted['messages'])
    
    # 处理可能的工具字段
    if 'tools' not in adapted:
        # ClawdBot 可能使用不同的字段名
        possible_tool_fields = ['functions', 'tool_calls', 'tools_list', 'available_tools']
        for field in possible_tool_fields:
            if field in adapted:
                adapted['tools'] = adapted[field]
                debug.api(f"从字段 '{field}' 提取工具信息")
                break
        else:
            adapted['tools'] = None
    
    # 处理可能的参数字段
    for param in ['temperature', 'top_p', 'max_tokens', 'stream']:
        if param not in adapted:
            adapted[param] = None
    
    # 特殊处理：如果 stream 字段缺失，默认为 False
    if 'stream' not in adapted:
        adapted['stream'] = False
    
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
            if 'text' in item:
                text_content = item['text']
                if isinstance(text_content, list):
                    # 递归处理嵌套列表
                    text_content = _extract_content_from_list(text_content)
                extracted_parts.append(str(text_content))
            elif 'content' in item:
                content_value = item['content']
                if isinstance(content_value, list):
                    # 递归处理嵌套列表
                    content_value = _extract_content_from_list(content_value)
                extracted_parts.append(str(content_value))
            elif 'value' in item:
                value_content = item['value']
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
    
    return '\n'.join(extracted_parts)


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
        if 'content' in msg:
            content = msg['content']
            if isinstance(content, list):
                fixed_msg['content'] = _extract_content_from_list(content)
            elif not isinstance(content, str):
                fixed_msg['content'] = str(content)
        
        # 确保 role 字段存在
        if 'role' not in fixed_msg:
            fixed_msg['role'] = 'user'
            
        fixed_messages.append(fixed_msg)
    
    return fixed_messages


# ============================================================
# 配置相关 API
# ============================================================

@router.get("/config", response_model=dict)
async def get_config_endpoint():
    """
    获取后端配置信息
    
    返回推理相关的默认配置，用于前端初始化。
    """
    debug.api("获取配置信息")
    
    return {
        "inference": {
            "temperature": config.inference.temperature,
            "top_p": config.inference.top_p,
            "top_k": config.inference.top_k,
            "repetition_penalty": config.inference.repetition_penalty,
            "max_tokens": config.inference.max_tokens,
            "stream": config.inference.stream,
        },
        "model": {
            "model_name": config.model.model_name,
            "max_context_length": config.model.max_context_length,
        },
        "sglang": {
            "chat_template": config.sglang.chat_template,
        }
    }
