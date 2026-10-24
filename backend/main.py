"""
LLM-Test 后端服务入口

基于 FastAPI 构建的 RESTful API 服务，
提供 OpenAI 兼容的聊天 API 接口。

启动方式:
    # 开发模式（自动重载）
    uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

    # 生产模式
    python -m backend.main
"""

import sys
import hashlib
import asyncio
import inspect
import json
import os
from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from packaging.version import InvalidVersion, Version

# 确保项目根目录在 Python 路径中
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from backend.api import chat_router
from backend.api.chat_session_store import ChatSessionStore, chat_session_registry
from backend.api.agent import router as agent_router
from backend.api.compact import router as compact_router
from backend.api.cycle import router as cycle_router
from backend.api.asr import create_asr_router
from backend.api.request_status import router as request_status_router
from backend.api.session_hub import router as session_hub_router
from backend.config import get_config, reload_config, PROJECT_ROOT
from backend.agent.tool_configuration import (
    AgentToolConfigurationError,
    build_business_tool_catalog,
)
from backend.llm_service import (
    get_llm_service,
    resolve_backend_context_length,
    resolve_effective_max_tokens,
)
from backend.utils.logger import DebugLogger, get_logger
from backend.tools.read_file.paddleocr_client import probe_ocr_endpoint
from backend.tools.read_file.resource_guard import get_default_resource_guard
from backend.request_lifecycle import RequestLifecycleRegistry
from backend.session_hub_runtime import SessionHubRuntime
from backend.vision.body_limit import VisionRequestBodyLimitMiddleware
from backend.vision.validation import redact_data_urls

# 获取配置和日志
config = get_config()
logger = get_logger(__name__, level=config.logging.level)
debug = DebugLogger(config.debug)
session_hub_runtime = SessionHubRuntime()


def _log_asr_debug_audio_privacy_warning(asr_config) -> None:
    if asr_config.observability.save_debug_audio:
        logger.warning(
            "ASR debug audio saving is enabled; privacy-sensitive utterance audio "
            "will be written to the configured debug audio directory"
        )


