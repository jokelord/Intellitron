"""Native X11 evidence must bind screenshot geometry and root-coordinate input."""
import json
from dataclasses import replace

import pytest

from backend.computer_use.calibration import verify_deployment_report
from backend.computer_use.contracts import InputBinding
from backend.computer_use.errors import ComputerUseError
from backend.computer_use.native_client import NativeProtocolError
from test_native_live_client import client, frame_packet, binding as wayland_binding
from test_runtime import _measured_report


def x11_binding():
    return {**wayland_binding(), 'backend': 'x11', 'origin': [4, 5],
            'device_id': 'binding', 'region_id': 'xrandr:12:34',
            'mapping_evidence': {'mapping_id': 'xrandr:12:34', 'full_monitor': True,
                'source': 'xrandr', 'coordinate_space': 'x11_root', 'root_id': 12,
                'monitor_atom': 34, 'monitor_name': 'HDMI-0', 'output_ids': [56]}}


def test_x11_status_and_sealed_frame_accept_same_monitor_evidence():
    payload = x11_binding()
    obj = client(('binding', {'binding': payload}, ()))
    obj._binding = None
    assert obj.resolve_binding().origin == (4, 5)
    packet = frame_packet(binding=payload)
    obj.channel.response = packet
    frame = obj.freeze('binding')
    assert frame.binding.backend == 'x11'
    assert frame.rgb == b'\x12\x34\x56' * 4


@pytest.mark.parametrize('change', [
    {'source': 'portal'}, {'coordinate_space': 'wayland_logical'},
    {'root_id': True}, {'root_id': 0}, {'monitor_atom': -1},
    {'monitor_name': ''}, {'output_ids': []}, {'output_ids': [True]},
    {'output_ids': [56, 56]}, {'mapping_id': 'other'},
])
def test_x11_malformed_monitor_evidence_closes_channel(change):
    payload = x11_binding()
    payload['mapping_evidence'].update(change)
    obj = client(('binding', {'binding': payload}, ()))
    obj._binding = None
    with pytest.raises(NativeProtocolError, match='INVALID_BINDING'):
        obj.resolve_binding()
    assert obj.channel is None


def test_x11_geometry_change_requires_new_context_binding():
    payload = x11_binding()
    obj = client(('binding', {'binding': {**payload, 'origin': [5, 5]}}, ()))
    obj._binding = InputBinding(**payload)
    with pytest.raises(NativeProtocolError, match='BINDING_CHANGED'):
        obj.resolve_binding()
    assert obj.channel is None


def test_x11_frame_dimensions_must_match_monitor_pixel_size():
    payload = x11_binding()
    payload['size'] = [3, 2]
    obj = client(frame_packet(binding=payload))
    obj._binding = InputBinding(**payload)
    with pytest.raises(NativeProtocolError, match='INVALID_FRAME_DIMENSIONS'):
        obj.freeze('binding')
    assert obj.channel is None


def measured_x11_report():
    return {**_measured_report('fp'), 'desktop_geometry': {
        'backend': 'x11', 'logical_origin': [4, 5], 'logical_size': [2, 2],
        'pixel_size': [2, 2]}}


def test_x11_measured_calibration_is_accepted():
    verify_deployment_report(measured_x11_report(), 'fp', 'relative_1000')


@pytest.mark.parametrize('field,value', [('pixel_size', [3, 2]), ('logical_origin', [-1, 5])])
def test_x11_measured_geometry_rejects_non_root_pixel_mapping(field, value):
    report = measured_x11_report()
    report['desktop_geometry'][field] = value
    with pytest.raises(ComputerUseError, match='CALIBRATION_REQUIRED'):
        verify_deployment_report(report, 'fp', 'relative_1000')


