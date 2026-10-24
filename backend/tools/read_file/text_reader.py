from __future__ import annotations

from pathlib import Path


_LEGACY_ENCODING_FALLBACKS = (
    "gb18030",
    "big5",
    "shift_jis",
    "euc_kr",
    "latin_1",
)
_DETECTION_SAMPLE_BYTES = 65536


def _decoded_looks_like(decoded: str, encoding: str) -> bool:
    if encoding in ("shift_jis", "cp932", "shift_jis_2004"):
        return any(
            "\u3040" <= char <= "\u309f" or "\u30a0" <= char <= "\u30ff"
            for char in decoded
        )
    if encoding in ("euc_kr", "cp949"):
        return any("\uac00" <= char <= "\ud7af" for char in decoded)
    return True


def _detect_encoding(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if raw.startswith(b"\xfe\xff"):
        return "utf-16-be"
    if not raw:
        return "utf-8"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        matches = list(from_bytes(raw[:_DETECTION_SAMPLE_BYTES]))
        if matches:
            best = matches[0]
            if best.chaos == 0 and all(match.chaos > 0 for match in matches[1:]):
                raw.decode(best.encoding)
                return best.encoding
    except Exception:
        pass
    for encoding in _LEGACY_ENCODING_FALLBACKS:
        try:
            decoded = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if _decoded_looks_like(decoded, encoding):
            return encoding
    return "utf-8"


def _strip_bom(decoded: str, encoding: str) -> str:
    if encoding in ("utf-16-le", "utf-16-be", "utf-16") and decoded.startswith("\ufeff"):
        return decoded[1:]
    return decoded


def _decode_bytes(raw: bytes, encoding: str) -> tuple[str, str]:
    try:
        return _strip_bom(raw.decode(encoding), encoding), encoding
    except (UnicodeDecodeError, LookupError):
        detected = _detect_encoding(raw)
        try:
            return _strip_bom(raw.decode(detected), detected), detected
        except (UnicodeDecodeError, LookupError):
            return raw.decode("utf-8", errors="replace"), f"{detected}(replace)"


def read_text(
    path: str | Path, encoding_override: str | None = None
) -> tuple[str, str]:
    raw = Path(path).read_bytes()
    if encoding_override:
        text, used = _decode_bytes(raw, encoding_override)
        if not used.endswith("(replace)") and used != encoding_override:
            return text, f"{encoding_override}(fallback)"
        return text, used
    detected = _detect_encoding(raw)
    try:
        return _strip_bom(raw.decode(detected), detected), detected
    except (UnicodeDecodeError, LookupError):
        return raw.decode("utf-8", errors="replace"), f"{detected}(replace)"


def render_line_range(
    text: str,
    start_line: int | None,
    end_line: int | None,
    header: str,
) -> str:
    lines = text.splitlines(keepends=True)
    total = len(lines)
    start = (start_line - 1) if start_line and start_line >= 1 else 0
    end = end_line if end_line and end_line <= total else total
    body = "".join(f"{index + 1:4d} | {lines[index]}" for index in range(start, end))
    if start_line or end_line:
        return f"[显示第 {start + 1}-{end} 行，共 {total} 行 | {header}]\n{body}"
    return f"[共 {total} 行 | {header}]\n{body}"


_read_text_with_encoding_detection = read_text
