"""Tests for multi-agent config parsing and prompt migration."""

import json
import re
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from backend.config import AgentConfig, AgentPromptsConfig, ConfigLoader, WebUIConfig
from backend.agent.text_tool_protocol import parse_text_protocol
from backend.agent.qwen_tool_protocol import parse_qwen_protocol
from smolagents.models import get_tool_json_schema
from unittest.mock import MagicMock


PROMPTS_PATH = Path(__file__).resolve().parents[1] / "config" / "config-agent-prompts.yaml"


def _active_prompt_text() -> dict[str, str]:
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for nested in value.values():
                yield from strings(nested)

    data = yaml.safe_load(PROMPTS_PATH.read_text(encoding="utf-8"))
    active = {}
    for profile in ("coordinator_1", "coordinator_2", "manager_agent", "basic_agent"):
        active[profile] = data["native_tool_protocol_prompt"] + "\n" + "\n".join(strings(data[profile]))
    return active


def test_active_agent_prompts_use_only_native_control_protocol():
    active = _active_prompt_text()
    for prompt in active.values():
        assert "<tool_call>" in prompt and "</tool_call>" in prompt
        assert "<<TOOL_CALL>>" not in prompt
        assert "<<END_TOOL_CALL>>" not in prompt
        assert "<<SWITCH_AGENT>>" not in prompt
        assert "Calling tools:" not in prompt
        assert "调用工具:" not in prompt
    for profile in ("coordinator_1", "coordinator_2"):
        assert "<function=switch_agent>" in active[profile]
        assert "<parameter=target_agent>" in active[profile]


def test_active_examples_parse_against_actual_dispatch_and_switch_schemas():
    from backend.agent.agent_runner import AgentRunner, AgentSession
    cfg = ConfigLoader().load(reload=True)
    cfg.inference.tool_protocol = "qwen35-native"
    cfg.agent.enabled_agent_profiles = ["coordinator_1", "coordinator_2", "manager_agent", "basic_agent"]
    service = MagicMock(); service.config = cfg
    runner = AgentRunner(service, cfg)
    parsed_names = []
    for profile in ("coordinator_1", "coordinator_2", "manager_agent"):
        session = AgentSession(session_id="example", agent=MagicMock(), mode="tool_calling", tool_protocol="qwen35-native")
        agent = runner._build_profile_agent(profile, session=session)
        session.agent = agent
        schemas = [tool if isinstance(tool, dict) else get_tool_json_schema(tool)
                   for tool in agent.tools_and_managed_agents]
        schemas = {schema["function"]["name"]: schema for schema in schemas}
        text = agent.system_prompt
        if profile == "coordinator_1":
            text += "\n" + runner._build_coordinator_turn_message("route", profile, session=session)
        frames = re.findall(r"(?ms)^<tool_call>\n.*?^</tool_call>$", text)
        examples = [frame for frame in frames if re.search(r"<function=(switch_agent|dispatching_parallel_agent)>", frame)]
        assert examples, profile
        for frame in examples:
            parsed = parse_qwen_protocol(frame, tool_schemas=schemas)
            assert len(parsed.calls) == 1
            parsed_names.append(parsed.calls[0].name)
            if parsed.calls[0].name == "dispatching_parallel_agent":
                assert set(parsed.calls[0].arguments) == {"child_id", "task"}
            else:
                assert set(parsed.calls[0].arguments) == {"target_agent"}
    assert parsed_names.count("switch_agent") == 2
    assert parsed_names.count("dispatching_parallel_agent") == 1


def test_frozen_legacy_examples_remain_valid_and_isolated():
    resource = json.loads((PROMPTS_PATH.parent / "compatibility/legacy-agent-prompts.json").read_text())
    text = "\n".join(resource["profiles"][name]["system_prompt"] for name in ("manager_agent", "basic_agent"))
    frames = re.findall(r"(?ms)^<<TOOL_CALL>>\n.*?^<<END_TOOL_CALL>>$", text)
    assert len(frames) >= 4
    calls = [call for frame in frames for call in parse_text_protocol(frame).calls]
    assert len(calls) >= 6
    assert all(isinstance(call.arguments, dict) for call in calls)
    assert "<<SWITCH_AGENT>> basic_agent" in resource["profiles"]["coordinator_1"]["routing_turn_prompt"]


