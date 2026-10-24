"""Private, bounded desktop helper channel. Never exposes executable argv to a model."""
from __future__ import annotations

import array
import json
import os
import secrets
import socket
import stat
import struct
import fcntl
import hashlib
import math
import mmap
import select
import threading
import time

from .contracts import InputBinding, CanonicalFrame


class NativeProtocolError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class NativeUnavailable(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class PacketChannel:
    """Single-owner SOCK_SEQPACKET channel; descriptor ownership transfers on receive."""

    def __init__(self, sock, nonce, generation, *, max_packet_bytes=65536, peer_pid=None):
        if len(nonce) != 64 or any(c not in '0123456789abcdef' for c in nonce):
            raise ValueError('invalid nonce')
        if type(generation) is not int or generation < 1:
            raise ValueError('invalid generation')
        if type(max_packet_bytes) is not int or not 256 <= max_packet_bytes <= 1048576:
            raise ValueError('invalid packet bound')
        self.socket, self.nonce, self.generation = sock, nonce, generation
        self.max_packet_bytes, self.peer_pid = max_packet_bytes, peer_pid
        self.sent = self.received = 0
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        except OSError as exc:
            # Per-message credentials are part of the trust boundary. A
            # sandbox that denies this option is explicitly unavailable; do
            # not silently downgrade to unauthenticated IPC.
            raise NativeUnavailable("CREDENTIALS_UNAVAILABLE") from exc

    def send(self, kind, payload):
        packet = json.dumps(dict(version=1, generation=self.generation, sequence=self.sent+1,
            nonce=self.nonce, kind=kind, payload=payload), allow_nan=False,
            separators=(',', ':')).encode()
        if len(packet) > self.max_packet_bytes:
            raise NativeProtocolError('PACKET_TOO_LARGE')
        if self.socket.send(packet) != len(packet):
            raise NativeProtocolError('PARTIAL_PACKET')
        self.sent += 1

    def receive(self, *, expected_fds=0, allow_error=False):
        fds = []
        try:
            packet, ancillary, flags, _ = self.socket.recvmsg(self.max_packet_bytes,
                socket.CMSG_SPACE(16 * 4) + socket.CMSG_SPACE(12), socket.MSG_CMSG_CLOEXEC)
            credentials = []
            invalid_ancillary = False
            for level, kind, data in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    values = array.array('i')
                    values.frombytes(data[:len(data) - len(data) % values.itemsize])
                    fds.extend(values)
                elif level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS:
                    if len(data) == struct.calcsize('3i'):
                        credentials.append(struct.unpack('3i', data))
                    else:
                        invalid_ancillary = True
                else:
                    invalid_ancillary = True
            if invalid_ancillary:
                raise NativeProtocolError('UNEXPECTED_ANCILLARY')
            if not packet or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                raise NativeProtocolError('EOF_OR_TRUNCATED_PACKET')
            if expected_fds not in (0, 1) or (len(fds) != expected_fds and not (allow_error and len(fds) == 0)):
                raise NativeProtocolError('UNEXPECTED_FDS')
            if len(credentials) != 1 or credentials[0][1] != os.getuid():
                raise NativeProtocolError('INVALID_CREDENTIALS')
            if self.peer_pid is not None and credentials[0][0] != self.peer_pid:
                raise NativeProtocolError('INVALID_SENDER')
            def unique(pairs):
                obj = {}
                for key, value in pairs:
                    if key in obj:
                        raise NativeProtocolError('DUPLICATE_FIELD')
                    obj[key] = value
                return obj
            obj = json.loads(packet, object_pairs_hook=unique,
                parse_constant=lambda value: (_ for _ in ()).throw(NativeProtocolError('NONFINITE')))
            if not isinstance(obj, dict) or set(obj) != {'version','generation','sequence','nonce','kind','payload'}:
                raise NativeProtocolError('INVALID_FIELDS')
            for key, expected in [('version',1), ('generation',self.generation), ('sequence',self.received+1)]:
                if type(obj[key]) is not int or obj[key] != expected:
                    raise NativeProtocolError('INVALID_' + key.upper())
            if obj['nonce'] != self.nonce or not isinstance(obj['kind'], str) or not isinstance(obj['payload'], dict):
                raise NativeProtocolError('INVALID_ENVELOPE')
            if fds and obj['kind'] != 'frame':
                raise NativeProtocolError('UNEXPECTED_FRAME_FD')
            if expected_fds and not fds and not (allow_error and obj['kind'] in {'error', 'unavailable'}):
                raise NativeProtocolError('MISSING_FRAME_FD')
            self.received += 1
            return obj['kind'], obj['payload'], tuple(fds)
        except BaseException:
            for fd in fds:
                os.close(fd)
            raise


class NativeClient:
    """Session-owned synchronous channel; callers may run operations in a worker.

    Cancellation polls the caller's abort check while waiting, and destroys the
    helper on uncertainty. Input packets are never retried.
    """
    def __init__(self, *, helper_binary, supervisor, environment_builder, command_policy,
                 owner_session_id, timeout=5.0, max_packet_bytes=65536,
                 authorization_timeout=120.0, max_frame_bytes=268435456, check_abort=None):
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
               not math.isfinite(v) or not .001 <= v <= 300 for v in (timeout, authorization_timeout)):
            raise ValueError('invalid timeout')
        if type(max_frame_bytes) is not int or not 3 <= max_frame_bytes <= 268435456:
            raise ValueError('invalid frame bound')
        self.helper_binary, self.supervisor = helper_binary, supervisor
        self.environment_builder, self.command_policy = environment_builder, command_policy
        self.owner_session_id, self.timeout = owner_session_id, timeout
        self.authorization_timeout, self.max_frame_bytes = authorization_timeout, max_frame_bytes
        self.max_packet_bytes = max_packet_bytes
        self.process = self.channel = None
        self.binding_id = self._binding = None
        self._check_abort = check_abort
        self._lock = threading.RLock()
        self._exchange_active = False
        self._last_sequence = 0
        self._last_capture = 0.0

    def set_abort_check(self, callback):
        self._check_abort = callback

    def _abort_check(self):
        if self._check_abort is not None and self._check_abort():
            raise NativeUnavailable('CANCELLED')

    def start(self, binding_id, capability, *, context_nonce=None, check_abort=None):
        from backend.security.capabilities import require_capability
        require_capability(capability, 'session', self.owner_session_id)
        require_capability(capability, 'desktop_capture')
        require_capability(capability, 'desktop_input')
        if not isinstance(binding_id, str) or not binding_id:
            raise ValueError('invalid binding')
        if check_abort is not None:
            self.set_abort_check(check_abort)
        with self._lock:
            if self.process is not None:
                raise NativeUnavailable('HELPER_ALREADY_STARTED')
            self._abort_check()
            # Diagnose only the configured executable, before socket/launch.
            # Other FileNotFoundError causes (cwd, transport, etc.) stay distinct.
            if not isinstance(self.helper_binary, (str, os.PathLike)) or not self.helper_binary:
                raise NativeUnavailable('HELPER_NOT_BUILT')
            try:
                helper_stat = os.stat(self.helper_binary)
            except FileNotFoundError as exc:
                raise NativeUnavailable('HELPER_NOT_BUILT') from exc
            except PermissionError as exc:
                raise NativeUnavailable('HELPER_NOT_EXECUTABLE') from exc
            if not stat.S_ISREG(helper_stat.st_mode):
                raise NativeUnavailable('HELPER_NOT_BUILT')
            if not os.access(self.helper_binary, os.X_OK):
                raise NativeUnavailable('HELPER_NOT_EXECUTABLE')
            parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            try:
                self._abort_check()
                argv = (self.helper_binary, '--control-fd', str(child.fileno()))
                decision = self.command_policy.evaluate_desktop_helper(argv, capability,
                    configured_binary=self.helper_binary, owner_session_id=self.owner_session_id)
                if not decision.allowed:
                    raise NativeUnavailable(decision.reason_code)
                # Fixed-helper policy above authorizes this exact absolute path
                # and FD-only argv. The generic tool argv resolver intentionally
                # rejects absolute paths; keep that restriction intact.
                from backend.security.environment_builder import ArgvExecutionPlan
                environment = self.environment_builder.build_environment()
                plan = ArgvExecutionPlan(argv=argv, env=environment.env, cwd=environment.cwd)
                self.process = self.supervisor.start_managed(plan, pass_fds=(child.fileno(),),
                    owner_session_id=self.owner_session_id)
                parent.settimeout(self.timeout)
                nonce = secrets.token_hex(32)
                self.channel = PacketChannel(parent, nonce, 1,
                    max_packet_bytes=self.max_packet_bytes, peer_pid=self.process.pid)
                self.binding_id = binding_id
                hello_kind, hello_payload, _ = self._exchange('hello', {}, diagnostic=True)
                if hello_kind != 'unavailable' or not isinstance(hello_payload.get('code'), str):
                    raise NativeProtocolError('INVALID_HANDSHAKE_RESPONSE')
                kind, payload, _ = self._exchange('start', dict(binding_id=binding_id,
                    owner_session_id=self.owner_session_id, context_nonce=context_nonce or nonce,
                    allow_input=True, authorization_timeout_ms=max(1, int(self.authorization_timeout*1000)),
                    startup_timeout_ms=max(1, int(self.timeout*1000)),
                    max_frame_bytes=self.max_frame_bytes),
                    timeout=self.authorization_timeout + self.timeout)
                self._expect(kind, payload, 'ready', {'binding'})
                self._binding = self._decode_binding(payload['binding'])
                self._last_sequence, self._last_capture = 0, 0.0
                return self._binding
            except BaseException:
                parent.close()
                self.close()
                raise
            finally:
                child.close()

    def close(self, binding_id=None):
        # A concurrent cancellation must interrupt recv immediately. Only an
        # idle channel may serialize graceful native cleanup before shutdown.
        acquired = self._lock.acquire(blocking=False)
        channel, process = self.channel, self.process
        self.channel = self.process = None
        self.binding_id = self._binding = None
        try:
            if channel is not None:
                try:
                    if acquired and not self._exchange_active:
                        budget = min(self.timeout, 0.25)
                        deadline = time.monotonic() + budget
                        if isinstance(channel.socket, socket.socket):
                            channel.socket.settimeout(budget)
                        channel.send('close', {})
                        if isinstance(channel.socket, socket.socket):
                            remaining = deadline - time.monotonic()
                            if remaining <= 0 or not select.select([channel.socket], [], [], remaining)[0]:
                                raise TimeoutError('native close deadline')
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError('native close deadline')
                            channel.socket.settimeout(remaining)
                        kind, payload, _ = channel.receive()
                        if kind != 'closed' or payload.get('code') != 'CLOSED':
                            raise NativeProtocolError('INVALID_CLOSE_ACK')
                except Exception:
                    # No retries: hard shutdown remains the bounded fallback.
                    pass
                finally:
                    try:
                        channel.socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    channel.socket.close()
            if process is not None:
                process.close()
        finally:
            if acquired:
                self._lock.release()

    def doctor(self):
        from .doctor import diagnose
        return diagnose(self.helper_binary)

    def _exchange(self, kind, payload, *, frame=False, timeout=None, diagnostic=False):
        with self._lock:
            self._exchange_active = True
            try:
                self._abort_check()
                channel = self.channel
                if channel is None:
                    raise NativeUnavailable('NATIVE_BACKEND_UNAVAILABLE')
                channel.send(kind, payload)
                deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
                if isinstance(channel.socket, socket.socket):
                    while True:
                        self._abort_check()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise NativeUnavailable('NATIVE_TIMEOUT')
                        if select.select([channel.socket], [], [], min(0.05, remaining))[0]:
                            break
                result = channel.receive(expected_fds=1 if frame else 0, allow_error=frame)
                if not diagnostic and result[0] in {'error', 'unavailable'}:
                    if set(result[1]) != {'code'} or not isinstance(result[1]['code'], str):
                        raise NativeProtocolError('INVALID_ERROR')
                    raise NativeUnavailable(result[1]['code'])
                return result
            except BaseException:
                self.close()
                raise
            finally:
                self._exchange_active = False

    @staticmethod
    def _expect(kind, payload, expected, fields):
        if kind != expected or set(payload) != fields:
            raise NativeProtocolError('INVALID_' + expected.upper())

    def _decode_binding(self, payload):
        if not isinstance(payload, dict) or set(payload) != set(InputBinding.__dataclass_fields__):
            raise NativeProtocolError('INVALID_BINDING_FIELDS')
        try:
            binding = InputBinding(**payload)
        except (ValueError, TypeError) as exc:
            raise NativeProtocolError('INVALID_BINDING') from exc
        if (binding.binding_id != self.binding_id or binding.session_id != self.owner_session_id
                or binding.backend not in {'wayland', 'x11'} or not binding.ready
                or binding.stream_generation < 1 or binding.geometry_version < 1
                or binding.mapping_evidence.get('full_monitor') is not True
                or not isinstance(binding.mapping_evidence.get('mapping_id'), str)
                or not binding.mapping_evidence['mapping_id']):
            raise NativeProtocolError('INVALID_BINDING_IDENTITY')
        if binding.backend == 'x11':
            evidence = binding.mapping_evidence
            root, monitor = evidence.get('root_id'), evidence.get('monitor_atom')
            outputs = evidence.get('output_ids')
            if (evidence.get('source') != 'xrandr'
                    or evidence.get('coordinate_space') != 'x11_root'
                    or type(root) is not int or root < 1
                    or type(monitor) is not int or monitor < 1
                    or not isinstance(evidence.get('monitor_name'), str)
                    or not evidence['monitor_name']
                    or not isinstance(outputs, tuple) or not outputs
                    or any(type(output) is not int or output < 1 for output in outputs)
                    or len(set(outputs)) != len(outputs)
                    or binding.region_id != f'xrandr:{root}:{monitor}'
                    or evidence['mapping_id'] != binding.region_id
                    or binding.device_id != binding.binding_id
                    or any(value < 0 for value in binding.origin)):
                raise NativeProtocolError('INVALID_BINDING_EVIDENCE')
        if self._binding is not None and binding != self._binding:
            raise NativeProtocolError('BINDING_CHANGED')
        return binding

    def resolve_binding(self):
        with self._lock:
            try:
                kind, payload, _ = self._exchange('status', {})
                self._expect(kind, payload, 'binding', {'binding'})
                self._binding = self._decode_binding(payload['binding'])
                return self._binding
            except BaseException:
                self.close()
                raise

    def freeze(self, binding_id):
        with self._lock:
            fds = ()
            try:
                if binding_id != self.binding_id:
                    raise NativeProtocolError('INVALID_BINDING_IDENTITY')
                # The helper waits for a newer PipeWire frame until timeout_ms.
                # Leave the IPC channel time to receive its stale-frame error
                # instead of racing the helper with the same deadline.
                response_margin = min(self.timeout / 2,
                    max(0.05, min(0.5, self.timeout * 0.05)))
                helper_timeout_ms = int((self.timeout - response_margin) * 1000)
                if helper_timeout_ms < 1:
                    raise NativeUnavailable('NATIVE_TIMEOUT')
                kind, p, fds = self._exchange('freeze', dict(binding_id=binding_id,
                    timeout_ms=helper_timeout_ms), frame=True)
                self._expect(kind, p, 'frame', {'binding','frame_id','observation_id','width',
                    'height','stride','format','byte_size','sha256','sequence',
                    'captured_monotonic','liveness_proven'})
                if len(fds) != 1:
                    raise NativeProtocolError('UNEXPECTED_FDS')
                if any(not isinstance(p[k], str) or not p[k] for k in ('frame_id', 'observation_id')):
                    raise NativeProtocolError('INVALID_FRAME_IDENTITY')
                binding = self._decode_binding(p['binding'])
                if any(type(p[k]) is not int or p[k] < 1 for k in
                       ('width','height','stride','byte_size','sequence')):
                    raise NativeProtocolError('INVALID_FRAME_DIMENSIONS')
                if binding.backend == 'x11' and (p['width'], p['height']) != binding.size:
                    raise NativeProtocolError('INVALID_FRAME_DIMENSIONS')
                if (p['format'] != 'RGB' or p['stride'] != p['width']*3 or
                    p['byte_size'] != p['stride']*p['height'] or
                    p['byte_size'] > self.max_frame_bytes):
                    raise NativeProtocolError('INVALID_FRAME_SIZE')
                arrival = time.monotonic()
                stamp = p['captured_monotonic']
                if (isinstance(stamp, bool) or not isinstance(stamp, (int,float)) or
                    not math.isfinite(stamp) or not 0 <= stamp <= arrival or
                    stamp < self._last_capture or p['sequence'] <= self._last_sequence or
                    p['liveness_proven'] is not True):
                    raise NativeProtocolError('INVALID_FRAME_LIVENESS')
                fd = fds[0]
                required = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
                if fcntl.fcntl(fd, fcntl.F_GET_SEALS) & required != required:
                    raise NativeProtocolError('INVALID_FRAME_SEALS')
                if os.fstat(fd).st_size != p['byte_size']:
                    raise NativeProtocolError('INVALID_FRAME_FILE_SIZE')
                # Seals make bounded pread equally immutable on hosts that deny mmap.
                try:
                    with mmap.mmap(fd, p['byte_size'], access=mmap.ACCESS_READ) as mapping:
                        rgb = bytes(mapping)
                except PermissionError:
                    rgb = os.pread(fd, p['byte_size'], 0)
                if len(rgb) != p['byte_size']:
                    raise NativeProtocolError('INVALID_FRAME_FILE_SIZE')
                if hashlib.sha256(rgb).hexdigest() != p['sha256']:
                    raise NativeProtocolError('INVALID_FRAME_HASH')
                frame = CanonicalFrame(p['frame_id'], p['observation_id'], binding,
                    p['width'], p['height'], rgb, binding.stream_generation, p['sequence'],
                    stamp, arrival, True, {'full_monitor': True}, {'identity': True})
                self._last_capture, self._last_sequence = stamp, p['sequence']
                return frame
            except BaseException:
                self.close()
                raise
            finally:
                for fd in fds:
                    os.close(fd)

    def send_event(self, binding_id, geometry_version, event, *, deadline_monotonic=None):
        with self._lock:
            if (self._binding is None or binding_id != self.binding_id or
                    geometry_version != self._binding.geometry_version):
                raise NativeProtocolError('INVALID_BINDING_IDENTITY')
            if (isinstance(deadline_monotonic, bool) or not isinstance(deadline_monotonic, (int,float))
                    or not math.isfinite(deadline_monotonic) or deadline_monotonic <= time.monotonic()):
                raise NativeUnavailable('DISPATCH_DEADLINE_EXPIRED')
            try:
                kind, payload, _ = self._exchange('send_event', dict(binding_id=binding_id,
                    geometry_version=geometry_version, deadline_monotonic=deadline_monotonic,
                    event=event))
                self._expect(kind, payload, 'event', {'code'})
                if payload['code'] != 'EVENT_SUBMITTED':
                    raise NativeProtocolError('INVALID_EVENT_RESULT')
            except BaseException as exc:
                self.close()
                raise NativeUnavailable('ACTION_DELIVERY_UNKNOWN') from exc
