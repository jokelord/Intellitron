from __future__ import annotations

from io import BytesIO
import json
from types import SimpleNamespace

from PIL import Image, ImageDraw
import pytest

from scripts.probe_computer_use_skill import (
    ClickDeliveryError,
    ProbeError,
    _send_one_click,
    _expected_frame_target,
    _wait_for_test_click,
    add_retry_feedback,
    approve_click_point,
    crop_contains_target,
    detect_test_target,
    main,
    run_probe,
)


def test_live_skill_probe_uses_non_thinking_for_automated_measurements(monkeypatch):
    import scripts.probe_computer_use_skill as probe

    sent = {}

    def fake_urlopen(request, timeout):
        sent.update(json.loads(request.data))
        response = {'choices': [{'finish_reason': 'tool_calls', 'message': {'tool_calls': [
            {'function': {'name': 'inspect_region',
                          'arguments': '{"x":500,"y":500,"zoom":4}'}}
        ]}}]}
        return BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(probe.urllib.request, 'urlopen', fake_urlopen)
    encoded = SimpleNamespace(messages=({'role': 'user', 'content': [{'type': 'text', 'text': 'overview'}]},),
                              tools=({'name': 'inspect_region', 'parameters': {'type': 'object'}},))
    probe.infer('http://127.0.0.1:30000', 'fixture-model', encoded, 'offline overview', timeout=5)

    assert sent['chat_template_kwargs'] == {'enable_thinking': False}
    assert sent['max_tokens'] == 2048
    assert 'max_new_tokens' not in sent


def test_probe_surface_must_show_green_fullscreen_and_magenta_target():
    image = Image.new("RGB", (100, 100), "#16c94c")
    ImageDraw.Draw(image).rectangle((44, 44, 55, 55), fill="#ff00ff")
    frame = type("Frame", (), {"width": 100, "height": 100, "rgb": image.tobytes()})()

    assert detect_test_target(frame, expected_bounds=(44, 44, 56, 56)) == (44, 44, 56, 56)

    off_center_spoof = Image.new("RGB", (100, 100), "#16c94c")
    ImageDraw.Draw(off_center_spoof).rectangle((12, 12, 23, 23), fill="#ff00ff")
    frame.rgb = off_center_spoof.tobytes()
    with pytest.raises(ProbeError, match="TEST_TARGET_NOT_VISIBLE") as absent:
        detect_test_target(frame, expected_bounds=(44, 44, 56, 56))
    assert absent.value.diagnostics["unrelated_magenta_bounds_px"] == [12, 12, 24, 24]

    missing_target = Image.new("RGB", (100, 100), "#16c94c")
    frame.rgb = missing_target.tobytes()
    with pytest.raises(ProbeError, match="TEST_TARGET_NOT_VISIBLE"):
        detect_test_target(frame, expected_bounds=(44, 44, 56, 56))

    non_fullscreen = Image.new("RGB", (100, 100), "#101010")
    ImageDraw.Draw(non_fullscreen).rectangle((44, 44, 55, 55), fill="#ff00ff")
    frame.rgb = non_fullscreen.tobytes()
    with pytest.raises(ProbeError, match="TEST_SURFACE_NOT_VISIBLE"):
        detect_test_target(frame, expected_bounds=(44, 44, 56, 56))

    partial_surface = Image.new("RGB", (100, 100), "#101010")
    draw = ImageDraw.Draw(partial_surface)
    draw.rectangle((0, 0, 99, 12), fill="#16c94c")
    draw.rectangle((0, 87, 99, 99), fill="#16c94c")
    draw.rectangle((44, 44, 55, 55), fill="#ff00ff")
    frame.rgb = partial_surface.tobytes()
    with pytest.raises(ProbeError, match="TEST_SURFACE_NOT_VISIBLE"):
        detect_test_target(frame, expected_bounds=(44, 44, 56, 56))


def test_windowed_target_requires_captured_green_panel_not_green_desktop():
    image = Image.new("RGB", (1280, 720), "#101010")
    draw = ImageDraw.Draw(image)
    panel = (80, 80, 1200, 640)
    target = (620, 340, 660, 380)
    draw.rectangle((80, 80, 1199, 639), fill="#16c94c")
    draw.rectangle((620, 340, 659, 379), fill="#ff00ff")
    frame = type("Frame", (), {"width": 1280, "height": 720, "rgb": image.tobytes()})()
    assert detect_test_target(frame, expected_bounds=target, panel_bounds=panel) == target

    # A matching magenta mark and small green patch cannot impersonate the Tk panel.
    image = Image.new("RGB", (1280, 720), "#101010")
    draw = ImageDraw.Draw(image)
    draw.rectangle((590, 310, 689, 409), fill="#16c94c")
    draw.rectangle((620, 340, 659, 379), fill="#ff00ff")
    frame.rgb = image.tobytes()
    with pytest.raises(ProbeError, match="TEST_SURFACE_NOT_VISIBLE"):
        detect_test_target(frame, expected_bounds=target, panel_bounds=panel)

    # Keep the predicted outline while occluding most of its interior.
    draw.rectangle((624, 344, 655, 375), fill="#101010")
    frame.rgb = image.tobytes()
    with pytest.raises(ProbeError, match="TEST_TARGET_NOT_VISIBLE"):
        detect_test_target(frame, expected_bounds=target, panel_bounds=panel)


