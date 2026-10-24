from __future__ import annotations

import dataclasses
import json
import math
from fractions import Fraction

import pytest


def test_default_disabled_and_request_identity_is_immutable(settings, grounding_request):
    assert settings().enabled is False
    item = grounding_request()
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.request_id = "different"


def test_nested_evidence_is_defensively_deep_frozen(binding):
    from backend.computer_use.contracts import InputBinding

    source = {"ids": ["one"], "nested": {"verified": True}}
    item = InputBinding(**{**dataclasses.asdict(binding), "mapping_evidence": source})
    source["ids"].append("two")
    source["nested"]["verified"] = False
    assert item.mapping_evidence["ids"] == ("one",)
    assert item.mapping_evidence["nested"]["verified"] is True
    with pytest.raises(TypeError):
        item.mapping_evidence["new"] = 1


def test_frame_owns_bytes_and_computes_age(frame):
    source = bytearray([1, 2, 3] * 4)
    frozen = frame(width=2, height=2, rgb=source, captured_monotonic=10.0)
    source[:] = bytes(len(source))
    assert frozen.rgb[:3] == b"\x01\x02\x03"
    assert frozen.age(10.25) == pytest.approx(0.25)


@pytest.mark.parametrize("bad", [0, -1, True, 1.2, math.nan, math.inf])
def test_frame_rejects_invalid_dimensions(frame, bad):
    with pytest.raises((TypeError, ValueError)):
        frame(width=bad)


def test_receipt_json_keeps_unknown_distinct_from_success():
    from backend.computer_use.contracts import DispatchReceipt

    receipt = DispatchReceipt(
        request_id="r1",
        crop_id="c1",
        state="ACTION_DELIVERY_UNKNOWN",
        last_possible_event=4,
        cleanup_status="release_uncertain",
    )
    payload = json.loads(receipt.to_json())
    assert payload["state"] == "ACTION_DELIVERY_UNKNOWN"
    assert "success" not in payload


def test_named_freshness_constructors_have_uppercase_states():
    from backend.computer_use.contracts import FreshnessResult

    assert FreshnessResult.indeterminate("NO_LIVENESS").state == "INDETERMINATE"
    assert FreshnessResult.stale("TOO_OLD").state == "STALE"


def test_detail_crop_uses_independent_axis_baselines_for_rectangular_source(crop):
    item = crop()
    assert (item.local_width, item.local_height) == (Fraction(1000), Fraction(1000))
    assert item.exact_crop_aspect == Fraction(8, 9)
    assert item.model_scale_x == Fraction(32, 25)
    assert item.model_scale_y == Fraction(256, 225)


@pytest.mark.parametrize("dimensions", [
    {"detail_height": 625},
    {"model_height": 625},
    {"model_width": 1536},
])
def test_detail_crop_rejects_rectangular_or_mismatched_model_canvas(crop, dimensions):
    with pytest.raises(ValueError, match="square"):
        crop(**dimensions)


def test_detail_crop_rejects_nonindependent_local_axis(crop):
    with pytest.raises(ValueError, match="both local axes"):
        crop(local_height=Fraction(625), detail_height=625, model_height=625)


@pytest.mark.parametrize("action", ["click", "drag", "LEFT_CLICK", True, 3])
def test_request_rejects_unknown_actions(grounding_request, action):
    with pytest.raises((TypeError, ValueError)):
        grounding_request(action=action)


def test_config_defaults_are_fail_closed_and_require_calibrated_values(settings):
    config = settings()
    assert config.enabled is False
    assert config.protocol == "relative_1000"
    assert config.grid_divisions == 16
    assert config.grid_label_placement == "edge"
    assert config.model_square_side_px == 1024
    assert config.inference_timeout_seconds == 120
    assert config.max_control_packet_bytes == 65536
    assert config.max_received_fds == 1
    assert config.max_canonical_frame_bytes == 268435456
    assert config.ready is False


def test_config_ready_only_with_all_safety_values(settings):
    config = settings(
        enabled=True,
        calibration_fingerprint="fixture-fingerprint",
        max_observation_age_seconds=10,
        max_sample_age_seconds=1,
        max_validation_to_dispatch_seconds=0.1,
        global_diff_threshold=0.01,
        target_diff_threshold=0.01,
        double_click_interval_seconds=0.1,
    )
    assert config.ready is True


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("model_square_side_px", True),
        ("model_square_side_px", 0),
        ("model_square_side_px", -8),
        ("model_square_side_px", 1000),
        ("model_square_side_px", 1001),
        ("model_square_side_px", 1500),
        ("model_square_side_px", 1000.0),
        ("grid_divisions", 3),
        ("grid_divisions", 8),
        ("grid_divisions", 10),
        ("inference_timeout_seconds", math.nan),
        ("global_diff_threshold", math.inf),
        ("max_received_fds", 0),
    ],
)
def test_config_rejects_invalid_values(settings, field, bad):
    with pytest.raises((TypeError, ValueError)):
        settings(**{field: bad})


def test_live_config_rejects_legacy_pixel_wire_protocol(settings):
    with pytest.raises(ValueError, match="offline"):
        settings(protocol="image_pixel", pixel_semantics="extent")


def test_config_rejects_unknown_fields():
    from backend.computer_use.config import ComputerUseConfig

    with pytest.raises(ValueError, match="unknown computer_use config"):
        ComputerUseConfig.from_mapping({"enabled": False, "mystery": 1})


@pytest.mark.parametrize("side", [1024, 1536, 3840])
def test_config_accepts_sixteen_divisible_square_side(settings, side):
    assert settings(model_square_side_px=side).model_square_side_px == side


def test_grid_label_placement_accepts_only_edge_or_repeated(settings):
    assert settings(grid_label_placement="edge").grid_label_placement == "edge"
    assert settings(grid_label_placement="repeated").grid_label_placement == "repeated"
    for invalid in ("middle", "", None, True, 1):
        with pytest.raises((TypeError, ValueError)):
            settings(grid_label_placement=invalid)


def test_detail_crop_requires_sixteen_grid_cells_and_divisible_square_side(crop):
    assert crop(grid_divisions=16).grid_divisions == 16
    with pytest.raises(ValueError, match="sixteen"):
        crop(grid_divisions=10)
    with pytest.raises(ValueError, match="divisible"):
        crop(grid_divisions=16, detail_width=1000, detail_height=1000,
             model_width=1000, model_height=1000)


@pytest.mark.parametrize("legacy", ["overview_long_edge_px", "detail_long_edge_px"])
def test_config_rejects_legacy_long_edge_key(settings, legacy):
    with pytest.raises(ValueError, match="unknown computer_use config"):
        settings(**{legacy: 1000})
