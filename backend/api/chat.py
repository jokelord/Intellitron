"""
聊天 API 路由模块

实现 OpenAI 兼容的聊天 API 接口，支持：
- POST /v1/chat/completions: 对话补全（支持流式）
- GET /v1/models: 获取可用模型列表
"""

import asyncio
import json
import time
import uuid
import re
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, StrictBool, model_validator

from ..config import get_config
from backend.agent.canonical_messages import canonicalize_model_messages
from ..llm_service import (
    LLMService,
    Message,
    GenerationResult,
    abort_request_by_id,
    create_request_id,
    get_llm_service,
    resolve_backend_context_length,
    prepare_multimodal_messages,
)
from ..compact.chat_message_filter import filter_messages_for_llm
from ..compact.custom_focus import build_next_turn_focus_instruction
from ..compact.prompt_plan import build_reliable_source_with_compact_system
from ..compact.types import CompactRequest, CompactEvent, CompactEventKind
from ..utils.logger import DebugLogger, get_logger
from .chat_session_store import ChatSessionRecord, ChatSessionStore, chat_session_registry
from .chat_local_memory import ChatMemoryTurn, ChatMemoryAborted
from ..request_lifecycle import (
    RequestLifecycleCapacityError,
    RequestLifecycleConflict,
    RequestLifecycleValidationError,
)
from ..vision.contracts import (
    VisionAutomationTransportMetadata,
    fingerprint_request_payload,
)
from ..vision.validation import (
    VisionRequestValidationError,
    redact_data_urls,
    validate_chat_vision_request,
)
from ..session_hub_runtime import (
    SessionHubRuntime,
    SessionProducerPublisher,
    subscribe_session_hub,
)

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
    tool_calls: Optional[List[dict]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    reasoning_content: Optional[str] = Field(default=None, description="推理过程内容")
    metadata: Optional[dict] = Field(
        default=None,
        description=(
            "Optional client-side metadata. Reserved keys for /compact: "
            "pre_compact=true (this message was summarized out), "
            "compact_summary=true + summary_id (this is a compact summary). "
            "Not sent to the LLM; consumed by filter_messages_for_llm()."
        ),
    )


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
    request_id: Optional[str] = Field(
        default=None, description="请求 ID（用于中止流式请求）"
    )
    enable_thinking: Optional[bool] = Field(
        default=None,
        description="是否为本次普通聊天请求开启模型 reasoning",
    )
    session_id: Optional[str] = Field(
        default=None,
        description="唯一可恢复聊天会话 ID",
    )
    vision_automation: Optional[VisionAutomationTransportMetadata] = None
    enable_local_memory: StrictBool = False
    chat_history_start_ref: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_chat_session_id(cls, value):
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        legacy = normalized.pop("chat_session_id", None)
        canonical = normalized.get("session_id")
        if (
            legacy is not None
            and canonical is not None
            and legacy != canonical
        ):
            raise ValueError("session_id conflicts with chat_session_id")
        if canonical is None and legacy is not None:
            normalized["session_id"] = legacy
        return normalized


class ChatStopRequest(BaseModel):
    """停止聊天请求"""

    request_id: str = Field(..., description="要中止的请求 ID")


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


class _ChatStreamTerminal(str):
    """Internal typed terminal that remains the legacy `[DONE]` wire string."""

    def __new__(
        cls,
        status: str,
        error: BaseException | None = None,
    ):
        instance = super().__new__(cls, "data: [DONE]\n\n")
        instance.status = status
        instance.error = error
        return instance


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


def _request_lifecycle_registry(raw_request: Request):
    return getattr(raw_request.app.state, "request_lifecycle_registry", None)


def _reserve_chat_lifecycle(raw_request: Request, request, request_id: str):
    registry = _request_lifecycle_registry(raw_request)
    if registry is None:
        return None, None, None
    try:
        reservation = registry.reserve(
            request_id=request_id,
            request_kind="chat",
            context_identity=request.session_id,
            payload_fingerprint=fingerprint_request_payload(
                request.model_dump(mode="json", exclude={"request_id"})
            ),
        )
    except RequestLifecycleValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail="invalid request lifecycle metadata",
        ) from exc
    except RequestLifecycleConflict as exc:
        raise HTTPException(status_code=409, detail="request ID conflict") from exc
    except RequestLifecycleCapacityError as exc:
        raise HTTPException(
            status_code=503,
            detail="request lifecycle capacity reached",
        ) from exc
    if not reservation.should_start:
        return registry, None, JSONResponse(
            {"request_id": request_id, "status": reservation.status}
        )
    return registry, reservation.registration_token, None


async def _stream_with_chat_lifecycle(source, registry, token, request_id: str):
    if registry is None or token is None:
        async for event in source:
            yield event
        return

    accepted = False
    outcome = "error"
    terminal_outcome = None
    queue: asyncio.Queue[tuple[str, object | None]] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for event in source:
                await queue.put(("event", event))
        except BaseException as exc:
            await queue.put(("error", exc))
        finally:
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()
            await queue.put(("done", None))

    worker = None
    try:
        worker = asyncio.create_task(pump())
        registry.accept(token)
        registry.mark_running(token)
        accepted = True
        yield "data: " + json.dumps(
            {
                "type": "request_accepted",
                "request_id": request_id,
                "status": "accepted",
            }
        ) + "\n\n"
        while True:
            kind, value = await queue.get()
            if kind == "event":
                if isinstance(value, _ChatStreamTerminal):
                    terminal_outcome = value.status
                    outcome = value.status
                yield value
                continue
            if kind == "error":
                if isinstance(value, BaseException):
                    raise value
                raise RuntimeError("chat stream worker failed")
            if terminal_outcome is None:
                outcome = "completed"
            break
    except asyncio.CancelledError:
        outcome = "aborted"
        raise
    finally:
        if worker is not None:
            if not worker.done():
                worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        if accepted:
            registry.mark_terminal(token, outcome)
        else:
            registry.abandon(token)


def _chat_session_store(raw_request: Request) -> ChatSessionStore:
    store = getattr(raw_request.app.state, "chat_session_store", None)
    if store is None:
        store = chat_session_registry()
        raw_request.app.state.chat_session_store = store
    return store


def _session_hub_runtime(raw_request: Request) -> SessionHubRuntime:
    runtime = getattr(raw_request.app.state, "session_hub_runtime", None)
    if not isinstance(runtime, SessionHubRuntime):
        raise HTTPException(status_code=503, detail="Session Hub is unavailable")
    return runtime