def test_windowed_target_mapping_uses_actual_canvas_origin(binding):
    from dataclasses import replace
    from scripts.probe_computer_use_skill import _detect_bound_target

    selected = replace(binding, origin=(0, 0), size=(1280, 720),
        mapping_evidence={"full_monitor": True, "mapping_id": "selected-monitor"})
    image = Image.new("RGB", (1280, 720), "#101010")
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 80, 1199, 639), fill="#16c94c")
    draw.rectangle((620, 340, 659, 379), fill="#ff00ff")
    frame = type("Frame", (), {"width": 1280, "height": 720, "rgb": image.tobytes()})()
    geometry = {"canvas_origin": (80, 80), "canvas_size": (1120, 560),
                "target_bounds": (540, 260, 580, 300), "windowed": True}
    assert _detect_bound_target(frame, selected, geometry) == (620, 340, 660, 380)
    geometry["canvas_origin"] = (200, 80)
    with pytest.raises(ProbeError, match="TEST_SURFACE_GEOMETRY_MISMATCH"):
        _detect_bound_target(frame, selected, geometry)


def test_new_probe_surface_is_windowed_and_records_actual_canvas_origin(monkeypatch):
    from types import SimpleNamespace
    from scripts import probe_computer_use_skill as probe

    state = {"attributes": [], "geometry": None, "rectangles": []}

    class Root:
        def title(self, _value):
            pass

        def configure(self, **_kwargs):
            pass

        def attributes(self, *args):
            state["attributes"].append(args)

        def geometry(self, value):
            state["geometry"] = value

        def update_idletasks(self):
            pass

        def update(self):
            pass

        def focus_force(self):
            pass

        def lift(self):
            pass

    class Canvas:
        def __init__(self, _root, **_kwargs):
            pass

        def pack(self, **_kwargs):
            pass

        def winfo_width(self):
            return 1120

        def winfo_height(self):
            return 560

        def winfo_rootx(self):
            return 80

        def winfo_rooty(self):
            return 80

        def create_rectangle(self, *bounds, **_kwargs):
            state["rectangles"].append(bounds)
            return len(state["rectangles"]) - 1

        def coords(self, index):
            return state["rectangles"][index]

        def bind(self, *_args):
            pass

    monkeypatch.setattr(probe.tk, "Tk", Root)
    monkeypatch.setattr(probe.tk, "Canvas", Canvas)
    _root, _canvas, _counts, geometry = probe._new_surface(
        SimpleNamespace(origin=(0, 0), size=(1280, 720)), target_side_px=40)
    assert state["geometry"] == "1120x560+80+80"
    assert ("-fullscreen", True) not in state["attributes"]
    assert geometry["windowed"] is True
    assert geometry["canvas_origin"] == (80, 80)
    assert geometry["target_bounds"] == (540, 260, 580, 300)

def test_target_expected_bounds_follow_full_monitor_physical_scale(binding):
    frame = type("Frame", (), {"width": 3840, "height": 2400})()
    scaled_binding = __import__("dataclasses").replace(
        binding, size=(1920, 1200),
        mapping_evidence={"full_monitor": True, "mapping_id": "selected-monitor"})
    geometry = {"canvas_size": (1920, 1200), "target_bounds": (932, 572, 988, 628)}
    expected, tolerance = _expected_frame_target(frame, scaled_binding, geometry)
    assert expected == (1864, 1144, 1976, 1256)
    assert tolerance == 2


def test_test_click_verification_waits_for_both_down_and_up_callbacks():
    counts = {"inside_down": 0, "inside_up": 0, "outside_down": 0}

    class DelayedRoot:
        updates = 0

        def update(self):
            self.updates += 1
            if self.updates == 1:
                counts["inside_down"] += 1
            elif self.updates == 2:
                counts["inside_up"] += 1

    root = DelayedRoot()
    verified, deltas = _wait_for_test_click(
        root, counts, {"inside_down": 0, "inside_up": 0, "outside_down": 0},
        deadline=__import__("time").monotonic() + 1)
    assert verified is True
    assert deltas == {"inside_down": 1, "inside_up": 1, "outside_down": 0}
    assert root.updates == 2


def test_click_gate_requires_crop_containment_and_model_point_inside_target(
    frame, crop, grounding_request, binding,
):
    from backend.computer_use.contracts import Action, InteractDetail

    source = frame(width=1000, height=1000)
    request = grounding_request(protocol=grounding_request().protocol)
    crop_record = crop(
        source_frame=source,
        grounding_request=request,
        x0=300,
        y0=300,
        x1=700,
        y1=700,
        zoom=4,
    )
    target_bounds = (490, 490, 511, 511)
    decision = InteractDetail(crop_record.crop_id, 500, 500, Action.LEFT_CLICK)

    assert crop_contains_target(crop_record, target_bounds, margin_px=8)
    point = approve_click_point(decision, crop_record, target_bounds, binding, 1000, 1000)
    assert point is not None
    assert point[0] == pytest.approx(-960.0)
    assert point[1] == pytest.approx(540.0)

    clipped_crop = crop(
        source_frame=source,
        grounding_request=request,
        x0=500,
        y0=300,
        x1=700,
        y1=700,
        zoom=4,
    )
    assert not crop_contains_target(clipped_crop, target_bounds, margin_px=8)
    assert approve_click_point(decision, clipped_crop, target_bounds, binding, 1000, 1000) is None

    outside_target = InteractDetail(crop_record.crop_id, 800, 500, Action.LEFT_CLICK)
    assert approve_click_point(outside_target, crop_record, target_bounds, binding, 1000, 1000) is None


