from fractions import Fraction

import pytest


def test_pixel_index_center_and_exact_isotropic_decode():
    from backend.computer_use.model_adapter import decode_axis

    assert decode_axis(0, size=800, mode="index_center") == Fraction(1, 1600)
    assert decode_axis(444, size=889, mode="relative_extent") == Fraction(444, 889)


@pytest.mark.parametrize(
    ("name", "semantics", "image_size", "wire"),
    [
        ("relative_1000", None, (1024, 1024), (1000, 1000)),
        ("custom_isotropic_1000", None, (1024, 1024), (1000, 1000)),
        ("image_pixel", "extent", (889, 1000), (889, 1000)),
        ("image_pixel", "index_center", (889, 1000), (888, 999)),
    ],
)
def test_stage2_descriptor_has_explicit_wire_domain(name, semantics, image_size, wire, grounding_request, crop):
    from backend.computer_use.contracts import ImageArtifact, ProtocolDescriptor
    from backend.computer_use.model_adapter import describe_stage

    req = grounding_request(protocol=ProtocolDescriptor(name=name, pixel_semantics=semantics))
    item = crop(grounding_request=req, detail_width=1024, detail_height=1024, model_width=1024, model_height=1024)
    image = ImageArtifact(png=b"png", width=image_size[0], height=image_size[1], content_hash="h", renderer_fingerprint="r")
    instance = describe_stage("stage2", image, item, req)
    assert (instance.wire_x_max, instance.wire_y_max) == wire


def test_stage2_decode_preserves_fraction_without_second_rounding(grounding_request, crop):
    from backend.computer_use.contracts import ImageArtifact, ProtocolDescriptor
    from backend.computer_use.model_adapter import decode_stage, describe_stage, encode_stage

    req = grounding_request(protocol=ProtocolDescriptor(name="custom_isotropic_1000"))
    item = crop(grounding_request=req, detail_width=1024, detail_height=1024, model_width=1024, model_height=1024)
    image = ImageArtifact(png=b"png", width=1024, height=1024, content_hash="h", renderer_fingerprint="r")
    encoded = encode_stage(describe_stage("stage2", image, item, req), image)
    decision = decode_stage(
        encoded,
        {"name": "interact_detail", "arguments": {"x": 444, "y": 500, "action": "left_click"}},
    )
    assert decision.x == Fraction(444)


@pytest.mark.parametrize(
    "raw",
    [
        {"name": "interact_detail", "arguments": {"crop_id": "wrong", "x": 1, "y": 1, "action": "left_click"}},
        {"name": "interact_detail", "arguments": {"crop_id": "c1", "x": True, "y": 1, "action": "left_click"}},
        {"name": "interact_detail", "arguments": {"crop_id": "c1", "x": float("nan"), "y": 1, "action": "left_click"}},
        {"name": "interact_detail", "arguments": {"x": -1, "y": 1, "action": "left_click"}},
        {"name": "interact_detail", "arguments": {"x": 1, "y": 1001, "action": "left_click"}},
        {"name": "interact_detail", "arguments": {"x": 1, "y": 1}},
        {"name": "interact_detail", "arguments": {"crop_id": "c1", "x": 1, "y": 1, "action": "right_click"}},
        {"name": "interact_detail", "arguments": {"crop_id": "c1", "x": 1, "y": 1, "action": "left_click", "extra": 1}},
    ],
)
def test_decode_rejects_wrong_ids_types_action_and_extra_fields(raw, grounding_request, crop):
    from backend.computer_use.contracts import ImageArtifact
    from backend.computer_use.errors import ComputerUseError
    from backend.computer_use.model_adapter import decode_stage, describe_stage, encode_stage

    req = grounding_request()
    item = crop(grounding_request=req, detail_width=1024, detail_height=1024, model_width=1024, model_height=1024)
    image = ImageArtifact(png=b"png", width=1024, height=1024, content_hash="h", renderer_fingerprint="r")
    encoded = encode_stage(describe_stage("stage2", image, item, req), image)
    with pytest.raises(ComputerUseError):
        decode_stage(encoded, raw)


