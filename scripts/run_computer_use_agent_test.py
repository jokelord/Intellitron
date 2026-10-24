"""Run one real, isolated Agent computer-use experiment on the operator's desktop.

This CLI bypasses the release calibration report for one fresh in-process
Agent session. It does not change deployment configuration. Portal consent, native binding, two-stage
decoding, freshness checks and at-most-once dispatch remain active.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import sys
import uuid


from backend.agent.agent_runner import AgentRunner
from backend.computer_use.operator_policy import (
    DEFAULT_HELPER, SAFETY_FIELDS, OPERATOR_CONFIG_PATH,
    OPERATOR_LIMIT_CAPS, OPERATOR_TIMEOUT_CAPS, load_operator_preset,
)
from backend.computer_use.executable_backend import VisionDesktopGroundingBackend
from backend.computer_use.diagnostics import DiagnosticSink
from backend.computer_use.executable_protocol import (
    BackendExecutableResult, clear_vision_desktop_backend, install_vision_desktop_backend,
)
from backend.computer_use.runtime import (
    OperatorSessionRuntimeFactory, deployment_fingerprint,
)
from backend.config import get_config
from backend.llm_service import LLMService


EVIDENCE_ROOT = Path(__file__).resolve().parents[1] / 'logs' / 'computer-use' / 'operator-agent-tests'


def prepare_operator_test_app(deployment, *, helper_binary: str, safety_limits: dict,
                              test_timeouts: dict | None = None):
    """Build a private test config; never mutate or mark the deployment ready."""
    if deployment.computer_use.ready:
        raise ValueError('operator test requires release readiness to remain false')
    if ((deployment.agent.allow_desktop_capture or deployment.agent.allow_desktop_input)
            and not deployment.computer_use.operator_session_enabled):
        raise ValueError('desktop authority requires explicit operator session mode')
    if deployment.model.model_type != 'vision' or deployment.agent.entry_agent_name != 'basic_agent':
        raise ValueError('operator test requires the vision model and basic_agent entry profile')
    if not deployment.computer_use.enabled or deployment.computer_use.calibration_report_path is not None:
        raise ValueError('operator test requires enabled computer-use with no deployment report')
    if not isinstance(helper_binary, str) or not Path(helper_binary).is_absolute() or '\x00' in helper_binary:
        raise ValueError('helper_binary must be an absolute path')
    if not isinstance(safety_limits, dict) or set(safety_limits) != set(SAFETY_FIELDS):
        raise ValueError('operator test requires exactly six explicit safety limits')
    for name, cap in OPERATOR_LIMIT_CAPS.items():
        value = safety_limits[name]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not 0 < value <= cap):
            raise ValueError(f'{name} exceeds the operator test safety cap or is invalid')
    if test_timeouts is not None:
        if not isinstance(test_timeouts, dict) or set(test_timeouts) != set(OPERATOR_TIMEOUT_CAPS):
            raise ValueError('operator test requires exactly two test timeouts')
        for name, cap in OPERATOR_TIMEOUT_CAPS.items():
            value = test_timeouts[name]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not 0 < value <= cap):
                raise ValueError(f'{name} exceeds the operator test timeout cap or is invalid')
    fingerprint = deployment_fingerprint(deployment)
    app = copy.deepcopy(deployment)
    app.agent.allow_desktop_capture = True
    app.agent.allow_desktop_input = True
    app.agent.enabled_agent_profiles = ['basic_agent']
    app.agent.default_profile_tools = {
        'manager_agent': (), 'basic_agent': ('read_skill', 'exec_command'),
    }
    app.agent.allowed_executables = ['vision_desktop']
    app.agent.max_steps = min(app.agent.max_steps or 12, 12)
    app.computer_use = replace(app.computer_use, helper_binary=helper_binary,
                               calibration_report_path=None,
                               calibration_fingerprint=fingerprint,
                               **safety_limits, **(test_timeouts or {}))
    if not app.computer_use.ready:
        raise ValueError('operator test safety configuration is incomplete')
    if deployment_fingerprint(app) != fingerprint:
        raise ValueError('operator test configuration unexpectedly changed the deployment fingerprint')
    return app, fingerprint


class AuditedOperatorBackend:
    """Record operation outcomes without storing screenshots or model arguments."""

    def __init__(self, backend, *, session_id: str, audit):
        self.backend = backend
        self.session_id = session_id
        self.audit = audit

    def execute(self, envelope, capability, check_abort=None):
        if envelope.session_id != self.session_id:
            return BackendExecutableResult('INVALID_CONTEXT_BINDING',
                                           'operator test session mismatch', verified=False)
        result = self.backend.execute(envelope, capability, check_abort)
        self.audit({'operation': envelope.operation.value, 'state': result.state,
                    'verified': result.verified})
        return result

    def dispose_session(self, session_id):
        if session_id == self.session_id:
            self.backend.dispose_session(session_id)


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('limit must be a positive finite number') from exc
    import math
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError('limit must be a positive finite number')
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true',
                        help='validate configuration and print the live test command without Portal or input')
    parser.add_argument('--authorize-test-input', action='store_true',
                        help='authorize one isolated real Agent session and Portal input prompt')
    parser.add_argument('--enable-thinking', action='store_true',
                        help='enable model thinking for this Agent test (default: off)')
    parser.add_argument('--helper-binary', default=DEFAULT_HELPER, type=Path)
    parser.add_argument('--safety-config', default=OPERATOR_CONFIG_PATH, type=Path,
                        help='test-only YAML preset; individual safety flags override it')
    parser.add_argument('--target-description', required=True)
    parser.add_argument('--action', default='left_click', choices=(
        'left_click', 'right_click', 'left_double_click', 'right_double_click',
        'scroll_up', 'scroll_down', 'scroll_up_1', 'scroll_down_1'))
    for name in SAFETY_FIELDS:
        parser.add_argument('--' + name.replace('_', '-'), type=_positive_float)
    args = parser.parse_args(argv)
    if not args.check_only and not args.authorize_test_input:
        parser.error('--authorize-test-input is required')
    if not args.helper_binary.is_file() or not os.access(args.helper_binary, os.X_OK):
        parser.error('--helper-binary must name an executable file')
    if not args.target_description.strip():
        parser.error('--target-description must be non-empty')
    try:
        safety_limits, test_timeouts = load_operator_preset(args.safety_config)
        safety_limits.update({name: getattr(args, name) for name in SAFETY_FIELDS
                              if getattr(args, name) is not None})
        deployment = get_config()
        app, fingerprint = prepare_operator_test_app(
            deployment, helper_binary=str(args.helper_binary.resolve()),
            safety_limits=safety_limits, test_timeouts=test_timeouts)
    except ValueError as exc:
        parser.error(str(exc))

    if args.check_only:
        from backend.tools.skill_ops import list_skills
        if 'computer-use-vision' not in list_skills():
            parser.error('computer-use-vision skill is not available')
        supplied = list(sys.argv[1:] if argv is None else argv)
        live_args = [token for token in supplied if token != '--check-only']
        if '--authorize-test-input' not in live_args:
            live_args.append('--authorize-test-input')
        print(json.dumps({
            'check_only': True, 'ready_to_run': True, 'deployment_ready': False,
            'release_calibration_used': False, 'thinking_enabled': args.enable_thinking,
            'deployment_fingerprint': fingerprint,
            'test_command': shlex.join([sys.executable, '-m',
                                        'scripts.run_computer_use_agent_test', *live_args]),
        }, indent=2))
        return 0

    session_id = str(uuid.uuid4())
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
    directory = EVIDENCE_ROOT / run_id
    directory.mkdir(parents=True, exist_ok=False)
    audit_path = directory / 'audit.jsonl'
    status_path = directory / 'status.json'
    status = {
        'session_id': session_id, 'deployment_fingerprint': fingerprint,
        'deployment_ready': False, 'release_calibration_used': False,
        'calibration_exception': 'one_process_one_agent_session_operator_test',
        'safety_limits': safety_limits, 'test_timeouts': test_timeouts, 'action': args.action,
        'thinking_enabled': args.enable_thinking,
        'audit_path': str(audit_path), 'outcome': 'not_started',
    }
    status_path.write_text(json.dumps(status, indent=2) + '\n')
    print(json.dumps({'session_id': session_id, 'status': str(status_path),
                      'release_calibration_used': False, 'deployment_ready': False}))

    operation_states = []

    def audit(record):
        operation_states.append(record['state'])
        with audit_path.open('a') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')

    prompt = (
        'Read the computer-use-vision skill, then use its vision_desktop sequence in this '
        f'same Agent session to perform {args.action} on {args.target_description}. '
        'This is an operator-authorized desktop interaction; the operator judges the result. '
        'Use only read_skill and the vision_desktop command through exec_command. '
        'If the target or a fresh binding is uncertain, stop without input.'
    )
    runner = factory = None
    installed = False
    try:
        service = LLMService(app)
        if not service.initialize() or service.active_backend == 'mock':
            raise RuntimeError('real LLM provider unavailable; start SGLang or vLLM before Agent test')
        runner = AgentRunner(service, app)
        session = runner.get_or_create_session(session_id, mode='tool_calling')
        factory = OperatorSessionRuntimeFactory(
            app.computer_use, current_fingerprint=lambda: deployment_fingerprint(app),
            session_id=session_id, capability=session.capability)
        backend = AuditedOperatorBackend(VisionDesktopGroundingBackend(factory, diagnostics=DiagnosticSink(app.computer_use)),
                                         session_id=session_id, audit=audit)
        install_vision_desktop_backend(backend)
        installed = True
        status['outcome'] = 'running'
        status_path.write_text(json.dumps(status, indent=2) + '\n')
        result = asyncio.run(runner.run_sync(session_id, prompt, mode='tool_calling',
                                             enable_thinking=args.enable_thinking))
        status['outcome'] = 'agent_completed'
        print(result.get('answer', ''))
    except BaseException as exc:
        status['outcome'] = 'failed'
        status['error'] = getattr(exc, 'code', type(exc).__name__)
        raise
    finally:
        try:
            if runner is not None:
                asyncio.run(runner.dispose_session(session_id))
        finally:
            try:
                if factory is not None:
                    factory.shutdown()
            finally:
                if installed:
                    clear_vision_desktop_backend()
                status['action_sent'] = 'ACTION_SENT' in operation_states
                status['operation_states'] = operation_states
                status_path.write_text(json.dumps(status, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