def test_retry_feedback_omits_prior_coordinates_and_crop(grounding_request):
    from backend.computer_use.contracts import ImageArtifact
    from backend.computer_use.model_adapter import describe_stage, encode_stage

    request = grounding_request(protocol=grounding_request().protocol)
    image = ImageArtifact(b"png", 1000, 1000, "hash", "renderer")
    encoded = encode_stage(describe_stage("stage1", image, None, request), image)
    retry = add_retry_feedback(encoded, rough_xy=(123, 456), crop_rect=(0, 156, 248, 444),
                               source_size=(1000, 600), reobserve_reason="target_ambiguous")
    text = retry.messages[0]["content"][0]["text"].lower()
    assert "x=123" not in text and "y=456" not in text
    assert "covering normalized" not in text
    assert "stage2 requested observe() for a fresh overview" in text
    assert "did not yield an approved final point" in text
    assert "only the current visible overview" in text
    assert "target is near" not in text


def test_click_send_failure_preserves_acknowledged_and_attempted_counts(binding):
    class FakeNative:
        def __init__(self):
            self.events = []

        def send_event(self, binding_id, geometry_version, event, *, deadline_monotonic):
            self.events.append(event["kind"])
            if event["kind"] == "button_down":
                raise RuntimeError("ack lost")

    native = FakeNative()
    with pytest.raises(ClickDeliveryError) as exc_info:
        _send_one_click(native, binding, (10, 20))
    assert native.events == ["motion", "button_down", "release_cleanup"]
    assert exc_info.value.sent_events == 2
    assert exc_info.value.attempted_events == 3
    assert exc_info.value.button_down_attempted is True


