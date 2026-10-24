#!/usr/bin/env python3
"""Run the Read File v7 Thor acceptance manifest and emit JSON/Markdown evidence."""

from __future__ import annotations

import argparse
import itertools
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

MANIFEST_BEGIN = "<!-- READ_FILE_V7_MANIFEST_BEGIN -->"
MANIFEST_END = "<!-- READ_FILE_V7_MANIFEST_END -->"


def load_manifest(path: str | Path) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    try:
        payload = text.split(MANIFEST_BEGIN, 1)[1].split(MANIFEST_END, 1)[0]
        manifest = json.loads(payload)
    except (IndexError, json.JSONDecodeError) as exc:
        raise ValueError("acceptance document contains no valid embedded manifest") from exc
    cases = manifest.get("cases")
    if manifest.get("schemaVersion") != 1 or not isinstance(cases, list) or not cases:
        raise ValueError("acceptance manifest schema is invalid")
    identifiers = [case.get("id") for case in cases]
    if any(not identifier for identifier in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError("acceptance case IDs must be non-empty and unique")
    return manifest


def _aggregate_tegrastats_samples(samples: list[str]) -> dict[str, Any]:
    patterns = {
        "ram": re.compile(r"\bRAM\s+(\d+)/\d+MB"),
        "cpu_temperature": re.compile(r"\bcpu@([0-9.]+)C"),
        "gpu_temperature": re.compile(r"\bgpu@([0-9.]+)C"),
        "gpu_power": re.compile(r"\bVDD_GPU\s+(\d+)mW/"),
        "input_power": re.compile(r"(?:^|\s)VIN\s+(\d+)mW/"),
    }
    values: dict[str, list[float]] = {name: [] for name in patterns}
    for sample in samples:
        for name, pattern in patterns.items():
            match = pattern.search(sample)
            if match:
                values[name].append(float(match.group(1)))
    result: dict[str, Any] = {"sampleCount": len(samples)}
    if values["ram"]:
        result["ramUsedMiBMax"] = int(max(values["ram"]))
        result["ramUsedMiBAverage"] = round(
            sum(values["ram"]) / len(values["ram"]), 3
        )
    if values["cpu_temperature"]:
        result["cpuTemperatureCMax"] = max(values["cpu_temperature"])
    if values["gpu_temperature"]:
        result["gpuTemperatureCMax"] = max(values["gpu_temperature"])
    if values["gpu_power"]:
        result["gpuPowerMilliwattsMax"] = int(max(values["gpu_power"]))
    if values["input_power"]:
        result["inputPowerMilliwattsMax"] = int(max(values["input_power"]))
    return result


class _TegrastatsSampler:
    def __init__(self) -> None:
        self._executable = shutil.which("tegrastats")
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._samples: list[str] = []
        self._warning: str | None = None

    def _read_samples(self) -> None:
        if self._process is None or self._process.stdout is None:
            return
        for line in self._process.stdout:
            sample = line.strip()
            if sample:
                self._samples.append(sample)

    def start(self) -> None:
        if self._executable is None:
            self._warning = (
                "tegrastats is unavailable; Jetson GPU telemetry was not fabricated"
            )
            return
        try:
            self._process = subprocess.Popen(
                [self._executable, "--interval", "200"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            self._warning = f"tegrastats could not start: {exc}"
            return
        self._thread = threading.Thread(target=self._read_samples, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if self._thread is not None:
            self._thread.join(timeout=2)

    def report(self) -> tuple[dict[str, Any] | None, str | None]:
        if self._warning:
            return None, self._warning
        telemetry = _aggregate_tegrastats_samples(self._samples)
        telemetry["rawSamples"] = self._samples[-32:]
        warning = None
        if not self._samples:
            warning = "tegrastats produced no samples during the benchmark workload"
        return telemetry, warning


def _read_file(path: str) -> str:
    from backend.tools.file_ops import read_file

    return read_file(path)


class _HighWaterSampler:
    def __init__(self) -> None:
        self._process = psutil.Process()
        self._stop = threading.Event()
        self.process_rss = self._process.memory_info().rss
        self.system_available_low = psutil.virtual_memory().available
        self.temporary_disk_before = psutil.disk_usage(tempfile.gettempdir()).used
        self.temporary_disk_used = self.temporary_disk_before
        self._thread = threading.Thread(target=self._sample_until_stopped, daemon=True)

    def _sample(self) -> None:
        self.process_rss = max(self.process_rss, self._process.memory_info().rss)
        self.system_available_low = min(
            self.system_available_low, psutil.virtual_memory().available
        )
        self.temporary_disk_used = max(
            self.temporary_disk_used, psutil.disk_usage(tempfile.gettempdir()).used
        )

    def _sample_until_stopped(self) -> None:
        while not self._stop.wait(0.02):
            self._sample()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sample()


def _actual_counts(output: str, case: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    if "pages" in case:
        counts["pages"] = len(re.findall(r"\bsource_page=\d+\b", output))
    if "sheets" in case:
        counts["sheets"] = len(re.findall(r"\bsource_sheet=", output))
    if "images" in case:
        counts["images"] = output.count("### 嵌入图片：")
    return counts


def _run_case(
    case: dict[str, Any], *, include_warm: bool = True
) -> dict[str, Any]:
    path = Path(str(case["path"])).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.is_file():
        return {
            "id": case["id"],
            "status": "missing_fixture",
            "warning": f"fixture is absent: {case['path']}",
        }
    process = psutil.Process()
    memory_before = process.memory_info().rss
    system_before = psutil.virtual_memory().available
    cpu_before = psutil.cpu_percent(interval=None)
    sampler = _HighWaterSampler()
    sampler.start()
    started = time.monotonic()
    output = ""
    warm_output = ""
    warm_ms: float | None = None
    execution_error: str | None = None
    try:
        output = _read_file(str(path))
        cold_ms = round((time.monotonic() - started) * 1000, 3)
        if include_warm:
            started = time.monotonic()
            warm_output = _read_file(str(path))
            warm_ms = round((time.monotonic() - started) * 1000, 3)
    except Exception as exc:
        cold_ms = round((time.monotonic() - started) * 1000, 3)
        execution_error = f"{type(exc).__name__}: {exc}"
    finally:
        sampler.stop()
    memory_after = process.memory_info().rss
    expected_error = case.get("expectedError")
    def content_is_valid(value: str) -> bool:
        return (
            expected_error in value
            if expected_error
            else "[read_file failed]" not in value and bool(value.strip())
        )

    content_passed = content_is_valid(output)
    warm_content_passed = not include_warm or content_is_valid(warm_output)
    actual_counts = _actual_counts(output, case)
    warm_actual_counts = _actual_counts(warm_output, case) if include_warm else {}
    counts_passed = all(actual_counts[name] == case[name] for name in actual_counts)
    warm_counts_passed = not include_warm or all(
        warm_actual_counts[name] == case[name] for name in warm_actual_counts
    )
    passed = (
        execution_error is None
        and content_passed
        and warm_content_passed
        and counts_passed
        and warm_counts_passed
    )
    warnings = [
        "first-result latency is unavailable because read_file is a synchronous API",
        "retry count is unavailable in the rendered read_file contract",
    ]
    return {
        "id": case["id"],
        "status": "passed" if passed else "failed",
        "format": case.get("format"),
        "coldTotalMs": cold_ms,
        "firstResultMs": None,
        "warmTotalMs": warm_ms,
        "processRssBeforeBytes": memory_before,
        "processRssAfterBytes": memory_after,
        "processRssHighWaterBytes": sampler.process_rss,
        "systemAvailableBeforeBytes": system_before,
        "systemAvailableAfterBytes": psutil.virtual_memory().available,
        "systemAvailableLowWaterBytes": sampler.system_available_low,
        "cpuPercentBefore": cpu_before,
        "cpuPercentAfter": psutil.cpu_percent(interval=None),
        "outputCharacters": len(output),
        "warmOutputCharacters": len(warm_output),
        "executedRequests": 2 if include_warm else 1,
        "retryCount": None,
        "temporaryDiskHighWaterBytes": max(
            0, sampler.temporary_disk_used - sampler.temporary_disk_before
        ),
        "actualCounts": actual_counts,
        "warmActualCounts": warm_actual_counts,
        "expectedCounts": {
            name: case[name] for name in ("pages", "sheets", "images") if name in case
        },
        "warnings": warnings,
        "error": execution_error,
    }


def _run_soak(cases: list[dict[str, Any]], requests: int) -> dict[str, Any]:
    results = [
        _run_case(case, include_warm=False)
        for case in itertools.islice(itertools.cycle(cases), max(0, requests))
    ]
    passed = all(result.get("status") == "passed" for result in results)
    return {
        "requestedRequests": requests,
        "completedRequests": len(results),
        "status": "passed" if passed and len(results) == requests else "failed",
        "results": results,
    }


def _markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Read File v7 Benchmark Result",
        "",
        f"Generated: {report['generatedAt']}",
        "",
        "| Case | Status | Cold ms | Warm ms | Output chars |",
        "|---|---:|---:|---:|---:|",
    ]
    for case in report["cases"]:
        lines.append(
            f"| {case['id']} | {case['status']} | {case.get('coldTotalMs', '')} | "
            f"{case.get('warmTotalMs', '')} | {case.get('outputCharacters', '')} |"
        )
    if report.get("warnings"):
        lines.extend(["", "## Warnings"])
        lines.extend(f"- {warning}" for warning in report["warnings"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--soak", type=int, default=0)
    args = parser.parse_args(argv)
    manifest = load_manifest(args.manifest)
    warnings: list[str] = []
    telemetry_sampler = _TegrastatsSampler()
    telemetry_sampler.start()
    soak = None
    try:
        if args.smoke:
            cases = [
                {"id": case["id"], "status": "manifest_validated"}
                for case in manifest["cases"]
            ]
        elif args.soak > 0:
            soak = _run_soak(manifest["cases"], args.soak)
            cases = soak["results"]
            warnings.extend(
                warning
                for case in cases
                for warning in case.get("warnings", [])
            )
        else:
            cases = [_run_case(case) for case in manifest["cases"]]
            warnings.extend(
                warning
                for case in cases
                for warning in (
                    [case["warning"]]
                    if case.get("warning")
                    else case.get("warnings", [])
                )
            )
    finally:
        telemetry_sampler.stop()
    telemetry, telemetry_warning = telemetry_sampler.report()
    if telemetry_warning:
        warnings.append(telemetry_warning)
    acceptance_complete = bool(cases) and all(
        case.get("status") == "passed" for case in cases
    )
    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "platform": sys.platform,
        "smoke": args.smoke,
        "soakRequests": args.soak,
        "soak": soak,
        "acceptanceComplete": acceptance_complete,
        "jetsonTelemetry": telemetry,
        "cases": cases,
        "warnings": warnings,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    args.output.with_suffix(".md").write_text(_markdown_report(report), encoding="utf-8")
    print("benchmark smoke passed" if args.smoke else f"benchmark completed: {args.output}")
    return 0 if args.smoke or acceptance_complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
