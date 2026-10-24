from __future__ import annotations

import hashlib
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import imagehash
import tinycss2
from defusedxml import ElementTree as SafeElementTree
from PIL import Image, ImageOps, UnidentifiedImageError

from backend.config import ReadFileConfig

from .errors import ReadFileError, ReadFileErrorCode


@dataclass
class NormalizedPage:
    path: Path
    sha256: str
    width: int
    height: int
    source_page: int | None = None
    source_frame: int | None = None
    warnings: list[str] = field(default_factory=list)
    _temporary_directory: tempfile.TemporaryDirectory[str] | None = field(
        default=None, repr=False
    )

    @property
    def bytes(self) -> bytes:
        return self.path.read_bytes()

    def close(self) -> None:
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None


def _abort_if_requested(check_abort: Callable[[], bool] | None) -> None:
    if check_abort and check_abort():
        raise ReadFileError(ReadFileErrorCode.ABORTED, "request was cancelled")


def _register_optional_plugins(extension: str) -> None:
    if extension in {".heic", ".heif", ".avif"}:
        try:
            from pillow_heif import register_avif_opener, register_heif_opener

            register_heif_opener()
            register_avif_opener()
        except ImportError as exc:
            raise ReadFileError(
                ReadFileErrorCode.INVALID_IMAGE,
                "HEIF/AVIF decoder plugin is not installed",
            ) from exc


def _validate_dimensions(
    width: int, height: int, frames: int, config: ReadFileConfig
) -> None:
    if width <= 0 or height <= 0 or frames <= 0:
        raise ReadFileError(ReadFileErrorCode.INVALID_IMAGE, "image dimensions are invalid")
    pixels = width * height
    if pixels > config.max_image_pixels:
        raise ReadFileError(
            ReadFileErrorCode.IMAGE_LIMIT_EXCEEDED, "image pixel count exceeds limit"
        )
    if frames > config.max_image_frames:
        raise ReadFileError(
            ReadFileErrorCode.IMAGE_LIMIT_EXCEEDED, "image frame count exceeds limit"
        )
    if pixels * frames * 4 > config.max_decoded_image_bytes:
        raise ReadFileError(
            ReadFileErrorCode.IMAGE_LIMIT_EXCEEDED, "estimated decoded image bytes exceed limit"
        )


def _to_rgb(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image)
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def _resize(image: Image.Image, config: ReadFileConfig) -> Image.Image:
    longest = max(image.size)
    if longest <= config.max_image_dimension:
        return image
    scale = config.max_image_dimension / longest
    size = (
        max(1, round(image.width * scale)),
        max(1, round(image.height * scale)),
    )
    return image.resize(size, Image.Resampling.LANCZOS)


def _write_page(
    image: Image.Image,
    config: ReadFileConfig,
    *,
    source_page: int | None,
    source_frame: int | None,
) -> NormalizedPage:
    image = _resize(_to_rgb(image), config)
    temporary = tempfile.TemporaryDirectory(prefix="read-file-image-")
    path = Path(temporary.name) / "page.png"
    image.save(path, "PNG", optimize=True)
    size = path.stat().st_size
    if size > config.max_normalized_bytes:
        temporary.cleanup()
        raise ReadFileError(
            ReadFileErrorCode.IMAGE_LIMIT_EXCEEDED, "normalized image exceeds byte limit"
        )
    return NormalizedPage(
        path=path,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        width=image.width,
        height=image.height,
        source_page=source_page,
        source_frame=source_frame,
        _temporary_directory=temporary,
    )


def _validate_svg_presentation_value(attribute: str, raw_value: str) -> None:
    color_attributes = {
        "color",
        "fill",
        "flood-color",
        "lighting-color",
        "stop-color",
        "stroke",
    }
    safe_color_functions = {"rgb", "rgba", "hsl", "hsla"}
    tokens = tinycss2.parse_component_value_list(raw_value)
    for token in tokens:
        if token.type == "error":
            raise ReadFileError(
                ReadFileErrorCode.INVALID_IMAGE,
                "SVG presentation value is malformed",
            )
        if token.type == "url":
            fragment = str(token.value)
            if attribute in {"fill", "stroke"} and re.fullmatch(
                r"#[A-Za-z_][A-Za-z0-9_.:-]*", fragment
            ):
                continue
            raise ReadFileError(
                ReadFileErrorCode.INVALID_IMAGE,
                "SVG resource references are forbidden",
            )
        if token.type != "function":
            continue
        if token.lower_name == "url":
            arguments = token.arguments
            if (
                attribute in {"fill", "stroke"}
                and len(arguments) == 1
                and arguments[0].type == "string"
                and re.fullmatch(
                    r"#[A-Za-z_][A-Za-z0-9_.:-]*", str(arguments[0].value)
                )
            ):
                continue
            raise ReadFileError(
                ReadFileErrorCode.INVALID_IMAGE,
                "SVG resource references are forbidden",
            )
        if attribute not in color_attributes or token.lower_name not in safe_color_functions:
            raise ReadFileError(
                ReadFileErrorCode.INVALID_IMAGE,
                "SVG resource-capable presentation functions are forbidden",
            )
        if any(
            argument.type
            not in {"number", "percentage", "dimension", "literal", "whitespace"}
            for argument in token.arguments
        ):
            raise ReadFileError(
                ReadFileErrorCode.INVALID_IMAGE,
                "SVG color function contains unsafe tokens",
            )


