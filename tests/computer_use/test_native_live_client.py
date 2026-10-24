import fcntl
import hashlib
import os
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from backend.computer_use.contracts import InputBinding
from backend.computer_use.native_client import NativeClient, NativeProtocolError, NativeUnavailable


def binding():
    return dict(binding_id='binding', session_id='owner', geometry_version=1,
                backend='wayland', origin=[0, 0], size=[2, 2], stream_generation=1,
                device_id='device', region_id='region',
                mapping_evidence={'mapping_id': 'monitor', 'full_monitor': True}, ready=True)


def frame_packet(*, sealed=True, **changes):
    rgb = b'\x12\x34\x56' * 4
    fd = os.memfd_create('test-frame', os.MFD_ALLOW_SEALING | os.MFD_CLOEXEC)
    os.write(fd, rgb)
    if sealed:
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
    payload = dict(binding=binding(), frame_id='frame', observation_id='observation', width=2,
                   height=2, stride=6, format='RGB', byte_size=12,
                   sha256=hashlib.sha256(rgb).hexdigest(), sequence=1,
                   captured_monotonic=time.monotonic(), liveness_proven=True)
    payload.update(changes)
    return ('frame', payload, (fd,))


class Channel:
    def __init__(self, response):
        self.response = response
        self.sent = []
        self.socket = SimpleNamespace(close=lambda: None, shutdown=lambda *_: None)
    def send(self, kind, payload):
        self.sent.append((kind, payload))
    def receive(self, **kwargs):
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def client(response):
    obj = NativeClient(helper_binary='/fake', supervisor=None, environment_builder=None,
                       command_policy=None, owner_session_id='owner')
    obj.binding_id = 'binding'
    obj.channel = Channel(response)
    obj._binding = InputBinding(**binding())
    return obj


def test_freeze_reads_immutable_sealed_rgb_and_closes_descriptor():
    packet = frame_packet()
    fd = packet[2][0]
    obj = client(packet)
    result = obj.freeze('binding')
    assert result.rgb == b'\x12\x34\x56' * 4
    assert result.binding.session_id == 'owner'
    with pytest.raises(OSError):
        os.fstat(fd)


@pytest.mark.parametrize('outer_seconds, helper_milliseconds', [
    (15.0, 14500), (5.0, 4750), (0.1, 50), (0.01, 5),
])
def test_freeze_reserves_ipc_response_time_for_helper_stale_result(
    outer_seconds, helper_milliseconds,
):
    obj = client(('error', {'code': 'STALE_CHECK_INDETERMINATE'}, ()))
    obj.timeout = outer_seconds
    channel = obj.channel

    with pytest.raises(NativeUnavailable, match='STALE_CHECK_INDETERMINATE'):
        obj.freeze('binding')

    assert channel.sent == [('freeze', {
        'binding_id': 'binding', 'timeout_ms': helper_milliseconds,
    })]
    assert helper_milliseconds < outer_seconds * 1000
    assert obj.channel is None


def test_freeze_too_short_for_helper_and_ipc_fails_before_send():
    obj = client(('error', {'code': 'STALE_CHECK_INDETERMINATE'}, ()))
    obj.timeout = 0.001
    channel = obj.channel

    with pytest.raises(NativeUnavailable, match='NATIVE_TIMEOUT'):
        obj.freeze('binding')

    assert all(kind != 'freeze' for kind, _payload in channel.sent)
    assert obj.channel is None


@pytest.mark.parametrize('changes', [dict(sha256='0'*64), dict(stride=7), dict(byte_size=11),
    dict(width=True), dict(sequence=0), dict(captured_monotonic=float('nan')),
    dict(captured_monotonic=time.monotonic()+10000), dict(extra=1),
    dict(binding={**binding(), 'session_id': 'other'})])
def test_bad_frame_closes_descriptor_and_channel(changes):
    packet = frame_packet(**changes)
    fd = packet[2][0]
    obj = client(packet)
    with pytest.raises(NativeProtocolError):
        obj.freeze('binding')
    assert obj.channel is None
    with pytest.raises(OSError):
        os.fstat(fd)


def test_unsealed_frame_rejected():
    obj = client(frame_packet(sealed=False))
    with pytest.raises(NativeProtocolError, match='SEAL'):
        obj.freeze('binding')


def test_transport_error_never_retries_input():
    obj = client(TimeoutError('timeout'))
    channel = obj.channel
    with pytest.raises(Exception, match='ACTION_DELIVERY_UNKNOWN'):
        obj.send_event('binding', 1, {'kind': 'button_down', 'button': 'left'},
                       deadline_monotonic=time.monotonic()+1)
    assert len(channel.sent) == 1
    assert obj.channel is None


def test_status_accepts_valid_live_binding():
    obj = client(('binding', {'binding': binding()}, ()))
    assert obj.resolve_binding() == InputBinding(**binding())


