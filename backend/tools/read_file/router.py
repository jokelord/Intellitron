from __future__ import annotations

from enum import StrEnum

class FileRoute(StrEnum):
    TEXT = "text"
    VISUAL_OFFICE = "visual_office"
    PDF = "pdf"
    IMAGE = "image"
    XLSX = "xlsx"
    PLACEHOLDER_SPREADSHEET = "placeholder_spreadsheet"


def classify_route(detected) -> FileRoute:
    """Classify an already detected type without opening or parsing the file."""
    return FileRoute(detected.kind)