def _install_fake_live_probe(monkeypatch, tmp_path, *, backend="wayland"):
    from types import SimpleNamespace
    from backend.computer_use.config import ComputerUseConfig
    from backend.computer_use.contracts import CanonicalFrame, InputBinding

    binding = InputBinding("b1", "session-1", 1, backend, (0, 0), (1280, 720),
        1, "device", "region", {"full_monitor": True, "mapping_id": "region"}, True)
    rgb_image = Image.new("RGB", (1280, 720), "#101010")
    ImageDraw.Draw(rgb_image).rectangle((80, 80, 1199, 639), fill="#16c94c")
    ImageDraw.Draw(rgb_image).rectangle((620, 340, 659, 379), fill="#ff00ff")
    state = {"sequence": 0, "events": [], "stage_calls": [], "freezes": 0,
             "binding_checks": 0, "counts": {"inside_down": 0, "inside_up": 0, "outside_down": 0},
             "pointer": (0, 0), "lock_observations": [], "surface_refreshes": [],
             "required_refreshes_for_final_frame": 0, "canvas_origin": (80, 80),
             "frame_image": rgb_image}

    def new_frame():
        state["sequence"] += 1
        stamp = __import__("time").monotonic()
        return CanonicalFrame(f"f{state['sequence']}", f"o{state['sequence']}", binding,
            1280, 720, state["frame_image"].tobytes(), 1, state["sequence"], stamp, stamp + .001,
            True, {"full_monitor": True}, {"identity": True})

    class FakeNative:
        def __init__(self, **kwargs):
            self.current = new_frame()

        def start(self, *_args, **_kwargs):
            return binding

        def freeze(self, _binding_id):
            state["freezes"] += 1
            if state["freezes"] >= 2 and state["required_refreshes_for_final_frame"]:
                import time
                end = time.monotonic() + 2
                while (len(state["surface_refreshes"]) < state["required_refreshes_for_final_frame"]
                       and time.monotonic() < end):
                    time.sleep(0.01)
                if len(state["surface_refreshes"]) < state["required_refreshes_for_final_frame"]:
                    raise TimeoutError("test surface stopped pumping")
            from backend.computer_use.dispatch import _SEAT_LOCK
            state["lock_observations"].append(("freeze", _SEAT_LOCK.locked()))
            self.current = new_frame()
            return self.current

        def resolve_binding(self):
            state["binding_checks"] += 1
            from backend.computer_use.dispatch import _SEAT_LOCK
            state["lock_observations"].append(("resolve_binding", _SEAT_LOCK.locked()))
            return binding

        def send_event(self, _binding_id, _geometry_version, event, **_kwargs):
            from backend.computer_use.dispatch import _SEAT_LOCK
            state["lock_observations"].append((event["kind"], _SEAT_LOCK.locked()))
            state["events"].append(event["kind"])
            if event["kind"] == "motion":
                state["pointer"] = (event["x"], event["y"])
            elif event["kind"] == "button_down":
                x, y = state["pointer"]
                state["counts"]["inside_down" if 620 <= x < 660 and 340 <= y < 380
                                else "outside_down"] += 1
            elif event["kind"] == "button_up":
                x, y = state["pointer"]
                if 620 <= x < 660 and 340 <= y < 380:
                    state["counts"]["inside_up"] += 1

        def close(self):
            pass

    class FakeSupervisor:
        def __init__(self, **_kwargs):
            pass

        def shutdown(self):
            pass

    class FakePolicy:
        def __init__(self, **_kwargs):
            pass

    class FakeRuntime:
        command_mode = "allowlist"
        allowed_executables = ()
        command_cwd_allowlist = ()
        command_blacklist = ()
        max_exec_timeout_seconds = 1
        max_output_bytes = 1024
        termination_grace_seconds = .1

    class FakeCapability:
        def command_runtime_config(self):
            return FakeRuntime()

    class FakeRoot:
        def attributes(self, *_args):
            state["windowed"] = True

        def geometry(self, _value):
            state["window_geometry_set"] = True

        def update(self):
            pass

        def update_idletasks(self):
            pass

        def destroy(self):
            pass

    class FakeCanvas:
        def configure(self, **kwargs):
            state["surface_refreshes"].append(kwargs)

        def winfo_rootx(self):
            return state["canvas_origin"][0]

        def winfo_rooty(self):
            return state["canvas_origin"][1]

        def winfo_width(self):
            return 1120

        def winfo_height(self):
            return 560

    app = SimpleNamespace(
        agent=SimpleNamespace(allow_desktop_capture=False, allow_desktop_input=False),
        computer_use=ComputerUseConfig(enabled=False, grid_enabled=True, grid_divisions=16),
        model=SimpleNamespace(),
    )
    monkeypatch.setattr("scripts.probe_computer_use_skill.DIAGRAMS_DIR", tmp_path)
    monkeypatch.setattr("scripts.probe_computer_use_skill.deployment_fingerprint", lambda _app: "fp")
    monkeypatch.setattr("scripts.probe_computer_use_skill.model_id", lambda _api, *, timeout: "local-model")
    monkeypatch.setattr("scripts.probe_computer_use_skill.NativeClient", FakeNative)
    monkeypatch.setattr("scripts.probe_computer_use_skill.ProcessSupervisor", FakeSupervisor)
    monkeypatch.setattr("scripts.probe_computer_use_skill.CommandPolicy", FakePolicy)
    monkeypatch.setattr("scripts.probe_computer_use_skill.EnvironmentBuilder", lambda _runtime: object())
    monkeypatch.setattr("scripts.probe_computer_use_skill.build_session_capability", lambda *_args, **_kwargs: FakeCapability())
    monkeypatch.setattr("scripts.probe_computer_use_skill.require_capability", lambda *_args: None)
    surface_geometry = {"canvas_origin": (80, 80), "canvas_size": (1120, 560),
                        "target_bounds": (540, 260, 580, 300), "windowed": True}
    state["windowed"] = True
    monkeypatch.setattr("scripts.probe_computer_use_skill._new_surface",
                        lambda *_args, **_kwargs: (FakeRoot(), FakeCanvas(), state["counts"], surface_geometry))
    monkeypatch.setattr("scripts.probe_computer_use_skill._capture_visible_target",
                        lambda native, _binding, _root, _surface, **_kwargs:
                        (native.freeze("b1"), (620, 340, 660, 380)))

    return state, app


def _run_fake_probe(app, monkeypatch, *, max_reobserves=0, overall_timeout_seconds=300,
                    seat_lock_timeout_seconds=3):
    return run_probe(app, helper_binary="/tmp/helper", api="http://127.0.0.1:30000",
        authorization_timeout=1, transport_timeout=1, max_reobserves=max_reobserves,
        overall_timeout_seconds=overall_timeout_seconds,
        seat_lock_timeout_seconds=seat_lock_timeout_seconds)


def test_freeze_pumps_surface_while_waiting_for_new_frame():
    import time
    from scripts.probe_computer_use_skill import _freeze_with_surface_refresh

    refreshes = []

    class WaitingNative:
        timeout = 1.0

        def freeze(self, _binding_id):
            end = time.monotonic() + 1.0
            while len(refreshes) < 2 and time.monotonic() < end:
                time.sleep(0.005)
            if len(refreshes) < 2:
                raise TimeoutError("surface did not refresh during freeze")
            return "new-frame"

    class Surface:
        def update(self):
            pass

    assert _freeze_with_surface_refresh(
        WaitingNative(), "b1", Surface(), lambda: refreshes.append(True),
        deadline=time.monotonic() + 2.0, phase="visible_test_target",
        refresh_interval=0.01,
    ) == "new-frame"
    assert len(refreshes) >= 2


