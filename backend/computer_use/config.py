from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import Any, Mapping


def _positive_number(value: Any, name: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise TypeError(f"{name} must be a positive finite number")
    return float(value)


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TypeError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class ComputerUseConfig:
    enabled: bool = False
    helper_binary: str | None = None
    calibration_report_path: str | None = None
    operator_session_enabled: bool = False
    operator_session_safety_config_path: str | None = None
    authorization_timeout_seconds: float = 120.0
    transport_timeout_seconds: float = 5.0
    protocol: str = "relative_1000"
    pixel_semantics: str | None = None
    stage1_zoom: int = 4  # Nonbinding offline fixture choice; omitted live zoom is always 4.
    pre_action_validation: str = "binding_only"
    debug_save_images: bool = False
    debug_image_max_files: int = 64
    debug_image_max_bytes: int = 134217728
    diagnostics_enabled: bool = False
    diagnostics_path: str = "logs/computer-use/vision-desktop.jsonl"
    diagnostics_max_bytes: int = 1048576
    diagnostics_backup_count: int = 3
    model_square_side_px: int = 1024
    grid_enabled: bool = True
    grid_divisions: int = 16
    grid_label_placement: str = "edge"
    grid_label_shadow_width_px: int = 1  # Outline width at a 1000-pixel canvas baseline.
    max_tool_contract_retries: int = 1
    max_pre_dispatch_attempts: int = 3
    inference_timeout_seconds: float = 120.0
    max_control_packet_bytes: int = 65536
    max_received_fds: int = 1
    max_canonical_frame_bytes: int = 268435456
    max_observation_age_seconds: float | None = None
    max_sample_age_seconds: float | None = None
    max_validation_to_dispatch_seconds: float | None = None
    global_diff_threshold: float | None = None
    target_diff_threshold: float | None = None
    double_click_interval_seconds: float | None = None
    calibration_fingerprint: str | None = None

    def __post_init__(self) -> None:
        for name in ("helper_binary", "calibration_report_path", "operator_session_safety_config_path"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.startswith("/") or "\x00" in value):
                raise ValueError(f"{name} must be an absolute path")
        _positive_number(self.authorization_timeout_seconds, "authorization_timeout_seconds")
        _positive_number(self.transport_timeout_seconds, "transport_timeout_seconds")
        if not all(.001 <= value <= 300 for value in (self.authorization_timeout_seconds, self.transport_timeout_seconds)):
            raise ValueError("native timeouts must be between .001 and 300 seconds")
        if not 256 <= self.max_control_packet_bytes <= 1048576 or not 3 <= self.max_canonical_frame_bytes <= 268435456:
            raise ValueError("native transport bounds exceeded")
        if self.max_received_fds != 1:
            raise ValueError("native frame transport requires one descriptor")
        if not isinstance(self.enabled, bool) or not isinstance(self.grid_enabled, bool):
            raise TypeError("enabled and grid_enabled must be bool")
        if not isinstance(self.operator_session_enabled, bool):
            raise TypeError("operator_session_enabled must be bool")
        if self.operator_session_enabled and not self.operator_session_safety_config_path:
            raise ValueError("operator session requires a safety preset path")
        if isinstance(self.stage1_zoom, bool) or self.stage1_zoom not in (2, 4, 6, 8) or not isinstance(self.stage1_zoom, int):
            raise ValueError("stage1_zoom must be 2, 4, 6 or 8")
        if self.pre_action_validation not in ("binding_only", "frame_and_binding"):
            raise ValueError("pre_action_validation must be binding_only or frame_and_binding")
        if not isinstance(self.debug_save_images, bool):
            raise TypeError("debug_save_images must be bool")
        _positive_int(self.debug_image_max_files, "debug_image_max_files")
        _positive_int(self.debug_image_max_bytes, "debug_image_max_bytes")
        if self.debug_image_max_files > 1024 or not 1024 <= self.debug_image_max_bytes <= 1073741824:
            raise ValueError("debug image retention limits exceed bounds")
        if not isinstance(self.diagnostics_enabled, bool):
            raise TypeError("diagnostics_enabled must be bool")
        if not isinstance(self.diagnostics_path, str) or not self.diagnostics_path or "\x00" in self.diagnostics_path:
            raise ValueError("diagnostics_path must be a non-empty path")
        _positive_int(self.diagnostics_max_bytes, "diagnostics_max_bytes")
        if not 4096 <= self.diagnostics_max_bytes <= 67108864:
            raise ValueError("diagnostics_max_bytes must be between 4096 and 67108864")
        if isinstance(self.diagnostics_backup_count, bool) or not isinstance(self.diagnostics_backup_count, int) or not 1 <= self.diagnostics_backup_count <= 10:
            raise ValueError("diagnostics_backup_count must be between 1 and 10")
        if self.protocol == "image_pixel":
            raise ValueError("image_pixel is an offline legacy protocol, not a live configuration")
        if self.protocol not in {"relative_1000", "custom_isotropic_1000"}:
            raise ValueError("unknown protocol")
        if self.pixel_semantics is not None:
            raise ValueError("pixel_semantics only applies to image_pixel")
        for name in ("model_square_side_px", "max_control_packet_bytes", "max_received_fds", "max_canonical_frame_bytes"):
            _positive_int(getattr(self, name), name)
        if self.grid_divisions != 16:
            raise ValueError("grid_divisions must be 16")
        if not isinstance(self.grid_label_placement, str) or self.grid_label_placement not in {"edge", "repeated"}:
            raise ValueError("grid_label_placement must be edge or repeated")
        if (isinstance(self.grid_label_shadow_width_px, bool)
                or not isinstance(self.grid_label_shadow_width_px, int)
                or self.grid_label_shadow_width_px < 0):
            raise ValueError("grid_label_shadow_width_px must be a non-negative integer")
        for name in ("max_tool_contract_retries",):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TypeError(f"{name} must be a non-negative integer")
        _positive_int(self.max_pre_dispatch_attempts, "max_pre_dispatch_attempts")
        _positive_number(self.inference_timeout_seconds, "inference_timeout_seconds")
        for name in (
            "max_observation_age_seconds", "max_sample_age_seconds",
            "max_validation_to_dispatch_seconds", "global_diff_threshold",
            "target_diff_threshold", "double_click_interval_seconds",
        ):
            _positive_number(getattr(self, name), name, allow_none=True)
        for name in ("global_diff_threshold", "target_diff_threshold"):
            value = getattr(self, name)
            if value is not None and value > 1:
                raise ValueError(f"{name} must be <= 1")
        if self.calibration_fingerprint is not None and (not isinstance(self.calibration_fingerprint, str) or not self.calibration_fingerprint):
            raise TypeError("calibration_fingerprint must be a non-empty string or None")

    @property
    def ready(self) -> bool:
        required = (
            self.max_observation_age_seconds,
            self.max_sample_age_seconds,
            self.max_validation_to_dispatch_seconds,
            self.global_diff_threshold,
            self.target_diff_threshold,
            self.double_click_interval_seconds,
            self.calibration_fingerprint,
        )
        return self.enabled and all(value is not None for value in required)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "ComputerUseConfig":
        if values is None:
            return cls()
        if not isinstance(values, Mapping):
            raise TypeError("computer_use config must be a mapping")
        known = {item.name for item in fields(cls)}
        unknown = sorted(set(values) - known)
        if unknown:
            raise ValueError(f"unknown computer_use config fields: {', '.join(unknown)}")
        return cls(**dict(values))
