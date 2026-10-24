from dataclasses import replace
import pytest
from backend.computer_use.errors import ComputerUseError
from test_stale_dispatch_audit import _fixture


def _measured_report(fingerprint):
    return {
        'passed': True,
        'fingerprint': fingerprint,
        'protocol': 'relative_1000',
        'max_axis_error': 0,
        'pass_rate': 1,
        'thresholds': {'max_axis_error': .1, 'min_pass_rate': 1},
        'serving_preprocessing': {'transform': 'identity', 'verified': True},
        'case_results': [
            {'case_id': 'left', 'width': 8, 'height': 6,
             'target_ratio': [.2, .2], 'errors': [0, 0]},
            {'case_id': 'right', 'width': 8, 'height': 6,
             'target_ratio': [.8, .8], 'errors': [0, 0]},
        ],
    }


def test_renderer_composes_bound_detail_without_recapture():
    from backend.computer_use.runtime import GroundingRenderer
    from backend.computer_use.contracts import InspectRegion
    binding, frame, request, _, config = _fixture()
    config = replace(config, grid_enabled=False)  # tiny 8×6 fault fixture cannot fit labels
    renderer = GroundingRenderer(clock=lambda: 100.2)
    overview = renderer.overview(frame, config)
    crop = renderer.crop(frame, InspectRegion(500, 500, 2), request, config)
    detail = renderer.detail(crop, config)
    assert overview.png.startswith(b'\x89PNG')
    assert detail.png.startswith(b'\x89PNG')
    assert (overview.width, overview.height) == (config.model_square_side_px,) * 2
    assert (detail.width, detail.height) == (config.model_square_side_px,) * 2
    assert (crop.detail_width, crop.detail_height, crop.model_width, crop.model_height) == (config.model_square_side_px,) * 4
    assert crop.frame_id == frame.frame_id
    assert crop.binding_id == binding.binding_id
    assert crop.calibration_fingerprint == request.calibration_fingerprint


def test_runtime_rejects_missing_report_before_spawning(tmp_path):
    from backend.computer_use.runtime import SessionRuntimeFactory
    from test_executable_backend import _capability
    _, _, _, _, config = _fixture()
    spawned = []
    factory = SessionRuntimeFactory(config, current_fingerprint='fp',
                                    native_factory=lambda **kwargs: spawned.append(kwargs))
    cap = _capability(tmp_path)
    with pytest.raises(ComputerUseError, match='CALIBRATION_REQUIRED'):
        factory(cap.session_id, cap)
    assert not spawned


def test_forged_pass_flag_without_measurements_is_not_calibration(tmp_path):
    import json
    from backend.computer_use.runtime import SessionRuntimeFactory
    from test_executable_backend import _capability
    _, _, _, _, config = _fixture()
    report = tmp_path / 'report.json'
    report.write_text(json.dumps({'passed': True, 'fingerprint': 'fp'}))
    factory = SessionRuntimeFactory(replace(config, calibration_report_path=str(report)), current_fingerprint='fp')
    cap = _capability(tmp_path)
    with pytest.raises(ComputerUseError, match='CALIBRATION_REQUIRED'):
        factory(cap.session_id, cap)


def test_passed_report_without_measured_monitor_geometry_rejects_before_native_creation(tmp_path):
    import json
    from backend.computer_use.runtime import SessionRuntimeFactory
    from test_executable_backend import _capability

    _, _, _, _, config = _fixture()
    report = tmp_path / 'report.json'
    report.write_text(json.dumps(_measured_report('fp')))
    spawned = []
    factory = SessionRuntimeFactory(
        replace(config, helper_binary='/fake/helper', calibration_report_path=str(report),
                calibration_fingerprint='fp'),
        current_fingerprint='fp', native_factory=lambda **kwargs: spawned.append(kwargs))
    cap = _capability(tmp_path)
    with pytest.raises(ComputerUseError, match='CALIBRATION_REQUIRED'):
        factory(cap.session_id, cap)
    assert spawned == []


def test_operator_test_factory_is_pinned_to_one_exact_session_capability(tmp_path):
    from backend.computer_use.runtime import OperatorSessionRuntimeFactory
    from test_executable_backend import _capability

    _, _, _, _, config = _fixture()
    config = replace(config, helper_binary='/fake/helper', calibration_report_path=None)
    cap = _capability(tmp_path)
    spawned = []
    factory = OperatorSessionRuntimeFactory(
        config, current_fingerprint='fp', session_id=cap.session_id,
        capability=cap, native_factory=lambda **kwargs: spawned.append(kwargs))
    assert factory._verify()['desktop_geometry'] is None
    with pytest.raises(ComputerUseError, match='INVALID_CONTEXT_BINDING'):
        factory('another-session', cap)
    with pytest.raises(ComputerUseError, match='INVALID_CONTEXT_BINDING'):
        factory(cap.session_id, replace(cap))
    assert spawned == []


