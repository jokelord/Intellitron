"""Operator-run live model-to-click probe constrained to its own Tk target.

The probe sends a desktop image only to a loopback SGLang endpoint. Desktop
input requires both explicit CLI switches and is emitted only after the model
point is proven to lie inside a captured, isolated test target.

The transaction uses the same per-user advisory seat lock as production.
Run only while production computer_use and desktop permissions are disabled.
"""
from __future__ import annotations

import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
import tkinter as tk
import uuid
from io import BytesIO
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from PIL import Image, ImageChops, ImageDraw, ImageFont

from backend.config import get_config
from backend.computer_use.contracts import (
    Action,
    GroundingRequest,
    InspectRegion,
    InteractDetail,
    ProtocolDescriptor,
    Reobserve,
)
from backend.computer_use.coordinates import local_to_monitor, ratio_to_wayland, ratio_to_x11
from backend.computer_use.errors import ComputerUseError
from backend.computer_use.dispatch import (
    _SEAT_LOCK, _open_process_seat_lock, _try_process_seat_lock,
    _release_process_seat_lock,
)
from backend.computer_use.model_adapter import decode_stage, describe_stage, encode_stage, provider_messages_image_first
from backend.computer_use.native_client import NativeClient
from backend.computer_use.runtime import GroundingRenderer, deployment_fingerprint
from backend.security.capabilities import build_session_capability, require_capability
from backend.security.command_policy import CommandPolicy
from backend.security.environment_builder import EnvironmentBuilder
from backend.security.process_supervisor import ProcessSupervisor
from backend.computer_use.contracts import thaw
from scripts.probe_computer_use_live import error_domain, transport_evidence
from scripts.computer_use_probe_response import extract_probe_call


GREEN = "#16c94c"
MAGENTA = "#ff00ff"
TARGET_DESCRIPTION = "the single small magenta target on the solid green test surface"
DIAGRAMS_DIR = Path(__file__).resolve().parents[1] / "logs" / "computer-use" / "live-probes"


class ProbeError(RuntimeError):
    def __init__(self, code: str, phase: str | None = None, *, diagnostics: dict | None = None):
        self.code = code
        self.phase = phase
        self.diagnostics = diagnostics
        super().__init__(code)


class ClickDeliveryError(ProbeError):
    def __init__(self, *, sent_events: int, attempted_events: int, button_down_attempted: bool):
        super().__init__("ACTION_DELIVERY_UNKNOWN", "isolated_test_click")
        self.sent_events = sent_events
        self.attempted_events = attempted_events
        self.button_down_attempted = button_down_attempted


def _is_green(rgb: tuple[int, int, int]) -> bool:
    red, green, blue = rgb
    return green > 130 and green > red + 70 and green > blue + 60


def detect_test_target(
    frame,
    *,
    expected_bounds: tuple[int, int, int, int],
    panel_bounds: tuple[int, int, int, int] | None = None,
    tolerance_px: int = 3,
) -> tuple[int, int, int, int]:
    """Verify the captured target and its surrounding Tk-owned green panel."""
    image = Image.frombytes("RGB", (frame.width, frame.height), frame.rgb)
    pixels = image.load()
    red, green, blue = image.split()
    magenta = ImageChops.multiply(
        ImageChops.multiply(
            red.point(lambda value: 255 if value >= 170 else 0),
            green.point(lambda value: 255 if value <= 125 else 0),
        ),
        blue.point(lambda value: 255 if value >= 170 else 0),
    )
    # A desktop can contain unrelated magenta icons. Only the Tk-owned
    # rectangle at its trusted mapped location is eligible as a test target.
    ex0, ey0, ex1, ey1 = expected_bounds
    margin = max(tolerance_px, 3)
    search = (max(0, ex0 - margin), max(0, ey0 - margin),
              min(frame.width, ex1 + margin), min(frame.height, ey1 + margin))
    local = magenta.crop(search).getbbox()
    bounds = None if local is None else (
        search[0] + local[0], search[1] + local[1],
        search[0] + local[2], search[1] + local[3],
    )
    if bounds is None:
        raise ProbeError("TEST_TARGET_NOT_VISIBLE", diagnostics={
            "expected_bounds_px": list(expected_bounds),
            "unrelated_magenta_bounds_px": list(magenta.getbbox()) if magenta.getbbox() else None,
            "frame_size_px": [frame.width, frame.height],
        })
    x0, y0, x1, y1 = bounds
    if x1 - x0 < 8 or y1 - y0 < 8 or x1 - x0 > frame.width // 8 or y1 - y0 > frame.height // 8:
        raise ProbeError("TEST_TARGET_BOUNDS_INVALID")
    if magenta.histogram()[255] < 32:
        raise ProbeError("TEST_TARGET_NOT_VISIBLE")
    if (isinstance(tolerance_px, bool) or not isinstance(tolerance_px, int)
            or tolerance_px < 0):
        raise ValueError("target tolerance must be a non-negative integer")
    if any(abs(actual - expected) > tolerance_px
           for actual, expected in zip(bounds, expected_bounds)):
        raise ProbeError("TEST_TARGET_POSITION_MISMATCH", diagnostics={
            "captured_bounds_px": list(bounds),
            "expected_bounds_px": list(expected_bounds),
            "tolerance_px": tolerance_px,
            "frame_size_px": [frame.width, frame.height],
        })

    # The actual filled target must be visible, not merely a few magenta
    # pixels on the perimeter of a predicted rectangle.
    inner = (x0 + 2, y0 + 2, x1 - 2, y1 - 2)
    inner_pixels = max(0, inner[2] - inner[0]) * max(0, inner[3] - inner[1])
    if inner_pixels == 0 or magenta.crop(inner).histogram()[255] / inner_pixels < 0.8:
        raise ProbeError("TEST_TARGET_NOT_VISIBLE")

    if panel_bounds is None:
        panel_bounds = (0, 0, frame.width, frame.height)
    px0, py0, px1, py1 = panel_bounds
    if (px0 < 0 or py0 < 0 or px1 > frame.width or py1 > frame.height
            or px1 <= px0 or py1 <= py0
            or x0 < px0 + 8 or y0 < py0 + 8 or x1 > px1 - 8 or y1 > py1 - 8):
        raise ProbeError("TEST_SURFACE_GEOMETRY_MISMATCH")

    # For a windowed test, sample throughout the predicted canvas rather
    # than the unrelated desktop around it.
    sample_offsets = tuple(px0 + round((index + 0.5) * (px1 - px0) / 7) for index in range(7))
    sample_ys = tuple(py0 + round((index + 0.5) * (py1 - py0) / 7) for index in range(7))
    green_samples = 0
    visible_samples = 0
    radius = 2
    for cy in sample_ys:
        for cx in sample_offsets:
            if x0 - 8 <= cx < x1 + 8 and y0 - 8 <= cy < y1 + 8:
                continue
            visible_samples += 1
            green_count = sum(
                _is_green(pixels[x, y])
                for y in range(max(0, cy - radius), min(frame.height, cy + radius + 1))
                for x in range(max(0, cx - radius), min(frame.width, cx + radius + 1))
            )
            green_samples += green_count >= 18
    required_green_fraction = 0.95 if panel_bounds != (0, 0, frame.width, frame.height) else 0.85
    if visible_samples < 24 or green_samples / visible_samples < required_green_fraction:
        raise ProbeError("TEST_SURFACE_NOT_VISIBLE")
    return bounds


