"""
本地 LLM 模型适配器 (Local LLM Model)

继承 smolagents.Model，桥接现有 LLMService，
使 smolagents Agent 可直接调用本地推理后端（SGLang/vLLM），无需 HTTP 回环。
"""

from __future__ import annotations

import base64
import contextvars
import io
import json
import math
import threading
from dataclasses import dataclass
from copy import copy, deepcopy
from typing import AbstractSet, Any, Generator, Literal, Sequence
from uuid import uuid4

from smolagents.models import (
    ChatMessage,
    ChatMessageStreamDelta,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    ChatMessageToolCallStreamDelta,
    MessageRole,
    Model,
    get_clean_message_list,
    get_tool_json_schema,
    tool_role_conversions,
)
from smolagents.monitoring import TokenUsage

from backend.config import get_config
from backend.llm_service import (
    GenerationResult,
    LLMService,
    Message,
    prepare_multimodal_messages,
)
from backend.agent.canonical_messages import (canonicalize_model_messages, clone_message, message_to_template, build_switch_agent_schema, call_to_dict)
from backend.agent.qwen_tool_protocol import (native_control_prose, parse_qwen_protocol, validate_qwen_tool_calls, QwenStreamControlGate, QwenToolProtocolError)
from backend.agent.text_tool_protocol import (
    NaturalText,
    StreamControlGate,
    SwitchAgentCommand,
    TextToolCall,
    TextToolProtocolError,
    ToolCallSequence,
    format_switch_agent,
    has_malformed_control_attempt,
    parse_text_protocol,
    reject_legacy_opener,
    starts_new_control_opener,
    starts_textual_tool_call_attempt,
)
from backend.utils.logger import get_logger

logger = get_logger(__name__)


_normal_turn_enable_thinking: contextvars.ContextVar = contextvars.ContextVar(
    "_normal_turn_enable_thinking", default=None
)


def _tool_json_schema(tool: Any) -> dict[str, Any]:
    schema = deepcopy(tool) if isinstance(tool, dict) else get_tool_json_schema(tool)
    if getattr(tool, "name", None) == "dispatching_parallel_agent":
        schema["function"]["parameters"]["additionalProperties"] = False
    return schema


def _reasoning_extra_body_from_context() -> dict[str, dict[str, bool]] | None:
    enable_thinking = _normal_turn_enable_thinking.get()
    if enable_thinking is None:
        return None
    return {"chat_template_kwargs": {"enable_thinking": bool(enable_thinking)}}


def _should_retry_without_extra_body(error: object) -> bool:
    text = str(error or "").lower()
    if not text:
        return False
    return (
        (
            "extra_body" in text
            or "chat_template_kwargs" in text
            or "enable_thinking" in text
        )
        and (
            "400" in text
            or "422" in text
            or "not supported" in text
            or "unsupported" in text
            or "unknown field" in text
            or "extra inputs are not permitted" in text
            or "unrecognized" in text
            or "unexpected keyword" in text
        )
    )


def _inject_runtime_context(
    messages: list[ChatMessage],
    runtime_context: str | None,
) -> list[ChatMessage]:
    if not runtime_context:
        return messages

    injected = list(messages)
    for idx, message in enumerate(injected):
        if message.role != MessageRole.SYSTEM:
            continue
        existing = message.content or ""
        merged = f"{existing}\n\n{runtime_context}" if existing else runtime_context
        injected[idx] = clone_message(message, content=merged)
        return injected

    insert_at = len(injected)
    for idx in range(len(injected) - 1, -1, -1):
        if injected[idx].role == MessageRole.USER:
            insert_at = idx
            break
    injected.insert(insert_at, ChatMessage(role=MessageRole.SYSTEM, content=runtime_context))
    return injected


@dataclass(frozen=True)
class AssistantControlResolution:
    source: Literal["natural", "text", "native"]
    tool_calls: tuple[ChatMessageToolCall, ...] = ()
    switch_agent: SwitchAgentCommand | None = None
    control_call: ChatMessageToolCall | None = None


