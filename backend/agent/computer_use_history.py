"""Trusted, copy-on-write projection for historical computer-use images.

Only an in-process verified executable bridge may call
``mark_computer_use_observation``.  Textual provenance and ordinary PIL image
attributes do not satisfy the private issuer identity checked here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from smolagents.memory import ActionStep


OMISSION_REASON = "computer_use_historical_image_omission"
PROVENANCE = "computer_use_observation"
_ISSUER = object()
_MARKER_ATTR = "_trusted_computer_use_observation"
_PLACEHOLDER_ATTR = "_computer_use_history_placeholder"
AUTO_PROJECTION = object()


@dataclass(frozen=True, slots=True)
class ComputerUseObservationProvenance:
    provenance: str
    context_id: str
    observation_id: str
    geometry_version: int
    _issuer: object


@dataclass(frozen=True, slots=True)
class _Replacement:
    step: ActionStep
    original_container: Any
    original_images: tuple[Any, ...]
    placeholder: str


@dataclass(frozen=True, slots=True)
class StagedComputerUseHistoryOmission:
    session: Any
    memory: Any
    context_id: str
    replacements: tuple[_Replacement, ...]
    latest_step: ActionStep
    signature: tuple[tuple[int, tuple[int, ...]], ...]

    @property
    def omitted_image_count(self) -> int:
        return sum(len(item.original_images) for item in self.replacements)

    @property
    def omitted_block_count(self) -> int:
        return len(self.replacements)


def mark_computer_use_observation(
    image: Any,
    *,
    context_id: str,
    observation_id: str,
    geometry_version: int,
) -> Any:
    """Mark the exact verified PIL object attached by the trusted runtime."""

    if not isinstance(context_id, str) or not context_id:
        raise ValueError("context_id must be a non-empty string")
    if not isinstance(observation_id, str) or not observation_id:
        raise ValueError("observation_id must be a non-empty string")
    if isinstance(geometry_version, bool) or not isinstance(geometry_version, int) or geometry_version < 0:
        raise ValueError("geometry_version must be a non-negative integer")
    try:
        from PIL import Image as PILImage

        if not isinstance(image, PILImage.Image):
            raise TypeError("computer-use observation must be a PIL image")
    except ImportError as exc:
        raise RuntimeError("Pillow is required for computer-use observations") from exc
    setattr(
        image,
        _MARKER_ATTR,
        ComputerUseObservationProvenance(
            provenance=PROVENANCE,
            context_id=context_id,
            observation_id=observation_id,
            geometry_version=geometry_version,
            _issuer=_ISSUER,
        ),
    )
    return image


def computer_use_provenance(image: Any, context_id: str) -> ComputerUseObservationProvenance | None:
    marker = getattr(image, _MARKER_ATTR, None)
    if (
        isinstance(marker, ComputerUseObservationProvenance)
        and marker._issuer is _ISSUER
        and marker.provenance == PROVENANCE
        and marker.context_id == context_id
    ):
        return marker
    return None


def trusted_computer_use_images(images: Any, context_id: str) -> tuple[Any, ...]:
    if not isinstance(images, (list, tuple)):
        return ()
    return tuple(image for image in images if computer_use_provenance(image, context_id) is not None)


def _eligible_blocks(
    session: Any,
    pending_step: ActionStep | None = None,
) -> tuple[tuple[ActionStep, tuple[Any, ...]], ...]:
    if session is None or bool(getattr(session, "is_child_context", False)):
        return ()
    context_id = getattr(session, "session_id", None)
    memory = getattr(getattr(session, "agent", None), "memory", None)
    steps = getattr(memory, "steps", None)
    if not isinstance(context_id, str) or not isinstance(steps, list):
        return ()
    blocks: list[tuple[ActionStep, tuple[Any, ...]]] = []
    candidates = list(steps)
    if pending_step is not None and all(step is not pending_step for step in candidates):
        candidates.append(pending_step)
    for step in candidates:
        if not isinstance(step, ActionStep):
            continue
        images = trusted_computer_use_images(step.observations_images, context_id)
        if images:
            blocks.append((step, images))
    return tuple(blocks)


def _signature(blocks: tuple[tuple[ActionStep, tuple[Any, ...]], ...]) -> tuple[tuple[int, tuple[int, ...]], ...]:
    return tuple((id(step), tuple(id(image) for image in images)) for step, images in blocks)


def _placeholder(images: tuple[Any, ...]) -> str:
    return (
        f"[historical computer-use images omitted: count={len(images)}; "
        f"provenance={PROVENANCE}]"
    )


def stage_computer_use_history_omission(
    session: Any,
    *,
    pending_step: ActionStep | None = None,
) -> StagedComputerUseHistoryOmission | None:
    """Stage latest-only projection without mutating canonical Agent memory."""

    blocks = _eligible_blocks(session, pending_step=pending_step)
    if len(blocks) < 2:
        return None
    signature = _signature(blocks)
    current = getattr(session, "computer_use_history_omission_stage", None)
    if (isinstance(current, StagedComputerUseHistoryOmission)
            and current.signature == signature
            and current.memory is session.agent.memory):
        return current
    memory = session.agent.memory
    replacements = tuple(
        _Replacement(
            step=step,
            original_container=step.observations_images,
            original_images=images,
            placeholder=_placeholder(images),
        )
        for step, images in blocks[:-1]
    )
    stage = StagedComputerUseHistoryOmission(
        session=session,
        memory=memory,
        context_id=session.session_id,
        replacements=replacements,
        latest_step=blocks[-1][0],
        signature=signature,
    )
    session.computer_use_history_omission_stage = stage
    return stage


def restage_computer_use_history_for_same_blocks(session):
    """Carry pending permission across a profile memory wrapper, never compact.

    Only the same canonical ActionSteps/images and unchanged replacement
    containers qualify. This changes a pending candidate, not history;
    acceptance still commits it.
    """
    stage = getattr(session, "computer_use_history_omission_stage", None)
    memory = getattr(getattr(session, "agent", None), "memory", None)
    if (not isinstance(stage, StagedComputerUseHistoryOmission)
            or stage.session is not session
            or stage.context_id != getattr(session, "session_id", None)
            or stage.memory is memory
            or _signature(_eligible_blocks(session)) != stage.signature
            or any(item.step.observations_images is not item.original_container
                   for item in stage.replacements)):
        return
    stage_computer_use_history_omission(session)


def computer_use_history_stage_for_request(session):
    """Select an unchanged candidate belonging to the memory being serialized.

    Compact/profile boundaries leave the old pending permission untouched, but
    an unrelated memory must never project or accept that permission.
    """
    stage = getattr(session, "computer_use_history_omission_stage", None)
    if (not isinstance(stage, StagedComputerUseHistoryOmission)
            or stage.session is not session or stage.context_id != getattr(session, "session_id", None)
            or stage.memory is not getattr(getattr(session, "agent", None), "memory", None)
            or _signature(_eligible_blocks(session)) != stage.signature):
        return None
    return stage


def bind_computer_use_history_request(session, stage, messages):
    """Require the exact candidate's placeholders/latest image in this request."""
    if stage is None or computer_use_history_stage_for_request(session) is not stage:
        return None
    from collections import Counter
    images = []
    texts = []
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image":
                    images.append(part.get("image"))
                elif isinstance(part, dict) and part.get("type") == "text":
                    texts.append(part.get("text"))
    image_ids = {id(image) for image in images}
    latest = trusted_computer_use_images(stage.latest_step.observations_images, stage.context_id)
    if not latest or not all(id(image) in image_ids for image in latest):
        return None
    if any(id(image) in image_ids for item in stage.replacements for image in item.original_images):
        return None
    required = Counter(item.placeholder for item in stage.replacements)
    supplied = Counter(text for text in texts if isinstance(text, str))
    if any(supplied[text] < count for text, count in required.items()):
        return None
    return stage


