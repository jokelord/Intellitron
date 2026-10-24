"""Offline A/B probe must compare one crop, changing only the Stage-2 grid."""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from io import BytesIO
from types import SimpleNamespace

from backend.computer_use.config import ComputerUseConfig
from scripts.calibrate_computer_use import generate_fixtures
import scripts.probe_computer_use_two_stage as probe


def test_infer_offers_both_stage2_tools_without_forcing_a_click(monkeypatch):
    sent = {}

    def fake_urlopen(request, timeout):
        sent.update(json.loads(request.data))
        response = {'choices': [{'finish_reason': 'tool_calls', 'message': {'tool_calls': [
            {'function': {'name': 'reobserve', 'arguments': '{"reason":"target_not_visible"}'}}
        ]}}]}
        return BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(probe.urllib.request, 'urlopen', fake_urlopen)
    encoded = SimpleNamespace(messages=({'role': 'user', 'content': [{'type': 'text', 'text': 'detail'}]},),
                              tools=({'name': 'interact_detail', 'parameters': {'type': 'object'}},
                                     {'name': 'reobserve', 'parameters': {'type': 'object'}}))
    raw, _ = probe.infer('http://127.0.0.1:30000', 'fixture-model', encoded, 'offline detail')
    assert [tool['function']['name'] for tool in sent['tools']] == ['interact_detail', 'reobserve']
    assert sent['tool_choice'] != {'type': 'function', 'function': {'name': 'interact_detail'}}
    assert sent['chat_template_kwargs'] == {'enable_thinking': False}
    assert sent['max_tokens'] == 2048
    assert 'max_new_tokens' not in sent
    assert raw == {'name': 'reobserve', 'arguments': {'reason': 'target_not_visible'}}


def test_probe_provider_message_matches_agent_image_first_order():
    from backend.computer_use.model_adapter import provider_messages_image_first

    encoded = SimpleNamespace(messages=({'role': 'user', 'content': [
        {'type': 'text', 'text': 'locate target'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}},
    ]},))
    sent = provider_messages_image_first(encoded)
    assert [part['type'] for part in sent[0]['content']] == ['image_url', 'text']
    assert encoded.messages[0]['content'][0]['type'] == 'text'


def test_target_hit_scoring_uses_manifest_pixel_bounds_for_each_target_size():
    assert probe.point_hits_target(108, 108, (100, 100, 117, 117))
    assert not probe.point_hits_target(117, 108, (100, 100, 117, 117))
    assert probe.point_hits_target(127, 127, (64, 64, 192, 192))
    assert not probe.point_hits_target(192, 127, (64, 64, 192, 192))


def test_safe_inset_and_eligible_two_stage_scores_are_separate():
    bounds = (576, 0, 704, 128)
    outer_only = probe.score_target_point(580.4, 8.4, bounds,
                                          stage1_full_containment=True)
    assert outer_only['on_target'] is True
    assert outer_only['on_target_safe_inset'] is False
    assert outer_only['eligible_two_stage_hit'] is False

    safe_but_crop_clipped = probe.score_target_point(640, 64, bounds,
                                                      stage1_full_containment=False)
    assert safe_but_crop_clipped['on_target_safe_inset'] is True
    assert safe_but_crop_clipped['eligible_two_stage_hit'] is False

    eligible = probe.score_target_point(640, 64, bounds,
                                        stage1_full_containment=True)
    assert eligible['eligible_two_stage_hit'] is True


