#!/usr/bin/env python3
"""
load_local_images.py — 加载本地图片文件，输出 JSON 供 Agent 框架注入到 VLM 对话。

用法:
    python load_local_images.py --session-id <id> --files "<逗号分隔路径>" [--max-dim 1280]

输出:
    1. JSON 文件写入 /tmp/agent_images/<session_id>/<uuid>.json
    2. stdout 打印人类可读摘要
"""

import argparse
import base64
import io
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image as PILImage

# Import config and the shared filesystem policy from the repository root.
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from backend.security.capabilities import (
    CapabilityError,
    OperationCapability,
    build_session_capability,
    require_capability,
)
from backend.security.path_policy import PathPolicy, PathPolicyError, ResolvedPath

# ---------------------------------------------------------------------------
# Config resolution without heavy dependencies.
#
# The Agent executes this skill with its dedicated interpreter
# (`~/.agent/bin/python`, see `python_executable` in config-agent-operation.yaml),
# which provides only stdlib + PIL: neither `yaml` nor `pydantic` is available,
# so `backend.config` cannot be imported there.  `backend.security.*` is
# stdlib-only and works in every interpreter.
#
# Resolution order (first success wins):
#   1. canonical: `backend.config.get_config()` + `build_session_capability`
#      (full-dependency environments: dev, CI, backend host);
#   2. dependency-free fallback below: read the canonical YAML files with a
#      strict subset parser and construct an equivalent `OperationCapability`.
#      Only filesystem-authority inputs are sourced this way (agent/read_file
#      roots, session base dir, byte limits, image dimension); every value is
#      validated by the real `OperationCapability`/`PathPolicy` constructors,
#      and command authority is intentionally left inert — this skill never
#      executes commands, it only loads image files.
#
# The fallback deliberately FAILS CLOSED: anything outside the strict subset
# (anchors, tags, explicit keys, doc markers, ambiguous indentation, duplicate
# keys) raises instead of guessing, so a future config edit can break loudly
# but never silently widen authority.  This skill must NOT depend on the
# repository `.sglang` venv: the Agent runtime is pinned to `~/.agent`.
# ---------------------------------------------------------------------------
# NOTE: `get_config` must stay bound even when the import fails; otherwise
# `_default_image_capability()` raises NameError instead of an actionable error.
get_config = None  # type: ignore[assignment]
_CONFIG_IMPORT_ERROR: Exception | None = None
try:
    from backend.config import get_config  # type: ignore[no-redef]
except ImportError as exc:
    _CONFIG_IMPORT_ERROR = exc


class _ConfigFallbackError(ValueError):
    """Internal: dependency-free config read failed (converted to CapabilityError)."""


_YAML_NULL_WORDS = {"null", "Null", "NULL", "~", ""}
_YAML_TRUE_WORDS = {"y", "Y", "yes", "Yes", "YES", "true", "True", "TRUE", "on", "On", "ON"}
_YAML_FALSE_WORDS = {"n", "N", "no", "No", "NO", "false", "False", "FALSE", "off", "Off", "OFF"}


def _yaml_comment_start(line: str) -> int | None:
    """Return the index where a `#` comment starts (quote-aware), if any."""
    in_single = in_double = False
    i = 0
    while i < len(line):
        ch = line[i]
        if in_single:
            if ch == "'":
                if i + 1 < len(line) and line[i + 1] == "'":
                    i += 2
                    continue
                in_single = False
        elif in_double:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "#" and (i == 0 or line[i - 1] in (" ", "\t")):
            return i
        i += 1
    return None


def _flow_depth(text: str) -> int:
    """Bracket depth of `[`/`{` vs `]`/`}` outside quotes (escapes honored)."""
    depth = 0
    in_single = in_double = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_single:
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 1
                else:
                    in_single = False
        elif in_double:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        i += 1
    return depth


def _quotes_balanced(text: str) -> bool:
    """Whether single/double quotes are all closed (escapes/`''` honored)."""
    in_single = in_double = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_single:
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 1
                else:
                    in_single = False
        elif in_double:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        i += 1
    return not (in_single or in_double)


