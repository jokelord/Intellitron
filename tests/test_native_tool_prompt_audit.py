"""Audit actual model-facing schemas for every selectable repository tool."""
from copy import deepcopy
import json
import re
from types import SimpleNamespace

import pytest

from backend.agent.agent_runner import AgentRunner, AgentSession, _get_tools
from backend.agent.local_llm_model import LocalLLMModel
from backend.agent.tool_configuration import AgentToolConfigurationRequest
from backend.config import get_config
from backend.llm_service import GenerationResult, Message


# Literal examples independently specify the public arguments, including all
# optional parameters. No tool is executed: the LLM transport is the only sink.
EXAMPLES = {
    "read_file": {"file_path": "/tmp/a", "start_line": 1, "end_line": 4,
                  "encoding": "utf-8", "password": "", "document_mode": "auto",
                  "recognize_seals": False},
    "write_file": {"file_path": "/tmp/a", "content": '  中文\nprint("x")\n'},
    "edit_file": {"file_path": "/tmp/a", "search_text": "", "replace_text": "\nnew\n"},
    "glob": {"pattern": "**/*.py", "path": "/tmp", "limit": 5},
    "grep": {"pattern": "tool_calls?", "path": "/tmp", "include": "*.py",
             "output_mode": "content", "case_insensitive": False, "multiline": True,
             "context_before": 1, "context_after": 2, "context": 3,
             "line_numbers": True, "head_limit": 5, "offset": 0},
    "exec_command": {"command": 'printf "x"', "timeout": 3.5},
    "check_process": {"name": "python"},
    "list_skills": {},
    "read_skill": {"skill_name": "example"},
    "web_search": {"query": "qwen", "max_results": 5, "timeout": 30},
    "web_fetch": {"url": "https://example.invalid", "timeout": 30},
    "search_and_fetch": {"query": "qwen", "max_results": 5},
    "wikipedia_search_and_fetch": {"query": "qwen", "max_results": 5},
}
INTERNAL_ARGS = {"check_abort", "use_fallback", "session_id", "runtime_config", "capability"}


@pytest.fixture(scope="module")
def all_tools():
    tools = {tool.name: tool for tool in _get_tools(enable_wikipedia=True)}
    assert set(tools) == set(EXAMPLES)
    return tools


def _native_call(name, args):
    parameters = []
    for key, value in args.items():
        body = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        parameters.append(f"<parameter={key}>\n{body}\n</parameter>")
    return f"<tool_call>\n<function={name}>\n" + "\n".join(parameters) + "\n</function>\n</tool_call>"


class CaptureService:
    config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=1280))

    def __init__(self, response):
        self.response = response
        self.request = None

    def chat(self, **kwargs):
        self.request = deepcopy(kwargs)
        return GenerationResult(text=self.response)


@pytest.mark.parametrize("name", EXAMPLES)
def test_all_tools_actual_native_request_accepts_documented_public_arguments(all_tools, name):
    service = CaptureService(_native_call(name, EXAMPLES[name]))
    model = LocalLLMModel(llm_service=service, session=SimpleNamespace(
        tool_protocol="qwen35-native", session_id="tool-audit", current_agent_name="basic_agent"))
    result = model([Message("user", "Perform the requested operation.")],
                   tools_to_call_from=[all_tools[name]])
    schema = service.request["tools"][0]["function"]
    assert schema["name"] == name
    properties = schema["parameters"]["properties"]
    assert set(properties) == set(EXAMPLES[name])
    assert not INTERNAL_ARGS.intersection(properties)
    assert all(prop.get("description") for prop in properties.values())
    assert result.tool_calls[0].function.name == name
    assert result.tool_calls[0].function.arguments == EXAMPLES[name]


@pytest.mark.parametrize("name", EXAMPLES)
def test_model_facing_parameter_guidance_uses_native_json_values(all_tools, name):
    service = CaptureService("Done.")
    model = LocalLLMModel(llm_service=service, session=SimpleNamespace(
        tool_protocol="qwen35-native", session_id="tool-audit", current_agent_name="basic_agent"))
    model([Message("user", "Answer directly.")], tools_to_call_from=[all_tools[name]])
    schema = service.request["tools"][0]["function"]
    assert not re.search(r"<<(?:END_)?TOOL_CALL>>|<<SWITCH_AGENT>>|Calling tools:",
                         json.dumps(schema, ensure_ascii=False))
    for key, prop in schema["parameters"]["properties"].items():
        # Python values in public parameter guidance invite invalid native
        # booleans or literal "None" strings instead of optional omission.
        assert not re.search(r"\b(?:True|False|None)\b", prop["description"]), (name, key)


