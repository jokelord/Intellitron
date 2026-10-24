"""Strict configuration for browser capture, VAD, and SGLang ASR."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import shutil
import ssl
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
import numpy as np
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    computed_field,
    model_validator,
)


EXPECTED_ASR_ARCHITECTURE = "Qwen3ASRForConditionalGeneration"
TLS_REGENERATE_GUIDANCE = "bash scripts/generate_asr_tls_cert.sh --force"


def _gain_step_aligned(value: float, minimum: float, step: float) -> bool:
    ticks = (value - minimum) / step
    return math.isclose(ticks, round(ticks), rel_tol=0.0, abs_tol=1e-7)


def _float32_gain_factor_is_valid(gain_db: float) -> bool:
    try:
        factor = 10.0 ** (gain_db / 20.0)
    except OverflowError:
        return False
    minimum_positive_float32 = float(
        np.nextafter(np.float32(0.0), np.float32(1.0))
    )
    return minimum_positive_float32 <= factor <= float(np.finfo(np.float32).max)


@dataclass(frozen=True, slots=True)
class AsrModelArtifacts:
    architecture: str
    shards: tuple[Path, ...]


def _require_readable_file(path: Path, field_name: str) -> None:
    if not path.is_file():
        raise ValueError(f"{field_name} does not exist: {path}")
    if not os.access(path, os.R_OK):
        raise ValueError(f"{field_name} is not readable: {path}")


def _read_json(path: Path, field_name: str) -> object:
    _require_readable_file(path, field_name)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field_name} is not valid readable JSON: {path}: {exc}") from exc


def validate_asr_model_artifacts(model_path: Path) -> AsrModelArtifacts:
    """Validate the complete local Qwen3-ASR model artifact set."""
    model_path = model_path.expanduser()
    if not model_path.is_dir():
        raise ValueError(f"sglang.model_path is not a directory: {model_path}")
    if not os.access(model_path, os.R_OK | os.X_OK):
        raise ValueError(f"sglang.model_path is not readable: {model_path}")

    config_path = model_path / "config.json"
    tokenizer_path = model_path / "tokenizer_config.json"
    index_path = model_path / "model.safetensors.index.json"
    model_config = _read_json(config_path, "sglang.model_path/config.json")
    _require_readable_file(
        tokenizer_path, "sglang.model_path/tokenizer_config.json"
    )
    tokenizer_asset_sets = (
        (model_path / "tokenizer.json",),
        (model_path / "tokenizer.model",),
        (model_path / "vocab.json", model_path / "merges.txt"),
    )
    if not any(
        all(path.is_file() and os.access(path, os.R_OK) for path in asset_set)
        for asset_set in tokenizer_asset_sets
    ):
        raise ValueError(
            "sglang.model_path requires readable tokenizer vocabulary assets: "
            "tokenizer.json, tokenizer.model, or vocab.json plus merges.txt"
        )
    index = _read_json(index_path, "sglang.model_path/model.safetensors.index.json")

    architectures = model_config.get("architectures") if isinstance(model_config, dict) else None
    if (
        not isinstance(architectures, list)
        or EXPECTED_ASR_ARCHITECTURE not in architectures
    ):
        raise ValueError(
            f"sglang.model_path config.json architectures must include "
            f"{EXPECTED_ASR_ARCHITECTURE}: {architectures}"
        )

    weight_map = index.get("weight_map") if isinstance(index, dict) else None
    if not isinstance(weight_map, dict) or not weight_map:
        raise ValueError("sglang.model_path model.safetensors.index.json has no weight_map")
    shard_values = list(weight_map.values())
    if any(not isinstance(value, str) or not value.strip() for value in shard_values):
        raise ValueError(
            "sglang.model_path model.safetensors.index.json contains an invalid shard name"
        )
    shard_names = sorted(set(shard_values))
    for name in shard_names:
        shard_path = Path(name)
        if shard_path.is_absolute() or ".." in shard_path.parts:
            raise ValueError(
                "sglang.model_path model.safetensors.index.json contains an "
                f"unsafe shard path: {name}"
            )

    shards = tuple(model_path / name for name in shard_names)
    for shard in shards:
        _require_readable_file(shard, "sglang.model_path indexed shard")
    return AsrModelArtifacts(EXPECTED_ASR_ARCHITECTURE, shards)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BrowserConstraintsConfig(StrictModel):
    echo_cancellation: bool
    noise_suppression: bool
    auto_gain_control: bool


class AudioConfig(StrictModel):
    sample_rate_hz: Literal[16000]
    channels: Literal[1]
    sample_format: Literal["float32"]
    capture_frame_ms: int = Field(gt=0)
    input_gain_db: float = Field(allow_inf_nan=False)
    input_gain_min_db: float = Field(allow_inf_nan=False)
    input_gain_max_db: float = Field(allow_inf_nan=False)
    input_gain_step_db: float = Field(gt=0.0, allow_inf_nan=False)
    clip_limit: float = Field(gt=0.0, le=1.0)
    browser_constraints: BrowserConstraintsConfig

    @model_validator(mode="after")
    def validate_input_gain_range(self) -> "AudioConfig":
        if self.input_gain_min_db >= self.input_gain_max_db:
            raise ValueError("input_gain_min_db must be less than input_gain_max_db")
        if not (
            self.input_gain_min_db
            <= self.input_gain_db
            <= self.input_gain_max_db
        ):
            raise ValueError("input_gain_db must be within the configured range")
        width = self.input_gain_max_db - self.input_gain_min_db
        if self.input_gain_step_db > width:
            raise ValueError("input_gain_step_db must not exceed the configured range")
        if not _gain_step_aligned(
            self.input_gain_db,
            self.input_gain_min_db,
            self.input_gain_step_db,
        ):
            raise ValueError("input_gain_db must align to input_gain_step_db")
        if not _gain_step_aligned(
            self.input_gain_max_db,
            self.input_gain_min_db,
            self.input_gain_step_db,
        ):
            raise ValueError("input_gain_max_db must align to input_gain_step_db")
        for field_name, gain_db in (
            ("input_gain_min_db", self.input_gain_min_db),
            ("input_gain_max_db", self.input_gain_max_db),
            ("input_gain_db", self.input_gain_db),
        ):
            if not _float32_gain_factor_is_valid(gain_db):
                raise ValueError(
                    f"{field_name} cannot produce a Float32-compatible linear gain"
                )
        return self

    def accepts_input_gain_db(self, value: object) -> bool:
        if type(value) not in (int, float):
            return False
        gain_db = float(value)
        return (
            math.isfinite(gain_db)
            and self.input_gain_min_db <= gain_db <= self.input_gain_max_db
            and _gain_step_aligned(
                gain_db, self.input_gain_min_db, self.input_gain_step_db
            )
            and _float32_gain_factor_is_valid(gain_db)
        )

    @computed_field
    @property
    def frame_samples(self) -> int:
        total = self.sample_rate_hz * self.capture_frame_ms
        if total % 1000:
            raise ValueError(
                "audio.capture_frame_ms must produce an integer sample count"
            )
        return total // 1000


class RecordingConfig(StrictModel):
    pretrigger_buffer_ms: int = Field(ge=0)
    posttrigger_tail_ms: int = Field(ge=0)
    max_utterance_ms: int = Field(gt=0)
    min_utterance_ms: int = Field(gt=0)
    candidate_confirmation_timeout_ms: int = Field(gt=0)
    end_no_voice_duration_ms: int = Field(gt=0)
    end_no_voice_trim_pad_ms: int = Field(ge=0)
    prevent_segment_overlap: Literal[True]

    @model_validator(mode="after")
    def validate_duration_bounds(self) -> "RecordingConfig":
        lower_bounds = {
            "min_utterance_ms": self.min_utterance_ms,
            "candidate_confirmation_timeout_ms": self.candidate_confirmation_timeout_ms,
            "end_no_voice_duration_ms": self.end_no_voice_duration_ms,
            "posttrigger_tail_ms": self.posttrigger_tail_ms,
        }
        invalid = [name for name, value in lower_bounds.items() if value >= self.max_utterance_ms]
        if invalid:
            raise ValueError(
                "recording.max_utterance_ms must exceed " + ", ".join(invalid)
            )
        if self.end_no_voice_trim_pad_ms > self.end_no_voice_duration_ms:
            raise ValueError(
                "recording.end_no_voice_trim_pad_ms must not exceed "
                "recording.end_no_voice_duration_ms"
            )
        return self


class AudioTriggerConfig(StrictModel):
    enabled: bool
    metric: Literal["rms_dbfs"]
    start_threshold_dbfs: float = Field(le=0.0, allow_inf_nan=False)
    min_trigger_duration_ms: int = Field(gt=0)
    require_vad_confirmation: bool


class VadConfig(StrictModel):
    engine: Literal["silero"]
    version: Literal["6.2"]
    backend: Literal["onnx"]
    model_path: str = Field(min_length=1)
    device: Literal["cpu"]
    threshold: float = Field(gt=0.0, lt=1.0)
    min_silence_duration_ms: int = Field(ge=0)
    speech_pad_ms: int = Field(ge=0)
    frame_samples: Literal[512]
    compute_gating_enabled: bool
    compute_wake_threshold_dbfs: float = Field(le=0.0, allow_inf_nan=False)
    compute_suspend_after_quiet_ms: int = Field(gt=0)
    compute_warmup_context_ms: int = Field(ge=0)


class SGLangAsrConfig(StrictModel):
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    served_model_name: str = Field(min_length=1)
    model_path: str = Field(min_length=1)
    dtype: Literal["bfloat16", "float16", "float32"]
    mem_fraction_static: float = Field(gt=0.0, le=1.0)
    context_length: int = Field(gt=0)
    max_total_tokens: int = Field(gt=0)
    max_running_requests: int = Field(gt=0)
    trust_remote_code: bool
    health_timeout_seconds: float = Field(gt=0.0, allow_inf_nan=False)
    startup_timeout_seconds: float = Field(gt=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_token_pool_capacity(self) -> "SGLangAsrConfig":
        if self.max_total_tokens < self.context_length:
            raise ValueError(
                "sglang.max_total_tokens must be greater than or equal to "
                "sglang.context_length"
            )
        return self

    @computed_field
    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class TranscriptionConfig(StrictModel):
    streaming: bool
    auto_send: bool
    language: str | None
    temperature: float = Field(ge=0.0)
    response_format: Literal["json", "text", "verbose_json"]
    request_timeout_seconds: int = Field(gt=0)


class DictationMagicSendConfig(StrictModel):
    commands: list[str]
    poll_interval_ms: int = Field(ge=100)

    @model_validator(mode="after")
    def validate_commands(self) -> "DictationMagicSendConfig":
        if any(not command.strip() for command in self.commands):
            raise ValueError("dictation_magic_send.commands cannot contain blank commands")
        return self


class TransportConfig(StrictModel):
    websocket_path: str = Field(min_length=1)
    public_websocket_url: str | None
    handshake_timeout_seconds: int = Field(gt=0)
    max_binary_frame_bytes: int = Field(gt=0)
    max_buffered_frames: int = Field(gt=0)
    level_event_interval_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_urls(self) -> "TransportConfig":
        if not self.websocket_path.startswith("/"):
            raise ValueError("transport.websocket_path must start with '/'")
        if self.public_websocket_url:
            scheme = urlparse(self.public_websocket_url).scheme
            if scheme not in {"ws", "wss"}:
                raise ValueError(
                    "transport.public_websocket_url must use ws or wss"
                )
        return self


class RuntimeConfig(StrictModel):
    config_reload_enabled: Literal[False]
    fail_on_unknown_config: Literal[True]
    max_active_sessions: int = Field(gt=0)
    max_pending_utterances_per_session: Literal[1]
    asr_worker_concurrency: int = Field(gt=0)
    shutdown_timeout_seconds: int = Field(gt=0)


class BrowserSecurityConfig(StrictModel):
    https_enabled: bool
    require_secure_context_for_voice: bool
    ca_certificate_path: str = Field(min_length=1)
    certificate_path: str = Field(min_length=1)
    private_key_path: str = Field(min_length=1)
    subject_alt_names: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_subject_alt_names(self) -> "BrowserSecurityConfig":
        for value in self.subject_alt_names:
            normalized = value.strip()
            if not normalized:
                raise ValueError("browser_security.subject_alt_names cannot be empty")
            try:
                ipaddress.ip_address(normalized)
            except ValueError:
                if "." not in normalized and normalized != "localhost":
                    raise ValueError(
                        f"invalid browser_security subject alternative name: {value}"
                    )
        return self

    @model_validator(mode="after")
    def validate_https_files(self, info: ValidationInfo) -> "BrowserSecurityConfig":
        if not self.https_enabled:
            return self
        context = info.context or {}
        project_root = Path(context.get("project_root", Path.cwd()))
        resolved_paths = {}
        for field_name in (
            "ca_certificate_path",
            "certificate_path",
            "private_key_path",
        ):
            resolved = _resolve_path(project_root, getattr(self, field_name))
            _require_readable_file(resolved, f"browser_security.{field_name}")
            resolved_paths[field_name] = resolved

        certificate_path = resolved_paths["certificate_path"]
        try:
            decoded = ssl._ssl._test_decode_cert(str(certificate_path))
        except (OSError, ValueError, ssl.SSLError) as exc:
            raise ValueError(
                f"browser_security.certificate_path cannot be decoded: "
                f"{certificate_path}: {exc}. Regenerate it with: "
                + TLS_REGENERATE_GUIDANCE
            ) from exc

        openssl = shutil.which("openssl")
        if openssl is None:
            raise ValueError(
                "browser_security HTTPS trust validation requires openssl. "
                f"Install openssl and regenerate with: {TLS_REGENERATE_GUIDANCE}"
            )

        def run_openssl(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
            try:
                return subprocess.run(
                    [openssl, *arguments],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ValueError(
                    "browser_security HTTPS trust validation could not run. "
                    f"Regenerate with: {TLS_REGENERATE_GUIDANCE}"
                ) from exc

        ca_certificate_path = resolved_paths["ca_certificate_path"]
        verify_result = run_openssl(
            [
                "verify",
                "-trusted",
                str(ca_certificate_path),
                "-purpose",
                "sslserver",
                str(certificate_path),
            ]
        )
        if verify_result.returncode != 0:
            raise ValueError(
                "browser_security server certificate does not verify against "
                "configured CA. Regenerate with: "
                + TLS_REGENERATE_GUIDANCE
            )

        private_key_path = resolved_paths["private_key_path"]
        certificate_public_key = run_openssl(
            ["x509", "-in", str(certificate_path), "-pubkey", "-noout"]
        )
        private_public_key = run_openssl(
            ["pkey", "-in", str(private_key_path), "-pubout"]
        )
        if (
            certificate_public_key.returncode != 0
            or private_public_key.returncode != 0
            or certificate_public_key.stdout != private_public_key.stdout
        ):
            raise ValueError(
                "browser_security private key does not match server certificate. "
                "Regenerate with: "
                + TLS_REGENERATE_GUIDANCE
            )

        actual_sans = set()
        for kind, value in decoded.get("subjectAltName", ()):
            if kind == "DNS":
                actual_sans.add(("DNS", value.lower()))
            elif kind == "IP Address":
                try:
                    actual_sans.add(("IP", str(ipaddress.ip_address(value))))
                except ValueError:
                    continue

        expected_sans = set()
        for value in self.subject_alt_names:
            normalized = value.strip()
            try:
                expected_sans.add(("IP", str(ipaddress.ip_address(normalized))))
            except ValueError:
                expected_sans.add(("DNS", normalized.lower()))
        required_internal_sans = {("DNS", "localhost"), ("IP", "127.0.0.1")}
        missing_internal = sorted(required_internal_sans - expected_sans)
        if missing_internal:
            formatted = ", ".join(
                f"{kind}:{value}" for kind, value in missing_internal
            )
            raise ValueError(
                "browser_security.subject_alt_names must include internal backend "
                f"health and Next proxy names: {formatted}. Regenerate it with: "
                + TLS_REGENERATE_GUIDANCE
            )
        missing = sorted(expected_sans - actual_sans)
        unexpected = sorted(actual_sans - expected_sans)
        if missing or unexpected:
            differences = []
            if missing:
                differences.append(
                    "missing configured SANs: "
                    + ", ".join(f"{kind}:{value}" for kind, value in missing)
                )
            if unexpected:
                differences.append(
                    "unexpected SANs: "
                    + ", ".join(f"{kind}:{value}" for kind, value in unexpected)
                )
            raise ValueError(
                "browser_security.certificate_path SANs must exactly match "
                "browser_security.subject_alt_names with DNS/IP kinds; "
                + "; ".join(differences)
                + ". Regenerate it with: "
                + TLS_REGENERATE_GUIDANCE
            )
        return self


class ObservabilityConfig(StrictModel):
    save_debug_audio: bool
    debug_audio_directory: str = Field(min_length=1)
    log_vad_probability: bool
    log_audio_level: bool
    log_state_transitions: bool


class DebugConfig(StrictModel):
    replay: bool


class AsrConfig(StrictModel):
    version: Literal[1]
    enabled: bool
    audio: AudioConfig
    recording: RecordingConfig
    audio_trigger: AudioTriggerConfig
    vad: VadConfig
    sglang: SGLangAsrConfig
    transcription: TranscriptionConfig
    dictation_magic_send: DictationMagicSendConfig
    transport: TransportConfig
    runtime: RuntimeConfig
    browser_security: BrowserSecurityConfig
    debug: DebugConfig
    observability: ObservabilityConfig

    @model_validator(mode="after")
    def validate_cross_section_constraints(self, info: ValidationInfo) -> "AsrConfig":
        _ = self.audio.frame_samples
        required_frame_bytes = self.audio.frame_samples * 4
        if self.transport.max_binary_frame_bytes < required_frame_bytes:
            raise ValueError(
                "transport.max_binary_frame_bytes must be at least "
                f"audio.frame_samples * 4 ({required_frame_bytes})"
            )
        if (
            self.browser_security.https_enabled
            and self.transport.public_websocket_url
            and urlparse(self.transport.public_websocket_url).scheme != "wss"
        ):
            raise ValueError(
                "transport.public_websocket_url must use wss when "
                "browser_security.https_enabled is true"
            )
        if self.runtime.asr_worker_concurrency > self.sglang.max_running_requests:
            raise ValueError(
                "runtime.asr_worker_concurrency must not exceed "
                "sglang.max_running_requests"
            )
        if (
            self.audio_trigger.enabled
            and self.vad.compute_gating_enabled
            and self.vad.compute_wake_threshold_dbfs
            > self.audio_trigger.start_threshold_dbfs
        ):
            raise ValueError(
                "vad.compute_wake_threshold_dbfs must not exceed "
                "audio_trigger.start_threshold_dbfs when the SPL trigger is "
                "enabled; the wake line must let Silero warm up before the "
                "SPL trigger can fire"
            )
        if self.enabled:
            context = info.context or {}
            project_root = Path(context.get("project_root", Path.cwd()))
            validate_asr_model_artifacts(
                _resolve_path(project_root, self.sglang.model_path)
            )
        return self


def _resolve_path(project_root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else project_root / path


def load_asr_config(path: Path, *, project_root: Path) -> AsrConfig:
    """Load a complete ASR YAML file and reject invalid or unknown values."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"ASR config must be a mapping: {path}")

    return AsrConfig.model_validate(raw, context={"project_root": project_root})
