"""Offline two-stage SGLang probe with exact, inspectable point overlays.

The model sees only the unmarked production-rendered images. This script never
opens a Portal session or sends desktop input. The annotated PNGs are outputs.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import urllib.error
import urllib.request
from dataclasses import replace
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from backend.config import get_config
from backend.computer_use.contracts import (
    CanonicalFrame, GroundingRequest, InputBinding, InspectRegion, InteractDetail, Reobserve,
    ProtocolDescriptor, thaw,
)
from backend.computer_use.coordinates import local_to_monitor
from backend.computer_use.model_adapter import decode_stage, describe_stage, encode_stage, provider_messages_image_first
from backend.computer_use.render import render_detail_base, overlay_detail
from backend.computer_use.runtime import GroundingRenderer, deployment_fingerprint
from scripts.calibrate_computer_use import DIAGRAMS_DIR
from scripts.computer_use_probe_response import extract_probe_call


def font(size: int):
    path = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    return ImageFont.truetype(str(path), size) if path.exists() else ImageFont.load_default()


def cross(draw, x: float, y: float, color: str, radius: int = 12) -> None:
    draw.ellipse((x-radius, y-radius, x+radius, y+radius), outline=color, width=3)
    draw.line((x-radius-5, y, x+radius+5, y), fill=color, width=3)
    draw.line((x, y-radius-5, x, y+radius+5), fill=color, width=3)


def source_error_metrics(crop, source_x: float, source_y: float,
                         target_x: float, target_y: float) -> dict:
    """Measure both source axes against the actual clipped crop, not model pixels."""
    dx, dy = abs(source_x - target_x), abs(source_y - target_y)
    return {
        'absolute_source_axis_error_px': [dx, dy],
        'crop_relative_axis_error': [dx / crop.crop_width, dy / crop.crop_height],
        'crop_aspect_log_distance': abs(math.log(crop.crop_width / crop.crop_height)),
    }


def target_geometry(case: dict) -> tuple[tuple[float, float], tuple[int, int, int, int]]:
    """Read explicit target geometry, retaining compatibility with 17px manifests."""
    width, height = case['width'], case['height']
    size = int(case.get('target_size_px', 17))
    center = case.get('target_center_source_pixel')
    if center is None:
        center = [round(case['target_ratio'][0] * width),
                  round(case['target_ratio'][1] * height)]
    bounds = case.get('target_source_bounds')
    if bounds is None:
        left = size // 2
        x0, y0 = round(center[0]) - left, round(center[1]) - left
        bounds = [x0, y0, x0 + size, y0 + size]
    if len(bounds) != 4 or bounds[0] < 0 or bounds[1] < 0 or bounds[2] > width or bounds[3] > height:
        raise ValueError(f"{case.get('case_id', 'fixture')}: invalid target bounds")
    if bounds[2] - bounds[0] != size or bounds[3] - bounds[1] != size:
        raise ValueError(f"{case.get('case_id', 'fixture')}: target size differs from its bounds")
    return (float(center[0]), float(center[1])), tuple(int(value) for value in bounds)


def point_hits_target(source_x: float, source_y: float,
                      bounds: tuple[int, int, int, int]) -> bool:
    """Score a continuous point against half-open source-pixel bounds."""
    x0, y0, x1, y1 = bounds
    return x0 <= source_x < x1 and y0 <= source_y < y1


def target_safe_inset_bounds(bounds: tuple[int, int, int, int],
                             inset_fraction: float = 0.15) -> tuple[int, int, int, int]:
    """Match the controlled live probe's conservative target interior."""
    x0, y0, x1, y1 = bounds
    inset_x = max(2, int((x1 - x0) * inset_fraction))
    inset_y = max(2, int((y1 - y0) * inset_fraction))
    return x0 + inset_x, y0 + inset_y, x1 - inset_x, y1 - inset_y


def score_target_point(source_x: float, source_y: float,
                       bounds: tuple[int, int, int, int], *,
                       stage1_full_containment: bool) -> dict:
    safe_bounds = target_safe_inset_bounds(bounds)
    on_target = point_hits_target(source_x, source_y, bounds)
    on_safe_inset = point_hits_target(source_x, source_y, safe_bounds)
    return {
        'on_target': on_target,
        'on_target_safe_inset': on_safe_inset,
        'eligible_two_stage_hit': bool(stage1_full_containment and on_safe_inset),
        'target_safe_inset_source_bounds': list(safe_bounds),
    }


