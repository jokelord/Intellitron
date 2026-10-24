"""
知识库工具 (Skill Operations)

提供静态知识库（Skills）的列表与读取功能。
Skills 存储在 backend/skills/ 目录下，每个 skill 使用 Anthropic 风格的 SKILL.md：
文件头部 YAML frontmatter 用于渐进式披露，正文在 read_skill() 时按需读取。
"""

import json
import re
from pathlib import Path
from typing import Any

import yaml


_SKILL_NAME_RE = re.compile(r"^[a-z0-9-]{1,64}$")
_XML_TAG_RE = re.compile(r"<[^>]+>")
_RESERVED_NAME_PARTS = ("anthropic", "claude")


def _get_skills_dir() -> Path:
    """获取 skills 目录路径"""
    return Path(__file__).resolve().parent.parent / "skills"


def _alias_values(value: str) -> set[str]:
    """Return equivalent lookup aliases for hyphen/underscore migration."""
    return {value, value.replace("_", "-"), value.replace("-", "_")}


def _read_frontmatter_block(skill_path: Path) -> str:
    """Read only the leading YAML frontmatter block from SKILL.md."""
    lines: list[str] = []
    with open(skill_path, "r", encoding="utf-8") as f:
        first_line = f.readline()
        if first_line.strip() != "---":
            raise ValueError("missing YAML frontmatter")
        lines.append(first_line)
        for line in f:
            lines.append(line)
            if line.strip() == "---":
                return "".join(lines)
    raise ValueError("unterminated YAML frontmatter")


def _extract_frontmatter(frontmatter_block: str) -> dict[str, Any]:
    """Parse a leading YAML frontmatter block without reading the skill body."""
    lines = frontmatter_block.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        raise ValueError("missing YAML frontmatter")

    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
    if end_index is None:
        raise ValueError("unterminated YAML frontmatter")

    metadata = yaml.safe_load("\n".join(lines[1:end_index])) or {}
    if not isinstance(metadata, dict):
        raise ValueError("YAML frontmatter must be a mapping")
    return metadata


def _validate_skill_metadata(metadata: dict[str, Any]) -> tuple[str, str]:
    """Validate Anthropic-compatible skill metadata."""
    name = metadata.get("name")
    description = metadata.get("description")

    if not isinstance(name, str) or not name:
        raise ValueError("invalid skill name: missing or non-string")
    if not _SKILL_NAME_RE.fullmatch(name):
        raise ValueError(
            "invalid skill name: use lowercase letters, numbers, and hyphens only"
        )
    if any(reserved in name for reserved in _RESERVED_NAME_PARTS):
        raise ValueError("invalid skill name: reserved word")
    if _XML_TAG_RE.search(name):
        raise ValueError("invalid skill name: XML tags are not allowed")

    if not isinstance(description, str) or not description.strip():
        raise ValueError("invalid skill description: missing or non-string")
    if len(description) > 1024:
        raise ValueError("invalid skill description: longer than 1024 characters")
    if _XML_TAG_RE.search(description):
        raise ValueError("invalid skill description: XML tags are not allowed")

    return name, description


def _load_skill_record(skills_dir: Path, skill_dir: Path) -> dict[str, Any] | None:
    """Load one direct skill directory's frontmatter discovery record."""
    if not skill_dir.is_dir() or skill_dir.name.startswith("."):
        return None

    skill_path = skill_dir / "SKILL.md"
    relative_path = str(skill_path.relative_to(skills_dir))
    if not skill_path.exists():
        return {
            "name": skill_dir.name,
            "description": "",
            "path": relative_path,
            "directory": skill_dir.name,
            "error": "missing SKILL.md",
        }

    try:
        metadata = _extract_frontmatter(_read_frontmatter_block(skill_path))
        name, description = _validate_skill_metadata(metadata)
        return {
            "name": name,
            "description": description,
            "path": relative_path,
            "directory": skill_dir.name,
            "error": None,
        }
    except Exception as exc:
        return {
            "name": skill_dir.name,
            "description": "",
            "path": relative_path,
            "directory": skill_dir.name,
            "error": str(exc),
        }


def _iter_skill_records(skills_dir: Path) -> list[dict[str, Any]]:
    """Return discovery records for direct non-hidden skill directories."""
    if not skills_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    for entry in sorted(skills_dir.iterdir()):
        record = _load_skill_record(skills_dir, entry)
        if record is not None:
            records.append(record)
    return records


def _public_skill_record(record: dict[str, Any]) -> dict[str, Any]:
    """Remove internal fields before returning a skill to the agent."""
    public = {
        "name": record["name"],
        "description": record["description"],
        "path": record["path"],
    }
    if record.get("error"):
        public["error"] = record["error"]
    return public


def list_skills() -> str:
    """
    列出所有可用的 Skills（标准作业程序）。

    遍历 backend/skills/ 目录，解析每个非隐藏子目录 SKILL.md 的 YAML
    frontmatter，返回包含 name/description/path 的 JSON 格式列表。

    Returns:
        JSON 格式的 skill 列表字符串
    """
    skills_dir = _get_skills_dir()
    if not skills_dir.exists():
        return json.dumps(
            {"skills": [], "message": "skills 目录不存在"}, ensure_ascii=False
        )

    skills = [_public_skill_record(record) for record in _iter_skill_records(skills_dir)]
    return json.dumps({"skills": skills}, ensure_ascii=False, indent=2)


def read_skill(skill_name: str) -> str:
    """
    读取指定 Skill 的详细指令内容。

    Args:
        skill_name: Skill 名称（frontmatter name 或 backend/skills/ 下的子目录名）

    Returns:
        SKILL.md 的完整文本内容，或错误信息
    """
    skills_dir = _get_skills_dir()
    requested_aliases = _alias_values(skill_name)
    skill_dir = None
    records = _iter_skill_records(skills_dir)

    for record in records:
        record_aliases = _alias_values(record["directory"]) | _alias_values(
            record["name"]
        )
        if requested_aliases & record_aliases:
            skill_dir = skills_dir / record["directory"]
            break

    if skill_dir is None:
        available = sorted(
            {
                alias
                for record in records
                for alias in (record["name"], record["directory"])
            }
        )
        return f"Skill '{skill_name}' 不存在。可用的 Skills: {available}"

    skill_path = skill_dir / "SKILL.md"
    if not skill_path.exists():
        return f"Skill '{skill_name}' 目录存在但缺少 SKILL.md 文件"

    with open(skill_path, "r", encoding="utf-8") as f:
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