def test_freeze_discards_pre_refresh_frame_and_waits_for_fresh_one():
    import time
    from types import SimpleNamespace
    from scripts.probe_computer_use_skill import _freeze_with_surface_refresh

    boundary = time.monotonic()

    class Native:
        timeout = 1.0
        calls = 0

        def freeze(self, _binding_id):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(sequence=2, captured_monotonic=boundary - 1)
            return SimpleNamespace(sequence=3, captured_monotonic=time.monotonic())

    class Surface:
        def update(self):
            pass

    native = Native()
    frame = _freeze_with_surface_refresh(native, "b1", Surface(), lambda: None,
        deadline=time.monotonic() + 1, phase="test", refresh_interval=0.01,
        captured_after=boundary, sequence_after=1)
    assert frame.sequence == 3
    assert native.calls == 2


def test_freeze_preserves_native_failure_at_local_deadline():
    import time
    from scripts.probe_computer_use_skill import _freeze_with_surface_refresh

    class Native:
        timeout = 0.1

        def freeze(self, _binding_id):
            time.sleep(0.03)
            raise RuntimeError("PIPEWIRE_NO_PROCESS_CALLBACK")

    class Surface:
        def update(self):
            pass

    with pytest.raises(RuntimeError, match="PIPEWIRE_NO_PROCESS_CALLBACK"):
        _freeze_with_surface_refresh(Native(), "b1", Surface(), lambda: None,
            deadline=time.monotonic() + 0.01, phase="visible_test_target",
            refresh_interval=0.01)


def test_refresh_surface_moves_visible_marker_without_touching_target():
    from scripts.probe_computer_use_skill import _refresh_test_surface

    class Canvas:
        def __init__(self):
            self.backgrounds = []
            self.positions = []

        def configure(self, **kwargs):
            self.backgrounds.append(kwargs["bg"])

        def coords(self, marker, *bounds):
            assert marker == "marker"
            self.positions.append(bounds)

    canvas = Canvas()
    _refresh_test_surface(canvas, "marker", 1)
    _refresh_test_surface(canvas, "marker", 2)
    assert canvas.backgrounds == ["#00f020", "#16c94c"]
    assert canvas.positions == [(24, 24, 120, 120), (224, 24, 320, 120)]


def test_idle_liveness_diagnostic_never_calls_model_or_sends_input(tmp_path, monkeypatch):
    state, app = _install_fake_live_probe(monkeypatch, tmp_path)

    def forbid_infer(*_args, **_kwargs):
        raise AssertionError("idle diagnostic must not call the model")

    monkeypatch.setattr("scripts.probe_computer_use_skill.infer", forbid_infer)
    result = run_probe(app, helper_binary="/tmp/helper", api="http://127.0.0.1:30000",
        authorization_timeout=1, transport_timeout=1, idle_diagnostic_seconds=0.01)
    assert result["idle_frame_liveness"] == "verified"
    assert result["input_events_sent"] == 0
    assert result["attempts"] == []
    assert state["events"] == []
    assert state["freezes"] >= 2


def test_windowed_liveness_checks_two_new_frames_without_input(tmp_path, monkeypatch):
    state, app = _install_fake_live_probe(monkeypatch, tmp_path)

    def forbid_infer(*_args, **_kwargs):
        raise AssertionError("windowed diagnostic must not call the model")

    monkeypatch.setattr("scripts.probe_computer_use_skill.infer", forbid_infer)
    result = run_probe(app, helper_binary="/tmp/helper", api="http://127.0.0.1:30000",
        authorization_timeout=1, transport_timeout=1, idle_diagnostic_seconds=0,
        windowed_diagnostic=True)
    assert result["windowed_frame_liveness"] == "verified"
    assert result["after_second_sequence"] > result["after_first_sequence"] > result["before_sequence"]
    assert state["windowed"] is True
    assert state["events"] == []


def test_absent_target_crop_still_runs_stage2_and_never_sends_input(tmp_path, monkeypatch):
    state, app = _install_fake_live_probe(monkeypatch, tmp_path)

    def fake_infer(_api, _model, encoded, _description, *, timeout):
        state["stage_calls"].append(encoded.instance.stage)
        raw = ({"name": "inspect_region", "arguments": {"x": 50, "y": 50, "zoom": 4}}
               if encoded.instance.stage == "stage1" else
               {"name": "observe", "arguments": {}})
        return raw, {"finish_reason": "tool_calls", "tool_call": raw}

    monkeypatch.setattr("scripts.probe_computer_use_skill.infer", fake_infer)
    result = _run_fake_probe(app, monkeypatch)
    assert state["stage_calls"] == ["stage1", "stage2"]
    assert state["events"] == []
    assert result["input_events_sent"] == 0
    assert result["input_outcome"] == "not_run"
    assert result["eis_mapping"] == "mapped"
    assert result["attempts"][0]["stage1"]["target_inside_crop"] is False
    assert result["attempts"][0]["stage2"]["status"] == "reobserve"


