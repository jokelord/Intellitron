from __future__ import annotations

import subprocess
import os
import signal
from dataclasses import replace
from pathlib import Path

import fitz
import pytest

from backend.config import ReadFileConfig
from backend.tools.read_file.errors import ReadFileError
from backend.tools.read_file.office_renderer import _build_office_command, render_office_to_pdf
from backend.tools.read_file.pdf_chunker import inspect_pdf, iter_pdf_chunks
from backend.tools.read_file import pdf_chunker


def _pdf(path: Path, pages: int) -> None:
    document = fitz.open()
    for _ in range(pages):
        document.new_page(width=100, height=100)
    document.save(path)
    document.close()


def _pdf_subset_size(path: Path, pages: tuple[int, ...], output: Path) -> int:
    source = fitz.open(path)
    subset = fitz.open()
    try:
        for page in pages:
            subset.insert_pdf(source, from_page=page - 1, to_page=page - 1)
        subset.save(output, garbage=4, deflate=True)
    finally:
        subset.close()
        source.close()
    return output.stat().st_size


@pytest.mark.parametrize(
    ("suffix", "filter_name"),
    [(".docx", "writer_pdf_Export"), (".doc", "writer_pdf_Export"),
     (".pptx", "impress_pdf_Export"), (".ppt", "impress_pdf_Export")],
)
def test_office_command_uses_exact_writer_or_impress_filter(tmp_path, suffix, filter_name):
    source = tmp_path / f"input{suffix}"
    profile = tmp_path / "profile"
    output = tmp_path / "output"
    command = _build_office_command(source, profile, output, "libreoffice")
    assert command[0] == "libreoffice"
    assert "--headless" in command
    assert f"pdf:{filter_name}" in command
    assert any(item.startswith("-env:UserInstallation=file://") for item in command)
    assert not any("calc" in item.lower() for item in command)


@pytest.mark.parametrize("suffix", [".xls", ".ods", ".xlsx"])
def test_office_renderer_rejects_calc_formats_before_subprocess(tmp_path, suffix, monkeypatch):
    source = tmp_path / f"input{suffix}"
    source.write_bytes(b"x")
    called = False

    def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(ReadFileError, match="UNSUPPORTED_FORMAT"):
        render_office_to_pdf(source, ReadFileConfig(), None)
    assert called is False


def test_pdf_inspection_and_24_page_overlap_map(tmp_path):
    path = tmp_path / "long.pdf"
    _pdf(path, 50)
    config = replace(ReadFileConfig(), pdf_chunk_pages=24, pdf_chunk_overlap_pages=1)
    info = inspect_pdf(path, None, config)
    chunks = list(iter_pdf_chunks(path, info, config))

    assert info.page_count == 50
    assert chunks[0].source_pages == tuple(range(1, 25))
    assert chunks[1].source_pages == tuple(range(24, 48))
    assert chunks[2].source_pages == tuple(range(47, 51))
    assert chunks[1].overlap_source_pages == (24,)
    assert chunks[2].overlap_source_pages == (47,)
    assert all(len(chunk.source_pages) <= config.pdf_chunk_pages for chunk in chunks)
    assert all(chunk.path.stat().st_size <= config.pdf_chunk_bytes for chunk in chunks)
    for chunk in chunks:
        chunk.close()


