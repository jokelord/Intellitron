"""Render v2 builder output through the real Qwen3 chat template.

Per spec §6.1 I9, §9.1 [V2], and 踩坑记录3 §20:
  - v2 output must render through the real Qwen3 chat template without raising
    the 'No user query found in messages.' exception from the multi_step_tool check
    (chat_template.jinja:67-80).
  - The rendered output must contain at least one <|im_start|>user / <|im_end|>\n pair
    whose content is NOT a <tool_response> envelope.

This is the test that would catch a regression to a shape that breaks the Qwen3
chat template's multi_step_tool check.
"""
from __future__ import annotations

from pathlib import Path

import jinja2
import pytest
import yaml

from backend.compact.prompt_builder import build_compact_messages_v2
from backend.config import CompactionPromptsConfig
from backend.llm_service import Message

CHAT_TEMPLATE_PATH = Path("~/Project/LLM-weight/Qwen3___5-35B-A3B-GPTQ-Int4/chat_template.jinja").expanduser()
YAML_PATH = Path(__file__).resolve().parents[1] / "config" / "config-agent-prompts.yaml"


@pytest.fixture
def prompts() -> CompactionPromptsConfig:
    with open(YAML_PATH) as f:
        cfg = yaml.safe_load(f).get("compaction", {})
    return CompactionPromptsConfig(
        no_tools_preamble=cfg.get("no_tools_preamble", ""),
        main_prompt=cfg.get("main_prompt", ""),
        custom_instructions_header=cfg.get("custom_instructions_header", ""),
        no_tools_trailer=cfg.get("no_tools_trailer", ""),
        summary_envelope_preface=cfg.get("summary_envelope_preface", ""),
        summary_envelope_suffix=cfg.get("summary_envelope_suffix", ""),
        compact_user_instruction=cfg.get("compact_user_instruction", ""),
    )


def _render_chat_template(messages):
    """Render messages through the real Qwen3 chat template, converting any AssertionError
    raised by raise_exception to a normal exception with a descriptive prefix."""
    if not CHAT_TEMPLATE_PATH.exists():
        pytest.skip(f"Qwen3 chat template not found at {CHAT_TEMPLATE_PATH}")

    template_text = CHAT_TEMPLATE_PATH.read_text()
    env = jinja2.Environment(keep_trailing_newline=True)
    env.globals["raise_exception"] = lambda msg: (_ for _ in ()).throw(
        AssertionError(f"[chat_template raise_exception] {msg}")
    )
    tpl = env.from_string(template_text)

    # Convert backend Message to the dict shape the template expects
    rendered_messages = []
    for m in messages:
        if isinstance(m, Message):
            rendered_messages.append({"role": m.role, "content": m.content})
        else:
            rendered_messages.append({"role": m["role"], "content": m["content"]})
    return tpl.render(messages=rendered_messages, add_generation_prompt=True, tools=None)


def test_v2_passes_qwen3_chat_template(prompts: CompactionPromptsConfig) -> None:
    body = [
        Message(role="system", content="SYS"),
        Message(role="user", content="A 说：xxx"),
        Message(role="assistant", content="回：yyy"),
        Message(role="user", content="B 说：zzz"),
    ]
    out = build_compact_messages_v2(body, prompts)
    rendered = _render_chat_template(out)
    # The rendered output must contain <|im_start|>user and <|im_end|>\n at least twice
    # (once for the v2 compact-instr user, once for the body).
    assert rendered.count("<|im_start|>user") >= 2
    assert rendered.count("<|im_end|>") >= 4  # system + 2 body user + 1 compact-instr user + at least 1 assistant (actually 2)
    # And the rendered output must end with the assistant generation prompt.
    # Qwen3's chat template appends <think> to the assistant generation prompt
    # (thinking mode); the literal suffix is "<|im_start|>assistant\n<think>\n"
    # with the trailing newline preserved.
    assert rendered.rstrip().endswith("<|im_start|>assistant\n<think>")


def test_v2_does_not_raise_multi_step_tool_check(prompts: CompactionPromptsConfig) -> None:
    """Specifically verify that the multi_step_tool check does not raise on v2 output."""
    body = [
        Message(role="system", content="SYS"),
        Message(role="user", content="A 说：xxx"),
        Message(role="assistant", content="回：yyy"),
    ]
    out = build_compact_messages_v2(body, prompts)
    # If the multi_step_tool check raises, _render_chat_template will raise AssertionError.
    # We want it to NOT raise.
    rendered = _render_chat_template(out)
    assert "No user query found" not in rendered