def test_operator_test_factory_rejects_unset_safety_limits_and_stale_fingerprint(tmp_path):
    from backend.computer_use.runtime import OperatorSessionRuntimeFactory
    from test_executable_backend import _capability

    _, _, _, _, config = _fixture()
    cap = _capability(tmp_path)
    with pytest.raises(ComputerUseError, match='CALIBRATION_REQUIRED'):
        OperatorSessionRuntimeFactory(
            replace(config, max_sample_age_seconds=None), current_fingerprint='fp',
            session_id=cap.session_id, capability=cap)
    factory = OperatorSessionRuntimeFactory(
        replace(config, helper_binary='/fake/helper', calibration_fingerprint='old',
                calibration_report_path=None),
        current_fingerprint='fp', session_id=cap.session_id, capability=cap)
    with pytest.raises(ComputerUseError, match='CALIBRATION_REQUIRED'):
        factory._verify()


@pytest.mark.parametrize('scenario', ('v1.8_fingerprint', 'changed_square_side', 'failed_measurements'))
def test_stale_or_failed_calibration_rejects_before_native_creation(tmp_path, scenario):
    import json
    from backend.computer_use.runtime import SessionRuntimeFactory
    from test_executable_backend import _capability

    _, _, _, _, config = _fixture()
    report = _measured_report('v2-square-1000')
    current = 'v2-square-1000'
    if scenario == 'v1.8_fingerprint':
        report['fingerprint'] = 'v1.8-aspect-preserving'
    elif scenario == 'changed_square_side':
        config = replace(config, model_square_side_px=1536)
        current = f'v2-square-{config.model_square_side_px}'
    else:
        report['max_axis_error'] = .5
        report['pass_rate'] = .75
        report['case_results'][0]['errors'] = [0, .5]
    path = tmp_path / 'measured.json'
    path.write_text(json.dumps(report))
    config = replace(config, helper_binary='/fake/helper', calibration_report_path=str(path),
                     calibration_fingerprint=report['fingerprint'])
    spawned = []
    factory = SessionRuntimeFactory(config, current_fingerprint=current,
                                    native_factory=lambda **kwargs: spawned.append(kwargs))
    cap = _capability(tmp_path)
    with pytest.raises(ComputerUseError, match='CALIBRATION_REQUIRED'):
        factory(cap.session_id, cap)
    assert spawned == []


@pytest.mark.parametrize('evidence', [
    None,
    {'transform': 'unknown', 'verified': True},
    {'transform': 'identity', 'verified': False},
    {'transform': 'identity', 'verified': True, 'padding': 'unknown'},
])
def test_unknown_or_unverified_serving_transform_rejects_before_native_creation(tmp_path, evidence):
    import json
    from backend.computer_use.runtime import SessionRuntimeFactory
    from test_executable_backend import _capability

    _, _, _, _, config = _fixture()
    report = _measured_report('v2-square-1000')
    if evidence is None:
        del report['serving_preprocessing']
    else:
        report['serving_preprocessing'] = evidence
    path = tmp_path / 'measured.json'
    path.write_text(json.dumps(report))
    config = replace(config, helper_binary='/fake/helper', calibration_report_path=str(path),
                     calibration_fingerprint=report['fingerprint'])
    spawned = []
    factory = SessionRuntimeFactory(config, current_fingerprint='v2-square-1000',
                                    native_factory=lambda **kwargs: spawned.append(kwargs))
    cap = _capability(tmp_path)
    with pytest.raises(ComputerUseError, match='CALIBRATION_REQUIRED'):
        factory(cap.session_id, cap)
    assert spawned == []


def test_disconnected_engine_requires_explicit_session_reset():
    from backend.computer_use.runtime import RuntimeEngine
    from types import SimpleNamespace
    engine = RuntimeEngine(config=None, native=SimpleNamespace(channel=None),
                           binding=None, frame_store=None, supervisor=None)
    with pytest.raises(ComputerUseError, match='DESKTOP_SESSION_RESET_REQUIRED'):
        engine.prepare(None)


