#!/usr/bin/env python3
"""Validate and canary the isolated full PaddleOCR-VL Pipeline service."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import re
import resource
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from packaging.version import Version


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_PATH = PROJECT_ROOT / "config/paddleocr-vl-thor-model-manifest.json"
PHOTO_FIXTURE_PATH = PROJECT_ROOT / "tests/fixtures/paddleocr/doc_test_rotated.jpg"
PHOTO_FIXTURE_URL = (
    "https://paddle-model-ecology.bj.bcebos.com/paddlex/demo_image/"
    "doc_test_rotated.jpg"
)
PHOTO_FIXTURE_SHA256 = "6fb6b8ea8836a0acb46762a85d2bcf97100e33f0c4b49dcfc0b97f5955f4e534"
PHOTO_FIXTURE_EXPECTED_PHRASES = [
    "THE DISAPPEARING SUM",
    "VERONICA",
    "CHOICES",
]
REQUIRED_MODEL_FILES = {
    "LayoutDetection": {
        "config.json",
        "inference.yml",
        "model.safetensors",
        "preprocessor_config.json",
    },
    "VLRecognition": {
        "added_tokens.json",
        "chat_template.jinja",
        "config.json",
        "configuration_paddleocr_vl.py",
        "generation_config.json",
        "image_processing_paddleocr_vl.py",
        "inference.yml",
        "model.safetensors",
        "modeling_paddleocr_vl.py",
        "preprocessor_config.json",
        "processing_paddleocr_vl.py",
        "processor_config.json",
        "special_tokens_map.json",
        "tokenizer_config.json",
        "tokenizer.json",
        "tokenizer.model",
    },
    "DocOrientationClassify": {
        "config.json",
        "inference.yml",
        "model.safetensors",
        "preprocessor_config.json",
    },
    "DocUnwarping": {
        "config.json",
        "inference.yml",
        "model.safetensors",
        "preprocessor_config.json",
    },
}
EXPECTED_MODEL_IDENTITIES = {
    "LayoutDetection": (
        "PaddlePaddle/PP-DocLayoutV3_safetensors",
        "f8bd2e7f6bb8773617356c59592a9beea51a9a6f",
    ),
    "VLRecognition": (
        "PaddlePaddle/PaddleOCR-VL-1.6",
        "d911116c363676c602c4786ad0b9667b1aee055f",
    ),
    "DocOrientationClassify": (
        "PaddlePaddle/PP-LCNet_x1_0_doc_ori_safetensors",
        "04cd0dee25d2300e63442f6989e8984b1f3dae77",
    ),
    "DocUnwarping": (
        "PaddlePaddle/UVDoc_safetensors",
        "505cd57c9915ac2656bee70ae4204aa12201aa1f",
    ),
}
OCR_ATTESTATION_SCHEMA_VERSION = 1
SGLANG_OCR_ATTESTATION_SCHEMA_VERSION = 2
SGLANG_REQUIRED_DISTRIBUTIONS = (
    "sglang",
    "sglang-kernel",
    "torch",
    "transformers",
    "triton",
    "flashinfer-python",
    "flashinfer-cubin",
)
SGLANG_CANARY_TEXT = "THOR SGLANG CANARY 2026"


class PreflightError(RuntimeError):
    """A required P0 invariant was not satisfied."""


def _package_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "missing"


def _walk_mappings(value: Any, path: str = "root"):
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from _walk_mappings(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_mappings(child, f"{path}[{index}]")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json_mapping(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise PreflightError(f"Cannot read {description} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PreflightError(f"{description} must contain a JSON object")
    return value


def _validate_cuda_transformers_module(
    module_name: str,
    module: dict[str, Any],
    *,
    dtype: str,
    attention: str,
) -> dict[str, Any]:
    if module.get("engine") != "transformers":
        raise PreflightError(f"{module_name} must explicitly use Transformers")
    engine_config = module.get("engine_config")
    if not isinstance(engine_config, dict):
        raise PreflightError(f"{module_name} must define engine_config")
    if (
        engine_config.get("dtype") != dtype
        or engine_config.get("device_type") != "gpu"
        or engine_config.get("device_id") != 0
        or engine_config.get("attn_implementation") != attention
    ):
        raise PreflightError(
            f"{module_name} must use Transformers {dtype}/{attention} on CUDA GPU 0"
        )
    return engine_config


def _validate_model_manifest(
    module_configs: dict[str, dict[str, Any]], manifest_path: Path
) -> tuple[dict[str, Any], str]:
    manifest = _load_json_mapping(manifest_path, "model manifest")
    if manifest.get("schema_version") != 1:
        raise PreflightError("Model manifest schema_version must be 1")
    models = manifest.get("models")
    if not isinstance(models, dict) or set(models) != set(REQUIRED_MODEL_FILES):
        raise PreflightError("Model manifest must contain exactly all four Pipeline models")

    summaries: dict[str, Any] = {}
    for module_name, required_files in REQUIRED_MODEL_FILES.items():
        entry = models[module_name]
        if not isinstance(entry, dict):
            raise PreflightError(f"Manifest entry {module_name} must be an object")
        module = module_configs[module_name]
        if entry.get("model_name") != module.get("model_name"):
            raise PreflightError(f"{module_name} manifest model name does not match config")
        if entry.get("model_dir") != module.get("model_dir"):
            raise PreflightError(f"{module_name} manifest model directory does not match config")
        expected_model_id, expected_revision = EXPECTED_MODEL_IDENTITIES[module_name]
        if entry.get("model_id") != expected_model_id:
            raise PreflightError(f"{module_name} manifest model ID is not the reviewed model")
        revision = entry.get("revision")
        if revision != expected_revision:
            raise PreflightError(f"{module_name} manifest revision is not the reviewed revision")
        files = entry.get("files")
        if not isinstance(files, dict) or set(files) != required_files:
            raise PreflightError(
                f"{module_name} manifest does not cover every execution artifact"
            )
        model_dir = Path(str(entry["model_dir"])).expanduser()
        if not model_dir.is_absolute() or not model_dir.is_dir():
            raise PreflightError(f"{module_name} must use an existing absolute model directory")
        total_bytes = 0
        for relative_path, expected in files.items():
            if not isinstance(expected, dict):
                raise PreflightError(f"{module_name}/{relative_path} manifest metadata is invalid")
            artifact_path = model_dir / relative_path
            if not artifact_path.is_file():
                raise PreflightError(f"{module_name} local model artifact is missing: {relative_path}")
            size = artifact_path.stat().st_size
            if size != expected.get("bytes") or _sha256(artifact_path) != expected.get("sha256"):
                raise PreflightError(
                    f"{module_name}/{relative_path} checksum or size does not match manifest"
                )
            total_bytes += size
        summaries[module_name] = {
            "modelId": entry.get("model_id"),
            "modelName": entry["model_name"],
            "modelDir": entry["model_dir"],
            "revision": revision,
            "fileCount": len(files),
            "bytes": total_bytes,
        }
    return summaries, _sha256(manifest_path)


def validate_pipeline_config(
    config_path: Path, manifest_path: Path = DEFAULT_MANIFEST_PATH
) -> dict[str, Any]:
    try:
        config = yaml.safe_load(config_path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise PreflightError(f"Cannot read Pipeline config {config_path}: {exc}") from exc
    if not isinstance(config, dict):
        raise PreflightError("Pipeline config must contain a YAML mapping")

    pipeline_name = str(config.get("pipeline_name", ""))
    if pipeline_name != "PaddleOCR-VL-1.6":
        raise PreflightError(f"Pipeline config must select PaddleOCR-VL-1.6: {pipeline_name!r}")

    engines: list[tuple[str, str]] = []
    devices: list[tuple[str, str]] = []
    for path, mapping in _walk_mappings(config):
        for key, value in mapping.items():
            normalized_key = str(key).lower().replace("-", "_")
            if value is None:
                continue
            if normalized_key == "engine":
                engines.append((path, str(value).lower()))
            elif normalized_key == "device":
                devices.append((path, str(value).lower()))

    if not engines or any(value != "transformers" for _, value in engines):
        raise PreflightError(
            "Every configured inference engine must explicitly be Transformers"
        )
    if not devices or any(value not in {"gpu:0", "cuda:0"} for _, value in devices):
        raise PreflightError("Every configured device must explicitly select CUDA GPU 0")
    engine_config = config.get("engine_config")
    if not isinstance(engine_config, dict):
        raise PreflightError("Pipeline config must include Transformers engine_config")
    if str(engine_config.get("dtype", "")).lower() not in {"bf16", "bfloat16"}:
        raise PreflightError("Transformers engine_config must explicitly select BF16")
    if engine_config.get("device_type") != "gpu" or engine_config.get("device_id") != 0:
        raise PreflightError("Transformers engine_config must explicitly select CUDA GPU 0")
    submodules = config.get("SubModules", {})
    layout_module = submodules.get("LayoutDetection", {})
    recognition_module = submodules.get("VLRecognition", {})
    if layout_module.get("model_name") != "PP-DocLayoutV3":
        raise PreflightError("PaddleOCR-VL-1.6 must use PP-DocLayoutV3")
    if recognition_module.get("model_name") != "PaddleOCR-VL-1.6-0.9B":
        raise PreflightError("PaddleOCR-VL-1.6 recognition model is not selected")
    layout_engine_config = _validate_cuda_transformers_module(
        "LayoutDetection", layout_module, dtype="float32", attention="eager"
    )
    recognition_engine_config = _validate_cuda_transformers_module(
        "VLRecognition", recognition_module, dtype="bfloat16", attention="sdpa"
    )
    generation_config = recognition_engine_config.get("generation_config")
    if not isinstance(generation_config, dict) or generation_config.get("do_sample") is not False:
        raise PreflightError("VLRecognition generation must be deterministic")
    expected_switches = {
        "use_queues": True,
        "use_doc_preprocessor": True,
        "use_layout_detection": True,
        "use_chart_recognition": True,
        "use_seal_recognition": False,
        "use_ocr_for_image_block": True,
        "format_block_content": True,
        "merge_layout_blocks": True,
    }
    for key, expected in expected_switches.items():
        if config.get(key) is not expected:
            raise PreflightError(f"Pipeline config switch {key} must be {expected}")
    if set(config.get("markdown_ignore_labels", [])) != {
        "number",
        "header",
        "header_image",
        "footer",
        "footer_image",
    }:
        raise PreflightError("Pipeline config must retain footnote and aside text")

    doc_preprocessor = config.get("SubPipelines", {}).get("DocPreprocessor", {})
    if doc_preprocessor.get("pipeline_name") != "doc_preprocessor":
        raise PreflightError("DocPreprocessor parent Pipeline must be configured")
    if doc_preprocessor.get("use_doc_orientation_classify") is not True:
        raise PreflightError("Document orientation classification must be enabled by default")
    if doc_preprocessor.get("use_doc_unwarping") is not True:
        raise PreflightError(
            "DocUnwarping must be initialized for request-controlled distorted retries"
        )
    preprocessing_modules = doc_preprocessor.get("SubModules", {})
    orientation_module = preprocessing_modules.get("DocOrientationClassify", {})
    unwarping_module = preprocessing_modules.get("DocUnwarping", {})
    if orientation_module.get("model_name") != "PP-LCNet_x1_0_doc_ori":
        raise PreflightError("Official document orientation model must be configured")
    if unwarping_module.get("model_name") != "UVDoc":
        raise PreflightError("Official document unwarping model must be configured")
    orientation_engine_config = _validate_cuda_transformers_module(
        "DocOrientationClassify", orientation_module, dtype="float32", attention="eager"
    )
    unwarping_engine_config = _validate_cuda_transformers_module(
        "DocUnwarping", unwarping_module, dtype="float32", attention="eager"
    )

    serving = config.get("Serving", {})
    if serving.get("visualize") is not False or serving.get("return_urls") is not False:
        raise PreflightError("Serving config must disable visualization and URL image payloads")
    if serving.get("extra", {}).get("max_num_input_imgs") != 32:
        raise PreflightError("Serving.extra.max_num_input_imgs must be 32")

    module_configs = {
        "LayoutDetection": layout_module,
        "VLRecognition": recognition_module,
        "DocOrientationClassify": orientation_module,
        "DocUnwarping": unwarping_module,
    }
    model_artifacts, manifest_sha256 = _validate_model_manifest(
        module_configs, manifest_path
    )

    return {
        "pipelineName": pipeline_name,
        "pipelineVersion": "v1.6",
        "engines": engines,
        "devices": devices,
        "dtype": engine_config["dtype"],
        "layoutAttention": layout_engine_config["attn_implementation"],
        "layoutDtype": layout_engine_config["dtype"],
        "vlmAttention": recognition_engine_config["attn_implementation"],
        "vlmDtype": recognition_engine_config["dtype"],
        "orientationDtype": orientation_engine_config["dtype"],
        "unwarpingDtype": unwarping_engine_config["dtype"],
        "unwarpingInitialized": True,
        "projectNormalRequestUnwarping": False,
        "modelArtifacts": model_artifacts,
        "configSha256": _sha256(config_path),
        "manifestSha256": manifest_sha256,
    }


def validate_sglang_pipeline_config(
    config_path: Path,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
) -> dict[str, Any]:
    """Validate the complete Pipeline with remote SGLang VL recognition."""
    try:
        config = yaml.safe_load(config_path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise PreflightError(f"Cannot read Pipeline config {config_path}: {exc}") from exc
    if not isinstance(config, dict):
        raise PreflightError("Pipeline config must contain a YAML mapping")
    if config.get("pipeline_name") != "PaddleOCR-VL-1.6":
        raise PreflightError("Remote Pipeline config must select PaddleOCR-VL-1.6")
    if "engine" in config or "engine_config" in config:
        raise PreflightError("Remote Pipeline must not define a top-level local engine")
    if config.get("device") != "gpu:0":
        raise PreflightError("Remote Pipeline must explicitly select CUDA GPU 0")
    if config.get("batch_size") != 8 or config.get("use_queues") is not True:
        raise PreflightError("Remote Pipeline batch_size must be 8 with queues enabled")

    expected_switches = {
        "use_doc_preprocessor": True,
        "use_layout_detection": True,
        "use_chart_recognition": True,
        "use_seal_recognition": False,
        "use_ocr_for_image_block": True,
        "format_block_content": True,
        "merge_layout_blocks": True,
    }
    for key, expected in expected_switches.items():
        if config.get(key) is not expected:
            raise PreflightError(f"Remote Pipeline switch {key} must be {expected}")
    if set(config.get("markdown_ignore_labels", [])) != {
        "number",
        "header",
        "header_image",
        "footer",
        "footer_image",
    }:
        raise PreflightError("Remote Pipeline Markdown ignore labels changed")

    submodules = config.get("SubModules")
    if not isinstance(submodules, dict):
        raise PreflightError("Remote Pipeline SubModules must be a mapping")
    layout_module = submodules.get("LayoutDetection")
    recognition_module = submodules.get("VLRecognition")
    if not isinstance(layout_module, dict) or not isinstance(recognition_module, dict):
        raise PreflightError("Remote Pipeline must define layout and VLM modules")
    if layout_module.get("model_name") != "PP-DocLayoutV3":
        raise PreflightError("Remote Pipeline must use PP-DocLayoutV3")
    if layout_module.get("batch_size") != 4:
        raise PreflightError("LayoutDetection batch_size must be 4")
    layout_engine_config = _validate_cuda_transformers_module(
        "LayoutDetection", layout_module, dtype="float32", attention="eager"
    )
    if recognition_module.get("model_name") != "PaddleOCR-VL-1.6-0.9B":
        raise PreflightError("Remote Pipeline recognition model is not selected")
    if "engine" in recognition_module:
        raise PreflightError("VLRecognition must not define a local engine")
    if "engine_config" in recognition_module:
        raise PreflightError("VLRecognition must not define local engine_config")

    genai_config = recognition_module.get("genai_config")
    if not isinstance(genai_config, dict):
        raise PreflightError("VLRecognition must define remote genai_config")
    if genai_config.get("backend") != "sglang-server":
        raise PreflightError("VLRecognition backend must be sglang-server")
    backend = config.get("LLMDeploymentSGLangBackend")
    if not isinstance(backend, dict):
        raise PreflightError("LLMDeploymentSGLangBackend must be a mapping")
    if backend.get("host") != "127.0.0.1":
        raise PreflightError("SGLang host must be the IPv4 loopback address")
    if type(backend.get("port")) is not int or backend["port"] != 30002:
        raise PreflightError("SGLang port must be 30002")
    expected_server_url = f"http://{backend['host']}:{backend['port']}/v1"
    if genai_config.get("server_url") != expected_server_url:
        raise PreflightError("VLRecognition server_url must match the SGLang host and port")
    if backend.get("served_model_name") != recognition_module.get("model_name"):
        raise PreflightError("SGLang served model must match VLRecognition.model_name")

    manifest = _load_json_mapping(manifest_path, "model manifest")
    manifest_models = manifest.get("models")
    manifest_vlm = (
        manifest_models.get("VLRecognition") if isinstance(manifest_models, dict) else None
    )
    if not isinstance(manifest_vlm, dict):
        raise PreflightError("Model manifest has no VLRecognition entry")
    model_dir = backend.get("model_dir")
    if (
        not isinstance(model_dir, str)
        or model_dir != recognition_module.get("model_dir")
        or model_dir != manifest_vlm.get("model_dir")
    ):
        raise PreflightError("SGLang model directory must match Pipeline and manifest")
    chat_template = backend.get("chat_template")
    manifest_files = manifest_vlm.get("files")
    expected_chat_template = str(Path(model_dir) / "chat_template.jinja")
    if (
        not isinstance(chat_template, str)
        or chat_template != expected_chat_template
        or not isinstance(manifest_files, dict)
        or "chat_template.jinja" not in manifest_files
    ):
        raise PreflightError("SGLang chat template must be the manifested model template")

    max_concurrency = genai_config.get("max_concurrency")
    max_running_requests = backend.get("max_running_requests")
    vlm_batch_size = recognition_module.get("batch_size")
    if type(max_concurrency) is not int or max_concurrency <= 0:
        raise PreflightError("VLRecognition max_concurrency must be positive")
    if type(max_running_requests) is not int or max_running_requests <= 0:
        raise PreflightError("SGLang max_running_requests must be positive")
    if type(vlm_batch_size) is not int or vlm_batch_size < max_concurrency:
        raise PreflightError("VLRecognition batch_size must cover max_concurrency")
    if max_concurrency != max_running_requests:
        raise PreflightError("max_concurrency must equal max_running_requests")
    for key in ("max_prefill_tokens", "context_length"):
        if type(backend.get(key)) is not int or backend[key] <= 0:
            raise PreflightError(f"SGLang {key} must be positive")
    mem_fraction_static = backend.get("mem_fraction_static")
    if (
        isinstance(mem_fraction_static, bool)
        or not isinstance(mem_fraction_static, (int, float))
        or not 0 < float(mem_fraction_static) < 1
    ):
        raise PreflightError("SGLang mem_fraction_static must be between zero and one")
    if type(backend.get("log_requests_level")) is not int or backend["log_requests_level"] != 0:
        raise PreflightError("SGLang log_requests_level must be 0")
    for key in ("trust_remote_code", "enable_metrics", "log_requests"):
        if backend.get(key) is not True:
            raise PreflightError(f"SGLang {key} must be true")
    for key in ("readiness_timeout_seconds", "shutdown_timeout_seconds"):
        if type(backend.get(key)) is not int or backend[key] <= 0:
            raise PreflightError(f"SGLang {key} must be positive")

    subpipelines = config.get("SubPipelines")
    doc_preprocessor = (
        subpipelines.get("DocPreprocessor") if isinstance(subpipelines, dict) else None
    )
    if not isinstance(doc_preprocessor, dict):
        raise PreflightError("Remote Pipeline must define DocPreprocessor")
    if (
        doc_preprocessor.get("pipeline_name") != "doc_preprocessor"
        or doc_preprocessor.get("batch_size") != 4
        or doc_preprocessor.get("use_doc_orientation_classify") is not True
        or doc_preprocessor.get("use_doc_unwarping") is not True
    ):
        raise PreflightError("Remote DocPreprocessor settings changed")
    preprocessing_modules = doc_preprocessor.get("SubModules")
    if not isinstance(preprocessing_modules, dict):
        raise PreflightError("DocPreprocessor SubModules must be a mapping")
    orientation_module = preprocessing_modules.get("DocOrientationClassify")
    unwarping_module = preprocessing_modules.get("DocUnwarping")
    if not isinstance(orientation_module, dict) or not isinstance(unwarping_module, dict):
        raise PreflightError("Remote Pipeline preprocessing modules are incomplete")
    if orientation_module.get("model_name") != "PP-LCNet_x1_0_doc_ori":
        raise PreflightError("Official document orientation model must be configured")
    if unwarping_module.get("model_name") != "UVDoc":
        raise PreflightError("Official document unwarping model must be configured")
    if orientation_module.get("batch_size") != 4 or unwarping_module.get("batch_size") != 1:
        raise PreflightError("Remote preprocessing batch settings changed")
    orientation_engine_config = _validate_cuda_transformers_module(
        "DocOrientationClassify", orientation_module, dtype="float32", attention="eager"
    )
    unwarping_engine_config = _validate_cuda_transformers_module(
        "DocUnwarping", unwarping_module, dtype="float32", attention="eager"
    )

    serving = config.get("Serving")
    if not isinstance(serving, dict):
        raise PreflightError("Remote Pipeline Serving config must be a mapping")
    if serving.get("visualize") is not False or serving.get("return_urls") is not False:
        raise PreflightError("Serving config must disable visualization and URL payloads")
    if serving.get("extra", {}).get("max_num_input_imgs") != 32:
        raise PreflightError("Serving.extra.max_num_input_imgs must be 32")

    module_configs = {
        "LayoutDetection": layout_module,
        "VLRecognition": recognition_module,
        "DocOrientationClassify": orientation_module,
        "DocUnwarping": unwarping_module,
    }
    model_artifacts, manifest_sha256 = _validate_model_manifest(
        module_configs, manifest_path
    )
    engines = [
        (path, str(mapping["engine"]).lower())
        for path, mapping in _walk_mappings(config)
        if "engine" in mapping
    ]
    batch_settings = {
        "pipelineBatchSize": config["batch_size"],
        "layoutBatchSize": layout_module["batch_size"],
        "vlmBatchSize": vlm_batch_size,
        "vlmMaxConcurrency": max_concurrency,
        "docPreprocessorBatchSize": doc_preprocessor["batch_size"],
        "orientationBatchSize": orientation_module["batch_size"],
        "unwarpingBatchSize": unwarping_module["batch_size"],
        "maxRunningRequests": max_running_requests,
    }
    return {
        "pipelineName": "PaddleOCR-VL-1.6",
        "pipelineVersion": "v1.6",
        "engines": engines,
        "devices": [("root", config["device"])],
        "layoutAttention": layout_engine_config["attn_implementation"],
        "layoutDtype": layout_engine_config["dtype"],
        "vlmAttention": "remote",
        "vlmDtype": "remote",
        "orientationDtype": orientation_engine_config["dtype"],
        "unwarpingDtype": unwarping_engine_config["dtype"],
        "unwarpingInitialized": True,
        "projectNormalRequestUnwarping": False,
        "modelArtifacts": model_artifacts,
        "configSha256": _sha256(config_path),
        "manifestSha256": manifest_sha256,
        "mode": "remote_sglang",
        "vlmBackend": "sglang-server",
        "sglangBackend": {
            "host": backend["host"],
            "port": backend["port"],
            "url": f"http://{backend['host']}:{backend['port']}",
            "servedModelName": backend["served_model_name"],
            "modelDir": model_dir,
            "chatTemplate": chat_template,
            "contextLength": backend["context_length"],
            "maxRunningRequests": max_running_requests,
            "maxPrefillTokens": backend["max_prefill_tokens"],
            "effectiveMaxPrefillTokens": max(
                backend["max_prefill_tokens"], backend["context_length"]
            ),
            "memFractionStatic": float(mem_fraction_static),
            "enableMetrics": backend["enable_metrics"],
            "logRequests": backend["log_requests"],
            "logRequestsLevel": backend["log_requests_level"],
            "readinessTimeoutSeconds": backend["readiness_timeout_seconds"],
            "shutdownTimeoutSeconds": backend["shutdown_timeout_seconds"],
        },
        "batchAndConcurrencySettings": batch_settings,
    }


def check_sglang_dependency_names(python_path: Path) -> dict[str, Any]:
    """Check distribution presence in another interpreter without version gating."""
    program = """
