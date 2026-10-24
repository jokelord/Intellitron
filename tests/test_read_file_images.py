from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor

from backend.config import ReadFileConfig
from backend.tools.read_file.errors import ReadFileError
from backend.tools.read_file.image_normalizer import _safe_svg_png, normalize_image_document


def _close(pages):
    for page in pages:
        page.close()


def test_exif_rotation_and_rgb_alpha_background(tmp_path):
    jpeg = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (20, 10), "red")
    exif = Image.Exif()
    exif[274] = 6
    image.save(jpeg, exif=exif)
    pages = normalize_image_document(jpeg, ReadFileConfig(), "auto", None)
    assert (pages[0].width, pages[0].height) == (10, 20)
    _close(pages)

    png = tmp_path / "alpha.png"
    Image.new("RGBA", (2, 2), (0, 0, 0, 0)).save(png)
    pages = normalize_image_document(png, ReadFileConfig(), "auto", None)
    with Image.open(pages[0].path) as normalized:
        assert normalized.mode == "RGB"
        assert normalized.getpixel((0, 0)) == (255, 255, 255)
    _close(pages)


def test_gif_preserves_frame_numbers_and_deduplicates_consecutive_frames(tmp_path):
    path = tmp_path / "frames.gif"
    frames = [
        Image.new("RGB", (16, 16), "red"),
        Image.new("RGB", (16, 16), "red"),
        Image.new("RGB", (16, 16), "blue"),
    ]
    frames[1].putpixel((0, 0), (254, 0, 0))
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=50, loop=0)
    pages = normalize_image_document(path, ReadFileConfig(), "auto", None)
    assert [page.source_frame for page in pages] == [1, 3]
    assert any("duplicate" in warning for warning in pages[0].warnings + pages[1].warnings)
    _close(pages)


@pytest.mark.parametrize("suffix", [".bmp", ".webp", ".tiff", ".pbm", ".pgm", ".ppm", ".ico"])
def test_supported_raster_formats_normalize_to_png(tmp_path, suffix):
    path = tmp_path / f"source{suffix}"
    image = Image.new("RGB", (16, 16), "green") if suffix == ".ico" else Image.new("RGB", (8, 6), "green")
    format_name = {".tiff": "TIFF", ".ico": "ICO"}.get(suffix)
    image.save(path, format=format_name)
    pages = normalize_image_document(path, ReadFileConfig(), "auto", None)
    assert pages[0].path.suffix == ".png"
    assert pages[0].sha256
    _close(pages)


def test_dimension_scaling_preserves_aspect_ratio_and_pixel_limit_rejects(tmp_path):
    path = tmp_path / "wide.png"
    Image.new("RGB", (100, 50), "white").save(path)
    pages = normalize_image_document(
        path, replace(ReadFileConfig(), max_image_dimension=40), "auto", None
    )
    assert (pages[0].width, pages[0].height) == (40, 20)
    _close(pages)

    with pytest.raises(ReadFileError, match="IMAGE_LIMIT_EXCEEDED"):
        normalize_image_document(
            path, replace(ReadFileConfig(), max_image_pixels=100), "auto", None
        )


@pytest.mark.parametrize(
    "payload",
    [
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///etc/passwd"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/a.png"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(https://example.com/a.css)</style></svg>',
    ],
)
def test_svg_rejects_script_file_and_external_references(tmp_path, payload):
    path = tmp_path / "unsafe.svg"
    path.write_text(payload)
    with pytest.raises(ReadFileError, match="INVALID_IMAGE"):
        normalize_image_document(path, ReadFileConfig(), "auto", None)


def test_unreadable_heif_reports_explicit_plugin_error(tmp_path):
    path = tmp_path / "broken.heic"
    path.write_bytes(b"not a heif")
    with pytest.raises(ReadFileError, match="HEIF|AVIF|decode"):
        normalize_image_document(path, ReadFileConfig(), "auto", None)


def test_svg_rejects_style_surfaces_without_blacklisting_benign_text(tmp_path):
    benign = tmp_path / "benign.svg"
    benign.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
        '<text x="1" y="20">literal url(example) text</text></svg>'
    )
    pages = normalize_image_document(
        benign,
        replace(ReadFileConfig(), max_image_dimension=100),
        "auto",
        None,
    )
    assert (pages[0].width, pages[0].height) == (100, 50)
    _close(pages)

    styled = tmp_path / "styled.svg"
    styled.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:red"/></svg>'
    )
    with pytest.raises(ReadFileError, match="INVALID_IMAGE"):
        normalize_image_document(styled, ReadFileConfig(), "auto", None)

    viewbox = tmp_path / "viewbox.svg"
    viewbox.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" viewBox="0 0 100 50">'
        '<rect width="100" height="50" fill="red"/></svg>'
    )
    pages = normalize_image_document(viewbox, ReadFileConfig(), "auto", None)
    assert (pages[0].width, pages[0].height) == (200, 100)
    _close(pages)


def test_svg_rejects_css_escape_resource_before_rasterization(tmp_path, monkeypatch):
    import cairosvg

    path = tmp_path / "escaped-resource.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<rect width="10" height="10" fill="u\\72l(file:///etc/passwd)"/>'
        "</svg>"
    )
    called = False

    def forbidden_rasterization(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("escaped resource reached CairoSVG")

    monkeypatch.setattr(cairosvg, "svg2png", forbidden_rasterization)
    with pytest.raises(ReadFileError, match="INVALID_IMAGE"):
        _safe_svg_png(path, ReadFileConfig())
    assert called is False


def test_svg_rasterizer_receives_deny_all_url_fetcher(tmp_path, monkeypatch):
    from cairosvg.surface import PNGSurface

    path = tmp_path / "benign.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<rect width="10" height="10" fill="red"/></svg>'
    )
    observed = {}

    def capture_convert(**kwargs):
        observed.update(kwargs)
        return b"rasterized"

    monkeypatch.setattr(PNGSurface, "convert", capture_convert)
    assert _safe_svg_png(path, ReadFileConfig()) == b"rasterized"
    assert callable(observed["url_fetcher"])
    with pytest.raises(ReadFileError, match="INVALID_IMAGE"):
        observed["url_fetcher"]("file:///etc/passwd")


