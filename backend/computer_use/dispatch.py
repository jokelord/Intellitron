"""At-most-once physical input dispatch with a seat-wide serialization lock."""
from __future__ import annotations

import asyncio
import errno
import fcntl
import inspect
import os
from pathlib import Path
import stat
import time
import threading
from dataclasses import dataclass, replace
from typing import Any

from .contracts import DispatchReceipt, DispatchState
from .errors import ComputerUseError, ERROR_CODES, NATIVE_DISPATCH_REASON_CODES
from .stale_check import check_freshness
from .coordinates import local_to_monitor, ratio_to_wayland, ratio_to_x11


@dataclass(frozen=True)
class Reservation:
    request_id: str
    crop_id: str
    previous_receipt: DispatchReceipt | None = None


class DispatchLedger:
    def __init__(self):
        self._entries: dict[str, DispatchReceipt] = {}
        self._inflight: set[str] = set()
        self._closed = False

    def reserve(self, request_id: str, crop_id: str) -> Reservation:
        if self._closed:
            raise ComputerUseError("SESSION_CLOSED", "dispatch binding is closed")
        if request_id in self._entries:
            return Reservation(request_id, crop_id, self._entries[request_id])
        if request_id in self._inflight:
            # A concurrent duplicate must wait on the same request rather than
            # beginning a second physical transaction.
            return Reservation(request_id, crop_id, None)
        self._inflight.add(request_id)
        return Reservation(request_id, crop_id, None)

    def finish(self, receipt: DispatchReceipt) -> None:
        self._inflight.discard(receipt.request_id)
        self._entries[receipt.request_id] = receipt

    def get(self, request_id: str) -> DispatchReceipt | None:
        return self._entries.get(request_id)

    def close(self) -> None:
        self._closed = True
        self._inflight.clear()


def _receipt(request, crop, state: str, last_event: int | None = None, cleanup_status: str = "complete", freshness=None):
    return DispatchReceipt(
        request_id=request.request_id,
        crop_id=crop.crop_id,
        state=state,
        last_possible_event=last_event,
        cleanup_status=cleanup_status,
        freshness=freshness or {},
    )


def _error_reason(exc):
    code = getattr(exc, 'code', None)
    return code if isinstance(code, str) and code in ERROR_CODES | NATIVE_DISPATCH_REASON_CODES else 'freshness_unavailable'


def _freshness_evidence(result):
    return {key: getattr(result, key, None) for key in
            ('age_seconds', 'observed_sequence', 'current_sequence', 'global_score', 'target_score')}


# Shared across runtimes and adapter-created event loops; no loop-bound lock.
_SEAT_LOCK = threading.Lock()


def _seat_lock_path() -> Path:
    """Return the per-UID lock path in the private system runtime directory."""
    return Path(f"/run/user/{os.getuid()}/llm-deployment-computer-use-seat.lock")


def _open_process_seat_lock() -> int:
    """Open a protected per-user lock file without following attacker paths."""
    path = _seat_lock_path()
    uid = os.getuid()
    try:
        parent_stat = os.stat(path.parent, follow_symlinks=False)
        if (not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != uid
                or parent_stat.st_mode & 0o077):
            raise OSError("runtime lock directory is not private and user-owned")
        if not path.is_absolute():
            raise OSError("runtime lock path must be absolute")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        file_stat = os.fstat(fd)
        if (not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != uid
                or file_stat.st_mode & 0o077 or file_stat.st_nlink != 1):
            os.close(fd)
            raise OSError("seat lock file is not a private user-owned regular file")
        return fd
    except OSError as exc:
        raise ComputerUseError("SEAT_LOCK_UNAVAILABLE", "cannot open the protected per-user seat lock") from exc


def _try_process_seat_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise ComputerUseError("SEAT_LOCK_UNAVAILABLE", "cannot acquire the per-user seat lock") from exc


def _release_process_seat_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

class _SeatGuard:
    def __init__(self):
        self._fd: int | None = None

    async def __aenter__(self):
        while not _SEAT_LOCK.acquire(blocking=False):
            await asyncio.sleep(.01)
        try:
            self._fd = _open_process_seat_lock()
            while not _try_process_seat_lock(self._fd):
                await asyncio.sleep(.01)
            return self
        except BaseException:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            _SEAT_LOCK.release()
            raise

    async def __aexit__(self, *args):
        try:
            if self._fd is not None:
                _release_process_seat_lock(self._fd)
                self._fd = None
        finally:
            _SEAT_LOCK.release()

