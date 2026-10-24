"""Real capture/input/invalidation through the authenticated helper on private Xvfb.

Catches missing X11 selection/capture, XTest delivery, stale geometry admission,
missing release-on-close, and Wayland-to-XWayland fallback. No desktop is touched.
"""
import array
import ctypes as C
import fcntl
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time


class XButtonEvent(C.Structure):
    _fields_ = [('type', C.c_int), ('serial', C.c_ulong), ('send_event', C.c_int),
                ('display', C.c_void_p), ('window', C.c_ulong), ('root', C.c_ulong),
                ('subwindow', C.c_ulong), ('time', C.c_ulong), ('x', C.c_int),
                ('y', C.c_int), ('x_root', C.c_int), ('y_root', C.c_int),
                ('state', C.c_uint), ('button', C.c_uint), ('same_screen', C.c_int)]


def main(binary):
    if not shutil.which('Xvfb') or not shutil.which('xrandr'):
        return 77
    x = C.CDLL('libX11.so.6')
    x.XOpenDisplay.restype = C.c_void_p
    x.XOpenDisplay.argtypes = [C.c_char_p]
    for name in ('XDefaultRootWindow', 'XCreateSimpleWindow'):
        getattr(x, name).restype = C.c_ulong
    x.XDefaultRootWindow.argtypes = [C.c_void_p]
    x.XCreateSimpleWindow.argtypes = [C.c_void_p, C.c_ulong, C.c_int, C.c_int,
                                     C.c_uint, C.c_uint, C.c_uint, C.c_ulong, C.c_ulong]
    x.XSetWindowBackground.argtypes = [C.c_void_p, C.c_ulong, C.c_ulong]
    x.XClearWindow.argtypes = [C.c_void_p, C.c_ulong]
    x.XMapWindow.argtypes = [C.c_void_p, C.c_ulong]
    x.XSelectInput.argtypes = [C.c_void_p, C.c_ulong, C.c_long]
    x.XSync.argtypes = [C.c_void_p, C.c_int]
    x.XPending.argtypes = [C.c_void_p]
    x.XNextEvent.argtypes = [C.c_void_p, C.c_void_p]
    x.XCloseDisplay.argtypes = [C.c_void_p]
    x.XQueryPointer.argtypes = [C.c_void_p, C.c_ulong, C.POINTER(C.c_ulong),
        C.POINTER(C.c_ulong), C.POINTER(C.c_int), C.POINTER(C.c_int),
        C.POINTER(C.c_int), C.POINTER(C.c_int), C.POINTER(C.c_uint)]
    with tempfile.TemporaryDirectory(prefix='cu-x11-test-') as tmp:
        with open(tmp + '/display', 'w+b') as display_file:
            server = subprocess.Popen(['Xvfb', '-displayfd', str(display_file.fileno()),
                '-screen', '0', '128x96x24', '-nolisten', 'tcp', '-ac'],
                pass_fds=(display_file.fileno(),), stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    display_file.seek(0)
                    number = display_file.read().strip()
                    if number:
                        break
                    assert server.poll() is None
                    time.sleep(.01)
                assert number, 'Xvfb startup timeout'
                display = b':' + number
                d = x.XOpenDisplay(display)
                assert d
                root = x.XDefaultRootWindow(d)
                win = x.XCreateSimpleWindow(d, root, 0, 0, 128, 96, 0, 0, 0x123456)
                x.XSelectInput(d, win, (1 << 2) | (1 << 3))
                x.XMapWindow(d, win)
                x.XSync(d, False)
                env = {**os.environ, 'DISPLAY': display.decode(), 'XDG_SESSION_TYPE': 'x11',
                       'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/no-cu-test-bus'}
                env.pop('WAYLAND_DISPLAY', None)

                def launch(extra=None):
                    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                    parent.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
                    p = subprocess.Popen([binary, '--control-fd', str(child.fileno())],
                        pass_fds=(child.fileno(),), env={**env, **(extra or {})})
                    child.close()
                    parent.settimeout(5)
                    return parent, p

                def exchange(s, sequence, kind, payload=None):
                    s.send(json.dumps(dict(version=1, generation=1, sequence=sequence,
                        nonce='a' * 64, kind=kind, payload=payload or {})).encode())
                    data, ancillary, flags, _ = s.recvmsg(65536, socket.CMSG_SPACE(12) + socket.CMSG_SPACE(4 * 8))
                    assert not flags
                    fds = []
                    for level, t, b in ancillary:
                        if level == socket.SOL_SOCKET and t == socket.SCM_RIGHTS:
                            a = array.array('i'); a.frombytes(b[:len(b) - len(b) % a.itemsize]); fds.extend(a)
                    reply = json.loads(data)
                    assert reply['sequence'] == sequence
                    return reply, fds

                start = dict(binding_id='test-binding', owner_session_id='test-owner',
                    context_nonce='test-context', allow_input=True,
                    authorization_timeout_ms=100, startup_timeout_ms=1000, max_frame_bytes=128*96*3)
                s, helper = launch()
                try:
                    hello, _ = exchange(s, 1, 'hello')
                    assert hello['payload']['x11_code'] == 'X11_INTERFACES_SUPPORTED', hello
                    reply, _ = exchange(s, 2, 'start', start)
                    assert reply['kind'] == 'ready', reply
                    b = reply['payload']['binding']
                    assert b['backend'] == 'x11' and b['origin'] == [0, 0] and b['size'] == [128, 96]
                    assert b['mapping_evidence']['source'] == 'xrandr'
                    assert b['mapping_evidence']['root_id'] == root
                    assert b['mapping_evidence']['output_ids']
                    reply, fds = exchange(s, 3, 'freeze', dict(binding_id='test-binding', timeout_ms=1000))
                    assert reply['kind'] == 'frame' and len(fds) == 1, reply
                    frame = reply['payload']
                    try:
                        rgb = os.pread(fds[0], frame['byte_size'], 0)
                        assert rgb == bytes.fromhex('123456') * (128 * 96), (rgb[:30], len(rgb), set(rgb))
                        assert hashlib.sha256(rgb).hexdigest() == frame['sha256']
                        assert fcntl.fcntl(fds[0], getattr(fcntl, "F_GET_SEALS", 1034)) == 15
                    finally:
                        os.close(fds[0])
                    seq = 3
                    def event(payload):
                        nonlocal seq
                        seq += 1
                        return exchange(s, seq, 'send_event', dict(binding_id='test-binding',
                            geometry_version=1, deadline_monotonic=time.monotonic()+1, event=payload))[0]
                    assert event(dict(kind='motion', x=25, y=30))['kind'] == 'event'
                    r, child = C.c_ulong(), C.c_ulong()
                    rx, ry, wx, wy, mask = C.c_int(), C.c_int(), C.c_int(), C.c_int(), C.c_uint()
                    def pointer():
                        x.XQueryPointer(d, root, C.byref(r), C.byref(child), C.byref(rx), C.byref(ry),
                            C.byref(wx), C.byref(wy), C.byref(mask))
                        return rx.value, ry.value, mask.value
                    assert pointer()[:2] == (25, 30)
                    assert event(dict(kind='motion', x=128, y=0))['payload']['code'] == 'INVALID_EVENT'
                    assert pointer()[:2] == (25, 30)
                    assert event(dict(kind='button_down', button='left'))['kind'] == 'event'
                    assert pointer()[2] & (1 << 8)
                    assert event(dict(kind='release_cleanup', button='left'))['kind'] == 'event'
                    assert not pointer()[2] & (1 << 8)
                    assert event(dict(kind='wheel_step', units=1))['kind'] == 'event'
                    x.XSync(d, False)
                    buttons = []
                    buf = (C.c_long * 24)()
                    while x.XPending(d):
                        x.XNextEvent(d, C.byref(buf))
                        ev = C.cast(C.byref(buf), C.POINTER(XButtonEvent)).contents
                        if ev.type in (4, 5): buttons.append((ev.type, ev.button))
                    assert (4, 4) in buttons and (5, 4) in buttons, buttons
                    assert event(dict(kind='button_down', button='right'))['kind'] == 'event'
                    seq += 1
                    assert exchange(s, seq, 'close')[0]['kind'] == 'closed'
                    assert helper.wait(timeout=5) == 0
                    assert not pointer()[2] & (1 << 10), 'close must release held right button'
                finally:
                    s.close()
                    if helper.poll() is None: helper.kill()
                    helper.wait()
                # ProcessSupervisor cancellation sends SIGTERM instead of a close command.
                s, helper = launch()
                try:
                    exchange(s, 1, 'hello')
                    assert exchange(s, 2, 'start', start)[0]['kind'] == 'ready'
                    reply, _ = exchange(s, 3, 'send_event', dict(binding_id='test-binding', geometry_version=1,
                        deadline_monotonic=time.monotonic()+1, event=dict(kind='button_down', button='left')))
                    assert reply['kind'] == 'event' and pointer()[2] & (1 << 8)
                    helper.terminate()
                    helper.wait(timeout=5)
                    assert not pointer()[2] & (1 << 8), 'termination must release held left button'
                finally:
                    s.close()
                    if helper.poll() is None: helper.kill()
                    helper.wait()
                # A fresh binding must revoke once RandR's selected monitor identity changes.
                s, helper = launch()
                try:
                    exchange(s, 1, 'hello')
                    assert exchange(s, 2, 'start', start)[0]['kind'] == 'ready'
                    subprocess.run(['xrandr', '--setmonitor', 'changed', '64/10x96/10+0+0', 'screen'],
                        env=env, check=True, capture_output=True)
                    reply, _ = exchange(s, 3, 'send_event', dict(binding_id='test-binding', geometry_version=1,
                        deadline_monotonic=time.monotonic()+1, event=dict(kind='motion', x=12, y=12)))
                    assert reply['kind'] == 'error' and reply['payload']['code'] == 'X11_GEOMETRY_CHANGED', reply
                    exchange(s, 4, 'close')
                    assert helper.wait(timeout=5) == 0
                finally:
                    s.close()
                    if helper.poll() is None: helper.kill()
                    helper.wait()
                s, helper = launch({'XDG_SESSION_TYPE': 'wayland', 'WAYLAND_DISPLAY': 'wayland-test'})
                try:
                    hello, _ = exchange(s, 1, 'hello')
                    expected = hello['payload']['code']
                    assert expected in {'SESSION_BUS_UNAVAILABLE', 'GIO_PIPEWIRE_DEVELOPMENT_DEPENDENCIES_MISSING'}
                    reply, _ = exchange(s, 2, 'start', start)
                    assert reply['kind'] == 'error' and reply['payload']['code'] == expected, reply
                    exchange(s, 3, 'close')
                    helper.wait(timeout=5)
                finally:
                    s.close()
                    if helper.poll() is None: helper.kill()
                    helper.wait()
                x.XCloseDisplay(d)
            finally:
                server.terminate()
                server.wait(timeout=5)
    print('X11 production protocol: capture, sealed frame, motion, buttons, wheel, cleanup, geometry revocation, Wayland selection passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
