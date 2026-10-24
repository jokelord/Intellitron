from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import fitz

from backend.config import ReadFileConfig

from .errors import ReadFileError, ReadFileErrorCode


@dataclass(frozen=True)
class PDFInfo:
    page_count: int
    size_bytes: int
    encrypted: bool
    password: str | None = None


@dataclass
class PDFChunk:
    path: Path
    source_pages: tuple[int, ...]
    overlap_source_pages: tuple[int, ...]
    owned_source_pages: tuple[int, ...]
    _temporary_directory: tempfile.TemporaryDirectory[str]

    def close(self) -> None:
        self._temporary_directory.cleanup()

    def __enter__(self) -> "PDFChunk":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _split_owned_source_pages(
    source_pages: tuple[int, ...],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if len(source_pages) < 2:
        raise ValueError("an ownership range needs at least two pages to split")
    middle = len(source_pages) // 2
    left = source_pages[:middle]
    right = source_pages[middle:]
    if not left or not right or len(left) >= len(source_pages) or len(right) >= len(source_pages):
        raise AssertionError("PDF ownership split did not strictly shrink")
    return left, right


def _open_pdf(path: Path, password: str | None):
    try:
        document = fitz.open(path)
    except Exception as exc:
        raise ReadFileError(ReadFileErrorCode.INVALID_PDF, "PDF cannot be opened") from exc
    if document.needs_pass:
        if not password:
            document.close()
            raise ReadFileError(ReadFileErrorCode.PDF_PASSWORD_REQUIRED, "PDF password is required")
        if not document.authenticate(password):
            document.close()
            raise ReadFileError(ReadFileErrorCode.PDF_PASSWORD_REQUIRED, "PDF password is invalid")
    return document


def inspect_pdf(
    path: str | Path,
    password: str | None,
    config: ReadFileConfig,
) -> PDFInfo:
    pdf_path = Path(path)
    document = _open_pdf(pdf_path, password)
    try:
        page_count = document.page_count
        encrypted = bool(document.is_encrypted)
    finally:
        document.close()
    if page_count <= 0 or page_count > config.max_pdf_pages:
        raise ReadFileError(
            ReadFileErrorCode.INVALID_PDF, "PDF page count is outside configured limits"
        )
    return PDFInfo(page_count, pdf_path.stat().st_size, encrypted, password)


def _write_chunk(
    source: Path,
    info: PDFInfo,
    owned_source_pages: tuple[int, ...],
    byte_limit: int,
    overlap: int,
    chunk_pages: int,
) -> list[PDFChunk]:
    if not 0 <= overlap < chunk_pages:
        raise ValueError("PDF overlap must satisfy 0 <= overlap < chunk_pages")
    overlap_pages = tuple(
        range(
            max(1, owned_source_pages[0] - overlap),
            owned_source_pages[0],
        )
    )
    source_pages = overlap_pages + owned_source_pages
    if len(source_pages) > chunk_pages:
        raise AssertionError("emitted PDF chunk exceeds configured page limit")
    temporary = tempfile.TemporaryDirectory(prefix="read-file-pdf-")
    output_path = Path(temporary.name) / "chunk.pdf"
    source_document = _open_pdf(source, info.password)
    output_document = fitz.open()
    try:
        for source_page in source_pages:
            output_document.insert_pdf(
                source_document, from_page=source_page - 1, to_page=source_page - 1
            )
        output_document.save(output_path, garbage=4, deflate=True)
    finally:
        output_document.close()
        source_document.close()
    if output_path.stat().st_size > byte_limit and len(owned_source_pages) > 1:
        temporary.cleanup()
        left_pages, right_pages = _split_owned_source_pages(owned_source_pages)
        return _write_chunk(
            source,
            info,
            left_pages,
            byte_limit,
            overlap,
            chunk_pages,
        ) + _write_chunk(
            source,
            info,
            right_pages,
            byte_limit,
            overlap,
            chunk_pages,
        )
    if output_path.stat().st_size > byte_limit:
        temporary.cleanup()
        raise ReadFileError(
            ReadFileErrorCode.FILE_TOO_LARGE,
            "overlap-expanded PDF leaf exceeds chunk byte limit",
        )
    return [
        PDFChunk(
            output_path,
            source_pages,
            overlap_pages,
            owned_source_pages,
            temporary,
        )
    ]


def _page_ranges(page_count: int, chunk_pages: int, overlap: int):
    if page_count <= 0 or chunk_pages <= 0:
        raise ValueError("PDF page range settings must be positive")
    if not 0 <= overlap < chunk_pages:
        raise ValueError("PDF overlap must satisfy 0 <= overlap < chunk_pages")
    owned_start = 1
    first_range = True
    while owned_start <= page_count:
        ownership_capacity = (
            chunk_pages if first_range else max(1, chunk_pages - overlap)
        )
        owned_end = min(page_count, owned_start + ownership_capacity - 1)
        owned_pages = tuple(range(owned_start, owned_end + 1))
        overlap_pages = tuple(
            range(max(1, owned_start - overlap), owned_start)
        )
        yield overlap_pages + owned_pages, overlap_pages
        owned_start = owned_end + 1
        first_range = False


def iter_pdf_chunks(
    path: str | Path,
    info: PDFInfo,
    config: ReadFileConfig,
) -> Iterator[PDFChunk]:
    for pages, overlap_pages in _page_ranges(
        info.page_count, config.pdf_chunk_pages, config.pdf_chunk_overlap_pages
    ):
        owned_pages = tuple(page for page in pages if page not in overlap_pages)
        yield from _write_chunk(
            Path(path),
            info,
            owned_pages,
            config.pdf_chunk_bytes,
            config.pdf_chunk_overlap_pages,
            config.pdf_chunk_pages,
        )


def iter_retry_pdf_chunks(
    path: str | Path,
    info: PDFInfo,
    config: ReadFileConfig,
    source_pages: tuple[int, ...],
    chunk_pages: int,
) -> Iterator[PDFChunk]:
    if chunk_pages <= 0:
        raise ValueError("PDF retry chunk_pages must be positive")
    retry_overlap = 0
    for offset in range(0, len(source_pages), chunk_pages):
        pages = source_pages[offset : offset + chunk_pages]
        yield from _write_chunk(
            Path(path),
            info,
            pages,
            config.pdf_chunk_bytes,
            retry_overlap,
            chunk_pages,
        )
