"""测试流式模式下的工具传递策略。

验证 generate_stream 在所有情况下都不传 tools 给 SGLang，
因为 Qwen3-VL 在 auto + stream 模式下传 tools 会导致
tool_call_parser 吞掉内容（AGENTS.md 陷阱 #3）。

工具调用改为从文本中解析（_parse_tool_calls_from_text）。
"""

from unittest.mock import MagicMock, call

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
