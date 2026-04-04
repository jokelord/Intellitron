"""Tests for {{SESSION_ID}} template variable in read_skill()."""

from unittest.mock import patch

import pytest

from backend.tools.skill_ops import read_skill
from backend.agent.agent_runner import current_session_context


@pytest.fixture(autouse=True)
def cleanup_context():
    yield
    current_session_context.clear()


@pytest.fixture
def mock_skill(tmp_path):
    """Create a temporary skill with {{SESSION_ID}} in instructions."""
    skill_dir = tmp_path / "test_skill"
    skill_dir.mkdir()
    (skill_dir / "metadata.yaml").write_text(
        "name: test_skill\ndescription: test\ntags: []\n"
    )
    (skill_dir / "instructions.md").write_text(
        "Run: script --session-id {{SESSION_ID}} --files {{SKILL_DIR}}/data"
    )
    return tmp_path


class TestSessionIdTemplate:
    def test_session_id_replaced_when_set(self, mock_skill):
        current_session_context.set("abc-123")
        with patch("backend.tools.skill_ops._get_skills_dir", return_value=mock_skill):
            result = read_skill("test_skill")
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
            result = read_skill("test_skill")
        skill_dir_str = str(mock_skill / "test_skill")
        assert skill_dir_str in result
        assert "{{SKILL_DIR}}" not in result
