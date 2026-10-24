from __future__ import annotations

import threading
import time
import sqlite3
from dataclasses import replace

import pytest
from PIL import Image
import fitz

from backend.config import ReadFileConfig
from backend.tools.read_file.cache import ReadFileCache, build_cache_key
from backend.tools.read_file.errors import ReadFileError, ReadFileErrorCode
from backend.tools.read_file.models import ReadFileResult
from backend.tools.read_file.resource_guard import ResourceGuard
from backend.tools.read_file.paddleocr_client import OCRPage
from backend.tools.read_file.service import ReadFileService


def _result(markdown="ok"):
    return ReadFileResult(
        success=True,
        markdown=markdown,
        source_path="/tmp/a.pdf",
        source_format="pdf",
        normalized_format=None,
        engine="PaddleOCR-VL-1.6",
        model_version="1.6",
    )


def test_cache_key_is_deterministic_invalidates_inputs_and_excludes_plain_password():
    options = {"document_mode": "auto", "password": "top-secret"}
    first = build_cache_key("source", "parser", "model", "config", options)
    assert first == build_cache_key("source", "parser", "model", "config", options)
    assert first != build_cache_key("source", "parser2", "model", "config", options)
    assert first != build_cache_key("source", "parser", "model2", "config", options)
    assert first != build_cache_key("source", "parser", "model", "config2", options)
    assert "top-secret" not in first


def test_sqlite_cache_round_trip_ttl_cleanup_and_rollback(tmp_path, monkeypatch):
    now = [1000.0]
    cache = ReadFileCache(
        tmp_path / "cache.sqlite3",
        success_ttl_seconds=30,
        partial_ttl_seconds=7,
        failure_ttl_seconds=1,
        clock=lambda: now[0],
    )
    cache.put("key", _result(), "success")
    assert cache.get("key") == _result()
    now[0] += 31
    assert cache.get("key") is None
    assert cache.count() == 0

    cache.put("stable", _result("old"), "success")
    monkeypatch.setattr(cache, "_serialize", lambda result: (_ for _ in ()).throw(ValueError("boom")))
    with pytest.raises(ValueError):
        cache.put("stable", _result("new"), "success")
    monkeypatch.undo()
    assert cache.get("stable").markdown == "old"
    cache.close()


def test_resource_guard_rejects_low_memory(monkeypatch):
    guard = ResourceGuard(
        replace(ReadFileConfig(), main_llm_memory_reserve_gb=64)
    )
    monkeypatch.setattr(
        "backend.tools.read_file.resource_guard.psutil.virtual_memory",
        lambda: type("Memory", (), {"available": 63 * 1024**3})(),
    )
    with pytest.raises(ReadFileError, match="MEMORY_RESERVE") as caught:
        with guard.acquire(None):
            pass
    assert caught.value.safe_message == (
        "available UMA is below the configured document-processing floor"
    )


def test_resource_guard_limits_one_active_two_queued_and_releases_after_exception(monkeypatch):
    config = replace(
        ReadFileConfig(),
        ocr_concurrency=1,
        ocr_queue_limit=2,
        main_llm_memory_reserve_gb=0,
    )
    guard = ResourceGuard(config)
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with guard.acquire(None):
            entered.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(2)
    abort = threading.Event()
    errors = []

    def waiter():
        try:
            with guard.acquire(abort.is_set):
                pass
        except ReadFileError as exc:
            errors.append(exc.code.value)

    waiters = [threading.Thread(target=waiter) for _ in range(2)]
    for item in waiters:
        item.start()
    deadline = time.monotonic() + 2
    while guard.queue_depth < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    with pytest.raises(ReadFileError, match="RESOURCE_BUSY"):
        with guard.acquire(None):
            pass
    abort.set()
    release.set()
    thread.join(2)
    for item in waiters:
        item.join(2)
    assert "ABORTED" in errors

    with pytest.raises(RuntimeError):
        with guard.acquire(None):
            raise RuntimeError("body failure")
    assert guard.active_count == 0


def test_service_validates_then_caches_complete_visual_result(tmp_path):
    path = tmp_path / "page.png"
    Image.new("RGB", (8, 8), "white").save(path)
    config = replace(
        ReadFileConfig(),
        allowed_roots=[str(tmp_path)],
        main_llm_memory_reserve_gb=0,
    )
    cache = ReadFileCache(
        tmp_path / "cache.sqlite3",
        success_ttl_seconds=30,
        partial_ttl_seconds=7,
        failure_ttl_seconds=1,
    )

    class OCR:
        calls = 0

        def parse_file(self, path, file_type, options, check_abort):
            self.calls += 1
            return [OCRPage(1, "cached content")]

    ocr = OCR()
    service = ReadFileService(
        config=config,
        ocr_client=ocr,
        cache=cache,
        resource_guard=ResourceGuard(config),
    )
    assert "缓存: 未命中" in service.read(str(path))
    assert "缓存: 命中" in service.read(str(path))
    assert ocr.calls == 1
    cache.close()


