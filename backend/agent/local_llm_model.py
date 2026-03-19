"""
本地 LLM 模型适配器 (Local LLM Model)

继承 smolagents.Model，桥接现有 LLMService，
使 smolagents Agent 可直接调用本地推理后端（SGLang/vLLM），无需 HTTP 回环。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from smolagents.models import (
    ChatMessage,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    MessageRole,
    Model,
    get_clean_message_list,
    get_tool_json_schema,
    tool_role_conversions,
)
from smolagents.monitoring import TokenUsage

from backend.llm_service import GenerationResult, LLMService, Message

logger = logging.getLogger(__name__)


class LocalLLMModel(Model):
    """
    将本地 LLMService 包装为 smolagents Model。

    直接调用 LLMService.chat()（同步），绕过 HTTP 回环。
    支持 ToolCallingAgent（结构化 tool_calls）和 CodeAgent（纯文本代码块）。
    """

    def __init__(self, llm_service: LLMService, model_id: str | None = None, **kwargs):
        super().__init__(model_id=model_id or "local-llm", **kwargs)
        self.llm_service = llm_service

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
        # 使用 convert_images_to_image_urls=True，让 smolagents 将
        # {"type": "image", "image": <PIL.Image>} 转换为
        # {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
        # 若不传此参数（默认 False），smolagents 只将 PIL 转为裸 base64 字符串，
        # 导致后续 _convert_content 调用 .save() 失败，图片无声地被丢弃。
        clean_messages = get_clean_message_list(
            list(messages),  # type: ignore[arg-type]
            role_conversions=tool_role_conversions,
            convert_images_to_image_urls=True,
        )
        llm_messages = [
            Message(role=msg["role"], content=self._convert_content(msg["content"]))
            for msg in clean_messages
        ]

        # 2. 转换工具定义
        # 过滤掉 final_answer：不让模型看到这个工具，
        # 模型完成任务后会返回纯文本，由 SmartToolCallingAgent 自动包装为 final_answer
        tools_json = None
        if tools_to_call_from:
            tools_json = [
                get_tool_json_schema(t)
                for t in tools_to_call_from
                if getattr(t, "name", None) != "final_answer"
            ]
            if not tools_json:
                tools_json = None

        # 3. 调用 LLMService
        chat_result = self.llm_service.chat(
            messages=llm_messages,
            stream=False,
            tools=tools_json,
        )
        result: GenerationResult = chat_result  # type: ignore[assignment]

        # 4. 解析响应
        content = result.text or ""
        tool_calls = None
        token_usage = None

        # 解析 token 使用量
        if result.usage:
            token_usage = TokenUsage(
                input_tokens=result.usage.get("prompt_tokens", 0),
                output_tokens=result.usage.get("completion_tokens", 0),
            )

        # 尝试解析 tool_calls（模型可能以结构化格式返回工具调用）
        if tools_to_call_from:
            tool_calls = self._extract_tool_calls(content, result)

        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content=content,
            tool_calls=tool_calls,
            token_usage=token_usage,
        )

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
            if isinstance(item, dict) and item.get("type") == "image":
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
                    elif isinstance(img_val, str):
                        # (b) 裸 base64 字符串（smolagents 默认转换结果）：
                        #     直接复用，格式未知时默认 png（与 smolagents 一致）
                        b64 = img_val
                        mime = "image/png"
                    elif isinstance(img_val, bytes):
                        b64 = base64.b64encode(img_val).decode()
                        mime = "image/png"
                    else:
                        logger.warning(f"未知图片类型 {type(img_val)}，跳过")
                        continue
                    converted.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    })
                except Exception as e:
                    logger.warning(f"图片内容转换失败，跳过: {e}")
            else:
                converted.append(item)
        return converted

    def _extract_tool_calls(
        self, content: str, result: GenerationResult
    ) -> list[ChatMessageToolCall] | None:
        """
        从 LLM 响应中提取 tool_calls。

        优先从 GenerationResult 的结构化 tool_calls 字段提取（SGLang tool_call_parser 解析结果），
        回退到从文本内容中解析。

        注意：final_answer 不过滤，交由 SmartToolCallingAgent._step_stream 拦截处理。
        """
        # 优先使用 SGLang 结构化 tool_calls
        raw_tool_calls = getattr(result, "tool_calls", None)
        if raw_tool_calls:
            parsed = self._parse_raw_tool_calls(raw_tool_calls)
            if parsed:
                return parsed

        # 回退：从文本内容中解析工具调用
        return self._parse_tool_calls_from_text(content)

    def _parse_raw_tool_calls(
        self, raw_tool_calls: list[dict]
    ) -> list[ChatMessageToolCall] | None:
        """将原始 tool_calls 字典列表转换为 smolagents 格式"""
        calls: list[ChatMessageToolCall] = []
        for tc in raw_tool_calls:
            func = tc.get("function", {})
            arguments = func.get("arguments", "{}")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"raw": arguments}

            calls.append(
                ChatMessageToolCall(
                    id=tc.get("id", f"call_{len(calls)}"),
                    type="function",
                    function=ChatMessageToolCallFunction(
                        name=func.get("name", "unknown"),
                        arguments=arguments,
                    ),
                )
            )
        return calls or None

    def _parse_tool_calls_from_text(
        self, content: str
    ) -> list[ChatMessageToolCall] | None:
        """
        从文本内容中提取 JSON 格式的工具调用。
        支持以下格式：
        1. XML 标签: <tool_call>{"name": "tool_name", "arguments": {...}}</tool_call>
        2. 纯 JSON:  {"name": "tool_name", "arguments": {...}}
        """
        if not content:
            return None

        # 格式1：<tool_call> XML 标签格式（Qwen 等模型常用）
        xml_matches = re.findall(r'<tool_call>\s*(.*?)\s*</tool_call>', content, re.DOTALL)
        if xml_matches:
            calls: list[ChatMessageToolCall] = []
            for match in xml_matches:
                try:
                    parsed = json.loads(match.strip())
                    if isinstance(parsed, dict) and "name" in parsed:
                        calls.append(
                            ChatMessageToolCall(
                                id=f"call_{len(calls)}",
                                type="function",
                                function=ChatMessageToolCallFunction(
                                    name=parsed["name"],
                                    arguments=parsed.get("arguments", {}),
                                ),
                            )
                        )
                except (json.JSONDecodeError, KeyError):
                    continue
            if calls:
                return calls

        # 格式2：带文本前缀的 JSON（如 "动作:\n{...}"、"Action:\n{...}"）
        # 尝试提取内容中第一个完整的 JSON 对象
        json_match = re.search(r'\{\s*"name"\s*:', content)
        if json_match:
            json_str = content[json_match.start():]
            try:
                parsed = json.loads(json_str)
                if isinstance(parsed, dict) and "name" in parsed:
                    return [
                        ChatMessageToolCall(
                            id=f"call_0",
                            type="function",
                            function=ChatMessageToolCallFunction(
                                name=parsed["name"],
                                arguments=parsed.get("arguments", {}),
                            ),
                        )
                    ]
            except json.JSONDecodeError:
                pass

        # 格式3：纯 JSON（以 { 或 [ 开头）
        stripped = content.strip()
        if not (stripped.startswith("{") or stripped.startswith("[")):
            return None

        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict) and "name" in parsed:
                parsed = [parsed]
            elif not isinstance(parsed, list):
                return None

            calls = []
            for item in parsed:
                if isinstance(item, dict) and "name" in item:
                    calls.append(
                        ChatMessageToolCall(
                            id=item.get("id", f"call_{len(calls)}"),
                            type="function",
                            function=ChatMessageToolCallFunction(
                                name=item["name"],
                                arguments=item.get("arguments", {}),
                            ),
                        )
                    )
            return calls or None
        except (json.JSONDecodeError, KeyError):
            return None
