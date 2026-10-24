from pathlib import Path

import yaml


def test_compact_control_prompt_requests_summary_only_for_low_latency():
    config_path = Path(__file__).resolve().parents[1] / "config" / "config-agent-prompts.yaml"
    prompt = yaml.safe_load(config_path.read_text())["compaction"]["compact_control_user_instruction"]

    assert "<summary>" in prompt
    assert "<analysis>" not in prompt


def test_compact_source_budget_is_low_latency_fallback_not_full_context():
    config_path = Path(__file__).resolve().parents[1] / "config" / "config-misc.yaml"
    compact = yaml.safe_load(config_path.read_text())["compact"]

    assert compact["source_token_budget"] <= 10000