def test_active_prompts_have_no_bare_function_argument_json_examples():
    for prompt in _active_prompt_text().values():
        assert not re.search(r'(?m)^\s*"name"\s*:', prompt)
        assert not re.search(r'(?m)^\s*"arguments"\s*:', prompt)


def test_active_tool_prompts_explain_native_control_and_parameter_encoding():
    for profile, prompt in _active_prompt_text().items():
        assert "每个函数调用输出一个完整的块" in prompt, profile
        assert "多个完整块" in prompt
        assert "string 参数正文保留实际文本、空白与换行" in prompt
        assert "非 string 参数使用严格 JSON" in prompt
        assert "true/false/null" in prompt
        assert "不放 Markdown 代码围栏" in prompt
        assert "不要自行生成结果" in prompt
        assert "自然语言回答" in prompt


def _parse_web_ui(content=None):
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {"webUI": {"content": content}} if content is not None else {}
    return loader._parse_web_ui_config()


def test_web_ui_content_limit_defaults_and_preserves_override():
    assert _parse_web_ui().content.bubble_character_limit == 20_000
    assert _parse_web_ui({"bubble_character_limit": 31_337}).content.bubble_character_limit == 31_337


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "20000", None])
def test_web_ui_content_limit_rejects_non_positive_or_non_integer(value):
    with pytest.raises(ValueError, match="bubble_character_limit"):
        _parse_web_ui({"bubble_character_limit": value})


def test_web_ui_config_constructor_validates_content_limit():
    with pytest.raises(ValueError, match="bubble_character_limit"):
        WebUIConfig(content={"bubble_character_limit": True})