import importlib.metadata
import json
import sys

versions = {}
for name in sys.argv[1:]:
    try:
        versions[name] = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        versions[name] = None
print(json.dumps(versions))
"""
    command = [str(python_path), "-c", program, *SGLANG_REQUIRED_DISTRIBUTIONS]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreflightError(f"Cannot run SGLang interpreter {python_path}: {exc}") from exc
    if completed.returncode != 0:
        raise PreflightError(
            f"SGLang dependency inspection failed with exit code {completed.returncode}"
        )
    try:
        versions = json.loads(completed.stdout)
    except ValueError as exc:
        raise PreflightError("SGLang dependency inspection returned invalid JSON") from exc
    if not isinstance(versions, dict) or set(versions) != set(SGLANG_REQUIRED_DISTRIBUTIONS):
        raise PreflightError("SGLang dependency inspection returned an unexpected name set")
    missing = [name for name in SGLANG_REQUIRED_DISTRIBUTIONS if not versions.get(name)]
    if missing:
        raise PreflightError(
            f"Required SGLang distributions are missing: {', '.join(missing)}"
        )
    return {
        "python": str(python_path),
        "requiredDistributions": list(SGLANG_REQUIRED_DISTRIBUTIONS),
        "versions": versions,
    }


def probe_sglang_service(
    url: str,
    served_model_name: str,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Require live SGLang health and exact served-model identity."""
    base_url = url.rstrip("/")
    try:
        with httpx.Client(trust_env=False, timeout=timeout) as client:
            health = client.get(f"{base_url}/health")
            health.raise_for_status()
            response = client.get(f"{base_url}/v1/models")
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PreflightError(f"SGLang service probe failed: {exc}") from exc
    data = body.get("data") if isinstance(body, dict) else None
    model_ids = {
        item.get("id")
        for item in data
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    } if isinstance(data, list) else set()
    if served_model_name not in model_ids:
        raise PreflightError(
            f"SGLang served model {served_model_name!r} is not present in /v1/models"
        )
    return {
        "url": base_url,
        "servedModelName": served_model_name,
        "modelReady": True,
    }


