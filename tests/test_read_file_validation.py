from __future__ import annotations

import stat
import zipfile
from dataclasses import replace

import pytest

from backend.config import ReadFileConfig
from backend.tools.read_file.errors import ReadFileError
from backend.tools.read_file.file_validator import validate_source


def _config(root, **overrides):
    return replace(ReadFileConfig(allowed_roots=[str(root)]), **overrides)


def test_resolved_path_allowlist_rejects_symlink_escape(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = allowed / "escape.txt"
    link.symlink_to(outside)

    with pytest.raises(ReadFileError, match="PATH_NOT_ALLOWED"):
        validate_source(link, _config(allowed))


def test_resolved_path_allowlist_accepts_symlink_target_inside_root(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    target = allowed / "target.txt"
    target.write_text("ok")
    link = allowed / "inside.txt"
    link.symlink_to(target)

    validated = validate_source(link, _config(allowed))
    assert validated.path == target.resolve()


def test_oversized_source_is_rejected(tmp_path):
    path = tmp_path / "large.txt"
    path.write_bytes(b"12345")
    with pytest.raises(ReadFileError, match="FILE_TOO_LARGE"):
        validate_source(path, _config(tmp_path, max_source_bytes=4))


@pytest.mark.parametrize("kind", ["members", "expanded", "ratio", "traversal", "symlink"])
def test_suspicious_zip_metadata_is_rejected(tmp_path, kind):
    path = tmp_path / "book.xlsx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "A" * (1000 if kind == "ratio" else 10))
        if kind == "members":
            archive.writestr("xl/a.xml", "a")
        elif kind == "traversal":
            archive.writestr("../escape", "x")
        elif kind == "symlink":
            info = zipfile.ZipInfo("xl/media/link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "target")

    config = _config(
        tmp_path,
        max_archive_members=2 if kind == "members" else 100,
        max_archive_uncompressed_bytes=5 if kind == "expanded" else 10_000,
        max_archive_compression_ratio=2 if kind == "ratio" else 1000,
    )
    with pytest.raises(ReadFileError, match="UNSAFE_ARCHIVE"):
        validate_source(path, config)
