"""Test that _get_tools() no longer includes load_images."""

from unittest.mock import MagicMock

from backend.agent.agent_runner import _get_tools


class TestGetToolsExclusion:
    def test_load_images_not_in_tools(self):
        config = MagicMock()
        config.agent.python_authorized_imports = []
        config.agent.enable_wikipedia = False
        config.agent.mcp_servers = None

        tools = _get_tools(config)
        tool_names = [t.name for t in tools]
        assert "load_images" not in tool_names

    def test_standard_tools_still_present(self):
        config = MagicMock()
        config.agent.python_authorized_imports = []
        config.agent.enable_wikipedia = False
        config.agent.mcp_servers = None

        tools = _get_tools(config)
        tool_names = [t.name for t in tools]
        assert "read_file" in tool_names
        assert "exec_command" in tool_names
        assert "list_skills" in tool_names
        assert "read_skill" in tool_names
