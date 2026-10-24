import os
import socket

import pytest


@pytest.fixture(autouse=True)
def credential_socket_support():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        try:
            a.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        except PermissionError:
            pytest.skip('sandbox denies SO_PASSCRED; native IPC gate unavailable')
    finally:
        a.close()
        b.close()


def test_packet_rejects_sequence_and_unknown_fields():
    from backend.computer_use.native_client import PacketChannel, NativeProtocolError
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        sender = PacketChannel(a, 'a' * 64, 1)
        receiver = PacketChannel(b, 'a' * 64, 1)
        sender.send('hello', {})
        assert receiver.receive()[0] == 'hello'
        a.send(b'{"version":1,"generation":1,"sequence":1,"nonce":"' + b'a'*64 + b'","kind":"hello","payload":{}}')
        with pytest.raises(NativeProtocolError):
            receiver.receive()
    finally:
        a.close()
        b.close()


def test_unexpected_rights_closed():
    import array
    from backend.computer_use.native_client import PacketChannel, NativeProtocolError
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    fd = os.open('/dev/null', os.O_RDONLY)
    before = len(os.listdir('/proc/self/fd'))
    try:
        a.sendmsg([b'{}'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [fd]))])
        with pytest.raises(NativeProtocolError):
            PacketChannel(b, 'a'*64, 1).receive()
        assert len(os.listdir('/proc/self/fd')) == before
    finally:
        os.close(fd)
        a.close()
        b.close()
