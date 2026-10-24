---
name: collect-texts-to-csv
description: Collects .log, .txt, and .md files from a specified directory into one CSV table with file name, creation time, modification time, and content. Use when the user asks to organize text files into CSV.
---

# 文本文件整理为 CSV 汇总表

## 适用场景

当用户要求将某个目录下的文本文件（.log、.txt、.md）整理、合并、汇总为结构化表格时，使用本技能。

## 调用

通过 `exec_command` 直接执行，不套用 Bash 包装器：

```bash
python3 {{SKILL_DIR}}/collect_texts_to_csv.py "<目标目录的绝对路径>"
```

- 参数只有一个：目标目录的绝对路径
- 脚本会自动扫描该目录下的 `.log`、`.txt`、`.md` 文件（非递归，不进入子目录）
- 输出 CSV 文件自动保存在同一目录下，文件名格式为 `collected_texts_YYYYMMDD_HHMMSS.csv`

CSV 列为 `No.`、`file_name`、`create_time`、`modify_time`、`content`。

## 注意事项

- 脚本先用 UTF-8 读取；遇到解码错误时再尝试 GBK
- CSV 使用 `utf-8-sig` 编码（带 BOM），可直接用 Excel 打开而不乱码
- 文件按文件名排序
- `create_time` 在无法取得文件创建时间时使用元数据变更时间，不能保证是真正的创建时间
- 没有匹配文件时不生成 CSV，即使命令正常退出也应报告未找到文件
- 无法解码或读取的文件会在 `content` 中写入错误说明；不要将其当作成功读取的正文
