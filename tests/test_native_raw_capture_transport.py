"""Native raw capture keeps the production request prompt and tool schema."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment

from backend.agent.local_llm_model import LocalLLMModel
from backend.config import InferenceConfig, get_config
from backend.llm_service import GenerationResult, LLMService, Message, SGLangProvider, VLLMProvider
from test_native_compact_committed_tail import CALL, LiteralRead


@pytest.mark.parametrize('provider_type', [SGLangProvider, VLLMProvider])
@pytest.mark.parametrize('mode', ['sync', 'stream', 'fallback', 'retry_sync', 'retry_stream', 'retry_fallback'])
def test_actual_native_http_requests_keep_raw_transport_on_retry_and_fallback(monkeypatch, provider_type, mode):
    config = deepcopy(get_config())
    assert config.inference.tool_protocol == 'qwen35-native'
    assert config.inference.capture_raw_tool_calls is True
    provider = provider_type(config)
    monkeypatch.setattr(provider, 'is_loaded', lambda: True)
    captures = []

    def sync(request):
        captures.append(deepcopy(request))
        if mode == 'retry_sync' and len(captures) == 1:
            raise RuntimeError('400 extra_body optional_sampling not supported')
        return GenerationResult(text=CALL)

    def stream(request, *_):
        captures.append(deepcopy(request))
        if mode in ('retry_stream', 'retry_fallback') and len(captures) == 1:
            return iter([{'content': '400 extra_body optional_sampling not supported'}])
        return iter([{'content': '' if mode in ('fallback', 'retry_fallback') else CALL}])

    monkeypatch.setattr(provider, '_sync_generate', sync)
    monkeypatch.setattr(provider, '_stream_generate', stream)
    service = LLMService.__new__(LLMService)
    service.config, service.debug, service._provider = config, provider.debug, provider
    service._active_backend = 'sglang' if provider_type is SGLangProvider else 'vllm'
    session = SimpleNamespace(tool_protocol='qwen35-native', session_id='raw-capture')
    model = LocalLLMModel(service, session=session)
    kwargs = {'tools_to_call_from':[LiteralRead()], 'extra_body':{'chat_template_kwargs':{'enable_thinking':False}, 'optional_sampling':7},
              'retry_without_extra_body':True}
    messages = [Message('system', 'stable'), Message('user', 'read')]
    if mode in ('sync', 'retry_sync'):
        model.generate(messages, **kwargs)
    else:
        list(model.generate_stream(messages, **kwargs))
    assert len(captures) == (3 if mode == 'retry_fallback' else 2 if mode in ('fallback','retry_sync','retry_stream') else 1)
    for request in captures:
        assert request['tool_choice'] == 'none'
        assert request['skip_special_tokens'] is False
        template = request['chat_template_kwargs']
        assert template['raw_tool_call_passthrough'] is True
        assert template['raw_request_tools'] == request['tools']
        assert request['messages'] == captures[0]['messages']
        assert request['tools'] == captures[0]['tools']
    if mode.startswith('retry'):
        assert captures[-1]['chat_template_kwargs']['enable_thinking'] is False
        assert 'optional_sampling' not in captures[-1]
        template = Environment().from_string(Path('config/chat-templates/qwen-native-cache-stable.jinja').read_text())
        def rendered(request):
            return template.render(messages=request['messages'], tools=request['tools'],
                add_generation_prompt=True, **request['chat_template_kwargs'])
        assert rendered(captures[0]) == rendered(captures[-1])


def test_raw_capture_default_off():
    assert InferenceConfig().capture_raw_tool_calls is False


def test_template_raw_alias_keeps_exact_tools_prefix_and_history():
    template = Environment().from_string(Path('config/chat-templates/qwen-native-cache-stable.jinja').read_text())
    tools = [{'type':'function','function':{'name':'read_skill','parameters':{'type':'object'}}}]
    messages = [{'role':'system','content':'stable'}, {'role':'user','content':'read'}]
    for thinking in (False, True):
        normal = template.render(messages=messages, tools=tools, enable_thinking=thinking, add_generation_prompt=True, tool_choice='auto')
        raw = template.render(messages=messages, tools=None, raw_request_tools=tools, raw_tool_call_passthrough=True,
                              enable_thinking=thinking, add_generation_prompt=True, tool_choice='none')
        assert raw == normal

@pytest.mark.parametrize('value', ['true', 1, None, [], {}])
def test_capture_config_rejects_non_boolean(value):
    from backend.config import ConfigLoader
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {'inference': {'capture_raw_tool_calls': value}}
    with pytest.raises(ValueError, match='capture_raw_tool_calls must be a boolean'):
        loader._parse_inference_config()


@pytest.mark.parametrize('dialect,capture,has_tools', [
    ('legacy_text', True, True), ('qwen35-native', False, True), ('qwen35-native', True, False)])
def test_non_opted_in_model_http_request_remains_default(monkeypatch, dialect, capture, has_tools):
    config = deepcopy(get_config())
    config.inference.capture_raw_tool_calls = capture
    provider = VLLMProvider(config)
    monkeypatch.setattr(provider, 'is_loaded', lambda: True)
    seen = []
    monkeypatch.setattr(provider, '_sync_generate', lambda request: seen.append(request) or GenerationResult(text='answer'))
    service = LLMService.__new__(LLMService)
    service.config, service.debug, service._provider, service._active_backend = config, provider.debug, provider, 'vllm'
    model = LocalLLMModel(service, session=SimpleNamespace(tool_protocol=dialect, session_id='default'))
    model.generate([Message('user','answer')], tools_to_call_from=[LiteralRead()] if has_tools else None)
    assert 'tool_choice' not in seen[0]
    assert 'raw_tool_call_passthrough' not in repr(seen[0])


@pytest.mark.parametrize('provider_type', [SGLangProvider, VLLMProvider])
@pytest.mark.asyncio
async def test_answer_now_and_compact_keep_capture_transport_scoped_to_native_model_requests(tmp_path, monkeypatch, provider_type):
    from test_native_compact_committed_tail import context, ordinary_step
    runner, session, agent, service = context(tmp_path)
    config = deepcopy(runner.config)
    assert config.inference.capture_raw_tool_calls is True
    service.config = config
    provider = provider_type(config)
    monkeypatch.setattr(provider, 'is_loaded', lambda: True)
    captured = []
    real_service = LLMService.__new__(LLMService)
    real_service.config, real_service.debug, real_service._provider, real_service._active_backend = config, provider.debug, provider, ('sglang' if provider_type is SGLangProvider else 'vllm')

    def sync(request):
        captured.append(deepcopy(request))
        return GenerationResult(text=CALL if len(captured) == 1 else 'Immediate answer.')

    def stream(request, *_):
        captured.append(deepcopy(request))
        return iter([{'content': CALL if len(captured) == 1 else 'Immediate answer.'}])

    monkeypatch.setattr(provider, '_sync_generate', sync)
    monkeypatch.setattr(provider, '_stream_generate', stream)
    import threading
    real_service._stream_usage_lock = threading.Lock()
    real_service._stream_usage_requests = {}
    # Normal native inference crosses the real provider HTTP builder.
    agent.model.llm_service = real_service
    _, events = ordinary_step(agent)
    from backend.agent.agent_runner import RawToolCallDebug
    disclosures = [event for event in events if isinstance(event, RawToolCallDebug)]
    assert [(event.source, event.raw_tool_call) for event in disclosures] == [('model_text', CALL)]
    runner.llm_service = real_service
    answer = await runner.generate_answer_now_reply(session.session_id)
    assert answer['status'] == 'ok'
    assert len(captured) == 2
    normal, now = captured
    assert now['tools'] == normal['tools']
    assert now['chat_template_kwargs']['raw_request_tools'] == now['tools']
    assert now['tool_choice'] == normal['tool_choice'] == 'none'
    assert now['skip_special_tokens'] is False
    assert 'Use no tools for this response only' in repr(now['messages'][-1])
    # Compact uses the same canonical snapshot/schema, without Agent raw flags.
    body = runner._build_cache_first_compact_body_messages(session)
    real_service.chat(body, tools=runner._native_compact_tools(session), stream=False)
    compact = captured[-1]
    assert compact['tools'] == normal['tools']
    assert 'tool_choice' not in compact
    assert compact['messages'][:len(normal['messages'])] == normal['messages']

@pytest.mark.parametrize('streaming', [False, True])
def test_unsupported_essential_raw_template_flag_fails_without_prefix_downgrade(monkeypatch, streaming):
    config = deepcopy(get_config())
    config.inference.capture_raw_tool_calls = True
    provider = VLLMProvider(config)
    seen = []
    error = '400 chat_template_kwargs raw_tool_call_passthrough not supported'
    monkeypatch.setattr(provider, 'is_loaded', lambda: True)

    def reject(request, *_):
        seen.append(deepcopy(request))
        if streaming:
            return iter([{'content': error}])
        raise RuntimeError(error)

    monkeypatch.setattr(provider, '_sync_generate', reject)
    monkeypatch.setattr(provider, '_stream_generate', reject)
    service = LLMService.__new__(LLMService)
    service.config, service.debug, service._provider, service._active_backend = config, provider.debug, provider, 'vllm'
    model = LocalLLMModel(service, session=SimpleNamespace(tool_protocol='qwen35-native', session_id='reject'))
    kwargs = {'tools_to_call_from':[LiteralRead()], 'retry_without_extra_body':True,
              'extra_body':{'chat_template_kwargs':{'enable_thinking':False}}}
    with pytest.raises(RuntimeError, match='raw_tool_call_passthrough not supported'):
        if streaming:
            list(model.generate_stream([Message('user','go')], **kwargs))
        else:
            model.generate([Message('user','go')], **kwargs)
    assert len(seen) == 2
    assert all(request['chat_template_kwargs']['raw_tool_call_passthrough'] is True for request in seen)
    assert all(request['chat_template_kwargs']['enable_thinking'] is False for request in seen)
