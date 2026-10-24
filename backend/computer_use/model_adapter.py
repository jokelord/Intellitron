from __future__ import annotations

import hashlib
import json
import math
import base64
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Mapping

from .contracts import (
    Action,
    CoordinateProtocol,
    DetailCrop,
    EncodedStage,
    GroundingRequest,
    ImageArtifact,
    InspectRegion,
    InteractDetail,
    LabelFormatter,
    ProtocolInstance,
    Reobserve,
    StageDecision,
    exact_number,
    thaw,
)
from decimal import Decimal

from .coordinates import half_up_positive
from .errors import ComputerUseError


def provider_messages_image_first(encoded: EncodedStage) -> list[dict]:
    """Match the production Agent adapter's image-before-text wire ordering."""
    messages = thaw(encoded.messages)
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "image_url" for part in content
        ):
            images = [part for part in content if isinstance(part, dict) and part.get("type") == "image_url"]
            others = [part for part in content if not (isinstance(part, dict) and part.get("type") == "image_url")]
            message["content"] = images + others
    return messages


def _wire_number(value: Any, *, integer: bool, field: str) -> Fraction:
    try:
        number = exact_number(value, field)
    except (TypeError, ValueError) as exc:
        raise ComputerUseError("INVALID_MODEL_OUTPUT", str(exc), field=field) from exc
    if integer and number.denominator != 1:
        raise ComputerUseError("INVALID_MODEL_OUTPUT", "integer protocol requires an integral value", field=field)
    return number


def decode_axis(raw: Any, *, size: int, mode: str) -> Fraction:
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("size must be a positive integer")
    number = _wire_number(raw, integer=(mode == "index_center"), field="axis")
    if mode == "relative_extent":
        if not 0 <= number <= size:
            raise ComputerUseError("INVALID_MODEL_OUTPUT", f"axis must be within [0,{size}]", field="axis")
        return number / size
    if mode == "index_center":
        if not 0 <= number < size:
            raise ComputerUseError("INVALID_MODEL_OUTPUT", f"pixel index must be within [0,{size - 1}]", field="axis")
        return (number + Fraction(1, 2)) / size
    raise ValueError(f"unknown axis mode: {mode}")


@dataclass(frozen=True)
class _ProtocolLabels(LabelFormatter):
    x_limit: Fraction
    y_limit: Fraction
    wire_x_max: int
    wire_y_max: int
    mode: str
    grid_label_placement: str
    normalized_decimal: bool = False

    def _limit(self, axis: str) -> tuple[Fraction, int]:
        if axis == "x":
            return self.x_limit, self.wire_x_max
        if axis == "y":
            return self.y_limit, self.wire_y_max
        raise ValueError("axis must be x or y")

    def format_axis(self, axis: str, canonical_value: Fraction) -> str:
        limit, wire_max = self._limit(axis)
        ratio = canonical_value / limit
        if self.mode == "index_center":
            pixel_count = wire_max + 1
            value = min(wire_max, max(0, half_up_positive(ratio * pixel_count - Fraction(1, 2))))
        elif self.normalized_decimal:
            value = ratio * wire_max
            text = (str(value.numerator) if value.denominator == 1 else
                    format(Decimal(value.numerator) / Decimal(value.denominator), "f").rstrip("0").rstrip("."))
            return f"{axis}={text}"
        else:
            value = half_up_positive(ratio * wire_max)
        return f"{axis}={value}"

    def boundary_ticks(self, axis: str) -> tuple[tuple[Fraction, str], ...]:
        limit, wire_max = self._limit(axis)
        return ((Fraction(0), f"{axis}=0"), (limit, f"{axis}={wire_max}"))


