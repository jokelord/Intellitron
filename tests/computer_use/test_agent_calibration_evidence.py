"""Opt-in calibration evidence must not perturb the Agent provider request."""
from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from PIL import Image
from smolagents.models import ChatMessage, MessageRole


def _trusted_message(session_id: str):
    from backend.agent.computer_use_history import mark_computer_use_observation

    image = Image.new('RGB', (64, 32), '#224466')
    mark_computer_use_observation(image, context_id=session_id,
                                  observation_id='obs-1', geometry_version=1)
    return ChatMessage(role=MessageRole.USER, content=[
        {'type': 'text', 'text': 'find target'}, {'type': 'image', 'image': image},
    ]), image


def test_opt_in_request_evidence_keeps_provider_payload_identical(tmp_path):
    from backend.agent.local_llm_model import LocalLLMModel
    from backend.llm_service import GenerationResult
    from backend.agent.computer_use_calibration_evidence import AgentCalibrationEvidence

    session_id = '11111111-1111-4111-8111-111111111111'
    message, image = _trusted_message(session_id)
    baseline_session = SimpleNamespace(session_id=session_id, is_child_context=False)
    enabled_session = SimpleNamespace(session_id=session_id, is_child_context=False)
    enabled_session.agent = SimpleNamespace(memory=SimpleNamespace(steps=[
        SimpleNamespace(observations_images=[image]),
    ]))
    enabled_session.computer_use_calibration_evidence = AgentCalibrationEvidence(
        session_id, output_root=tmp_path, run_id='evidence-run')
    services = []
    for session in (baseline_session, enabled_session):
        service = MagicMock()
        service.config.model.max_image_dimension = 1024
        service.chat.return_value = GenerationResult(text='accepted')
        LocalLLMModel(service, session=session).generate([message], request_id='request-1')
        services.append(service)

    assert services[0].chat.call_args == services[1].chat.call_args
    record = json.loads((tmp_path / 'evidence-run' / 'request-1' / 'request.json').read_text())
    assert record['session_id'] == session_id
    assert record['observation_id'] == 'obs-1'
    assert record['accepted'] is True
    assert record['provider_request']['request_id'] == 'request-1'
    sent = services[1].chat.call_args.kwargs['messages'][-1].content[0]['image_url']['url']
    assert sent.startswith('data:image/png;base64,')
    assert (tmp_path / 'evidence-run' / 'request-1' / 'model-visible.png').read_bytes() == base64.b64decode(sent.partition(',')[2])
    assert 'data:image/' not in (tmp_path / 'evidence-run' / 'request-1' / 'request.json').read_text()


def test_request_projection_redacts_unrelated_uploads_and_keeps_exact_hash(tmp_path):
    from backend.agent.computer_use_calibration_evidence import AgentCalibrationEvidence

    session_id = '11111111-1111-4111-8111-111111111111'
    sink = AgentCalibrationEvidence(session_id, output_root=tmp_path, run_id='redacted-run')
    trusted = 'data:image/png;base64,' + base64.b64encode(_png()).decode()
    other = 'data:image/png;base64,' + base64.b64encode(b'private-upload').decode()
    payload = {'request_id': 'request-1', 'messages': [
        {'role': 'system', 'content': 'fixed Agent prompt'},
        {'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': other}},
            {'type': 'image_url', 'image_url': {'url': trusted}},
            {'type': 'text', 'text': 'find magenta'}]}],
        'tools': [{'function': {'name': 'exec_command'}}]}
    assert sink.record_accepted_request('request-1', payload, trusted_urls={trusted},
                                        observation_id='obs-1', geometry_version=1)
    path = tmp_path / 'redacted-run' / 'request-1' / 'request.json'
    record = json.loads(path.read_text())
    assert other not in path.read_text() and trusted not in path.read_text()
    assert b'private-upload' not in (tmp_path / 'redacted-run' / 'request-1' / 'model-visible.png').read_bytes()
    assert record['provider_request']['messages'][0]['content'] == 'fixed Agent prompt'
    assert record['provider_request']['tools'] == payload['tools']
    assert record['provider_request_sha256'] == hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def test_empty_or_failed_provider_response_writes_no_evidence(tmp_path):
    from backend.agent.local_llm_model import LocalLLMModel
    from backend.llm_service import GenerationResult
    from backend.agent.computer_use_calibration_evidence import AgentCalibrationEvidence

    session_id = '11111111-1111-4111-8111-111111111111'
    message, image = _trusted_message(session_id)
    session = SimpleNamespace(session_id=session_id, is_child_context=False,
                              agent=SimpleNamespace(memory=SimpleNamespace(steps=[
                                  SimpleNamespace(observations_images=[image]),
                              ])))
    session.computer_use_calibration_evidence = AgentCalibrationEvidence(
        session_id, output_root=tmp_path, run_id='evidence-run')
    service = MagicMock()
    service.config.model.max_image_dimension = 1024
    service.chat.return_value = GenerationResult(text='')
    LocalLLMModel(service, session=session).generate([message], request_id='empty')
    assert not (tmp_path / 'evidence-run' / 'empty').exists()
    service.chat.side_effect = RuntimeError('provider failed')
    try:
        LocalLLMModel(service, session=session).generate([message], request_id='failed')
    except RuntimeError:
        pass
    assert not (tmp_path / 'evidence-run' / 'failed').exists()


