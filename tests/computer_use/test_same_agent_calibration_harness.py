"""Offline calibration drives a real Agent context with no desktop capability."""
from __future__ import annotations

import copy
from dataclasses import replace
import json
from PIL import Image
import pytest

from backend.llm_service import GenerationResult


class ScriptedService:
    def __init__(self, config):
        self.config = config
        self.calls = []

    def get_stream_usage(self, *args, **kwargs):
        return None

    def discard_stream_usage(self, *args, **kwargs):
        return None

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs['stream']:
            return iter(())
        stage = sum(not call['stream'] for call in self.calls) - 1
        if stage == 0:
            command = "vision_desktop observe --target-description 'center of the small magenta square' --action left_click"
        elif stage == 1:
            command = f'vision_desktop inspect_region --x 200 --y 700 --zoom {self.config.computer_use.stage1_zoom}'
        elif stage == 2:
            command = 'vision_desktop interact_detail --x 500 --y 500 --action left_click'
        else:
            return GenerationResult(text='calibration point recorded')
        return GenerationResult(text='', tool_calls=[{
            'id': f'call-{stage}', 'type': 'function',
            'function': {'name': 'exec_command', 'arguments': json.dumps({'command': command})},
        }])


def test_fixture_harness_repeats_real_agent_context_without_desktop_authority(tmp_path, monkeypatch):
    from backend.config import get_config
    from scripts.calibrate_computer_use import generate_fixtures
    from scripts.collect_computer_use_same_agent import run_fixture_calibration
    from backend.computer_use.runtime import deployment_fingerprint
    import backend.computer_use.executable_protocol as executable_protocol

    def forbidden_live_backend(*args, **kwargs):
        raise AssertionError('offline harness reached a live desktop backend')

    monkeypatch.setattr(executable_protocol, 'execute_vision_desktop_command', forbidden_live_backend)

    app = copy.deepcopy(get_config())
    app.computer_use = replace(app.computer_use, diagnostics_enabled=False, stage1_zoom=2, model_square_side_px=1024)
    assert app.agent.allow_desktop_capture is True
    assert app.agent.allow_desktop_input is True
    fixtures = tmp_path / 'fixtures'
    generate_fixtures(app.computer_use, deployment_fingerprint(app), fixtures)
    services = []

    def service_factory():
        service = ScriptedService(app)
        services.append(service)
        return service

    output = tmp_path / 'computer-use-diagrams' / 'calibration-evidence'
    measured = run_fixture_calibration(
        app, fixtures=fixtures, output_dir=output, repeats=2,
        service_factory=service_factory, case_ids=('case-0-stage1', 'case-1-stage1'))
    assert len(measured['cases']) == 2
    assert all(len(case['outputs']) == 2 for case in measured['cases'])
    # At this fixture's explicit 2x zoom, rough x=.2 clips the crop to [0,.45];
    # Stage-2's independent midpoint maps to .225 in the source, not .2.
    assert app.computer_use.stage1_zoom == 2
    assert measured['cases'][0]['outputs'] == [[.225, .7], [.225, .7]]
    assert measured['cases'][1]['outputs'] != [measured['cases'][1]['target_ratio']] * 2
    assert measured['serving_preprocessing'] is None
    assert measured['desktop_geometry'] is None
    assert measured.get('passed') is not True
    assert len(services) == 4
    assert all(any(call['stream'] for call in service.calls) for service in services)
    for case in measured['cases']:
        for run in case['runs']:
            assert run['input_events_sent'] == 0
            evidence = output / run['evidence_run_id']
            requests = [json.loads(path.read_text()) for path in evidence.glob('*/request.json')]
            requests.sort(key=lambda row: row['turn_index'])
            assert len(requests) >= 2
            assert len(list(evidence.glob('*/decision.json'))) >= 2
            assert all(row['session_id'] == run['session_id'] for row in requests)
            assert requests[0]['turn_index'] == 1
            assert requests[1]['turn_index'] == 2
            assert requests[0]['provider_request']['messages'][0] == requests[1]['provider_request']['messages'][0]
            assert 'exec_command' in json.dumps(requests[0]['provider_request']['messages'][0])
            assert len(requests[1]['provider_request']['messages']) > len(requests[0]['provider_request']['messages'])
            with Image.open(evidence / requests[0]['request_id'] / 'model-visible.png') as stage1:
                assert stage1.size == (app.computer_use.model_square_side_px,) * 2
            with Image.open(evidence / requests[1]['request_id'] / 'model-visible.png') as stage2:
                assert stage2.size == (1024, 1024)
            assert all('data:image/' not in (evidence / row['request_id'] / 'request.json').read_text()
                       for row in requests)
    assert (output / 'samples.json').is_file()


