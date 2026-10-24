from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path

import fitz
import pytest
from PIL import Image

from backend.config import OCRServiceConfig, ReadFileConfig
from backend.tools.read_file.errors import ReadFileError, ReadFileErrorCode
from backend.tools.read_file.models import PageResult, ParseError
from backend.tools.read_file.paddleocr_client import OCRPage
from backend.tools.read_file.result_assembler import assemble_pages
from backend.tools.read_file.service import ReadFileService


class FakeOCR:
    def __init__(self, fail_normal_calls=0):
        self.calls = []
        self.fail_normal_calls = fail_normal_calls

    def parse_file(self, path, file_type, options, check_abort):
        self.calls.append((file_type, dict(options)))
        if self.fail_normal_calls and not options.get("use_doc_unwarping"):
            self.fail_normal_calls -= 1
            raise ReadFileError(ReadFileErrorCode.OCR_PAGE_FAILED, "retry")
        return [
            OCRPage(index + 1, f"content page {index + 1}")
            for index in range(options["expected_pages"])
        ]

    def close(self):
        return None


def test_page_assembly_adds_provenance_and_strips_temporary_links(tmp_path):
    source = tmp_path / "sample.pdf"
    result = assemble_pages(
        source,
        [PageResult(source_page=1, source_slide=7, markdown='Text ![tmp](http://127.0.0.1:9999/objects/x)<img src="imgs/temp.jpg"/>')],
        {"source_format": "pdf", "engine": "PaddleOCR-VL-1.6", "model_version": "1.6", "pipeline_version": "v1.6"},
    )
    assert "source_page=1" in result.markdown
    assert "source_format=pdf" in result.markdown
    assert "source_slide=7" in result.markdown
    assert "PaddleOCR-VL-1.6" in result.markdown
    assert "127.0.0.1" not in result.markdown
    assert "imgs/temp.jpg" not in result.markdown


def test_image_route_uses_normal_ocr_switch_and_slices_after_assembly(tmp_path):
    path = tmp_path / "page.png"
    Image.new("RGB", (40, 20), "white").save(path)
    client = FakeOCR()
    config = replace(ReadFileConfig(), allowed_roots=[str(tmp_path)])
    service = ReadFileService(config=config, ocr_client=client)

    full = service.read(str(path))
    sliced = service.read(str(path), start_line=1, end_line=1)

    assert "content page 1" in full
    assert "source_page=1" in full
    assert "content page 1" not in sliced
    assert all(call[1].get("use_doc_unwarping") is False for call in client.calls)


def test_pdf_chunk_failure_retries_to_single_page_then_unwarping(tmp_path):
    path = tmp_path / "page.pdf"
    document = fitz.open()
    document.new_page()
    document.save(path)
    document.close()
    client = FakeOCR(fail_normal_calls=2)
    config = replace(ReadFileConfig(), allowed_roots=[str(tmp_path)], pdf_chunk_pages=24)
    service = ReadFileService(config=config, ocr_client=client)

    output = service.read(str(path))

    assert "content page 1" in output
    assert [options["use_doc_unwarping"] for _, options in client.calls] == [False, True]


def test_placeholder_spreadsheets_never_call_ocr(tmp_path):
    path = tmp_path / "legacy.xls"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    client = FakeOCR()
    service = ReadFileService(
        config=replace(ReadFileConfig(), allowed_roots=[str(tmp_path)]),
        ocr_client=client,
    )
    output = service.read(str(path))
    assert "UNSUPPORTED_FORMAT" in output
    assert "planned" in output
    assert client.calls == []


def test_abort_is_checked_before_visual_work(tmp_path):
    path = tmp_path / "page.png"
    Image.new("RGB", (4, 4), "white").save(path)
    client = FakeOCR()
    service = ReadFileService(
        config=replace(ReadFileConfig(), allowed_roots=[str(tmp_path)]),
        ocr_client=client,
    )
    output = service.read(str(path), check_abort=lambda: True)
    assert "ABORTED" in output
    assert client.calls == []


@pytest.mark.parametrize("suffix", [".png", ".pdf"])
def test_inflight_abort_is_not_retried_or_downgraded_to_page_failure(tmp_path, suffix):
    path = tmp_path / f"source{suffix}"
    image = Image.new("RGB", (20, 20), "white")
    if suffix == ".pdf":
        image.save(path, "PDF")
    else:
        image.save(path)

    class AbortingOCR(FakeOCR):
        def parse_file(self, path, file_type, options, check_abort):
            self.calls.append((file_type, dict(options)))
            raise ReadFileError(ReadFileErrorCode.ABORTED, "stopped during OCR")

    client = AbortingOCR()
    service = ReadFileService(
        config=replace(
            ReadFileConfig(),
            allowed_roots=[str(tmp_path)],
            main_llm_memory_reserve_gb=0,
            cache_path=str(tmp_path / "cache.sqlite3"),
        ),
        ocr_client=client,
    )
    output = service.read(str(path))
    assert "ABORTED" in output
    assert len(client.calls) == 1