def test_large_target_variant_probe_reports_size_and_scores_against_visible_bounds(
    tmp_path, monkeypatch, capsys,
):
    base = tmp_path / 'computer-use-diagrams' / 'v2.0' / 'target-size-128px'
    manifest = generate_fixtures(ComputerUseConfig(), 'fp', base, target_size_px=128)
    cases = {int(case['case_id'].split('-')[1]): case for case in manifest['cases']
             if case['case_id'].endswith('-stage1')}

    def fake_infer(api, model, encoded, description):
        instance = encoded.instance
        index = int(instance.request.request_id.split('-')[1])
        case = cases[index]
        if instance.stage == 'stage1':
            raw = {'name': 'inspect_region', 'arguments': {
                'x': round(case['target_ratio'][0] * 1000),
                'y': round(case['target_ratio'][1] * 1000), 'zoom': 4}}
        else:
            crop = instance.crop
            target_x, target_y = case['target_center_source_pixel']
            raw = {'name': 'interact_detail', 'arguments': {
                'x': round((target_x - crop.x0) * 1000 / crop.crop_width),
                'y': round((target_y - crop.y0) * 1000 / crop.crop_height),
                'action': 'left_click'}}
        return raw, {'finish_reason': 'tool_calls', 'tool_call': raw}

    monkeypatch.setattr(probe, 'get_config', lambda: SimpleNamespace(computer_use=ComputerUseConfig()))
    monkeypatch.setattr(probe, 'deployment_fingerprint', lambda app: 'fp')
    monkeypatch.setattr(probe, 'model_id', lambda api: 'fixture-model')
    monkeypatch.setattr(probe, 'infer', fake_infer)
    monkeypatch.setattr(sys, 'argv', ['probe', '--fixtures', str(base), '--infer-stage1', '--infer-stage2'])
    probe.main()

    output = json.loads(capsys.readouterr().out)
    report = json.loads((base / 'annotated' / 'two-stage-results.json').read_text())
    assert output['report'] == str(base / 'annotated' / 'two-stage-results.json')
    assert report['fixture_target_size_px'] == 128
    assert report['fixture_variant'] == 'target-size-128px'
    assert len(report['cases']) == 6
    assert report['summary']['stage1_full_target_containment'] == {
        'count': 6, 'total': 6, 'rate': 1.0}
    assert report['summary']['eligible_two_stage_hits'] == {
        'count': 6, 'model_point_count': 6, 'case_count': 6}
    for result in report['cases']:
        assert result['target_size_px'] == 128
        x0, y0, x1, y1 = result['target_source_bounds']
        assert (x1 - x0, y1 - y0) == (128, 128)
        assert result['stage1']['target_fully_inside_crop'] is True
        assert result['stage2']['target_size_px'] == 128
        assert result['stage2']['on_target'] is True
        assert result['stage2']['target_source_bounds'] == result['target_source_bounds']