def _instance_fingerprint(stage: str, request: GroundingRequest, image: ImageArtifact, crop: DetailCrop | None, wire: tuple[int, int], mode: str, grid_label_placement: str, stage1_zoom: int) -> str:
    payload = {
        "stage": stage,
        "protocol": request.protocol.name.value,
        "pixel_semantics": request.protocol.pixel_semantics,
        "output_type": request.protocol.output_type,
        "adapter_version": request.protocol.adapter_version,
        "preprocessing": dict(request.protocol.preprocessing),
        "image": [image.width, image.height, image.renderer_fingerprint],
        "wire": list(wire),
        "mode": mode,
        "grid_label_placement": grid_label_placement,
        "offline_fixture_zoom": stage1_zoom,
        "allowed_zooms": [2, 4, 6, 8],
        "default_zoom": 4,
        "grid_label_policy": "stage1_none_stage2_even_values_shared_orthogonal_shift31_25_v6",
        "detail_recovery_policy": "current_detail_point_then_interact_no_stage2_rough_recovery_tail_v5",
        "scroll_steps_policy": "canonical_directions_one_transaction_steps_1_to_5_v1",
        "crop": crop.crop_id if crop else None,
        "calibration": request.calibration_fingerprint,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def describe_stage(
    stage: str,
    image: ImageArtifact,
    crop: DetailCrop | None,
    request: GroundingRequest,
    *,
    grid_label_placement: str = "edge",
    stage1_zoom: int = 4,
) -> ProtocolInstance:
    if isinstance(stage1_zoom, bool) or not isinstance(stage1_zoom, int) or stage1_zoom not in (2, 4, 6, 8):
        raise ValueError("stage1_zoom must be 2, 4, 6 or 8")
    if stage not in {"stage1", "stage2"}:
        raise ValueError("stage must be stage1 or stage2")
    if not isinstance(grid_label_placement, str) or grid_label_placement not in {"edge", "repeated"}:
        raise ValueError("grid_label_placement must be edge or repeated")
    if stage == "stage1" and crop is not None:
        raise ValueError("stage1 must not bind a crop")
    if stage == "stage2":
        if crop is None:
            raise ValueError("stage2 requires a crop")
        if crop.request_id != request.request_id or crop.session_id != request.session_id or crop.binding_id != request.binding_id:
            raise ComputerUseError("INVALID_CROP_ID", "crop is not owned by this request")
        if crop.calibration_fingerprint != request.calibration_fingerprint:
            raise ComputerUseError("CALIBRATION_REQUIRED", "crop calibration fingerprint differs from request")
    protocol = request.protocol.name
    if request.protocol.preprocessing and request.protocol.preprocessing.get("mapping") not in (None, "identity"):
        raise ComputerUseError("CALIBRATION_REQUIRED", "unknown image preprocessing inverse mapping")
    if protocol in {CoordinateProtocol.RELATIVE_1000, CoordinateProtocol.CUSTOM_ISOTROPIC_1000}:
        if stage == "stage1" and image.width != image.height:
            raise ComputerUseError("CALIBRATION_REQUIRED", "live Stage-1 model image must be square")
        if stage == "stage2":
            assert crop is not None
            if image.width != image.height:
                raise ComputerUseError("CALIBRATION_REQUIRED", "live Stage-2 model image must be square")
            if crop.model_width != crop.model_height:
                raise ComputerUseError("CALIBRATION_REQUIRED", "live Stage-2 crop model canvas must be square")
            if (image.width, image.height) != (crop.model_width, crop.model_height):
                raise ComputerUseError("CALIBRATION_REQUIRED", "Stage-2 image dimensions differ from the trusted crop model canvas")
    if protocol in {CoordinateProtocol.RELATIVE_1000, CoordinateProtocol.CUSTOM_ISOTROPIC_1000}:
        wire = (1000, 1000)
        mode = "relative_extent"
    else:
        if request.protocol.pixel_semantics == "extent":
            wire = (image.width, image.height)
            mode = "relative_extent"
        else:
            wire = (image.width - 1, image.height - 1)
            mode = "index_center"
    x_limit = Fraction(1000) if stage == "stage1" else crop.local_width  # type: ignore[union-attr]
    y_limit = Fraction(1000) if stage == "stage1" else crop.local_height  # type: ignore[union-attr]
    formatter = _ProtocolLabels(x_limit, y_limit, wire[0], wire[1], mode, grid_label_placement,
        request.protocol.name in {CoordinateProtocol.RELATIVE_1000, CoordinateProtocol.CUSTOM_ISOTROPIC_1000})
    fingerprint = _instance_fingerprint(stage, request, image, crop, wire, mode, grid_label_placement, stage1_zoom)
    return ProtocolInstance(
        stage=stage,
        descriptor=request.protocol,
        protocol_fingerprint=fingerprint,
        wire_x_max=wire[0],
        wire_y_max=wire[1],
        axis_mode=mode,
        image_width=image.width,
        image_height=image.height,
        request=request,
        crop=crop,
        label_formatter=formatter,
        stage1_zoom=stage1_zoom,
    )


def _number_schema(instance: ProtocolInstance, axis: str) -> dict[str, Any]:
    limit = instance.wire_x_max if axis == "x" else instance.wire_y_max
    schema: dict[str, Any] = {"minimum": 0, "maximum": limit}
    schema["type"] = "integer" if instance.descriptor.output_type == "integer" or instance.axis_mode == "index_center" else "number"
    return schema


def _grid_description(stage: str, placement: str) -> str:
    if stage == "stage1":
        return "Both axes have eight equal cells with translucent grid lines and a border, without text labels."
    grid = (
        "Both axes have sixteen equal cells. Only even interior guides carry normalized labels "
        "125,250,375,500,625,750,875; odd guides are not labeled with their own values. "
    )
    labels = (
        "X labels appear beside the top and bottom ends of their vertical guides; "
        "y labels appear beside the left and right ends of their horizontal guides. "
        if placement == "edge" else
        "Each x label repeats at y=31.25,156.25,281.25,406.25,531.25,656.25,781.25,906.25, just right of its vertical "
        "guide with its text vertically centered on each y anchor. Each y label repeats at x=31.25,156.25,281.25,406.25,531.25,656.25,781.25,906.25, "
        "with its text horizontally centered on each x anchor and just below its horizontal guide. There are eight copies per labeled value, "
        "56 x labels and 56 y labels. "
    )
    return grid + labels + "X labels rotate 90 degrees counterclockwise; y labels stay horizontal. Lines and labels use 0.5 opacity."


def encode_stage(instance: ProtocolInstance, image: ImageArtifact) -> EncodedStage:
    if (image.width, image.height) != (instance.image_width, instance.image_height):
        raise ComputerUseError("CALIBRATION_REQUIRED", "encoded image dimensions differ from described image")
    if instance.descriptor.name in {CoordinateProtocol.RELATIVE_1000, CoordinateProtocol.CUSTOM_ISOTROPIC_1000}:
        visible_image = "the complete screenshot stretched along each axis into a square, then overlaid with a translucent coordinate grid" if instance.stage == "stage1" else "a square model image"
        protocol_text = (
            f"Coordinate protocol: {instance.descriptor.name.value}; visible image is {visible_image}; "
            f"valid normalized x 0..{instance.wire_x_max}, y 0..{instance.wire_y_max}."
        )
    else:
        protocol_text = (
            f"Coordinate protocol: {instance.descriptor.name.value}; visible image {image.width}x{image.height}; "
            f"valid x 0..{instance.wire_x_max}, y 0..{instance.wire_y_max}; {instance.axis_mode}."
        )
    if instance.stage == "stage1":
        if instance.descriptor.name == CoordinateProtocol.IMAGE_PIXEL and instance.axis_mode == "relative_extent":
            coordinate_mapping = (
                "Return image pixel extent coordinates directly: "
                f"x spans 0..{image.width} across the visible image, and y spans 0..{image.height}; "
                "do not normalize either axis."
            )
        elif instance.axis_mode == "relative_extent":
            coordinate_mapping = (
                "The complete screenshot was stretched independently along x and y into this square before a translucent eight-division guide was overlaid. No content was cropped or padded. "
                "Return normalized position fractions, not image pixel counts: "
                f"x is the target's horizontal fraction of this visible square multiplied by {instance.wire_x_max}, "
                f"and y is its vertical fraction multiplied by {instance.wire_y_max}. "
                f"Both axes independently span 0..{instance.wire_x_max}. Estimate each fraction from the full visible image. "
            )
            coordinate_mapping += " " + _grid_description(instance.stage, instance.label_formatter.grid_label_placement)
            coordinate_mapping += " Do not compensate for the stretch. The backend maps the relative point to source geometry."
        else:
            coordinate_mapping = (
                "Return zero-based pixel-center indexes in the visible image; do not scale either axis."
            )
        prompt = (
            "You are viewing a screenshot of one monitor with a light coordinate guide. Locate the requested target approximately. "
            "Do not perform the final interaction. Call inspect_region(x,y,zoom). Choose zoom from 2,4,6,8 to show the requested interaction feature; omitted zoom defaults to 4. "
            + protocol_text + " " + coordinate_mapping +
            f" Target: {instance.request.target_description}"
        )
        if instance.descriptor.name in {CoordinateProtocol.RELATIVE_1000, CoordinateProtocol.CUSTOM_ISOTROPIC_1000}:
            prompt = (
                f"Find {instance.request.target_description} in this complete square image. "
                "The original screenshot was independently stretched to this square before its eight-division grid without text labels. "
                "Each displayed axis spans 0..1000. Give a rough center for a crop containing "
                "the requested interaction feature. Think briefly; call "
                "inspect_region(x,y,zoom). Choose zoom from 2,4,6,8; omitted zoom defaults to 4."
            )
        prompt += (
            "After an absent-target or wrong-region crop, treat that failed crop as negative evidence. "
            "Use the fresh overview to independently locate the closest rough target center. "
            "MUST NOT consecutively repeat the same failed inspect_region coordinates (x,y) or replay its command, "
            "even with a different zoom. Reassess visually and adjust the location from the latest image; "
            "never add an arbitrary or random offset. If the target cannot be located, stop without input instead of guessing. "
        )
        properties = {"x": _number_schema(instance, "x"), "y": _number_schema(instance, "y"), "zoom": {"type": "integer", "enum": [2, 4, 6, 8], "default": 4}}
        tool = {"name": "inspect_region", "parameters": {"type": "object", "properties": properties, "required": ["x", "y"], "additionalProperties": False}}
    else:
        assert instance.crop is not None
        local_mapping = ""
        if instance.descriptor.name in {CoordinateProtocol.RELATIVE_1000, CoordinateProtocol.CUSTOM_ISOTROPIC_1000}:
            square_crop = (
                " The clipped detail crop has been resized and stretched to fill this square model image; "
                "use its displayed shape as shown. "
            )
            if instance.crop.grid_enabled:
                local_mapping = square_crop + (
                    "Stage-2 uses independent normalized coordinates: both axes independently span 0..1000. "
                    "The horizontal position as a fraction of the displayed image determines x/1000; "
                    "the vertical position as a fraction of the displayed image determines y/1000. "
                )
                local_mapping += _grid_description(instance.stage, instance.label_formatter.grid_label_placement) + " "
                local_mapping += (
                    "Aim at the requested interaction point/feature and interpolate between ticks; do not snap "
                    "to a grid intersection or compensate for the clipped crop's original shape. "
                    "The backend maps the relative point through the trusted crop geometry to source pixels."
                )
            else:
                local_mapping = square_crop + (
                    "This image has no grid or tick labels. Estimate the requested interaction point directly from the displayed square "
                    "using independent normalized fractions: x is its horizontal fraction multiplied by 1000, and y is its "
                    "vertical fraction multiplied by 1000; x and y each range from 0 to 1000. "
                    "Aim at the requested interaction point and do not return raw image pixel counts or compensate "
                    "for the clipped crop's original shape. The backend maps the relative point through the trusted crop "
                    "geometry to source pixels."
                )
        prompt = (
            "You are viewing the latest detail image derived from the desktop observation. "
            "Use THIS image to determine the requested interaction point. Do not calculate global desktop coordinates. "
            "For a user-requested interaction, if its point or feature is identifiable here, the next call MUST be interact_detail(x,y,action); "
            "action must equal requested_action. Do not call observe or inspect_region merely for perfect centering, "
            "extra margins, another zoom, or complete visibility of the surrounding object. "
            "A clipped outer object does not prevent acting at its identifiable requested point. "
            "For observation-only or inspection-only requests, stop without input. "
            "Only if a requested interaction point itself is absent, cut off or unidentifiable, call observe() for a fresh overview "
            "before another inspect_region. Never derive overview coordinates from detail or reuse an older overview point. "
            + protocol_text + local_mapping +
            f" target={instance.request.target_description}; requested_action={instance.request.action.value}."
        )
        if instance.descriptor.name in {CoordinateProtocol.RELATIVE_1000, CoordinateProtocol.CUSTOM_ISOTROPIC_1000}:
            guide = "This image has a normalized grid. " if instance.crop.grid_enabled else "This image has no grid or tick labels. "
            prompt = (
                f"Locate the requested interaction point for {instance.request.target_description} in this latest square detail image. "
                f"For a user-requested interaction, if that point or feature is identifiable, the next call MUST be interact_detail with action={instance.request.action.value}. "
                "Do not call observe or inspect_region merely for perfect centering, extra margins, another zoom, "
                "or full surrounding-object visibility. A clipped outer object does not prevent acting at its identifiable requested point. "
                "For observation-only or inspection-only requests, stop without input. "
                "Only if a requested interaction point itself is absent, cut off or unidentifiable, call observe() for a fresh overview "
                "before another inspect_region. Never derive overview coordinates from detail or reuse an older overview point. "
                "x and y each span 0..1000; estimate the precise point from this image without snapping to grid intersections. "
                + guide + "Think briefly; call one tool."
            )
        properties = {
            "x": _number_schema(instance, "x"),
            "y": _number_schema(instance, "y"),
            "action": {"type": "string", "const": instance.request.action.value},
        }
        if instance.request.action in {Action.SCROLL_UP, Action.SCROLL_DOWN, Action.SCROLL_UP_1, Action.SCROLL_DOWN_1}:
            properties["steps"] = {"type": "integer", "minimum": 1,
                                   "maximum": 1 if instance.request.action.value.endswith("_1") else 5,
                                   "default": 1}
            prompt += " For scrolling, steps is the number of native discrete wheel units, default 1; it is not a pixel distance. "
        tool = {"name": "interact_detail", "parameters": {"type": "object", "properties": properties, "required": ["x", "y", "action"], "additionalProperties": False}}
        observe_tool = {
            "name": "observe",
            "description": "Return a fresh overview; inspect_region may follow only after that overview is received.",
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        }

    return EncodedStage(
        instance=instance,
        # This is the complete private multimodal payload.  It is built before
        # the provider adapter is called and is never spliced into parent
        # Agent history.  One stage call contains exactly one image block.
        messages=({"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(image.png).decode("ascii")
            }},
        ]},),
        tools=(tool, observe_tool) if instance.stage == "stage2" else (tool,),
        image_metadata={"width": image.width, "height": image.height, "content_hash": image.content_hash, "protocol_fingerprint": instance.protocol_fingerprint},
    )


