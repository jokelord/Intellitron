"""Local filesystem search tools backed by whitelist-aware path validation."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from bisect import bisect_right
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path

from backend.config import get_config
from backend.security.capabilities import CapabilityError, OperationCapability, require_capability
from backend.security.process_supervisor import ProcessSupervisor


@dataclass(frozen=True)
class SearchSettings:
    whitelist_roots: tuple[Path, ...]
    exclude_globs: tuple[str, ...]
    glob_default_limit: int
    glob_max_limit: int
    ripgrep_timeout_seconds: int
    grep_default_head_limit: int
    grep_max_head_limit: int
    grep_max_columns: int


_DEFAULT_EXCLUDE_GLOBS = (
    ".git",
    "__pycache__",
    "node_modules",
    ".next",
    ".sglang",
    ".worktrees",
)
_VALID_GREP_OUTPUT_MODES = {"files_with_matches", "content", "count"}
_GREP_OUTPUT_MODE_ALIASES = {
    "files": "files_with_matches",
    "file": "files_with_matches",
    "matches": "files_with_matches",
    "lines": "content",
}


def _coerce_positive_int(value, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def _normalize_paths(raw_paths) -> tuple[Path, ...]:
    normalized: list[Path] = []
    seen: set[Path] = set()
    for raw in raw_paths or []:
        text = str(raw).strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.is_absolute():
            continue
        resolved = path.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        normalized.append(resolved)
    return tuple(normalized)


def _get_search_settings(
    capability: OperationCapability | None = None,
) -> SearchSettings:
    config = get_config()
    agent_cfg = getattr(config, "agent", None)

    if capability is not None:
        bound = require_capability(capability, "search")
        whitelist_roots = _normalize_paths(bound.search_roots)
    else:
        whitelist_roots = _normalize_paths(
            getattr(agent_cfg, "search_whitelist_paths", None) or []
        )
    exclude_globs = tuple(
        str(item).strip()
        for item in (getattr(agent_cfg, "search_exclude_globs", None) or [])
        if str(item).strip()
    ) or _DEFAULT_EXCLUDE_GLOBS

    return SearchSettings(
        whitelist_roots=whitelist_roots,
        exclude_globs=exclude_globs,
        glob_default_limit=_coerce_positive_int(
            getattr(agent_cfg, "glob_default_limit", None), 100
        ),
        glob_max_limit=_coerce_positive_int(
            getattr(agent_cfg, "glob_max_limit", None), 1000
        ),
        ripgrep_timeout_seconds=_coerce_positive_int(
            getattr(agent_cfg, "ripgrep_timeout_seconds", None), 15
        ),
        grep_default_head_limit=_coerce_positive_int(
            getattr(agent_cfg, "grep_default_head_limit", None), 200
        ),
        grep_max_head_limit=_coerce_positive_int(
            getattr(agent_cfg, "grep_max_head_limit", None), 1000
        ),
        grep_max_columns=_coerce_positive_int(
            getattr(agent_cfg, "grep_max_columns", None), 400
        ),
    )


def _format_whitelist(roots: tuple[Path, ...]) -> str:
    if not roots:
        return "(空白名单)"
    return ", ".join(str(root) for root in roots)


def _format_searched_root_lines(targets: list[Path]) -> list[str]:
    return ["- searched root paths:", *(f"  - {target}" for target in targets)]


def _is_path_whitelisted(candidate: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        if candidate == root or root in candidate.parents:
            return True
    return False


def _resolve_search_targets(
    path: str | None,
    *,
    settings: SearchSettings,
    require_directory: bool = False,
) -> tuple[list[Path], str | None]:
    if not settings.whitelist_roots:
        return [], "错误: 搜索白名单为空。请在 config/config-agent-operation.yaml 的 agent.search_whitelist_paths 中配置绝对路径。"

    if path is None:
        existing_roots = [root for root in settings.whitelist_roots if root.exists()]
        if not existing_roots:
            return (
                [],
                "错误: 白名单中的路径当前都不存在。请检查配置: "
                + _format_whitelist(settings.whitelist_roots),
            )
        if require_directory:
            existing_roots = [root for root in existing_roots if root.is_dir()]
        if not existing_roots:
            return [], "错误: 白名单中没有可搜索的目录。"
        return existing_roots, None

    raw_path = str(path).strip()
    if not raw_path:
        return [], "错误: path 不能为空字符串；请省略该参数或传入白名单内的绝对路径。"

    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        return [], "错误: path 必须是绝对路径；请省略 path 以搜索全部白名单，或传入白名单内的绝对路径。"

    resolved = candidate.resolve(strict=False)
    if not _is_path_whitelisted(resolved, settings.whitelist_roots):
        return (
            [],
            "错误: 请求的 path 不在白名单内。允许的白名单根路径: "
            + _format_whitelist(settings.whitelist_roots),
        )

    if not resolved.exists():
        return [], f"错误: 指定的 path 不存在: {resolved}"

    if require_directory and not resolved.is_dir():
        return [], f"错误: 指定的 path 不是目录: {resolved}"

    return [resolved], None


def _clamp_limit(value: int | None, default: int, max_value: int) -> int:
    if value is None:
        resolved = default
    else:
        try:
            resolved = int(value)
        except (TypeError, ValueError):
            resolved = default
    if resolved < 1:
        resolved = default
    return min(resolved, max_value)


def _normalize_offset(offset: int) -> int:
    try:
        return max(0, int(offset))
    except (TypeError, ValueError):
        return 0


def _expand_braces(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]+)\}", pattern)
    if not match:
        return [pattern]
    expanded: list[str] = []
    prefix = pattern[: match.start()]
    suffix = pattern[match.end() :]
    for part in match.group(1).split(","):
        for candidate in _expand_braces(prefix + part.strip() + suffix):
            expanded.append(candidate)
    return expanded


def _expand_globstar_zero_depth(pattern: str) -> list[str]:
    variants = [pattern]
    while True:
        expanded = False
        next_variants: list[str] = []
        for variant in variants:
            if "**/" in variant:
                next_variants.append(variant.replace("**/", "", 1))
                next_variants.append(variant.replace("**/", "*/", 1))
                expanded = True
            else:
                next_variants.append(variant)
        variants = next_variants
        if not expanded:
            break
    return list(dict.fromkeys(variants))


def _split_glob_parts(argument: str | None) -> tuple[str, ...]:
    if not argument or not str(argument).strip():
        return tuple()

    text = str(argument).strip()
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in text:
        if char == "{" and depth >= 0:
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
        if char == "," and depth == 0:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue
        current.append(char)

    tail = "".join(current).strip()
    if tail:
        parts.append(tail)

    expanded: list[str] = []
    for part in parts:
        for candidate in _expand_braces(part):
            if candidate:
                expanded.append(candidate)
    return tuple(dict.fromkeys(expanded))


def _split_glob_argument(argument: str | None) -> tuple[str, ...]:
    expanded: list[str] = []
    for part in _split_glob_parts(argument):
        expanded.extend(
            glob_candidate
            for glob_candidate in _expand_globstar_zero_depth(part)
            if glob_candidate
        )
    return tuple(dict.fromkeys(expanded))


def _suggest_filename_keyword_globs(pattern: str) -> tuple[str, ...]:
    suggestions: list[str] = []
    for part in _split_glob_parts(pattern):
        separator_index = part.rfind("/")
        prefix = part[: separator_index + 1] if separator_index >= 0 else ""
        name_pattern = part[separator_index + 1 :] if separator_index >= 0 else part
        if not name_pattern or name_pattern[0] in "*?[":
            continue
        suggestions.append(f"{prefix}*{name_pattern}")
    return tuple(dict.fromkeys(item for item in suggestions if item != pattern))


def _expand_exclude_globs(patterns: tuple[str, ...]) -> tuple[str, ...]:
    expanded: list[str] = []
    for pattern in patterns:
        text = str(pattern).strip()
        if not text:
            continue
        if any(ch in text for ch in "*?[") or "/" in text:
            expanded.append(text)
            continue
        expanded.extend([text, f"{text}/**", f"**/{text}", f"**/{text}/**"])
    return tuple(dict.fromkeys(expanded))


def _matches_glob_patterns(relative_posix_path: str, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return False
    return any(fnmatch.fnmatch(relative_posix_path, pattern) for pattern in patterns)


def _is_probably_binary(file_path: Path) -> bool:
    try:
        descriptor = os.open(
            file_path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            sample = handle.read(2048)
        return b"\x00" in sample
    except OSError:
        return True


def _read_text(file_path: Path) -> str | None:
    if _is_probably_binary(file_path):
        return None
    try:
        descriptor = os.open(
            file_path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        with os.fdopen(
            descriptor,
            "r",
            encoding="utf-8",
            errors="replace",
            closefd=True,
        ) as handle:
            return handle.read()
    except OSError:
        return None


def _iter_files(target: Path, exclude_patterns: tuple[str, ...]):
    if target.is_file():
        yield Path(os.path.abspath(os.fspath(target))), target.name
        return

    if not target.is_dir():
        return

    for current_root, dirnames, filenames in os.walk(target, topdown=True):
        current_path = Path(current_root)

        pruned_dirnames: list[str] = []
        for dirname in dirnames:
            candidate = current_path / dirname
            relative = candidate.relative_to(target).as_posix()
            if _matches_glob_patterns(relative, exclude_patterns):
                continue
            pruned_dirnames.append(dirname)
        dirnames[:] = pruned_dirnames

        for filename in filenames:
            candidate = current_path / filename
            relative = candidate.relative_to(target).as_posix()
            if _matches_glob_patterns(relative, exclude_patterns):
                continue
            yield Path(os.path.abspath(os.fspath(candidate))), relative


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    deduped: list[Path] = []
    for path in paths:
        normalized = os.path.abspath(os.fspath(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(Path(normalized))
    return deduped


def _sort_paths(paths: list[Path]) -> list[Path]:
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(paths, key=lambda item: (-_mtime(item), str(item)))


def _has_ripgrep() -> bool:
    return shutil.which("rg") is not None


def _run_ripgrep(
    command: list[str],
    *,
    timeout_seconds: int | None = None,
    check_abort: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess | None:
    """Run ripgrep in a bounded process group with a minimal environment."""
    executable = shutil.which(command[0]) if command else None
    if executable is None:
        return None
    argv = [executable, *command[1:]]
    timeout = float(timeout_seconds if timeout_seconds is not None else 15)
    environment = {
        "PATH": str(Path(executable).parent),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    result = ProcessSupervisor(
        max_output_bytes=8_388_608,
        termination_grace_seconds=0.5,
    ).run_argv(
        argv,
        cwd=None,
        env=environment,
        timeout=timeout,
        check_abort=check_abort,
    )
    if result.timed_out:
        raise subprocess.TimeoutExpired(argv, timeout)
    if result.aborted:
        return subprocess.CompletedProcess(
            argv,
            130,
            stdout=result.stdout,
            stderr=result.stderr or "ripgrep search aborted",
        )
    return subprocess.CompletedProcess(
        argv,
        result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _is_rg_permission_issue(completed: subprocess.CompletedProcess) -> bool:
    if completed.returncode != 2:
        return False
    if (completed.stdout or "").strip():
        return False
    stderr = (completed.stderr or "").lower()
    if not stderr.strip():
        return True
    return "permission denied" in stderr or "os error 13" in stderr


def _is_rg_partial_success(completed: subprocess.CompletedProcess) -> bool:
    return completed.returncode == 2 and bool((completed.stdout or "").strip())


def _normalize_glob_pattern_for_target(pattern: str, target: Path) -> str:
    text = (pattern or "").strip()
    if not text:
        return text

    target_root = target.resolve(strict=False)
    pattern_path = Path(text).expanduser()
    if not pattern_path.is_absolute():
        return text

    resolved_pattern = pattern_path.resolve(strict=False)
    if not (resolved_pattern == target_root or target_root in resolved_pattern.parents):
        return text

    try:
        relative = resolved_pattern.relative_to(target_root)
    except ValueError:
        return text

    relative_text = relative.as_posix()
    return relative_text or "**/*"


def _resolve_rg_path(raw_path: str, target: Path) -> Path:
    candidate = Path(raw_path.strip())
    if candidate.is_absolute():
        return candidate
    base = target if target.is_dir() else target.parent
    return (base / candidate)


def _discover_matches_with_ripgrep(
    *,
    pattern: str,
    targets: list[Path],
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    case_insensitive: bool,
    multiline: bool,
    max_columns: int,
    timeout_seconds: int,
    check_abort: Callable[[], bool] | None = None,
) -> tuple[list[Path] | None, str | None]:
    if not _has_ripgrep():
        return None, None

    results: list[Path] = []
    for target in targets:
        command = [
            "rg",
            "-l",
            "--hidden",
            "--no-messages",
            "--max-columns",
            str(max_columns),
        ]
        if case_insensitive:
            command.append("-i")
        if multiline:
            command.extend(["-U", "--multiline-dotall"])
        for include_pattern in include_patterns:
            command.extend(["-g", include_pattern])
        for exclude_pattern in exclude_patterns:
            command.extend(["-g", f"!{exclude_pattern}"])
        if pattern.startswith("-"):
            command.extend(["-e", pattern])
        else:
            command.append(pattern)
        command.append(str(target))

        try:
            completed = _run_ripgrep(
                command,
                timeout_seconds=timeout_seconds,
                check_abort=check_abort,
            )
        except subprocess.TimeoutExpired:
            return (
                None,
                f"错误: ripgrep 搜索超时（>{timeout_seconds}s）。请缩小 path/include 范围后重试。",
            )
        if completed is None:
            return None, None
        if _is_rg_permission_issue(completed):
            return None, None
        if completed.returncode not in (0, 1) and not _is_rg_partial_success(completed):
            stderr = (completed.stderr or "").strip() or "未知 ripgrep 错误"
            return None, f"错误: ripgrep 搜索失败: {stderr}"
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            results.append(_resolve_rg_path(line, target))

    return _sort_paths(_dedupe_paths(results)), None


def _compile_regex(pattern: str, case_insensitive: bool, multiline: bool):
    flags = re.MULTILINE
    if case_insensitive:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.DOTALL
    return re.compile(pattern, flags)


def _discover_matches_with_python(
    *,
    pattern: str,
    targets: list[Path],
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    case_insensitive: bool,
    multiline: bool,
    check_abort: Callable[[], bool] | None = None,
) -> tuple[list[Path] | None, str | None]:
    try:
        regex = _compile_regex(pattern, case_insensitive, multiline)
    except re.error as exc:
        return None, f"错误: 无效正则表达式: {exc}"

    matches: list[Path] = []
    for target in targets:
        if check_abort and check_abort():
            return None, "错误: 搜索已被用户中止"
        if target.is_file():
            candidate_files = [(target.resolve(strict=False), target.name)]
        else:
            candidate_files = list(_iter_files(target, exclude_patterns))
        for file_path, relative_path in candidate_files:
            if check_abort and check_abort():
                return None, "错误: 搜索已被用户中止"
            if include_patterns and not _matches_glob_patterns(relative_path, include_patterns):
                continue
            text = _read_text(file_path)
            if text is None:
                continue
            if regex.search(text):
                matches.append(file_path)

    return _sort_paths(_dedupe_paths(matches)), None


def _discover_matching_files(
    *,
    pattern: str,
    targets: list[Path],
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    case_insensitive: bool,
    multiline: bool,
    max_columns: int,
    timeout_seconds: int,
    check_abort: Callable[[], bool] | None = None,
) -> tuple[list[Path] | None, str | None]:
    matches, error = _discover_matches_with_ripgrep(
        pattern=pattern,
        targets=targets,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        case_insensitive=case_insensitive,
        multiline=multiline,
        max_columns=max_columns,
        timeout_seconds=timeout_seconds,
        check_abort=check_abort,
    )
    if matches is not None or error is not None:
        return matches, error
    return _discover_matches_with_python(
        pattern=pattern,
        targets=targets,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        case_insensitive=case_insensitive,
        multiline=multiline,
        check_abort=check_abort,
    )


def _glob_with_ripgrep(
    *,
    targets: list[Path],
    pattern: str,
    exclude_patterns: tuple[str, ...],
    timeout_seconds: int,
    check_abort: Callable[[], bool] | None = None,
) -> tuple[list[Path] | None, str | None]:
    if not _has_ripgrep():
        return None, None

    positive_patterns = _split_glob_argument(pattern)
    if not positive_patterns:
        return None, "错误: pattern 不能为空。"

    results: list[Path] = []
    for target in targets:
        normalized_patterns = tuple(
            _normalize_glob_pattern_for_target(include_pattern, target)
            for include_pattern in positive_patterns
        )
        command = ["rg", "--files", "--hidden", "-L"]
        for include_pattern in normalized_patterns:
            command.extend(["-g", include_pattern])
        for exclude_pattern in exclude_patterns:
            command.extend(["-g", f"!{exclude_pattern}"])
        command.append(str(target))

        try:
            completed = _run_ripgrep(
                command,
                timeout_seconds=timeout_seconds,
                check_abort=check_abort,
            )
        except subprocess.TimeoutExpired:
            return (
                None,
                f"错误: ripgrep 文件搜索超时（>{timeout_seconds}s）。请缩小 path 或 pattern 范围后重试。",
            )
        if completed is None:
            return None, None
        if _is_rg_permission_issue(completed):
            return None, None
        if completed.returncode not in (0, 1) and not _is_rg_partial_success(completed):
            stderr = (completed.stderr or "").strip() or "未知 ripgrep 错误"
            return None, f"错误: ripgrep 文件搜索失败: {stderr}"
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            results.append(_resolve_rg_path(line, target))

    return _sort_paths(_dedupe_paths(results)), None


def _glob_with_python(
    *,
    targets: list[Path],
    pattern: str,
    exclude_patterns: tuple[str, ...],
    check_abort: Callable[[], bool] | None = None,
) -> tuple[list[Path] | None, str | None]:
    positive_patterns = _split_glob_argument(pattern)
    if not positive_patterns:
        return None, "错误: pattern 不能为空。"

    matches: list[Path] = []
    for target in targets:
        if check_abort and check_abort():
            return None, "错误: 搜索已被用户中止"
        normalized_patterns = tuple(
            _normalize_glob_pattern_for_target(include_pattern, target)
            for include_pattern in positive_patterns
        )
        for file_path, relative_path in _iter_files(target, exclude_patterns):
            if check_abort and check_abort():
                return None, "错误: 搜索已被用户中止"
            if _matches_glob_patterns(relative_path, normalized_patterns):
                matches.append(file_path)
    return _sort_paths(_dedupe_paths(matches)), None


def _count_matches_in_file(file_path: Path, regex: re.Pattern) -> int:
    text = _read_text(file_path)
    if text is None:
        return 0
    return sum(1 for _ in regex.finditer(text))


def _build_line_starts(lines_with_endings: list[str]) -> list[int]:
    starts: list[int] = []
    position = 0
    for line in lines_with_endings:
        starts.append(position)
        position += len(line)
    return starts


def _offset_to_line(line_starts: list[int], offset: int) -> int:
    if not line_starts:
        return 0
    return max(0, bisect_right(line_starts, offset) - 1)


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    ranges = sorted(ranges)
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 1:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _render_content_lines(
    file_path: Path,
    regex: re.Pattern,
    *,
    line_numbers: bool,
    context_before: int,
    context_after: int,
) -> list[str]:
    text = _read_text(file_path)
    if text is None:
        return []

    lines_with_endings = text.splitlines(keepends=True)
    if not lines_with_endings and text == "":
        return []
    plain_lines = text.splitlines()
    if text.endswith("\n"):
        plain_lines.append("")
    if not plain_lines and text:
        plain_lines = [text]

    line_starts = _build_line_starts(lines_with_endings)
    match_line_indexes: set[int] = set()
    expanded_ranges: list[tuple[int, int]] = []
    for match in regex.finditer(text):
        start_line = _offset_to_line(line_starts, match.start())
        end_offset = match.start() if match.end() <= match.start() else match.end() - 1
        end_line = _offset_to_line(line_starts, end_offset)
        for line_index in range(start_line, end_line + 1):
            match_line_indexes.add(line_index)
        expanded_ranges.append(
            (
                max(0, start_line - context_before),
                min(len(plain_lines) - 1, end_line + context_after),
            )
        )

    if not expanded_ranges:
        return []

    rendered: list[str] = []
    for start, end in _merge_ranges(expanded_ranges):
        for line_index in range(start, end + 1):
            line_text = plain_lines[line_index] if line_index < len(plain_lines) else ""
            is_match = line_index in match_line_indexes
            if line_numbers:
                separator = ":" if is_match else "-"
                rendered.append(
                    f"{file_path}{separator}{line_index + 1}:{line_text}"
                )
            else:
                separator = ": " if is_match else "- "
                rendered.append(f"{file_path}{separator}{line_text}")
    return rendered


def glob_files(
    pattern: str,
    path: str | None = None,
    limit: int | None = None,
    capability: OperationCapability | None = None,
    check_abort: Callable[[], bool] | None = None,
) -> str:
    """在白名单绝对路径中按 glob 模式查找文件。

    使用建议：
    - 当你需要按文件名、目录结构或通配路径找文件时，优先使用本工具。
    - 若你不知道文件大概在哪个白名单根路径下，省略 path，让工具搜索全部白名单根路径。
    - 若你已经知道目标目录，请传入白名单内的绝对路径 path 来缩小搜索范围；path 不需要以 / 结尾。
    - 如果返回 0 个结果，先检查摘要中的 searched root paths 是否包含你想搜索的目录。
    - 按文件名关键词搜索时用 **/*关键词*；**/关键词* 只匹配以关键词开头的路径段。
    - 返回结果是绝对路径，可直接交给后续文件操作。
    - 若结果过多，优先缩小 pattern，或者把 path 改成更窄的绝对目录。

    Args:
        pattern: Glob 模式，例如 "**/*.py"、"**/*关键词*"、"frontend/src/**/*.tsx"、"src/**/*.{ts,tsx}"。
        path: 可选绝对路径。只能是白名单内目录；省略时会搜索全部白名单根路径；不需要以 / 结尾。
        limit: 最多返回多少个文件。省略时使用配置默认值；过大时会被安全裁剪。

    Returns:
        面向 LLM 的结构化文本，包含搜索摘要、命中的绝对路径列表，以及在结果过多时的收窄范围提示。
    """

    pattern = (pattern or "").strip()
    if not pattern:
        return "错误: pattern 不能为空。"

    try:
        settings = _get_search_settings(capability)
    except CapabilityError as error:
        return f"操作被安全策略阻断 [{error.code}]: {error.message}"
    targets, error = _resolve_search_targets(
        path,
        settings=settings,
        require_directory=True,
    )
    if error:
        return error

    effective_limit = _clamp_limit(limit, settings.glob_default_limit, settings.glob_max_limit)
    exclude_patterns = _expand_exclude_globs(settings.exclude_globs)

    matches, error = _glob_with_ripgrep(
        targets=targets,
        pattern=pattern,
        exclude_patterns=exclude_patterns,
        timeout_seconds=settings.ripgrep_timeout_seconds,
        check_abort=check_abort,
    )
    if matches is None and error is None:
        matches, error = _glob_with_python(
            targets=targets,
            pattern=pattern,
            exclude_patterns=exclude_patterns,
            check_abort=check_abort,
        )
    if error:
        return error

    matches = matches or []
    shown = matches[:effective_limit]
    lines = [
        "Glob search summary",
        f"- pattern: {pattern}",
        f"- searched roots: {len(targets)}",
        *_format_searched_root_lines(targets),
        f"- matched files: {len(matches)}",
        f"- showing: {len(shown)}",
        "",
        "Files:",
    ]

    if shown:
        for index, file_path in enumerate(shown, start=1):
            lines.append(f"{index}. {file_path}")
    else:
        lines.append("(no files matched)")

        suggested_patterns = _suggest_filename_keyword_globs(pattern)
        if suggested_patterns:
            suggested_matches, suggested_error = _glob_with_ripgrep(
                targets=targets,
                pattern=",".join(suggested_patterns),
                exclude_patterns=exclude_patterns,
                timeout_seconds=settings.ripgrep_timeout_seconds,
                check_abort=check_abort,
            )
            if suggested_matches is None and suggested_error is None:
                suggested_matches, suggested_error = _glob_with_python(
                    targets=targets,
                    pattern=",".join(suggested_patterns),
                    exclude_patterns=exclude_patterns,
                    check_abort=check_abort,
                )
            if suggested_matches:
                shown_suggestions = suggested_matches[:effective_limit]
                lines.extend(
                    [
                        "",
                        "Possible filename keyword matches",
                        "- note: strict glob matched 0 files. If you intended a "
                        "filename substring search, use `*` before the keyword "
                        "in the filename segment.",
                        f"- suggested pattern: {suggested_patterns[0]}",
                        f"- possible matches: {len(suggested_matches)}",
                        f"- showing: {len(shown_suggestions)}",
                        "",
                        "Files:",
                    ]
                )
                for index, file_path in enumerate(shown_suggestions, start=1):
                    lines.append(f"{index}. {file_path}")

    if len(matches) > len(shown):
        lines.extend(
            [
                "",
                "Note: results truncated. Narrow `pattern` or pass a narrower absolute `path`.",
            ]
        )

    return "\n".join(lines)


def grep_search(
    pattern: str,
    path: str | None = None,
    include: str | None = None,
    output_mode: str = "files_with_matches",
    case_insensitive: bool = False,
    multiline: bool = False,
    context_before: int = 0,
    context_after: int = 0,
    context: int | None = None,
    line_numbers: bool = True,
    head_limit: int | None = None,
    offset: int = 0,
    capability: OperationCapability | None = None,
    check_abort: Callable[[], bool] | None = None,
) -> str:
    """在白名单绝对路径中按正则搜索文件内容。

    使用建议：
    - 当你需要按内容、符号名、报错文本、函数签名或正则表达式搜索时，优先使用本工具。
    - 第一次探索通常先用 output_mode="files_with_matches" 找到候选文件，再切换到 output_mode="content" 看上下文。
    - 如果你已经知道只想搜某类文件，传 include，例如 "*.py" 或 "*.{ts,tsx}"。
    - 若结果很多，用 offset 翻页；用 head_limit 控制每页大小；不要一次取过多内容。
    - path 只能是白名单内绝对路径，且不需要以 / 结尾；若不确定目录，省略 path 以搜索全部白名单根路径。
    - 如果返回 0 个结果，先检查摘要中的 searched root paths 是否包含你想搜索的目录。
    - 如果想查看更多上下文，优先用 context；若需要不对称上下文，再用 context_before 和 context_after。

    Args:
        pattern: 要搜索的正则表达式，例如 "class\\s+AgentRunner" 或 "tool_calls?"。
        path: 可选绝对路径。可以是白名单内的目录或单个文件；省略时搜索全部白名单根路径；不需要以 / 结尾。
        include: 可选通配符过滤，例如 "*.py"、"*.md"、"*.{ts,tsx}"。
        output_mode: 输出模式，可选 "files_with_matches"、"content"、"count"。
        case_insensitive: 是否忽略大小写。
        multiline: 是否启用跨行匹配；当模式可能跨越换行时设为 true。
        context_before: content 模式下，每个命中前额外显示多少行。
        context_after: content 模式下，每个命中后额外显示多少行。
        context: content 模式下，对称上下文行数；若提供则优先于 context_before/context_after。
        line_numbers: content 模式下是否显示行号。
        head_limit: 最多返回多少条结果；省略时使用配置默认值，过大时会被安全裁剪。
        offset: 跳过前多少条结果后再开始返回；常用于分页。

    Returns:
        面向 LLM 的结构化文本，包含搜索摘要，以及文件列表、内容片段或计数结果。
    """

    pattern = (pattern or "").strip()
    if not pattern:
        return "错误: pattern 不能为空。"

    output_mode = (output_mode or "files_with_matches").strip()
    output_mode = _GREP_OUTPUT_MODE_ALIASES.get(output_mode, output_mode)
    if output_mode not in _VALID_GREP_OUTPUT_MODES:
        valid = ", ".join(sorted(_VALID_GREP_OUTPUT_MODES))
        return f"错误: output_mode 必须是以下之一: {valid}"

    try:
        settings = _get_search_settings(capability)
    except CapabilityError as error:
        return f"操作被安全策略阻断 [{error.code}]: {error.message}"
    targets, error = _resolve_search_targets(path, settings=settings)
    if error:
        return error

    include_patterns = _split_glob_argument(include)
    exclude_patterns = _expand_exclude_globs(settings.exclude_globs)
    effective_head_limit = _clamp_limit(
        head_limit,
        settings.grep_default_head_limit,
        settings.grep_max_head_limit,
    )
    effective_offset = _normalize_offset(offset)
    if context is not None:
        context_before = context_after = max(0, int(context))
    else:
        context_before = max(0, int(context_before or 0))
        context_after = max(0, int(context_after or 0))

    try:
        regex = _compile_regex(pattern, case_insensitive, multiline)
    except re.error as exc:
        return f"错误: 无效正则表达式: {exc}"

    matched_files, error = _discover_matching_files(
        pattern=pattern,
        targets=targets,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        case_insensitive=case_insensitive,
        multiline=multiline,
        max_columns=settings.grep_max_columns,
        timeout_seconds=settings.ripgrep_timeout_seconds,
        check_abort=check_abort,
    )
    if error:
        return error

    matched_files = matched_files or []

    if output_mode == "files_with_matches":
        paged = matched_files[effective_offset : effective_offset + effective_head_limit]
        lines = [
            "Grep search summary",
            f"- pattern: {pattern}",
            f"- output_mode: {output_mode}",
            f"- searched roots: {len(targets)}",
            *_format_searched_root_lines(targets),
            f"- matched files: {len(matched_files)}",
            f"- showing files: {len(paged)}",
            f"- offset: {effective_offset}",
            "",
            "Files:",
        ]
        if paged:
            for index, file_path in enumerate(paged, start=1):
                lines.append(f"{index}. {file_path}")
        else:
            lines.append("(no files matched on this page)")
        if effective_offset + len(paged) < len(matched_files):
            lines.extend(
                [
                    "",
                    "Note: results truncated. Increase `offset` for the next page or narrow `path`/`include`.",
                ]
            )
        return "\n".join(lines)

    if output_mode == "count":
        count_entries = [
            (file_path, _count_matches_in_file(file_path, regex)) for file_path in matched_files
        ]
        count_entries = [(file_path, count) for file_path, count in count_entries if count > 0]
        paged = count_entries[effective_offset : effective_offset + effective_head_limit]
        lines = [
            "Grep search summary",
            f"- pattern: {pattern}",
            f"- output_mode: {output_mode}",
            f"- searched roots: {len(targets)}",
            *_format_searched_root_lines(targets),
            f"- matched files: {len(count_entries)}",
            f"- showing files: {len(paged)}",
            f"- offset: {effective_offset}",
            "",
            "Counts:",
        ]
        if paged:
            for file_path, count in paged:
                lines.append(f"{count}\t{file_path}")
        else:
            lines.append("(no count results on this page)")
        if effective_offset + len(paged) < len(count_entries):
            lines.extend(
                [
                    "",
                    "Note: results truncated. Increase `offset` for the next page or narrow `path`/`include`.",
                ]
            )
        return "\n".join(lines)

    content_lines: list[str] = []
    for file_path in matched_files:
        content_lines.extend(
            _render_content_lines(
                file_path,
                regex,
                line_numbers=line_numbers,
                context_before=context_before,
                context_after=context_after,
            )
        )

    paged = content_lines[effective_offset : effective_offset + effective_head_limit]
    lines = [
        "Grep search summary",
        f"- pattern: {pattern}",
        f"- output_mode: {output_mode}",
        f"- searched roots: {len(targets)}",
        *_format_searched_root_lines(targets),
        f"- matched files: {len(matched_files)}",
        f"- showing lines: {len(paged)}",
        f"- offset: {effective_offset}",
        "",
        "Content:",
    ]
    if paged:
        lines.extend(paged)
    else:
        lines.append("(no content results on this page)")
    if effective_offset + len(paged) < len(content_lines):
        lines.extend(
            [
                "",
                "Note: results truncated. Increase `offset` for the next page or narrow `path`/`include`.",
            ]
        )
    return "\n".join(lines)
