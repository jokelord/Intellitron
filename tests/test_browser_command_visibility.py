from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.agent.agent_runner import AgentRunner, SmartToolCallingAgent, _get_tools
from backend.agent.tool_configuration import AgentToolConfigurationRequest
from backend.config import get_config


def _make_agent():
    tools = _get_tools()
    agent = SmartToolCallingAgent.__new__(SmartToolCallingAgent)
    agent.tools = {tool.name: tool for tool in tools}
    agent.managed_agents = {}
    agent.prompt_templates = {
        "system_prompt": (
            "你拥有的工具汇总如下：\n"
            "{%- for tool in prompt_visible_tools.values() %}\n"
            "- {{ tool.name }}\n"
            "{%- endfor %}"
        )
    }
    agent.instructions = None
    return agent


def test_browser_command_is_not_registered_or_prompt_visible():
    runner = AgentRunner(MagicMock(), get_config())
    manager = runner._build_profile_agent("manager_agent")
    basic = runner._build_profile_agent("basic_agent")

    manager_prompt = manager.initialize_system_prompt()
    basic_prompt = basic.initialize_system_prompt()
    assert "browser_command" not in manager_prompt
    assert "playwright-cli" not in manager_prompt
    assert "browser_command" not in {tool.name for tool in manager.tools_and_managed_agents}
    assert "browser_command" not in basic_prompt
    assert "playwright-cli" not in basic_prompt
    assert "browser_command" not in {tool.name for tool in basic.tools_and_managed_agents}
    assert "browser_command" not in runner.tool_catalog.available_tool_names


def test_playwright_skill_is_pure_exec_command_skill():
    skill_path = Path(__file__).parents[1] / "backend" / "skills" / "playwright-browser" / "SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    assert "browser_command" not in content
    assert "exec_command" in content
    assert "scripts/playwright-browser" in content
    assert "--probe-runtime" in content
    assert "chromium=" in content


def test_wikipedia_composite_is_default_off_but_explicit_snapshot_selection_is_visible():
    runner = AgentRunner(MagicMock(), get_config())
    default_basic = runner._build_profile_agent("basic_agent")
    assert "wikipedia_search_and_fetch" not in default_basic.initialize_system_prompt()
    assert "wikipedia_search_and_fetch" not in {
        tool.name for tool in default_basic.tools_and_managed_agents
    }

    config = deepcopy(get_config())
    config.web_tools.enable_wikipedia = True
    enabled_runner = AgentRunner(MagicMock(), config)
    candidate = AgentToolConfigurationRequest(
        schema_version=1,
        catalog_revision=enabled_runner.tool_catalog.catalog_revision,
        manager_agent_tools=[],
        basic_agent_tools=["wikipedia_search_and_fetch"],
    )
    snapshot = enabled_runner.tool_catalog.resolve_candidate(candidate)
    selected_basic = enabled_runner._build_profile_agent("basic_agent", tool_snapshot=snapshot)
    assert "wikipedia_search_and_fetch" in selected_basic.initialize_system_prompt()
    assert "wikipedia_search_and_fetch" in {
        tool.name for tool in selected_basic.tools_and_managed_agents
    }


def test_split_wikipedia_tools_remain_special_and_hidden():
    agent = _make_agent()
    agent.tools.update({
        name: SimpleNamespace(name=name, to_tool_calling_prompt=lambda n=name: n)
        for name in ("wikipedia_search", "wikipedia_fetch", "wikipedia_summary")
    })
    prompt = agent.initialize_system_prompt()
    schema_tool_names = [tool.name for tool in agent.tools_and_managed_agents]
    prompt_lines = {line.strip() for line in prompt.splitlines()}
    for name in ("wikipedia_search", "wikipedia_fetch", "wikipedia_summary"):
        assert f"- {name}" not in prompt_lines
        assert name not in schema_tool_names


def test_read_skill_stays_prompt_visible_for_progressive_disclosure():
    agent = _make_agent()

    prompt = agent.initialize_system_prompt()

    assert "read_skill" in prompt
    assert "list_skills" in prompt


def test_python_interpreter_not_visible_in_prompt_or_schema():
    agent = _make_agent()

    prompt = agent.initialize_system_prompt()
    schema_tool_names = [tool.name for tool in agent.tools_and_managed_agents]

    assert "python_interpreter" not in prompt
    assert "python_interpreter" not in schema_tool_names


def test_prompt_renders_configured_agent_python_path():
    tools = _get_tools()
    agent = SmartToolCallingAgent.__new__(SmartToolCallingAgent)
    agent.tools = {tool.name: tool for tool in tools}
    agent.managed_agents = {}
    agent.prompt_templates = {
        "system_prompt": "Python={{ agent_python_executable }}\nBin={{ agent_python_bin_dir }}"
    }
    agent.instructions = None
    agent._agent_python_executable = "/opt/custom-agent/bin/python"

    prompt = agent.initialize_system_prompt()

    assert "Python=/opt/custom-agent/bin/python" in prompt
    assert "Bin=/opt/custom-agent/bin" in prompt


def test_default_prompt_guides_exact_calculation_to_simple_exec_command():
    runner = AgentRunner(MagicMock(), get_config())
    agent = runner._build_profile_agent("basic_agent")
    prompt = agent.initialize_system_prompt()
    assert "exec_command" in prompt
    assert "受限 shell" in prompt
    assert "命令必须是必要且可审计的" in prompt