def _require_version(actual: str, minimum: str, maximum: str) -> bool:
    if actual == "missing":
        return False
    parsed = Version(actual)
    return Version(minimum) <= parsed < Version(maximum)


def _transformers_version_supported(actual: str) -> bool:
    return Version("5.10.0") <= Version(actual) < Version("6.0.0")


def _is_thor_gpu_name(name: str | None) -> bool:
    return bool(name and "thor" in name.casefold())


def _validate_attested_runtime(attestation: dict[str, Any]) -> None:
    if attestation.get("schemaVersion") != OCR_ATTESTATION_SCHEMA_VERSION:
        raise PreflightError("Reusable service attestation schema is unsupported")
    if not _is_thor_gpu_name(attestation.get("gpuName")):
        raise PreflightError("Reusable service attestation is not for a Thor GPU")
    packages = attestation.get("packages")
    transformers_version = (
        packages.get("transformers") if isinstance(packages, dict) else None
    )
    try:
        supported = isinstance(transformers_version, str) and _transformers_version_supported(
            transformers_version
        )
    except ValueError:
        supported = False
    if not supported:
        raise PreflightError(
            "Reusable service Transformers version must be >=5.10.0,<6.0"
        )


def validate_sbsa_cuda_artifact(
    wheel_metadata_path: Path, library_path: Path
) -> dict[str, Any]:
    try:
        wheel_lines = wheel_metadata_path.read_text().splitlines()
        header = library_path.read_bytes()[:20]
    except OSError as exc:
        raise PreflightError(f"Cannot inspect NVIDIA SBSA artifact: {exc}") from exc
    wheel_tags = [line[5:].strip() for line in wheel_lines if line.startswith("Tag:")]
    sbsa_tags = [tag for tag in wheel_tags if tag.endswith("manylinux2014_sbsa")]
    if not sbsa_tags:
        raise PreflightError("NVIDIA CUDA wheel does not declare an SBSA platform tag")
    if len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != 2:
        raise PreflightError("NVIDIA CUDA library is not an ELF64 artifact")
    byte_order = "little" if header[5] == 1 else "big" if header[5] == 2 else None
    if byte_order is None:
        raise PreflightError("NVIDIA CUDA ELF has an invalid byte-order marker")
    machine = int.from_bytes(header[18:20], byte_order)
    if machine != 183:
        raise PreflightError(
            f"NVIDIA CUDA ELF is not AArch64 (ELF e_machine={machine})"
        )
    return {
        "wheelTag": sbsa_tags[0],
        "elfClass": 64,
        "elfMachine": "AArch64",
    }