@pytest.mark.parametrize(
    "color",
    [
        "rgb(255, 0, 0)",
        "rgba(255, 0, 0, 0.5)",
        "hsl(120, 100%, 50%)",
        "hsla(120, 100%, 50%, 0.5)",
    ],
)
def test_svg_allows_token_parsed_safe_color_functions(tmp_path, color):
    path = tmp_path / "safe-color.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        f'<rect width="10" height="10" fill="{color}"/></svg>'
    )
    pages = normalize_image_document(path, ReadFileConfig(), "auto", None)
    try:
        assert (pages[0].width, pages[0].height) == (10, 10)
    finally:
        _close(pages)


@pytest.mark.parametrize(
    "paint",
    [
        "url(file:///etc/passwd)",
        "url(https://example.com/paint.svg)",
        "url(&quot;file:///etc/passwd&quot;)",
        "url('https://example.com/paint.svg')",
        "url(&quot;paint.svg#gradient&quot;)",
        "u\\72l(file:///etc/passwd)",
        "u\\72l(&quot;#gradient&quot;)",
        "image-set(url(https://example.com/a.png) 1x)",
        "image-set(url(&quot;#gradient&quot;) 1x)",
        "url(&quot;#gradient&quot; extra)",
    ],
)
def test_svg_rejects_resource_paint_functions_before_rasterization(
    tmp_path, monkeypatch, paint
):
    from cairosvg.surface import PNGSurface

    path = tmp_path / "unsafe-paint.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        f'<rect width="10" height="10" fill="{paint}"/></svg>'
    )
    called = False

    def forbidden(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("unsafe paint reached rasterization")

    monkeypatch.setattr(PNGSurface, "convert", forbidden)
    with pytest.raises(ReadFileError, match="INVALID_IMAGE"):
        _safe_svg_png(path, ReadFileConfig())
    assert called is False


def test_svg_allows_proven_document_local_fragment_paint(tmp_path):
    path = tmp_path / "local-gradient.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<defs><linearGradient id="local-gradient">'
        '<stop offset="0" stop-color="rgb(255, 0, 0)"/>'
        '<stop offset="1" stop-color="hsl(120, 100%, 50%)"/>'
        '</linearGradient></defs>'
        '<rect width="10" height="10" fill="url(#local-gradient)"/></svg>'
    )
    pages = normalize_image_document(path, ReadFileConfig(), "auto", None)
    try:
        assert (pages[0].width, pages[0].height) == (10, 10)
    finally:
        _close(pages)


@pytest.mark.parametrize(
    "paint",
    [
        "url(&quot;#local-gradient&quot;)",
        "url('#local-gradient')",
    ],
    ids=["double-quoted", "single-quoted"],
)
def test_svg_allows_quoted_document_local_fragment_paint(tmp_path, paint):
    path = tmp_path / "quoted-local-gradient.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<defs><linearGradient id="local-gradient">'
        '<stop offset="0" stop-color="red"/>'
        '<stop offset="1" stop-color="blue"/>'
        '</linearGradient></defs>'
        f'<rect width="10" height="10" fill="{paint}"/></svg>'
    )
    pages = normalize_image_document(path, ReadFileConfig(), "auto", None)
    try:
        assert (pages[0].width, pages[0].height) == (10, 10)
    finally:
        _close(pages)


def test_multiframe_limits_are_aggregate_for_decoded_and_normalized_bytes(tmp_path):
    path = tmp_path / "varying.tiff"
    first = Image.new("RGB", (5, 5), "red")
    second = Image.new("RGB", (20, 20), "blue")
    first.save(path, save_all=True, append_images=[second], format="TIFF")
    with pytest.raises(ReadFileError, match="IMAGE_LIMIT_EXCEEDED"):
        normalize_image_document(
            path,
            replace(ReadFileConfig(), max_decoded_image_bytes=1000),
            "auto",
            None,
        )

    baseline = normalize_image_document(path, ReadFileConfig(), "auto", None)
    sizes = [page.path.stat().st_size for page in baseline]
    _close(baseline)
    assert len(sizes) == 2
    with pytest.raises(ReadFileError, match="IMAGE_LIMIT_EXCEEDED"):
        normalize_image_document(
            path,
            replace(ReadFileConfig(), max_normalized_bytes=max(sizes) + 1),
            "auto",
            None,
        )


def test_image_workers_bounds_normalization_executor(tmp_path, monkeypatch):
    from backend.tools.read_file import image_normalizer

    path = tmp_path / "pages.tiff"
    Image.new("RGB", (10, 10), "red").save(
        path,
        save_all=True,
        append_images=[Image.new("RGB", (10, 10), "blue")],
        format="TIFF",
    )
    observed = []

    class CapturingExecutor(RealThreadPoolExecutor):
        def __init__(self, max_workers, *args, **kwargs):
            observed.append(max_workers)
            super().__init__(max_workers=max_workers, *args, **kwargs)

    monkeypatch.setattr(
        image_normalizer, "ThreadPoolExecutor", CapturingExecutor, raising=False
    )
    pages = normalize_image_document(
        path, replace(ReadFileConfig(), image_workers=2), "auto", None
    )
    _close(pages)
    assert observed == [2]