def _strict_envelope(raw: Any) -> tuple[str, Mapping[str, Any]]:
    if not isinstance(raw, Mapping) or set(raw) != {"name", "arguments"}:
        raise ComputerUseError("INVALID_MODEL_OUTPUT", "tool output must contain exactly name and arguments", field="$")
    name = raw["name"]
    arguments = raw["arguments"]
    if not isinstance(name, str) or not isinstance(arguments, Mapping):
        raise ComputerUseError("INVALID_MODEL_OUTPUT", "invalid tool-call envelope", field="$")
    return name, arguments


def _strict_fields(arguments: Mapping[str, Any], required: set[str], optional: set[str] = frozenset()) -> None:
    missing = required - set(arguments)
    extra = set(arguments) - required - optional
    if missing:
        raise ComputerUseError("INVALID_MODEL_OUTPUT", f"missing fields: {sorted(missing)}", field="arguments")
    if extra:
        raise ComputerUseError("INVALID_MODEL_OUTPUT", f"extra fields: {sorted(extra)}", field="arguments")


def _axis(instance: ProtocolInstance, raw: Any, axis: str) -> Fraction:
    integer = instance.descriptor.output_type == "integer" or instance.axis_mode == "index_center"
    number = _wire_number(raw, integer=integer, field=f"arguments.{axis}")
    if instance.axis_mode == "index_center":
        size = instance.image_width if axis == "x" else instance.image_height
    else:
        size = instance.wire_x_max if axis == "x" else instance.wire_y_max
    return decode_axis(number, size=size, mode=instance.axis_mode)


