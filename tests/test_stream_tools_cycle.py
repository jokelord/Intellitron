"""测试流式模式下的工具传递策略。

验证 generate_stream 在所有情况下都不传 tools 给 SGLang，
因为 Qwen3-VL 在 auto + stream 模式下传 tools 会导致
tool_call_parser 吞掉内容（AGENTS.md 陷阱 #3）。

工具调用使用带边界口令的文本协议，并在完整响应后解析。
"""

import threading
from unittest.mock import MagicMock, call

import pytest

from backend.agent.local_llm_model import LocalLLMModel
from smolagents.models import ChatMessage, MessageRole


def _make_mock_llm_service():
    """创建能正确处理 stream=True 和 stream=False 的 mock LLM service。

    stream=True → 返回包含一个 chunk 的迭代器（产生非空内容避免空流回退）
    stream=False → 返回带 .text 属性的对象
    """
    mock_llm_service = MagicMock()

    def chat_side_effect(**kwargs):
        if kwargs.get("stream"):
            # 返回一个有内容的 chunk，避免触发空流回退
            return iter([{"content": "hello", "reasoning": ""}])
        else:
            result = MagicMock()
            result.text = "fallback result"
            return result

    mock_llm_service.chat.side_effect = chat_side_effect
    return mock_llm_service


def test_generate_stream_never_passes_tools():
    """流式调用必须始终传 tools=None，即使消息中包含 TOOL_RESPONSE"""
    mock_llm_service = _make_mock_llm_service()
    model = LocalLLMModel(llm_service=mock_llm_service, model_id="test-model")

    # 消息中包含 TOOL_RESPONSE（模拟工具调用链中的第二步）
    messages = [
        ChatMessage(role=MessageRole.USER, content="test"),
        ChatMessage(role=MessageRole.ASSISTANT, content="Calling tool: test_tool"),
        ChatMessage(role=MessageRole.TOOL_RESPONSE, content="tool result"),
    ]

    # Mock tools
    mock_tool = MagicMock()
    mock_tool.name = "test_tool"
    mock_tool.description = "A test tool"
    mock_tool.inputs = {"arg": {"type": "string", "description": "test arg"}}
    mock_tool.output_type = "string"

    # Consume the generator
    for _ in model.generate_stream(messages, tools_to_call_from=[mock_tool]):
        pass

    # 验证 stream=True 调用的 tools 参数为 None
    stream_call = mock_llm_service.chat.call_args_list[0]
    assert stream_call.kwargs["stream"] is True
    assert stream_call.kwargs["tools"] is None, (
        "generate_stream 不应传递 tools 参数给 SGLang，"
        "否则 Qwen3-VL 的 tool_call_parser 会吞掉内容"
    )


def test_generate_stream_no_tools_without_tool_response():
    """即使消息中没有 TOOL_RESPONSE，也不传 tools"""
    mock_llm_service = _make_mock_llm_service()
    model = LocalLLMModel(llm_service=mock_llm_service, model_id="test-model")

    messages = [
        ChatMessage(role=MessageRole.USER, content="hello"),
    ]

    for _ in model.generate_stream(messages, tools_to_call_from=None):
        pass

    stream_call = mock_llm_service.chat.call_args_list[0]
    assert stream_call.kwargs["stream"] is True
    assert stream_call.kwargs["tools"] is None


def test_generate_stream_split_control_opener_is_hidden_but_raw_content_is_retained():
    mock_llm_service = MagicMock()
    mock_llm_service.chat.return_value = iter(
        [
            {"content": "<<TOOL_", "reasoning": None},
            {"content": "CALL>>\n", "reasoning": None},
            {"content": '{"name":"read_file","arguments":{"file_path":"/tmp/a"}}\n', "reasoning": None},
            {"content": "<<END_TOOL_CALL>>", "reasoning": None},
        ]
    )
    model = LocalLLMModel(llm_service=mock_llm_service, model_id="test-model")

    visible = list(
        model.generate_stream(
            [ChatMessage(role=MessageRole.USER, content="read it")],
            request_id="split-control",
        )
    )

    assert visible == []
    assert model.get_stream_state("split-control")["content"] == (
        '<<TOOL_CALL>>\n{"name":"read_file","arguments":{"file_path":"/tmp/a"}}\n'
        "<<END_TOOL_CALL>>"
    )


