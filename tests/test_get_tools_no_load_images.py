"""Test tool exposure and profile-aware agent construction."""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from backend.agent.agent_runner import AgentRunner, _get_tools, EMPTY_PROMPT_TEMPLATES
from backend.config import AppConfig, get_config
from backend.agent.text_tool_protocol import parse_text_protocol, SwitchAgentCommand


class TestGetToolsExclusion:
    def test_load_images_not_in_tools(self):
        config = MagicMock()
        tools = _get_tools()
        tool_names = [t.name for t in tools]
        assert "load_images" not in tool_names
        assert "python_interpreter" not in tool_names

    def test_standard_tools_still_present(self):
        tools = _get_tools()
        tool_names = [t.name for t in tools]
        assert "read_file" in tool_names
        assert "exec_command" in tool_names
        assert "glob" in tool_names
        assert "grep" in tool_names
        assert "browser_command" not in tool_names
        assert "list_skills" in tool_names
        assert "read_skill" in tool_names

    def test_read_file_exposes_document_options_but_hides_abort(self):
        tool = next(t for t in _get_tools(enable_wikipedia=False) if t.name == "read_file")

        assert "password" in tool.inputs
        assert "document_mode" in tool.inputs
        assert tool.inputs["document_mode"]["enum"] == ["auto", "distorted"]
        assert "recognize_seals" in tool.inputs
        assert "check_abort" not in tool.inputs

    def test_only_composite_wikipedia_tool_is_exposed(self):
        tools = _get_tools(enable_wikipedia=True)
        tool_names = [t.name for t in tools]

        assert "wikipedia_search_and_fetch" in tool_names
        assert "wikipedia_search" not in tool_names
        assert "wikipedia_summary" not in tool_names


def _make_runner(config: AppConfig | None = None) -> AgentRunner:
    config = deepcopy(config or get_config())
    config.inference.tool_protocol = "legacy_text"
    return AgentRunner(MagicMock(), config)


