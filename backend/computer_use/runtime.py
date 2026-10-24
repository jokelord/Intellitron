"""Lazy, capability-bound production composition; no model calls or shell fallback."""
from __future__ import annotations

import hashlib
import json
import time
import threading
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from .calibration import calibration_fingerprint, verify_deployment_report
from .contracts import DetailCrop, ProtocolDescriptor
from .coordinates import crop_rect, local_extent
from .dispatch import Dispatcher
from .errors import ComputerUseError
from .executable_backend import VisionDesktopGroundingBackend
from .frame_store import FrameStore
from .grounding import GroundingEngine
from .model_adapter import describe_stage
from .native_client import NativeClient
from .diagnostics import DiagnosticSink
from .operator_policy import load_operator_preset
from .render import render_overview, render_detail_base, overlay_detail
from backend.security.command_policy import CommandPolicy
from backend.security.environment_builder import EnvironmentBuilder
from backend.security.process_supervisor import ProcessSupervisor


class GroundingRenderer:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self._details = {}

    def overview(self, frame, config):
        self._details.clear()
        return render_overview(frame, config)

    def crop(self, frame, decision, request, config):
        rect = crop_rect(frame.width, frame.height, decision.x, decision.y, decision.zoom)
        width, height = rect[2] - rect[0], rect[3] - rect[1]
        local = local_extent(width, height, frame.width, frame.height)
        side = config.model_square_side_px
        size = (side, side)
        crop = DetailCrop(
            str(uuid.uuid4()), request.request_id, request.session_id, frame.observation_id,
            frame.frame_id, request.binding_id, frame.geometry_version, frame.width,
            frame.height, *rect, *size, *local, *size, request.protocol.name,
            'relative_extent', 'pending-description', request.calibration_fingerprint,
            decision.x, decision.y, decision.zoom, config.grid_enabled, config.grid_divisions,
            self.clock(), request.request_id)
        base = render_detail_base(frame, crop, config)
        instance = describe_stage(
            'stage2', base, crop, request, grid_label_placement=config.grid_label_placement, stage1_zoom=config.stage1_zoom)
        crop = replace(crop, protocol_fingerprint=instance.protocol_fingerprint,
                       wire_semantics=instance.axis_mode)
        emitted = overlay_detail(base, crop, instance.label_formatter, config)
        final_instance = describe_stage(
            'stage2', emitted, crop, request, grid_label_placement=config.grid_label_placement, stage1_zoom=config.stage1_zoom)
        crop = replace(crop, protocol_fingerprint=final_instance.protocol_fingerprint)
        self._details = {crop.crop_id: emitted}
        return crop

    def detail(self, crop, config):
        try:
            return self._details[crop.crop_id]
        except KeyError as exc:
            raise ComputerUseError('INVALID_CROP_ID', 'detail image has expired') from exc


class RuntimeEngine(GroundingEngine):
    def __init__(self, *, supervisor, verify=None, **kwargs):
        super().__init__(**kwargs)
        self.supervisor = supervisor
        self.verify = verify

    def prepare(self, check_abort):
        if self.native.channel is None:
            raise ComputerUseError('DESKTOP_SESSION_RESET_REQUIRED', 'native connection ended; dispose this Agent session and start a new session')
        if self.verify is not None:
            self.verify()
        self.native.set_abort_check(check_abort)

    def close(self):
        self.dispatcher.stop()
        self.frame_store.invalidate(self.binding.binding_id)
        try:
            self.native.close()
        finally:
            self.supervisor.shutdown()


