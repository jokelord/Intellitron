from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .models import PageResult, ParseError, ReadFileResult


_TEMPORARY_IMAGE_LINK = re.compile(
    r"!\[[^\]]*\]\((?:file:|https?://127\.0\.0\.1(?::\d+)?/objects/)[^)]+\)",
    re.IGNORECASE,
)
_HTML_IMAGE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


def _sanitize_markdown(markdown: str) -> str:
    markdown = _TEMPORARY_IMAGE_LINK.sub("[temporary image omitted]", markdown)
    return _HTML_IMAGE.sub("[image omitted]", markdown).strip()


def assemble_pages(
    source: str | Path,
    pages: list[PageResult],
    metadata: dict[str, Any],
) -> ReadFileResult:
    source_format = str(metadata.get("source_format") or Path(source).suffix.lstrip("."))
    engine = str(metadata.get("engine", "PaddleOCR-VL-1.6"))
    model_version = str(metadata.get("model_version", "1.6"))
    pipeline_version = str(metadata.get("pipeline_version", "v1.6"))
    sections: list[str] = []
    warnings: list[str] = []
    errors: list[ParseError] = []
    successful_pages = 0
    for page in sorted(pages, key=lambda item: item.source_page):
        provenance = [
            f"source_page={page.source_page}",
            f"source_format={source_format}",
            f"engine={engine}",
            f"model={model_version}",
            f"pipeline={pipeline_version}",
        ]
        if page.source_slide is not None:
            provenance.append(f"source_slide={page.source_slide}")
        sections.append(f"<!-- read-file: {' '.join(provenance)} -->")
        if page.error is not None:
            errors.append(page.error)
            warnings.append(
                f"page {page.source_page} failed: {page.error.code}: {page.error.message}"
            )
            sections.append(
                f"[Page {page.source_page} failed: {page.error.code}: {page.error.message}]"
            )
        else:
            successful_pages += 1
            sections.append(_sanitize_markdown(page.markdown))
        warnings.extend(page.warnings)
    if warnings:
        sections.append("## Warnings\n" + "\n".join(f"- {item}" for item in warnings))
    return ReadFileResult(
        success=successful_pages > 0,
        markdown="\n\n".join(sections).strip(),
        source_path=str(Path(source)),
        source_format=source_format,
        normalized_format=metadata.get("normalized_format"),
        engine=engine,
        model_version=model_version,
        pipeline_version=pipeline_version,
        pages=pages,
        warnings=warnings,
        errors=errors,
        cache_hit=bool(metadata.get("cache_hit", False)),
        elapsed_ms=int(metadata.get("elapsed_ms", 0)),
        retries=int(metadata.get("retries", 0)),
    )
