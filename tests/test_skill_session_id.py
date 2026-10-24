"""Tests for SKILL.md frontmatter discovery and read_skill templates."""

import json
from unittest.mock import patch

import pytest

from backend.tools.skill_ops import list_skills, read_skill
from backend.agent.agent_runner import current_session_context


@pytest.fixture(autouse=True)
def cleanup_context():
    yield
    current_session_context.clear()


@pytest.fixture
def mock_skill(tmp_path):
    """Create a temporary skill with {{SESSION_ID}} in SKILL.md."""
    skill_dir = tmp_path / "test_skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: test-skill\n"
        "description: Test skill for session template substitution.\n"
        "---\n\n"
        "# Test Skill\n\n"
        "Run: script --session-id {{SESSION_ID}} --files {{SKILL_DIR}}/data",
        encoding="utf-8",
    )
    return tmp_path


class TestSessionIdTemplate:
    def test_session_id_replaced_when_set(self, mock_skill):
        current_session_context.set("abc-123")
        with patch("backend.tools.skill_ops._get_skills_dir", return_value=mock_skill):
            result = read_skill("test-skill")
        assert "abc-123" in result
        assert "{{SESSION_ID}}" not in result

    def test_session_id_empty_when_not_set(self, mock_skill):
        # No session context set
        with patch("backend.tools.skill_ops._get_skills_dir", return_value=mock_skill):
            result = read_skill("test_skill")
        assert "{{SESSION_ID}}" not in result
        assert "--session-id " in result  # replaced with empty string

    def test_skill_dir_still_replaced(self, mock_skill):
        current_session_context.set("xyz-789")
        with patch("backend.tools.skill_ops._get_skills_dir", return_value=mock_skill):
            result = read_skill("test-skill")
        skill_dir_str = str(mock_skill / "test_skill")
        assert skill_dir_str in result
        assert "{{SKILL_DIR}}" not in result

    def test_read_skill_resolves_directory_name(self, tmp_path):
        skill_dir = tmp_path / "playwright-cli"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: playwright-browser-skill\ndescription: browser test\n---\n\n"
            "# Browser Skill\n\nbrowser tool doc",
            encoding="utf-8",
        )

        with patch("backend.tools.skill_ops._get_skills_dir", return_value=tmp_path):
            result = read_skill("playwright-cli")

        assert "browser tool doc" in result

    def test_read_skill_resolves_frontmatter_name_alias(self, tmp_path):
        skill_dir = tmp_path / "playwright-cli"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: playwright-browser-skill\ndescription: browser test\n---\n\n"
            "# Browser Skill\n\nbrowser tool doc",
            encoding="utf-8",
        )

        with patch("backend.tools.skill_ops._get_skills_dir", return_value=tmp_path):
            result = read_skill("playwright-browser-skill")

        assert "browser tool doc" in result

    def test_read_skill_resolves_underscore_alias_for_hyphenated_name(self, mock_skill):
        with patch("backend.tools.skill_ops._get_skills_dir", return_value=mock_skill):
            result = read_skill("test_skill")

        assert "# Test Skill" in result

    def test_list_skills_returns_frontmatter_name_for_progressive_disclosure(
        self, tmp_path
    ):
        skill_dir = tmp_path / "playwright-cli"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: playwright-browser-skill\ndescription: browser test\n---\n\n"
            "# Browser Skill\n\nbody text that must stay out of list output",
            encoding="utf-8",
        )

        with patch("backend.tools.skill_ops._get_skills_dir", return_value=tmp_path):
            result = list_skills()

        payload = json.loads(result)
        assert payload["skills"] == [
            {
                "name": "playwright-browser-skill",
                "description": "browser test",
                "path": "playwright-cli/SKILL.md",
            }
        ]
        assert "body text that must stay out of list output" not in result

    def test_list_skills_ignores_hidden_directories(self, tmp_path):
        hidden_dir = tmp_path / ".hidden-skill"
        hidden_dir.mkdir()
        (hidden_dir / "SKILL.md").write_text(
            "---\nname: hidden-skill\ndescription: hidden\n---\n\n# Hidden",
            encoding="utf-8",
        )
        visible_dir = tmp_path / "visible-skill"
        visible_dir.mkdir()
        (visible_dir / "SKILL.md").write_text(
            "---\nname: visible-skill\ndescription: visible\n---\n\n# Visible",
            encoding="utf-8",
        )

        with patch("backend.tools.skill_ops._get_skills_dir", return_value=tmp_path):
            result = list_skills()

        payload = json.loads(result)
        assert [skill["name"] for skill in payload["skills"]] == ["visible-skill"]

    def test_list_skills_reports_malformed_frontmatter_without_crashing(self, tmp_path):
        skill_dir = tmp_path / "bad-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: Bad Skill!\ndescription: broken\n---\n\n# Bad",
            encoding="utf-8",
        )

        with patch("backend.tools.skill_ops._get_skills_dir", return_value=tmp_path):
            result = list_skills()

        payload = json.loads(result)
        assert payload["skills"][0]["name"] == "bad-skill"
        assert "invalid" in payload["skills"][0]["error"]

    def test_read_skill_reports_missing_skill_md(self, tmp_path):
        skill_dir = tmp_path / "empty-skill"
        skill_dir.mkdir()

        with patch("backend.tools.skill_ops._get_skills_dir", return_value=tmp_path):
            result = read_skill("empty-skill")

        assert "缺少 SKILL.md" in result
