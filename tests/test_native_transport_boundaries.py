"""Exercise production HTTP builders and API preparation without executing tools."""
from copy import deepcopy
import base64
import io
import json

import pytest
from PIL import Image

from backend.agent.canonical_messages import canonicalize_model_messages, message_to_template, message_to_wire
from backend.api import chat as chat_api
from backend.compact.prompt_diagnostics import canonicalize_messages, make_prompt_snapshot
from backend.config import get_config
from backend.llm_service import LLMService, Message, SGLangProvider, VLLMProvider, OpenAICompatibleProxyProvider, prepare_multimodal_messages

ARGS = {'raw': ' true \n ', 'flag': True, 'none': None, 'list': [1, '中文']}
CALL = {'id': 'same_name_1', 'type': 'function', 'function': {'name': 'inspect', 'arguments': ARGS}}
TOOLS = [{'type': 'function', 'function': {'name': 'inspect', 'parameters': {'type': 'object', 'properties': {'raw': {'type':'string'}, 'flag':{'type':'boolean'}, 'none':{'type':'null'}, 'list':{'type':'array'}}, 'required':['raw','flag','none','list']}}}]

def transcript():
    return [Message('user', 'inspect'), Message('assistant', '', tool_calls=[deepcopy(CALL)], reasoning_content=''), Message('tool', ' \n    code\n ', tool_call_id='same_name_1', name='inspect')]

@pytest.mark.parametrize('provider_type', [SGLangProvider, VLLMProvider, OpenAICompatibleProxyProvider])
@pytest.mark.parametrize('stream', [False, True])
def test_service_actual_provider_http_builder_preserves_canonical_pair(monkeypatch, provider_type, stream):
    config = deepcopy(get_config())
    provider = provider_type(config)
    monkeypatch.setattr(provider, 'is_loaded', lambda: True)
    # Intercept only the transport sink, after the production request builder.
    monkeypatch.setattr(provider, '_sync_generate', lambda request: request)
    monkeypatch.setattr(provider, '_stream_generate', lambda request, *_: request)
    service = LLMService.__new__(LLMService)
    service.config, service.debug, service._provider = config, provider.debug, provider
    original = transcript()
    captured = service.chat(original, tools=deepcopy(TOOLS), stream=stream, max_new_tokens=20)
    assert captured['messages'] == [message_to_wire(m) for m in original]
    assert captured['tools'] == TOOLS
    calls = captured['messages'][1]['tool_calls']
    assert json.loads(calls[0]['function']['arguments']) == ARGS
    assert isinstance(json.loads(calls[0]['function']['arguments']), dict)
    assert captured['messages'][1]['reasoning_content'] == ''
    assert captured['messages'][2]['tool_call_id'] == 'same_name_1'
    assert original[1].tool_calls[0]['function']['arguments'] == ARGS


def test_api_input_conversion_and_compact_filter_keep_tool_metadata(monkeypatch):
    config = deepcopy(get_config())
    assert config.agent_prompts.compaction is not None
    monkeypatch.setattr(chat_api, 'get_config', lambda: config)
    incoming = chat_api.ChatCompletionRequest.model_validate({'messages': [
        {'role':'user','content':[{'type':'text','text':'inspect'}, {'type':'image_url','image_url':{'url':'https://example.invalid/image.png'}, 'provenance':'user_upload'}],'metadata':{'bubble_id':'UI_ONLY'}},
        {'role':'assistant','content':'','tool_calls':[CALL], 'reasoning_content':''},
        {'role':'tool','content':'  observation\n','tool_call_id':'same_name_1','name':'inspect'},
    ], 'tools':TOOLS})
    canonical = canonicalize_model_messages(incoming.messages)
    prepared = chat_api._prepare_chat_messages_for_llm(canonical, incoming.messages)
    assert [message_to_template(m) for m in prepared] == [message_to_template(m) for m in canonical]
    assert 'UI_ONLY' not in canonicalize_messages(prepared)


def test_multimodal_resize_preserves_provenance_and_call_sidecar():
    data = io.BytesIO()
    Image.new('RGB', (16, 8), 'red').save(data, format='PNG')
    url = 'data:image/png;base64,' + base64.b64encode(data.getvalue()).decode()
    content = [{'type':'text','text':'  pixels\n'}, {'type':'image_url','image_url':{'url':url}, 'provenance':'computer_use_observation'}]
    message = Message('tool', content, tool_call_id='same_name_1', name='inspect', reasoning_content='')
    message.canonical_tool_results = [{'call_id':'same_name_1', 'status':'completed'}]
    prepared = prepare_multimodal_messages([message], max_image_dimension=4)[0]
    assert prepared.content[0] == content[0]
    assert prepared.content[1]['provenance'] == 'computer_use_observation'
    new_url = prepared.content[1]['image_url']['url']
    assert new_url != url
    with Image.open(io.BytesIO(base64.b64decode(new_url.split(',')[1]))) as resized:
        assert resized.size == (4, 2)
    assert prepared.tool_call_id == message.tool_call_id
    assert prepared.name == 'inspect' and prepared.reasoning_content == ''
    assert prepared.canonical_tool_results == message.canonical_tool_results
    assert prepared.canonical_tool_results is not message.canonical_tool_results
    assert message.content[1]['image_url']['url'] == url


def test_wire_template_roundtrip_does_not_double_encode_or_mutate():
    original = transcript()[1]
    wire = message_to_wire(original)
    template = message_to_template(wire)
    assert template['tool_calls'][0]['function']['arguments'] == ARGS
    assert message_to_wire(template) == wire
    assert original.tool_calls[0]['function']['arguments'] == ARGS


def test_diagnostics_include_ids_reasoning_and_arguments_but_exclude_ui():
    original = transcript()
    original[1].metadata = {'bubble_id':'UI_ONLY'}
    a = make_prompt_snapshot('native', original)
    assert 'same_name_1' in a.canonical_text
    assert 'reasoning_content' in a.canonical_text
    assert 'tool_calls' in a.canonical_text
    assert 'UI_ONLY' not in a.canonical_text
    for field, value in [('tool_call_id','same_name_2'), ('name','another')]:
        changed = deepcopy(original)
        setattr(changed[2], field, value)
        assert make_prompt_snapshot('native', changed).content_hash != a.content_hash
    changed = deepcopy(original)
    changed[1].reasoning_content = 'different reasoning'
    assert make_prompt_snapshot('native', changed).content_hash != a.content_hash