def test_stream_evidence_keeps_provider_payload_and_history_unchanged(tmp_path):
    from backend.agent.local_llm_model import LocalLLMModel
    from backend.agent.computer_use_calibration_evidence import AgentCalibrationEvidence

    session_id = '11111111-1111-4111-8111-111111111111'
    message, image = _trusted_message(session_id)
    captured = []

    class Service:
        config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=1024))

        def chat(self, **kwargs):
            captured.append(kwargs)
            yield {'content': 'accepted'}

    for enabled in (False, True):
        session = SimpleNamespace(session_id=session_id, is_child_context=False,
                                  agent=SimpleNamespace(memory=SimpleNamespace(steps=[
                                      SimpleNamespace(observations_images=[image]),
                                  ])))
        if enabled:
            session.computer_use_calibration_evidence = AgentCalibrationEvidence(
                session_id, output_root=tmp_path, run_id='stream-run')
        list(LocalLLMModel(Service(), session=session).generate_stream([message], request_id='stream-1'))
        assert session.agent.memory.steps[0].observations_images == [image]
    assert captured[0] == captured[1]
    assert (tmp_path / 'stream-run' / 'stream-1' / 'request.json').is_file()


def test_aborted_stream_and_untrusted_image_leave_no_evidence(tmp_path):
    from backend.agent.local_llm_model import LocalLLMModel
    from backend.agent.computer_use_calibration_evidence import AgentCalibrationEvidence

    session_id = '11111111-1111-4111-8111-111111111111'
    message, image = _trusted_message(session_id)
    session = SimpleNamespace(session_id=session_id, is_child_context=False,
                              agent=SimpleNamespace(memory=SimpleNamespace(steps=[
                                  SimpleNamespace(observations_images=[image]),
                              ])))
    session.computer_use_calibration_evidence = AgentCalibrationEvidence(
        session_id, output_root=tmp_path, run_id='aborted-run')

    class AbortedService:
        config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=1024))

        def chat(self, **kwargs):
            yield {'content': 'partial'}
            yield {'aborted': True}

    list(LocalLLMModel(AbortedService(), session=session).generate_stream([message], request_id='aborted'))
    assert not (tmp_path / 'aborted-run' / 'aborted').exists()

    wrong_session = SimpleNamespace(session_id='22222222-2222-4222-8222-222222222222',
                                    is_child_context=False, agent=session.agent,
                                    computer_use_calibration_evidence=session.computer_use_calibration_evidence)

    class AcceptedService:
        config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=1024))

        def chat(self, **kwargs):
            yield {'content': 'accepted'}

    list(LocalLLMModel(AcceptedService(), session=wrong_session).generate_stream(
        [message], request_id='wrong-context'))
    assert not (tmp_path / 'aborted-run' / 'wrong-context').exists()


