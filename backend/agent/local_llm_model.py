"""
本地 LLM 模型适配器 (Local LLM Model)

继承 smolagents.Model，桥接现有 LLMService，
使 smolagents Agent 可直接调用本地推理后端（SGLang/vLLM），无需 HTTP 回环。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
from copy import copy
from typing import Any, Generator

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

    @staticmethod
    def _pre_encode_images(messages: list[ChatMessage]) -> list[ChatMessage]:
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
                isinstance(item, dict) and item.get("type") == "image"
                and isinstance(item.get("image"), _PILImage.Image)
                for item in content
            )
            if not has_pil:
                result.append(msg)
                continue

            new_content = []
            for item in content:
                if (isinstance(item, dict) and item.get("type") == "image"
                        and isinstance(item.get("image"), _PILImage.Image)):
                    pil_img = item["image"]

                    # 优先使用预编码字节（由 agent_runner 处理后一次性编码），
                    # 避免对已经是 JPEG 的像素再次 JPEG 编码导致的质量损失。
                    if hasattr(pil_img, '_pre_encoded_b64') and hasattr(pil_img, '_pre_encoded_mime'):
                        b64 = pil_img._pre_encoded_b64
                        mime = pil_img._pre_encoded_mime
                        logger.debug(
                            f"_pre_encode_images: 使用预编码字节, mime={mime}, "
                            f"base64_len={len(b64)}"
                        )
                    else:
                        # 回退：手动编码（非 agent 路径的 PIL 图片）
                        fmt = (getattr(pil_img, "format", None) or "JPEG").upper()
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
                    new_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    })
                else:
                    new_content.append(item)

            new_msg = copy(msg)
            new_msg.content = new_content
            result.append(new_msg)

        return result

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
                item for item in content
                if isinstance(item, dict) and item.get("type") == "image_url"
            ]
            others = [
                item for item in content
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
        # 先预编码 PIL 图片（保持原始格式 JPEG/PNG），再交给 smolagents
        # 处理。smolagents 的 encode_image_base64 总是转为 PNG，JPEG→PNG 会
        # 造成 5-6x 体积膨胀。预编码后 smolagents 看到已经是 image_url 类型，直接跳过。
        pre_encoded = self._pre_encode_images(list(messages))
        clean_messages = get_clean_message_list(
            pre_encoded,  # type: ignore[arg-type]
            role_conversions=tool_role_conversions,
            convert_images_to_image_urls=True,
        )
        # Qwen3-VL 要求图片 token 出现在文字之前，才能有效进行视觉锚定
        clean_messages = self._put_images_first(clean_messages)
        llm_messages = [
            Message(role=msg["role"], content=self._convert_content(msg["content"]))
            for msg in clean_messages
        ]

        # ── 诊断：确认系统提示中包含 VLM 指令 ──
        if llm_messages and llm_messages[0].role == "system":
            _sys_content = str(llm_messages[0].content) if llm_messages[0].content else ""
            _has_vlm = "视觉语言模型" in _sys_content or "VLM" in _sys_content
            logger.info(
                f"📋 系统提示: {len(_sys_content)} 字符, 包含VLM指令: {_has_vlm}"
            )
            if not _has_vlm:
                logger.warning("⚠️ 系统提示中未包含 VLM 身份指令！模型可能误判为无法看到图片")

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

        **不传 tools 给 SGLang**——避免 tool_call_parser 在流式模式下
        吞掉内容增量（Qwen3 系列已知问题，见 AGENTS.md 陷阱 #3）。

        策略：
        - reasoning_content 和 content 都作为 content delta 实时推送（思考框流式显示）
        - 同时将 content 部分单独追踪到 self._last_stream_content，供 _step_stream
          在流式结束后用于工具调用解析和 final_answer 提取（不含 reasoning）
        - 空流时回退到非流式生成
        """
        # 1. 转换消息格式（同 generate，先预编码保持 JPEG 格式）
        pre_encoded = self._pre_encode_images(list(messages))
        clean_messages = get_clean_message_list(
            pre_encoded,
            role_conversions=tool_role_conversions,
            convert_images_to_image_urls=True,
        )
        # Qwen3-VL 要求图片 token 出现在文字之前，才能有效进行视觉锚定
        clean_messages = self._put_images_first(clean_messages)
        llm_messages = [
            Message(role=msg["role"], content=self._convert_content(msg["content"]))
            for msg in clean_messages
        ]

        # ── 诊断：确认系统提示中包含 VLM 指令 ──
        if llm_messages and llm_messages[0].role == "system":
            _sys_content = str(llm_messages[0].content) if llm_messages[0].content else ""
            _has_vlm = "视觉语言模型" in _sys_content or "VLM" in _sys_content
            logger.info(
                f"📋 系统提示: {len(_sys_content)} 字符, 包含VLM指令: {_has_vlm}"
            )
            if not _has_vlm:
                logger.warning("⚠️ 系统提示中未包含 VLM 身份指令！模型可能误判为无法看到图片")

        # 2. 转换工具定义（仅用于空流回退的非流式调用，不传给流式调用）
        tools_json = None
        if tools_to_call_from:
            tools_json = [
                get_tool_json_schema(t)
                for t in tools_to_call_from
                if getattr(t, "name", None) != "final_answer"
            ]
            if not tools_json:
                tools_json = None

        # 3. 流式调用 —— 不传 tools，绕过 tool_call_parser
        stream = self.llm_service.chat(messages=llm_messages, stream=True, tools=None)

        has_content = False
        self._last_stream_content = ""  # 仅追踪 content 部分（不含 reasoning）
        self._last_stream_reasoning = ""  # 追踪 reasoning 部分，用于工具调用回退搜索

        # 重复检测参数
        _repeat_window = 200   # 固定窗口长度（字符），用于短片段检测
        _repeat_threshold = 3  # 同一片段出现次数阈值
        _repetition_detected = False

        for chunk in stream:
            reasoning = chunk.get("reasoning") or ""
            content = chunk.get("content") or ""

            # reasoning → 实时推送（思考框流式显示）
            if reasoning:
                has_content = True
                self._last_stream_reasoning += reasoning
                yield ChatMessageStreamDelta(content=reasoning)

            # content → 实时推送 + 单独追踪
            if content:
                has_content = True
                self._last_stream_content += content
                yield ChatMessageStreamDelta(content=content)

            # ── 重复检测 ──
            # 规则一：若当前处于未闭合的 JSON 块内（{ > }），跳过检测。
            #         这样模型在 JSON arguments 里写入合法的重复数据（如 "123\n" × N）
            #         不会被误判为重复循环而中断。
            _open_braces = (
                self._last_stream_content.count('{') - self._last_stream_content.count('}')
            )
            if _open_braces > 0:
                continue  # 在 JSON 块内，不检测，等待 JSON 完成

            combined = self._last_stream_reasoning + self._last_stream_content

            # 规则二：固定窗口滑动检测（短片段，≤200 字符）
            if len(combined) > _repeat_window * (_repeat_threshold + 1):
                window = combined[-_repeat_window:]
                preceding = combined[:-_repeat_window]
                count = preceding.count(window)
                if count >= _repeat_threshold:
                    logger.warning(
                        f"⚠️ generate_stream [窗口级重复]: '{window[:50]}...' "
                        f"出现 {count + 1} 次，终止流式"
                    )
                    _repetition_detected = True
                    break

            # 规则三：段落级检测（用 \n\n 分割）— 捕捉整段落在循环中反复出现
            # 固定窗口因窗口边界偏移无法稳定捕捉 ~130 字符的段落重复；
            # 段落级检测可在第 4 次重复时立即触发，节省 90%+ 的无效推理时间。
            if len(combined) > 200:
                blocks = [b.strip() for b in combined.split('\n\n') if b.strip()]
                if len(blocks) >= 4:
                    last_blocks = blocks[-4:]
                    if len(set(last_blocks)) <= 2:
                        logger.warning(
                            f"⚠️ generate_stream [段落级重复]: 最近4段仅 "
                            f"{len(set(last_blocks))} 种不同内容，终止流式"
                        )
                        _repetition_detected = True
                        break

        logger.info(
            f"generate_stream 完成: reasoning={len(self._last_stream_reasoning)} 字符, "
            f"content={len(self._last_stream_content)} 字符, "
            f"重复检测触发={_repetition_detected}"
        )
        if self._last_stream_content:
            preview = self._last_stream_content[-200:]
            logger.debug(f"generate_stream content 尾部: ...{preview}")

        # 4a. 重复检测回退 ——
        #     流式路径产生了乱码（段落/内容重复），清空脏数据后用非流式生成干净响应。
        #     不 yield 到 SSE（前端已看到流式乱码，让控制流静默修正）；
        #     SmartToolCallingAgent._step_stream 会从 _last_stream_content 里
        #     解析工具调用或 final_answer，无论前端推流了什么都不影响后端逻辑。
        #     tools=None：强制模型以文本格式输出工具调用，与流式路径解析方式一致。
        if _repetition_detected:
            logger.warning("generate_stream: 重复输出 → 非流式回退（不推流，静默修正）")
            self._last_stream_content = ""
            self._last_stream_reasoning = ""
            _fb_result = self.llm_service.chat(messages=llm_messages, stream=False, tools=None)
            _fb_text = _fb_result.text or ""
            self._last_stream_content = _fb_text
            logger.info(f"非流式回退完成: content={len(_fb_text)} 字符")
            return  # 不再 yield；后续由 _step_stream 从 _last_stream_content 解析

        # 4b. 空流回退（Qwen3-VL 在特殊条件下可能返回空增量）
        if not has_content:
            logger.warning("generate_stream: 流式响应为空，回退到非流式生成")
            result = self.llm_service.chat(messages=llm_messages, stream=False, tools=tools_json)
            text = result.text or ""
            if text:
                self._last_stream_content = text
                yield ChatMessageStreamDelta(content=text)

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
                        logger.info(f"🖼 _convert_content: pass-through image_url, mime={mime}, "
                                   f"base64_len={len(b64_part)}, approx_bytes={data_size}")
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
                        logger.info(f"🖼 _convert_content: PIL→base64, mode={img_val.mode}, "
                                   f"size={img_val.size}, fmt={fmt}, bytes={len(buf.getvalue())}")
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

    def _extract_balanced_json(self, text: str, start: int) -> str | None:
        """使用平衡括号计数从文本中提取完整的 JSON 对象。

        比 raw_decode 更鲁棒：能正确处理 JSON 字符串内包含原始换行符的情况
        （raw_decode 遇到原始换行会报错），因为这里只跟踪括号平衡，不解析 JSON 语法。
        """
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(text)):
            c = text[i]
            if escape_next:
                escape_next = False
                continue
            if c == '\\' and in_string:
                escape_next = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return None

    def _try_parse_json(self, json_str: str) -> Any:
        """尝试解析 JSON 字符串，包含对原始换行符的修复。

        模型有时在 JSON 字符串值内输出原始换行符（而非 \\n 转义序列），
        导致标准 json.loads 失败。此方法先尝试标准解析，失败后将
        JSON 字符串值内的原始换行替换为 \\n 后重试。
        """
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

        # 修复原始换行：将 JSON 字符串值内的换行替换为 \\n
        # 策略：逐字符扫描，在双引号之间（字符串值内部）将 \n 替换为 \\n
        fixed_chars = []
        in_str = False
        escape_next = False
        for c in json_str:
            if escape_next:
                fixed_chars.append(c)
                escape_next = False
                continue
            if c == '\\' and in_str:
                fixed_chars.append(c)
                escape_next = True
                continue
            if c == '"':
                in_str = not in_str
                fixed_chars.append(c)
                continue
            if in_str and c == '\n':
                fixed_chars.append('\\')
                fixed_chars.append('n')
                continue
            if in_str and c == '\r':
                fixed_chars.append('\\')
                fixed_chars.append('r')
                continue
            if in_str and c == '\t':
                fixed_chars.append('\\')
                fixed_chars.append('t')
                continue
            fixed_chars.append(c)

        fixed = ''.join(fixed_chars)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError as e:
            logger.debug(f"_try_parse_json: 修复后仍失败: {e}")
            return None

    def _parse_tool_calls_from_text(
        self, content: str
    ) -> list[ChatMessageToolCall] | None:
        """
        从文本内容中提取 JSON 格式的工具调用。
        支持以下格式：
        1. XML 标签: <tool_call>{"name": "tool_name", "arguments": {...}}</tool_call>
        2. 带动作前缀: 动作:\n{...}  或 Action:\n{...}
        3. 纯 JSON:  {"name": "tool_name", "arguments": {...}}
        """
        if not content:
            return None

        logger.debug(f"🔍 _parse_tool_calls_from_text: 输入 {len(content)} 字符")

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
                logger.info(f"🔍 格式1(tool_call标签) 匹配成功: {[c.function.name for c in calls]}")
                return calls

        # 格式2：带动作前缀的 JSON（如 "动作:\n{...}"、"Action:\n{...}"）
        action_json_match = re.search(
            r'(?:动作|Action)\s*[：:]\s*(\{)',
            content,
            re.IGNORECASE,
        )
        if action_json_match:
            json_start = action_json_match.start(1)
            # 使用平衡括号提取完整 JSON 对象（比 raw_decode 更鲁棒）
            extracted = self._extract_balanced_json(content, json_start)
            if extracted:
                parsed = self._try_parse_json(extracted)
                if isinstance(parsed, dict) and "name" in parsed:
                    logger.info(f"🔍 格式2(动作:) 匹配成功: {parsed['name']}")
                    return [
                        ChatMessageToolCall(
                            id="call_0",
                            type="function",
                            function=ChatMessageToolCallFunction(
                                name=parsed["name"],
                                arguments=parsed.get("arguments", {}),
                            ),
                        )
                    ]
                else:
                    logger.warning(f"🔍 格式2(动作:) 找到JSON但解析失败, 提取的字符串长度: {len(extracted)}")
            else:
                # 回退到 raw_decode
                json_str = content[json_start:].lstrip()
                try:
                    decoder = json.JSONDecoder()
                    parsed, _ = decoder.raw_decode(json_str)
                    if isinstance(parsed, dict) and "name" in parsed:
                        logger.info(f"🔍 格式2(动作:) raw_decode 匹配成功: {parsed['name']}")
                        return [
                            ChatMessageToolCall(
                                id="call_0",
                                type="function",
                                function=ChatMessageToolCallFunction(
                                    name=parsed["name"],
                                    arguments=parsed.get("arguments", {}),
                                ),
                            )
                        ]
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"🔍 格式2(动作:) raw_decode 也失败: {e}")
        else:
            # 检查是否包含 "动作" 或类似关键词但格式不匹配
            if '动作' in content or 'Action' in content.lower():
                logger.warning(f"🔍 检测到 '动作/Action' 关键词但正则未匹配，内容片段: "
                             f"...{content[max(0, content.find('动作')-20):content.find('动作')+50]}...")

        # 格式3：纯 JSON（以 { 或 [ 开头）
        stripped = content.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict) and "name" in parsed:
                    parsed = [parsed]
                elif not isinstance(parsed, list):
                    parsed = None

                if parsed:
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
                    if calls:
                        return calls
            except (json.JSONDecodeError, KeyError):
                pass

        # 格式4：行内 JSON（"name" 和 "arguments" 在正文任意位置出现）
        # 模型可能在大段文字之后直接输出 JSON，不使用 "动作:" 前缀
        if '"name"' in content and '"arguments"' in content:
            # 找到最后一个看起来像工具调用的 JSON 对象
            last_brace = content.rfind('{')
            while last_brace >= 0:
                extracted = self._extract_balanced_json(content, last_brace)
                if extracted:
                    parsed = self._try_parse_json(extracted)
                    if isinstance(parsed, dict) and "name" in parsed and "arguments" in parsed:
                        logger.info(f"🔍 格式4(行内JSON) 匹配成功: {parsed['name']}")
                        return [
                            ChatMessageToolCall(
                                id="call_0",
                                type="function",
                                function=ChatMessageToolCallFunction(
                                    name=parsed["name"],
                                    arguments=parsed.get("arguments", {}),
                                ),
                            )
                        ]
                last_brace = content.rfind('{', 0, last_brace)

        # 格式5：smolagents 历史记录格式 "Calling tools:\n[{...}]"（Python dict with single quotes）
        # 当模型在对话历史中见过此格式后，会直接将其作为文本输出，需要用 ast.literal_eval 解析
        if "Calling tools:" in content:
            try:
                import ast
                # 找到 [ 开头的列表部分
                bracket_pos = content.find("[", content.find("Calling tools:"))
                if bracket_pos != -1:
                    calls_list = ast.literal_eval(content[bracket_pos:].strip())
                    if isinstance(calls_list, list):
                        calls = []
                        for item in calls_list:
                            if isinstance(item, dict) and item.get("type") == "function":
                                func = item.get("function", {})
                                name = func.get("name")
                                args = func.get("arguments", {})
                                if name:
                                    if isinstance(args, str):
                                        try:
                                            args = json.loads(args)
                                        except json.JSONDecodeError:
                                            pass
                                    calls.append(
                                        ChatMessageToolCall(
                                            id=item.get("id", f"call_{len(calls)}"),
                                            type="function",
                                            function=ChatMessageToolCallFunction(
                                                name=name,
                                                arguments=args if isinstance(args, dict) else {"raw": str(args)},
                                            ),
                                        )
                                    )
                        if calls:
                            logger.debug(f"从 'Calling tools:' 格式中解析出 {len(calls)} 个工具调用")
                            return calls
            except Exception:
                pass

        return None