def test_invalid_and_over_page_limit_pdfs_are_rejected(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-broken")
    with pytest.raises(ReadFileError, match="INVALID_PDF"):
        inspect_pdf(bad, None, ReadFileConfig())

    many = tmp_path / "many.pdf"
    _pdf(many, 2)
    with pytest.raises(ReadFileError, match="INVALID_PDF"):
        inspect_pdf(many, None, replace(ReadFileConfig(), max_pdf_pages=1))


@pytest.mark.parametrize("pages", [tuple(range(1, 3)), tuple(range(1, 5))])
def test_byte_split_ownership_children_are_strict_disjoint_subsets(pages):
    left, right = pdf_chunker._split_owned_source_pages(pages)
    assert left and right
    assert len(left) < len(pages)
    assert len(right) < len(pages)
    assert left + right == pages
    assert set(left).isdisjoint(right)


@pytest.mark.parametrize("overlap", [0, 1])
def test_two_page_tiny_byte_caps_converge_or_fail_explicitly(tmp_path, overlap):
    path = tmp_path / f"two-{overlap}.pdf"
    _pdf(path, 2)
    one_page_cap = max(
        _pdf_subset_size(path, (page,), tmp_path / f"one-{overlap}-{page}.pdf")
        for page in (1, 2)
    )
    config = replace(
        ReadFileConfig(),
        pdf_chunk_pages=2,
        pdf_chunk_overlap_pages=overlap,
        pdf_chunk_bytes=one_page_cap,
    )
    info = inspect_pdf(path, None, config)
    if overlap:
        with pytest.raises(ReadFileError, match="FILE_TOO_LARGE") as failure:
            list(iter_pdf_chunks(path, info, config))
        assert "overlap-expanded PDF leaf exceeds chunk byte limit" in str(
            failure.value
        )
        return

    chunks = list(iter_pdf_chunks(path, info, config))
    try:
        assert [page for chunk in chunks for page in chunk.owned_source_pages] == [1, 2]
        assert all(not chunk.overlap_source_pages for chunk in chunks)
        assert all(chunk.path.stat().st_size <= one_page_cap for chunk in chunks)
    finally:
        for chunk in chunks:
            chunk.close()


@pytest.mark.parametrize("overlap", [0, 1, 2])
def test_four_page_recursive_chunks_preserve_ownership_and_overlap(tmp_path, overlap):
    path = tmp_path / f"four-{overlap}.pdf"
    _pdf(path, 4)
    emitted_width = overlap + 1
    cap = max(
        _pdf_subset_size(
            path,
            tuple(range(max(1, page - overlap), page + 1)),
            tmp_path / f"leaf-{overlap}-{page}.pdf",
        )
        for page in range(1, 5)
    )
    whole_size = _pdf_subset_size(path, (1, 2, 3, 4), tmp_path / f"whole-{overlap}.pdf")
    assert cap < whole_size or emitted_width >= 4
    config = replace(
        ReadFileConfig(),
        pdf_chunk_pages=4,
        pdf_chunk_overlap_pages=overlap,
        pdf_chunk_bytes=cap,
    )
    info = inspect_pdf(path, None, config)
    chunks = list(iter_pdf_chunks(path, info, config))
    try:
        owned = [page for chunk in chunks for page in chunk.owned_source_pages]
        assert owned == [1, 2, 3, 4]
        assert len(owned) == len(set(owned))
        for chunk in chunks:
            assert chunk.path.stat().st_size <= cap
            assert set(chunk.owned_source_pages).isdisjoint(chunk.overlap_source_pages)
            if chunk.overlap_source_pages:
                assert chunk.overlap_source_pages == tuple(
                    range(
                        max(1, chunk.owned_source_pages[0] - overlap),
                        chunk.owned_source_pages[0],
                    )
                )
    finally:
        for chunk in chunks:
            chunk.close()


@pytest.mark.parametrize("overlap", [-1, 2, 3])
def test_page_ranges_reject_overlap_equal_to_or_greater_than_chunk_pages(overlap):
    with pytest.raises(ValueError, match="overlap"):
        list(pdf_chunker._page_ranges(4, 2, overlap))


def test_office_profile_disables_macros_updates_and_uses_safe_mode(tmp_path):
    source = tmp_path / "input.docx"
    profile = tmp_path / "profile"
    output = tmp_path / "output"
    command = _build_office_command(source, profile, output, "libreoffice")
    assert "--safe-mode" in command
    registry = (profile / "user/registrymodifications.xcu").read_text()
    assert "MacroSecurity" in registry
    assert "UpdateDocMode" in registry
    assert "/org.openoffice.Office.Common/Security/Scripting" in registry
    assert "/org.openoffice.Office.Common/Load" in registry


def test_office_conversion_polls_abort_and_terminates_only_owned_group(tmp_path, monkeypatch):
    source = tmp_path / "input.docx"
    source.write_bytes(b"doc")
    terminated = []
    launched = {}

    class Process:
        pid = 4321
        returncode = None

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            self.returncode = -signal.SIGTERM
            return b"", b""

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/libreoffice")
    def popen(*args, **kwargs):
        launched.update(kwargs)
        return Process()

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: terminated.append((pid, sig)))
    checks = iter([False, True])
    with pytest.raises(ReadFileError, match="ABORTED"):
        render_office_to_pdf(
            source,
            replace(ReadFileConfig(), office_timeout_seconds=10),
            lambda: next(checks, True),
        )
    assert terminated == [(4321, signal.SIGTERM)]
    assert launched["cwd"].startswith("/tmp/read-file-office-")
    assert launched["env"]["HOME"].startswith("/tmp/read-file-office-")
    assert launched["env"]["TMPDIR"].startswith("/tmp/read-file-office-")
    assert launched["env"]["http_proxy"] == "http://127.0.0.1:9"
    assert launched["env"]["https_proxy"] == "http://127.0.0.1:9"
