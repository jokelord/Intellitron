from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import sys
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEBUG_CLIENT_PATH = PROJECT_ROOT / "scripts/debug_OCR_with_SGLang_backend.py"
PADDLEX_ERROR_SENTINEL = "SENSITIVE_PADDLEX_INTERNAL_DETAIL"
EXPECTED_SWITCHES = {
    "useDocOrientationClassify": True,
    "useDocUnwarping": False,
    "useLayoutDetection": True,
    "useChartRecognition": True,
    "useSealRecognition": False,
    "useOcrForImageBlock": True,
    "formatBlockContent": True,
    "mergeLayoutBlocks": True,
    "layoutShapeMode": "auto",
    "markdownIgnoreLabels": ["number", "header", "header_image", "footer", "footer_image"],
    "prettifyMarkdown": True,
    "showFormulaNumber": True,
    "restructurePages": True,
    "mergeTables": True,
    "relevelTitles": True,
    "returnMarkdownImages": False,
    "visualize": False,
    "maxNewTokens": 8192,
}


def _load_client():
    assert DEBUG_CLIENT_PATH.is_file(), "SGLang-backed OCR debug client is missing"
    spec = importlib.util.spec_from_file_location("paddle_ocr_sglang_debug", DEBUG_CLIENT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _DebugHandler(BaseHTTPRequestHandler):
    layout_requests: list[dict[str, object]] = []
    restructure_requests: list[dict[str, object]] = []
    model_name = "PaddleOCR-VL-1.6-0.9B"
    response_mode = "success"
    expose_restructure = True

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if type(self).response_mode == "http-500":
            self._send(500, b"failure", "text/plain")
            return
        if self.path == "/v1/models":
            self._send(200, json.dumps({"data": [{"id": type(self).model_name}]}).encode())
        elif self.path == "/openapi.json":
            paths = {"/layout-parsing": {"post": {}}}
            if type(self).expose_restructure:
                paths["/restructure-pages"] = {"post": {}}
            self._send(200, json.dumps({"paths": paths}).encode())
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path not in {"/layout-parsing", "/restructure-pages"}:
            self.send_error(404)
            return
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/restructure-pages":
            type(self).restructure_requests.append(payload)
            if (
                type(self).response_mode
                not in {"chunked-33", "chunked-empty-raw", "chunked-reported-mismatch"}
                or len(payload.get("pages", [])) != 33
            ):
                self.send_error(400)
                return
            body = {
                "errorCode": 0,
                "result": {
                    "dataInfo": {"type": "pdf", "numPages": 33},
                    "layoutParsingResults": [
                        {"markdown": {"text": f"page {index}"}}
                        for index in range(1, 34)
                    ],
                },
            }
            self._send(200, json.dumps(body).encode())
            return

        type(self).layout_requests.append(payload)
        mode = type(self).response_mode
        if mode == "malformed-json":
            self._send(200, b"not-json")
            return
        if mode == "paddlex-error":
            body: object = {"errorCode": 17, "errorMsg": PADDLEX_ERROR_SENTINEL}
        elif mode == "missing-pages":
            body = {"errorCode": 0, "result": {"layoutParsingResults": []}}
        elif mode == "empty-markdown":
            body = {
                "errorCode": 0,
                "result": {"layoutParsingResults": [{"markdown": {"text": "  "}}]},
            }
        elif mode in {
            "chunked-33",
            "chunked-empty-raw",
            "chunked-second-error",
            "chunked-reported-mismatch",
        }:
            request_index = len(type(self).layout_requests)
            start, count = (1, 32) if request_index == 1 else (33, 1)
            if mode == "chunked-second-error" and request_index == 2:
                body = {"errorCode": 23, "errorMsg": PADDLEX_ERROR_SENTINEL}
            else:
                body = {
                    "errorCode": 0,
                    "result": {
                        "dataInfo": {
                            "type": (
                                "unknown"
                                if mode == "chunked-reported-mismatch" and request_index == 1
                                else "pdf"
                            ),
                            "numPages": (
                                31
                                if mode == "chunked-reported-mismatch" and request_index == 1
                                else count
                            ),
                        },
                        "layoutParsingResults": [
                            {
                                "prunedResult": {"pageNumber": index},
                                "markdown": {
                                    "text": "" if mode == "chunked-empty-raw" else f"raw page {index}",
                                    "images": {f"image-{index}.png": f"image-{index}"},
                                },
                            }
                            for index in range(start, start + count)
                        ],
                    },
                }
        else:
            body = {
                "errorCode": 0,
                "result": {
                    "layoutParsingResults": [
                        {"markdown": {"text": "# First page"}},
                        {"markdown": {"text": "Second page"}},
                    ]
                },
            }
        self._send(200, json.dumps(body).encode())


def test_debug_client_constants_and_file_type_payloads(tmp_path: Path) -> None:
    client = _load_client()
    assert client.DOCUMENT_PATH == "~/Documents/example.pdf"
    assert client.PADDLEX_API_URL == "http://127.0.0.1:8118"
    assert client.OCR_SGLANG_URL == "http://127.0.0.1:30002"
    assert client.MAX_NEW_TOKENS == 8192
    pdf = tmp_path / "sample.pdf"
    image = tmp_path / "sample.JPEG"
    pdf.write_bytes(b"pdf-content")
    image.write_bytes(b"image-content")
    assert client.build_payload(pdf)["fileType"] == 0
    assert client.build_payload(image)["fileType"] == 1
    for path in (pdf, image):
        payload = client.build_payload(path)
        for key, expected in EXPECTED_SWITCHES.items():
            assert payload[key] == expected


@pytest.mark.parametrize("suffix", [".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"])
def test_debug_client_calls_running_services_and_prints_pages_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    suffix: str,
) -> None:
    client = _load_client()
    document = tmp_path / f"override{suffix}"
    if suffix == ".pdf":
        if shutil.which("pdfseparate") is None or shutil.which("pdfunite") is None:
            pytest.skip("Poppler PDF tools are unavailable")
        import fitz

        pdf = fitz.open()
        try:
            pdf.new_page()
            pdf.new_page()
            pdf.save(document)
        finally:
            pdf.close()
    else:
        document.write_bytes(b"secret-debug-document")
    _DebugHandler.layout_requests = []
    _DebugHandler.restructure_requests = []
    _DebugHandler.model_name = client.SERVED_MODEL_NAME
    _DebugHandler.response_mode = "success"
    monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:1")
    with _serve(_DebugHandler) as url:
        monkeypatch.setattr(client, "PADDLEX_API_URL", url)
        monkeypatch.setattr(client, "OCR_SGLANG_URL", url)
        assert client.main([str(document)]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == (
        "===== Page 1 =====\n# First page\n\n"
        "===== Page 2 =====\nSecond page\n"
    )
    assert len(_DebugHandler.layout_requests) == 1
    payload = _DebugHandler.layout_requests[0]
    assert payload["fileType"] == (0 if suffix == ".pdf" else 1)
    for key, expected in EXPECTED_SWITCHES.items():
        assert payload[key] == expected
    assert str(payload["file"]) not in output.out
    assert str(payload["file"]) not in output.err


def test_debug_client_uses_editable_default_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _load_client()
    document = tmp_path / "default.png"
    document.write_bytes(b"default-image")
    _DebugHandler.layout_requests = []
    _DebugHandler.restructure_requests = []
    _DebugHandler.response_mode = "success"
    monkeypatch.setattr(client, "DOCUMENT_PATH", str(document))
    with _serve(_DebugHandler) as url:
        monkeypatch.setattr(client, "PADDLEX_API_URL", url)
        monkeypatch.setattr(client, "OCR_SGLANG_URL", url)
        assert client.main([]) == 0
    assert len(_DebugHandler.layout_requests) == 1


def test_debug_client_accepts_explicit_merged_table_continuation() -> None:
    client = _load_client()
    body = {
        "errorCode": 0,
        "result": {
            "layoutParsingResults": [
                {
                    "markdown": {"text": "merged table content"},
                    "prunedResult": {
                        "parsing_res_list": [
                            {
                                "block_label": "table",
                                "block_content": "merged table content",
                                "global_block_id": 3,
                                "global_group_id": 3,
                            }
                        ]
                    },
                },
                {
                    "markdown": {"text": "\n"},
                    "prunedResult": {
                        "parsing_res_list": [
                            {
                                "block_label": "table",
                                "block_content": "",
                                "global_block_id": 7,
                                "global_group_id": 3,
                            },
                            {
                                "block_label": "footer",
                                "block_content": "Footer text",
                            },
                        ]
                    },
                },
            ]
        },
    }

    assert client.parse_markdown_pages(body) == [
        "merged table content",
        client.MERGED_TABLE_NOTICE,
    ]


@pytest.mark.parametrize("reference_location", ["unknown", "same-page", "future"])
def test_debug_client_rejects_merged_table_reference_not_found_on_earlier_page(
    reference_location: str,
) -> None:
    client = _load_client()
    continuation_blocks = [
        {
            "block_label": "table",
            "block_content": "",
            "global_block_id": 7,
            "global_group_id": 3,
        },
        {
            "block_label": "footer",
            "block_content": "Footer text",
            **({"global_block_id": 3} if reference_location == "same-page" else {}),
        },
    ]
    pages = [
        {
            "markdown": {"text": "first page content"},
            "prunedResult": {
                "parsing_res_list": [
                    {
                        "block_label": "text",
                        "block_content": "first page content",
                        "global_block_id": 2,
                    }
                ]
            },
        },
        {
            "markdown": {"text": ""},
            "prunedResult": {"parsing_res_list": continuation_blocks},
        },
    ]
    if reference_location == "future":
        pages.append(
            {
                "markdown": {"text": "future page content"},
                "prunedResult": {
                    "parsing_res_list": [
                        {
                            "block_label": "text",
                            "block_content": "future page content",
                            "global_block_id": 3,
                        }
                    ]
                },
            }
        )

    body = {"errorCode": 0, "result": {"layoutParsingResults": pages}}
    with pytest.raises(client.DebugClientError, match="empty"):
        client.parse_markdown_pages(body)


def test_debug_client_rejects_reported_pdf_page_truncation() -> None:
    client = _load_client()
    body = {
        "errorCode": 0,
        "result": {
            "dataInfo": {"type": "pdf", "numPages": 33, "pages": []},
            "layoutParsingResults": [
                {"markdown": {"text": f"page {index}"}} for index in range(32)
            ],
        },
    }
    with pytest.raises(client.DebugClientError, match="33.*32"):
        client.parse_markdown_pages(body)


def test_debug_client_reports_reported_pdf_page_truncation_to_zero() -> None:
    client = _load_client()
    body = {
        "errorCode": 0,
        "result": {
            "dataInfo": {"type": "pdf", "numPages": 33, "pages": []},
            "layoutParsingResults": [],
        },
    }
    with pytest.raises(client.DebugClientError, match="33.*0"):
        client.parse_markdown_pages(body)


def test_debug_client_ordinary_parser_does_not_infer_unknown_type_page_count() -> None:
    client = _load_client()
    body = {
        "errorCode": 0,
        "result": {
            "dataInfo": {"type": "unknown", "numPages": 31},
            "layoutParsingResults": [
                {"markdown": {"text": f"page {index}"}} for index in range(32)
            ],
        },
    }

    assert len(client.parse_markdown_pages(body)) == 32


def test_debug_client_does_not_print_paddlex_error_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _load_client()
    document = tmp_path / "input.png"
    document.write_bytes(b"secret-payload")
    _DebugHandler.layout_requests = []
    _DebugHandler.restructure_requests = []
    _DebugHandler.model_name = client.SERVED_MODEL_NAME
    _DebugHandler.response_mode = "paddlex-error"
    with _serve(_DebugHandler) as url:
        monkeypatch.setattr(client, "PADDLEX_API_URL", url)
        monkeypatch.setattr(client, "OCR_SGLANG_URL", url)
        assert client.main([str(document)]) == 1

    captured = capsys.readouterr()
    assert "errorCode=17" in captured.err
    assert PADDLEX_ERROR_SENTINEL not in captured.out
    assert PADDLEX_ERROR_SENTINEL not in captured.err


@pytest.mark.skipif(
    shutil.which("pdfseparate") is None or shutil.which("pdfunite") is None,
    reason="Poppler PDF tools are unavailable",
)
def test_prepare_pdf_chunks_preserves_all_pages_in_order(tmp_path: Path) -> None:
    import fitz

    source = tmp_path / "source.pdf"
    document = fitz.open()
    try:
        for width in range(101, 134):
            document.new_page(width=width, height=200)
        document.save(source)
    finally:
        document.close()

    client = _load_client()
    work = tmp_path / "work"
    work.mkdir()
    total, chunks = client.prepare_pdf_chunks(source, work)

    assert total == 33
    assert [count for _, count in chunks] == [32, 1]
    widths = []
    for chunk, _ in chunks:
        chunk_document = fitz.open(chunk)
        try:
            widths.extend(round(page.rect.width) for page in chunk_document)
        finally:
            chunk_document.close()
    assert widths == list(range(101, 134))


@pytest.mark.skipif(
    shutil.which("pdfseparate") is None or shutil.which("pdfunite") is None,
    reason="Poppler PDF tools are unavailable",
)
def test_debug_client_chunks_and_restructures_33_page_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import fitz

    document = tmp_path / "document.pdf"
    pdf = fitz.open()
    try:
        for page_number in range(1, 34):
            page = pdf.new_page(width=100 + page_number, height=200)
            page.insert_text((20, 40), f"source page {page_number}")
        pdf.save(document)
    finally:
        pdf.close()

    client = _load_client()
    _DebugHandler.layout_requests = []
    _DebugHandler.restructure_requests = []
    _DebugHandler.model_name = client.SERVED_MODEL_NAME
    _DebugHandler.response_mode = "chunked-33"
    with _serve(_DebugHandler) as url:
        monkeypatch.setattr(client, "PADDLEX_API_URL", url)
        monkeypatch.setattr(client, "OCR_SGLANG_URL", url)
        assert client.main([str(document)]) == 0

    captured = capsys.readouterr()
    layout_requests = _DebugHandler.layout_requests
    restructure_requests = _DebugHandler.restructure_requests
    assert [request["restructurePages"] for request in layout_requests] == [False, False]
    assert [len(request["pages"]) for request in restructure_requests] == [33]
    for request in layout_requests:
        for key, expected in EXPECTED_SWITCHES.items():
            if key != "restructurePages":
                assert request[key] == expected
    ordered_pages = [
        {
            "prunedResult": {"pageNumber": index},
            "markdown": {"images": {f"image-{index}.png": f"image-{index}"}},
        }
        for index in range(1, 34)
    ]
    assert restructure_requests == [
        {
            "pages": ordered_pages,
            "mergeTables": True,
            "relevelTitles": True,
            "concatenatePages": False,
            "prettifyMarkdown": True,
            "showFormulaNumber": True,
            "logId": "paddleocr-sglang-debug-restructure",
        }
    ]
    assert "===== Page 33 =====\npage 33\n" in captured.out
    assert captured.err == ""
    encoded_source = base64.b64encode(document.read_bytes()).decode("ascii")
    assert encoded_source not in captured.out
    assert encoded_source not in captured.err
    for request in layout_requests:
        assert str(request["file"]) not in captured.out
        assert str(request["file"]) not in captured.err


@pytest.mark.skipif(
    shutil.which("pdfseparate") is None or shutil.which("pdfunite") is None,
    reason="Poppler PDF tools are unavailable",
)
def test_debug_client_accepts_empty_raw_markdown_before_global_restructure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import fitz

    document = tmp_path / "empty-raw.pdf"
    pdf = fitz.open()
    try:
        for _ in range(33):
            pdf.new_page()
        pdf.save(document)
    finally:
        pdf.close()

    client = _load_client()
    _DebugHandler.layout_requests = []
    _DebugHandler.restructure_requests = []
    _DebugHandler.model_name = client.SERVED_MODEL_NAME
    _DebugHandler.response_mode = "chunked-empty-raw"
    with _serve(_DebugHandler) as url:
        monkeypatch.setattr(client, "PADDLEX_API_URL", url)
        monkeypatch.setattr(client, "OCR_SGLANG_URL", url)
        assert client.main([str(document)]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "===== Page 33 =====\npage 33\n" in captured.out
    assert len(_DebugHandler.layout_requests) == 2
    assert len(_DebugHandler.restructure_requests) == 1


@pytest.mark.skipif(
    shutil.which("pdfseparate") is None or shutil.which("pdfunite") is None,
    reason="Poppler PDF tools are unavailable",
)
def test_debug_client_does_not_restructure_after_second_chunk_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import fitz

    document = tmp_path / "second-chunk-failure.pdf"
    pdf = fitz.open()
    try:
        for _ in range(33):
            pdf.new_page()
        pdf.save(document)
    finally:
        pdf.close()

    client = _load_client()
    _DebugHandler.layout_requests = []
    _DebugHandler.restructure_requests = []
    _DebugHandler.model_name = client.SERVED_MODEL_NAME
    _DebugHandler.response_mode = "chunked-second-error"
    temp_root = Path(tempfile.gettempdir())
    temp_paths_before = set(temp_root.glob("paddleocr-debug-*"))
    with _serve(_DebugHandler) as url:
        monkeypatch.setattr(client, "PADDLEX_API_URL", url)
        monkeypatch.setattr(client, "OCR_SGLANG_URL", url)
        assert client.main([str(document)]) == 1

    captured = capsys.readouterr()
    assert len(_DebugHandler.layout_requests) == 2
    assert _DebugHandler.restructure_requests == []
    assert "errorCode=23" in captured.err
    assert PADDLEX_ERROR_SENTINEL not in captured.err
    assert set(temp_root.glob("paddleocr-debug-*")) == temp_paths_before


@pytest.mark.skipif(
    shutil.which("pdfseparate") is None or shutil.which("pdfunite") is None,
    reason="Poppler PDF tools are unavailable",
)
def test_debug_client_rejects_required_raw_reported_count_for_unknown_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import fitz

    document = tmp_path / "reported-count-mismatch.pdf"
    pdf = fitz.open()
    try:
        for _ in range(33):
            pdf.new_page()
        pdf.save(document)
    finally:
        pdf.close()

    client = _load_client()
    _DebugHandler.layout_requests = []
    _DebugHandler.restructure_requests = []
    _DebugHandler.model_name = client.SERVED_MODEL_NAME
    _DebugHandler.response_mode = "chunked-reported-mismatch"
    with _serve(_DebugHandler) as url:
        monkeypatch.setattr(client, "PADDLEX_API_URL", url)
        monkeypatch.setattr(client, "OCR_SGLANG_URL", url)
        assert client.main([str(document)]) == 1

    captured = capsys.readouterr()
    assert "expected 32, actual 31" in captured.err
    assert len(_DebugHandler.layout_requests) == 1
    assert _DebugHandler.restructure_requests == []


@pytest.mark.parametrize("missing_executable", ["pdfseparate", "pdfunite"])
def test_prepare_pdf_chunks_reports_missing_poppler_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_executable: str,
) -> None:
    client = _load_client()
    monkeypatch.setattr(
        shutil,
        "which",
        lambda executable: None if executable == missing_executable else f"/usr/bin/{executable}",
    )

    with pytest.raises(client.DebugClientError, match=missing_executable):
        client.prepare_pdf_chunks(tmp_path / "source.pdf", tmp_path)


def test_debug_client_requires_restructure_endpoint_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _load_client()
    _DebugHandler.expose_restructure = False
    try:
        with _serve(_DebugHandler) as url, client.httpx.Client(
            trust_env=False
        ) as http:
            monkeypatch.setattr(client, "PADDLEX_API_URL", url)
            with pytest.raises(client.DebugClientError, match="restructure-pages"):
                client.verify_paddlex(http, require_restructure=True)
    finally:
        _DebugHandler.expose_restructure = True


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing", "regular file"),
        ("directory", "regular file"),
        ("unsupported", "Unsupported"),
        ("wrong-model", "model"),
        ("http-500", "HTTP"),
        ("malformed-json", "JSON"),
        ("paddlex-error", "errorCode"),
        ("missing-pages", "pages"),
        ("empty-markdown", "empty"),
    ],
)
def test_debug_client_returns_nonzero_without_dumping_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    message: str,
) -> None:
    client = _load_client()
    if case == "missing":
        document = tmp_path / "missing.png"
    elif case == "directory":
        document = tmp_path
    elif case == "unsupported":
        document = tmp_path / "input.txt"
        document.write_bytes(b"secret-payload")
    else:
        document = tmp_path / "input.png"
        document.write_bytes(b"secret-payload")
    _DebugHandler.layout_requests = []
    _DebugHandler.restructure_requests = []
    _DebugHandler.model_name = (
        "different-model" if case == "wrong-model" else client.SERVED_MODEL_NAME
    )
    _DebugHandler.response_mode = case if case not in {"wrong-model", "missing", "directory", "unsupported"} else "success"
    with _serve(_DebugHandler) as url:
        monkeypatch.setattr(client, "PADDLEX_API_URL", url)
        monkeypatch.setattr(client, "OCR_SGLANG_URL", url)
        assert client.main([str(document)]) == 1
    captured = capsys.readouterr()
    assert message.casefold() in captured.err.casefold()
    assert "c2VjcmV0LXBheWxvYWQ=" not in captured.out
    assert "c2VjcmV0LXBheWxvYWQ=" not in captured.err
    assert "duration" not in captured.out.casefold()
    assert "timing" not in captured.out.casefold()
