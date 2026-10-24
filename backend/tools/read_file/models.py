from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ParseError:
    code: str
    message: str
    source_page: int | None = None
    sheet_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ParseError":
        return cls(**value)


@dataclass(frozen=True)
class PageResult:
    source_page: int
    markdown: str
    source_slide: int | None = None
    service_page: int | None = None
    warnings: list[str] = field(default_factory=list)
    error: ParseError | None = None
    used_doc_unwarping: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PageResult":
        data = dict(value)
        if isinstance(data.get("error"), dict):
            data["error"] = ParseError.from_dict(data["error"])
        return cls(**data)


@dataclass(frozen=True)
class EmbeddedImageResult:
    sha256: str
    sheet_name: str
    anchor: str
    markdown: str
    media_name: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EmbeddedImageResult":
        return cls(**value)


@dataclass(frozen=True)
class SheetResult:
    name: str
    index: int
    markdown: str
    embedded_images: list[EmbeddedImageResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SheetResult":
        data = dict(value)
        data["embedded_images"] = [
            EmbeddedImageResult.from_dict(item)
            for item in data.get("embedded_images", [])
        ]
        return cls(**data)


@dataclass(frozen=True)
class ReadFileResult:
    success: bool
    markdown: str
    source_path: str
    source_format: str
    normalized_format: str | None
    engine: str
    model_version: str
    pages: list[PageResult] = field(default_factory=list)
    spreadsheet_sheets: list[SheetResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)
    cache_hit: bool = False
    elapsed_ms: int = 0
    pipeline_version: str = "v1.6"
    retries: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ReadFileResult":
        data = dict(value)
        data["pages"] = [PageResult.from_dict(item) for item in data.get("pages", [])]
        data["spreadsheet_sheets"] = [
            SheetResult.from_dict(item) for item in data.get("spreadsheet_sheets", [])
        ]
        data["errors"] = [ParseError.from_dict(item) for item in data.get("errors", [])]
        return cls(**data)
