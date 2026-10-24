from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError


def _write_fake_model(tmp_path: Path) -> Path:
    model = tmp_path / "Qwen3-ASR-1.7B"
    model.mkdir(exist_ok=True)
    (model / "config.json").write_text(
        json.dumps({"architectures": ["Qwen3ASRForConditionalGeneration"]}),
        encoding="utf-8",
    )
    (model / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    (model / "vocab.json").write_text("{}", encoding="utf-8")
    (model / "merges.txt").write_text("#version: 0.2\n", encoding="utf-8")
    (model / "model.safetensors.index.json").write_text(
        json.dumps(
            {
                "weight_map": {
                    "a": "model-00001-of-00002.safetensors",
                    "b": "model-00002-of-00002.safetensors",
                    "c": "model-00002-of-00002.safetensors",
                }
            }
        ),
        encoding="utf-8",
    )
    (model / "model-00001-of-00002.safetensors").write_bytes(b"one")
    (model / "model-00002-of-00002.safetensors").write_bytes(b"two")
    return model


def _valid_config(tmp_path: Path) -> dict:
    model = _write_fake_model(tmp_path)
    return {
        "version": 1,
        "enabled": True,
        "audio": {
            "sample_rate_hz": 16000,
            "channels": 1,
            "sample_format": "float32",
            "capture_frame_ms": 20,
            "input_gain_db": 12.0,
            "input_gain_min_db": -24.0,
            "input_gain_max_db": 36.0,
            "input_gain_step_db": 0.5,
            "clip_limit": 0.999,
            "browser_constraints": {
                "echo_cancellation": False,
                "noise_suppression": False,
                "auto_gain_control": False,
            },
        },
        "recording": {
            "pretrigger_buffer_ms": 1000,
            "posttrigger_tail_ms": 250,
            "max_utterance_ms": 30000,
            "min_utterance_ms": 200,
            "candidate_confirmation_timeout_ms": 500,
            "end_no_voice_duration_ms": 800,
            "end_no_voice_trim_pad_ms": 200,
            "prevent_segment_overlap": True,
        },
        "audio_trigger": {
            "enabled": True,
            "metric": "rms_dbfs",
            "start_threshold_dbfs": -40.0,
            "min_trigger_duration_ms": 40,
            "require_vad_confirmation": True,
        },
        "vad": {
            "engine": "silero",
            "version": "6.2",
            "backend": "onnx",
            "model_path": "package://silero_vad/data/silero_vad.onnx",
            "device": "cpu",
            "threshold": 0.5,
            "min_silence_duration_ms": 500,
            "speech_pad_ms": 30,
            "frame_samples": 512,
            "compute_gating_enabled": True,
            "compute_wake_threshold_dbfs": -45.0,
            "compute_suspend_after_quiet_ms": 2000,
            "compute_warmup_context_ms": 240,
        },
        "sglang": {
            "host": "127.0.0.1",
            "port": 30003,
            "served_model_name": "Qwen3-ASR-1.7B",
            "model_path": str(model),
            "dtype": "bfloat16",
            "mem_fraction_static": 0.08,
            "context_length": 16384,
            "max_total_tokens": 16384,
            "max_running_requests": 1,
            "trust_remote_code": False,
            "health_timeout_seconds": 2.0,
            "startup_timeout_seconds": 300.0,
        },
        "transcription": {
            "streaming": False,
            "auto_send": True,
            "language": None,
            "temperature": 0.0,
            "response_format": "json",
            "request_timeout_seconds": 300,
        },
        "dictation_magic_send": {
            "commands": ["Over, over.", "Over, over. ", "over, over. ", "over, over."],
            "poll_interval_ms": 500,
        },
        "transport": {
            "websocket_path": "/v1/asr/stream",
            "public_websocket_url": None,
            "handshake_timeout_seconds": 10,
            "max_binary_frame_bytes": 65536,
            "max_buffered_frames": 250,
            "level_event_interval_ms": 100,
        },
        "runtime": {
            "config_reload_enabled": False,
            "fail_on_unknown_config": True,
            "max_active_sessions": 4,
            "max_pending_utterances_per_session": 1,
            "asr_worker_concurrency": 1,
            "shutdown_timeout_seconds": 15,
        },
        "browser_security": {
            "https_enabled": False,
            "require_secure_context_for_voice": True,
            "ca_certificate_path": "data/asr-tls/ca.crt",
            "certificate_path": "data/asr-tls/server.crt",
            "private_key_path": "data/asr-tls/server.key",
            "subject_alt_names": ["localhost", "127.0.0.1"],
        },
        "debug": {
            "replay": False,
        },
        "observability": {
            "save_debug_audio": False,
            "debug_audio_directory": "data/debug-audio",
            "log_vad_probability": False,
            "log_audio_level": False,
            "log_state_transitions": True,
        },
    }


def _write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config-asr.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_asr_config_loads_defaults_and_derived_values(tmp_path: Path):
    from backend.asr.config import load_asr_config

    config = load_asr_config(
        _write_config(tmp_path, _valid_config(tmp_path)), project_root=tmp_path
    )

    assert config.audio.frame_samples == 320
    assert config.audio.input_gain_db == 12.0
    assert config.audio.input_gain_min_db == -24.0
    assert config.audio.input_gain_max_db == 36.0
    assert config.audio.input_gain_step_db == 0.5
    assert config.audio.accepts_input_gain_db(6.5)
    assert not config.audio.accepts_input_gain_db(True)
    assert not config.audio.accepts_input_gain_db(6.25)
    assert config.sglang.port == 30003
    assert config.sglang.health_timeout_seconds == 2.0
    assert config.sglang.startup_timeout_seconds == 300.0
    assert config.sglang.context_length == 16384
    assert config.sglang.max_total_tokens == 16384
    assert config.sglang.base_url == "http://127.0.0.1:30003"
    assert config.transcription.streaming is False
    assert config.transcription.auto_send is True
    assert config.debug.replay is False


def test_asr_config_preserves_exact_dictation_magic_send_commands(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["dictation_magic_send"] = {
        "commands": ["Over, over.", " Over, over. ", "over, over.  "],
        "poll_interval_ms": 100,
    }

    config = load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)

    assert config.dictation_magic_send.commands == [
        "Over, over.",
        " Over, over. ",
        "over, over.  ",
    ]
    assert config.dictation_magic_send.poll_interval_ms == 100


@pytest.mark.parametrize("commands", [[""], ["   "], ["Over, over.", "\t"]])
def test_asr_config_rejects_blank_dictation_magic_send_commands(
    tmp_path: Path, commands: list[str]
):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["dictation_magic_send"]["commands"] = commands

    with pytest.raises(ValidationError, match="commands"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


@pytest.mark.parametrize("interval", [99, 0, -1])
def test_asr_config_rejects_dictation_magic_send_interval_below_100ms(
    tmp_path: Path, interval: int
):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["dictation_magic_send"]["poll_interval_ms"] = interval

    with pytest.raises(ValidationError, match="poll_interval_ms"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_asr_config_rejects_unknown_dictation_magic_send_fields(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["dictation_magic_send"]["unexpected"] = True

    with pytest.raises(ValidationError, match="unexpected"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"input_gain_min_db": 12.0, "input_gain_max_db": 12.0}, "min"),
        ({"input_gain_min_db": 13.0}, "input_gain_db"),
        ({"input_gain_max_db": 11.0}, "input_gain_db"),
        ({"input_gain_step_db": 0.0}, "step"),
        ({"input_gain_step_db": 100.0}, "step"),
        ({"input_gain_db": 12.25}, "step"),
        ({"input_gain_max_db": 36.25}, "step"),
    ],
)
def test_asr_config_rejects_invalid_input_gain_range(
    tmp_path: Path, updates: dict, message: str
):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["audio"].update(updates)

    with pytest.raises(ValidationError, match=message):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_gain_db", float("nan")),
        ("input_gain_min_db", float("inf")),
        ("input_gain_max_db", float("-inf")),
    ],
)
def test_asr_config_rejects_non_finite_input_gain_values(
    tmp_path: Path, field: str, value: float
):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["audio"][field] = value

    with pytest.raises(ValidationError, match=field):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_asr_config_rejects_input_gain_outside_float32_linear_path(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["audio"].update(
        {
            "input_gain_db": 0.0,
            "input_gain_min_db": -1e50,
            "input_gain_max_db": 1e50,
            "input_gain_step_db": 1e50,
        }
    )

    with pytest.raises(ValidationError, match="Float32"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_asr_config_rejects_finite_gain_that_underflows_float32(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["audio"].update(
        {
            "input_gain_db": 0.0,
            "input_gain_min_db": -1000.0,
            "input_gain_max_db": 0.0,
            "input_gain_step_db": 100.0,
        }
    )

    with pytest.raises(ValidationError, match="Float32"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_asr_config_rejects_token_pool_smaller_than_context(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["sglang"]["context_length"] = 16384
    data["sglang"]["max_total_tokens"] = 8192

    with pytest.raises(ValidationError, match="max_total_tokens"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_asr_config_rejects_unknown_fields(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["audio"]["mystery_gain"] = 99
    with pytest.raises(ValidationError, match="mystery_gain"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_asr_config_rejects_non_boolean_debug_replay(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["debug"]["replay"] = "true"
    with pytest.raises(ValidationError, match="replay"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


@pytest.mark.parametrize(
    "start",
    [-40.0, -30.0, -46.0],
)
def test_asr_config_allows_arbitrary_start_threshold(
    tmp_path: Path, start: float
):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["audio_trigger"]["start_threshold_dbfs"] = start
    data["vad"]["compute_wake_threshold_dbfs"] = min(-45.0, start - 5.0)
    config = load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)

    assert config.audio_trigger.start_threshold_dbfs == start


def test_asr_config_rejects_removed_release_threshold_field(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["audio_trigger"]["release_threshold_dbfs"] = -46.0

    with pytest.raises(ValidationError, match="release_threshold_dbfs"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_asr_config_rejects_vad_wake_threshold_above_spl_start(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["vad"]["compute_wake_threshold_dbfs"] = -39.0
    data["audio_trigger"]["start_threshold_dbfs"] = -40.0

    with pytest.raises(ValidationError, match="compute_wake_threshold_dbfs"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_asr_config_rejects_trim_pad_exceeding_no_voice_window(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["recording"]["end_no_voice_trim_pad_ms"] = (
        data["recording"]["end_no_voice_duration_ms"] + 1
    )

    with pytest.raises(ValidationError, match="end_no_voice_trim_pad_ms"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_asr_config_wake_threshold_may_ignore_gating_when_trigger_disabled(
    tmp_path: Path,
):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["audio_trigger"]["enabled"] = False
    data["vad"]["compute_wake_threshold_dbfs"] = -10.0

    config = load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)

    assert config.vad.compute_gating_enabled is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start_threshold_dbfs", float("-inf")),
        ("start_threshold_dbfs", float("nan")),
    ],
)
def test_asr_config_rejects_non_finite_audio_thresholds(
    tmp_path: Path, field: str, value: float
):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["audio_trigger"][field] = value

    with pytest.raises(ValidationError, match=field):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_asr_config_requires_integer_capture_frame_samples(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["audio"]["capture_frame_ms"] = 20.5
    with pytest.raises(ValidationError, match="capture_frame_ms"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


@pytest.mark.parametrize("enabled", [True, False])
def test_asr_config_requires_binary_limit_to_fit_one_pcm_frame(
    tmp_path: Path, enabled: bool
):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["enabled"] = enabled
    data["transport"]["max_binary_frame_bytes"] = 1279

    with pytest.raises(ValidationError, match="max_binary_frame_bytes"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_asr_config_rejects_disabled_segment_overlap_prevention(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["recording"]["prevent_segment_overlap"] = False

    with pytest.raises(ValidationError) as exc_info:
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)

    errors = exc_info.value.errors()
    assert any(
        error["loc"] == ("recording", "prevent_segment_overlap")
        and "True" in error["msg"]
        for error in errors
    )


def test_asr_config_requires_https_files_when_enabled(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["browser_security"]["https_enabled"] = True
    with pytest.raises(ValidationError, match="ca_certificate_path"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


@pytest.mark.parametrize(
    "missing_name",
    ["config.json", "tokenizer_config.json", "model.safetensors.index.json"],
)
def test_enabled_asr_requires_model_metadata_files(
    tmp_path: Path, missing_name: str
):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    (Path(data["sglang"]["model_path"]) / missing_name).unlink()

    with pytest.raises(ValidationError, match=missing_name):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


@pytest.mark.parametrize(
    "tokenizer_assets",
    [("tokenizer.json",), ("tokenizer.model",), ("vocab.json", "merges.txt")],
)
def test_enabled_asr_accepts_supported_tokenizer_vocabulary_assets(
    tmp_path: Path, tokenizer_assets: tuple[str, ...]
):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    model = Path(data["sglang"]["model_path"])
    (model / "vocab.json").unlink()
    (model / "merges.txt").unlink()
    for name in tokenizer_assets:
        (model / name).write_bytes(b"tokenizer-vocabulary")

    config = load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)

    assert config.enabled is True


def test_enabled_asr_rejects_tokenizer_metadata_without_vocabulary_assets(
    tmp_path: Path,
):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    model = Path(data["sglang"]["model_path"])
    (model / "vocab.json").unlink()
    (model / "merges.txt").unlink()

    with pytest.raises(ValidationError, match="tokenizer vocabulary assets"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_enabled_asr_requires_every_unique_indexed_shard(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    model = Path(data["sglang"]["model_path"])
    (model / "model-00002-of-00002.safetensors").unlink()

    with pytest.raises(ValidationError, match="model-00002-of-00002.safetensors"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_enabled_asr_rejects_invalid_indexed_shard_name(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    model = Path(data["sglang"]["model_path"])
    (model / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"a": ["not", "a", "path"]}}),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="invalid shard name"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


@pytest.mark.parametrize(
    "unsafe_shard",
    ["../outside.safetensors", "weights/../../outside.safetensors", "/tmp/absolute.safetensors"],
)
def test_enabled_asr_rejects_unsafe_indexed_shard_path(
    tmp_path: Path, unsafe_shard: str
):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    model = Path(data["sglang"]["model_path"])
    (model / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"a": unsafe_shard}}), encoding="utf-8"
    )

    with pytest.raises(ValidationError, match="unsafe shard path"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_enabled_asr_requires_qwen3_asr_architecture(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    model = Path(data["sglang"]["model_path"])
    (model / "config.json").write_text(
        json.dumps({"architectures": ["WrongArchitecture"]}), encoding="utf-8"
    )

    with pytest.raises(ValidationError, match="Qwen3ASRForConditionalGeneration"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_enabled_asr_rejects_unreadable_model_artifact(tmp_path: Path):
    if os.geteuid() == 0:
        pytest.skip("root can read mode-000 files")

    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    tokenizer = Path(data["sglang"]["model_path"]) / "tokenizer_config.json"
    tokenizer.chmod(0)

    with pytest.raises(ValidationError, match="not readable.*tokenizer_config.json"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_disabled_asr_skips_local_model_artifact_validation(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["enabled"] = False
    data["sglang"]["model_path"] = str(tmp_path / "does-not-exist")

    config = load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)

    assert config.enabled is False


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan")])
def test_asr_config_requires_positive_finite_startup_timeout(
    tmp_path: Path, timeout: float
):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["sglang"]["startup_timeout_seconds"] = timeout

    with pytest.raises(ValidationError, match="startup_timeout_seconds"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def _generate_test_certificates(
    tmp_path: Path,
    sans: list[str],
    *,
    prefix: str = "tls",
    extended_key_usage: str | None = None,
) -> tuple[Path, Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required for real certificate validation tests")

    directory = tmp_path / prefix
    directory.mkdir()
    ca_key = directory / "ca.key"
    ca_cert = directory / "ca.crt"
    server_key = directory / "server.key"
    request = directory / "server.csr"
    server_cert = directory / "server.crt"
    extensions = directory / "extensions.cnf"
    extension_lines = ["[server]", "subjectAltName=" + ",".join(sans)]
    if extended_key_usage:
        extension_lines.append(f"extendedKeyUsage={extended_key_usage}")
    extensions.write_text("\n".join(extension_lines) + "\n", encoding="utf-8")
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_cert),
            "-subj",
            "/CN=ASR Test CA",
            "-days",
            "1",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            openssl,
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(server_key),
            "-out",
            str(request),
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            openssl,
            "x509",
            "-req",
            "-in",
            str(request),
            "-CA",
            str(ca_cert),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(server_cert),
            "-days",
            "1",
            "-sha256",
            "-extfile",
            str(extensions),
            "-extensions",
            "server",
        ],
        check=True,
        capture_output=True,
    )
    return ca_cert, server_cert, server_key


def _enable_https(data: dict, paths: tuple[Path, Path, Path]) -> None:
    ca_cert, server_cert, server_key = paths
    security = data["browser_security"]
    security.update(
        {
            "https_enabled": True,
            "ca_certificate_path": str(ca_cert),
            "certificate_path": str(server_cert),
            "private_key_path": str(server_key),
        }
    )


def test_https_validation_accepts_matching_chain_key_and_sans(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    _enable_https(
        data,
        _generate_test_certificates(tmp_path, ["DNS:localhost", "IP:127.0.0.1"]),
    )

    config = load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)

    assert config.browser_security.https_enabled is True


def test_https_validation_rejects_server_certificate_signed_by_wrong_ca(
    tmp_path: Path,
):
    from backend.asr.config import load_asr_config

    sans = ["DNS:localhost", "IP:127.0.0.1"]
    ca_one, server_one, key_one = _generate_test_certificates(
        tmp_path, sans, prefix="one"
    )
    ca_two, _, _ = _generate_test_certificates(tmp_path, sans, prefix="two")
    data = _valid_config(tmp_path)
    _enable_https(data, (ca_two, server_one, key_one))

    with pytest.raises(ValidationError) as exc_info:
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)

    message = str(exc_info.value)
    assert "does not verify against configured CA" in message
    assert "bash scripts/generate_asr_tls_cert.sh --force" in message
    assert "BEGIN PRIVATE KEY" not in message
    assert ca_one != ca_two


def test_https_validation_does_not_fall_back_to_platform_trust_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from backend.asr.config import load_asr_config

    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required for real certificate validation tests")
    sans = ["DNS:localhost", "IP:127.0.0.1"]
    trusted_ca, server_cert, server_key = _generate_test_certificates(
        tmp_path, sans, prefix="platform-trusted"
    )
    wrong_ca, _, _ = _generate_test_certificates(
        tmp_path, sans, prefix="configured-wrong"
    )
    trust_directory = tmp_path / "platform-trust"
    trust_directory.mkdir()
    subject_hash = subprocess.run(
        [openssl, "x509", "-in", str(trusted_ca), "-hash", "-noout"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (trust_directory / f"{subject_hash}.0").symlink_to(trusted_ca)
    monkeypatch.setenv("SSL_CERT_DIR", str(trust_directory))

    data = _valid_config(tmp_path)
    _enable_https(data, (wrong_ca, server_cert, server_key))

    with pytest.raises(
        ValidationError, match="does not verify against configured CA"
    ):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_https_validation_rejects_client_auth_only_certificate(tmp_path: Path):
    from backend.asr.config import load_asr_config

    sans = ["DNS:localhost", "IP:127.0.0.1"]
    data = _valid_config(tmp_path)
    _enable_https(
        data,
        _generate_test_certificates(
            tmp_path,
            sans,
            prefix="client-only",
            extended_key_usage="clientAuth",
        ),
    )

    with pytest.raises(
        ValidationError, match="does not verify against configured CA"
    ):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_https_validation_uses_exclusive_ca_and_server_purpose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import backend.asr.config as config_module

    calls: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(command, *args, **kwargs):
        calls.append([str(value) for value in command])
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(config_module.subprocess, "run", recording_run)
    data = _valid_config(tmp_path)
    _enable_https(
        data,
        _generate_test_certificates(
            tmp_path, ["DNS:localhost", "IP:127.0.0.1"]
        ),
    )

    config_module.load_asr_config(
        _write_config(tmp_path, data), project_root=tmp_path
    )

    verify_call = next(command for command in calls if "verify" in command)
    assert "-trusted" in verify_call
    assert "-CAfile" not in verify_call
    assert verify_call[verify_call.index("-purpose") + 1] == "sslserver"


def test_https_validation_rejects_private_key_for_different_certificate(
    tmp_path: Path,
):
    from backend.asr.config import load_asr_config

    sans = ["DNS:localhost", "IP:127.0.0.1"]
    ca_one, server_one, _ = _generate_test_certificates(
        tmp_path, sans, prefix="one"
    )
    _, _, key_two = _generate_test_certificates(tmp_path, sans, prefix="two")
    data = _valid_config(tmp_path)
    _enable_https(data, (ca_one, server_one, key_two))

    with pytest.raises(ValidationError) as exc_info:
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)

    message = str(exc_info.value)
    assert "private key does not match server certificate" in message
    assert "bash scripts/generate_asr_tls_cert.sh --force" in message
    assert "BEGIN PRIVATE KEY" not in message


def test_https_validation_rejects_plaintext_public_websocket_url(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    _enable_https(
        data,
        _generate_test_certificates(
            tmp_path, ["DNS:localhost", "IP:127.0.0.1"]
        ),
    )
    data["transport"]["public_websocket_url"] = "ws://localhost/v1/asr/stream"

    with pytest.raises(ValidationError, match="public_websocket_url.*wss"):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


def test_https_validation_accepts_secure_public_websocket_url(tmp_path: Path):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    _enable_https(
        data,
        _generate_test_certificates(
            tmp_path, ["DNS:localhost", "IP:127.0.0.1"]
        ),
    )
    data["transport"]["public_websocket_url"] = "wss://localhost/v1/asr/stream"

    config = load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)

    assert config.transport.public_websocket_url.startswith("wss://")


def test_http_validation_keeps_plaintext_public_websocket_url_compatible(
    tmp_path: Path,
):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["transport"]["public_websocket_url"] = "ws://localhost/v1/asr/stream"

    config = load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)

    assert config.transport.public_websocket_url.startswith("ws://")


@pytest.mark.parametrize("extra_san", ["DNS:extra.local", "IP:192.168.50.50"])
def test_https_validation_rejects_unexpected_dns_or_ip_san(
    tmp_path: Path, extra_san: str
):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    _enable_https(
        data,
        _generate_test_certificates(
            tmp_path, ["DNS:localhost", "IP:127.0.0.1", extra_san]
        ),
    )

    with pytest.raises(
        ValidationError,
        match="unexpected SANs.*bash scripts/generate_asr_tls_cert.sh --force",
    ):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


@pytest.mark.parametrize(
    ("configured_sans", "certificate_sans", "required"),
    [
        (["127.0.0.1"], ["IP:127.0.0.1"], "DNS:localhost"),
        (["localhost"], ["DNS:localhost"], "IP:127.0.0.1"),
    ],
)
def test_https_validation_requires_internal_proxy_and_health_sans(
    tmp_path: Path,
    configured_sans: list[str],
    certificate_sans: list[str],
    required: str,
):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["browser_security"]["subject_alt_names"] = configured_sans
    _enable_https(data, _generate_test_certificates(tmp_path, certificate_sans))

    with pytest.raises(
        ValidationError,
        match=f"{required}.*bash scripts/generate_asr_tls_cert.sh --force",
    ):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)


@pytest.mark.parametrize(
    ("configured_sans", "certificate_sans", "missing"),
    [
        (["localhost", "127.0.0.1"], ["DNS:localhost"], "127.0.0.1"),
        (["localhost", "127.0.0.1"], ["IP:127.0.0.1"], "DNS:localhost"),
    ],
)
def test_https_validation_rejects_missing_or_wrong_kind_san(
    tmp_path: Path,
    configured_sans: list[str],
    certificate_sans: list[str],
    missing: str,
):
    from backend.asr.config import load_asr_config

    data = _valid_config(tmp_path)
    data["browser_security"]["subject_alt_names"] = configured_sans
    _enable_https(data, _generate_test_certificates(tmp_path, certificate_sans))

    with pytest.raises(
        ValidationError,
        match=f"{missing}.*bash scripts/generate_asr_tls_cert.sh --force",
    ):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)
