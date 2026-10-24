"""Regression: prompt_messages from build_compact_messages are dicts, but
llm_service.chat() reads them as Message objects (m.role / m.content
attribute access). The orchestrator MUST normalize dicts to Message
objects before calling chat_stream_async, otherwise the LLM call raises
AttributeError('dict' object has no attribute 'role') and the user sees
'Compact failed: llm_unreachable' with no obvious cause.
"""
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


class _SpyLLM:
    """Records the messages it receives and yields predetermined chunks."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.received_messages = None
        self.received_max_new_tokens = None
        self.received_request_id = None

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
        # CRITICAL: if the orchestrator forgot to coerce dicts to
        # Message objects, this attribute access raises AttributeError.
        # We assert here so the test fails fast with a clear signal.
        self.received_messages = messages
        for m in messages:
            _ = m.role  # must be attribute, not dict
            _ = m.content  # must be attribute, not dict
        self.received_max_new_tokens = max_new_tokens
        self.received_request_id = request_id
        for c in self.chunks:
            yield {"content": c}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _collect(agen):
    out = []
    async for ev in agen:
        out.append(ev)
    return out


def test_orchestrator_normalizes_dict_prompts_to_message_objects():
    """The orchestrator must convert dicts (from build_compact_messages)
    to Message objects before calling llm_service.chat_stream_async,
    otherwise the LLM service's attribute-access pattern raises
    AttributeError('dict' object has no attribute 'role')."""
    req = CompactRequest(
        mode="chat",
        messages=[{"role": "user", "content": "history"}],
        trigger="manual",
    )
    spy = _SpyLLM(chunks=["<summary>OK</summary>"])
    orch = CompactOrchestrator(llm_service=spy, config=make_config(), prompts=make_prompts())
    events = _run(_collect(orch.run_compact(req)))
    # The orchestrator should have completed successfully (not yielded
    # compact_failed(llm_unreachable)).
    kinds = [e.kind for e in events]
    assert CompactEventKind.STARTED in kinds
    assert CompactEventKind.COMPLETED in kinds, (
        f"expected compact_completed, got: {kinds}; "
        "the orchestrator likely passed dicts instead of Message objects, "
        "making chat_stream_async raise AttributeError('dict' has no 'role')"
    )
    # And the spy must have observed Message objects (not dicts).
    assert spy.received_messages is not None
    for m in spy.received_messages:
        assert not isinstance(m, dict), (
            f"orchestrator passed dict {m!r} to llm_service; "
            "must coerce to Message first"
        )
    # The compact orchestrator must also pass the request_id through
    # so the SGLang abort tracking continues to work.
    assert spy.received_request_id is not None
    assert spy.received_request_id.startswith("compact-")
    # And max_new_tokens must be forwarded unchanged.
    assert spy.received_max_new_tokens == 2000