def _visible_chat_turn(
    chat_session: ChatSessionRecord,
    request: ChatCompletionRequest,
    *,
    turn_index: int,
) -> tuple[dict[str, Any], Any]:
    """Build projection-only turn metadata without changing model messages."""

    submitted = request.messages[-1] if request.messages else None
    content = getattr(submitted, "content", "") if submitted is not None else ""
    images: list[str] = []
    if isinstance(content, str):
        user_content = content
    elif isinstance(content, list):
        text_parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
            elif part.get("type") == "image_url":
                image_url = part.get("image_url")
                url = image_url.get("url") if isinstance(image_url, dict) else None
                if isinstance(url, str):
                    images.append(url)
        user_content = "\n".join(text_parts)
    else:
        user_content = ""

    metadata = request.vision_automation
    omission_committed = bool(
        metadata is not None
        and metadata.source_count > 0
        and metadata.retained_tick_count > 0
    )
    staged_assets = chat_session.stage_display_assets(
        turn_index=turn_index,
        images=images,
        evict_prior=omission_committed,
    )
    message_id = f"chat:{chat_session.session_id}:{turn_index}:assistant"
    event = {
        "kind": "turn_started",
        "session_id": chat_session.session_id,
        "turn_index": turn_index,
        "user_content": user_content,
        "images": [],
        "image_asset_ids": staged_assets.asset_ids,
        "mode": "chat",
        "message_id": message_id,
        "historical_image_omission": omission_committed,
        **({"history_start_ref": request.chat_history_start_ref} if request.chat_history_start_ref else
           ({"history_start_ref": f"chat:{chat_session.session_id}:{turn_index}:user"}
            if not _ordinary_chat_history(request) else {})),
    }
    if metadata is not None:
        event["vision_automation_manual_image_count"] = metadata.manual_image_count
    return {"type": "timeline_event", "event": event}, staged_assets


def _ordinary_chat_history(request):
    return [message for message in request.messages[:-1]
        if message.role in {"user", "assistant"}
        and not (message.metadata or {}).get("pre_compact")
        and not (message.metadata or {}).get("compact_summary")]


def _validate_chat_history_anchor(request, record):
    if request.chat_history_start_ref is None:
        return
    history = _ordinary_chat_history(request)
    first = history[0] if history else None
    metadata = (first.metadata or {}) if first is not None else {}
    bootstrap = re.fullmatch(r"chat:(.*):bootstrap:([^:\s]{1,256}):(user|assistant)", request.chat_history_start_ref)
    if bootstrap is not None:
        if record is not None and request.chat_history_start_ref in record.local_memory_bootstrap_snapshot():
            return
        if (bootstrap.group(1) != request.session_id or first is None
            or first.role != bootstrap.group(3)
            or metadata.get("chat_message_ref") != request.chat_history_start_ref):
            raise ValueError("invalid_chat_history_start_ref")
        return
    supplied = metadata.get("chat_turn_ref") if first is not None and first.role == "user" else metadata.get("chat_message_ref")
    match = re.fullmatch(r"chat:(.*):([1-9]\d*):(user|assistant)", request.chat_history_start_ref)
    if (match is None or match.group(1) != request.session_id or first is None
        or supplied != request.chat_history_start_ref or first.role != match.group(3)
        or record is None or int(match.group(2)) > record.turn_index):
        raise ValueError("invalid_chat_history_start_ref")


@dataclass(slots=True)
class _PreparedChatHubTurn:
    store: ChatSessionStore
    session: ChatSessionRecord
    publisher: SessionProducerPublisher
    turn_started: dict[str, Any]
    staged_assets: Any
    start_sequence: int


def _prepare_chat_hub_turn(
    raw_request: Request,
    request: ChatCompletionRequest,
    *,
    request_id: str,
    registry: Any,
    lifecycle_token: object,
    runtime: SessionHubRuntime,
) -> _PreparedChatHubTurn:
    store = _chat_session_store(raw_request)
    chat_session = None
    producer_token = None
    staged_assets = None
    turn_index = None
    try:
        chat_session = store.get_or_create(request.session_id, runtime=runtime)
        start_sequence = chat_session.hub.last_sequence
        producer_token = chat_session.hub.claim_producer()
        if producer_token is None:
            raise ValueError("chat session producer is already active")
        turn_index = chat_session.begin_turn(request_id=request_id)
        turn_started, staged_assets = _visible_chat_turn(
            chat_session,
            request,
            turn_index=turn_index,
        )
        if chat_session.entry is None:
            raise RuntimeError("chat session runtime entry is missing")
        publisher = SessionProducerPublisher(
            entry=chat_session.entry,
            request_id=request_id,
            producer_token=producer_token,
            request_lifecycle_registry=registry,
            reservation_token=lifecycle_token,
        )
        return _PreparedChatHubTurn(
            store=store,
            session=chat_session,
            publisher=publisher,
            turn_started=turn_started,
            staged_assets=staged_assets,
            start_sequence=start_sequence,
        )
    except (ValueError, RuntimeError) as exc:
        if chat_session is not None and turn_index is not None:
            chat_session.rollback_turn(
                turn_index=turn_index,
                request_id=request_id,
            )
        if chat_session is not None and producer_token is not None:
            chat_session.hub.release_producer(producer_token)
        registry.abandon(lifecycle_token)
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def produce_chat_session_events(
    chat_session: ChatSessionRecord,
    source: AsyncGenerator[str, None],
    publisher: SessionProducerPublisher,
    turn_started: dict[str, Any],
    staged_assets: Any,
    store: ChatSessionStore,
) -> None:
    """Translate one OpenAI stream into one shared-Hub producer generation."""

    outcome = "completed"
    failure: BaseException | None = None
    accepted = False
    message_id = turn_started["event"]["message_id"]
    captured_request_id = publisher.request_id
    captured_turn_index = turn_started["event"]["turn_index"]
    try:
        try:
            chat_session.accept_display_assets(
                staged_assets,
                lambda: publisher.accept(turn_started),
                lambda: publisher.acceptance_published,
            )
            accepted = True
        except BaseException:
            accepted = publisher.acceptance_published
            if not publisher.lifecycle_accepted:
                publisher.abandon_if_unaccepted()
            raise
        async for sse_line in source:
            if isinstance(sse_line, _ChatStreamTerminal):
                outcome = sse_line.status
                failure = sse_line.error
                break
            if not sse_line.startswith("data: "):
                continue
            body = sse_line[len("data: "):].strip()
            if body == "[DONE]":
                break
            try:
                chunk = json.loads(body)
            except (TypeError, ValueError):
                continue
            compact_kind = chunk.get("kind")
            if chunk.get("type") == "timeline_event" and chunk.get("event", {}).get("kind") == "local_memory_retrieved":
                publisher.publish(chunk)
                continue
            if compact_kind in {
                event_kind.value for event_kind in CompactEventKind
            }:
                publisher.publish(
                    {
                        "type": "timeline_event",
                        "event": chunk,
                    }
                )
                continue
            delta = ((chunk.get("choices") or [{}])[0]).get("delta") or {}
            for reasoning, key in (
                (False, "content"),
                (True, "reasoning_content"),
            ):
                text = delta.get(key)
                if not isinstance(text, str) or not text:
                    continue
                publisher.publish(
                    {
                        "type": "timeline_event",
                        "event": {
                            "kind": "message_delta",
                            "source": "chat",
                            "message_id": message_id,
                            "text": text,
                            "reasoning": reasoning,
                            "display": "chat_bubble",
                            "partial": True,
                        },
                    }
                )
    except asyncio.CancelledError:
        outcome = "aborted"
        raise
    except BaseException as exc:
        outcome = "error"
        safe_error = redact_data_urls(exc)
        failure = RuntimeError(str(safe_error))
        logger.error(
            "chat session %s inference failed: %s",
            chat_session.session_id,
            safe_error,
        )
    finally:
        close = getattr(source, "aclose", None)
        if close is not None:
            try:
                await close()
            except BaseException as close_error:
                if outcome != "aborted":
                    outcome = "error"
                    failure = RuntimeError(redact_data_urls(close_error))
        if not accepted:
            chat_session.rollback_turn(
                turn_index=turn_started["event"]["turn_index"],
                request_id=publisher.request_id,
            )
        try:
            if accepted:
                publisher.finish(outcome, error=failure)
        finally:
            store.mark_finished_if_same(
                chat_session,
                outcome,
                request_id=captured_request_id,
                turn_index=captured_turn_index,
            )