def _safe_svg_png(path: Path, config: ReadFileConfig) -> bytes:
    try:
        source = path.read_bytes()
        root = SafeElementTree.fromstring(source)
    except Exception as exc:
        raise ReadFileError(ReadFileErrorCode.INVALID_IMAGE, "SVG XML is malformed") from exc
    forbidden_elements = {
        "script",
        "style",
        "foreignobject",
        "iframe",
        "object",
        "embed",
    }
    resource_capable_attributes = {
        "clip-path",
        "color-profile",
        "cursor",
        "fill",
        "filter",
        "font-family",
        "marker",
        "marker-end",
        "marker-mid",
        "marker-start",
        "mask",
        "stroke",
    }
    color_attributes = {
        "color",
        "fill",
        "flood-color",
        "lighting-color",
        "stop-color",
        "stroke",
    }
    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1].lower()
        if local_name in forbidden_elements:
            raise ReadFileError(ReadFileErrorCode.INVALID_IMAGE, "SVG contains forbidden elements")
        for name, raw_value in element.attrib.items():
            value = str(raw_value).strip().lower()
            attribute = name.rsplit("}", 1)[-1].lower()
            if attribute == "style":
                raise ReadFileError(
                    ReadFileErrorCode.INVALID_IMAGE, "SVG style attributes are forbidden"
                )
            if "\\" in value:
                raise ReadFileError(
                    ReadFileErrorCode.INVALID_IMAGE,
                    "SVG CSS escape sequences are forbidden",
                )
            if "url(" in value or "@import" in value:
                if attribute not in resource_capable_attributes | color_attributes:
                    raise ReadFileError(ReadFileErrorCode.INVALID_IMAGE, "SVG resource references are forbidden")
            if attribute in resource_capable_attributes | color_attributes:
                _validate_svg_presentation_value(attribute, str(raw_value).strip())
            if attribute in resource_capable_attributes and any(
                token in value for token in ("file:", "http:", "https:", "data:")
            ):
                raise ReadFileError(
                    ReadFileErrorCode.INVALID_IMAGE,
                    "SVG resource-capable presentation values are forbidden",
                )
            if attribute in {"href", "src"} and value and not value.startswith("#"):
                raise ReadFileError(ReadFileErrorCode.INVALID_IMAGE, "SVG external references are forbidden")

    def svg_length(value: str | None) -> float | None:
        if not value:
            return None
        match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(?:px)?\s*", value)
        return float(match.group(1)) if match else None

    width = svg_length(root.attrib.get("width"))
    height = svg_length(root.attrib.get("height"))
    view_box = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    view_width: float | None = None
    view_height: float | None = None
    if view_box:
        try:
            _, _, view_width, view_height = [float(item) for item in re.split(r"[\s,]+", view_box.strip())]
            if view_width <= 0 or view_height <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ReadFileError(ReadFileErrorCode.INVALID_IMAGE, "SVG viewBox is invalid") from None
    if width is None and height is None and view_width and view_height:
        width, height = view_width, view_height
    elif width is None and height is not None and view_width and view_height:
        width = height * view_width / view_height
    elif height is None and width is not None and view_width and view_height:
        height = width * view_height / view_width
    width = width or 300.0
    height = height or 150.0
    scale = min(1.0, config.max_image_dimension / max(width, height))
    output_width = max(1, round(width * scale))
    output_height = max(1, round(height * scale))
    try:
        from cairosvg.surface import PNGSurface

        def deny_fetch(*args: object, **kwargs: object) -> bytes:
            del args, kwargs
            raise ReadFileError(
                ReadFileErrorCode.INVALID_IMAGE,
                "SVG resource fetching is forbidden",
            )

        return PNGSurface.convert(
            bytestring=source,
            output_width=output_width,
            output_height=output_height,
            url_fetcher=deny_fetch,
        )
    except ReadFileError:
        raise
    except Exception as exc:
        raise ReadFileError(ReadFileErrorCode.INVALID_IMAGE, "SVG rasterization failed") from exc