class LocalLLMModel(Model):
    """
    将本地 LLMService 包装为 smolagents Model。

    直接调用 LLMService.chat()（同步），绕过 HTTP 回环。
    当前仅服务于 ToolCallingAgent（结构化 tool_calls）。
    """

    def __init__(
        self,
        llm_service: LLMService,
        model_id: str | None = None,
        runtime_context: str | None = None,
        session: object | None = None,
        **kwargs,
    ):
        super().__init__(model_id=model_id or "local-llm", **kwargs)
        self.llm_service = llm_service
        self.runtime_context = runtime_context
        self._session = session
        self._stream_state_lock = threading.Lock()
        self._stream_states: dict[str, dict[str, Any]] = {}
        self._unscoped_stream_state = {"content": "", "reasoning": ""}
        self._stream_diagnostics: dict[str, dict[str, Any]] = {}
        self._unscoped_stream_diagnostics: dict[str, Any] = {}
        self._aborted_stream_ids: set[str | None] = set()

    @property
    def native_tool_protocol(self) -> bool:
        return getattr(getattr(self, "_session", None), "tool_protocol", "legacy_text") == "qwen35-native"

    def _request_tools(self, tools) -> list[dict] | None:
        hidden = {"final_answer", "wikipedia_fetch", "wikipedia_search", "wikipedia_summary"}
        schemas = [_tool_json_schema(t) for t in (tools or ())]
        schemas = [s for s in schemas if s["function"]["name"] not in hidden]
        return schemas or None

    def _prepare_model_messages(self, messages):
        pre_encoded = self._pre_encode_images(
            list(messages), trusted_context_id=getattr(self._session, "session_id", None))
        if self.native_tool_protocol:
            canonical = canonicalize_model_messages(pre_encoded)
            clean = [message_to_template(m) for m in canonical]
        else:
            clean = get_clean_message_list(pre_encoded, role_conversions=tool_role_conversions,
                                           convert_images_to_image_urls=True)
        protected = self._extract_trusted_desktop_urls(clean)
        clean = self._put_images_first(clean)
        canonical = canonicalize_model_messages(clean)
        for m in canonical:
            m.content = self._convert_content(m.content)
        return canonical, protected

    def _raw_tool_capture_extra_body(self, tools, extra_body=None):
        """Opt in native Agent calls to raw transport without changing the prompt."""
        inference = getattr(getattr(self.llm_service, "config", None), "inference", None)
        if not (self.native_tool_protocol and tools
                and getattr(inference, "capture_raw_tool_calls", False)):
            return extra_body
        body = deepcopy(extra_body or {})
        template = dict(body.get("chat_template_kwargs") or {})
        template.update(raw_tool_call_passthrough=True, raw_request_tools=deepcopy(tools))
        body.update(tool_choice="none", skip_special_tokens=False, chat_template_kwargs=template)
        return body

    def _retry_extra_body(self, tools, extra_body):
        # Native template kwargs determine the rendered prefix. Preserve them
        # and raw transport even when optional top-level sampling knobs fail.
        # Unsupported essential flags remain an error, never a silent downgrade.
        required = self._raw_tool_capture_extra_body(tools)
        if required is None:
            return None
        template = deepcopy((extra_body or {}).get("chat_template_kwargs") or {})
        return self._raw_tool_capture_extra_body(tools, {"chat_template_kwargs": template})

    def _control_gate(self):
        return QwenStreamControlGate() if self.native_tool_protocol else StreamControlGate()

    def _set_stream_state(
        self,
        *,
        request_id: str | None,
        content: str,
        reasoning: str,
        provider_tool_calls: Sequence[dict | ChatMessageToolCall] | None = None,
    ) -> None:
        state: dict[str, Any] = {"content": content, "reasoning": reasoning}
        if provider_tool_calls:
            state["provider_tool_calls"] = provider_tool_calls
        with self._stream_state_lock:
            if request_id is not None:
                self._stream_states[request_id] = state
            else:
                self._unscoped_stream_state = state

    def get_stream_state(
        self, request_id: str | None = None
    ) -> dict[str, Any] | None:
        with self._stream_state_lock:
            if request_id is not None:
                state = self._stream_states.get(request_id)
                return dict(state) if state is not None else None
            return dict(self._unscoped_stream_state)

    def _update_stream_diagnostics(
        self, request_id: str | None, **updates: Any
    ) -> None:
        with self._stream_state_lock:
            if request_id is not None:
                self._stream_diagnostics.setdefault(request_id, {}).update(updates)
            else:
                self._unscoped_stream_diagnostics.update(updates)

    def get_stream_diagnostics(
        self, request_id: str | None = None
    ) -> dict[str, Any]:
        with self._stream_state_lock:
            if request_id is not None:
                return dict(self._stream_diagnostics.get(request_id, {}))
            return dict(self._unscoped_stream_diagnostics)

    def clear_stream_state(self, request_id: str | None = None) -> None:
        with self._stream_state_lock:
            self._aborted_stream_ids.discard(request_id)
            if request_id is not None:
                self._stream_states.pop(request_id, None)
                self._stream_diagnostics.pop(request_id, None)
            else:
                self._unscoped_stream_state = {"content": "", "reasoning": ""}
                self._unscoped_stream_diagnostics = {}

    def was_stream_aborted(self, request_id: str | None = None) -> bool:
        with self._stream_state_lock:
            return request_id in self._aborted_stream_ids

    def _remember_compactable_payload(
        self,
        llm_messages: list[Message],
        *,
        assistant_content: str | None = None,
        has_tool_calls: bool = False,
        assistant_tool_calls=None,
        assistant_reasoning: str | None = None,
        tools=None,
    ) -> None:
        if not self._can_update_compactable_payload():
            return
        if self.native_tool_protocol:
            if not (assistant_content or assistant_tool_calls or assistant_reasoning):
                return
            from .qwen_tool_protocol import native_control_prose
            payload = deepcopy(llm_messages)
            payload.append(Message(role="assistant", content=native_control_prose(assistant_content or "")
                if assistant_tool_calls else (assistant_content or ""),
                tool_calls=[call_to_dict(c) for c in assistant_tool_calls] if assistant_tool_calls else None,
                reasoning_content=assistant_reasoning))
            self._session.last_compactable_llm_messages = payload
            memory_steps = getattr(getattr(getattr(self._session, "agent", None), "memory", None), "steps", None)
            # The current response is accepted before smolagents appends its
            # ActionStep. Retain identity anchors, not a rewritten history.
            self._session.last_compactable_memory_steps = tuple(memory_steps) if isinstance(memory_steps, list) else None
            self._session.last_compactable_llm_tools = deepcopy(self._request_tools(tools))
            self._session.last_compactable_agent_name = getattr(self._session, "current_agent_name", None)
            return
        if not assistant_content:
            return
        if has_tool_calls:
            return
        try:
            protocol_result = parse_text_protocol(assistant_content)
        except TextToolProtocolError:
            return
        if not isinstance(protocol_result, NaturalText):
            return
        payload = deepcopy(llm_messages)
        if assistant_content:
            payload.append(Message(role="assistant", content=assistant_content))
        self._session.last_compactable_llm_messages = payload
        self._session.last_compactable_agent_name = getattr(self._session, "current_agent_name", None)

    def _can_update_compactable_payload(self) -> bool:
        if self._session is None:
            return False
        if getattr(self._session, "answering_now", False) or getattr(self._session, "answer_now", False):
            return False
        return getattr(self._session, "current_agent_name", None) != "coordinator_1"

    def _clear_compactable_payload_for_current_call(self) -> None:
        if self.native_tool_protocol:
            # Keep the last successful prefix until a newer accepted response replaces it.
            return
        if not self._can_update_compactable_payload():
            return
        self._session.last_compactable_llm_messages = None
        self._session.last_compactable_agent_name = None

    def _commit_computer_use_history_if_accepted(
        self, accepted: bool, *, stage=None, session=None, request_id=None, run_scope_id=None,
    ) -> bool:
        """Commit only the candidate serialized by this ordinary, live request."""
        if (not accepted or stage is None or session is None or self._session is not session
                or getattr(session, "is_child_context", False)
                or getattr(session, "answering_now", False) or getattr(session, "answer_now", False)
                or getattr(session, "aborted", False)):
            return False
        from backend.llm_service import is_request_aborted
        if request_id is not None and is_request_aborted(request_id):
            return False
        if run_scope_id is not None:
            from .agent_runner import run_abort_manager
            if run_abort_manager.is_aborted(run_scope_id) or run_scope_id not in session.capture_active_run_scopes():
                return False
        from .computer_use_history import commit_computer_use_history_omission
        return commit_computer_use_history_omission(session, stage)

    def _record_calibration_request_if_accepted(
        self, accepted: bool, provider_request: dict[str, Any], trusted_urls: frozenset[str]
    ) -> None:
        """Optional evidence side channel; never alters provider or Agent state."""
        session = self._session
        sink = getattr(session, "computer_use_calibration_evidence", None)
        if (not accepted or sink is None or not trusted_urls or session is None
                or getattr(sink, "session_id", None) != getattr(session, "session_id", None)):
            return
        try:
            from .computer_use_calibration_evidence import trusted_observation_for_request

            provenance = trusted_observation_for_request(session, trusted_urls)
            if provenance is None:
                return
            sink.record_accepted_request(
                provider_request.get("request_id"), provider_request,
                trusted_urls=trusted_urls,
                observation_id=provenance.observation_id,
                geometry_version=provenance.geometry_version,
            )
        except Exception as exc:
            # Evidence is never allowed to fail or reshape a production turn.
            logger.warning("computer-use calibration evidence capture failed: %s", exc)

    @staticmethod
    def _pre_encode_images(
        messages: list[ChatMessage], *, trusted_context_id: str | None = None
    ) -> list[ChatMessage]:
        """
        在 get_clean_message_list 之前，把消息中的 PIL 图片用原始格式（JPEG/PNG）编码为 data URL。

        smolagents 的 encode_image_base64 / make_image_url 无论输入是什么格式，
        都强制转换为 PNG（image.save(buf, format="PNG")），导致：
          - JPEG 354KB → PNG 1985KB（5-6x 膨胀）
          - 增大 visual token 处理时间，浪费带宽

        本函数在 smolagents 处理之前，把 {"type": "image", "image": PIL} 转换为
        {"type": "image_url", "image_url": {"url": "data:image/{fmt};base64,..."}}，
        格式和 PIL.format 属性一致（JPEG 保持 JPEG，PNG 保持 PNG）。
        get_clean_message_list 遇到已经是 image_url 的条目会直接跳过，不再重新编码。
        """
        try:
            from PIL import Image as _PILImage
        except ImportError:
            return messages  # PIL not available, skip

        result = []
        for msg in messages:
            content = msg.content
            if not isinstance(content, list):
                result.append(msg)
                continue

            has_pil = any(
                isinstance(item, dict)
                and item.get("type") == "image"
                and isinstance(item.get("image"), _PILImage.Image)
                for item in content
            )
            if not has_pil:
                result.append(msg)
                continue

            new_content = []
            for item in content:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "image"
                    and isinstance(item.get("image"), _PILImage.Image)
                ):
                    pil_img = item["image"]
                    trusted_desktop = False
                    if trusted_context_id:
                        from .computer_use_history import computer_use_provenance

                        trusted_desktop = computer_use_provenance(pil_img, trusted_context_id) is not None

                    # 优先使用预编码字节（由 agent_runner 处理后一次性编码），
                    # 避免对已经是 JPEG 的像素再次 JPEG 编码导致的质量损失。
                    if not trusted_desktop and hasattr(pil_img, "_pre_encoded_b64") and hasattr(
                        pil_img, "_pre_encoded_mime"
                    ):
                        b64 = pil_img._pre_encoded_b64
                        mime = pil_img._pre_encoded_mime
                        logger.debug(
                            f"_pre_encode_images: 使用预编码字节, mime={mime}, "
                            f"base64_len={len(b64)}"
                        )
                    else:
                        # 回退：手动编码（非 agent 路径的 PIL 图片）
                        fmt = "PNG" if trusted_desktop else (getattr(pil_img, "format", None) or "JPEG").upper()
                        if fmt not in ("JPEG", "PNG", "WEBP", "GIF"):
                            fmt = "JPEG"
                        # JPEG 不支持 RGBA/P 模式
                        if fmt == "JPEG" and pil_img.mode not in ("RGB", "L", "CMYK"):
                            pil_img = pil_img.convert("RGB")
                        buf = io.BytesIO()
                        save_kwargs = {"quality": 85} if fmt == "JPEG" else {}
                        pil_img.save(buf, format=fmt, **save_kwargs)
                        b64 = base64.b64encode(buf.getvalue()).decode()
                        mime = f"image/{fmt.lower()}"
                        logger.debug(
                            f"_pre_encode_images: {pil_img.mode} {pil_img.size} {fmt} "
                            f"→ {len(buf.getvalue()) / 1024:.1f}KB"
                        )
                    new_content.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                            **({"_trusted_desktop_original": True} if trusted_desktop else {}),
                        }
                    )
                else:
                    new_content.append(item)

            new_msg = copy(msg)
            new_msg.content = new_content
            result.append(new_msg)

        return result

    @staticmethod
    def _extract_trusted_desktop_urls(clean_messages: list[dict]) -> frozenset[str]:
        """Consume private provenance tags before provider serialization."""
        protected: set[str] = set()
        for message in clean_messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict) or not item.pop("_trusted_desktop_original", False):
                    continue
                image_url = item.get("image_url")
                url = image_url.get("url") if isinstance(image_url, dict) else None
                if isinstance(url, str) and url.startswith("data:image/png;base64,"):
                    protected.add(url)
        return frozenset(protected)

    @staticmethod
    def _put_images_first(clean_messages: list[dict]) -> list[dict]:
        """
        将消息内容中的 image_url 项移到 text 项前面。

        Qwen3-VL 要求图片 token 出现在文本之前才能有效进行视觉锚定（visual grounding）。
        smolagents 的 TaskStep.to_messages() 生成的内容顺序是先文字后图片，
        此函数在最终发送给 SGLang 前对内容重排：image_url → text/其他。
        """
        for msg in clean_messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            has_image = any(
                isinstance(item, dict) and item.get("type") == "image_url"
                for item in content
            )
            if not has_image:
                continue
            images = [
                item
                for item in content
                if isinstance(item, dict) and item.get("type") == "image_url"
            ]
            others = [
                item
                for item in content
                if not (isinstance(item, dict) and item.get("type") == "image_url")
            ]
            msg["content"] = images + others
            logger.debug(
                f"_put_images_first: 图片移至文字前，图片数={len(images)}, 其他={len(others)}"
            )
        return clean_messages

    def generate(
        self,
        messages: list[ChatMessage],
        stop_sequences: list[str] | None = None,
        response_format: dict[str, str] | None = None,
        tools_to_call_from: list | None = None,
        **kwargs,
    ) -> ChatMessage:
        """
        调用本地 LLM 推理后端生成响应。

        1. 将 smolagents ChatMessage 转换为 LLMService Message 格式
        2. 若有 tools_to_call_from，转换为 OpenAI tools 格式
        3. 调用 LLMService.chat() 同步获取结果
        4. 解析响应中的 tool_calls（若有），封装为 ChatMessage 返回
        """
        # 1. 转换消息格式
        request_id = kwargs.get("request_id")
        computer_use_history_session = self._session
        from .computer_use_history import bind_computer_use_history_request
        computer_use_history_stage = bind_computer_use_history_request(
            computer_use_history_session, kwargs.get("computer_use_history_stage"), messages)
        from .agent_runner import current_session_context
        computer_use_history_run_scope = current_session_context.get_abort_scope()
        temperature = kwargs.get("temperature")
        max_tokens = kwargs.get("max_tokens")
        extra_body = copy(kwargs.get("extra_body")) if kwargs.get("extra_body") else None
        if extra_body is None:
            extra_body = _reasoning_extra_body_from_context()
        retry_without_extra_body = bool(kwargs.get("retry_without_extra_body"))
        runtime_context = kwargs.get("runtime_context") or self.runtime_context
        # 1. 转换消息格式
        # 先预编码 PIL 图片（保持原始格式 JPEG/PNG），再交给 smolagents
        # 处理。smolagents 的 encode_image_base64 总是转为 PNG，JPEG→PNG 会
        # 造成 5-6x 体积膨胀。预编码后 smolagents 看到已经是 image_url 类型，直接跳过。
        llm_messages, protected_desktop_urls = self._prepare_model_messages(messages)
        llm_messages = _inject_runtime_context(llm_messages, runtime_context)
        service_config = getattr(self.llm_service, "config", None)
        max_image_dimension = getattr(
            getattr(service_config, "model", None), "max_image_dimension", None
        )
        if not isinstance(max_image_dimension, int):
            max_image_dimension = get_config().model.max_image_dimension
        llm_messages = prepare_multimodal_messages(
            llm_messages,
            max_image_dimension=max_image_dimension,
            preserve_image_urls=protected_desktop_urls,
        )
        self._clear_compactable_payload_for_current_call()
        if self._session is not None:
            try:
                from ..compact.prompt_diagnostics import make_prompt_snapshot

                self._session.last_llm_prompt_snapshot = make_prompt_snapshot(
                    "normal_agent",
                    llm_messages,
                    tools=self._request_tools(tools_to_call_from) if self.native_tool_protocol else None,
                )
            except Exception as exc:
                logger.debug(f"prompt snapshot skipped: {exc}")

        # 2. 转换工具定义
        # 注：SmartToolCallingAgent.tools_and_managed_agents 已在上层过滤掉隐藏工具
        # 仅保留特殊协议工具的防御性过滤；一般业务工具由 session snapshot 决定。
        tools_json = self._request_tools(tools_to_call_from)
        extra_body = self._raw_tool_capture_extra_body(tools_json, extra_body)

        # 3. 调用 LLMService
        chat_kwargs = {
            "messages": llm_messages,
            "stream": False,
            "tools": tools_json,
            "request_id": request_id,
            "preserve_prompt_shape": True,
        }
        if temperature is not None:
            chat_kwargs["temperature"] = temperature
        if max_tokens is not None:
            chat_kwargs["max_new_tokens"] = max_tokens
        if extra_body is not None:
            chat_kwargs["extra_body"] = extra_body

        accepted_kwargs = chat_kwargs
        from .computer_use_delivery_diagnostics import DeliveryDiagnostics
        delivery = DeliveryDiagnostics(self.llm_service, computer_use_history_session,
            llm_messages, protected_desktop_urls, computer_use_history_stage, computer_use_history_run_scope)
        try:
            chat_result = delivery.chat(self.llm_service, chat_kwargs)
        except Exception as exc:
            if (
                not retry_without_extra_body
                or extra_body is None
                or not _should_retry_without_extra_body(exc)
            ):
                raise
            logger.warning(
                "LLM backend rejected extra_body for coordinator routing sync call; retrying without optional extra_body fields"
            )
            retry_kwargs = dict(chat_kwargs)
            retry_kwargs.pop("extra_body", None)
            essential_body = self._retry_extra_body(tools_json, extra_body)
            if essential_body is not None:
                retry_kwargs["extra_body"] = essential_body
            accepted_kwargs = retry_kwargs
            chat_result = delivery.chat(self.llm_service, retry_kwargs)
        result: GenerationResult = chat_result  # type: ignore[assignment]

        # 4. 解析响应
        content = result.text or ""
        token_usage = None

        # 解析 token 使用量
        if result.usage:
            token_usage = TokenUsage(
                input_tokens=result.usage.get("prompt_tokens", 0),
                output_tokens=result.usage.get("completion_tokens", 0),
            )
            image_tokens = (result.usage.get("prompt_tokens_details") or {}).get(
                "image_tokens"
            )
            if image_tokens:
                logger.info(
                    f"非流式 token 统计: input={token_usage.input_tokens}, "
                    f"output={token_usage.output_tokens}, image_tokens={image_tokens}"
                )

        try:
            resolution = self._resolve_assistant_control(
                content,
                getattr(result, "tool_calls", None),
                tools_to_call_from,
            )
        except TextToolProtocolError as exc:
            if self.native_tool_protocol and native_control_prose(content) != content:
                exc.raw_call_debug = {"text": content, "source": "model_text"}
            raise
        tool_calls = list(resolution.tool_calls) or None

        accepted = bool(
                content.strip()
                or tool_calls
                or getattr(result, "tool_calls", None)
                or resolution.switch_agent is not None
            )
        self._record_calibration_request_if_accepted(accepted, accepted_kwargs, protected_desktop_urls)
        delivery.accepted(accepted_kwargs, accepted)
        self._commit_computer_use_history_if_accepted(accepted,
            stage=computer_use_history_stage, session=computer_use_history_session,
            request_id=request_id, run_scope_id=computer_use_history_run_scope)

        self._remember_compactable_payload(
            llm_messages,
            assistant_content=content,
            assistant_tool_calls=resolution.tool_calls or ((resolution.control_call,) if resolution.control_call else ()),
            assistant_reasoning=getattr(result, "reasoning_content", None),
            tools=tools_to_call_from,
            has_tool_calls=bool(
                tool_calls
                or getattr(result, "tool_calls", None)
                or resolution.switch_agent is not None
            ),
        )

        message = ChatMessage(
            role=MessageRole.ASSISTANT, content=content,
            tool_calls=tool_calls, token_usage=token_usage,
        )
        message.control_resolution = resolution
        if self.native_tool_protocol:
            # Display-only source; accepted canonical calls remain separate.
            message.raw_provider_tool_calls = deepcopy(getattr(result, "tool_calls", None))
        message.reasoning_content = getattr(result, "reasoning_content", None)
        return message

    def generate_stream(
        self,
        messages: list[ChatMessage],
        stop_sequences: list[str] | None = None,
        response_format: dict[str, str] | None = None,
        tools_to_call_from: list | None = None,
        **kwargs,
    ) -> Generator[ChatMessageStreamDelta, None, None]:
        """
        流式调用本地 LLM，逐 token 返回 ChatMessageStreamDelta。

        Legacy streams omit tools to avoid historical server parser issues.
        Native streams retain canonical tools; optional raw capture bypasses
        server parsing while preserving the same rendered tools prefix.

        策略：
        - reasoning_content 和 content 都作为 content delta 实时推送（思考框流式显示）
        - 同时按 request_id 追踪 content，供 _step_stream
          在流式结束后用于工具调用解析和 final_answer 提取（不含 reasoning）
        - 空流时回退到非流式生成
        """
        request_id = kwargs.get("request_id")
        computer_use_history_session = self._session
        from .computer_use_history import bind_computer_use_history_request
        computer_use_history_stage = bind_computer_use_history_request(
            computer_use_history_session, kwargs.get("computer_use_history_stage"), messages)
        from .agent_runner import current_session_context
        computer_use_history_run_scope = current_session_context.get_abort_scope()
        temperature = kwargs.get("temperature")
        max_tokens = kwargs.get("max_tokens")
        extra_body = copy(kwargs.get("extra_body")) if kwargs.get("extra_body") else None
        if extra_body is None:
            extra_body = _reasoning_extra_body_from_context()
        retry_without_extra_body = bool(kwargs.get("retry_without_extra_body"))
        runtime_context = kwargs.get("runtime_context") or self.runtime_context
        # 1. 转换消息格式（同 generate，先预编码保持 JPEG 格式）
        llm_messages, protected_desktop_urls = self._prepare_model_messages(messages)
        llm_messages = _inject_runtime_context(llm_messages, runtime_context)
        service_config = getattr(self.llm_service, "config", None)
        max_image_dimension = getattr(
            getattr(service_config, "model", None), "max_image_dimension", None
        )
        if not isinstance(max_image_dimension, int):
            max_image_dimension = get_config().model.max_image_dimension
        llm_messages = prepare_multimodal_messages(
            llm_messages,
            max_image_dimension=max_image_dimension,
            preserve_image_urls=protected_desktop_urls,
        )
        self._clear_compactable_payload_for_current_call()
        if self._session is not None:
            try:
                from ..compact.prompt_diagnostics import make_prompt_snapshot

                self._session.last_llm_prompt_snapshot = make_prompt_snapshot(
                    "normal_agent",
                    llm_messages,
                    tools=self._request_tools(tools_to_call_from) if self.native_tool_protocol else None,
                )
            except Exception as exc:
                logger.debug(f"prompt snapshot skipped: {exc}")

        # Native keeps canonical tools; legacy retains its tools-free stream.
        # Raw capture changes transport parsing, not the rendered tools prefix.
        stream_kwargs = {
            "messages": llm_messages,
            "stream": True,
            "tools": self._request_tools(tools_to_call_from) if self.native_tool_protocol else None,
            "request_id": request_id,
            "stream_usage_id": request_id,
            "preserve_prompt_shape": True,
        }
        extra_body = self._raw_tool_capture_extra_body(stream_kwargs["tools"], extra_body)
        if temperature is not None:
            stream_kwargs["temperature"] = temperature
        if max_tokens is not None:
            stream_kwargs["max_new_tokens"] = max_tokens
        if extra_body is not None:
            stream_kwargs["extra_body"] = extra_body

        accepted_stream_kwargs = stream_kwargs
        from .computer_use_delivery_diagnostics import DeliveryDiagnostics
        delivery = DeliveryDiagnostics(self.llm_service, computer_use_history_session,
            llm_messages, protected_desktop_urls, computer_use_history_stage, computer_use_history_run_scope)
        stream = delivery.chat(self.llm_service, stream_kwargs)
        retried_without_extra_body = False

        has_content = False
        aborted = False
        fallback_had_provider_calls = False
        control_gate = self._control_gate()
        provider_calls_by_index = {}
        stream_content = ""  # 仅追踪 content 部分（不含 reasoning）
        stream_reasoning = ""  # 仅追踪 reasoning 部分，用于显示和诊断
        self._set_stream_state(
            request_id=request_id,
            content=stream_content,
            reasoning=stream_reasoning,
        )
        self._update_stream_diagnostics(request_id)
        with self._stream_state_lock:
            self._aborted_stream_ids.discard(request_id)

        while True:
            for chunk in stream:
                diagnostic_updates = {
                    key: chunk[key]
                    for key in (
                        "finish_reason",
                        "stream_done",
                        "stream_residual_chars",
                    )
                    if key in chunk
                }
                if diagnostic_updates:
                    self._update_stream_diagnostics(
                        request_id, **diagnostic_updates
                    )
                if (retried_without_extra_body
                        and self._retry_extra_body(stream_kwargs["tools"], extra_body) is not None
                        and _should_retry_without_extra_body(chunk.get("content") or chunk.get("reasoning"))):
                    raise RuntimeError(chunk.get("content") or chunk.get("reasoning"))
                if (
                    retry_without_extra_body
                    and extra_body is not None
                    and not retried_without_extra_body
                    and _should_retry_without_extra_body(
                        chunk.get("content") or chunk.get("reasoning")
                    )
                ):
                    logger.warning(
                        "LLM backend rejected extra_body for coordinator routing stream call; retrying without optional extra_body fields"
                    )
                    retry_kwargs = dict(stream_kwargs)
                    retry_kwargs.pop("extra_body", None)
                    essential_body = self._retry_extra_body(stream_kwargs["tools"], extra_body)
                    if essential_body is not None:
                        retry_kwargs["extra_body"] = essential_body
                    accepted_stream_kwargs = retry_kwargs
                    retried_without_extra_body = True
                    has_content = False
                    control_gate = self._control_gate()
                    provider_calls_by_index = {}
                    stream_content = ""
                    stream_reasoning = ""
                    self._set_stream_state(
                        request_id=request_id,
                        content=stream_content,
                        reasoning=stream_reasoning,
                    )
                    self._update_stream_diagnostics(request_id)
                    stream = delivery.chat(self.llm_service, retry_kwargs)
                    break

                if chunk.get("aborted"):
                    logger.info("generate_stream: 检测到上游中止信号，停止当前流式生成")
                    aborted = True
                    break

                if self.native_tool_protocol and chunk.get("tool_calls"):
                    has_content = True
                    for part in chunk["tool_calls"]:
                        index = part.get("index", 0)
                        call = provider_calls_by_index.setdefault(index, {
                            "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        if part.get("id"):
                            call["id"] = part["id"]
                        function = part.get("function") or {}
                        if function.get("name"):
                            call["function"]["name"] += function["name"]
                        args = function.get("arguments")
                        if isinstance(args, dict):
                            call["function"]["arguments"] += json.dumps(args, ensure_ascii=False, allow_nan=False)
                        elif args is not None:
                            call["function"]["arguments"] += args

                reasoning = chunk.get("reasoning") or ""
                content = chunk.get("content") or ""

                if reasoning:
                    has_content = True
                    stream_reasoning += reasoning
                    self._set_stream_state(
                        request_id=request_id,
                        content=stream_content,
                        reasoning=stream_reasoning,
                    )
                    delta = ChatMessageStreamDelta(content=reasoning)
                    delta.reasoning = True
                    yield delta

                if content:
                    has_content = True
                    stream_content += content
                    self._set_stream_state(
                        request_id=request_id,
                        content=stream_content,
                        reasoning=stream_reasoning,
                    )
                    visible_content = control_gate.push(content)
                    if visible_content:
                        delta = ChatMessageStreamDelta(content=visible_content)
                        delta.reasoning = False
                        yield delta
            else:
                break

            if aborted:
                break
            continue

        logger.info(
            f"generate_stream 完成: reasoning={len(stream_reasoning)} 字符, "
            f"content={len(stream_content)} 字符"
        )
        if stream_content:
            preview = stream_content[-200:]
            logger.debug(f"generate_stream content 尾部: ...{preview}")

        if aborted:
            with self._stream_state_lock:
                self._aborted_stream_ids.add(request_id)
            return

        if has_content:
            visible_tail = control_gate.finish()
            if visible_tail:
                delta = ChatMessageStreamDelta(content=visible_tail)
                delta.reasoning = False
                yield delta

        # 4a. 空流回退（Qwen3-VL 在特殊条件下可能返回空增量）
        if not has_content:
            logger.warning("generate_stream: 流式响应为空，回退到非流式生成")
            fallback_kwargs = {
                "messages": llm_messages,
                "stream": False,
                "tools": stream_kwargs["tools"],
                "request_id": request_id,
                "preserve_prompt_shape": True,
            }
            if temperature is not None:
                fallback_kwargs["temperature"] = temperature
            if max_tokens is not None:
                fallback_kwargs["max_new_tokens"] = max_tokens
            fallback_extra = self._retry_extra_body(stream_kwargs["tools"], extra_body) if retried_without_extra_body else extra_body
            if fallback_extra is not None:
                fallback_kwargs["extra_body"] = fallback_extra
            result = delivery.chat(self.llm_service, fallback_kwargs, fallback=True)
            # 空流回退走非流式路径，provider 已解析出权威 usage；
            # 回填到流式统计快照，保证 get_stream_usage 仍返回服务端统计
            fallback_usage = getattr(result, "usage", None)
            if isinstance(fallback_usage, dict) and request_id:
                self.llm_service.record_stream_usage(request_id, fallback_usage)
            text = result.text or ""
            raw_fallback_tool_calls = getattr(result, "tool_calls", None)
            fallback_had_provider_calls = bool(raw_fallback_tool_calls)
            stream_content = text
            self._set_stream_state(
                request_id=request_id,
                content=stream_content,
                reasoning=stream_reasoning,
                provider_tool_calls=raw_fallback_tool_calls,
            )
            defers_to_step_boundary = (
                starts_new_control_opener(text)
                or starts_textual_tool_call_attempt(text)
                or has_malformed_control_attempt(text)
            )
            if raw_fallback_tool_calls and not defers_to_step_boundary and not self.native_tool_protocol:
                # Native-only fallback remains a structured provider path. Any
                # textual control attempt, however, must reach _step_stream with
                # both exact sources intact so that one boundary emits debug and
                # then performs fail-closed arbitration.
                resolution = self._resolve_assistant_control(
                    text,
                    raw_fallback_tool_calls,
                    tools_to_call_from,
                )
                fallback_tool_calls = resolution.tool_calls
                if fallback_tool_calls:
                    # Empty-response fallback preserves the pending screenshot
                    # projection even when its corrective response is nonempty.
                    self._record_calibration_request_if_accepted(True, fallback_kwargs, protected_desktop_urls)
                    delivery.accepted(fallback_kwargs, True, fallback=True)
                    yield ChatMessageStreamDelta(
                        tool_calls=[
                            ChatMessageToolCallStreamDelta(
                                index=index,
                                id=tool_call.id,
                                type=tool_call.type,
                                function=ChatMessageToolCallFunction(
                                    name=tool_call.function.name,
                                    arguments=json.dumps(
                                        tool_call.function.arguments,
                                        ensure_ascii=False,
                                    ),
                                ),
                            )
                            for index, tool_call in enumerate(fallback_tool_calls)
                        ]
                    )
                    return
                if resolution.switch_agent is not None:
                    self._record_calibration_request_if_accepted(True, fallback_kwargs, protected_desktop_urls)
                    delivery.accepted(fallback_kwargs, True, fallback=True)
                    return
            visible_text = control_gate.push(text) if text else ""
            visible_text += control_gate.finish()
            if visible_text:
                delta = ChatMessageStreamDelta(content=visible_text)
                delta.reasoning = False
                yield delta

        native_resolution = None
        if self.native_tool_protocol:
            raw_calls = ([provider_calls_by_index[i] for i in sorted(provider_calls_by_index)]
                         if provider_calls_by_index else None)
            if not has_content:
                raw_calls = raw_fallback_tool_calls
            native_resolution = self._resolve_assistant_control(stream_content, raw_calls, tools_to_call_from)
            with self._stream_state_lock:
                target_state = self._unscoped_stream_state if request_id is None else self._stream_states.setdefault(request_id, {})
                target_state["control_resolution"] = native_resolution
            if raw_calls:
                self._set_stream_state(request_id=request_id, content=stream_content,
                    reasoning=stream_reasoning, provider_tool_calls=raw_calls)
                with self._stream_state_lock:
                    target_state = self._unscoped_stream_state if request_id is None else self._stream_states[request_id]
                    target_state["control_resolution"] = native_resolution
            if raw_calls:
                accepted_calls = native_resolution.tool_calls or ((native_resolution.control_call,)
                    if native_resolution.control_call is not None else ())
                yield ChatMessageStreamDelta(tool_calls=[ChatMessageToolCallStreamDelta(
                    index=i, id=c.id, type=c.type,
                    function=ChatMessageToolCallFunction(name=c.function.name,
                        arguments=json.dumps(c.function.arguments, ensure_ascii=False, allow_nan=False)))
                    for i, c in enumerate(accepted_calls)])

        self._remember_compactable_payload(
            llm_messages,
            assistant_content=stream_content,
            assistant_tool_calls=(native_resolution.tool_calls or ((native_resolution.control_call,) if native_resolution.control_call else ())) if native_resolution else None,
            assistant_reasoning=stream_reasoning or None,
            tools=tools_to_call_from,
            has_tool_calls=fallback_had_provider_calls,
        )
        # A naturally completed stream is the ordinary transport acceptance
        # barrier.  Do not commit on the first delta: the provider can still
        # abort, fail, or trigger the empty-stream fallback after that delta.
        if has_content or stream_content.strip() or fallback_had_provider_calls:
            delivery.accepted(accepted_stream_kwargs if has_content else fallback_kwargs,
                              True, fallback=not has_content)
            self._record_calibration_request_if_accepted(
                True, fallback_kwargs if not has_content else accepted_stream_kwargs,
                protected_desktop_urls,
            )
            if has_content:
                self._commit_computer_use_history_if_accepted(True,
                    stage=computer_use_history_stage, session=computer_use_history_session,
                    request_id=request_id, run_scope_id=computer_use_history_run_scope)

    def _convert_content(self, content: Any) -> Any:
        """
        将 smolagents 消息内容转换为 LLMService 兼容格式。

        经 convert_images_to_image_urls=True 处理后，smolagents 已将图片转为
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}，
        此函数直接 pass-through 这些项即可。

        作为保底，仍处理以下两种残留情况：
          (a) {"type": "image", "image": <PIL.Image>}  — 未经 smolagents 转换的原始 PIL
          (b) {"type": "image", "image": "<raw_base64>"}  — smolagents 旧版/默认输出
        """
        if not isinstance(content, list):
            return content

        import base64
        import io

        converted = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "image_url":
                # smolagents 已转换的 image_url — 直接 pass-through
                url = item.get("image_url", {}).get("url", "")
                if url.startswith("data:"):
                    # 诊断：打印图片大小和 MIME
                    try:
                        header, b64_part = url.split(",", 1)
                        mime = header.split(":")[1].split(";")[0]
                        data_size = len(b64_part) * 3 // 4  # base64 → bytes approx
                        logger.info(
                            f"🖼 _convert_content: pass-through image_url, mime={mime}, "
                            f"base64_len={len(b64_part)}, approx_bytes={data_size}"
                        )
                    except Exception:
                        pass
                converted.append(item)
            elif isinstance(item, dict) and item.get("type") == "image":
                img_val = item.get("image")
                if img_val is None:
                    continue  # 无内容，跳过
                try:
                    from PIL import Image as PILImage

                    if isinstance(img_val, PILImage.Image):
                        # (a) PIL 对象：直接编码
                        buf = io.BytesIO()
                        fmt = getattr(img_val, "format", None) or "JPEG"
                        if fmt.upper() not in ("JPEG", "PNG", "WEBP", "GIF"):
                            fmt = "JPEG"
                        img_val.save(buf, format=fmt)
                        b64 = base64.b64encode(buf.getvalue()).decode()
                        mime = f"image/{fmt.lower()}"
                        logger.info(
                            f"🖼 _convert_content: PIL→base64, mode={img_val.mode}, "
                            f"size={img_val.size}, fmt={fmt}, bytes={len(buf.getvalue())}"
                        )
                    elif isinstance(img_val, str):
                        # (b) 裸 base64 字符串（smolagents 默认转换结果）：
                        #     直接复用，格式未知时默认 png（与 smolagents 一致）
                        b64 = img_val
                        mime = "image/png"
                        logger.info(f"🖼 _convert_content: raw_base64, len={len(b64)}")
                    elif isinstance(img_val, bytes):
                        b64 = base64.b64encode(img_val).decode()
                        mime = "image/png"
                    else:
                        logger.warning(f"未知图片类型 {type(img_val)}，跳过")
                        continue
                    converted.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        }
                    )
                except Exception as e:
                    logger.warning(f"图片内容转换失败，跳过: {e}")
            else:
                converted.append(item)
        return converted

    @staticmethod
    def _native_arguments_from_json(
        raw_arguments: str,
        *,
        call_index: int,
    ) -> Any:
        def reject_duplicate_keys(
            pairs: list[tuple[str, Any]],
        ) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise TextToolProtocolError(
                        "invalid_native_arguments_json",
                        (
                            f"Provider-native call {call_index} has duplicate "
                            f"argument key {key!r}."
                        ),
                        block_index=call_index,
                        duplicate_key=key,
                    )
                result[key] = value
            return result

        def reject_non_finite(value: str) -> Any:
            raise TextToolProtocolError(
                "invalid_native_arguments_json",
                (
                    f"Provider-native call {call_index} contains non-finite "
                    f"JSON constant {value}."
                ),
                block_index=call_index,
            )

        try:
            return json.loads(
                raw_arguments,
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=reject_non_finite,
            )
        except TextToolProtocolError:
            raise
        except json.JSONDecodeError as exc:
            raise TextToolProtocolError(
                "invalid_native_arguments_json",
                (
                    f"Provider-native call {call_index} has invalid argument JSON "
                    f"at line {exc.lineno}, column {exc.colno}: {exc.msg}."
                ),
                block_index=call_index,
                line=exc.lineno,
                column=exc.colno,
            ) from exc

    def _parse_raw_tool_calls(
        self,
        raw_tool_calls: Sequence[dict | ChatMessageToolCall],
    ) -> list[ChatMessageToolCall] | None:
        """Adapt provider-native structured calls without reading assistant text."""

        calls: list[ChatMessageToolCall] = []
        id_batch = uuid4().hex
        for call_index, raw_call in enumerate(raw_tool_calls, start=1):
            if isinstance(raw_call, ChatMessageToolCall):
                call_id = raw_call.id
                call_type = raw_call.type
                name = raw_call.function.name
                arguments = raw_call.function.arguments
            elif isinstance(raw_call, dict):
                function = raw_call.get("function")
                if not isinstance(function, dict):
                    raise TextToolProtocolError(
                        "invalid_native_tool_call",
                        (
                            f"Provider-native call {call_index} must contain one "
                            "function object."
                        ),
                        block_index=call_index,
                    )
                call_id = raw_call.get("id")
                call_type = raw_call.get("type", "function")
                name = function.get("name")
                if "arguments" not in function:
                    raise TextToolProtocolError(
                        "invalid_native_tool_call",
                        (
                            f"Provider-native call {call_index} must include an "
                            "arguments field, including for zero-argument tools."
                        ),
                        block_index=call_index,
                    )
                arguments = function["arguments"]
            else:
                raise TextToolProtocolError(
                    "invalid_native_tool_call",
                    f"Provider-native call {call_index} must be an object.",
                    block_index=call_index,
                )

            if call_type != "function":
                raise TextToolProtocolError(
                    "invalid_native_tool_call",
                    (
                        f"Provider-native call {call_index} has unsupported type "
                        f"{call_type!r}; expected 'function'."
                    ),
                    block_index=call_index,
                )
            if not isinstance(name, str) or not name:
                raise TextToolProtocolError(
                    "invalid_native_tool_call",
                    f"Provider-native call {call_index} has no valid function name.",
                    block_index=call_index,
                )
            if not isinstance(arguments, (str, dict)):
                raise TextToolProtocolError(
                    "arguments_must_be_object",
                    (
                        f"Provider-native call {call_index} arguments must be a "
                        "strict JSON string or object."
                    ),
                    block_index=call_index,
                )
            if isinstance(arguments, str):
                arguments = self._native_arguments_from_json(
                    arguments,
                    call_index=call_index,
                )
            if not isinstance(call_id, str) or not call_id:
                call_id = f"call_native_{id_batch}_{call_index - 1}"

            calls.append(
                ChatMessageToolCall(
                    id=call_id,
                    type="function",
                    function=ChatMessageToolCallFunction(
                        name=name,
                        arguments=arguments,
                    ),
                )
            )
        return calls or None

    @staticmethod
    def _adapt_text_calls(
        calls: Sequence[TextToolCall],
    ) -> tuple[ChatMessageToolCall, ...]:
        id_batch = uuid4().hex
        return tuple(
            ChatMessageToolCall(
                id=f"call_text_{id_batch}_{index}",
                type="function",
                function=ChatMessageToolCallFunction(
                    name=call.name,
                    arguments=call.arguments,
                ),
            )
            for index, call in enumerate(calls)
        )

    @staticmethod
    def _is_json_value(value: Any) -> bool:
        if value is None or isinstance(value, (str, bool)):
            return True
        if isinstance(value, int):
            return True
        if isinstance(value, float):
            return math.isfinite(value)
        if isinstance(value, list):
            return all(LocalLLMModel._is_json_value(item) for item in value)
        if isinstance(value, dict):
            return all(
                isinstance(key, str)
                and LocalLLMModel._is_json_value(item)
                for key, item in value.items()
            )
        return False

    @staticmethod
    def _matches_input_type(value: Any, expected_type: str) -> bool:
        if expected_type == "any":
            return LocalLLMModel._is_json_value(value)
        if expected_type == "string":
            return isinstance(value, str)
        if expected_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected_type == "number":
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and (not isinstance(value, float) or math.isfinite(value))
            )
        if expected_type == "boolean":
            return isinstance(value, bool)
        if expected_type == "array":
            return isinstance(value, list)
        if expected_type == "object":
            return isinstance(value, dict)
        if expected_type == "null":
            return value is None
        return False

    def _validate_tool_call_batch(
        self,
        calls: Sequence[ChatMessageToolCall],
        tools_to_call_from: Sequence[Any],
    ) -> None:
        """Validate the complete batch before any call can reach the agent."""

        tools_by_name = {
            tool.name: tool
            for tool in tools_to_call_from
            if isinstance(getattr(tool, "name", None), str)
        }
        for call_index, call in enumerate(calls, start=1):
            name = call.function.name
            tool = tools_by_name.get(name)
            if tool is None:
                allowed = ", ".join(sorted(tools_by_name)) or "<none>"
                raise TextToolProtocolError(
                    "unknown_tool",
                    (
                        f"Tool call {call_index} uses unknown tool {name!r}; "
                        f"allowed tools: {allowed}."
                    ),
                    block_index=call_index,
                )

            arguments = call.function.arguments
            if not isinstance(arguments, dict):
                raise TextToolProtocolError(
                    "arguments_must_be_object",
                    (
                        f"Tool call {call_index} ({name}) arguments must be one "
                        "JSON object."
                    ),
                    block_index=call_index,
                )
            if not self._is_json_value(arguments):
                raise TextToolProtocolError(
                    "argument_schema",
                    (
                        f"Tool call {call_index} ({name}) arguments contain a "
                        "non-JSON value."
                    ),
                    block_index=call_index,
                )

            inputs = getattr(tool, "inputs", None)
            if not isinstance(inputs, dict):
                inputs = {}
            unexpected = sorted(set(arguments) - set(inputs))
            if unexpected:
                raise TextToolProtocolError(
                    "argument_schema",
                    (
                        f"Tool call {call_index} ({name}) has unexpected argument "
                        f"{unexpected[0]!r}."
                    ),
                    block_index=call_index,
                )

            missing = [
                key
                for key, metadata in inputs.items()
                if key not in arguments
                and isinstance(metadata, dict)
                and not metadata.get("nullable", False)
                and "default" not in metadata
            ]
            if missing:
                raise TextToolProtocolError(
                    "argument_schema",
                    (
                        f"Tool call {call_index} ({name}) is missing required "
                        f"argument {missing[0]!r}."
                    ),
                    block_index=call_index,
                )

            for key, value in arguments.items():
                metadata = inputs.get(key)
                if not isinstance(metadata, dict):
                    raise TextToolProtocolError(
                        "argument_schema",
                        (
                            f"Tool call {call_index} ({name}) has no schema for "
                            f"argument {key!r}."
                        ),
                        block_index=call_index,
                    )
                if value is None and metadata.get("nullable", False):
                    continue
                expected_type = metadata.get("type", "any")
                if (
                    not isinstance(expected_type, str)
                    or not self._matches_input_type(value, expected_type)
                ):
                    raise TextToolProtocolError(
                        "argument_schema",
                        (
                            f"Tool call {call_index} ({name}) argument {key!r} "
                            f"must have JSON type {expected_type!r}."
                        ),
                        block_index=call_index,
                    )

    @staticmethod
    def _normalize_native_switch_target(arguments: Any) -> str | None:
        if isinstance(arguments, str):
            target = arguments.strip()
            return target or None
        if not isinstance(arguments, dict):
            return None

        candidate_keys = ("agent_name", "target_agent", "target", "name")
        candidates = [
            arguments[key].strip()
            for key in candidate_keys
            if isinstance(arguments.get(key), str) and arguments[key].strip()
        ]
        if len(candidates) != 1:
            return None
        return candidates[0]

    def _resolve_assistant_control(
        self,
        content: str,
        provider_tool_calls: Sequence[dict | ChatMessageToolCall] | None,
        tools_to_call_from: Sequence[Any] | None,
        *,
        allowed_agent_names: AbstractSet[str] | None = None,
    ) -> AssistantControlResolution:
        """Select exactly one control adapter and validate its whole batch."""

        if self.native_tool_protocol:
            schemas = self._request_tools(tools_to_call_from) or []
            if allowed_agent_names is not None:
                schemas = [s for s in schemas if s["function"]["name"] != "switch_agent"]
                schemas.append(build_switch_agent_schema(allowed_agent_names))
            schema_map = {s["function"]["name"]: s for s in schemas}
            try:
                parsed = parse_qwen_protocol(content, tool_schemas=schema_map)
            except TextToolProtocolError as exc:
                # Temporary display data, consumed before the error is retained
                # in an ActionStep. Never capture rejected XML as model memory.
                exc.raw_call_debug = {"text": content, "source": "model_text"}
                raise
            if provider_tool_calls:
                if isinstance(parsed, ToolCallSequence):
                    raise QwenToolProtocolError("ambiguous_dual_encoding", "Provider calls and native text calls cannot coexist.")
                try:
                    calls = tuple(self._parse_raw_tool_calls(provider_tool_calls) or ())
                except TextToolProtocolError as exc:
                    detail = str(exc).split("Correction:", 1)[0]
                    raise QwenToolProtocolError(exc.code, detail, block_index=exc.block_index,
                                                duplicate_key=exc.duplicate_key) from exc
                seen_ids = set()
                for index, call in enumerate(calls):
                    if call.id in seen_ids:
                        raise QwenToolProtocolError("duplicate_native_call_id",
                            f"tool_calls[{index}].id: duplicate call ID {call.id!r}.")
                    seen_ids.add(call.id)
                validate_qwen_tool_calls([TextToolCall(name=c.function.name, arguments=c.function.arguments)
                                         for c in calls], tool_schemas=schema_map)
            elif isinstance(parsed, ToolCallSequence):
                calls = tuple(ChatMessageToolCall(id=f"call_{uuid4().hex}", type="function",
                    function=ChatMessageToolCallFunction(name=c.name, arguments=c.arguments)) for c in parsed.calls)
            else:
                return AssistantControlResolution(source="natural")
            switches = [c for c in calls if c.function.name == "switch_agent"]
            if switches:
                if len(calls) != 1:
                    raise QwenToolProtocolError("mixed_native_switch", "switch_agent cannot be mixed with other calls.")
                call = switches[0]
                target = call.function.arguments["target_agent"]
                return AssistantControlResolution(source="native", control_call=call,
                    switch_agent=SwitchAgentCommand(target_agent=target, raw_payload=content))
            return AssistantControlResolution(source="native", tool_calls=calls)

        reject_legacy_opener(content)
        tools = tuple(tools_to_call_from or ())
        if provider_tool_calls:
            if (
                starts_new_control_opener(content)
                or has_malformed_control_attempt(content)
            ):
                raise TextToolProtocolError(
                    "ambiguous_dual_encoding",
                    (
                        "Provider-native calls and local text control cannot "
                        "coexist."
                    ),
                )
            calls = tuple(self._parse_raw_tool_calls(provider_tool_calls) or ())
            if allowed_agent_names is not None:
                switch_calls = [
                    call
                    for call in calls
                    if call.function.name == "switch_agent"
                ]
                if switch_calls:
                    if len(calls) != 1:
                        raise TextToolProtocolError(
                            "mixed_native_switch",
                            (
                                "A provider-native switch_agent call cannot be "
                                "mixed with other calls."
                            ),
                        )
                    target = self._normalize_native_switch_target(
                        switch_calls[0].function.arguments
                    )
                    allowed = ", ".join(sorted(allowed_agent_names)) or "<none>"
                    if target is None:
                        raise TextToolProtocolError(
                            "invalid_native_switch",
                            (
                                "Provider-native switch_agent has no single valid "
                                f"target; allowed targets: {allowed}."
                            ),
                        )
                    try:
                        raw_payload = format_switch_agent(target)
                    except TextToolProtocolError as exc:
                        raise TextToolProtocolError(
                            "invalid_native_switch",
                            (
                                f"Provider-native switch_agent target {target!r} "
                                f"is invalid; allowed targets: {allowed}."
                            ),
                        ) from exc
                    if target not in allowed_agent_names:
                        raise TextToolProtocolError(
                            "unsupported_agent",
                            (
                                f"Provider-native switch_agent target {target!r} "
                                f"is unsupported; allowed targets: {allowed}."
                            ),
                        )
                    return AssistantControlResolution(
                        source="native",
                        switch_agent=SwitchAgentCommand(
                            target_agent=target,
                            raw_payload=raw_payload,
                        ),
                    )
            source: Literal["natural", "text", "native"] = "native"
            switch_agent = None
        else:
            allowed_tools = {
                tool.name
                for tool in tools
                if isinstance(getattr(tool, "name", None), str)
            }
            allowed_tools.add("final_answer")
            parsed = parse_text_protocol(
                content,
                allowed_tool_names=allowed_tools,
                allowed_agent_names=allowed_agent_names,
            )
            calls = (
                self._adapt_text_calls(parsed.calls)
                if isinstance(parsed, ToolCallSequence)
                else ()
            )
            switch_agent = (
                parsed if isinstance(parsed, SwitchAgentCommand) else None
            )
            source = "text" if calls or switch_agent is not None else "natural"

        visible_calls = tuple(
            call
            for call in calls
            if call.function.name != "final_answer"
        )
        self._validate_tool_call_batch(visible_calls, tools)
        return AssistantControlResolution(
            source=source,
            tool_calls=visible_calls,
            switch_agent=switch_agent,
        )
