from types import SimpleNamespace

from backend.agent.agent_runner import SmartToolCallingAgent
from smolagents.models import ChatMessageToolCall, ChatMessageToolCallFunction


def _make_tool_call(name: str, arguments: dict):
    return ChatMessageToolCall(
        id="call_0",
        type="function",
        function=ChatMessageToolCallFunction(name=name, arguments=arguments),
    )


def test_unregistered_playwright_cli_tool_name_is_not_invented_as_a_tool():
    agent = SmartToolCallingAgent.__new__(SmartToolCallingAgent)
    tool_call = _make_tool_call(
        "playwright-cli", {"command": "playwright-cli click e58"}
    )

    normalized = agent._normalize_tool_call(tool_call)

    assert normalized.function.name == "playwright-cli"
    assert normalized.function.arguments == {"command": "playwright-cli click e58"}


def test_exec_command_playwright_cli_text_is_not_rewritten_to_a_hidden_tool():
    agent = SmartToolCallingAgent.__new__(SmartToolCallingAgent)
    tool_call = _make_tool_call(
        "exec_command", {"command": "playwright-cli snapshot", "timeout": 10}
    )

    normalized = agent._normalize_tool_call(tool_call)

    assert normalized.function.name == "exec_command"
    assert normalized.function.arguments == {"command": "playwright-cli snapshot", "timeout": 10}


def test_non_playwright_tool_call_kept_unchanged():
    agent = SmartToolCallingAgent.__new__(SmartToolCallingAgent)
    tool_call = _make_tool_call("read_file", {"file_path": "/tmp/a.txt"})

    normalized = agent._normalize_tool_call(tool_call)

    assert normalized.function.name == "read_file"
    assert normalized.function.arguments == {"file_path": "/tmp/a.txt"}