def _validate_ocr_worker_count(
    ocr_config,
    configured_workers: int | None,
    environment: Mapping[str, str] = os.environ,
) -> None:
    if not getattr(ocr_config, "enabled", True):
        return
    worker_values: list[object] = [configured_workers]
    if environment.get("WEB_CONCURRENCY"):
        worker_values.append(environment["WEB_CONCURRENCY"])
    for value in worker_values:
        if value is None:
            continue
        try:
            workers = int(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("OCR backend worker count must be an integer") from exc
        if workers != 1:
            raise RuntimeError(
                "OCR admission is process-local; backend worker count must be 1"
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理

    启动时初始化 LLM 服务，关闭时释放资源。
    """
    if not isinstance(
        getattr(app.state, "session_hub_runtime", None),
        SessionHubRuntime,
    ):
        app.state.session_hub_runtime = session_hub_runtime
    chat_store = getattr(app.state, "chat_session_store", None)
    if not isinstance(chat_store, ChatSessionStore):
        chat_store = chat_session_registry()
        app.state.chat_session_store = chat_store
    chat_store.bind_runtime(app.state.session_hub_runtime)
    _validate_ocr_worker_count(config.ocr, config.server.workers)
    app.state.request_lifecycle_registry = RequestLifecycleRegistry(
        ttl_seconds=config.vision_automation.request_status_ttl_seconds,
    )
    # A deployment may inject a fully constructed, readiness-checked
    # vision_desktop backend before entering lifespan.  The default remains
    # unavailable; startup never guesses a native backend or grants desktop
    # authority on its own.
    configured_computer_use_backend = getattr(app.state, "computer_use_backend", None)
    if configured_computer_use_backend is None and config.computer_use.enabled:
        from backend.computer_use.runtime import build_runtime_backend
        try:
            configured_computer_use_backend = build_runtime_backend(config)
        except Exception as exc:
            from backend.computer_use.executable_backend import InitializationUnavailableBackend
            configured_computer_use_backend = InitializationUnavailableBackend(exc)
            logger.warning("Computer-use unavailable [%s]: %s",
                configured_computer_use_backend.initialization_error_code, str(exc)[:1024])
    app.state.computer_use_backend = configured_computer_use_backend
    from backend.computer_use.executable_protocol import (
        clear_vision_desktop_backend,
        install_vision_desktop_backend,
    )

    if configured_computer_use_backend is None:
        clear_vision_desktop_backend()
    else:
        install_vision_desktop_backend(configured_computer_use_backend)
    # 启动时
    logger.info("=" * 60)
    logger.info("LLM-Test 服务启动中...")
    logger.info(f"项目根目录: {PROJECT_ROOT}")
    logger.info(f"模型路径: {config.model.model_path}")
    logger.info(f"服务地址: http://{config.server.host}:{config.server.port}")
    logger.info("=" * 60)

    debug.model_loading("正在初始化 LLM 服务...")

    # 初始化 LLM 服务
    service = get_llm_service()
    try:
        if service.initialize():
            logger.info("LLM 服务初始化成功")
        else:
            logger.warning("LLM 服务初始化失败，将使用模拟模式")
    except Exception as e:
        logger.error(f"LLM 服务初始化异常: {e}")
        logger.warning("服务将以降级模式运行")

    # Initialize TokenCounter with the configured model path. This is
    # REQUIRED for auto-compact to fire correctly: the previous code never
    # initialized the real tokenizer, so the heuristic `len(content) // 4`
    # was used instead. For Chinese conversations that undercounted by
    # 3-4x and auto-compact never triggered, even when SGLang reported
    # 30k+ full tokens (threshold was 24k, heuristic returned ~7k).
    # We swallow any tokenizer-load failure: a missing/broken tokenizer
    # is degraded-mode, not a startup-fatal error.
    try:
        from backend.compact.tokenizer import TokenCounter
        model_path = config.model.model_path
        if model_path:
            TokenCounter.get(model_path=model_path)
            logger.info(
                f"TokenCounter 已初始化 (model_path={model_path})"
            )
        else:
            logger.warning(
                "model.model_path 未配置, TokenCounter 将使用 multilingual "
                "char-based heuristic fallback"
            )
    except Exception as e:
        logger.warning(f"TokenCounter 初始化失败: {e}, auto-compact 将使用 heuristic")

    # start() only schedules service-owned background work; /health startup
    # never waits for the gate, Markdown scanning or encoder initialization.
    from backend.retrieval.config import LocalMemoryConfig
    from backend.retrieval.local_memory import LocalMemoryService

    app.state.local_memory_service = LocalMemoryService(
        getattr(config, "local_memory", None) or LocalMemoryConfig(),
        project_root=PROJECT_ROOT,
    )
    await app.state.local_memory_service.start()

    # 初始化 Agent 服务
    app.state.agent_runner = None
    if config.agent.enabled:
        try:
            from backend.agent.agent_runner import AgentRunner

            agent_runner = AgentRunner(
                llm_service=service,
                config=config,
                session_hub_runtime=app.state.session_hub_runtime,
            )
            agent_runner.local_memory_service = app.state.local_memory_service
            app.state.agent_runner = agent_runner
            logger.info(f"Agent 服务已启用 (mode={config.agent.agent_mode})")
        except AgentToolConfigurationError:
            # A malformed active catalog is a deployment configuration error,
            # not a recoverable missing-model/runtime condition.  Fail fast so
            # /config cannot advertise a silently empty tool catalog.
            raise
        except ValueError:
            # Catalog parsing can surface plain ValueError from configuration
            # adapters.  Preserve startup-fatal semantics for those errors.
            raise
        except Exception as e:
            logger.warning(f"Agent 服务初始化失败: {e}")
            logger.warning("Agent 端点将不可用")
    else:
        logger.info("Agent 服务未启用")

    # 初始化 Cycle 服务
    app.state.cycle_executor = None
    app.state.camera_service = None
    if config.cycle.enabled:
        try:
            from backend.cycle.camera import CameraService
            from backend.cycle.executor import CycleExecutor

            camera_service = CameraService()
            app.state.camera_service = camera_service

            cycle_executor = CycleExecutor(
                agent_runner=app.state.agent_runner,
                camera_service=camera_service,
                max_depth=config.cycle.max_depth,
                max_duration=config.cycle.max_duration,
                timeout_policy=config.cycle.timeout_policy,
                scheduled_track_max_duration_seconds=config.cycle.scheduled_track_max_duration_seconds,
                max_scheduled_message_days=config.cycle.max_scheduled_message_days,
                event_journal_retention_seconds=config.cycle.event_journal_retention_seconds,
                event_journal_max_records=config.cycle.event_journal_max_records,
                heartbeat_seconds=config.server.session_hub_heartbeat_seconds,
            )
            app.state.cycle_executor = cycle_executor
            logger.info(
                f"Cycle 服务已启用 (max_depth={config.cycle.max_depth}, "
                f"max_duration={config.cycle.max_duration}s)"
            )
        except Exception as e:
            logger.warning(f"Cycle 服务初始化失败: {e}")
    else:
        logger.info("Cycle 服务未启用")

    # 初始化 Compact (/compact) 服务
    app.state.compact_orchestrator = None
    if config.compact.enabled:
        try:
            from backend.compact.orchestrator import CompactOrchestrator

            prompts_cfg = config.agent_prompts.compaction
            if prompts_cfg is None:
                logger.warning(
                    "Compact 启用但 agent_prompts.yaml 缺少 compaction 段，compact 端点将返回 503"
                )
            else:
                app.state.compact_orchestrator = CompactOrchestrator(
                    llm_service=service,
                    config=config.compact,
                    prompts=prompts_cfg,
                )
                logger.info(
                    f"Compact 服务已启用 (strategy={config.compact.strategy}, "
                    f"auto_trigger={config.compact.auto_trigger})"
                )
        except Exception as e:
            logger.warning(f"Compact 服务初始化失败: {e}")
    else:
        logger.info("Compact 服务未启用")

    # Initialize the lightweight ASR gateway. Qwen3-ASR weights stay in the
    # dedicated SGLang process; only Silero ONNX is validated locally.
    app.state.asr_service = None
    if config.asr is not None and config.asr.enabled:
        from backend.asr.service import AsrService

        _log_asr_debug_audio_privacy_warning(config.asr)
        asr_service = AsrService(config.asr)
        asr_service.validate_vad()
        app.state.asr_service = asr_service
        logger.info(
            "ASR gateway 已启用 (model=%s, sglang=%s)",
            config.asr.sglang.served_model_name,
            config.asr.sglang.base_url,
        )
    else:
        logger.info("ASR gateway 未启用")

    # Wire compact_orchestrator into agent_runner (if both are available).
    # MUST be after compact_orchestrator init above — otherwise
    # `app.state.compact_orchestrator` is still None and the wiring is
    # silently skipped. Symptom: agent_runner._should_auto_compact_session
    # always returns False on `if self.compact_orchestrator is None`,
    # so auto-compact never fires in agent mode (manual /compact still
    # works because /v1/compact/run uses app.state.compact_orchestrator
    # directly). Tracked in 踩坑记录3.md §7.5 / trap #16 family.
    if (
        getattr(app.state, "agent_runner", None) is not None
        and getattr(app.state, "compact_orchestrator", None) is not None
    ):
        app.state.agent_runner.compact_orchestrator = app.state.compact_orchestrator
        logger.info("Compact orchestrator 已接入 AgentRunner")

    yield

    # 关闭时
    logger.info("正在关闭服务...")
    if getattr(app.state, "cycle_executor", None) is not None:
        await app.state.cycle_executor.shutdown()
    # 清理 Agent sessions
    if hasattr(app.state, "agent_runner") and app.state.agent_runner:
        app.state.agent_runner.cleanup_expired_sessions(0)  # 清理所有
    if getattr(app.state, "local_memory_service", None) is not None:
        await app.state.local_memory_service.shutdown()
    if getattr(app.state, "asr_service", None) is not None:
        await app.state.asr_service.shutdown()
    app.state.asr_service = None
    computer_use_backend = getattr(app.state, "computer_use_backend", None)
    if computer_use_backend is not None:
        shutdown = getattr(computer_use_backend, "shutdown", None)
        if callable(shutdown):
            result = shutdown()
            if inspect.isawaitable(result):
                await result
    clear_vision_desktop_backend()
    app.state.computer_use_backend = None
    from backend.tools.fetch_process_pool import shutdown_fetch_pool
    shutdown_fetch_pool()
    service.shutdown()
    logger.info("服务已停止")


# 创建 FastAPI 应用
app = FastAPI(
    title="LLM-Test API",
    description=f"""
## LLM 体验程序 API 服务

基于 {config.model.model_name} 模型的对话服务。

### 功能特性
- OpenAI 兼容的 Chat Completions API
- 支持流式输出 (SSE)
- 完善的调试日志系统

### 目标平台
- NVIDIA Jetson Thor (ARM64, Ubuntu)
- NVIDIA Desktop GPU (Blackwell)
    """,
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)
app.state.request_lifecycle_registry = RequestLifecycleRegistry(
    ttl_seconds=config.vision_automation.request_status_ttl_seconds,
)
app.state.session_hub_runtime = session_hub_runtime
app.state.chat_session_store = chat_session_registry()
app.state.chat_session_store.bind_runtime(session_hub_runtime)

# 配置 CORS
cors_origins = config.server.cors_origins
if cors_origins == "*":
    allow_origins = ["*"]
else:
    allow_origins = [origin.strip() for origin in cors_origins.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    VisionRequestBodyLimitMiddleware,
    max_body_bytes=config.vision_automation.max_request_body_bytes,
)


# ============================================================
# 全局异常处理
# ============================================================


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError):
    """Keep malformed Agent tool configuration errors machine-stable."""

    if request.url.path in {"/v1/agent/chat", "/v1/cycle/start"}:
        for error in exc.errors():
            location = error.get("loc", ())
            if "tool_configuration" in location:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": {
                            "code": "invalid_tool_configuration",
                            "message": "invalid tool configuration",
                        }
                    },
                )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理器"""
    logger.error(
        "未处理的异常: %s: %s",
        type(exc).__name__,
        redact_data_urls(exc),
    )
    debug.api(f"请求路径: {request.url.path}, 异常: {type(exc).__name__}")

    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "message": redact_data_urls(exc),
                "type": type(exc).__name__,
            }
        },
    )