def test_compare_grid_uses_one_stage1_crop_and_exact_sent_images_for_six_cases(
    tmp_path, monkeypatch, capsys,
):
    base = tmp_path / 'computer-use-diagrams' / 'v2.0'
    manifest = generate_fixtures(ComputerUseConfig(), 'fp', base)
    target_by_index = {
        int(case['case_id'].split('-')[1]): case
        for case in manifest['cases'] if case['case_id'].endswith('-stage1')
    }
    calls = []

    def fake_infer(api, model, encoded, description):
        assert api == 'http://127.0.0.1:30000'
        assert model == 'fixture-model'
        instance = encoded.instance
        index = int(instance.request.request_id.split('-')[1])
        case = target_by_index[index]
        if instance.stage == 'stage1':
            raw = {'name': 'inspect_region', 'arguments': {
                'x': round(case['target_ratio'][0] * 1000),
                'y': round(case['target_ratio'][1] * 1000), 'zoom': 4}}
            variant = 'stage1'
        else:
            crop = instance.crop
            assert crop is not None
            target_x = round(case['target_ratio'][0] * case['width'])
            target_y = round(case['target_ratio'][1] * case['height'])
            raw = {'name': 'interact_detail', 'arguments': {
                'x': round((target_x - crop.x0) * 1000 / crop.crop_width),
                'y': round((target_y - crop.y0) * 1000 / crop.crop_height),
                'action': 'left_click'}}
            variant = 'gridded' if crop.grid_enabled else 'gridless'
        message = encoded.messages[0]
        sent_url = message['content'][1]['image_url']['url']
        sent_png = base64.b64decode(sent_url.split(',', 1)[1])
        calls.append({'index': index, 'variant': variant,
                      'crop_rect': None if instance.crop is None else
                      [instance.crop.x0, instance.crop.y0, instance.crop.x1, instance.crop.y1],
                      'crop_id': None if instance.crop is None else instance.crop.crop_id,
                      'png_sha256': hashlib.sha256(sent_png).hexdigest(),
                      'tool_schema': encoded.tools,
                      'prompt': message['content'][0]['text']})
        return raw, {'finish_reason': 'tool_calls', 'tool_call': raw}

    monkeypatch.setattr(probe, 'get_config', lambda: SimpleNamespace(computer_use=ComputerUseConfig()))
    monkeypatch.setattr(probe, 'deployment_fingerprint', lambda app: 'fp')
    monkeypatch.setattr(probe, 'model_id', lambda api: 'fixture-model')
    monkeypatch.setattr(probe, 'infer', fake_infer)
    monkeypatch.setattr(sys, 'argv', ['probe', '--fixtures', str(base), '--infer-stage1',
                                      '--infer-stage2', '--compare-grid'])
    probe.main()

    output = json.loads(capsys.readouterr().out)
    report = json.loads((base / 'annotated' / 'two-stage-grid-comparison-results.json').read_text())
    assert output['report'] == str(base / 'annotated' / 'two-stage-grid-comparison-results.json')
    assert len(report['cases']) == 6
    assert len([call for call in calls if call['variant'] == 'stage1']) == 6
    assert len([call for call in calls if call['variant'] == 'gridded']) == 6
    assert len([call for call in calls if call['variant'] == 'gridless']) == 6
    assert len(report['paired_summary']['cases']) == 6
    for index, case in enumerate(report['cases']):
        variants = case['stage2_variants']
        pair_summary = report['paired_summary']['cases'][index]
        assert set(variants) == {'gridded', 'gridless'}
        pair = [call for call in calls if call['index'] == index and call['variant'] != 'stage1']
        assert pair[0]['crop_rect'] == pair[1]['crop_rect'] == case['crop']['source_rect']
        assert pair[0]['crop_id'] == pair[1]['crop_id']
        assert pair[0]['png_sha256'] != pair[1]['png_sha256']
        assert pair[0]['tool_schema'] == pair[1]['tool_schema']
        for call in pair:
            variant = variants[call['variant']]
            assert variant['status'] == 'model_point'
            assert variant['on_target'] is True
            image_path = base / variant['input_image_relative']
            assert image_path.read_bytes().startswith(b'\x89PNG')
            assert variant['input_sha256'] == call['png_sha256'] == hashlib.sha256(image_path.read_bytes()).hexdigest()
            assert (base / variant['annotated_image_relative']).is_file()
            assert (base / variant['prompt_relative']).is_file()
        gridded_prompt = next(call['prompt'] for call in pair if call['variant'] == 'gridded').lower()
        gridless_prompt = next(call['prompt'] for call in pair if call['variant'] == 'gridless').lower()
        assert 'normalized grid' in gridded_prompt
        assert 'normalized grid' not in gridless_prompt
        assert 'repeats 16 times' not in gridded_prompt
        assert 'sixteen times' not in gridless_prompt
        assert 'no grid or tick labels' in gridless_prompt
        assert [schema['name'] for schema in pair[0]['tool_schema']] == ['interact_detail', 'reobserve']
        for variant in ('gridded', 'gridless'):
            assert pair_summary[variant]['absolute_source_axis_error_px'] == variants[variant]['absolute_source_axis_error_px']
    for path in output['comparison_contact_sheets'].values():
        assert path.startswith(str(base))
        assert __import__('pathlib').Path(path).is_file()
    assert output['stage2_input_contact_sheet'] == output['comparison_contact_sheets']['gridded_input']
    assert __import__('pathlib').Path(output['stage2_input_contact_sheet']).is_file()
    assert output['stage2_contact_sheet'] == output['comparison_contact_sheets']['gridded_marked']


