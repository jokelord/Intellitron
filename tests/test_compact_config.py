"""Tests for CompactConfig and CompactionPromptsConfig loading from YAML."""
import sys
from pathlib import Path

import yaml


def _make_minimal_config_tree(project_root: Path, compact_section: dict | None = None, agent_prompts: dict | None = None) -> None:
    """Create a minimal config/ tree for a ConfigLoader to consume.

    Writes all needed subfiles referenced by config-overall.yaml.
    """
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    overall = {
        "model": "config/model.yaml",
        "infer": "config/infer.yaml",
        "server": "config/server.yaml",
        "misc": "config/misc.yaml",
        "agent_prompts": "config/agent_prompts.yaml",
    }
    (config_dir / "config-overall.yaml").write_text(yaml.safe_dump(overall))

    (config_dir / "model.yaml").write_text(
        yaml.safe_dump({"model_path": "/dummy/model", "model_name": "dummy"})
    )
    (config_dir / "infer.yaml").write_text(
        yaml.safe_dump({"max_context_length": 32000})
    )
    (config_dir / "server.yaml").write_text(
        yaml.safe_dump({"host": "127.0.0.1", "port": 8090})
    )

    misc = {"some_other_field": "value"}
    if compact_section is not None:
        misc["compact"] = compact_section
    (config_dir / "misc.yaml").write_text(yaml.safe_dump(misc))

    prompts_data = agent_prompts or {}
    (config_dir / "agent_prompts.yaml").write_text(yaml.safe_dump(prompts_data))