def _expected_frame_geometry(frame, binding, surface_geometry):
    """Map the actual Tk canvas and its local target through the monitor binding."""
    canvas_width, canvas_height = surface_geometry["canvas_size"]
    logical_width, logical_height = binding.size
    canvas_origin = surface_geometry.get("canvas_origin")
    if canvas_origin is None and (canvas_width, canvas_height) == binding.size:
        canvas_origin = binding.origin
    if (canvas_origin is None or logical_width <= 0 or logical_height <= 0
            or canvas_width <= 0 or canvas_height <= 0
            or binding.mapping_evidence.get("full_monitor") is not True
            or not binding.mapping_evidence.get("mapping_id")):
        raise ProbeError("TEST_SURFACE_GEOMETRY_MISMATCH")
    offset_x = canvas_origin[0] - binding.origin[0]
    offset_y = canvas_origin[1] - binding.origin[1]
    x0, y0, x1, y1 = surface_geometry["target_bounds"]
    if (offset_x < 0 or offset_y < 0
            or offset_x + canvas_width > logical_width
            or offset_y + canvas_height > logical_height
            or x0 < 8 or y0 < 8 or x1 > canvas_width - 8 or y1 > canvas_height - 8
            or x1 <= x0 or y1 <= y0):
        raise ProbeError("TEST_SURFACE_GEOMETRY_MISMATCH")
    sx, sy = frame.width / logical_width, frame.height / logical_height
    panel = (round(offset_x * sx), round(offset_y * sy),
             round((offset_x + canvas_width) * sx), round((offset_y + canvas_height) * sy))
    expected = (round((offset_x + x0) * sx), round((offset_y + y0) * sy),
                round((offset_x + x1) * sx), round((offset_y + y1) * sy))
    # Allow for compositor rasterization at fractional output scales, but keep
    # the captured mark within roughly one logical pixel of Tk's exact target.
    tolerance = max(2, math.ceil(max(sx, sy)))
    return expected, panel, tolerance


def _expected_frame_target(frame, binding, surface_geometry):
    expected, _panel, tolerance = _expected_frame_geometry(frame, binding, surface_geometry)
    return expected, tolerance


def _detect_bound_target(frame, binding, surface_geometry):
    expected, panel, tolerance = _expected_frame_geometry(frame, binding, surface_geometry)
    return detect_test_target(frame, expected_bounds=expected,
                              panel_bounds=panel, tolerance_px=tolerance)


def crop_contains_target(crop, target_bounds: tuple[int, int, int, int], *, margin_px: int = 8) -> bool:
    x0, y0, x1, y1 = target_bounds
    return (
        crop.x0 <= x0 - margin_px
        and crop.y0 <= y0 - margin_px
        and x1 + margin_px <= crop.x1
        and y1 + margin_px <= crop.y1
    )


def _inner_target_bounds(target_bounds: tuple[int, int, int, int],
                         inset_fraction: float = 0.15) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = target_bounds
    inset_x = max(2, int((x1 - x0) * inset_fraction))
    inset_y = max(2, int((y1 - y0) * inset_fraction))
    return x0 + inset_x, y0 + inset_y, x1 - inset_x, y1 - inset_y


def _source_bounds_to_detail_normalized(bounds, crop) -> list[float]:
    x0, y0, x1, y1 = bounds
    return [
        float((x0 - crop.x0) * crop.local_width / crop.crop_width),
        float((y0 - crop.y0) * crop.local_height / crop.crop_height),
        float((x1 - crop.x0) * crop.local_width / crop.crop_width),
        float((y1 - crop.y0) * crop.local_height / crop.crop_height),
    ]


def _format_number(value) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.3f}".rstrip("0").rstrip(".")


def add_retry_feedback(encoded, *, rough_xy, crop_rect, source_size, reobserve_reason):
    """Tell a retry why the prior model attempt requested a fresh observation."""
    messages = thaw(encoded.messages)
    text = messages[0]["content"][0]["text"]
    messages[0]["content"][0]["text"] = (
        text + " Prior attempt feedback: the previous detail did not yield an approved final point. "
        "Stage2 requested observe() for a fresh overview. "
        "Choose a fresh approximate point using only the current visible overview."
    )
    return replace(encoded, messages=tuple(messages))


def infer(api: str, model: str, encoded, description: str, *, timeout: float):
    """Run one bounded, non-streaming tool call against loopback SGLang."""
    from backend.computer_use.contracts import thaw

    tools = thaw(encoded.tools)
    payload = {
        "model": model,
        "messages": provider_messages_image_first(encoded),
        "tools": [{"type": "function", "function": {
            "name": tool["name"],
            "description": (description if tool["name"] != "observe" else
                            "Request a fresh overview if the target is missing, ambiguous, stale, or in the wrong region."),
            "parameters": tool["parameters"],
        }} for tool in tools],
        "tool_choice": ("auto" if len(tools) > 1 else
                        {"type": "function", "function": {"name": tools[0]["name"]}}),
        "chat_template_kwargs": {"enable_thinking": False},
        "temperature": 0,
        "max_tokens": 2048,
        "stream": False,
    }
    request = urllib.request.Request(
        api.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"SGLang HTTP {error.code}: {error.read().decode(errors='replace')}") from error
    return extract_probe_call(result["choices"][0], encoded)


def model_id(api: str, *, timeout: float) -> str:
    with urllib.request.urlopen(api.rstrip("/") + "/v1/models", timeout=timeout) as response:
        return json.load(response)["data"][0]["id"]