# ============================================================
# API 路由
# ============================================================

# 注册聊天 API 路由
app.include_router(chat_router)
app.include_router(request_status_router)
app.include_router(session_hub_router)

# 注册 Agent API 路由（按配置启用）
if config.agent.enabled:
    # Answer Now remains available to internal callers, but is intentionally
    # not included in the public router while the user-facing action is retired.
    app.include_router(agent_router)

# 注册 Cycle API 路由（按配置启用）
if config.cycle.enabled:
    app.include_router(cycle_router)

# 注册 Compact API 路由（按配置启用）
if config.compact.enabled:
    app.include_router(compact_router)

# ASR router stays registered so disabled/degraded deployments return a
# structured WebSocket error instead of a missing route.
asr_websocket_path = (
    config.asr.transport.websocket_path
    if config.asr is not None
    else "/v1/asr/stream"
)
app.include_router(create_asr_router(asr_websocket_path))


@app.get("/", tags=["root"])
async def root():
    """
    根路径 - 服务信息
    """
    return {
        "service": "LLM-Test API",
        "version": "0.1.0",
        "model": config.model.model_name,
        "docs": "/docs",
        "status": "running",
    }


def _resolve_project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _active_tool_configuration_metadata(active_runner, fallback_config) -> dict[str, object]:
    """Expose the running catalog; compile fallback metadata only without a runner."""

    runner_catalog = getattr(active_runner, "tool_catalog", None)
    public_metadata = getattr(runner_catalog, "public_metadata", None)
    if callable(public_metadata):
        runner_config = getattr(active_runner, "config", fallback_config)
        defaults = getattr(getattr(runner_config, "agent", None), "default_profile_tools", {})
        normalized_defaults = {
            profile: list(values)
            for profile, values in defaults.items()
        }
        return {
            **public_metadata(),
            "default_profile_tools": normalized_defaults,
            # Compatibility alias for clients that consumed the pre-SPEC key.
            "defaults": normalized_defaults,
        }

    # A valid deployment without an active runner still deserves the same
    # catalog projection.  Configuration errors intentionally propagate here;
    # fabricating an empty catalog would hide a startup/deployment mistake.
    fallback_catalog = build_business_tool_catalog(fallback_config)
    metadata = fallback_catalog.public_metadata()
    defaults = getattr(getattr(fallback_config, "agent", None), "default_profile_tools", {})
    normalized_defaults = {
        profile: list(values)
        for profile, values in defaults.items()
    }
    metadata["default_profile_tools"] = normalized_defaults
    metadata["defaults"] = normalized_defaults
    return metadata


