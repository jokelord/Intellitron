"""Exercise the compiled helper through its authenticated production channel."""
import json
import os
import socket
import subprocess
import sys


def packet(sequence=1, kind="hello", **changes):
    data = dict(version=1, generation=1, sequence=sequence,
                nonce="a" * 64, kind=kind, payload={})
    data.update(changes)
    return json.dumps(data).encode()


def launch(binary):
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        parent.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        process = subprocess.Popen([binary, "--control-fd", str(child.fileno())],
            pass_fds=(child.fileno(),), env={**os.environ,
                "XDG_SESSION_TYPE": "wayland", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/computer-use-test-bus"})
    except BaseException:
        parent.close()
        raise
    finally:
        child.close()
    parent.settimeout(5)
    return parent, process


def receive(sock, sequence):
    result = json.loads(sock.recv(65536))
    assert result["sequence"] == sequence
    assert result["nonce"] == "a" * 64
    assert result["generation"] == 1
    return result


def main(binary):
    try:
        sock, process = launch(binary)
    except PermissionError:
        print("SKIP: sandbox denies SO_PASSCRED; run this gate on the desktop host")
        return 77
    try:
        sock.send(packet())
        first = receive(sock, 1)
        assert first["kind"] == "unavailable"
        assert first["payload"]["code"] == "SESSION_BUS_UNAVAILABLE"
        assert first["payload"]["capture_ready"] is False
        assert first["payload"]["input_ready"] is False
        sock.send(packet(2, "status"))
        assert receive(sock, 2)["kind"] == "unavailable"
        sock.send(packet(3, "send_event"))
        assert receive(sock, 3)["payload"]["code"] == "INVALID_EVENT"
        sock.send(packet(4, "close"))
        assert receive(sock, 4)["kind"] == "closed"
        assert process.wait(timeout=5) == 0
    finally:
        sock.close()
        if process.poll() is None:
            process.kill()
        process.wait()

    invalid = [packet(version=2), packet(sequence=2), packet(nonce="bad"),
               packet(generation=True), packet(extra=1),
               packet().replace(b'"version": 1', b'"version": 1, "version": 1')]
    for data in invalid:
        sock, process = launch(binary)
        try:
            sock.send(data)
            assert process.wait(timeout=5) == 4
        finally:
            sock.close()
            if process.poll() is None:
                process.kill()
            process.wait()
    print("helper handshake/status/close, unavailable diagnostics, malformed envelope gates passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
