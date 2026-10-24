from dataclasses import replace

import yaml
import pytest

from backend.config import ConfigLoader, WebUIConfig


def test_web_ui_config_constructor_defaults_message_templates_to_empty_list():
    assert WebUIConfig().message_templates == []


def test_parse_web_ui_message_templates_preserves_order_and_text():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {
        "webUI": {
            "components": {
                "message_templates": {
                    "items": [
                        " Hello, what is your name? ",
                        "Tell me the weather forecast for Changsha.\nUse Celsius.",
                    ]
                }
            }
        }
    }

    assert loader._parse_web_ui_config().message_templates == [
        " Hello, what is your name? ",
        "Tell me the weather forecast for Changsha.\nUse Celsius.",
    ]


def test_parse_web_ui_message_templates_filters_blank_and_non_string_items():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {
        "webUI": {
            "components": {
                "message_templates": {
                    "items": ["first", "  ", None, 42, "second"]
                }
            }
        }
    }

    assert loader._parse_web_ui_config().message_templates == ["first", "second"]


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"webUI": None},
        {"webUI": "not-an-object"},
        {"webUI": {"components": "not-an-object"}},
        {"webUI": {"components": {"message_templates": None}}},
        {"webUI": {"components": {"message_templates": []}}},
        {"webUI": {"components": {"message_templates": "not-an-object"}}},
        {
            "webUI": {
                "components": {"message_templates": {"items": "not-an-array"}}
            }
        },
    ],
)
def test_parse_web_ui_message_templates_defaults_to_empty_list_for_invalid_shape(data):
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = data

    assert loader._parse_web_ui_config().message_templates == []


def test_parse_web_ui_button_sizes():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {
        "webUI": {
            "skin": {"id": "default-v1"},
            "components": {
                "action_buttons": {
                    "send": {"width_px": 60, "height_px": 52},
                    "stop": {"width_px": 62, "height_px": 54},
                    "answer_now": {"width_px": 64, "height_px": 56},
                }
            },
        }
    }

    cfg = loader._parse_web_ui_config()

    assert isinstance(cfg, WebUIConfig)
    assert cfg.skin_id == "default-v1"
    assert cfg.skin_name == "Default Skin v1"
    assert cfg.action_buttons["send"] == {"width_px": 60, "height_px": 52}
    assert cfg.action_buttons["stop"] == {"width_px": 62, "height_px": 54}
    assert cfg.action_buttons["answer_now"] == {"width_px": 64, "height_px": 56}


def test_parse_web_ui_mermaid_and_page_header_config():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {
        "webUI": {
            "components": {
                "mermaid": {"min_height_px": 420},
                "page_header": {
                    "text_title": "自定义文本模式",
                    "vision_title": "自定义视觉模式",
                    "subtitle": "Custom Device",
                },
                "message_avatar": {
                    "user_image_path": "/avatars/user.png",
                    "assistant_image_path": "/avatars/assistant.png",
                    "size_px": 44,
                },
                "vision_welcome": {
                    "title": "自定义欢迎",
                    "description": "自定义说明",
                    "icon_image_path": "/welcome.png",
                },
            }
        }
    }

    cfg = loader._parse_web_ui_config()

    assert cfg.mermaid == {"min_height_px": 420}
    assert cfg.page_header == {
        "display": False,
        "text_title": "自定义文本模式",
        "vision_title": "自定义视觉模式",
        "subtitle": "Custom Device",
    }
    assert cfg.message_avatar == {
        "user_image_path": "/avatars/user.png",
        "assistant_image_path": "/avatars/assistant.png",
        "size_px": 44,
    }
    assert cfg.vision_welcome == {
        "title": "自定义欢迎",
        "description": "自定义说明",
        "icon_image_path": "/welcome.png",
    }


def test_parse_web_ui_avatar_size_is_clamped():
    loader = ConfigLoader.__new__(ConfigLoader)

    loader._data = {"webUI": {"components": {"message_avatar": {"size_px": 12}}}}
    assert loader._parse_web_ui_config().message_avatar["size_px"] == 24

    loader._data = {"webUI": {"components": {"message_avatar": {"size_px": 128}}}}
    assert loader._parse_web_ui_config().message_avatar["size_px"] == 96


