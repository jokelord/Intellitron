"""
文件操作工具 (File Operations)

提供文件读取、写入和精确编辑功能，供 Agent 调用。
编辑功能使用 editblock_utils 的 SEARCH/REPLACE 匹配算法。

---
write_file 的指数退避重试逻辑借鉴自 Aider-AI/aider:
  - 仓库: https://github.com/Aider-AI/aider
  - 文件: aider/io.py (write_text 方法)
  - 协议: Apache License 2.0
---
"""

import os
import stat
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

from backend.security.capabilities import (
    CapabilityError,
    OperationCapability,
    require_capability,
)
from backend.security.path_policy import PathPolicy, PathPolicyError, ResolvedPath

from .editblock_utils import find_similar_lines, replace_most_similar_chunk
from .read_file.service import ReadFileService
from .read_file.text_reader import (
    _decode_bytes,
    _decoded_looks_like,
    _detect_encoding,
    _read_text_with_encoding_detection,
    _strip_bom,
)

_read_file_service: ReadFileService | None = None
_file_operation_policy_context: ContextVar[
    tuple[PathPolicy, OperationCapability] | None
] = ContextVar(
    "file_operation_policy", default=None
)


def _get_file_operation_policy() -> PathPolicy:
    """Return only a capability-bound policy at the side-effect boundary."""
    injected = _file_operation_policy_context.get()
    if injected is None:
        raise PathPolicyError(
            "missing_capability", "operation capability is required"
        )
    policy, _capability = injected
    return policy


@contextmanager
def use_file_operation_policy(
    policy: PathPolicy,
    *,
    capability: OperationCapability,
):
    """Bind an already-resolved runtime policy without changing tool schemas."""
    bound = require_capability(capability, "file")
    expected = bound.path_policy()
    if (
        policy.file_operation_roots != expected.file_operation_roots
        or policy.image_input_roots != expected.image_input_roots
        or policy.session_base_dir != expected.session_base_dir
        or policy.max_file_bytes != expected.max_file_bytes
        or policy.allow_overwrite_existing != expected.allow_overwrite_existing
    ):
        raise PathPolicyError(
            "capability_policy_mismatch",
            "file operation policy does not match the bound capability",
        )
    token = _file_operation_policy_context.set((policy, capability))
    try:
        yield
    finally:
        _file_operation_policy_context.reset(token)


def _get_read_file_service() -> ReadFileService:
    global _read_file_service
    if _read_file_service is None:
        _read_file_service = ReadFileService()
    return _read_file_service


def read_file(
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    encoding: str | None = None,
    password: str | None = None,
    document_mode: Literal["auto", "distorted"] = "auto",
    recognize_seals: bool = False,
    check_abort: Callable[[], bool] | None = None,
    capability: OperationCapability | None = None,
) -> str:
    """
    读取文件内容，每行前附行号。

    自动检测文件编码（UTF-8、UTF-8 BOM、UTF-16 LE/BE、GBK/GB18030、Big5、
    Shift_JIS、EUC-KR、Latin-1 等）。本地 LLM 无需关心文件实际编码，工具
    会自动处理并在返回头部注明检测到的编码，便于后续编辑类工具保持编码一致。

    Args:
        file_path: 文件的绝对或相对路径
        start_line: 起始行号（1-based，含），省略表示从第一行开始。建议按需限制范围。
        end_line: 结束行号（1-based，含），省略表示到最后一行。建议按需限制范围。
        encoding: 可选。显式指定文件编码（如 "utf-8"、"gbk"、"gb18030"）。
            省略（默认）表示自动检测。仅在自动检测出错或你明确知道文件是
            特殊编码时使用；LLM 通常不需要关心此参数。
        password: 可选的加密 PDF 密码。仅适用于 PDF，不支持 Office 文档解密；
            不会记录或明文缓存。
        document_mode: 文档模式，只能是 "auto" 或 "distorted"，默认 "auto"。
        recognize_seals: 是否启用印章识别，默认 false；启用时传 true。
        check_abort: 内部取消回调，不暴露给模型。
        capability: 后端注入的会话 capability，不暴露给模型。

    Returns:
        带行号前缀的文件内容字符串，包含范围、总行数和实际使用的编码信息。
        如果文件无法读取（如不存在），返回一段人类可读的错误描述字符串，
        而不是抛异常。
    """
    if capability is not None:
        try:
            bound = require_capability(capability, "read_file")
            read_policy = PathPolicy(
                file_operation_roots=bound.read_roots,
                max_file_bytes=bound.max_file_bytes,
            )
            file_path = read_policy.resolve_existing(file_path, "read").resolved
        except (CapabilityError, PathPolicyError) as error:
            if isinstance(error, CapabilityError):
                return f"文件读取失败 [{error.code}] {error.message}"
            return _policy_failure("文件读取失败", error)

    return _get_read_file_service().read(
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
        encoding=encoding,
        password=password,
        document_mode=document_mode,
        recognize_seals=recognize_seals,
        check_abort=check_abort,
    )


