"""Strict, provider-neutral admission for inbound Vision images."""

from __future__ import annotations

import base64
import binascii
import io
import re
import warnings
from collections.abc import Iterable, Sequence
from typing import Any

from PIL import Image, UnidentifiedImageError

from .contracts import VisionAutomationTransportMetadata, VisionValidationResult

MAX_REAL_IMAGES = 250
MAX_MANUAL_IMAGES = 9
MAX_AUTOMATION_SOURCES = 241
MAX_ENCODED_DATA_URL_BYTES = 134_217_728

_DATA_URL_RE = re.compile(
    r"\Adata:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/]*={0,2})\Z"
)
_MIME_TO_FORMAT = {
    "image/jpeg": "JPEG",
    "image/jpg": "JPEG",
    "image/png": "PNG",
    "image/gif": "GIF",
    "image/webp": "WEBP",
}
_DATA_URL_LOG_RE = re.compile(
    r"data:image.*\Z",
    re.IGNORECASE | re.DOTALL,
)
_PNG_IEND = b"\x00\x00\x00\x00IEND\xaeB\x60\x82"


class VisionRequestValidationError(ValueError):
    """A redacted, HTTP-mappable inbound Vision validation failure."""

    def __init__(
        self,
        reason: str,
        *,
        status_code: int = 422,
        image_index: int | None = None,
    ) -> None:
        self.status_code = status_code
        self.image_index = image_index
        prefix = f"image {image_index}: " if image_index is not None else ""
        super().__init__(prefix + reason)


def redact_data_urls(value: object) -> str:
    return _DATA_URL_LOG_RE.sub("[image-data-url-redacted]", str(value))


def validate_encoded_data_url_budget(encoded_bytes: int, maximum: int) -> None:
    if encoded_bytes > maximum:
        raise VisionRequestValidationError(
            "encoded image payload exceeds limit",
            status_code=413,
        )


def validate_chat_vision_request(
    messages: Sequence[Any],
    config: Any,
    *,
    metadata: VisionAutomationTransportMetadata | None = None,
) -> VisionValidationResult:
    return _validate_image_urls(
        _chat_image_urls(messages),
        config,
        metadata=metadata,
    )


def validate_agent_vision_request(
    images: Sequence[str] | None,
    config: Any,
    *,
    metadata: VisionAutomationTransportMetadata | None = None,
    historical_image_count: int = 0,
) -> VisionValidationResult:
    return _validate_image_urls(
        images or (),
        config,
        metadata=metadata,
        historical_image_count=historical_image_count,
    )


def validate_agent_final_image_count(
    current_image_count: int,
    historical_image_count: int,
) -> int:
    if (
        isinstance(current_image_count, bool)
        or not isinstance(current_image_count, int)
        or current_image_count < 0
        or isinstance(historical_image_count, bool)
        or not isinstance(historical_image_count, int)
        or historical_image_count < 0
    ):
        raise VisionRequestValidationError("invalid historical image count")
    final_image_count = historical_image_count + current_image_count
    if final_image_count > MAX_REAL_IMAGES:
        raise VisionRequestValidationError("real image count exceeds 250")
    return final_image_count


def _chat_image_urls(messages: Sequence[Any]) -> list[str]:
    urls: list[str] = []
    for message in messages:
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "image_url":
                continue
            image_url = item.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else None
            if not isinstance(url, str):
                raise VisionRequestValidationError(
                    "image_url.url must be a Data URL",
                    image_index=len(urls) + 1,
                )
            urls.append(url)
    return urls


def _validate_image_urls(
    image_urls: Iterable[str],
    config: Any,
    *,
    metadata: VisionAutomationTransportMetadata | None,
    historical_image_count: int = 0,
) -> VisionValidationResult:
    urls = list(image_urls)
    final_image_count = validate_agent_final_image_count(
        len(urls), historical_image_count
    )

    _validate_automation_metadata(metadata, len(urls))

    automation_config = getattr(config, "vision_automation", None)
    encoded_limit = getattr(
        automation_config,
        "max_encoded_payload_bytes",
        MAX_ENCODED_DATA_URL_BYTES,
    )
    encoded_bytes = sum(len(url.encode("utf-8")) for url in urls)
    validate_encoded_data_url_budget(encoded_bytes, encoded_limit)

    model_config = getattr(config, "model", None)
    supported_formats = _supported_formats(
        getattr(model_config, "supported_formats", None)
    )
    max_image_size = getattr(model_config, "max_image_size", None)
    max_dimension = getattr(model_config, "max_image_dimension", None)

    for image_index, url in enumerate(urls, start=1):
        _validate_one_image(
            url,
            image_index=image_index,
            supported_formats=supported_formats,
            max_image_size=max_image_size,
            max_dimension=max_dimension,
        )

    return VisionValidationResult(
        image_count=final_image_count,
        encoded_data_url_bytes=encoded_bytes,
    )