def test_stage1_decode_and_reobserve_are_typed(grounding_request, crop):
    from backend.computer_use.contracts import ImageArtifact, InspectRegion, Reobserve
    from backend.computer_use.model_adapter import decode_stage, describe_stage, encode_stage

    req = grounding_request()
    image = ImageArtifact(png=b"png", width=3840, height=3840, content_hash="h", renderer_fingerprint="r")
    stage1 = encode_stage(describe_stage("stage1", image, None, req), image)
    decision = decode_stage(stage1, {"name": "inspect_region", "arguments": {"x": 500, "y": 250, "zoom": 4}})
    assert decision == InspectRegion(x=Fraction(500), y=Fraction(250), zoom=4)
    center = decode_stage(stage1, {"name": "inspect_region", "arguments": {"x": 500, "y": 500, "zoom": 4}})
    assert center == InspectRegion(x=Fraction(500), y=Fraction(500), zoom=4)

    item = crop(grounding_request=req, detail_width=1024, detail_height=1024, model_width=1024, model_height=1024)
    detail_image = ImageArtifact(png=b"png", width=1024, height=1024, content_hash="h", renderer_fingerprint="r")
    stage2 = encode_stage(describe_stage("stage2", detail_image, item, req), detail_image)
    assert decode_stage(stage2, {"name": "reobserve", "arguments": {"reason": "target_ambiguous"}}) == Reobserve("target_ambiguous")


def test_stage1_prompt_uses_axis_fractions_for_full_square_overview(grounding_request):
    from backend.computer_use.contracts import ImageArtifact
    from backend.computer_use.model_adapter import describe_stage, encode_stage

    image = ImageArtifact(png=b"png", width=3840, height=3840, content_hash="h", renderer_fingerprint="r")
    encoded = encode_stage(describe_stage("stage1", image, None, grounding_request()), image)
    prompt = encoded.messages[0]["content"][0]["text"]

    assert "each displayed axis spans 0..1000" in prompt.lower()
    assert "stretched to this square" in prompt.lower()
    assert "eight-division grid" in prompt.lower()
    assert "complete square image" in prompt.lower()
    assert "divided into sixteen equal cells" not in prompt.lower()
    assert "square" in prompt.lower()
    assert "stretched" in prompt.lower()


def test_stage1_image_pixel_extent_prompt_keeps_pixel_coordinates(grounding_request):
    from backend.computer_use.contracts import ImageArtifact, ProtocolDescriptor
    from backend.computer_use.model_adapter import describe_stage, encode_stage

    request = grounding_request(protocol=ProtocolDescriptor(name="image_pixel", pixel_semantics="extent"))
    image = ImageArtifact(png=b"png", width=1000, height=563, content_hash="h", renderer_fingerprint="r")
    encoded = encode_stage(describe_stage("stage1", image, None, request), image)
    prompt = encoded.messages[0]["content"][0]["text"]

    assert "Return image pixel extent coordinates directly" in prompt
    assert "x spans 0..1000 across the visible image" in prompt
    assert "y spans 0..563" in prompt
    assert "do not normalize either axis" in prompt
    assert "Return normalized position fractions" not in prompt
    assert "zoom=4" in prompt


