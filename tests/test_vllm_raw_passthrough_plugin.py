"""Repository parser preserves generated text and default vLLM parsing."""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'config/vllm-plugins/qwen_raw_passthrough.py'


@pytest.fixture
def parser(monkeypatch):
    # vLLM runs exclusively in NGC; substitute only its import boundary here.
    # The real cached container replay is tests/vllm_raw_passthrough_replay.py.
    assert PLUGIN.is_file(), 'repository raw passthrough plugin is missing'
    class Base:
        def extract_tool_calls(self, model_output, request):
            return ('default-full', model_output, request)
        def extract_tool_calls_streaming(self, *args):
            return ('default-stream', args)
    registry = {}
    class Manager:
        @classmethod
        def register_module(cls, name=None, module=None):
            registry[name] = module
    modules = {
        'vllm.tool_parsers.qwen3coder_tool_parser': {'Qwen3CoderToolParser': Base},
        'vllm.tool_parsers.abstract_tool_parser': {'ToolParserManager': Manager},
        'vllm.entrypoints.openai.engine.protocol': {
            'DeltaMessage': SimpleNamespace, 'ExtractedToolCallInformation': SimpleNamespace},
    }
    for name, attrs in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location('qwen_raw_passthrough_test', PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return registry['qwen3_coder_raw_passthrough']()


def test_raw_full_response_keeps_exact_model_text(parser):
    raw = '  before\n<tool_call><function=read><parameter=path> 文件🙂 </parameter></function></tool_call>\n'
    request = SimpleNamespace(chat_template_kwargs={'raw_tool_call_passthrough': True})
    result = parser.extract_tool_calls(raw, request)
    assert result.content == raw
    assert result.tools_called is False
    assert result.tool_calls == []


def test_raw_stream_keeps_every_fragment_including_incomplete_markup(parser):
    request = SimpleNamespace(chat_template_kwargs={'raw_tool_call_passthrough': True})
    fragments = ['<tool_call>', '\n<function=read>', '<parameter=path>  文件', '🙂\n', '</parameter>']
    current = ''
    received = []
    for fragment in fragments:
        result = parser.extract_tool_calls_streaming(current, current + fragment, fragment, [], [], [], request)
        received.append(result.content)
        current += fragment
    assert received == fragments


@pytest.mark.parametrize('kwargs', [None, {}, {'raw_tool_call_passthrough': False}, {'raw_tool_call_passthrough': 'true'}])
def test_requests_without_explicit_boolean_flag_delegate_unchanged(parser, kwargs):
    request = SimpleNamespace(chat_template_kwargs=kwargs)
    assert parser.extract_tool_calls('original', request) == ('default-full', 'original', request)
    args = ('before', 'current', 'delta', [1], [1, 2], [2], request)
    assert parser.extract_tool_calls_streaming(*args) == ('default-stream', args)


@pytest.mark.parametrize('script', ['1_B_start_vllm_container_with_snapshots.sh', '1_C_start_vllm_container.sh'])
def test_launcher_mounts_configured_parser_plugin_read_only(script, tmp_path):
    from test_vllm_container_scripts import run_launcher, API_SERVER_HELP
    plugin = tmp_path / 'custom parser.py'
    plugin.write_text('# fixture plugin\n')
    result, log, _ = run_launcher(script, tmp_path, infer_overrides={
        'tool_parser_plugin': str(plugin), 'tool_call_parser': 'qwen3_coder_raw_passthrough',
        'enable_auto_tool_choice': True}, api_server_help=API_SERVER_HELP + '\n--tool-parser-plugin\n')
    assert result.returncode == 0, result.stdout + result.stderr
    log = log.read_text()
    assert f'<{plugin}:/opt/llm-tool-parser-plugin.py:ro>' in log
    assert '<--tool-parser-plugin> </opt/llm-tool-parser-plugin.py>' in log
    assert '<--tool-call-parser> <qwen3_coder_raw_passthrough>' in log


def test_selected_plugin_configuration_reaches_backend_model():
    from backend.config import ConfigLoader
    config = ConfigLoader().load()
    assert config.inference.tool_protocol == 'qwen35-native'
    assert config.inference.capture_raw_tool_calls is True
    assert config.vllm.tool_parser_plugin == 'config/vllm-plugins/qwen_raw_passthrough.py'
    assert config.vllm.tool_call_parser == 'qwen3_coder_raw_passthrough'


@pytest.mark.parametrize('script', ['1_B_start_vllm_container_with_snapshots.sh', '1_C_start_vllm_container.sh'])
def test_missing_plugin_fails_before_container_removal(script, tmp_path):
    from test_vllm_container_scripts import run_launcher
    result, log, _ = run_launcher(script, tmp_path, infer_overrides={
        'tool_parser_plugin': str(tmp_path / 'missing.py'),
    })
    assert result.returncode != 0
    assert 'Tool parser plugin 不存在' in result.stdout + result.stderr
    assert not log.exists() or '<rm> <-f>' not in log.read_text()