def _policy_failure(prefix: str, error: PathPolicyError) -> str:
    return f"{prefix} {error}"


def _atomic_write(
    policy: PathPolicy,
    target: ResolvedPath,
    content: bytes,
    *,
    require_existing: bool,
    allow_overwrite: bool,
) -> None:
    """Write a complete replacement through a no-follow parent descriptor."""
    parent_fd = policy.open_parent_dir(target, create=True)
    temporary_name: str | None = None
    temporary_fd: int | None = None
    try:
        existing_metadata = policy.check_atomic_target(
            parent_fd,
            target.name,
            require_existing=require_existing,
            allow_overwrite=allow_overwrite,
        )
        target_mode = stat.S_IMODE(existing_metadata.st_mode) if existing_metadata else 0o600
        temporary_name = policy.temporary_name()
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        if existing_metadata is not None:
            os.fchmod(temporary_fd, target_mode)
        with os.fdopen(temporary_fd, "wb", closefd=True) as handle:
            temporary_fd = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        # Revalidate after writing the temporary file.  If an attacker swaps
        # the final component, a symlink is rejected; os.replace itself acts
        # only within this already-open parent directory.
        policy.check_atomic_target(
            parent_fd,
            target.name,
            require_existing=require_existing,
            allow_overwrite=allow_overwrite,
        )
        os.replace(
            temporary_name,
            target.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary_name = None
        os.fsync(parent_fd)
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _write_with_retries(
    policy: PathPolicy,
    target: ResolvedPath,
    content: bytes,
    *,
    require_existing: bool,
    allow_overwrite: bool,
) -> PathPolicyError | None:
    max_retries = 5
    delay = 0.1
    for attempt in range(max_retries):
        try:
            _atomic_write(
                policy,
                target,
                content,
                require_existing=require_existing,
                allow_overwrite=allow_overwrite,
            )
            return None
        except PathPolicyError as error:
            return error
        except PermissionError:
            if attempt == max_retries - 1:
                return PathPolicyError("io_error", "temporary file operation was denied")
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def write_file(file_path: str, content: str) -> str:
    """
    写入新文件（覆盖），自动创建父目录。
    【重要提示】如已有同名文件，将被覆盖且无法恢复，请谨慎使用。
    如果需求是添加、修改或删除特定文本，则使用适合局部变更的编辑能力，而非直接本工具覆盖整个文件。

    Args:
        file_path: 目标文件路径
        content: 要写入的内容

    Returns:
        操作结果描述
    """
    try:
        policy = _get_file_operation_policy()
        encoded = policy.validate_content(content)
        target = policy.validate_create(file_path, "write")
    except PathPolicyError as error:
        return _policy_failure("文件写入失败", error)

    error = _write_with_retries(
        policy,
        target,
        encoded,
        require_existing=False,
        allow_overwrite=policy.allow_overwrite_existing,
    )
    if error is not None:
        return _policy_failure("文件写入失败", error)
    return f"文件已写入: {file_path} ({len(content)} 字节)"


def _normalize_escape_sequences(text: str) -> str:
    """将 LLM JSON 中残留的字面转义序列还原为真实字符。

    当 LLM 在 JSON 字符串中发送 ``"def f():\\n    return"`` 时，
    Python ``json.loads`` 会把 ``\\n`` 解析为两个字符（反斜杠 + n），而非真正的换行。
    本函数将这些字面量序列转换回真实的空白字符。

    仅转换 \\n / \\t / \\r 三种最常见的空白转义，不处理 \\\\ 等，
    以避免误伤含有合法反斜杠的代码（如正则表达式）。
    """
    result = text.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
    return result


def _has_literal_escapes(text: str) -> bool:
    """检查文本中是否包含字面转义序列（反斜杠 + n/t/r）。"""
    return "\\n" in text or "\\t" in text or "\\r" in text


def edit_file(file_path: str, search_text: str, replace_text: str) -> str:
    """
    编辑已存在的文件：先区分“末尾追加”与“替换/中间插入”。独立于整体创建/覆盖开关。

    【末尾追加】必须将 search_text 设为空字符串，replace_text 只含新增内容；不要复制旧末尾文本作匹配锚点。
    无需为寻找追加锚点先读取文件。不会自动补换行，所需换行或空行须放入 replace_text。
    search_text 是必填字符串，不可省略或传 null。

    【替换/中间插入】search_text 提供现有原文中足够精确、唯一的一整行或多行，按需读取确认；replace_text 提供替换内容。
    匹配依次尝试精确、空白归一化、省略号、模糊匹配；失败时可尝试还原字面转义序列（如 \\n）后再匹配。

    Args:
        file_path: 已存在的目标文件路径
        search_text: 必填字符串；末尾追加必须为空字符串，不含旧文本；替换/中间插入则为准确且唯一的原文片段。
        replace_text: 追加时只含新增内容及所需换行，不复制旧文本；替换时为新的文本片段。

    Returns:
        操作结果描述（成功或包含最相似片段的错误信息）
    """
    try:
        policy = _get_file_operation_policy()
        target = policy.validate_modify(file_path, "edit")
        descriptor = policy.open_existing_file(target)
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as f:
            content = f.read()
    except PathPolicyError as error:
        return _policy_failure("编辑失败", error)

    # 追加模式：search_text 为空时直接追加到文件末尾
    if not search_text or not search_text.strip():
        append_text = replace_text
        if _has_literal_escapes(append_text):
            append_text = _normalize_escape_sequences(append_text)
        new_content = content + append_text
        try:
            encoded = policy.validate_content(new_content)
        except PathPolicyError as error:
            return _policy_failure("编辑失败", error)
        error = _write_with_retries(
            policy,
            target,
            encoded,
            require_existing=True,
            allow_overwrite=True,
        )
        if error is not None:
            return _policy_failure("编辑失败", error)
        return f"文件已追加: {file_path} (追加了 {len(append_text)} 字节)"

    new_content = replace_most_similar_chunk(content, search_text, replace_text)

    # 回退：如果原始文本含有字面转义序列且匹配失败，尝试还原后重新匹配
    if new_content is None and _has_literal_escapes(search_text):
        norm_search = _normalize_escape_sequences(search_text)
        norm_replace = _normalize_escape_sequences(replace_text)
        new_content = replace_most_similar_chunk(content, norm_search, norm_replace)

    if new_content is None:
        # 匹配失败，尝试给出最相似的片段帮助 LLM 修正
        similar = find_similar_lines(search_text, content)
        error_msg = f"编辑失败: 在 {file_path} 中未找到匹配的 SEARCH 块。"
        if similar:
            error_msg += f"\n\n最相似的代码片段:\n```\n{similar}\n```\n\n请根据上述实际内容修正你的 SEARCH 块后重试。"
        else:
            error_msg += "\n文件中没有找到相似内容，请检查文件路径和搜索文本是否正确。"
        return error_msg

    try:
        encoded = policy.validate_content(new_content)
    except PathPolicyError as error:
        return _policy_failure("编辑失败", error)
    error = _write_with_retries(
        policy,
        target,
        encoded,
        require_existing=True,
        allow_overwrite=True,
    )
    if error is not None:
        return _policy_failure("编辑失败", error)

    return f"文件已编辑: {file_path}"
