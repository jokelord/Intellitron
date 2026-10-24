"""Native canonical memory preserves calls, original observations, and prose."""
from types import SimpleNamespace
from unittest.mock import MagicMock
from smolagents.memory import AgentMemory, ActionStep, ToolCall
from smolagents.monitoring import Timing
from backend.agent.agent_runner import SmartToolCallingAgent, _chat_messages_to_backend_messages


def agent_with_step():
    agent = SmartToolCallingAgent.__new__(SmartToolCallingAgent)
    agent.memory = AgentMemory(system_prompt="system")
    agent._agent_session = SimpleNamespace(tool_protocol="qwen35-native", session_id="native")
    agent.logger = MagicMock()
    step = ActionStep(step_number=1, timing=Timing(start_time=0., end_time=.1),
        model_output="Let me read.", tool_calls=[ToolCall(name="read_file", arguments={"file_path":"a"}, id="a")],
        observations="  def f():\n    pass\n")
    agent.memory.steps.append(step)
    return agent, step


def test_native_single_call_has_matching_tool_role():
    agent, step = agent_with_step()
    messages = agent.write_memory_to_messages()
    assert [m.role for m in messages] == ["system", "assistant", "tool"]
    assert messages[1].content == "Let me read."
    assert messages[1].tool_calls[0]["id"] == "a"
    assert messages[2].tool_call_id == "a"
    assert messages[2].content == "  def f():\n    pass\n"


def test_native_compact_adapter_preserves_metadata():
    agent, step = agent_with_step()
    messages = _chat_messages_to_backend_messages(agent.write_memory_to_messages())
    assert messages[1].tool_calls[0]["id"] == "a"
    assert messages[2].name == "read_file"
    assert messages[2].content == step.observations


def test_native_parse_error_without_call_is_user_feedback():
    agent, step = agent_with_step()
    step.tool_calls = None
    step.observations = None
    step.error = ValueError("bad format")
    messages = agent.write_memory_to_messages()
    assert messages[-1].role == "user"
    assert messages[-1].tool_call_id is None


def test_real_single_tool_result_keeps_original_whitespace():
    from smolagents.tools import Tool
    from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction
    from backend.agent.local_llm_model import LocalLLMModel
    class Whitespace(Tool):
        name = "whitespace"
        description = "Return code whitespace."
        inputs = {}
        output_type = "string"
        def forward(self):
            return "  def f():\n    pass\n\n"
    session = SimpleNamespace(tool_protocol="qwen35-native", session_id="native")
    model = LocalLLMModel(llm_service=MagicMock(), session=session)
    agent = SmartToolCallingAgent(tools=[Whitespace()], model=model, tool_protocol="qwen35-native")
    agent._agent_session = session
    step = ActionStep(step_number=1, timing=Timing(start_time=0., end_time=.1))
    message = ChatMessage(role="assistant", content="", tool_calls=[ChatMessageToolCall(id="w1", type="function",
        function=ChatMessageToolCallFunction(name="whitespace", arguments={}))])
    list(agent.process_tool_calls(message, step))
    agent.memory.steps.append(step)
    result = [m for m in agent.write_memory_to_messages() if m.role == "tool"]
    assert result[0].content == "  def f():\n    pass\n\n"