def test_generate_stream_merges_runtime_context_into_first_system_message():
    mock_llm_service = _make_mock_llm_service()
    model = LocalLLMModel(
        llm_service=mock_llm_service,
        model_id="test-model",
        runtime_context="Current local time: Wednesday, 2026-05-27 20:09",
    )

    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content="Base system prompt"),
        ChatMessage(role=MessageRole.USER, content="hello"),
    ]

    for _ in model.generate_stream(messages):
        pass

    stream_call = mock_llm_service.chat.call_args_list[0]
    injected_messages = stream_call.kwargs["messages"]

    assert injected_messages[0].role == MessageRole.SYSTEM
    assert "Base system prompt" in str(injected_messages[0].content)
    assert "Current local time: Wednesday, 2026-05-27 20:09" in str(
        injected_messages[0].content
    )
    assert sum(1 for message in injected_messages if message.role == MessageRole.SYSTEM) == 1


def test_generate_stream_does_not_insert_runtime_context_before_system_prompt():
    mock_llm_service = _make_mock_llm_service()
    model = LocalLLMModel(
        llm_service=mock_llm_service,
        model_id="test-model",
        runtime_context="Current local time: Wednesday, 2026-05-27 20:09",
    )

    messages = [
        ChatMessage(role=MessageRole.USER, content="hello"),
    ]

    for _ in model.generate_stream(messages):
        pass

    stream_call = mock_llm_service.chat.call_args_list[0]
    injected_messages = stream_call.kwargs["messages"]

    assert injected_messages[0].role == MessageRole.SYSTEM
    assert str(injected_messages[0].content) == "Current local time: Wednesday, 2026-05-27 20:09"


def test_generate_stream_aborted_stops_without_fallback():
    mock_llm_service = MagicMock()

    def chat_side_effect(**kwargs):
        if kwargs.get("stream"):
            return iter(
                [
                    {
                        "content": None,
                        "reasoning": None,
                        "aborted": True,
                    }
                ]
            )
        raise AssertionError("aborted stream must not trigger non-stream fallback")

    mock_llm_service.chat.side_effect = chat_side_effect
    model = LocalLLMModel(llm_service=mock_llm_service, model_id="test-model")

    messages = [ChatMessage(role=MessageRole.USER, content="hello")]

    chunks = list(model.generate_stream(messages))

    assert chunks == []
    assert mock_llm_service.chat.call_count == 1


def test_generate_stream_aborted_preserves_partial_content():
    mock_llm_service = MagicMock()

    def chat_side_effect(**kwargs):
        if kwargs.get("stream"):
            return iter(
                [
                    {"reasoning": "先解释背景。", "content": None},
                    {"reasoning": None, "content": "纳维-斯托克斯方程通常写作："},
                    {"content": None, "reasoning": None, "aborted": True},
                ]
            )
        raise AssertionError("aborted stream must not trigger non-stream fallback")

    mock_llm_service.chat.side_effect = chat_side_effect
    model = LocalLLMModel(llm_service=mock_llm_service, model_id="test-model")

    messages = [ChatMessage(role=MessageRole.USER, content="hello")]

    chunks = list(model.generate_stream(messages))

    assert [chunk.content for chunk in chunks] == [
        "先解释背景。",
        "纳维-斯托克斯方程通常写作：",
    ]
    assert model.get_stream_state() == {
        "reasoning": "先解释背景。",
        "content": "纳维-斯托克斯方程通常写作：",
    }
    assert mock_llm_service.chat.call_count == 1


def test_generate_stream_retains_provider_termination_diagnostics():
    mock_llm_service = MagicMock()
    mock_llm_service.chat.return_value = iter(
        [
            {"content": "partial transcript", "reasoning": None},
            {
                "content": None,
                "reasoning": None,
                "finish_reason": "length",
            },
            {
                "content": None,
                "reasoning": None,
                "stream_done": True,
                "stream_residual_chars": 0,
            },
        ]
    )
    model = LocalLLMModel(llm_service=mock_llm_service, model_id="test-model")

    chunks = list(model.generate_stream(
        [ChatMessage(role=MessageRole.USER, content="hello")],
        request_id="req-termination",
    ))

    assert [chunk.content for chunk in chunks] == ["partial transcript"]
    assert model.get_stream_diagnostics("req-termination") == {
        "finish_reason": "length",
        "stream_done": True,
        "stream_residual_chars": 0,
    }