def test_real_collector_initializes_provider_before_first_inference(tmp_path, monkeypatch):
    from backend.config import get_config
    from backend.computer_use.runtime import deployment_fingerprint
    from scripts.calibrate_computer_use import generate_fixtures
    from scripts import collect_computer_use_same_agent as collector

    app = copy.deepcopy(get_config())
    app.computer_use = replace(app.computer_use, diagnostics_enabled=False, stage1_zoom=2, model_square_side_px=1024)
    fixtures = tmp_path / 'fixtures'
    generate_fixtures(app.computer_use, deployment_fingerprint(app), fixtures)
    services = []
    class CheckedService(ScriptedService):
        active_backend = 'sglang'
        def __init__(self, config):
            super().__init__(config)
            self.initialized = False
            services.append(self)
        def initialize(self):
            self.initialized = True
            return True
        def chat(self, **kwargs):
            assert self.initialized
            return super().chat(**kwargs)
    monkeypatch.setattr(collector, 'LLMService', CheckedService)
    monkeypatch.setattr(collector, 'DIAGRAMS_ROOT', tmp_path)
    measured = collector.run_fixture_calibration(
        app, fixtures=fixtures, output_dir=tmp_path / 'evidence', repeats=2,
        case_ids=('case-0-stage1',))
    assert len(services) == 2
    assert all(service.initialized and service.calls for service in services)
    assert all(run['input_events_sent'] == 0 for run in measured['cases'][0]['runs'])


def test_real_model_harness_rejects_non_repo_evidence_path(tmp_path):
    from backend.config import get_config
    from scripts.collect_computer_use_same_agent import run_fixture_calibration

    with pytest.raises(ValueError, match='computer-use-diagrams/calibration-evidence'):
        run_fixture_calibration(get_config(), fixtures=tmp_path,
                                output_dir=tmp_path / 'outside-repo')


@pytest.mark.parametrize('name,arguments', [
    ('exec_command', {'command': 'echo forbidden'}),
    ('read_file', {'path': '/etc/passwd'}),
])
def test_fixture_executor_blocks_unrelated_tools_without_native_or_shell(name, arguments, monkeypatch):
    from backend.config import get_config
    from backend.computer_use.runtime import deployment_fingerprint
    from scripts.collect_computer_use_same_agent import FixtureDesktop
    import backend.computer_use.executable_protocol as executable_protocol
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError('a real subprocess or native call was attempted')

    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(executable_protocol, 'execute_vision_desktop_command', forbidden)
    app = copy.deepcopy(get_config())
    app.computer_use = replace(app.computer_use, diagnostics_enabled=False, stage1_zoom=2, model_square_side_px=1024)
    fixture = FixtureDesktop(session_id='offline-test', source=Image.new('RGB', (1280, 720)),
                             config=app.computer_use, fingerprint=deployment_fingerprint(app))
    with pytest.raises((RuntimeError, ValueError), match='OFFLINE_CALIBRATION_TOOL_BLOCKED|vision_desktop'):
        fixture.execute(name, arguments)
    assert fixture.input_events_sent == 0
    assert fixture.request is None