def test_stage2_square_coordinates_map_each_axis_to_its_physical_baseline(grounding_request, crop):
    from backend.computer_use.contracts import ImageArtifact, ProtocolDescriptor
    from backend.computer_use.coordinates import local_to_monitor
    from backend.computer_use.model_adapter import decode_stage, describe_stage, encode_stage

    req = grounding_request(protocol=ProtocolDescriptor(name="custom_isotropic_1000"))
    item = crop(
        grounding_request=req,
        source_width=3840,
        source_height=2400,
        x0=960,
        y0=600,
        x1=2880,
        y1=1800,
        detail_width=1024,
        detail_height=1024,
        local_width=Fraction(1000),
        local_height=Fraction(1000),
        model_width=1024,
        model_height=1024,
    )
    image = ImageArtifact(png=b"png", width=1024, height=1024, content_hash="h", renderer_fingerprint="r")
    encoded = encode_stage(describe_stage("stage2", image, item, req), image)
    decision = decode_stage(encoded, {
        "name": "interact_detail",
        "arguments": {"x": 500, "y": 500, "action": "left_click"},
    })

    assert (decision.x, decision.y) == (Fraction(500), Fraction(500))
    assert local_to_monitor(item, decision.x, decision.y) == (Fraction(1, 2), Fraction(1, 2))


@pytest.mark.parametrize("protocol_name", ["relative_1000", "custom_isotropic_1000"])
def test_stage2_edge_normalized_axes_ignore_crop_pixel_aspect(protocol_name, grounding_request, crop):
    from backend.computer_use.contracts import ImageArtifact, ProtocolDescriptor
    from backend.computer_use.coordinates import local_to_monitor
    from backend.computer_use.model_adapter import decode_stage, describe_stage, encode_stage

    req = grounding_request(protocol=ProtocolDescriptor(name=protocol_name))
    item = crop(
        grounding_request=req,
        source_width=3840,
        source_height=2400,
        x0=960,
        y0=0,
        x1=2880,
        y1=624,
        detail_width=1024,
        detail_height=1024,
        local_width=Fraction(1000),
        local_height=Fraction(1000),
        model_width=1024,
        model_height=1024,
    )
    image = ImageArtifact(png=b"png", width=1024, height=1024, content_hash="h", renderer_fingerprint="r")
    encoded = encode_stage(describe_stage("stage2", image, item, req), image)

    assert (encoded.instance.wire_x_max, encoded.instance.wire_y_max) == (1000, 1000)
    assert encoded.instance.label_formatter.format_axis("x", Fraction(125)) == "x=125"
    assert encoded.instance.label_formatter.format_axis("y", Fraction(125)) == "y=125"
    assert encoded.tools[0]["parameters"]["properties"]["y"]["maximum"] == 1000
    assert "x and y each span 0..1000" in encoded.messages[0]["content"][0]["text"].lower()
    prompt = encoded.messages[0]["content"][0]["text"].lower()
    assert "normalized grid" in prompt
    decision = decode_stage(encoded, {
        "name": "interact_detail",
        "arguments": {"x": 500, "y": 500, "action": "left_click"},
    })
    assert (decision.x, decision.y) == (Fraction(500), Fraction(500))
    assert local_to_monitor(item, decision.x, decision.y) == (Fraction(1, 2), Fraction(13, 100))


@pytest.mark.parametrize("protocol_name", ["relative_1000", "custom_isotropic_1000"])
def test_live_relative_stage1_rejects_nonsquare_model_image(protocol_name, grounding_request):
    from backend.computer_use.contracts import ImageArtifact, ProtocolDescriptor
    from backend.computer_use.model_adapter import describe_stage
    from backend.computer_use.errors import ComputerUseError

    request = grounding_request(protocol=ProtocolDescriptor(name=protocol_name))
    image = ImageArtifact(png=b"png", width=3840, height=2400, content_hash="h", renderer_fingerprint="r")

    with pytest.raises(ComputerUseError, match="square"):
        describe_stage("stage1", image, None, request)


@pytest.mark.parametrize(
    ("image_size", "model_size"),
    [((3840, 2400), (3840, 3840)), ((3840, 3840), (1920, 1920))],
)
def test_live_relative_stage2_requires_square_image_matching_crop_model_size(image_size, model_size, grounding_request, crop):
    from backend.computer_use.contracts import ImageArtifact, ProtocolDescriptor
    from backend.computer_use.errors import ComputerUseError
    from backend.computer_use.model_adapter import describe_stage

    request = grounding_request(protocol=ProtocolDescriptor(name="relative_1000"))
    item = crop(
        grounding_request=request,
        detail_width=model_size[0],
        detail_height=model_size[1],
        model_width=model_size[0],
        model_height=model_size[1],
    )
    image = ImageArtifact(png=b"png", width=image_size[0], height=image_size[1], content_hash="h", renderer_fingerprint="r")

    with pytest.raises(ComputerUseError, match="square|crop model"):
        describe_stage("stage2", image, item, request)