def _validate_automation_metadata(
    metadata: VisionAutomationTransportMetadata | None,
    actual_image_count: int,
) -> None:
    if metadata is None:
        return
    if not 0 <= metadata.manual_image_count <= MAX_MANUAL_IMAGES:
        raise VisionRequestValidationError("invalid automation manual image count")
    if not 0 <= metadata.source_count <= MAX_AUTOMATION_SOURCES:
        raise VisionRequestValidationError("invalid automation source count")
    if metadata.retained_tick_count < 0:
        raise VisionRequestValidationError("invalid automation retained tick count")
    declared = (
        metadata.manual_image_count
        + metadata.source_count * metadata.retained_tick_count
    )
    if declared != actual_image_count:
        raise VisionRequestValidationError("automation image count mismatch")


def _supported_formats(value: Any) -> set[str]:
    if not isinstance(value, str) or not value.strip():
        return {"JPEG", "PNG", "GIF", "WEBP"}
    normalized = set()
    for item in value.split(","):
        name = item.strip().lower()
        normalized.add("JPEG" if name in {"jpg", "jpeg"} else name.upper())
    return normalized


def _validate_one_image(
    url: str,
    *,
    image_index: int,
    supported_formats: set[str],
    max_image_size: int | None,
    max_dimension: int | None,
) -> None:
    match = _DATA_URL_RE.fullmatch(url)
    if match is None:
        raise VisionRequestValidationError(
            "malformed image Data URL or Base64 payload",
            image_index=image_index,
        )
    declared_mime = match.group(1).lower()
    expected_format = _MIME_TO_FORMAT.get(declared_mime)
    if expected_format is None:
        raise VisionRequestValidationError(
            "unsupported image MIME type",
            image_index=image_index,
        )
    try:
        image_bytes = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise VisionRequestValidationError(
            "malformed Base64 payload",
            image_index=image_index,
        ) from None
    if isinstance(max_image_size, int) and max_image_size > 0:
        if len(image_bytes) > max_image_size:
            raise VisionRequestValidationError(
                "decoded image exceeds per-image size limit",
                image_index=image_index,
                status_code=413,
            )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_bytes)) as image:
                detected_format = (image.format or "").upper()
                width, height = image.size
                image.verify()
            # verify() checks container structure but does not decode every
            # format's pixel data (notably GIF). Reopen and force full decode.
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
        raise VisionRequestValidationError(
            "corrupt or unsafe image payload",
            image_index=image_index,
        ) from None
    except Image.DecompressionBombWarning:
        raise VisionRequestValidationError(
            "unsafe image dimensions",
            image_index=image_index,
        ) from None

    if detected_format != expected_format:
        raise VisionRequestValidationError(
            "declared MIME does not match image format",
            image_index=image_index,
        )
    _validate_container_completeness(
        image_bytes,
        expected_format=detected_format,
        image_index=image_index,
    )
    if detected_format not in supported_formats:
        raise VisionRequestValidationError(
            "image format is not enabled",
            image_index=image_index,
        )
    if isinstance(max_dimension, int) and max_dimension > 0:
        if width > max_dimension or height > max_dimension:
            raise VisionRequestValidationError(
                "image dimensions exceed limit",
                image_index=image_index,
            )


def _validate_container_completeness(
    image_bytes: bytes,
    *,
    expected_format: str,
    image_index: int,
) -> None:
    complete = True
    if expected_format == "JPEG":
        complete = image_bytes.endswith(b"\xff\xd9")
    elif expected_format == "PNG":
        complete = image_bytes.endswith(_PNG_IEND)
    elif expected_format == "GIF":
        complete = image_bytes.endswith(b"\x3b")
    elif expected_format == "WEBP":
        complete = (
            len(image_bytes) >= 12
            and image_bytes[:4] == b"RIFF"
            and image_bytes[8:12] == b"WEBP"
            and int.from_bytes(image_bytes[4:8], "little") + 8 == len(image_bytes)
        )
    if not complete:
        raise VisionRequestValidationError(
            "corrupt or incomplete image payload",
            image_index=image_index,
        )
