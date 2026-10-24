import asyncio
import json
from types import SimpleNamespace
import re
from unittest.mock import MagicMock

import pytest
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request

from backend.agent.agent_runner import AgentRunner
from backend.agent.tool_configuration import (
    AgentToolConfigurationError,
    AgentToolConfigurationRequest,
    BUSINESS_TOOL_NAMES,
    BusinessToolCatalog,
    default_candidate,
)
from backend.config import get_config
from backend.main import _active_tool_configuration_metadata, request_validation_handler


def _catalog():
    config = get_config()
    prompts = config.agent_prompts
    return BusinessToolCatalog(
        prompt_config={
            "business_tool_protocol": prompts.business_tool_protocol,
            "business_tool_modules": prompts.business_tool_modules,
            "tool_prompt_combinations": prompts.tool_prompt_combinations,
        },
        default_profile_tools=config.agent.default_profile_tools,
    )


def test_yaml_defaults_are_separate_and_manager_has_write_execution():
    defaults = get_config().agent.default_profile_tools
    assert defaults["manager_agent"] == (
        "read_file", "write_file", "edit_file", "glob", "grep",
        "exec_command", "list_skills", "read_skill", "web_fetch",
        "search_and_fetch",
    )
    assert defaults["basic_agent"] == (
        "read_file", "write_file", "edit_file", "glob", "grep",
        "exec_command", "check_process", "list_skills",
        "read_skill", "web_search", "web_fetch", "search_and_fetch",
    )


def test_catalog_normalizes_order_and_compiles_required_combinations():
    catalog = _catalog()
    candidate = AgentToolConfigurationRequest(
        schema_version=1,
        catalog_revision=catalog.catalog_revision,
        manager_agent_tools=["exec_command", "read_skill", "list_skills"],
        basic_agent_tools=[],
    )
    snapshot = catalog.resolve_candidate(candidate)
    assert snapshot.manager_agent.tool_names == (
        "exec_command", "list_skills", "read_skill"
    )
    assert any("list_skills" in module and "read_skill" in module
               for module in snapshot.manager_agent.combination_modules)
    assert snapshot.basic_agent.tool_names == ()
    assert snapshot.basic_agent.business_tool_protocol == ""


def test_default_candidate_reads_catalog_initialization_defaults():
    catalog = _catalog()
    candidate = default_candidate(catalog)
    assert tuple(candidate.manager_agent_tools) == catalog.default_profile_tools["manager_agent"]
    assert tuple(candidate.basic_agent_tools) == catalog.default_profile_tools["basic_agent"]


def test_duplicate_and_special_tools_are_rejected():
    catalog = _catalog()
    for names in (["read_file", "read_file"], ["final_answer"], ["wikipedia_search"]):
        try:
            candidate = AgentToolConfigurationRequest(
                schema_version=1,
                catalog_revision=catalog.catalog_revision,
                manager_agent_tools=names,
                basic_agent_tools=[],
            )
            catalog.resolve_candidate(candidate)
        except (ValueError, AgentToolConfigurationError):
            continue
        raise AssertionError(f"selection unexpectedly accepted: {names}")


def test_snapshot_metadata_and_runtime_prompt_schema_symmetry_for_empty_selection():
    config = get_config()
    runner = AgentRunner(MagicMock(), config)
    candidate = AgentToolConfigurationRequest(
        schema_version=1,
        catalog_revision=runner.tool_catalog.catalog_revision,
        manager_agent_tools=[],
        basic_agent_tools=[],
    )
    snapshot = runner.tool_catalog.resolve_candidate(candidate)
    metadata = snapshot.public_metadata()
    assert metadata["locked"] is True
    assert metadata["fingerprint"] == snapshot.fingerprint

    for profile in ("manager_agent", "basic_agent"):
        agent = runner._build_profile_agent(profile, tool_snapshot=snapshot)
        business_names = set(BUSINESS_TOOL_NAMES)
        visible_schema = {
            tool.name for tool in agent.tools_and_managed_agents
            if tool.name in business_names
        }
        prompt = agent.initialize_system_prompt()
        assert visible_schema == set()
        assert not any(name in prompt for name in business_names)
    assert "dispatching_parallel_agent" in runner._build_profile_agent(
        "manager_agent", tool_snapshot=snapshot
    ).tools