def test_cancel_before_send_closes_channel_without_event():
    obj = client(('event', {'code': 'EVENT_SUBMITTED'}, ()))
    channel = obj.channel
    obj.set_abort_check(lambda: True)
    with pytest.raises(Exception):
        obj.send_event('binding', 1, {'kind': 'button_down', 'button': 'left'},
                       deadline_monotonic=time.monotonic()+1)
    assert channel.sent == []
    assert obj.channel is None


def test_expired_input_deadline_sends_nothing():
    obj = client(('event', {'code': 'EVENT_SUBMITTED'}, ()))
    with pytest.raises(Exception, match='DEADLINE'):
        obj.send_event('binding', 1, {'kind': 'button_down', 'button': 'left'},
                       deadline_monotonic=time.monotonic()-1)
    assert obj.channel.sent == []


def test_frame_bound_rejected_before_read():
    obj = client(frame_packet())
    obj.max_frame_bytes = 9
    with pytest.raises(NativeProtocolError, match='SIZE'):
        obj.freeze('binding')


def test_stale_sequence_rejected():
    obj = client(frame_packet())
    obj._last_sequence = 1
    with pytest.raises(NativeProtocolError, match='LIVENESS'):
        obj.freeze('binding')


def test_binding_geometry_change_invalidates_helper():
    obj = client(('binding', {'binding': {**binding(), 'geometry_version': 2}}, ()))
    with pytest.raises(NativeProtocolError, match='BINDING_CHANGED'):
        obj.resolve_binding()
    assert obj.channel is None


def test_structured_frame_error_has_no_descriptor():
    obj = client(('error', {'code': 'PORTAL_CLOSED'}, ()))
    with pytest.raises(Exception, match='PORTAL_CLOSED') as failure:
        obj.freeze('binding')
    assert failure.value.code == 'PORTAL_CLOSED'
    assert obj.channel is None


def test_packet_channel_allows_error_without_frame_fd_but_not_frame_without_fd():
    import json
    import socket
    import struct
    from backend.computer_use.native_client import PacketChannel
    class Wire:
        def setsockopt(self, *args):
            pass
        def recvmsg(self, *args):
            envelope = dict(version=1,generation=1,sequence=1,nonce='a'*64,
                            kind=self.kind,payload={'code':'PORTAL_CLOSED'})
            return (json.dumps(envelope).encode(), [(socket.SOL_SOCKET,socket.SCM_CREDENTIALS,
                    struct.pack('3i',os.getpid(),os.getuid(),os.getgid()))], 0, None)
    wire = Wire()
    wire.kind = 'error'
    assert PacketChannel(wire,'a'*64,1).receive(expected_fds=1,allow_error=True)[0] == 'error'
    wire.kind = 'frame'
    with pytest.raises(NativeProtocolError,match='MISSING_FRAME_FD'):
        PacketChannel(wire,'a'*64,1).receive(expected_fds=1,allow_error=True)


def test_start_checks_input_authority_before_spawning(monkeypatch):
    import backend.security.capabilities as caps
    checked = []
    def require(capability, operation, target=None):
        checked.append(operation)
        if operation == 'desktop_input':
            raise caps.CapabilityError('desktop_disabled','disabled')
    monkeypatch.setattr(caps, 'require_capability', require)
    obj = client(None)
    obj.channel = None
    with pytest.raises(caps.CapabilityError):
        obj.start('binding', object())
    assert checked == ['session','desktop_capture','desktop_input']


def test_start_negotiates_ready_binding(monkeypatch):
    import backend.computer_use.native_client as module
    import backend.security.capabilities as caps
    monkeypatch.setattr(caps, 'require_capability', lambda *args: args[0])
    class ReadyChannel(Channel):
        def receive(self, **kwargs):
            if self.sent[-1][0] == 'hello':
                return ('unavailable', {'code':'NATIVE_NOT_STARTED'}, ())
            return super().receive(**kwargs)
    response_channel = ReadyChannel(('ready', {'binding':binding()}, ()))
    monkeypatch.setattr(module, 'PacketChannel', lambda *args, **kwargs: response_channel)
    process = SimpleNamespace(pid=os.getpid(), close=lambda: None)
    obj = NativeClient(helper_binary='/fake', owner_session_id='owner',
         supervisor=SimpleNamespace(start_managed=lambda *args, **kwargs: process),
         command_policy=SimpleNamespace(evaluate_desktop_helper=lambda *args, **kwargs: SimpleNamespace(allowed=True)),
         environment_builder=SimpleNamespace(build_environment=lambda: SimpleNamespace(env={}, cwd='/tmp')))
    exchanges = []
    original_exchange = obj._exchange
    def exchange(kind, payload, **kwargs):
        exchanges.append((kind, kwargs))
        return original_exchange(kind, payload, **kwargs)
    obj._exchange = exchange
    obj._last_sequence, obj._last_capture = 500, time.monotonic()
    assert obj.start('binding',object(),context_nonce='context') == InputBinding(**binding())
    assert obj._last_sequence == 0 and obj._last_capture == 0
    command, payload = response_channel.sent[1]
    assert command == 'start'
    assert payload['context_nonce'] == 'context'
    assert payload['allow_input'] is True
    assert payload['startup_timeout_ms'] == 5000
    assert dict(exchanges)['start']['timeout'] == 125
    assert payload['owner_session_id'] == 'owner'
    obj.close()