def test_edit_file_shared_description_keeps_semantics_without_completion_examples(all_tools):
    service = CaptureService("Done.")
    model = LocalLLMModel(llm_service=service, session=SimpleNamespace(
        tool_protocol="qwen35-native", session_id="tool-audit", current_agent_name="basic_agent"))
    model([Message("user", "Answer directly.")], tools_to_call_from=[all_tools["edit_file"]])
    schema = service.request["tools"][0]["function"]
    description = schema["description"]
    assert re.search(r'\{\s*"file_path"\s*:', description) is None
    assert "XML" not in description and "<tool_call>" not in description
    assert "空字符串" in description and "不会自动补换行" in description
    assert schema["parameters"]["required"] == ["file_path", "search_text", "replace_text"]
    assert schema["parameters"]["properties"]["search_text"]["type"] == "string"
    assert "空字符串" in schema["parameters"]["properties"]["search_text"]["description"]


def test_web_search_omitted_result_count_preserves_documented_default(monkeypatch, all_tools):
    from backend.tools import web_tools

    captured = {}

    def capture_search(query, timeout, config, *, max_results):
        captured["max_results"] = max_results
        return []

    # Replace the network operation only: the real business implementation
    # still receives the generated session wrapper's omitted arguments.
    monkeypatch.setattr(web_tools, "_search_with_searxng", capture_search)
    all_tools["web_search"].forward(query="native schema audit")
    assert captured["max_results"] == 80


@pytest.mark.parametrize("profile", ["coordinator_1", "coordinator_2", "manager_agent",
                                     "basic_agent", "explorer_agent", "auditor_agent"])
def test_all_configured_profiles_send_native_prompt_and_exact_exposed_tools(profile):
    cfg = deepcopy(get_config())
    cfg.model.model_type = "text"
    cfg.inference.tool_protocol = "qwen35-native"
    cfg.agent.enabled_agent_profiles = ["coordinator_1", "coordinator_2", "manager_agent",
                                       "basic_agent", "explorer_agent", "auditor_agent"]
    service = CaptureService("Done.")
    service.config = cfg
    runner = AgentRunner(service, cfg)
    candidate = AgentToolConfigurationRequest(
        schema_version=1, catalog_revision=runner.tool_catalog.catalog_revision,
        manager_agent_tools=list(EXAMPLES), basic_agent_tools=list(EXAMPLES))
    snapshot = runner.tool_catalog.resolve_candidate(candidate)
    session = AgentSession(session_id="profile-audit", agent=None, mode="tool_calling",
                           tool_protocol="qwen35-native")
    session.tool_configuration_snapshot = snapshot
    agent = runner._build_profile_agent(profile, session=session)
    session.agent = agent
    # Exercise the ordinary production adapter, including runtime context.
    agent.model([*agent.write_memory_to_messages(), Message("user", "Answer directly.")],
                tools_to_call_from=agent.tools_and_managed_agents)
    request = service.request
    system = next(m.content for m in request["messages"] if m.role == "system")
    if isinstance(system, list):
        system = "".join(part["text"] for part in system if part.get("type") == "text")
    assert not re.search(r"<<(?:END_)?TOOL_CALL>>|<<SWITCH_AGENT>>|Calling tools:", system)
    assert "Qwen 原生工具调用协议" in system
    schemas = {s["function"]["name"]: s["function"] for s in request["tools"] or []}
    if profile in {"manager_agent", "basic_agent"}:
        assert set(schemas).intersection(EXAMPLES) == set(EXAMPLES)
        for module in snapshot.manager_agent.tool_modules:
            assert module in system
        for combination in snapshot.manager_agent.combination_modules:
            assert combination in system
        assert "final_answer" not in schemas
        if profile == "manager_agent":
            assert set(schemas["dispatching_parallel_agent"]["parameters"]["properties"]) == {"child_id", "task"}
    elif profile == "coordinator_1":
        assert set(schemas) == {"switch_agent"}
        assert schemas["switch_agent"]["parameters"]["required"] == ["target_agent"]
    elif profile == "coordinator_2":
        assert set(schemas) == {"switch_agent", "basic_agent"}
        assert set(schemas["basic_agent"]["parameters"]["properties"]) == {"task", "additional_args"}
    else:
        assert schemas == {}