def _current_ocr_listener_inodes(pid: int, host: str, port: int) -> set[str]:
    if host != "127.0.0.1":
        return set()
    expected_port = f"{port:04X}"
    listener_inodes: set[str] = set()
    for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[3] != "0A":
            continue
        address, raw_port = fields[1].split(":", 1)
        if address == "0100007F" and raw_port == expected_port:
            listener_inodes.add(fields[9])
    process_inodes: set[str] = set()
    for descriptor in Path(f"/proc/{pid}/fd").iterdir():
        try:
            target = os.readlink(descriptor)
        except FileNotFoundError:
            continue
        if target.startswith("socket:["):
            process_inodes.add(target[8:-1])
    return listener_inodes & process_inodes


def _probe_remote_sglang(
    url: str,
    served_model_name: str,
    timeout: float = 2.0,
) -> bool:
    try:
        with httpx.Client(trust_env=False, timeout=timeout) as client:
            health = client.get(f"{url.rstrip('/')}/health")
            health.raise_for_status()
            models = client.get(f"{url.rstrip('/')}/v1/models")
            models.raise_for_status()
            body = models.json()
    except (httpx.HTTPError, ValueError, AttributeError):
        return False
    data = body.get("data") if isinstance(body, dict) else None
    return isinstance(data, list) and any(
        isinstance(item, dict) and item.get("id") == served_model_name
        for item in data
    )


