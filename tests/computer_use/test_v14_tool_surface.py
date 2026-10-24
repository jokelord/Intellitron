import json
import re

from backend.agent.tool_configuration import BUSINESS_TOOL_NAMES, build_business_tool_catalog
from backend.agent.agent_runner import _get_tools
from backend.config import get_config


def test_computer_use_is_not_a_public_business_tool():
    assert "computer_use" not in BUSINESS_TOOL_NAMES
    catalog = build_business_tool_catalog(get_config())
    assert "computer_use" not in catalog.available_tool_names
    assert all(item["name"] != "computer_use" for item in catalog.public_metadata()["business_tool_catalog"])


def test_agent_tool_surface_has_exec_command_but_no_computer_use():
    names = {getattr(tool, "name", None) for tool in _get_tools()}
    assert "exec_command" in names
    assert "computer_use" not in names


def test_skill_sequence_examples_are_valid_public_commands():
    from backend.computer_use.executable_protocol import parse_vision_desktop_command
    from backend.tools.skill_ops import read_skill

    skill = read_skill("computer-use-vision")
    examples = re.findall(r'exec_command\("([^"]+)"\)', skill)
    values = {
        "<overview-x>": "250",
        "<overview-y>": "500",
        "<chosen-zoom>": "2",
        "<detail-x>": "375",
        "<detail-y>": "625",
        "<requested-action>": "left_click",
    }
    operations = []
    for example in examples:
        for placeholder, value in values.items():
            example = example.replace(placeholder, value)
        operations.append(parse_vision_desktop_command(example).operation.value)

    assert operations == ["observe", "inspect_region", "interact_detail"]


def test_public_skill_is_discoverable():
    from backend.tools.skill_ops import list_skills, read_skill

    records = json.loads(list_skills())["skills"]
    record = next(item for item in records if item["name"] == "computer-use-vision")
    assert "error" not in record
    skill = read_skill("computer-use-vision")
    assert skill.startswith("---\nname: computer-use-vision\n")
