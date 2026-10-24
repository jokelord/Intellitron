import pytest

from backend.compact.validation import validate_compact_summary
from backend.config import CompactConfig


def _cfg(**overrides):
    cfg = CompactConfig(
        validation_min_summary_chars=50,
        validation_min_summary_prompt_ratio=0.01,
        validation_require_summary_sections=True,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


VALID_SUMMARY = """
1. Primary Request and Intent:
   User asked for implementation planning.
2. Key Technical Concepts:
   Prefix cache and compact prompt shape.
3. Files and Code Sections:
   backend/compact/orchestrator.py.
4. Errors and fixes:
   None.
5. Problem Solving:
   Designed fallback validation.
6. All user messages:
   One planning request.
7. Pending Tasks:
   Implement plan.
8. Current Work:
   Awaiting implementation.
9. Optional Next Step:
   Run tests.
"""


def test_validate_rejects_empty_summary():
    result = validate_compact_summary("", prompt_chars=10000, config=_cfg())
    assert result.ok is False
    assert result.reason == "empty_summary"


def test_validate_rejects_routing_xml():
    result = validate_compact_summary("<action>switch_agent</action><target>coordinator_2</target>", 10000, _cfg())
    assert result.ok is False
    assert result.reason == "routing_or_tool_output"


def test_validate_rejects_tool_call_markers():
    result = validate_compact_summary('Calling tools:\n[{"type":"function"}]', 10000, _cfg())
    assert result.ok is False
    assert result.reason == "routing_or_tool_output"


@pytest.mark.parametrize(
    "summary",
    [
        "<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{}}\n<<END_TOOL_CALL>>",
        "Summary accidentally copied <<END_TOOL_CALL>> into prose.",
        "<<SWITCH_AGENT>> basic_agent",
        "<<<TOOL_CALL>>> read_file\n{}\n<<<END_TOOL_CALL>>>",
        "Summary accidentally copied <<<END_TOOL_CALL>>> into prose.",
        "<<<SWITCH_AGENT>>> basic_agent",
    ],
)
def test_validate_rejects_current_and_retired_control_markers(summary):
    result = validate_compact_summary(summary, prompt_chars=10000, config=_cfg())

    assert result.ok is False
    assert result.reason == "routing_or_tool_output"


def test_validate_rejects_too_short_summary():
    result = validate_compact_summary("short", prompt_chars=10000, config=_cfg())
    assert result.ok is False
    assert result.reason == "too_short"


def test_validate_rejects_missing_required_sections():
    result = validate_compact_summary("This is long enough but has no section skeleton." * 10, 1000, _cfg())
    assert result.ok is False
    assert result.reason == "missing_summary_sections"


def test_validate_accepts_required_sections():
    result = validate_compact_summary(VALID_SUMMARY, prompt_chars=1000, config=_cfg())
    assert result.ok is True
    assert result.reason is None
