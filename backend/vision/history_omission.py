"""Copy-on-write historical image omission for top-level Agent memory."""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass, fields, is_dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from typing import Any

from smolagents.memory import TaskStep
from smolagents.models import ChatMessage, MessageRole

from .contracts import VisionAutomationBinding, VisionAutomationTransportMetadata
from .validation import VisionRequestValidationError


logger = logging.getLogger(__name__)

OMISSION_REASON = "vision_automation_historical_image_omission"
_DIAGNOSTIC_LIMIT = 64
_MANUAL_RE = re.compile(
    r"\AManual images: (0|[1-9]\d{0,2})"
    r"(?:, image indexes ([1-9]\d{0,2})-([1-9]\d{0,2}))?\Z"
)
_SOURCE_RE = re.compile(
    r"\ASource ([1-9]\d{0,2}): (Desktop|Camera ([1-9]\d{0,2}))"
    r"(?:, image indexes ([1-9]\d{0,2})-([1-9]\d{0,2}))?\Z"
)
_RELATIVE_RE = re.compile(
    r"\ARelative times: ?((?:0|[1-9]\d{0,8})\.\d{2}s"
    r"(?:, (?:0|[1-9]\d{0,8})\.\d{2}s)*)?\Z"
)
_SOURCE_ALIAS_RE = re.compile(r"\Acamera_([1-9]\d*)\Z")


@dataclass
class HistoricalImagePlaceholderTaskStep(TaskStep):
    """A canonical TaskStep whose former image container is now one text part."""

    historical_image_placeholder: str = ""

    def to_messages(self, summary_mode: bool = False) -> list[ChatMessage]:
        content = [
            {"type": "text", "text": f"New task:\n{self.task}"},
            {"type": "text", "text": self.historical_image_placeholder},
        ]
        return [ChatMessage(role=MessageRole.USER, content=content)]


@dataclass(frozen=True, slots=True)
class VisionAutomationPinnedBinding:
    page_generation: int
    mode: str
    top_level_context_id: str
    automation_task_id: str
    capture_cycle_id: str


@dataclass(frozen=True, slots=True)
class VisionAutomationCleanupTokenState:
    token_digest: str
    page_generation: int
    mode: str
    top_level_context_id: str
    automation_task_id: str
    capture_cycle_id: str
    consumed: bool = False
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class _TaskImageReplacement:
    index: int
    original_step: TaskStep
    original_task: str
    original_container: list[Any] | tuple[Any, ...]
    original_images: tuple[Any, ...]
    replacement: HistoricalImagePlaceholderTaskStep
    manifest_fallback: bool


@dataclass(frozen=True, slots=True)
class StagedAgentHistoryImageOmission:
    session: Any
    memory: Any
    original_steps_container: list[Any]
    original_steps: tuple[Any, ...]
    original_step_snapshots: tuple[Any, ...]
    projected_steps: tuple[Any, ...]
    replacements: tuple[_TaskImageReplacement, ...]
    metadata: VisionAutomationTransportMetadata | None
    previous_binding: Any
    next_binding: VisionAutomationPinnedBinding | None
    previous_cleanup_token: Any
    next_cleanup_token: VisionAutomationCleanupTokenState | None
    previous_sticky_omission: bool
    next_sticky_omission: bool
    previous_compactable_messages: Any
    previous_compactable_agent_name: Any
    previous_prompt_snapshot: Any
    previous_diagnostics_present: bool
    previous_diagnostics: Any

    @property
    def omitted_image_count(self) -> int:
        return sum(len(item.original_images) for item in self.replacements)

    @property
    def omitted_block_count(self) -> int:
        return len(self.replacements)


def _image_identity_snapshot(value: Any) -> tuple[Any, ...]:
    return (
        "image",
        type(value).__module__,
        type(value).__qualname__,
        id(value),
    )


