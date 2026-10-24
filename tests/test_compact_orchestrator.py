"""Tests for CompactOrchestrator."""
import asyncio

from backend.compact.orchestrator import CompactOrchestrator
from backend.compact.types import CompactEventKind, CompactRequest
from backend.config import CompactConfig, CompactionPromptsConfig


def make_config():
    return CompactConfig(
        max_summary_tokens=2000,
        temperature=0.0,
        top_p=0.9,
        validation_min_summary_chars=1,
        validation_min_summary_prompt_ratio=0.0,
        validation_require_summary_sections=False,
    )


def make_prompts():
    return CompactionPromptsConfig(
        status_message="Compressing",
        no_tools_preamble="NO TOOLS",
        main_prompt="9 sections",
        custom_instructions_header="USER INSTR",
        no_tools_trailer="NO TOOLS REMINDER",
        summary_envelope_preface="[PRE]",
        summary_envelope_suffix="[POST]",
    )


class FakeLLMService:
    """Returns predetermined chunks; optionally raises."""

    def __init__(self, chunks=None, raises=None):
        self.chunks = chunks or []
        self.raises = raises

    async def chat_stream_async(
        self,
        messages,
        max_new_tokens=None,
        temperature=None,
        top_p=None,
        tools=None,
        request_id=None,
        runtime_context=None,
    ):
        if self.raises:
            raise self.raises
        for c in self.chunks:
            yield {"content": c}


def run(coro):
    return asyncio.run(coro)


def test_successful_compact_emits_started_delta_completed():
    body = [{"role": "user", "content": "old conversation"}]
    req = CompactRequest(mode="chat", messages=body, trigger="manual")
    fake = FakeLLMService(chunks=["<analysis>think</analysis><summary>My summary</summary>"])
    orch = CompactOrchestrator(llm_service=fake, config=make_config(), prompts=make_prompts())
    events = run(_collect(orch.run_compact(req)))
    assert events[0].kind == CompactEventKind.STARTED
    assert any(e.kind == CompactEventKind.DELTA for e in events)
    assert events[-1].kind == CompactEventKind.COMPLETED
    assert "My summary" in events[-1].text


def test_llm_failure_yields_failed_event():
    req = CompactRequest(mode="chat", messages=[{"role": "user", "content": "x"}], trigger="manual")
    fake = FakeLLMService(raises=ConnectionError("backend down"))
    orch = CompactOrchestrator(llm_service=fake, config=make_config(), prompts=make_prompts())
    events = run(_collect(orch.run_compact(req)))
    assert events[0].kind == CompactEventKind.STARTED
    assert events[-1].kind == CompactEventKind.FAILED
    assert events[-1].metadata["reason"] == "llm_unreachable"
    # The exception type and message MUST be in metadata.error so the
    # user can diagnose WHY the LLM call failed (not just a generic
    # "llm_unreachable" label). The user reported the error was
    # uninformative before this assertion was added.
    assert "ConnectionError" in events[-1].metadata["error"]
    assert "backend down" in events[-1].metadata["error"]


def test_empty_summary_yields_failed_event():
    req = CompactRequest(mode="chat", messages=[{"role": "user", "content": "x"}], trigger="manual")
    fake = FakeLLMService(chunks=["<analysis>no summary</analysis>"])
    orch = CompactOrchestrator(llm_service=fake, config=make_config(), prompts=make_prompts())
    events = run(_collect(orch.run_compact(req)))
    assert events[-1].kind == CompactEventKind.FAILED
    assert events[-1].metadata["reason"] == "empty_summary"


def test_no_messages_yields_failed_event():
    """When neither body_messages (v2) nor messages (v1) is provided, the
    orchestrator must yield compact_failed with reason='no_messages'.
    The v1 'collect_error' reason was renamed in the v2 refactor (spec §5.1
    / Task 4): the helper that raised ValueError was inlined, so the
    surface reason now reflects the missing-input contract rather than
    a generic collect failure.
    """
    req = CompactRequest(mode="chat")
    fake = FakeLLMService(chunks=[])
    orch = CompactOrchestrator(llm_service=fake, config=make_config(), prompts=make_prompts())
    events = run(_collect(orch.run_compact(req)))
    assert events[0].kind == CompactEventKind.STARTED
    assert events[-1].kind == CompactEventKind.FAILED
    assert events[-1].metadata["reason"] == "no_messages"


def test_apply_compaction_called_on_success():
    """apply_compaction callback is invoked with the cleaned summary."""
    body = [{"role": "user", "content": "x"}]
    req = CompactRequest(mode="chat", messages=body, trigger="manual")
    fake = FakeLLMService(chunks=["<summary>Cleaned</summary>"])
    captured = {}

    def apply(req, sid, text):
        captured["sid"] = sid
        captured["text"] = text

    orch = CompactOrchestrator(
        llm_service=fake,
        config=make_config(),
        prompts=make_prompts(),
        apply_compaction=apply,
    )
    events = run(_collect(orch.run_compact(req)))
    assert events[-1].kind == CompactEventKind.COMPLETED
    assert captured["text"] == "Summary:\nCleaned"
    assert captured["sid"].startswith("compact-")


def test_apply_compaction_failure_yields_failed_event():
    """If apply_compaction raises, the orchestrator yields compact_failed."""
    body = [{"role": "user", "content": "x"}]
    req = CompactRequest(mode="chat", messages=body, trigger="manual")
    fake = FakeLLMService(chunks=["<summary>Cleaned</summary>"])

    def apply(req, sid, text):
        raise RuntimeError("apply failed")

    orch = CompactOrchestrator(
        llm_service=fake,
        config=make_config(),
        prompts=make_prompts(),
        apply_compaction=apply,
    )
    events = run(_collect(orch.run_compact(req)))
    assert events[-1].kind == CompactEventKind.FAILED
    assert events[-1].metadata["reason"] == "apply_error"


async def _collect(agen):
    out = []
    async for ev in agen:
        out.append(ev)
    return out
