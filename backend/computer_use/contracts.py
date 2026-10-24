from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from fractions import Fraction
from typing import Any, Protocol, TypeAlias


class FrozenDict(Mapping[str, Any]):
    """A recursively immutable, defensively copied string-key mapping."""

    __slots__ = ("_items", "_index")

    def __init__(self, source: Mapping[str, Any] | None = None):
        source = source or {}
        if not isinstance(source, Mapping):
            raise TypeError("snapshot must be a mapping")
        items = tuple((str(key), deep_freeze(value)) for key, value in source.items())
        self._items = items
        self._index = dict(items)

    def __getitem__(self, key: str) -> Any:
        return self._index[key]

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"FrozenDict({dict(self._items)!r})"

    def __deepcopy__(self, memo: dict[int, Any]) -> "FrozenDict":
        return self


def deep_freeze(value: Any) -> Any:
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        return FrozenDict(value)
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(deep_freeze(item) for item in value)
    if isinstance(value, bytearray | memoryview):
        return bytes(value)
    return value


def thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(thaw(item) for item in value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Fraction):
        return {"numerator": value.numerator, "denominator": value.denominator}
    return value


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TypeError(f"{name} must be a positive integer")
    return value


def exact_number(value: Any, name: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float, Fraction)):
        raise TypeError(f"{name} must be a finite real number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value if isinstance(value, Fraction) else Fraction(str(value))


class Action(StrEnum):
    LEFT_CLICK = "left_click"
    RIGHT_CLICK = "right_click"
    LEFT_DOUBLE_CLICK = "left_double_click"
    RIGHT_DOUBLE_CLICK = "right_double_click"
    SCROLL_UP = "scroll_up"
    SCROLL_DOWN = "scroll_down"
    SCROLL_UP_1 = "scroll_up_1"
    SCROLL_DOWN_1 = "scroll_down_1"


class DispatchState(StrEnum):
    PENDING = "PENDING"
    DISPATCH_RESERVED = "DISPATCH_RESERVED"
    ACTION_SENT = "ACTION_SENT"
    ACTION_DELIVERY_UNKNOWN = "ACTION_DELIVERY_UNKNOWN"
    INDETERMINATE = "INDETERMINATE"
    CANCELLED = "CANCELLED"


class CoordinateProtocol(StrEnum):
    RELATIVE_1000 = "relative_1000"
    CUSTOM_ISOTROPIC_1000 = "custom_isotropic_1000"
    IMAGE_PIXEL = "image_pixel"


@dataclass(frozen=True)
class ProtocolDescriptor:
    name: CoordinateProtocol | str
    pixel_semantics: str | None = None
    output_type: str = "integer"
    adapter_version: str = "1"
    preprocessing: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            protocol = CoordinateProtocol(self.name)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unknown coordinate protocol: {self.name!r}") from exc
        semantics = self.pixel_semantics
        if protocol is CoordinateProtocol.IMAGE_PIXEL:
            if semantics not in {"extent", "index_center"}:
                raise ValueError("image_pixel requires extent or index_center semantics")
        elif semantics is not None:
            raise ValueError("pixel_semantics only applies to image_pixel")
        if self.output_type not in {"integer", "finite_float"}:
            raise ValueError("output_type must be integer or finite_float")
        object.__setattr__(self, "name", protocol)
        object.__setattr__(self, "preprocessing", FrozenDict(self.preprocessing))


@dataclass(frozen=True)
class GroundingRequest:
    request_id: str
    session_id: str
    binding_id: str
    target_description: str
    action: Action | str
    protocol: ProtocolDescriptor
    calibration_fingerprint: str

    def __post_init__(self) -> None:
        for name in ("request_id", "session_id", "binding_id", "target_description", "calibration_fingerprint"):
            _nonempty(getattr(self, name), name)
        try:
            action = Action(self.action)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid action: {self.action!r}") from exc
        if not isinstance(self.protocol, ProtocolDescriptor):
            raise TypeError("protocol must be a ProtocolDescriptor")
        object.__setattr__(self, "action", action)


@dataclass(frozen=True)
class InputBinding:
    binding_id: str
    session_id: str
    geometry_version: int
    backend: str
    origin: tuple[int, int]
    size: tuple[int, int]
    stream_generation: int
    device_id: str
    region_id: str
    mapping_evidence: Mapping[str, Any]
    ready: bool

    def __post_init__(self) -> None:
        for name in ("binding_id", "session_id", "backend", "device_id", "region_id"):
            _nonempty(getattr(self, name), name)
        if isinstance(self.geometry_version, bool) or not isinstance(self.geometry_version, int) or self.geometry_version < 0:
            raise TypeError("geometry_version must be a non-negative integer")
        if isinstance(self.stream_generation, bool) or not isinstance(self.stream_generation, int) or self.stream_generation < 0:
            raise TypeError("stream_generation must be a non-negative integer")
        if len(self.origin) != 2 or any(isinstance(v, bool) or not isinstance(v, int) for v in self.origin):
            raise TypeError("origin must contain two signed integers")
        if len(self.size) != 2:
            raise TypeError("size must contain width and height")
        _positive_int(self.size[0], "size.width")
        _positive_int(self.size[1], "size.height")
        if not isinstance(self.ready, bool):
            raise TypeError("ready must be bool")
        object.__setattr__(self, "origin", tuple(self.origin))
        object.__setattr__(self, "size", tuple(self.size))
        object.__setattr__(self, "mapping_evidence", FrozenDict(self.mapping_evidence))


@dataclass(frozen=True)
class CanonicalFrame:
    frame_id: str
    observation_id: str
    binding: InputBinding
    width: int
    height: int
    rgb: bytes
    stream_generation: int
    sequence: int
    captured_monotonic: float
    arrived_monotonic: float
    liveness_proven: bool
    coverage_evidence: Mapping[str, Any]
    transform_evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        _nonempty(self.frame_id, "frame_id")
        _nonempty(self.observation_id, "observation_id")
        if not isinstance(self.binding, InputBinding):
            raise TypeError("binding must be an InputBinding")
        width = _positive_int(self.width, "width")
        height = _positive_int(self.height, "height")
        rgb = bytes(self.rgb)
        if len(rgb) != width * height * 3:
            raise ValueError("rgb byte length does not match packed RGB dimensions")
        for name in ("stream_generation", "sequence"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TypeError(f"{name} must be a non-negative integer")
        if self.stream_generation != self.binding.stream_generation:
            raise ValueError("frame stream_generation does not match its binding")
        capture = float(exact_number(self.captured_monotonic, "captured_monotonic"))
        arrival = float(exact_number(self.arrived_monotonic, "arrived_monotonic"))
        if capture < 0 or arrival < capture:
            raise ValueError("monotonic timestamps are invalid")
        if not isinstance(self.liveness_proven, bool):
            raise TypeError("liveness_proven must be bool")
        object.__setattr__(self, "rgb", rgb)
        object.__setattr__(self, "captured_monotonic", capture)
        object.__setattr__(self, "arrived_monotonic", arrival)
        object.__setattr__(self, "coverage_evidence", FrozenDict(self.coverage_evidence))
        object.__setattr__(self, "transform_evidence", FrozenDict(self.transform_evidence))

    @property
    def geometry_version(self) -> int:
        return self.binding.geometry_version

    def age(self, now: float) -> float:
        current = float(exact_number(now, "now"))
        if current < self.captured_monotonic:
            raise ValueError("now precedes capture")
        return current - self.captured_monotonic


@dataclass(frozen=True)
class DetailCrop:
    crop_id: str
    request_id: str
    session_id: str
    observation_id: str
    frame_id: str
    binding_id: str
    geometry_version: int
    source_width: int
    source_height: int
    x0: int
    y0: int
    x1: int
    y1: int
    detail_width: int
    detail_height: int
    local_width: Fraction | int | float
    local_height: Fraction | int | float
    model_width: int
    model_height: int
    wire_protocol: CoordinateProtocol | str
    wire_semantics: str
    protocol_fingerprint: str
    calibration_fingerprint: str
    rough_x: Fraction | int | float
    rough_y: Fraction | int | float
    zoom: int
    grid_enabled: bool
    grid_divisions: int
    created_monotonic: float
    dispatch_identity: str
    dispatch_state: DispatchState | str = DispatchState.PENDING

    def __post_init__(self) -> None:
        for name in (
            "crop_id", "request_id", "session_id", "observation_id", "frame_id",
            "binding_id", "wire_semantics", "protocol_fingerprint",
            "calibration_fingerprint", "dispatch_identity",
        ):
            _nonempty(getattr(self, name), name)
        for name in ("source_width", "source_height", "detail_width", "detail_height", "model_width", "model_height"):
            _positive_int(getattr(self, name), name)
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (self.x0, self.y0, self.x1, self.y1)):
            raise TypeError("crop bounds must be integers")
        if not (0 <= self.x0 < self.x1 <= self.source_width and 0 <= self.y0 < self.y1 <= self.source_height):
            raise ValueError("crop bounds must be a nonempty source subrectangle")
        local_width = exact_number(self.local_width, "local_width")
        local_height = exact_number(self.local_height, "local_height")
        if local_width != 1000 or local_height != 1000:
            raise ValueError("both local axes must independently span 0..1000")
        if not (self.detail_width == self.detail_height == self.model_width == self.model_height):
            raise ValueError("detail and model canvas must share one square side")
        # The source crop may remain rectangular after clipping. Both model
        # dimensions are square; inverse mapping still uses each source axis.
        rough_x = exact_number(self.rough_x, "rough_x")
        rough_y = exact_number(self.rough_y, "rough_y")
        if not (0 <= rough_x <= 1000 and 0 <= rough_y <= 1000):
            raise ValueError("rough coordinates must be within [0,1000]")
        if self.zoom not in {2, 4, 6, 8} or isinstance(self.zoom, bool):
            raise ValueError("zoom must be one of 2,4,6,8")
        if not isinstance(self.grid_enabled, bool):
            raise TypeError("grid_enabled must be bool")
        if self.grid_divisions != 16:
            raise ValueError("Stage-2 grid must divide each normalized axis into sixteen cells")
        try:
            protocol = CoordinateProtocol(self.wire_protocol)
            state = DispatchState(self.dispatch_state)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid protocol or dispatch state") from exc
        object.__setattr__(self, "local_width", local_width)
        object.__setattr__(self, "local_height", local_height)
        object.__setattr__(self, "rough_x", rough_x)
        object.__setattr__(self, "rough_y", rough_y)
        object.__setattr__(self, "wire_protocol", protocol)
        object.__setattr__(self, "dispatch_state", state)

    @property
    def crop_width(self) -> int:
        return self.x1 - self.x0

    @property
    def crop_height(self) -> int:
        return self.y1 - self.y0

    @property
    def exact_crop_aspect(self) -> Fraction:
        return Fraction(self.crop_width, self.crop_height)

    @property
    def model_scale_x(self) -> Fraction:
        return Fraction(self.model_width, self.crop_width)

    @property
    def model_scale_y(self) -> Fraction:
        return Fraction(self.model_height, self.crop_height)


@dataclass(frozen=True)
class ImageArtifact:
    png: bytes
    width: int
    height: int
    content_hash: str
    renderer_fingerprint: str

    def __post_init__(self) -> None:
        _positive_int(self.width, "width")
        _positive_int(self.height, "height")
        _nonempty(self.content_hash, "content_hash")
        _nonempty(self.renderer_fingerprint, "renderer_fingerprint")
        object.__setattr__(self, "png", bytes(self.png))


class LabelFormatter(Protocol):
    def format_axis(self, axis: str, canonical_value: Fraction) -> str: ...
    def boundary_ticks(self, axis: str) -> tuple[tuple[Fraction, str], ...]: ...


@dataclass(frozen=True)
class ProtocolInstance:
    stage: str
    descriptor: ProtocolDescriptor
    protocol_fingerprint: str
    wire_x_max: int
    wire_y_max: int
    axis_mode: str
    image_width: int
    image_height: int
    request: GroundingRequest
    crop: DetailCrop | None
    label_formatter: LabelFormatter
    stage1_zoom: int = 4


@dataclass(frozen=True)
class EncodedStage:
    instance: ProtocolInstance
    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[Mapping[str, Any], ...]
    image_metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(FrozenDict(item) for item in self.messages))
        object.__setattr__(self, "tools", tuple(FrozenDict(item) for item in self.tools))
        object.__setattr__(self, "image_metadata", FrozenDict(self.image_metadata))


