from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from backend.config import OCRServiceConfig, ReadFileConfig
from backend.tools.read_file.errors import ReadFileError
from backend.tools.read_file.paddleocr_client import PaddleOCRClient
from backend.tools.read_file.service import ReadFileService
from backend.tools.read_file.temporary_object_server import TemporaryObjectServer


def test_temporary_object_url_is_loopback_single_use_and_opaque(tmp_path):
    path = tmp_path / "large.pdf"
    path.write_bytes(b"payload")
    server = TemporaryObjectServer(port=0)
    try:
        registered = server.register(path, ttl_seconds=30)
        assert registered.url.startswith("http://127.0.0.1:")
        assert str(path) not in registered.url
        assert urllib.request.urlopen(registered.url).read() == b"payload"
        with pytest.raises(urllib.error.HTTPError) as second:
            urllib.request.urlopen(registered.url)
        assert second.value.code == 404
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(registered.url.rsplit("/", 1)[0] + "/../etc/passwd")
    finally:
        server.close()


class _Handler(BaseHTTPRequestHandler):
    payloads = []
    failures = 0
    response_body = None
    response_bodies = []

    def log_message(self, format, *args):
        return

    def do_GET(self):
        body = json.dumps({"paths": {"/layout-parsing": {"post": {}}}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        size = int(self.headers["Content-Length"])
        type(self).payloads.append(json.loads(self.rfile.read(size)))
        if type(self).failures:
            type(self).failures -= 1
            self.send_error(503)
            return
        response_body = (
            type(self).response_bodies.pop(0)
            if type(self).response_bodies
            else type(self).response_body
        )
        body = json.dumps(
            response_body
            or {
                "errorCode": 0,
                "result": {
                    "layoutParsingResults": [
                        {"markdown": {"text": "page one"}},
                        {"markdown": {"text": "page two"}},
                    ]
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _service():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_client_uses_base64_and_exact_normal_request_switches(tmp_path):
    path = tmp_path / "page.pdf"
    path.write_bytes(b"%PDF-small")
    _Handler.payloads = []
    _Handler.failures = 0
    _Handler.response_body = None
    _Handler.response_bodies = []
    with _service() as url:
        client = PaddleOCRClient(
            OCRServiceConfig(base_url=url),
            ReadFileConfig(base64_threshold_bytes=1024),
        )
        pages = client.parse_file(
            path, 0, {"expected_pages": 2, "recognize_seals": True}, None
        )
        client.close()

    payload = _Handler.payloads[0]
    assert not payload["file"].startswith("http")
    expected = {
        "useDocOrientationClassify": True,
        "useDocUnwarping": False,
        "useLayoutDetection": True,
        "useChartRecognition": True,
        "useSealRecognition": True,
        "useOcrForImageBlock": True,
        "formatBlockContent": True,
        "mergeLayoutBlocks": True,
        "layoutShapeMode": "auto",
        "prettifyMarkdown": True,
        "showFormulaNumber": True,
        "restructurePages": True,
        "mergeTables": True,
        "relevelTitles": True,
        "returnMarkdownImages": False,
        "visualize": False,
        "maxNewTokens": 8192,
    }
    for key, value in expected.items():
        assert payload[key] == value
    assert [page.markdown for page in pages] == ["page one", "page two"]


def test_client_forwards_configured_ocr_max_new_tokens(tmp_path):
    path = tmp_path / "page.pdf"
    path.write_bytes(b"%PDF-small")
    _Handler.payloads = []
    _Handler.failures = 0
    _Handler.response_body = {
        "errorCode": 0,
        "result": {"layoutParsingResults": [{"markdown": {"text": "valid page"}}]},
    }
    _Handler.response_bodies = []
    with _service() as url:
        client = PaddleOCRClient(
            OCRServiceConfig(base_url=url),
            ReadFileConfig(ocr_max_new_tokens=6144),
        )
        client.parse_file(path, 0, {"expected_pages": 1}, None)
        client.close()

    assert _Handler.payloads[0]["maxNewTokens"] == 6144


def test_client_accepts_explicit_merged_table_continuation(tmp_path):
    path = tmp_path / "pages.pdf"
    path.write_bytes(b"%PDF-small")
    _Handler.payloads = []
    _Handler.failures = 0
    _Handler.response_body = {
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
    _Handler.response_bodies = []
    with _service() as url:
        client = PaddleOCRClient(OCRServiceConfig(base_url=url), ReadFileConfig())
        pages = client.parse_file(path, 0, {"expected_pages": 2}, None)
        client.close()

    assert [page.markdown for page in pages] == ["merged table content", ""]


@pytest.mark.parametrize("reference_location", ["unknown", "same-page", "future"])
def test_client_rejects_merged_table_reference_not_found_on_earlier_page(
    reference_location,
):
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

    with pytest.raises(ReadFileError, match="OCR_PAGE_FAILED"):
        PaddleOCRClient._parse_response(
            {"errorCode": 0, "result": {"layoutParsingResults": pages}},
            len(pages),
            min_markdown_characters=8,
            severe_tilt_degrees=45,
            classify_severe_tilt=True,
        )


def test_client_uses_object_url_over_threshold_and_retries_5xx(tmp_path):
    path = tmp_path / "large.png"
    path.write_bytes(b"large input")
    _Handler.payloads = []
    _Handler.failures = 1
    _Handler.response_body = {
        "errorCode": 0,
        "result": {"layoutParsingResults": [{"markdown": {"text": "valid page"}}]},
    }
    _Handler.response_bodies = []
    with _service() as url:
        client = PaddleOCRClient(
            OCRServiceConfig(base_url=url),
            ReadFileConfig(base64_threshold_bytes=1, ocr_retry_count=1),
        )
        pages = client.parse_file(path, 1, {"expected_pages": 1}, None)
        client.close()
    assert pages[0].markdown == "valid page"
    assert len(_Handler.payloads) == 2
    assert all(payload["file"].startswith("http://127.0.0.1:") for payload in _Handler.payloads)


@pytest.mark.parametrize(
    "body",
    [
        {"errorCode": 9, "errorMsg": "bad"},
        {"errorCode": 0, "result": {}},
    ],
)
def test_client_rejects_malformed_service_results(tmp_path, body):
    path = tmp_path / "page.png"
    path.write_bytes(b"small")
    _Handler.payloads = []
    _Handler.failures = 0
    _Handler.response_body = body
    _Handler.response_bodies = []
    with _service() as url:
        client = PaddleOCRClient(OCRServiceConfig(base_url=url), ReadFileConfig())
        with pytest.raises(ReadFileError, match="OCR_RESPONSE_INVALID"):
            client.parse_file(path, 1, {"expected_pages": 1}, None)
        client.close()


@pytest.mark.parametrize(
    "indices",
    [[0, 2], [1, 1], ["bad", 2], [0, None]],
)
def test_client_requires_one_global_monotonic_page_index_convention(tmp_path, indices):
    path = tmp_path / "page.pdf"
    path.write_bytes(b"small")
    _Handler.payloads = []
    _Handler.failures = 0
    _Handler.response_body = {
        "errorCode": 0,
        "result": {
            "layoutParsingResults": [
                {
                    "pageIndex": index,
                    "markdown": {"text": f"valid page {position} content"},
                }
                for position, index in enumerate(indices, 1)
            ]
        },
    }
    with _service() as url:
        client = PaddleOCRClient(OCRServiceConfig(base_url=url), ReadFileConfig())
        with pytest.raises(ReadFileError, match="OCR_RESPONSE_INVALID"):
            client.parse_file(path, 0, {"expected_pages": 2}, None)
        client.close()


@pytest.mark.parametrize("indices", [[0, 1], [1, 2]])
def test_client_accepts_exact_zero_or_one_based_page_indices(tmp_path, indices):
    path = tmp_path / "page.pdf"
    path.write_bytes(b"small")
    _Handler.payloads = []
    _Handler.failures = 0
    _Handler.response_body = {
        "errorCode": 0,
        "result": {
            "layoutParsingResults": [
                {
                    "pageIndex": index,
                    "markdown": {"text": f"valid page {position} content"},
                }
                for position, index in enumerate(indices, 1)
            ]
        },
    }
    with _service() as url:
        client = PaddleOCRClient(OCRServiceConfig(base_url=url), ReadFileConfig())
        pages = client.parse_file(path, 0, {"expected_pages": 2}, None)
        client.close()
    assert [page.source_page for page in pages] == [1, 2]


@pytest.mark.parametrize(
    "page",
    [
        {"markdown": {"text": ""}},
        {"markdown": {"text": "tiny"}},
        {
            "markdown": {"text": "otherwise adequate page text"},
            "prunedResult": {"doc_preprocessor_res": {"angle": 90}},
        },
    ],
)
def test_client_classifies_valid_low_quality_pages_for_unwarping_retry(tmp_path, page):
    path = tmp_path / "page.png"
    path.write_bytes(b"small")
    _Handler.payloads = []
    _Handler.failures = 0
    _Handler.response_bodies = []
    _Handler.response_body = {
        "errorCode": 0,
        "result": {"layoutParsingResults": [page]},
    }
    config = replace(
        ReadFileConfig(),
        ocr_min_page_markdown_characters=8,
        ocr_severe_tilt_degrees=45,
    )
    with _service() as url:
        client = PaddleOCRClient(OCRServiceConfig(base_url=url), config)
        with pytest.raises(ReadFileError, match="OCR_PAGE_FAILED"):
            client.parse_file(path, 1, {"expected_pages": 1}, None)
        client.close()


def test_real_client_and_service_retry_quality_once_with_unwarping(tmp_path):
    path = tmp_path / "page.png"
    Image.new("RGB", (32, 16), "white").save(path)
    _Handler.payloads = []
    _Handler.failures = 0
    _Handler.response_body = None
    _Handler.response_bodies = [
        {
            "errorCode": 0,
            "result": {"layoutParsingResults": [{"markdown": {"text": "x"}}]},
        },
        {
            "errorCode": 0,
            "result": {
                "layoutParsingResults": [
                    {
                        "markdown": {"text": "quality recovered content"},
                        "prunedResult": {
                            "doc_preprocessor_res": {"angle": 90}
                        },
                    }
                ]
            },
        },
    ]
    config = replace(
        ReadFileConfig(),
        allowed_roots=[str(tmp_path)],
        cache_path=str(tmp_path / "quality-cache.sqlite3"),
        main_llm_memory_reserve_gb=0,
        ocr_min_page_markdown_characters=8,
    )
    with _service() as url:
        client = PaddleOCRClient(OCRServiceConfig(base_url=url), config)
        service = ReadFileService(config=config, ocr_client=client)
        try:
            output = service.read(str(path))
        finally:
            service.close()
            client.close()
    assert "quality recovered content" in output
    assert [payload["useDocUnwarping"] for payload in _Handler.payloads] == [False, True]


def test_real_client_and_service_keep_malformed_indexes_terminal(tmp_path):
    path = tmp_path / "page.png"
    Image.new("RGB", (32, 16), "white").save(path)
    _Handler.payloads = []
    _Handler.failures = 0
    _Handler.response_bodies = []
    _Handler.response_body = {
        "errorCode": 0,
        "result": {
            "layoutParsingResults": [
                {"pageIndex": "bad", "markdown": {"text": "valid page text"}}
            ]
        },
    }
    config = replace(
        ReadFileConfig(),
        allowed_roots=[str(tmp_path)],
        cache_path=str(tmp_path / "protocol-cache.sqlite3"),
        main_llm_memory_reserve_gb=0,
    )
    with _service() as url:
        client = PaddleOCRClient(OCRServiceConfig(base_url=url), config)
        service = ReadFileService(config=config, ocr_client=client)
        try:
            output = service.read(str(path))
        finally:
            service.close()
            client.close()
    assert "OCR_RESPONSE_INVALID" in output
    assert len(_Handler.payloads) == 1