def test_document_options_are_validated_and_propagated_to_pdf(tmp_path):
    path = tmp_path / "page.pdf"
    image = Image.new("RGB", (20, 20), "white")
    image.save(path, "PDF")
    client = FakeOCR()
    service = ReadFileService(
        config=replace(
            ReadFileConfig(),
            allowed_roots=[str(tmp_path)],
            main_llm_memory_reserve_gb=0,
            cache_path=str(tmp_path / "options-cache.sqlite3"),
        ),
        ocr_client=client,
    )
    invalid = service.read(str(path), document_mode="unknown")
    assert "INVALID_DOCUMENT_MODE" in invalid
    assert client.calls == []
    output = service.read(
        str(path), document_mode="distorted", recognize_seals=True
    )
    assert "content page 1" in output
    assert client.calls[0][1]["use_doc_unwarping"] is True
    assert client.calls[0][1]["recognize_seals"] is True


def test_office_password_is_explicitly_unsupported_without_conversion(tmp_path, monkeypatch):
    path = tmp_path / "protected.docx"
    _minimal_docx(path)
    called = False

    def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("LibreOffice must not be invoked")

    monkeypatch.setattr(
        "backend.tools.read_file.service.render_office_to_pdf", forbidden
    )
    service = ReadFileService(
        config=replace(ReadFileConfig(), allowed_roots=[str(tmp_path)]),
        ocr_client=FakeOCR(),
    )
    output = service.read(str(path), password="office-password")
    assert "OFFICE_PASSWORD_UNSUPPORTED" in output
    assert called is False


def test_terminal_ocr_unavailable_is_not_adaptively_split_or_rewrapped(tmp_path):
    path = tmp_path / "page.pdf"
    Image.new("RGB", (20, 20), "white").save(path, "PDF")

    class Unavailable(FakeOCR):
        def parse_file(self, path, file_type, options, check_abort):
            self.calls.append((file_type, dict(options)))
            raise ReadFileError(ReadFileErrorCode.OCR_UNAVAILABLE, "service offline")

    client = Unavailable()
    service = ReadFileService(
        config=replace(
            ReadFileConfig(),
            allowed_roots=[str(tmp_path)],
            main_llm_memory_reserve_gb=0,
            cache_path=str(tmp_path / "terminal-cache.sqlite3"),
        ),
        ocr_client=client,
    )
    output = service.read(str(path))
    assert "OCR_UNAVAILABLE" in output
    assert len(client.calls) == 1


def test_quality_page_failure_retries_once_with_unwarping(tmp_path):
    path = tmp_path / "page.pdf"
    Image.new("RGB", (20, 20), "white").save(path, "PDF")

    class Quality(FakeOCR):
        def parse_file(self, path, file_type, options, check_abort):
            self.calls.append((file_type, dict(options)))
            if len(self.calls) == 1:
                raise ReadFileError(ReadFileErrorCode.OCR_PAGE_FAILED, "low quality")
            return [OCRPage(1, "quality recovered content")]

    client = Quality()
    service = ReadFileService(
        config=replace(
            ReadFileConfig(),
            allowed_roots=[str(tmp_path)],
            main_llm_memory_reserve_gb=0,
            cache_path=str(tmp_path / "quality-cache.sqlite3"),
        ),
        ocr_client=client,
    )
    assert "quality recovered content" in service.read(str(path))
    assert [call[1]["use_doc_unwarping"] for call in client.calls] == [False, True]


def test_overlap_merge_replaces_failed_page_with_later_success():
    from backend.tools.read_file import service as service_module

    failed = PageResult(
        4,
        "",
        error=ParseError("OCR_PAGE_FAILED", "first chunk failed", source_page=4),
    )
    success = PageResult(4, "recovered overlap")
    assert service_module._merge_page_result(failed, success) == success


def test_partial_page_failure_adds_stable_warning_and_error(tmp_path):
    result = assemble_pages(
        tmp_path / "partial.pdf",
        [
            PageResult(1, "successful page"),
            PageResult(
                2,
                "",
                error=ParseError(
                    "OCR_PAGE_FAILED", "quality exhausted", source_page=2
                ),
            ),
        ],
        {"source_format": "pdf"},
    )
    assert result.success is True
    assert result.errors[0].source_page == 2
    assert result.warnings == ["page 2 failed: OCR_PAGE_FAILED: quality exhausted"]