def test_x11_runtime_preserves_monitor_origin_and_rejects_report_drift(tmp_path):
    from backend.computer_use.runtime import SessionRuntimeFactory
    from test_executable_backend import _capability
    from test_stale_dispatch_audit import _fixture
    _, frame, _, _, config = _fixture()
    cap = _capability(tmp_path)
    report_path = tmp_path / 'report.json'
    report_path.write_text(json.dumps(measured_x11_report()))
    class Native:
        def __init__(self, **kwargs): self.channel = object()
        def start(self, binding_id, capability, **kwargs):
            self.binding = InputBinding(**{**x11_binding(), 'binding_id': binding_id,
                'device_id': binding_id, 'session_id': capability.session_id})
            return self.binding
        def freeze(self, binding_id):
            return replace(frame, binding=self.binding, width=2, height=2, rgb=b'\x12\x34\x56'*4)
        def close(self): self.channel = None
    factory = SessionRuntimeFactory(replace(config, helper_binary='/fake/helper',
        calibration_report_path=str(report_path)), current_fingerprint='fp', native_factory=Native)
    engine = factory(cap.session_id, cap)
    assert engine.binding.origin == (4, 5)
    factory.dispose_session(cap.session_id)
    report = measured_x11_report()
    report['desktop_geometry']['logical_origin'] = [0, 0]
    report_path.write_text(json.dumps(report))
    with pytest.raises(ComputerUseError, match='CALIBRATION_REQUIRED'):
        factory(cap.session_id, cap)
    assert not factory._engines


@pytest.mark.parametrize('native_code,public_state', [
    ('X11_GEOMETRY_CHANGED', 'GEOMETRY_CHANGED'),
    ('X11_CAPTURE_FAILED', 'CAPTURE_STALLED'),
    ('FRAME_ALLOCATION_FAILED', 'CAPTURE_STALLED'),
    ('FRAME_TIMEOUT', 'CAPTURE_STALLED'),
    ('X11_XWAYLAND_NOT_SUPPORTED', 'INPUT_DEVICE_NOT_READY'),
    ('NATIVE_BACKEND_UNAVAILABLE', 'INPUT_DEVICE_NOT_READY'),
    ('PRIVATE_TARGET_PROSE', 'STALE_CHECK_INDETERMINATE'),
])
def test_x11_dispatch_failure_survives_public_receipt_without_input(tmp_path, native_code, public_state):
    from backend.computer_use.runtime import RuntimeEngine, GroundingRenderer
    from backend.computer_use.dispatch import Dispatcher
    from backend.computer_use.frame_store import FrameStore
    from backend.computer_use.native_client import NativeUnavailable
    from backend.computer_use.executable_backend import VisionDesktopGroundingBackend
    from backend.computer_use.executable_protocol import VisionDesktopExecutableBridge, parse_vision_desktop_command
    from test_executable_backend import _capability
    from test_stale_dispatch_audit import _fixture
    binding, frame, _, _, config = _fixture()
    config = replace(config, grid_enabled=False, pre_action_validation='binding_only')
    class Native:
        channel = object()
        def set_abort_check(self, check_abort): pass
        def resolve_binding(self): raise NativeUnavailable(native_code)
        def send_event(self, *args, **kwargs): raise AssertionError('rejected validation must not send input')
    native, store = Native(), FrameStore()
    engine = RuntimeEngine(config=config, native=native, binding=binding, frame_store=store,
        renderer=GroundingRenderer(), supervisor=None, initial_frame=frame,
        dispatcher=Dispatcher(native, config=config, binding=binding, frame_store=store))
    bridge = VisionDesktopExecutableBridge(VisionDesktopGroundingBackend(lambda *_: engine))
    capability = _capability(tmp_path)
    def execute(text):
        return bridge.execute(parse_vision_desktop_command('vision_desktop '+text),
            session_id=capability.session_id, capability=capability)
    assert execute('observe --target-description target --action left_click').state == 'OBSERVATION_READY'
    assert execute('inspect_region --x 500 --y 500 --zoom 4').state == 'DETAIL_READY'
    result = execute('interact_detail --x 500 --y 500 --action left_click')
    assert result.state == public_state
    assert 'no input sent' in result.text