def normalize_image_document(
    source: str | Path,
    config: ReadFileConfig,
    document_mode: str,
    check_abort: Callable[[], bool] | None,
) -> list[NormalizedPage]:
    del document_mode
    path = Path(source)
    _abort_if_requested(check_abort)
    _register_optional_plugins(path.suffix.lower())
    svg_temporary: tempfile.TemporaryDirectory[str] | None = None
    if path.suffix.lower() == ".svg":
        svg_temporary = tempfile.TemporaryDirectory(prefix="read-file-svg-")
        raster_path = Path(svg_temporary.name) / "source.png"
        raster_path.write_bytes(_safe_svg_png(path, config))
        path = raster_path
    old_max_pixels = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = config.max_image_pixels
    pages: list[NormalizedPage] = []
    candidates: list[tuple[Image.Image, int, int | None, list[str]]] = []
    try:
        with Image.open(path) as image:
            frames = int(getattr(image, "n_frames", 1))
            _validate_dimensions(image.width, image.height, frames, config)
            previous_fingerprint: tuple[imagehash.ImageHash, imagehash.ImageHash] | None = None
            decoded_bytes = 0
            for frame_index in range(frames):
                _abort_if_requested(check_abort)
                image.seek(frame_index)
                frame = image.copy()
                _validate_dimensions(frame.width, frame.height, 1, config)
                rgb = _to_rgb(frame)
                decoded_bytes += rgb.width * rgb.height * 4
                if decoded_bytes > config.max_decoded_image_bytes:
                    raise ReadFileError(
                        ReadFileErrorCode.IMAGE_LIMIT_EXCEEDED,
                        "aggregate decoded image bytes exceed limit",
                    )
                fingerprint = (imagehash.phash(rgb), imagehash.colorhash(rgb))
                perceptual_duplicate = bool(
                    previous_fingerprint
                    and fingerprint[0] - previous_fingerprint[0] <= 2
                    and fingerprint[1] - previous_fingerprint[1] <= 1
                )
                if path.suffix.lower() == ".gif" and perceptual_duplicate:
                    if candidates:
                        candidates[-1][3].append(
                            f"consecutive duplicate frame {frame_index + 1} was removed"
                        )
                    continue
                previous_fingerprint = fingerprint
                candidates.append(
                    (
                        rgb,
                        frame_index + 1 if frames > 1 else 1,
                        frame_index + 1 if path.suffix.lower() == ".gif" else None,
                        [],
                    )
                )
        with ThreadPoolExecutor(max_workers=max(1, config.image_workers)) as executor:
            futures = [
                executor.submit(
                    _write_page,
                    image,
                    config,
                    source_page=source_page,
                    source_frame=source_frame,
                )
                for image, source_page, source_frame, _ in candidates
            ]
            normalized_bytes = 0
            for future, candidate in zip(futures, candidates):
                _abort_if_requested(check_abort)
                page = future.result()
                page.warnings.extend(candidate[3])
                pages.append(page)
                normalized_bytes += page.path.stat().st_size
                if normalized_bytes > config.max_normalized_bytes:
                    raise ReadFileError(
                        ReadFileErrorCode.IMAGE_LIMIT_EXCEEDED,
                        "aggregate normalized image bytes exceed limit",
                    )
    except ReadFileError:
        for page in pages:
            page.close()
        raise
    except Image.DecompressionBombError as exc:
        for page in pages:
            page.close()
        raise ReadFileError(
            ReadFileErrorCode.IMAGE_LIMIT_EXCEEDED,
            "image pixel count exceeds limit",
        ) from exc
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        for page in pages:
            page.close()
        label = "HEIF/AVIF decode failed" if source and Path(source).suffix.lower() in {".heic", ".heif", ".avif"} else "image decode failed"
        raise ReadFileError(ReadFileErrorCode.INVALID_IMAGE, label) from exc
    finally:
        Image.MAX_IMAGE_PIXELS = old_max_pixels
        if svg_temporary is not None:
            svg_temporary.cleanup()
    return pages
