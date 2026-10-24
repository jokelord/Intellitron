"""Threshold computation for /compact trigger.

Two strategies:
- performance: threshold = max_context_length * performance_ratio
- utilization: threshold = max_context_length - summary_reserve - buffer
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import CompactConfig


def compute_threshold(
    config: "CompactConfig",
    max_context_length: int,
) -> int:
    if max_context_length <= 0:
        raise ValueError(
            f"max_context_length must be positive, got {max_context_length}"
        )
    if config.strategy == "performance":
        return int(max_context_length * config.performance_ratio)
    if config.strategy == "utilization":
        return (
            max_context_length
            - config.utilization_summary_reserve
            - config.utilization_buffer
        )
    raise ValueError(
        f"Unknown strategy: {config.strategy!r}. Expected 'performance' or 'utilization'."
    )


def should_compact(token_count: int, threshold: int) -> bool:
    return token_count >= threshold