def test_absent_target_crop_requires_model_reobserve_without_backend_repoint(tmp_path, monkeypatch):
    state, app = _install_fake_live_probe(monkeypatch, tmp_path)

    def fake_infer(_api, _model, encoded, _description, *, timeout):
        state["stage_calls"].append(encoded.instance.stage)
        raw = ({"name": "inspect_region", "arguments": {"x": 50, "y": 50, "zoom": 4}}
               if encoded.instance.stage == "stage1" else
               {"name": "interact_detail", "arguments": {"x": 500, "y": 500, "action": "left_click"}})
        return raw, {"finish_reason": "tool_calls", "tool_call": raw}

    monkeypatch.setattr("scripts.probe_computer_use_skill.infer", fake_infer)
    result = _run_fake_probe(app, monkeypatch, max_reobserves=2)
    assert state["stage_calls"] == ["stage1", "stage2"]
    assert state["events"] == []
    assert result["code"] == "MODEL_FAILED_TO_REOBSERVE_ABSENT_TARGET"
    assert result["attempts"][0]["stage2"]["status"] == "target_absent_from_crop_model_point_ignored"


def test_only_explicit_model_reobserve_triggers_bounded_retry(tmp_path, monkeypatch):
    state, app = _install_fake_live_probe(monkeypatch, tmp_path)
    prompts = []

    def fake_infer(_api, _model, encoded, _description, *, timeout):
        stage = encoded.instance.stage
        state["stage_calls"].append(stage)
        prompts.append(encoded.messages[0]["content"][0]["text"])
        if stage == "stage1":
            rough = 50 if state["stage_calls"].count("stage1") == 1 else 500
            raw = {"name": "inspect_region", "arguments": {"x": rough, "y": rough, "zoom": 4}}
        elif state["stage_calls"].count("stage2") == 1:
            raw = {"name": "observe", "arguments": {}}
        else:
            raw = {"name": "interact_detail", "arguments": {"x": 500, "y": 500, "action": "left_click"}}
        return raw, {"finish_reason": "tool_calls", "tool_call": raw}

    monkeypatch.setattr("scripts.probe_computer_use_skill.infer", fake_infer)
    result = _run_fake_probe(app, monkeypatch, max_reobserves=1)
    assert state["stage_calls"] == ["stage1", "stage2", "stage1", "stage2"]
    assert "stage2 requested observe() for a fresh overview" in prompts[2].lower()
    assert result["input_outcome"] == "verified_in_test_target"
    assert result["eis_mapping"] == "verified_in_test_window"
    assert state["events"] == ["motion", "button_down", "button_up"]
    assert state["surface_refreshes"] == [{"bg": "#00f020"}, {"bg": "#16c94c"}]
    assert state["lock_observations"][-4:] == [
        ("freeze", True), ("motion", True), ("button_down", True), ("button_up", True),
    ]  # final frame revalidation and the complete transaction hold the shared seat lock
    assert all(locked for kind, locked in state["lock_observations"] if kind in {"motion", "button_down", "button_up"})


def test_stage2_point_outside_visible_target_fails_without_repoint(tmp_path, monkeypatch):
    state, app = _install_fake_live_probe(monkeypatch, tmp_path)

    def fake_infer(_api, _model, encoded, _description, *, timeout):
        state["stage_calls"].append(encoded.instance.stage)
        if encoded.instance.stage == "stage1":
            raw = {"name": "inspect_region", "arguments": {"x": 500, "y": 500, "zoom": 4}}
        else:
            raw = {"name": "interact_detail", "arguments": {"x": 900, "y": 500, "action": "left_click"}}
        return raw, {"finish_reason": "tool_calls", "tool_call": raw}

    monkeypatch.setattr("scripts.probe_computer_use_skill.infer", fake_infer)
    result = _run_fake_probe(app, monkeypatch, max_reobserves=2)
    assert state["stage_calls"] == ["stage1", "stage2"]
    assert state["events"] == []
    assert result["code"] == "MODEL_POINT_OUTSIDE_TEST_TARGET"
    assert result["input_events_sent"] == 0
    stage1 = result["attempts"][0]["stage1"]
    stage2 = result["attempts"][0]["stage2"]
    x0, y0, x1, y1 = stage1["source_crop"]
    assert stage2["model_point_source_px"] == pytest.approx([
        x0 + .9 * (x1 - x0), y0 + .5 * (y1 - y0)])
    assert stage2["model_point_detail_image_px"] == pytest.approx([.9 * 1024, .5 * 1024])
    assert stage2["target_outer_bounds_detail_normalized"] == pytest.approx([
        (620 - x0) * 1000 / (x1 - x0), (340 - y0) * 1000 / (y1 - y0),
        (660 - x0) * 1000 / (x1 - x0), (380 - y0) * 1000 / (y1 - y0)])
    assert stage2["target_inner_bounds_detail_normalized"] == pytest.approx([
        (626 - x0) * 1000 / (x1 - x0), (346 - y0) * 1000 / (y1 - y0),
        (654 - x0) * 1000 / (x1 - x0), (374 - y0) * 1000 / (y1 - y0)])
    assert stage2["target_inner_bounds_source_px"] == [626, 346, 654, 374]
    assert stage2["target_inner_bounds_detail"] == stage2["target_outer_bounds_detail_image_px"]