def test_generate_stream_fallback_reuses_same_request_id():
    calls = []

    def chat_side_effect(**kwargs):
        calls.append(kwargs)
        if kwargs.get("stream"):
            return iter(())
        result = MagicMock()
        result.text = "fallback result"
        return result

    mock_llm_service = MagicMock()
    mock_llm_service.chat.side_effect = chat_side_effect
    model = LocalLLMModel(llm_service=mock_llm_service, model_id="test-model")

    messages = [ChatMessage(role=MessageRole.USER, content="hello")]

    chunks = list(model.generate_stream(messages, request_id="req-fallback-1"))

    assert len(calls) == 2
    assert calls[0]["request_id"] == "req-fallback-1"
    assert calls[1]["request_id"] == "req-fallback-1"
    assert chunks[0].content == "fallback result"


def test_generate_stream_empty_fallback_preserves_text_for_agent_validation():
    calls = []

    def chat_side_effect(**kwargs):
        calls.append(kwargs)
        if kwargs.get("stream"):
            return iter(())
        result = MagicMock()
        result.text = """<<TOOL_CALL>>
{
  "name": "read_file",
  "arguments": {
  "file_path": "/tmp/a"
}
}
<<END_TOOL_CALL>>"""
        result.tool_calls = None
        return result

    service = MagicMock()
    service.chat.side_effect = chat_side_effect
    model = LocalLLMModel(llm_service=service, model_id="test-model")

    read_file_tool = MagicMock()
    read_file_tool.name = "read_file"
    read_file_tool.description = "Read one file"
    read_file_tool.inputs = {
        "file_path": {"type": "string", "description": "Absolute file path"}
    }
    read_file_tool.output_type = "string"

    chunks = list(
        model.generate_stream(
            [ChatMessage(role=MessageRole.USER, content="hello")],
            tools_to_call_from=[read_file_tool],
            request_id="framed-fallback",
        )
    )

    assert chunks == []
    assert calls[0]["tools"] is None
    assert calls[1]["tools"] is calls[0]["tools"]
    assert model.get_stream_state("framed-fallback")["content"] == (
        "<<TOOL_CALL>>\n"
        "{\n"
        '  "name": "read_file",\n'
        '  "arguments": {\n'
        '  "file_path": "/tmp/a"\n'
        "}\n}\n"
        "<<END_TOOL_CALL>>"
    )


def test_generate_stream_keeps_per_request_state_isolated_under_concurrency():
    start_second_stream = threading.Event()
    allow_first_stream_to_finish = threading.Event()

    def chat_side_effect(**kwargs):
        request_id = kwargs.get("request_id")
        assert kwargs.get("stream") is True

        def _stream():
            if request_id == "req-1":
                yield {"content": "alpha-1", "reasoning": "think-1"}
                start_second_stream.set()
                allow_first_stream_to_finish.wait(timeout=1)
                yield {"content": "-done", "reasoning": ""}
                return
            if request_id == "req-2":
                start_second_stream.wait(timeout=1)
                yield {"content": "beta", "reasoning": "think-2"}
                return
            raise AssertionError(f"unexpected request_id: {request_id}")

        return _stream()

    mock_llm_service = MagicMock()
    mock_llm_service.chat.side_effect = chat_side_effect
    model = LocalLLMModel(llm_service=mock_llm_service, model_id="test-model")
    messages = [ChatMessage(role=MessageRole.USER, content="hello")]

    first_chunks: list[str] = []
    second_chunks: list[str] = []

    def _run_first_stream():
        for chunk in model.generate_stream(messages, request_id="req-1"):
            first_chunks.append(chunk.content or "")

    first_thread = threading.Thread(target=_run_first_stream)
    first_thread.start()

    assert start_second_stream.wait(timeout=1)
    second_chunks = [chunk.content or "" for chunk in model.generate_stream(messages, request_id="req-2")]

    second_state = model.get_stream_state("req-2")
    first_state_before_finish = model.get_stream_state("req-1")
    unknown_state = model.get_stream_state("req-missing")

    allow_first_stream_to_finish.set()
    first_thread.join(timeout=1)
    assert not first_thread.is_alive()

    first_state = model.get_stream_state("req-1")

    assert first_chunks == ["think-1", "alpha-1", "-done"]
    assert second_chunks == ["think-2", "beta"]
    assert second_state == {"content": "beta", "reasoning": "think-2"}
    assert unknown_state is None
    assert first_state_before_finish == {"content": "alpha-1", "reasoning": "think-1"}
    assert first_state == {"content": "alpha-1-done", "reasoning": "think-1"}

    model.clear_stream_state("req-2")
    assert model.get_stream_state("req-2") is None
    assert model.get_stream_state("req-1") == {
        "content": "alpha-1-done",
        "reasoning": "think-1",
    }


