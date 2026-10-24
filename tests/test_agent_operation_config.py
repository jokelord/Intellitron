"""Tests for the standalone agent-operation configuration route."""

from pathlib import Path

import pytest
import yaml

from backend.config import ConfigLoader


def test_agent_operation_route_loads_all_four_sections(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    overall = {
        "misc": "config/misc.yaml",
        "agent_operation": "config/agent-operation.yaml",
    }
    (config_dir / "config-overall.yaml").write_text(
        yaml.safe_dump(overall), encoding="utf-8"
    )
    (config_dir / "misc.yaml").write_text(
        yaml.safe_dump({"compact": {}}), encoding="utf-8"
    )
    (config_dir / "agent-operation.yaml").write_text(
        yaml.safe_dump(
            {
                "agent": {
                    "enabled": False,
                    "entry_agent_name": "basic_agent",
                    "coordinator_1_timeout_seconds": 37,
                    "file_operation_roots": None,
                    "image_input_roots": None,
                    "session_temp_base_dir": str(tmp_path / "agent-temp"),
                    "max_file_bytes": 8192,
                    "allow_overwrite_existing": False,
                },
                "read_file": {"max_source_bytes": 12345},
                "mcp": {
                    "servers": [
                        {
                            "name": "demo",
                            "command": "demo-mcp",
                            "args": ["stdio"],
                        }
                    ]
                },
                "web_tools": {
                    "provider": "searxng",
                    "searxng": {"base_url": "http://example.test"},
                },
            }
        ),
        encoding="utf-8",
    )

    import backend.config as config_module

    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    config = ConfigLoader(config_path=config_dir / "config-overall.yaml").load()

    assert config.agent.enabled is False
    assert config.agent.entry_agent_name == "basic_agent"
    assert config.agent.coordinator_1_timeout_seconds == 37
    assert config.agent.file_operation_roots is None
    assert config.agent.image_input_roots is None
    assert config.agent.session_temp_base_dir == str(tmp_path / "agent-temp")
    assert config.agent.max_file_bytes == 8192
    assert config.agent.allow_overwrite_existing is False
    assert config.read_file.max_source_bytes == 12345
    assert config.mcp.servers[0].name == "demo"
    assert config.web_tools.searxng_base_url == "http://example.test"


def test_agent_operation_security_fields_fail_closed_with_precise_names(
    monkeypatch, tmp_path
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config-overall.yaml").write_text(
        yaml.safe_dump({"agent_operation": "config/agent-operation.yaml"}),
        encoding="utf-8",
    )
    (config_dir / "agent-operation.yaml").write_text(
        yaml.safe_dump(
            {
                "agent": {
                    "entry_agent_name": "basic_agent",
                    "allow_overwrite_existing": "yes",
                }
            }
        ),
        encoding="utf-8",
    )

    import backend.config as config_module

    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)

    with pytest.raises(ValueError, match="agent.allow_overwrite_existing"):
        ConfigLoader(config_path=config_dir / "config-overall.yaml").load()
