from __future__ import annotations

import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from backend.config import ReadFileConfig

from .errors import ReadFileError, ReadFileErrorCode
from .mime_detector import DetectedFileType, detect_file_type


@dataclass(frozen=True)
class ValidatedSource:
    path: Path
    detected_type: DetectedFileType
    size_bytes: int


def _is_allowed(path: Path, roots: list[str]) -> bool:
    for raw_root in roots:
        root = Path(raw_root).expanduser().resolve()
        if path == root or path.is_relative_to(root):
            return True
    return False


def _validate_archive(path: Path, config: ReadFileConfig) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > config.max_archive_members:
                raise ReadFileError(ReadFileErrorCode.UNSAFE_ARCHIVE, "archive has too many members")
            expanded = 0
            for info in infos:
                member = PurePosixPath(info.filename.replace("\\", "/"))
                mode = info.external_attr >> 16
                if (
                    member.is_absolute()
                    or ".." in member.parts
                    or info.flag_bits & 0x1
                    or stat.S_ISLNK(mode)
                ):
                    raise ReadFileError(ReadFileErrorCode.UNSAFE_ARCHIVE, "archive member is unsafe")
                expanded += info.file_size
                if expanded > config.max_archive_uncompressed_bytes:
                    raise ReadFileError(ReadFileErrorCode.UNSAFE_ARCHIVE, "archive expands beyond limit")
                ratio = info.file_size / max(info.compress_size, 1)
                if ratio > config.max_archive_compression_ratio:
                    raise ReadFileError(ReadFileErrorCode.UNSAFE_ARCHIVE, "archive compression ratio is unsafe")
    except ReadFileError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReadFileError(ReadFileErrorCode.UNSAFE_ARCHIVE, "archive is malformed") from exc


def validate_source(
    path: str | Path,
    config: ReadFileConfig,
    password: str | None = None,
) -> ValidatedSource:
    del password
    source = Path(path).expanduser()
    try:
        resolved = source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ReadFileError(ReadFileErrorCode.FILE_NOT_FOUND, "source file was not found") from exc
    if not resolved.is_file():
        raise ReadFileError(ReadFileErrorCode.FILE_NOT_FOUND, "source is not a regular file")
    if not _is_allowed(resolved, config.allowed_roots):
        raise ReadFileError(ReadFileErrorCode.PATH_NOT_ALLOWED, "resolved path is outside allowed roots")
    size = resolved.stat().st_size
    if size > config.max_source_bytes:
        raise ReadFileError(ReadFileErrorCode.FILE_TOO_LARGE, "source exceeds configured byte limit")
    detected = detect_file_type(resolved)
    if detected.is_archive:
        _validate_archive(resolved, config)
    return ValidatedSource(resolved, detected, size)