def approve_click_point(
    decision,
    crop,
    target_bounds: tuple[int, int, int, int],
    binding,
    frame_width: int,
    frame_height: int,
    *,
    inset_fraction: float = 0.15,
) -> tuple[float, float] | None:
    """Map a typed Stage-2 point and accept only the inner target rectangle."""
    if not isinstance(decision, InteractDetail) or decision.crop_id != crop.crop_id:
        return None
    if not crop_contains_target(crop, target_bounds):
        return None
    try:
        nx, ny = local_to_monitor(crop, decision.x, decision.y)
    except (TypeError, ValueError, ComputerUseError):
        return None
    px, py = float(nx * frame_width), float(ny * frame_height)
    x0, y0, x1, y1 = _inner_target_bounds(target_bounds, inset_fraction)
    if not (x0 <= px < x1 and y0 <= py < y1):
        return None
    mapper = ratio_to_x11 if binding.backend == "x11" else ratio_to_wayland
    return mapper(nx, ny, binding.origin, *binding.size)


def _local_api(api: str) -> bool:
    try:
        parsed = urlsplit(api)
        return parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    except ValueError:
        return False


def _artifact_bytes(encoded) -> bytes:
    url = encoded.messages[0]["content"][1]["image_url"]["url"]
    prefix = "data:image/png;base64,"
    if not isinstance(url, str) or not url.startswith(prefix):
        raise ProbeError("MODEL_IMAGE_PAYLOAD_INVALID")
    import base64
    import binascii
    try:
        return base64.b64decode(url[len(prefix):], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ProbeError("MODEL_IMAGE_PAYLOAD_INVALID") from exc


def _save_input(encoded, expected: bytes, path: Path) -> None:
    sent = _artifact_bytes(encoded)
    if sent != expected:
        raise ProbeError("MODEL_IMAGE_PAYLOAD_MISMATCH")
    path = Path(path).expanduser()
    path.write_bytes(sent)


def _annotation(
    png: bytes,
    title: str,
    target_bounds: tuple[int, int, int, int] | None,
    *,
    point: tuple[float, float] | None = None,
    crop_bounds: tuple[int, int, int, int] | None = None,
) -> Image.Image:
    source = Image.open(BytesIO(png)).convert("RGB")
    header, footer = 42, 30
    canvas = Image.new("RGB", (source.width, source.height + header + footer), "#111827")
    canvas.paste(source, (0, header))
    draw = ImageDraw.Draw(canvas)
    draw.text((10, 10), title, fill="white", font=ImageFont.load_default(size=16))
    if crop_bounds is not None:
        x0, y0, x1, y1 = crop_bounds
        draw.rectangle((x0, y0 + header, x1 - 1, y1 - 1 + header), outline="#ffd400", width=3)
    if target_bounds is not None:
        x0, y0, x1, y1 = target_bounds
        draw.rectangle((x0, y0 + header, x1 - 1, y1 - 1 + header), outline="#00e5ff", width=3)
    if point is not None:
        px, py = point
        draw.ellipse((px - 9, py + header - 9, px + 9, py + header + 9), outline="#ff3b30", width=3)
        draw.line((px - 13, py + header, px + 13, py + header), fill="#ff3b30", width=2)
        draw.line((px, py + header - 13, px, py + header + 13), fill="#ff3b30", width=2)
    draw.text((10, header + source.height + 8), "cyan: captured target  |  red: model point  |  yellow: source crop", fill="white", font=ImageFont.load_default(size=12))
    return canvas


def _new_surface(binding, *, target_side_px: int = 56):
    root = tk.Tk()
    root.title("Isolated computer-use model probe")
    root.configure(bg=GREEN)
    logical_width, logical_height = binding.size
    if logical_width < target_side_px + 176 or logical_height < target_side_px + 176:
        root.destroy()
        raise ProbeError("TEST_SURFACE_GEOMETRY_MISMATCH")
    origin_x, origin_y = binding.origin
    root.geometry(f"{logical_width - 160}x{logical_height - 160}"
                  f"{origin_x + 80:+d}{origin_y + 80:+d}")
    canvas = tk.Canvas(root, bg=GREEN, highlightthickness=0, bd=0, cursor="none")
    canvas.pack(fill="both", expand=True)
    root.update_idletasks()
    root.update()
    width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
    cx, cy = width // 2, height // 2
    half = target_side_px // 2
    logical_bounds = (cx - half, cy - half, cx + half, cy + half)
    refresh_marker = canvas.create_rectangle(24, 24, 120, 120,
        fill="#008c16", outline="#008c16", width=0)
    rectangle = canvas.create_rectangle(cx - half, cy - half, cx + half - 1, cy + half - 1,
                                        fill=MAGENTA, outline=MAGENTA, width=0)
    root.update_idletasks()
    counts = {"inside_down": 0, "inside_up": 0, "outside_down": 0}
    rect = canvas.coords(rectangle)

    def inside(event):
        return rect[0] <= event.x < logical_bounds[2] and rect[1] <= event.y < logical_bounds[3]

    def on_down(event):
        counts["inside_down" if inside(event) else "outside_down"] += 1

    def on_up(event):
        if inside(event):
            counts["inside_up"] += 1

    canvas.bind("<ButtonPress-1>", on_down)
    canvas.bind("<ButtonRelease-1>", on_up)
    try:
        root.focus_force()
        root.lift()
        root.update()
    except tk.TclError:
        pass
    canvas_origin = (canvas.winfo_rootx(), canvas.winfo_rooty())
    surface_geometry = {"canvas_origin": canvas_origin, "canvas_size": (width, height),
                        "target_bounds": logical_bounds, "refresh_marker": refresh_marker,
                        "windowed": True}
    return root, canvas, counts, surface_geometry


def _assert_surface_stable(canvas, surface_geometry):
    """Reject a moved or resized test canvas before trusting mapped pixels."""
    try:
        actual_origin = (canvas.winfo_rootx(), canvas.winfo_rooty())
        actual_size = (canvas.winfo_width(), canvas.winfo_height())
    except tk.TclError as exc:
        raise ProbeError("TEST_SURFACE_CLOSED") from exc
    if (actual_origin != tuple(surface_geometry["canvas_origin"])
            or actual_size != tuple(surface_geometry["canvas_size"])):
        raise ProbeError("TEST_SURFACE_GEOMETRY_CHANGED", diagnostics={
            "expected_canvas_origin": list(surface_geometry["canvas_origin"]),
            "actual_canvas_origin": list(actual_origin),
            "expected_canvas_size": list(surface_geometry["canvas_size"]),
            "actual_canvas_size": list(actual_size),
        })


def _refresh_test_surface(canvas, marker, refresh_count):
    canvas.configure(bg="#00f020" if refresh_count % 2 else GREEN)
    if marker is not None:
        left = 24 if refresh_count % 2 else 224
        canvas.coords(marker, left, 24, left + 96, 120)


def _capture_visible_target(native, binding, root, surface_geometry, *, timeout: float | None = None,
                            overall_deadline: float | None = None,
                            refresh_surface=None,
                            diagnostic_path: Path | None = None):
    if timeout is None:
        transport_timeout = getattr(native, "timeout", 0)
        timeout = max(5.0, float(transport_timeout) + 1.0)
    deadline = time.monotonic() + timeout
    if overall_deadline is not None:
        deadline = min(deadline, overall_deadline)
    last_target_error = None
    last_frame = None
    while time.monotonic() < deadline:
        try:
            root.update()
        except tk.TclError as exc:
            raise ProbeError("TEST_SURFACE_CLOSED") from exc
        if overall_deadline is not None:
            _cap_native_timeout(native, overall_deadline, "visible_test_target")
        if refresh_surface is not None:
            frame = _freeze_with_surface_refresh(
                native, binding.binding_id, root, refresh_surface,
                deadline=deadline, phase="visible_test_target")
        else:
            frame = native.freeze(binding.binding_id)
        last_frame = frame
        try:
            bounds = _detect_bound_target(frame, binding, surface_geometry)
            return frame, bounds
        except ProbeError as exc:
            if exc.code not in {"TEST_SURFACE_NOT_VISIBLE", "TEST_TARGET_NOT_VISIBLE",
                                "TEST_TARGET_POSITION_MISMATCH"}:
                raise
            last_target_error = exc
        time.sleep(0.1)
    if overall_deadline is not None:
        _check_deadline(overall_deadline, "visible_test_target")
    if last_target_error is not None:
        if diagnostic_path is not None and last_frame is not None:
            try:
                Image.frombytes("RGB", (last_frame.width, last_frame.height), last_frame.rgb).save(
                    diagnostic_path, format="PNG")
                last_target_error.diagnostics = {
                    **(last_target_error.diagnostics or {}),
                    "diagnostic_frame": str(diagnostic_path),
                }
            except (OSError, ValueError):
                pass
        raise last_target_error
    raise ProbeError("TEST_TARGET_NOT_VISIBLE")


def _cap_native_timeout(native, deadline: float, phase: str) -> float:
    remaining = _check_deadline(deadline, phase)
    transport_timeout = getattr(native, "timeout", None)
    if isinstance(transport_timeout, (int, float)) and not isinstance(transport_timeout, bool):
        native.timeout = min(transport_timeout, remaining)
    return remaining


def _freeze_with_surface_refresh(native, binding_id, root, refresh_surface, *,
                                 deadline: float, phase: str,
                                 refresh_interval: float = 0.25,
                                 captured_after: float | None = None,
                                 sequence_after: int | None = None):
    """Keep Tk repainting in its owner thread while PipeWire awaits a new frame."""
    if refresh_interval <= 0:
        raise ValueError("refresh_interval must be positive")
    last_stale = None
    while time.monotonic() < deadline:
        _cap_native_timeout(native, deadline, phase)
        refresh_surface()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(native.freeze, binding_id)
            next_refresh = time.monotonic() + refresh_interval
            while not pending.done() and time.monotonic() < deadline:
                try:
                    root.update()
                except tk.TclError as exc:
                    raise ProbeError("TEST_SURFACE_CLOSED", phase) from exc
                if time.monotonic() >= next_refresh:
                    refresh_surface()
                    next_refresh = time.monotonic() + refresh_interval
                time.sleep(min(0.02, refresh_interval / 2))
            frame = pending.result()
            _check_deadline(deadline, phase)
        stale_time = captured_after is not None and frame.captured_monotonic < captured_after
        stale_sequence = sequence_after is not None and frame.sequence <= sequence_after
        if not stale_time and not stale_sequence:
            return frame
        last_stale = {"candidate_sequence": frame.sequence,
                      "candidate_captured_monotonic": frame.captured_monotonic,
                      "required_after_monotonic": captured_after,
                      "required_after_sequence": sequence_after}
    raise ProbeError("TEST_FRAME_NOT_FRESH", phase, diagnostics=last_stale)


def _checked_binding(native, expected, *, deadline: float | None = None):
    if deadline is not None:
        _cap_native_timeout(native, deadline, "binding_revalidation")
    current = native.resolve_binding()
    if current != expected or not current.ready or current.backend not in {"wayland", "x11"}:
        raise ProbeError("GEOMETRY_CHANGED")
    return current


@contextmanager
def _seat_transaction(timeout_seconds: float):
    deadline = time.monotonic() + timeout_seconds
    if not _SEAT_LOCK.acquire(timeout=timeout_seconds):
        raise ProbeError("INPUT_SEAT_BUSY", "pre_dispatch_revalidation")
    fd = None
    process_lock_acquired = False
    try:
        fd = _open_process_seat_lock()
        while not process_lock_acquired:
            process_lock_acquired = _try_process_seat_lock(fd)
            if process_lock_acquired:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeError("INPUT_SEAT_BUSY", "pre_dispatch_revalidation")
            time.sleep(min(0.01, remaining))
        yield
    finally:
        try:
            if fd is not None:
                if process_lock_acquired:
                    _release_process_seat_lock(fd)
                else:
                    os.close(fd)
        finally:
            _SEAT_LOCK.release()


def _check_deadline(deadline: float, phase: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProbeError("PROBE_INFERENCE_DEADLINE_EXCEEDED", phase)
    return remaining


def _wait_for_test_click(root, counts, baseline, *, deadline: float):
    """Wait for exactly one in-target press/release pair, draining Tk events."""
    deltas = {key: counts[key] - baseline[key] for key in baseline}
    while time.monotonic() < deadline:
        root.update()
        deltas = {key: counts[key] - baseline[key] for key in baseline}
        if deltas["outside_down"] >= 1 or (deltas["inside_down"] >= 1 and deltas["inside_up"] >= 1):
            break
        time.sleep(0.01)
    verified = (deltas["inside_down"] == 1 and deltas["inside_up"] == 1
                and deltas["outside_down"] == 0)
    return verified, deltas


def _send_one_click(native, binding, point, *, deadline_seconds: float = 3.0) -> tuple[int, int]:
    deadline = time.monotonic() + deadline_seconds
    sent = 0
    attempted = 0
    down_may_be_held = False
    try:
        for event in (
            {"kind": "motion", "x": point[0], "y": point[1]},
            {"kind": "button_down", "button": "left"},
            {"kind": "button_up", "button": "left"},
        ):
            if event["kind"] == "button_down":
                down_may_be_held = True
            attempted += 1
            native.send_event(binding.binding_id, binding.geometry_version, event,
                              deadline_monotonic=deadline)
            sent += 1
            if event["kind"] == "button_up":
                down_may_be_held = False
        return sent, attempted
    except Exception as exc:
        if down_may_be_held:
            attempted += 1
            try:
                native.send_event(binding.binding_id, binding.geometry_version,
                                  {"kind": "release_cleanup", "button": "left"},
                                  deadline_monotonic=time.monotonic() + deadline_seconds)
                sent += 1
            except Exception:
                pass
        raise ClickDeliveryError(sent_events=sent, attempted_events=attempted,
                                 button_down_attempted=down_may_be_held) from exc


def _expected_config(app_config):
    config = app_config.computer_use
    if config.protocol != "relative_1000" or config.grid_divisions != 16 or not config.grid_enabled:
        raise ProbeError("PROBE_CONFIG_MISMATCH")
    return config


def _require_production_disabled(app_config):
    if (app_config.computer_use.ready
            or app_config.agent.allow_desktop_capture
            or app_config.agent.allow_desktop_input):
        raise ProbeError("PROBE_REQUIRES_PRODUCTION_DISABLED", "configuration")


def _bounded_int(text: str, *, lower: int, upper: int, name: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} must be an integer") from exc
    if not lower <= value <= upper:
        raise argparse.ArgumentTypeError(f"{name} must be between {lower} and {upper}")
    return value


def _retry_count(text: str) -> int:
    return _bounded_int(text, lower=0, upper=2, name="--max-reobserves")


def _target_side(text: str) -> int:
    return _bounded_int(text, lower=24, upper=256, name="--target-side-px")


def run_probe(
    app_config,
    *,
    helper_binary: str,
    api: str,
    authorization_timeout: float,
    transport_timeout: float,
    max_reobserves: int = 1,
    target_side_px: int = 56,
    overall_timeout_seconds: float = 300,
    seat_lock_timeout_seconds: float = 3,
    idle_diagnostic_seconds: float | None = None,
    windowed_diagnostic: bool = False,
):
    """Run real Stage-1/2 inference, with strict isolated-target click gates."""
    helper_binary = str(Path(helper_binary).expanduser())
    if (isinstance(overall_timeout_seconds, bool) or not isinstance(overall_timeout_seconds, (int, float))
            or not 0 < overall_timeout_seconds <= 900):
        raise ValueError("overall_timeout_seconds must be in (0, 900]")
    if (isinstance(seat_lock_timeout_seconds, bool) or not isinstance(seat_lock_timeout_seconds, (int, float))
            or not 0 < seat_lock_timeout_seconds <= 10):
        raise ValueError("seat_lock_timeout_seconds must be in (0, 10]")
    if idle_diagnostic_seconds is not None and (
            isinstance(idle_diagnostic_seconds, bool)
            or not isinstance(idle_diagnostic_seconds, (int, float))
            or not math.isfinite(idle_diagnostic_seconds)
            or not 0 <= idle_diagnostic_seconds <= 60):
        raise ValueError("idle_diagnostic_seconds must be in [0, 60]")
    if windowed_diagnostic and idle_diagnostic_seconds is None:
        raise ValueError("windowed diagnostic requires an idle diagnostic interval")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    artifacts = DIAGRAMS_DIR / run_id
    artifacts.mkdir(parents=True, exist_ok=False)
    report = {
        "ready": False,
        "probe": "controlled_live_model_to_tk_click",
        "calibration": "not_run",
        "input_events_sent": 0,
        "input_outcome": "not_run",
        "artifact_dir": str(artifacts),
        "attempts": [],
    }
    native = root = None
    supervisor = None
    surface_geometry = None
    click_attempted = False
    click_sent = 0
    phase = "configuration"
    started = time.monotonic()
    deadline = started + overall_timeout_seconds
    try:
        _require_production_disabled(app_config)
        if not _local_api(api):
            raise ProbeError("LOCAL_SGLANG_ENDPOINT_REQUIRED", phase)
        config = _expected_config(app_config)
        if not helper_binary.startswith("/") or "\x00" in helper_binary:
            raise ProbeError("INVALID_HELPER_PATH", phase)
        _check_deadline(deadline, phase)
        actual_model = model_id(api, timeout=_check_deadline(deadline, phase))
        _check_deadline(deadline, phase)
        fingerprint = deployment_fingerprint(app_config)
        report.update({"model": actual_model, "deployment_fingerprint": fingerprint,
                       "grid": "Stage1: 8 divisions, no labels; Stage2: 16 divisions, labels 125..875; shared orthogonal anchors31.25..906.25 in steps125 (8 copies each)", "stage1_zoom": app_config.computer_use.stage1_zoom})

        isolated = copy.deepcopy(app_config)
        isolated.agent.allow_desktop_capture = True
        isolated.agent.allow_desktop_input = True
        isolated.computer_use = replace(isolated.computer_use, helper_binary=helper_binary)
        owner = str(uuid.uuid4())
        capability = build_session_capability(isolated, session_id=owner, mode="tool_calling")
        require_capability(capability, "desktop_capture")
        require_capability(capability, "desktop_input")
        runtime = capability.command_runtime_config()
        supervisor = ProcessSupervisor(max_output_bytes=runtime.max_output_bytes,
                                       termination_grace_seconds=runtime.termination_grace_seconds)
        policy = CommandPolicy(command_mode=runtime.command_mode,
            allowed_executables=runtime.allowed_executables,
            command_cwd_allowlist=runtime.command_cwd_allowlist,
            custom_blacklist=runtime.command_blacklist,
            max_exec_timeout_seconds=runtime.max_exec_timeout_seconds,
            max_output_bytes=runtime.max_output_bytes)
        native_timeout = min(transport_timeout, _check_deadline(deadline, phase))
        native = NativeClient(helper_binary=helper_binary, supervisor=supervisor,
            environment_builder=EnvironmentBuilder(runtime), command_policy=policy,
            owner_session_id=owner, timeout=native_timeout,
            authorization_timeout=min(authorization_timeout, _check_deadline(deadline, phase)),
            max_packet_bytes=config.max_control_packet_bytes,
            max_frame_bytes=config.max_canonical_frame_bytes)

        phase = "authorization_and_native_start"
        _check_deadline(deadline, phase)
        binding = native.start(str(uuid.uuid4()), capability, context_nonce=str(uuid.uuid4()))
        _check_deadline(deadline, phase)
        if not binding.ready or binding.backend not in {"wayland", "x11"}:
            raise ProbeError("TEST_REQUIRES_READY_DESKTOP_BINDING", phase)
        root, canvas, counts, surface_geometry = _new_surface(binding, target_side_px=target_side_px)
        _assert_surface_stable(canvas, surface_geometry)
        surface_refreshes = 0

        def request_new_test_frame():
            nonlocal surface_refreshes
            surface_refreshes += 1
            # PipeWire may suppress duplicate frames when the desktop is
            # motionless. Change only the green test background so a fresh
            # compositor frame can be proven before any input transaction.
            # A one-channel colour change can be too subtle to reliably
            # produce a new screencast frame after a long model inference.
            # Both colours remain recognisably green to the surface verifier.
            _refresh_test_surface(canvas, surface_geometry.get("refresh_marker"), surface_refreshes)
            root.update_idletasks()
            root.update()
        surface_started = time.monotonic()
        phase = "visible_test_target"
        frame, target_bounds = _capture_visible_target(native, binding, root, surface_geometry,
                                                       overall_deadline=deadline,
                                                       refresh_surface=request_new_test_frame,
                                                       diagnostic_path=artifacts / "diagnostic-frame.png")
        _assert_surface_stable(canvas, surface_geometry)
        _check_deadline(deadline, phase)
        if frame.captured_monotonic < surface_started:
            raise ProbeError("TEST_FRAME_PREDATES_SURFACE", phase)
        report.update({**transport_evidence(binding, mapping_status="mapped"),
            "desktop_geometry": {"backend": binding.backend, "logical_origin": list(binding.origin), "logical_size": list(binding.size),
                                 "pixel_size": [frame.width, frame.height],
                                 "geometry_version": binding.geometry_version,
                                 "stream_generation": binding.stream_generation},
            "captured_target_bounds": list(target_bounds)})

        if idle_diagnostic_seconds is not None:
            # Reproduce the model's time away from Tk without making any model
            # request or arming the input transaction.
            phase = "idle_frame_liveness"
            time.sleep(min(idle_diagnostic_seconds, _check_deadline(deadline, phase)))
            _checked_binding(native, binding, deadline=deadline)
            refresh_started = time.monotonic()
            if windowed_diagnostic:
                phase = "windowed_frame_liveness"
            fresh = _freeze_with_surface_refresh(
                native, binding.binding_id, root, request_new_test_frame,
                deadline=deadline, phase=phase,
                captured_after=refresh_started, sequence_after=frame.sequence)
            _assert_surface_stable(canvas, surface_geometry)
            if fresh.captured_monotonic < refresh_started or fresh.sequence <= frame.sequence:
                raise ProbeError("TEST_FRAME_NOT_FRESH", phase)
            fresh_bounds = _detect_bound_target(fresh, binding, surface_geometry)
            if fresh_bounds != target_bounds:
                raise ProbeError("TEST_TARGET_CHANGED", phase)
            if windowed_diagnostic:
                second_started = time.monotonic()
                second = _freeze_with_surface_refresh(
                    native, binding.binding_id, root, request_new_test_frame,
                    deadline=deadline, phase=phase,
                    captured_after=second_started, sequence_after=fresh.sequence)
                _assert_surface_stable(canvas, surface_geometry)
                if second.captured_monotonic < second_started or second.sequence <= fresh.sequence:
                    raise ProbeError("TEST_FRAME_NOT_FRESH", phase)
                second_bounds = _detect_bound_target(second, binding, surface_geometry)
                if second_bounds != target_bounds:
                    raise ProbeError("TEST_TARGET_CHANGED", phase)
                report.update({"windowed_frame_liveness": "verified",
                               "before_sequence": frame.sequence,
                               "after_first_sequence": fresh.sequence,
                               "after_second_sequence": second.sequence})
                return report
            report.update({"idle_frame_liveness": "verified",
                           "before_sequence": frame.sequence,
                           "after_sequence": fresh.sequence})
            return report

        request = GroundingRequest(
            request_id=str(uuid.uuid4()), session_id=owner, binding_id=binding.binding_id,
            target_description=TARGET_DESCRIPTION, action=Action.LEFT_CLICK,
            protocol=ProtocolDescriptor(name="relative_1000"),
            calibration_fingerprint=fingerprint,
        )
        renderer = GroundingRenderer()
        retry_feedback = None
        for attempt in range(max_reobserves + 1):
            phase = f"stage1_attempt_{attempt + 1}"
            _checked_binding(native, binding, deadline=deadline)
            _assert_surface_stable(canvas, surface_geometry)
            current_bounds = _detect_bound_target(frame, binding, surface_geometry)
            if current_bounds != target_bounds:
                raise ProbeError("TEST_TARGET_CHANGED", phase)
            overview = renderer.overview(frame, config)
            overview_path = artifacts / f"attempt-{attempt + 1}-stage1-input.png"
            stage1_encoded = encode_stage(describe_stage(
                "stage1", overview, None, request,
                grid_label_placement=config.grid_label_placement, stage1_zoom=config.stage1_zoom), overview)
            if retry_feedback is not None:
                stage1_encoded = add_retry_feedback(stage1_encoded, **retry_feedback)
            _save_input(stage1_encoded, overview.png, overview_path)
            (artifacts / f"attempt-{attempt + 1}-stage1-prompt.txt").write_text(
                stage1_encoded.messages[0]["content"][0]["text"] + "\n")
            raw1, trace1 = infer(api, actual_model, stage1_encoded,
                                "Return the approximate target position for a later detail crop.",
                                timeout=_check_deadline(deadline, phase))
            _check_deadline(deadline, phase)
            decision1 = decode_stage(stage1_encoded, raw1)
            if not isinstance(decision1, InspectRegion):
                raise ProbeError("INVALID_STAGE1_DECISION", phase)
            crop = renderer.crop(frame, decision1, request, config)
            crop_contains = crop_contains_target(crop, target_bounds)
            stage1_point = (float(decision1.x / 1000) * overview.width,
                            float(decision1.y / 1000) * overview.height)
            scale_x, scale_y = overview.width / frame.width, overview.height / frame.height
            displayed_target = tuple(round(value * (scale_x if index % 2 == 0 else scale_y))
                                     for index, value in enumerate(target_bounds))
            displayed_crop = tuple(round(value * (scale_x if index % 2 == 0 else scale_y))
                                   for index, value in enumerate((crop.x0, crop.y0, crop.x1, crop.y1)))
            stage1_marked = _annotation(overview.png, f"Stage 1 attempt {attempt + 1}",
                                        displayed_target, point=stage1_point,
                                        crop_bounds=displayed_crop)
            stage1_marked.save(artifacts / f"attempt-{attempt + 1}-stage1-marked.png")
            attempt_record = {"stage1": {"wire_xy": [float(decision1.x), float(decision1.y)],
                "zoom": decision1.zoom, "target_inside_crop": crop_contains,
                "source_crop": [crop.x0, crop.y0, crop.x1, crop.y1], "model_trace": trace1}}
            report["attempts"].append(attempt_record)
            phase = f"stage2_attempt_{attempt + 1}"
            detail = renderer.detail(crop, config)
            stage2_encoded = encode_stage(describe_stage(
                "stage2", detail, crop, request,
                grid_label_placement=config.grid_label_placement, stage1_zoom=config.stage1_zoom), detail)
            detail_path = artifacts / f"attempt-{attempt + 1}-stage2-input.png"
            _save_input(stage2_encoded, detail.png, detail_path)
            (artifacts / f"attempt-{attempt + 1}-stage2-prompt.txt").write_text(
                stage2_encoded.messages[0]["content"][0]["text"] + "\n")
            raw2, trace2 = infer(api, actual_model, stage2_encoded,
                                "Return the final target point in this detail image without sending input.",
                                timeout=_check_deadline(deadline, phase))
            _check_deadline(deadline, phase)
            decision2 = decode_stage(stage2_encoded, raw2)
            attempt_record["stage2"] = {"model_trace": trace2}
            if isinstance(decision2, Reobserve):
                attempt_record["stage2"].update({"status": "reobserve", "reason": decision2.reason})
                attempt_record["outcome"] = "bounded_reobserve"
                if attempt < max_reobserves:
                    phase = "bounded_reobserve"
                    retry_feedback = {"rough_xy": (decision1.x, decision1.y),
                                      "crop_rect": (crop.x0, crop.y0, crop.x1, crop.y1),
                                      "source_size": (frame.width, frame.height),
                                      "reobserve_reason": decision2.reason}
                    request_new_test_frame()
                    frame, fresh_bounds = _capture_visible_target(
                        native, binding, root, surface_geometry, overall_deadline=deadline,
                        refresh_surface=request_new_test_frame)
                    _assert_surface_stable(canvas, surface_geometry)
                    _check_deadline(deadline, phase)
                    if fresh_bounds != target_bounds:
                        raise ProbeError("TEST_TARGET_CHANGED", phase)
                    continue
                raise ProbeError("REOBSERVE_LIMIT_REACHED", phase)
            if not isinstance(decision2, InteractDetail):
                raise ProbeError("INVALID_STAGE2_DECISION", phase)
            click_point = approve_click_point(decision2, crop, target_bounds, binding,
                                              frame.width, frame.height)
            source_nx, source_ny = local_to_monitor(crop, decision2.x, decision2.y)
            source_point = [float(source_nx * frame.width), float(source_ny * frame.height)]
            inner_target_bounds = _inner_target_bounds(target_bounds)
            stage2_point = (float(decision2.x / crop.local_width) * detail.width,
                            float(decision2.y / crop.local_height) * detail.height)
            target_detail = (
                (target_bounds[0] - crop.x0) / crop.crop_width * detail.width,
                (target_bounds[1] - crop.y0) / crop.crop_height * detail.height,
                (target_bounds[2] - crop.x0) / crop.crop_width * detail.width,
                (target_bounds[3] - crop.y0) / crop.crop_height * detail.height,
            )
            visible_target_detail = (
                max(0, int(target_detail[0])), max(0, int(target_detail[1])),
                min(detail.width, int(target_detail[2])), min(detail.height, int(target_detail[3])),
            )
            if visible_target_detail[0] >= visible_target_detail[2] or visible_target_detail[1] >= visible_target_detail[3]:
                visible_target_detail = None
            detail_marked = _annotation(detail.png, f"Stage 2 attempt {attempt + 1}",
                visible_target_detail,
                point=stage2_point)
            detail_marked.save(artifacts / f"attempt-{attempt + 1}-stage2-marked.png")
            attempt_record["stage2"].update({"wire_xy": [float(decision2.x), float(decision2.y)],
                "model_point_detail_image_px": list(stage2_point),
                "model_point_source_px": source_point,
                "target_outer_bounds_detail_normalized": _source_bounds_to_detail_normalized(target_bounds, crop),
                "target_inner_bounds_detail_normalized": _source_bounds_to_detail_normalized(inner_target_bounds, crop),
                "target_inner_bounds_source_px": list(inner_target_bounds),
                "target_outer_bounds_detail_image_px": list(target_detail),
                # Historical key was misnamed: retain its outer image-pixel values for report readers.
                "target_inner_bounds_detail": list(target_detail),
                "point_inside_target": click_point is not None})
            if not crop_contains:
                attempt_record["stage2"]["status"] = "target_absent_from_crop_model_point_ignored"
                attempt_record["outcome"] = "model_failed_to_reobserve_absent_target"
                raise ProbeError("MODEL_FAILED_TO_REOBSERVE_ABSENT_TARGET", phase)
            if click_point is None:
                attempt_record["outcome"] = "model_point_outside_target"
                raise ProbeError("MODEL_POINT_OUTSIDE_TEST_TARGET", phase)

            phase = "pre_dispatch_revalidation"
            _check_deadline(deadline, phase)
            with _seat_transaction(min(seat_lock_timeout_seconds, max(0.001, deadline - time.monotonic()))):
                refresh_started = time.monotonic()
                phase = "pre_dispatch_binding"
                _checked_binding(native, binding, deadline=deadline)
                phase = "pre_dispatch_frame"
                fresh_frame = _freeze_with_surface_refresh(
                    native, binding.binding_id, root, request_new_test_frame,
                    deadline=deadline, phase=phase,
                    captured_after=refresh_started, sequence_after=frame.sequence)
                _assert_surface_stable(canvas, surface_geometry)
                phase = "pre_dispatch_target"
                if (fresh_frame.width, fresh_frame.height) != (frame.width, frame.height):
                    raise ProbeError("GEOMETRY_CHANGED", phase)
                fresh_bounds = _detect_bound_target(fresh_frame, binding, surface_geometry)
                if fresh_bounds != target_bounds or fresh_frame.captured_monotonic < refresh_started:
                    raise ProbeError("TEST_TARGET_CHANGED", phase)
                fresh_point = approve_click_point(decision2, crop, fresh_bounds, binding,
                                                  fresh_frame.width, fresh_frame.height)
                if fresh_point is None or fresh_point != click_point:
                    raise ProbeError("PRE_DISPATCH_TARGET_GATE_FAILED", phase)
                _check_deadline(deadline, phase)

                # Drain queued Tk callbacks after the final native freeze, then
                # compare only events arriving during this transaction.
                root.update()
                _assert_surface_stable(canvas, surface_geometry)
                baseline_counts = dict(counts)
                phase = "isolated_test_click"
                click_attempted = True
                sent, attempted = _send_one_click(
                    native, binding, click_point,
                    deadline_seconds=min(3.0, _check_deadline(deadline, phase)))
                click_sent += sent
                report["input_events_sent"] = click_sent
                verified, event_deltas = _wait_for_test_click(
                    root, counts, baseline_counts, deadline=min(deadline, time.monotonic() + 2))
                report.update({"final_logical_point": list(click_point),
                    "input_outcome": "verified_in_test_target" if verified else "not_verified",
                    "test_window_events": event_deltas,
                    "click_events_attempted": attempted,
                    "target_bounds_revalidated": list(fresh_bounds),
                    "live_frame": "verified",
                    **transport_evidence(binding,
                        mapping_status="verified_in_test_window" if verified else "unverified",
                        input_status="verified_in_test_window" if verified else "unverified")})
            attempt_record["outcome"] = "click_verified" if verified else "click_not_verified"
            if not verified:
                report["code"] = "TEST_CLICK_NOT_VERIFIED"
            break
        if not click_attempted:
            raise ProbeError("TEST_CLICK_NOT_SENT", phase)
    except ClickDeliveryError as exc:
        click_sent = exc.sent_events
        report.update({"code": exc.code, "failure_phase": exc.phase,
            "error_domain": "input_mapping", "input_outcome": "delivery_unknown",
            "click_events_attempted": exc.attempted_events,
            "button_down_attempted": exc.button_down_attempted})
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
        report.update({"code": code, "failure_phase": getattr(exc, "phase", None) or phase,
            "error_domain": error_domain(code),
            "error": str(exc)[:300],
            "input_outcome": "delivery_unknown" if click_attempted else "not_run"})
        if isinstance(exc, ProbeError) and exc.diagnostics is not None:
            report["diagnostics"] = exc.diagnostics
            if surface_geometry is not None:
                report["diagnostics"]["test_surface"] = surface_geometry
    finally:
        report["input_events_sent"] = click_sent
        try:
            (artifacts / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        except OSError:
            pass
        try:
            if native is not None:
                native.close()
        finally:
            if root is not None:
                try:
                    root.destroy()
                except tk.TclError:
                    pass
            if supervisor is not None:
                supervisor.shutdown()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorize", action="store_true",
                        help="authorize capture and pointer access for this in-memory probe")
    parser.add_argument("--send-test-click", action="store_true",
                        help="explicitly allow one click, only after strict test-target gates pass")
    parser.add_argument("--helper-binary", required=True)
    parser.add_argument("--api", default="http://127.0.0.1:30000")
    parser.add_argument("--authorization-timeout", type=float, default=120)
    parser.add_argument("--transport-timeout", type=float, default=15)
    parser.add_argument("--max-reobserves", type=_retry_count, default=1)
    parser.add_argument("--target-side-px", type=_target_side, default=56,
                        help="logical-pixel side of the Tk target square")
    parser.add_argument("--diagnose-idle-seconds", type=float,
                        help="test fresh capture after an idle period without model inference or input")
    parser.add_argument("--diagnose-windowed", action="store_true",
                        help="in idle diagnostic mode, verify two fresh frames from the windowed Tk surface")
    parser.add_argument("--overall-timeout-seconds", type=lambda text: _bounded_int(
        text, lower=1, upper=900, name="--overall-timeout-seconds"), default=300,
        help="elapsed-time budget before input; a blocked socket may return later, but expired probes never send input")
    parser.add_argument("--seat-lock-timeout-seconds", type=lambda text: _bounded_int(
        text, lower=1, upper=10, name="--seat-lock-timeout-seconds"), default=3,
        help="maximum wait for the shared per-user input seat lock")
    args = parser.parse_args(argv)
    if not args.authorize:
        parser.error("--authorize is required")
    if args.diagnose_idle_seconds is None and not args.send_test_click:
        parser.error("--send-test-click is required outside idle diagnostic mode")
    if args.diagnose_idle_seconds is not None and args.send_test_click:
        parser.error("idle diagnostic mode must not include --send-test-click")
    if args.diagnose_windowed and args.diagnose_idle_seconds is None:
        parser.error("--diagnose-windowed requires --diagnose-idle-seconds")
    args.helper_binary = str(Path(args.helper_binary).expanduser())
    if not args.helper_binary.startswith("/") or "\x00" in args.helper_binary:
        parser.error("--helper-binary must be an absolute path")
    if not _local_api(args.api):
        parser.error("--api must point to a loopback SGLang endpoint")
    for name, value in (("authorization-timeout", args.authorization_timeout),
                        ("transport-timeout", args.transport_timeout)):
        if not 0.001 <= value <= 300:
            parser.error(f"--{name} must be between .001 and 300 seconds")
    result = run_probe(copy.deepcopy(get_config()), helper_binary=args.helper_binary,
        api=args.api, authorization_timeout=args.authorization_timeout,
        transport_timeout=args.transport_timeout, max_reobserves=args.max_reobserves,
        target_side_px=args.target_side_px,
        overall_timeout_seconds=args.overall_timeout_seconds,
        seat_lock_timeout_seconds=args.seat_lock_timeout_seconds,
        idle_diagnostic_seconds=args.diagnose_idle_seconds,
        windowed_diagnostic=args.diagnose_windowed)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if (result.get("input_outcome") == "verified_in_test_target"
                 or result.get("idle_frame_liveness") == "verified"
                 or result.get("windowed_frame_liveness") == "verified") else 1


if __name__ == "__main__":
    raise SystemExit(main())
