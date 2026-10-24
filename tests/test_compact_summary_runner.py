"""Tests for format_compact_summary post-processing."""
from backend.compact.summary_runner import format_compact_summary


def test_strips_analysis_block():
    raw = "<analysis>thinking...</analysis>\n<summary>Real content</summary>"
    out = format_compact_summary(raw)
    assert "<analysis>" not in out
    assert "thinking" not in out
    assert "Real content" in out


def test_unwraps_summary_tag_to_summary_label():
    raw = "<summary>Section 1: ...</summary>"
    out = format_compact_summary(raw)
    assert out.startswith("Summary:")


def test_empty_after_strip_returns_empty_string():
    raw = "<analysis>only thinking, no summary</analysis>"
    out = format_compact_summary(raw)
    assert out == ""


def test_plain_text_passes_through():
    raw = "Just plain summary text without tags."
    out = format_compact_summary(raw)
    assert "plain summary" in out


def test_empty_input_returns_empty():
    assert format_compact_summary("") == ""


def test_case_insensitive_tags():
    raw = "<ANALYSIS>think</ANALYSIS><Summary>Hello</Summary>"
    out = format_compact_summary(raw)
    assert "think" not in out
    assert "Hello" in out
    assert out.startswith("Summary:")


def test_removes_internal_compact_control_lines_from_summary():
    raw = """<summary>
Primary Request and Intent: Debug why LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1 appears in compact summaries.

All user messages:
"Read /tmp/book.txt"
"[[__LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1__]] Summarize the prior messages"
"[[LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1]] ... [Internal control message - excluded]"
"Next real request"

Pending Tasks: None.
</summary>"""

    out = format_compact_summary(raw)

    assert "Internal control message - excluded" not in out
    assert "Debug why LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1 appears" in out
    assert "Read /tmp/book.txt" in out
    assert "Next real request" in out
    assert "All user messages" in out


def test_removes_numbered_and_unwrapped_internal_compact_control_lines_from_all_user_messages_only():
    raw = """<summary>
Primary Request and Intent: Explain the LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1 mechanism.

All user messages:
1. Read AGENTS.md
2. LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1 Summarize the prior messages
3. [[__LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1__]] Summarize again
4. Continue the task

Pending Tasks: None.
</summary>"""

    out = format_compact_summary(raw)

    assert "Explain the LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1 mechanism" in out
    assert "Read AGENTS.md" in out
    assert "Continue the task" in out
    assert "Summarize the prior messages" not in out
    assert "Summarize again" not in out


def test_removes_speaker_labelled_internal_compact_control_lines_from_all_user_messages_only():
    raw = """<summary>
Primary Request and Intent: Debug why User: [[__LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1__]] leaked.

Files and Code Sections:
- backend/compact/summary_runner.py handles User: [[__LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1__]] discussion.

All user messages:
- User: [[__LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1__]] Summarize prior messages
2. user: [[LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1]] stale compact request
3. "User: LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1 stale unwrapped request"
4. Real user request

Pending Tasks: None.
</summary>"""

    out = format_compact_summary(raw)

    assert "Real user request" in out
    assert "Summarize prior messages" not in out
    assert "stale compact request" not in out
    assert "stale unwrapped request" not in out
    assert "Primary Request and Intent: Debug why User: [[__LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1__]] leaked." in out
    assert "summary_runner.py handles User: [[__LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1__]] discussion." in out


def test_removes_internal_system_instruction_placeholder_from_all_user_messages_only():
    raw = """<summary>
Primary Request and Intent: Prevent summaries from mentioning Internal System Instruction placeholders.

Files and Code Sections:
- backend/compact/summary_runner.py discusses Internal System Instruction cleanup.

All user messages:
User: "读取 ~/Documents/sci-fi/76《聪明飞船》作者 基尔·布雷切夫 然后总结下 注意文件名中有空格"
Internal System Instruction: "This message is an internal server/system instruction... Do only context compaction..." (Excluded per instructions).
User: "Continue the real task"

Pending Tasks: None.
</summary>"""

    out = format_compact_summary(raw)

    assert "读取 ~/Documents/sci-fi/76《聪明飞船》作者" in out
    assert "Continue the real task" in out
    assert "Internal System Instruction cleanup" in out
    assert "Internal System Instruction: \"This message is an internal server/system instruction" not in out
    assert "Excluded per instructions" not in out


def test_removes_system_instruction_internal_placeholder_from_all_user_messages_only():
    raw = """<summary>
Primary Request and Intent: Summarize local sci-fi files.

Files and Code Sections:
- This section may discuss System Instruction (Internal) cleanup behavior.

All user messages:
- User: "读取 `~/Documents/sci-fi/139《2061：太空漫游》[英] 阿瑟·克拉克.txt` 然后总结下 注意文件名中有空格"
- System Instruction (Internal): Context compaction instruction (excluded per instructions).
- User: "再找同目录下的另外一篇小说，随机选一篇小说，再总结。"

Pending Tasks: None.
</summary>"""

    out = format_compact_summary(raw)

    assert "2061：太空漫游" in out
    assert "再找同目录下" in out
    assert "System Instruction (Internal) cleanup behavior" in out
    assert "System Instruction (Internal): Context compaction instruction" not in out
    assert "excluded per instructions" not in out.lower()
