from __future__ import annotations

import hashlib
import posixpath
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from defusedxml import ElementTree as SafeElementTree

from backend.config import ReadFileConfig

from .errors import ReadFileError, ReadFileErrorCode


REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
DRAWING_MAIN_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


@dataclass
class EmbeddedImageSource:
    path: Path
    sha256: str
    media_name: str
    placements: tuple[tuple[str, str], ...]
    _temporary_directory: tempfile.TemporaryDirectory[str] | None = None

    def close(self) -> None:
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None


def _relationships_path(part: str) -> str:
    parent, name = posixpath.split(part)
    return posixpath.join(parent, "_rels", f"{name}.rels")


def _resolve_part(base_part: str, target: str, expected_prefix: str) -> str:
    if not target or target.startswith("\\") or ":" in target:
        raise ReadFileError(ReadFileErrorCode.SPREADSHEET_INVALID, "OOXML relationship target is unsafe")
    if target.startswith("/"):
        resolved = posixpath.normpath(target.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))
    if resolved.startswith("../") or not resolved.startswith(expected_prefix):
        raise ReadFileError(ReadFileErrorCode.SPREADSHEET_INVALID, "OOXML relationship escapes its allowed area")
    return resolved


def _parse_xml(archive: zipfile.ZipFile, name: str):
    try:
        return SafeElementTree.fromstring(archive.read(name))
    except (KeyError, ValueError, SafeElementTree.ParseError) as exc:
        raise ReadFileError(ReadFileErrorCode.SPREADSHEET_INVALID, f"OOXML part is missing or malformed: {name}") from exc


def _relationships(archive: zipfile.ZipFile, part: str) -> dict[str, str]:
    root = _parse_xml(archive, _relationships_path(part))
    result = {}
    for relationship in root.findall(f"{{{PKG_REL_NS}}}Relationship"):
        relation_id = relationship.attrib.get("Id")
        target = relationship.attrib.get("Target")
        mode = relationship.attrib.get("TargetMode")
        if relation_id and target:
            if mode and mode.lower() == "external":
                raise ReadFileError(ReadFileErrorCode.SPREADSHEET_INVALID, "external OOXML relationship is forbidden")
            result[relation_id] = target
    return result


def _column_name(index: int) -> str:
    value = index + 1
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _anchor_cell(marker) -> str:
    col = marker.find(f"{{{DRAWING_NS}}}col")
    row = marker.find(f"{{{DRAWING_NS}}}row")
    if col is None or row is None or col.text is None or row.text is None:
        raise ReadFileError(ReadFileErrorCode.SPREADSHEET_INVALID, "drawing anchor is malformed")
    return f"{_column_name(int(col.text))}{int(row.text) + 1}"


def extract_embedded_images(
    path: str | Path, limits: ReadFileConfig
) -> list[EmbeddedImageSource]:
    source = Path(path)
    grouped: dict[str, dict[str, object]] = {}
    try:
        with zipfile.ZipFile(source) as archive:
            workbook = _parse_xml(archive, "xl/workbook.xml")
            workbook_relationships = _relationships(archive, "xl/workbook.xml")
            sheets: list[tuple[str, str]] = []
            for sheet in workbook.findall(f".//{{{SHEET_NS}}}sheet"):
                name = sheet.attrib.get("name")
                relation_id = sheet.attrib.get(f"{{{REL_NS}}}id")
                if not name or relation_id not in workbook_relationships:
                    raise ReadFileError(ReadFileErrorCode.SPREADSHEET_INVALID, "worksheet relationship is missing")
                sheet_part = _resolve_part(
                    "xl/workbook.xml", workbook_relationships[relation_id], "xl/worksheets/"
                )
                sheets.append((name, sheet_part))

            for sheet_name, sheet_part in sheets:
                worksheet = _parse_xml(archive, sheet_part)
                drawing = worksheet.find(f"{{{SHEET_NS}}}drawing")
                if drawing is None:
                    continue
                relation_id = drawing.attrib.get(f"{{{REL_NS}}}id")
                relationships = _relationships(archive, sheet_part)
                if relation_id not in relationships:
                    raise ReadFileError(ReadFileErrorCode.SPREADSHEET_INVALID, "drawing relationship is missing")
                drawing_part = _resolve_part(sheet_part, relationships[relation_id], "xl/drawings/")
                drawing_root = _parse_xml(archive, drawing_part)
                drawing_relationships = _relationships(archive, drawing_part)
                for anchor in list(drawing_root):
                    local_name = anchor.tag.rsplit("}", 1)[-1]
                    if local_name not in {"oneCellAnchor", "twoCellAnchor"}:
                        continue
                    start = _anchor_cell(anchor.find(f"{{{DRAWING_NS}}}from"))
                    end_marker = anchor.find(f"{{{DRAWING_NS}}}to")
                    end = _anchor_cell(end_marker) if end_marker is not None else start
                    anchor_range = start if start == end else f"{start}:{end}"
                    blip = anchor.find(f".//{{{DRAWING_MAIN_NS}}}blip")
                    relation_id = blip.attrib.get(f"{{{REL_NS}}}embed") if blip is not None else None
                    if relation_id not in drawing_relationships:
                        raise ReadFileError(ReadFileErrorCode.SPREADSHEET_INVALID, "image relationship is missing")
                    media_part = _resolve_part(
                        drawing_part, drawing_relationships[relation_id], "xl/media/"
                    )
                    data = archive.read(media_part)
                    if len(data) > limits.max_normalized_bytes:
                        raise ReadFileError(ReadFileErrorCode.SPREADSHEET_INVALID, "embedded image exceeds byte limit")
                    digest = hashlib.sha256(data).hexdigest()
                    entry = grouped.setdefault(
                        digest,
                        {"data": data, "media_name": PurePosixPath(media_part).name, "placements": []},
                    )
                    entry["placements"].append((sheet_name, anchor_range))
    except ReadFileError:
        raise
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ReadFileError(ReadFileErrorCode.SPREADSHEET_INVALID, "XLSX media container is malformed") from exc

    results = []
    for digest, entry in grouped.items():
        temporary = tempfile.TemporaryDirectory(prefix="read-file-xlsx-media-")
        media_name = str(entry["media_name"])
        output = Path(temporary.name) / media_name
        output.write_bytes(entry["data"])
        results.append(
            EmbeddedImageSource(
                output,
                digest,
                media_name,
                tuple(entry["placements"]),
                temporary,
            )
        )
    return results