def get_ocr_health_diagnostics(ocr_config, resource_guard, *, probe=probe_ocr_endpoint) -> dict:
    diagnostics = {
        "status": "unavailable",
        "pipeline": None,
        "pipelineVersion": None,
        "engine": None,
        "vlmBackend": None,
        "modelDevice": None,
        "architecture": None,
        "cudaAvailable": False,
        "canaryPassed": False,
        "queueDepth": resource_guard.queue_depth,
    }
    live = probe(ocr_config)
    if not live.available:
        return diagnostics
    if not live.compatible:
        diagnostics["status"] = "incompatible"
        return diagnostics

    attestation_path = Path(ocr_config.attestation_path).expanduser()
    if not attestation_path.is_absolute():
        attestation_path = PROJECT_ROOT / attestation_path
    try:
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
        mode = attestation.get("mode", "local_transformers")
        if mode == "local_transformers":
            expected_schema = 1
            config_path_value = ocr_config.pipeline_config_path
        elif mode == "remote_sglang":
            expected_schema = 2
            config_path_value = ocr_config.sglang_pipeline_config_path
        else:
            raise ValueError("unsupported OCR attestation mode")
        timestamp = datetime.fromisoformat(str(attestation["timestamp"]))
        if timestamp.tzinfo is None:
            raise ValueError("attestation timestamp has no timezone")
        age = (datetime.now(timezone.utc) - timestamp).total_seconds()
        identity = attestation.get("processIdentity")
        pid = identity.get("pid") if isinstance(identity, dict) else None
        if (
            age < -5
            or age > ocr_config.attestation_max_age_seconds
            or not isinstance(pid, int)
            or not Path(f"/proc/{pid}").is_dir()
            or attestation.get("pipelineApi")
            != f"{ocr_config.base_url.rstrip('/')}/layout-parsing"
        ):
            raise ValueError("attestation is stale or process identity is dead")
        if isinstance(identity, dict) and "startTimeTicks" in identity:
            stat_text = Path(f"/proc/{pid}/stat").read_text()
            stat_fields = stat_text[stat_text.rfind(")") + 2 :].split()
            if int(stat_fields[19]) != identity["startTimeTicks"]:
                raise ValueError("attested process start identity changed")
        pipeline = attestation.get("pipeline")
        if not isinstance(pipeline, dict):
            raise ValueError("attestation pipeline is missing")
        attested_inodes = identity.get("listenerSocketInodes")
        if (
            not isinstance(attested_inodes, list)
            or not attested_inodes
            or set(attested_inodes)
            != _current_ocr_listener_inodes(pid, ocr_config.host, ocr_config.port)
        ):
            raise ValueError("attested process no longer owns the OCR listener")
        pipeline_config = _resolve_project_path(config_path_value)
        model_manifest = _resolve_project_path(ocr_config.model_manifest_path)
        expected_config_sha256 = _sha256_file(pipeline_config)
        expected_manifest_sha256 = _sha256_file(model_manifest)
        if (
            pipeline.get("configSha256") != expected_config_sha256
            or pipeline.get("manifestSha256") != expected_manifest_sha256
        ):
            raise ValueError("attested OCR config or model manifest changed")
        packages = attestation.get("packages")
        transformers_version = (
            packages.get("transformers") if isinstance(packages, dict) else None
        )
        try:
            supported_transformers = (
                isinstance(transformers_version, str)
                and Version("5.10.0") <= Version(transformers_version) < Version("6.0.0")
            )
        except InvalidVersion:
            supported_transformers = False
        required = (
            attestation.get("schemaVersion") == expected_schema,
            attestation.get("architecture") == "aarch64",
            attestation.get("cudaAvailable") is True,
            isinstance(attestation.get("gpuName"), str)
            and "thor" in attestation["gpuName"].casefold(),
            attestation.get("engine") == "transformers",
            attestation.get("device") == "cuda:0",
            attestation.get("canaryPassed") is True,
            supported_transformers,
        )
        if mode == "remote_sglang":
            sglang_identity = attestation.get("sglangProcessIdentity")
            if not isinstance(sglang_identity, dict):
                raise ValueError("remote SGLang process identity is missing")
            sglang_pid = sglang_identity.get("pid")
            sglang_start_ticks = sglang_identity.get("startTimeTicks")
            sglang_api = attestation.get("sglangApi")
            served_model_name = attestation.get("servedModelName")
            if (
                attestation.get("vlmBackend") != "sglang-server"
                or attestation.get("fullApi")
                != f"{ocr_config.base_url.rstrip('/')}/layout-parsing"
                or sglang_api != "http://127.0.0.1:30002/v1"
                or served_model_name != "PaddleOCR-VL-1.6-0.9B"
                or attestation.get("pipelineConfigSha256")
                != expected_config_sha256
                or attestation.get("modelManifestSha256")
                != expected_manifest_sha256
                or not isinstance(sglang_pid, int)
                or not isinstance(sglang_start_ticks, int)
                or not Path(f"/proc/{sglang_pid}").is_dir()
            ):
                raise ValueError("remote SGLang identity is invalid")
            sglang_stat = Path(f"/proc/{sglang_pid}/stat").read_text()
            sglang_fields = sglang_stat[sglang_stat.rfind(")") + 2 :].split()
            if int(sglang_fields[19]) != sglang_start_ticks:
                raise ValueError("remote SGLang process start identity changed")
            current_sglang_identity = {
                "bootId": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "executable": os.readlink(f"/proc/{sglang_pid}/exe"),
                "cmdlineSha256": hashlib.sha256(
                    Path(f"/proc/{sglang_pid}/cmdline").read_bytes()
                ).hexdigest(),
                "listenerSocketInodes": sorted(
                    _current_ocr_listener_inodes(
                        sglang_pid, "127.0.0.1", 30002
                    )
                ),
            }
            for key, current_value in current_sglang_identity.items():
                attested_value = sglang_identity.get(key)
                if key == "listenerSocketInodes" and isinstance(attested_value, list):
                    attested_value = sorted(attested_value)
                if not attested_value or attested_value != current_value:
                    raise ValueError(f"remote SGLang process identity changed: {key}")
            if not _probe_remote_sglang(
                sglang_api.removesuffix("/v1"),
                served_model_name,
                ocr_config.connect_timeout_seconds,
            ):
                raise ValueError("remote SGLang endpoint is unavailable")
        if not all(required):
            raise ValueError("attestation failed the Thor CUDA gate")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        diagnostics["status"] = "degraded"
        return diagnostics

    diagnostics.update(
        {
            "status": "healthy",
            "pipeline": pipeline.get("pipelineName"),
            "pipelineVersion": pipeline.get("pipelineVersion"),
            "engine": attestation.get("engine"),
            "vlmBackend": attestation.get("vlmBackend"),
            "modelDevice": attestation.get("device"),
            "architecture": attestation.get("architecture"),
            "cudaAvailable": True,
            "canaryPassed": True,
        }
    )
    return diagnostics


