"""The real serving template preserves native messages and historical prefixes."""
from pathlib import Path
import pytest
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'config/chat-templates/qwen-native-cache-stable.jinja'

def render(messages, **kwargs):
    assert TEMPLATE.exists(), 'repository native template is missing'
    env = Environment()
    env.globals['raise_exception'] = lambda message: (_ for _ in ()).throw(ValueError(message))
    return env.from_string(TEMPLATE.read_text()).render(messages=messages, **kwargs)

def test_native_calls_json_scalars_and_lossless_observation():
    calls = [{'id':'x', 'function':{'name':'demo', 'arguments':{'flag':True,'empty':None,'raw':' true \n ', 'obj':{'n':1}}}}]
    observation = ' \n    def hello():\n        return True\n  '
    text = render([{'role':'user','content':'go'}, {'role':'assistant','content':'','tool_calls':calls}, {'role':'tool','content':observation}], tools=[{'type':'function','function':{'name':'demo'}}])
    assert text.count('<tools>') == 1
    assert text.count('<function=demo>') == 1
    assert '<parameter=flag>\ntrue\n</parameter>' in text
    assert '<parameter=empty>\nnull\n</parameter>' in text
    assert '<parameter=raw>\n true \n \n</parameter>' in text
    assert '<tool_response>\n' + observation + '\n</tool_response>' in text
    assert '<|im_start|>tool' not in text

@pytest.mark.parametrize('reasoning', ['', 'consider carefully'])
def test_history_thinking_is_stable_after_new_user_and_toggle(reasoning):
    history = [{'role':'user','content':'first'}, {'role':'assistant','content':'answer','reasoning_content':reasoning}]
    old = render(history, enable_thinking=True)
    new = render(history + [{'role':'user','content':'next'}], enable_thinking=False, add_generation_prompt=True)
    assert '<think>\n' + reasoning + '\n</think>' in old
    assert new.startswith(old)
    assert new.endswith('<think>\n\n</think>\n\n')

def test_complete_tool_group_prefix_stable_and_multimodal_supported():
    history = [{'role':'user','content':[{'type':'image','image':'x'},{'type':'video','video':'x'},{'type':'text','text':'go'}]}, {'role':'assistant','content':'','tool_calls':[]}, {'role':'tool','content':'one'}, {'role':'tool','content':'two'}]
    old = render(history, add_vision_id=True)
    new = render(history + [{'role':'user','content':'next'}], add_vision_id=True)
    assert new.startswith(old)
    assert 'Picture 1: <|vision_start|><|image_pad|><|vision_end|>' in old
    assert 'Video 1: <|vision_start|><|video_pad|><|vision_end|>' in old
    assert old.count('<tool_response>') == 2
    assert old.endswith('</tool_response><|im_end|>\n')

def test_embedded_empty_think_and_image_tool_observation_keep_prefix():
    messages = [{'role':'user','content':'go'}, {'role':'assistant','content':'<think>\n\n</think>\n\nanswer'}, {'role':'tool','content':[{'type':'text','text':' \n  code\n '},{'type':'image_url','image_url':{'url':'x'}}]}]
    old = render(messages)
    assert '<think>\n\n</think>\n\nanswer' in old
    assert '<tool_response>\n \n  code\n <|vision_start|><|image_pad|><|vision_end|>\n</tool_response>' in old
    assert render(messages + [{'role':'user','content':'next'}], enable_thinking=False).startswith(old)


def test_selected_infer_template_paths_are_repository_artifact():
    import yaml
    config = yaml.safe_load((ROOT / 'config/qwen3.8-radixArk-no-DSPARK-NVFP4-config-infer.yaml').read_text())
    for provider in ('sglang', 'vllm'):
        assert ROOT / config[provider]['chat_template'] == TEMPLATE

@pytest.mark.parametrize('value', ['/tmp/demo.txt', '  /tmp/demo.txt  ', '\n  code\n\n', '', '\r\n\tcode\r\n'])
def test_actual_template_native_parser_roundtrip(value):
    from backend.agent.qwen_tool_protocol import parse_qwen_protocol
    calls = [{'function': {'name':'read_file','arguments':{'file_path':value}}}]
    text = render([{'role':'user','content':'go'}, {'role':'assistant','content':'','tool_calls':calls}])
    controls = text[text.index('<tool_call>'):text.rindex('</tool_call>')+len('</tool_call>')]
    schemas = {'read_file': {'type':'object','properties':{'file_path':{'type':'string'}},'required':['file_path']}}
    assert parse_qwen_protocol(controls, tool_schemas=schemas).calls[0].arguments == {'file_path':value}