def test_catalog_lock_rejects_different_candidate():
    catalog = _catalog()
    first = AgentToolConfigurationRequest(
        schema_version=1,
        catalog_revision=catalog.catalog_revision,
        manager_agent_tools=[],
        basic_agent_tools=[],
    )
    second = AgentToolConfigurationRequest(
        schema_version=1,
        catalog_revision=catalog.catalog_revision,
        manager_agent_tools=["read_file"],
        basic_agent_tools=[],
    )
    snapshot = catalog.resolve_candidate(first)
    try:
        catalog.assert_compatible(snapshot, second)
    except AgentToolConfigurationError as exc:
        assert exc.code == "tool_configuration_locked"
    else:
        raise AssertionError("different candidate did not lock")


@pytest.mark.parametrize("protocol", ["legacy_text", "qwen35-native"])
def test_singleton_selection_has_no_unselected_business_tool_prompt_leak(protocol):
    from copy import deepcopy
    config = deepcopy(get_config())
    config.inference.tool_protocol = protocol
    runner = AgentRunner(MagicMock(), config)
    available = [
        name for name in BUSINESS_TOOL_NAMES
        if name in runner.tool_catalog.available_tool_names
    ]
    for profile in ("manager_agent", "basic_agent"):
        for selected_name in available:
            candidate = AgentToolConfigurationRequest(
                schema_version=1,
                catalog_revision=runner.tool_catalog.catalog_revision,
                manager_agent_tools=[selected_name] if profile == "manager_agent" else [],
                basic_agent_tools=[selected_name] if profile == "basic_agent" else [],
            )
            snapshot = runner.tool_catalog.resolve_candidate(candidate)
            agent = runner._build_profile_agent(profile, tool_snapshot=snapshot)
            prompt = agent.initialize_system_prompt()
            selected_tool_prompt = agent.tools[
                selected_name
            ].to_tool_calling_prompt()
            if protocol == "legacy_text":
                assert prompt.count(selected_tool_prompt) == 1, (profile, selected_name)
            else:
                # Native definitions are encoded once by the inference template
                # from request.tools; policy modules remain in the stable prompt.
                assert selected_tool_prompt not in prompt
                schema = agent.model._request_tools(agent.tools_and_managed_agents)
                assert sum(s["function"]["name"] == selected_name for s in schema) == 1
                hint = runner.config.agent_prompts.business_tool_modules[selected_name]["prompt"]
                assert hint.strip() in prompt
                assert "<<TOOL_CALL>>" not in prompt
            for other_name in available:
                if other_name == selected_name:
                    continue
                assert re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(other_name)}(?![A-Za-z0-9_])",
                    prompt,
                ) is None, (profile, selected_name, other_name)


def test_catalog_rejects_duplicate_unknown_special_and_unavailable_defaults():
    config = get_config()
    prompt_config = {
        "business_tool_protocol": config.agent_prompts.business_tool_protocol,
        "business_tool_modules": config.agent_prompts.business_tool_modules,
        "tool_prompt_combinations": config.agent_prompts.tool_prompt_combinations,
    }
    bad_defaults = [
        {"manager_agent": ["read_file", "read_file"], "basic_agent": []},
        {"manager_agent": ["unknown_tool"], "basic_agent": []},
        {"manager_agent": ["final_answer"], "basic_agent": []},
        {"manager_agent": ["wikipedia_search"], "basic_agent": []},
        {"manager_agent": ["wikipedia_search_and_fetch"], "basic_agent": []},
    ]
    for defaults in bad_defaults:
        kwargs = {"default_profile_tools": defaults}
        if defaults["manager_agent"] == ["wikipedia_search_and_fetch"]:
            kwargs["available_tool_names"] = [
                name for name in BUSINESS_TOOL_NAMES
                if name != "wikipedia_search_and_fetch"
            ]
        try:
            BusinessToolCatalog(prompt_config=prompt_config, **kwargs)
        except AgentToolConfigurationError as exc:
            assert exc.code == "invalid_tool_configuration"
        else:
            raise AssertionError(f"invalid defaults accepted: {defaults}")

    try:
        BusinessToolCatalog(
            prompt_config=prompt_config,
            default_profile_tools={},
        )
    except AgentToolConfigurationError as exc:
        assert exc.code == "invalid_tool_configuration"
    else:
        raise AssertionError("explicit empty defaults were silently replaced")


