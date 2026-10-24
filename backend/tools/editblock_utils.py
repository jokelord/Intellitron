"""
编辑块匹配工具 (Edit Block Utilities)

核心 SEARCH/REPLACE 匹配算法，用于精确修改文件内容。
支持 4 种匹配策略：精确匹配 → 空白归一化 → 省略号展开 → 模糊匹配。

---
本文件中的核心算法代码源自开源项目 Aider-AI/aider:
  - 仓库: https://github.com/Aider-AI/aider
  - 文件: aider/coders/editblock_coder.py
  - 协议: Apache License 2.0
已根据本项目需求去除 aider 内部依赖（InputOutput, Git, Coder 类等），
仅保留标准库依赖（difflib, re, math）。
---
"""

import math
import re
from difflib import SequenceMatcher


def prep(content):
    """预处理：确保内容以换行结尾，并拆分为行列表"""
    if content and not content.endswith("\n"):
        content += "\n"
    lines = content.splitlines(keepends=True)
    return content, lines


def perfect_replace(whole_lines, part_lines, replace_lines):
    """策略 1：精确字节匹配"""
    part_tup = tuple(part_lines)
    part_len = len(part_lines)

    for i in range(len(whole_lines) - part_len + 1):
        whole_tup = tuple(whole_lines[i : i + part_len])
        if part_tup == whole_tup:
            res = whole_lines[:i] + replace_lines + whole_lines[i + part_len :]
            return "".join(res)


def perfect_or_whitespace(whole_lines, part_lines, replace_lines):
    """依次尝试精确匹配和空白归一化匹配"""
    res = perfect_replace(whole_lines, part_lines, replace_lines)
    if res:
        return res

    res = replace_part_with_missing_leading_whitespace(
        whole_lines, part_lines, replace_lines
    )
    if res:
        return res


def match_but_for_leading_whitespace(whole_lines, part_lines):
    """检查两组行是否仅前导空白不同，返回统一的前导空白偏移"""
    num = len(whole_lines)

    if not all(whole_lines[i].lstrip() == part_lines[i].lstrip() for i in range(num)):
        return

    add = set(
        whole_lines[i][: len(whole_lines[i]) - len(part_lines[i])]
        for i in range(num)
        if whole_lines[i].strip()
    )

    if len(add) != 1:
        return

    return add.pop()


def replace_part_with_missing_leading_whitespace(
    whole_lines, part_lines, replace_lines
):
    """策略 2：修复 LLM 常见的前导空白偏差"""
    leading = [len(p) - len(p.lstrip()) for p in part_lines if p.strip()] + [
        len(p) - len(p.lstrip()) for p in replace_lines if p.strip()
    ]

    if leading and min(leading):
        num_leading = min(leading)
        part_lines = [p[num_leading:] if p.strip() else p for p in part_lines]
        replace_lines = [p[num_leading:] if p.strip() else p for p in replace_lines]

    num_part_lines = len(part_lines)

    for i in range(len(whole_lines) - num_part_lines + 1):
        add_leading = match_but_for_leading_whitespace(
            whole_lines[i : i + num_part_lines], part_lines
        )

        if add_leading is None:
            continue

        adjusted_replace = [
            add_leading + rline if rline.strip() else rline for rline in replace_lines
        ]
        whole_lines = (
            whole_lines[:i] + adjusted_replace + whole_lines[i + num_part_lines :]
        )
        return "".join(whole_lines)

    return None


def try_dotdotdots(whole, part, replace):
    """策略 3：处理 ... 省略行，按片段逐一替换"""
    dots_re = re.compile(r"(^\s*\.\.\.\n)", re.MULTILINE | re.DOTALL)

    part_pieces = re.split(dots_re, part)
    replace_pieces = re.split(dots_re, replace)

    if len(part_pieces) != len(replace_pieces):
        raise ValueError("Unpaired ... in SEARCH/REPLACE block")

    if len(part_pieces) == 1:
        return

    all_dots_match = all(
        part_pieces[i] == replace_pieces[i] for i in range(1, len(part_pieces), 2)
    )

    if not all_dots_match:
        raise ValueError("Unmatched ... in SEARCH/REPLACE block")

    part_pieces = [part_pieces[i] for i in range(0, len(part_pieces), 2)]
    replace_pieces = [replace_pieces[i] for i in range(0, len(replace_pieces), 2)]

    pairs = zip(part_pieces, replace_pieces)
    for part_piece, replace_piece in pairs:
        if not part_piece and not replace_piece:
            continue

        if not part_piece and replace_piece:
            if not whole.endswith("\n"):
                whole += "\n"
            whole += replace_piece
            continue

        if whole.count(part_piece) == 0:
            raise ValueError(f"SEARCH block with ... not found in file")
        if whole.count(part_piece) > 1:
            raise ValueError(f"SEARCH block with ... matched multiple times")

        whole = whole.replace(part_piece, replace_piece, 1)

    return whole