def _find_map_separator(line: str) -> int | None:
    """Index of the `:` ending a mapping key (quote/flow-depth aware)."""
    in_single = in_double = False
    depth = 0
    i = 0
    while i < len(line):
        ch = line[i]
        if in_single:
            if ch == "'":
                if i + 1 < len(line) and line[i + 1] == "'":
                    i += 1
                else:
                    in_single = False
        elif in_double:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_double = False
        elif ch in "'\"":
            in_single = ch == "'"
            in_double = ch == '"'
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            nxt = line[i + 1] if i + 1 < len(line) else ""
            if nxt == "" or nxt in (" ", "\t"):
                return i
        i += 1
    return None


def _unescape_double(inner: str, source: str, lineno: int) -> str:
    out: list[str] = []
    i = 0
    simple = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "0": "\0", "a": "\a", "v": "\v", "e": "\x1b", " ": " ", "_": "\xa0", "\n": ""}
    while i < len(inner):
        ch = inner[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(inner):
            raise _ConfigFallbackError(f"{source}:{lineno}: dangling backslash")
        esc = inner[i]
        if esc in simple:
            out.append(simple[esc])
            i += 1
        elif esc == "x":
            hexd = inner[i + 1 : i + 3]
            if len(hexd) != 2 or any(c not in "0123456789abcdefABCDEF" for c in hexd):
                raise _ConfigFallbackError(f"{source}:{lineno}: bad \\x escape")
            out.append(chr(int(hexd, 16)))
            i += 3
        elif esc in ("u", "U"):
            width = 4 if esc == "u" else 8
            hexd = inner[i + 1 : i + 1 + width]
            if len(hexd) != width or any(c not in "0123456789abcdefABCDEF" for c in hexd):
                raise _ConfigFallbackError(f"{source}:{lineno}: bad unicode escape")
            out.append(chr(int(hexd, 16)))
            i += 1 + width
        else:
            raise _ConfigFallbackError(f"{source}:{lineno}: unknown escape '\\{esc}'")
    return "".join(out)


def _parse_yaml_scalar(text: str, source: str, lineno: int):
    """Resolve one scalar with PyYAML-1.1-compatible null/bool/int/float rules."""
    s = text.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        inner = s[1:-1]
        if s[0] == "'":
            return inner.replace("''", "'")
        return _unescape_double(inner, source, lineno)
    if s in _YAML_NULL_WORDS:
        return None
    if s in _YAML_TRUE_WORDS:
        return True
    if s in _YAML_FALSE_WORDS:
        return False
    plain = s.replace("_", "")
    try:
        if plain[:1] in ("+", "-"):
            sign, digits = plain[0], plain[1:]
        else:
            sign, digits = "", plain
        if digits.startswith(("0x", "0X")):
            return int(sign + digits[2:], 16)
        if digits.startswith(("0o", "0O")):
            return int(sign + digits[2:], 8)
        if digits.startswith("0") and len(digits) > 1 and digits[1:2].isdigit():
            raise ValueError("leading zero")
        return int(sign + digits, 10)
    except ValueError:
        pass
    lowered = s.lower().replace("_", "")
    if lowered in (".inf", "+.inf", "-.inf", ".nan"):
        try:
            return float(lowered)
        except ValueError:
            pass
    else:
        try:
            return float(s.replace("_", ""))
        except ValueError:
            pass
    return s


def _split_flow_items(inner: str, source: str, lineno: int) -> list[str]:
    """Split one flow level on top-level commas (nesting/quotes honored)."""
    items: list[str] = []
    depth = 0
    in_single = in_double = False
    current: list[str] = []
    i = 0
    while i < len(inner):
        ch = inner[i]
        if in_single:
            current.append(ch)
            if ch == "'":
                if i + 1 < len(inner) and inner[i + 1] == "'":
                    current.append("'")
                    i += 1
                else:
                    in_single = False
        elif in_double:
            current.append(ch)
            if ch == "\\" and i + 1 < len(inner):
                current.append(inner[i + 1])
                i += 1
            elif ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
            current.append(ch)
        elif ch == '"':
            in_double = True
            current.append(ch)
        elif ch in "[{":
            depth += 1
            current.append(ch)
        elif ch in "]}":
            depth -= 1
            if depth < 0:
                raise _ConfigFallbackError(f"{source}:{lineno}: unbalanced flow collection")
            current.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    if in_single or in_double:
        raise _ConfigFallbackError(f"{source}:{lineno}: unterminated quote in flow collection")
    items.append("".join(current))
    return items


def _parse_flow_value(text: str, source: str, lineno: int):
    """Parse one `[...]`/`{...}` value (already bracket-balanced)."""
    s = text.strip()
    if not s or s[0] not in "[{":
        return _parse_yaml_scalar(s, source, lineno)
    closer = "]" if s[0] == "[" else "}"
    if not s.endswith(closer):
        raise _ConfigFallbackError(f"{source}:{lineno}: unbalanced flow collection")
    parts = _split_flow_items(s[1:-1], source, lineno)
    if s[0] == "[":
        return [_parse_flow_value(p, source, lineno) for p in parts if p.strip() != ""]
    result: dict = {}
    for part in parts:
        if part.strip() == "":
            continue
        sep = _find_map_separator(part)
        if sep is None:
            raise _ConfigFallbackError(f"{source}:{lineno}: bad flow mapping entry")
        key = _parse_flow_value(part[:sep], source, lineno)
        if not isinstance(key, str):
            raise _ConfigFallbackError(f"{source}:{lineno}: flow mapping key must be a string")
        result[key] = _parse_flow_value(part[sep + 1 :], source, lineno)
    return result


def _looks_like_block_start(text: str) -> bool:
    if text == "-" or text.startswith("- "):
        return True
    return _find_map_separator(text) is not None


def _collect_multiline_text(lines, i: int, first: str, min_indent: int, source: str) -> tuple[str, int]:
    """Join `first` with following deeper-indented lines until brackets/quotes close."""
    buf = [first]
    j = i + 1
    while _flow_depth(" ".join(buf)) > 0 or not _quotes_balanced(" ".join(buf)):
        if j >= len(lines) or lines[j][1] <= min_indent:
            raise _ConfigFallbackError(
                f"{source}:{lines[i][0]}: unbalanced flow collection or quote"
            )
        buf.append(lines[j][2])
        j += 1
    if _flow_depth(" ".join(buf)) < 0:
        raise _ConfigFallbackError(f"{source}:{lines[i][0]}: unbalanced flow collection")
    return " ".join(buf), j


def _parse_block(lines, pos: int, indent: int, source: str):
    if pos >= len(lines):
        raise _ConfigFallbackError(f"{source}: unexpected end of document")
    first = lines[pos][2]
    if first == "-" or first.startswith("- "):
        return _parse_seq(lines, pos, indent, source)
    return _parse_map(lines, pos, indent, source)


def _parse_map(lines, pos: int, indent: int, source: str):
    result: dict = {}
    i = pos
    n = len(lines)
    while i < n:
        lineno, ind, text = lines[i]
        if ind < indent:
            break
        if ind > indent:
            raise _ConfigFallbackError(f"{source}:{lineno}: unexpected indentation")
        if text == "-" or text.startswith("- "):
            raise _ConfigFallbackError(f"{source}:{lineno}: sequence entry inside mapping")
        if text.startswith("? "):
            raise _ConfigFallbackError(f"{source}:{lineno}: explicit keys are not supported")
        sep = _find_map_separator(text)
        if sep is None:
            raise _ConfigFallbackError(f"{source}:{lineno}: expected 'key: value'")
        key = _parse_yaml_scalar(text[:sep].rstrip(" "), source, lineno)
        if not isinstance(key, str) or key == "":
            raise _ConfigFallbackError(f"{source}:{lineno}: mapping key must be a string")
        if key == "<<":
            raise _ConfigFallbackError(f"{source}:{lineno}: merge keys are not supported")
        if key in result:
            raise _ConfigFallbackError(f"{source}:{lineno}: duplicate key '{key}'")
        rest = text[sep + 1 :]
        rest = rest[1:] if rest.startswith(" ") else rest
        if rest.startswith("\t"):
            raise _ConfigFallbackError(f"{source}:{lineno}: tab after ':'")
        if rest == "":
            if i + 1 < n and lines[i + 1][1] > indent:
                nxt = lines[i + 1][2]
                if nxt[:1] in "[{" or not _quotes_balanced(nxt):
                    # Flow/quote value starting on the next line (e.g. a
                    # multiline `[...]` list); join before interpreting.
                    full, i = _collect_multiline_text(lines, i + 1, nxt, indent, source)
                    result[key] = (
                        _parse_flow_value(full, source, lineno)
                        if full.lstrip()[:1] in "[{"
                        else _parse_yaml_scalar(full, source, lineno)
                    )
                    continue
                value, i = _parse_block(lines, i + 1, lines[i + 1][1], source)
            else:
                value, i = None, i + 1
            result[key] = value
            continue
        if rest[0] in "*&!":
            raise _ConfigFallbackError(
                f"{source}:{lineno}: anchors/aliases/tags are not supported"
            )
        if rest[0] in "[{" or not _quotes_balanced(rest):
            full, i = _collect_multiline_text(lines, i, rest, indent, source)
            result[key] = _parse_flow_value(full, source, lineno) if full.lstrip()[:1] in "[{" else _parse_yaml_scalar(full, source, lineno)
            continue
        parts = [rest]
        j = i + 1
        while j < n and lines[j][1] > indent and not _looks_like_block_start(lines[j][2]):
            parts.append(lines[j][2])
            j += 1
        result[key] = _parse_yaml_scalar(" ".join(parts), source, lineno)
        i = j
    return result, i


def _parse_seq(lines, pos: int, indent: int, source: str):
    items: list = []
    i = pos
    n = len(lines)
    while i < n:
        lineno, ind, text = lines[i]
        if ind < indent:
            break
        if ind > indent:
            raise _ConfigFallbackError(f"{source}:{lineno}: unexpected indentation")
        if not (text == "-" or text.startswith("- ")):
            break
        rest = "" if text == "-" else text[2:]
        if rest.startswith("\t"):
            raise _ConfigFallbackError(f"{source}:{lineno}: tab after '-'")
        if rest == "":
            if i + 1 < n and lines[i + 1][1] > indent:
                nxt = lines[i + 1][2]
                if nxt[:1] in "[{" or not _quotes_balanced(nxt):
                    full, i = _collect_multiline_text(lines, i + 1, nxt, indent, source)
                    items.append(
                        _parse_flow_value(full, source, lineno)
                        if full.lstrip()[:1] in "[{"
                        else _parse_yaml_scalar(full, source, lineno)
                    )
                    continue
                value, i = _parse_block(lines, i + 1, lines[i + 1][1], source)
            else:
                value, i = None, i + 1
            items.append(value)
            continue
        if rest[0] in "*&!":
            raise _ConfigFallbackError(
                f"{source}:{lineno}: anchors/aliases/tags are not supported"
            )
        if rest[0] in "[{" or not _quotes_balanced(rest):
            full, i = _collect_multiline_text(lines, i, rest, indent, source)
            items.append(
                _parse_flow_value(full, source, lineno) if full.lstrip()[:1] in "[{" else _parse_yaml_scalar(full, source, lineno)
            )
            continue
        if _find_map_separator(rest) is not None:
            sub = [(lineno, indent + 2, rest)]
            j = i + 1
            while j < n and lines[j][1] > indent:
                sub.append(lines[j])
                j += 1
            value, consumed = _parse_block(sub, 0, indent + 2, source)
            if consumed != len(sub):
                raise _ConfigFallbackError(f"{source}:{lineno}: bad mapping in sequence")
            items.append(value)
            i = j
            continue
        parts = [rest]
        j = i + 1
        while j < n and lines[j][1] > indent and not _looks_like_block_start(lines[j][2]):
            parts.append(lines[j][2])
            j += 1
        items.append(_parse_yaml_scalar(" ".join(parts), source, lineno))
        i = j
    return items, i


def _parse_subset_yaml(text: str, *, source: str) -> dict:
    """Parse canonical config YAML restricted to a strict, fail-closed subset."""
    cleaned: list[tuple[int, int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        cut = _yaml_comment_start(raw)
        code = raw if cut is None else raw[:cut]
        if "\t" in code:
            # Tabs are rejected conservatively (even inside quoted scalars):
            # canonical files never use them; fail closed instead of guessing.
            raise _ConfigFallbackError(f"{source}:{lineno}: tabs are not supported")
        if code.strip() == "":
            continue
        stripped = code.strip()
        if stripped in ("---", "...") or stripped.startswith("--- ") or stripped.startswith("%"):
            raise _ConfigFallbackError(f"{source}:{lineno}: directives/documents are not supported")
        cleaned.append((lineno, len(code) - len(code.lstrip(" ")), stripped))
    if not cleaned:
        return {}
    value, consumed = _parse_block(cleaned, 0, cleaned[0][1], source)
    if consumed != len(cleaned):
        lineno = cleaned[consumed][0]
        raise _ConfigFallbackError(f"{source}:{lineno}: unexpected content")
    if not isinstance(value, dict):
        raise _ConfigFallbackError(f"{source}: top-level mapping expected")
    return value


_OVERALL_SUBFILE_ORDER = ("model", "infer", "server", "misc", "agent_operation", "webUI", "vision")


def _load_merged_config_sections() -> dict:
    """Reproduce `ConfigLoader` routing+merge for the sections this skill needs."""
    overall_path = REPOSITORY_ROOT / "config" / "config-overall.yaml"
    try:
        overall_text = overall_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _ConfigFallbackError(f"overall config not readable: {overall_path}") from exc
    overall = _parse_subset_yaml(overall_text, source=str(overall_path))
    merged: dict = {}
    infer_sub_path: Path | None = None
    for key in _OVERALL_SUBFILE_ORDER:
        rel = overall.get(key)
        if not rel:
            continue
        if not isinstance(rel, str):
            raise _ConfigFallbackError(f"overall key '{key}' must be a path string")
        sub_path = REPOSITORY_ROOT / rel
        if key == "infer":
            infer_sub_path = sub_path
        if not sub_path.is_file():
            continue
        try:
            sub_data = _parse_subset_yaml(sub_path.read_text(encoding="utf-8"), source=str(sub_path))
        except OSError as exc:
            raise _ConfigFallbackError(f"config not readable: {sub_path}") from exc
        if isinstance(sub_data.get("model"), dict) and isinstance(merged.get("model"), dict):
            merged_model = dict(merged["model"])
            merged_model.update(sub_data["model"] or {})
            merged_rest = {k: v for k, v in sub_data.items() if k != "model"}
            merged["model"] = merged_model
            merged.update(merged_rest)
        else:
            merged.update(sub_data)
    # Legacy sibling fallback: old infer files without an embedded model section
    # are completed from `*-config-model.yaml` (mirrors `ConfigLoader.load`).
    model_section = merged.get("model")
    needs_sibling = not isinstance(model_section, dict) or not model_section.get("model_path")
    if needs_sibling and infer_sub_path is not None:
        name = infer_sub_path.name
        if "-config-infer.yaml" in name:
            sibling = infer_sub_path.parent / name.replace("-config-infer.yaml", "-config-model.yaml")
            if sibling.is_file():
                try:
                    sibling_data = _parse_subset_yaml(sibling.read_text(encoding="utf-8"), source=str(sibling))
                except OSError as exc:
                    raise _ConfigFallbackError(f"config not readable: {sibling}") from exc
                sibling_model = (sibling_data or {}).get("model")
                if isinstance(sibling_model, dict) and sibling_model:
                    if not isinstance(merged.get("model"), dict):
                        merged["model"] = {}
                    for k, v in sibling_model.items():
                        if k not in merged["model"] or merged["model"][k] is None:
                            merged["model"][k] = v
    return merged


def _fallback_str_list(section: dict, key: str, default: list[str]) -> list[str]:
    """Mirror `ConfigLoader._get_str_list` (missing/null → default)."""
    value = section.get(key, None) if isinstance(section, dict) else None
    if value is None:
        return list(default)
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = str(value).split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def _fallback_coerce_int(value, default: int) -> int:
    """Mirror `ConfigLoader._coerce_int` (bool coerces via int(), like canonical)."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _fallback_max_dim(merged: dict) -> int:
    """Mirror the `vision_upload → model → 1280` max_image_dimension chain."""
    vision_upload = merged.get("vision_upload")
    if isinstance(vision_upload, dict):
        raw = vision_upload.get("max_image_dimension")
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
    model = merged.get("model")
    if isinstance(model, dict):
        raw = model.get("max_image_dimension")
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
    return 1280


def _fallback_image_capability(session_id: str) -> OperationCapability:
    """Build the same filesystem authority as canonical parsing, without deps.

    Only the inputs consumed by this skill's `PathPolicy` are sourced from the
    YAML files (`agent`/`read_file` roots, session base dir, byte limits);
    null/default semantics mirror `ConfigLoader` + `build_session_capability`
    exactly.  Command fields are inert dummies — this skill never executes
    commands — while root validation is fully delegated to the real
    `OperationCapability`/`PathPolicy` constructors.
    """
    try:
        merged = _load_merged_config_sections()
    except _ConfigFallbackError as exc:
        raise CapabilityError("capability_config_missing", f"fallback config read failed: {exc}") from exc
    project_root = str(REPOSITORY_ROOT)
    agent = merged.get("agent")
    if agent is None:
        agent = {}
    if not isinstance(agent, dict):
        raise CapabilityError("invalid_capability_config", "agent section must be a mapping")
    read_file = merged.get("read_file")
    if read_file is None:
        read_file = {}
    if not isinstance(read_file, dict):
        raise CapabilityError("invalid_capability_config", "read_file section must be a mapping")

    read_roots = _fallback_str_list(
        read_file, "allowed_roots", [project_root, "~", "/tmp", "/var/tmp"]
    )
    search_roots = _fallback_str_list(
        agent, "search_whitelist_paths", [project_root, "/tmp"]
    )
    raw_file_roots = agent.get("file_operation_roots")
    if raw_file_roots is not None and not isinstance(raw_file_roots, (list, tuple)):
        raise CapabilityError("invalid_capability_config", "agent.file_operation_roots must be a list or null")
    file_roots = read_roots if raw_file_roots is None else [str(p).strip() for p in raw_file_roots if str(p).strip()]
    raw_image_roots = agent.get("image_input_roots")
    if raw_image_roots is not None and not isinstance(raw_image_roots, (list, tuple)):
        raise CapabilityError("invalid_capability_config", "agent.image_input_roots must be a list or null")
    image_roots = read_roots if raw_image_roots is None else [str(p).strip() for p in raw_image_roots if str(p).strip()]

    session_base = agent.get("session_temp_base_dir", "/tmp/agent_images")
    if (
        not isinstance(session_base, str)
        or not session_base.strip()
        or "\x00" in session_base
        or not os.path.isabs(os.path.expanduser(session_base))
    ):
        raise CapabilityError("invalid_capability_config", "agent.session_temp_base_dir must be an absolute path")

    raw_max_file_bytes = agent.get("max_file_bytes")
    if raw_max_file_bytes is None:
        max_file_bytes = _fallback_coerce_int(read_file.get("max_source_bytes"), 536_870_912)
    else:
        if isinstance(raw_max_file_bytes, bool) or not isinstance(raw_max_file_bytes, int) or raw_max_file_bytes < 0:
            raise CapabilityError(
                "invalid_capability_config",
                "agent.max_file_bytes must be a non-negative integer or null",
            )
        max_file_bytes = raw_max_file_bytes

    allow_overwrite = agent.get("allow_overwrite_existing", True)
    if not isinstance(allow_overwrite, bool):
        raise CapabilityError(
            "invalid_capability_config", "agent.allow_overwrite_existing must be a boolean"
        )

    try:
        return OperationCapability(
            session_id=session_id,
            read_roots=tuple(read_roots),
            search_roots=tuple(search_roots),
            file_operation_roots=tuple(file_roots),
            image_input_roots=tuple(image_roots),
            session_temp_base_dir=session_base,
            command_mode="argv",
            allowed_executables=("python3",),
            max_exec_timeout_seconds=3600.0,
            max_output_bytes=4194304,
            max_file_bytes=max_file_bytes,
            allow_overwrite_existing=allow_overwrite,
            allow_browser=False,
            exec_timeout=60.0,
            termination_grace_seconds=2.0,
            work_dir=os.path.expanduser(session_base),
            python_executable=os.path.abspath(sys.executable),
            nvm_dir=os.path.expanduser("~/.nvm"),
            playwright_profile_dir=None,
            environment_allowlist=(),
            command_path_entries=(),
            command_cwd_allowlist=(),
            command_blacklist=(),
        )
    except CapabilityError:
        raise
    except (TypeError, ValueError) as exc:
        raise CapabilityError(
            "invalid_capability_config", "operation capability configuration is invalid"
        ) from exc


if get_config is not None:
    try:
        _config = get_config()
        DEFAULT_MAX_DIM = _config.model.max_image_dimension or 1280
    except Exception:
        # Config exists but is unreadable here; capability building will
        # surface the real error instead of silently continuing.
        DEFAULT_MAX_DIM = 1280
else:
    try:
        DEFAULT_MAX_DIM = _fallback_max_dim(_load_merged_config_sections())
    except Exception:
        DEFAULT_MAX_DIM = 1280

SUPPORTED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB
MAX_IMAGE_COUNT = 9
SESSION_IMAGE_BASE_DIR = "/tmp/agent_images"


@dataclass(frozen=True)
class ImageLoadResult:
    summary: str
    exit_code: int


def _default_image_capability(session_id: str) -> OperationCapability:
    """Resolve CLI authority from parsed config without reading prompt text."""
    if get_config is not None:
        try:
            return build_session_capability(
                get_config(),
                session_id=session_id,
                mode="tool_calling",
            )
        except CapabilityError:
            raise
        except Exception as exc:
            raise CapabilityError(
                "capability_config_missing", f"application configuration is invalid: {exc}"
            ) from exc
    return _fallback_image_capability(session_id)


def _process_image(policy: PathPolicy, target: ResolvedPath, max_dimension: int) -> tuple:
    """处理单张图片: 打开 → 缩放 → 色彩转换 → base64 JPEG 编码。

    Returns:
        (base64_str, mime, size_info, mode) on success
        Raises Exception on failure
    """
    descriptor = policy.open_existing_file(target)
    with os.fdopen(descriptor, "rb", closefd=True) as source:
        # The pre-open size check is necessarily subject to a file replacement
        # race.  Bound the actual read as well so a changed target cannot turn
        # image loading into an unbounded memory allocation.
        raw_bytes = source.read(MAX_FILE_SIZE + 1)
    if len(raw_bytes) > MAX_FILE_SIZE:
        raise PathPolicyError(
            "file_too_large", "image exceeds the configured byte limit"
        )
    pil_img = PILImage.open(io.BytesIO(raw_bytes))
    pil_img.load()
    original_fmt = (pil_img.format or "").lower()
    w, h = pil_img.size
    original_w, original_h = w, h

    # 缩放
    resized = False
    if max_dimension > 0 and (w > max_dimension or h > max_dimension):
        if w > h:
            nw, nh = max_dimension, int(h * max_dimension / w)
        else:
            nh, nw = max_dimension, int(w * max_dimension / h)
        pil_img = pil_img.resize((nw, nh), PILImage.Resampling.LANCZOS)
        resized = True
        w, h = nw, nh

    # 透明通道转换
    color_changed = False
    if pil_img.mode == "RGBA":
        pil_img.load()
        bg = PILImage.new("RGB", pil_img.size, (255, 255, 255))
        bg.paste(pil_img, mask=pil_img.split()[3])
        pil_img = bg
        color_changed = True
    elif pil_img.mode in ("P", "PA", "LA"):
        pil_img.load()
        rgba_tmp = pil_img.convert("RGBA")
        bg = PILImage.new("RGB", rgba_tmp.size, (255, 255, 255))
        bg.paste(rgba_tmp, mask=rgba_tmp.split()[3])
        pil_img = bg
        color_changed = True
    elif pil_img.mode not in ("RGB", "L"):
        pil_img = pil_img.convert("RGB")
        color_changed = True

    # base64 编码
    if not resized and not color_changed and original_fmt == "jpeg":
        # 无变换的 JPEG：直接使用原始文件字节，避免重编码
        b64_str = base64.b64encode(raw_bytes).decode()
    else:
        buf = io.BytesIO()
        save_img = pil_img if pil_img.mode in ("RGB", "L") else pil_img.convert("RGB")
        save_img.save(buf, format="JPEG", quality=90)
        b64_str = base64.b64encode(buf.getvalue()).decode()

    size_info = f"{original_w}x{original_h}→{w}x{h}" if resized else f"{w}x{h}"
    return b64_str, "image/jpeg", size_info, pil_img.mode


def load_images_to_ipc(
    session_id: str,
    raw_paths: list[str],
    *,
    max_dimension: int,
    capability: OperationCapability | None,
) -> ImageLoadResult:
    """Load authorized image files and write one IPC payload below a safe session dir."""
    try:
        PathPolicy.validate_session_id(session_id)
        bound = require_capability(capability, "session", session_id)
        policy = bound.path_policy()
        policy.validate_session_dir(session_id)
    except CapabilityError as error:
        return ImageLoadResult(f"错误: [{error.code}] {error.message}", 1)
    except PathPolicyError as error:
        return ImageLoadResult(f"错误: {error}", 1)

    images_data = []
    results = []
    for idx, raw_path in enumerate(raw_paths, 1):
        expanded = os.path.expanduser(os.path.expandvars(raw_path))
        ext = os.path.splitext(expanded)[1].lstrip(".").lower()
        if ext not in SUPPORTED_EXTENSIONS:
            results.append(
                f"[{idx}] {raw_path}: 不支持的格式 '{ext}' "
                f"(支持: {', '.join(sorted(SUPPORTED_EXTENSIONS))})"
            )
            continue

        try:
            target = policy.validate_image_input(expanded)
        except PathPolicyError as error:
            results.append(f"[{idx}] {raw_path}: {error}")
            continue

        try:
            file_size = os.stat(target.resolved, follow_symlinks=False).st_size
        except OSError as error:
            results.append(f"[{idx}] {raw_path}: 文件不存在 ({error})")
            continue
        if file_size > MAX_FILE_SIZE:
            results.append(
                f"[{idx}] {raw_path}: 文件过大 "
                f"({file_size / 1024 / 1024:.1f}MB > {MAX_FILE_SIZE / 1024 / 1024:.0f}MB)"
            )
            continue

        try:
            b64_str, mime, size_info, mode = _process_image(policy, target, max_dimension)
            images_data.append({"mime": mime, "base64": b64_str})
            results.append(f"[{idx}] {raw_path}: 已加载 ({size_info}, {mode})")
        except PathPolicyError as error:
            results.append(f"[{idx}] {raw_path}: {error}")
        except Exception as error:
            results.append(f"[{idx}] {raw_path}: 加载失败 ({error})")

    success_count = len(images_data)
    total_count = len(raw_paths)
    summary = f"图片加载完成: {success_count}/{total_count} 张成功" + "\n" + "\n".join(results)
    if images_data:
        try:
            output_dir = Path(policy.allocate_session_dir(session_id))
            output_file = output_dir / f"{uuid.uuid4().hex}.json"
            with output_file.open("w", encoding="utf-8") as output:
                json.dump({"images": images_data, "summary": summary}, output, ensure_ascii=False)
        except PathPolicyError as error:
            return ImageLoadResult(f"{summary}\n错误: {error}", 1)
        except OSError as error:
            return ImageLoadResult(f"{summary}\n错误: 图片 IPC 输出失败 ({error})", 1)
    return ImageLoadResult(summary, 0 if success_count > 0 else 1)


def main():
    parser = argparse.ArgumentParser(description="加载本地图片文件")
    parser.add_argument("--session-id", required=True, help="会话 ID")
    parser.add_argument("--files", required=True, help="逗号分隔的文件路径")
    parser.add_argument(
        "--max-dim",
        type=int,
        default=DEFAULT_MAX_DIM,
        help=f"图片最大边长像素，默认 {DEFAULT_MAX_DIM}",
    )
    args = parser.parse_args()

    raw_paths = [p.strip() for p in args.files.split(",") if p.strip()]
    if not raw_paths:
        print("错误: 未提供任何文件路径")
        sys.exit(1)
    if len(raw_paths) > MAX_IMAGE_COUNT:
        print(
            f"错误: 单次最多加载 {MAX_IMAGE_COUNT} 张图片，收到 {len(raw_paths)} 个路径"
        )
        sys.exit(1)

    try:
        capability = _default_image_capability(args.session_id)
    except CapabilityError as error:
        print(f"错误: [{error.code}] {error.message}")
        sys.exit(1)
    except Exception as error:
        print(f"错误: 配置加载失败 ({error})")
        sys.exit(1)

    result = load_images_to_ipc(
        args.session_id,
        raw_paths,
        max_dimension=args.max_dim,
        capability=capability,
    )
    print(result.summary)
    sys.exit(result.exit_code)


if __name__ == "__main__":
    main()