class SessionRuntimeFactory:
    def __init__(self, config, *, current_fingerprint, native_factory=NativeClient):
        self.config = config
        self.current_fingerprint = current_fingerprint
        self.native_factory = native_factory
        self._engines = {}

    def _verify(self):
        if not self.config.ready:
            raise ComputerUseError('CALIBRATION_REQUIRED', 'computer-use configuration is incomplete')
        try:
            report = json.loads(Path(self.config.calibration_report_path).read_text())
        except (OSError, TypeError, ValueError) as exc:
            raise ComputerUseError('CALIBRATION_REQUIRED', 'calibration report is unavailable') from exc
        current = self.current_fingerprint() if callable(self.current_fingerprint) else self.current_fingerprint
        verify_deployment_report(report, current, self.config.protocol)
        if self.config.calibration_fingerprint != current:
            raise ComputerUseError('CALIBRATION_REQUIRED', 'configured deployment fingerprint differs')
        return report

    def __call__(self, session_id, capability, *, context_nonce=None, check_abort=None):
        if capability.session_id != session_id or not capability.allow_desktop_capture:
            raise ComputerUseError('INVALID_CONTEXT_BINDING', 'capture authority is not bound')
        if not capability.allow_desktop_input:
            raise ComputerUseError('INPUT_AUTHORITY_REQUIRED', 'desktop binding requires input authority')
        self._verify()
        if not self.config.helper_binary:
            raise ComputerUseError('HELPER_NOT_BUILT', 'helper_binary is not configured')
        if session_id in self._engines:
            return self._engines[session_id]
        runtime = capability.command_runtime_config()
        supervisor = ProcessSupervisor(max_output_bytes=runtime.max_output_bytes,
                                       termination_grace_seconds=runtime.termination_grace_seconds)
        policy = CommandPolicy(command_mode=runtime.command_mode,
                               allowed_executables=runtime.allowed_executables,
                               command_cwd_allowlist=runtime.command_cwd_allowlist,
                               custom_blacklist=runtime.command_blacklist,
                               max_exec_timeout_seconds=runtime.max_exec_timeout_seconds,
                               max_output_bytes=runtime.max_output_bytes)
        native = self.native_factory(helper_binary=self.config.helper_binary,
            supervisor=supervisor, environment_builder=EnvironmentBuilder(runtime),
            command_policy=policy, owner_session_id=session_id,
            timeout=self.config.transport_timeout_seconds,
            authorization_timeout=self.config.authorization_timeout_seconds,
            max_packet_bytes=self.config.max_control_packet_bytes,
            max_frame_bytes=self.config.max_canonical_frame_bytes)
        try:
            binding = native.start(str(uuid.uuid4()), capability,
                                   context_nonce=context_nonce, check_abort=check_abort)
            if not binding.ready or binding.session_id != session_id or binding.backend not in {'wayland', 'x11'}:
                raise ComputerUseError('INPUT_DEVICE_NOT_READY', 'native binding is not ready')
            first_frame = native.freeze(binding.binding_id)
            expected_geometry = self._verify().get('desktop_geometry')
            actual_geometry = {'backend': binding.backend, 'logical_origin': list(binding.origin),
                               'logical_size': list(binding.size),
                               'pixel_size': [first_frame.width, first_frame.height]}
            if expected_geometry is not None and expected_geometry != actual_geometry:
                raise ComputerUseError('CALIBRATION_REQUIRED', 'selected monitor geometry differs from measured report')
            store = FrameStore()
            engine = RuntimeEngine(config=self.config, native=native, binding=binding,
                frame_store=store, renderer=GroundingRenderer(), supervisor=supervisor, verify=self._verify,
                initial_frame=first_frame,
                dispatcher=Dispatcher(native, config=self.config, binding=binding, frame_store=store))
            self._engines[session_id] = engine
            return engine
        except BaseException:
            native.close()
            supervisor.shutdown()
            raise

    def dispose_session(self, session_id):
        engine = self._engines.pop(session_id, None)
        if engine is not None:
            engine.close()

    def shutdown(self):
        for session_id in tuple(self._engines):
            self.dispose_session(session_id)


class OperatorSessionRuntimeFactory(SessionRuntimeFactory):
    """One-session native operator session without a release calibration.

    The explicit CLI or deployed operator policy may construct this factory. It
    does not persist a report or alter deployment readiness.
    """

    def __init__(self, config, *, current_fingerprint, session_id, capability,
                 native_factory=NativeClient):
        if (not config.ready or config.calibration_report_path is not None
                or not config.helper_binary):
            raise ComputerUseError('CALIBRATION_REQUIRED',
                                   'operator session requires explicit safety limits and helper')
        if capability.session_id != session_id or not capability.allow_desktop_capture or not capability.allow_desktop_input:
            raise ComputerUseError('INVALID_CONTEXT_BINDING',
                                   'operator session requires one bound desktop capability')
        super().__init__(config, current_fingerprint=current_fingerprint,
                         native_factory=native_factory)
        self.bound_session_id = session_id
        self.bound_capability = capability

    def _verify(self):
        current = self.current_fingerprint() if callable(self.current_fingerprint) else self.current_fingerprint
        if self.config.calibration_fingerprint != current:
            raise ComputerUseError('CALIBRATION_REQUIRED', 'operator session deployment fingerprint changed')
        return {'desktop_geometry': None}

    def __call__(self, session_id, capability, *, context_nonce=None, check_abort=None):
        if session_id != self.bound_session_id or capability is not self.bound_capability:
            raise ComputerUseError('INVALID_CONTEXT_BINDING',
                                   'operator session is bound to one exact Agent session')
        return super().__call__(session_id, capability,
                                context_nonce=context_nonce, check_abort=check_abort)