def _collect_sbsa_cuda_attestation() -> dict[str, Any]:
    try:
        distribution = importlib.metadata.distribution("nvidia-cusparselt-cu13")
        wheel_relative = next(
            file
            for file in distribution.files or []
            if str(file).endswith(".dist-info/WHEEL")
        )
    except (importlib.metadata.PackageNotFoundError, StopIteration) as exc:
        raise PreflightError("NVIDIA cuSPARSELt SBSA wheel metadata is missing") from exc
    wheel_path = Path(distribution.locate_file(wheel_relative))
    library_path = Path(
        distribution.locate_file("nvidia/cusparselt/lib/libcusparseLt.so.0")
    )
    return {
        "distribution": "nvidia-cusparselt-cu13",
        "version": distribution.version,
        "library": str(library_path),
        **validate_sbsa_cuda_artifact(wheel_path, library_path),
    }


def collect_environment_attestation() -> dict[str, Any]:
    try:
        import torch
        import transformers
    except ImportError as exc:
        raise PreflightError(f"Required inference package is missing: {exc.name}") from exc

    paddleocr_version = _package_version("paddleocr")
    paddlex_version = _package_version("paddlex")
    torchvision_version = _package_version("torchvision")
    sbsa_cuda_artifact = _collect_sbsa_cuda_attestation()
    cuda_available = torch.cuda.is_available()
    gpu_name = torch.cuda.get_device_name(0) if cuda_available else None
    checks = {
        "python_3_12": sys.version_info[:2] == (3, 12),
        "isolated_environment": Path(sys.prefix).name == ".paddle-ocr",
        "architecture": platform.machine() == "aarch64",
        "cuda_available": cuda_available,
        "thor_gpu_identity": _is_thor_gpu_name(gpu_name),
        "torch_version": Version(torch.__version__) == Version("2.11.0+cu130"),
        "torchvision_version": torchvision_version != "missing"
        and Version(torchvision_version) == Version("0.26.0+cu130"),
        "torch_cuda_13": str(torch.version.cuda or "").startswith("13."),
        "transformers_version": _transformers_version_supported(
            transformers.__version__
        ),
        "paddleocr_version": _require_version(paddleocr_version, "3.6", "3.7"),
        "paddlex_version": _require_version(paddlex_version, "3.6", "3.7"),
        "paddle_absent": importlib.util.find_spec("paddle") is None,
        "sglang_absent": importlib.util.find_spec("sglang") is None,
        "vllm_absent": importlib.util.find_spec("vllm") is None,
        "nvidia_sbsa_wheel": sbsa_cuda_artifact["wheelTag"].endswith(
            "manylinux2014_sbsa"
        ),
        "nvidia_elf_aarch64": sbsa_cuda_artifact["elfMachine"] == "AArch64",
    }
    failed = [name for name, passed in checks.items() if not passed]
    result = {
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "pythonPrefix": sys.prefix,
        "cudaAvailable": bool(cuda_available),
        "cudaRuntime": torch.version.cuda,
        "gpuName": gpu_name,
        "engine": "transformers",
        "device": "cuda:0",
        "packages": {
            "torch": torch.__version__,
            "torchvision": torchvision_version,
            "transformers": transformers.__version__,
            "paddleocr": paddleocr_version,
            "paddlex": paddlex_version,
        },
        "sbsaCudaArtifact": sbsa_cuda_artifact,
        "checks": checks,
    }
    if failed:
        raise PreflightError(
            f"Environment checks failed: {', '.join(failed)}\n{json.dumps(result, indent=2)}"
        )
    return result


def probe_service(url: str, timeout: float = 5.0) -> dict[str, Any]:
    try:
        with httpx.Client(trust_env=False, timeout=timeout) as client:
            response = client.get(f"{url.rstrip('/')}/openapi.json")
            response.raise_for_status()
            schema = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PreflightError(f"PaddleX OpenAPI probe failed: {exc}") from exc
    operation = schema.get("paths", {}).get("/layout-parsing", {}).get("post")
    if not isinstance(operation, dict):
        raise PreflightError("Listener does not expose POST /layout-parsing")
    return {"url": url.rstrip("/"), "layout_parsing_post": True}


