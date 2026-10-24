"""Run offline fixture calibration through a real top-level Agent context.

The production Agent builds its ordinary prompt, tool schema, and multimodal
history. Every tool implementation is replaced on this isolated runner with a
fixture-only executor before inference. No native helper, Portal session,
desktop authority, production backend installation, or physical input exists.
The output is measured samples and accepted-request evidence, never a passed
calibration report.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import uuid

from PIL import Image

from backend.config import get_config
from backend.agent.agent_runner import AgentRunner
from backend.agent.computer_use_calibration_evidence import AgentCalibrationEvidence, DIAGRAMS_ROOT
from backend.computer_use.contracts import (
    Action,
    CanonicalFrame, GroundingRequest, InputBinding, InspectRegion, ProtocolDescriptor,
)
from backend.computer_use.coordinates import local_to_monitor
from backend.computer_use.executable_protocol import (
    VisionDesktopCommandResult, make_trusted_observation_attachment,
    parse_vision_desktop_command, VisionDesktopOperation,
)
from backend.computer_use.runtime import GroundingRenderer, deployment_fingerprint
from backend.llm_service import LLMService


_SKILL = Path(__file__).resolve().parents[1] / 'backend' / 'skills' / 'computer-use-vision' / 'SKILL.md'


class FixtureDesktop:
    """A synthetic observation source whose final point is measured, never sent."""

    def __init__(self, *, session_id, source: Image.Image, config, fingerprint):
        width, height = source.size
        binding = InputBinding('offline-binding', session_id, 1, 'wayland', (0, 0),
                               (width, height), 1, 'offline-device', 'offline-region', {}, False)
        self.frame = CanonicalFrame('fixture-frame', 'fixture-observation', binding,
                                    width, height, source.convert('RGB').tobytes(),
                                    1, 1, 1, 1, False, {}, {})
        self.config = replace(config, calibration_fingerprint=fingerprint)
        self.renderer = GroundingRenderer(clock=lambda: 1)
        self.fingerprint = fingerprint
        self.session_id = session_id
        self.request = None
        self.crop = None
        self.final_ratio = None
        self.input_events_sent = 0
        self.observation_count = 0

    def _image_result(self, state: str, artifact) -> VisionDesktopCommandResult:
        image = Image.open(BytesIO(artifact.png)).convert('RGB')
        self.observation_count += 1
        return VisionDesktopCommandResult(
            state, state.lower(), make_trusted_observation_attachment(
                image=image, observation_id=f'fixture-observation-{self.observation_count}',
                frame_id='fixture-frame', geometry_version=1))

    def execute(self, tool_name: str, arguments) -> object:
        if tool_name == 'read_file':
            path = (arguments.get('path') or arguments.get('file_path')
                    if isinstance(arguments, dict) else None)
            if isinstance(path, str) and Path(path).resolve() == _SKILL.resolve():
                return _SKILL.read_text()
            raise RuntimeError('OFFLINE_CALIBRATION_TOOL_BLOCKED')
        if tool_name != 'exec_command' or not isinstance(arguments, dict) or set(arguments) != {'command'}:
            raise RuntimeError('OFFLINE_CALIBRATION_TOOL_BLOCKED')
        command = parse_vision_desktop_command(arguments['command'])
        if command.operation is VisionDesktopOperation.OBSERVE:
            self.request = GroundingRequest(
                str(uuid.uuid4()), self.session_id, self.frame.binding.binding_id,
                command.target_description or "current desktop", command.action or "left_click",
                ProtocolDescriptor(self.config.protocol, pixel_semantics=self.config.pixel_semantics),
                self.fingerprint)
            self.bound_action = command.action
            self.crop = None
            return self._image_result('OBSERVATION_READY', self.renderer.overview(self.frame, self.config))
        if self.request is None:
            raise RuntimeError('OFFLINE_CALIBRATION_OBSERVE_REQUIRED')
        if command.operation is VisionDesktopOperation.INSPECT_REGION:
            self.crop = None
            rough = InspectRegion(command.x, command.y, command.zoom)
            self.crop = self.renderer.crop(self.frame, rough, self.request, self.config)
            return self._image_result('DETAIL_READY', self.renderer.detail(self.crop, self.config))
        if self.crop is None or self.final_ratio is not None:
            raise RuntimeError('OFFLINE_CALIBRATION_DETAIL_REQUIRED')
        if self.bound_action is not None and command.action != self.bound_action:
            raise RuntimeError('OFFLINE_CALIBRATION_ACTION_MISMATCH')
        self.request = replace(self.request, action=Action(command.action))
        nx, ny = local_to_monitor(self.crop, command.x, command.y)
        self.final_ratio = [float(nx), float(ny)]
        return VisionDesktopCommandResult('CALIBRATION_POINT_RECORDED',
                                          'Measured normalized point; no desktop input was sent.')


def _guard_agent_tools(runner: AgentRunner, fixture: FixtureDesktop) -> None:
    """Preserve prompt/schema objects while replacing every executable body."""
    original = runner._build_profile_agent

    def guarded_build(*args, **kwargs):
        profile = args[0] if args else kwargs.get('profile_name', kwargs.get('profile'))
        if profile != 'basic_agent':
            raise RuntimeError('OFFLINE_CALIBRATION_PROFILE_BLOCKED')
        agent = original(*args, **kwargs)
        if agent.managed_agents:
            raise RuntimeError('OFFLINE_CALIBRATION_MANAGED_AGENT_BLOCKED')
        for name, tool in agent.tools.items():
            def offline_forward(*positional, _name=name, **named):
                arguments = dict(named)
                if positional:
                    if len(positional) != 1 or arguments:
                        raise RuntimeError('OFFLINE_CALIBRATION_TOOL_BLOCKED')
                    arguments = positional[0]
                return fixture.execute(_name, arguments)
            tool.forward = offline_forward
        return agent

    runner._build_profile_agent = guarded_build


def run_fixture_calibration(app, *, fixtures: Path, output_dir: Path = DIAGRAMS_ROOT,
                            repeats: int = 2, service_factory=None,
                            case_ids: tuple[str, ...] | None = None,
                            resume: bool = False) -> dict:
    if app.model.model_type != 'vision' or app.agent.entry_agent_name != 'basic_agent':
        raise ValueError('offline harness requires vision/basic_agent')
    if ((app.agent.allow_desktop_capture or app.agent.allow_desktop_input)
            and not app.computer_use.operator_session_enabled):
        raise ValueError('offline harness refuses unscoped deployment desktop authority')
    app = copy.deepcopy(app)
    app.agent.allow_desktop_capture = False
    app.agent.allow_desktop_input = False
    if type(repeats) is not int or not 2 <= repeats <= 20:
        raise ValueError('repeats must be an integer in [2,20]')
    fixtures, output_dir = Path(fixtures), Path(output_dir)
    if service_factory is None and not output_dir.resolve().is_relative_to(DIAGRAMS_ROOT.resolve()):
        raise ValueError('real-model evidence must remain under logs/computer-use/calibration-evidence')
    manifest = json.loads((fixtures / 'samples.json').read_text())
    fingerprint = deployment_fingerprint(app)
    if manifest.get('fingerprint') != fingerprint:
        raise ValueError('fixture fingerprint differs from current deployment')
    available = [case for case in manifest['cases'] if case['case_id'].endswith('-stage1')]
    if case_ids is not None and len(set(case_ids)) != len(case_ids):
        raise ValueError('requested fixture cases must be unique')
    selected = [case for case in available if case_ids is None or case['case_id'] in case_ids]
    if not selected or (case_ids is not None and len(selected) != len(case_ids)):
        raise ValueError('requested fixture cases are missing')
    result = {'fingerprint': fingerprint, 'desktop_geometry': None,
              'serving_preprocessing': None, 'passed': False,
              'purpose': 'offline measured same-Agent model outputs; no desktop input or readiness claim',
              'cases': []}
    samples_path = output_dir / 'samples.json'
    if resume:
        try:
            previous = json.loads(samples_path.read_text())
        except (OSError, ValueError) as exc:
            raise ValueError('resume requires an existing measured samples.json') from exc
        if (not isinstance(previous, dict)
                or {key: value for key, value in previous.items() if key != 'cases'} !=
                    {key: value for key, value in result.items() if key != 'cases'}
                or not isinstance(previous.get('cases'), list)
                or len(previous['cases']) > len(selected)):
            raise ValueError('resume samples differ from current deployment or harness')
        for position, old in enumerate(previous['cases']):
            expected = selected[position]
            if (not isinstance(old, dict)
                    or old.get('case_id') != expected['case_id'].replace('-stage1', '-stage2-measured')
                    or old.get('width') != expected['width'] or old.get('height') != expected['height']
                    or old.get('target_ratio') != expected['target_ratio']
                    or not isinstance(old.get('outputs'), list)
                    or not isinstance(old.get('runs'), list)
                    or len(old['outputs']) != len(old['runs'])
                    or len(old['outputs']) > repeats
                    or (position < len(previous['cases']) - 1 and len(old['outputs']) != repeats)
                    or any(not isinstance(run, dict) or run.get('input_events_sent') != 0
                           or run.get('measured_ratio') != output
                           for run, output in zip(old['runs'], old['outputs']))):
                raise ValueError('resume samples differ from selected fixture or requested repeats')
        result['cases'] = previous['cases']
    elif samples_path.exists():
        raise ValueError('samples.json already exists; use resume to preserve measured runs')
    output_dir.mkdir(parents=True, exist_ok=True)
    for case_position, case in enumerate(selected):
        index = case['case_id'].split('-')[1]
        with Image.open(fixtures / f'case-{index}-source.png') as image:
            source = image.convert('RGB').copy()
        if source.size != tuple(case['source_size']):
            raise ValueError('source fixture dimensions differ from manifest')
        prior = result['cases'][case_position] if case_position < len(result['cases']) else None
        outputs = list(prior['outputs']) if prior is not None else []
        runs = list(prior['runs']) if prior is not None else []
        for repetition in range(len(outputs), repeats):
            session_id = str(uuid.uuid4())
            run_id = f'{case["case_id"]}-repeat-{repetition + 1}-{uuid.uuid4().hex[:8]}'
            fixture = FixtureDesktop(session_id=session_id, source=source,
                                     config=app.computer_use, fingerprint=fingerprint)
            service = service_factory() if service_factory is not None else LLMService(app)
            if service_factory is None and (not service.initialize() or service.active_backend == 'mock'):
                raise RuntimeError('real LLM provider unavailable; start SGLang or vLLM before calibration')
            runner = AgentRunner(service, app)
            _guard_agent_tools(runner, fixture)
            session = runner.get_or_create_session(session_id, mode='tool_calling')
            if session.capability.allow_desktop_capture or session.capability.allow_desktop_input:
                raise RuntimeError('offline session unexpectedly has desktop authority')
            session.computer_use_calibration_evidence = AgentCalibrationEvidence(
                session_id, run_id=run_id, output_root=output_dir)
            error = None
            try:
                asyncio.run(runner.run_sync(session_id, 
                    'Use the computer-use-vision executable sequence to locate the center of the small magenta '
                    'square on this offline synthetic fixture. Return after the final point is recorded; '
                    'the fixture executor never sends desktop input.', mode='tool_calling',
                    enable_thinking=False))
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'[:300]
            outputs.append(fixture.final_ratio if fixture.final_ratio is not None else [None, None])
            runs.append({'evidence_run_id': run_id, 'session_id': session_id,
                         'measured_ratio': fixture.final_ratio, 'error': error,
                         'input_events_sent': fixture.input_events_sent})
            current = {'case_id': f'case-{index}-stage2-measured',
                       'width': case['width'], 'height': case['height'],
                       'target_ratio': case['target_ratio'], 'outputs': list(outputs),
                       'runs': list(runs)}
            partial = {**result, 'cases': [*result['cases'][:case_position], current]}
            samples_path.write_text(json.dumps(partial, ensure_ascii=False, indent=2) + '\n')
        completed = {'case_id': f'case-{index}-stage2-measured',
                     'width': case['width'], 'height': case['height'],
                     'target_ratio': case['target_ratio'], 'outputs': outputs,
                     'runs': runs}
        if prior is None:
            result['cases'].append(completed)
        else:
            result['cases'][case_position] = completed
    samples_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path, default=Path(__file__).resolve().parents[1] /
                        'logs' / 'computer-use' / 'v2.0')
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--case-id', action='append', dest='case_ids',
                        help='stage-1 fixture case to measure (repeat for more cases)')
    parser.add_argument('--output-dir', type=Path,
                        help='evidence run directory; required when resuming')
    parser.add_argument('--resume', action='store_true',
                        help='continue incomplete repetitions in an existing output directory')
    args = parser.parse_args(argv)
    if args.resume and args.output_dir is None:
        parser.error('--resume requires --output-dir')
    output = args.output_dir or DIAGRAMS_ROOT / f'run-{uuid.uuid4().hex[:12]}'
    result = run_fixture_calibration(get_config(), fixtures=args.fixtures,
                                     repeats=args.repeats, output_dir=output,
                                     case_ids=tuple(args.case_ids) if args.case_ids else None,
                                     resume=args.resume)
    print(json.dumps({'samples': str(output / 'samples.json'),
                      'case_count': len(result['cases']), 'passed': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
