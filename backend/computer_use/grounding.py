"""Same-Agent desktop grounding state machine.

This module owns capture, crop and dispatch transitions only.  It deliberately
contains no model client and never opens a private VLM request.  The Agent
receives the observation image, emits the Stage-1/Stage-2 coordinates in its
own context, and the executable bridge calls these typed transitions.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable

from .contracts import Action, CanonicalFrame, DetailCrop, GroundingRequest, InspectRegion, InteractDetail
from .errors import ComputerUseError


@dataclass(frozen=True)
class ObservationArtifact:
    frame: CanonicalFrame
    overview: Any


@dataclass(frozen=True)
class DetailArtifact:
    frame: CanonicalFrame
    crop: DetailCrop
    detail: Any


class GroundingEngine:
    """Capture/crop/dispatch transitions driven by the same Agent context."""

    def __init__(
        self,
        *,
        config,
        frame_store,
        native,
        binding,
        renderer=None,
        clock=None,
        dispatcher=None,
        initial_frame=None,
    ):
        self.config = config
        self.frame_store = frame_store
        self.native = native
        self.binding = binding
        self.renderer = renderer
        self.clock = clock
        self.dispatcher = dispatcher
        self._initial_frame = initial_frame
        self._active_generations: dict[str, tuple[str, int, str | None]] = {}

    @property
    def active_request_ids(self) -> frozenset[str]:
        # Kept as a diagnostic compatibility property.  No upstream model
        # request IDs exist in the same-Agent design.
        return frozenset()

    async def observe(
        self,
        request: GroundingRequest,
        capability: object,
        check_abort: Callable[[], bool] | None = None,
    ) -> ObservationArtifact:
        self._admit(request, check_abort)
        frame = self._initial_frame
        self._initial_frame = None
        if frame is None:
            frame = self.native.freeze(request.binding_id)
        if inspect.isawaitable(frame):
            frame = await frame
        if not isinstance(frame, CanonicalFrame):
            raise ComputerUseError("INVALID_CAPTURE_RESULT", "native freeze did not return a canonical frame")
        if frame.binding.binding_id != request.binding_id or frame.binding.session_id != request.session_id:
            raise ComputerUseError("INVALID_CONTEXT_BINDING", "frame binding does not match request")
        self.frame_store.put(frame)
        overview = self.renderer.overview(frame, self.config) if self.renderer is not None else frame
        self._active_generations[request.session_id] = (frame.frame_id, frame.geometry_version, None)
        return ObservationArtifact(frame=frame, overview=overview)

    async def inspect_region(
        self,
        request: GroundingRequest,
        observation: ObservationArtifact,
        decision: InspectRegion,
        check_abort: Callable[[], bool] | None = None,
    ) -> DetailArtifact:
        self._admit(request, check_abort)
        self._validate_observation(request, observation)
        if not isinstance(decision, InspectRegion):
            raise ComputerUseError("INVALID_MODEL_OUTPUT", "Stage-1 must provide inspect_region coordinates")
        if self.renderer is None or not callable(getattr(self.renderer, "crop", None)):
            raise ComputerUseError("CAPTURE_NOT_READY", "detail crop renderer is not configured")
        crop = self.renderer.crop(observation.frame, decision, request, self.config)
        if not isinstance(crop, DetailCrop):
            raise ComputerUseError("INVALID_CROP_ID", "renderer did not return a trusted detail crop")
        if crop.frame_id != observation.frame.frame_id or crop.geometry_version != observation.frame.geometry_version:
            raise ComputerUseError("GEOMETRY_CHANGED", "detail crop is not bound to the observed frame")
        detail = self.renderer.detail(crop, self.config) if callable(getattr(self.renderer, "detail", None)) else crop
        self._active_generations[request.session_id] = (observation.frame.frame_id, observation.frame.geometry_version, crop.crop_id)
        return DetailArtifact(frame=observation.frame, crop=crop, detail=detail)

    async def interact_detail(
        self,
        request: GroundingRequest,
        detail: DetailArtifact,
        decision: InteractDetail,
        capability: object,
        check_abort: Callable[[], bool] | None = None,
    ):
        self._admit(request, check_abort)
        self._validate_observation(request, ObservationArtifact(detail.frame, None))
        if not isinstance(decision, InteractDetail):
            raise ComputerUseError("INVALID_MODEL_OUTPUT", "Stage-2 must provide precise local coordinates")
        if decision.action != request.action:
            raise ComputerUseError("INVALID_ACTION", "Stage-2 action differs from trusted request")
        if decision.crop_id != detail.crop.crop_id:
            raise ComputerUseError("INVALID_CROP_ID", "Stage-2 result is bound to another crop")
        if self.dispatcher is None:
            raise ComputerUseError("INPUT_DEVICE_NOT_READY", "dispatcher is not configured")
        return await self.dispatcher.dispatch(request, detail.crop, capability, decision=decision, check_abort=check_abort)

    async def run(
        self,
        request: GroundingRequest,
        capability: object,
        check_abort: Callable[[], bool] | None = None,
        *,
        stage1: InspectRegion | None = None,
        stage2: InteractDetail | None = None,
    ):
        """Run only explicitly supplied Agent decisions; never infer them."""
        observation = await self.observe(request, capability, check_abort)
        if stage1 is None:
            return observation
        detail = await self.inspect_region(request, observation, stage1, check_abort)
        if stage2 is None:
            return detail
        return await self.interact_detail(request, detail, stage2, capability, check_abort)

    def _admit(self, request: GroundingRequest, check_abort: Callable[[], bool] | None) -> None:
        if not getattr(self.config, "ready", False):
            raise ComputerUseError("CALIBRATION_REQUIRED", "computer-use configuration is not ready")
        if check_abort is not None and check_abort():
            raise ComputerUseError("CANCELLED", "computer-use request cancelled")
        if request.session_id != getattr(self.binding, "session_id", request.session_id):
            raise ComputerUseError("INVALID_CONTEXT_BINDING", "request session is not bound to input")
        if not getattr(self.binding, "ready", False):
            raise ComputerUseError("INPUT_DEVICE_NOT_READY", "input binding is not live and ready")

    def _validate_observation(self, request: GroundingRequest, observation: ObservationArtifact) -> None:
        frame = observation.frame
        active = self._active_generations.get(request.session_id)
        if active is None or active[:2] != (frame.frame_id, frame.geometry_version):
            raise ComputerUseError("STALE_FRAME", "observation is no longer the active generation")
        if frame.binding.binding_id != request.binding_id:
            raise ComputerUseError("INVALID_CONTEXT_BINDING", "observation binding mismatch")


__all__ = ["DetailArtifact", "GroundingEngine", "ObservationArtifact"]
