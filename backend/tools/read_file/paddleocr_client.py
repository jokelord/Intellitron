from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from backend.config import OCRServiceConfig, ReadFileConfig

from .errors import ReadFileError, ReadFileErrorCode
from .temporary_object_server import TemporaryObjectServer


_MARKDOWN_IGNORED_LABELS = {
    "number",
    "header",
    "header_image",
    "footer",
    "footer_image",
}


def _page_global_block_ids(page: dict[str, Any]) -> set[int]:
    pruned = page.get("prunedResult")
    blocks = pruned.get("parsing_res_list") if isinstance(pruned, dict) else None
    if not isinstance(blocks, list):
        return set()
    return {
        block_id
        for block in blocks
        if isinstance(block, dict)
        and not isinstance((block_id := block.get("global_block_id")), bool)
        and isinstance(block_id, int)
    }


def _is_merged_table_continuation(
    page: dict[str, Any], prior_global_block_ids: set[int]
) -> bool:
    pruned = page.get("prunedResult")
    blocks = pruned.get("parsing_res_list") if isinstance(pruned, dict) else None
    if not isinstance(blocks, list) or not blocks:
        return False
    content_blocks = []
    for block in blocks:
        if not isinstance(block, dict):
            return False
        if block.get("block_label") in _MARKDOWN_IGNORED_LABELS:
            continue
        content_blocks.append(block)
        block_id = block.get("global_block_id")
        group_id = block.get("global_group_id")
        if (
            block.get("block_label") != "table"
            or not isinstance(block.get("block_content"), str)
            or block["block_content"].strip()
            or isinstance(block_id, bool)
            or not isinstance(block_id, int)
            or isinstance(group_id, bool)
            or not isinstance(group_id, int)
            or group_id >= block_id
            or group_id not in prior_global_block_ids
        ):
            return False
    return bool(content_blocks)


@dataclass(frozen=True)
class OCRPage:
    source_page: int
    markdown: str
    model_settings: dict[str, Any] | None = None


@dataclass(frozen=True)
class OCRServiceStatus:
    available: bool
    compatible: bool
    detail: str


def probe_ocr_endpoint(service_config: OCRServiceConfig) -> OCRServiceStatus:
    try:
        with httpx.Client(
            trust_env=False,
            timeout=service_config.connect_timeout_seconds,
        ) as client:
            response = client.get(
                f"{service_config.base_url.rstrip('/')}/openapi.json"
            )
            response.raise_for_status()
            schema = response.json()
        compatible = isinstance(
            schema.get("paths", {}).get("/layout-parsing", {}).get("post"), dict
        )
        return OCRServiceStatus(
            True, compatible, "healthy" if compatible else "incompatible API"
        )
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        return OCRServiceStatus(False, False, str(exc))