def test_probe_without_compare_grid_preserves_single_stage2_output(tmp_path, monkeypatch, capsys):
    base = tmp_path / 'computer-use-diagrams' / 'v2.0'
    generate_fixtures(ComputerUseConfig(), 'fp', base)

    def fake_infer(api, model, encoded, description):
        if encoded.instance.stage == 'stage1':
            raw = {'name': 'inspect_region', 'arguments': {'x': 500, 'y': 500, 'zoom': 4}}
        else:
            raw = {'name': 'interact_detail', 'arguments': {'x': 500, 'y': 500, 'action': 'left_click'}}
        return raw, {'finish_reason': 'tool_calls', 'tool_call': raw}

    monkeypatch.setattr(probe, 'get_config', lambda: SimpleNamespace(computer_use=ComputerUseConfig()))
    monkeypatch.setattr(probe, 'deployment_fingerprint', lambda app: 'fp')
    monkeypatch.setattr(probe, 'model_id', lambda api: 'fixture-model')
    monkeypatch.setattr(probe, 'infer', fake_infer)
    monkeypatch.setattr(sys, 'argv', ['probe', '--fixtures', str(base), '--infer-stage1', '--infer-stage2'])
    probe.main()
    output = json.loads(capsys.readouterr().out)
    report = json.loads((base / 'annotated' / 'two-stage-results.json').read_text())
    assert output['report'].endswith('/two-stage-results.json')
    assert 'comparison_contact_sheets' not in output
    assert len(report['cases']) == 6
    assert all('stage2_variants' not in row for row in report['cases'])
    assert all(row['stage2']['status'] == 'model_point' for row in report['cases'])
    assert (base / 'annotated' / 'stage2' / 'stage2-contact-sheet.png').is_file()


def test_compare_grid_keeps_failed_variant_separate_from_successful_pair(tmp_path, monkeypatch, capsys):
    base = tmp_path / 'computer-use-diagrams' / 'v2.0'
    generate_fixtures(ComputerUseConfig(), 'fp', base)

    def fake_infer(api, model, encoded, description):
        if encoded.instance.stage == 'stage1':
            raw = {'name': 'inspect_region', 'arguments': {'x': 500, 'y': 500, 'zoom': 4}}
        elif encoded.instance.crop.grid_enabled:
            raw = {'name': 'interact_detail', 'arguments': {'x': 500, 'y': 500, 'action': 'left_click'}}
        else:
            if encoded.instance.request.request_id == 'case-0':
                raise ConnectionRefusedError('offline fake connection failure')
            raw = {'name': 'wrong_tool', 'arguments': {}}
        return raw, {'finish_reason': 'tool_calls', 'tool_call': raw}

    monkeypatch.setattr(probe, 'get_config', lambda: SimpleNamespace(computer_use=ComputerUseConfig()))
    monkeypatch.setattr(probe, 'deployment_fingerprint', lambda app: 'fp')
    monkeypatch.setattr(probe, 'model_id', lambda api: 'fixture-model')
    monkeypatch.setattr(probe, 'infer', fake_infer)
    monkeypatch.setattr(sys, 'argv', ['probe', '--fixtures', str(base), '--infer-stage1',
                                      '--infer-stage2', '--compare-grid'])
    probe.main()
    capsys.readouterr()
    report = json.loads((base / 'annotated' / 'two-stage-grid-comparison-results.json').read_text())
    assert len(report['cases']) == 6
    for row in report['cases']:
        assert row['stage2_variants']['gridded']['status'] == 'model_point'
        assert row['stage2_variants']['gridless']['status'] == 'invalid_model_output'
        assert row['stage2_variants']['gridless']['error']
        assert (base / row['stage2_variants']['gridless']['annotated_image_relative']).is_file()