class TestParseAgentConfig:
    def _make_loader_with_data(self, agent_data: dict) -> ConfigLoader:
        loader = ConfigLoader.__new__(ConfigLoader)
        loader._data = {"agent": agent_data}
        loader._agent_prompts_data = {}
        return loader

    def test_parse_multi_agent_runtime_fields(self):
        loader = self._make_loader_with_data(
            {
                "entry_agent_name": "coordinator_1",
                "enabled_agent_profiles": ["coordinator_1", "basic_agent"],
                "max_agent_switches_per_turn": 2,
            }
        )

        cfg = loader._parse_agent_config()

        assert isinstance(cfg, AgentConfig)
        assert cfg.entry_agent_name == "coordinator_1"
        assert cfg.enabled_agent_profiles == ["coordinator_1", "basic_agent"]
        assert cfg.max_agent_switches_per_turn == 2

    def test_parse_multi_agent_runtime_defaults(self):
        loader = self._make_loader_with_data({})

        cfg = loader._parse_agent_config()

        assert cfg.entry_agent_name == "manager_agent"
        assert cfg.enabled_agent_profiles == [
            "coordinator_1",
            "coordinator_2",
            "manager_agent",
            "basic_agent",
        ]
        assert cfg.max_agent_switches_per_turn == 2

    @pytest.mark.parametrize("invalid_tool", ["unknown_tool", "final_answer"])
    def test_parse_agent_defaults_reject_non_selectable_business_tool(self, invalid_tool):
        loader = self._make_loader_with_data(
            {
                "default_profile_tools": {
                    "manager_agent": [invalid_tool],
                    "basic_agent": [],
                }
            }
        )

        with pytest.raises(ValueError, match="default_profile_tools.manager_agent"):
            loader._parse_agent_config()

    def test_parse_agent_defaults_allows_optional_wikipedia_composite(self):
        loader = self._make_loader_with_data(
            {
                "default_profile_tools": {
                    "manager_agent": ["wikipedia_search_and_fetch"],
                    "basic_agent": [],
                }
            }
        )

        cfg = loader._parse_agent_config()
        assert cfg.default_profile_tools["manager_agent"] == (
            "wikipedia_search_and_fetch",
        )

    def test_parse_agent_config_exposes_manager_agent_defaults(self):
        loader = self._make_loader_with_data({})

        cfg = loader._parse_agent_config()

        assert cfg.entry_agent_name == "manager_agent"
        assert cfg.enabled_agent_profiles == [
            "coordinator_1",
            "coordinator_2",
            "manager_agent",
            "basic_agent",
        ]
        assert cfg.manager_agent_max_parallel_children == 4
        assert not hasattr(cfg, "manager_agent_max_child_agents_per_dispatch")
        assert not hasattr(cfg, "manager_agent_default_max_parallel_children")
        assert cfg.manager_agent_child_event_buffer_limit == 5000
        assert cfg.manager_agent_child_timeout_seconds == 2400
        assert cfg.session_disposal_timeout_seconds == 30.0

    def test_parse_agent_config_preserves_session_disposal_timeout_override(self):
        loader = self._make_loader_with_data(
            {"session_disposal_timeout_seconds": 12.5}
        )

        cfg = loader._parse_agent_config()

        assert cfg.session_disposal_timeout_seconds == 12.5

    @pytest.mark.parametrize(
        "value",
        [0, -1, float("nan"), float("inf"), float("-inf"), True, "invalid"],
        ids=[
            "zero",
            "negative",
            "nan",
            "positive-infinity",
            "negative-infinity",
            "boolean",
            "non-numeric",
        ],
    )
    def test_parse_agent_config_rejects_invalid_session_disposal_timeout(self, value):
        loader = self._make_loader_with_data(
            {"session_disposal_timeout_seconds": value}
        )

        with pytest.raises(
            ValueError, match="agent.session_disposal_timeout_seconds"
        ):
            loader._parse_agent_config()

    def test_parse_agent_config_preserves_manager_agent_overrides(self):
        loader = self._make_loader_with_data(
            {
                "entry_agent_name": "manager_agent",
                "enabled_agent_profiles": ["manager_agent", "basic_agent"],
                "manager_agent_max_parallel_children": 2,
                "manager_agent_child_event_buffer_limit": 123,
                "manager_agent_child_timeout_seconds": 9,
            }
        )

        cfg = loader._parse_agent_config()

        assert cfg.entry_agent_name == "manager_agent"
        assert cfg.enabled_agent_profiles == ["manager_agent", "basic_agent"]
        assert cfg.manager_agent_max_parallel_children == 2
        assert cfg.manager_agent_child_event_buffer_limit == 123
        assert cfg.manager_agent_child_timeout_seconds == 9

    def test_parse_agent_config_rejects_non_positive_manager_parallel_limit(self):
        loader = self._make_loader_with_data(
            {"manager_agent_max_parallel_children": 0}
        )

        with pytest.raises(ValueError, match="manager_agent_max_parallel_children"):
            loader._parse_agent_config()

    def test_parse_multi_agent_runtime_preserves_zero_switch_limit(self):
        loader = self._make_loader_with_data(
            {
                "entry_agent_name": "coordinator_1",
                "enabled_agent_profiles": ["coordinator_1", "basic_agent"],
                "max_agent_switches_per_turn": 0,
            }
        )

        cfg = loader._parse_agent_config()

        assert cfg.max_agent_switches_per_turn == 0

    def test_parse_agent_config_exposes_routing_only_overrides_and_timeout(self):
        loader = self._make_loader_with_data(
            {
                "coordinator_1_timeout_seconds": 0,
                "coordinator_1_routing_temperature": 0.0,
                "coordinator_1_routing_max_tokens": 150,
                "ripgrep_timeout_seconds": 7,
                "coordinator_1_routing_extra_body": {
                    "chat_template_kwargs": {"enable_thinking": False}
                },
            }
        )

        cfg = loader._parse_agent_config()

        assert cfg.coordinator_1_timeout_seconds == 0
        assert cfg.coordinator_1_routing_temperature == 0.0
        assert cfg.coordinator_1_routing_max_tokens == 150
        assert cfg.ripgrep_timeout_seconds == 7
        assert cfg.coordinator_1_routing_extra_body == {
            "chat_template_kwargs": {"enable_thinking": False}
        }

    def test_parse_multi_agent_runtime_rejects_empty_enabled_profiles(self):
        loader = self._make_loader_with_data(
            {
                "entry_agent_name": "coordinator_agent",
                "enabled_agent_profiles": [],
            }
        )

        with pytest.raises(ValueError, match="enabled_agent_profiles"):
            loader._parse_agent_config()

    def test_parse_multi_agent_runtime_rejects_missing_entry_agent_profile(self):
        loader = self._make_loader_with_data(
            {
                "entry_agent_name": "coordinator_agent",
                "enabled_agent_profiles": ["basic_agent"],
            }
        )

        with pytest.raises(
            ValueError,
            match="entry_agent_name 'coordinator_agent' must be included",
        ):
            loader._parse_agent_config()

    def test_parse_multi_agent_runtime_allows_basic_agent_entry_profile(self):
        loader = self._make_loader_with_data(
            {
                "entry_agent_name": "basic_agent",
                "enabled_agent_profiles": ["coordinator_1", "basic_agent"],
            }
        )

        cfg = loader._parse_agent_config()

        assert cfg.entry_agent_name == "basic_agent"
        assert cfg.enabled_agent_profiles == ["coordinator_1", "basic_agent"]

