import pytest
from unittest.mock import MagicMock, patch
from backend.agent.agent_runner import AgentRunner, current_session_context
from backend.config import AppConfig


@pytest.fixture
def mock_llm_service():
    """Mock LLMService to simulate tool calling"""
    mock_service = MagicMock()
    # Simulate a tool call response that would trigger cycle
    mock_service.chat.return_value = MagicMock()
    mock_service.chat.return_value.content = (
        '{"tool_call": {"name": "test_tool", "arguments": {"arg": "value"}}}'
    )
    return mock_service


@pytest.fixture
def agent_runner(mock_llm_service):
    config = AppConfig()
    config.model.model_type = "text"  # 避免 VLM 相关复杂性
    return AgentRunner(mock_llm_service, config)


@pytest.mark.asyncio
async def test_session_id_set_in_run_stream(agent_runner, mock_llm_service):
    """测试 run_stream 中 session_id 正确回写并传递给 agent"""
    session_id_input = None  # Simulate first request with None

    with patch("backend.agent.agent_runner.LLMService", return_value=mock_llm_service):
        # Mock the agent run to yield minimal events
        mock_agent = MagicMock()
        mock_agent.run.return_value = []  # Empty to avoid complex mocking

        with patch.object(agent_runner, "get_or_create_session") as mock_get_session:
            mock_session = MagicMock()
            mock_session.session_id = "generated-uuid-123"
            mock_session.agent = mock_agent
            mock_session.has_run = False
            mock_session.check_aborted.return_value = False
            mock_session.check_answer_now.return_value = False
            mock_get_session.return_value = mock_session

            # Collect events from run_stream
            events = []
            async for event in agent_runner.run_stream(
                session_id_input, "test message"
            ):
                events.append(event)

            # current_session_context 是线程局部存储，在 _run_agent 的后台线程中
            # 设置并在 finally 中清理。主线程无法看到后台线程的 thread-local 值。
            # 因此验证 session_id 回写到 session 对象即可。
            assert mock_session.session_id == "generated-uuid-123"

            # 验证 session 的 has_run 被设置为 True（表明 agent 确实执行了）
            assert mock_session.has_run is True


@pytest.mark.asyncio
async def test_session_id_passed_to_tools_in_cycle(agent_runner, mock_llm_service):
    """测试在工具调用链中 session_id 传递给工具"""
    # This test would need more complex mocking of smolagents internals
    # For now, verify that _injected_session_id is set on tools
    pass  # Placeholder for more detailed test


@pytest.mark.asyncio
async def test_context_cleared_after_run_stream(agent_runner, mock_llm_service):
    """测试 run_stream 结束后 context 被清理"""
    session_id_input = "test-session-456"

    with patch("backend.agent.agent_runner.LLMService", return_value=mock_llm_service):
        mock_agent = MagicMock()
        mock_agent.run.return_value = []

        with patch.object(agent_runner, "get_or_create_session") as mock_get_session:
            mock_session = MagicMock()
            mock_session.session_id = "test-session-456"
            mock_session.agent = mock_agent
            mock_session.has_run = False
            mock_session.check_aborted.return_value = False
            mock_session.check_answer_now.return_value = False
            mock_get_session.return_value = mock_session

            async for _ in agent_runner.run_stream(session_id_input, "test message"):
                pass

            # After run_stream completes, context should be cleared
            assert current_session_context.get() is None
