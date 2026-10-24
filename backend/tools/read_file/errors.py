from __future__ import annotations

from enum import StrEnum


class ReadFileErrorCode(StrEnum):
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    MIME_MISMATCH = "MIME_MISMATCH"
    UNSAFE_ARCHIVE = "UNSAFE_ARCHIVE"
    INVALID_PDF = "INVALID_PDF"
    PDF_PASSWORD_REQUIRED = "PDF_PASSWORD_REQUIRED"
    INVALID_IMAGE = "INVALID_IMAGE"
    IMAGE_LIMIT_EXCEEDED = "IMAGE_LIMIT_EXCEEDED"
    OFFICE_UNAVAILABLE = "OFFICE_UNAVAILABLE"
    OFFICE_CONVERSION_FAILED = "OFFICE_CONVERSION_FAILED"
    OCR_UNAVAILABLE = "OCR_UNAVAILABLE"
    OCR_REQUEST_FAILED = "OCR_REQUEST_FAILED"
    OCR_RESPONSE_INVALID = "OCR_RESPONSE_INVALID"
    OCR_PAGE_FAILED = "OCR_PAGE_FAILED"
    RESOURCE_BUSY = "RESOURCE_BUSY"
    MEMORY_RESERVE = "MEMORY_RESERVE"
    ABORTED = "ABORTED"
    SPREADSHEET_INVALID = "SPREADSHEET_INVALID"
    INVALID_DOCUMENT_MODE = "INVALID_DOCUMENT_MODE"
    OFFICE_PASSWORD_UNSUPPORTED = "OFFICE_PASSWORD_UNSUPPORTED"


class ReadFileError(RuntimeError):
    def __init__(self, code: ReadFileErrorCode | str, message: str):
        self.code = ReadFileErrorCode(code)
        self.safe_message = " ".join(str(message).split())
        super().__init__(self.safe_message)

    def __str__(self) -> str:
        return f"[read_file failed] code={self.code.value}: {self.safe_message}"