def test_benchmark_acceptance_manifest_smoke(tmp_path):
    output = tmp_path / "benchmark.json"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/benchmark_read_file_v7.py",
            "--manifest",
            "docs/read-file-v7-thor-acceptance.md",
            "--output",
            str(output),
            "--smoke",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(output.read_text())
    assert report["schemaVersion"] == 1
    assert report["cases"]
    assert report["smoke"] is True
    assert output.with_suffix(".md").is_file()
    assert "benchmark smoke passed" in completed.stdout


def test_benchmark_missing_fixture_is_incomplete_and_nonzero(tmp_path):
    manifest = tmp_path / "acceptance.md"
    manifest.write_text(
        "<!-- READ_FILE_V7_MANIFEST_BEGIN -->\n"
        '{"schemaVersion":1,"cases":[{"id":"missing","format":"pdf",'
        '"path":"missing.pdf","pages":1}]}\n'
        "<!-- READ_FILE_V7_MANIFEST_END -->\n"
    )
    output = tmp_path / "benchmark.json"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/benchmark_read_file_v7.py",
            "--manifest",
            str(manifest),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    report = json.loads(output.read_text())
    assert completed.returncode != 0
    assert report["acceptanceComplete"] is False
    assert report["cases"][0]["status"] == "missing_fixture"


def test_benchmark_counts_soak_and_metrics_are_measured(tmp_path, monkeypatch):
    import importlib.util

    script = Path("scripts/benchmark_read_file_v7.py").resolve()
    spec = importlib.util.spec_from_file_location("benchmark_read_file_v7", script)
    benchmark = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(benchmark)
    fixture = tmp_path / "fixture.pdf"
    fixture.write_bytes(b"fixture")
    monkeypatch.setattr(
        benchmark,
        "_read_file",
        lambda path: (
            "<!-- read-file: source_page=1 -->\n"
            "<!-- read-file: source_page=2 -->\ncontent"
        ),
        raising=False,
    )
    result = benchmark._run_case(
        {"id": "two-pages", "path": str(fixture), "format": "pdf", "pages": 2}
    )
    assert result["status"] == "passed"
    assert result["actualCounts"] == {"pages": 2}
    assert result["firstResultMs"] is None
    assert result["retryCount"] is None
    assert result["processRssHighWaterBytes"] >= result["processRssBeforeBytes"]
    assert result["temporaryDiskHighWaterBytes"] is not None

    calls = []
    run_case = benchmark._run_case
    monkeypatch.setattr(
        benchmark,
        "_run_case",
        lambda case, *, include_warm: calls.append((case["id"], include_warm))
        or {"id": case["id"], "status": "passed"},
    )
    soak = benchmark._run_soak([{"id": "a"}, {"id": "b"}], 5)
    assert calls == [
        ("a", False),
        ("b", False),
        ("a", False),
        ("b", False),
        ("a", False),
    ]
    assert soak["completedRequests"] == 5
    assert soak["status"] == "passed"

    monkeypatch.setattr(
        benchmark,
        "_read_file",
        lambda path: (_ for _ in ()).throw(RuntimeError("execution failed")),
    )
    failed = run_case(
        {"id": "failed", "path": str(fixture), "format": "pdf"}
    )
    assert failed["status"] == "failed"
    assert "execution failed" in failed["error"]

    outputs = iter(["valid cold output", "[read_file failed] warm failure"])
    monkeypatch.setattr(benchmark, "_read_file", lambda path: next(outputs))
    warm_failed = run_case(
        {"id": "warm-failed", "path": str(fixture), "format": "pdf"}
    )
    assert warm_failed["status"] == "failed"


def test_benchmark_aggregates_tegrastats_samples_across_workload(tmp_path, monkeypatch):
    import importlib.util

    script = Path("scripts/benchmark_read_file_v7.py").resolve()
    spec = importlib.util.spec_from_file_location("benchmark_read_file_v7_telemetry", script)
    benchmark = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(benchmark)

    samples = [
        "RAM 100/1000MB CPU [10%,20%] cpu@40C gpu@42C VDD_GPU 2000mW/1900mW VIN 20000mW/19500mW",
        "RAM 120/1000MB CPU [30%,40%] cpu@50C gpu@45C VDD_GPU 3000mW/2500mW VIN 22000mW/21000mW",
    ]
    aggregates = benchmark._aggregate_tegrastats_samples(samples)
    assert aggregates == {
        "sampleCount": 2,
        "ramUsedMiBMax": 120,
        "ramUsedMiBAverage": 110.0,
        "cpuTemperatureCMax": 50.0,
        "gpuTemperatureCMax": 45.0,
        "gpuPowerMilliwattsMax": 3000,
        "inputPowerMilliwattsMax": 22000,
    }

    fixture = tmp_path / "fixture.pdf"
    fixture.write_bytes(b"fixture")
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "<!-- READ_FILE_V7_MANIFEST_BEGIN -->\n"
        + json.dumps(
            {
                "schemaVersion": 1,
                "cases": [{"id": "case", "path": str(fixture), "format": "pdf"}],
            }
        )
        + "\n<!-- READ_FILE_V7_MANIFEST_END -->\n"
    )
    output = tmp_path / "report.json"
    events = []

    class FakeTelemetry:
        def start(self):
            events.append("telemetry-start")

        def stop(self):
            events.append("telemetry-stop")

        def report(self):
            return {"sampleCount": 2}, None

    monkeypatch.setattr(benchmark, "_TegrastatsSampler", FakeTelemetry)
    monkeypatch.setattr(
        benchmark,
        "_run_case",
        lambda case: events.append("case") or {"id": case["id"], "status": "passed"},
    )
    assert benchmark.main(["--manifest", str(manifest), "--output", str(output)]) == 0
    assert events == ["telemetry-start", "case", "telemetry-stop"]
    assert json.loads(output.read_text())["jetsonTelemetry"]["sampleCount"] == 2


