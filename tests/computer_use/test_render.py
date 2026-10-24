from collections import Counter
from fractions import Fraction
from io import BytesIO

import pytest
from PIL import Image, ImageDraw, ImageFont


class RecordingLabels:
    def __init__(self):
        self.seen = []

    def format_axis(self, axis, canonical_value):
        self.seen.append((axis, canonical_value))
        return f"{axis}={int(canonical_value)}"

    def boundary_ticks(self, axis):
        return ((Fraction(0), f"{axis}=0"), (Fraction(1000), f"{axis}=1000"))


@pytest.mark.parametrize("width,height", [(16, 10), (10, 16)])
def test_overview_stretches_complete_frame_to_configured_square_without_grid(frame, settings, width, height):
    from backend.computer_use.render import render_overview

    pixels = bytearray((40, 60, 80) * (width * height))
    pixels[0:3] = bytes((240, 10, 10))
    pixels[-3:] = bytes((10, 240, 10))
    source = frame(width=width, height=height, rgb=bytes(pixels))

    artifact = render_overview(source, settings(model_square_side_px=1024, grid_enabled=False))
    image = Image.open(BytesIO(artifact.png))

    side = 1024
    assert image.size == (side, side)
    assert (artifact.width, artifact.height) == (side, side)
    assert image.getpixel((0, 0))[0] > 180
    assert image.getpixel((side - 1, side - 1))[1] > 180


def test_overview_adds_eight_intervals_after_square_stretch(frame, settings):
    from backend.computer_use.render import render_overview

    width, height = 1280, 720
    source = frame(width=width, height=height, rgb=bytes((20, 50, 80)) * (width * height))
    artifact = render_overview(source, settings(model_square_side_px=1024, grid_enabled=True))
    image = Image.open(BytesIO(artifact.png)).convert("RGB")

    assert image.size == (1024, 1024)
    assert image.getpixel((512, 512)) != (20, 50, 80)
    assert image.getpixel((64, 64)) == (20, 50, 80)
    assert artifact.content_hash != render_overview(source, settings(grid_enabled=False)).content_hash


def test_overview_stretch_moves_an_interior_landmark_by_each_axis_ratio(frame, settings):
    from backend.computer_use.render import render_overview

    image = Image.new('RGB', (100, 50), (20, 30, 40))
    ImageDraw.Draw(image).rectangle((18, 19, 22, 23), fill=(255, 0, 255))
    source = frame(width=100, height=50, rgb=image.tobytes())
    artifact = render_overview(source, settings(grid_enabled=False))
    stretched = Image.open(BytesIO(artifact.png)).convert('RGB')

    assert stretched.size == (1024, 1024)
    assert stretched.getpixel((205, 430))[0] > 240
    assert stretched.getpixel((205, 430))[2] > 240
    assert stretched.getpixel((205, 215)) == (20, 30, 40)


def test_grid_label_font_uses_twelve_pixel_baseline(frame, crop, settings, monkeypatch):
    import backend.computer_use.render as render

    requested = []
    original = render.ImageFont.load_default

    def record_font(*, size):
        requested.append(size)
        return original(size=size)

    monkeypatch.setattr(render.ImageFont, 'load_default', record_font)
    source = frame(width=1280, height=720)
    config = settings(model_square_side_px=1024, grid_enabled=True)
    render.render_overview(source, config)
    detail_crop = crop(source_frame=source, x0=0, y0=0, x1=640, y1=360,
                       detail_width=1024, detail_height=1024,
                       model_width=1024, model_height=1024)
    base = render.render_detail_base(source, detail_crop, config)
    render.overlay_detail(base, detail_crop, RecordingLabels(), config)

    assert requested == [12, 12]  # 12 px per 1000 canvas pixels, with a 12 px floor


