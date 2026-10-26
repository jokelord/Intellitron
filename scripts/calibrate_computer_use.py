"""Fingerprint deployment and score operator-collected same-Agent calibration samples.

This command never invents model measurements or performs desktop input.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import replace
from pathlib import Path

from backend.config import get_config
from backend.computer_use.calibration import run_calibration, verify_deployment_report
from backend.computer_use.contracts import CalibrationCase, ProtocolDescriptor, thaw
from backend.computer_use.runtime import deployment_fingerprint, GroundingRenderer

DIAGRAMS_DIR = Path(__file__).resolve().parents[1] / 'logs' / 'computer-use' / 'v2.0'


def _square_target_bounds(width: int, height: int, target_ratio, target_size_px: int):
    """Return a fully visible half-open pixel bbox and its raster-center point."""
    if not isinstance(target_size_px, int) or target_size_px < 1:
        raise ValueError('target_size_px must be a positive integer')
    if target_size_px > min(width, height):
        raise ValueError('target_size_px must fit every source frame')
    requested_x = round(target_ratio[0] * width)
    requested_y = round(target_ratio[1] * height)
    left = target_size_px // 2
    x0 = min(max(0, requested_x - left), width - target_size_px)
    y0 = min(max(0, requested_y - left), height - target_size_px)
    x1, y1 = x0 + target_size_px, y0 + target_size_px
    center = ((x0 + x1 - 1) / 2, (y0 + y1 - 1) / 2)
    return (x0, y0, x1, y1), center


def generate_fixtures(config, fingerprint, output_dir, *, target_size_px=17):
    """Export offline known targets through the real renderer; never call a model."""
    from PIL import Image, ImageDraw
    from backend.computer_use.contracts import CanonicalFrame, InputBinding, GroundingRequest, InspectRegion
    from backend.computer_use.model_adapter import describe_stage, encode_stage
    output_dir = Path(output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    config = replace(config, calibration_fingerprint=fingerprint)
    descriptor = ProtocolDescriptor(config.protocol, pixel_semantics=config.pixel_semantics)
    result = {'fingerprint': fingerprint, 'desktop_geometry': None,
              'serving_preprocessing': None, 'cases': [],
              'fixture_target_size_px': target_size_px,
              'fixture_variant': f'target-size-{target_size_px}px',
              'purpose': 'offline model/renderer measurements; no live desktop acceptance',
              'stage2_reference_policy': 'synthetic_target_offset_not_stage1_model_output'}
    for index, (width, height, target) in enumerate(((1280, 720, (.2, .7)), (720, 1280, (.8, .2)),
                                                   (1280, 720, (.05, .05)), (1280, 720, (.5, .02)),
                                                   (1280, 720, (.93, .52)), (1280, 720, (.32, .93)))):
        image = Image.new('RGB', (width, height), '#243247')
        draw = ImageDraw.Draw(image)
        draw.rectangle((width*.4, height*.3, width*.6, height*.5), fill='#44bb88')
        bounds, target_center = _square_target_bounds(width, height, target, target_size_px)
        tx, ty = target_center
        x0, y0, x1, y1 = bounds
        draw.rectangle((x0, y0, x1-1, y1-1), fill='#ff00ff')
        image.save(output_dir / f'case-{index}-source.png', format='PNG')
        target = (tx/width, ty/height)
        binding = InputBinding('offline-binding', 'offline-context', 1, 'wayland', (0,0),
                               (width,height), 1, 'offline-device', 'offline-region', {}, False)
        frame = CanonicalFrame(f'frame-{index}', f'observation-{index}', binding,
                               width, height, image.tobytes(), 1, index+1, 1, 1, False, {}, {})
        request = GroundingRequest(f'case-{index}', binding.session_id, binding.binding_id,
                                   'center of the magenta square target', 'left_click', descriptor, fingerprint)
        renderer = GroundingRenderer(clock=lambda: 1)
        overview = renderer.overview(frame, config)
        rough = crop = None
        rough_offset = None
        for offset_x, offset_y in ((40, 30), (20, 15), (0, 0)):
            candidate = InspectRegion(min(1000, target[0]*1000+offset_x),
                                      min(1000, target[1]*1000+offset_y), config.stage1_zoom)
            candidate_crop = renderer.crop(frame, candidate, request, config)
            if (candidate_crop.x0 <= x0 and x1 <= candidate_crop.x1 and
                    candidate_crop.y0 <= y0 and y1 <= candidate_crop.y1):
                rough, crop = candidate, candidate_crop
                rough_offset = [offset_x, offset_y]
                break
        if rough is None:
            raise RuntimeError(f'could not create a fully visible Stage-2 fixture for case-{index}')
        detail = renderer.detail(crop, config)
        for stage, artifact, bound_crop in (('stage1',overview,None),('stage2',detail,crop)):
            case_id = f'case-{index}-{stage}'
            instance = describe_stage(
                stage, artifact, bound_crop, request,
                grid_label_placement=config.grid_label_placement, stage1_zoom=config.stage1_zoom)
            encoded = encode_stage(instance, artifact)
            image_name, prompt_name = case_id+'.png', case_id+'.txt'
            (output_dir/image_name).write_bytes(artifact.png)
            reference_notice = (
                'Precomputed reference Stage-2 image, not the actual Stage-1-selected model input. '
                'Its rough point was constructed from the known target for offline inspection.\n\n'
                if bound_crop is not None else ''
            )
            prompt = (reference_notice +
                      'Calibration only: return the requested JSON name/arguments as plain text. '
                      'Do not execute or call any tool. This is an offline image, not a live desktop.\n\n' +
                      encoded.messages[0]['content'][0]['text'] + '\n\nJSON schema: ' +
                      json.dumps(thaw(encoded.tools[0]), sort_keys=True))
            (output_dir/prompt_name).write_text(prompt)
            offset = [0,0] if bound_crop is None else [crop.x0/width,crop.y0/height]
            span = [1,1] if bound_crop is None else [crop.crop_width/width,crop.crop_height/height]
            case_record = dict(case_id=case_id, width=width, height=height,
                source_size=[width, height], model_image_size=[artifact.width, artifact.height],
                target_size_px=target_size_px, target_source_bounds=list(bounds),
                target_center_source_pixel=[tx, ty],
                source_rect=None if bound_crop is None else [crop.x0, crop.y0, crop.x1, crop.y1],
                target_ratio=list(target), outputs=[], image=image_name, prompt=prompt_name,
                protocol_fingerprint=instance.protocol_fingerprint,
                normalization={'wire_max':[instance.wire_x_max,instance.wire_y_max],
                               'axis_mode':instance.axis_mode, 'offset':offset, 'span':span})
            if bound_crop is not None:
                case_record.update(
                    artifact_role='precomputed_stage2_reference',
                    stage1_model_selected=False,
                    rough_point_policy=('known_target_plus_40_x_30_y_clamped'
                                        if rough_offset == [40, 30] else
                                        'known_target_plus_crop_safe_offset_clamped'),
                    rough_point_offset_wire_xy=rough_offset,
                    rough_point_wire_xy=[float(rough.x), float(rough.y)],
                )
            result['cases'].append(case_record)
    (output_dir/'samples.json').write_text(json.dumps(result,indent=2)+'\n')
    (output_dir/'README.md').write_text(
        '# Offline computer-use fixtures\n\n'
        f'This fixture set uses a {target_size_px}×{target_size_px}-pixel magenta target; '
        'the target size and half-open source-pixel bounds are recorded for each case in `samples.json`.\n\n'
        '`case-*-source.png` contains the original synthetic frame and '
        '`case-*-stage1.png` contains the complete overview independently stretched '
        'to a long-edge square before grid composition. The original source remains unchanged. The normalized X and Y axes '
        'each span 0–1000 independently of the PNG dimensions.\n\n'
        '`case-*-stage2.png` is a **precomputed reference** detail made with a '
        'synthetic rough point: it starts at the known target plus 40 on X and 30 on Y '
        'in the normalized 0–1000 domain, clamped at 1000, and reduces that offset only '
        'when needed to keep the full target visible inside the crop. The actual offset is '
        'recorded per case. It is **not the actual '
        'Stage-1-selected model input** of a two-stage probe. Its crop provenance '
        'and source rect are recorded in `samples.json`.\n\n'
        'The two-stage probe recomputes the crop from the Stage-1 model output. '
        'In ordinary mode, its Stage-2 input is saved at '
        '`annotated/stage2/case-*-stage2-input.png` relative to the fixture directory. '
        'With `--compare-grid`, the two actual paired model payloads are saved at '
        '`annotated/stage2/compare-grid/gridded/case-*-stage2-input.png` and '
        '`annotated/stage2/compare-grid/gridless/case-*-stage2-input.png`. '
        'All three paths are relative to the selected fixture directory. '
        'Those images may differ from the precomputed reference. '
        'Neither fixture type proves '
        'live desktop readiness or authorizes input.\n'
    )
    return result


def finite_json(value):
    """Retain failed samples as null instead of discarding a report on infinity."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: finite_json(item) for key,item in value.items()}
    if isinstance(value, (tuple,list)):
        return [finite_json(item) for item in value]
    return value

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=Path)
    parser.add_argument('--generate-fixtures', type=Path, nargs='?', const=DIAGRAMS_DIR)
    parser.add_argument('--target-size-px', type=int, default=17,
                        help='offline synthetic target side length in source pixels (default: 17)')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--max-axis-error', type=float)
    parser.add_argument('--min-pass-rate', type=float)
    args = parser.parse_args()
    args.samples = args.samples.expanduser() if args.samples is not None else None
    args.generate_fixtures = (
        args.generate_fixtures.expanduser() if args.generate_fixtures is not None else None
    )
    args.output = args.output.expanduser() if args.output is not None else None
    app = get_config()
    fingerprint = deployment_fingerprint(app)
    if args.generate_fixtures is not None:
        if args.target_size_px < 1 or args.target_size_px > 720:
            parser.error('--target-size-px must be between 1 and 720 to fit all fixtures')
        output_dir = args.generate_fixtures
        if output_dir == DIAGRAMS_DIR and args.target_size_px != 17:
            output_dir = DIAGRAMS_DIR / f'target-size-{args.target_size_px}px'
        generate_fixtures(app.computer_use, fingerprint, output_dir,
                          target_size_px=args.target_size_px)
        print(json.dumps({'fingerprint': fingerprint, 'artifacts': str(output_dir),
                          'target_size_px': args.target_size_px}))
        return 0
    if args.target_size_px != 17:
        parser.error('--target-size-px is available only with --generate-fixtures')
    if args.samples is None:
        print(json.dumps({'fingerprint': fingerprint}))
        return 0
    if args.output is None or args.max_axis_error is None or args.min_pass_rate is None:
        parser.error('samples require output and both measured acceptance limits')
    payload = json.loads(args.samples.read_text())
    if payload.get('fingerprint') != fingerprint:
        parser.error('sample deployment fingerprint differs')
    records = payload['cases']
    cases = [CalibrationCase(item['case_id'], item['width'], item['height'],
                            tuple(item['target_ratio']), len(item['outputs'])) for item in records]
    outputs = {item['case_id']: item['outputs'] for item in records}
    report = run_calibration(lambda case, protocol: outputs[case.case_id], cases,
        ProtocolDescriptor(app.computer_use.protocol, pixel_semantics=app.computer_use.pixel_semantics),
        {'max_axis_error': args.max_axis_error, 'min_pass_rate': args.min_pass_rate}, fingerprint=fingerprint)
    result = {name: thaw(getattr(report, name)) for name in report.__dataclass_fields__}
    result['desktop_geometry'] = payload['desktop_geometry']
    # Only the operator's measured serving evidence may populate this field.
    result['serving_preprocessing'] = payload.get('serving_preprocessing')
    # Failed reports remain inspectable but cannot be enabled by this tool.
    args.output.write_text(json.dumps(finite_json(result), indent=2, allow_nan=False) + '\n')
    verify_deployment_report(result, fingerprint, app.computer_use.protocol)
    print(json.dumps({'fingerprint': fingerprint, 'passed': report.passed, 'report': str(args.output)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
