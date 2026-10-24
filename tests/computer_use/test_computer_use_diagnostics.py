"""Actual command/backend contracts with synthetic native bytes and no input."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import re

from PIL import Image
import pytest

from backend.computer_use.config import ComputerUseConfig
from backend.computer_use.contracts import CanonicalFrame, ImageArtifact, InputBinding
from backend.computer_use.diagnostics import DiagnosticSink
from backend.computer_use.errors import ComputerUseError
from backend.computer_use.executable_protocol import (
    clear_vision_desktop_backend, install_vision_desktop_backend,
    parse_vision_desktop_command,
)
from backend.computer_use.model_adapter import describe_stage, encode_stage, decode_stage
from backend.computer_use.executable_backend import VisionDesktopGroundingBackend
from backend.computer_use.runtime import GroundingRenderer
from backend.tools.sys_ops import exec_command


@pytest.mark.parametrize('zoom', [2, 4])
def test_zoom_config_parser_schema_prompt_decode_and_fingerprint(grounding_request, zoom):
    config = ComputerUseConfig(stage1_zoom=zoom)
    image = ImageArtifact(b'png', 1024, 1024, 'h', 'r')
    instance = describe_stage('stage1', image, None, grounding_request(), stage1_zoom=config.stage1_zoom)
    encoded = encode_stage(instance, image)
    schema = encoded.tools[0]['function']['parameters'] if 'function' in encoded.tools[0] else encoded.tools[0]['parameters']
    assert schema['properties']['zoom']['const'] == zoom
    assert f'zoom={zoom}' in encoded.messages[0]['content'][0]['text']
    assert parse_vision_desktop_command(f'vision_desktop inspect_region --x 321 --y 654 --zoom {zoom}').zoom == zoom
    assert decode_stage(encoded, {'name': 'inspect_region', 'arguments': {'x': 321, 'y': 654, 'zoom': zoom}}).zoom == zoom
    with pytest.raises(ComputerUseError, match='INVALID_ZOOM'):
        decode_stage(encoded, {'name': 'inspect_region', 'arguments': {'x': 321, 'y': 654, 'zoom': 6-zoom}})
    other = describe_stage('stage1', image, None, grounding_request(), stage1_zoom=6-zoom)
    assert instance.protocol_fingerprint != other.protocol_fingerprint


@pytest.mark.parametrize('bad', [True, 3, 2.0, '2'])
def test_zoom_rejects_non_contract_values(bad):
    with pytest.raises(ValueError):
        ComputerUseConfig(stage1_zoom=bad)


def test_pre_action_policy_defaults_and_config_mapping():
    assert ComputerUseConfig().pre_action_validation == 'binding_only'
    assert ComputerUseConfig.from_mapping({'pre_action_validation': 'frame_and_binding'}).pre_action_validation == 'frame_and_binding'
    for invalid in (False, None, 1, 'ignore_everything', []):
        with pytest.raises(ValueError):
            ComputerUseConfig(pre_action_validation=invalid)


def _test_backend(tmp_path, *, failure=None, blank=False, broken_sink=False, side=1024, debug=False, logging=True):
    from test_executable_backend import _capability
    capability = _capability(tmp_path)
    session_id = capability.session_id
    config = ComputerUseConfig(stage1_zoom=2, calibration_fingerprint='test-fingerprint',
        model_square_side_px=side, debug_save_images=debug,
        diagnostics_enabled=logging, diagnostics_path=str(tmp_path / 'events.jsonl'))
    binding = InputBinding('binding', session_id, 7, 'wayland', (-10, 20), (450, 300),
                           3, 'device', 'region', {}, True)
    raw = Image.new('RGB', (900, 600), '#123456').tobytes()
    if blank:
        raw = Image.new('RGB', (900, 600), 'white').tobytes()
    frame = CanonicalFrame('frame', 'observation', binding, 900, 600, raw,
                           3, 11, 100., 100.1, True, {}, {})

    class Engine:
        def __init__(self):
            self.config = config
            self.binding = binding
            self.renderer = GroundingRenderer(clock=lambda: 100.2)
            self.sent = 0
        async def observe(self, request, capability, check_abort):
            if failure:
                raise failure
            return SimpleNamespace(frame=frame, overview=self.renderer.overview(frame, config))
        async def inspect_region(self, request, observation, decision, check_abort):
            crop = self.renderer.crop(frame, decision, request, config)
            return SimpleNamespace(frame=frame, crop=crop, detail=self.renderer.detail(crop, config))
        async def interact_detail(self, *args):
            self.sent += 1
            return SimpleNamespace(state='ACTION_SENT')
    engine = Engine()
    sink = DiagnosticSink(config)
    if broken_sink:
        sink.write = lambda *_: (_ for _ in ()).throw(OSError('private target prose 321,654'))
    backend = VisionDesktopGroundingBackend(lambda *_: engine, diagnostics=sink)
    install_vision_desktop_backend(backend)
    return backend, engine, capability, tmp_path / 'events.jsonl'


def _run(capability, command):
    return exec_command(command, session_id=capability.session_id, capability=capability)


def _observe(capability):
    return _run(capability, "vision_desktop observe --target-description 'PRIVATE_TARGET_PROSE' --action left_click")


def _inspect(capability):
    return _run(capability, 'vision_desktop inspect_region --x 361 --y 500 --zoom 2')


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _no_coordinates(result):
    assert not re.search(r'\d|\bx=|\by=|source_rect|overview region', result.text)


@pytest.mark.parametrize('stream_outputs', [True, False])
@pytest.mark.parametrize('side,debug', [(1280, False), (1680, True), (2048, True)])
def test_real_executable_wrapper_agent_submits_latest_detail_png(tmp_path, monkeypatch, stream_outputs, side, debug):
    import base64
    import hashlib
    from smolagents.memory import ActionStep
    from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction
    from smolagents.monitoring import Timing
    from backend.agent.agent_runner import AgentRunner, SmartToolCallingAgent, _create_tool_wrapper, current_session_context
    from backend.agent.local_llm_model import LocalLLMModel
    from backend.llm_service import GenerationResult

    import backend.computer_use.debug_images as debug_images
    vision = tmp_path / 'vision'
    monkeypatch.setattr(debug_images, '_VISION_ROOT', vision)
    backend, engine, capability, path = _test_backend(tmp_path, side=side, debug=debug)
    session = SimpleNamespace(session_id=capability.session_id, is_child_context=False,
        answer_now=False, answering_now=False, aborted=False, behavior_records={},
        computer_use_history_omission_stage=None, computer_use_history_projection_enabled=False)
    class Service:
        config = SimpleNamespace(model=SimpleNamespace(max_image_dimension=64), computer_use=engine.config)
        def __init__(self):
            self.requests = []
        def get_stream_usage(self, *args, **kwargs):
            return None
        def chat(self, **kwargs):
            self.requests.append(kwargs)
            return iter([{'content': 'accepted'}]) if kwargs['stream'] else GenerationResult(text='accepted')
    service = Service()
    tool = _create_tool_wrapper(exec_command, 'exec_command')
    assert tool.output_type == 'any'
    agent = SmartToolCallingAgent(tools=[tool], model=LocalLLMModel(service, session=session),
                                  stream_outputs=stream_outputs)
    session.agent = session.active_agent = agent
    agent._agent_session = session
    AgentRunner._bind_tool_runtime_context(agent, capability.session_id, None, capability)
    current_session_context.set(capability.session_id)
    try:
        for index, command in enumerate([
            "vision_desktop observe --target-description 'PRIVATE_TARGET_PROSE' --action left_click",
            'vision_desktop inspect_region --x 361 --y 500 --zoom 2',
        ], 1):
            step = ActionStep(step_number=index, timing=Timing(start_time=index))
            call = ChatMessageToolCall(id=f'call-{index}', type='function',
                function=ChatMessageToolCallFunction(name='exec_command', arguments={'command': command}))
            list(agent.process_tool_calls(ChatMessage(role='assistant', content='', tool_calls=[call]), step))
            assert step.observations_images and len(step.observations_images) == 1
            agent.memory.steps.append(step)
            list(agent._step_stream(ActionStep(step_number=10 + index, timing=Timing(start_time=10 + index))))
        overview, detail = list(agent.memory.steps)
        # Real _step_stream captures the projection, then LocalLLMModel prepares
        # the exact lossless image URL received by the spy provider.
        assert not overview.observations_images and detail.observations_images
        assert session.computer_use_history_omission_stage is None
        # A subsequent same-detail call has no pending stage; it still logs and
        # submits the latest screenshot rather than losing diagnostic coverage.
        list(agent._step_stream(ActionStep(step_number=4, timing=Timing(start_time=4))))
        records = _records(path)
        backend_images = [r for r in records if r['phase'] == 'backend' and r['operation'] in {'observe', 'inspect_region'}]
        backend_detail = backend_images[1]
        pngs = []
        requests = [r for r in records if r['phase'] == 'llm_request']
        accepted = [r for r in records if r['phase'] == 'llm_accepted']
        assert len(requests) == len(accepted) == 3
        for record, request, expected in zip(requests, service.requests, [*backend_images, backend_detail]):
            urls = [part['image_url']['url'] for message in request['messages']
                    if isinstance(message.content, list) for part in message.content
                    if part.get('type') == 'image_url']
            assert len(urls) == record['prepared_image_count'] == record['prepared_trusted_image_count'] == 1
            png = base64.b64decode(urls[0].split(',', 1)[1])
            pngs.append(png)
            digest = hashlib.sha256(png).hexdigest()
            assert digest == record['png_sha256'] == expected['png_sha256']
            assert record['image_size'] == expected['image_size'] == [side, side]
            assert record['latest_image_present'] and record['latest_image_match_count'] == 1
            assert record['session_id'] == capability.session_id and record['untrusted_image_count'] == 0
            assert record['request_id'] == request['request_id'] and record['request_id']
            assert request['preserve_prompt_shape']
        assert [r['omitted_image_count'] for r in requests] == [0, 1, 0]
        if debug:
            saved = sorted(vision.glob('*.png'))
            assert len(saved) == 2
            for index, record in enumerate(backend_images, 1):
                artifact_path = Path(record['debug_image_path'])
                assert f'-stage{index}-' in artifact_path.name
                assert artifact_path.read_bytes() == pngs[index - 1]
                assert artifact_path.stat().st_mode & 0o777 == 0o600
                assert str(artifact_path) not in repr(service.requests)
            assert vision.stat().st_mode & 0o777 == 0o700
        else:
            assert not vision.exists()
            assert all('debug_image_path' not in r for r in records)
        assert all(record['result'] == 'accepted' and not record['fallback'] for record in accepted)
        assert 'PRIVATE_TARGET_PROSE' not in path.read_text() and 'base64' not in path.read_text()
    finally:
        current_session_context.clear()
        clear_vision_desktop_backend()


@pytest.mark.parametrize('mode', ['aborted', 'error', 'fallback', 'canceled'])
def test_delivery_metadata_records_abort_error_and_fallback_identity(tmp_path, mode):
    from backend.agent.computer_use_history import mark_computer_use_observation
    from backend.agent.local_llm_model import LocalLLMModel
    from backend.llm_service import GenerationResult
    from smolagents.memory import ActionStep
    from smolagents.models import ChatMessage
    from smolagents.monitoring import Timing
    image = mark_computer_use_observation(Image.new('RGB', (2, 3)),
        context_id='delivery-test-context', observation_id='observation', geometry_version=1)
    session = SimpleNamespace(session_id='delivery-test-context', is_child_context=False,
        agent=SimpleNamespace(memory=SimpleNamespace(steps=[ActionStep(step_number=1,
            timing=Timing(start_time=1), observations_images=[image])])),
        aborted=False, answer_now=False, answering_now=False, current_agent_name='basic_agent')
    diagnostic_config = ComputerUseConfig(diagnostics_enabled=True, diagnostics_path=str(tmp_path / 'delivery.jsonl'))
    class Service:
        config = SimpleNamespace(computer_use=diagnostic_config, model=SimpleNamespace(max_image_dimension=1))
        def chat(self, **kwargs):
            if mode == 'error':
                def failing():
                    raise RuntimeError('PRIVATE_TARGET_PROSE')
                    yield
                return failing()
            if mode == 'aborted':
                return iter([{'reasoning': 'private reasoning'}, {'aborted': True}])
            if mode == 'canceled':
                session.aborted = True
                return iter([{'content': 'late answer'}])
            return iter(()) if kwargs['stream'] else GenerationResult(text='fallback')
    model = LocalLLMModel(Service(), session=session)
    messages = [ChatMessage(role='user', content=[{'type': 'image', 'image': image}])]
    if mode == 'error':
        with pytest.raises(RuntimeError, match='PRIVATE_TARGET_PROSE'):
            list(model.generate_stream(messages, request_id='delivery-request'))
    else:
        list(model.generate_stream(messages, request_id='delivery-request'))
    records = _records(tmp_path / 'delivery.jsonl')
    accepted = [record for record in records if record['phase'] == 'llm_accepted']
    if mode in {'error', 'aborted'}:
        assert not accepted
        assert records[-1]['phase'] == 'llm_completed' and records[-1]['result'] == mode
    elif mode == 'fallback':
        assert len(accepted) == 1 and accepted[0]['fallback']
        assert [record['fallback'] for record in records if record['phase'] == 'llm_request'] == [False, True]
    else:
        assert len(accepted) == 1 and accepted[0]['result'] == 'canceled'
    assert all(record['latest_image_present'] and record['prepared_trusted_image_count'] == 1 for record in records)
    assert 'PRIVATE_TARGET_PROSE' not in json.dumps(records) and 'private reasoning' not in json.dumps(records)


def test_actual_exec_success_logs_internal_mapping_without_observation_echo(tmp_path):
    backend, engine, capability, path = _test_backend(tmp_path)
    try:
        observation = _observe(capability)
        detail = _inspect(capability)
        assert observation.state == 'OBSERVATION_READY'
        assert detail.state == 'DETAIL_READY'
        for result in (observation, detail):
            _no_coordinates(result)
        state = backend._states[capability.session_id]
        crop = state.detail.crop
        x = (325-crop.x0)/crop.crop_width*1000
        y = (300-crop.y0)/crop.crop_height*1000
        result = _run(capability, f'vision_desktop interact_detail --x {round(x)} --y {round(y)} --action left_click')
        assert result.state == 'ACTION_SENT' and engine.sent == 1
        _no_coordinates(result)
        records = _records(path)
        assert [r['operation'] for r in records] == ['observe', 'inspect_region', 'interact_detail']
        assert records[1]['stage1_point'] == [361, 500]
        assert records[1]['selected_zoom'] == 2
        action = records[2]
        assert action['crop_id'] == crop.crop_id
        assert action['source_rect'] == [crop.x0, crop.y0, crop.x1, crop.y1]
        assert abs(action['source_point'][0]-325) <= 1
        assert action['frame_sequence'] == 11 and action['stream_generation'] == 3
        assert action['logical_size'] == [450, 300]
        assert 'recognition' not in records[0] and 'witness_status' not in records[0]
        assert 'PRIVATE_TARGET_PROSE' not in path.read_text()
        assert 'base64' not in path.read_text() and 'rgb' not in path.read_text()
    finally:
        clear_vision_desktop_backend()


@pytest.mark.parametrize('blank', [True, False])
def test_arbitrary_screenshot_and_off_center_point_reach_common_engine(tmp_path, blank):
    backend, engine, capability, path = _test_backend(tmp_path, blank=blank)
    try:
        _observe(capability)
        _inspect(capability)
        result = _run(capability, 'vision_desktop interact_detail --x 901 --y 654 --action left_click')
        assert result.state == 'ACTION_SENT' and engine.sent == 1
        _no_coordinates(result)
        record = _records(path)[-1]
        assert record['source_point'] and record['stage2_point'] == [901, 654]
        assert record['crop_id'] and record['geometry_version'] == 7
        assert not any(key.startswith('witness') or key.startswith('recognition') for key in record)
        assert not hasattr(backend, '_pre_dispatch_guard')
    finally:
        clear_vision_desktop_backend()


@pytest.mark.parametrize('failure', [
    ComputerUseError('CAPTURE_STALLED', 'sensitive coords x=321 y=654 target prose'),
    ValueError('crop=(123,456,789,900)'),
    RuntimeError('native target PRIVATE_TARGET_PROSE x=321 y=654'),
])
def test_actual_exec_native_and_validation_errors_are_logged_privately(tmp_path, failure):
    _, engine, capability, path = _test_backend(tmp_path, failure=failure)
    try:
        result = _observe(capability)
        _no_coordinates(result)
        assert result.attachment is None and engine.sent == 0
        record = _records(path)[0]
        assert record['error_type'] == type(failure).__name__
        assert record['error_code'] == result.state
        # Timestamp fractions and elapsed time may legitimately contain the
        # same digits as private coordinates; check structured fields instead.
        assert set(record) <= {
            'schema_version', 'recorded_at', 'session_id', 'context_nonce',
            'sequence', 'operation', 'phase', 'elapsed_ms', 'result',
            'error_code', 'error_type', 'error_field', 'action',
        }
        assert record['operation'] == 'observe'
        assert record['action'] == 'left_click'
        assert record.get('error_field') is None
        assert 'PRIVATE_TARGET_PROSE' not in json.dumps(record)
        assert 'sensitive coords' not in json.dumps(record)
        assert 'crop=(123,456,789,900)' not in json.dumps(record)
    finally:
        clear_vision_desktop_backend()


def test_actual_exec_preflight_errors_logged_without_raw_flags(tmp_path):
    _, engine, capability, path = _test_backend(tmp_path)
    try:
        for command in ('vision_desktop inspect_region --x 321 --y 654 --zoom 2',
                        'vision_desktop observe --x=321,654 bad'):
            result = _run(capability, command)
            assert result.state == 'REJECTED'
            _no_coordinates(result)
        result = exec_command('vision_desktop observe --target-description PRIVATE --action left_click',
                              session_id=capability.session_id, capability=None)
        assert result.state == 'REJECTED'
        _no_coordinates(result)
        records = _records(path)
        assert len(records) == 3
        assert records[0]['stage1_point'] == [321, 654]
        assert records[0]['phase'] == 'executable_preflight'
        assert 'x=321' not in path.read_text()
        assert engine.sent == 0
    finally:
        clear_vision_desktop_backend()


def test_diagnostic_failure_does_not_change_sent_action_result(tmp_path):
    backend, engine, capability, path = _test_backend(tmp_path, broken_sink=True)
    try:
        assert _observe(capability).state == 'OBSERVATION_READY'
        assert _inspect(capability).state == 'DETAIL_READY'
        result = _run(capability, 'vision_desktop interact_detail --x 500 --y 500 --action left_click')
        assert result.state == 'ACTION_SENT' and engine.sent == 1
        assert not path.exists()
    finally:
        clear_vision_desktop_backend()


def test_shared_path_concurrent_rotation_is_bounded_and_private(tmp_path):
    config = ComputerUseConfig(diagnostics_enabled=True, diagnostics_path=str(tmp_path/'audit.jsonl'),
                              diagnostics_max_bytes=4096, diagnostics_backup_count=2)
    sinks = [DiagnosticSink(config), DiagnosticSink(replace(config, diagnostics_path=str(tmp_path/'sub/../audit.jsonl')))]
    assert sinks[0]._lock is sinks[1]._lock
    def write(index):
        sinks[index % 2].write({'sequence': index, 'session_id': 'session', 'operation': 'observe',
                               'result': 'OBSERVATION_READY', 'prompt': 'SECRET_PROMPT',
                               'image': 'SECRET_BASE64', 'target_description': 'SECRET_TARGET',
                               'reasoning': 'SECRET_REASONING'})
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(180)))
    paths = list(tmp_path.glob('audit.jsonl*'))
    records = []
    for path in paths:
        if path.suffix == '.lock':
            continue
        assert path.stat().st_size <= 4096
        assert 'SECRET' not in path.read_text()
        records.extend(_records(path))
    assert len(paths) <= 4  # active + two backups + empty lock
    assert records and len({r['sequence'] for r in records}) == len(records)
    disabled = DiagnosticSink(replace(config, diagnostics_enabled=False, diagnostics_path=str(tmp_path/'disabled.jsonl')))
    disabled.write({'operation': 'observe'})
    assert not disabled.path.exists()


def test_reobserve_record_does_not_label_previous_crop_as_current(tmp_path):
    _, _, capability, path = _test_backend(tmp_path)
    try:
        _observe(capability)
        _inspect(capability)
        result = _run(capability, 'vision_desktop reobserve --reason wrong_region')
        assert result.state == 'OBSERVATION_READY'
        records = _records(path)
        assert records[1]['crop_id'] and records[1]['source_rect']
        assert 'crop_id' not in records[2] and 'source_rect' not in records[2]
        assert records[2]['frame_id'] == 'frame' and records[2]['reobserve_reason'] == 'wrong_region'
    finally:
        clear_vision_desktop_backend()


def test_generic_backend_pins_exact_capability_until_session_disposal(tmp_path):
    from backend.computer_use.executable_protocol import ExecutableEnvelope, VisionDesktopOperation, VisionDesktopExecutableBridge
    backend, _, capability, _ = _test_backend(tmp_path)
    bridge = VisionDesktopExecutableBridge(backend)
    observe = parse_vision_desktop_command("vision_desktop observe --target-description ordinary --action left_click")
    try:
        envelope = ExecutableEnvelope(1, 'invalid', 1, capability.session_id,
            VisionDesktopOperation.OBSERVE, observe.public_arguments)
        invalid = backend.execute(envelope, replace(capability, session_id='foreign'), None)
        assert invalid.state == 'INVALID_CONTEXT_BINDING'
        assert not backend._capabilities
        def call(command, cap=capability):
            return bridge.execute(command, session_id=capability.session_id, capability=cap)
        assert call(observe).state == 'OBSERVATION_READY'
        assert call(parse_vision_desktop_command('vision_desktop reobserve --reason ui_changed')).state == 'OBSERVATION_READY'
        assert backend._capabilities[capability.session_id] is capability
        replacement = replace(capability)
        rejected = call(parse_vision_desktop_command('vision_desktop inspect_region --x 500 --y 500 --zoom 2'), replacement)
        assert rejected.state == 'INVALID_CONTEXT_BINDING'
        assert backend._capabilities[capability.session_id] is capability
        bridge.dispose_session(capability.session_id)
        assert capability.session_id not in backend._capabilities
        assert capability.session_id not in backend._states
        assert call(observe, replacement).state == 'OBSERVATION_READY'
        assert backend._capabilities[capability.session_id] is replacement
    finally:
        bridge.dispose_session(capability.session_id)
        clear_vision_desktop_backend()


@pytest.mark.parametrize(('cleanup', 'last_event', 'expected'), [
    ('TARGET_CHANGED', None, 'DETAIL_FRAME_CHANGED'),
    ('PIPEWIRE_NO_PROCESS_CALLBACK', None, 'CAPTURE_STALLED'),
    ('PRIVATE_TARGET_PROSE x=321,y=654', None, 'CANCELLED'),
    ('STALE_FRAME', 4, 'ACTION_DELIVERY_UNKNOWN'),
])
def test_receipt_reason_privacy_and_unknown_delivery_remain_terminal(tmp_path, cleanup, last_event, expected):
    from backend.computer_use.contracts import DispatchReceipt
    backend, engine, capability, path = _test_backend(tmp_path)
    async def receipt(request, detail, *_args):
        return DispatchReceipt(request.request_id, detail.crop.crop_id, 'CANCELLED', last_event, cleanup,
            freshness={'age_seconds': .5, 'observed_sequence': 11, 'current_sequence': 12,
                       'global_score': .01, 'target_score': .02, 'prompt': 'PRIVATE_TARGET_PROSE'})
    engine.interact_detail = receipt
    try:
        _observe(capability)
        _inspect(capability)
        result = _run(capability, 'vision_desktop interact_detail --x 500 --y 500 --action left_click')
        assert result.state == expected
        _no_coordinates(result)
        assert 'PRIVATE_TARGET_PROSE' not in result.text and 'TARGET_CHANGED' not in result.text
        record = _records(path)[-1]
        assert record['last_possible_event'] == last_event
        assert record['dispatch_stage'] == ('validation' if last_event is None else 'input')
        assert set(record['freshness']) == {'age_seconds', 'observed_sequence', 'current_sequence', 'global_score', 'target_score'}
        assert 'PRIVATE_TARGET_PROSE' not in json.dumps(record)
        if last_event is not None:
            assert 'never replay' in result.text
        retry = _run(capability, 'vision_desktop interact_detail --x 500 --y 500 --action left_click')
        assert retry.state == 'REJECTED'
        assert backend._states[capability.session_id].terminal
    finally:
        clear_vision_desktop_backend()

@pytest.mark.parametrize('logging,blocked', [(False, False), (True, True)])
def test_debug_images_are_independent_and_disk_failure_does_not_block_input(tmp_path, monkeypatch, logging, blocked):
    import backend.computer_use.debug_images as debug_images
    vision = tmp_path / 'vision'
    if blocked:
        vision.write_text('user file')
    monkeypatch.setattr(debug_images, '_VISION_ROOT', vision)
    _, engine, capability, _ = _test_backend(tmp_path, debug=True, logging=logging)
    try:
        assert _observe(capability).state == 'OBSERVATION_READY'
        assert _inspect(capability).state == 'DETAIL_READY'
        assert _run(capability, 'vision_desktop interact_detail --x 500 --y 500 --action left_click').state == 'ACTION_SENT'
        assert engine.sent == 1
        if blocked:
            assert vision.read_text() == 'user file'
        else:
            assert len(list(vision.glob('*.png'))) == 2
    finally:
        clear_vision_desktop_backend()


def test_debug_retention_prunes_only_owned_files_and_uses_unique_names(tmp_path, monkeypatch):
    import backend.computer_use.debug_images as debug_images
    from io import BytesIO
    root = tmp_path / 'vision'
    root.mkdir()
    unrelated = root / 'user-screenshot.png'
    unrelated.write_bytes(b'user data')
    monkeypatch.setattr(debug_images, '_VISION_ROOT', root)
    output = BytesIO()
    Image.new('RGB', (2, 2)).save(output, format='PNG')
    artifact = SimpleNamespace(png=output.getvalue())
    config = ComputerUseConfig(debug_save_images=True, debug_image_max_files=2, debug_image_max_bytes=1024)
    envelope = SimpleNamespace(session_id='session', context_nonce='context', sequence=1)
    import os
    paths = []
    for index in range(3):
        saved = debug_images.save_debug_image(config, envelope, 'stage1', artifact)
        assert saved is not None
        paths.append(saved)
        # Retention orders filesystem mtime; make age distinct even on filesystems
        # whose timestamp precision causes rapid sequential writes to tie.
        os.utime(saved, ns=((index + 1) * 1000000000,) * 2)
    assert len(set(paths)) == 3 and all(paths)
    assert len([p for p in root.glob('*.png') if debug_images._OWNED_NAME.fullmatch(p.name)]) == 2
    assert not Path(paths[0]).exists() and Path(paths[-1]).read_bytes() == artifact.png
    assert unrelated.read_bytes() == b'user data'


def test_debug_config_types_and_retention_bounds():
    for fields in ({'debug_save_images': 1}, {'debug_image_max_files': 0},
                   {'debug_image_max_bytes': 0}, {'debug_image_max_files': False}):
        with pytest.raises((TypeError, ValueError)):
            ComputerUseConfig(**fields)
