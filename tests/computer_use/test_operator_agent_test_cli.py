"""Operator Agent experiment cannot change the deployed desktop authority."""
import copy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image


LIMITS = {
    'max_observation_age_seconds': 10,
    'max_sample_age_seconds': 1,
    'max_validation_to_dispatch_seconds': .1,
    'global_diff_threshold': .01,
    'target_diff_threshold': .01,
    'double_click_interval_seconds': .1,
}


def test_operator_test_app_is_isolated_and_uses_explicit_safety_limits():
    from backend.config import get_config
    from backend.computer_use.runtime import deployment_fingerprint
    from scripts.run_computer_use_agent_test import DEFAULT_HELPER, prepare_operator_test_app

    deployment = copy.deepcopy(get_config())
    assert deployment.computer_use.ready is False
    app, fingerprint = prepare_operator_test_app(
        deployment, helper_binary='/tmp/operator-helper', safety_limits=LIMITS)
    assert deployment.agent.allow_desktop_capture is True
    assert deployment.agent.allow_desktop_input is True
    assert deployment.computer_use.helper_binary == str(DEFAULT_HELPER)
    assert deployment.computer_use.calibration_fingerprint is None
    assert deployment.computer_use.ready is False
    assert app is not deployment
    assert app.agent.allow_desktop_capture is True
    assert app.agent.allow_desktop_input is True
    assert app.agent.entry_agent_name == 'basic_agent'
    assert app.agent.default_profile_tools['basic_agent'] == ('read_skill', 'exec_command')
    assert app.computer_use.calibration_report_path is None
    assert app.computer_use.pre_action_validation == deployment.computer_use.pre_action_validation == 'binding_only'
    assert app.computer_use.calibration_fingerprint == fingerprint
    assert app.computer_use.ready is True  # test-only safety config, no release report
    assert deployment_fingerprint(app) == deployment_fingerprint(deployment) == fingerprint
    assert all(getattr(app.computer_use, key) == value for key, value in LIMITS.items())


def test_operator_test_app_rejects_incomplete_limits_or_enabled_deployment():
    from backend.config import get_config
    from scripts.run_computer_use_agent_test import prepare_operator_test_app

    deployment = copy.deepcopy(get_config())
    missing = dict(LIMITS)
    del missing['target_diff_threshold']
    with pytest.raises(ValueError, match='exactly six'):
        prepare_operator_test_app(deployment, helper_binary='/tmp/operator-helper',
                                  safety_limits=missing)
    with pytest.raises(ValueError, match='absolute'):
        prepare_operator_test_app(deployment, helper_binary='helper', safety_limits=LIMITS)
    permissive = dict(LIMITS, global_diff_threshold=.5)
    with pytest.raises(ValueError, match='test safety cap'):
        prepare_operator_test_app(deployment, helper_binary='/tmp/operator-helper',
                                  safety_limits=permissive)
    deployment.computer_use = replace(
        deployment.computer_use, operator_session_enabled=False)
    with pytest.raises(ValueError, match='explicit operator session mode'):
        prepare_operator_test_app(deployment, helper_binary='/tmp/operator-helper',
                                  safety_limits=LIMITS)


def test_operator_audit_records_only_operation_status_not_images_or_arguments():
    from types import SimpleNamespace
    from scripts.run_computer_use_agent_test import AuditedOperatorBackend

    class Backend:
        def execute(self, envelope, capability, check_abort=None):
            return SimpleNamespace(state='DETAIL_READY', verified=True,
                                   image=b'secret image bytes')
        def dispose_session(self, session_id):
            pass

    audit = []
    wrapper = AuditedOperatorBackend(Backend(), session_id='test-session', audit=audit.append)
    envelope = SimpleNamespace(session_id='test-session', operation=SimpleNamespace(value='inspect_region'),
                               public_arguments={'x': 123, 'y': 456})
    result = wrapper.execute(envelope, object())
    assert result.state == 'DETAIL_READY'
    assert audit == [{'operation': 'inspect_region', 'state': 'DETAIL_READY',
                      'verified': True}]
    foreign = SimpleNamespace(
        session_id='other-session', operation=envelope.operation,
        public_arguments=envelope.public_arguments)
    rejected = wrapper.execute(foreign, object())
    assert rejected.state == 'INVALID_CONTEXT_BINDING'
    assert len(audit) == 1