def test_start_requires_diagnostic_hello_before_authorization(monkeypatch):
    import backend.computer_use.native_client as module
    import backend.security.capabilities as caps
    monkeypatch.setattr(caps, 'require_capability', lambda *args: args[0])
    class Handshake(Channel):
        def receive(self, **kwargs):
            if self.sent[-1][0] == 'hello':
                return ('unavailable', {'code':'NATIVE_NOT_STARTED','probe':False}, ())
            return ('ready', {'binding':binding()}, ())
    channel = Handshake(None)
    monkeypatch.setattr(module,'PacketChannel',lambda *args,**kwargs:channel)
    obj = NativeClient(helper_binary='/fake',owner_session_id='owner',
        supervisor=SimpleNamespace(start_managed=lambda *args,**kwargs:SimpleNamespace(pid=1,close=lambda:None)),
        command_policy=SimpleNamespace(evaluate_desktop_helper=lambda *args,**kwargs:SimpleNamespace(allowed=True)),
        environment_builder=SimpleNamespace(build_environment=lambda: SimpleNamespace(env={}, cwd='/tmp')))
    obj.start('binding',object())
    assert [item[0] for item in channel.sent] == ['hello','start']


@pytest.mark.parametrize('bounds', [dict(timeout=301),dict(timeout=.0001),
    dict(authorization_timeout=.0001),dict(authorization_timeout=301),
    dict(max_frame_bytes=268435457)])
def test_constructor_rejects_native_incompatible_bounds(bounds):
    with pytest.raises(ValueError):
        NativeClient(helper_binary='/fake', supervisor=None, environment_builder=None,
                     command_policy=None, owner_session_id='owner', **bounds)


def test_idle_close_waits_for_native_cleanup_ack_before_process_shutdown():
    order = []
    obj = client(('closed', {'code': 'CLOSED'}, ()))
    channel = obj.channel
    original_receive = channel.receive
    def receive(**kwargs):
        order.append('ack')
        return original_receive(**kwargs)
    channel.receive = receive
    obj.process = SimpleNamespace(close=lambda: order.append('process_shutdown'))
    obj.close()
    assert channel.sent == [('close', {})]
    assert order == ['ack','process_shutdown']
    assert obj.channel is None and obj.process is None


def test_busy_close_does_not_interleave_with_pending_request():
    obj = client(('closed', {'code': 'CLOSED'}, ()))
    channel = obj.channel
    obj._exchange_active = True
    obj.close()
    assert channel.sent == []
    assert obj.channel is None


def test_idle_close_timeout_is_bounded_and_still_stops_process():
    import socket
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    obj = client(None)
    obj.channel.socket = left
    obj.timeout = .01
    stopped = []
    obj.process = SimpleNamespace(close=lambda: stopped.append(True))
    before = time.monotonic()
    try:
        obj.close()
        assert time.monotonic() - before < .5
        assert stopped == [True]
        assert left.fileno() == -1
    finally:
        right.close()


def test_fixed_absolute_helper_uses_real_policy_and_minimal_environment(tmp_path):
    import sys
    from dataclasses import replace
    from backend.security.environment_builder import EnvironmentBuilder, ArgvExecutionPlan
    from backend.security.command_policy import CommandPolicy
    from test_executable_backend import _capability
    cap = replace(_capability(tmp_path), python_executable=sys.executable)
    helper = tmp_path / 'native-helper'
    helper.write_text('#!/bin/sh\nexit 0\n')
    helper.chmod(0o700)
    plans = []
    class ReachedSupervisor(Exception): pass
    def spawn(plan, **kwargs):
        plans.append(plan)
        raise ReachedSupervisor()
    obj = NativeClient(helper_binary=str(helper), owner_session_id=cap.session_id,
        command_policy=CommandPolicy(), supervisor=SimpleNamespace(start_managed=spawn),
        environment_builder=EnvironmentBuilder(cap.command_runtime_config(),
            parent_environment={'PATH':'/untrusted','LD_PRELOAD':'/untrusted.so'}))
    with pytest.raises(ReachedSupervisor):
        obj.start('binding',cap)
    assert isinstance(plans[0], ArgvExecutionPlan)
    assert plans[0].argv[0] == str(helper)
    assert plans[0].argv[1] == '--control-fd'
    assert plans[0].cwd == str(tmp_path)
    assert 'LD_PRELOAD' not in plans[0].env
    assert '/untrusted' not in plans[0].env['PATH']
