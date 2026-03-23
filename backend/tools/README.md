# 工具层 (Tools)
## 1. 创建 backend/tools/editblock_utils.py
从 editblock_coder.py 复制 6 个核心函数（attribution 注释置于文件头部）：replace_most_similar_chunk、perfect_replace、perfect_or_whitespace、replace_part_with_missing_leading_whitespace、try_dotdotdots、replace_closest_edit_distance、prep。去除 aider 内部依赖，仅保留标准库（difflib、re、math）。

## 2. 创建 backend/tools/file_ops.py
read_file(path, start_line, end_line) — with open 读取，每行前加行号
write_file(path, content) — 覆盖写入，指数退避重试（借鉴 aider io.py write_text）
edit_file(path, search, replace) — 调用 editblock_utils.replace_most_similar_chunk，匹配失败时返回含最相似代码片段的错误提示
创建 backend/tools/sys_ops.py

exec_command(command, timeout=60) — subprocess.run shell=True
**黑名单正则覆盖**：rm\s+-rf\s+/、sudo\s+、mkfs、dd\s+.*of=/dev/、:(){ :|:& };:、shutdown/reboot/poweroff、chmod 777 /

## 3. 创建 backend/tools/skill_ops.py
list_skills() — 遍历 backend/skills/, 用 pyyaml 解析所有 metadata.yaml
read_skill(skill_name) — 读取对应 instructions.md

## 4. 创建 backend/skills/git_workflow/ — 作为样本知识库（metadata.yaml + instructions.md）