def test_overview_edge_labels_appear_only_at_guide_ends(frame, settings):
    from backend.computer_use.render import render_overview

    width, height = 1280, 720
    source = frame(width=width, height=height, rgb=bytes((50, 100, 150)) * (width * height))
    edge = render_overview(source, settings(grid_label_placement="edge"))
    repeated = render_overview(source, settings(grid_label_placement="repeated"))
    image = Image.open(BytesIO(edge.png)).convert("RGB")
    repeated_image = Image.open(BytesIO(repeated.png)).convert("RGB")

    def has_label(raster, x0, y0, x1, y1):
        return any((lambda p: p[0] > 90 and p[1] > 130 and p[2] > 175)(raster.getpixel((x, y)))
                   for y in range(y0, y1) for x in range(x0, x1))

    width = height = image.width  # labels follow the configured square resize
    x125, y125 = width // 8, height // 8
    assert has_label(image, x125 + 2, 2, x125 + 25, 40)
    assert has_label(image, x125 + 2, height - 45, x125 + 25, height - 2)
    assert not has_label(image, x125 + 2, 65, x125 + 25, 95)
    assert has_label(repeated_image, x125 + 2, 65, x125 + 25, 95)
    assert has_label(image, 2, y125 - 15, 45, y125 + 16)
    assert has_label(image, width - 50, y125 - 15, width - 2, y125 + 16)
    assert not has_label(image, 65, y125 - 15, 95, y125 + 16)
    assert has_label(repeated_image, 65, y125 - 15, 95, y125 + 16)
    assert edge.renderer_fingerprint != repeated.renderer_fingerprint


@pytest.mark.parametrize("bounds", [(4, 3, 12, 8), (0, 0, 8, 3)])
def test_detail_stretches_only_exact_source_crop_to_square(frame, crop, settings, bounds):
    from backend.computer_use.render import render_detail_base

    width, height = 16, 10
    pixels = bytearray((20, 40, 60) * (width * height))
    x0, y0, x1, y1 = bounds
    for x, y, color in ((x0, y0, (240, 10, 10)), (x1 - 1, y1 - 1, (10, 240, 10))):
        start = 3 * (y * width + x)
        pixels[start:start + 3] = bytes(color)
    if x1 < width:
        start = 3 * (y0 * width + x1)
        pixels[start:start + 3] = bytes((0, 0, 255))
    source = frame(width=width, height=height, rgb=bytes(pixels))
    detail = crop(source_frame=source, x0=x0, y0=y0, x1=x1, y1=y1,
                  detail_width=1024, detail_height=1024,
                  model_width=1024, model_height=1024)

    artifact = render_detail_base(source, detail, settings(model_square_side_px=1024))
    image = Image.open(BytesIO(artifact.png))

    assert image.size == (1024, 1024)
    assert image.getpixel((0, 0))[0] > 200
    assert image.getpixel((1023, 1023))[1] > 200
    assert image.getpixel((512, 512)) == (20, 40, 60)
    assert image.getpixel((1023, 0))[2] < 100


def test_detail_rejects_metadata_not_matching_configured_square(frame, crop, settings):
    from backend.computer_use.errors import ComputerUseError
    from backend.computer_use.render import render_detail_base

    source = frame(width=16, height=10)
    detail = crop(source_frame=source, detail_width=1024, detail_height=1024,
                  model_width=1024, model_height=1024)
    with pytest.raises(ComputerUseError, match="CALIBRATION_REQUIRED"):
        render_detail_base(source, detail, settings(model_square_side_px=1536))


