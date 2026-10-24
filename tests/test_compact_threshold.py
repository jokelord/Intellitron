"""Threshold computation for /compact trigger.

Two strategies:
- performance: threshold = max_context_length * performance_ratio
- utilization: threshold = max_context_length - summary_reserve - buffer
"""
from __future__ import annotations

import pytest

from backend.compact.threshold import compute_threshold, should_compact
from backend.config import CompactConfig


def test_performance_strategy_uses_ratio():
    cfg = CompactConfig(strategy="performance", performance_ratio=0.5)
    assert compute_threshold(cfg, max_context_length=90000) == 45000


def test_performance_strategy_custom_ratio():
    cfg = CompactConfig(strategy="performance", performance_ratio=0.7)
    assert compute_threshold(cfg, max_context_length=100000) == 70000


def test_utilization_strategy_subtracts_reserve_and_buffer():
    cfg = CompactConfig(
        strategy="utilization",
        utilization_summary_reserve=20000,
        utilization_buffer=13000,
    )
    assert compute_threshold(cfg, max_context_length=90000) == 57000


def test_should_compact_at_or_above_threshold():
    assert should_compact(50000, 50000) is True
    assert should_compact(49999, 50000) is False
    assert should_compact(0, 0) is True  # edge: 0 >= 0


def test_zero_max_context_raises():
    cfg = CompactConfig(strategy="performance", performance_ratio=0.5)
    with pytest.raises(ValueError, match="max_context_length"):
        compute_threshold(cfg, max_context_length=0)


def test_negative_max_context_raises():
    cfg = CompactConfig(strategy="performance", performance_ratio=0.5)
    with pytest.raises(ValueError, match="max_context_length"):
        compute_threshold(cfg, max_context_length=-1)


def test_unknown_strategy_raises():
    cfg = CompactConfig(strategy="invalid")
    with pytest.raises(ValueError, match="Unknown strategy"):
        compute_threshold(cfg, max_context_length=1000)