def test_generate_propagates_per_call_inference_overrides():
    result = MagicMock()
    result.text = "ok"
    result.usage = None
    result.tool_calls = None
    mock_llm_service = MagicMock()
    mock_llm_service.chat.return_value = result
    model = LocalLLMModel(llm_service=mock_llm_service, model_id="test-model")

    messages = [ChatMessage(role=MessageRole.USER, content="hello")]

    model.generate(
        messages,
        temperature=0.0,
        max_tokens=150,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        request_id="req-sync-override",
    )

    call_kwargs = mock_llm_service.chat.call_args.kwargs
    assert call_kwargs["temperature"] == 0.0
    assert call_kwargs["max_new_tokens"] == 150
    assert call_kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    assert call_kwargs["request_id"] == "req-sync-override"


def test_generate_retries_once_without_extra_body_when_backend_rejects_it(caplog):
    calls = []
    rejection = RuntimeError("400 Bad Request: extra_body is not supported")

    def chat_side_effect(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise rejection
        result = MagicMock()
        result.text = "ok"
        result.usage = None
        result.tool_calls = None
        return result

    mock_llm_service = MagicMock()
    mock_llm_service.chat.side_effect = chat_side_effect
    model = LocalLLMModel(llm_service=mock_llm_service, model_id="test-model")

    messages = [ChatMessage(role=MessageRole.USER, content="hello")]

    response = model.generate(
        messages,
        temperature=0.0,
        max_tokens=150,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        retry_without_extra_body=True,
    )

    assert response.content == "ok"
    assert len(calls) == 2
    assert calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "extra_body" not in calls[1]
    assert calls[1]["temperature"] == 0.0
    assert calls[1]["max_new_tokens"] == 150
    assert "extra_body" in caplog.text


def test_generate_does_not_retry_without_opt_in_when_backend_rejects_extra_body():
    rejection = RuntimeError("400 Bad Request: extra_body is not supported")
    mock_llm_service = MagicMock()
    mock_llm_service.chat.side_effect = rejection
    model = LocalLLMModel(llm_service=mock_llm_service, model_id="test-model")

    messages = [ChatMessage(role=MessageRole.USER, content="hello")]

    with pytest.raises(RuntimeError, match="extra_body is not supported"):
        model.generate(
            messages,
            temperature=0.0,
            max_tokens=150,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

    assert mock_llm_service.chat.call_count == 1


def test_generate_stream_retries_once_without_extra_body_when_backend_rejects_it(caplog):
    calls = []

    def chat_side_effect(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return iter(
                [
                    {
                        "content": "[流式生成错误] RuntimeError: 422 extra_body is not supported",
                        "reasoning": None,
                    }
                ]
            )
        return iter([{"content": "ok", "reasoning": None}])

    mock_llm_service = MagicMock()
    mock_llm_service.chat.side_effect = chat_side_effect
    model = LocalLLMModel(llm_service=mock_llm_service, model_id="test-model")

    messages = [ChatMessage(role=MessageRole.USER, content="hello")]

    chunks = list(
        model.generate_stream(
            messages,
            temperature=0.0,
            max_tokens=150,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            retry_without_extra_body=True,
        )
    )

    assert [chunk.content for chunk in chunks] == ["ok"]
    assert len(calls) == 2
    assert calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "extra_body" not in calls[1]
    assert calls[1]["temperature"] == 0.0
    assert calls[1]["max_new_tokens"] == 150
    assert "extra_body" in caplog.text


def test_generate_stream_retried_empty_stream_still_falls_back_to_non_stream(caplog):
    calls = []

    def chat_side_effect(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return iter(
                [
                    {
                        "content": "[流式生成错误] RuntimeError: 422 extra_body is not supported",
                        "reasoning": None,
                    }
                ]
            )
        if kwargs.get("stream"):
            return iter(())
        result = MagicMock()
        result.text = "fallback result"
        result.usage = None
        result.tool_calls = None
        return result

    mock_llm_service = MagicMock()
    mock_llm_service.chat.side_effect = chat_side_effect
    model = LocalLLMModel(llm_service=mock_llm_service, model_id="test-model")

    messages = [ChatMessage(role=MessageRole.USER, content="hello")]

    chunks = list(
        model.generate_stream(
            messages,
            temperature=0.0,
            max_tokens=150,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            retry_without_extra_body=True,
        )
    )

    assert [chunk.content for chunk in chunks] == ["fallback result"]
    assert len(calls) == 3
    assert calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "extra_body" not in calls[1]
    assert "extra_body" not in calls[2]
    assert calls[1]["stream"] is True
    assert calls[2]["stream"] is False
    assert "extra_body" in caplog.text