def test_partial_evidence_write_failure_cannot_perturb_agent_request(tmp_path, monkeypatch):
    from backend.agent.local_llm_model import LocalLLMModel
    from backend.llm_service import GenerationResult
    from backend.agent.computer_use_calibration_evidence import AgentCalibrationEvidence

    session_id = '11111111-1111-4111-8111-111111111111'
    message, image = _trusted_message(session_id)
    session = SimpleNamespace(session_id=session_id, is_child_context=False,
                              agent=SimpleNamespace(memory=SimpleNamespace(steps=[
                                  SimpleNamespace(observations_images=[image]),
                              ])))
    session.computer_use_calibration_evidence = AgentCalibrationEvidence(
        session_id, output_root=tmp_path, run_id='failed-write-run')
    service = MagicMock()
    service.config.model.max_image_dimension = 1024
    service.chat.return_value = GenerationResult(text='accepted')
    original = Path.write_bytes

    def fail_evidence_png(path, data):
        if path.name == 'model-visible.png':
            raise OSError('disk failure')
        return original(path, data)

    monkeypatch.setattr(Path, 'write_bytes', fail_evidence_png)
    result = LocalLLMModel(service, session=session).generate([message], request_id='request-1')
    assert result.content == 'accepted'
    assert service.chat.call_count == 1
    assert not (tmp_path / 'failed-write-run' / 'request-1').exists()
    assert not list((tmp_path / 'failed-write-run').glob('.request-1-*'))


def test_evidence_records_only_single_same_context_agent_coordinate_decision(tmp_path):
    from backend.agent.computer_use_calibration_evidence import AgentCalibrationEvidence

    session_id = '11111111-1111-4111-8111-111111111111'
    sink = AgentCalibrationEvidence(session_id, output_root=tmp_path, run_id='evidence-run')
    sink.record_accepted_request('request-1', {
        'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {
            'url': 'data:image/png;base64,' + base64.b64encode(_png()).decode()}}]}],
        'tools': None, 'request_id': 'request-1'},
        trusted_urls={'data:image/png;base64,' + base64.b64encode(_png()).decode()},
        observation_id='obs-1', geometry_version=1)
    assert sink.record_decision('request-1', session_id, [
        {'name': 'exec_command', 'arguments': {'command':
            'vision_desktop inspect_region --x 200 --y 700 --zoom 4'}}])
    record = json.loads((tmp_path / 'evidence-run' / 'request-1' / 'decision.json').read_text())
    assert record['operation'] == 'inspect_region'
    assert record['arguments'] == {'x': 200.0, 'y': 700.0, 'zoom': 4}
    assert not sink.record_decision('request-1', 'different-context', [
        {'name': 'exec_command', 'arguments': {'command':
            'vision_desktop inspect_region --x 200 --y 700 --zoom 4'}}])


def test_agent_decision_hook_is_opt_in_and_does_not_mutate_tool_calls(tmp_path):
    from backend.agent.agent_runner import SmartToolCallingAgent
    from backend.agent.computer_use_calibration_evidence import AgentCalibrationEvidence
    from smolagents.models import ChatMessageToolCall, ChatMessageToolCallFunction

    session_id = '11111111-1111-4111-8111-111111111111'
    sink = AgentCalibrationEvidence(session_id, output_root=tmp_path, run_id='hook-run')
    png_url = 'data:image/png;base64,' + base64.b64encode(_png()).decode()
    sink.record_accepted_request('request-2', {
        'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': png_url}}]}],
        'tools': None, 'request_id': 'request-2'},
        trusted_urls={png_url}, observation_id='obs-1', geometry_version=1)
    command = 'vision_desktop inspect_region --x 200 --y 700 --zoom 4'
    calls = [ChatMessageToolCall(id='call-1', type='function', function=ChatMessageToolCallFunction(
        name='exec_command', arguments={'command': command}))]
    agent = object.__new__(SmartToolCallingAgent)
    agent._agent_session = SimpleNamespace(session_id=session_id, is_child_context=False,
                                            computer_use_calibration_evidence=sink)
    agent._record_calibration_decision('request-2', calls)
    assert calls[0].function.arguments == {'command': command}
    assert json.loads((tmp_path / 'hook-run' / 'request-2' / 'decision.json').read_text())['operation'] == 'inspect_region'
    agent._agent_session = SimpleNamespace(session_id=session_id, is_child_context=False)
    agent._record_calibration_decision('request-2', calls)
    assert calls[0].function.arguments == {'command': command}


def _png():
    buffer = BytesIO()
    Image.new('RGB', (3, 3), 'red').save(buffer, format='PNG')
    return buffer.getvalue()