def _minimal_docx(path: Path):
    content_types = b'''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>'''
    relationships = b'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''
    document = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>THOR DOCUMENT CANARY 2026</w:t></w:r></w:p><w:sectPr/></w:body></w:document>'''
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document)


@pytest.mark.paddle_ocr_integration
@pytest.mark.skipif(
    os.environ.get("RUN_PADDLE_OCR_INTEGRATION") != "1",
    reason="set RUN_PADDLE_OCR_INTEGRATION=1 on Thor with the live PaddleX service",
)
def test_live_paddleocr_image_pdf_and_document_routes(tmp_path):
    from PIL import ImageDraw
    from backend.config import get_config

    image_path = tmp_path / "canary.png"
    image = Image.new("RGB", (1400, 500), "white")
    ImageDraw.Draw(image).text((80, 220), "THOR IMAGE ACCEPTANCE 2026", fill="black")
    image.save(image_path)
    pdf_path = tmp_path / "canary.pdf"
    image.save(pdf_path, "PDF", resolution=150)
    docx_path = tmp_path / "canary.docx"
    _minimal_docx(docx_path)

    app_config = get_config()
    read_config = replace(
        app_config.read_file,
        allowed_roots=[str(tmp_path)],
        cache_path=str(tmp_path / "cache.sqlite3"),
        main_llm_memory_reserve_gb=0,
    )
    service = ReadFileService(config=read_config, ocr_config=app_config.ocr)
    try:
        outputs = {
            "image": service.read(str(image_path)),
            "pdf": service.read(str(pdf_path)),
            "document": service.read(str(docx_path)),
        }
    finally:
        service.close()
    for route, output in outputs.items():
        assert "[read_file failed]" not in output, (route, output)
        assert "THOR" in output.upper(), (route, output)
        assert "127.0.0.1" not in output


@pytest.mark.paddle_ocr_integration
@pytest.mark.skipif(
    os.environ.get("RUN_PADDLE_OCR_INTEGRATION") != "1",
    reason="set RUN_PADDLE_OCR_INTEGRATION=1 on Thor with the live PaddleX service",
)
def test_live_paddleocr_twenty_request_soak_keeps_service_identity(tmp_path):
    import psutil
    from PIL import ImageDraw
    from backend.config import get_config

    app_config = get_config()
    attestation_path = Path(app_config.ocr.attestation_path)
    if not attestation_path.is_absolute():
        attestation_path = Path(__file__).resolve().parents[1] / attestation_path
    before_attestation = json.loads(attestation_path.read_text())
    service_pid = before_attestation["processIdentity"]["pid"]
    process = psutil.Process()
    rss_before = process.memory_info().rss
    read_config = replace(
        app_config.read_file,
        allowed_roots=[str(tmp_path)],
        cache_path=str(tmp_path / "soak-cache.sqlite3"),
        main_llm_memory_reserve_gb=0,
    )
    service = ReadFileService(config=read_config, ocr_config=app_config.ocr)
    try:
        for index in range(20):
            path = tmp_path / f"soak-{index}.png"
            image = Image.new("RGB", (1000, 320), "white")
            ImageDraw.Draw(image).text(
                (60, 140), f"THOR SOAK REQUEST {index + 1}", fill="black"
            )
            image.save(path)
            output = service.read(str(path))
            assert "[read_file failed]" not in output, (index, output)
            assert output.strip()
    finally:
        service.close()
    rss_growth = process.memory_info().rss - rss_before
    assert rss_growth < 512 * 1024**2
    assert Path(f"/proc/{service_pid}").is_dir()
    after_attestation = json.loads(attestation_path.read_text())
    assert after_attestation["processIdentity"]["pid"] == service_pid
