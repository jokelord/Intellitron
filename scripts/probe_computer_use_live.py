"""Explicit operator transport probe: authorizes desktop capture/input, never sends input."""
from __future__ import annotations

import argparse
import copy
from dataclasses import replace
from pathlib import Path
import hashlib
import json
import uuid

from backend.config import get_config
from backend.computer_use.native_client import NativeClient, NativeUnavailable
from backend.security.capabilities import build_session_capability, require_capability
from backend.security.command_policy import CommandPolicy
from backend.security.environment_builder import EnvironmentBuilder
from backend.security.process_supervisor import ProcessSupervisor


class ProbeFailure(NativeUnavailable):
    def __init__(self, code, phase):
        super().__init__(code)
        self.phase = phase


def error_domain(code):
    # Ambiguous legacy codes stay unclassified; never infer capture vs input
    # solely from a shared GEOMETRY_CHANGED result.
    if (code.startswith('PIPEWIRE_') or code.startswith('FRAME_')
            or code in {'STALE_CHECK_INDETERMINATE', 'TEST_FRAME_NOT_FRESH'}):
        return 'capture'
    if code.startswith(('EIS_', 'INPUT_', 'XTEST_')):
        return 'input_mapping'
    if code.startswith('X11_'):
        return 'x11_transport'
    if code.startswith('XRANDR_'):
        return 'input_mapping'
    if code.startswith('PORTAL_') or code == 'SESSION_BUS_UNAVAILABLE':
        return 'portal'
    if code.startswith('TEST_SURFACE_'):
        return 'test_surface'
    if code in {'CREDENTIALS_UNAVAILABLE', 'INVALID_CREDENTIALS', 'INVALID_SENDER',
                'EOF_OR_TRUNCATED_PACKET', 'NATIVE_TIMEOUT'}:
        return 'helper_transport'
    return 'undetermined'


def transport_evidence(binding, *, mapping_status="verified", input_status="not_run"):
    if binding.backend == "x11":
        return {"backend": "x11", "portal_authorization": "not_applicable",
                "eis_mapping": "not_applicable", "x11_connection": "verified",
                "xrandr_mapping": mapping_status, "xtest_input": input_status}
    if binding.backend == "wayland":
        return {"backend": "wayland", "portal_authorization": "verified",
                "eis_mapping": mapping_status, "x11_connection": "not_applicable",
                "xrandr_mapping": "not_applicable", "xtest_input": "not_applicable"}
    raise NativeUnavailable("UNSUPPORTED_BACKEND")


def run_probe(app_config, *, authorization_timeout=None, transport_timeout=None):
    """Use configured authority without changing production readiness/calibration."""
    owner = str(uuid.uuid4())
    capability = build_session_capability(app_config, session_id=owner, mode='tool_calling')
    require_capability(capability, 'desktop_capture')
    # The combined Portal authorizes POINTER even though this probe emits none.
    require_capability(capability, 'desktop_input')
    config = app_config.computer_use
    if not config.helper_binary:
        raise NativeUnavailable('HELPER_NOT_BUILT')
    runtime = capability.command_runtime_config()
    supervisor = ProcessSupervisor(max_output_bytes=runtime.max_output_bytes,
                                   termination_grace_seconds=runtime.termination_grace_seconds)
    native = None
    phase = 'helper_preflight'
    try:
        policy = CommandPolicy(command_mode=runtime.command_mode,
            allowed_executables=runtime.allowed_executables,
            command_cwd_allowlist=runtime.command_cwd_allowlist,
            custom_blacklist=runtime.command_blacklist,
            max_exec_timeout_seconds=runtime.max_exec_timeout_seconds,
            max_output_bytes=runtime.max_output_bytes)
        native = NativeClient(helper_binary=config.helper_binary, supervisor=supervisor,
            environment_builder=EnvironmentBuilder(runtime), command_policy=policy,
            owner_session_id=owner,
            timeout=config.transport_timeout_seconds if transport_timeout is None else transport_timeout,
            authorization_timeout=config.authorization_timeout_seconds if authorization_timeout is None else authorization_timeout,
            max_packet_bytes=config.max_control_packet_bytes,
            max_frame_bytes=config.max_canonical_frame_bytes)
        phase = 'authorization_and_native_start'
        binding = native.start(str(uuid.uuid4()), capability, context_nonce=str(uuid.uuid4()))
        phase = 'first_frame'
        frame = native.freeze(binding.binding_id)
        return {
            **transport_evidence(binding), 'live_frame': 'verified',
            'input_events_sent': 0,
            'input_outcome': 'not_run', 'calibration': 'not_run', 'ready': False,
            'desktop_geometry': {'backend': binding.backend,
                'logical_origin': list(binding.origin), 'logical_size': list(binding.size),
                'pixel_size': [frame.width, frame.height]},
            'frame': {'sha256': hashlib.sha256(frame.rgb).hexdigest(),
                'width': frame.width, 'height': frame.height,
                'sequence': frame.sequence, 'stream_generation': frame.stream_generation,
                'geometry_version': frame.geometry_version,
                'captured_monotonic': frame.captured_monotonic},
        }
    except Exception as exc:
        raise ProbeFailure(getattr(exc, 'code', type(exc).__name__), phase) from exc
    finally:
        try:
            if native is not None:
                native.close()
        finally:
            supervisor.shutdown()


def _timeout(text):
    import math
    value = float(text)
    if not math.isfinite(value) or not .001 <= value <= 300:
        raise argparse.ArgumentTypeError('timeout must be .001..300 seconds')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authorize', action='store_true', help='grant capture and POINTER authorization for this isolated probe only; no input events are sent')
    parser.add_argument('--helper-binary', required=True, help='absolute path to the operator-selected native helper')
    parser.add_argument('--authorization-timeout', type=_timeout)
    parser.add_argument('--transport-timeout', type=_timeout)
    args = parser.parse_args(argv)
    if not args.authorize:
        parser.error('--authorize is required for isolated capture and POINTER authorization')
    if not Path(args.helper_binary).is_absolute() or '\x00' in args.helper_binary:
        parser.error('--helper-binary must be an absolute path')
    try:
        # Explicit CLI authorization is scoped to a fresh in-memory config and
        # session capability. Never persist flags or alter production readiness.
        isolated = copy.deepcopy(get_config())
        isolated.agent.allow_desktop_capture = True
        isolated.agent.allow_desktop_input = True
        isolated.computer_use = replace(isolated.computer_use, helper_binary=args.helper_binary)
        result = run_probe(isolated, authorization_timeout=args.authorization_timeout,
                           transport_timeout=args.transport_timeout)
    except Exception as exc:
        code = getattr(exc, 'code', type(exc).__name__)
        print(json.dumps({'ready': False, 'code': code,
                          'failure_phase': getattr(exc, 'phase', 'configuration_or_capability'),
                          'error_domain': error_domain(code),
                          'input_events_sent': 0, 'input_outcome': 'not_run', 'calibration': 'not_run'}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