def _loader_with_root(monkeypatch, project_root: Path):
    """Build a ConfigLoader whose subfile resolution targets `project_root`."""
    import backend.config as cfg_mod

    overall_path = project_root / "config" / "config-overall.yaml"
    monkeypatch.setattr(cfg_mod, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", overall_path)
    return cfg_mod.ConfigLoader(config_path=overall_path)


def test_compact_config_defaults_loaded(monkeypatch, tmp_path):
    """All defaults applied when misc.yaml omits the compact section entirely."""
    _make_minimal_config_tree(tmp_path, compact_section=None)
    loader = _loader_with_root(monkeypatch, tmp_path)
    cfg = loader.load()
    assert cfg.compact.enabled is True
    assert cfg.compact.auto_trigger is True
    assert cfg.compact.strategy == "performance"
    assert cfg.compact.performance_ratio == 0.5
    assert cfg.compact.utilization_summary_reserve == 20000
    assert cfg.compact.utilization_buffer == 13000
    assert cfg.compact.max_summary_tokens == 3072
    assert cfg.compact.temperature == 0.3
    assert cfg.compact.top_p == 0.9
    assert cfg.compact.extra_body == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    assert cfg.compact.source_token_budget == 6000
    assert cfg.compact.reliable_fallback_enabled is False
    assert cfg.compact.cycle_min_steps_between_compacts == 0
    assert cfg.compact.image_token_estimate_per_image == 512


def test_overall_and_subconfig_paths_accept_home_spelling(monkeypatch, tmp_path):
    _make_minimal_config_tree(tmp_path, agent_prompts={"marker": "loaded"})
    monkeypatch.setenv("HOME", str(tmp_path))
    overall_path = tmp_path / "config" / "config-overall.yaml"
    overall = yaml.safe_load(overall_path.read_text())
    for key, name in (("model", "model.yaml"), ("infer", "infer.yaml"),
                      ("server", "server.yaml"), ("misc", "misc.yaml"),
                      ("agent_prompts", "agent_prompts.yaml")):
        overall[key] = f"~/config/{name}"
    (tmp_path / "config" / "local-memory.yaml").write_text(yaml.safe_dump({
        "local_memory": {"enabled": False},
    }))
    overall["local_memory"] = "~/config/local-memory.yaml"
    overall_path.write_text(yaml.safe_dump(overall))
    monkeypatch.setattr("backend.config.PROJECT_ROOT", tmp_path)

    import backend.config as cfg_mod
    loader = cfg_mod.ConfigLoader(config_path=Path("~/config/config-overall.yaml"))
    loader.load()

    assert loader._data["model_path"] == "/dummy/model"
    assert loader._data["max_context_length"] == 32000
    assert loader._data["host"] == "127.0.0.1"
    assert loader._data["some_other_field"] == "value"
    assert loader._agent_prompts_data["marker"] == "loaded"
    assert loader._config.local_memory.enabled is False


def test_compact_config_explicit_values_override_defaults(monkeypatch, tmp_path):
    """Explicit yaml values override defaults."""
    _make_minimal_config_tree(
        tmp_path,
        compact_section={
            "enabled": False,
            "auto_trigger": False,
            "strategy": "utilization",
            "performance_ratio": 0.7,
            "utilization_summary_reserve": 25000,
            "utilization_buffer": 15000,
            "max_summary_tokens": 18000,
            "temperature": 0.1,
            "top_p": 0.95,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": True}},
            "source_token_budget": 9000,
            "reliable_fallback_enabled": True,
            "cycle_min_steps_between_compacts": 3,
            "image_token_estimate_per_image": 1024,
        },
    )
    loader = _loader_with_root(monkeypatch, tmp_path)
    cfg = loader.load()
    assert cfg.compact.enabled is False
    assert cfg.compact.auto_trigger is False
    assert cfg.compact.strategy == "utilization"
    assert cfg.compact.performance_ratio == 0.7
    assert cfg.compact.utilization_summary_reserve == 25000
    assert cfg.compact.utilization_buffer == 15000
    assert cfg.compact.max_summary_tokens == 18000
    assert cfg.compact.temperature == 0.1
    assert cfg.compact.top_p == 0.95
    assert cfg.compact.extra_body == {"chat_template_kwargs": {"enable_thinking": True}}
    assert cfg.compact.source_token_budget == 9000
    assert cfg.compact.reliable_fallback_enabled is True
    assert cfg.compact.cycle_min_steps_between_compacts == 3
    assert cfg.compact.image_token_estimate_per_image == 1024


def test_compaction_prompts_loaded_from_agent_prompts_yaml(monkeypatch, tmp_path):
    """compaction section is parsed into AgentPromptsConfig.compaction."""
    _make_minimal_config_tree(
        tmp_path,
        agent_prompts={
            "coordinator_1": {"instructions": "test"},
            "compaction": {
                "status_message": "Compressing",
                "no_tools_preamble": "NO TOOLS",
                "main_prompt": "9 sections",
                "custom_instructions_header": "USER INSTR",
                "no_tools_trailer": "NO TOOLS REMINDER",
                "summary_envelope_preface": "PRE",
                "summary_envelope_suffix": "POST",
            },
        },
    )
    loader = _loader_with_root(monkeypatch, tmp_path)
    cfg = loader.load()
    assert cfg.agent_prompts.compaction is not None
    assert cfg.agent_prompts.compaction.status_message == "Compressing"
    assert cfg.agent_prompts.compaction.no_tools_preamble == "NO TOOLS"
    assert cfg.agent_prompts.compaction.main_prompt == "9 sections"
    assert cfg.agent_prompts.compaction.custom_instructions_header == "USER INSTR"
    assert cfg.agent_prompts.compaction.no_tools_trailer == "NO TOOLS REMINDER"
    assert cfg.agent_prompts.compaction.summary_envelope_preface == "PRE"
    assert cfg.agent_prompts.compaction.summary_envelope_suffix == "POST"


def test_compaction_prompts_none_when_section_absent(monkeypatch, tmp_path):
    """When agent_prompts.yaml omits compaction, AgentPromptsConfig.compaction is None."""
    _make_minimal_config_tree(tmp_path, agent_prompts={"coordinator_1": {}})
    loader = _loader_with_root(monkeypatch, tmp_path)
    cfg = loader.load()
    assert cfg.agent_prompts.compaction is None


def test_compact_prompt_shape_config_defaults(monkeypatch, tmp_path):
    _make_minimal_config_tree(tmp_path, compact_section=None)
    loader = _loader_with_root(monkeypatch, tmp_path)
    cfg = loader.load()

    assert cfg.compact.diagnostics_enabled is True
    assert cfg.compact.prompt_shape == "cache_first_tail_with_fallback"
    assert cfg.compact.prompt_control_enabled is True
    assert cfg.compact.validation_min_summary_chars == 200
    assert cfg.compact.validation_min_summary_prompt_ratio == 0.002
    assert cfg.compact.validation_require_summary_sections is True


def test_compact_prompt_shape_config_explicit_values(monkeypatch, tmp_path):
    _make_minimal_config_tree(
        tmp_path,
        compact_section={
            "diagnostics_enabled": False,
            "prompt_shape": "cache_first_tail_with_fallback",
            "prompt_control_enabled": True,
            "validation_min_summary_chars": 500,
            "validation_min_summary_prompt_ratio": 0.01,
            "validation_require_summary_sections": False,
        },
    )
    loader = _loader_with_root(monkeypatch, tmp_path)
    cfg = loader.load()

    assert cfg.compact.diagnostics_enabled is False
    assert cfg.compact.prompt_shape == "cache_first_tail_with_fallback"
    assert cfg.compact.prompt_control_enabled is True
    assert cfg.compact.validation_min_summary_chars == 500
    assert cfg.compact.validation_min_summary_prompt_ratio == 0.01
    assert cfg.compact.validation_require_summary_sections is False


def test_compaction_prompt_control_user_instruction_loaded_without_legacy_fields(monkeypatch, tmp_path):
    _make_minimal_config_tree(
        tmp_path,
        agent_prompts={
            "coordinator_1": {"instructions": "test"},
            "compaction": {
                "compact_control_sentinel": "[[SENTINEL]]",
                "compact_control_system_clause": "If latest user starts with [[SENTINEL]], summarize.",
                "compact_control_user_instruction": "[[SENTINEL]] summarize prior messages.",
            },
        },
    )
    loader = _loader_with_root(monkeypatch, tmp_path)
    cfg = loader.load()

    assert cfg.agent_prompts.compaction is not None
    assert "prior messages" in cfg.agent_prompts.compaction.compact_control_user_instruction
    assert not hasattr(cfg.agent_prompts.compaction, "compact_control_sentinel")
    assert not hasattr(cfg.agent_prompts.compaction, "compact_control_system_clause")