@app.get("/health", tags=["health"])
async def health_check():
    """
    健康检查端点

    返回服务状态信息，用于负载均衡器或容器编排。
    """
    service = get_llm_service()

    ocr = await asyncio.to_thread(
        get_ocr_health_diagnostics,
        config.ocr,
        get_default_resource_guard(config.read_file),
    )
    asr_service = getattr(app.state, "asr_service", None)
    if config.asr is None or not config.asr.enabled:
        asr = {
            "status": "disabled",
            "vad_ready": False,
            "sglang_ready": False,
            "model_name": None,
            "active_sessions": 0,
            "queued_utterances": 0,
        }
    elif asr_service is None:
        asr = {
            "status": "degraded",
            "vad_ready": False,
            "sglang_ready": False,
            "model_name": config.asr.sglang.served_model_name,
            "active_sessions": 0,
            "queued_utterances": 0,
        }
    else:
        asr = await asr_service.health()

    return {
        "status": "healthy" if service.is_ready else "degraded",
        "model_loaded": service.is_ready,
        "model_name": config.model.model_name,
        "platform": config.platform.current,
        "ocr": ocr,
        "asr": asr,
    }


def _serialize_cycle_steps(steps) -> list[dict]:
    """将 CycleStepConfig 列表序列化为前端可用的格式"""
    result = []
    for step in steps:
        if step.step_type == "wait":
            result.append({"type": "wait", "wait": step.wait_seconds})
        elif step.step_type == "message":
            item: dict = {"type": "message", "message": step.message}
            if step.photo:
                item["photo"] = step.photo
            result.append(item)
        elif step.step_type == "cycle":
            result.append(
                {
                    "type": "cycle",
                    "cycle_count": step.cycle_count,
                    "cycle_steps": _serialize_cycle_steps(step.cycle_steps),
                }
            )
    return result


def _serialize_cycle_scheduled_messages(messages) -> list[dict]:
    """Serialize only configured top-level scheduled-track defaults."""
    result = []
    for message in messages:
        item = {
            "id": message.id,
            "time": message.time,
            "recurrence": message.recurrence,
            "day_count": message.day_count,
            "message": message.message,
        }
        if message.photo:
            item["photo"] = message.photo
        result.append(item)
    return result


def _web_ui_asset_id(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:24]


def _is_external_or_web_path(value: str) -> bool:
    normalized = value.strip().lower()
    return (
        normalized.startswith("http://")
        or normalized.startswith("https://")
        or normalized.startswith("//")
        or normalized.startswith("data:")
        or normalized.startswith("blob:")
        or value.strip().startswith("/v1/")
    )


def _resolve_web_ui_asset_path(value: object) -> Path | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed or _is_external_or_web_path(trimmed):
        return None

    path = Path(trimmed).expanduser()
    if path.is_absolute():
        resolved = path.resolve()
        return resolved if resolved.exists() else None

    candidate = (PROJECT_ROOT / path).resolve()
    if candidate.exists():
        return candidate
    public_candidate = (PROJECT_ROOT / "frontend" / "public" / path).resolve()
    return public_candidate if public_candidate.exists() else None


def _web_ui_asset_registry() -> dict[str, Path]:
    paths = [
        config.web_ui.message_avatar.get("user_image_path"),
        config.web_ui.message_avatar.get("assistant_image_path"),
        config.web_ui.vision_welcome.get("icon_image_path"),
    ]
    registry: dict[str, Path] = {}
    for value in paths:
        path = _resolve_web_ui_asset_path(value)
        if path is not None:
            registry[_web_ui_asset_id(path)] = path
    return registry


def _web_ui_asset_url(value: object) -> object:
    path = _resolve_web_ui_asset_path(value)
    if path is None:
        return value
    return f"/v1/webui/assets/{_web_ui_asset_id(path)}"


def _serialize_web_ui_components(current_config) -> dict:
    return {
        "input_bar": current_config.web_ui.input_bar,
        "action_buttons": current_config.web_ui.action_buttons,
        "mermaid": current_config.web_ui.mermaid,
        "page_header": current_config.web_ui.page_header,
        "message_avatar": current_config.web_ui.message_avatar,
        "vision_welcome": current_config.web_ui.vision_welcome,
        "agent_steps": current_config.web_ui.agent_steps,
        "message_templates": {
            "items": current_config.web_ui.message_templates,
        },
    }


@app.get("/v1/webui/assets/{asset_id}", tags=["config"])
async def get_web_ui_asset(asset_id: str):
    path = _web_ui_asset_registry().get(asset_id)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Web UI asset not found")
    return FileResponse(path)


