from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from .config import ComputerUseConfig
from .contracts import CanonicalFrame, DetailCrop, ImageArtifact, LabelFormatter
from .coordinates import half_up_positive
from .errors import ComputerUseError


def _fingerprint(kind: str, config: ComputerUseConfig, width: int, height: int, *, overlay: bool) -> str:
    payload = {
        "kind": kind,
        "width": width,
        "height": height,
        "model_square_side_px": config.model_square_side_px,
        "resize_policy": "full_frame_independent_axis_square_no_padding" if kind == "overview" else "independent_axis_square_no_padding",
        "resampling": "LANCZOS",
        "grid_enabled": config.grid_enabled if overlay else False,
        "grid_divisions": (8 if kind == "overview" else config.grid_divisions) if overlay else 0,
        "grid_label_placement": config.grid_label_placement if overlay and kind != "overview" else None,
        "grid_label_shadow_width_px": config.grid_label_shadow_width_px if overlay and kind != "overview" else None,
        "label_shadow_policy": "black_half_opacity_scaled_1000px_baseline" if overlay and kind != "overview" else None,
        "grid_policy": ("eight_equal_square_canvas_intervals_half_opacity" if kind == "overview" else "sixteen_equal_canvas_intervals_endpoint_last_pixel_half_opacity") if overlay else None,
        "label_policy": ("none" if kind == "overview" else
                         "even_value_centered_x_ccw_y_horizontal_half_opacity_" +
                         ("edge_ends_only" if config.grid_label_placement == "edge" else "shared_orthogonal_anchors_shift31_25"))
                        if overlay else None,
        "renderer_version": 25,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _artifact(image: Image.Image, fingerprint: str) -> ImageArtifact:
    output = BytesIO()
    image.save(output, format="PNG", optimize=False)
    png = output.getvalue()
    return ImageArtifact(
        png=png,
        width=image.width,
        height=image.height,
        content_hash=hashlib.sha256(png).hexdigest(),
        renderer_fingerprint=fingerprint,
    )


def _source_image(frame: CanonicalFrame) -> Image.Image:
    return Image.frombytes("RGB", (frame.width, frame.height), frame.rgb)


def _fit_label_origin(desired: int, bbox_start: int, bbox_end: int, canvas_size: int) -> int:
    first = 2 - bbox_start
    last = canvas_size - bbox_end - 2
    if first > last:
        raise ComputerUseError("CALIBRATION_REQUIRED", "grid label does not fit inside the detail image")
    return max(first, min(desired, last))


def _counterclockwise_text_mask(text: str, font: ImageFont.ImageFont, shadow_width: int = 0) -> Image.Image | None:
    # Padding carries the glyph outline through rotation without clipping.
    bbox = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox((0, 0), text, font=font, stroke_width=0)
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if width <= 0 or height <= 0:
        return None
    mask = Image.new("L", (width + 2 * shadow_width, height + 2 * shadow_width), 0)
    ImageDraw.Draw(mask).text((shadow_width - bbox[0], shadow_width - bbox[1]), text, font=font, fill=255, stroke_width=0)
    return mask.rotate(90, expand=True)


def _shadow_width(config: ComputerUseConfig, side: int) -> int:
    baseline = config.grid_label_shadow_width_px
    return max(1, half_up_positive(Fraction(baseline * side, 1000))) if baseline else 0


def _paste_label_mask(layer, mask, position, fill, shadow_width):
    if shadow_width:
        outline = mask.filter(ImageFilter.MaxFilter(2 * shadow_width + 1))
        layer.paste((0, 0, 0, 128), position, outline)
    # Replace overlay pixels rather than stacking opacity over the outline.
    layer.paste(fill, position, mask)


def render_overview(frame: CanonicalFrame, config: ComputerUseConfig) -> ImageArtifact:
    image = _source_image(frame)
    side = config.model_square_side_px
    if image.size != (side, side):
        image = image.resize((side, side), Image.Resampling.LANCZOS)
    if not config.grid_enabled:
        return _artifact(image, _fingerprint("overview", config, side, side, overlay=False))
    # Stretch the complete source frame first; guides describe the model-facing
    # square. The unmodified CanonicalFrame remains authoritative for cropping.
    width, height = image.size
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")
    line, edge = (255, 64, 64, 128), (255, 255, 255, 128)
    draw.rectangle((0, 0, width - 1, height - 1), outline=edge, width=1)
    for index in range(1, 8):
        gx = half_up_positive(Fraction(index * width, 8))
        gy = half_up_positive(Fraction(index * height, 8))
        draw.line((gx, 0, gx, height - 1), fill=line, width=1)
        draw.line((0, gy, width - 1, gy), fill=line, width=1)
    # Stage 1 has guides only; all text labels belong to Stage 2.
    rendered = Image.alpha_composite(image.convert("RGBA"), layer).convert("RGB")
    return _artifact(rendered, _fingerprint("overview", config, width, height, overlay=True))


def render_detail_base(frame: CanonicalFrame, crop: DetailCrop, config: ComputerUseConfig) -> ImageArtifact:
    if frame.frame_id != crop.frame_id or frame.binding.binding_id != crop.binding_id:
        raise ComputerUseError("INVALID_CROP_ID", "crop is not bound to the supplied frame")
    if frame.binding.geometry_version != crop.geometry_version:
        raise ComputerUseError("GEOMETRY_CHANGED", "crop geometry is stale")
    side = config.model_square_side_px
    expected_size = (side, side)
    if (crop.detail_width, crop.detail_height, crop.model_width, crop.model_height) != (side,) * 4:
        raise ComputerUseError("CALIBRATION_REQUIRED", "detail model dimensions differ from the configured square")
    native = _source_image(frame).crop((crop.x0, crop.y0, crop.x1, crop.y1))
    image = native.resize(expected_size, Image.Resampling.LANCZOS)
    return _artifact(image, _fingerprint("detail-base", config, *expected_size, overlay=False))


def overlay_detail(
    base: ImageArtifact,
    crop: DetailCrop,
    labels: LabelFormatter,
    config: ComputerUseConfig,
) -> ImageArtifact:
    side = config.model_square_side_px
    if (base.width, base.height, crop.detail_width, crop.detail_height,
            crop.model_width, crop.model_height) != (side,) * 6:
        raise ComputerUseError("CALIBRATION_REQUIRED", "detail overlay requires the configured square image")
    try:
        image = Image.open(BytesIO(base.png))
        if image.size != (side, side):
            raise ComputerUseError("CALIBRATION_REQUIRED", "detail image bytes differ from square metadata")
    except (OSError, ValueError) as exc:
        raise ComputerUseError("CALIBRATION_REQUIRED", "detail image bytes are invalid") from exc
    if not config.grid_enabled:
        return base
    image = image.convert("RGBA")
    # Compose a single half-opacity overlay over the immutable base. Labels
    # replace guide pixels on this layer rather than stacking extra opacity.
    layer = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")
    font = ImageFont.load_default(size=max(1, half_up_positive(Fraction(12 * side, 1000))))
    step = Fraction(1000, config.grid_divisions)
    line = (255, 64, 64, 128)
    edge = (255, 255, 255, 128)
    draw.rectangle((0, 0, image.width - 1, image.height - 1), outline=edge, width=1)

    ticks = tuple(step * index for index in range(config.grid_divisions + 1))
    for position in ticks[1:-1]:
        guide = half_up_positive(position / 1000 * side)
        draw.line((guide, 0, guide, side - 1), fill=line, width=1)
        draw.line((0, guide, side - 1, guide), fill=line, width=1)

    label_fill = (255, 255, 255, 128)
    shadow_width = _shadow_width(config, side)
    # Repeated labels on both axes share orthogonal centers 31.25,156.25,...,906.25.
    repeated_indices = tuple(range(0, config.grid_divisions, 2))
    anchors = (0, config.grid_divisions) if config.grid_label_placement == "edge" else repeated_indices
    gap = 2
    for index in range(2, config.grid_divisions, 2):
        position = ticks[index]
        coordinate = half_up_positive(position / 1000 * side)
        x_text = labels.format_axis("x", position)
        y_text = labels.format_axis("y", position)
        x_mask = _counterclockwise_text_mask(x_text, font, shadow_width)
        y_box = draw.textbbox((0, 0), y_text, font=font, stroke_width=shadow_width)
        for anchor_index in anchors:
            anchor = (half_up_positive(
                (Fraction(anchor_index) + Fraction(1, 2)) * side / config.grid_divisions)
                if config.grid_label_placement == "repeated"
                else half_up_positive(Fraction(anchor_index * side, config.grid_divisions)))
            if x_mask is not None:
                x_label_x = coordinate + gap
                x_label_y = (gap if anchor_index == 0 else side - gap - x_mask.height) \
                    if config.grid_label_placement == "edge" else anchor - x_mask.height // 2
                if config.grid_label_placement == "edge" and (
                        x_label_x + x_mask.width > side - gap or x_label_y < gap
                        or x_label_y + x_mask.height > side - gap):
                    raise ComputerUseError("CALIBRATION_REQUIRED", "rotated label does not fit its intersection")
                _paste_label_mask(layer, x_mask, (x_label_x, x_label_y), label_fill, shadow_width)
            y_width, y_height = y_box[2] - y_box[0], y_box[3] - y_box[1]
            y_left = (gap if anchor_index == 0 else side - gap - y_width) \
                if config.grid_label_placement == "edge" else anchor - y_width // 2
            y_top = coordinate + gap
            if config.grid_label_placement == "edge" and (
                    y_left < gap or y_left + y_width > side - gap or y_top + y_height > side - gap):
                raise ComputerUseError("CALIBRATION_REQUIRED", "horizontal label does not fit its intersection")
            draw.text((y_left - y_box[0], y_top - y_box[1]), y_text, font=font, fill=label_fill,
                      stroke_width=shadow_width, stroke_fill=(0, 0, 0, 128))
    rendered = Image.alpha_composite(image, layer).convert("RGB")
    return _artifact(rendered, _fingerprint("detail-overlay", config, side, side, overlay=True))