async def produce_chat_nonstream_session(
    prepared_turn: _PreparedChatHubTurn,
    service: LLMService,
    *,
    messages: List[Message],
    max_tokens: Optional[int],
    temperature: Optional[float],
    top_p: Optional[float],
    tools: Optional[List[dict]],
    enable_thinking: Optional[bool],
    memory_turn: ChatMemoryTurn | None = None,
):
    """Run one non-stream model call under the same detached Hub lifecycle."""

    publisher = prepared_turn.publisher
    chat_session = prepared_turn.session
    turn_index = prepared_turn.turn_started["event"]["turn_index"]
    accepted = False
    outcome = "error"
    failure: BaseException | None = None
    result = None
    try:
        try:
            chat_session.accept_display_assets(
                prepared_turn.staged_assets,
                lambda: publisher.accept(prepared_turn.turn_started),
                lambda: publisher.acceptance_published,
            )
            accepted = True
        except BaseException:
            accepted = publisher.acceptance_published
            if not publisher.lifecycle_accepted:
                publisher.abandon_if_unaccepted()
            raise
        if memory_turn is not None:
            memory_turn.admit_bootstrap()
            try:
                messages = await memory_turn.prepare(service, messages, tools, max_tokens, enable_thinking)
            except ChatMemoryAborted:
                outcome = "aborted"
                return GenerationResult(text="")
            event = memory_turn.event()
            if event is not None:
                publisher.publish({"type": "timeline_event", "event": event})
        result = await service.chat_async(
            messages=messages,
            max_new_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            tools=tools,
            extra_body=_build_reasoning_extra_body(enable_thinking),
            **({"request_id": publisher.request_id, "preserve_prompt_shape": True} if memory_turn is not None else {}),
        )
        if getattr(result, "transport_error", None):
            raise RuntimeError(result.transport_error)
        if memory_turn is not None:
            memory_turn.commit()
        message_id = prepared_turn.turn_started["event"]["message_id"]
        for reasoning, text in (
            (True, getattr(result, "reasoning_content", None)),
            (False, getattr(result, "text", None)),
        ):
            if isinstance(text, str) and text:
                publisher.publish(
                    {
                        "type": "timeline_event",
                        "event": {
                            "kind": "message_delta",
                            "source": "chat",
                            "message_id": message_id,
                            "text": text,
                            "reasoning": reasoning,
                            "display": "chat_bubble",
                            "partial": True,
                        },
                    }
                )
        outcome = "completed"
        return result
    except asyncio.CancelledError:
        outcome = "aborted"
        raise
    except BaseException as exc:
        outcome = "error"
        failure = RuntimeError(redact_data_urls(exc))
        raise
    finally:
        if not accepted:
            chat_session.rollback_turn(
                turn_index=turn_index,
                request_id=publisher.request_id,
            )
        try:
            if accepted:
                publisher.finish(outcome, error=failure)
        finally:
            prepared_turn.store.mark_finished_if_same(
                chat_session,
                outcome,
                request_id=publisher.request_id,
                turn_index=turn_index,
            )