@pytest.mark.parametrize("side", [1024, 1536])
def test_grid_has_sixteen_cells_but_labels_only_even_interior_ticks(frame, crop, settings, side):
    from backend.computer_use.render import overlay_detail, render_detail_base

    source = frame(width=16, height=10)
    detail = crop(source_frame=source, x0=0, y0=0, x1=8, y1=3,
                  detail_width=side, detail_height=side,
                  model_width=side, model_height=side)
    config = settings(model_square_side_px=side, grid_divisions=16, grid_label_placement="repeated")
    base = render_detail_base(source, detail, config)
    labels = RecordingLabels()
    rendered = overlay_detail(base, detail, labels, config)
    image = Image.open(BytesIO(rendered.png))
    plain = Image.open(BytesIO(base.png))

    assert image.size == (side, side)
    expected = [Fraction(tick) for tick in range(125, 876, 125)]
    assert Counter(labels.seen) == Counter({(axis, tick): 16
                                           for axis in ("x", "y") for tick in expected})
    for index in range(1, 16):
        position = index * side // 16
        assert image.getpixel((position, side // 2 + 30)) != plain.getpixel((position, side // 2 + 30))
        assert image.getpixel((side // 2 + 30, position)) != plain.getpixel((side // 2 + 30, position))
    for point in ((0, side // 2), (side - 1, side // 2), (side // 2, 0), (side // 2, side - 1)):
        assert image.getpixel(point) != plain.getpixel(point)
    assert min(min(pixel) for pixel in image.get_flattened_data()) >= 30

    def has_light_text(x0, y0, x1, y1):
        def is_label_pixel(x, y):
            red, green, blue = image.getpixel((x, y))
            return red > 100 and green > 130 and blue > 170

        return any(is_label_pixel(x, y)
                   for y in range(y0, y1) for x in range(x0, x1))

    x125 = side // 8
    y125 = side // 8
    for cell in range(16):
        row_mid = (2 * cell + 1) * side // 32
        col_mid = (2 * cell + 1) * side // 32
        assert has_light_text(x125 + 2, row_mid - 25, x125 + 30, row_mid + 26)
        assert has_light_text(col_mid - 35, y125 - 15, col_mid + 36, y125 + 16)
        # Odd-index guides and the outer border have no adjacent tick labels.
        x_odd = side // 16
        assert not has_light_text(x_odd + 2, row_mid - 20, x_odd + 30, row_mid + 21)
        assert not has_light_text(2, row_mid - 20, 30, row_mid + 21)
        y_odd = side // 16
        assert not has_light_text(col_mid - 20, y_odd + 2, col_mid + 21, y_odd + 20)
        assert not has_light_text(col_mid - 20, 2, col_mid + 21, 20)


def test_detail_edge_labels_appear_only_at_guide_ends(frame, crop, settings):
    from backend.computer_use.render import overlay_detail, render_detail_base

    side = 1024
    source = frame(width=16, height=10)
    detail = crop(source_frame=source, x1=8, y1=3)
    base = render_detail_base(source, detail, settings())
    labels = RecordingLabels()
    edge = overlay_detail(base, detail, labels, settings(grid_label_placement="edge"))
    repeated = overlay_detail(base, detail, RecordingLabels(), settings(grid_label_placement="repeated"))
    image = Image.open(BytesIO(edge.png)).convert("RGB")
    repeated_image = Image.open(BytesIO(repeated.png)).convert("RGB")

    def has_label(raster, x0, y0, x1, y1):
        return any((lambda p: p[0] > 100 and p[1] > 130 and p[2] > 170)(raster.getpixel((x, y)))
                   for y in range(y0, y1) for x in range(x0, x1))

    assert Counter(labels.seen) == Counter({(axis, Fraction(tick)): 2
                                           for axis in ("x", "y") for tick in range(125, 876, 125)})
    x125 = y125 = side // 8
    assert has_label(image, x125 + 2, 2, x125 + 24, 40)
    assert has_label(image, x125 + 2, side - 45, x125 + 24, side - 2)
    assert not has_label(image, x125 + 2, 64, x125 + 24, 85)
    assert has_label(repeated_image, x125 + 2, 64, x125 + 24, 85)
    assert has_label(image, 2, y125 - 15, 45, y125 + 16)
    assert has_label(image, side - 50, y125 - 15, side - 2, y125 + 16)
    assert not has_label(image, 75, y125 - 15, 90, y125 + 16)
    assert has_label(repeated_image, 75, y125 - 15, 90, y125 + 16)
    assert edge.renderer_fingerprint != repeated.renderer_fingerprint


@pytest.mark.parametrize("side", [1024, 1536])
def test_x_labels_are_vertical_beside_grid_line_in_every_cell_and_y_stays_horizontal(
    frame, crop, settings, side
):
    from backend.computer_use.render import overlay_detail, render_detail_base

    source = frame(width=16, height=10)
    detail = crop(source_frame=source, x1=8, y1=3,
                  detail_width=side, detail_height=side,
                  model_width=side, model_height=side)
    config = settings(model_square_side_px=side, grid_divisions=16, grid_label_placement="repeated")
    base = render_detail_base(source, detail, config)
    image = Image.open(BytesIO(overlay_detail(base, detail, RecordingLabels(), config).png))

    def is_label_pixel(x, y):
        red, green, blue = image.getpixel((x, y))
        return red > 100 and green > 130 and blue > 170

    def light_bounds(x0, y0, x1, y1):
        points = [(x, y) for y in range(y0, y1) for x in range(x0, x1)
                  if is_label_pixel(x, y)]
        assert points
        return min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points), max(y for _, y in points)

    for coordinate, x0, x1 in (
        (side // 8, side // 8 + 2, side // 8 + 36),
        (side * 7 // 8, side * 7 // 8 + 2, side * 7 // 8 + 36),
    ):
        for cell in range(16):
            row_start = cell * side // 16
            left, top, right, bottom = light_bounds(
                x0, row_start + 8, x1, row_start + side // 16 - 8
            )
            assert bottom - top >= 2 * (right - left + 1), (coordinate, cell, left, top, right, bottom)
            assert right - left < max(24, side // 60)
            assert abs((top + bottom) / 2 - (row_start + side / 32)) <= 10
            assert left >= coordinate + 2

    # Horizontal Y text stays near the first row's guide, centred in its column.
    mid = side // 32
    left, top, right, bottom = light_bounds(max(2, mid - 35), side // 8 - 20,
                                           mid + 36, side // 8 + 21)
    assert right - left >= 2 * (bottom - top + 1)
    assert abs((left + right) / 2 - mid) <= 4


def test_x_label_reads_counterclockwise_from_bottom_to_top(frame, crop, settings):
    from backend.computer_use.render import overlay_detail, render_detail_base

    source = frame(width=16, height=10)
    detail = crop(source_frame=source, x1=8, y1=3, detail_width=1024,
                  detail_height=1024, model_width=1024, model_height=1024, grid_divisions=16)
    config = settings(model_square_side_px=1024, grid_divisions=16, grid_label_placement="repeated")
    base = render_detail_base(source, detail, config)
    image = Image.open(BytesIO(overlay_detail(base, detail, RecordingLabels(), config).png))

    font = ImageFont.load_default(size=12)
    label = "x=125"
    bbox = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox((0, 0), label, font=font)
    horizontal = Image.new("L", (bbox[2] - bbox[0], bbox[3] - bbox[1]), 0)
    ImageDraw.Draw(horizontal).text((-bbox[0], -bbox[1]), label, font=font, fill=255)
    ccw = horizontal.rotate(90, expand=True)
    cw = horizontal.rotate(270, expand=True)

    def difference(mask):
        start_y = (64 - mask.height) // 2
        return sum(abs(image.getpixel((130 + x, start_y + y))[0] -
                       (50 + round(205 * mask.getpixel((x, y)) * 128 / (255 * 255))))
                   for y in range(mask.height) for x in range(mask.width))

    assert difference(ccw) < difference(cw) / 4


def test_grid_lines_border_and_labels_blend_at_half_opacity(frame, crop, settings):
    from backend.computer_use.render import overlay_detail, render_detail_base

    source = frame(width=16, height=10)
    detail = crop(source_frame=source, x1=8, y1=3, detail_width=1024,
                  detail_height=1024, model_width=1024, model_height=1024, grid_divisions=16)
    config = settings(model_square_side_px=1024, grid_divisions=16, grid_label_placement="repeated")
    base = render_detail_base(source, detail, config)
    rendered = Image.open(BytesIO(overlay_detail(base, detail, RecordingLabels(), config).png))

    def half_blend(foreground):
        return tuple(round((background + overlay) / 2)
                     for background, overlay in zip((50, 100, 150), foreground))

    for got, expected in (
        (rendered.getpixel((64, 50)), half_blend((255, 64, 64))),
        (rendered.getpixel((0, 50)), half_blend((255, 255, 255))),
    ):
        assert all(abs(actual - wanted) <= 1 for actual, wanted in zip(got, expected))

    x_label_red = max(rendered.getpixel((x, y))[0]
                      for y in range(15, 50) for x in range(130, 144))
    y_label_red = max(rendered.getpixel((x, y))[0]
                      for y in range(122, 135) for x in range(10, 55))
    assert 115 <= x_label_red <= 154
    assert 115 <= y_label_red <= 154


def test_disabled_overlay_keeps_square_base_byte_identical(frame, crop, settings):
    from backend.computer_use.render import overlay_detail, render_detail_base

    source = frame(width=16, height=10)
    detail = crop(source_frame=source, x1=8, y1=3)
    config = settings(model_square_side_px=1024, grid_enabled=False)
    base = render_detail_base(source, detail, config)
    assert overlay_detail(base, detail, RecordingLabels(), config).png == base.png


def test_disabled_overlay_rejects_rectangular_image_bytes_even_with_square_metadata(crop, settings):
    from backend.computer_use.contracts import ImageArtifact
    from backend.computer_use.errors import ComputerUseError
    from backend.computer_use.render import overlay_detail

    output = BytesIO()
    Image.new("RGB", (1024, 625), (30, 40, 50)).save(output, format="PNG")
    disguised = ImageArtifact(png=output.getvalue(), width=1024, height=1024,
                              content_hash="synthetic", renderer_fingerprint="synthetic")
    with pytest.raises(ComputerUseError, match="CALIBRATION_REQUIRED"):
        overlay_detail(disguised, crop(), RecordingLabels(),
                       settings(model_square_side_px=1024, grid_enabled=False))