def _listening_socket_inodes(host: str, port: int) -> set[str]:
    if host != "127.0.0.1":
        raise PreflightError(f"Service identity requires loopback IPv4, got {host!r}")
    expected_address = "0100007F"
    expected_port = f"{port:04X}"
    inodes: set[str] = set()
    try:
        lines = Path("/proc/net/tcp").read_text().splitlines()[1:]
    except OSError as exc:
        raise PreflightError(f"Cannot inspect TCP listeners: {exc}") from exc
    for line in lines:
        fields = line.split()
        if len(fields) < 10 or fields[3] != "0A":
            continue
        address, raw_port = fields[1].split(":", 1)
        if address == expected_address and raw_port == expected_port:
            inodes.add(fields[9])
    if not inodes:
        raise PreflightError(f"No loopback listener identity found for {host}:{port}")
    return inodes


def collect_process_identity(pid: int, url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port
    if host is None or port is None:
        raise PreflightError(f"Service URL has no host/port: {url}")
    process_root = Path(f"/proc/{pid}")
    try:
        stat = (process_root / "stat").read_text()
        stat_fields = stat[stat.rfind(")") + 2 :].split()
        start_time_ticks = int(stat_fields[19])
        command_line = (process_root / "cmdline").read_bytes()
        executable = os.readlink(process_root / "exe")
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        process_socket_inodes = set()
        for descriptor in (process_root / "fd").iterdir():
            try:
                target = os.readlink(descriptor)
            except FileNotFoundError:
                continue
            if target.startswith("socket:["):
                process_socket_inodes.add(target[8:-1])
    except (OSError, ValueError, IndexError) as exc:
        raise PreflightError(f"Cannot inspect service process {pid}: {exc}") from exc

    listener_inodes = sorted(
        _listening_socket_inodes(host, port) & process_socket_inodes
    )
    if not listener_inodes:
        raise PreflightError(
            f"Service process {pid} does not own the listener at {host}:{port}"
        )
    return {
        "pid": pid,
        "bootId": boot_id,
        "startTimeTicks": start_time_ticks,
        "executable": executable,
        "cmdlineSha256": hashlib.sha256(command_line).hexdigest(),
        "listenerSocketInodes": listener_inodes,
    }


def validate_reusable_service(
    url: str,
    attestation_path: Path,
    config_path: Path,
    *,
    max_age_seconds: float,
    memory_report_path: Path | None = None,
    cuda_memory_limit_gb: int | None = None,
) -> dict[str, Any]:
    probe_service(url)
    attestation = _load_json_mapping(attestation_path, "runtime attestation")
    _validate_attested_runtime(attestation)
    if (
        attestation.get("architecture") != "aarch64"
        or attestation.get("cudaAvailable") is not True
        or attestation.get("engine") != "transformers"
        or attestation.get("device") != "cuda:0"
        or attestation.get("canaryPassed") is not True
    ):
        raise PreflightError("Reusable service attestation failed the Thor CUDA gate")
    if attestation.get("pipelineApi") != f"{url.rstrip('/')}/layout-parsing":
        raise PreflightError("Reusable service attestation API does not match the listener")

    try:
        timestamp = datetime.fromisoformat(str(attestation["timestamp"]))
    except (KeyError, ValueError) as exc:
        raise PreflightError("Reusable service attestation timestamp is invalid") from exc
    if timestamp.tzinfo is None:
        raise PreflightError("Reusable service attestation timestamp must be timezone-aware")
    age_seconds = (datetime.now(timezone.utc) - timestamp).total_seconds()
    if age_seconds < -5 or age_seconds > max_age_seconds:
        raise PreflightError(
            f"Reusable service attestation is stale ({age_seconds:.1f} seconds old)"
        )

    expected_pipeline = validate_pipeline_config(config_path)
    attested_pipeline = attestation.get("pipeline")
    if not isinstance(attested_pipeline, dict):
        raise PreflightError("Reusable service attestation has no Pipeline identity")
    if attested_pipeline.get("configSha256") != expected_pipeline.get("configSha256"):
        raise PreflightError("Reusable service config hash does not match")
    if attested_pipeline.get("manifestSha256") != expected_pipeline.get("manifestSha256"):
        raise PreflightError("Reusable service manifest hash does not match")

    attested_identity = attestation.get("processIdentity")
    if not isinstance(attested_identity, dict) or not isinstance(
        attested_identity.get("pid"), int
    ):
        raise PreflightError("Reusable service process identity is missing")
    current_identity = collect_process_identity(attested_identity["pid"], url)
    for key in (
        "bootId",
        "startTimeTicks",
        "executable",
        "cmdlineSha256",
        "listenerSocketInodes",
    ):
        if attested_identity.get(key) != current_identity.get(key):
            raise PreflightError(f"Reusable service process identity mismatch: {key}")
    cuda_memory_limit = None
    if memory_report_path is not None or cuda_memory_limit_gb is not None:
        if memory_report_path is None or cuda_memory_limit_gb is None:
            raise PreflightError("Reusable service memory limit evidence is incomplete")
        cuda_memory_limit = validate_cuda_memory_limit_report(
            memory_report_path,
            expected_limit_gb=cuda_memory_limit_gb,
            expected_pid=current_identity["pid"],
        )
        if attestation.get("cudaMemoryLimit") != cuda_memory_limit:
            raise PreflightError(
                "Reusable service memory limit attestation does not match"
            )
    return {
        "reuse": True,
        "servicePid": current_identity["pid"],
        "configSha256": expected_pipeline["configSha256"],
        "manifestSha256": expected_pipeline["manifestSha256"],
        "attestationTimestamp": attestation["timestamp"],
        **(
            {"cudaMemoryLimitGiB": cuda_memory_limit["cudaMemoryLimitGiB"]}
            if cuda_memory_limit is not None
            else {}
        ),
    }


def _create_canary_files(
    directory: Path,
    *,
    image_text: str = "THOR IMAGE CANARY 2026",
) -> dict[str, Path]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise PreflightError("Pillow is required to generate canary documents") from exc

    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 72
        )
    except OSError:
        font = ImageFont.load_default()

    def make_page(text: str) -> "Image.Image":
        page = Image.new("RGB", (1600, 700), "white")
        draw = ImageDraw.Draw(page)
        draw.rectangle((35, 35, 1565, 665), outline="black", width=8)
        draw.text((90, 285), text, fill="black", font=font)
        return page

    files = {
        "image": directory / "canary-image.png",
        "pdf": directory / "canary-pdf.pdf",
        "rotated": directory / "canary-rotated.png",
    }
    make_page(image_text).save(files["image"], format="PNG")
    make_page("THOR PDF CANARY 2026").save(
        files["pdf"], format="PDF", resolution=150.0
    )
    make_page("THOR ROTATED CANARY 2026").rotate(90, expand=True).save(
        files["rotated"], format="PNG"
    )
    return files


def _build_canary_payload(
    path: Path, *, file_type: int, kind: str, use_doc_unwarping: bool
) -> dict[str, Any]:
    return {
        "file": base64.b64encode(path.read_bytes()).decode("ascii"),
        "fileType": file_type,
        "useDocOrientationClassify": True,
        "useDocUnwarping": use_doc_unwarping,
        "useLayoutDetection": True,
        "useChartRecognition": True,
        "useSealRecognition": False,
        "useOcrForImageBlock": True,
        "formatBlockContent": True,
        "mergeLayoutBlocks": True,
        "layoutShapeMode": "auto",
        "markdownIgnoreLabels": [
            "number",
            "header",
            "header_image",
            "footer",
            "footer_image",
        ],
        "prettifyMarkdown": True,
        "showFormulaNumber": True,
        "restructurePages": True,
        "mergeTables": True,
        "relevelTitles": True,
        "returnMarkdownImages": False,
        "visualize": False,
        "logId": f"paddleocr-p0-{kind}",
    }


def _post_canary(
    url: str,
    payload: dict[str, Any],
    *,
    kind: str,
    expected_text: str,
    expected_unwarping: bool,
    timeout: float,
) -> dict[str, Any]:
    request_started = time.monotonic()
    try:
        with httpx.Client(trust_env=False, timeout=timeout) as client:
            response = client.post(f"{url.rstrip('/')}/layout-parsing", json=payload)
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PreflightError(f"{kind} canary request failed: {exc}") from exc
    markdown, doc_preprocessor = _extract_canary_result(
        body,
        kind,
        expected_text=expected_text,
        expected_unwarping=expected_unwarping,
    )
    return {
        "passed": True,
        "durationSeconds": round(time.monotonic() - request_started, 3),
        "markdown": markdown,
        "markdownCharacters": len(markdown),
        "docPreprocessor": doc_preprocessor,
    }