def test_fixture_guard_rejects_profile_switch_and_managed_children():
    from scripts.collect_computer_use_same_agent import _guard_agent_tools

    class StubAgent:
        def __init__(self, managed_agents=None):
            self.tools = {}
            self.managed_agents = managed_agents or {}

    class StubRunner:
        def _build_profile_agent(self, *args, **kwargs):
            return StubAgent()

    runner = StubRunner()
    _guard_agent_tools(runner, object())
    with pytest.raises(RuntimeError, match='OFFLINE_CALIBRATION_PROFILE_BLOCKED'):
        runner._build_profile_agent('manager_agent')

    class ManagedRunner:
        def _build_profile_agent(self, *args, **kwargs):
            return StubAgent({'child': object()})

    managed = ManagedRunner()
    _guard_agent_tools(managed, object())
    with pytest.raises(RuntimeError, match='OFFLINE_CALIBRATION_MANAGED_AGENT_BLOCKED'):
        managed._build_profile_agent('basic_agent')


def test_fixture_harness_preserves_completed_runs_if_next_service_fails(tmp_path):
    from backend.config import get_config
    from backend.computer_use.runtime import deployment_fingerprint
    from scripts.calibrate_computer_use import generate_fixtures
    from scripts.collect_computer_use_same_agent import run_fixture_calibration

    app = copy.deepcopy(get_config())
    app.computer_use = replace(app.computer_use, diagnostics_enabled=False, stage1_zoom=2, model_square_side_px=1024)
    fixtures = tmp_path / 'fixtures'
    generate_fixtures(app.computer_use, deployment_fingerprint(app), fixtures)
    output = tmp_path / 'evidence'
    constructed = 0

    def service_factory():
        nonlocal constructed
        constructed += 1
        if constructed == 2:
            raise RuntimeError('simulated second-run startup failure')
        return ScriptedService(app)

    with pytest.raises(RuntimeError, match='simulated second-run startup failure'):
        run_fixture_calibration(app, fixtures=fixtures, output_dir=output, repeats=2,
                                service_factory=service_factory, case_ids=('case-0-stage1',))
    partial = json.loads((output / 'samples.json').read_text())
    assert partial['passed'] is False
    assert partial['cases'][0]['outputs'] == [[.225, .7]]
    assert partial['cases'][0]['runs'][0]['input_events_sent'] == 0

    resumed_services = []
    def resumed_factory():
        service = ScriptedService(app)
        resumed_services.append(service)
        return service

    resumed = run_fixture_calibration(app, fixtures=fixtures, output_dir=output, repeats=2,
                                      service_factory=resumed_factory,
                                      case_ids=('case-0-stage1',), resume=True)
    assert len(resumed_services) == 1
    assert resumed['cases'][0]['outputs'] == [[.225, .7], [.225, .7]]
    assert resumed['cases'][0]['runs'][0] == partial['cases'][0]['runs'][0]
    assert resumed['passed'] is False


def test_fixture_resume_rejects_changed_target_without_model_call(tmp_path):
    from backend.config import get_config
    from backend.computer_use.runtime import deployment_fingerprint
    from scripts.calibrate_computer_use import generate_fixtures
    from scripts.collect_computer_use_same_agent import run_fixture_calibration

    app = copy.deepcopy(get_config())
    app.computer_use = replace(app.computer_use, diagnostics_enabled=False, stage1_zoom=2, model_square_side_px=1024)
    fixtures = tmp_path / 'fixtures'
    generate_fixtures(app.computer_use, deployment_fingerprint(app), fixtures)
    output = tmp_path / 'evidence'
    run_fixture_calibration(app, fixtures=fixtures, output_dir=output, repeats=2,
                            service_factory=lambda: ScriptedService(app),
                            case_ids=('case-0-stage1',))
    samples = output / 'samples.json'
    prior = json.loads(samples.read_text())
    prior['cases'][0]['target_ratio'] = [.9, .9]
    samples.write_text(json.dumps(prior))

    def forbidden_service():
        raise AssertionError('resume reached model after mismatch')

    with pytest.raises(ValueError, match='resume.*fixture'):
        run_fixture_calibration(app, fixtures=fixtures, output_dir=output, repeats=2,
                                service_factory=forbidden_service,
                                case_ids=('case-0-stage1',), resume=True)