@dataclass(frozen=True)
class InspectRegion:
    x: Fraction
    y: Fraction
    zoom: int = 4


@dataclass(frozen=True)
class InteractDetail:
    crop_id: str
    x: Fraction
    y: Fraction
    action: Action
    steps: int = 1

    def __post_init__(self) -> None:
        action = Action(self.action)
        object.__setattr__(self, "action", action)
        if isinstance(self.steps, bool) or not isinstance(self.steps, int) or not 1 <= self.steps <= 5:
            raise ValueError("steps must be an integer from 1 through 5")
        if action not in {Action.SCROLL_UP, Action.SCROLL_DOWN} and self.steps != 1:
            raise ValueError("multiple steps require scroll_up or scroll_down")


@dataclass(frozen=True)
class Reobserve:
    reason: str = "other"


StageDecision: TypeAlias = InspectRegion | InteractDetail | Reobserve


@dataclass(frozen=True)
class DispatchReceipt:
    request_id: str
    crop_id: str
    state: DispatchState | str
    last_possible_event: int | None
    cleanup_status: str
    freshness: Mapping[str, Any] = field(default_factory=dict)
    pre_action_validation: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.request_id, "request_id")
        _nonempty(self.crop_id, "crop_id")
        _nonempty(self.cleanup_status, "cleanup_status")
        object.__setattr__(self, "state", DispatchState(self.state))
        object.__setattr__(self, "freshness", FrozenDict(self.freshness))

    def to_json(self) -> str:
        return json.dumps(
            {
                "request_id": self.request_id,
                "crop_id": self.crop_id,
                "state": self.state.value,
                "last_possible_event": self.last_possible_event,
                "cleanup_status": self.cleanup_status,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class Reservation:
    request_id: str
    crop_id: str
    previous_receipt: DispatchReceipt | None = None


@dataclass(frozen=True)
class FreshnessResult:
    state: str
    code: str
    validity_deadline: float | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    age_seconds: float | None = None
    observed_sequence: int | None = None
    current_sequence: int | None = None
    global_score: float | None = None
    target_score: float | None = None

    def __post_init__(self) -> None:
        if self.state not in {"FRESH", "STALE", "INDETERMINATE"}:
            raise ValueError("invalid freshness state")
        object.__setattr__(self, "evidence", FrozenDict(self.evidence))

    @classmethod
    def indeterminate(cls, code: str) -> "FreshnessResult":
        return cls("INDETERMINATE", code)

    @classmethod
    def stale(cls, code: str) -> "FreshnessResult":
        return cls("STALE", code)


@dataclass(frozen=True)
class CalibrationCase:
    case_id: str
    width: int
    height: int
    target_ratio: tuple[float, float]
    repeats: int = 1

    def __post_init__(self) -> None:
        _nonempty(self.case_id, "case_id")
        _positive_int(self.width, "width")
        _positive_int(self.height, "height")
        _positive_int(self.repeats, "repeats")
        if len(self.target_ratio) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in self.target_ratio):
            raise ValueError("target_ratio must contain two finite values in [0,1]")


@dataclass(frozen=True)
class CalibrationReport:
    fingerprint: str
    protocol: str
    passed: bool
    pass_rate: float
    max_axis_error: float
    case_results: tuple[Mapping[str, Any], ...]
    thresholds: Mapping[str, Any]
    created_monotonic: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_results", tuple(FrozenDict(item) for item in self.case_results))
        object.__setattr__(self, "thresholds", FrozenDict(self.thresholds))
