from __future__ import annotations

import time
import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path

from backend.config import OCRServiceConfig, PROJECT_ROOT, ReadFileConfig, get_config

from .cache import ReadFileCache, build_cache_key
from .errors import ReadFileError, ReadFileErrorCode
from .file_validator import ValidatedSource, validate_source
from .image_normalizer import normalize_image_document
from .models import EmbeddedImageResult, PageResult, ParseError, ReadFileResult
from .office_renderer import render_office_to_pdf
from .paddleocr_client import PaddleOCRClient
from .pdf_chunker import PDFChunk, PDFInfo, inspect_pdf, iter_pdf_chunks, iter_retry_pdf_chunks
from .result_assembler import assemble_pages
from .resource_guard import ResourceGuard, get_default_resource_guard
from .router import FileRoute, classify_route
from .spreadsheet_media_extractor import extract_embedded_images
from .spreadsheet_parser import parse_xlsx
from .text_reader import read_text, render_line_range


def _merge_page_result(
    existing: PageResult | None, incoming: PageResult
) -> PageResult:
    if existing is None:
        return incoming
    if existing.error is not None and incoming.error is None:
        return incoming
    return existing


class ReadFileService:
    _RETRYABLE_OCR_CODES = {
        ReadFileErrorCode.OCR_PAGE_FAILED,
    }
    def __init__(
        self,
        config: ReadFileConfig | None = None,
        *,
        ocr_config: OCRServiceConfig | None = None,
        ocr_client=None,
        cache: ReadFileCache | None = None,
        resource_guard: ResourceGuard | None = None,
    ):
        app_config = None
        if config is None or ocr_config is None:
            app_config = get_config()
        self.config = config or app_config.read_file
        self.ocr_config = ocr_config or app_config.ocr
        self._ocr_client = ocr_client
        self._owns_ocr_client = ocr_client is None
        self._cache = cache
        self._owns_cache = cache is None
        self.resource_guard = resource_guard or get_default_resource_guard(self.config)

    @property
    def ocr_client(self):
        if self._ocr_client is None:
            self._ocr_client = PaddleOCRClient(self.ocr_config, self.config)
        return self._ocr_client

    def close(self) -> None:
        if self._owns_ocr_client and self._ocr_client is not None:
            self._ocr_client.close()
            self._ocr_client = None
        if self._owns_cache and self._cache is not None:
            self._cache.close()
            self._cache = None

    @property
    def cache(self) -> ReadFileCache:
        if self._cache is None:
            self._cache = ReadFileCache(
                self.config.cache_path,
                success_ttl_seconds=self.config.cache_success_ttl_seconds,
                partial_ttl_seconds=self.config.cache_partial_ttl_seconds,
                failure_ttl_seconds=self.config.cache_failure_ttl_seconds,
            )
        return self._cache

    @staticmethod
    def _check_abort(check_abort: Callable[[], bool] | None) -> None:
        if check_abort and check_abort():
            raise ReadFileError(ReadFileErrorCode.ABORTED, "request was cancelled")

    @classmethod
    def _retryable_ocr_error(cls, error: ReadFileError) -> bool:
        return error.code in cls._RETRYABLE_OCR_CODES

    def _ocr_chunk(
        self,
        chunk: PDFChunk,
        check_abort: Callable[[], bool] | None,
        *,
        use_doc_unwarping: bool,
        recognize_seals: bool,
    ) -> list[PageResult]:
        self._check_abort(check_abort)
        self.resource_guard.check_memory()
        ocr_pages = self.ocr_client.parse_file(
            chunk.path,
            0,
            {
                "expected_pages": len(chunk.source_pages),
                "use_doc_unwarping": use_doc_unwarping,
                "recognize_seals": recognize_seals,
            },
            check_abort,
        )
        return [
            PageResult(
                source_page=source_page,
                markdown=ocr_page.markdown,
                service_page=ocr_page.source_page,
                used_doc_unwarping=use_doc_unwarping,
            )
            for source_page, ocr_page in zip(chunk.source_pages, ocr_pages, strict=True)
        ]

    def _retry_pdf_pages(
        self,
        source: Path,
        info: PDFInfo,
        source_pages: tuple[int, ...],
        check_abort: Callable[[], bool] | None,
        recognize_seals: bool,
        distorted: bool,
        normal_already_failed: bool = False,
        original_error: ReadFileError | None = None,
    ) -> list[PageResult]:
        self._check_abort(check_abort)
        if len(source_pages) > self.config.pdf_retry_chunk_pages:
            results: list[PageResult] = []
            for chunk in iter_retry_pdf_chunks(
                source,
                info,
                self.config,
                source_pages,
                self.config.pdf_retry_chunk_pages,
            ):
                try:
                    try:
                        results.extend(
                            self._ocr_chunk(
                                chunk,
                                check_abort,
                                use_doc_unwarping=distorted,
                                recognize_seals=recognize_seals,
                            )
                        )
                    except ReadFileError as exc:
                        if not self._retryable_ocr_error(exc):
                            raise
                        results.extend(
                            self._retry_pdf_pages(
                                source,
                                info,
                                chunk.owned_source_pages,
                                check_abort,
                                recognize_seals,
                                distorted,
                                original_error=original_error or exc,
                            )
                        )
                finally:
                    chunk.close()
            return results
        if len(source_pages) > 1:
            results = []
            for page in source_pages:
                results.extend(
                    self._retry_pdf_pages(
                        source,
                        info,
                        (page,),
                        check_abort,
                        recognize_seals,
                        distorted,
                        original_error=original_error,
                    )
                )
            return results
        page = source_pages[0]
        chunk = next(
            iter_retry_pdf_chunks(source, info, self.config, (page,), 1)
        )
        try:
            if distorted and normal_already_failed:
                error = original_error or ReadFileError(
                    ReadFileErrorCode.OCR_PAGE_FAILED, "distorted page quality failed"
                )
                return [
                    PageResult(
                        page,
                        "",
                        error=ParseError(error.code.value, error.safe_message, source_page=page),
                    )
                ]
            try:
                if normal_already_failed:
                    raise original_error or ReadFileError(
                        ReadFileErrorCode.OCR_PAGE_FAILED, "page quality failed"
                    )
                return self._ocr_chunk(
                    chunk,
                    check_abort,
                    use_doc_unwarping=distorted,
                    recognize_seals=recognize_seals,
                )
            except ReadFileError as exc:
                if not self._retryable_ocr_error(exc):
                    raise
                try:
                    if distorted:
                        raise
                    return self._ocr_chunk(
                        chunk,
                        check_abort,
                        use_doc_unwarping=True,
                        recognize_seals=recognize_seals,
                    )
                except ReadFileError as retry_error:
                    if not self._retryable_ocr_error(retry_error):
                        raise
                    preserved = original_error or exc
                    error = ParseError(
                        preserved.code.value,
                        preserved.safe_message,
                        source_page=page,
                    )
                    return [PageResult(page, "", error=error)]
        finally:
            chunk.close()

    def _read_pdf(
        self,
        source: Path,
        password: str | None,
        source_format: str,
        check_abort: Callable[[], bool] | None,
        document_mode: str,
        recognize_seals: bool,
        info: PDFInfo | None = None,
    ) -> ReadFileResult:
        info = info or inspect_pdf(source, password, self.config)
        distorted = document_mode == "distorted"
        page_results: dict[int, PageResult] = {}
        retries = 0
        for chunk in iter_pdf_chunks(source, info, self.config):
            try:
                self._check_abort(check_abort)
                try:
                    results = self._ocr_chunk(
                        chunk,
                        check_abort,
                        use_doc_unwarping=distorted,
                        recognize_seals=recognize_seals,
                    )
                except ReadFileError as exc:
                    if not self._retryable_ocr_error(exc):
                        raise
                    retries += 1
                    results = self._retry_pdf_pages(
                        source,
                        info,
                        chunk.owned_source_pages,
                        check_abort,
                        recognize_seals,
                        distorted,
                        normal_already_failed=len(chunk.owned_source_pages) == 1,
                        original_error=exc,
                    )
                for result in results:
                    page_results[result.source_page] = _merge_page_result(
                        page_results.get(result.source_page), result
                    )
            finally:
                chunk.close()
        assembled = assemble_pages(
            source,
            list(page_results.values()),
            {
                "source_format": source_format,
                "engine": "PaddleOCR-VL-1.6",
                "model_version": "1.6",
                "pipeline_version": "v1.6",
                "retries": retries,
            },
        )
        if not assembled.success:
            raise ReadFileError(ReadFileErrorCode.OCR_PAGE_FAILED, "all document pages failed")
        return assembled

    def _read_image(
        self,
        source: Path,
        source_format: str,
        document_mode: str,
        recognize_seals: bool,
        check_abort: Callable[[], bool] | None,
    ) -> ReadFileResult:
        normalized_pages = normalize_image_document(
            source, self.config, document_mode, check_abort
        )
        results: list[PageResult] = []
        try:
            for index, normalized in enumerate(normalized_pages, 1):
                self._check_abort(check_abort)
                try:
                    self.resource_guard.check_memory()
                    use_unwarping = document_mode == "distorted"
                    try:
                        pages = self.ocr_client.parse_file(
                            normalized.path,
                            1,
                            {
                                "expected_pages": 1,
                                "recognize_seals": recognize_seals,
                                "use_doc_unwarping": use_unwarping,
                            },
                            check_abort,
                        )
                    except ReadFileError as exc:
                        if (
                            exc.code is not ReadFileErrorCode.OCR_PAGE_FAILED
                            or use_unwarping
                        ):
                            raise
                        pages = self.ocr_client.parse_file(
                            normalized.path,
                            1,
                            {
                                "expected_pages": 1,
                                "recognize_seals": recognize_seals,
                                "use_doc_unwarping": True,
                            },
                            check_abort,
                        )
                    results.append(
                        PageResult(
                            source_page=normalized.source_page or index,
                            markdown=pages[0].markdown,
                            warnings=list(normalized.warnings),
                        )
                    )
                except ReadFileError as exc:
                    if not self._retryable_ocr_error(exc):
                        raise
                    results.append(
                        PageResult(
                            source_page=normalized.source_page or index,
                            markdown="",
                            warnings=list(normalized.warnings),
                            error=ParseError(
                                ReadFileErrorCode.OCR_PAGE_FAILED.value,
                                exc.safe_message,
                                source_page=normalized.source_page or index,
                            ),
                        )
                    )
        finally:
            for normalized in normalized_pages:
                normalized.close()
        assembled = assemble_pages(
            source,
            results,
            {
                "source_format": source_format,
                "normalized_format": "png",
                "engine": "PaddleOCR-VL-1.6",
                "model_version": "1.6",
                "pipeline_version": "v1.6",
            },
        )
        if not assembled.success:
            raise ReadFileError(ReadFileErrorCode.OCR_PAGE_FAILED, "all image pages failed")
        return assembled

    def _render_result(
        self,
        result: ReadFileResult,
        start_line: int | None,
        end_line: int | None,
    ) -> str:
        return render_line_range(
            result.markdown,
            start_line,
            end_line,
            f"格式: {result.source_format} | 引擎: {result.engine} | 缓存: {'命中' if result.cache_hit else '未命中'}",
        )

    def _read_xlsx(
        self,
        source: Path,
        check_abort: Callable[[], bool] | None,
    ) -> ReadFileResult:
        self._check_abort(check_abort)
        sheets = parse_xlsx(source, self.config)
        media_sources = extract_embedded_images(source, self.config)
        media_results: dict[str, tuple[str, list[EmbeddedImageResult]]] = {}
        try:
            for media in media_sources:
                self._check_abort(check_abort)
                normalized = normalize_image_document(
                    media.path, self.config, "auto", check_abort
                )
                try:
                    if not normalized:
                        raise ReadFileError(
                            ReadFileErrorCode.INVALID_IMAGE,
                            "embedded image normalization returned no page",
                        )
                    self.resource_guard.check_memory()
                    ocr_pages = self.ocr_client.parse_file(
                        normalized[0].path,
                        1,
                        {
                            "expected_pages": 1,
                            "recognize_seals": False,
                            "use_doc_unwarping": False,
                        },
                        check_abort,
                    )
                    markdown = ocr_pages[0].markdown
                finally:
                    for page in normalized:
                        page.close()
                placements = [
                    EmbeddedImageResult(
                        sha256=media.sha256,
                        sheet_name=sheet_name,
                        anchor=anchor,
                        markdown=markdown,
                        media_name=media.media_name,
                    )
                    for sheet_name, anchor in media.placements
                ]
                media_results[media.sha256] = (markdown, placements)
        finally:
            for media in media_sources:
                media.close()

        rendered_sheets = []
        all_images: list[EmbeddedImageResult] = []
        for sheet in sheets:
            additions = []
            sheet_images = []
            for _, placements in media_results.values():
                for image in placements:
                    if image.sheet_name != sheet.name:
                        continue
                    sheet_images.append(image)
                    all_images.append(image)
                    additions.append(
                        f"### 嵌入图片：{sheet.name}!{image.anchor}\n"
                        f"<!-- read-file: sheet={sheet.name} anchor={image.anchor} "
                        f"media_sha256={image.sha256} engine=PaddleOCR-VL-1.6 -->\n\n"
                        f"{image.markdown}"
                    )
            markdown = (
                f"<!-- read-file: source_sheet={sheet.name} source_format=xlsx "
                "engine=MarkItDown -->\n\n"
                f"{sheet.markdown}"
            )
            if additions:
                markdown += "\n\n" + "\n\n".join(additions)
            rendered_sheets.append(
                replace(sheet, markdown=markdown, embedded_images=sheet_images)
            )
        markdown = "\n\n".join(sheet.markdown for sheet in rendered_sheets)
        return ReadFileResult(
            success=True,
            markdown=markdown,
            source_path=str(source),
            source_format="xlsx",
            normalized_format=None,
            engine="MarkItDown + PaddleOCR-VL-1.6",
            model_version="1.6",
            pipeline_version="v1.6",
            spreadsheet_sheets=rendered_sheets,
        )

    @staticmethod
    def _source_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(8 * 1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _cache_key(
        self,
        source: Path,
        *,
        detected_format: str,
        route: str,
        password: str | None,
        document_mode: str,
        recognize_seals: bool,
    ) -> str:
        pipeline_path = Path(self.ocr_config.pipeline_config_path).expanduser()
        if not pipeline_path.is_absolute():
            pipeline_path = PROJECT_ROOT / pipeline_path
        pipeline_hash = (
            hashlib.sha256(pipeline_path.read_bytes()).hexdigest()
            if pipeline_path.is_file()
            else "missing"
        )
        config_hash = hashlib.sha256(
            json.dumps(asdict(self.config), sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return build_cache_key(
            self._source_sha256(source),
            "read-file-v7-20260727",
            "PaddleOCR-VL-1.6",
            f"{pipeline_hash}:{config_hash}",
            {
                "password_digest": self.cache.password_digest(password),
                "detected_format": detected_format,
                "route": route,
                "document_mode": document_mode,
                "recognize_seals": recognize_seals,
            },
        )

    def _read_visual_route(
        self,
        validated: ValidatedSource,
        route: FileRoute,
        password: str | None,
        document_mode: str,
        recognize_seals: bool,
        check_abort: Callable[[], bool] | None,
    ) -> ReadFileResult:
        if route is FileRoute.IMAGE:
            return self._read_image(
                validated.path,
                validated.detected_type.extension.lstrip("."),
                document_mode,
                recognize_seals,
                check_abort,
            )
        if route is FileRoute.PDF:
            return self._read_pdf(
                validated.path,
                password,
                "pdf",
                check_abort,
                document_mode,
                recognize_seals,
            )
        if route is FileRoute.VISUAL_OFFICE:
            if password:
                raise ReadFileError(
                    ReadFileErrorCode.OFFICE_PASSWORD_UNSUPPORTED,
                    "LibreOffice Writer/Impress password decryption is not supported; password applies only to encrypted PDF inputs",
                )
            self._check_abort(check_abort)
            temporary_pdf = render_office_to_pdf(
                validated.path, self.config, check_abort
            )
            try:
                result = self._read_pdf(
                    temporary_pdf.path,
                    None,
                        temporary_pdf.source_format,
                        check_abort,
                        document_mode,
                        recognize_seals,
                )
                if temporary_pdf.source_format in {"ppt", "pptx"}:
                    pages = [
                        replace(page, source_slide=page.source_page)
                        for page in result.pages
                    ]
                    result = assemble_pages(
                        validated.path,
                        pages,
                        {
                            "source_format": temporary_pdf.source_format,
                            "engine": result.engine,
                            "model_version": result.model_version,
                            "pipeline_version": result.pipeline_version,
                            "retries": result.retries,
                        },
                    )
                else:
                    result = replace(result, source_path=str(validated.path))
                return result
            finally:
                temporary_pdf.close()
        if route is FileRoute.XLSX:
            return self._read_xlsx(validated.path, check_abort)
        raise ReadFileError(ReadFileErrorCode.UNSUPPORTED_FORMAT, "document route is unavailable")

    def read(
        self,
        file_path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        encoding: str | None = None,
        password: str | None = None,
        document_mode: str = "auto",
        recognize_seals: bool = False,
        check_abort: Callable[[], bool] | None = None,
    ) -> str:
        started = time.monotonic()
        try:
            if document_mode not in {"auto", "distorted"}:
                raise ReadFileError(
                    ReadFileErrorCode.INVALID_DOCUMENT_MODE,
                    "document_mode must be 'auto' or 'distorted'",
                )
            self._check_abort(check_abort)
            validated = validate_source(file_path, self.config, password)
            route = classify_route(validated.detected_type)
            if route is FileRoute.TEXT:
                text, used_encoding = read_text(validated.path, encoding)
                return render_line_range(
                    text, start_line, end_line, f"编码: {used_encoding}"
                )
            if route is FileRoute.PLACEHOLDER_SPREADSHEET:
                raise ReadFileError(
                    ReadFileErrorCode.UNSUPPORTED_FORMAT,
                    f"{validated.detected_type.extension} is recognized; conversion is planned but unavailable in this release",
                )
            pdf_info = None
            if route is FileRoute.PDF:
                pdf_info = inspect_pdf(validated.path, password, self.config)
            cache_key = self._cache_key(
                validated.path,
                detected_format=validated.detected_type.extension,
                route=route.value,
                password=password,
                document_mode=document_mode,
                recognize_seals=recognize_seals,
            )
            cached = self.cache.get(cache_key)
            if cached is not None:
                return self._render_result(
                    replace(cached, cache_hit=True), start_line, end_line
                )
            with self.resource_guard.acquire(check_abort):
                cached = self.cache.get(cache_key)
                if cached is not None:
                    return self._render_result(
                        replace(cached, cache_hit=True), start_line, end_line
                    )
                try:
                    result = self._read_visual_route(
                        validated,
                        route,
                        password,
                        document_mode,
                        recognize_seals,
                        check_abort,
                    )
                except ReadFileError as exc:
                    if exc.code in {
                        ReadFileErrorCode.ABORTED,
                        ReadFileErrorCode.RESOURCE_BUSY,
                        ReadFileErrorCode.MEMORY_RESERVE,
                    }:
                        raise
                    failure = ReadFileResult(
                        success=False,
                        markdown=str(exc),
                        source_path=str(validated.path),
                        source_format=validated.detected_type.extension.lstrip("."),
                        normalized_format=None,
                        engine="PaddleOCR-VL-1.6",
                        model_version="1.6",
                        pipeline_version="v1.6",
                        errors=[ParseError(exc.code.value, exc.safe_message)],
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
                    self.cache.put(cache_key, failure, "failure")
                    raise
                result = replace(
                    result, elapsed_ms=int((time.monotonic() - started) * 1000)
                )
                status = "partial" if result.errors else "success"
                self.cache.put(cache_key, result, status)
            return self._render_result(result, start_line, end_line)
        except ReadFileError as exc:
            return str(exc)
