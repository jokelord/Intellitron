from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from backend.tools.read_file.errors import ReadFileError, ReadFileErrorCode
from backend.tools.read_file.models import (
    EmbeddedImageResult,
    PageResult,
    ParseError,
    ReadFileResult,
    SheetResult,
)


def test_result_round_trip_preserves_partial_success_and_nested_records():
    result = ReadFileResult(
        success=True,
        markdown="ok",
        source_path="~/example.pdf",
        source_format="pdf",
        normalized_format=None,
        engine="PaddleOCR-VL-1.6",
        model_version="1.6",
        pipeline_version="v1.6",
        pages=[PageResult(source_page=1, markdown="page")],
        spreadsheet_sheets=[
            SheetResult(
                name="表一",
                index=0,
                markdown="sheet",
                embedded_images=[
                    EmbeddedImageResult(
                        sha256="a" * 64,
                        sheet_name="表一",
                        anchor="B4:F18",
                        markdown="image",
                    )
                ],
            )
        ],
        warnings=["page 2 failed"],
        errors=[
            ParseError(code="OCR_PAGE_FAILED", message="page 2", source_page=2)
        ],
        cache_hit=False,
        elapsed_ms=10,
    )

    restored = ReadFileResult.from_dict(result.to_dict())

    assert restored == result
    assert PageResult.from_dict(result.pages[0].to_dict()) == result.pages[0]
    assert SheetResult.from_dict(result.spreadsheet_sheets[0].to_dict()) == result.spreadsheet_sheets[0]
    assert EmbeddedImageResult.from_dict(
        result.spreadsheet_sheets[0].embedded_images[0].to_dict()
    ) == result.spreadsheet_sheets[0].embedded_images[0]
    assert ParseError.from_dict(result.errors[0].to_dict()) == result.errors[0]
    assert restored.success is True
    with pytest.raises(FrozenInstanceError):
        restored.markdown = "changed"  # type: ignore[misc]


def test_read_file_error_string_is_stable_and_safe():
    error = ReadFileError(ReadFileErrorCode.UNSUPPORTED_FORMAT, "format unavailable")

    rendered = str(error)
    assert rendered.startswith("[read_file failed]")
    assert "UNSUPPORTED_FORMAT" in rendered
    assert "format unavailable" in rendered