def decode_stage(encoded: EncodedStage, raw: dict[str, Any]) -> StageDecision:
    instance = encoded.instance
    name, arguments = _strict_envelope(raw)
    if instance.stage == "stage1":
        if name != "inspect_region":
            raise ComputerUseError("INVALID_MODEL_OUTPUT", "stage1 only accepts inspect_region", field="name")
        _strict_fields(arguments, {"x", "y"}, {"zoom"})
        zoom = arguments.get("zoom", 4)
        if isinstance(zoom, bool) or not isinstance(zoom, int) or zoom not in (2, 4, 6, 8):
            raise ComputerUseError("INVALID_ZOOM", "zoom must be one of 2,4,6,8", field="arguments.zoom")
        x = _axis(instance, arguments["x"], "x") * 1000
        y = _axis(instance, arguments["y"], "y") * 1000
        return InspectRegion(x=x, y=y, zoom=zoom)
    if name == "observe":
        _strict_fields(arguments, set())
        return Reobserve("other")  # Internal offline recovery marker; exposed tool is observe.
    if name != "interact_detail":
        raise ComputerUseError("INVALID_MODEL_OUTPUT", "stage2 accepts interact_detail or observe", field="name")
    _strict_fields(arguments, {"x", "y", "action"}, {"steps"})
    crop = instance.crop
    assert crop is not None
    try:
        action = Action(arguments["action"])
    except (TypeError, ValueError) as exc:
        raise ComputerUseError("INVALID_ACTION", "unknown action", field="arguments.action") from exc
    if action is not instance.request.action:
        raise ComputerUseError("INVALID_ACTION", "action differs from requested_action", field="arguments.action")
    steps = arguments.get("steps", 1)
    if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 5:
        raise ComputerUseError("INVALID_STEPS", "steps must be an integer from 1 through 5", field="arguments.steps")
    if "steps" in arguments and action not in {Action.SCROLL_UP, Action.SCROLL_DOWN, Action.SCROLL_UP_1, Action.SCROLL_DOWN_1}:
        raise ComputerUseError("INVALID_STEPS", "steps apply only to scrolling", field="arguments.steps")
    if action in {Action.SCROLL_UP_1, Action.SCROLL_DOWN_1} and steps != 1:
        raise ComputerUseError("INVALID_STEPS", "legacy scrolling action sends one step", field="arguments.steps")
    x = _axis(instance, arguments["x"], "x") * crop.local_width
    y = _axis(instance, arguments["y"], "y") * crop.local_height
    return InteractDetail(crop.crop_id, x, y, action, steps)
