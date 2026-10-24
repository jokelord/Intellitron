"""Direct probes accept only a complete Qwen tool-code call in final content."""
from io import BytesIO
import json

import pytest


def _encoded(stage, grounding_request, crop):
    from backend.computer_use.contracts import ImageArtifact
    from backend.computer_use.model_adapter import describe_stage, encode_stage

    request = grounding_request()
    image = ImageArtifact(png=b'png', width=1024, height=1024,
                          content_hash='h', renderer_fingerprint='r')
    bound_crop = (crop(grounding_request=request, detail_width=1024, detail_height=1024,
                       model_width=1024, model_height=1024) if stage == 'stage2' else None)
    return encode_stage(describe_stage(stage, image, bound_crop, request), image)


def _infer(probe, monkeypatch, encoded, *, content=None, tool_calls=None,
           reasoning_content=None, finish_reason='stop'):
    response = {'choices': [{'finish_reason': finish_reason, 'message': {
        'content': content, 'reasoning_content': reasoning_content,
        'tool_calls': tool_calls,
    }}]}
    monkeypatch.setattr(probe.urllib.request, 'urlopen',
                        lambda request, timeout: BytesIO(json.dumps(response).encode()))
    if probe.__name__.endswith('_skill'):
        return probe.infer('http://127.0.0.1:30000', 'fixture-model', encoded,
                           'offline target', timeout=5)
    return probe.infer('http://127.0.0.1:30000', 'fixture-model', encoded,
                       'offline target')


@pytest.mark.parametrize('module_name', [
    'scripts.probe_computer_use_two_stage', 'scripts.probe_computer_use_skill',
])
@pytest.mark.parametrize('stage,content,expected_name', [
    ('stage1', '<tool_code>\ninspect_region(x=200, y=700, zoom=4)\n</tool_code>', 'inspect_region'),
    ('stage2', "<tool_code>\ninteract_detail(x=500, y=125, action='left_click')\n</tool_code>", 'interact_detail'),
    ('stage2', "<tool_code>\nreobserve(reason='target_not_visible')\n</tool_code>", 'reobserve'),
])
def test_complete_final_tool_code_uses_existing_stage_decoder(
    module_name, stage, content, expected_name, monkeypatch, grounding_request, crop,
):
    import importlib
    from backend.computer_use.model_adapter import decode_stage

    probe = importlib.import_module(module_name)
    encoded = _encoded(stage, grounding_request, crop)
    raw, trace = _infer(probe, monkeypatch, encoded, content=content)
    assert raw['name'] == expected_name
    assert trace['response_source'] == 'final_content_tool_code'
    assert decode_stage(encoded, raw) is not None


@pytest.mark.parametrize('module_name', [
    'scripts.probe_computer_use_two_stage', 'scripts.probe_computer_use_skill',
])
@pytest.mark.parametrize('content,reasoning_content', [
    ('x=500, y=125; click the target', None),
    (None, '<tool_code>\ninspect_region(x=200, y=700, zoom=4)\n</tool_code>'),
    ('Here is the call: <tool_code>inspect_region(x=200,y=700,zoom=4)</tool_code>', None),
    ('<tool_code>inspect_region(x=200,y=700,zoom=4)\nreobserve(reason="other")</tool_code>', None),
    ('<tool_code>inspect_region(x=__import__("os"),y=700,zoom=4)</tool_code>', None),
    ('<reobserve><parameter=reason>target_ambiguous</parameter></function></tool_call>', None),
    ('<tool_code>interact_detail(x=500,y=125,action="left_click")</tool_code>', None),
])
def test_ambiguous_or_wrong_stage_final_text_is_not_a_probe_call(
    module_name, content, reasoning_content, monkeypatch, grounding_request, crop,
):
    import importlib

    probe = importlib.import_module(module_name)
    encoded = _encoded('stage1', grounding_request, crop)
    with pytest.raises(ValueError):
        _infer(probe, monkeypatch, encoded, content=content,
               reasoning_content=reasoning_content)


@pytest.mark.parametrize('module_name', [
    'scripts.probe_computer_use_two_stage', 'scripts.probe_computer_use_skill',
])
def test_structured_call_remains_authoritative_over_final_content(
    module_name, monkeypatch, grounding_request, crop,
):
    import importlib

    probe = importlib.import_module(module_name)
    encoded = _encoded('stage1', grounding_request, crop)
    raw, trace = _infer(probe, monkeypatch, encoded,
        content='<tool_code>inspect_region(x=1,y=1,zoom=4)</tool_code>',
        tool_calls=[{'function': {'name': 'inspect_region',
                                  'arguments': '{"x":200,"y":700,"zoom":4}'}}])
    assert raw['arguments'] == {'x': 200, 'y': 700, 'zoom': 4}
    assert trace['response_source'] == 'structured_tool_calls'


@pytest.mark.parametrize('module_name', [
    'scripts.probe_computer_use_two_stage', 'scripts.probe_computer_use_skill',
])
def test_truncated_or_empty_final_response_reports_only_concise_diagnostics(
    module_name, monkeypatch, grounding_request, crop,
):
    import importlib

    probe = importlib.import_module(module_name)
    encoded = _encoded('stage1', grounding_request, crop)
    content = '<tool_code>inspect_region(x=200,y=700,zoom=4)</tool_code>'
    with pytest.raises(ValueError) as truncated:
        _infer(probe, monkeypatch, encoded, content=content,
               reasoning_content='private reasoning must not appear', finish_reason='length')
    assert "finish_reason='length'" in str(truncated.value)
    assert 'final_content_tool_code_present=True' in str(truncated.value)
    assert content not in str(truncated.value)
    assert 'private reasoning' not in str(truncated.value)

    with pytest.raises(ValueError) as empty:
        _infer(probe, monkeypatch, encoded, content='',
               reasoning_content=content, finish_reason='stop')
    assert 'final_content_tool_code_present=False' in str(empty.value)
    assert 'inspect_region' not in str(empty.value)