def test_operator_test_builds_real_agent_with_only_skill_and_desktop_command_tools():
    from backend.config import get_config
    from backend.agent.agent_runner import AgentRunner
    from backend.llm_service import LLMService
    from backend.tools.skill_ops import list_skills
    from scripts.run_computer_use_agent_test import prepare_operator_test_app

    app, _ = prepare_operator_test_app(copy.deepcopy(get_config()),
                                       helper_binary='/tmp/operator-helper',
                                       safety_limits=LIMITS)
    runner = AgentRunner(LLMService(app), app)
    session = runner.get_or_create_session('b713eb8d-0290-4404-b8a7-ae979b85a885', mode='tool_calling')
    assert session.capability.allow_desktop_capture is True
    assert session.capability.allow_desktop_input is True
    assert set(session.agent.tools) >= {'read_skill', 'exec_command'}
    assert set(session.agent.tools) <= {'read_skill', 'exec_command', 'final_answer'}
    assert session.capability.allowed_executables == ('vision_desktop',)
    assert 'computer-use-vision' in list_skills()




















def test_check_only_never_constructs_runner_or_opens_portal(tmp_path, monkeypatch, capsys):
    from scripts import run_computer_use_agent_test as cli

    helper = tmp_path / 'helper'
    helper.write_text('#!/bin/sh\n')
    helper.chmod(0o700)
    def forbidden(*_args, **_kwargs):
        raise AssertionError('check-only attempted live Agent or Portal work')
    monkeypatch.setattr(cli, 'AgentRunner', forbidden)
    monkeypatch.setattr(cli, 'OperatorSessionRuntimeFactory', forbidden)
    argv = ['--target-description', 'ordinary visible button', '--check-only', '--enable-thinking', '--helper-binary', str(helper)]
    for name, value in LIMITS.items():
        argv.extend(['--' + name.replace('_', '-'), str(value)])
    assert cli.main(argv) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['check_only'] is True
    assert output['deployment_ready'] is False
    assert output['release_calibration_used'] is False
    assert output['thinking_enabled'] is True
    assert '--authorize-test-input' in output['test_command']
    assert '--check-only' not in output['test_command']


def test_checked_in_preset_and_one_flag_override_are_test_only(capsys):
    from backend.config import get_config
    from backend.computer_use.runtime import deployment_fingerprint
    from scripts import run_computer_use_agent_test as cli

    limits, timeouts = cli.load_operator_preset(cli.OPERATOR_CONFIG_PATH)
    assert set(limits) == set(cli.SAFETY_FIELDS)
    assert timeouts == {'transport_timeout_seconds': 15,
                        'authorization_timeout_seconds': 120}
    deployment = copy.deepcopy(get_config())
    app, fingerprint = cli.prepare_operator_test_app(
        deployment, helper_binary=str(cli.DEFAULT_HELPER),
        safety_limits=limits, test_timeouts=timeouts)
    assert app.computer_use.transport_timeout_seconds == 15
    assert app.computer_use.authorization_timeout_seconds == 120
    assert deployment.computer_use.transport_timeout_seconds == 5
    assert deployment_fingerprint(app) == deployment_fingerprint(deployment) == fingerprint
    assert cli.main(['--target-description', 'ordinary visible button', '--check-only', '--max-observation-age-seconds', '30']) == 0
    output = json.loads(capsys.readouterr().out)
    assert '--max-observation-age-seconds 30' in output['test_command']


