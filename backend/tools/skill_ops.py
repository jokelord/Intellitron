"""
知识库工具 (Skill Operations)

提供静态知识库（Skills）的列表与读取功能。
Skills 存储在 backend/skills/ 目录下，每个 skill 包含 metadata.yaml 和 instructions.md。
"""

import os
from pathlib import Path

import yaml


def _get_skills_dir() -> Path:
    """获取 skills 目录路径"""
    return Path(__file__).resolve().parent.parent / "skills"


def list_skills() -> str:
    """
    列出所有可用的 Skills（标准作业程序）。

    遍历 backend/skills/ 目录，解析每个子目录的 metadata.yaml，
    返回包含 name/description/tags 的 JSON 格式列表。

    Returns:
        JSON 格式的 skill 列表字符串
    """
    import json

    skills_dir = _get_skills_dir()
    if not skills_dir.exists():
        return json.dumps(
            {"skills": [], "message": "skills 目录不存在"}, ensure_ascii=False
        )

    skills = []
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        metadata_path = entry / "metadata.yaml"
        if not metadata_path.exists():
            continue
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata = yaml.safe_load(f) or {}
            skills.append(
                {
                    "name": metadata.get("name", entry.name),
                    "description": metadata.get("description", ""),
                    "tags": metadata.get("tags", []),
                }
            )
        except Exception:
            skills.append(
                {
                    "name": entry.name,
                    "description": "(metadata.yaml 解析失败)",
                    "tags": [],
                }
            )

    return json.dumps({"skills": skills}, ensure_ascii=False, indent=2)


def read_skill(skill_name: str) -> str:
    """
    读取指定 Skill 的详细指令内容。

    Args:
        skill_name: Skill 名称（对应 backend/skills/ 下的子目录名）

    Returns:
        instructions.md 的完整文本内容，或错误信息
    """
    skills_dir = _get_skills_dir()
    skill_dir = skills_dir / skill_name

    if not skill_dir.exists():
        available = (
            [d.name for d in skills_dir.iterdir() if d.is_dir()]
            if skills_dir.exists()
            else []
        )
        return f"Skill '{skill_name}' 不存在。可用的 Skills: {available}"

    instructions_path = skill_dir / "instructions.md"
    if not instructions_path.exists():
        return f"Skill '{skill_name}' 目录存在但缺少 instructions.md 文件"

    with open(instructions_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 将 {{SKILL_DIR}} 占位符替换为实际的 skill 目录绝对路径
    content = content.replace("{{SKILL_DIR}}", str(skill_dir))

    # 将 {{SESSION_ID}} 占位符替换为当前会话 ID
    try:
        from backend.agent.agent_runner import current_session_context

        _session_id = current_session_context.get() or ""
        content = content.replace("{{SESSION_ID}}", _session_id)
    except Exception:
        content = content.replace("{{SESSION_ID}}", "")

    return content