def test_visible_edge_point_outside_inset_reports_why_no_click(tmp_path, monkeypatch):
    state, app = _install_fake_live_probe(monkeypatch, tmp_path)

    def fake_infer(_api, _model, encoded, _description, *, timeout):
        raw = ({"name": "inspect_region", "arguments": {"x": 500, "y": 500, "zoom": 4}}
               if encoded.instance.stage == "stage1" else
               {"name": "interact_detail", "arguments": {"x": 444, "y": 500, "action": "left_click"}})
        return raw, {"finish_reason": "tool_calls", "tool_call": raw}

    monkeypatch.setattr("scripts.probe_computer_use_skill.infer", fake_infer)
    result = _run_fake_probe(app, monkeypatch)
    stage2 = result["attempts"][0]["stage2"]
    point_x = stage2["model_point_source_px"][0]
    assert 620 <= point_x < stage2["target_inner_bounds_source_px"][0]
    assert stage2["point_inside_target"] is False
    assert result["code"] == "MODEL_POINT_OUTSIDE_TEST_TARGET"
    assert state["events"] == []


@pytest.mark.parametrize("required_refreshes", [0, 2])
def test_success_path_revalidates_and_clicks_only_the_test_target_once(tmp_path, monkeypatch, required_refreshes):
    import base64
    from io import BytesIO
    from PIL import Image

    state, app = _install_fake_live_probe(monkeypatch, tmp_path)
    state["required_refreshes_for_final_frame"] = required_refreshes

    def fake_infer(_api, _model, encoded, _description, *, timeout):
        stage = encoded.instance.stage
        state["stage_calls"].append(stage)
        prompt = encoded.messages[0]["content"][0]["text"].lower()
        assert "magenta target" in prompt and "magenta square" not in prompt
        payload = encoded.messages[0]["content"][1]["image_url"]["url"]
        image = Image.open(BytesIO(base64.b64decode(payload.split(",", 1)[1])))
        assert image.size == (app.computer_use.model_square_side_px,) * 2
        if stage == "stage1":
            raw = {"name": "inspect_region", "arguments": {"x": 500, "y": 500, "zoom": 4}}
        else:
            raw = {"name": "interact_detail", "arguments": {"x": 500, "y": 500, "action": "left_click"}}
        return raw, {"finish_reason": "tool_calls", "tool_call": raw}

    monkeypatch.setattr("scripts.probe_computer_use_skill.infer", fake_infer)
    result = _run_fake_probe(app, monkeypatch)
    assert state["stage_calls"] == ["stage1", "stage2"]
    assert state["binding_checks"] >= 2
    assert state["freezes"] >= 2
    assert state["events"] == ["motion", "button_down", "button_up"]
    assert state["counts"] == {"inside_down": 1, "inside_up": 1, "outside_down": 0}
    assert state["surface_refreshes"][:1] == [{"bg": "#00f020"}]
    assert len(state["surface_refreshes"]) >= max(1, required_refreshes)
    assert result["input_events_sent"] == 3
    assert result["input_outcome"] == "verified_in_test_target"
    assert result["attempts"][0]["stage1"]["target_inside_crop"] is True
    assert result["attempts"][0]["stage2"]["point_inside_target"] is True


def test_moved_window_after_model_blocks_input(tmp_path, monkeypatch):
    state, app = _install_fake_live_probe(monkeypatch, tmp_path)

    def fake_infer(_api, _model, encoded, _description, *, timeout):
        if encoded.instance.stage == "stage1":
            raw = {"name": "inspect_region", "arguments": {"x": 500, "y": 500, "zoom": 4}}
        else:
            state["canvas_origin"] = (81, 80)
            raw = {"name": "interact_detail", "arguments": {"x": 500, "y": 500, "action": "left_click"}}
        return raw, {"finish_reason": "tool_calls", "tool_call": raw}

    monkeypatch.setattr("scripts.probe_computer_use_skill.infer", fake_infer)
    result = _run_fake_probe(app, monkeypatch)
    assert result["code"] == "TEST_SURFACE_GEOMETRY_CHANGED"
    assert result["input_events_sent"] == 0
    assert state["events"] == []


def test_occluded_window_target_after_model_blocks_input(tmp_path, monkeypatch):
    state, app = _install_fake_live_probe(monkeypatch, tmp_path)

    def fake_infer(_api, _model, encoded, _description, *, timeout):
        if encoded.instance.stage == "stage1":
            raw = {"name": "inspect_region", "arguments": {"x": 500, "y": 500, "zoom": 4}}
        else:
            obscured = state["frame_image"].copy()
            ImageDraw.Draw(obscured).rectangle((625, 345, 654, 374), fill="#101010")
            state["frame_image"] = obscured
            raw = {"name": "interact_detail", "arguments": {"x": 500, "y": 500, "action": "left_click"}}
        return raw, {"finish_reason": "tool_calls", "tool_call": raw}

    monkeypatch.setattr("scripts.probe_computer_use_skill.infer", fake_infer)
    result = _run_fake_probe(app, monkeypatch)
    assert result["code"] == "TEST_TARGET_NOT_VISIBLE"
    assert result["input_events_sent"] == 0
    assert state["events"] == []