def _extract_canary_result(
    response_body: dict[str, Any],
    kind: str,
    *,
    expected_text: str,
    expected_unwarping: bool,
) -> tuple[str, dict[str, Any]]:
    if response_body.get("errorCode") not in (None, 0):
        raise PreflightError(
            f"{kind} canary returned errorCode={response_body.get('errorCode')}: "
            f"{response_body.get('errorMsg', '')}"
        )
    pages = response_body.get("result", {}).get("layoutParsingResults")
    if not isinstance(pages, list) or not pages:
        raise PreflightError(f"{kind} canary returned no layoutParsingResults")
    markdown_parts = []
    preprocessing_results = []
    for index, page in enumerate(pages):
        text = page.get("markdown", {}).get("text") if isinstance(page, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise PreflightError(f"{kind} canary page {index + 1} has empty markdown.text")
        markdown_parts.append(text.strip())
        pruned_result = page.get("prunedResult") if isinstance(page, dict) else None
        if not isinstance(pruned_result, dict):
            raise PreflightError(
                f"{kind} canary page {index + 1} has no DocPreprocessor execution evidence"
            )
        model_settings = pruned_result.get("model_settings")
        doc_preprocessor = pruned_result.get("doc_preprocessor_res")
        if (
            not isinstance(model_settings, dict)
            or model_settings.get("use_doc_preprocessor") is not True
            or not isinstance(doc_preprocessor, dict)
        ):
            raise PreflightError(
                f"{kind} canary page {index + 1} did not execute DocPreprocessor"
            )
        preprocessor_settings = doc_preprocessor.get("model_settings")
        if not isinstance(preprocessor_settings, dict):
            raise PreflightError(
                f"{kind} canary page {index + 1} has no DocPreprocessor model settings"
            )
        if preprocessor_settings.get("use_doc_orientation_classify") is not True:
            raise PreflightError(
                f"{kind} canary page {index + 1} did not execute orientation classification"
            )
        if preprocessor_settings.get("use_doc_unwarping") is not expected_unwarping:
            raise PreflightError(
                f"{kind} canary page {index + 1} unwarping execution does not match request"
            )
        angle = doc_preprocessor.get("angle")
        if not isinstance(angle, int):
            raise PreflightError(
                f"{kind} canary page {index + 1} has no orientation angle evidence"
            )
        preprocessing_results.append(
            {
                "orientationEnabled": True,
                "unwarpingEnabled": expected_unwarping,
                "angle": angle,
            }
        )

    markdown = "\n\n".join(markdown_parts)
    normalized_markdown = " ".join(markdown.upper().split())
    missing_tokens = [
        token for token in expected_text.upper().split() if token not in normalized_markdown
    ]
    if missing_tokens:
        raise PreflightError(
            f"{kind} canary Markdown is missing expected content: "
            f"{', '.join(missing_tokens)}; got {markdown[:300]!r}"
        )
    if kind == "rotated" and not any(
        item["angle"] in {90, 270} for item in preprocessing_results
    ):
        raise PreflightError("rotated canary was not corrected by orientation classification")
    return markdown, preprocessing_results[0]


def _read_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0])
    except (OSError, ValueError, IndexError):
        return {}
    return values


def validate_available_memory_floor(minimum_gb: int) -> dict[str, Any]:
    if isinstance(minimum_gb, bool) or not isinstance(minimum_gb, int) or minimum_gb <= 0:
        raise PreflightError("Available-memory reserve must be a positive integer")
    memory = _read_meminfo()
    available_kib = memory.get("MemAvailable")
    minimum_kib = minimum_gb * 1024**2
    if not isinstance(available_kib, int):
        raise PreflightError("Cannot read MemAvailable for the UMA reserve check")
    if available_kib < minimum_kib:
        raise PreflightError(
            f"Available UMA is below the configured {minimum_gb} GiB floor "
            f"({available_kib / 1024**2:.2f} GiB available)"
        )
    return {
        "minimumAvailableGiB": minimum_gb,
        "memAvailableKiB": available_kib,
        "memTotalKiB": memory.get("MemTotal"),
    }


def _read_sglang_chat_completion_count(url: str, timeout: float) -> float:
    try:
        with httpx.Client(trust_env=False, timeout=timeout) as client:
            response = client.get(f"{url.rstrip('/')}/metrics")
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise PreflightError(f"Cannot read SGLang request metrics: {exc}") from exc

    total = 0.0
    for line in response.text.splitlines():
        if not line.startswith("sglang:http_requests_total{"):
            continue
        labels_end = line.find("}")
        if labels_end < 0:
            continue
        labels = dict(re.findall(r'(\w+)="([^"]*)"', line[: labels_end + 1]))
        if labels.get("endpoint") != "/v1/chat/completions" or labels.get("method") != "POST":
            continue
        try:
            total += float(line[labels_end + 1 :].strip())
        except ValueError as exc:
            raise PreflightError("SGLang request metrics contain an invalid counter") from exc
    return total


def _completed_sglang_request_ids(log_text: str) -> set[str]:
    request_id_pattern = re.compile(
        r"\brid=(?:\[\s*)?(?:['\"]([^'\"]+)['\"]|([^,\s\]\)]+))"
    )
    received: set[str] = set()
    finished: set[str] = set()
    for line in log_text.splitlines():
        match = request_id_pattern.search(line)
        if match is None:
            continue
        request_id = match.group(1) or match.group(2)
        if "Receive:" in line:
            received.add(request_id)
        if "Finish:" in line:
            finished.add(request_id)
    return received & finished


