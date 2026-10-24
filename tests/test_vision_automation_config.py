from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from backend.config import ConfigLoader, VisionAutomationConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_overall_routes_vision_config_through_vision_key():
    overall = yaml.safe_load(
        (PROJECT_ROOT / "config/config-overall.yaml").read_text(encoding="utf-8")
    )

    assert overall["vision"] == "config/config-vision.yaml"
    assert "vision_automation" not in overall


def test_vision_automation_defaults_are_loaded_from_overall():
    config = ConfigLoader(PROJECT_ROOT / "config/config-overall.yaml").load()

    automation = config.vision_automation
    assert automation.enabled is True
    assert automation.interval_seconds == 1.0
    assert automation.max_frames_per_source == 16
    assert automation.thumbnail_size_px == 32
    assert automation.capture_during_inference is False
    assert automation.lock_first_bundle is False
    assert automation.tick_timeout_seconds == 10.0
    assert automation.consecutive_tick_failure_limit == 3
    assert automation.max_encoded_payload_mib == 128
    assert automation.initial_jpeg_quality == 85
    assert automation.minimum_jpeg_quality == 40
    assert automation.minimum_image_dimension_px == 256
    assert automation.base64_encode_concurrency == 8
    assert automation.max_request_body_mib == 136
    assert automation.request_status_ttl_seconds == 600
    assert automation.max_encoded_payload_bytes == 134_217_728
    assert automation.max_request_body_bytes == 142_606_336


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("interval_seconds", 0.09),
        ("max_frames_per_source", 0),
        ("max_frames_per_source", 251),
        ("thumbnail_size_px", 15),
        ("thumbnail_size_px", 129),
        ("tick_timeout_seconds", 0.9),
        ("consecutive_tick_failure_limit", 0),
        ("max_encoded_payload_mib", 0),
        ("max_encoded_payload_mib", 129),
        ("max_request_body_mib", 137),
        ("initial_jpeg_quality", 39),
        ("initial_jpeg_quality", 96),
        ("minimum_jpeg_quality", 0),
        ("minimum_image_dimension_px", 63),
        ("base64_encode_concurrency", 0),
        ("base64_encode_concurrency", 9),
        ("request_status_ttl_seconds", 0),
    ],
)
def test_vision_automation_rejects_out_of_bounds_values(field_name, value):
    with pytest.raises(ValueError, match=field_name):
        VisionAutomationConfig(**{field_name: value})


@pytest.mark.parametrize("field_name", ["interval_seconds", "tick_timeout_seconds"])
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_vision_automation_rejects_non_finite_numeric_values(field_name, value):
    with pytest.raises(ValueError, match=field_name):
        VisionAutomationConfig(**{field_name: value})


def test_vision_automation_rejects_request_ceiling_not_above_image_budget():
    with pytest.raises(ValueError, match="max_request_body_mib"):
        VisionAutomationConfig(max_encoded_payload_mib=64, max_request_body_mib=64)


def test_vision_automation_rejects_minimum_quality_above_initial_quality():
    with pytest.raises(ValueError, match="minimum_jpeg_quality"):
        VisionAutomationConfig(initial_jpeg_quality=60, minimum_jpeg_quality=61)


def test_vision_automation_requires_boolean_first_bundle_lock():
    with pytest.raises(ValueError, match="lock_first_bundle"):
        VisionAutomationConfig(lock_first_bundle=1)


def test_loader_rejects_minimum_dimension_above_model_maximum(tmp_path, monkeypatch):
    import backend.config as config_module

    (tmp_path / "model.yaml").write_text(
        "model:\n  max_image_dimension: 128\n", encoding="utf-8"
    )
    (tmp_path / "vision.yaml").write_text(
        "vision_automation:\n  minimum_image_dimension_px: 256\n", encoding="utf-8"
    )
    overall = tmp_path / "overall.yaml"
    overall.write_text(
        "model: model.yaml\nvision: vision.yaml\n", encoding="utf-8"
    )
    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)

    with pytest.raises(ValueError, match="minimum_image_dimension_px"):
        ConfigLoader(overall).load()


def test_parser_rejects_non_mapping_section():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {"vision_automation": []}

    with pytest.raises(ValueError, match="must be a mapping"):
        loader._parse_vision_automation_config()


@pytest.mark.parametrize("field_name", ["interval_seconds", "tick_timeout_seconds"])
@pytest.mark.parametrize("yaml_value", [".inf", "-.inf", ".nan"])
def test_loader_rejects_yaml_non_finite_numeric_values(
    tmp_path, monkeypatch, field_name, yaml_value
):
    import backend.config as config_module

    (tmp_path / "vision.yaml").write_text(
        f"vision_automation:\n  {field_name}: {yaml_value}\n", encoding="utf-8"
    )
    overall = tmp_path / "overall.yaml"
    overall.write_text("vision: vision.yaml\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)

    with pytest.raises(ValueError, match=field_name):
        ConfigLoader(overall).load()


@pytest.mark.asyncio
async def test_config_endpoint_uses_reloaded_object(monkeypatch):
    import backend.main as main

    original = ConfigLoader(PROJECT_ROOT / "config/config-overall.yaml").load()
    replacement = replace(
        original,
        vision_automation=replace(original.vision_automation, interval_seconds=2.5),
    )
    service = type("Service", (), {"active_backend": "vllm"})()
    monkeypatch.setattr(main, "reload_config", lambda: replacement)
    monkeypatch.setattr(main, "get_llm_service", lambda: service)

    response = await main.get_current_config()

    assert response["vision"]["automation"]["interval_seconds"] == 2.5
    assert response["vision"]["automation"]["lock_first_bundle"] is False
    assert response["vision"]["automation"]["max_encoded_payload_bytes"] == 134_217_728
    assert response["vision"]["automation"]["max_request_body_bytes"] == 142_606_336
