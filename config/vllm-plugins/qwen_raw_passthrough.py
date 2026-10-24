"""Opt-in lossless Qwen text transport for the repository's native adapter.

Loaded with vLLM's supported --tool-parser-plugin hook. Ordinary API callers
retain Qwen3Coder parsing; native callers request raw text so the backend can
show and strictly parse the actual generated XML, including malformed output.
"""

from vllm.entrypoints.openai.engine.protocol import (
    DeltaMessage,
    ExtractedToolCallInformation,
)
from vllm.tool_parsers.abstract_tool_parser import ToolParserManager
from vllm.tool_parsers.qwen3coder_tool_parser import Qwen3CoderToolParser


class QwenRawPassthroughParser(Qwen3CoderToolParser):
    @staticmethod
    def _raw_requested(request):
        return (request.chat_template_kwargs or {}).get("raw_tool_call_passthrough") is True

    def extract_tool_calls(self, model_output, request):
        if self._raw_requested(request):
            return ExtractedToolCallInformation(
                tools_called=False, tool_calls=[], content=model_output
            )
        return super().extract_tool_calls(model_output, request)

    def extract_tool_calls_streaming(
        self, previous_text, current_text, delta_text,
        previous_token_ids, current_token_ids, delta_token_ids, request,
    ):
        if self._raw_requested(request):
            return DeltaMessage(content=delta_text)
        return super().extract_tool_calls_streaming(
            previous_text, current_text, delta_text,
            previous_token_ids, current_token_ids, delta_token_ids, request,
        )


ToolParserManager.register_module(
    name="qwen3_coder_raw_passthrough", module=QwenRawPassthroughParser
)
