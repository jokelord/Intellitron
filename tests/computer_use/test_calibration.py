import pytest


def test_renderer_change_invalidates_calibration():
    from backend.computer_use.calibration import verify_calibration
    from backend.computer_use.errors import ComputerUseError

    report = {"fingerprint": "renderer-1000", "passed": True}
    with pytest.raises(ComputerUseError, match="CALIBRATION_REQUIRED"):
        verify_calibration(report, "renderer-1536")


def test_every_fingerprint_component_invalidates(protocol):
    from backend.computer_use.calibration import calibration_fingerprint

    base = {
        "descriptor": protocol,
        "renderer": {"square_side": 1000, "square_policy": "stretch_axes", "grid": 8, "label_policy": "repeated"},
        "checkpoint": {"name": "model", "revision": "a"},
        "serving": {"stack": "sglang", "preprocessing": "identity"},
    }
    expected = calibration_fingerprint(**base)
    for group, key, value in (
        ("renderer", "square_side", 1536),
        ("renderer", "square_policy", "preserve_aspect"),
        ("renderer", "grid", 4),
        ("renderer", "label_policy", "outer_only"),
        ("checkpoint", "revision", "b"),
        ("serving", "preprocessing", "letterbox"),
    ):
        changed = {name: dict(item) if isinstance(item, dict) else item for name, item in base.items()}
        changed[group][key] = value
        assert calibration_fingerprint(**changed) != expected


def test_calibration_verifies_configured_protocol_without_guessing(protocol):
    from backend.computer_use.calibration import run_calibration
    from backend.computer_use.contracts import CalibrationCase

    cases = (
        CalibrationCase("landscape", 1000, 520, (0.25, 0.75), repeats=2),
        CalibrationCase("portrait", 600, 1000, (0.8, 0.2), repeats=2),
    )

    def infer(case, configured_protocol):
        assert configured_protocol is protocol
        return [case.target_ratio] * case.repeats

    report = run_calibration(infer, cases, protocol, {"max_axis_error": 0.01, "min_pass_rate": 1.0}, fingerprint="fp")
    assert report.passed is True
    assert report.protocol == "custom_isotropic_1000"
    assert report.fingerprint == "fp"


def test_calibration_fails_asymmetric_protocol_mismatch(protocol):
    from backend.computer_use.calibration import run_calibration
    from backend.computer_use.contracts import CalibrationCase

    cases = (CalibrationCase("portrait", 600, 1000, (0.5, 0.5), repeats=2),)

    def wrong_square_decoder(case, configured_protocol):
        return [(0.5, 0.3)] * case.repeats

    report = run_calibration(wrong_square_decoder, cases, protocol, {"max_axis_error": 0.05, "min_pass_rate": 1.0}, fingerprint="fp")
    assert report.passed is False


def test_offline_fixtures_use_production_renderer_without_model_or_desktop(tmp_path):
    from scripts.calibrate_computer_use import generate_fixtures
    from backend.computer_use.config import ComputerUseConfig
    from PIL import Image
    result = generate_fixtures(ComputerUseConfig(), 'deployment-fp', tmp_path)
    assert result['fingerprint'] == 'deployment-fp'
    assert result['serving_preprocessing'] is None
    assert len(result['cases']) == 12
    assert [case['case_id'] for case in result['cases']] == [
        f'case-{index}-{stage}' for index in range(6) for stage in ('stage1', 'stage2')]
    for index, size in enumerate(((1280, 720), (720, 1280), (1280, 720),
                                  (1280, 720), (1280, 720), (1280, 720))):
        with Image.open(tmp_path / f'case-{index}-source.png') as source:
            assert source.size == size
    for case in result['cases']:
        assert case['outputs'] == []
        assert (tmp_path / case['image']).read_bytes().startswith(b'\x89PNG')
        assert 'Calibration only' in (tmp_path / case['prompt']).read_text()
        assert case['protocol_fingerprint']
        assert case['normalization']['wire_max'][0] > 0
        assert case['source_size'] == [case['width'], case['height']]
        expected_size = [ComputerUseConfig().model_square_side_px] * 2
        assert case['model_image_size'] == expected_size
        with Image.open(tmp_path / case['image']) as rendered:
            assert rendered.size == tuple(expected_size)
            if case['case_id'].endswith('stage1'):
                index = case['case_id'].split('-')[1]
                with Image.open(tmp_path / f'case-{index}-source.png') as source:
                    assert rendered.size == (ComputerUseConfig().model_square_side_px,) * 2
                    assert source.size == (case['width'], case['height'])
        if case['case_id'].endswith('stage2'):
            x0, y0, x1, y1 = case['source_rect']
            assert 0 <= x0 < x1 <= case['width']
            assert 0 <= y0 < y1 <= case['height']
            tx = round(case['target_ratio'][0] * case['width'])
            ty = round(case['target_ratio'][1] * case['height'])
            assert x0 <= tx - 8 and tx + 8 < x1
            assert y0 <= ty - 8 and ty + 8 < y1
    assert result['cases'][8]['target_ratio'] == [1190 / 1280, 374 / 720]
    assert result['cases'][9]['target_ratio'] == [1190 / 1280, 374 / 720]
    assert result['cases'][10]['target_ratio'] == [410 / 1280, 670 / 720]
    assert result['cases'][11]['target_ratio'] == [410 / 1280, 670 / 720]
    edge = next(case for case in result['cases'] if case['case_id'] == 'case-3-stage2')
    with Image.open(tmp_path / edge['image']) as image:
        assert image.size == (1024, 1024)
        assert edge['normalization']['wire_max'] == [1000, 1000]
    assert not result.get('passed', False)


