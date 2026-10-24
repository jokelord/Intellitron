"""Explicit live desktop-input outcome probe using a temporary full-screen test surface.

This bypasses production calibration only for an isolated, operator-run
transport test. It never enables persistent computer-use settings.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import replace
import json
from pathlib import Path
import time
import tkinter as tk
import uuid

from backend.config import get_config
from backend.computer_use.native_client import NativeClient
from backend.security.capabilities import build_session_capability, require_capability
from backend.security.command_policy import CommandPolicy
from backend.security.environment_builder import EnvironmentBuilder
from backend.security.process_supervisor import ProcessSupervisor
from scripts.probe_computer_use_live import _timeout, error_domain, transport_evidence


GREEN = "#16c94c"


class ProbeError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _surface_visible(frame, binding, x: int, y: int) -> bool:
    ox, oy = binding.origin
    width, height = binding.size
    px = int((x - ox) * frame.width / width)
    py = int((y - oy) * frame.height / height)
    if not (3 <= px < frame.width - 3 and 3 <= py < frame.height - 3):
        return False
    good = 0
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            offset = ((py + dy) * frame.width + px + dx) * 3
            red, green, blue = frame.rgb[offset:offset + 3]
            good += green > 130 and green > red + 70 and green > blue + 60
    return good >= 20


def run_probe(app_config, *, authorization_timeout: float, transport_timeout: float):
    owner = str(uuid.uuid4())
    capability = build_session_capability(app_config, session_id=owner, mode="tool_calling")
    require_capability(capability, "desktop_capture")
    require_capability(capability, "desktop_input")
    config = app_config.computer_use
    runtime = capability.command_runtime_config()
    supervisor = ProcessSupervisor(max_output_bytes=runtime.max_output_bytes,
                                   termination_grace_seconds=runtime.termination_grace_seconds)
    native = None
    root = None
    sent = 0
    attempted = 0
    phase = "helper_preflight"
    try:
        policy = CommandPolicy(command_mode=runtime.command_mode,
            allowed_executables=runtime.allowed_executables,
            command_cwd_allowlist=runtime.command_cwd_allowlist,
            custom_blacklist=runtime.command_blacklist,
            max_exec_timeout_seconds=runtime.max_exec_timeout_seconds,
            max_output_bytes=runtime.max_output_bytes)
        native = NativeClient(helper_binary=config.helper_binary, supervisor=supervisor,
            environment_builder=EnvironmentBuilder(runtime), command_policy=policy,
            owner_session_id=owner, timeout=transport_timeout,
            authorization_timeout=authorization_timeout,
            max_packet_bytes=config.max_control_packet_bytes,
            max_frame_bytes=config.max_canonical_frame_bytes)
        phase = "authorization_and_native_start"
        binding = native.start(str(uuid.uuid4()), capability, context_nonce=str(uuid.uuid4()))
        if not binding.ready or binding.backend not in {"wayland", "x11"}:
            raise ProbeError("TEST_REQUIRES_READY_DESKTOP_BINDING")
        phase = "test_surface"
        try:
            root = tk.Tk()
            root.title("Computer-use isolated input test")
            root.configure(bg=GREEN)
            if binding.backend == "wayland":
                root.attributes("-fullscreen", True)
            else:
                # A root-screen fullscreen request may land on another output.
                # Place the owned test surface inside the selected XRandR monitor.
                ox, oy = binding.origin
                width, height = binding.size
                root.overrideredirect(True)
                root.geometry(f"{width}x{height}{ox:+d}{oy:+d}")
            canvas = tk.Canvas(root, bg=GREEN, highlightthickness=0, bd=0)
            canvas.pack(fill="both", expand=True)
            counts = {"down": 0, "up": 0, "wheel": 0}
            canvas.bind("<ButtonPress-1>", lambda _event: counts.__setitem__("down", counts["down"] + 1))
            canvas.bind("<ButtonRelease-1>", lambda _event: counts.__setitem__("up", counts["up"] + 1))
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                canvas.bind(sequence, lambda _event: counts.__setitem__("wheel", counts["wheel"] + 1))
            surface_started = time.monotonic()
            root.update()
        except tk.TclError as exc:
            raise ProbeError("TEST_SURFACE_UNAVAILABLE") from exc
        x = binding.origin[0] + binding.size[0] // 2
        y = binding.origin[1] + binding.size[1] // 2
        phase = "test_surface_visibility"
        visible = False
        for _ in range(2):
            root.update()
            time.sleep(0.1)
            frame = native.freeze(binding.binding_id)
            if frame.captured_monotonic >= surface_started and _surface_visible(frame, binding, x, y):
                visible = True
                break
        if not visible:
            raise ProbeError("TEST_SURFACE_NOT_VISIBLE_IN_CAPTURE")
        phase = "isolated_input_delivery"
        events = ({"kind": "motion", "x": x, "y": y},
                  {"kind": "button_down", "button": "left"},
                  {"kind": "button_up", "button": "left"},
                  {"kind": "wheel_step", "units": 1})
        for event in events:
            attempted += 1
            native.send_event(binding.binding_id, binding.geometry_version, event,
                              deadline_monotonic=time.monotonic() + 2)
            sent += 1
            root.update()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not all(counts.values()):
            root.update()
            time.sleep(0.01)
        verified = all(counts.values())
        return {"ready": False, **transport_evidence(binding,
                    input_status="verified_in_test_window" if verified else "unverified"),
                "live_frame": "verified",
                "input_events_sent": sent,
                "input_outcome": "verified_in_test_window" if verified else "not_verified",
                "test_window_events": counts, "calibration": "not_run"}
    except Exception as exc:
        return {"ready": False, "code": getattr(exc, "code", type(exc).__name__),
                "failure_phase": phase,
                "error_domain": error_domain(getattr(exc, "code", type(exc).__name__)),
                "input_events_sent": sent,
                "input_outcome": "delivery_unknown" if attempted else "not_run",
                "calibration": "not_run"}
    finally:
        try:
            if native is not None:
                native.close()
        finally:
            if root is not None:
                try:
                    root.destroy()
                except tk.TclError:
                    pass
            supervisor.shutdown()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorize", action="store_true")
    parser.add_argument("--helper-binary", required=True)
    parser.add_argument("--authorization-timeout", type=_timeout, default=120)
    parser.add_argument("--transport-timeout", type=_timeout, default=15)
    args = parser.parse_args(argv)
    if not args.authorize:
        parser.error("--authorize is required: this probe sends four input events to its own test window")
    if not Path(args.helper_binary).is_absolute() or "\x00" in args.helper_binary:
        parser.error("--helper-binary must be an absolute path")
    isolated = copy.deepcopy(get_config())
    isolated.agent.allow_desktop_capture = True
    isolated.agent.allow_desktop_input = True
    isolated.computer_use = replace(isolated.computer_use, helper_binary=args.helper_binary)
    result = run_probe(isolated, authorization_timeout=args.authorization_timeout,
                       transport_timeout=args.transport_timeout)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("input_outcome") == "verified_in_test_window" else 1


if __name__ == "__main__":
    raise SystemExit(main())