class Dispatcher:
    def __init__(self, native, *, config, binding, frame_store=None, clock=time.monotonic, ledger=None):
        self.native = native
        self.config = config
        self.binding = binding
        self.frame_store = frame_store
        self.clock = clock
        self.ledger = ledger or DispatchLedger()
        self._seat_lock = _SeatGuard()
        self._event_index = 0
        self._stopped = False

    def register(self, request, crop) -> None:
        """Register immutable stage outputs before entering the seat lock."""
        if not hasattr(self, "_registered"):
            self._registered = {}
        self._registered[(request.request_id, crop.crop_id)] = (request, crop)

    async def dispatch(self, request=None, crop=None, capability=None, *, request_id=None, crop_id=None, decision=None, check_abort=None) -> DispatchReceipt:
        # The public service passes records directly.  The small fixture API
        # used by fault-injection tests may pass stable IDs after registering
        # those records; it never allows the model to invent records.
        if request is None and request_id is not None:
            candidates = getattr(self, "_registered", {})
            found = candidates.get((request_id, crop_id))
            if found is None:
                for (rid, _cid), value in candidates.items():
                    if rid == request_id:
                        found = value
                        break
            if found is None:
                raise ComputerUseError("INVALID_CONTEXT_BINDING", "unregistered dispatch identity")
            request, crop = found
        if request is None or crop is None:
            raise TypeError("dispatch requires a trusted request and crop")
        if self._stopped:
            raise ComputerUseError("SESSION_CLOSED", "dispatch binding is closed")
        if getattr(self.binding, "backend", None) not in {"wayland", "x11"}:
            receipt = _receipt(request, crop, "CANCELLED", cleanup_status="unsupported_backend")
            self.ledger.finish(receipt)
            return receipt
        previous = self.ledger.get(request.request_id)
        if previous is not None:
            return previous
        async with self._seat_lock:
            if self._stopped or (check_abort is not None and check_abort()):
                return _receipt(request, crop, "CANCELLED", cleanup_status="session_closed" if self._stopped else "user_cancelled")
            if capability is not None and not getattr(capability, "allow_desktop_input", False):
                return _receipt(request, crop, "CANCELLED", cleanup_status="desktop_input_disabled")
            previous = self.ledger.get(request.request_id)
            if previous is not None:
                return previous
            if capability is not None and getattr(capability, "session_id", None) != request.session_id:
                receipt = _receipt(request, crop, "INDETERMINATE", cleanup_status="capability_mismatch")
                self.ledger.finish(receipt)
                return receipt
            reservation = self.ledger.reserve(request.request_id, crop.crop_id)
            if reservation.previous_receipt is not None:
                return reservation.previous_receipt
            if getattr(crop, "binding_id", None) != getattr(self.binding, "binding_id", None):
                receipt = _receipt(request, crop, "CANCELLED", cleanup_status="binding_changed")
                self.ledger.finish(receipt)
                return receipt
            if getattr(crop, "geometry_version", None) != getattr(self.binding, "geometry_version", None):
                receipt = _receipt(request, crop, "CANCELLED", cleanup_status="geometry_changed")
                self.ledger.finish(receipt)
                return receipt
            if self.frame_store is None:
                receipt = _receipt(request, crop, "INDETERMINATE", cleanup_status="freshness_unavailable")
                self.ledger.finish(receipt)
                return receipt
            try:
                observed = self.frame_store.get(crop.frame_id, crop.binding_id, crop.geometry_version)
                binding = await self._call_native("resolve_binding")
                if binding != self.binding or not binding.ready:
                    raise ComputerUseError("GEOMETRY_CHANGED", "input binding changed")
                if observed.binding != binding or observed.stream_generation != binding.stream_generation:
                    raise ComputerUseError("GEOMETRY_CHANGED", "observation binding changed")
                if self.config.pre_action_validation == "binding_only":
                    # The model chose its point from the trusted crop. Scene
                    # changes and damage-driven frame age do not revoke that
                    # decision; native connection and geometry still must match.
                    deadline = self.clock() + self.config.max_validation_to_dispatch_seconds
                    receipt = await self._emit(request, crop, decision, deadline=deadline, check_abort=check_abort)
                    receipt = replace(receipt, pre_action_validation="binding_only")
                    self.ledger.finish(receipt)
                    return receipt
                current = await self._call_native("freeze", request.binding_id)
                freshness = check_freshness(observed, current, binding, self.clock(), self.config,
                                            crop=crop, clock=self.clock)
                if getattr(freshness, "state", None) != "FRESH":
                    # DispatchReceipt intentionally has a small terminal
                    # state set; freshness reason is retained in cleanup
                    # diagnostics rather than inventing a new receipt
                    # state that callers could mistake for delivery.
                    receipt = _receipt(request, crop, "CANCELLED", cleanup_status=getattr(freshness, "code", "freshness"),
                                       freshness=_freshness_evidence(freshness))
                    receipt = replace(receipt, pre_action_validation="frame_and_binding")
                    self.ledger.finish(receipt)
                    return receipt
            except Exception as exc:
                receipt = replace(_receipt(request, crop, "INDETERMINATE", cleanup_status=_error_reason(exc)),
                                  pre_action_validation=self.config.pre_action_validation)
                self.ledger.finish(receipt)
                return receipt
            receipt = await self._emit(request, crop, decision, deadline=freshness.validity_deadline,
                                       check_abort=check_abort)
            receipt = replace(receipt, freshness=_freshness_evidence(freshness), pre_action_validation="frame_and_binding")
            self.ledger.finish(receipt)
            return receipt

    async def _emit(self, request, crop, decision=None, *, deadline=None, check_abort=None):
        last_event = None
        held_button = None
        transaction_deadline = (deadline or self.clock()) + self.config.transport_timeout_seconds + 2 * self.config.double_click_interval_seconds

        async def send(payload):
            nonlocal last_event
            if payload["kind"] != "release_cleanup":
                if self._stopped or (check_abort is not None and check_abort()):
                    raise ComputerUseError("CANCELLED", "input cancelled")
                if last_event is None:
                    if deadline is None or self.clock() > deadline:
                        raise ComputerUseError("STALE_FRAME", "validation deadline expired")
                    if await self._call_native("resolve_binding") != self.binding:
                        raise ComputerUseError("GEOMETRY_CHANGED", "binding changed before input")
            self._event_index += 1
            last_event = self._event_index
            # The production client enforces the same deadline at its IPC boundary.
            method = self.native.send_event
            event_deadline = deadline if payload["kind"] == "motion" else transaction_deadline
            kwargs = ({"deadline_monotonic": event_deadline}
                      if "deadline_monotonic" in inspect.signature(method).parameters else {})
            result = method(request.binding_id, self.binding.geometry_version, payload, **kwargs)
            return await result if inspect.isawaitable(result) else result

        try:
            if decision is not None and hasattr(decision, "x") and hasattr(decision, "y"):
                nx, ny = local_to_monitor(crop, decision.x, decision.y)
                if getattr(self.binding, "backend", "") == "x11":
                    point = ratio_to_x11(nx, ny, self.binding.origin, *self.binding.size)
                elif getattr(self.binding, "backend", None) == "wayland":
                    point = ratio_to_wayland(nx, ny, self.binding.origin, *self.binding.size)
                else:  # defensive; the public backend set is checked above
                    raise ComputerUseError("INPUT_REGION_MISMATCH", "unsupported input backend")
            else:
                # A dispatcher may be used by a pre-dispatch fault fixture;
                # production grounding always supplies the typed Stage-2
                # decision above.
                point = (crop.rough_x, crop.rough_y)
            await send({"kind": "motion", "x": point[0], "y": point[1]})
            action = request.action.value if hasattr(request.action, "value") else request.action
            if action.startswith("left_"):
                held_button = "left"
            elif action.startswith("right_"):
                held_button = "right"
            if action.startswith("scroll_"):
                units = 1 if action in {"scroll_up", "scroll_up_1"} else -1
                for _ in range(getattr(decision, "steps", 1)):
                    await send({"kind": "wheel_step", "units": units})
            else:
                for click in range(2 if "double" in action else 1):
                    await send({"kind": "button_down", "button": held_button})
                    await send({"kind": "button_up", "button": held_button})
                    if click == 0 and "double" in action:
                        await asyncio.sleep(float(self.config.double_click_interval_seconds))
            receipt = _receipt(request, crop, "ACTION_SENT", last_event)
        except asyncio.CancelledError:
            receipt = _receipt(request, crop, "ACTION_DELIVERY_UNKNOWN" if last_event is not None else "CANCELLED", last_event, "cancelled")
        except Exception as exc:
            receipt = _receipt(request, crop, "ACTION_DELIVERY_UNKNOWN" if last_event is not None else "INDETERMINATE", last_event, _error_reason(exc))
        finally:
            if held_button is not None and last_event is not None:
                try:
                    await send({"kind": "release_cleanup", "button": held_button})
                except Exception:
                    if receipt.state == "ACTION_SENT":
                        receipt = _receipt(request, crop, "ACTION_DELIVERY_UNKNOWN", last_event, "release_uncertain")
            self.ledger.finish(receipt)
        return receipt

    async def _call_native(self, method: str, *args):
        fn = getattr(self.native, method)
        result = fn(*args)
        if inspect.isawaitable(result):
            return await result
        return result

    def stop(self):
        self._stopped = True
        self.ledger.close()

    async def dispose(self):
        self.stop()


__all__ = ["DispatchLedger", "Dispatcher", "Reservation"]
