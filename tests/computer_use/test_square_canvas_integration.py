"""The Stage-2 square canvas must not change source crop or input ratio."""

from fractions import Fraction

import pytest


@pytest.mark.parametrize("side", [1280, 1680, 2048])
def test_edge_crop_square_model_canvas_preserves_independent_source_axes(
    frame, grounding_request, settings, side
):
    from backend.computer_use.contracts import InspectRegion
    from backend.computer_use.coordinates import local_to_monitor
    from backend.computer_use.model_adapter import decode_stage, describe_stage, encode_stage
    from backend.computer_use.runtime import GroundingRenderer

    source = frame(width=3840, height=2400, rgb=bytes((12, 34, 56)) * (3840 * 2400))
    request = grounding_request()
    config = settings(model_square_side_px=side)
    renderer = GroundingRenderer(clock=lambda: 100.02)

    overview = renderer.overview(source, config)
    assert (overview.width, overview.height) == (side, side)
    crop = renderer.crop(source, InspectRegion(500, 10, 4), request, config)
    detail = renderer.detail(crop, config)
    assert (crop.x0, crop.y0, crop.x1, crop.y1) == (1440, 0, 2400, 324)
    assert (detail.width, detail.height) == (side, side)
    assert crop.frame_id == source.frame_id
    assert crop.observation_id == source.observation_id
    assert crop.binding_id == source.binding.binding_id
    assert crop.geometry_version == source.geometry_version

    encoded = encode_stage(describe_stage("stage2", detail, crop, request), detail)
    decision = decode_stage(encoded, {
        "name": "interact_detail",
        "arguments": {"x": 500, "y": 500, "action": "left_click"},
    })
    assert decision.crop_id == crop.crop_id
    assert (decision.x, decision.y) == (Fraction(500), Fraction(500))
    assert local_to_monitor(crop, decision.x, decision.y) == (Fraction(1, 2), Fraction(27, 400))
