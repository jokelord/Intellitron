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
import time

from .editblock_utils import find_similar_lines, replace_most_similar_chunk


def read_file(
    file_path: str, start_line: int | None = None, end_line: int | None = None
) -> str:
    """
    读取文件内容，每行前附行号。

    【重要】对于大文件，必须始终提供 start_line 和 end_line 以限定读取范围，
    避免一次性读取整个文件导致上下文溢出。建议每次读取不超过 200 行。
    如果不确定行号，先用 start_line=1, end_line=50 预览文件头部，
    再根据返回的总行数信息决定后续读取范围。

    Args:
        file_path: 文件的绝对或相对路径
        start_line: 起始行号（1-based，含），None 表示从第一行开始。建议始终提供。
        end_line: 结束行号（1-based，含），None 表示到最后一行。建议始终提供。

    Returns:
        带行号前缀的文件内容字符串，包含范围和总行数信息
    """
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    total = len(lines)
    start = (start_line - 1) if start_line and start_line >= 1 else 0
    end = end_line if end_line and end_line <= total else total

    numbered_lines = []
    for i in range(start, end):
        numbered_lines.append(f"{i + 1:4d} | {lines[i]}")

    result = "".join(numbered_lines)
    if start_line or end_line:
        result = f"[显示第 {start + 1}-{end} 行，共 {total} 行]\n" + result
    return result


def write_file(file_path: str, content: str) -> str:
    """
    写入文件（覆盖），自动创建父目录。
    使用指数退避重试应对文件锁。

    Args:
        file_path: 目标文件路径
        content: 要写入的内容

    Returns:
        操作结果描述
    """
    max_retries = 5
    initial_delay = 0.1

    # 确保父目录存在
    parent = os.path.dirname(file_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    delay = initial_delay
    for attempt in range(max_retries):
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"文件已写入: {file_path} ({len(content)} 字节)"
        except PermissionError:
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
            else:
                raise

    return f"文件写入失败: {file_path}"


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
    精确编辑文件：在文件中查找 search_text 并替换为 replace_text。
    使用 4 层匹配策略（精确 → 空白归一化 → 省略号 → 模糊匹配）。

    如果 search_text 含有字面转义序列（如 ``\\n``），会自动尝试
    将其还原为真实换行后再匹配，作为回退策略。

    Args:
        file_path: 要编辑的文件路径
        search_text: 要查找的代码片段（SEARCH 块）
        replace_text: 替换后的代码片段（REPLACE 块）

    Returns:
        操作结果描述（成功或包含最相似片段的错误信息）
    """
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

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

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    return f"文件已编辑: {file_path}"