def test_service_caches_terminal_ocr_failure_without_retrying_provider(tmp_path):
    path = tmp_path / "page.png"
    Image.new("RGB", (8, 8), "white").save(path)
    config = replace(
        ReadFileConfig(),
        allowed_roots=[str(tmp_path)],
        main_llm_memory_reserve_gb=0,
    )
    cache = ReadFileCache(
        tmp_path / "failure-cache.sqlite3",
        success_ttl_seconds=30,
        partial_ttl_seconds=7,
        failure_ttl_seconds=60,
    )

    class OCR:
        calls = 0

        def parse_file(self, path, file_type, options, check_abort):
            self.calls += 1
            raise ReadFileError(ReadFileErrorCode.OCR_REQUEST_FAILED, "provider failed")

    ocr = OCR()
    service = ReadFileService(
        config=config,
        ocr_client=ocr,
        cache=cache,
        resource_guard=ResourceGuard(config),
    )
    first = service.read(str(path))
    second = service.read(str(path))
    assert "OCR_REQUEST_FAILED" in first
    assert "OCR_REQUEST_FAILED" in second
    assert ocr.calls == 1
    cache.close()


def test_encrypted_pdf_authenticates_before_cache_and_password_digest_is_keyed(tmp_path):
    path = tmp_path / "encrypted.pdf"
    document = fitz.open()
    document.new_page()
    document.save(
        path,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="correct-secret",
    )
    document.close()
    config = replace(
        ReadFileConfig(),
        allowed_roots=[str(tmp_path)],
        main_llm_memory_reserve_gb=0,
        cache_path=str(tmp_path / "encrypted-cache.sqlite3"),
    )

    class OCR:
        calls = 0

        def parse_file(self, path, file_type, options, check_abort):
            self.calls += 1
            return [OCRPage(1, "authenticated PDF content")]

    service = ReadFileService(config=config, ocr_client=OCR())
    correct_key = service._cache_key(
        path,
        detected_format=".pdf",
        route="pdf",
        password="correct-secret",
        document_mode="auto",
        recognize_seals=False,
    )
    other_key = service._cache_key(
        path,
        detected_format=".pdf",
        route="pdf",
        password="different-secret",
        document_mode="auto",
        recognize_seals=False,
    )
    assert correct_key != other_key
    assert "correct-secret" not in correct_key
    assert "different-secret" not in other_key
    assert "authenticated PDF content" in service.read(str(path), password="correct-secret")
    wrong = service.read(str(path), password="wrong-secret")
    assert "PDF_PASSWORD_REQUIRED" in wrong
    assert service._ocr_client.calls == 1
    service.close()


def test_cache_key_partitions_detected_format_route_and_schema(tmp_path):
    path = tmp_path / "same.bin"
    path.write_bytes(b"same bytes")
    config = replace(ReadFileConfig(), allowed_roots=[str(tmp_path)])
    service = ReadFileService(config=config, ocr_client=object())
    common = dict(
        password=None,
        document_mode="auto",
        recognize_seals=False,
    )
    pdf = service._cache_key(path, detected_format=".pdf", route="pdf", **common)
    image = service._cache_key(path, detected_format=".png", route="image", **common)
    assert pdf != image
    service.close()


def test_cache_schema_mismatch_clears_existing_entries(tmp_path):
    path = tmp_path / "cache.sqlite3"
    cache = ReadFileCache(
        path,
        success_ttl_seconds=30,
        partial_ttl_seconds=7,
        failure_ttl_seconds=1,
    )
    cache.put("old", _result(), "success")
    cache.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE metadata SET value = '0' WHERE key = 'schema_version'"
        )
        connection.commit()

    reopened = ReadFileCache(
        path,
        success_ttl_seconds=30,
        partial_ttl_seconds=7,
        failure_ttl_seconds=1,
    )
    assert reopened.count() == 0
    reopened.close()


def test_service_caches_final_elapsed_metadata(tmp_path, monkeypatch):
    path = tmp_path / "page.png"
    Image.new("RGB", (8, 8), "white").save(path)
    config = replace(
        ReadFileConfig(),
        allowed_roots=[str(tmp_path)],
        main_llm_memory_reserve_gb=0,
        cache_path=str(tmp_path / "elapsed-cache.sqlite3"),
    )

    class OCR:
        def parse_file(self, path, file_type, options, check_abort):
            return [OCRPage(1, "timed content")]

    ticks = iter([10.0, 10.125])
    monkeypatch.setattr(
        "backend.tools.read_file.service.time.monotonic", lambda: next(ticks)
    )
    service = ReadFileService(config=config, ocr_client=OCR())
    service.read(str(path))
    key = service._cache_key(
        path,
        detected_format=".png",
        route="image",
        password=None,
        document_mode="auto",
        recognize_seals=False,
    )
    cached = service.cache.get(key)
    assert cached is not None
    assert cached.elapsed_ms == 125
    service.close()