def _image_container_snapshot(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ("image_container", None)
    if not isinstance(value, (list, tuple)):
        return ("invalid_image_container", type(value).__qualname__, id(value))
    return (
        "image_container",
        type(value).__qualname__,
        id(value),
        tuple(_image_identity_snapshot(image) for image in value),
    )


def _preserved_value_snapshot(
    value: Any,
    seen: set[int] | None = None,
) -> Any:
    """Snapshot mutable prompt fields without reading or copying image pixels."""

    if value is None or isinstance(value, (bool, int, str)):
        return (type(value).__qualname__, value)
    if isinstance(value, float):
        if math.isnan(value):
            return ("float", "nan")
        if math.isinf(value):
            return ("float", "inf" if value > 0 else "-inf")
        return ("float", value)
    if isinstance(value, Enum):
        return ("enum", type(value).__module__, type(value).__qualname__, value.value)
    if isinstance(value, bytes):
        return ("bytes", id(value), len(value), hashlib.sha256(value).digest())
    if isinstance(value, bytearray):
        return ("bytearray", id(value), len(value), hashlib.sha256(value).digest())

    try:
        from PIL import Image as PILImage

        if isinstance(value, PILImage.Image):
            return _image_identity_snapshot(value)
    except ImportError:
        pass

    if seen is None:
        seen = set()
    value_id = id(value)
    if value_id in seen:
        return ("shared_reference", type(value).__qualname__, value_id)
    seen.add(value_id)

    if isinstance(value, (list, tuple)):
        return (
            type(value).__qualname__,
            value_id,
            tuple(_preserved_value_snapshot(item, seen) for item in value),
        )
    if isinstance(value, dict):
        return (
            "dict",
            value_id,
            tuple(
                (
                    _preserved_value_snapshot(key, seen),
                    _preserved_value_snapshot(item, seen),
                )
                for key, item in value.items()
            ),
        )
    if isinstance(value, (set, frozenset)):
        snapshots = [_preserved_value_snapshot(item, seen) for item in value]
        return (type(value).__qualname__, value_id, tuple(sorted(snapshots, key=repr)))
    if is_dataclass(value) and not isinstance(value, type):
        return (
            "dataclass",
            type(value).__module__,
            type(value).__qualname__,
            value_id,
            tuple(
                (
                    field_info.name,
                    _preserved_value_snapshot(getattr(value, field_info.name), seen),
                )
                for field_info in fields(value)
            ),
        )
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return (
            "object",
            type(value).__module__,
            type(value).__qualname__,
            value_id,
            tuple(
                (name, _preserved_value_snapshot(item, seen))
                for name, item in sorted(attributes.items())
            ),
        )
    return ("identity", type(value).__module__, type(value).__qualname__, value_id)


def _memory_step_snapshot(step: Any) -> Any:
    if isinstance(step, TaskStep):
        task_fields = []
        for field_info in fields(step):
            value = getattr(step, field_info.name)
            snapshot = (
                _image_container_snapshot(value)
                if field_info.name == "task_images"
                else _preserved_value_snapshot(value)
            )
            task_fields.append((field_info.name, snapshot))
        return (
            "task_step",
            type(step).__module__,
            type(step).__qualname__,
            id(step),
            tuple(task_fields),
        )
    return _preserved_value_snapshot(step)


def _quantized_seconds(value: float) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("relative range values must be finite numbers")
    if not math.isfinite(value) or value < 0:
        raise ValueError("relative range values must be finite non-negative numbers")
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _normalized_sources(sources: tuple[str, ...]) -> tuple[str, ...]:
    unique = set(sources)
    for source in unique:
        if source not in {"manual", "desktop"} and _SOURCE_ALIAS_RE.fullmatch(source) is None:
            raise ValueError("sources must use stable internal aliases")

    def rank(source: str) -> tuple[int, int]:
        if source == "manual":
            return (0, 0)
        if source == "desktop":
            return (1, 0)
        match = _SOURCE_ALIAS_RE.fullmatch(source)
        assert match is not None
        return (2, int(match.group(1)))

    return tuple(sorted(unique, key=rank))


def format_historical_image_placeholder(
    *,
    count: int,
    sources: tuple[str, ...],
    relative_start: float | None,
    relative_end: float | None,
) -> str:
    """Format the shared Chat/Agent deterministic omission grammar."""

    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("count must be a non-negative integer")
    normalized_sources = _normalized_sources(sources)
    source_text = ",".join(normalized_sources) if normalized_sources else "none"
    if relative_start is None and relative_end is None:
        range_text = "unavailable"
    elif relative_start is None or relative_end is None:
        raise ValueError("relative range must provide both endpoints")
    else:
        start_text = _quantized_seconds(relative_start)
        end_text = _quantized_seconds(relative_end)
        if Decimal(start_text) > Decimal(end_text):
            raise ValueError("relative range must be ordered")
        range_text = f"{start_text}..{end_text}s"
    return (
        f"[historical images omitted: count={count}; sources={source_text}; "
        f"relative_range={range_text}]"
    )


def _manifest_provenance(
    task: str,
    actual_image_count: int,
) -> tuple[tuple[str, ...], float | None, float | None] | None:
    opening = "<vision_automation_manifest>\n"
    closing = "\n</vision_automation_manifest>"
    if not isinstance(task, str) or not task.startswith(opening) or "\r" in task:
        return None
    closing_index = task.find(closing, len(opening))
    if closing_index < 0:
        return None
    suffix = task[closing_index + len(closing) :]
    if suffix and not suffix.startswith("\n\n"):
        return None
    lines = task[len(opening) : closing_index].split("\n")
    if len(lines) < 2:
        return None

    manual_match = _MANUAL_RE.fullmatch(lines[0])
    relative_match = _RELATIVE_RE.fullmatch(lines[-1])
    if manual_match is None or relative_match is None:
        return None

    manual_count = int(manual_match.group(1))
    next_image_index = 1
    if manual_count:
        if (
            manual_match.group(2) is None
            or int(manual_match.group(2)) != next_image_index
            or int(manual_match.group(3)) != next_image_index + manual_count - 1
        ):
            return None
        next_image_index += manual_count
    elif manual_match.group(2) is not None:
        return None

    relative_text = relative_match.group(1)
    relative_values: list[Decimal] = []
    if relative_text:
        try:
            relative_values = [
                Decimal(item[:-1]) for item in relative_text.split(", ")
            ]
        except InvalidOperation:
            return None
        if any(value < 0 for value in relative_values):
            return None
        if any(left > right for left, right in zip(relative_values, relative_values[1:])):
            return None

    aliases: list[str] = []
    previous_source_rank: tuple[int, int] | None = None
    source_lines = lines[1:-1]
    for expected_number, line in enumerate(source_lines, start=1):
        source_match = _SOURCE_RE.fullmatch(line)
        if source_match is None or int(source_match.group(1)) != expected_number:
            return None
        if source_match.group(2) == "Desktop":
            alias = "desktop"
            source_rank = (0, 0)
        else:
            camera_number = int(source_match.group(3))
            alias = f"camera_{camera_number}"
            source_rank = (1, camera_number)
        if previous_source_rank is not None and source_rank <= previous_source_rank:
            return None
        previous_source_rank = source_rank
        aliases.append(alias)

        image_count = len(relative_values)
        range_start = source_match.group(4)
        range_end = source_match.group(5)
        if image_count:
            if (
                range_start is None
                or int(range_start) != next_image_index
                or int(range_end) != next_image_index + image_count - 1
            ):
                return None
            next_image_index += image_count
        elif range_start is not None:
            return None

    if bool(aliases) != bool(relative_values):
        return None
    if next_image_index - 1 != actual_image_count:
        return None
    sources = tuple((["manual"] if manual_count else []) + aliases)
    if not relative_values:
        return sources, None, None
    return sources, float(relative_values[0]), float(relative_values[-1])


def _pinned_binding(binding: VisionAutomationBinding) -> VisionAutomationPinnedBinding:
    return VisionAutomationPinnedBinding(
        page_generation=binding.page_generation,
        mode=binding.mode,
        top_level_context_id=binding.top_level_context_id,
        automation_task_id=binding.automation_task_id,
        capture_cycle_id=binding.capture_cycle_id,
    )


def _binding_matches_pin(
    binding: VisionAutomationBinding,
    pinned: VisionAutomationPinnedBinding,
) -> bool:
    return (
        binding.page_generation == pinned.page_generation
        and binding.mode == pinned.mode
        and binding.top_level_context_id == pinned.top_level_context_id
        and binding.automation_task_id == pinned.automation_task_id
        and binding.capture_cycle_id == pinned.capture_cycle_id
    )


def _cleanup_state_matches_pin(
    state: VisionAutomationCleanupTokenState,
    pinned: VisionAutomationPinnedBinding,
) -> bool:
    return (
        state.page_generation == pinned.page_generation
        and state.mode == pinned.mode
        and state.top_level_context_id == pinned.top_level_context_id
        and state.automation_task_id == pinned.automation_task_id
        and state.capture_cycle_id == pinned.capture_cycle_id
    )


def register_agent_vision_automation_cleanup(
    session: Any,
    binding: VisionAutomationBinding,
    cleanup_token: str,
) -> VisionAutomationCleanupTokenState:
    """Register one hashed cleanup permission against the exact pinned task."""

    if getattr(session, "is_child_context", False):
        raise VisionRequestValidationError("vision automation cannot target a child Agent session")
    if binding.mode != "agent":
        raise VisionRequestValidationError("Agent vision automation binding mode must be agent")
    if binding.top_level_context_id != getattr(session, "session_id", None):
        raise VisionRequestValidationError("vision automation top-level context mismatch")
    if binding.cleanup_token is not None:
        raise VisionRequestValidationError("cleanup registration requires the prior binding")
    pinned = getattr(session, "vision_automation_binding", None)
    if pinned is None or not _binding_matches_pin(binding, pinned):
        raise VisionRequestValidationError("cleanup registration binding does not match the exact prior binding")

    token_digest = hashlib.sha256(cleanup_token.encode("utf-8")).hexdigest()
    current = getattr(session, "vision_automation_cleanup_token", None)
    if (
        current is not None
        and current.consumed
        and current.token_digest == token_digest
    ):
        raise VisionRequestValidationError("cleanup token was already consumed")
    if current is not None and current.cancelled:
        if _cleanup_state_matches_pin(current, pinned):
            if current.token_digest == token_digest:
                return current
            raise VisionRequestValidationError(
                "a different cleanup token was already cancelled"
            )
    elif current is not None and not current.consumed:
        if (
            current.token_digest == token_digest
            and current.page_generation == pinned.page_generation
            and current.mode == pinned.mode
            and current.top_level_context_id == pinned.top_level_context_id
            and current.automation_task_id == pinned.automation_task_id
            and current.capture_cycle_id == pinned.capture_cycle_id
        ):
            return current
        raise VisionRequestValidationError("a different cleanup token is already pending")

    registered = VisionAutomationCleanupTokenState(
        token_digest=token_digest,
        page_generation=pinned.page_generation,
        mode=pinned.mode,
        top_level_context_id=pinned.top_level_context_id,
        automation_task_id=pinned.automation_task_id,
        capture_cycle_id=pinned.capture_cycle_id,
    )
    session.vision_automation_cleanup_token = registered
    return registered


def cancel_agent_vision_automation_cleanup(
    session: Any,
    binding: VisionAutomationBinding,
    cleanup_token: str,
) -> VisionAutomationCleanupTokenState:
    """Cancel only the exact pending cleanup token, retaining a hashed tombstone."""

    if getattr(session, "is_child_context", False):
        raise VisionRequestValidationError("vision automation cannot target a child Agent session")
    if binding.mode != "agent":
        raise VisionRequestValidationError("Agent vision automation binding mode must be agent")
    if binding.top_level_context_id != getattr(session, "session_id", None):
        raise VisionRequestValidationError("vision automation top-level context mismatch")
    if binding.cleanup_token is not None:
        raise VisionRequestValidationError("cleanup cancellation requires the prior binding")

    token_digest = hashlib.sha256(cleanup_token.encode("utf-8")).hexdigest()
    current = getattr(session, "vision_automation_cleanup_token", None)
    if current is not None and _cleanup_state_matches_pin(
        current, _pinned_binding(binding)
    ):
        if token_digest != current.token_digest:
            raise VisionRequestValidationError(
                "cleanup cancellation token does not match registration"
            )
        if current.consumed:
            raise VisionRequestValidationError("cleanup token was already consumed")
        if current.cancelled:
            return current
        cancelled = replace(current, cancelled=True)
        session.vision_automation_cleanup_token = cancelled
        return cancelled

    pinned = getattr(session, "vision_automation_binding", None)
    if pinned is None or not _binding_matches_pin(binding, pinned):
        raise VisionRequestValidationError(
            "cleanup cancellation binding does not match the exact prior binding"
        )
    if current is None or not _cleanup_state_matches_pin(current, pinned):
        cancelled = VisionAutomationCleanupTokenState(
            token_digest=token_digest,
            page_generation=pinned.page_generation,
            mode=pinned.mode,
            top_level_context_id=pinned.top_level_context_id,
            automation_task_id=pinned.automation_task_id,
            capture_cycle_id=pinned.capture_cycle_id,
            cancelled=True,
        )
        session.vision_automation_cleanup_token = cancelled
        return cancelled
    raise AssertionError("exact cleanup state should have returned before pin validation")


def _validate_binding(session: Any, binding: VisionAutomationBinding) -> Any:
    if getattr(session, "is_child_context", False):
        raise VisionRequestValidationError("vision automation cannot target a child Agent session")
    if binding.mode != "agent":
        raise VisionRequestValidationError("Agent vision automation binding mode must be agent")
    if binding.top_level_context_id != getattr(session, "session_id", None):
        raise VisionRequestValidationError("vision automation top-level context mismatch")

    previous = getattr(session, "vision_automation_binding", None)
    cleanup_state = getattr(session, "vision_automation_cleanup_token", None)
    if binding.cleanup_token is not None:
        if cleanup_state is None:
            raise VisionRequestValidationError("cleanup token was not previously registered")
        if cleanup_state.consumed:
            raise VisionRequestValidationError("cleanup token was already consumed")
        if cleanup_state.cancelled:
            raise VisionRequestValidationError("cleanup token was cancelled")
        token_digest = hashlib.sha256(binding.cleanup_token.encode("utf-8")).hexdigest()
        if token_digest != cleanup_state.token_digest:
            raise VisionRequestValidationError("cleanup token does not match the registered token")
        if binding.page_generation != cleanup_state.page_generation:
            raise VisionRequestValidationError("cleanup token page generation mismatch")
        if (
            binding.mode != cleanup_state.mode
            or binding.top_level_context_id != cleanup_state.top_level_context_id
        ):
            raise VisionRequestValidationError("cleanup token originating identity mismatch")
        if previous is None:
            raise VisionRequestValidationError("cleanup token has no pinned Agent binding")
        return previous

    if previous is None:
        if cleanup_state is not None and cleanup_state.consumed:
            if binding.page_generation < cleanup_state.page_generation:
                raise VisionRequestValidationError("stale vision automation page generation")
            if (
                binding.page_generation == cleanup_state.page_generation
                and binding.automation_task_id == cleanup_state.automation_task_id
            ):
                raise VisionRequestValidationError("stale cleaned vision automation task")
        return None
    if (
        cleanup_state is not None
        and not cleanup_state.consumed
        and not cleanup_state.cancelled
    ):
        raise VisionRequestValidationError("registered cleanup token is required for this admission")
    if previous.mode != binding.mode or previous.top_level_context_id != binding.top_level_context_id:
        raise VisionRequestValidationError("vision automation binding identity mismatch")
    if binding.page_generation < previous.page_generation:
        raise VisionRequestValidationError("stale vision automation page generation")
    if (
        binding.page_generation == previous.page_generation
        and binding.automation_task_id != previous.automation_task_id
    ):
        raise VisionRequestValidationError("vision automation task binding mismatch")
    return previous


def stage_agent_history_image_omission(
    session: Any,
    metadata: VisionAutomationTransportMetadata | None,
    *,
    enable_sticky_omission: bool = False,
    omit_history: bool = True,
) -> StagedAgentHistoryImageOmission:
    """Build an immutable replacement plan without touching canonical memory."""

    if metadata is None:
        if getattr(session, "is_child_context", False):
            raise VisionRequestValidationError("vision automation cannot target a child Agent session")
        binding = None
        previous_binding = getattr(session, "vision_automation_binding", None)
    else:
        binding = metadata.binding
        previous_binding = _validate_binding(session, binding)
    memory = getattr(getattr(session, "agent", None), "memory", None)
    steps = getattr(memory, "steps", None)
    if not isinstance(steps, list):
        raise VisionRequestValidationError("cannot inspect canonical Agent memory")

    previous_cleanup = getattr(session, "vision_automation_cleanup_token", None)

    original_steps = tuple(steps)
    original_step_snapshots = tuple(
        _memory_step_snapshot(step) for step in original_steps
    )
    projected_steps = list(original_steps)
    replacements: list[_TaskImageReplacement] = []
    for index, step in enumerate(original_steps):
        if not omit_history:
            break
        if not isinstance(step, TaskStep):
            continue
        container = step.task_images
        if container is None:
            continue
        if not isinstance(container, (list, tuple)):
            raise VisionRequestValidationError("cannot inspect canonical Agent image block")
        if not container:
            continue
        images = tuple(container)
        provenance = _manifest_provenance(step.task, len(images))
        if provenance is None:
            sources: tuple[str, ...] = ()
            relative_start = None
            relative_end = None
        else:
            sources, relative_start, relative_end = provenance
        replacement = HistoricalImagePlaceholderTaskStep(
            task=step.task,
            task_images=None,
            historical_image_placeholder=format_historical_image_placeholder(
                count=len(images),
                sources=sources,
                relative_start=relative_start,
                relative_end=relative_end,
            ),
        )
        # Native routing tails omit smolagents' task wrapper. Preserve that
        # serialization policy across this permitted image-only boundary.
        if getattr(step, "canonical_routing_tail", False):
            replacement.canonical_routing_tail = True
        projected_steps[index] = replacement
        replacements.append(
            _TaskImageReplacement(
                index=index,
                original_step=step,
                original_task=step.task,
                original_container=container,
                original_images=images,
                replacement=replacement,
                manifest_fallback=provenance is None,
            )
        )

    next_cleanup = previous_cleanup
    if binding is not None and binding.cleanup_token is not None and replacements:
        assert previous_cleanup is not None
        next_cleanup = replace(previous_cleanup, consumed=True)
    next_binding: VisionAutomationPinnedBinding | None = (
        _pinned_binding(binding) if binding is not None else previous_binding
    )
    if (
        binding is not None
        and binding.cleanup_token is not None
        and replacements
        and previous_binding is not None
        and previous_binding.automation_task_id == binding.automation_task_id
    ):
        next_binding = None

    diagnostics_present = OMISSION_REASON in session.behavior_records
    return StagedAgentHistoryImageOmission(
        session=session,
        memory=memory,
        original_steps_container=steps,
        original_steps=original_steps,
        original_step_snapshots=original_step_snapshots,
        projected_steps=tuple(projected_steps),
        replacements=tuple(replacements),
        metadata=metadata,
        previous_binding=previous_binding,
        next_binding=next_binding,
        previous_cleanup_token=previous_cleanup,
        next_cleanup_token=next_cleanup,
        previous_sticky_omission=bool(
            getattr(session, "vision_automation_image_omission_sticky", False)
        ),
        next_sticky_omission=(
            bool(getattr(session, "vision_automation_image_omission_sticky", False))
            or enable_sticky_omission
        ),
        previous_compactable_messages=getattr(session, "last_compactable_llm_messages", None),
        previous_compactable_agent_name=getattr(session, "last_compactable_agent_name", None),
        previous_prompt_snapshot=getattr(session, "last_llm_prompt_snapshot", None),
        previous_diagnostics_present=diagnostics_present,
        previous_diagnostics=session.behavior_records.get(OMISSION_REASON),
    )


def _validate_unchanged_stage(
    session: Any,
    staged: StagedAgentHistoryImageOmission,
) -> None:
    if staged.session is not session or getattr(session.agent, "memory", None) is not staged.memory:
        raise RuntimeError("canonical Agent memory changed after omission staging")
    current_steps = staged.memory.steps
    if current_steps is not staged.original_steps_container:
        raise RuntimeError("canonical Agent memory container changed after omission staging")
    if len(current_steps) != len(staged.original_steps) or any(
        current is not original
        for current, original in zip(current_steps, staged.original_steps)
    ):
        raise RuntimeError("canonical Agent memory changed after omission staging")
    current_step_snapshots = tuple(
        _memory_step_snapshot(step) for step in current_steps
    )
    if current_step_snapshots != staged.original_step_snapshots:
        raise RuntimeError("canonical Agent memory step fields changed after omission staging")
    for item in staged.replacements:
        if item.original_step.task != item.original_task:
            raise RuntimeError("canonical Agent task text changed after omission staging")
        if item.original_step.task_images is not item.original_container:
            raise RuntimeError("canonical Agent image block changed after omission staging")
        current_images = tuple(item.original_container)
        if len(current_images) != len(item.original_images) or any(
            current is not original
            for current, original in zip(current_images, item.original_images)
        ):
            raise RuntimeError("canonical Agent image block changed after omission staging")


def _release_removed_resources(staged: StagedAgentHistoryImageOmission) -> None:
    for item in staged.replacements:
        for image in item.original_images:
            for attribute in ("_pre_encoded_b64", "_pre_encoded_mime"):
                if hasattr(image, attribute):
                    try:
                        delattr(image, attribute)
                    except Exception as exc:
                        logger.warning(
                            "historical Agent image payload release failed: %s",
                            exc,
                        )
            close = getattr(image, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    logger.warning("historical Agent image resource close failed: %s", exc)


def commit_agent_history_image_omission(
    session: Any,
    staged: StagedAgentHistoryImageOmission,
    *,
    release_resources: bool = True,
) -> None:
    """Atomically install the staged shape, then optionally release old images."""

    _validate_unchanged_stage(session, staged)
    session.agent.memory.steps = list(staged.projected_steps)
    session.vision_automation_binding = staged.next_binding
    session.vision_automation_cleanup_token = staged.next_cleanup_token
    session.vision_automation_image_omission_sticky = staged.next_sticky_omission
    if staged.replacements:
        session.last_compactable_llm_messages = None
        session.last_compactable_agent_name = None
        session.last_llm_prompt_snapshot = None
        diagnostics = list(staged.previous_diagnostics or ())
        diagnostics.append(
            {
                "reason": OMISSION_REASON,
                "omitted_block_count": staged.omitted_block_count,
                "omitted_image_count": staged.omitted_image_count,
                "manifest_fallback_count": sum(
                    item.manifest_fallback for item in staged.replacements
                ),
                "cleanup_token_consumed": (
                    staged.next_cleanup_token is not None
                    and staged.next_cleanup_token.consumed
                    and staged.next_cleanup_token is not staged.previous_cleanup_token
                ),
            }
        )
        session.behavior_records[OMISSION_REASON] = diagnostics[-_DIAGNOSTIC_LIMIT:]
    if release_resources:
        _release_removed_resources(staged)


def finalize_agent_history_image_omission(
    staged: StagedAgentHistoryImageOmission,
) -> None:
    """Release old image resources after the acceptance callback succeeds."""

    _release_removed_resources(staged)


def rollback_committed_agent_history_image_omission(
    session: Any,
    staged: StagedAgentHistoryImageOmission,
) -> None:
    """Restore a provisional commit when acceptance itself fails."""

    current_steps = session.agent.memory.steps
    if len(current_steps) != len(staged.projected_steps) or any(
        current is not projected
        for current, projected in zip(current_steps, staged.projected_steps)
    ):
        raise RuntimeError("canonical Agent memory changed before omission rollback")
    session.agent.memory.steps = staged.original_steps_container
    session.vision_automation_binding = staged.previous_binding
    session.vision_automation_cleanup_token = staged.previous_cleanup_token
    session.vision_automation_image_omission_sticky = staged.previous_sticky_omission
    session.last_compactable_llm_messages = staged.previous_compactable_messages
    session.last_compactable_agent_name = staged.previous_compactable_agent_name
    session.last_llm_prompt_snapshot = staged.previous_prompt_snapshot
    if staged.previous_diagnostics_present:
        session.behavior_records[OMISSION_REASON] = staged.previous_diagnostics
    else:
        session.behavior_records.pop(OMISSION_REASON, None)