def deployment_fingerprint(app_config):
    """Derive current identity from model artifacts and effective preprocessing/config."""
    config = app_config.computer_use
    root = Path(app_config.model.model_path or '').expanduser()
    if not root.is_dir():
        raise ComputerUseError('CALIBRATION_REQUIRED', 'model directory unavailable for fingerprint')
    metadata = {}
    for name in ('config.json', 'tokenizer_config.json', 'preprocessor_config.json', 'processor_config.json', 'chat_template.jinja'):
        path = root / name
        if path.is_file():
            metadata[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    weights = [(str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
               for p in sorted(root.glob('*.safetensors'))]
    if not metadata or not weights:
        raise ComputerUseError('CALIBRATION_REQUIRED', 'model artifact identity is incomplete')
    renderer = {key: getattr(config, key) for key in
                ('model_square_side_px', 'grid_enabled', 'grid_divisions', 'grid_label_placement', 'grid_label_shadow_width_px', 'stage1_zoom')}
    renderer['canvas_policy'] = 'configured_shared_square_full_frame_and_clipped_crop_v5'
    renderer['implementation_sha256'] = hashlib.sha256(b''.join(
        Path(__file__).with_name(name).read_bytes() for name in
        ('render.py', 'runtime.py', 'coordinates.py', 'model_adapter.py', 'contracts.py', 'dispatch.py'))).hexdigest()
    from importlib.metadata import version, PackageNotFoundError
    packages = {}
    for name in ('Pillow', 'transformers', 'tokenizers', 'sglang', 'vllm'):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    serving = {'model_config': asdict(app_config.model),
               'computer_use_dispatch': {'pre_action_validation': config.pre_action_validation},
               'vision_upload': asdict(app_config.vision_upload),
               'inference': asdict(app_config.inference),
               'sglang': asdict(app_config.sglang), 'vllm': asdict(app_config.vllm),
               'packages': packages}
    return calibration_fingerprint(ProtocolDescriptor(config.protocol, pixel_semantics=config.pixel_semantics),
                                   renderer, {'metadata': metadata, 'weights': weights}, serving)


def build_runtime_backend(app_config):
    if app_config.computer_use.operator_session_enabled:
        return build_operator_session_backend(app_config)
    factory = SessionRuntimeFactory(app_config.computer_use,
                                    current_fingerprint=lambda: deployment_fingerprint(app_config))
    backend = VisionDesktopGroundingBackend(factory, diagnostics=DiagnosticSink(app_config.computer_use))
    backend.runtime_mode = 'release_calibrated'
    return backend


class WebOperatorSessionFactory:
    """Lazy exact-capability runtime per new web Agent session."""

    def __init__(self, config, *, current_fingerprint, native_factory=NativeClient):
        self.config = config
        self.current_fingerprint = current_fingerprint
        self.native_factory = native_factory
        self._factories = {}
        self._lock = threading.RLock()

    def __call__(self, session_id, capability, *, context_nonce=None, check_abort=None):
        with self._lock:
            factory = self._factories.get(session_id)
            if factory is None:
                factory = OperatorSessionRuntimeFactory(
                    self.config, current_fingerprint=self.current_fingerprint,
                    session_id=session_id, capability=capability,
                    native_factory=self.native_factory)
                self._factories[session_id] = factory
            return factory(session_id, capability, context_nonce=context_nonce,
                           check_abort=check_abort)

    def dispose_session(self, session_id):
        with self._lock:
            factory = self._factories.pop(session_id, None)
            if factory is not None:
                factory.dispose_session(session_id)

    def shutdown(self):
        with self._lock:
            for session_id in tuple(self._factories):
                self.dispose_session(session_id)


def build_operator_session_backend(app_config, *, native_factory=NativeClient):
    """Explicit operator permission policy; no application-specific checks."""
    config, agent = app_config.computer_use, app_config.agent
    if (not config.enabled or config.ready or config.calibration_report_path is not None
            or not agent.allow_desktop_capture or not agent.allow_desktop_input
            or app_config.model.model_type != 'vision' or agent.entry_agent_name != 'basic_agent'
            or not config.helper_binary or not Path(config.helper_binary).is_file()):
        raise ComputerUseError('CALIBRATION_REQUIRED',
                               'operator session configuration is incomplete')
    try:
        safety, timeouts = load_operator_preset(Path(config.operator_session_safety_config_path))
    except ValueError as exc:
        raise ComputerUseError(
            "INVALID_OPERATOR_SESSION_CONFIG", str(exc)) from exc
    fingerprint = deployment_fingerprint(app_config)
    operator_config = replace(config, calibration_fingerprint=fingerprint, **safety, **timeouts)
    if not operator_config.ready:
        raise ComputerUseError('CALIBRATION_REQUIRED', 'operator session safety limits are incomplete')
    factory = WebOperatorSessionFactory(
        operator_config, current_fingerprint=lambda: deployment_fingerprint(app_config),
        native_factory=native_factory)
    backend = VisionDesktopGroundingBackend(factory, diagnostics=DiagnosticSink(operator_config))
    backend.runtime_mode = 'operator_session'
    return backend