def test_parse_web_ui_agent_steps_fold_after_final_answer_defaults_to_true():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {"webUI": {}}
    assert loader._parse_web_ui_config().agent_steps == {"fold_after_final_answer": True}

    loader._data = {"webUI": {"components": {}}}
    assert loader._parse_web_ui_config().agent_steps == {"fold_after_final_answer": True}

    loader._data = {
        "webUI": {"components": {"agent_steps": {"fold_after_final_answer": False}}}
    }
    assert loader._parse_web_ui_config().agent_steps == {"fold_after_final_answer": False}

    loader._data = {
        "webUI": {"components": {"agent_steps": {"fold_after_final_answer": "not-a-bool"}}}
    }
    assert loader._parse_web_ui_config().agent_steps == {"fold_after_final_answer": True}


def test_parse_web_ui_page_header_display_defaults_false_and_coerces_bool():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {"webUI": {"components": {"page_header": {}}}}
    assert loader._parse_web_ui_config().page_header["display"] is False

    loader._data = {"webUI": {"components": {"page_header": {"display": True}}}}
    assert loader._parse_web_ui_config().page_header["display"] is True

    loader._data = {"webUI": {"components": {"page_header": {"display": "not-a-bool"}}}}
    assert loader._parse_web_ui_config().page_header["display"] is False


def test_parse_web_ui_layout_background_alpha_defaults_and_clamps():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {"webUI": {}}
    assert loader._parse_web_ui_config().layout == {
        "header_background_alpha": 0.7,
        "footer_background_alpha": 0.6,
    }

    loader._data = {
        "webUI": {
            "layout": {
                "header_background_alpha": 0.42,
                "footer_background_alpha": "0.33",
            }
        }
    }
    cfg = loader._parse_web_ui_config()
    assert cfg.layout["header_background_alpha"] == 0.42
    assert cfg.layout["footer_background_alpha"] == 0.33

    loader._data = {
        "webUI": {
            "layout": {
                "header_background_alpha": 5,
                "footer_background_alpha": -2,
            }
        }
    }
    cfg = loader._parse_web_ui_config()
    assert cfg.layout["header_background_alpha"] == 1.0
    assert cfg.layout["footer_background_alpha"] == 0.0

    loader._data = {
        "webUI": {
            "layout": {
                "header_background_alpha": "not-a-float",
            }
        }
    }
    assert loader._parse_web_ui_config().layout["header_background_alpha"] == 0.7