@router.get("/chat/session/{chat_session_id}/events")
async def chat_session_events_endpoint(
    chat_session_id: str,
    raw_request: Request,
    after_sequence: Any = Query(default="0"),
):
    """重放并跟随一次普通聊天推理的 journal（页面关闭后的恢复入口）。"""
    try:
        cursor = int(after_sequence)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="after_sequence must be an integer")
    if cursor < 0:
        raise HTTPException(status_code=422, detail="after_sequence must be >= 0")
    runtime = _session_hub_runtime(raw_request)
    entry = runtime.resolve(chat_session_id)
    if entry is None or entry.kind != "chat" or entry.hub.released:
        raise HTTPException(
            status_code=404,
            detail="Chat session is unavailable or expired",
        )
    return StreamingResponse(
        subscribe_session_hub(entry, cursor),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/completions", response_model=ChatCompletionResponse)
async def create_chat_completion(request: ChatCompletionRequest, raw_request: Request):
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

    if request.vision_automation is not None:
        binding = request.vision_automation.binding
        if request.session_id is None:
            raise HTTPException(
                status_code=422,
                detail="Chat vision automation requires session_id",
            )
        if binding.mode != "chat" or binding.top_level_context_id != request.session_id:
            raise HTTPException(
                status_code=422,
                detail="Chat vision automation top-level context mismatch",
            )

    # 转换工具格式（兼容SGLang）
    converted_tools = None
    if request.tools:
        converted_tools = convert_tools_for_sglang(request.tools)
        debug.api(f"转换工具: {len(request.tools)} -> {len(converted_tools)}")

    # 转换消息格式
    messages = canonicalize_model_messages(request.messages)
    canonical_messages = request.messages
    history_updates, retiring = {}, frozenset()
    bootstrap_updates, initial_bootstrap_updates, retiring_bootstrap = {}, {}, frozenset()
    existing_chat = (_chat_session_store(raw_request).get(request.session_id)
        if request.session_id is not None else None)
    final_messages = _prepare_chat_messages_for_llm(messages, canonical_messages)
    try:
        validate_chat_vision_request(
            final_messages,
            get_config(),
            metadata=request.vision_automation,
        )
    except VisionRequestValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    session_runtime = (
        _session_hub_runtime(raw_request)
        if request.session_id is not None
        else None
    )

    # 获取 LLM 服务 after admission, before lifecycle reservation.
    service = get_llm_service()
    if not service.is_ready:
        debug.api("LLM 服务未就绪，尝试初始化...")
        if not service.initialize():
            raise HTTPException(status_code=503, detail="模型服务不可用")

    req_id = request.request_id or create_request_id()
    registry, lifecycle_token, duplicate_response = _reserve_chat_lifecycle(
        raw_request, request, req_id
    )
    if duplicate_response is not None:
        return duplicate_response
    try:
        _validate_chat_history_anchor(request, existing_chat)
    except ValueError as exc:
        if registry is not None and lifecycle_token is not None:
            registry.abandon(lifecycle_token)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if existing_chat is not None:
        try:
            canonical_messages, bootstrap_updates = existing_chat.resolve_chat_bootstrap(
                request.messages, request.chat_history_start_ref,
                allow_image_omission=request.vision_automation is not None)
            retiring_bootstrap = existing_chat.bootstrap_retirements(canonical_messages)
            canonical_messages, history_updates, retiring = existing_chat.resolve_local_memory_history(
                canonical_messages, allow_image_omission=request.vision_automation is not None)
            final_messages = _prepare_chat_messages_for_llm(messages, canonical_messages)
        except ValueError as exc:
            if registry is not None and lifecycle_token is not None:
                registry.abandon(lifecycle_token)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    if request.session_id is not None and (
        registry is None or lifecycle_token is None
    ):
        raise HTTPException(
            status_code=503,
            detail="recoverable Chat requires strict request lifecycle admission",
        )

    if (request.chat_history_start_ref and ":bootstrap:" in request.chat_history_start_ref
            and not (existing_chat and request.chat_history_start_ref in existing_chat.local_memory_bootstrap_snapshot())):
        initial_bootstrap_updates = {request.chat_history_start_ref: deepcopy(request.messages[:-1])}
    memory_turn = ChatMemoryTurn(service=getattr(raw_request.app.state, "local_memory_service", None),
        enabled=request.enable_local_memory, request_id=req_id,
        raw_content=deepcopy(request.messages[-1].content) if request.messages else "",
        record=existing_chat, updates=history_updates, retiring=retiring,
        history_start_ref=request.chat_history_start_ref, bootstrap_updates=bootstrap_updates,
        initial_bootstrap_updates=initial_bootstrap_updates, retiring_bootstrap=retiring_bootstrap,
        can_retrieve=not _ordinary_chat_history(request) or request.chat_history_start_ref is not None)

    # 处理流式请求
    if request.stream:
        debug.api("使用流式输出模式")
        source = _generate_stream_response(
            service=service,
            messages=messages,
            model=request.model,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            tools=converted_tools,
            request_id=req_id,
            raw_request=raw_request,
            original_chat_messages=canonical_messages,
            enable_thinking=request.enable_thinking,
            memory_turn=memory_turn,
        )
        if request.session_id:
            # A POST transport is only a subscriber. The producer and model
            # source belong to the shared session generation.
            runtime = session_runtime
            if runtime is None:
                raise HTTPException(
                    status_code=503,
                    detail="Session Hub is unavailable",
                )
            try:
                prepared_turn = _prepare_chat_hub_turn(
                    raw_request,
                    request,
                    request_id=req_id,
                    registry=registry,
                    lifecycle_token=lifecycle_token,
                    runtime=runtime,
                )
            except HTTPException:
                close = getattr(source, "aclose", None)
                if close is not None:
                    await close()
                raise
            memory_turn.record = prepared_turn.session
            memory_turn.turn_index = prepared_turn.turn_started["event"]["turn_index"]
            memory_turn.history_start_ref = prepared_turn.turn_started["event"].get("history_start_ref")
            produce_task = asyncio.create_task(
                produce_chat_session_events(
                    prepared_turn.session,
                    source,
                    prepared_turn.publisher,
                    prepared_turn.turn_started,
                    prepared_turn.staged_assets,
                    prepared_turn.store,
                )
            )
            if not prepared_turn.session.attach_producer_task(
                request_id=req_id,
                turn_index=prepared_turn.turn_started["event"]["turn_index"],
                task=produce_task,
            ):
                produce_task.cancel()
                raise HTTPException(
                    status_code=409,
                    detail="chat session producer reservation is no longer current",
                )
            return StreamingResponse(
                subscribe_session_hub(
                    prepared_turn.session.entry,
                    prepared_turn.start_sequence,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        return StreamingResponse(
            _stream_with_chat_lifecycle(
                source, registry, lifecycle_token, req_id
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    if request.session_id:
        runtime = session_runtime
        if runtime is None:
            raise HTTPException(status_code=503, detail="Session Hub is unavailable")
        prepared_turn = _prepare_chat_hub_turn(
            raw_request,
            request,
            request_id=req_id,
            registry=registry,
            lifecycle_token=lifecycle_token,
            runtime=runtime,
        )
        memory_turn.record = prepared_turn.session
        memory_turn.turn_index = prepared_turn.turn_started["event"]["turn_index"]
        memory_turn.history_start_ref = prepared_turn.turn_started["event"].get("history_start_ref")
        final_messages = prepare_multimodal_messages(final_messages,
            max_image_dimension=getattr(getattr(service, "config", None), "model", get_config().model).max_image_dimension)
        produce_task = asyncio.create_task(
            produce_chat_nonstream_session(
                prepared_turn,
                service,
                messages=final_messages,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                top_p=request.top_p,
                tools=converted_tools,
                enable_thinking=request.enable_thinking,
                memory_turn=memory_turn,
            )
        )
        if not prepared_turn.session.attach_producer_task(
            request_id=req_id,
            turn_index=prepared_turn.turn_started["event"]["turn_index"],
            task=produce_task,
        ):
            produce_task.cancel()
            raise HTTPException(
                status_code=409,
                detail="chat session producer reservation is no longer current",
            )
        try:
            result = await asyncio.shield(produce_task)
        except asyncio.CancelledError:
            # Subscriber/handler cancellation must not own the producer.
            raise
        except Exception as exc:
            logger.error("聊天生成失败: %s", redact_data_urls(exc))
            raise HTTPException(status_code=500, detail="生成失败") from exc

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
                prompt_tokens=result.usage.get("prompt_tokens", 0)
                if result.usage
                else 0,
                completion_tokens=result.usage.get("completion_tokens", 0)
                if result.usage
                else 0,
                total_tokens=result.usage.get("total_tokens", 0)
                if result.usage
                else 0,
            ),
        )

    # Non-streaming inference runs in an explicitly registered asyncio task.
    accepted = False
    outcome = "error"
    try:
        async def _ordinary_sync():
            prepared = await memory_turn.prepare(service, final_messages, converted_tools, request.max_tokens, request.enable_thinking)
            return await service.chat_async(
                messages=prepared,
                max_new_tokens=request.max_tokens,
                temperature=request.temperature,
                top_p=request.top_p,
                tools=converted_tools,
                extra_body=_build_reasoning_extra_body(request.enable_thinking),
                request_id=req_id,
            )
        worker = asyncio.create_task(_ordinary_sync())
        if registry is not None and lifecycle_token is not None:
            registry.accept(lifecycle_token)
            registry.mark_running(lifecycle_token)
            accepted = True
        result = await worker
        outcome = "completed"

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
    except ChatMemoryAborted:
        outcome = "aborted"
        return ChatCompletionResponse(model=request.model, choices=[], usage=ChatCompletionUsage())
    except Exception as e:
        logger.error("聊天生成失败: %s", redact_data_urls(e))
        debug.api(f"生成异常: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="生成失败") from e
    finally:
        if registry is not None and lifecycle_token is not None:
            if accepted:
                registry.mark_terminal(lifecycle_token, outcome)
            else:
                registry.abandon(lifecycle_token)


@router.post("/chat/stop")
async def stop_chat(request: ChatStopRequest):
    """
    停止正在进行的聊天请求

    通过 request_id 标记流式请求为中止状态，并关闭当前 provider 的上游连接。
    SGLang 额外调用 /abort_request；vLLM 会因 HTTP 连接关闭而中止引擎请求。
    """
    from ..llm_service import abort_request_by_id
    from ..config import get_config

    config = get_config()
    sglang_url = f"http://{config.sglang.host}:{config.sglang.port}"

    debug.api(f"[STOP] 收到停止请求: request_id={request.request_id}")

    # 定向中止当前 request，避免误伤其他正在运行的会话/推理。
    aborted = abort_request_by_id(
        request.request_id,
        sglang_url,
        fallback_to_abort_all=False,
        require_provider_abort=True,
    )
    if not aborted:
        raise HTTPException(status_code=502, detail="Targeted provider abort failed; retry Stop")

    return {"status": "stopped", "request_id": request.request_id}


def _event_to_sse_dict(ev: CompactEvent) -> dict:
    """Same flattening as backend.api.compact._event_to_dict."""
    out: dict = {
        "kind": ev.kind.value if isinstance(ev.kind, CompactEventKind) else str(ev.kind),
        "summary_message_id": ev.summary_message_id,
        "text": ev.text,
        "partial": ev.partial,
        "metadata": ev.metadata,
    }
    meta = ev.metadata or {}
    for key in ("trigger", "mode", "original_tokens", "threshold", "strategy"):
        if key in meta and meta[key] is not None:
            out[key] = meta[key]
    return out


def _should_auto_compact_chat(messages: List[Message]) -> bool:
    """Decide whether the chat-mode LLM input exceeds the compact threshold.

    Mirrors `AgentRunner._should_auto_compact_session` so chat and agent
    share the same threshold/token-counting code path (same TokenCounter,
    same `compute_threshold` + `should_compact`). The message source is
    the canonical post-`filter_messages_for_llm` list (i.e. what SGLang
    will actually receive), so the token count here matches the LLM
    input the next request will send — same accuracy as agent mode.

    The function is a pure decision. Caller still has to check
    `raw_request.app.state.compact_orchestrator` and run the orchestrator
    if it returns True. A defensive try/except mirrors agent mode: any
    unexpected failure falls through to False (no auto-compact) so the
    main chat flow is never blocked by the threshold check.
    """
    cfg = get_config()
    if not cfg.compact.enabled or not cfg.compact.auto_trigger:
        return False
    prompts_cfg = cfg.agent_prompts.compaction
    if prompts_cfg is None:
        return False
    try:
        from ..compact.threshold import compute_threshold, should_compact
        from ..compact.tokenizer import TokenCounter
        from ..llm_service import get_llm_service, resolve_backend_context_length

        service = get_llm_service()
        backend = service.active_backend or "sglang"
        max_ctx = resolve_backend_context_length(cfg, backend) or 0
        if max_ctx <= 0:
            return False
        threshold = compute_threshold(cfg.compact, max_ctx)
        tokens = TokenCounter.get().count_messages(messages)
        return should_compact(tokens, threshold)
    except Exception as exc:
        logger.debug(f"chat auto-compact check skipped: {exc}")
        return False


def _build_reasoning_extra_body(enable_thinking: Optional[bool]) -> Optional[dict]:
    if enable_thinking is None:
        return None
    return {"chat_template_kwargs": {"enable_thinking": bool(enable_thinking)}}


def _prepare_chat_messages_for_llm(
    messages: List[Message],
    original_chat_messages: Optional[list] = None,
) -> List[Message]:
    """Apply compact-boundary filtering and stable prompt-control injection.

    This is the single pre-LLM view for normal chat-style endpoints. It keeps
    non-streaming, streaming, and /responses prefixes aligned for cache-first.
    """
    prompts_cfg = get_config().agent_prompts.compaction
    if prompts_cfg is None:
        return messages

    if original_chat_messages is not None:
        try:
            pre_compact_count = sum(
                1 for m in original_chat_messages
                if isinstance(getattr(m, "metadata", None), dict) and m.metadata.get("pre_compact")
            )
            compact_summary_count = sum(
                1 for m in original_chat_messages
                if isinstance(getattr(m, "metadata", None), dict) and m.metadata.get("compact_summary")
            )
            logger.info(
                f"[chat] pre-filter: {len(original_chat_messages)} msgs "
                f"(pre_compact={pre_compact_count}, compact_summary={compact_summary_count})"
            )
            filtered_namespaces = filter_messages_for_llm(
                [
                    SimpleNamespace(
                        role=m.role,
                        content=m.content,
                        metadata=getattr(m, "metadata", None),
                        tool_calls=getattr(m, "tool_calls", None),
                        tool_call_id=getattr(m, "tool_call_id", None),
                        name=getattr(m, "name", None),
                        reasoning_content=getattr(m, "reasoning_content", None),
                    )
                    for m in original_chat_messages
                ],
                prompts_cfg,
            )
            filtered = canonicalize_model_messages(filtered_namespaces)
            logger.info(
                f"[chat] post-filter: {len(filtered)} msgs "
                f"(was {len(original_chat_messages)})"
            )
        except Exception as exc:
            logger.warning(
                "compact filter failed, falling back to unfiltered: %s",
                redact_data_urls(exc),
            )
            filtered = canonicalize_model_messages(original_chat_messages)
    else:
        filtered = list(messages)

    return filtered


async def _generate_stream_response(
    service: LLMService,
    messages: List[Message],
    model: str,
    max_tokens: Optional[int],
    temperature: Optional[float],
    top_p: Optional[float],
    tools: Optional[List[dict]] = None,
    request_id: Optional[str] = None,
    raw_request: Optional[Request] = None,
    original_chat_messages: Optional[list] = None,
    enable_thinking: Optional[bool] = None,
    memory_turn: ChatMemoryTurn | None = None,
) -> AsyncGenerator[str, None]:
    """
    生成流式响应

    格式化为 SSE (Server-Sent Events) 格式，兼容 OpenAI 流式 API。

    Args:
        request_id: Optional request ID for abort tracking
        raw_request: Optional FastAPI Request (used for compact orchestrator access)
        original_chat_messages: The original ChatMessage list with metadata markers
                                (used for compact re-derivation when triggering)
    """
    response_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
    created = int(time.time())
    if memory_turn is not None:
        memory_turn.admit_bootstrap()

    # Apply the /compact filter as the DEFAULT view of the messages that
    # reach the LLM. This is defense-in-depth for the manual-/compact
    # path: the user has already run /compact, the frontend has marked
    # pre_compact + compact_summary in the wire metadata, and we MUST
    # drop the marked messages and replace the summary with the
    # envelope before sending to SGLang. The auto-compact branch below
    # is the only path that overrides this default (it builds a
    # brand-new [envelope, last] pair from the just-generated summary).
    filtered = _prepare_chat_messages_for_llm(messages, original_chat_messages)
    service_config = getattr(service, "config", None)
    max_image_dimension = getattr(
        getattr(service_config, "model", None), "max_image_dimension", None
    )
    if not isinstance(max_image_dimension, int):
        max_image_dimension = get_config().model.max_image_dimension
    prepared_messages = prepare_multimodal_messages(
        filtered,
        max_image_dimension=max_image_dimension,
        debug=debug,
    )
    final_messages = prepared_messages
    if raw_request is not None and original_chat_messages is not None:
        prompts_cfg = get_config().agent_prompts.compaction
        if prompts_cfg is not None:
            try:
                # Threshold check goes through the same code path as agent mode
                # (`_should_auto_compact_chat` mirrors `_should_auto_compact_session`):
                # same config, same `compute_threshold`, same `TokenCounter`. The
                # decision is independent of the orchestrator; we still check
                # `app.state.compact_orchestrator` here because running the
                # compact needs it, but the count is consistent either way.
                current_user_message: Optional[Message] = None
                compact_source_messages = prepared_messages
                if prepared_messages and getattr(prepared_messages[-1], "role", None) == "user":
                    current_user_message = prepared_messages[-1]
                    compact_source_messages = prepared_messages[:-1]

                if compact_source_messages and _should_auto_compact_chat(compact_source_messages):
                    orchestrator = getattr(raw_request.app.state, "compact_orchestrator", None)
                    if orchestrator is not None:
                        compact_cfg = get_config().compact
                        reliable_body_messages = build_reliable_source_with_compact_system(
                            compact_source_messages,
                            prompts_cfg,
                        )
                        cache_first_body_messages = (
                            compact_source_messages
                            if compact_cfg.prompt_control_enabled
                            and compact_source_messages
                            and getattr(compact_source_messages[0], "role", None) == "system"
                            else None
                        )
                        compact_req = CompactRequest(
                            mode="chat",
                            # v2: 含 system 的历史 messages 列表。当前 user turn
                            # 在 compact 完成后才绑定到新的 summary context。
                            body_messages=reliable_body_messages,
                            cache_first_body_messages=cache_first_body_messages,
                            tools=tools,
                            reference_messages=cache_first_body_messages,
                            trigger="auto",
                            custom_instructions=build_next_turn_focus_instruction(
                                current_user_message.content
                                if current_user_message is not None
                                else None
                            ),
                        )
                        cleaned_text: Optional[str] = None
                        completed_summary_id = None
                        memory_bootstrap_snapshot = (memory_turn.record.local_memory_bootstrap_snapshot()
                            if memory_turn is not None and memory_turn.record is not None else {})
                        memory_compact_snapshot = (memory_turn.record.local_memory_source_snapshot()
                            if memory_turn is not None and memory_turn.record is not None else {})
                        async for ev in orchestrator.run_compact(compact_req):
                            yield f"data: {json.dumps(_event_to_sse_dict(ev), ensure_ascii=False)}\n\n"
                            if ev.kind == CompactEventKind.COMPLETED:
                                cleaned_text = ev.text
                                completed_summary_id = ev.summary_message_id
                            if ev.kind == CompactEventKind.FAILED:
                                cleaned_text = None
                                break
                        if cleaned_text:
                            if memory_turn is not None:
                                memory_turn.compacted(completed_summary_id, cleaned_text, memory_compact_snapshot, memory_bootstrap_snapshot)
                            envelope_content = (
                                prompts_cfg.summary_envelope_preface
                                + cleaned_text
                                + prompts_cfg.summary_envelope_suffix
                            )
                            new_messages: List[Message] = [Message(role="user", content=envelope_content)]
                            if current_user_message is not None:
                                new_messages.append(
                                    Message(
                                        role=current_user_message.role,
                                        content=current_user_message.content,
                                    )
                                )
                            elif prepared_messages:
                                last = prepared_messages[-1]
                                new_messages.append(Message(role=last.role, content=last.content))
                            final_messages = new_messages
            except Exception as exc:
                logger.warning(
                    "auto-compact skipped due to error: %s",
                    redact_data_urls(exc),
                )

    if memory_turn is not None:
        try:
            final_messages = await memory_turn.prepare(service, final_messages, tools, max_tokens, enable_thinking)
        except ChatMemoryAborted:
            yield _ChatStreamTerminal("aborted")
            return
        memory_event = memory_turn.event()
        if memory_event is not None:
            yield f"data: {json.dumps({'type': 'timeline_event', 'event': memory_event}, ensure_ascii=False)}\n\n"

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
        # Diagnostic: dump the EXACT messages being sent to SGLang so
        # the user can copy-paste this into a bug report when the chat
        # template raises "No user query found in messages." or
        # "System message must be at the beginning." or similar.
        try:
            for i, m in enumerate(final_messages):
                role = getattr(m, "role", "?")
                content = getattr(m, "content", None)
                if isinstance(content, str):
                    safe_content = redact_data_urls(content)
                    head = safe_content[:200].replace("\n", "\\n")
                    tail = (
                        safe_content[-200:].replace("\n", "\\n")
                        if len(safe_content) > 200
                        else ""
                    )
                    logger.info(
                        f"[chat] LLM call msg[{i}]: role={role} content_len={len(content)} "
                        f"head={head!r} tail={tail!r}"
                    )
                elif isinstance(content, list):
                    image_count = sum(
                        1
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "image_url"
                    )
                    text_chars = sum(
                        len(str(item.get("text", "")))
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "text"
                    )
                    logger.info(
                        f"[chat] LLM call msg[{i}]: role={role} content_type=list "
                        f"parts={len(content)} images={image_count} text_chars={text_chars}"
                    )
                else:
                    logger.info(
                        f"[chat] LLM call msg[{i}]: role={role} "
                        f"content_type={type(content).__name__}"
                    )
            logger.info(
                f"[chat] LLM call: {len(final_messages)} msgs (tools={'set' if tools else 'none'})"
            )
        except Exception as diag_exc:
            logger.warning(
                "[chat] diagnostic logging failed: %s",
                redact_data_urls(diag_exc),
            )
        async for chunk_data in service.chat_stream_async(
            messages=final_messages,
            max_new_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            tools=tools,
            request_id=request_id,
            extra_body=_build_reasoning_extra_body(enable_thinking),
            preserve_prompt_shape=True,
        ):
            # Check for abort signal from LLM service
            if chunk_data.get("aborted"):
                debug.api(f"请求 {request_id} 已被中止")
                # A str subclass preserves exact legacy SSE while giving the
                # Hub adapter an out-of-band terminal classification.
                yield _ChatStreamTerminal("aborted")
                return
            if chunk_data.get("transport_error"):
                raise RuntimeError(chunk_data["transport_error"])
            if memory_turn is not None:
                memory_turn.commit()

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
        safe_error = redact_data_urls(e)
        logger.error(f"流式生成异常: {safe_error}")
        debug.api(f"流式生成异常: {type(e).__name__}: {safe_error}")
        terminal = _ChatStreamTerminal(
            "error",
            RuntimeError(safe_error),
        )
    else:
        if memory_turn is not None:
            memory_turn.commit()
        terminal = "data: [DONE]\n\n"

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
    yield terminal


@router.get("/models", response_model=ModelsResponse)
async def list_models():
    """
    获取可用模型列表

    返回当前配置的模型信息。
    """
    debug.api("获取模型列表")

    # 根据活跃的推理后端确定上下文长度
    service = get_llm_service()
    context_length = resolve_backend_context_length(config, service.active_backend)

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
    context_length = resolve_backend_context_length(config, service.active_backend)

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
        debug.api(f"收到 /responses 请求: fields={sorted(raw_body.keys())}")

        normalized_body = raw_body.copy()
        if "messages" in normalized_body:
            normalized_body["messages"] = _fix_messages_format(
                normalized_body["messages"],
                preserve_parts=True,
            )

        # 尝试解析为标准的 ChatCompletionRequest
        try:
            chat_request = ChatCompletionRequest(**normalized_body)
            debug.api(
                f"成功解析为标准格式: model={chat_request.model}, messages_count={len(chat_request.messages)}"
            )
        except Exception as parse_error:
            logger.warning(
                "请求格式不符合标准 ChatCompletionRequest: %s",
                type(parse_error).__name__,
            )
            debug.api(f"解析错误详情: {type(parse_error).__name__}")

            # 尝试手动构建请求对象
            chat_request = _adapt_clawdbot_request(normalized_body)
            debug.api(
                f"自适应解析完成: model={chat_request.model}, messages_count={len(chat_request.messages)}"
            )

        # Use the exact Chat path so /responses cannot bypass image admission,
        # idempotency, acceptance ordering, or terminal cleanup.
        return await create_chat_completion(chat_request, request)

    except HTTPException:
        # 重新抛出 HTTP 异常
        raise
    except Exception as e:
        logger.error("/responses 请求处理失败: %s", redact_data_urls(e))
        debug.api(f"错误: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="生成失败") from e


def _adapt_clawdbot_request(raw_body: dict) -> ChatCompletionRequest:
    """
    自适应解析 ClawdBot 的请求格式

    处理 ClawdBot 可能使用的非标准字段或格式
    """
    debug.api(f"ClawdBot 请求字段: {sorted(raw_body.keys())}")

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
            messages = _adapt_responses_input(adapted["input"])
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
    for param in ["temperature", "top_p", "max_tokens"]:
        if param not in adapted:
            adapted[param] = None

    # Responses clients commonly omit stream or send it as null.
    if adapted.get("stream") is None:
        adapted["stream"] = False

    try:
        return ChatCompletionRequest(**adapted)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("自适应解析失败: %s", type(e).__name__)
        raise HTTPException(status_code=400, detail="无法解析请求格式") from e


def _adapt_responses_input(value: object) -> list[dict]:
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    if not isinstance(value, list) or not value:
        raise HTTPException(status_code=400, detail="无法解析请求格式")
    if all(
        isinstance(item, dict) and "role" in item and "content" in item
        for item in value
    ):
        return _fix_messages_format(value, preserve_parts=True)
    return [
        {
            "role": "user",
            "content": _extract_content_from_list(value, preserve_parts=True),
        }
    ]


def _extract_content_from_list(
    content_list: list,
    *,
    preserve_parts: bool = False,
) -> str | list[dict]:
    """
    从内容列表中提取字符串内容

    ClawdBot 可能发送消息内容作为嵌套对象列表
    """
    if not isinstance(content_list, list):
        raise HTTPException(status_code=400, detail="无法解析请求格式")

    canonical_parts: list[dict] = []
    has_image = False
    for item in content_list:
        if isinstance(item, dict):
            item_type = item.get("type")
            if item_type in {"input_text", "text"}:
                text = item.get("text")
                if not isinstance(text, str):
                    raise HTTPException(status_code=400, detail="无法解析请求格式")
                canonical_parts.append({"type": "text", "text": text})
            elif item_type in {"input_image", "image_url"}:
                image_url = item.get("image_url")
                if isinstance(image_url, dict):
                    image_url = image_url.get("url")
                if not isinstance(image_url, str):
                    raise HTTPException(status_code=400, detail="无法解析请求格式")
                canonical_parts.append(
                    {"type": "image_url", "image_url": {"url": image_url}}
                )
                has_image = True
            elif item_type is not None:
                raise HTTPException(
                    status_code=400, detail="unsupported multimodal content"
                )
            else:
                text_value = next(
                    (item[key] for key in ("text", "content", "value") if key in item),
                    None,
                )
                if isinstance(text_value, list):
                    nested = _extract_content_from_list(
                        text_value, preserve_parts=preserve_parts
                    )
                    if isinstance(nested, list):
                        canonical_parts.extend(nested)
                        has_image = has_image or any(
                            part.get("type") == "image_url" for part in nested
                        )
                    elif nested:
                        canonical_parts.append({"type": "text", "text": nested})
                elif isinstance(text_value, str):
                    canonical_parts.append({"type": "text", "text": text_value})
                else:
                    raise HTTPException(status_code=400, detail="无法解析请求格式")
        elif isinstance(item, list):
            nested = _extract_content_from_list(
                item, preserve_parts=preserve_parts
            )
            if isinstance(nested, list):
                canonical_parts.extend(nested)
                has_image = has_image or any(
                    part.get("type") == "image_url" for part in nested
                )
            elif nested:
                canonical_parts.append({"type": "text", "text": nested})
        elif isinstance(item, str):
            canonical_parts.append({"type": "text", "text": item})
        else:
            raise HTTPException(status_code=400, detail="无法解析请求格式")

    if has_image or preserve_parts:
        return canonical_parts
    return "\n".join(
        part["text"] for part in canonical_parts if part.get("type") == "text"
    )


def _fix_messages_format(
    messages: list,
    *,
    preserve_parts: bool = False,
) -> list:
    """
    修复消息格式

    处理 ClawdBot 发送的消息格式问题，包括嵌套列表
    """
    fixed_messages = []

    for msg in messages:
        if not isinstance(msg, dict):
            raise HTTPException(status_code=400, detail="无法解析请求格式")

        fixed_msg = msg.copy()

        # 修复 content 字段
        if "content" in msg:
            content = msg["content"]
            if isinstance(content, list):
                fixed_msg["content"] = _extract_content_from_list(
                    content,
                    preserve_parts=preserve_parts,
                )
            elif not isinstance(content, str):
                raise HTTPException(status_code=400, detail="无法解析请求格式")

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
            f"耗时: {_t_b64dec * 1000:.1f}ms"
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
            f"耗时: {_t_pil * 1000:.1f}ms"
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
            f"总耗时: {_t_total * 1000:.1f}ms "
            f"[b64解码={_t_b64dec * 1000:.1f} + PIL={_t_pil * 1000:.1f} + 转换+b64编码={_t_encode * 1000:.1f}]"
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
            f"[process-image] ❌ 失败: 耗时 {_t_total * 1000:.1f}ms, "
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
    format: str = Form(
        default="auto", description="输出格式: auto, webp, jpeg, png (auto=智能选择)"
    ),
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
            f"{TAG} 读取原始字节: {original_size} bytes ({original_size / 1024:.1f} KB), "
            f"耗时: {_t_read * 1000:.1f}ms (无 Base64 解码)"
        )

        # ── 阶段 2: PIL 解码 + 获取原始尺寸 ──
        _t_decode_start = _time.perf_counter()
        img = Image.open(_io.BytesIO(image_bytes))
        original_width, original_height = img.size
        original_format = (img.format or "").lower()  # "jpeg", "png", "webp", ...
        _t_decode = _time.perf_counter() - _t_decode_start

        debug.api(
            f"{TAG} PIL 图片解码: {original_width}x{original_height}, "
            f"mode={img.mode}, format={original_format}, 耗时: {_t_decode * 1000:.1f}ms "
            f"(lazy decode, 未 load 像素)"
        )

        # ── 阶段 3: 判断是否需要缩放 ──
        need_resize = max_dim > 0 and (
            original_width > max_dim or original_height > max_dim
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
                f"(max_dim={max_dim}), 耗时: {_t_resize * 1000:.1f}ms, "
                f"像素数: {original_width * original_height} → {new_width * new_height} "
                f"(减少 {(1 - new_width * new_height / (original_width * original_height)) * 100:.0f}%)"
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
            # RGBA/P 等含透明通道的模式需要特殊处理：SGLang/Qwen3-VL 对 RGBA PNG 将透明区域
            # 合成为黑色背景，导致模型只看到前景 Logo 而丢失背景内容。必须先合成白色背景再输出。
            _needs_rgb_convert = img.mode in ("RGBA", "P", "PA", "LA")
            if not resized and not _needs_rgb_convert:
                # 无需缩放且格式兼容 → 直接透传原始字节, 零重编码!
                processed_bytes = image_bytes
                # 保留原始 MIME 类型
                _fmt_map = {
                    "jpeg": "jpeg",
                    "jpg": "jpeg",
                    "webp": "webp",
                    "png": "png",
                    "gif": "gif",
                    "bmp": "jpeg",
                }
                output_format = _fmt_map.get(original_format, "jpeg")
                _t_encode = _time.perf_counter() - _t_encode_start
                debug.api(
                    f"{TAG} ⚡ 原格式透传 ({original_format}): 跳过 PIL 重编码, 直接使用原始字节 "
                    f"({original_size} bytes), 耗时: {_t_encode * 1000:.1f}ms (几乎为零!)"
                )
            else:
                # 需要缩放 或 含透明通道 → 转 RGB 后编码为 JPEG
                img.load()
                if img.mode == "RGBA":
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[3])  # alpha 合成到白色背景
                    img = background
                elif img.mode == "LA":
                    rgba_tmp = img.convert("RGBA")
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    background.paste(rgba_tmp, mask=rgba_tmp.split()[3])
                    img = background
                elif img.mode in ("P", "PA"):
                    img = img.convert("RGBA")
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[3])
                    img = background
                elif img.mode != "RGB":
                    img = img.convert("RGB")
                output_buffer = _io.BytesIO()
                img.save(output_buffer, format="JPEG", quality=85, optimize=False)
                processed_bytes = output_buffer.getvalue()
                output_format = "jpeg"
                _t_encode = _time.perf_counter() - _t_encode_start
                reason = "含透明通道,已合成白色背景" if _needs_rgb_convert else "缩放后"
                debug.api(
                    f"{TAG} JPEG 编码 (auto, {reason}): {len(processed_bytes)} bytes, "
                    f"耗时: {_t_encode * 1000:.1f}ms"
                )
        elif format_lower in ("jpeg", "jpg"):
            if not resized and original_format in ("jpeg", "jpg"):
                processed_bytes = image_bytes
                output_format = "jpeg"
                _t_encode = _time.perf_counter() - _t_encode_start
                debug.api(
                    f"{TAG} ⚡ JPEG 透传: 直接使用原始字节, "
                    f"耗时: {_t_encode * 1000:.1f}ms"
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
                    f"耗时: {_t_encode * 1000:.1f}ms"
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
                f"耗时: {_t_encode * 1000:.1f}ms (注意: WebP 编码远慢于 JPEG)"
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
                f"耗时: {_t_encode * 1000:.1f}ms"
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
                f"耗时: {_t_encode * 1000:.1f}ms"
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
            f"{len(processed_b64)} chars ({len(processed_b64) / 1024:.1f} KB), "
            f"耗时: {_t_b64 * 1000:.1f}ms"
        )

        # ── 总结 ──
        pixel_reduction = (
            f", 像素缩减 {(1 - new_width * new_height / (original_width * original_height)) * 100:.0f}%"
            if resized
            else ""
        )
        debug.api(
            f"{TAG} ✅ 完成: {original_width}x{original_height} → "
            f"{new_width}x{new_height} {output_format}, "
            f"原始 {original_size / 1024:.1f}KB → 处理后 {len(processed_bytes) / 1024:.1f}KB "
            f"(压缩比 {len(processed_bytes) / original_size * 100:.1f}%{pixel_reduction}), "
            f"总耗时: {_t_total * 1000:.1f}ms "
            f"[读取={_t_read * 1000:.1f} + 解码={_t_decode * 1000:.1f} + "
            f"缩放={_t_resize * 1000:.1f} + 编码={_t_encode * 1000:.1f} + b64={_t_b64 * 1000:.1f}]"
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
            f"耗时: {_t_total * 1000:.1f}ms, 错误: {type(e).__name__}: {e}",
            exc_info=True,
        )
        debug.api(
            f"{TAG} ❌ 失败: {type(e).__name__}: {e}, "
            f"耗时: {_t_total * 1000:.1f}ms — "
            f"请检查图片文件是否损坏或格式不受支持"
        )
        raise HTTPException(status_code=400, detail=f"图片处理失败: {str(e)}")