@app.get("/config", tags=["config"])
async def get_current_config():
    """
    获取当前配置

    返回模型和推理相关的配置信息，用于前端初始化设置。
    每次请求重新加载 YAML，确保前端重启后能拿到最新配置。
    """
    current_config = reload_config()
    active_computer_use_mode = getattr(
        getattr(app.state, "computer_use_backend", None), "runtime_mode", "unavailable"
    )
    desired_operator_session = current_config.computer_use.operator_session_enabled
    # 判断是否为视觉模型
    is_vision_model = current_config.model.model_type == "vision"
    service = get_llm_service()
    active_backend = service.active_backend
    context_length = resolve_backend_context_length(current_config, active_backend)
    max_tokens = resolve_effective_max_tokens(current_config, active_backend)

    asr_config = current_config.asr
    asr = None
    if asr_config is not None:
        constraints = asr_config.audio.browser_constraints
        asr = {
            "enabled": asr_config.enabled,
            "sample_rate_hz": asr_config.audio.sample_rate_hz,
            "capture_frame_ms": asr_config.audio.capture_frame_ms,
            "input_gain_db": asr_config.audio.input_gain_db,
            "input_gain_min_db": asr_config.audio.input_gain_min_db,
            "input_gain_max_db": asr_config.audio.input_gain_max_db,
            "input_gain_step_db": asr_config.audio.input_gain_step_db,
            "clip_limit": asr_config.audio.clip_limit,
            "auto_send": asr_config.transcription.auto_send,
            "streaming": asr_config.transcription.streaming,
            "websocket_path": asr_config.transport.websocket_path,
            "public_websocket_url": asr_config.transport.public_websocket_url,
            "abort_timeout_seconds": asr_config.runtime.shutdown_timeout_seconds,
            "max_buffered_frames": asr_config.transport.max_buffered_frames,
            "dictation_magic_send": {
                "commands": asr_config.dictation_magic_send.commands,
                "poll_interval_ms": asr_config.dictation_magic_send.poll_interval_ms,
            },
            "require_secure_context": asr_config.browser_security.require_secure_context_for_voice,
            "browser_constraints": {
                "echo_cancellation": constraints.echo_cancellation,
                "noise_suppression": constraints.noise_suppression,
                "auto_gain_control": constraints.auto_gain_control,
            },
        }

    active_runner = getattr(app.state, "agent_runner", None)
    tool_configuration = _active_tool_configuration_metadata(
        active_runner,
        current_config,
    )
    memory_service = getattr(app.state, "local_memory_service", None)
    if memory_service is not None:
        local_memory_status = memory_service.status_snapshot()
    else:
        from backend.retrieval.config import LocalMemoryConfig
        defaults = LocalMemoryConfig().retrieval
        local_memory_status = {
            "default_enabled": False, "available": False, "status": "disabled",
            "indexed_files": 0, "indexed_chunks": 0, "last_sync": None,
            "error_code": None,
            "applied": {"min_similarity": defaults.min_similarity, "top_k": defaults.top_k},
        }

    return {
        "local_memory": local_memory_status,
        "model": {
            "model_name": current_config.model.model_name,
            "model_type": current_config.model.model_type or "text",
            "tensor_parallel_size": current_config.model.tensor_parallel_size,
            "max_context_length": context_length,
        },
        "vision": {
            "enabled": is_vision_model,
            # vision_upload 段为权威源；ModelConfig 已做 vision_upload → model 回退，
            # 此处再显式优先 vision_upload 以保证 wire 格式稳定。
            "max_image_size": (
                current_config.vision_upload.max_image_size
                if current_config.vision_upload.max_image_size is not None
                else current_config.model.max_image_size
            ),
            "supported_formats": (
                current_config.vision_upload.supported_formats
                or current_config.model.supported_formats
            ),
            "max_image_tokens": (
                current_config.vision_upload.max_image_tokens
                if current_config.vision_upload.max_image_tokens is not None
                else current_config.model.max_image_tokens
            ),
            "max_image_dimension": (
                current_config.vision_upload.max_image_dimension
                if current_config.vision_upload.max_image_dimension is not None
                else current_config.model.max_image_dimension
            ),
            "camera_max_dimension": (
                current_config.vision_upload.camera_max_dimension
                if current_config.vision_upload.camera_max_dimension is not None
                else current_config.model.camera_max_dimension
            ),
            "capture_dimension": current_config.camera.capture_dimension,
            "automation": {
                "enabled": current_config.vision_automation.enabled,
                "interval_seconds": current_config.vision_automation.interval_seconds,
                "max_frames_per_source": current_config.vision_automation.max_frames_per_source,
                "thumbnail_size_px": current_config.vision_automation.thumbnail_size_px,
                "capture_during_inference": current_config.vision_automation.capture_during_inference,
                "lock_first_bundle": current_config.vision_automation.lock_first_bundle,
                "tick_timeout_seconds": current_config.vision_automation.tick_timeout_seconds,
                "consecutive_tick_failure_limit": current_config.vision_automation.consecutive_tick_failure_limit,
                "max_encoded_payload_mib": current_config.vision_automation.max_encoded_payload_mib,
                "max_encoded_payload_bytes": current_config.vision_automation.max_encoded_payload_bytes,
                "initial_jpeg_quality": current_config.vision_automation.initial_jpeg_quality,
                "minimum_jpeg_quality": current_config.vision_automation.minimum_jpeg_quality,
                "minimum_image_dimension_px": current_config.vision_automation.minimum_image_dimension_px,
                "base64_encode_concurrency": current_config.vision_automation.base64_encode_concurrency,
                "max_request_body_mib": current_config.vision_automation.max_request_body_mib,
                "max_request_body_bytes": current_config.vision_automation.max_request_body_bytes,
                "request_status_ttl_seconds": current_config.vision_automation.request_status_ttl_seconds,
            },
        },
        "inference": {
            "active_backend": active_backend,
            "supports_targeted_abort": active_backend in {"sglang", "vllm", "openai_proxy"},
            "temperature": current_config.inference.temperature,
            "top_p": current_config.inference.top_p,
            "top_k": current_config.inference.top_k,
            "max_tokens": max_tokens,
            "repetition_penalty": current_config.inference.repetition_penalty,
            "stream": current_config.inference.stream,
        },
        "sglang": {
            "chat_template": current_config.sglang.chat_template,
        },
        "server": {
            "host": current_config.server.host,
            "port": current_config.server.port,
        },
        "frontend": {
            "dev_port": current_config.frontend.dev_port,
            "api_base_url": current_config.frontend.api_base_url,
            "chat_stream_idle_timeout_seconds": current_config.frontend.chat_stream_idle_timeout_seconds,
            "agent_stream_idle_timeout_seconds": current_config.frontend.agent_stream_idle_timeout_seconds,
            "cycle_stream_idle_timeout_seconds": current_config.frontend.cycle_stream_idle_timeout_seconds,
        },
        "webUI": {
            "skin": {
                "id": current_config.web_ui.skin_id,
                "name": current_config.web_ui.skin_name,
            },
            "content": {
                "bubble_character_limit": (
                    current_config.web_ui.content.bubble_character_limit
                ),
            },
            "layout": current_config.web_ui.layout,
            "components": _serialize_web_ui_components(current_config),
            "effects": current_config.web_ui.effects,
        },
        "platform": {
            "current": current_config.platform.current,
        },
        "runtime_prompts": {
            "enable_runtime_time_context": current_config.runtime_prompts.enable_runtime_time_context,
        },
        "agent": {
            "enabled": current_config.agent.enabled,
            "mode": current_config.agent.agent_mode,
            "entry_agent_name": current_config.agent.entry_agent_name,
            "tool_configuration": tool_configuration,
            # Canonical SPEC 10.3 fields.  Keep the nested projection above
            # for clients that shipped before the modular catalog contract.
            "business_tool_catalog": tool_configuration.get(
                "business_tool_catalog", tool_configuration.get("tools", [])
            ),
            "default_profile_tools": tool_configuration.get(
                "default_profile_tools", tool_configuration.get("defaults", {})
            ),
            "tool_configuration_schema_version": tool_configuration.get(
                "tool_configuration_schema_version", tool_configuration.get("schema_version", 1)
            ),
            "tool_catalog_revision": tool_configuration.get(
                "tool_catalog_revision", tool_configuration.get("catalog_revision", "")
            ),
        },
        "computer_use": {
            "release_ready": current_config.computer_use.ready,
            "operator_session_configured": desired_operator_session,
            "operator_session_active": active_computer_use_mode == "operator_session",
            "restart_required": desired_operator_session != (
                active_computer_use_mode == "operator_session"
            ),
            "runtime_mode": active_computer_use_mode,
            "initialization_error_code": getattr(
                getattr(app.state, "computer_use_backend", None), "initialization_error_code", None),
        },
        "cycle": {
            "enabled": current_config.cycle.enabled,
            "max_depth": current_config.cycle.max_depth,
            "max_duration": current_config.cycle.max_duration,
            "show_thumbnails": current_config.cycle.show_thumbnails,
            "timeout_policy": current_config.cycle.timeout_policy,
            "scheduled_track_max_duration_seconds": (
                current_config.cycle.scheduled_track_max_duration_seconds
            ),
            "max_scheduled_message_days": current_config.cycle.max_scheduled_message_days,
            "event_journal_retention_seconds": (
                current_config.cycle.event_journal_retention_seconds
            ),
            "event_journal_max_records": current_config.cycle.event_journal_max_records,
            "default_sequence": _serialize_cycle_steps(current_config.cycle.default_sequence),
            "default_scheduled_messages": _serialize_cycle_scheduled_messages(
                current_config.cycle.default_scheduled_messages
            ),
        },
        "asr": asr,
    }


# ============================================================
# 主程序入口
# ============================================================


def main():
    """
    主程序入口

    直接运行时启动 uvicorn 服务器。
    """
    import uvicorn

    _validate_ocr_worker_count(config.ocr, config.server.workers)
    logger.info(f"启动服务器: {config.server.host}:{config.server.port}")

    uvicorn.run(
        "backend.main:app",
        host=config.server.host,
        port=config.server.port,
        workers=config.server.workers,
        reload=False,  # 生产模式禁用自动重载
        log_level=config.logging.level.lower(),
    )


if __name__ == "__main__":
    main()
