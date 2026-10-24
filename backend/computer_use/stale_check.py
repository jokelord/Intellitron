"""Fail-closed freshness checks for captured desktop observations."""
from __future__ import annotations

import math
from typing import Any

from .contracts import FreshnessResult


def _indeterminate(code: str, **evidence: Any) -> FreshnessResult:
    return FreshnessResult("INDETERMINATE", code, evidence=evidence,
                           age_seconds=evidence.get("age_seconds"),
                           observed_sequence=evidence.get("observed_sequence"),
                           current_sequence=evidence.get("current_sequence"),
                           global_score=evidence.get("global_score"),
                           target_score=evidence.get("target_score"))


def _stale(code: str, **evidence: Any) -> FreshnessResult:
    return FreshnessResult("STALE", code, evidence=evidence,
                           age_seconds=evidence.get("age_seconds"),
                           observed_sequence=evidence.get("observed_sequence"),
                           current_sequence=evidence.get("current_sequence"),
                           global_score=evidence.get("global_score"),
                           target_score=evidence.get("target_score"))


def _fresh(**evidence: Any) -> FreshnessResult:
    return FreshnessResult("FRESH", "FRESH", evidence=evidence,
                           validity_deadline=evidence.get("valid_until"),
                           age_seconds=evidence.get("age_seconds"),
                           observed_sequence=evidence.get("observed_sequence"),
                           current_sequence=evidence.get("current_sequence"),
                           global_score=evidence.get("global_score"),
                           target_score=evidence.get("target_score"))


def _pixel_score(a: bytes, b: bytes) -> float:
    if not a or not b or len(a) != len(b):
        return 1.0
    from PIL import Image, ImageChops
    histogram = ImageChops.difference(Image.frombytes("L", (len(a), 1), a),
                                     Image.frombytes("L", (len(b), 1), b)).histogram()
    total = sum(index * count for index, count in enumerate(histogram))
    return total / (255.0 * len(a))


def check_freshness(observed, current, binding, now: float, config, *, crop=None, clock=None) -> FreshnessResult:
    """Prove an observation is still usable under the current binding.

    Identical pixels never establish freshness.  A trustworthy liveness signal,
    matching stream identity and bounded monotonic age are required before an
    observation can be used for input dispatch.
    """
    if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
        return _indeterminate("INVALID_CLOCK")
    if observed is None or current is None or binding is None:
        return _indeterminate("MISSING_OBSERVATION")
    if getattr(observed, "binding", None) != binding or getattr(current, "binding", None) != binding:
        return _stale("BINDING_CHANGED")
    if getattr(observed, "stream_generation", None) != getattr(current, "stream_generation", None):
        return _stale("STREAM_GENERATION_CHANGED")
    if getattr(current, "stream_generation", None) != getattr(binding, "stream_generation", None):
        return _stale("STREAM_GENERATION_CHANGED")
    if getattr(observed, "geometry_version", getattr(binding, "geometry_version", None)) != getattr(binding, "geometry_version", None):
        return _stale("GEOMETRY_CHANGED")
    if getattr(current, "geometry_version", getattr(binding, "geometry_version", None)) != getattr(binding, "geometry_version", None):
        return _stale("GEOMETRY_CHANGED")
    if getattr(current, "sequence", 0) < getattr(observed, "sequence", 0):
        return _stale("SEQUENCE_REGRESSED", observed_sequence=getattr(observed, "sequence", None), current_sequence=getattr(current, "sequence", None))

    try:
        age = current.age(now) if hasattr(current, "age") else math.inf
        observed_age = observed.age(now) if hasattr(observed, "age") else age
    except (TypeError, ValueError):
        return _indeterminate("INVALID_TIMESTAMP")
    max_observation_age = getattr(config, "max_observation_age_seconds", None)
    if max_observation_age is None or not math.isfinite(float(max_observation_age)) or observed_age > float(max_observation_age):
        return _indeterminate("OBSERVATION_TOO_OLD", age_seconds=observed_age)
    max_age = getattr(config, "max_sample_age_seconds", None)
    if max_age is None or not math.isfinite(float(max_age)) or age < 0 or age > float(max_age):
        return _indeterminate("CAPTURE_STALLED", age_seconds=age, observed_sequence=getattr(observed, "sequence", None), current_sequence=getattr(current, "sequence", None))
    if not bool(getattr(current, "liveness_proven", False)):
        return _indeterminate("CAPTURE_STALLED", age_seconds=age, observed_sequence=getattr(observed, "sequence", None), current_sequence=getattr(current, "sequence", None))

    if (observed.width, observed.height) != (current.width, current.height):
        return _stale("GEOMETRY_CHANGED")
    target_score = None
    if crop is not None:
        if not (0 <= crop.x0 < crop.x1 <= current.width and 0 <= crop.y0 < crop.y1 <= current.height):
            return _indeterminate("INVALID_TARGET_REGION")
        def target_pixels(frame):
            return b"".join(frame.rgb[(y * frame.width + crop.x0) * 3:(y * frame.width + crop.x1) * 3]
                            for y in range(crop.y0, crop.y1))
        target_score = _pixel_score(target_pixels(observed), target_pixels(current))
    global_score = _pixel_score(bytes(getattr(observed, "rgb", b"")), bytes(getattr(current, "rgb", b"")))
    threshold = getattr(config, "global_diff_threshold", None)
    if threshold is None or global_score > float(threshold):
        return _stale("FRAME_CHANGED", global_score=global_score, age_seconds=age, observed_sequence=getattr(observed, "sequence", None), current_sequence=getattr(current, "sequence", None))

    if crop is not None and (config.target_diff_threshold is None or target_score > config.target_diff_threshold):
        return _stale("TARGET_CHANGED", global_score=global_score, target_score=target_score,
                      age_seconds=age, observed_sequence=observed.sequence, current_sequence=current.sequence)
    if clock is not None:
        # Pixel comparison is part of validation, not the subsequent dispatch
        # interval. Recheck age at its completion before granting that interval.
        completed = clock()
        if (isinstance(completed, bool) or not isinstance(completed, (int, float))
                or not math.isfinite(completed) or completed < now):
            return _indeterminate("INVALID_CLOCK")
        now = completed
        try:
            age = current.age(now)
            observed_age = observed.age(now)
        except (TypeError, ValueError, AttributeError):
            return _indeterminate("INVALID_TIMESTAMP")
        evidence = dict(age_seconds=age, observed_sequence=observed.sequence,
                        current_sequence=current.sequence, global_score=global_score, target_score=target_score)
        if observed_age > float(max_observation_age):
            return _indeterminate("OBSERVATION_TOO_OLD", **{**evidence, "age_seconds": observed_age})
        if age < 0 or age > float(max_age):
            return _indeterminate("CAPTURE_STALLED", **evidence)
    # A same-looking frame is acceptable only when the capture backend proves
    # liveness (damage/sequence evidence).  Pixel equality alone is not enough.
    deadline = now + float(getattr(config, "max_validation_to_dispatch_seconds", 0.0) or 0.0)
    return _fresh(age_seconds=age, global_score=global_score, target_score=target_score, valid_until=deadline,
                  observed_sequence=getattr(observed, "sequence", None),
                  current_sequence=getattr(current, "sequence", None))


__all__ = ["check_freshness"]