def test_static_stage2_fixtures_identify_synthetic_crop_as_reference_only(tmp_path):
    from backend.computer_use.config import ComputerUseConfig
    from scripts.calibrate_computer_use import generate_fixtures

    manifest = generate_fixtures(ComputerUseConfig(), 'deployment-fp', tmp_path)
    stage2_cases = [case for case in manifest['cases'] if case['case_id'].endswith('-stage2')]
    assert len(stage2_cases) == 6
    assert manifest['stage2_reference_policy'] == 'synthetic_target_offset_not_stage1_model_output'
    for case in stage2_cases:
        assert case['artifact_role'] == 'precomputed_stage2_reference'
        assert case['stage1_model_selected'] is False
        assert case['rough_point_policy'] == 'known_target_plus_40_x_30_y_clamped'
        assert len(case['rough_point_wire_xy']) == 2
        assert case['image'] == f"{case['case_id']}.png"  # preserve existing consumers
        prompt = (tmp_path / case['prompt']).read_text()
        assert 'precomputed reference' in prompt.lower()
        assert 'not the actual stage-1-selected' in prompt.lower()
    assert stage2_cases[0]['rough_point_wire_xy'] == [240, 730]
    readme = (tmp_path / 'README.md').read_text().lower()
    assert 'case-*-stage2.png' in readme
    assert 'complete overview independently stretched' in readme
    assert 'precomputed reference' in readme
    assert 'not the actual stage-1-selected' in readme
    assert 'case-*-stage2-input.png' in readme
    assert 'annotated/stage2/case-*-stage2-input.png' in readme
    assert 'annotated/stage2/compare-grid/gridded/case-*-stage2-input.png' in readme
    assert 'annotated/stage2/compare-grid/gridless/case-*-stage2-input.png' in readme


def test_large_target_fixture_variant_records_visible_bounds_for_all_six_cases(tmp_path):
    from PIL import Image
    from backend.computer_use.config import ComputerUseConfig
    from scripts.calibrate_computer_use import generate_fixtures

    manifest = generate_fixtures(ComputerUseConfig(), 'deployment-fp', tmp_path,
                                 target_size_px=128)
    assert manifest['fixture_target_size_px'] == 128
    source_cases = [case for case in manifest['cases'] if case['case_id'].endswith('-stage1')]
    assert len(source_cases) == 6
    for case in source_cases:
        x0, y0, x1, y1 = case['target_source_bounds']
        assert case['target_size_px'] == 128
        assert (x1 - x0, y1 - y0) == (128, 128)
        assert 0 <= x0 < x1 <= case['width']
        assert 0 <= y0 < y1 <= case['height']
        assert case['target_center_source_pixel'] == pytest.approx(
            [(x0 + x1 - 1) / 2, (y0 + y1 - 1) / 2])
        assert case['target_ratio'] == pytest.approx([
            case['target_center_source_pixel'][0] / case['width'],
            case['target_center_source_pixel'][1] / case['height']])
        with Image.open(tmp_path / f"case-{case['case_id'].split('-')[1]}-source.png") as image:
            # The generated source contains both the background and a visible target.
            assert image.size == (case['width'], case['height'])
            pixels = image.convert('RGB')
            assert pixels.getpixel((x0, y0)) == (255, 0, 255)
            assert pixels.getpixel((x1 - 1, y1 - 1)) == (255, 0, 255)
            color_counts = dict((color, count) for count, color in
                                pixels.getcolors(maxcolors=case['width'] * case['height']))
            assert color_counts[(255, 0, 255)] == 128 * 128
        detail = next(item for item in manifest['cases']
                      if item['case_id'] == case['case_id'].replace('stage1', 'stage2'))
        cx0, cy0, cx1, cy1 = detail['source_rect']
        assert cx0 <= x0 < x1 <= cx1
        assert cy0 <= y0 < y1 <= cy1
    edge_cases = [case for case in source_cases
                  if (case['target_source_bounds'][0] == 0 or
                      case['target_source_bounds'][1] == 0 or
                      case['target_source_bounds'][2] == case['width'] or
                      case['target_source_bounds'][3] == case['height'])]
    assert len(edge_cases) >= 3
    assert (tmp_path / 'samples.json').is_file()


