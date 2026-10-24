#!/usr/bin/env python3
"""Print ordered PaddleOCR-VL Markdown from already-running local services."""

from __future__ import annotations

import argparse
import base64
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx


DOCUMENT_PATH = "~/Documents/example.pdf"
PADDLEX_API_URL = "http://127.0.0.1:8118"
OCR_SGLANG_URL = "http://127.0.0.1:30002"
SERVED_MODEL_NAME = "PaddleOCR-VL-1.6-0.9B"
MAX_NEW_TOKENS = 8192
PDF_CHUNK_PAGES = 32
MERGED_TABLE_NOTICE = "[Table content merged into an earlier page]"
RESTRUCTURE_PATH = "/restructure-pages"

PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
MARKDOWN_IGNORED_LABELS = {
    "number",
    "header",
    "header_image",
    "footer",
    "footer_image",
}


class DebugClientError(RuntimeError):
    """The selected document or running OCR topology is invalid."""


def prepare_pdf_chunks(
    path: Path, temp_dir: Path
) -> tuple[int, list[tuple[Path, int]]]:
    pdfseparate = shutil.which("pdfseparate")
    if pdfseparate is None:
        raise DebugClientError("Required executable is unavailable: pdfseparate")
    pdfunite = shutil.which("pdfunite")
    if pdfunite is None:
        raise DebugClientError("Required executable is unavailable: pdfunite")

    try:
        subprocess.run(
            [pdfseparate, str(path), str(temp_dir / "page-%06d.pdf")],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DebugClientError(
            f"pdfseparate failed: {type(exc).__name__}"
        ) from exc

    pages = sorted(temp_dir.glob("page-*.pdf"))
    page_count = len(pages)
    if page_count == 0:
        raise DebugClientError("pdfseparate produced no valid PDF pages")
    if page_count <= PDF_CHUNK_PAGES:
        return page_count, [(path, page_count)]

    chunks: list[tuple[Path, int]] = []
    for index, start in enumerate(range(0, page_count, PDF_CHUNK_PAGES), start=1):
        group = pages[start : start + PDF_CHUNK_PAGES]
        chunk_path = temp_dir / f"chunk-{index:06d}.pdf"
        try:
            subprocess.run(
                [pdfunite, *map(str, group), str(chunk_path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DebugClientError(f"pdfunite failed: {type(exc).__name__}") from exc
        chunks.append((chunk_path, len(group)))
    return page_count, chunks


def build_payload(path: Path) -> dict[str, Any]:
    suffix = path.suffix.casefold()
    if suffix in PDF_SUFFIXES:
        file_type = 0
    elif suffix in IMAGE_SUFFIXES:
        file_type = 1
    else:
        raise DebugClientError(f"Unsupported OCR document type: {path.suffix or '<none>'}")
    try:
        encoded_file = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as exc:
        raise DebugClientError(f"Cannot read document {path}: {exc}") from exc
    return {
        "file": encoded_file,
        "fileType": file_type,
        "useDocOrientationClassify": True,
        "useDocUnwarping": False,
        "useLayoutDetection": True,
        "useChartRecognition": True,
        "useSealRecognition": False,
        "useOcrForImageBlock": True,
        "formatBlockContent": True,
        "mergeLayoutBlocks": True,
        "layoutShapeMode": "auto",
        "markdownIgnoreLabels": [
            "number",
            "header",
            "header_image",
            "footer",
            "footer_image",
        ],
        "prettifyMarkdown": True,
        "showFormulaNumber": True,
        "restructurePages": True,
        "mergeTables": True,
        "relevelTitles": True,
        "returnMarkdownImages": False,
        "visualize": False,
        "maxNewTokens": MAX_NEW_TOKENS,
        "logId": "paddleocr-sglang-debug",
    }


def _post_json(client: httpx.Client, path: str, payload: dict[str, Any]) -> Any:
    response = client.post(f"{PADDLEX_API_URL.rstrip('/')}{path}", json=payload)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise DebugClientError(f"PaddleX {path} returned malformed JSON") from exc


def verify_sglang(client: httpx.Client) -> None:
    response = client.get(f"{OCR_SGLANG_URL.rstrip('/')}/v1/models")
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError as exc:
        raise DebugClientError("SGLang /v1/models returned malformed JSON") from exc
    data = body.get("data") if isinstance(body, dict) else None
    model_ids = {
        item.get("id")
        for item in data
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    } if isinstance(data, list) else set()
    if SERVED_MODEL_NAME not in model_ids:
        raise DebugClientError(
            f"SGLang does not serve the required model {SERVED_MODEL_NAME!r}"
        )


def verify_paddlex(
    client: httpx.Client, *, require_restructure: bool = False
) -> None:
    response = client.get(f"{PADDLEX_API_URL.rstrip('/')}/openapi.json")
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError as exc:
        raise DebugClientError("PaddleX /openapi.json returned malformed JSON") from exc
    paths = body.get("paths") if isinstance(body, dict) else None
    layout_path = paths.get("/layout-parsing") if isinstance(paths, dict) else None
    if not isinstance(layout_path, dict) or not isinstance(layout_path.get("post"), dict):
        raise DebugClientError("PaddleX does not expose POST /layout-parsing")
    restructure_path = paths.get(RESTRUCTURE_PATH) if isinstance(paths, dict) else None
    if require_restructure and (
        not isinstance(restructure_path, dict)
        or not isinstance(restructure_path.get("post"), dict)
    ):
        raise DebugClientError("PaddleX does not expose POST /restructure-pages")


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
        if block.get("block_label") in MARKDOWN_IGNORED_LABELS:
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


def _validate_layout_pages(
    body: Any,
    expected_pages: int | None = None,
    *,
    require_reported_pages: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        raise DebugClientError("PaddleX response must be a JSON object")
    if body.get("errorCode") not in (None, 0):
        raise DebugClientError(f"PaddleX returned errorCode={body.get('errorCode')}")
    result = body.get("result")
    pages = result.get("layoutParsingResults") if isinstance(result, dict) else None
    if not isinstance(pages, list):
        raise DebugClientError("PaddleX returned no OCR pages")
    data_info = result.get("dataInfo") if isinstance(result, dict) else None
    document_type = data_info.get("type") if isinstance(data_info, dict) else None
    reported_pages = data_info.get("numPages") if isinstance(data_info, dict) else None
    has_valid_reported_pages = (
        not isinstance(reported_pages, bool)
        and isinstance(reported_pages, int)
        and reported_pages > 0
    )
    if require_reported_pages:
        if not has_valid_reported_pages or reported_pages != expected_pages:
            raise DebugClientError(
                "PaddleX page count mismatch: "
                f"expected {expected_pages}, actual {reported_pages!r}"
            )
    elif document_type in {"pdf", "tiff"} and has_valid_reported_pages:
        if expected_pages is None:
            expected_pages = reported_pages
        elif reported_pages != expected_pages:
            raise DebugClientError(
                "PaddleX page count mismatch: "
                f"expected {expected_pages}, actual {reported_pages}"
            )
    if expected_pages is not None and len(pages) != expected_pages:
        raise DebugClientError(
            f"PaddleX page count mismatch: expected {expected_pages}, actual {len(pages)}"
        )
    if not pages:
        raise DebugClientError("PaddleX returned no OCR pages")
    if not all(isinstance(page, dict) for page in pages):
        raise DebugClientError("PaddleX returned malformed OCR page data")
    return pages


def parse_markdown_pages(
    body: Any, expected_pages: int | None = None
) -> list[str]:
    pages = _validate_layout_pages(body, expected_pages)
    markdown_pages: list[str] = []
    prior_global_block_ids: set[int] = set()
    for index, page in enumerate(pages, start=1):
        markdown = page.get("markdown")
        text = markdown.get("text") if isinstance(markdown, dict) else None
        if not isinstance(text, str):
            raise DebugClientError(f"PaddleX page {index} has malformed markdown.text")
        if not text.strip() and _is_merged_table_continuation(
            page, prior_global_block_ids
        ):
            markdown_pages.append(MERGED_TABLE_NOTICE)
            prior_global_block_ids.update(_page_global_block_ids(page))
            continue
        if not text.strip():
            raise DebugClientError(f"PaddleX page {index} has empty markdown.text")
        markdown_pages.append(text.strip())
        prior_global_block_ids.update(_page_global_block_ids(page))
    return markdown_pages


def request_document(client: httpx.Client, path: Path) -> Any:
    if path.suffix.casefold() not in PDF_SUFFIXES:
        return _post_json(client, "/layout-parsing", build_payload(path))

    with TemporaryDirectory(prefix="paddleocr-debug-") as temp_dir_name:
        total_pages, chunks = prepare_pdf_chunks(path, Path(temp_dir_name))
        if total_pages <= PDF_CHUNK_PAGES:
            body = _post_json(client, "/layout-parsing", build_payload(path))
            parse_markdown_pages(body, expected_pages=total_pages)
            return body

        verify_paddlex(client, require_restructure=True)
        ordered_pages: list[dict[str, Any]] = []
        for chunk_path, chunk_pages in chunks:
            payload = build_payload(chunk_path)
            payload["restructurePages"] = False
            body = _post_json(client, "/layout-parsing", payload)
            raw_pages = _validate_layout_pages(
                body,
                expected_pages=chunk_pages,
                require_reported_pages=True,
            )
            for index, page in enumerate(raw_pages, start=1):
                pruned_result = page.get("prunedResult")
                markdown = page.get("markdown")
                images = markdown.get("images") if isinstance(markdown, dict) else None
                if not isinstance(pruned_result, dict) or not isinstance(images, dict):
                    raise DebugClientError(
                        f"PaddleX chunk page {index} has malformed restructure data"
                    )
                ordered_pages.append(
                    {
                        "prunedResult": pruned_result,
                        "markdown": {"images": images},
                    }
                )

        restructure_payload = {
            "pages": ordered_pages,
            "mergeTables": True,
            "relevelTitles": True,
            "concatenatePages": False,
            "prettifyMarkdown": True,
            "showFormulaNumber": True,
            "logId": "paddleocr-sglang-debug-restructure",
        }
        body = _post_json(client, RESTRUCTURE_PATH, restructure_payload)
        parse_markdown_pages(body, expected_pages=total_pages)
        return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", nargs="?", default=DOCUMENT_PATH)
    args = parser.parse_args(argv)
    path = Path(args.document).expanduser()
    if not path.is_file():
        print(f"OCR debug failed: document must be an existing regular file: {path}", file=sys.stderr)
        return 1
    try:
        with httpx.Client(trust_env=False, timeout=600.0) as client:
            verify_sglang(client)
            verify_paddlex(client)
            body = request_document(client, path)
        pages = parse_markdown_pages(body)
    except DebugClientError as exc:
        print(f"OCR debug failed: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"OCR debug failed: HTTP request failed: {exc}", file=sys.stderr)
        return 1

    for index, markdown in enumerate(pages, start=1):
        if index > 1:
            print()
        print(f"===== Page {index} =====")
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
