#!/usr/bin/env python3
"""
collect_texts_to_csv.py — 将目录下的 .log / .txt / .md 文件整理为一份 CSV 汇总表。

用法:
    python3 collect_texts_to_csv.py <目录路径>

输出:
    在该目录下生成 collected_texts_YYYYMMDD_HHMMSS.csv，包含以下列:
    No. | file_name | create_time | modify_time | content
"""

import csv
import os
import platform
import sys
from datetime import datetime
from pathlib import Path

SUPPORTED_EXTENSIONS = {".log", ".txt", ".md"}


def get_creation_time(filepath: Path) -> str:
    """获取文件创建时间（跨平台）。

    Linux ext4 (kernel >= 4.11) 支持 st_birthtime；
    若不可用则回退到 st_ctime（metadata change time）。
    """
    stat = filepath.stat()
    if platform.system() == "Windows":
        ts = stat.st_ctime
    else:
        # Linux/macOS: 优先 st_birthtime（真正的创建时间）
        ts = getattr(stat, "st_birthtime", None)
        if ts is None:
            ts = stat.st_ctime  # fallback: metadata change time
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def get_modify_time(filepath: Path) -> str:
    """获取文件最后修改时间。"""
    ts = filepath.stat().st_mtime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def collect_files(directory: Path) -> list[Path]:
    """收集目录下所有符合扩展名的文件（非递归），按文件名排序。"""
    files = []
    for entry in sorted(directory.iterdir()):
        if entry.is_file() and entry.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(entry)
    return files


def read_file_content(filepath: Path) -> str:
    """安全读取文件文本内容，处理编码错误。"""
    try:
        return filepath.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return filepath.read_text(encoding="gbk")
        except Exception:
            return "(无法解码文件内容)"
    except Exception as e:
        return f"(读取失败: {e})"


def main():
    if len(sys.argv) != 2:
        print(f"用法: {sys.argv[0]} <目录路径>", file=sys.stderr)
        sys.exit(1)

    directory = Path(sys.argv[1]).resolve()
    if not directory.is_dir():
        print(f"错误: '{directory}' 不是有效目录", file=sys.stderr)
        sys.exit(1)

    files = collect_files(directory)
    if not files:
        print(f"目录 '{directory}' 中未找到 .log / .txt / .md 文件")
        sys.exit(0)

    # 生成输出文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = directory / f"collected_texts_{timestamp}.csv"

    with open(output_path, "w", encoding="utf-8-sig", newline="") as csvfile:
        writer = csv.writer(csvfile, quoting=csv.QUOTE_ALL)
        writer.writerow(["No.", "file_name", "create_time", "modify_time", "content"])

        for idx, filepath in enumerate(files, start=1):
            writer.writerow(
                [
                    idx,
                    filepath.name,
                    get_creation_time(filepath),
                    get_modify_time(filepath),
                    read_file_content(filepath),
                ]
            )

    print(f"完成: 共整理 {len(files)} 个文件 → {output_path}")


if __name__ == "__main__":
    main()