class PaddleOCRClient:
    def __init__(
        self,
        service_config: OCRServiceConfig,
        read_file_config: ReadFileConfig,
        *,
        object_server: TemporaryObjectServer | None = None,
        http_client: httpx.Client | None = None,
    ):
        self.service_config = service_config
        self.read_file_config = read_file_config
        self._owns_object_server = object_server is None
        self.object_server = object_server or TemporaryObjectServer(
            read_file_config.object_server_port
        )
        timeout = httpx.Timeout(
            service_config.request_timeout_seconds,
            connect=service_config.connect_timeout_seconds,
            read=service_config.read_timeout_seconds,
        )
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.Client(timeout=timeout, trust_env=False)

    def close(self) -> None:
        if self._owns_http_client:
            self.http_client.close()
        if self._owns_object_server:
            self.object_server.close()

    def probe(self) -> OCRServiceStatus:
        return probe_ocr_endpoint(self.service_config)

    def _file_value(self, path: Path) -> str:
        if path.stat().st_size <= self.read_file_config.base64_threshold_bytes:
            return base64.b64encode(path.read_bytes()).decode("ascii")
        return self.object_server.register(
            path, self.read_file_config.object_url_ttl_seconds
        ).url

    def _payload(self, path: Path, file_type: int, options: dict[str, Any]) -> dict[str, Any]:
        return {
            "file": self._file_value(path),
            "fileType": file_type,
            "useDocOrientationClassify": True,
            "useDocUnwarping": bool(options.get("use_doc_unwarping", False)),
            "useLayoutDetection": True,
            "useChartRecognition": True,
            "useSealRecognition": bool(options.get("recognize_seals", False)),
            "useOcrForImageBlock": True,
            "formatBlockContent": True,
            "mergeLayoutBlocks": True,
            "layoutShapeMode": "auto",
            "markdownIgnoreLabels": [
                "number", "header", "header_image", "footer", "footer_image"
            ],
            "prettifyMarkdown": True,
            "showFormulaNumber": True,
            "restructurePages": True,
            "mergeTables": True,
            "relevelTitles": True,
            "returnMarkdownImages": False,
            "visualize": False,
            "maxNewTokens": self.read_file_config.ocr_max_new_tokens,
            "logId": f"read-file-{uuid.uuid4().hex}",
        }

    @staticmethod
    def _parse_response(
        body: Any,
        expected_pages: int | None,
        *,
        min_markdown_characters: int,
        severe_tilt_degrees: int,
        classify_severe_tilt: bool,
    ) -> list[OCRPage]:
        if not isinstance(body, dict) or body.get("errorCode") not in (None, 0):
            raise ReadFileError(ReadFileErrorCode.OCR_RESPONSE_INVALID, "PaddleX returned an error")
        result = body.get("result")
        pages = result.get("layoutParsingResults") if isinstance(result, dict) else None
        if not isinstance(pages, list) or not pages:
            raise ReadFileError(ReadFileErrorCode.OCR_RESPONSE_INVALID, "PaddleX returned no pages")
        if expected_pages is not None and len(pages) != expected_pages:
            raise ReadFileError(ReadFileErrorCode.OCR_RESPONSE_INVALID, "PaddleX page count mismatch")
        validated_pages: list[
            tuple[str, dict[str, Any] | None, int | None, bool]
        ] = []
        raw_indices = [
            page.get("pageIndex", page.get("page_index"))
            if isinstance(page, dict)
            else None
            for page in pages
        ]
        if any(index is not None for index in raw_indices):
            if any(index is None for index in raw_indices):
                raise ReadFileError(
                    ReadFileErrorCode.OCR_RESPONSE_INVALID,
                    "PaddleX page indexes are incomplete",
                )
            try:
                page_indices = [int(index) for index in raw_indices]
            except (TypeError, ValueError) as exc:
                raise ReadFileError(
                    ReadFileErrorCode.OCR_RESPONSE_INVALID,
                    "PaddleX page index is malformed",
                ) from exc
            if page_indices not in (
                list(range(len(pages))),
                list(range(1, len(pages) + 1)),
            ):
                raise ReadFileError(
                    ReadFileErrorCode.OCR_RESPONSE_INVALID,
                    "PaddleX page indexes are not a unique monotonic sequence",
                )
        prior_global_block_ids: set[int] = set()
        for index, page in enumerate(pages):
            if not isinstance(page, dict):
                raise ReadFileError(ReadFileErrorCode.OCR_RESPONSE_INVALID, "PaddleX page is malformed")
            markdown = page.get("markdown")
            text = markdown.get("text") if isinstance(markdown, dict) else None
            if not isinstance(text, str):
                raise ReadFileError(
                    ReadFileErrorCode.OCR_RESPONSE_INVALID,
                    "PaddleX page Markdown is malformed",
                )
            pruned = page.get("prunedResult")
            if pruned is not None and not isinstance(pruned, dict):
                raise ReadFileError(
                    ReadFileErrorCode.OCR_RESPONSE_INVALID,
                    "PaddleX page preprocessing evidence is malformed",
                )
            settings = pruned.get("model_settings") if isinstance(pruned, dict) else None
            doc_preprocessor = (
                pruned.get("doc_preprocessor_res") if isinstance(pruned, dict) else None
            )
            angle = None
            if isinstance(doc_preprocessor, dict) and "angle" in doc_preprocessor:
                raw_angle = doc_preprocessor["angle"]
                if isinstance(raw_angle, bool) or not isinstance(raw_angle, (int, float)):
                    raise ReadFileError(
                        ReadFileErrorCode.OCR_RESPONSE_INVALID,
                        "PaddleX page orientation evidence is malformed",
                    )
                angle = round(float(raw_angle))
            stripped_text = text.strip()
            merged_continuation = not stripped_text and _is_merged_table_continuation(
                page, prior_global_block_ids
            )
            validated_pages.append(
                (
                    stripped_text,
                    settings,
                    angle,
                    merged_continuation,
                )
            )
            prior_global_block_ids.update(_page_global_block_ids(page))

        parsed: list[OCRPage] = []
        for index, (text, settings, angle, merged_continuation) in enumerate(
            validated_pages, 1
        ):
            if not merged_continuation and len(text) < max(1, min_markdown_characters):
                raise ReadFileError(
                    ReadFileErrorCode.OCR_PAGE_FAILED,
                    f"PaddleX page {index} Markdown is empty or too short",
                )
            if classify_severe_tilt and angle is not None:
                normalized_angle = abs(angle) % 360
                tilt = min(normalized_angle, 360 - normalized_angle)
                if tilt >= max(1, severe_tilt_degrees):
                    raise ReadFileError(
                        ReadFileErrorCode.OCR_PAGE_FAILED,
                        f"PaddleX page {index} has severe orientation evidence",
                    )
            parsed.append(OCRPage(index, text, settings))
        return parsed

    def parse_file(
        self,
        path: str | Path,
        file_type: int,
        options: dict[str, Any],
        check_abort: Callable[[], bool] | None,
    ) -> list[OCRPage]:
        source = Path(path)
        attempts = self.read_file_config.ocr_retry_count + 1
        endpoint = f"{self.service_config.base_url.rstrip('/')}/layout-parsing"
        for attempt in range(attempts):
            if check_abort and check_abort():
                raise ReadFileError(ReadFileErrorCode.ABORTED, "request was cancelled")
            payload = self._payload(source, file_type, options)
            try:
                response = self.http_client.post(endpoint, json=payload)
                if response.status_code >= 500:
                    response.raise_for_status()
                if response.status_code >= 400:
                    raise ReadFileError(
                        ReadFileErrorCode.OCR_REQUEST_FAILED,
                        f"PaddleX HTTP status {response.status_code}",
                    )
                try:
                    body = response.json()
                except ValueError as exc:
                    raise ReadFileError(
                        ReadFileErrorCode.OCR_RESPONSE_INVALID,
                        "PaddleX returned malformed JSON",
                    ) from exc
                return self._parse_response(
                    body,
                    options.get("expected_pages"),
                    min_markdown_characters=self.read_file_config.ocr_min_page_markdown_characters,
                    severe_tilt_degrees=self.read_file_config.ocr_severe_tilt_degrees,
                    classify_severe_tilt=not bool(
                        options.get("use_doc_unwarping", False)
                    ),
                )
            except ReadFileError:
                raise
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                retryable = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code >= 500
                if not retryable or attempt + 1 >= attempts:
                    raise ReadFileError(
                        ReadFileErrorCode.OCR_UNAVAILABLE,
                        "PaddleX request failed after bounded retries",
                    ) from exc
                time.sleep(min(0.1 * (2**attempt), 1.0))
        raise AssertionError("unreachable")
