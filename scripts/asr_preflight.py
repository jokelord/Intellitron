#!/usr/bin/env python3
"""Validate Qwen3-ASR files and serialize launcher values from one YAML."""

from __future__ import annotations

import argparse
import shlex
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.asr.config import (
    EXPECTED_ASR_ARCHITECTURE,
    AsrConfig,
    load_asr_config,
    validate_asr_model_artifacts,
)


EXPECTED_ARCHITECTURE = EXPECTED_ASR_ARCHITECTURE


class ModelPreflightError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelPreflightReport:
    status: str
    architecture: str | None
    shard_count: int
    port: int
    model_path: Path
    config: AsrConfig


def _port_is_free(host: str, port: int) -> bool:
    try:
        addresses = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            flags=socket.AI_PASSIVE,
        )
    except socket.gaierror:
        return False

    checked = set()
    for family, socktype, protocol, _, address in addresses:
        key = (family, address)
        if key in checked:
            continue
        checked.add(key)
        sock = socket.socket(family, socktype, protocol)
        try:
            sock.bind(address)
        except OSError:
            return False
        finally:
            sock.close()
    return bool(checked)


def validate_asr_model(
    config_path: Path,
    *,
    check_port: bool = True,
    check_registry: bool = True,
) -> ModelPreflightReport:
    config_path = config_path.resolve()
    try:
        config = load_asr_config(config_path, project_root=PROJECT_ROOT)
        configured_path = Path(config.sglang.model_path).expanduser()
        model_path = (
            configured_path
            if configured_path.is_absolute()
            else PROJECT_ROOT / configured_path
        )
        if not config.enabled:
            return ModelPreflightReport(
                "disabled", None, 0, config.sglang.port, model_path, config
            )
        artifacts = validate_asr_model_artifacts(model_path)
    except (OSError, ValueError) as exc:
        raise ModelPreflightError(f"ASR config/model validation failed: {exc}") from exc

    if check_registry:
        from sglang.srt.models.registry import ModelRegistry

        if EXPECTED_ARCHITECTURE not in ModelRegistry.get_supported_archs():
            raise ModelPreflightError(
                f"installed SGLang does not support {EXPECTED_ARCHITECTURE}"
            )
    if check_port and not _port_is_free(config.sglang.host, config.sglang.port):
        raise ModelPreflightError(
            f"ASR SGLang port is already occupied: {config.sglang.host}:{config.sglang.port}"
        )
    return ModelPreflightReport(
        "enabled",
        EXPECTED_ARCHITECTURE,
        len(artifacts.shards),
        config.sglang.port,
        model_path,
        config,
    )


def _shell_assignments(report: ModelPreflightReport) -> str:
    config = report.config
    values = {
        "ASR_ENABLED": str(config.enabled).lower(),
        "MODEL_PATH": str(report.model_path),
        "ASR_HOST": config.sglang.host,
        "ASR_PORT": config.sglang.port,
        "SERVED_MODEL_NAME": config.sglang.served_model_name,
        "DTYPE": config.sglang.dtype,
        "MEM_FRACTION_STATIC": config.sglang.mem_fraction_static,
        "CONTEXT_LENGTH": config.sglang.context_length,
        "MAX_TOTAL_TOKENS": config.sglang.max_total_tokens,
        "MAX_RUNNING_REQUESTS": config.sglang.max_running_requests,
        "TRUST_REMOTE_CODE": str(config.sglang.trust_remote_code).lower(),
        "HEALTH_TIMEOUT_SECONDS": config.sglang.health_timeout_seconds,
        "STARTUP_TIMEOUT_SECONDS": config.sglang.startup_timeout_seconds,
        "SHUTDOWN_TIMEOUT_SECONDS": config.runtime.shutdown_timeout_seconds,
    }
    return "\n".join(f"{key}={shlex.quote(str(value))}" for key, value in values.items())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--shell", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    report = validate_asr_model(args.config)
    if args.shell:
        print(_shell_assignments(report))
    elif report.status == "disabled":
        print("ASR preflight skipped: ASR is disabled in the typed configuration.")
    else:
        print(
            f"ASR preflight passed: architecture={report.architecture}, "
            f"shards={report.shard_count}, port={report.port}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