def test_fixture_cli_routes_large_target_variant_to_repo_local_subdirectory(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    import scripts.calibrate_computer_use as cli
    from backend.computer_use.config import ComputerUseConfig

    diagrams = tmp_path / 'computer-use-diagrams' / 'v2.0'
    monkeypatch.setattr(cli, 'DIAGRAMS_DIR', diagrams, raising=False)
    monkeypatch.setattr(cli, 'get_config', lambda: SimpleNamespace(computer_use=ComputerUseConfig()))
    monkeypatch.setattr(cli, 'deployment_fingerprint', lambda app: 'fingerprint')
    monkeypatch.setattr(sys, 'argv', ['calibrate', '--generate-fixtures', '--target-size-px', '128'])

    assert cli.main() == 0
    large = diagrams / 'target-size-128px'
    assert (large / 'samples.json').is_file()
    assert (large / 'case-0-source.png').is_file()


def test_fixture_cli_without_path_uses_stable_diagrams_directory(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    import scripts.calibrate_computer_use as cli
    from backend.computer_use.config import ComputerUseConfig

    diagrams = tmp_path / 'computer-use-diagrams' / 'v2.0'
    monkeypatch.setattr(cli, 'DIAGRAMS_DIR', diagrams, raising=False)
    monkeypatch.setattr(cli, 'get_config', lambda: SimpleNamespace(computer_use=ComputerUseConfig()))
    monkeypatch.setattr(cli, 'deployment_fingerprint', lambda app: 'fingerprint')
    monkeypatch.setattr(sys, 'argv', ['calibrate', '--generate-fixtures'])

    assert cli.main() == 0
    assert (diagrams / 'samples.json').exists()
    assert (diagrams / 'case-0-stage1.png').exists()


def test_default_diagrams_directory_is_versioned_and_repo_local():
    from scripts.calibrate_computer_use import DIAGRAMS_DIR

    assert DIAGRAMS_DIR.parts[-2:] == ('computer-use-diagrams', 'v2.0')
    assert DIAGRAMS_DIR.parent.parent.is_dir()


def test_offline_probe_reports_source_axis_and_crop_relative_errors():
    from types import SimpleNamespace
    from scripts.probe_computer_use_two_stage import source_error_metrics

    crop = SimpleNamespace(crop_width=1920, crop_height=624)
    metrics = source_error_metrics(crop, 1936, 300, 1920, 312)
    assert metrics['absolute_source_axis_error_px'] == [16, 12]
    assert metrics['crop_relative_axis_error'] == pytest.approx([16 / 1920, 12 / 624])
    assert metrics['crop_aspect_log_distance'] == pytest.approx(__import__('math').log(1920 / 624))


def test_scored_report_requires_verified_serving_preprocessing_before_claiming_pass():
    from backend.computer_use.calibration import verify_deployment_report
    from backend.computer_use.errors import ComputerUseError

    report = {
        'fingerprint': 'v2-fingerprint', 'passed': True,
        'protocol': 'relative_1000',
        'thresholds': {'max_axis_error': .1, 'min_pass_rate': 1},
        'case_results': [
            {'case_id': 'left', 'width': 1000, 'height': 1000,
             'target_ratio': [.2, .2], 'errors': [0, 0]},
            {'case_id': 'right', 'width': 1000, 'height': 1000,
             'target_ratio': [.8, .8], 'errors': [0, 0]},
        ],
        'max_axis_error': 0, 'pass_rate': 1,
        'desktop_geometry': {'backend': 'wayland', 'logical_origin': [0, 0],
                             'logical_size': [1000, 1000], 'pixel_size': [1000, 1000]},
    }
    with pytest.raises(ComputerUseError, match='CALIBRATION_REQUIRED'):
        verify_deployment_report(report, 'v2-fingerprint', 'relative_1000')
    report['serving_preprocessing'] = {'transform': 'identity', 'verified': True}
    verify_deployment_report(report, 'v2-fingerprint', 'relative_1000')


def test_failed_scoring_writes_finite_inspectable_report(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    import scripts.calibrate_computer_use as scorer
    from backend.computer_use.config import ComputerUseConfig
    from backend.computer_use.errors import ComputerUseError
    source, output = tmp_path/'samples.json', tmp_path/'report.json'
    source.write_text(json.dumps({'fingerprint':'fp','desktop_geometry':None,'cases':[
        {'case_id':'bad','width':100,'height':100,'target_ratio':[.5,.5], 'outputs':[[2,0],[2,0]]}]}))
    monkeypatch.setattr(scorer, 'get_config', lambda: SimpleNamespace(computer_use=ComputerUseConfig()))
    monkeypatch.setattr(scorer, 'deployment_fingerprint', lambda app:'fp')
    monkeypatch.setattr('sys.argv', ['calibrate','--samples',str(source),'--output',str(output),
                                   '--max-axis-error','.1','--min-pass-rate','1'])
    with pytest.raises(ComputerUseError):
        scorer.main()
    report = json.loads(output.read_text(), parse_constant=lambda value: pytest.fail(value))
    assert report['passed'] is False
    assert report['serving_preprocessing'] is None
    assert report['case_results'][0]['errors'] == [None,None]
    assert report['max_axis_error'] is None