def test_probe_reports_reobserve_request_without_inventing_final_point(tmp_path, monkeypatch, capsys):
    base = tmp_path / 'computer-use-diagrams' / 'v2.0'
    generate_fixtures(ComputerUseConfig(), 'fp', base)

    def fake_infer(api, model, encoded, description):
        if encoded.instance.stage == 'stage1':
            raw = {'name': 'inspect_region', 'arguments': {'x': 500, 'y': 500, 'zoom': 4}}
        else:
            raw = {'name': 'reobserve', 'arguments': {'reason': 'target_not_visible'}}
        return raw, {'finish_reason': 'tool_calls', 'tool_call': raw}

    monkeypatch.setattr(probe, 'get_config', lambda: SimpleNamespace(computer_use=ComputerUseConfig()))
    monkeypatch.setattr(probe, 'deployment_fingerprint', lambda app: 'fp')
    monkeypatch.setattr(probe, 'model_id', lambda api: 'fixture-model')
    monkeypatch.setattr(probe, 'infer', fake_infer)
    monkeypatch.setattr(sys, 'argv', ['probe', '--fixtures', str(base), '--infer-stage1', '--infer-stage2'])
    probe.main()
    capsys.readouterr()
    report = json.loads((base / 'annotated' / 'two-stage-results.json').read_text())
    assert len(report['cases']) == 6
    for row in report['cases']:
        stage2 = row['stage2']
        assert stage2['status'] == 'reobserve_requested'
        assert stage2['reason'] == 'target_not_visible'
        assert stage2['on_target'] is None
        assert 'final_source_pixel' not in stage2
        assert 'wire_xy' not in stage2
        assert __import__('pathlib').Path(stage2['annotated_image']).is_file()


def test_compare_grid_reports_reobserve_per_variant(tmp_path, monkeypatch, capsys):
    base = tmp_path / 'computer-use-diagrams' / 'v2.0'
    generate_fixtures(ComputerUseConfig(), 'fp', base)

    def fake_infer(api, model, encoded, description):
        if encoded.instance.stage == 'stage1':
            raw = {'name': 'inspect_region', 'arguments': {'x': 500, 'y': 500, 'zoom': 4}}
        elif encoded.instance.crop.grid_enabled:
            raw = {'name': 'reobserve', 'arguments': {'reason': 'target_ambiguous'}}
        else:
            raw = {'name': 'interact_detail', 'arguments': {'x': 500, 'y': 500, 'action': 'left_click'}}
        return raw, {'finish_reason': 'tool_calls', 'tool_call': raw}

    monkeypatch.setattr(probe, 'get_config', lambda: SimpleNamespace(computer_use=ComputerUseConfig()))
    monkeypatch.setattr(probe, 'deployment_fingerprint', lambda app: 'fp')
    monkeypatch.setattr(probe, 'model_id', lambda api: 'fixture-model')
    monkeypatch.setattr(probe, 'infer', fake_infer)
    monkeypatch.setattr(sys, 'argv', ['probe', '--fixtures', str(base), '--infer-stage1',
                                      '--infer-stage2', '--compare-grid'])
    probe.main()
    capsys.readouterr()
    report = json.loads((base / 'annotated' / 'two-stage-grid-comparison-results.json').read_text())
    assert len(report['cases']) == 6
    assert report['paired_summary']['on_target_count']['gridded'] == 0
    for row in report['cases']:
        gridded = row['stage2_variants']['gridded']
        assert gridded['status'] == 'reobserve_requested'
        assert gridded['reason'] == 'target_ambiguous'
        assert gridded['on_target'] is None
        assert 'final_source_pixel' not in gridded
        assert (base / gridded['annotated_image_relative']).is_file()
        assert row['stage2_variants']['gridless']['status'] == 'model_point'
