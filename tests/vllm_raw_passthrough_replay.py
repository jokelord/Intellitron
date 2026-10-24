"""No-model replay against the supported NGC vLLM image (run explicitly).

docker run --rm --network none -v "$PWD:/workspace:ro" --entrypoint python3 \
  nvcr.io/nvidia/vllm:26.06-py3 /workspace/tests/vllm_raw_passthrough_replay.py
"""

from pathlib import Path
import sys

from vllm.entrypoints.openai.chat_completion.protocol import ChatCompletionRequest
from vllm.parser import ParserManager
from vllm.tool_parsers.abstract_tool_parser import ToolParserManager


class Tokenizer:
    def get_vocab(self):
        return {"<tool_call>": 1, "</tool_call>": 2}


PLUGIN = (Path(sys.argv[1]) if len(sys.argv) > 1 else
          Path(__file__).resolve().parents[1] / "config/vllm-plugins/qwen_raw_passthrough.py")
ToolParserManager.import_tool_parser(str(PLUGIN))
TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {
    "type": "object", "properties": {"file_path": {"type": "string"}},
}}}]
FRAGMENTS = [
    ("<tool_call>", [1]),
    ("\n<function=read_file>\n<parameter=file_path>", [3]),
    ("  /tmp/文件", [4]),
    ("🙂.txt  ", [5]),
    ("</parameter>\n</function>\n", [6]),
    ("</tool_call>", [2]),
]
RAW = "".join(fragment for fragment, _ in FRAGMENTS)


def request(*, raw=False, choice="auto"):
    return ChatCompletionRequest(
        model="qwen", messages=[{"role": "user", "content": "read"}],
        tools=TOOLS, tool_choice=choice, stream=True,
        chat_template_kwargs={"raw_tool_call_passthrough": True} if raw else {},
        skip_special_tokens=False,
    )


def replay(name, req):
    parser_cls = ParserManager.get_parser(
        tool_parser_name=name, reasoning_parser_name=None,
        enable_auto_tools=True, model_name="qwen",
    )
    parser = parser_cls(Tokenizer(), req.tools)
    return [parser.parse_delta(
        delta_text=text, delta_token_ids=ids, request=req, prompt_token_ids=[7],
    ) for text, ids in FRAGMENTS]


native_request = request(raw=True, choice="none")
native = replay("qwen3_coder_raw_passthrough", native_request)
assert [delta.content for delta in native] == [text for text, _ in FRAGMENTS]
assert all(not delta.tool_calls for delta in native)
plugin = ToolParserManager.get_tool_parser("qwen3_coder_raw_passthrough")(Tokenizer(), TOOLS)
full = plugin.extract_tool_calls(RAW, native_request)
assert full.content == RAW and not full.tools_called and full.tool_calls == []
# Malformed XML remains visible verbatim for the repository's strict parser.
malformed = RAW[:-7] + "  \n"
assert plugin.extract_tool_calls(malformed, native_request).content == malformed

# Compare the real delegated parser, ignoring only random provider call IDs.
def normalized(deltas):
    results = []
    for delta in deltas:
        if delta is None:
            results.append(None)
            continue
        data = delta.model_dump()
        for call in data.get("tool_calls") or []:
            call.pop("id", None)
        results.append(data)
    return results


ordinary = replay("qwen3_coder_raw_passthrough", request())
upstream = replay("qwen3_coder", request())
assert any(delta and delta.tool_calls for delta in ordinary)
assert normalized(ordinary) == normalized(upstream)
default_full = plugin.extract_tool_calls(RAW, request())
assert default_full.tools_called and default_full.tool_calls[0].function.name == "read_file"
print("NGC parser replay passed: exact Unicode XML fragments, malformed raw output, default delegation")
