from backend.agent.tool_configuration import (
    BUSINESS_TOOL_NAMES,
    build_business_tool_catalog,
)
from backend.config import get_config


def test_computer_use_is_not_selectable_as_a_business_tool():
    config = get_config()
    catalog = build_business_tool_catalog(config)

    assert config.computer_use.ready is False
    assert config.agent.allow_desktop_capture is True
    assert config.agent.allow_desktop_input is True
    assert config.computer_use.operator_session_enabled is True
    assert "computer_use" not in BUSINESS_TOOL_NAMES
    assert "computer_use" not in catalog.available_tool_names
    assert "computer_use" not in catalog.default_profile_tools["manager_agent"]
    assert "computer_use" not in catalog.default_profile_tools["basic_agent"]


def test_catalog_metadata_has_a_unique_stable_order_without_computer_use():
    metadata = build_business_tool_catalog(get_config()).public_metadata()
    tools = metadata["business_tool_catalog"]

    assert all(item["name"] != "computer_use" for item in tools)
    assert len({item["order"] for item in tools}) == len(tools)
