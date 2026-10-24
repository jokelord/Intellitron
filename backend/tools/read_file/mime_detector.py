from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

from .errors import ReadFileError, ReadFileErrorCode


TEXT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".py", ".js", ".jsx", ".ts", ".tsx", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".html",
    ".htm", ".css", ".scss", ".sh", ".bash", ".zsh", ".c", ".cc", ".cpp",
    ".h", ".hpp", ".java", ".go", ".rs", ".sql", ".csv", ".log",
}
IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".gif",
    ".avif", ".heic", ".heif", ".jp2", ".j2k", ".ico", ".pbm", ".pgm",
    ".ppm", ".svg",
}
OFFICE_EXTENSIONS = {".doc", ".docx", ".ppt", ".pptx"}


@dataclass(frozen=True)
class DetectedFileType:
    extension: str
    mime: str
    kind: str
    is_archive: bool = False


def _libmagic_mime(path: Path) -> str | None:
    try:
        import magic

        return str(magic.from_file(str(path), mime=True))
    except Exception:
        return None


def _looks_textual(data: bytes) -> bool:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            data.decode("utf-16")
            return True
        except UnicodeDecodeError:
            return False
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        controls = sum(byte < 9 or 13 < byte < 32 for byte in data)
        return bool(data) and controls / len(data) < 0.02


def _signature_mime(data: bytes) -> str | None:
    signatures = (
        (b"%PDF-", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"BM", "image/bmp"),
        (b"II*\x00", "image/tiff"),
        (b"MM\x00*", "image/tiff"),
        (b"\x00\x00\x01\x00", "image/x-icon"),
        (b"\xff\x4f\xff\x51", "image/jp2"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage"),
        (b"PK\x03\x04", "application/zip"),
    )
    for signature, mime in signatures:
        if data.startswith(signature):
            return mime
    stripped = data.lstrip()
    if stripped.startswith(b"<svg") or b"<svg" in stripped[:512].lower():
        return "image/svg+xml"
    if _looks_textual(data):
        return "text/plain"
    return None


def _validate_ooxml_kind(path: Path, extension: str) -> None:
    required = {
        ".docx": "word/document.xml",
        ".pptx": "ppt/presentation.xml",
        ".xlsx": "xl/workbook.xml",
    }[extension]
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReadFileError(
            ReadFileErrorCode.MIME_MISMATCH, "Office file is not a valid ZIP container"
        ) from exc
    if "[Content_Types].xml" not in names or required not in names:
        raise ReadFileError(
            ReadFileErrorCode.MIME_MISMATCH,
            f"Office container does not match {extension}",
        )


def detect_file_type(path: Path) -> DetectedFileType:
    extension = path.suffix.lower()
    try:
        with path.open("rb") as stream:
            prefix = stream.read(8192)
    except OSError as exc:
        raise ReadFileError(ReadFileErrorCode.FILE_NOT_FOUND, "source file was not found") from exc
    signature_mime = _signature_mime(prefix)
    magic_mime = _libmagic_mime(path)
    mime = signature_mime or magic_mime or "application/octet-stream"

    if extension == ".pdf":
        if signature_mime != "application/pdf":
            raise ReadFileError(ReadFileErrorCode.MIME_MISMATCH, "PDF signature is missing")
        return DetectedFileType(extension, mime, "pdf")
    if extension in {".docx", ".pptx", ".xlsx"}:
        if signature_mime != "application/zip":
            raise ReadFileError(ReadFileErrorCode.MIME_MISMATCH, "Office ZIP signature is missing")
        _validate_ooxml_kind(path, extension)
        route = "xlsx" if extension == ".xlsx" else "visual_office"
        return DetectedFileType(extension, mime, route, is_archive=True)
    if extension in {".doc", ".ppt", ".xls"}:
        if signature_mime != "application/x-ole-storage":
            raise ReadFileError(ReadFileErrorCode.MIME_MISMATCH, "OLE signature is missing")
        route = "placeholder_spreadsheet" if extension == ".xls" else "visual_office"
        return DetectedFileType(extension, mime, route)
    if extension == ".ods":
        if signature_mime != "application/zip":
            raise ReadFileError(ReadFileErrorCode.MIME_MISMATCH, "ODS ZIP signature is missing")
        return DetectedFileType(extension, mime, "placeholder_spreadsheet", True)
    if extension in IMAGE_EXTENSIONS:
        image_like = signature_mime and signature_mime.startswith("image/")
        if not image_like and not (magic_mime or "").startswith("image/"):
            raise ReadFileError(ReadFileErrorCode.MIME_MISMATCH, "image signature is missing")
        return DetectedFileType(extension, mime, "image")
    if extension in TEXT_EXTENSIONS or signature_mime == "text/plain":
        if not _looks_textual(prefix):
            raise ReadFileError(ReadFileErrorCode.MIME_MISMATCH, "text file contains binary data")
        return DetectedFileType(extension, mime, "text")
    raise ReadFileError(ReadFileErrorCode.UNSUPPORTED_FORMAT, "unknown binary format")