def test_catalog_rejects_unhashable_default_tool_with_stable_domain_error():
    """A malformed YAML item must not escape as an incidental TypeError."""

    config = get_config()
    prompt_config = {
        "business_tool_protocol": config.agent_prompts.business_tool_protocol,
        "business_tool_modules": config.agent_prompts.business_tool_modules,
        "tool_prompt_combinations": config.agent_prompts.tool_prompt_combinations,
    }
    with pytest.raises(AgentToolConfigurationError) as caught:
        BusinessToolCatalog(
            prompt_config=prompt_config,
            default_profile_tools={
                "manager_agent": [["read_file"]],
                "basic_agent": [],
            },
        )

    assert caught.value.code == "invalid_tool_configuration"


def test_catalog_requires_a_complete_active_prompt_module_set_and_active_combination():
    config = get_config()
    prompt_config = {
        "business_tool_protocol": config.agent_prompts.business_tool_protocol,
        "business_tool_modules": dict(config.agent_prompts.business_tool_modules),
        "tool_prompt_combinations": config.agent_prompts.tool_prompt_combinations,
    }
    prompt_config["business_tool_modules"].pop("read_file")
    try:
        BusinessToolCatalog(prompt_config=prompt_config)
    except AgentToolConfigurationError as exc:
        assert exc.code == "invalid_tool_configuration"
    else:
        raise AssertionError("catalog accepted an active tool without a prompt module")

    prompt_config = {
        "business_tool_protocol": config.agent_prompts.business_tool_protocol,
        "business_tool_modules": config.agent_prompts.business_tool_modules,
        "tool_prompt_combinations": [
            {"id": "inactive", "requires_all": ["read_file", "wikipedia_search_and_fetch"], "prompt": "x"}
        ],
    }
    try:
        BusinessToolCatalog(
            prompt_config=prompt_config,
            available_tool_names=[name for name in BUSINESS_TOOL_NAMES if name != "wikipedia_search_and_fetch"],
        )
    except AgentToolConfigurationError as exc:
        assert exc.code == "invalid_tool_configuration"
    else:
        raise AssertionError("catalog accepted a combination requiring an inactive tool")


def test_catalog_rejects_cross_tool_names_in_source_prompt_modules_and_combinations():
    config = get_config()
    base = {
        "business_tool_protocol": config.agent_prompts.business_tool_protocol,
        "business_tool_modules": config.agent_prompts.business_tool_modules,
        "tool_prompt_combinations": config.agent_prompts.tool_prompt_combinations,
    }

    protocol_config = dict(base)
    protocol_config["business_tool_protocol"] = "use read_file when needed"
    with pytest.raises(AgentToolConfigurationError, match="business_tool_protocol"):
        BusinessToolCatalog(prompt_config=protocol_config)

    modules = {
        name: dict(module)
        for name, module in base["business_tool_modules"].items()
    }
    modules["write_file"]["prompt"] = "write_file must first inspect with read_file"
    module_config = dict(base)
    module_config["business_tool_modules"] = modules
    with pytest.raises(AgentToolConfigurationError, match="write_file"):
        BusinessToolCatalog(prompt_config=module_config)

    combo_config = dict(base)
    combo_config["tool_prompt_combinations"] = [
        {
            "id": "invalid-owner",
            "requires_all": ["read_file"],
            "prompt": "also use write_file",
        }
    ]
    with pytest.raises(AgentToolConfigurationError, match="invalid-owner"):
        BusinessToolCatalog(prompt_config=combo_config)


def test_schema_version_is_runtime_catalog_error_not_pydantic_error():
    catalog = _catalog()
    candidate = AgentToolConfigurationRequest(
        schema_version=999,
        catalog_revision=catalog.catalog_revision,
        manager_agent_tools=[],
        basic_agent_tools=[],
    )
    try:
        catalog.resolve_candidate(candidate)
    except AgentToolConfigurationError as exc:
        assert exc.code == "tool_catalog_changed"
        assert exc.status_code == 409
    else:
        raise AssertionError("stale schema version was accepted")