def test_live_square_prompts_use_independent_fractions_and_hide_crop_identity(grounding_request, crop):
    from backend.computer_use.contracts import ImageArtifact
    from backend.computer_use.model_adapter import describe_stage, encode_stage

    stage1_image = ImageArtifact(png=b"png", width=3840, height=3840, content_hash="h", renderer_fingerprint="r")
    stage1 = encode_stage(describe_stage("stage1", stage1_image, None, grounding_request()), stage1_image)
    stage2_image = ImageArtifact(png=b"png", width=1024, height=1024, content_hash="h", renderer_fingerprint="r")
    item = crop(grounding_request=grounding_request(), detail_width=1024, detail_height=1024, model_width=1024, model_height=1024)
    stage2 = encode_stage(describe_stage("stage2", stage2_image, item, grounding_request()), stage2_image)

    stage1_prompt = stage1.messages[0]["content"][0]["text"].lower()
    stage2_prompt = stage2.messages[0]["content"][0]["text"].lower()
    assert "stretched" in stage1_prompt and "square" in stage1_prompt
    assert "square detail image" in stage2_prompt
    assert "each displayed axis spans 0..1000" in stage1_prompt
    assert "x and y each span 0..1000" in stage2_prompt
    assert "4x crop containing the whole target" in stage1_prompt
    assert "precise center" in stage2_prompt
    for prompt in (stage1_prompt, stage2_prompt):
        assert "3840" not in prompt
    assert "crop_id" not in stage2_prompt
    assert "crop_id" not in stage2.tools[0]["parameters"]["properties"]
    assert tuple(stage2.tools[0]["parameters"]["required"]) == ("x", "y", "action")


@pytest.mark.parametrize("placement", ["edge", "repeated"])
def test_gridless_stage2_prompt_uses_plain_normalized_square_coordinates(grounding_request, crop, placement):
    from backend.computer_use.contracts import ImageArtifact
    from backend.computer_use.model_adapter import describe_stage, encode_stage

    request = grounding_request()
    item = crop(
        grounding_request=request,
        detail_width=1024,
        detail_height=1024,
        model_width=1024,
        model_height=1024,
        grid_enabled=False,
    )
    image = ImageArtifact(png=b"png", width=1024, height=1024, content_hash="h", renderer_fingerprint="r")
    encoded = encode_stage(describe_stage("stage2", image, item, request,
                                          grid_label_placement=placement), image)
    prompt = encoded.messages[0]["content"][0]["text"].lower()

    assert "no grid or tick labels" in prompt
    assert "x and y each span 0..1000" in prompt
    assert "precise center" in prompt
    assert "0,125,250,375,500,625,750,875,1000" not in prompt
    assert "x labels repeat" not in prompt
    assert "y labels repeat" not in prompt


def test_gridded_stage2_prompt_describes_sixteen_divisions_and_edge_labels(grounding_request, crop):
    from backend.computer_use.contracts import ImageArtifact
    from backend.computer_use.model_adapter import describe_stage, encode_stage

    request = grounding_request()
    item = crop(
        grounding_request=request,
        detail_width=1024,
        detail_height=1024,
        model_width=1024,
        model_height=1024,
        grid_enabled=True,
    )
    image = ImageArtifact(png=b"png", width=1024, height=1024, content_hash="h", renderer_fingerprint="r")
    encoded = encode_stage(describe_stage("stage2", image, item, request, grid_label_placement="edge"), image)
    prompt = encoded.messages[0]["content"][0]["text"].lower()

    assert "normalized grid" in prompt
    assert "x and y each span 0..1000" in prompt
    assert "repeated sixteen times" not in prompt