def _replacement_for_step(session: Any, step: ActionStep, stage=AUTO_PROJECTION) -> _Replacement | None:
    if stage is AUTO_PROJECTION:
        if not bool(getattr(session, "computer_use_history_projection_enabled", False)):
            return None
        stage = getattr(session, "computer_use_history_omission_stage", None)
    if not isinstance(stage, StagedComputerUseHistoryOmission):
        return None
    if stage.session is not session or stage.context_id != getattr(session, "session_id", None):
        return None
    return next((item for item in stage.replacements if item.step is step), None)


def projected_action_observation(
    session: Any,
    step: ActionStep,
    *,
    stage=AUTO_PROJECTION,
) -> tuple[str, tuple[Any, ...] | str] | None:
    """Return one serialized image block or placeholder for an ActionStep."""

    committed = getattr(step, _PLACEHOLDER_ATTR, None)
    if isinstance(committed, str):
        return ("placeholder", committed)
    replacement = _replacement_for_step(session, step, stage)
    if replacement is not None:
        return ("placeholder", replacement.placeholder)
    context_id = getattr(session, "session_id", "")
    images = trusted_computer_use_images(step.observations_images, context_id)
    if images:
        return ("images", images)
    return None


def commit_computer_use_history_omission(
    session: Any,
    stage: StagedComputerUseHistoryOmission | None = None,
) -> bool:
    """Commit an exact staged projection after the ordinary transport barrier."""

    current = getattr(session, "computer_use_history_omission_stage", None)
    stage = stage or current
    if not isinstance(stage, StagedComputerUseHistoryOmission) or current is not stage:
        return False
    if stage.session is not session or stage.context_id != getattr(session, "session_id", None):
        return False
    if session.agent.memory is not stage.memory or _signature(_eligible_blocks(session)) != stage.signature:
        raise RuntimeError("computer-use history changed before omission commit")
    for item in stage.replacements:
        if item.step.observations_images is not item.original_container:
            raise RuntimeError("computer-use image container changed before omission commit")
        omitted_ids = {id(image) for image in item.original_images}
        item.step.observations_images = [
            image
            for image in item.original_container
            if id(image) not in omitted_ids
        ]
        setattr(item.step, _PLACEHOLDER_ATTR, item.placeholder)
    session.computer_use_history_omission_stage = None
    records = getattr(session, "behavior_records", None)
    if isinstance(records, dict):
        records.setdefault("prefix_boundaries", []).append(
            {
                "reason": OMISSION_REASON,
                "omitted_image_count": stage.omitted_image_count,
                "omitted_block_count": stage.omitted_block_count,
            }
        )
    return True


def rollback_computer_use_history_omission(
    session: Any,
    stage: StagedComputerUseHistoryOmission | None = None,
) -> bool:
    """Discard only the candidate projection; canonical memory was untouched."""

    current = getattr(session, "computer_use_history_omission_stage", None)
    stage = stage or current
    if not isinstance(stage, StagedComputerUseHistoryOmission) or current is not stage:
        return False
    session.computer_use_history_omission_stage = None
    return True
