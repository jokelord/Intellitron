"""Tests for backend.tools.file_ops.

Focus: read_file must robustly handle files in arbitrary text encodings
(UTF-8, UTF-8 BOM, UTF-16, GBK/GB18030, Big5, Shift_JIS, EUC-KR, Latin-1, ...).
The local LLM should not need to know the encoding; the tool must auto-detect
or accept an explicit override.
"""

from __future__ import annotations

import pytest

from backend.tools import file_ops


# ----------------------------- helpers --------------------------------------


def _write_bytes(path, raw: bytes) -> None:
    path.write_bytes(raw)


# A short Chinese paragraph that round-trips cleanly through several encodings.
SAMPLE_CN = "第一行：你好，世界。\n第二行：中文测试 abc 123。\n第三行：再见。\n"


# ----------------------------- backwards-compat -----------------------------


def test_read_file_utf8_still_works(tmp_path):
    """The original UTF-8 path must keep working unchanged (only header changes)."""
    p = tmp_path / "utf8.txt"
    p.write_text("hello\nworld\n", encoding="utf-8")

    out = file_ops.read_file(str(p), start_line=1, end_line=2)

    assert "hello" in out
    assert "world" in out
    assert "1 |" in out
    assert "2 |" in out
    # New behavior: encoding surfaced in header.
    assert "utf-8" in out


def test_read_file_default_range_uses_full_file(tmp_path):
    p = tmp_path / "all.txt"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    out = file_ops.read_file(str(p))
    assert "a" in out and "b" in out and "c" in out


def test_read_file_start_line_offset_is_one_based(tmp_path):
    p = tmp_path / "lines.txt"
    p.write_text("L1\nL2\nL3\nL4\n", encoding="utf-8")
    out = file_ops.read_file(str(p), start_line=2, end_line=3)
    assert "L2" in out
    assert "L3" in out
    assert "L1" not in out.split("\n", 1)[1]  # not in body (header ok)
    assert "L4" not in out