def _read_process_memory(pid: int) -> dict[str, int | None]:
    values: dict[str, int] = {}
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(("VmHWM:", "VmRSS:")):
                key, raw = line.split(":", 1)
                values[key] = int(raw.strip().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return {
        "servicePid": pid,
        "serviceVmHwmKiB": values.get("VmHWM"),
        "serviceVmRssKiB": values.get("VmRSS"),
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temporary:
            json.dump(payload, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def validate_cuda_memory_limit_report(
    path: Path,
    *,
    expected_limit_gb: int,
    expected_pid: int,
) -> dict[str, Any]:
    report = _load_json_mapping(path, "CUDA memory limit report")
    if report.get("schemaVersion") != 1:
        raise PreflightError("CUDA memory limit report schema is invalid")
    if report.get("pid") != expected_pid:
        raise PreflightError("CUDA memory limit report PID does not match service")

    limit_gb = report.get("cudaMemoryLimitGiB")
    limit_bytes = report.get("cudaMemoryLimitBytes")
    total_bytes = report.get("deviceTotalMemoryBytes")
    fraction = report.get("allocatorFraction")
    if (
        isinstance(limit_gb, bool)
        or not isinstance(limit_gb, int)
        or limit_gb != expected_limit_gb
    ):
        raise PreflightError("CUDA memory limit report limit does not match")
    if (
        isinstance(limit_bytes, bool)
        or not isinstance(limit_bytes, int)
        or limit_bytes != expected_limit_gb * 1024**3
    ):
        raise PreflightError("CUDA memory limit report byte limit does not match")
    if (
        isinstance(total_bytes, bool)
        or not isinstance(total_bytes, int)
        or total_bytes <= 0
        or total_bytes < limit_bytes
    ):
        raise PreflightError("CUDA memory limit report device total is invalid")
    if (
        isinstance(fraction, bool)
        or not isinstance(fraction, (int, float))
        or not math.isfinite(float(fraction))
        or not math.isclose(
            float(fraction), limit_bytes / total_bytes, rel_tol=1e-12, abs_tol=0.0
        )
    ):
        raise PreflightError("CUDA memory limit report allocator fraction is invalid")
    return report


def run_canaries(
    url: str,
    attestation_path: Path,
    timeout: float = 600.0,
    service_pid: int | None = None,
    config_path: Path | None = None,
    memory_report_path: Path | None = None,
    cuda_memory_limit_gb: int | None = None,
) -> dict[str, Any]:
    probe_service(url)
    cuda_memory_limit = None
    if memory_report_path is not None or cuda_memory_limit_gb is not None:
        if (
            memory_report_path is None
            or cuda_memory_limit_gb is None
            or service_pid is None
        ):
            raise PreflightError("Canary memory limit evidence is incomplete")
        cuda_memory_limit = validate_cuda_memory_limit_report(
            memory_report_path,
            expected_limit_gb=cuda_memory_limit_gb,
            expected_pid=service_pid,
        )
    environment = collect_environment_attestation()
    pipeline = validate_pipeline_config(config_path) if config_path is not None else None
    memory_before = _read_meminfo()
    canaries: dict[str, dict[str, Any]] = {}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="paddleocr-canary-") as temporary:
        canary_files = _create_canary_files(Path(temporary))
        specs = (
            ("image", 1, False, "THOR IMAGE CANARY 2026"),
            ("pdf", 0, False, "THOR PDF CANARY 2026"),
            ("rotated", 1, False, "THOR ROTATED CANARY 2026"),
        )
        for kind, file_type, use_unwarping, expected_text in specs:
            path = canary_files[kind]
            payload = _build_canary_payload(
                path,
                file_type=file_type,
                kind=kind,
                use_doc_unwarping=use_unwarping,
            )
            canaries[kind] = _post_canary(
                url,
                payload,
                kind=kind,
                expected_text=expected_text,
                expected_unwarping=use_unwarping,
                timeout=timeout,
            )

        if not PHOTO_FIXTURE_PATH.is_file() or _sha256(PHOTO_FIXTURE_PATH) != PHOTO_FIXTURE_SHA256:
            raise PreflightError("Official photographed canary fixture checksum does not match")
        distorted_results = {}
        for variant, use_unwarping in (("baseline", False), ("retry", True)):
            request_kind = f"distorted-{variant}"
            payload = _build_canary_payload(
                PHOTO_FIXTURE_PATH,
                file_type=1,
                kind=request_kind,
                use_doc_unwarping=use_unwarping,
            )
            expected_text = (
                " ".join(PHOTO_FIXTURE_EXPECTED_PHRASES)
                if use_unwarping
                else ""
            )
            result = _post_canary(
                url,
                payload,
                kind=request_kind,
                expected_text=expected_text,
                expected_unwarping=use_unwarping,
                timeout=timeout,
            )
            normalized = " ".join(result["markdown"].upper().split())
            result["matchedExpectedPhrases"] = sum(
                phrase in normalized for phrase in PHOTO_FIXTURE_EXPECTED_PHRASES
            )
            distorted_results[variant] = result
        if distorted_results["retry"]["matchedExpectedPhrases"] != len(
            PHOTO_FIXTURE_EXPECTED_PHRASES
        ):
            raise PreflightError(
                "Distorted retry canary did not match all expected phrases"
            )
        if (
            distorted_results["retry"]["matchedExpectedPhrases"]
            < distorted_results["baseline"]["matchedExpectedPhrases"]
        ):
            raise PreflightError(
                "Distorted retry canary regressed expected text relative to unwarped=false"
            )
        canaries["distorted"] = {
            "passed": True,
            "fixture": {
                "provenance": f"PaddleX official doc_preprocessor demo asset: {PHOTO_FIXTURE_URL}",
                "sha256": PHOTO_FIXTURE_SHA256,
                "expectedPhrases": PHOTO_FIXTURE_EXPECTED_PHRASES,
            },
            **distorted_results,
        }

    memory_after = _read_meminfo()
    attestation = {
        "schemaVersion": OCR_ATTESTATION_SCHEMA_VERSION,
        **environment,
        **({"pipeline": pipeline} if pipeline is not None else {}),
        **(
            {"cudaMemoryLimit": cuda_memory_limit}
            if cuda_memory_limit is not None
            else {}
        ),
        "pipelineApi": f"{url.rstrip('/')}/layout-parsing",
        "canaryPassed": True,
        "canaries": canaries,
        "durationSeconds": round(time.monotonic() - started, 3),
        "memory": {
            "memTotalKiB": memory_after.get("MemTotal"),
            "memAvailableBeforeKiB": memory_before.get("MemAvailable"),
            "memAvailableAfterKiB": memory_after.get("MemAvailable"),
            "processMaxRssKiB": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            **(_read_process_memory(service_pid) if service_pid is not None else {}),
        },
        "pid": os.getpid(),
        **(
            {"processIdentity": collect_process_identity(service_pid, url)}
            if service_pid is not None
            else {}
        ),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(attestation_path, attestation)
    return attestation


def run_sglang_canary(
    paddlex_url: str,
    sglang_url: str,
    served_model_name: str,
    attestation_path: Path,
    *,
    pipeline_pid: int,
    sglang_pid: int,
    config_path: Path,
    memory_report_path: Path,
    cuda_memory_limit_gb: int,
    sglang_log_path: Path,
    sglang_log_offset: int,
    sglang_python_path: Path,
    main_llm_memory_reserve_gb: int,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """Run one end-to-end canary and attest the remote SGLang topology."""
    paddlex_base_url = paddlex_url.rstrip("/")
    sglang_base_url = sglang_url.rstrip("/")
    probe_sglang_service(sglang_base_url, served_model_name, min(timeout, 30.0))
    probe_service(paddlex_base_url, min(timeout, 30.0))
    pipeline = validate_sglang_pipeline_config(config_path)
    configured_model = pipeline.get("sglangBackend", {}).get("servedModelName")
    if configured_model is not None and configured_model != served_model_name:
        raise PreflightError("Canary served model does not match the Pipeline config")
    cuda_memory_limit = validate_cuda_memory_limit_report(
        memory_report_path,
        expected_limit_gb=cuda_memory_limit_gb,
        expected_pid=pipeline_pid,
    )
    if type(sglang_log_offset) is not int or sglang_log_offset < 0:
        raise PreflightError("SGLang log offset must be a non-negative integer")
    try:
        initial_log_size = sglang_log_path.stat().st_size
    except OSError as exc:
        raise PreflightError(f"Cannot inspect SGLang request log: {exc}") from exc
    if initial_log_size < sglang_log_offset:
        raise PreflightError("SGLang log offset is beyond the current log size")
    memory_reserve_before = validate_available_memory_floor(
        main_llm_memory_reserve_gb
    )
    request_count_before = _read_sglang_chat_completion_count(
        sglang_base_url, min(timeout, 30.0)
    )

    with tempfile.TemporaryDirectory(prefix="paddleocr-sglang-canary-") as temporary:
        image_path = _create_canary_files(
            Path(temporary), image_text=SGLANG_CANARY_TEXT
        )["image"]
        payload = _build_canary_payload(
            image_path,
            file_type=1,
            kind="sglang-image",
            use_doc_unwarping=False,
        )
        result = _post_canary(
            paddlex_base_url,
            payload,
            kind="SGLang image",
            expected_text=SGLANG_CANARY_TEXT,
            expected_unwarping=False,
            timeout=timeout,
        )

        deadline = time.monotonic() + max(timeout, 0.0)
        appended_bytes = 0
        appended_log = ""
        completed_request_ids: set[str] = set()
        request_count_after = request_count_before
        while True:
            try:
                current_size = sglang_log_path.stat().st_size
            except OSError as exc:
                raise PreflightError(f"Cannot inspect SGLang request log: {exc}") from exc
            if current_size < sglang_log_offset:
                raise PreflightError("SGLang request log was truncated during canary")
            appended_bytes = current_size - sglang_log_offset
            if appended_bytes > 1024 * 1024:
                raise PreflightError(
                    "SGLang request log appended more than metadata-only evidence"
                )
            if appended_bytes > 0:
                try:
                    with sglang_log_path.open("rb") as stream:
                        stream.seek(sglang_log_offset)
                        appended_log = stream.read(appended_bytes).decode(
                            "utf-8", errors="replace"
                        )
                except OSError as exc:
                    raise PreflightError(
                        f"Cannot read SGLang request log evidence: {exc}"
                    ) from exc
                completed_request_ids = _completed_sglang_request_ids(appended_log)
            request_count_after = _read_sglang_chat_completion_count(
                sglang_base_url, min(max(timeout, 0.05), 5.0)
            )
            request_count_delta = request_count_after - request_count_before
            required_log_pairs = max(1, math.ceil(request_count_delta))
            if (
                appended_bytes > 0
                and request_count_delta > 0
                and len(completed_request_ids) >= required_log_pairs
            ) or time.monotonic() >= deadline:
                break
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        if appended_bytes <= 0:
            raise PreflightError("SGLang request log did not change after the canary")
        if request_count_after <= request_count_before:
            raise PreflightError(
                "SGLang chat completion counter did not increase after the canary"
            )
        if not appended_log.strip():
            raise PreflightError("SGLang request log appended no metadata evidence")
        encoded_image = str(payload["file"])
        if encoded_image in appended_log or SGLANG_CANARY_TEXT in appended_log:
            raise PreflightError("SGLang request log contains canary payload content")
        if len(completed_request_ids) < max(
            1, math.ceil(request_count_after - request_count_before)
        ):
            raise PreflightError(
                "SGLang request metadata has no matched Receive/Finish request ID"
            )

    memory_reserve_after = validate_available_memory_floor(
        main_llm_memory_reserve_gb
    )
    environment = collect_environment_attestation()
    sglang_dependencies = check_sglang_dependency_names(sglang_python_path)
    pipeline_identity = collect_process_identity(pipeline_pid, paddlex_base_url)
    sglang_identity = collect_process_identity(sglang_pid, sglang_base_url)
    batch_settings = pipeline.get("batchAndConcurrencySettings", {})
    canary_evidence = {
        "requestCount": 1,
        "sglangChatCompletionsBefore": request_count_before,
        "sglangChatCompletionsAfter": request_count_after,
        "sglangChatCompletionsDelta": request_count_after - request_count_before,
        "matchedRequestLogPairs": len(completed_request_ids),
        "fileType": 1,
        "expectedMarkdownMatched": True,
        "markdownCharacters": result["markdownCharacters"],
        "docPreprocessor": result["docPreprocessor"],
        "sglangLogOffsetBefore": sglang_log_offset,
        "sglangLogOffsetAfter": sglang_log_offset + appended_bytes,
        "sglangLogBytesAppended": appended_bytes,
        "metadataOnlyLogEvidence": True,
    }
    attestation = {
        "schemaVersion": SGLANG_OCR_ATTESTATION_SCHEMA_VERSION,
        **environment,
        "mode": "remote_sglang",
        "fullApi": f"{paddlex_base_url}/layout-parsing",
        "pipelineApi": f"{paddlex_base_url}/layout-parsing",
        "vlmBackend": "sglang-server",
        "sglangApi": f"{sglang_base_url}/v1",
        "servedModelName": served_model_name,
        "pipeline": pipeline,
        "pipelineConfigSha256": pipeline.get("configSha256"),
        "modelManifestSha256": pipeline.get("manifestSha256"),
        "processIdentity": pipeline_identity,
        "pipelineProcessIdentity": pipeline_identity,
        "sglangProcessIdentity": sglang_identity,
        "actualPackageVersions": sglang_dependencies["versions"],
        "requiredSGLangDistributions": sglang_dependencies["requiredDistributions"],
        "batchAndConcurrencySettings": batch_settings,
        "cudaMemoryLimit": cuda_memory_limit,
        "memoryReserve": {
            "minimumAvailableGiB": main_llm_memory_reserve_gb,
            "before": memory_reserve_before,
            "after": memory_reserve_after,
        },
        "canaryPassed": True,
        "canaryEvidence": canary_evidence,
        "canaries": {"image": {"passed": True, **canary_evidence}},
        "pid": os.getpid(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(attestation_path, attestation)
    return attestation


def _check_env(config_path: Path) -> dict[str, Any]:
    environment = collect_environment_attestation()
    environment["pipeline"] = validate_pipeline_config(config_path)
    return environment


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_env = subparsers.add_parser("check-env")
    check_env.add_argument("--config", required=True, type=Path)

    probe = subparsers.add_parser("probe")
    probe.add_argument("--url", required=True)
    probe.add_argument("--timeout", type=float, default=5.0)

    reuse = subparsers.add_parser("reuse")
    reuse.add_argument("--url", required=True)
    reuse.add_argument("--attestation", required=True, type=Path)
    reuse.add_argument("--config", required=True, type=Path)
    reuse.add_argument("--max-age-seconds", required=True, type=float)
    reuse.add_argument("--memory-report", required=True, type=Path)
    reuse.add_argument("--cuda-memory-limit-gb", required=True, type=int)

    canary = subparsers.add_parser("canary")
    canary.add_argument("--url", required=True)
    canary.add_argument("--attestation", required=True, type=Path)
    canary.add_argument("--config", required=True, type=Path)
    canary.add_argument("--timeout", type=float, default=600.0)
    canary.add_argument("--service-pid", type=int)
    canary.add_argument("--memory-report", required=True, type=Path)
    canary.add_argument("--cuda-memory-limit-gb", required=True, type=int)

    check_sglang_env = subparsers.add_parser("check-sglang-env")
    check_sglang_env.add_argument("--python", required=True, type=Path)
    check_sglang_env.add_argument("--config", required=True, type=Path)

    check_memory_reserve = subparsers.add_parser("check-memory-reserve")
    check_memory_reserve.add_argument("--minimum-gb", required=True, type=int)

    probe_sglang = subparsers.add_parser("probe-sglang")
    probe_sglang.add_argument("--url", required=True)
    probe_sglang.add_argument("--served-model-name", required=True)
    probe_sglang.add_argument("--timeout", type=float, default=5.0)

    canary_sglang = subparsers.add_parser("canary-sglang")
    canary_sglang.add_argument("--paddlex-url", required=True)
    canary_sglang.add_argument("--sglang-url", required=True)
    canary_sglang.add_argument("--served-model-name", required=True)
    canary_sglang.add_argument("--attestation", required=True, type=Path)
    canary_sglang.add_argument("--pipeline-pid", required=True, type=int)
    canary_sglang.add_argument("--sglang-pid", required=True, type=int)
    canary_sglang.add_argument("--config", required=True, type=Path)
    canary_sglang.add_argument("--memory-report", required=True, type=Path)
    canary_sglang.add_argument("--cuda-memory-limit-gb", required=True, type=int)
    canary_sglang.add_argument("--sglang-log", required=True, type=Path)
    canary_sglang.add_argument("--sglang-log-offset", required=True, type=int)
    canary_sglang.add_argument("--sglang-python", required=True, type=Path)
    canary_sglang.add_argument(
        "--main-llm-memory-reserve-gb", required=True, type=int
    )
    canary_sglang.add_argument("--timeout", type=float, default=600.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "check-env":
            result = _check_env(args.config)
        elif args.command == "probe":
            result = probe_service(args.url, args.timeout)
        elif args.command == "reuse":
            result = validate_reusable_service(
                args.url,
                args.attestation,
                args.config,
                max_age_seconds=args.max_age_seconds,
                memory_report_path=args.memory_report,
                cuda_memory_limit_gb=args.cuda_memory_limit_gb,
            )
        elif args.command == "check-sglang-env":
            dependencies = check_sglang_dependency_names(args.python)
            pipeline = validate_sglang_pipeline_config(args.config)
            result = {
                "pipeline": pipeline,
                "sglangBackend": pipeline["sglangBackend"],
                "dependencies": dependencies["requiredDistributions"],
                "actualVersions": dependencies["versions"],
            }
        elif args.command == "check-memory-reserve":
            result = validate_available_memory_floor(args.minimum_gb)
        elif args.command == "probe-sglang":
            result = probe_sglang_service(
                args.url,
                args.served_model_name,
                args.timeout,
            )
        elif args.command == "canary-sglang":
            result = run_sglang_canary(
                args.paddlex_url,
                args.sglang_url,
                args.served_model_name,
                args.attestation,
                pipeline_pid=args.pipeline_pid,
                sglang_pid=args.sglang_pid,
                config_path=args.config,
                memory_report_path=args.memory_report,
                cuda_memory_limit_gb=args.cuda_memory_limit_gb,
                sglang_log_path=args.sglang_log,
                sglang_log_offset=args.sglang_log_offset,
                sglang_python_path=args.sglang_python,
                main_llm_memory_reserve_gb=args.main_llm_memory_reserve_gb,
                timeout=args.timeout,
            )
        else:
            result = run_canaries(
                args.url,
                args.attestation,
                args.timeout,
                service_pid=args.service_pid,
                config_path=args.config,
                memory_report_path=args.memory_report,
                cuda_memory_limit_gb=args.cuda_memory_limit_gb,
            )
    except PreflightError as exc:
        print(f"P0 preflight failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