class TestProfileAwareAgentConstruction:
    def test_build_agent_preserves_basic_agent_compatibility_path(self):
        config = deepcopy(get_config())
        config.agent.entry_agent_name = "coordinator_1"
        config.agent.enabled_agent_profiles = ["coordinator_1", "basic_agent"]
        config.model.model_type = "text"
        runner = _make_runner(config)

        agent = runner._build_agent("chat")
        tool_names = set(agent.tools)

        assert agent.instructions == runner.config.agent_prompts.basic_agent[
            "instructions"
        ]
        expected_templates = deepcopy(EMPTY_PROMPT_TEMPLATES)
        expected_templates.update(runner.config.agent_prompts.legacy_prompt_compatibility["profiles"]["basic_agent"])
        assert agent.prompt_templates == expected_templates
        assert "read_file" in tool_names
        assert "exec_command" in tool_names

    def test_build_agent_preserves_legacy_vision_vlm_instructions(self):
        config = deepcopy(get_config())
        config.model.model_type = "vision"
        runner = _make_runner(config)

        agent = runner._build_agent("chat")

        assert runner.config.agent_prompts.vlm_instructions in agent.instructions

    def test_build_profile_agent_hides_all_tools_for_coordinator(self):
        config = deepcopy(get_config())
        config.agent.enabled_agent_profiles = ["coordinator_1", "basic_agent"]
        runner = _make_runner(config)

        agent = runner._build_profile_agent("coordinator_1")

        assert agent.tools_and_managed_agents == []

    def test_build_profile_agent_hides_all_tools_for_coordinator_1(self):
        config = deepcopy(get_config())
        config.agent.enabled_agent_profiles = [
            "coordinator_1",
            "coordinator_2",
            "basic_agent",
        ]
        runner = _make_runner(config)

        agent = runner._build_profile_agent("coordinator_1")

        assert agent.tools_and_managed_agents == []

    def test_build_profile_agent_hides_direct_tools_for_coordinator_2(self):
        config = deepcopy(get_config())
        config.agent.enabled_agent_profiles = [
            "coordinator_1",
            "coordinator_2",
            "basic_agent",
        ]
        runner = _make_runner(config)

        agent = runner._build_profile_agent("coordinator_2")

        assert set(agent.tools) == {"final_answer"}
        assert [tool.name for tool in agent.tools_and_managed_agents] == ["basic_agent"]

    def test_build_profile_agent_coordinator_2_prompt_exposes_only_basic_agent_handoff(self):
        config = deepcopy(get_config())
        config.agent.enabled_agent_profiles = [
            "coordinator_1",
            "coordinator_2",
            "basic_agent",
        ]
        runner = _make_runner(config)

        agent = runner._build_profile_agent("coordinator_2")
        prompt = agent.initialize_system_prompt()

        assert "read_file:" not in prompt
        assert "exec_command:" not in prompt
        assert "basic_agent" in prompt
        assert "<<SWITCH_AGENT>> basic_agent" in prompt

    def test_build_profile_agent_coordinator_prompt_does_not_leak_tool_names(self):
        config = deepcopy(get_config())
        config.agent.enabled_agent_profiles = ["coordinator_1", "basic_agent"]
        runner = _make_runner(config)

        agent = runner._build_profile_agent("coordinator_1")
        prompt = agent.initialize_system_prompt()

        assert "read_file:" not in prompt
        assert "exec_command:" not in prompt

    def test_build_profile_agent_keeps_standard_tools_for_basic_agent(self):
        runner = _make_runner()

        agent = runner._build_profile_agent("basic_agent")
        tool_names = set(agent.tools)

        assert "read_file" in tool_names
        assert "exec_command" in tool_names
        assert "glob" in tool_names
        assert "grep" in tool_names
        assert "browser_command" not in tool_names

    def test_build_profile_agent_uses_profile_specific_prompt_bundle(self):
        config = deepcopy(get_config())
        config.model.model_type = "text"
        config.agent.enabled_agent_profiles = ["coordinator_1", "basic_agent"]
        runner = _make_runner(config)

        coordinator = runner._build_profile_agent("coordinator_1")
        basic = runner._build_profile_agent("basic_agent")

        assert coordinator.instructions == runner.config.agent_prompts.coordinator_1[
            "instructions"
        ]
        assert (
            coordinator.prompt_templates["system_prompt"]
            == runner.config.agent_prompts.legacy_prompt_compatibility["profiles"]["coordinator_1"]["system_prompt"]
        )
        assert basic.instructions == runner.config.agent_prompts.basic_agent[
            "instructions"
        ]
        expected_templates = deepcopy(EMPTY_PROMPT_TEMPLATES)
        expected_templates.update(runner.config.agent_prompts.legacy_prompt_compatibility["profiles"]["basic_agent"])
        assert basic.prompt_templates == expected_templates

    def test_coordinator_prompt_bundle_includes_routing_turn_prompt(self):
        config = deepcopy(get_config())
        config.agent.enabled_agent_profiles = ["coordinator_1", "basic_agent"]
        runner = _make_runner(config)

        coordinator_templates = runner.config.agent_prompts.legacy_prompt_compatibility["profiles"]["coordinator_1"]

        assert "routing_turn_prompt" in coordinator_templates
        assert "少于100字" in coordinator_templates["routing_turn_prompt"]
        assert "<<SWITCH_AGENT>> basic_agent" in coordinator_templates["routing_turn_prompt"]

    def test_legacy_coordinator_routing_prompt_keeps_task_placeholder_and_strict_switch_examples(self):
        config = deepcopy(get_config())
        config.agent.enabled_agent_profiles = ["coordinator_1", "basic_agent"]
        runner = _make_runner(config)

        prompt = runner.config.agent_prompts.legacy_prompt_compatibility["profiles"]["coordinator_1"]["routing_turn_prompt"]

        assert "{{raw_task}}" in prompt
        examples = [line for line in prompt.splitlines() if line.startswith("<<SWITCH_AGENT>>")]
        assert examples == ["<<SWITCH_AGENT>> coordinator_2", "<<SWITCH_AGENT>> basic_agent"]
        for example, target in zip(examples, ["coordinator_2", "basic_agent"]):
            parsed = parse_text_protocol(example, allowed_agent_names={"coordinator_2", "basic_agent"})
            assert isinstance(parsed, SwitchAgentCommand)
            assert parsed.target_agent == target
        assert "<tool_call>" not in prompt

    def test_build_profile_agent_applies_vlm_instructions_only_to_coordinator(self):
        config = deepcopy(get_config())
        config.model.model_type = "vision"
        config.agent.enabled_agent_profiles = ["coordinator_1", "basic_agent"]
        runner = _make_runner(config)

        coordinator = runner._build_profile_agent("coordinator_1")
        basic = runner._build_profile_agent("basic_agent")

        vlm_instructions = runner.config.agent_prompts.vlm_instructions
        assert vlm_instructions in coordinator.instructions
        assert vlm_instructions not in basic.instructions

    def test_build_profile_agent_applies_vlm_instructions_to_coordinator_1(self):
        config = deepcopy(get_config())
        config.model.model_type = "vision"
        config.agent.enabled_agent_profiles = [
            "coordinator_agent",
            "coordinator_1",
            "basic_agent",
        ]
        runner = _make_runner(config)

        coordinator = runner._build_profile_agent("coordinator_1")
        basic = runner._build_profile_agent("basic_agent")

        vlm_instructions = runner.config.agent_prompts.vlm_instructions
        assert vlm_instructions in coordinator.instructions
        assert vlm_instructions not in basic.instructions

    def test_build_profile_agent_rejects_disabled_profiles(self):
        config = deepcopy(get_config())
        config.agent.enabled_agent_profiles = ["coordinator_1", "basic_agent"]
        runner = _make_runner(config)

        with pytest.raises(ValueError, match="explorer_agent"):
            runner._build_profile_agent("explorer_agent")

    def test_build_profile_agent_gives_coordinator_1_its_own_minimal_system_prompt(self):
        config = deepcopy(get_config())
        config.agent.enabled_agent_profiles = [
            "coordinator_1",
            "basic_agent",
        ]
        runner = _make_runner(config)

        agent = runner._build_profile_agent("coordinator_1")

        assert agent.prompt_templates["system_prompt"] == (
            runner.config.agent_prompts.legacy_prompt_compatibility["profiles"]["coordinator_1"]["system_prompt"]
        )
        assert "【工具使用的概念及基本流程】" not in agent.prompt_templates[
            "system_prompt"
        ]