def test_config_loader_loads_web_ui_from_overall_without_debug_stdout(
    monkeypatch, tmp_path, capsys
):
    import backend.config as cfg_mod

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config-overall.yaml").write_text(
        yaml.safe_dump(
            {
                "model": "config/model.yaml",
                "infer": "config/infer.yaml",
                "server": "config/server.yaml",
                "misc": "config/misc.yaml",
                "webUI": "config/webUI.yaml",
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "model.yaml").write_text(yaml.safe_dump({"model": {}}), encoding="utf-8")
    (config_dir / "infer.yaml").write_text(yaml.safe_dump({"inference": {}}), encoding="utf-8")
    (config_dir / "server.yaml").write_text(yaml.safe_dump({"server": {}}), encoding="utf-8")
    (config_dir / "misc.yaml").write_text(
        yaml.safe_dump({"debug": {"enabled": True, "debug_config": True}}),
        encoding="utf-8",
    )
    (config_dir / "webUI.yaml").write_text(
        yaml.safe_dump(
            {
                "webUI": {
                    "skin": {"id": "default-v1", "name": "Default Skin v1"},
                    "components": {
                        "action_buttons": {
                            "send": {"width_px": 58, "height_px": 50},
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    overall_path = config_dir / "config-overall.yaml"
    monkeypatch.setattr(cfg_mod, "PROJECT_ROOT", tmp_path)
    loader = ConfigLoader(config_path=overall_path)

    cfg = loader.load()
    reloaded = loader.load(reload=True)

    assert cfg.web_ui.skin_name == "Default Skin v1"
    assert reloaded.web_ui.skin_name == "Default Skin v1"
    assert cfg.web_ui.action_buttons["send"] == {"width_px": 58, "height_px": 50}
    assert cfg.web_ui.action_buttons["stop"] == {"width_px": 48, "height_px": 48}
    stdout = capsys.readouterr().out
    assert "[DEBUG] 配置加载完成" not in stdout
    assert "配置文件路径:" not in stdout


def test_parse_web_ui_glass_jelly_effect_defaults_when_absent():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {"webUI": {"skin": {"id": "default-v1", "name": "Default"}}}

    cfg = loader._parse_web_ui_config()

    assert cfg.effects == {
        "aurora_glow": {
            "enabled": False,
            "intensity": 0.72,
            "blur_px": 18,
            "chromatic_aberration": 0.018,
            "refraction_strength": 0.045,
            "tint_a": "#ffffff",
            "tint_b": "#76b900",
            "opacity": 0.72,
            "animated": True,
            "max_dpr": 1.5,
        }
    }


def test_parse_web_ui_glass_jelly_effect_values():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {
        "webUI": {
            "skin": {"id": "aurora-glow-r3f-v1"},
            "effects": {
                "aurora_glow": {
                    "enabled": True,
                    "intensity": 0.9,
                    "blur_px": 24,
                    "chromatic_aberration": 0.025,
                    "refraction_strength": 0.06,
                    "tint_a": "#f8fafc",
                    "tint_b": "#38bdf8",
                    "opacity": 0.8,
                    "animated": False,
                    "max_dpr": 1.25,
                }
            },
        }
    }

    cfg = loader._parse_web_ui_config()

    assert cfg.skin_id == "aurora-glow-r3f-v1"
    assert cfg.skin_name == "Aurora Glow R3F v1"
    assert cfg.effects["aurora_glow"] == {
        "enabled": True,
        "intensity": 0.9,
        "blur_px": 24,
        "chromatic_aberration": 0.025,
        "refraction_strength": 0.06,
        "tint_a": "#f8fafc",
        "tint_b": "#38bdf8",
        "opacity": 0.8,
        "animated": False,
        "max_dpr": 1.25,
    }


def test_parse_web_ui_skin_name_is_derived_from_id():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {
        "webUI": {
            "skin": {"id": "aurora-glow-r3f-v1", "name": "Ignored stale name"},
        }
    }

    cfg = loader._parse_web_ui_config()

    assert cfg.skin_id == "aurora-glow-r3f-v1"
    assert cfg.skin_name == "Aurora Glow R3F v1"


def test_parse_web_ui_unknown_skin_name_falls_back_to_id():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {"webUI": {"skin": {"id": "custom-skin-v1"}}}

    cfg = loader._parse_web_ui_config()

    assert cfg.skin_id == "custom-skin-v1"
    assert cfg.skin_name == "custom-skin-v1"


def test_parse_web_ui_accepts_legacy_glass_jelly_effect_key():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {
        "webUI": {
            "skin": {"id": "aurora-glow-r3f-v1"},
            "effects": {"glass_jelly": {"enabled": True, "opacity": 0.61}},
        }
    }

    cfg = loader._parse_web_ui_config()

    assert cfg.effects["aurora_glow"]["enabled"] is True
    assert cfg.effects["aurora_glow"]["opacity"] == 0.61


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active_backend", "supports_targeted_abort"),
    [
        ("sglang", True),
        ("vllm", True),
        ("openai_proxy", True),
        ("mock", False),
        (None, False),
    ],
)
async def test_current_config_reports_active_backend_abort_diagnostics(
    monkeypatch,
    active_backend,
    supports_targeted_abort,
):
    import backend.main as main

    current = ConfigLoader().load(reload=True)
    replacement = replace(current)
    service = type("Service", (), {"active_backend": active_backend})()
    monkeypatch.setattr(main, "reload_config", lambda: replacement)
    monkeypatch.setattr(main, "get_llm_service", lambda: service)

    response = await main.get_current_config()

    assert response["inference"]["active_backend"] == active_backend
    assert response["inference"]["supports_targeted_abort"] is supports_targeted_abort