class TestParseAgentPromptsConfig:
    def _make_loader_with_prompt_data(self, prompt_data: dict) -> ConfigLoader:
        loader = ConfigLoader.__new__(ConfigLoader)
        loader._data = {}
        loader._agent_prompts_data = prompt_data
        return loader

    def test_parse_multi_agent_prompt_profiles(self):
        loader = self._make_loader_with_prompt_data(
            {
                "coordinator_1": {"instructions": "route fast", "prompt_templates": {}},
                "coordinator_2": {"instructions": "reason deeply", "prompt_templates": {}},
                "manager_agent": {"instructions": "manage", "prompt_templates": {}},
                "basic_agent": {"instructions": "basic", "prompt_templates": {}},
                "explorer_agent": {"instructions": "placeholder", "prompt_templates": {}},
                "auditor_agent": {"instructions": "placeholder", "prompt_templates": {}},
                "vlm_instructions": "vision note",
            }
        )

        cfg = loader._parse_agent_prompts_config()

        assert isinstance(cfg, AgentPromptsConfig)
        assert cfg.coordinator_1["instructions"] == "route fast"
        assert cfg.coordinator_2["instructions"] == "reason deeply"
        assert cfg.manager_agent["instructions"] == "manage"
        assert cfg.basic_agent["instructions"] == "basic"
        assert cfg.explorer_agent["instructions"] == "placeholder"
        assert cfg.auditor_agent["instructions"] == "placeholder"
        assert cfg.vlm_instructions == "vision note"

    def test_parse_multi_agent_prompt_profiles_includes_manager_agent(self):
        loader = self._make_loader_with_prompt_data(
            {
                "manager_agent": {"instructions": "manage", "prompt_templates": {}},
                "basic_agent": {"instructions": "basic", "prompt_templates": {}},
            }
        )

        cfg = loader._parse_agent_prompts_config()

        assert cfg.manager_agent["instructions"] == "manage"

    def test_parse_multi_agent_prompt_defaults(self):
        loader = self._make_loader_with_prompt_data({})

        cfg = loader._parse_agent_prompts_config()

        assert cfg.coordinator_1 == {}
        assert cfg.coordinator_2 == {}
        assert cfg.manager_agent == {}
        assert cfg.basic_agent == {}
        assert cfg.explorer_agent == {}
        assert cfg.auditor_agent == {}
        assert cfg.vlm_instructions is None

    def test_parse_multi_agent_prompt_compatibility_bridge_uses_basic_agent(self):
        basic_agent = {
            "instructions": "basic instructions",
            "prompt_templates": {"system_prompt": "basic system prompt"},
        }
        loader = self._make_loader_with_prompt_data(
            {
                "basic_agent": basic_agent,
            }
        )

        cfg = loader._parse_agent_prompts_config()

        assert cfg.agent_instructions == "basic instructions"
        assert cfg.toolcalling_agent == {"system_prompt": "basic system prompt"}


class TestParseMiscConfig:
    def test_parse_runtime_prompts_config_exposes_runtime_time_context_flag(self):
        loader = ConfigLoader.__new__(ConfigLoader)
        loader._data = {"runtime_prompts": {"enable_runtime_time_context": True}}
        loader._agent_prompts_data = {}

        cfg = loader._parse_runtime_prompts_config()

        assert cfg.enable_runtime_time_context is True

    def test_parse_misc_config_keeps_compatibility_with_runtime_prompts(self):
        loader = ConfigLoader.__new__(ConfigLoader)
        loader._data = {"runtime_prompts": {"enable_runtime_time_context": True}}
        loader._agent_prompts_data = {}

        cfg = loader._parse_misc_config()

        assert cfg.enable_runtime_time_context is True

    def test_parse_multi_agent_prompt_preserves_unknown_future_profile_for_getattr(self):
        loader = TestParseAgentPromptsConfig()._make_loader_with_prompt_data(
            {
                "basic_agent": {"instructions": "basic", "prompt_templates": {}},
                "future_router": {
                    "instructions": "future profile",
                    "prompt_templates": {"system_prompt": "future system prompt"},
                },
            }
        )

        cfg = loader._parse_agent_prompts_config()

        assert getattr(cfg, "future_router", None) == {
            "instructions": "future profile",
            "prompt_templates": {"system_prompt": "future system prompt"},
        }

    def test_parse_multi_agent_prompt_unknown_profiles_survive_deepcopy(self):
        loader = TestParseAgentPromptsConfig()._make_loader_with_prompt_data(
            {
                "future_router": {
                    "instructions": "future profile",
                    "prompt_templates": {"system_prompt": "future system prompt"},
                }
            }
        )

        copied = deepcopy(loader._parse_agent_prompts_config())

        assert getattr(copied, "future_router", None) == {
            "instructions": "future profile",
            "prompt_templates": {"system_prompt": "future system prompt"},
        }