def test_concise_prompts_remain_valid_for_both_grid_label_placements(grounding_request, crop):
    from backend.computer_use.contracts import ImageArtifact
    from backend.computer_use.model_adapter import describe_stage, encode_stage

    stage1_image = ImageArtifact(png=b"png", width=3840, height=3840, content_hash="h", renderer_fingerprint="r")
    stage1 = encode_stage(describe_stage("stage1", stage1_image, None, grounding_request(), grid_label_placement="repeated"), stage1_image)
    stage1_prompt = stage1.messages[0]["content"][0]["text"].lower()
    assert "eight-division grid" in stage1_prompt

    request = grounding_request()
    item = crop(grounding_request=request, detail_width=1024, detail_height=1024, model_width=1024, model_height=1024)
    stage2_image = ImageArtifact(png=b"png", width=1024, height=1024, content_hash="h", renderer_fingerprint="r")
    stage2 = encode_stage(describe_stage("stage2", stage2_image, item, request, grid_label_placement="repeated"), stage2_image)
    stage2_prompt = stage2.messages[0]["content"][0]["text"].lower()
    assert "normalized grid" in stage2_prompt


def test_stage1_default_prompt_uses_square_normalized_grid(grounding_request):
    from backend.computer_use.contracts import ImageArtifact
    from backend.computer_use.model_adapter import describe_stage, encode_stage

    image = ImageArtifact(png=b"png", width=3840, height=3840, content_hash="h", renderer_fingerprint="r")
    prompt = encode_stage(describe_stage("stage1", image, None, grounding_request()), image).messages[0]["content"][0]["text"].lower()
    assert "eight-division grid" in prompt
    assert "each displayed axis spans 0..1000" in prompt


def test_grid_label_placement_is_validated_and_bound_to_protocol(grounding_request):
    from backend.computer_use.contracts import ImageArtifact
    from backend.computer_use.model_adapter import describe_stage

    image = ImageArtifact(png=b"png", width=3840, height=3840, content_hash="h", renderer_fingerprint="r")
    edge = describe_stage("stage1", image, None, grounding_request(), grid_label_placement="edge")
    repeated = describe_stage("stage1", image, None, grounding_request(), grid_label_placement="repeated")

    assert edge.protocol_fingerprint != repeated.protocol_fingerprint
    with pytest.raises(ValueError, match="grid_label_placement"):
        describe_stage("stage1", image, None, grounding_request(), grid_label_placement="center")


def test_stage1_prompt_schema_and_decoder_fix_zoom_at_four(grounding_request):
    from backend.computer_use.contracts import ImageArtifact, InspectRegion
    from backend.computer_use.errors import ComputerUseError
    from backend.computer_use.model_adapter import decode_stage, describe_stage, encode_stage

    image = ImageArtifact(png=b"png", width=3840, height=3840, content_hash="h", renderer_fingerprint="r")
    encoded = encode_stage(describe_stage("stage1", image, None, grounding_request()), image)
    prompt = encoded.messages[0]["content"][0]["text"].lower()
    zoom_schema = encoded.tools[0]["parameters"]["properties"]["zoom"]

    assert "zoom=4" in prompt
    assert "lowest sufficient zoom" not in prompt
    assert zoom_schema["const"] == 4
    assert tuple(encoded.tools[0]["parameters"]["required"]) == ("x", "y", "zoom")
    assert decode_stage(encoded, {"name": "inspect_region", "arguments": {"x": 500, "y": 500, "zoom": 4}}) == InspectRegion(Fraction(500), Fraction(500), 4)
    with pytest.raises(ComputerUseError) as exc_info:
        decode_stage(encoded, {"name": "inspect_region", "arguments": {"x": 500, "y": 500, "zoom": 2}})
    assert exc_info.value.code == "INVALID_ZOOM"