@pytest.mark.parametrize("grid_label_placement", ["edge", "repeated"])
def test_production_composition_two_stage_and_recovery(tmp_path, grid_label_placement):
    import json
    import time
    from types import SimpleNamespace
    from backend.computer_use.runtime import SessionRuntimeFactory
    from backend.computer_use.executable_backend import VisionDesktopGroundingBackend
    from backend.computer_use.executable_protocol import VisionDesktopExecutableBridge, parse_vision_desktop_command
    from test_executable_backend import _capability
    binding, frame, _, _, config = _fixture()
    config = replace(config, grid_enabled=False, grid_label_placement=grid_label_placement)  # tiny 8×6 fault fixture cannot fit labels
    report = tmp_path / 'calibration.json'
    report.write_text(json.dumps(dict(passed=True, fingerprint='fp', protocol=config.protocol,
        serving_preprocessing=dict(transform='identity', verified=True),
        desktop_geometry=dict(backend='wayland',logical_origin=[0,0],logical_size=[8,6],pixel_size=[8,6]),
        max_axis_error=0, pass_rate=1, thresholds=dict(max_axis_error=.1,min_pass_rate=1),
        case_results=[dict(case_id=str(i),width=8,height=6,target_ratio=target,errors=[0,0])
                      for i,target in enumerate(([.2,.2],[.8,.8]))])))
    class Native:
        def __init__(self, **kwargs):
            self.channel = object()
            self.sequence = 0
            self.events = []
        def start(self, binding_id, cap, **kwargs):
            self.binding = replace(binding, binding_id=binding_id, backend='wayland')
            return self.binding
        def set_abort_check(self, callback): self.abort = callback
        def resolve_binding(self): return self.binding
        def freeze(self, binding_id):
            self.sequence += 1
            now = time.monotonic()
            return replace(frame, frame_id=str(self.sequence), observation_id=str(self.sequence),
                binding=self.binding, sequence=self.sequence, captured_monotonic=now, arrived_monotonic=now)
        def send_event(self, *args, deadline_monotonic=None):
            assert deadline_monotonic > time.monotonic()
            self.events.append(args)
        def close(self): self.channel = None
    factory = SessionRuntimeFactory(replace(config, helper_binary='/fake/helper',
        calibration_report_path=str(report)), current_fingerprint='fp', native_factory=Native)
    backend = VisionDesktopGroundingBackend(factory)
    bridge = VisionDesktopExecutableBridge(backend)
    cap = _capability(tmp_path)
    def execute(text):
        return bridge.execute(parse_vision_desktop_command('vision_desktop '+text),session_id=cap.session_id,capability=cap)
    assert execute('observe --target-description target --action left_click').attachment
    assert factory._engines[cap.session_id].native.sequence == 1
    assert execute('inspect_region --x 500 --y 500 --zoom 4').attachment
    assert execute('interact_detail --x 500 --y 500 --action left_click').state == 'ACTION_SENT'
    native = factory._engines[cap.session_id].native
    assert len(native.events) == 4
    assert execute('reobserve').attachment
    assert execute('observe --target-description next --action left_click').attachment
    bridge.dispose_session(cap.session_id)
    assert native.channel is None
    assert factory._engines == {}
def test_deployment_fingerprint_changes_with_grid_label_placement(tmp_path, settings):
    from dataclasses import dataclass
    from types import SimpleNamespace
    from backend.computer_use.runtime import deployment_fingerprint

    @dataclass
    class Model:
        model_path: str

    @dataclass
    class Empty:
        pass

    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "model.safetensors").write_bytes(b"offline-identity")
    def app(placement):
        return SimpleNamespace(
            computer_use=settings(grid_label_placement=placement),
            model=Model(str(tmp_path)), vision_upload=Empty(),
            inference=Empty(), sglang=Empty(), vllm=Empty(),
        )

    assert deployment_fingerprint(app("edge")) != deployment_fingerprint(app("repeated"))
    bound = app('edge')
    strict = app('edge')
    strict.computer_use = replace(strict.computer_use, pre_action_validation='frame_and_binding')
    assert bound.computer_use.model_square_side_px == strict.computer_use.model_square_side_px
    assert bound.computer_use.stage1_zoom == strict.computer_use.stage1_zoom
    assert deployment_fingerprint(bound) != deployment_fingerprint(strict)

    debug = app('edge')
    debug.computer_use = replace(debug.computer_use, debug_save_images=True, debug_image_max_files=8, debug_image_max_bytes=1048576)
    assert deployment_fingerprint(bound) == deployment_fingerprint(debug)


@pytest.mark.parametrize('home_name', ['alice', 'bob'])
def test_deployment_fingerprint_reads_model_artifacts_relative_to_current_home(
        tmp_path, monkeypatch, settings, home_name):
    from dataclasses import dataclass
    from types import SimpleNamespace
    from backend.computer_use.runtime import deployment_fingerprint

    @dataclass
    class Model:
        model_path: str

    @dataclass
    class Empty:
        pass

    home = tmp_path / home_name
    root = home / 'models' / 'vision'
    root.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    (root / 'config.json').write_text('{}')
    (root / 'model.safetensors').write_bytes(b'offline-identity')
    app = SimpleNamespace(
        computer_use=settings(), model=Model('~/models/vision'),
        vision_upload=Empty(), inference=Empty(), sglang=Empty(), vllm=Empty(),
    )

    fingerprint = deployment_fingerprint(app)
    (root / 'config.json').write_text('{"model_type": "updated"}')
    assert deployment_fingerprint(app) != fingerprint

    (root / 'model.safetensors').unlink()
    with pytest.raises(ComputerUseError, match='artifact identity is incomplete'):
        deployment_fingerprint(app)

    app.model.model_path = '~/models/missing'
    with pytest.raises(ComputerUseError, match='model directory unavailable'):
        deployment_fingerprint(app)
