from types import SimpleNamespace
import pytest


def test_probe_requires_explicit_authorization_before_loading_config(monkeypatch):
    from scripts import probe_computer_use_live as probe
    monkeypatch.setattr(probe, 'get_config', lambda: pytest.fail('must not load/spawn'))
    with pytest.raises(SystemExit):
        probe.main([])


def test_probe_refuses_default_disabled_capabilities_before_spawn(monkeypatch):
    import copy
    from backend.config import get_config
    from backend.security.capabilities import CapabilityError
    from scripts import probe_computer_use_live as probe
    app = copy.deepcopy(get_config())
    app.agent.allow_desktop_capture = False
    app.agent.allow_desktop_input = False
    monkeypatch.setattr(probe, 'NativeClient', lambda **kwargs: pytest.fail('must not spawn'))
    with pytest.raises(CapabilityError):
        probe.run_probe(app)


@pytest.mark.parametrize("backend", ["wayland", "x11"])
def test_live_probe_reports_metadata_and_closes_without_input(tmp_path, monkeypatch, backend):
    import hashlib
    from dataclasses import replace
    from backend.config import get_config
    from scripts import probe_computer_use_live as probe
    from test_stale_dispatch_audit import _fixture
    app = get_config()
    monkeypatch.setattr(app.agent, 'allow_desktop_capture', True)
    monkeypatch.setattr(app.agent, 'allow_desktop_input', True)
    monkeypatch.setattr(app, 'computer_use', replace(app.computer_use, helper_binary='/fake/helper'))
    binding, frame, *_ = _fixture()
    calls = []
    class Native:
        def __init__(self, **kwargs): calls.append('construct')
        def start(self, binding_id, capability, **kwargs):
            self.binding = replace(binding, binding_id=binding_id, session_id=capability.session_id,backend=backend)
            calls.append('start')
            return self.binding
        def freeze(self, binding_id):
            calls.append('freeze')
            return replace(frame,binding=self.binding)
        def close(self): calls.append('close')
        def send_event(self, *args, **kwargs): pytest.fail('probe must never emit input')
    monkeypatch.setattr(probe,'NativeClient',Native)
    result = probe.run_probe(app)
    assert calls == ['construct','start','freeze','close']
    assert result['frame']['sha256'] == hashlib.sha256(frame.rgb).hexdigest()
    assert result['input_events_sent'] == 0
    assert result['input_outcome'] == 'not_run'
    assert result['calibration'] == 'not_run'
    assert 'rgb' not in result['frame']
    assert result['backend'] == backend
    assert result['portal_authorization'] == ('verified' if backend == 'wayland' else 'not_applicable')
    assert result['eis_mapping'] == ('verified' if backend == 'wayland' else 'not_applicable')
    if backend == 'x11':
        assert result['x11_connection'] == 'verified'
        assert result['xrandr_mapping'] == 'verified'
        assert result['xtest_input'] == 'not_run'


def test_cli_authorization_is_ephemeral_and_requires_explicit_helper(monkeypatch):
    from backend.config import get_config
    from scripts import probe_computer_use_live as probe
    app = get_config()
    monkeypatch.setattr(probe,'get_config',lambda:app)
    old_capture = app.agent.allow_desktop_capture
    old_input = app.agent.allow_desktop_input
    old_helper = app.computer_use.helper_binary
    seen = []
    def run(config, **kwargs):
        assert config is not app
        assert config.agent.allow_desktop_capture is True
        assert config.agent.allow_desktop_input is True
        assert config.computer_use.helper_binary == '/tmp/probe-helper'
        assert config.computer_use.enabled == app.computer_use.enabled
        seen.append(True)
        return {'ready':False}
    monkeypatch.setattr(probe,'run_probe',run)
    assert probe.main(['--authorize','--helper-binary','/tmp/probe-helper']) == 0
    assert seen == [True]
    assert app.agent.allow_desktop_capture == old_capture
    assert app.agent.allow_desktop_input == old_input
    assert app.computer_use.helper_binary == old_helper
    with pytest.raises(SystemExit):
        probe.main(['--authorize'])


def test_probe_error_reports_phase_and_only_known_domain(monkeypatch, capsys):
    import json
    from backend.config import get_config
    from scripts import probe_computer_use_live as probe
    monkeypatch.setattr(probe,'get_config',get_config)
    def fail(*args, **kwargs):
        raise probe.ProbeFailure('PIPEWIRE_GEOMETRY_CHANGED','authorization_and_native_start')
    monkeypatch.setattr(probe,'run_probe',fail)
    assert probe.main(['--authorize','--helper-binary','/tmp/helper']) == 1
    result=json.loads(capsys.readouterr().out)
    assert result['failure_phase']=='authorization_and_native_start'
    assert result['error_domain']=='capture'
    assert result['input_events_sent']==0
    assert probe.error_domain('EIS_GEOMETRY_CHANGED')=='input_mapping'
    assert probe.error_domain('STALE_CHECK_INDETERMINATE')=='capture'
    assert probe.error_domain('TEST_FRAME_NOT_FRESH')=='capture'
    assert probe.error_domain('GEOMETRY_CHANGED')=='undetermined'