def test_catalog_metadata_is_canonical_and_active_catalog_keeps_wikipedia_composite():
    config = get_config()
    catalog = BusinessToolCatalog(
        prompt_config={
            "business_tool_protocol": config.agent_prompts.business_tool_protocol,
            "business_tool_modules": config.agent_prompts.business_tool_modules,
            "tool_prompt_combinations": config.agent_prompts.tool_prompt_combinations,
        },
        default_profile_tools=config.agent.default_profile_tools,
        available_tool_names=BUSINESS_TOOL_NAMES,
    )
    metadata = catalog.public_metadata()
    assert [item["name"] for item in metadata["tools"]] == list(BUSINESS_TOOL_NAMES)
    assert metadata["business_tool_catalog"] == metadata["tools"]
    assert metadata["default_profile_tools"] == {
        key: list(value) for key, value in catalog.default_profile_tools.items()
    }
    assert "wikipedia_search_and_fetch" in catalog.available_tool_names


def test_catalog_metadata_never_fabricates_missing_ui_fields():
    """Corrupt catalog state must fail instead of manufacturing UI metadata."""

    catalog = _catalog()
    catalog.tool_modules["read_file"].pop("label")

    with pytest.raises(KeyError, match="label"):
        catalog.public_metadata()


def test_config_projection_publishes_canonical_tool_configuration_fields():
    catalog = _catalog()
    runner = SimpleNamespace(tool_catalog=catalog, config=get_config())
    metadata = _active_tool_configuration_metadata(runner, get_config())
    assert metadata["tool_configuration_schema_version"] == 1
    assert metadata["tool_catalog_revision"] == catalog.catalog_revision
    assert metadata["business_tool_catalog"]
    assert metadata["default_profile_tools"]


def test_basic_empty_business_selection_has_no_generic_business_framing():
    runner = AgentRunner(MagicMock(), get_config())
    candidate = AgentToolConfigurationRequest(
        schema_version=1,
        catalog_revision=runner.tool_catalog.catalog_revision,
        manager_agent_tools=[],
        basic_agent_tools=[],
    )
    snapshot = runner.tool_catalog.resolve_candidate(candidate)
    prompt = runner._build_profile_agent(
        "basic_agent", tool_snapshot=snapshot
    ).initialize_system_prompt()
    assert "工具使用的概念及基本流程" not in prompt
    assert "工具调用的重要准则" not in prompt


def test_prompt_template_failure_maps_to_stable_render_error_without_sanitizing():
    runner = AgentRunner(MagicMock(), get_config())
    candidate = AgentToolConfigurationRequest(
        schema_version=1,
        catalog_revision=runner.tool_catalog.catalog_revision,
        manager_agent_tools=["read_file"],
        basic_agent_tools=[],
    )
    snapshot = runner.tool_catalog.resolve_candidate(candidate)
    agent = runner._build_profile_agent("manager_agent", tool_snapshot=snapshot)
    agent.prompt_templates["system_prompt"] = "{{ missing_template_variable.foo }}"
    try:
        agent.initialize_system_prompt()
    except AgentToolConfigurationError as exc:
        assert exc.code == "tool_prompt_render_failed"
        assert exc.status_code == 500
    else:
        raise AssertionError("template rendering failure was not surfaced")


def test_config_fallback_propagates_catalog_errors_instead_of_empty_catalog():
    bad_config = SimpleNamespace(
        web_tools=SimpleNamespace(enable_wikipedia=False),
        agent_prompts=SimpleNamespace(
            business_tool_protocol="",
            business_tool_modules={},
            tool_prompt_combinations=[],
        ),
        agent=SimpleNamespace(default_profile_tools={
            "manager_agent": [],
            "basic_agent": [],
        }),
    )
    try:
        _active_tool_configuration_metadata(None, bad_config)
    except AgentToolConfigurationError as exc:
        assert exc.code == "invalid_tool_configuration"
    else:
        raise AssertionError("catalog configuration failure was swallowed")


def test_agent_tool_configuration_validation_error_has_stable_http_contract():
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/agent/chat",
        "raw_path": b"/v1/agent/chat",
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 123),
        "scheme": "http",
        "root_path": "",
        "http_version": "1.1",
    }
    request = Request(scope)
    error = RequestValidationError([
        {
            "type": "list_type",
            "loc": ("body", "tool_configuration", "manager_agent_tools"),
            "msg": "Input should be a valid list",
            "input": "read_file",
        }
    ])
    response = asyncio.run(request_validation_handler(request, error))
    assert response.status_code == 422
    assert json.loads(response.body)["detail"]["code"] == "invalid_tool_configuration"
