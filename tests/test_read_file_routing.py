from __future__ import annotations

import zipfile

import pytest

from backend.tools.file_ops import read_file
from backend.tools.read_file.errors import ReadFileError
from backend.tools.read_file.mime_detector import detect_file_type
from backend.tools.read_file.router import FileRoute, classify_route


@pytest.mark.parametrize(
    ("name", "content", "route"),
    [
        ("a.txt", b"hello\n", FileRoute.TEXT),
        ("a.py", b"print('ok')\n", FileRoute.TEXT),
        ("a.pdf", b"%PDF-1.7\n", FileRoute.PDF),
        ("a.png", b"\x89PNG\r\n\x1a\n", FileRoute.IMAGE),
        ("a.j2k", b"\xff\x4f\xff\x51", FileRoute.IMAGE),
        ("a.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", FileRoute.PLACEHOLDER_SPREADSHEET),
        ("a.ods", b"PK\x03\x04", FileRoute.PLACEHOLDER_SPREADSHEET),
    ],
)
def test_routes_supported_extensions(tmp_path, name, content, route):
    path = tmp_path / name
    path.write_bytes(content)
    assert classify_route(detect_file_type(path)) is route


@pytest.mark.parametrize(
    ("name", "member", "route"),
    [
        ("a.docx", "word/document.xml", FileRoute.VISUAL_OFFICE),
        ("a.pptx", "ppt/presentation.xml", FileRoute.VISUAL_OFFICE),
        ("a.xlsx", "xl/workbook.xml", FileRoute.XLSX),
    ],
)
def test_ooxml_route_requires_matching_container(tmp_path, name, member, route):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(member, "<root/>")
    assert classify_route(detect_file_type(path)) is route


def test_unknown_text_is_accepted_but_unknown_binary_is_rejected(tmp_path):
    text = tmp_path / "notes.custom"
    text.write_text("plain unknown text")
    assert classify_route(detect_file_type(text)) is FileRoute.TEXT

    binary = tmp_path / "payload.custom"
    binary.write_bytes(b"\x00\x01\x02\xff")
    with pytest.raises(ReadFileError, match="UNSUPPORTED_FORMAT"):
        detect_file_type(binary)


def test_fake_pdf_is_rejected(tmp_path):
    path = tmp_path / "fake.pdf"
    path.write_bytes(b"MZ executable")
    with pytest.raises(ReadFileError, match="MIME_MISMATCH"):
        detect_file_type(path)


def test_mime_detection_reads_only_a_bounded_prefix(tmp_path, monkeypatch):
    path = tmp_path / "bounded.txt"
    path.write_text("hello")
    monkeypatch.setattr(type(path), "read_bytes", lambda self: (_ for _ in ()).throw(AssertionError("whole-file read")))
    assert classify_route(detect_file_type(path)) is FileRoute.TEXT


def test_agent_password_documentation_is_encrypted_pdf_only():
    documentation = read_file.__doc__ or ""
    assert "加密 PDF" in documentation
    assert "Office 文档密码" not in documentation