def annotated(image_bytes: bytes, title: str, footer: str,
              predicted: tuple[float, float] | None, target: tuple[float, float],
              crop_box: tuple[float, float, float, float] | None = None) -> Image.Image:
    base = Image.open(BytesIO(image_bytes)).convert('RGB')
    header_h, footer_h = 42, 34
    canvas = Image.new('RGB', (base.width, base.height + header_h + footer_h), '#111827')
    canvas.paste(base, (0, header_h))
    draw = ImageDraw.Draw(canvas)
    if crop_box is not None:
        x0, y0, x1, y1 = crop_box
        draw.rectangle((x0, y0+header_h, x1-1, y1-1+header_h), outline='#ffd400', width=3)
    cross(draw, target[0], target[1]+header_h, '#00e5ff')
    if predicted is not None:
        cross(draw, predicted[0], predicted[1]+header_h, '#ff3b30')
    draw.text((10, 10), title, font=font(16), fill='white')
    draw.text((10, header_h+base.height+8), footer, font=font(12), fill='white')
    return canvas


def contact_sheet(images: list[tuple[str, Image.Image]], path: Path) -> None:
    if not images:
        return
    path = Path(path).expanduser()
    card_w, card_h = 650, 430
    columns = 2
    rows = (len(images) + columns - 1) // columns
    sheet = Image.new('RGB', (columns*card_w, rows*card_h), '#202938')
    draw = ImageDraw.Draw(sheet)
    for index, (label, image) in enumerate(images):
        ox, oy = (index % columns)*card_w, (index // columns)*card_h
        draw.text((ox+12, oy+8), label, font=font(18), fill='white')
        thumb = image.copy()
        thumb.thumbnail((card_w-18, card_h-40), Image.Resampling.LANCZOS)
        sheet.paste(thumb, (ox+(card_w-thumb.width)//2, oy+34+(card_h-40-thumb.height)//2))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)


def model_id(api: str) -> str:
    with urllib.request.urlopen(api.rstrip('/') + '/v1/models', timeout=5) as response:
        return json.load(response)['data'][0]['id']


def infer(api: str, model: str, encoded, description: str) -> tuple[dict, dict]:
    tools = thaw(encoded.tools)
    payload = {
        'model': model,
        'messages': provider_messages_image_first(encoded),
        'tools': [{'type': 'function', 'function': {
            'name': tool['name'],
            'description': (description if tool['name'] != 'observe' else
                            'Request a fresh overview if the target is missing, ambiguous, stale, or in the wrong region.'),
            'parameters': tool['parameters'],
        }} for tool in tools],
        'tool_choice': ('auto' if len(tools) > 1 else
                        {'type': 'function', 'function': {'name': tools[0]['name']}}),
        'chat_template_kwargs': {'enable_thinking': False},
        'temperature': 0,
        'max_tokens': 2048,
        'stream': False,
    }
    request = urllib.request.Request(
        api.rstrip('/') + '/v1/chat/completions',
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'}, method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'SGLang HTTP {error.code}: {error.read().decode(errors="replace")}') from error
    return extract_probe_call(result['choices'][0], encoded)


def _gridless_stage2(frame, crop, request, config):
    """Render the same trusted source rectangle with no grid overlay."""
    plain_config = replace(config, grid_enabled=False)
    plain_crop = replace(crop, grid_enabled=False)
    base = render_detail_base(frame, plain_crop, plain_config)
    described = describe_stage('stage2', base, plain_crop, request,
                               grid_label_placement=plain_config.grid_label_placement, stage1_zoom=plain_config.stage1_zoom)
    plain_crop = replace(plain_crop, protocol_fingerprint=described.protocol_fingerprint,
                         wire_semantics=described.axis_mode)
    detail = overlay_detail(base, plain_crop, described.label_formatter, plain_config)
    encoded = encode_stage(describe_stage(
        'stage2', detail, plain_crop, request,
        grid_label_placement=plain_config.grid_label_placement, stage1_zoom=plain_config.stage1_zoom), detail)
    return plain_crop, detail, encoded


def _compare_variant(*, api, model, base, output_dir, index, variant, detail, crop,
                     encoded, target_x, target_y, target_bounds, target_size_px,
                     stage1_full_containment, source_width, source_height):
    """Save the exact model input and score one grid variant independently."""
    base = Path(base).expanduser()
    output_dir = Path(output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    sent_url = encoded.messages[0]['content'][1]['image_url']['url']
    if not sent_url.startswith('data:image/png;base64,'):
        raise RuntimeError('Stage-2 model payload is not an encoded PNG')
    sent_png = base64.b64decode(sent_url.partition(',')[2], validate=True)
    if sent_png != detail.png:
        raise RuntimeError('Stage-2 model payload differs from rendered image')
    name = f'case-{index}-stage2'
    input_path = output_dir / f'{name}-input.png'
    prompt_path = output_dir / f'{name}-prompt.txt'
    marked_path = output_dir / f'{name}-marked.png'
    input_path.write_bytes(sent_png)
    prompt_path.write_text(encoded.messages[0]['content'][0]['text'] + '\n')
    result = {
        'status': 'not_run', 'variant': variant,
        'input_image': str(input_path), 'input_image_relative': str(input_path.relative_to(base)),
        'input_sha256': hashlib.sha256(sent_png).hexdigest(),
        'prompt': str(prompt_path), 'prompt_relative': str(prompt_path.relative_to(base)),
        'annotated_image': str(marked_path), 'annotated_image_relative': str(marked_path.relative_to(base)),
        'crop_id': crop.crop_id,
        'source_rect': [crop.x0, crop.y0, crop.x1, crop.y1],
        'image_size': [detail.width, detail.height],
        'target_size_px': target_size_px,
        'target_source_bounds': list(target_bounds),
        'target_safe_inset_source_bounds': list(target_safe_inset_bounds(target_bounds)),
        'stage1_full_target_containment': bool(stage1_full_containment),
        'on_target': None,
        'on_target_safe_inset': None,
        'eligible_two_stage_hit': None,
    }
    target_image = ((target_x - crop.x0) / crop.crop_width * detail.width,
                    (target_y - crop.y0) / crop.crop_height * detail.height)
    try:
        raw, trace = infer(api, model, encoded,
                           'Return the final target point in this detail image without sending input.')
        result['model_trace'] = trace
        decision = decode_stage(encoded, raw)
        if isinstance(decision, Reobserve):
            result.update({'status': 'reobserve_requested', 'reason': decision.reason,
                           'target_source_pixel': [target_x, target_y]})
            marked = annotated(sent_png, f'{name} | target={target_size_px}px | {variant} | reobserve requested',
                               f'cyan: target  |  reason: {decision.reason}  |  no click or new overview',
                               None, target_image)
        elif not isinstance(decision, InteractDetail):
            raise ValueError(f'Stage-2 returned {type(decision).__name__}')
        else:
            model_point = (float(decision.x / crop.local_width) * detail.width,
                           float(decision.y / crop.local_height) * detail.height)
            rx, ry = local_to_monitor(crop, decision.x, decision.y)
            source_x, source_y = float(rx * source_width), float(ry * source_height)
            scores = score_target_point(source_x, source_y, target_bounds,
                                        stage1_full_containment=stage1_full_containment)
            marked = annotated(sent_png,
                               f'{name} | target={target_size_px}px | {variant} | final=({raw["arguments"]["x"]},{raw["arguments"]["y"]})',
                               f'red: model final point  |  cyan: target  |  outer hit: {scores["on_target"]}  |  safe-inset hit: {scores["on_target_safe_inset"]}',
                               model_point, target_image)
            result.update({
                'status': 'model_point', 'wire_xy': [raw['arguments']['x'], raw['arguments']['y']],
                'final_source_pixel': [source_x, source_y],
                'target_source_pixel': [target_x, target_y], **scores,
                **source_error_metrics(crop, source_x, source_y, target_x, target_y),
            })
    except (ValueError, KeyError, TypeError, RuntimeError, OSError) as error:
        result.update({'status': 'invalid_model_output', 'error': str(error)})
        marked = annotated(sent_png, f'{name} | target={target_size_px}px | {variant} | invalid model output',
                           f'cyan: target  |  error: {str(error)[:60]}', None, target_image)
    marked.save(marked_path)
    return result, marked


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path, default=DIAGRAMS_DIR)
    parser.add_argument('--stage1-results', type=Path)
    parser.add_argument('--infer-stage1', action='store_true')
    parser.add_argument('--infer-stage2', action='store_true')
    parser.add_argument('--case-id', action='append', help='Run one selected fixture case; may be repeated')
    parser.add_argument('--compare-grid', action='store_true',
                        help='infer gridded and gridless Stage-2 images from the same Stage-1 crop')
    parser.add_argument('--api', default='http://127.0.0.1:30000')
    args = parser.parse_args()
    args.fixtures = args.fixtures.expanduser()
    args.stage1_results = (
        args.stage1_results.expanduser() if args.stage1_results is not None else None
    )
    if args.compare_grid and not args.infer_stage2:
        parser.error('--compare-grid requires --infer-stage2')

    base = args.fixtures
    output = base / 'annotated'
    stage1_dir = output / 'stage1'
    stage2_dir = output / 'stage2'
    stage1_dir.mkdir(parents=True, exist_ok=True)
    stage2_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((base / 'samples.json').read_text())
    app = get_config()
    fingerprint = deployment_fingerprint(app)
    if manifest['fingerprint'] != fingerprint:
        raise SystemExit('fixture fingerprint differs; regenerate with scripts.calibrate_computer_use')
    config = replace(app.computer_use, calibration_fingerprint=fingerprint)
    descriptor = ProtocolDescriptor(config.protocol, pixel_semantics=config.pixel_semantics)
    stage1_cases = [case for case in manifest['cases'] if case['case_id'].endswith('-stage1')]
    if args.case_id:
        selected_ids = set(args.case_id)
        stage1_cases = [case for case in stage1_cases if case['case_id'] in selected_ids]
        if len(stage1_cases) != len(selected_ids):
            raise SystemExit('selected Stage-1 fixture case is missing')
    if not stage1_cases:
        raise SystemExit('no Stage-1 fixtures')
    saved_path = args.stage1_results or stage1_dir / 'stage1-annotations.json'
    saved = None
    saved_model = None
    if not args.infer_stage1:
        saved = json.loads(saved_path.read_text())
        if saved.get('fixture_fingerprint') != fingerprint:
            raise SystemExit('Stage-1 result fingerprint differs from current fixtures')
        saved_model = saved.get('model')
        saved = {item['case_id']: item['llm_output'] for item in saved['cases']}
    model = model_id(args.api) if args.infer_stage1 or args.infer_stage2 else None
    if args.infer_stage2 and saved_model and model != saved_model:
        raise SystemExit('Stage-1 and Stage-2 model identifiers differ')
    if args.infer_stage2 and not args.compare_grid:
        for stale in stage2_dir.glob('case-*-stage2-marked.png'):
            stale.unlink()
        (stage2_dir / 'stage2-contact-sheet.png').unlink(missing_ok=True)
    stage1_images: list[tuple[str, Image.Image]] = []
    stage2_images: list[tuple[str, Image.Image]] = []
    stage2_inputs: list[tuple[str, Image.Image]] = []
    comparison_inputs: dict[str, list[tuple[str, Image.Image]]] = {'gridded': [], 'gridless': []}
    comparison_marked: dict[str, list[tuple[str, Image.Image]]] = {'gridded': [], 'gridless': []}
    comparison_dir = stage2_dir / 'compare-grid'
    report = {'fixture_fingerprint': fingerprint, 'model': model,
              'model_thinking': {'enabled': False, 'max_tokens': 2048} if (args.infer_stage1 or args.infer_stage2) else None,
              'fixture_target_size_px': manifest.get('fixture_target_size_px', 17),
              'fixture_variant': manifest.get('fixture_variant', 'target-size-17px'),
              'purpose': 'offline model/renderer probe; no desktop input is sent', 'cases': []}
    report_path = output / ('two-stage-grid-comparison-results.json' if args.compare_grid else 'two-stage-results.json')
    stage1_observations = []

    for case in stage1_cases:
        case_id = case['case_id']
        index = int(case_id.split('-')[1])
        source_path = base / f'case-{index}-source.png'
        source = Image.open(source_path).convert('RGB')
        width, height = case['width'], case['height']
        if source.size != (width, height):
            raise SystemExit(f'{case_id}: source frame dimensions differ')
        binding = InputBinding('offline-binding', 'offline-context', 1, 'wayland', (0, 0),
                               (width, height), 1, 'offline-device', 'offline-region', {}, False)
        frame = CanonicalFrame(f'frame-{index}', f'observation-{index}', binding,
                               width, height, source.tobytes(), 1, index+1, 1, 1, False, {}, {})
        request = GroundingRequest(f'case-{index}', binding.session_id, binding.binding_id,
                                   'center of the small magenta square', 'left_click', descriptor, fingerprint)
        renderer = GroundingRenderer(clock=lambda: 1)
        overview = renderer.overview(frame, config)
        if overview.png != (base / f'{case_id}.png').read_bytes():
            raise SystemExit(f'{case_id}: overview differs from the frozen fixture')
        stage1_encoded = encode_stage(describe_stage(
            'stage1', overview, None, request,
            grid_label_placement=config.grid_label_placement, stage1_zoom=config.stage1_zoom), overview)
        if args.infer_stage1:
            raw1, model_trace1 = infer(args.api, model, stage1_encoded,
                                       'Return the approximate target position for a later detail crop.')
            raw1_args = raw1['arguments']
        else:
            raw1_args = saved[case_id]
            raw1 = {'name': 'inspect_region', 'arguments': raw1_args}
            model_trace1 = {'source': str(saved_path)}
        decision1 = decode_stage(stage1_encoded, raw1)
        if not isinstance(decision1, InspectRegion):
            raise SystemExit(f'{case_id}: expected InspectRegion')
        crop_config = replace(config, grid_enabled=True) if args.compare_grid else config
        crop = renderer.crop(frame, decision1, request, crop_config)
        detail = renderer.detail(crop, config)
        stage2_encoded = encode_stage(describe_stage(
            'stage2', detail, crop, request,
            grid_label_placement=crop_config.grid_label_placement, stage1_zoom=crop_config.stage1_zoom), detail)
        (target_x, target_y), target_bounds = target_geometry(case)
        target_size_px = int(case.get('target_size_px', 17))
        bx0, by0, bx1, by1 = target_bounds
        target_inside = crop.x0 <= bx0 and bx1 <= crop.x1 and crop.y0 <= by0 and by1 <= crop.y1
        point1 = (float(decision1.x / 1000) * overview.width,
                  float(decision1.y / 1000) * overview.height)
        target1 = (target_x / width * overview.width, target_y / height * overview.height)
        box1 = (crop.x0 / width * overview.width, crop.y0 / height * overview.height,
                crop.x1 / width * overview.width, crop.y1 / height * overview.height)
        marked1 = annotated(overview.png,
                            f'{case_id} | target={target_size_px}px | rough=({raw1_args["x"]},{raw1_args["y"]}), zoom={decision1.zoom}',
                            f'red: model point  |  cyan: target  |  yellow: proposed crop  |  target fully inside: {target_inside}',
                            point1, target1, box1)
        marked1_path = stage1_dir / f'{case_id}-marked.png'
        marked1.save(marked1_path)
        stage1_images.append((f'{case_id} | {target_size_px}px target', marked1))
        stage1_observations.append({'case_id': case_id, 'llm_output': raw1_args,
                                    'target_size_px': target_size_px,
                                    'target_inside_proposed_crop': target_inside,
                                    'annotated_image': str(marked1_path)})
        if args.infer_stage1:
            (stage1_dir / 'stage1-annotations.json').write_text(json.dumps({
                'fixture_fingerprint': fingerprint, 'model': model, 'cases': stage1_observations,
            }, ensure_ascii=False, indent=2) + '\n')
        detail_path = (comparison_dir / 'gridded' if args.compare_grid else stage2_dir) / f'case-{index}-stage2-input.png'
        if not args.compare_grid:
            detail_path.write_bytes(detail.png)
            (stage2_dir / f'case-{index}-stage2-prompt.txt').write_text(
                thaw(stage2_encoded.messages[0])['content'][0]['text'] + '\n')
            stage2_inputs.append((f'case-{index}-stage2 input', Image.open(BytesIO(detail.png)).convert('RGB')))
        record = {
            'case_id': f'case-{index}',
            'source_size': [width, height],
            'target_size_px': target_size_px,
            'target_source_bounds': list(target_bounds),
            'target_center_source_pixel': [target_x, target_y],
            'stage1': {'wire_xy': [raw1_args['x'], raw1_args['y']], 'zoom': decision1.zoom,
                       'target_fully_inside_crop': target_inside, 'model_trace': model_trace1,
                       'annotated_image': str(marked1_path)},
            'crop': {'source_rect': [crop.x0, crop.y0, crop.x1, crop.y1],
                     'source_size': [crop.crop_width, crop.crop_height],
                     'local_domain': [float(crop.local_width), float(crop.local_height)],
                     'detail_image_size': [detail.width, detail.height],
                     'detail_input': str(detail_path)},
            'stage2': {'status': 'not_run'},
        }
        if args.compare_grid:
            plain_crop, plain_detail, plain_encoded = _gridless_stage2(frame, crop, request, config)
            variants = {
                'gridded': (crop, detail, stage2_encoded),
                'gridless': (plain_crop, plain_detail, plain_encoded),
            }
            record['stage2_variants'] = {}
            for variant, (variant_crop, variant_detail, variant_encoded) in variants.items():
                scored, marked = _compare_variant(
                    api=args.api, model=model, base=base,
                    output_dir=comparison_dir / variant, index=index, variant=variant,
                    detail=variant_detail, crop=variant_crop, encoded=variant_encoded,
                    target_x=target_x, target_y=target_y,
                    target_bounds=target_bounds, target_size_px=target_size_px,
                    stage1_full_containment=target_inside,
                    source_width=width, source_height=height)
                record['stage2_variants'][variant] = scored
                comparison_inputs[variant].append((f'case-{index} {variant} input',
                    Image.open(BytesIO(variant_detail.png)).convert('RGB')))
                comparison_marked[variant].append((f'case-{index} {variant}', marked))
            record['stage2'] = record['stage2_variants']['gridded']
        elif args.infer_stage2:
            try:
                raw2, model_trace2 = infer(args.api, model, stage2_encoded,
                                           'Return the final target point in this detail image without sending input.')
                decision2 = decode_stage(stage2_encoded, raw2)
                target_x2 = (target_x - crop.x0) / crop.crop_width * detail.width
                target_y2 = (target_y - crop.y0) / crop.crop_height * detail.height
                marked2_path = stage2_dir / f'case-{index}-stage2-marked.png'
                if isinstance(decision2, Reobserve):
                    marked2 = annotated(detail.png, f'case-{index}-stage2 | reobserve requested',
                                         f'cyan: target  |  reason: {decision2.reason}  |  no click or new overview',
                                         None, (target_x2, target_y2))
                    record['stage2'] = {
                        'status': 'reobserve_requested', 'reason': decision2.reason,
                        'on_target': None, 'target_source_pixel': [target_x, target_y],
                        'model_trace': model_trace2, 'annotated_image': str(marked2_path),
                    }
                elif isinstance(decision2, InteractDetail):
                    model_x = float(decision2.x / crop.local_width) * detail.width
                    model_y = float(decision2.y / crop.local_height) * detail.height
                    rx, ry = local_to_monitor(crop, decision2.x, decision2.y)
                    source_x, source_y = float(rx * width), float(ry * height)
                    scores = score_target_point(source_x, source_y, target_bounds,
                                                stage1_full_containment=target_inside)
                    marked2 = annotated(detail.png,
                                        f'case-{index}-stage2 | final=({raw2["arguments"]["x"]},{raw2["arguments"]["y"]})',
                                        f'red: model final point  |  cyan: known target  |  outer hit: {scores["on_target"]}  |  safe-inset hit: {scores["on_target_safe_inset"]}',
                                        (model_x, model_y), (target_x2, target_y2))
                    record['stage2'] = {
                        'status': 'model_point', 'wire_xy': [raw2['arguments']['x'], raw2['arguments']['y']],
                        'final_source_pixel': [source_x, source_y], 'target_source_pixel': [target_x, target_y],
                        'target_size_px': target_size_px,
                        'target_source_bounds': list(target_bounds),
                        **scores, 'model_trace': model_trace2,
                        'annotated_image': str(marked2_path),
                        **source_error_metrics(crop, source_x, source_y, target_x, target_y),
                    }
                else:
                    raise ValueError(f'Stage-2 returned {type(decision2).__name__}')
                marked2.save(marked2_path)
                stage2_images.append((f'case-{index}-stage2 | {target_size_px}px target', marked2))
            except (ValueError, KeyError, TypeError, RuntimeError, TimeoutError, OSError) as error:
                record['stage2'] = {'status': 'inference_error', 'error': f'{type(error).__name__}: {error}'}
        report['cases'].append(record)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')

    stage1_count = len(report['cases'])
    full_crop_count = sum(row['stage1']['target_fully_inside_crop'] is True
                          for row in report['cases'])
    stage2_points = [row['stage2'] for row in report['cases']
                     if row['stage2'].get('status') == 'model_point']
    report['summary'] = {
        'stage1_full_target_containment': {
            'count': full_crop_count, 'total': stage1_count,
            'rate': (full_crop_count / stage1_count if stage1_count else None),
        },
        'stage2_outer_target_hits': {
            'count': sum(item.get('on_target') is True for item in stage2_points),
            'model_point_count': len(stage2_points), 'case_count': stage1_count,
        },
        'stage2_safe_inset_hits': {
            'count': sum(item.get('on_target_safe_inset') is True for item in stage2_points),
            'model_point_count': len(stage2_points), 'case_count': stage1_count,
        },
        'eligible_two_stage_hits': {
            'count': sum(item.get('eligible_two_stage_hit') is True for item in stage2_points),
            'model_point_count': len(stage2_points), 'case_count': stage1_count,
        },
    }

    contact_sheet(stage1_images, stage1_dir / 'stage1-contact-sheet.png')
    if args.compare_grid:
        comparison_sheets = {}
        for variant in ('gridded', 'gridless'):
            input_sheet = comparison_dir / variant / 'stage2-input-contact-sheet.png'
            marked_sheet = comparison_dir / variant / 'stage2-contact-sheet.png'
            contact_sheet(comparison_inputs[variant], input_sheet)
            contact_sheet(comparison_marked[variant], marked_sheet)
            comparison_sheets[f'{variant}_input'] = str(input_sheet)
            comparison_sheets[f'{variant}_marked'] = str(marked_sheet)
        report['paired_summary'] = {
            'cases': [
                {'case_id': row['case_id'],
                 'same_crop_rect': row['stage2_variants']['gridded']['source_rect'] ==
                                   row['stage2_variants']['gridless']['source_rect'],
                 'gridded': {'status': row['stage2_variants']['gridded']['status'],
                             'on_target': row['stage2_variants']['gridded']['on_target'],
                             'absolute_source_axis_error_px': row['stage2_variants']['gridded'].get('absolute_source_axis_error_px')},
                 'gridless': {'status': row['stage2_variants']['gridless']['status'],
                              'on_target': row['stage2_variants']['gridless']['on_target'],
                              'absolute_source_axis_error_px': row['stage2_variants']['gridless'].get('absolute_source_axis_error_px')}}
                for row in report['cases']
            ],
            'on_target_count': {variant: sum(row['stage2_variants'][variant]['on_target'] is True
                                            for row in report['cases'])
                                for variant in ('gridded', 'gridless')},
        }
    else:
        contact_sheet(stage2_inputs, stage2_dir / 'stage2-input-contact-sheet.png')
        contact_sheet(stage2_images, stage2_dir / 'stage2-contact-sheet.png')
    if args.infer_stage1:
        (stage1_dir / 'stage1-annotations.json').write_text(json.dumps({
            'fixture_fingerprint': fingerprint, 'model': model, 'cases': stage1_observations,
        }, ensure_ascii=False, indent=2) + '\n')
    report_path = output / ('two-stage-grid-comparison-results.json' if args.compare_grid else 'two-stage-results.json')
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({
        'report': str(report_path),
        'stage1_contact_sheet': str(stage1_dir / 'stage1-contact-sheet.png'),
        'stage2_input_contact_sheet': (comparison_sheets['gridded_input'] if args.compare_grid
                                       else str(stage2_dir / 'stage2-input-contact-sheet.png')),
        'stage2_contact_sheet': (comparison_sheets['gridded_marked'] if args.compare_grid
                                 else str(stage2_dir / 'stage2-contact-sheet.png') if stage2_images else None),
        'target_inside_stage1_crop': [row['stage1']['target_fully_inside_crop'] for row in report['cases']],
        'stage2': [row['stage2'] for row in report['cases']],
        **({'comparison_contact_sheets': comparison_sheets,
            'paired_summary': report['paired_summary']} if args.compare_grid else {}),
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