@pytest.mark.parametrize('case', ['ordinary', 'stale', 'old_capture', 'scene_changed', 'geometry', 'capability', 'disconnected'])
def test_web_agent_exec_command_generic_input_and_common_guards(tmp_path, case, monkeypatch):
    import time
    from backend.config import get_config
    from backend.agent.agent_runner import AgentRunner
    from backend.llm_service import LLMService
    from backend.computer_use.contracts import CanonicalFrame, InputBinding
    from backend.computer_use.runtime import build_operator_session_backend
    from backend.computer_use.executable_protocol import (
        install_vision_desktop_backend, clear_vision_desktop_backend)

    app = copy.deepcopy(get_config())
    app.computer_use = replace(app.computer_use, diagnostics_path=str(tmp_path / "computer-use.jsonl"))
    def no_scene_comparison(*_args, **_kwargs):
        raise AssertionError('binding-only dispatch must not compare pixels')
    monkeypatch.setattr('backend.computer_use.dispatch.check_freshness', no_scene_comparison)
    created = []
    class FakeNative:
        def __init__(self, **kwargs):
            self.owner_session_id = kwargs['owner_session_id']
            self.channel = None
            self.events = []
            self.sequence = 0
            created.append(self)
        def start(self, binding_id, capability, **_kwargs):
            self.channel = object()
            self.binding = InputBinding(binding_id, self.owner_session_id, 1,
                                        'wayland', (0, 0), (500, 500), 1,
                                        'fake-device', 'fake-region', {}, True)
            return self.binding
        def freeze(self, _binding_id):
            self.sequence += 1
            now = time.monotonic()
            captured = now - 180 if case in {'stale', 'old_capture'} else now
            if self.sequence > 1:
                raise AssertionError('binding-only dispatch must not capture another frame')
            return CanonicalFrame(str(self.sequence), str(self.sequence), self.binding,
                                  500, 500, Image.new('RGB', (500, 500), 'white').tobytes(),
                                  1, self.sequence, captured, now, True, {}, {})
        def resolve_binding(self):
            if case == 'disconnected':
                from backend.computer_use.native_client import NativeUnavailable
                raise NativeUnavailable('PIPEWIRE_DISCONNECTED_AFTER_STREAMING')
            return replace(self.binding, geometry_version=2) if case == 'geometry' else self.binding
        def send_event(self, *args, deadline_monotonic=None):
            assert deadline_monotonic > time.monotonic()
            self.events.append(args)
        def set_abort_check(self, _check):
            pass
        def close(self):
            self.channel = None

    backend = build_operator_session_backend(app, native_factory=FakeNative)
    runner = AgentRunner(LLMService(app), app)
    session = runner.get_or_create_session('ed1e04af-8910-4808-97de-c1aa0ae4b251',
                                           mode='tool_calling')
    assert session.capability.allow_desktop_capture is True
    assert session.capability.allow_desktop_input is True
    assert {'exec_command', 'read_skill', 'read_file'} <= set(session.agent.tools)
    install_vision_desktop_backend(backend)
    try:
        result = session.agent.tools['exec_command'](
            command="vision_desktop observe --target-description 'ordinary visible button' --action left_click")
        assert result.state == 'OBSERVATION_READY'
        assert result.attachment is not None
        assert result.text == "desktop observation ready"
        assert len(created) == 1
        assert created[0].owner_session_id == session.session_id
        detail = session.agent.tools['exec_command'](
            command=f'vision_desktop inspect_region --x 500 --y 500 --zoom {app.computer_use.stage1_zoom}')
        assert detail.state == 'DETAIL_READY'
        if case == 'capability':
            session.agent.tools['exec_command']._injected_capability = replace(session.capability)
        dispatched = session.agent.tools['exec_command'](
            command='vision_desktop interact_detail --x 901 --y 654 --action left_click')
        if case in {'ordinary', 'stale', 'old_capture', 'scene_changed'}:
            assert dispatched.state == 'ACTION_SENT' and len(created[0].events) == 4
            assert created[0].sequence == 1
            records = [json.loads(line) for line in (tmp_path / 'computer-use.jsonl').read_text().splitlines()]
            assert records[-1]['pre_action_validation'] == 'binding_only'
            assert records[-1]['freshness'] == {}
        else:
            assert dispatched.state != 'ACTION_SENT' and not created[0].events
            if case == 'capability':
                assert dispatched.state == 'INVALID_CONTEXT_BINDING'
            elif case == 'geometry':
                assert dispatched.state == 'GEOMETRY_CHANGED'
        assert not hasattr(backend, '_witnesses')
    finally:
        backend.dispose_session(session.session_id)
        clear_vision_desktop_backend()


def test_operator_cli_initializes_real_provider_before_agent(tmp_path, monkeypatch):
    from scripts import run_computer_use_agent_test as cli

    events = []
    class FakeService:
        active_backend = 'sglang'
        def __init__(self, _app):
            events.append('service')
        def initialize(self):
            events.append('initialize')
            return True
    def stop_at_runner(_service, _app):
        events.append('agent')
        raise RuntimeError('stop before any native or model call')
    monkeypatch.setattr(cli, 'EVIDENCE_ROOT', tmp_path)
    monkeypatch.setattr(cli, 'LLMService', FakeService)
    monkeypatch.setattr(cli, 'AgentRunner', stop_at_runner)
    with pytest.raises(RuntimeError, match='stop before any native'):
        cli.main(['--target-description', 'ordinary visible button', '--authorize-test-input'])
    assert events == ['service', 'initialize', 'agent']