def test_live_probe_requires_production_desktop_authority_to_remain_disabled(tmp_path, monkeypatch):
    _state, app = _install_fake_live_probe(monkeypatch, tmp_path)
    app.agent.allow_desktop_input = True
    result = _run_fake_probe(app, monkeypatch)
    assert result["code"] == "PROBE_REQUIRES_PRODUCTION_DISABLED"
    assert result["input_events_sent"] == 0


def test_live_probe_fails_closed_when_overall_deadline_expires(tmp_path, monkeypatch):
    state, app = _install_fake_live_probe(monkeypatch, tmp_path)

    def slow_infer(_api, _model, encoded, _description, *, timeout):
        import time
        time.sleep(0.02)
        state["stage_calls"].append(encoded.instance.stage)
        raw = {"name": "inspect_region", "arguments": {"x": 500, "y": 500, "zoom": 4}}
        return raw, {"finish_reason": "tool_calls", "tool_call": raw}

    monkeypatch.setattr("scripts.probe_computer_use_skill.infer", slow_infer)
    result = _run_fake_probe(app, monkeypatch, overall_timeout_seconds=0.01)
    assert result["code"] == "PROBE_INFERENCE_DEADLINE_EXCEEDED"
    assert state["events"] == []
    assert result["input_events_sent"] == 0


def test_live_probe_fails_closed_if_shared_seat_lock_is_busy(tmp_path, monkeypatch):
    from backend.computer_use.dispatch import _SEAT_LOCK

    state, app = _install_fake_live_probe(monkeypatch, tmp_path)

    def fake_infer(_api, _model, encoded, _description, *, timeout):
        state["stage_calls"].append(encoded.instance.stage)
        raw = ({"name": "inspect_region", "arguments": {"x": 500, "y": 500, "zoom": 4}}
               if encoded.instance.stage == "stage1" else
               {"name": "interact_detail", "arguments": {"x": 500, "y": 500, "action": "left_click"}})
        return raw, {"finish_reason": "tool_calls", "tool_call": raw}

    monkeypatch.setattr("scripts.probe_computer_use_skill.infer", fake_infer)
    assert _SEAT_LOCK.acquire(blocking=False)
    try:
        result = _run_fake_probe(app, monkeypatch, seat_lock_timeout_seconds=0.01)
    finally:
        _SEAT_LOCK.release()
    assert result["code"] == "INPUT_SEAT_BUSY"
    assert state["events"] == []
    assert result["input_events_sent"] == 0


def test_malformed_stage2_output_sends_no_input(tmp_path, monkeypatch):
    state, app = _install_fake_live_probe(monkeypatch, tmp_path)

    def fake_infer(_api, _model, encoded, _description, *, timeout):
        state["stage_calls"].append(encoded.instance.stage)
        if encoded.instance.stage == "stage1":
            raw = {"name": "inspect_region", "arguments": {"x": 500, "y": 500, "zoom": 4}}
        else:
            raw = {"name": "wrong_tool", "arguments": {}}
        return raw, {"finish_reason": "tool_calls", "tool_call": raw}

    monkeypatch.setattr("scripts.probe_computer_use_skill.infer", fake_infer)
    result = _run_fake_probe(app, monkeypatch)
    assert state["stage_calls"] == ["stage1", "stage2"]
    assert state["events"] == []
    assert result["code"] == "INVALID_MODEL_OUTPUT"
    assert result["input_events_sent"] == 0
    assert result["input_outcome"] == "not_run"


@pytest.mark.parametrize(
    "flags",
    [
        ("--authorize",),
        ("--send-test-click",),
    ],
)
def test_probe_requires_both_explicit_input_authorization_flags(flags):
    with pytest.raises(SystemExit):
        main(["--helper-binary", "/tmp/helper", *flags])


@pytest.mark.parametrize("side", [1024, 1280, 1680, 2048])
def test_probe_expected_config_accepts_configured_shared_square(side):
    from types import SimpleNamespace
    from backend.computer_use.config import ComputerUseConfig
    from scripts.probe_computer_use_skill import _expected_config
    config = ComputerUseConfig(model_square_side_px=side)
    assert _expected_config(SimpleNamespace(computer_use=config)) is config

def test_x11_skill_probe_verifies_click_without_portal_or_eis_claim(tmp_path, monkeypatch):
    state, app = _install_fake_live_probe(monkeypatch, tmp_path, backend="x11")
    def infer(_api, _model, encoded, _description, *, timeout):
        raw = ({"name": "inspect_region", "arguments": {"x": 500, "y": 500, "zoom": 4}}
               if encoded.instance.stage == "stage1" else
               {"name": "interact_detail", "arguments": {"x": 500, "y": 500, "action": "left_click"}})
        return raw, {"tool_call": raw}
    monkeypatch.setattr("scripts.probe_computer_use_skill.infer", infer)
    result = _run_fake_probe(app, monkeypatch)
    assert result["input_outcome"] == "verified_in_test_target"
    assert result["backend"] == "x11"
    assert result["portal_authorization"] == "not_applicable"
    assert result["eis_mapping"] == "not_applicable"
    assert result["xtest_input"] == "verified_in_test_window"
    assert result["desktop_geometry"]["backend"] == "x11"