def test_real_config_files_use_multi_agent_prompt_layout():
    cfg = ConfigLoader().load(reload=True)

    assert cfg.agent.entry_agent_name in cfg.agent.enabled_agent_profiles
    assert cfg.agent.entry_agent_name in cfg.agent.enabled_agent_profiles
    assert cfg.agent.enabled_agent_profiles == [
        "coordinator_1",
        "coordinator_2",
        "manager_agent",
        "basic_agent",
    ]
    assert cfg.agent.manager_agent_max_parallel_children == 4
    assert cfg.agent.manager_agent_child_event_buffer_limit == 5000
    assert cfg.agent.max_agent_switches_per_turn >= 0
    assert cfg.agent.coordinator_1_timeout_seconds == 20
    assert cfg.agent.coordinator_1_routing_temperature == 0.0
    assert cfg.agent.coordinator_1_routing_max_tokens == 150
    assert cfg.agent.coordinator_1_routing_extra_body == {
        "chat_template_kwargs": {"enable_thinking": False}
    }

    assert cfg.agent_prompts.coordinator_1
    assert cfg.agent_prompts.coordinator_2
    assert cfg.agent_prompts.manager_agent
    assert cfg.agent_prompts.basic_agent
    assert cfg.agent_prompts.explorer_agent
    assert cfg.agent_prompts.auditor_agent
    assert cfg.agent_prompts.vlm_instructions

    coordinator_1_routing_prompt = cfg.agent_prompts.coordinator_1["prompt_templates"][
        "routing_turn_prompt"
    ]
    assert "<function=switch_agent>" in coordinator_1_routing_prompt
    assert "allowed_switch_targets" in coordinator_1_routing_prompt

    coordinator_2_system_prompt = cfg.agent_prompts.coordinator_2["prompt_templates"][
        "system_prompt"
    ]
    assert "<function=switch_agent>" in coordinator_2_system_prompt
    assert "Calling tools:" not in coordinator_2_system_prompt

    basic_templates = cfg.agent_prompts.basic_agent["prompt_templates"]
    assert "system_prompt" in basic_templates
    assert "final_answer" in basic_templates

    assert cfg.agent_prompts.agent_instructions == cfg.agent_prompts.basic_agent["instructions"]
    assert cfg.agent_prompts.toolcalling_agent == basic_templates


def test_real_manager_agent_prompt_contains_parallel_dispatch_constraints():
    cfg = ConfigLoader().load(reload=True)
    manager_agent_prompt = cfg.agent_prompts.manager_agent
    prompt_text = "\n".join(
        [
            manager_agent_prompt.get("instructions", ""),
            manager_agent_prompt.get("prompt_templates", {}).get("system_prompt", ""),
        ]
    ).lower()

    assert "manager" in prompt_text
    assert "read-only" in prompt_text
    assert "dispatching_parallel_agent" in prompt_text
    assert "brand-new context window" in prompt_text
    assert "has not seen this conversation" in prompt_text
    assert "never dispatch another manager" in prompt_text
    assert "nested management is forbidden" in prompt_text
    assert "child_id" in prompt_text
    assert "future turn" in prompt_text
    assert "run concurrently" in prompt_text
    assert "blackboard observation" in prompt_text
    assert (
        "synthesize reports" in prompt_text
        or "synthesize child reports" in prompt_text
    )


def test_coordinator_1_routing_choices_follow_exposed_schema_enum():
    cfg = ConfigLoader().load(reload=True)
    routing_prompt = cfg.agent_prompts.coordinator_1["prompt_templates"]["routing_turn_prompt"]
    assert "switch_agent" in routing_prompt
    assert "target_agent enum" in routing_prompt
    assert "allowed_switch_targets" in routing_prompt
    assert "<<SWITCH_AGENT>>" not in routing_prompt
