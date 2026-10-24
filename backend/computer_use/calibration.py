from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Callable, Iterable, Mapping

from .contracts import CalibrationCase, CalibrationReport, ProtocolDescriptor, thaw
from .errors import ComputerUseError


def _canonical(value: Any) -> Any:
    if is_dataclass(value):
        return _canonical(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if hasattr(value, "value") and isinstance(getattr(value, "value"), str):
        return value.value
    return thaw(value)


def calibration_fingerprint(
    descriptor: ProtocolDescriptor,
    renderer: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    serving: Mapping[str, Any],
) -> str:
    payload = {
        "descriptor": _canonical(descriptor),
        "renderer": _canonical(renderer),
        "checkpoint": _canonical(checkpoint),
        "serving": _canonical(serving),
        "fingerprint_version": 2,
    }
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


def verify_calibration(report: CalibrationReport | Mapping[str, Any], current_fingerprint: str) -> None:
    if isinstance(report, CalibrationReport):
        passed, fingerprint = report.passed, report.fingerprint
    elif isinstance(report, Mapping):
        passed, fingerprint = report.get("passed") is True, report.get("fingerprint")
    else:
        raise ComputerUseError("CALIBRATION_REQUIRED", "calibration report is missing")
    if not passed or fingerprint != current_fingerprint:
        raise ComputerUseError("CALIBRATION_REQUIRED", "deployment fingerprint changed or report did not pass")


def run_calibration(
    infer: Callable[[CalibrationCase, ProtocolDescriptor], Iterable[tuple[float, float]]],
    cases: Iterable[CalibrationCase],
    protocol: ProtocolDescriptor,
    thresholds: Mapping[str, Any],
    *,
    fingerprint: str,
    now: Callable[[], float] = time.monotonic,
) -> CalibrationReport:
    if not isinstance(protocol, ProtocolDescriptor):
        raise TypeError("protocol must be ProtocolDescriptor")
    if set(thresholds) != {"max_axis_error", "min_pass_rate"}:
        raise ValueError("thresholds require exactly max_axis_error and min_pass_rate")
    max_allowed = thresholds["max_axis_error"]
    min_rate = thresholds["min_pass_rate"]
    for value, name in ((max_allowed, "max_axis_error"), (min_rate, "min_pass_rate")):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{name} must be finite and within [0,1]")
    results: list[dict[str, Any]] = []
    passing = total = 0
    observed_max = 0.0
    for case in tuple(cases):
        if not isinstance(case, CalibrationCase):
            raise TypeError("cases must contain CalibrationCase records")
        outputs = tuple(infer(case, protocol))
        if len(outputs) != case.repeats:
            raise ValueError(f"case {case.case_id} returned {len(outputs)} outputs, expected {case.repeats}")
        errors: list[float] = []
        for output in outputs:
            if len(output) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in output):
                error = math.inf
            else:
                error = max(abs(float(output[0]) - case.target_ratio[0]), abs(float(output[1]) - case.target_ratio[1]))
            errors.append(error)
            observed_max = max(observed_max, error)
            total += 1
            if error <= max_allowed:
                passing += 1
        results.append({"case_id": case.case_id, "width": case.width, "height": case.height, "target_ratio": case.target_ratio, "errors": tuple(errors)})
    if total == 0:
        raise ValueError("calibration requires at least one repeated sample")
    rate = passing / total
    passed = rate >= min_rate and observed_max <= max_allowed
    return CalibrationReport(
        fingerprint=fingerprint,
        protocol=protocol.name.value,
        passed=passed,
        pass_rate=rate,
        max_axis_error=observed_max,
        case_results=tuple(results),
        thresholds=thresholds,
        created_monotonic=now(),
    )


def verify_deployment_report(report: Mapping[str, Any], current_fingerprint: str, protocol: str) -> None:
    """Validate persisted measured results, not just a caller-supplied pass flag."""
    verify_calibration(report, current_fingerprint)
    try:
        geometry = report['desktop_geometry']
        if (not isinstance(geometry, dict)
                or set(geometry) != {'backend', 'logical_origin', 'logical_size', 'pixel_size'}
                or geometry['backend'] not in {'wayland', 'x11'}):
            raise ValueError('desktop geometry is unverified')
        for name, positive in (('logical_origin', False), ('logical_size', True), ('pixel_size', True)):
            values = geometry[name]
            if (not isinstance(values, list) or len(values) != 2
                    or any(type(value) is not int or (positive and value < 1) for value in values)):
                raise ValueError('invalid desktop geometry')
        # XTest and XGetImage share root pixel coordinates, with no compositor
        # logical-to-pixel scaling. Reports cannot authorize a scaled mapping.
        if geometry['backend'] == 'x11' and (
                geometry['logical_size'] != geometry['pixel_size']
                or any(value < 0 for value in geometry['logical_origin'])):
            raise ValueError('X11 geometry must use root pixels')
        preprocessing = report['serving_preprocessing']
        if (not isinstance(preprocessing, dict)
                or set(preprocessing) != {'transform', 'verified'}
                or preprocessing.get('transform') != 'identity'
                or preprocessing.get('verified') is not True):
            raise ValueError('serving image preprocessing is unverified')
        if report['protocol'] != protocol:
            raise ValueError('protocol changed')
        thresholds = report['thresholds']
        maximum, minimum = thresholds['max_axis_error'], thresholds['min_pass_rate']
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1
               for v in (maximum, minimum)):
            raise ValueError('invalid limits')
        cases = report['case_results']
        if not isinstance(cases, list) or not cases:
            raise ValueError('no calibration samples')
        errors = []
        targets, identities = set(), set()
        for case in cases:
            identity = case['case_id']
            if not isinstance(identity, str) or not identity or identity in identities:
                raise ValueError('invalid case identity')
            identities.add(identity)
            if any(type(case[key]) is not int or case[key] < 1 for key in ('width', 'height')):
                raise ValueError('invalid geometry')
            target = case['target_ratio']
            if not isinstance(target, list) or len(target) != 2 or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in target):
                raise ValueError('invalid target')
            targets.add(tuple(target))
            samples = case['errors']
            if not isinstance(samples, list) or len(samples) < 2:
                raise ValueError('repeated samples required')
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in samples):
                raise ValueError('invalid sample')
            errors.extend(samples)
        if len(targets) < 2:
            raise ValueError('diverse targets required')
        measured_max = max(errors)
        measured_rate = sum(error <= maximum for error in errors) / len(errors)
        if measured_max > maximum or measured_rate < minimum:
            raise ValueError('measurements failed')
        if report['max_axis_error'] != measured_max or report['pass_rate'] != measured_rate:
            raise ValueError('inconsistent measurements')
    except (KeyError, TypeError, ValueError) as exc:
        raise ComputerUseError('CALIBRATION_REQUIRED', 'calibration measurements are incomplete or invalid') from exc