def test_read_file_empty_file(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_bytes(b"")
    out = file_ops.read_file(str(p))
    # Should not crash. Just return a header noting 0 lines.
    assert "0" in out  # total lines = 0
    assert "utf-8" in out  # default encoding for empty content


# ----------------------------- BOM & UTF variants --------------------------


def test_read_file_utf8_bom_is_decoded(tmp_path):
    p = tmp_path / "bom.txt"
    _write_bytes(p, b"\xef\xbb\xbf" + "第一行\n第二行\n".encode("utf-8"))

    out = file_ops.read_file(str(p))

    # BOM character must NOT be visible to LLM (no leading U+FEFF / ZWNBSP).
    assert "第一行" in out
    assert "第二行" in out
    # The tool must have used the utf-8-sig decoder.
    assert "utf-8-sig" in out


def test_read_file_utf16_le_with_bom(tmp_path):
    p = tmp_path / "u16le.txt"
    text = "中文 hello\n第二行\n"
    _write_bytes(p, b"\xff\xfe" + text.encode("utf-16-le"))

    out = file_ops.read_file(str(p))

    assert "中文" in out
    assert "第二行" in out
    assert "utf-16" in out  # utf-16-le


def test_read_file_utf16_be_with_bom(tmp_path):
    p = tmp_path / "u16be.txt"
    text = "中文 hello\n第二行\n"
    _write_bytes(p, b"\xfe\xff" + text.encode("utf-16-be"))

    out = file_ops.read_file(str(p))

    assert "中文" in out
    assert "第二行" in out
    assert "utf-16" in out  # utf-16-be


def test_read_file_utf16_bom_is_stripped_from_text(tmp_path):
    """Python's utf-16-le / utf-16-be codecs do NOT strip the BOM.

    The tool must strip it manually so the LLM never sees a stray
    U+FEFF at the start of a line (it would look like an empty
    line and waste tokens).
    """
    for label, marker, enc in [
        ("u16le", b"\xff\xfe", "utf-16-le"),
        ("u16be", b"\xfe\xff", "utf-16-be"),
    ]:
        p = tmp_path / f"{label}.txt"
        _write_bytes(p, marker + "第一行：你好\n第二行：世界\n".encode(enc))

        out = file_ops.read_file(str(p))

        # The decoded text must not contain the BOM character anywhere.
        assert "\ufeff" not in out, f"{label}: BOM leaked into output: {out!r}"
        # Sanity: real content is still there.
        assert "你好" in out
        assert "世界" in out


# ----------------------------- legacy CJK encodings -------------------------


def test_read_file_gbk_auto_detected(tmp_path):
    p = tmp_path / "gbk.txt"
    _write_bytes(p, SAMPLE_CN.encode("gbk"))

    out = file_ops.read_file(str(p))

    # LLM-visible text must be the natural-language Chinese, not mojibake.
    assert "你好，世界" in out
    assert "中文测试" in out
    assert "再见" in out
    # Encoding must be reported in the header (gb18030 is a superset of GBK
    # and the standard fallback for Chinese).
    assert "gb" in out.lower()


def test_read_file_gb18030_auto_detected(tmp_path):
    p = tmp_path / "gb18030.txt"
    # GB18030 includes characters that pure GBK cannot represent, e.g. some
    # minority scripts. Use a payload that decodes the same in both to keep
    # the test simple and stable.
    text = "简体中文测试 ABC 123\n"
    _write_bytes(p, text.encode("gb18030"))

    out = file_ops.read_file(str(p))

    # The real invariant: the LLM gets back the original natural-language
    # text. The exact encoding name in the header may be "gb18030" (curated
    # fallback path) or some other name from charset_normalizer that
    # happened to decode strictly. Both are acceptable as long as the text
    # round-trips.
    assert "简体中文测试" in out


def test_read_file_big5_auto_detected(tmp_path):
    p = tmp_path / "big5.txt"
    # Big5 is Traditional Chinese only — every character must exist in
    # Big5's repertoire or encoding will fail. Use only Traditional glyphs.
    text = "繁體中文測試 hello\n第二行資料\n"
    _write_bytes(p, text.encode("big5"))

    out = file_ops.read_file(str(p))

    # Text round-trip is the real invariant; encoding name is reported.
    assert "繁體中文測試" in out
    assert "第二行資料" in out
    assert "big5" in out.lower()


def test_read_file_shift_jis_auto_detected(tmp_path):
    p = tmp_path / "sjis.txt"
    text = "日本語のテストです\n二行目です\n"
    _write_bytes(p, text.encode("shift_jis"))

    out = file_ops.read_file(str(p))

    # Real invariant: text round-trips. The encoding name may be reported
    # as ``shift_jis`` (curated fallback), or ``cp932`` (charset_normalizer's
    # preferred Windows alias for Shift_JIS-family), or any other alias
    # that decodes these bytes strictly. We just check the decoded text.
    assert "日本語のテスト" in out
    assert "二行目" in out


# ----------------------------- explicit override ----------------------------


def test_read_file_explicit_encoding_override(tmp_path):
    p = tmp_path / "override.txt"
    _write_bytes(p, SAMPLE_CN.encode("gbk"))

    out = file_ops.read_file(str(p), encoding="gbk")

    assert "你好，世界" in out
    assert "中文测试" in out
    # Override wins over auto-detection.
    assert "gbk" in out


def test_read_file_explicit_override_invalid_name_does_not_crash(tmp_path):
    """Unknown encoding names must fall back to detection, not raise."""
    p = tmp_path / "override_bad.txt"
    p.write_text("hello world\n", encoding="utf-8")

    # Should not raise LookupError; should still return readable text.
    out = file_ops.read_file(str(p), encoding="not-a-real-encoding-xyz")
    assert "hello world" in out


def test_read_file_explicit_override_wrong_encoding_falls_back(tmp_path):
    """If the requested encoding cannot strictly decode the bytes, fall back to detection.

    Note: we use ``ascii`` here (not ``latin-1``) because Latin-1 is a
    1-to-1 byte mapping that silently decodes any byte sequence — it
    would never trigger the fallback path. ASCII on the other hand is
    strict: it raises ``UnicodeDecodeError`` on any byte > 0x7F, which
    is exactly what the fallback path is designed to handle.
    """
    p = tmp_path / "override_wrong.txt"
    _write_bytes(p, SAMPLE_CN.encode("gbk"))

    out = file_ops.read_file(str(p), encoding="ascii")

    # Auto-detect should kick in and still produce the correct Chinese.
    assert "你好，世界" in out


# ----------------------------- line range with non-utf8 --------------------


def test_read_file_line_range_works_with_gbk(tmp_path):
    p = tmp_path / "gbk_lines.txt"
    lines = [f"第{i}行：内容{i}\n" for i in range(1, 11)]
    raw = "".join(lines).encode("gbk")
    _write_bytes(p, raw)

    out = file_ops.read_file(str(p), start_line=3, end_line=5)

    assert "第3行" in out
    assert "第4行" in out
    assert "第5行" in out
    assert "第1行" not in out
    assert "第10行" not in out
    assert "10" in out  # total line count in header


# ----------------------------- error surface --------------------------------


def test_read_file_missing_file_returns_error_string(tmp_path):
    """A missing file should produce a readable error string, not a crash."""
    missing = tmp_path / "does_not_exist.txt"
    out = file_ops.read_file(str(missing))
    # Don't assert exact wording (locale/platform differences), but the
    # tool must return a non-empty string the LLM can act on.
    assert isinstance(out, str)
    assert out  # non-empty
    assert "does_not_exist" in out or "not found" in out.lower() or "errno" in out.lower()