def test_stage2_schema_exposes_typed_reobserve_for_absent_target(grounding_request, crop):
    from backend.computer_use.contracts import ImageArtifact, ProtocolDescriptor, Reobserve
    from backend.computer_use.errors import ComputerUseError
    from backend.computer_use.model_adapter import decode_stage, describe_stage, encode_stage

    request = grounding_request(protocol=ProtocolDescriptor(name="relative_1000"))
    item = crop(grounding_request=request, detail_width=1024, detail_height=1024, model_width=1024, model_height=1024)
    image = ImageArtifact(png=b"png", width=1024, height=1024, content_hash="h", renderer_fingerprint="r")
    encoded = encode_stage(describe_stage("stage2", image, item, request), image)
    prompt = encoded.messages[0]["content"][0]["text"].lower()
    tool_names = tuple(tool["name"] for tool in encoded.tools)
    reobserve_tool = next(tool for tool in encoded.tools if tool["name"] == "reobserve")

    assert "absent, or ambiguous" in prompt
    assert "reobserve(reason=target_not_visible)" in prompt
    assert "truly cut off by an image edge" in prompt
    assert "background separates the target from all four image edges" in prompt
    assert tool_names == ("interact_detail", "reobserve")
    assert "target_not_visible" in reobserve_tool["parameters"]["properties"]["reason"]["enum"]
    assert decode_stage(encoded, {"name": "reobserve", "arguments": {"reason": "target_not_visible"}}) == Reobserve("target_not_visible")
    with pytest.raises(ComputerUseError):
        decode_stage(encoded, {"name": "reobserve", "arguments": {}})
    for malformed_reason in ("", None, [], {}):
        with pytest.raises(ComputerUseError):
            decode_stage(encoded, {"name": "reobserve", "arguments": {"reason": malformed_reason}})


def test_live_stage2_binds_trusted_crop_after_strict_model_fields(grounding_request, crop):
    from backend.computer_use.contracts import ImageArtifact, ProtocolDescriptor
    from backend.computer_use.errors import ComputerUseError
    from backend.computer_use.model_adapter import decode_stage, describe_stage, encode_stage

    request = grounding_request(protocol=ProtocolDescriptor(name="relative_1000"))
    item = crop(grounding_request=request, detail_width=1024, detail_height=1024, model_width=1024, model_height=1024)
    image = ImageArtifact(png=b"png", width=1024, height=1024, content_hash="h", renderer_fingerprint="r")
    encoded = encode_stage(describe_stage("stage2", image, item, request), image)

    decision = decode_stage(encoded, {"name": "interact_detail", "arguments": {"x": 500, "y": 500, "action": "left_click"}})
    assert decision.crop_id == item.crop_id
    assert (decision.x, decision.y) == (Fraction(500), Fraction(500))
    with pytest.raises(ComputerUseError):
        decode_stage(encoded, {"name": "interact_detail", "arguments": {"crop_id": item.crop_id, "x": 500, "y": 500, "action": "left_click"}})


@pytest.mark.parametrize(
    ("protocol_name", "pixel_semantics"),
    [("relative_1000", None), ("image_pixel", "extent")],
)
def test_unknown_image_preprocessing_mapping_fails_closed(protocol_name, pixel_semantics, grounding_request):
    from backend.computer_use.contracts import ImageArtifact, ProtocolDescriptor
    from backend.computer_use.errors import ComputerUseError
    from backend.computer_use.model_adapter import describe_stage

    protocol = ProtocolDescriptor(
        name=protocol_name,
        pixel_semantics=pixel_semantics,
        preprocessing={"mapping": "crop"},
    )
    request = grounding_request(protocol=protocol)
    image = ImageArtifact(png=b"png", width=1000, height=1000, content_hash="h", renderer_fingerprint="r")

    with pytest.raises(ComputerUseError) as exc_info:
        describe_stage("stage1", image, None, request)

    assert exc_info.value.code == "CALIBRATION_REQUIRED"