def replace_closest_edit_distance(whole_lines, part, part_lines, replace_lines):
    """策略 4：基于 SequenceMatcher 的模糊匹配（相似度阈值 0.8，长度容忍 ±10%）"""
    similarity_thresh = 0.8

    max_similarity = 0
    most_similar_chunk_start = -1
    most_similar_chunk_end = -1

    scale = 0.1
    min_len = math.floor(len(part_lines) * (1 - scale))
    max_len = math.ceil(len(part_lines) * (1 + scale))

    for length in range(min_len, max_len):
        for i in range(len(whole_lines) - length + 1):
            chunk = whole_lines[i : i + length]
            chunk = "".join(chunk)

            similarity = SequenceMatcher(None, chunk, part).ratio()

            if similarity > max_similarity and similarity:
                max_similarity = similarity
                most_similar_chunk_start = i
                most_similar_chunk_end = i + length

    if max_similarity < similarity_thresh:
        return

    modified_whole = (
        whole_lines[:most_similar_chunk_start]
        + replace_lines
        + whole_lines[most_similar_chunk_end:]
    )
    modified_whole = "".join(modified_whole)

    return modified_whole


def replace_most_similar_chunk(whole, part, replace):
    """
    主入口：尽力在 whole 中找到 part 并替换为 replace。

    依次尝试 4 种策略：
    1. 精确匹配（含空白归一化）
    2. 跳过首行空行（LLM 常见问题）
    3. 省略号 (...) 展开
    4. 模糊匹配（SequenceMatcher，阈值 0.8）

    Returns:
        替换后的完整内容字符串，或 None（全部策略失败时）
    """
    whole, whole_lines = prep(whole)
    part, part_lines = prep(part)
    replace, replace_lines = prep(replace)

    # 策略 1: 精确匹配 + 空白归一化
    res = perfect_or_whitespace(whole_lines, part_lines, replace_lines)
    if res:
        return res

    # 策略 2: 跳过 LLM 偶尔添加的首行空行
    if len(part_lines) > 2 and not part_lines[0].strip():
        skip_blank_line_part_lines = part_lines[1:]
        res = perfect_or_whitespace(
            whole_lines, skip_blank_line_part_lines, replace_lines
        )
        if res:
            return res

    # 策略 3: 处理 ... 省略行
    try:
        res = try_dotdotdots(whole, part, replace)
        if res:
            return res
    except ValueError:
        pass

    # 策略 4: 模糊匹配
    res = replace_closest_edit_distance(whole_lines, part, part_lines, replace_lines)
    if res:
        return res

    return None


def find_similar_lines(search_text, content, threshold=0.6):
    """
    当匹配失败时，找到文件中最相似的代码片段用于错误提示。

    Args:
        search_text: 搜索块文本
        content: 文件完整内容
        threshold: 最低相似度阈值

    Returns:
        最相似片段的字符串（含 ±5 行上下文），或空字符串
    """
    search_lines = search_text.splitlines()
    content_lines = content.splitlines()

    if not search_lines or not content_lines:
        return ""

    best_ratio = 0
    best_match_i = 0

    for i in range(len(content_lines) - len(search_lines) + 1):
        chunk = content_lines[i : i + len(search_lines)]
        ratio = SequenceMatcher(None, search_lines, chunk).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match_i = i

    if best_ratio < threshold:
        return ""

    context = 5
    start = max(0, best_match_i - context)
    end = min(len(content_lines), best_match_i + len(search_lines) + context)

    return "\n".join(content_lines[start:end])
