from types import SimpleNamespace

from scripts.probe_computer_use_input import _surface_visible


def test_input_probe_requires_its_own_visible_surface_at_dispatch_point():
    binding = SimpleNamespace(origin=(0, 0), size=(10, 10))
    frame = SimpleNamespace(width=10, height=10, rgb=bytes((22, 201, 76)) * 100)
    assert _surface_visible(frame, binding, 5, 5)
    assert not _surface_visible(frame, binding, 0, 0)
    frame.rgb = bytes((201, 22, 76)) * 100
    assert not _surface_visible(frame, binding, 5, 5)


def test_x11_input_probe_verifies_owned_surface_with_monitor_origin(monkeypatch):
    import copy
    import time
    from dataclasses import replace
    from backend.config import get_config
    from scripts import probe_computer_use_input as probe

    app = copy.deepcopy(get_config())
    app.agent.allow_desktop_capture = app.agent.allow_desktop_input = True
    app.computer_use = replace(app.computer_use, helper_binary="/tmp/helper")
    callbacks = {}
    geometries = []
    events = []
    binding = SimpleNamespace(backend="x11", ready=True, binding_id="b",
                              geometry_version=1, origin=(400, 100), size=(100, 100))

    class Root:
        def title(self, *_): pass
        def configure(self, **_): pass
        def overrideredirect(self, *_): pass
        def geometry(self, value): geometries.append(value)
        def update(self): pass
        def destroy(self): pass
        def attributes(self, *_): raise AssertionError("X11 must select its bound monitor")

    class Canvas:
        def __init__(self, *_args, **_kwargs): pass
        def pack(self, **_): pass
        def bind(self, sequence, callback): callbacks[sequence] = callback

    class Native:
        def __init__(self, **_): pass
        def start(self, *_args, **_kwargs): return binding
        def freeze(self, *_):
            return SimpleNamespace(width=100, height=100, rgb=bytes((22, 201, 76)) * 10000,
                                   captured_monotonic=time.monotonic())
        def send_event(self, _binding, _geometry, event, **_):
            events.append(event)
            sequence = {"button_down": "<ButtonPress-1>", "button_up": "<ButtonRelease-1>",
                        "wheel_step": "<Button-4>"}.get(event["kind"])
            if sequence:
                callbacks[sequence](None)
        def close(self): pass

    monkeypatch.setattr(probe.tk, "Tk", Root)
    monkeypatch.setattr(probe.tk, "Canvas", Canvas)
    monkeypatch.setattr(probe, "NativeClient", Native)
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    result = probe.run_probe(app, authorization_timeout=1, transport_timeout=1)
    assert result["input_outcome"] == "verified_in_test_window"
    assert geometries == ["100x100+400+100"]
    assert events[0] == {"kind": "motion", "x": 450, "y": 150}
    assert result["input_events_sent"] == 4
    assert result["portal_authorization"] == result["eis_mapping"] == "not_applicable"
    assert result["xtest_input"] == "verified_in_test_window"
    assert result["ready"] is False
