"""
配置管理模块

负责从 config/config-overall.yaml 及其引用的模块化子配置文件加载配置，
提供类型安全的配置访问接口。

子配置文件路由（由 config-overall.yaml 声明）：
  infer  → config-*-infer.yaml （模型基本信息 model 段 + 推理/KV Cache/SGLang/vLLM/容器）
  server → config-server.yaml  （服务端/前端地址）
  misc   → config-misc.yaml    （compact/debug/platform/logging/runtime prompts）
  agent_operation → config-agent-operation.yaml （agent/read_file/mcp/web_tools）
  vision → config-vision.yaml （vision_upload 手动上传图片 + camera 手动拍照 + 视觉自动化）

兼容说明：
  config-overall.yaml 不再引用任何 *-config-model.yaml。
  新 infer 文件内嵌 model 段（model_path / model_name / model_type）；
  tensor_parallel_size / max_context_length 已去重，仅保留引擎侧，
  backend 会 fallback 读取 sglang/vllm。
  旧 infer 文件若缺 model 段，按同名 sibling 自动回退加载
  （如 *-config-infer.yaml → *-config-model.yaml）。
  图片 5 项优先读 vision_upload 段，缺失时回退读 model 段。
"""

import logging
import json
import math
import os
import re
import yaml
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional

from backend.security.command_policy import (
    DEFAULT_ALLOWED_EXECUTABLES,
    CommandPolicy,
)
from backend.security.environment_builder import (
    DEFAULT_ENVIRONMENT_ALLOWLIST,
    CommandRuntimeConfig,
)
from backend.security.skill_executables import resolve_skill_executables
from backend.agent.tool_configuration import BUSINESS_TOOL_NAMES, SPECIAL_TOOL_NAMES
from backend.computer_use.config import ComputerUseConfig
from backend.retrieval.config import LocalMemoryConfig, load_local_memory_config

if TYPE_CHECKING:
    from backend.asr.config import AsrConfig


logger = logging.getLogger(__name__)


ROUTING_ENTRY_AGENT_NAMES = {"coordinator_1"}
DIRECT_ENTRY_AGENT_NAMES = {"manager_agent", "basic_agent"}
ENTRY_AGENT_NAMES = ROUTING_ENTRY_AGENT_NAMES | DIRECT_ENTRY_AGENT_NAMES


def normalize_agent_mode(mode: str | None, *, log_warnings: bool = True) -> str:
    """归一化 Agent 模式，仅保留 tool_calling。"""
    normalized = str(mode or "tool_calling").strip() or "tool_calling"
    if normalized == "code":
        if log_warnings:
            logger.warning("Agent mode 'code' 已移除，自动回退到 'tool_calling'")
        return "tool_calling"
    if normalized != "tool_calling":
        if log_warnings:
            logger.warning(
                "不支持的 Agent mode=%r，自动回退到 'tool_calling'", normalized
            )
        return "tool_calling"
    return normalized


def _get_project_root() -> Path:
    """
    获取项目根目录路径

    Returns:
        Path: 项目根目录的绝对路径
    """
    # 从当前文件向上查找，找到包含总配置文件的 config 目录
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "config" / "config-overall.yaml").exists():
            return current
        current = current.parent
    # 默认返回 backend 的父目录
    return Path(__file__).resolve().parent.parent


# 项目根目录
PROJECT_ROOT = _get_project_root()
# 总配置文件路径（引用模块化子配置）
CONFIG_PATH = PROJECT_ROOT / "config" / "config-overall.yaml"


@dataclass
class ModelConfig:
    """模型配置（model 段现内嵌于 infer 文件；图片字段优先源为 vision_upload 段）"""

    model_path: Optional[str] = None
    model_name: Optional[str] = None
    tensor_parallel_size: Optional[int] = None
    max_context_length: Optional[int] = None
    # 视觉模型配置
    model_type: Optional[str] = None  # text | vision
    max_image_size: Optional[int] = None  # 最大图片大小 (字节)
    supported_formats: Optional[str] = None  # 支持的图片格式
    max_image_tokens: Optional[int] = None  # 最大图片 token 数
    max_image_dimension: Optional[int] = (
        None  # 所有图片发给 VLM 前的最终缩放上限(px), 0=不缩放
    )
    camera_max_dimension: Optional[int] = (
        None  # 摄像头图片发给 VLM 前的最终缩放上限(px), 0=不缩放
    )


@dataclass
class VisionUploadConfig:
    """VLM 手动上传图片配置（config-vision.yaml 的 vision_upload 段）"""

    max_image_size: Optional[int] = None
    supported_formats: Optional[str] = None
    max_image_tokens: Optional[int] = None
    max_image_dimension: Optional[int] = None
    camera_max_dimension: Optional[int] = None


@dataclass
class InferenceConfig:
    """推理配置"""

    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    max_tokens: Optional[int] = None
    max_new_tokens: Optional[int] = None  # 兼容 SGLang API
    repetition_penalty: Optional[float] = None
    stream: Optional[bool] = None
    timeout: Optional[int] = None  # 请求超时时间（秒），默认 90 分钟
    # 是否通过 stream_options（include_usage/continuous_usage_stats）向推理后端
    # 请求权威 token usage；关闭后流式统计回退到本地估算
    stream_usage_stats: bool = True
    tool_protocol: str = "legacy_text"
    capture_raw_tool_calls: bool = False


@dataclass
class KVCacheConfig:
    """KV Cache 配置"""

    enable_block_reuse: Optional[bool] = None
    max_cache_size_mb: Optional[int] = None


@dataclass
class ServerConfig:
    """服务器配置"""

    host: Optional[str] = None
    port: Optional[int] = None
    workers: Optional[int] = None
    api_key: Optional[str] = None
    cors_origins: Optional[str] = None
    session_hub_heartbeat_seconds: float = 15.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.session_hub_heartbeat_seconds, bool)
            or not isinstance(self.session_hub_heartbeat_seconds, (int, float))
            or not math.isfinite(self.session_hub_heartbeat_seconds)
            or self.session_hub_heartbeat_seconds < 0
        ):
            raise ValueError(
                "server.session_hub_heartbeat_seconds must be finite and non-negative"
            )


@dataclass
class FrontendConfig:
    """前端配置"""

    dev_port: Optional[int] = None
    api_base_url: Optional[str] = None
    chat_stream_idle_timeout_seconds: Optional[int] = None
    agent_stream_idle_timeout_seconds: Optional[int] = None
    cycle_stream_idle_timeout_seconds: Optional[int] = None


@dataclass
class OCRServiceConfig:
    """Connectivity and attestation settings for the isolated PaddleX service."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8118
    base_url: str = "http://127.0.0.1:8118"
    connect_timeout_seconds: float = 2.0
    read_timeout_seconds: float = 600.0
    request_timeout_seconds: float = 600.0
    pipeline_config_path: str = "config/paddleocr-vl-thor.yaml"
    sglang_pipeline_config_path: str = "config/paddleocr-vl-thor-sglang.yaml"
    model_manifest_path: str = "config/paddleocr-vl-thor-model-manifest.json"
    attestation_path: str = "logs/paddleocr-runtime-attestation.json"
    attestation_max_age_seconds: int = 86400
    cuda_memory_limit_gb: int = 16

    def __post_init__(self) -> None:
        if (
            isinstance(self.cuda_memory_limit_gb, bool)
            or not isinstance(self.cuda_memory_limit_gb, int)
            or self.cuda_memory_limit_gb <= 0
        ):
            raise ValueError("cuda_memory_limit_gb must be a positive integer")


@dataclass
class ReadFileConfig:
    """Security, normalization, resource, and cache limits for read_file v7."""

    allowed_roots: List[str] = field(default_factory=list)
    max_source_bytes: int = 536_870_912
    max_archive_members: int = 10_000
    max_archive_uncompressed_bytes: int = 1_073_741_824
    max_archive_compression_ratio: float = 100.0
    max_pdf_pages: int = 1_000
    pdf_chunk_pages: int = 24
    pdf_chunk_bytes: int = 67_108_864
    pdf_chunk_overlap_pages: int = 1
    pdf_retry_chunk_pages: int = 8
    max_image_dimension: int = 8_192
    max_image_pixels: int = 67_108_864
    max_image_frames: int = 200
    max_decoded_image_bytes: int = 536_870_912
    max_normalized_bytes: int = 67_108_864
    image_workers: int = 2
    max_xlsx_sheets: int = 200
    max_xlsx_cells: int = 2_000_000
    max_xlsx_rows: int = 200_000
    max_xlsx_columns: int = 16_384
    office_timeout_seconds: int = 120
    base64_threshold_bytes: int = 16_777_216
    object_url_ttl_seconds: int = 600
    object_server_port: int = 0
    ocr_concurrency: int = 1
    ocr_queue_limit: int = 2
    main_llm_memory_reserve_gb: int = 16
    ocr_retry_count: int = 2
    ocr_max_new_tokens: int = 8_192
    ocr_min_page_markdown_characters: int = 8
    ocr_severe_tilt_degrees: int = 45
    normal_request_use_doc_unwarping: bool = False
    cache_path: str = "~/.cache/llm-deployment/read_file/cache.sqlite3"
    cache_success_ttl_seconds: int = 2_592_000
    cache_partial_ttl_seconds: int = 604_800
    cache_failure_ttl_seconds: int = 3_600

    def __post_init__(self) -> None:
        if self.pdf_chunk_pages <= 0:
            raise ValueError("pdf_chunk_pages must be positive")
        if not 0 <= self.pdf_chunk_overlap_pages < self.pdf_chunk_pages:
            raise ValueError(
                "pdf_chunk_overlap_pages must satisfy 0 <= overlap < pdf_chunk_pages"
            )
        if (
            isinstance(self.ocr_max_new_tokens, bool)
            or not isinstance(self.ocr_max_new_tokens, int)
            or self.ocr_max_new_tokens <= 0
        ):
            raise ValueError("ocr_max_new_tokens must be a positive integer")


@dataclass
class WebUIContentConfig:
    """Backend-validated UI-only bubble rendering policy."""

    bubble_character_limit: int = 20_000

    def __post_init__(self) -> None:
        if (
            isinstance(self.bubble_character_limit, bool)
            or not isinstance(self.bubble_character_limit, int)
            or self.bubble_character_limit <= 0
        ):
            raise ValueError(
                "webUI.content.bubble_character_limit must be a positive integer"
            )


@dataclass
class WebUIConfig:
    """Web UI skin configuration."""

    skin_id: str = "default-v1"
    skin_name: str = "Default"
    content: WebUIContentConfig = field(default_factory=WebUIContentConfig)
    layout: dict[str, float] = field(
        default_factory=lambda: {
            "header_background_alpha": 0.7,
            "footer_background_alpha": 0.6,
        }
    )
    input_bar: dict[str, int] = field(
        default_factory=lambda: {
            "min_height_px": 48,
            "max_rows": 6,
            "line_height_px": 24,
            "vertical_padding_px": 24,
            "border_radius_px": 16,
        }
    )
    action_buttons: dict[str, dict[str, int]] = field(
        default_factory=lambda: {
            "send": {"width_px": 48, "height_px": 48},
            "stop": {"width_px": 48, "height_px": 48},
            "answer_now": {"width_px": 48, "height_px": 48},
        }
    )
    mermaid: dict[str, int] = field(default_factory=lambda: {"min_height_px": 360})
    page_header: dict[str, object] = field(
        default_factory=lambda: {
            "display": False,
            "text_title": "LLM-Test",
            "vision_title": "LLM-Test 视觉模式",
            "subtitle": "NVIDIA Jetson Thor",
        }
    )
    message_avatar: dict[str, object] = field(
        default_factory=lambda: {
            "user_image_path": "",
            "assistant_image_path": "",
            "size_px": 32,
        }
    )
    vision_welcome: dict[str, str] = field(
        default_factory=lambda: {
            "title": "欢迎使用视觉聊天",
            "description": "上传图片或使用摄像头拍照，询问关于图片的问题。",
            "icon_image_path": "",
        }
    )
    effects: dict[str, dict[str, object]] = field(
        default_factory=lambda: {
            "aurora_glow": {
                "enabled": False,
                "intensity": 0.72,
                "blur_px": 18,
                "chromatic_aberration": 0.018,
                "refraction_strength": 0.045,
                "tint_a": "#ffffff",
                "tint_b": "#76b900",
                "opacity": 0.72,
                "animated": True,
                "max_dpr": 1.5,
            }
        }
    )
    agent_steps: dict[str, bool] = field(
        default_factory=lambda: {"fold_after_final_answer": True}
    )
    message_templates: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.content, dict):
            self.content = WebUIContentConfig(**self.content)
        elif not isinstance(self.content, WebUIContentConfig):
            raise ValueError("webUI.content must be an object")


WEB_UI_SKIN_NAMES = {
    "default-v1": "Default Skin v1",
    "aurora-glow-r3f-v1": "Aurora Glow R3F v1",
}


@dataclass
class DebugConfig:
    """调试开关配置"""

    enabled: Optional[bool] = None
    debug_model_loading: Optional[bool] = None
    debug_inference: Optional[bool] = None
    debug_api: Optional[bool] = None
    debug_memory: Optional[bool] = None
    debug_config: Optional[bool] = None
    debug_frontend: Optional[bool] = None


@dataclass
class PlatformConfig:
    """平台配置"""

    current: Optional[str] = None
    # Jetson Thor 配置
    use_dla: Optional[bool] = None
    power_mode: Optional[str] = None
    memory_optimize: Optional[bool] = None
    # Desktop 配置
    multi_gpu: Optional[bool] = None
    gpu_devices: Optional[List[int]] = None


@dataclass
class LoggingConfig:
    """日志配置"""

    level: Optional[str] = None
    file_path: Optional[str] = None
    format: Optional[str] = None
    colorize: Optional[bool] = None


@dataclass
class RuntimePromptsConfig:
    """运行时提示配置"""
    enable_runtime_time_context: bool = False


@dataclass
class ContainerConfig:
    """TensorRT-LLM 容器配置"""

    enabled: Optional[bool] = None
    api_url: Optional[str] = None
    model_name: Optional[str] = None  # 容器内的模型名称，留空则使用主配置的 model_name
    timeout: Optional[int] = None  # 请求超时（秒）
    max_retries: Optional[int] = None


@dataclass
class VLLMConfig:
    """vLLM 配置"""

    # 基础配置
    host: Optional[str] = None
    port: Optional[int] = None
    tensor_parallel_size: Optional[int] = None
    pipeline_parallel_size: Optional[int] = None
    distributed_executor_backend: Optional[str] = None
    max_model_len: Optional[int] = None
    gpu_memory_utilization: Optional[float] = None
    dtype: Optional[str] = None
    quantization: Optional[str] = None
    # 模型加载配置
    tokenizer: Optional[str] = None
    revision: Optional[str] = None
    tokenizer_revision: Optional[str] = None
    load_format: Optional[str] = None
    download_dir: Optional[str] = None
    trust_remote_code: Optional[bool] = None
    enforce_eager: Optional[bool] = None
    # 批处理与内存配置
    max_num_seqs: Optional[int] = None
    max_num_batched_tokens: Optional[int] = None
    block_size: Optional[int] = None
    enable_prefix_caching: Optional[bool] = None
    kv_cache_dtype: Optional[str] = None
    swap_space: Optional[int] = None
    cpu_offload_gb: Optional[int] = None
    # Attention 配置
    attention_backend: Optional[str] = None
    disable_flash_attn: Optional[bool] = None
    flash_attn_version: Optional[int] = (
        None  # vLLM 0.17.1+ FA版本为选：2/3/4，None=自动
    )
    # 性能优化配置
    enable_chunked_prefill: Optional[bool] = None
    max_seq_len_to_capture: Optional[int] = None
    disable_sliding_window: Optional[bool] = None
    disable_log_stats: Optional[bool] = None
    disable_log_requests: Optional[bool] = None
    max_logprobs: Optional[int] = None
    enable_prompt_logprobs: Optional[bool] = None
    guided_decoding_backend: Optional[str] = None
    disable_custom_all_reduce: Optional[bool] = None
    # 推理模式配置
    enable_reasoning: Optional[bool] = None
    reasoning_parser: Optional[str] = None
    chat_template: Optional[str] = None
    tool_call_parser: Optional[str] = None
    tool_parser_plugin: Optional[str] = None
    enable_auto_tool_choice: Optional[bool] = None
    served_model_name: Optional[str] = None
    # LoRA 配置
    enable_lora: Optional[bool] = None
    max_loras: Optional[int] = None
    max_lora_rank: Optional[int] = None
    lora_extra_vocab_size: Optional[int] = None
    max_cpu_loras: Optional[int] = None
    fully_sharded_loras: Optional[bool] = None
    # 推测解码配置
    speculative_model: Optional[str] = None
    num_speculative_tokens: Optional[int] = None
    speculative_max_model_len: Optional[int] = None
    speculative_disable_by_batch_size: Optional[int] = None
    ngram_prompt_lookup_max: Optional[int] = None
    ngram_prompt_lookup_min: Optional[int] = None
    # 多模态配置
    enable_multi_modal: Optional[bool] = None
    disable_mm_preprocessor_cache: Optional[bool] = None
    # 调度器配置
    preemption_mode: Optional[str] = None
    scheduler_delay_factor: Optional[float] = None
    # 其他配置
    model_loader_extra_config: Optional[str] = None
    ignore_patterns: Optional[str] = None
    max_parallel_loading_workers: Optional[int] = None


@dataclass
class MCPServerConfig:
    """单个 MCP 服务器配置"""

    name: str = ""
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    url: Optional[str] = None


@dataclass
class MCPConfig:
    """MCP (Model Context Protocol) 配置"""

    servers: List[MCPServerConfig] = field(default_factory=list)


@dataclass
class WebToolsConfig:
    """Web 工具配置"""

    provider: str = "searxng"
    enable_wikipedia: bool = True
    # Optional proxy for direct webpage/Wikipedia fetches. SearXNG has its own
    # container-side outgoing proxy configuration and is always contacted directly.
    fetch_proxy: Optional[str] = None

    # SearXNG provider settings
    searxng_base_url: str = "http://127.0.0.1:8080"
    searxng_search_path: str = "/search"
    searxng_timeout: int = 10
    searxng_format: str = "json"
    searxng_language: str = "all"
    searxng_safe_search: int = 0
    searxng_max_pages: int = 3
    searxng_categories: List[str] = field(default_factory=list)
    searxng_engines: List[str] = field(default_factory=list)
    searxng_api_token: Optional[str] = None
    searxng_verify_tls: bool = True

    # 搜索工具超时（LLM 无法控制，由配置决定）
    search_timeout: int = 10
    wikipedia_search_timeout: int = 10

    # 网页抓取稳健性配置
    fetch_transport: str = "auto"  # auto | curl | curl_cffi | crawl4ai
    fetch_fallback_transport: Optional[str] = "curl"
    fetch_content_fallback: Optional[str] = None
    fetch_impersonate: Optional[str] = "chrome"
    fetch_max_workers: int = 3
    fetch_retry_count: int = 2
    fetch_retry_backoff_seconds: float = 1.0
    fetch_curl_timeout_seconds: int = 6
    fetch_curl_hedge_delay_seconds: int = 3
    fetch_curl_max_response_bytes: int = 8_388_608
    fetch_curl_cffi_timeout_seconds: int = 6
    fetch_curl_cffi_hedge_delay_seconds: int = 3
    fetch_crawl4ai_timeout_seconds: int = 15
    fetch_crawl4ai_max_concurrency: int = 50  # Global page tasks, not browser processes
    fetch_worker_processes: int = 4
    fetch_worker_cancel_grace_seconds: float = 1.0
    crawl4ai_memory_threshold_percent: float = 85.0
    crawl4ai_memory_check_interval_seconds: float = 0.1
    crawl4ai_batch_wait_seconds: float = 0.01
    fetch_min_text_chars: int = 200
    fetch_quality_min_text_chars: int = 1000
    fetch_quality_min_text_lines: int = 6
    fetch_enable_readability_fallback: bool = True
    fetch_block_markers: List[str] = field(default_factory=list)
    crawl4ai_browser_type: str = "chromium"
    crawl4ai_headless: bool = True
    crawl4ai_wait_until: str = "domcontentloaded"
    crawl4ai_page_timeout_ms: int = 15000
    crawl4ai_word_count_threshold: int = 10
    crawl4ai_bypass_cache: bool = True
    crawl4ai_retry_count: int = 0
    crawl4ai_enable_stealth: bool = True
    crawl4ai_magic: bool = False


@dataclass
class ToolFeedbackOmissionConfig:
    """Opt-in omission of historical tool text at the next user admission."""

    enabled: bool = False
    threshold_chars: int = 10_000


@dataclass
class AgentConfig:
    """Agent 工具调用配置"""

    enabled: Optional[bool] = None
    tool_feedback_omission: ToolFeedbackOmissionConfig = field(
        default_factory=ToolFeedbackOmissionConfig
    )
    agent_mode: str = "tool_calling"  # 仅支持 tool_calling；旧值 code 会兼容映射
    entry_agent_name: str = "manager_agent"
    enabled_agent_profiles: List[str] = field(
        default_factory=lambda: [
            "coordinator_1",
            "coordinator_2",
            "manager_agent",
            "basic_agent",
        ]
    )
    max_agent_switches_per_turn: int = 2
    max_steps: Optional[int] = None
    session_ttl_minutes: Optional[int] = None
    exec_timeout: Optional[int] = 60
    work_dir: str = field(default_factory=lambda: str(PROJECT_ROOT))
    python_executable: str = "~/.agent/bin/python"
    command_mode: str = "restricted_shell"
    allowed_executables: List[str] = field(
        default_factory=lambda: list(DEFAULT_ALLOWED_EXECUTABLES)
    )
    max_exec_timeout_seconds: float = 3600.0
    max_output_bytes: int = 4_194_304
    termination_grace_seconds: float = 2.0
    allow_browser: bool = True
    allow_desktop_capture: bool = False
    allow_desktop_input: bool = False
    nvm_dir: str = "~/.nvm"
    environment_allowlist: List[str] = field(
        default_factory=lambda: list(DEFAULT_ENVIRONMENT_ALLOWLIST)
    )
    command_path_entries: List[str] = field(default_factory=list)
    command_cwd_allowlist: List[str] = field(default_factory=list)
    command_blacklist: Optional[List[dict]] = (
        None  # [{"pattern": "...", "reason": "..."}]
    )
    playwright_profile_dir: Optional[str] = None
    search_whitelist_paths: List[str] = field(default_factory=list)
    file_operation_roots: Optional[List[str]] = None
    image_input_roots: Optional[List[str]] = None
    session_temp_base_dir: str = "/tmp/agent_images"
    max_file_bytes: Optional[int] = None
    allow_overwrite_existing: bool = True
    search_exclude_globs: List[str] = field(default_factory=list)
    glob_default_limit: int = 100
    glob_max_limit: int = 1000
    ripgrep_timeout_seconds: int = 15
    grep_default_head_limit: int = 200
    grep_max_head_limit: int = 1000
    grep_max_columns: int = 400
    coordinator_1_timeout_seconds: float = 10.0
    coordinator_1_routing_temperature: float = 0.0
    coordinator_1_routing_max_tokens: int = 150
    coordinator_1_routing_extra_body: dict[str, object] = field(
        default_factory=lambda: {
            "chat_template_kwargs": {"enable_thinking": False}
        }
    )
    manager_agent_max_parallel_children: int = 4
    manager_agent_child_event_buffer_limit: int = 5000
    manager_agent_child_timeout_seconds: float = 2400.0
    session_disposal_timeout_seconds: float = 30.0
    handoff_debug_log_enabled: bool = False
    handoff_debug_log_path: str = "/tmp/agent_handoff_debug.jsonl"
    # Future Agent sessions only.  A session freezes its resolved snapshot at
    # request acceptance; this YAML value is never reread for an existing ID.
    default_profile_tools: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            "manager_agent": (
                "read_file", "write_file", "edit_file", "glob", "grep",
                "exec_command", "list_skills", "read_skill", "web_fetch",
                "search_and_fetch",
            ),
            "basic_agent": (
                "read_file", "write_file", "edit_file", "glob", "grep",
                "exec_command", "check_process", "list_skills",
                "read_skill", "web_search", "web_fetch", "search_and_fetch",
            ),
        }
    )


@dataclass
class AgentPromptsConfig:
    """Agent 提示词配置（从 config-agent-prompts.yaml 加载）"""

    coordinator_1: dict = field(default_factory=dict)
    coordinator_2: dict = field(default_factory=dict)
    manager_agent: dict = field(default_factory=dict)
    basic_agent: dict = field(default_factory=dict)
    explorer_agent: dict = field(default_factory=dict)
    auditor_agent: dict = field(default_factory=dict)
    vlm_instructions: Optional[str] = None
    extra_profiles: dict[str, object] = field(default_factory=dict, repr=False)
    compaction: Optional["CompactionPromptsConfig"] = None
    business_tool_protocol: str = ""
    native_tool_protocol_prompt: str = ""
    legacy_prompt_compatibility: dict[str, object] = field(default_factory=dict, repr=False)
    business_tool_modules: dict[str, dict[str, object]] = field(default_factory=dict)
    tool_prompt_combinations: list[dict[str, object]] = field(default_factory=list)

    def __getattr__(self, name: str) -> object:
        """保持未来未知 profile 可通过 getattr 访问。"""
        try:
            extra_profiles = object.__getattribute__(self, "extra_profiles")
        except AttributeError as exc:
            raise AttributeError(name) from exc
        try:
            return extra_profiles[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    @property
    def agent_instructions(self) -> Optional[str]:
        """兼容当前单 Agent 运行时，从 basic_agent 读取 instructions。"""
        instructions = self.basic_agent.get("instructions")
        return instructions if isinstance(instructions, str) else None

    @property
    def toolcalling_agent(self) -> Optional[dict]:
        """兼容当前单 Agent 运行时，从 basic_agent 读取 prompt_templates。"""
        prompt_templates = self.basic_agent.get("prompt_templates")
        return prompt_templates if isinstance(prompt_templates, dict) else None


@dataclass
class CompactionPromptsConfig:
    """Compact 命令的提示词模板（从 config-agent-prompts.yaml: compaction 段加载）"""

    status_message: str = "Compressing conversation context..."
    no_tools_preamble: str = ""
    main_prompt: str = ""
    custom_instructions_header: str = ""
    no_tools_trailer: str = ""
    summary_envelope_preface: str = ""
    summary_envelope_suffix: str = ""
    compact_user_instruction: str = ""  # v2: appended as the trailing user message
    compact_control_user_instruction: str = ""


@dataclass
class CycleStepConfig:
    """Cycle 序列中的单个步骤配置"""

    step_type: str  # "wait" | "message" | "cycle"
    wait_seconds: float = 0.0
    message: str = ""
    photo: List[int] = field(default_factory=list)
    cycle_count: int = 0
    cycle_steps: List["CycleStepConfig"] = field(default_factory=list)


@dataclass
class ScheduledMessageConfig:
    """A configured top-level scheduled Cycle message template."""

    id: str
    time: str
    recurrence: str
    day_count: int = 1
    message: str = ""
    photo: List[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.id) is None:
            raise ValueError("cycle scheduled message id must match [A-Za-z0-9_-]{1,64}")
        if not isinstance(self.time, str) or re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d", self.time
        ) is None:
            raise ValueError("cycle scheduled message time must use HH:mm:ss")
        if self.recurrence not in {"once", "daily"}:
            raise ValueError("cycle scheduled message recurrence must be once or daily")
        if isinstance(self.day_count, bool) or not isinstance(self.day_count, int) or self.day_count <= 0:
            raise ValueError("cycle scheduled message day_count must be a positive integer")
        if self.recurrence == "once" and self.day_count != 1:
            raise ValueError("once cycle scheduled message requires day_count=1")


@dataclass
class CycleConfig:
    """Cycle（可编程序列）配置"""

    enabled: bool = False
    max_depth: int = 5
    max_duration: int = 3_600
    show_thumbnails: bool = True
    timeout_policy: str = "exempt_cycle_run"
    scheduled_track_max_duration_seconds: int = 2_592_000
    max_scheduled_message_days: int = 30
    event_journal_retention_seconds: int = 86_400
    event_journal_max_records: int = 20_000
    default_sequence: List[CycleStepConfig] = field(default_factory=list)
    default_scheduled_messages: List[ScheduledMessageConfig] = field(
        default_factory=list
    )

    def __post_init__(self) -> None:
        if self.timeout_policy not in {"exempt_cycle_run", "legacy_bounded"}:
            raise ValueError(
                "cycle.timeout_policy must be exempt_cycle_run or legacy_bounded"
            )
        for field_name in (
            "scheduled_track_max_duration_seconds",
            "max_scheduled_message_days",
            "event_journal_retention_seconds",
            "event_journal_max_records",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"cycle.{field_name} must be a positive integer")
        schedule_ids = [item.id for item in self.default_scheduled_messages]
        if len(schedule_ids) != len(set(schedule_ids)):
            raise ValueError("cycle.default_scheduled_messages ids must be unique")


@dataclass
class CompactConfig:
    """Compact (/compact) 命令的配置"""

    enabled: bool = True
    auto_trigger: bool = True
    strategy: str = "performance"  # "performance" | "utilization"
    performance_ratio: float = 0.5
    utilization_summary_reserve: int = 20000
    utilization_buffer: int = 13000
    max_summary_tokens: int = 3072
    temperature: float = 0.3
    top_p: float = 0.9
    extra_body: dict[str, Any] | None = field(
        default_factory=lambda: {"chat_template_kwargs": {"enable_thinking": False}}
    )
    source_token_budget: int = 6000
    cycle_min_steps_between_compacts: int = 0
    image_token_estimate_per_image: int = 512
    diagnostics_enabled: bool = True
    prompt_shape: str = "reliable_position1"
    prompt_control_enabled: bool = False
    reliable_fallback_enabled: bool = False
    validation_min_summary_chars: int = 200
    validation_min_summary_prompt_ratio: float = 0.002
    validation_require_summary_sections: bool = True


@dataclass
class CameraConfig:
    """摄像头配置"""

    # USB 摄像头参数
    usb_device_open_delay: float = 0.5
    usb_warmup_frames: int = 5

    # 内置摄像头参数
    builtin_device_open_delay: float = 1.0
    builtin_warmup_frames: int = 8

    # 通用参数
    max_capture_attempts: int = 3
    min_brightness: float = 10.0
    # 摄像头期望采集分辨率最大边长（硬件协商，超出实际上限时自动回退）
    capture_dimension: int = 1280


@dataclass(frozen=True)
class VisionAutomationConfig:
    """Browser-local vision automation limits and defaults."""

    enabled: bool = True
    interval_seconds: float = 1.0
    max_frames_per_source: int = 16
    thumbnail_size_px: int = 32
    capture_during_inference: bool = False
    lock_first_bundle: bool = False
    tick_timeout_seconds: float = 10.0
    consecutive_tick_failure_limit: int = 3
    max_encoded_payload_mib: int = 128
    initial_jpeg_quality: int = 85
    minimum_jpeg_quality: int = 40
    minimum_image_dimension_px: int = 256
    base64_encode_concurrency: int = 4
    max_request_body_mib: int = 136
    request_status_ttl_seconds: int = 600

    def __post_init__(self) -> None:
        for field_name in ("enabled", "capture_during_inference", "lock_first_bundle"):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"vision_automation.{field_name} must be a boolean")

        for field_name in ("interval_seconds", "tick_timeout_seconds"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"vision_automation.{field_name} must be numeric")
            if not math.isfinite(value):
                raise ValueError(f"vision_automation.{field_name} must be finite")

        integer_fields = (
            "max_frames_per_source",
            "thumbnail_size_px",
            "consecutive_tick_failure_limit",
            "max_encoded_payload_mib",
            "initial_jpeg_quality",
            "minimum_jpeg_quality",
            "minimum_image_dimension_px",
            "base64_encode_concurrency",
            "max_request_body_mib",
            "request_status_ttl_seconds",
        )
        for field_name in integer_fields:
            if isinstance(getattr(self, field_name), bool) or not isinstance(
                getattr(self, field_name), int
            ):
                raise ValueError(f"vision_automation.{field_name} must be an integer")

        bounds = (
            ("interval_seconds", self.interval_seconds >= 0.1, "at least 0.1"),
            (
                "max_frames_per_source",
                1 <= self.max_frames_per_source <= 250,
                "between 1 and 250",
            ),
            (
                "thumbnail_size_px",
                16 <= self.thumbnail_size_px <= 128,
                "between 16 and 128",
            ),
            ("tick_timeout_seconds", self.tick_timeout_seconds >= 1, "at least 1"),
            (
                "consecutive_tick_failure_limit",
                self.consecutive_tick_failure_limit >= 1,
                "at least 1",
            ),
            (
                "max_encoded_payload_mib",
                1 <= self.max_encoded_payload_mib <= 128,
                "between 1 and 128",
            ),
            (
                "initial_jpeg_quality",
                40 <= self.initial_jpeg_quality <= 95,
                "between 40 and 95",
            ),
            (
                "minimum_jpeg_quality",
                1 <= self.minimum_jpeg_quality <= self.initial_jpeg_quality,
                "between 1 and initial_jpeg_quality",
            ),
            (
                "minimum_image_dimension_px",
                self.minimum_image_dimension_px >= 64,
                "at least 64",
            ),
            (
                "base64_encode_concurrency",
                1 <= self.base64_encode_concurrency <= 8,
                "between 1 and 8",
            ),
            (
                "max_request_body_mib",
                self.max_encoded_payload_mib < self.max_request_body_mib <= 136,
                "greater than max_encoded_payload_mib and at most 136",
            ),
            (
                "request_status_ttl_seconds",
                self.request_status_ttl_seconds >= 1,
                "at least 1",
            ),
        )
        for field_name, valid, requirement in bounds:
            if not valid:
                raise ValueError(f"vision_automation.{field_name} must be {requirement}")

    @property
    def max_encoded_payload_bytes(self) -> int:
        return self.max_encoded_payload_mib * 1024 * 1024

    @property
    def max_request_body_bytes(self) -> int:
        return self.max_request_body_mib * 1024 * 1024


@dataclass
class SGLangConfig:
    """SGLang 配置"""

    external_model_package: Optional[str] = None
    flashinfer_prefill_uniform_q_len_compat: Optional[bool] = None
    host: Optional[str] = None
    port: Optional[int] = None
    reasoning_parser: Optional[str] = None
    chat_template: Optional[str] = None
    tool_call_parser: Optional[str] = None
    disable_cuda_graph: Optional[bool] = None
    cuda_graph_max_bs: Optional[int] = None
    fp8_gemm_backend: Optional[str] = None
    tensor_parallel_size: Optional[int] = None
    max_context_length: Optional[int] = None
    mem_fraction_static: Optional[float] = None
    kv_cache_dtype: Optional[str] = None
    max_total_tokens: Optional[int] = None
    max_running_requests: Optional[int] = None
    max_prefill_tokens: Optional[int] = None
    chunked_prefill_size: Optional[int] = None
    schedule_policy: Optional[str] = None
    enable_cache_report: Optional[bool] = None
    mamba_full_memory_ratio: Optional[float] = None
    mamba_ssm_dtype: Optional[str] = None
    max_mamba_cache_size: Optional[int] = None
    skip_server_warmup: Optional[bool] = None
    quantization: Optional[str] = None
    moe_runner_backend: Optional[str] = None
    trust_remote_code: Optional[bool] = None
    speculative_algorithm: Optional[str] = None
    speculative_num_steps: Optional[int] = None
    speculative_eagle_topk: Optional[int] = None
    speculative_num_draft_tokens: Optional[int] = None
    enable_linear_replayssm_spec: Optional[bool] = None
    speculative_draft_model_path: Optional[str] = None
    prefill_chunk_size: Optional[int] = None


@dataclass
class AppConfig:
    """
    应用程序总配置类

    整合所有子配置模块，提供统一的配置访问入口。
    """

    model: ModelConfig = field(default_factory=ModelConfig)
    inference: InferenceConfig = field(default_factory=InferenceConfig)
    kv_cache: KVCacheConfig = field(default_factory=KVCacheConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    frontend: FrontendConfig = field(default_factory=FrontendConfig)
    ocr: OCRServiceConfig = field(default_factory=OCRServiceConfig)
    read_file: ReadFileConfig = field(default_factory=ReadFileConfig)
    web_ui: WebUIConfig = field(default_factory=WebUIConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)
    platform: PlatformConfig = field(default_factory=PlatformConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    runtime_prompts: RuntimePromptsConfig = field(default_factory=RuntimePromptsConfig)
    container: ContainerConfig = field(default_factory=ContainerConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    agent_prompts: AgentPromptsConfig = field(default_factory=AgentPromptsConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    web_tools: WebToolsConfig = field(default_factory=WebToolsConfig)
    sglang: SGLangConfig = field(default_factory=SGLangConfig)
    vllm: VLLMConfig = field(default_factory=VLLMConfig)
    cycle: CycleConfig = field(default_factory=CycleConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    vision_upload: VisionUploadConfig = field(default_factory=VisionUploadConfig)
    vision_automation: VisionAutomationConfig = field(
        default_factory=VisionAutomationConfig
    )
    compact: CompactConfig = field(default_factory=CompactConfig)
    computer_use: ComputerUseConfig = field(default_factory=ComputerUseConfig)
    asr: Optional["AsrConfig"] = None
    local_memory: LocalMemoryConfig = field(default_factory=LocalMemoryConfig)


class ConfigLoader:
    """
    配置加载器

    负责从 config-overall.yaml 及其引用的子配置文件加载配置，
    转换为类型安全的配置对象。

    sub-file 路由由 config-overall.yaml 顶层键决定：
      infer / server / misc / agent_operation / webUI / vision → 对应子 YAML 路径
    （model 键已移除不再引用；旧 infer 缺 model 段时按 sibling 自动回退。）

    使用示例:
        loader = ConfigLoader()
        config = loader.load()
        print(config.model.model_path)
    """

    def __init__(self, config_path: Optional[Path] = None):
        """
        初始化配置加载器

        Args:
            config_path: 总配置文件路径，默认为 config/config-overall.yaml
        """
        self.config_path = config_path or CONFIG_PATH
        # 合并后的所有 YAML 章节数据（section -> dict）
        self._data: dict = {}
        self._config: Optional[AppConfig] = None

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _load_yaml_file(path: Path) -> dict:
        """加载单个 YAML 文件，返回 dict（空文件返回 {}）"""
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @staticmethod
    def _infer_sibling_model_path(infer_path: Path) -> Optional[Path]:
        """由 infer 路径推导旧 sibling model 路径（*-config-infer.yaml → *-config-model.yaml）。"""
        name = infer_path.name
        if "-config-infer.yaml" not in name:
            return None
        sibling_name = name.replace("-config-infer.yaml", "-config-model.yaml")
        return infer_path.parent / sibling_name

    def load(self, reload: bool = False) -> AppConfig:
        """
        加载配置文件

        Args:
            reload: 是否强制重新加载

        Returns:
            AppConfig: 应用配置对象

        Raises:
            FileNotFoundError: 配置文件不存在
        """
        if self._config is not None and not reload:
            return self._config

        if not self.config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {self.config_path}")

        # 1. 加载总配置，读取各子配置文件的相对路径
        overall = self._load_yaml_file(self.config_path)

        # 2. 依次加载并合并所有子配置章节
        # model 键仅为兼容旧 overall 而保留；新 overall 已不再引用任何 *-config-model.yaml。
        self._data = {}
        infer_sub_path = None
        for key in (
            "model",
            "infer",
            "server",
            "misc",
            "agent_operation",
            "webUI",
            "vision",
        ):
            rel_path = overall.get(key)
            if rel_path:
                sub_path = PROJECT_ROOT / rel_path
                if key == "infer":
                    infer_sub_path = sub_path
                if sub_path.exists():
                    sub_data = self._load_yaml_file(sub_path)
                    # model 段可能分散在 infer 内嵌段与旧 sibling 文件中，
                    # 浅 update 会整体覆盖；此处对 model 做深合并（后加载优先）。
                    if (
                        "model" in sub_data
                        and isinstance(sub_data.get("model"), dict)
                        and isinstance(self._data.get("model"), dict)
                    ):
                        merged_model = dict(self._data["model"])
                        merged_model.update(sub_data["model"] or {})
                        merged_rest = {
                            k: v for k, v in sub_data.items() if k != "model"
                        }
                        self._data["model"] = merged_model
                        self._data.update(merged_rest)
                    else:
                        self._data.update(sub_data)

        # 2.1 旧 infer 缺 model 段时的 sibling 回退：
        # infer 同目录下 *-config-infer.yaml → *-config-model.yaml。
        # 新 infer（qwen3.8）已内嵌 model 段时不覆盖，仅补齐缺失键。
        model_section = self._data.get("model")
        needs_sibling = not isinstance(model_section, dict) or not model_section.get(
            "model_path"
        )
        if needs_sibling and infer_sub_path is not None:
            sibling = self._infer_sibling_model_path(infer_sub_path)
            if sibling is not None and sibling.exists():
                try:
                    sibling_data = self._load_yaml_file(sibling)
                except Exception as exc:
                    logger.warning("旧 model sibling 加载失败 %s: %s", sibling, exc)
                    sibling_data = {}
                sibling_model = (sibling_data or {}).get("model")
                if isinstance(sibling_model, dict) and sibling_model:
                    if not isinstance(self._data.get("model"), dict):
                        self._data["model"] = {}
                    for k, v in sibling_model.items():
                        if k not in self._data["model"] or self._data["model"][k] is None:
                            self._data["model"][k] = v
                    logger.warning(
                        "infer 文件缺少内嵌 model 段，已回退加载 sibling %s；"
                        "请将 model 信息合并进 infer 文件",
                        sibling,
                    )

        # 2.5 加载 agent_prompts 配置（独立文件，不合并到 _data）
        self._agent_prompts_data: dict = {}
        agent_prompts_rel = overall.get("agent_prompts")
        if agent_prompts_rel:
            agent_prompts_path = PROJECT_ROOT / agent_prompts_rel
            if agent_prompts_path.exists():
                self._agent_prompts_data = self._load_yaml_file(agent_prompts_path)

        # ASR uses a standalone strict schema. Do not merge its generic
        # audio/runtime/sglang section names into the main config namespace.
        asr_config = None
        asr_rel = overall.get("asr")
        if asr_rel:
            from backend.asr.config import load_asr_config

            asr_config = load_asr_config(
                PROJECT_ROOT / asr_rel,
                project_root=PROJECT_ROOT,
            )

        local_memory_config = LocalMemoryConfig().validate(PROJECT_ROOT)
        local_memory_rel = overall.get("local_memory")
        if local_memory_rel:
            local_memory_config = load_local_memory_config(PROJECT_ROOT / local_memory_rel, PROJECT_ROOT)

        # 3. 解析各模块配置（顺序敏感：sglang/vllm 先解析供 model fallback 使用；
        # vision_upload 先解析供 model 图片字段回退使用；此处为保持最小改动，
        # model 内部自行按 section fallback 读取，不依赖先解析的对象）。
        self._config = AppConfig(
            model=self._parse_model_config(),
            inference=self._parse_inference_config(),
            kv_cache=self._parse_kv_cache_config(),
            server=self._parse_server_config(),
            frontend=self._parse_frontend_config(),
            ocr=self._parse_ocr_config(),
            read_file=self._parse_read_file_config(),
            web_ui=self._parse_web_ui_config(),
            debug=self._parse_debug_config(),
            platform=self._parse_platform_config(),
            logging=self._parse_logging_config(),
            runtime_prompts=self._parse_runtime_prompts_config(),
            container=self._parse_container_config(),
            agent=self._parse_agent_config(),
            agent_prompts=self._parse_agent_prompts_config(),
            mcp=self._parse_mcp_config(),
            web_tools=self._parse_web_tools_config(),
            sglang=self._parse_sglang_config(),
            vllm=self._parse_vllm_config(),
            cycle=self._parse_cycle_config(),
            camera=self._parse_camera_config(),
            vision_upload=self._parse_vision_upload_config(),
            vision_automation=self._parse_vision_automation_config(),
            compact=self._parse_compact_config(),
            computer_use=self._parse_computer_use_config(),
            asr=asr_config,
            local_memory=local_memory_config,
        )

        return self._config

    def _get(self, section: str, key: str, default=None):
        """
        安全获取 YAML 配置值。

        - 整节或键不存在  → 返回 default
        - 键存在但值为 null → 返回 None（明确不赋值，与 INI 空值语义一致）
        - 键存在且有实际值 → 返回该值
        """
        sec = self._data.get(section)
        if not isinstance(sec, dict):
            return default
        if key not in sec:
            return default
        return sec[key]  # YAML null → Python None

    def _get_bool(
        self, section: str, key: str, default: Optional[bool] = None
    ) -> Optional[bool]:
        """获取布尔配置值（YAML null → None）"""
        val = self._get(section, key, default)
        if val is None:
            return None
        if isinstance(val, bool):
            return val
        return str(val).lower() in ("true", "1", "yes")

    def _get_int(
        self, section: str, key: str, default: Optional[int] = None
    ) -> Optional[int]:
        """获取整数配置值（YAML null → None）"""
        val = self._get(section, key, default)
        if val is None:
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def _get_float(
        self, section: str, key: str, default: Optional[float] = None
    ) -> Optional[float]:
        """获取浮点数配置值（YAML null → None）"""
        val = self._get(section, key, default)
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _coerce_int(value, default: int) -> int:
        """Best-effort int coercion for nested config values."""
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _coerce_bool(value, default: bool) -> bool:
        """Best-effort bool coercion for nested config values."""
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off"):
            return False
        return default

    @staticmethod
    def _coerce_float(value, default: float) -> float:
        """Best-effort float coercion for nested config values."""
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _coerce_fetch_positive_count(value, default: int) -> int:
        """Reject invalid worker/page counts without truncating fractional values."""
        if value is None or isinstance(value, bool):
            return default
        try:
            parsed = int(value)
            if parsed <= 0 or float(value) != parsed:
                return default
        except (TypeError, ValueError, OverflowError):
            return default
        return parsed

    @staticmethod
    def _coerce_fetch_nonnegative_count(value, default: int) -> int:
        """Validate non-negative integer quality thresholds."""
        if value is None or isinstance(value, bool):
            return default
        try:
            parsed = int(value)
            if parsed < 0 or float(value) != parsed:
                return default
        except (TypeError, ValueError, OverflowError):
            return default
        return parsed

    @staticmethod
    def _coerce_fetch_float(value, default: float, *, allow_zero=False, upper=None) -> float:
        """Validate finite time intervals and memory percentages at the config boundary."""
        if value is None or isinstance(value, bool):
            return default
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        if not math.isfinite(parsed) or parsed < 0 or (parsed == 0 and not allow_zero):
            return default
        if upper is not None and parsed >= upper:
            return default
        return parsed

    @staticmethod
    def _coerce_str_list(value, default: Optional[List[str]] = None) -> List[str]:
        """Best-effort string list coercion for nested config values."""
        fallback = list(default or [])
        if value is None:
            return fallback
        if isinstance(value, (list, tuple, set)):
            items = value
        else:
            items = str(value).split(",")
        return [str(item).strip() for item in items if str(item).strip()]

    @staticmethod
    def _coerce_str(value, default: str) -> str:
        """Best-effort string coercion for nested config values."""
        if value is None:
            return default
        normalized = str(value).strip()
        return normalized or default

    def _get_str_list(
        self, section: str, key: str, default: Optional[List[str]] = None
    ) -> List[str]:
        """获取字符串列表配置值。"""
        fallback = list(default or [])
        val = self._get(section, key, fallback)
        if val is None:
            return fallback
        if isinstance(val, (list, tuple, set)):
            items = val
        else:
            items = str(val).split(",")
        return [str(item).strip() for item in items if str(item).strip()]

    @staticmethod
    def _validate_agent_config(agent_config: AgentConfig) -> AgentConfig:
        """校验多 Agent 运行时配置的基本约束。"""
        if not agent_config.enabled_agent_profiles:
            raise ValueError("agent.enabled_agent_profiles must not be empty")
        if agent_config.entry_agent_name not in agent_config.enabled_agent_profiles:
            raise ValueError(
                "agent.entry_agent_name "
                f"'{agent_config.entry_agent_name}' must be included in "
                "agent.enabled_agent_profiles"
            )
        if agent_config.entry_agent_name not in ENTRY_AGENT_NAMES:
            allowed_profiles = ", ".join(sorted(ENTRY_AGENT_NAMES))
            raise ValueError(
                "agent.entry_agent_name "
                f"'{agent_config.entry_agent_name}' must be one of the supported entry "
                f"profiles: {allowed_profiles}"
            )
        if (
            not math.isfinite(agent_config.session_disposal_timeout_seconds)
            or agent_config.session_disposal_timeout_seconds <= 0
        ):
            raise ValueError(
                "agent.session_disposal_timeout_seconds must be finite and positive"
            )
        CommandPolicy(
            command_mode=agent_config.command_mode,
            allowed_executables=agent_config.allowed_executables,
            command_cwd_allowlist=agent_config.command_cwd_allowlist,
            custom_blacklist=agent_config.command_blacklist,
            max_exec_timeout_seconds=agent_config.max_exec_timeout_seconds,
            max_output_bytes=agent_config.max_output_bytes,
        )
        if (
            isinstance(agent_config.exec_timeout, bool)
            or not isinstance(agent_config.exec_timeout, int)
            or agent_config.exec_timeout <= 0
            or agent_config.exec_timeout > agent_config.max_exec_timeout_seconds
        ):
            raise ValueError(
                "agent.exec_timeout must be a positive integer no greater than "
                "agent.max_exec_timeout_seconds"
            )
        CommandRuntimeConfig.from_agent_config(agent_config)
        return agent_config

    def _parse_model_config(self) -> ModelConfig:
        """解析模型配置。

        新布局：
          model.model_path / model_name / model_type 来自 infer 内嵌 model 段；
          tensor_parallel_size / max_context_length 去重后仅保留引擎侧，
          此处按 model → sglang → vllm 顺序 fallback；
          图片 5 项优先读 vision_upload 段，缺失时回退读 model 段（兼容旧文件）。
        """
        tensor_parallel_size = self._get_int("model", "tensor_parallel_size")
        if tensor_parallel_size is None:
            tensor_parallel_size = self._get_int("sglang", "tensor_parallel_size")
        if tensor_parallel_size is None:
            tensor_parallel_size = self._get_int("vllm", "tensor_parallel_size")

        max_context_length = self._get_int("model", "max_context_length")
        if max_context_length is None:
            max_context_length = self._get_int("sglang", "max_context_length")
        if max_context_length is None:
            max_context_length = self._get_int("vllm", "max_model_len")

        # vision_upload 优先，model 回退（旧 *-config-model.yaml 兼容）
        vision_upload_raw = self._data.get("vision_upload")
        vision_upload = (
            vision_upload_raw if isinstance(vision_upload_raw, dict) else {}
        )

        def _pick_vision_int(key: str, default: int) -> Optional[int]:
            raw = vision_upload.get(key)
            if raw is not None:
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    pass
            return self._get_int("model", key, default)

        def _pick_vision_str(key: str, default: str) -> Optional[str]:
            raw = vision_upload.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw
            if raw is not None and not isinstance(raw, str):
                return str(raw)
            return self._get("model", key, default)

        return ModelConfig(
            model_path=self._get("model", "model_path"),
            model_name=self._get("model", "model_name"),
            tensor_parallel_size=tensor_parallel_size,
            max_context_length=max_context_length,
            model_type=self._get("model", "model_type", "text"),
            max_image_size=_pick_vision_int("max_image_size", 10485760),
            supported_formats=_pick_vision_str(
                "supported_formats", "png,jpg,jpeg,gif,webp"
            ),
            max_image_tokens=_pick_vision_int("max_image_tokens", 8192),
            max_image_dimension=_pick_vision_int("max_image_dimension", 1280),
            camera_max_dimension=_pick_vision_int("camera_max_dimension", 1024),
        )

    def _parse_vision_upload_config(self) -> VisionUploadConfig:
        """解析 vision_upload 配置（缺失时回退读 model 段以兼容旧文件）。"""
        return VisionUploadConfig(
            max_image_size=self._get_int("vision_upload", "max_image_size")
            if self._get_int("vision_upload", "max_image_size") is not None
            else self._get_int("model", "max_image_size", 10485760),
            supported_formats=(
                self._get("vision_upload", "supported_formats")
                or self._get("model", "supported_formats", "png,jpg,jpeg,gif,webp")
            ),
            max_image_tokens=self._get_int("vision_upload", "max_image_tokens")
            if self._get_int("vision_upload", "max_image_tokens") is not None
            else self._get_int("model", "max_image_tokens", 8192),
            max_image_dimension=self._get_int("vision_upload", "max_image_dimension")
            if self._get_int("vision_upload", "max_image_dimension") is not None
            else self._get_int("model", "max_image_dimension", 1280),
            camera_max_dimension=self._get_int("vision_upload", "camera_max_dimension")
            if self._get_int("vision_upload", "camera_max_dimension") is not None
            else self._get_int("model", "camera_max_dimension", 1024),
        )

    def _parse_inference_config(self) -> InferenceConfig:
        """解析推理配置"""
        max_tokens = self._get_int("inference", "max_tokens", 4096)
        tool_protocol = self._get("inference", "tool_protocol", "legacy_text")
        if tool_protocol not in {"legacy_text", "qwen35-native"}:
            raise ValueError("inference.tool_protocol must be legacy_text or qwen35-native")
        capture_raw_tool_calls = self._get("inference", "capture_raw_tool_calls", False)
        if not isinstance(capture_raw_tool_calls, bool):
            raise ValueError("inference.capture_raw_tool_calls must be a boolean")
        return InferenceConfig(
            tool_protocol=tool_protocol,
            capture_raw_tool_calls=capture_raw_tool_calls,
            temperature=self._get_float("inference", "temperature", 0.1),
            top_p=self._get_float("inference", "top_p", 0.95),
            top_k=self._get_int("inference", "top_k", 0),
            max_tokens=max_tokens,
            max_new_tokens=max_tokens,  # 兼容 SGLang API
            repetition_penalty=self._get_float("inference", "repetition_penalty", 0.9),
            stream=self._get_bool("inference", "stream", True),
            timeout=self._get_int("inference", "timeout", 5400),
            stream_usage_stats=self._get_bool("inference", "stream_usage_stats", True),
        )

    def _parse_kv_cache_config(self) -> KVCacheConfig:
        """解析 KV Cache 配置（两字段均废弃，YAML null → None）"""
        return KVCacheConfig(
            enable_block_reuse=self._get_bool("kv_cache", "enable_block_reuse"),
            max_cache_size_mb=self._get_int("kv_cache", "max_cache_size_mb"),
        )

    def _parse_server_config(self) -> ServerConfig:
        """解析服务器配置"""
        return ServerConfig(
            host=self._get("server", "host", "0.0.0.0"),
            port=self._get_int("server", "port", 8090),
            workers=self._get_int("server", "workers", 1),
            api_key=self._get("server", "api_key"),  # null → None（不启用认证）
            cors_origins=self._get("server", "cors_origins", "*"),
            session_hub_heartbeat_seconds=self._get_float(
                "server", "session_hub_heartbeat_seconds", 15.0
            ),
        )

    def _parse_frontend_config(self) -> FrontendConfig:
        """解析前端配置"""
        return FrontendConfig(
            dev_port=self._get_int("frontend", "dev_port", 3000),
            api_base_url=self._get("frontend", "api_base_url", "http://localhost:8090"),
            chat_stream_idle_timeout_seconds=self._get_int(
                "frontend", "chat_stream_idle_timeout_seconds", 0
            ),
            agent_stream_idle_timeout_seconds=self._get_int(
                "frontend", "agent_stream_idle_timeout_seconds", 0
            ),
            cycle_stream_idle_timeout_seconds=self._get_int(
                "frontend", "cycle_stream_idle_timeout_seconds", 0
            ),
        )

    def _parse_ocr_config(self) -> OCRServiceConfig:
        raw = self._data.get("ocr") or {}
        host = self._coerce_str(raw.get("host"), "127.0.0.1")
        port = self._coerce_int(raw.get("port"), 8118)
        return OCRServiceConfig(
            enabled=self._coerce_bool(raw.get("enabled"), True),
            host=host,
            port=port,
            base_url=self._coerce_str(raw.get("base_url"), f"http://{host}:{port}"),
            connect_timeout_seconds=self._coerce_float(
                raw.get("connect_timeout_seconds"), 2.0
            ),
            read_timeout_seconds=self._coerce_float(
                raw.get("read_timeout_seconds"), 600.0
            ),
            request_timeout_seconds=self._coerce_float(
                raw.get("request_timeout_seconds"), 600.0
            ),
            pipeline_config_path=self._coerce_str(
                raw.get("pipeline_config_path"), "config/paddleocr-vl-thor.yaml"
            ),
            sglang_pipeline_config_path=self._coerce_str(
                raw.get("sglang_pipeline_config_path"),
                "config/paddleocr-vl-thor-sglang.yaml",
            ),
            model_manifest_path=self._coerce_str(
                raw.get("model_manifest_path"),
                "config/paddleocr-vl-thor-model-manifest.json",
            ),
            attestation_path=self._coerce_str(
                raw.get("attestation_path"),
                "logs/paddleocr-runtime-attestation.json",
            ),
            attestation_max_age_seconds=self._coerce_int(
                raw.get("attestation_max_age_seconds"), 86400
            ),
            cuda_memory_limit_gb=self._coerce_int(
                raw.get("cuda_memory_limit_gb"), 16
            ),
        )

    def _parse_read_file_config(self) -> ReadFileConfig:
        raw = self._data.get("read_file") or {}
        defaults = ReadFileConfig(
            allowed_roots=[str(PROJECT_ROOT), "~", "/tmp", "/var/tmp"]
        )
        values: dict[str, Any] = {}
        float_fields = {"max_archive_compression_ratio"}
        bool_fields = {"normal_request_use_doc_unwarping"}
        string_fields = {"cache_path"}
        for name in defaults.__dataclass_fields__:
            default = getattr(defaults, name)
            value = raw.get(name)
            if name == "allowed_roots":
                values[name] = self._coerce_str_list(value, default)
            elif name in float_fields:
                values[name] = self._coerce_float(value, default)
            elif name in bool_fields:
                values[name] = self._coerce_bool(value, default)
            elif name in string_fields:
                values[name] = self._coerce_str(value, default)
            elif name == "ocr_max_new_tokens" and name in raw:
                values[name] = value
            else:
                values[name] = self._coerce_int(value, default)
        return ReadFileConfig(**values)

    def _parse_debug_config(self) -> DebugConfig:
        """解析调试配置"""
        return DebugConfig(
            enabled=self._get_bool("debug", "enabled", False),
            debug_model_loading=self._get_bool("debug", "debug_model_loading", False),
            debug_inference=self._get_bool("debug", "debug_inference", False),
            debug_api=self._get_bool("debug", "debug_api", False),
            debug_memory=self._get_bool("debug", "debug_memory", False),
            debug_config=self._get_bool("debug", "debug_config", False),
            debug_frontend=self._get_bool("debug", "debug_frontend", False),
        )

    def _parse_platform_config(self) -> PlatformConfig:
        """解析平台配置（YAML 嵌套结构：platform.jetson_thor / platform.desktop）"""
        platform = self._data.get("platform") or {}
        jetson = platform.get("jetson_thor") or {}
        desktop = platform.get("desktop") or {}

        # gpu_devices 可为整数、逗号分隔字符串或列表
        gpu_raw = desktop.get("gpu_devices", 0)
        if isinstance(gpu_raw, list):
            gpu_devices = [int(x) for x in gpu_raw]
        elif isinstance(gpu_raw, int):
            gpu_devices = [gpu_raw]
        else:
            gpu_str = str(gpu_raw) if gpu_raw is not None else "0"
            gpu_devices = [int(x.strip()) for x in gpu_str.split(",") if x.strip()]

        return PlatformConfig(
            current=platform.get("current", "desktop"),
            use_dla=jetson.get("use_dla", False),
            power_mode=jetson.get("power_mode", "MAXN"),
            memory_optimize=jetson.get("memory_optimize", True),
            multi_gpu=desktop.get("multi_gpu", False),
            gpu_devices=gpu_devices,
        )

    def _parse_logging_config(self) -> LoggingConfig:
        """解析日志配置"""
        return LoggingConfig(
            level=self._get("logging", "level", "INFO"),
            file_path=self._get("logging", "file_path"),  # null → None（仅控制台输出）
            format=self._get(
                "logging",
                "format",
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            ),
            colorize=self._get_bool("logging", "colorize", True),
        )

    def _parse_runtime_prompts_config(self) -> RuntimePromptsConfig:
        """解析运行时提示配置"""
        return RuntimePromptsConfig(
            enable_runtime_time_context=self._get_bool(
                "runtime_prompts", "enable_runtime_time_context", False
            )
            or False,
        )

    def _parse_web_ui_config(self) -> WebUIConfig:
        """Parse web UI skin configuration."""
        web_ui_raw = self._data.get("webUI")
        web_ui = web_ui_raw if isinstance(web_ui_raw, dict) else {}
        skin = web_ui.get("skin") or {}
        content = web_ui.get("content") or {}
        layout = web_ui.get("layout") or {}
        components_raw = web_ui.get("components")
        components = components_raw if isinstance(components_raw, dict) else {}
        input_bar = components.get("input_bar") or {}
        buttons = components.get("action_buttons") or {}
        mermaid = components.get("mermaid") or {}
        page_header = components.get("page_header") or {}
        message_avatar = components.get("message_avatar") or {}
        vision_welcome = components.get("vision_welcome") or {}

        default_config = WebUIConfig()
        parsed_content = WebUIContentConfig(
            bubble_character_limit=content.get(
                "bubble_character_limit",
                default_config.content.bubble_character_limit,
            )
        )
        skin_id = self._coerce_str(skin.get("id"), "default-v1")
        skin_name = WEB_UI_SKIN_NAMES.get(skin_id, skin_id)
        input_defaults = default_config.input_bar
        parsed_input_bar = {
            "min_height_px": self._coerce_int(input_bar.get("min_height_px"), input_defaults["min_height_px"]),
            "max_rows": self._coerce_int(input_bar.get("max_rows"), input_defaults["max_rows"]),
            "line_height_px": self._coerce_int(input_bar.get("line_height_px"), input_defaults["line_height_px"]),
            "vertical_padding_px": self._coerce_int(input_bar.get("vertical_padding_px"), input_defaults["vertical_padding_px"]),
            "border_radius_px": self._coerce_int(input_bar.get("border_radius_px"), input_defaults["border_radius_px"]),
        }

        defaults = default_config.action_buttons
        action_buttons: dict[str, dict[str, int]] = {}
        for name, fallback in defaults.items():
            button = buttons.get(name) or {}
            action_buttons[name] = {
                "width_px": self._coerce_int(button.get("width_px"), fallback["width_px"]),
                "height_px": self._coerce_int(button.get("height_px"), fallback["height_px"]),
            }

        mermaid_defaults = default_config.mermaid
        parsed_mermaid = {
            "min_height_px": self._coerce_int(mermaid.get("min_height_px"), mermaid_defaults["min_height_px"]),
        }

        page_header_defaults = default_config.page_header
        parsed_page_header = {
            "display": self._coerce_bool(page_header.get("display"), page_header_defaults["display"]),
            "text_title": self._coerce_str(page_header.get("text_title"), page_header_defaults["text_title"]),
            "vision_title": self._coerce_str(page_header.get("vision_title"), page_header_defaults["vision_title"]),
            "subtitle": self._coerce_str(page_header.get("subtitle"), page_header_defaults["subtitle"]),
        }

        message_avatar_defaults = default_config.message_avatar
        parsed_message_avatar = {
            "user_image_path": self._coerce_str(
                message_avatar.get("user_image_path"),
                message_avatar_defaults["user_image_path"],
            ),
            "assistant_image_path": self._coerce_str(
                message_avatar.get("assistant_image_path"),
                message_avatar_defaults["assistant_image_path"],
            ),
            "size_px": max(
                24,
                min(96, self._coerce_int(message_avatar.get("size_px"), int(message_avatar_defaults["size_px"]))),
            ),
        }

        vision_welcome_defaults = default_config.vision_welcome
        parsed_vision_welcome = {
            "title": self._coerce_str(vision_welcome.get("title"), vision_welcome_defaults["title"]),
            "description": self._coerce_str(
                vision_welcome.get("description"),
                vision_welcome_defaults["description"],
            ),
            "icon_image_path": self._coerce_str(
                vision_welcome.get("icon_image_path"),
                vision_welcome_defaults["icon_image_path"],
            ),
        }

        effects = web_ui.get("effects") or {}
        aurora_glow = effects.get("aurora_glow") or effects.get("glass_jelly") or {}
        effect_defaults = default_config.effects["aurora_glow"]
        parsed_effects = {
            "aurora_glow": {
                "enabled": self._coerce_bool(aurora_glow.get("enabled"), effect_defaults["enabled"]),
                "intensity": self._coerce_float(aurora_glow.get("intensity"), effect_defaults["intensity"]),
                "blur_px": self._coerce_int(aurora_glow.get("blur_px"), effect_defaults["blur_px"]),
                "chromatic_aberration": self._coerce_float(
                    aurora_glow.get("chromatic_aberration"),
                    effect_defaults["chromatic_aberration"],
                ),
                "refraction_strength": self._coerce_float(
                    aurora_glow.get("refraction_strength"),
                    effect_defaults["refraction_strength"],
                ),
                "tint_a": self._coerce_str(aurora_glow.get("tint_a"), effect_defaults["tint_a"]),
                "tint_b": self._coerce_str(aurora_glow.get("tint_b"), effect_defaults["tint_b"]),
                "opacity": self._coerce_float(aurora_glow.get("opacity"), effect_defaults["opacity"]),
                "animated": self._coerce_bool(aurora_glow.get("animated"), effect_defaults["animated"]),
                "max_dpr": self._coerce_float(aurora_glow.get("max_dpr"), effect_defaults["max_dpr"]),
            }
        }

        agent_steps_raw = components.get("agent_steps") or {}
        parsed_agent_steps = {
            "fold_after_final_answer": self._coerce_bool(
                agent_steps_raw.get("fold_after_final_answer"),
                default_config.agent_steps["fold_after_final_answer"],
            )
        }

        message_templates_config = components.get("message_templates")
        message_templates_raw = (
            message_templates_config.get("items")
            if isinstance(message_templates_config, dict)
            else None
        )
        parsed_message_templates = (
            [
                item
                for item in message_templates_raw
                if isinstance(item, str) and item.strip()
            ]
            if isinstance(message_templates_raw, list)
            else []
        )

        layout_defaults = default_config.layout
        header_background_alpha = max(
            0.0,
            min(
                1.0,
                self._coerce_float(
                    layout.get("header_background_alpha"),
                    layout_defaults["header_background_alpha"],
                ),
            ),
        )
        footer_background_alpha = max(
            0.0,
            min(
                1.0,
                self._coerce_float(
                    layout.get("footer_background_alpha"),
                    layout_defaults["footer_background_alpha"],
                ),
            ),
        )
        parsed_layout = {
            "header_background_alpha": header_background_alpha,
            "footer_background_alpha": footer_background_alpha,
        }

        return WebUIConfig(
            skin_id=skin_id,
            skin_name=skin_name,
            content=parsed_content,
            layout=parsed_layout,
            input_bar=parsed_input_bar,
            action_buttons=action_buttons,
            mermaid=parsed_mermaid,
            page_header=parsed_page_header,
            message_avatar=parsed_message_avatar,
            vision_welcome=parsed_vision_welcome,
            effects=parsed_effects,
            agent_steps=parsed_agent_steps,
            message_templates=parsed_message_templates,
        )

    def _parse_misc_config(self) -> RuntimePromptsConfig:
        """兼容旧名称的运行时提示配置解析入口。"""
        return self._parse_runtime_prompts_config()

    def _parse_container_config(self) -> ContainerConfig:
        """解析容器配置"""
        return ContainerConfig(
            enabled=self._get_bool("container", "enabled", False),
            api_url=self._get("container", "api_url", "http://localhost:8000/v1"),
            model_name=self._get("container", "model_name"),  # null → None
            timeout=self._get_int("container", "timeout", 300),
            max_retries=self._get_int("container", "max_retries", 3),
        )

    def _parse_agent_config(self) -> AgentConfig:
        """解析 Agent 配置"""
        default_search_whitelist_paths = [str(PROJECT_ROOT), "/tmp"]
        default_search_exclude_globs = [
            ".git",
            "__pycache__",
            "node_modules",
            ".next",
            ".sglang",
            ".worktrees",
        ]
        raw_agent_mode = self._get("agent", "agent_mode", "tool_calling")
        raw_default_profile_tools = self._get("agent", "default_profile_tools", None)
        if raw_default_profile_tools is None:
            raw_default_profile_tools = {
                "manager_agent": [
                    "read_file", "write_file", "edit_file", "glob", "grep",
                    "exec_command", "list_skills", "read_skill", "web_fetch",
                    "search_and_fetch",
                ],
                "basic_agent": [
                    "read_file", "write_file", "edit_file", "glob", "grep",
                    "exec_command", "check_process", "list_skills",
                    "read_skill", "web_search", "web_fetch", "search_and_fetch",
                ],
            }
        if not isinstance(raw_default_profile_tools, dict):
            raise ValueError("agent.default_profile_tools must be a mapping")
        if set(raw_default_profile_tools) != {"manager_agent", "basic_agent"}:
            raise ValueError("agent.default_profile_tools only supports manager_agent and basic_agent")
        default_profile_tools: dict[str, tuple[str, ...]] = {}
        for profile_name in ("manager_agent", "basic_agent"):
            raw_tools = raw_default_profile_tools[profile_name]
            if not isinstance(raw_tools, (list, tuple)):
                raise ValueError(f"agent.default_profile_tools.{profile_name} must be a list")
            if any(not isinstance(tool_name, str) or not tool_name for tool_name in raw_tools):
                raise ValueError(f"agent.default_profile_tools.{profile_name} must contain strings")
            if len(raw_tools) != len(set(raw_tools)):
                raise ValueError(f"agent.default_profile_tools.{profile_name} contains duplicates")
            invalid_tools = [
                tool_name
                for tool_name in raw_tools
                if tool_name not in BUSINESS_TOOL_NAMES
            ]
            if invalid_tools:
                special = [
                    tool_name
                    for tool_name in invalid_tools
                    if tool_name in SPECIAL_TOOL_NAMES
                ]
                category = "special" if special else "unknown or unavailable"
                raise ValueError(
                    f"agent.default_profile_tools.{profile_name} contains {category} tools: {invalid_tools}"
                )
            default_profile_tools[profile_name] = tuple(raw_tools)
        raw_exec_timeout = self._get("agent", "exec_timeout", 60)
        if (
            isinstance(raw_exec_timeout, bool)
            or not isinstance(raw_exec_timeout, int)
            or raw_exec_timeout <= 0
        ):
            raise ValueError("agent.exec_timeout must be a positive integer")
        raw_max_exec_timeout = self._get(
            "agent", "max_exec_timeout_seconds", 3600.0
        )
        if isinstance(raw_max_exec_timeout, bool):
            raise ValueError(
                "agent.max_exec_timeout_seconds must be finite and positive"
            )
        try:
            max_exec_timeout_seconds = float(raw_max_exec_timeout)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(
                "agent.max_exec_timeout_seconds must be finite and positive"
            ) from None
        raw_max_output_bytes = self._get("agent", "max_output_bytes", 4_194_304)
        if (
            isinstance(raw_max_output_bytes, bool)
            or not isinstance(raw_max_output_bytes, int)
            or raw_max_output_bytes <= 0
        ):
            raise ValueError("agent.max_output_bytes must be a positive integer")
        raw_termination_grace = self._get(
            "agent", "termination_grace_seconds", 2.0
        )
        if isinstance(raw_termination_grace, bool):
            raise ValueError(
                "agent.termination_grace_seconds must be finite and non-negative"
            )
        try:
            termination_grace_seconds = float(raw_termination_grace)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(
                "agent.termination_grace_seconds must be finite and non-negative"
            ) from None
        raw_allow_browser = self._get("agent", "allow_browser", True)
        if not isinstance(raw_allow_browser, bool):
            raise ValueError("agent.allow_browser must be a boolean")
        raw_allow_desktop_capture = self._get(
            "agent", "allow_desktop_capture", False
        )
        if not isinstance(raw_allow_desktop_capture, bool):
            raise ValueError("agent.allow_desktop_capture must be a boolean")
        raw_allow_desktop_input = self._get(
            "agent", "allow_desktop_input", False
        )
        if not isinstance(raw_allow_desktop_input, bool):
            raise ValueError("agent.allow_desktop_input must be a boolean")
        if raw_allow_desktop_input and not raw_allow_desktop_capture:
            raise ValueError(
                "agent.allow_desktop_input requires agent.allow_desktop_capture"
            )
        raw_environment_allowlist = self._get(
            "agent",
            "environment_allowlist",
            list(DEFAULT_ENVIRONMENT_ALLOWLIST),
        )
        if not isinstance(raw_environment_allowlist, (list, tuple)):
            raise ValueError("agent.environment_allowlist must be a list")
        environment_allowlist = list(raw_environment_allowlist)
        raw_command_path_entries = self._get("agent", "command_path_entries", [])
        if not isinstance(raw_command_path_entries, (list, tuple)):
            raise ValueError("agent.command_path_entries must be a list")
        command_path_entries = list(raw_command_path_entries)
        raw_command_cwd_allowlist = self._get("agent", "command_cwd_allowlist", [])
        if not isinstance(raw_command_cwd_allowlist, (list, tuple)):
            raise ValueError("agent.command_cwd_allowlist must be a list")
        command_cwd_allowlist = [
            os.path.expanduser(root) if isinstance(root, str) else root
            for root in raw_command_cwd_allowlist
        ]
        raw_allowed_executables = self._get(
            "agent", "allowed_executables", list(DEFAULT_ALLOWED_EXECUTABLES)
        )
        if not isinstance(raw_allowed_executables, (list, tuple)):
            raise ValueError("agent.allowed_executables must be a non-empty list")
        allowed_executables = resolve_skill_executables(
            raw_allowed_executables, PROJECT_ROOT / "backend" / "skills"
        )
        raw_file_operation_roots = self._get("agent", "file_operation_roots")
        if raw_file_operation_roots is not None and not isinstance(
            raw_file_operation_roots, (list, tuple)
        ):
            raise ValueError("agent.file_operation_roots must be a list or null")
        file_operation_roots = (
            None if raw_file_operation_roots is None else list(raw_file_operation_roots)
        )
        raw_image_input_roots = self._get("agent", "image_input_roots")
        if raw_image_input_roots is not None and not isinstance(
            raw_image_input_roots, (list, tuple)
        ):
            raise ValueError("agent.image_input_roots must be a list or null")
        image_input_roots = (
            None if raw_image_input_roots is None else list(raw_image_input_roots)
        )
        session_temp_base_dir = self._get(
            "agent", "session_temp_base_dir", "/tmp/agent_images"
        )
        if (
            not isinstance(session_temp_base_dir, str)
            or not session_temp_base_dir.strip()
            or "\x00" in session_temp_base_dir
            or not os.path.isabs(os.path.expanduser(session_temp_base_dir))
        ):
            raise ValueError("agent.session_temp_base_dir must be an absolute path")
        raw_max_file_bytes = self._get("agent", "max_file_bytes")
        if raw_max_file_bytes is not None and (
            isinstance(raw_max_file_bytes, bool)
            or not isinstance(raw_max_file_bytes, int)
            or raw_max_file_bytes < 0
        ):
            raise ValueError("agent.max_file_bytes must be a non-negative integer or null")
        allow_overwrite_existing = self._get(
            "agent", "allow_overwrite_existing", True
        )
        if not isinstance(allow_overwrite_existing, bool):
            raise ValueError("agent.allow_overwrite_existing must be a boolean")
        max_agent_switches_per_turn = self._get_int(
            "agent", "max_agent_switches_per_turn", 2
        )
        coordinator_1_timeout_seconds = self._get_float(
            "agent", "coordinator_1_timeout_seconds", 10.0
        )
        coordinator_1_routing_temperature = self._get_float(
            "agent", "coordinator_1_routing_temperature", 0.0
        )
        coordinator_1_routing_max_tokens = self._get_int(
            "agent", "coordinator_1_routing_max_tokens", 150
        )
        manager_agent_max_parallel_children = self._get_int(
            "agent", "manager_agent_max_parallel_children", 4
        )
        if (
            manager_agent_max_parallel_children is None
            or manager_agent_max_parallel_children <= 0
        ):
            raise ValueError("agent.manager_agent_max_parallel_children must be positive")
        manager_agent_child_event_buffer_limit = self._get_int(
            "agent", "manager_agent_child_event_buffer_limit", 5000
        )
        manager_agent_child_timeout_seconds = self._get_float(
            "agent", "manager_agent_child_timeout_seconds", 2400.0
        )
        raw_session_disposal_timeout = self._get(
            "agent", "session_disposal_timeout_seconds", 30.0
        )
        if raw_session_disposal_timeout is None:
            session_disposal_timeout_seconds = 30.0
        elif isinstance(raw_session_disposal_timeout, bool):
            raise ValueError(
                "agent.session_disposal_timeout_seconds must be finite and positive"
            )
        else:
            try:
                session_disposal_timeout_seconds = float(
                    raw_session_disposal_timeout
                )
            except (TypeError, ValueError, OverflowError):
                raise ValueError(
                    "agent.session_disposal_timeout_seconds must be finite and positive"
                ) from None
        raw_tool_feedback_omission = self._get("agent", "tool_feedback_omission", {})
        if not isinstance(raw_tool_feedback_omission, dict):
            raise ValueError("agent.tool_feedback_omission must be a mapping")
        omission_enabled = raw_tool_feedback_omission.get("enabled", False)
        if not isinstance(omission_enabled, bool):
            raise ValueError("agent.tool_feedback_omission.enabled must be a boolean")
        omission_threshold = raw_tool_feedback_omission.get("threshold_chars", 10_000)
        if (isinstance(omission_threshold, bool)
                or not isinstance(omission_threshold, int)
                or omission_threshold <= 0):
            raise ValueError(
                "agent.tool_feedback_omission.threshold_chars must be a positive integer"
            )
        agent_config = AgentConfig(
            enabled=self._get_bool("agent", "enabled", True),
            tool_feedback_omission=ToolFeedbackOmissionConfig(
                enabled=omission_enabled, threshold_chars=omission_threshold,
            ),
            agent_mode=normalize_agent_mode(raw_agent_mode),
            entry_agent_name=self._get("agent", "entry_agent_name", "manager_agent")
            or "manager_agent",
            enabled_agent_profiles=self._get_str_list(
                "agent",
                "enabled_agent_profiles",
                ["coordinator_1", "coordinator_2", "manager_agent", "basic_agent"],
            ),
            max_agent_switches_per_turn=(
                2
                if max_agent_switches_per_turn is None
                else max_agent_switches_per_turn
            ),
            max_steps=self._get_int("agent", "max_steps", 20),
            session_ttl_minutes=self._get_int("agent", "session_ttl_minutes", 60),
            exec_timeout=raw_exec_timeout,
            work_dir=self._get("agent", "work_dir", str(PROJECT_ROOT)),
            python_executable=self._get(
                "agent", "python_executable", "~/.agent/bin/python"
            ),
            command_mode=self._get(
                "agent", "command_mode", "restricted_shell"
            ),
            allowed_executables=allowed_executables,
            max_exec_timeout_seconds=max_exec_timeout_seconds,
            max_output_bytes=raw_max_output_bytes,
            termination_grace_seconds=termination_grace_seconds,
            allow_browser=raw_allow_browser,
            allow_desktop_capture=raw_allow_desktop_capture,
            allow_desktop_input=raw_allow_desktop_input,
            nvm_dir=self._get("agent", "nvm_dir", "~/.nvm"),
            environment_allowlist=environment_allowlist,
            command_path_entries=command_path_entries,
            command_cwd_allowlist=command_cwd_allowlist,
            command_blacklist=self._get(
                "agent", "command_blacklist"
            ),  # [{"pattern": "...", "reason": "..."}]
            playwright_profile_dir=self._get("agent", "playwright_profile_dir"),
            search_whitelist_paths=self._get_str_list(
                "agent",
                "search_whitelist_paths",
                default_search_whitelist_paths,
            ),
            file_operation_roots=file_operation_roots,
            image_input_roots=image_input_roots,
            session_temp_base_dir=session_temp_base_dir,
            max_file_bytes=raw_max_file_bytes,
            allow_overwrite_existing=allow_overwrite_existing,
            search_exclude_globs=self._get_str_list(
                "agent",
                "search_exclude_globs",
                default_search_exclude_globs,
            ),
            glob_default_limit=self._get_int("agent", "glob_default_limit", 100),
            glob_max_limit=self._get_int("agent", "glob_max_limit", 1000),
            ripgrep_timeout_seconds=self._get_int(
                "agent", "ripgrep_timeout_seconds", 15
            ),
            grep_default_head_limit=self._get_int(
                "agent", "grep_default_head_limit", 200
            ),
            grep_max_head_limit=self._get_int("agent", "grep_max_head_limit", 1000),
            grep_max_columns=self._get_int("agent", "grep_max_columns", 400),
            coordinator_1_timeout_seconds=(
                10.0
                if coordinator_1_timeout_seconds is None
                else coordinator_1_timeout_seconds
            ),
            coordinator_1_routing_temperature=(
                0.0
                if coordinator_1_routing_temperature is None
                else coordinator_1_routing_temperature
            ),
            coordinator_1_routing_max_tokens=(
                150
                if coordinator_1_routing_max_tokens is None
                else coordinator_1_routing_max_tokens
            ),
            coordinator_1_routing_extra_body=self._get(
                "agent",
                "coordinator_1_routing_extra_body",
                {"chat_template_kwargs": {"enable_thinking": False}},
            )
            or {"chat_template_kwargs": {"enable_thinking": False}},
            manager_agent_max_parallel_children=manager_agent_max_parallel_children,
            manager_agent_child_event_buffer_limit=(
                5000
                if manager_agent_child_event_buffer_limit is None
                else manager_agent_child_event_buffer_limit
            ),
            manager_agent_child_timeout_seconds=(
                2400.0
                if manager_agent_child_timeout_seconds is None
                else manager_agent_child_timeout_seconds
            ),
            session_disposal_timeout_seconds=(
                30.0
                if session_disposal_timeout_seconds is None
                else session_disposal_timeout_seconds
            ),
            handoff_debug_log_enabled=self._get_bool(
                "agent", "handoff_debug_log_enabled", False
            )
            or False,
            handoff_debug_log_path=self._get(
                "agent", "handoff_debug_log_path", "/tmp/agent_handoff_debug.jsonl"
            )
            or "/tmp/agent_handoff_debug.jsonl",
            default_profile_tools=default_profile_tools,
        )
        return self._validate_agent_config(agent_config)

    def _parse_agent_prompts_config(self) -> AgentPromptsConfig:
        """解析 Agent 提示词配置（从独立 YAML 加载）"""
        data = self._agent_prompts_data
        known_keys = {
            "coordinator_1",
            "coordinator_2",
            "manager_agent",
            "basic_agent",
            "explorer_agent",
            "auditor_agent",
            "vlm_instructions",
            "compaction",
            "business_tool_protocol",
            "native_tool_protocol_prompt",
            "business_tool_modules",
            "tool_prompt_combinations",
        }
        compaction: Optional[CompactionPromptsConfig] = None
        comp_raw = data.get("compaction")
        if isinstance(comp_raw, dict):
            compaction = CompactionPromptsConfig(
                status_message=comp_raw.get("status_message", "Compressing conversation context..."),
                no_tools_preamble=comp_raw.get("no_tools_preamble", ""),
                main_prompt=comp_raw.get("main_prompt", ""),
                custom_instructions_header=comp_raw.get("custom_instructions_header", ""),
                no_tools_trailer=comp_raw.get("no_tools_trailer", ""),
                summary_envelope_preface=comp_raw.get("summary_envelope_preface", ""),
                summary_envelope_suffix=comp_raw.get("summary_envelope_suffix", ""),
                compact_user_instruction=comp_raw.get("compact_user_instruction", ""),
                compact_control_user_instruction=comp_raw.get("compact_control_user_instruction", ""),
            )
        return AgentPromptsConfig(
            coordinator_1=data.get("coordinator_1") or {},
            coordinator_2=data.get("coordinator_2") or {},
            manager_agent=data.get("manager_agent") or {},
            basic_agent=data.get("basic_agent") or {},
            explorer_agent=data.get("explorer_agent") or {},
            auditor_agent=data.get("auditor_agent") or {},
            vlm_instructions=data.get("vlm_instructions"),
            native_tool_protocol_prompt=data.get("native_tool_protocol_prompt", ""),
            legacy_prompt_compatibility=json.loads(
                (Path(__file__).resolve().parents[1] / "config/compatibility/legacy-agent-prompts.json").read_text(encoding="utf-8")
            ),
            extra_profiles={
                key: value for key, value in data.items() if key not in known_keys
            },
            compaction=compaction,
            business_tool_protocol=(
                data.get("business_tool_protocol")
                if isinstance(data.get("business_tool_protocol"), str)
                else ""
            ),
            business_tool_modules=(
                data.get("business_tool_modules")
                if isinstance(data.get("business_tool_modules"), dict)
                else {}
            ),
            tool_prompt_combinations=(
                data.get("tool_prompt_combinations")
                if isinstance(data.get("tool_prompt_combinations"), list)
                else []
            ),
        )

    def _parse_mcp_config(self) -> MCPConfig:
        """解析 MCP 配置"""
        servers = []
        for srv in self._get("mcp", "servers", []):
            servers.append(
                MCPServerConfig(
                    name=srv.get("name", ""),
                    command=srv.get("command", ""),
                    args=srv.get("args", []),
                    env=srv.get("env", {}),
                    url=srv.get("url"),
                )
            )
        return MCPConfig(servers=servers)

    def _parse_web_tools_config(self) -> WebToolsConfig:
        """解析 Web 工具配置"""
        raw_web_tools = self._data.get("web_tools")
        web_tools = raw_web_tools if isinstance(raw_web_tools, dict) else {}
        raw_searxng = web_tools.get("searxng")
        searxng = raw_searxng if isinstance(raw_searxng, dict) else {}
        provider = self._coerce_str(web_tools.get("provider"), "searxng")
        if provider != "searxng":
            raise ValueError(
                f"Unsupported web_tools.provider={provider!r}; expected 'searxng'"
            )
        search_timeout = self._get_int("web_tools", "search_timeout", 10)
        resolved_search_timeout = 10 if search_timeout is None else search_timeout
        wikipedia_search_timeout = self._get_int(
            "web_tools", "wikipedia_search_timeout", resolved_search_timeout
        )
        raw_block_markers = self._get("web_tools", "fetch_block_markers", [])
        if isinstance(raw_block_markers, list):
            fetch_block_markers = [
                str(item).strip() for item in raw_block_markers if str(item).strip()
            ]
        elif raw_block_markers:
            fetch_block_markers = [
                item.strip()
                for item in str(raw_block_markers).split(",")
                if item.strip()
            ]
        else:
            fetch_block_markers = []

        return WebToolsConfig(
            provider=provider,
            enable_wikipedia=self._get_bool("web_tools", "enable_wikipedia", True),
            fetch_proxy=self._get("web_tools", "fetch_proxy", None),
            searxng_base_url=self._coerce_str(
                searxng.get("base_url"), "http://127.0.0.1:8080"
            ),
            searxng_search_path=self._coerce_str(
                searxng.get("search_path"), "/search"
            ),
            searxng_timeout=self._coerce_int(searxng.get("timeout"), 10),
            searxng_format=self._coerce_str(searxng.get("format"), "json"),
            searxng_language=self._coerce_str(searxng.get("language"), "all"),
            searxng_safe_search=self._coerce_int(searxng.get("safe_search"), 0),
            searxng_max_pages=self._coerce_int(searxng.get("max_pages"), 3),
            searxng_categories=self._coerce_str_list(
                searxng.get("categories"), []
            ),
            searxng_engines=self._coerce_str_list(
                searxng.get("engines"), []
            ),
            searxng_api_token=searxng.get("api_token"),
            searxng_verify_tls=self._coerce_bool(searxng.get("verify_tls"), True),
            # 搜索工具超时
            search_timeout=resolved_search_timeout,
            wikipedia_search_timeout=(
                resolved_search_timeout
                if wikipedia_search_timeout is None
                else wikipedia_search_timeout
            ),
            # 网页抓取稳健性配置
            fetch_transport=self._coerce_str(
                self._get("web_tools", "fetch_transport", "auto"), "auto"
            ),
            fetch_fallback_transport=self._get(
                "web_tools", "fetch_fallback_transport", "curl"
            ),
            fetch_content_fallback=self._get(
                "web_tools", "fetch_content_fallback", None
            ),
            fetch_impersonate=self._get("web_tools", "fetch_impersonate", "chrome"),
            fetch_max_workers=(
                3
                if self._get_int("web_tools", "fetch_max_workers", 3) is None
                else self._get_int("web_tools", "fetch_max_workers", 3)
            ),
            fetch_retry_count=(
                2
                if self._get_int("web_tools", "fetch_retry_count", 2) is None
                else self._get_int("web_tools", "fetch_retry_count", 2)
            ),
            fetch_retry_backoff_seconds=(
                1.0
                if self._get_float("web_tools", "fetch_retry_backoff_seconds", 1.0)
                is None
                else self._get_float("web_tools", "fetch_retry_backoff_seconds", 1.0)
            ),
            fetch_curl_timeout_seconds=(
                6
                if self._get_int("web_tools", "fetch_curl_timeout_seconds", 6)
                is None
                else self._get_int("web_tools", "fetch_curl_timeout_seconds", 6)
            ),
            fetch_curl_hedge_delay_seconds=(
                3
                if self._get_int("web_tools", "fetch_curl_hedge_delay_seconds", 3)
                is None
                else self._get_int("web_tools", "fetch_curl_hedge_delay_seconds", 3)
            ),
            fetch_curl_max_response_bytes=(
                8_388_608
                if self._get_int(
                    "web_tools", "fetch_curl_max_response_bytes", 8_388_608
                )
                is None
                else self._get_int(
                    "web_tools", "fetch_curl_max_response_bytes", 8_388_608
                )
            ),
            fetch_curl_cffi_timeout_seconds=(
                6
                if self._get_int("web_tools", "fetch_curl_cffi_timeout_seconds", 6)
                is None
                else self._get_int("web_tools", "fetch_curl_cffi_timeout_seconds", 6)
            ),
            fetch_curl_cffi_hedge_delay_seconds=(
                3
                if self._get_int("web_tools", "fetch_curl_cffi_hedge_delay_seconds", 3)
                is None
                else self._get_int("web_tools", "fetch_curl_cffi_hedge_delay_seconds", 3)
            ),
            fetch_crawl4ai_timeout_seconds=(
                15
                if self._get_int("web_tools", "fetch_crawl4ai_timeout_seconds", 15)
                is None
                else self._get_int("web_tools", "fetch_crawl4ai_timeout_seconds", 15)
            ),
            fetch_crawl4ai_max_concurrency=self._coerce_fetch_positive_count(
                self._get("web_tools", "fetch_crawl4ai_max_concurrency"), 50
            ),
            fetch_worker_processes=self._coerce_fetch_positive_count(
                self._get("web_tools", "fetch_worker_processes"), 4
            ),
            fetch_worker_cancel_grace_seconds=self._coerce_fetch_float(
                self._get("web_tools", "fetch_worker_cancel_grace_seconds"), 1.0
            ),
            crawl4ai_memory_threshold_percent=self._coerce_fetch_float(
                self._get("web_tools", "crawl4ai_memory_threshold_percent"), 85.0, upper=100
            ),
            crawl4ai_memory_check_interval_seconds=self._coerce_fetch_float(
                self._get("web_tools", "crawl4ai_memory_check_interval_seconds"), 0.1
            ),
            crawl4ai_batch_wait_seconds=self._coerce_fetch_float(
                self._get("web_tools", "crawl4ai_batch_wait_seconds"), 0.01, allow_zero=True
            ),
            fetch_min_text_chars=(
                200
                if self._get_int("web_tools", "fetch_min_text_chars", 200) is None
                else self._get_int("web_tools", "fetch_min_text_chars", 200)
            ),
            fetch_quality_min_text_chars=self._coerce_fetch_nonnegative_count(
                self._get("web_tools", "fetch_quality_min_text_chars"), 1000
            ),
            fetch_quality_min_text_lines=self._coerce_fetch_nonnegative_count(
                self._get("web_tools", "fetch_quality_min_text_lines"), 6
            ),
            fetch_enable_readability_fallback=(
                True
                if self._get_bool(
                    "web_tools", "fetch_enable_readability_fallback", True
                )
                is None
                else self._get_bool(
                    "web_tools", "fetch_enable_readability_fallback", True
                )
            ),
            fetch_block_markers=fetch_block_markers,
            crawl4ai_browser_type=self._coerce_str(
                self._get("web_tools", "crawl4ai_browser_type", "chromium"),
                "chromium",
            ),
            crawl4ai_headless=(
                True
                if self._get_bool("web_tools", "crawl4ai_headless", True) is None
                else self._get_bool("web_tools", "crawl4ai_headless", True)
            ),
            crawl4ai_wait_until=self._coerce_str(
                self._get("web_tools", "crawl4ai_wait_until", "domcontentloaded"),
                "domcontentloaded",
            ),
            crawl4ai_page_timeout_ms=(
                15000
                if self._get_int("web_tools", "crawl4ai_page_timeout_ms", 15000)
                is None
                else self._get_int("web_tools", "crawl4ai_page_timeout_ms", 15000)
            ),
            crawl4ai_word_count_threshold=(
                10
                if self._get_int(
                    "web_tools", "crawl4ai_word_count_threshold", 10
                )
                is None
                else self._get_int("web_tools", "crawl4ai_word_count_threshold", 10)
            ),
            crawl4ai_bypass_cache=(
                True
                if self._get_bool("web_tools", "crawl4ai_bypass_cache", True) is None
                else self._get_bool("web_tools", "crawl4ai_bypass_cache", True)
            ),
            crawl4ai_retry_count=(
                0
                if self._get_int("web_tools", "crawl4ai_retry_count", 0) is None
                else self._get_int("web_tools", "crawl4ai_retry_count", 0)
            ),
            crawl4ai_enable_stealth=(
                True
                if self._get_bool("web_tools", "crawl4ai_enable_stealth", True) is None
                else self._get_bool("web_tools", "crawl4ai_enable_stealth", True)
            ),
            crawl4ai_magic=(
                False
                if self._get_bool("web_tools", "crawl4ai_magic", False) is None
                else self._get_bool("web_tools", "crawl4ai_magic", False)
            ),
        )

    def _parse_sglang_config(self) -> SGLangConfig:
        """解析 SGLang 配置"""
        return SGLangConfig(
            external_model_package=self._get("sglang", "external_model_package"),
            flashinfer_prefill_uniform_q_len_compat=self._get_bool(
                "sglang", "flashinfer_prefill_uniform_q_len_compat"
            ),
            host=self._get("sglang", "host", "127.0.0.1"),
            port=self._get_int("sglang", "port", 30000),
            reasoning_parser=self._get("sglang", "reasoning_parser"),  # null → None
            chat_template=self._get("sglang", "chat_template"),  # null → None
            tool_call_parser=self._get("sglang", "tool_call_parser"),
            disable_cuda_graph=self._get_bool("sglang", "disable_cuda_graph", False),
            cuda_graph_max_bs=self._get_int("sglang", "cuda_graph_max_bs"),
            fp8_gemm_backend=self._get("sglang", "fp8_gemm_backend", "triton"),
            tensor_parallel_size=self._get_int("sglang", "tensor_parallel_size", 1),
            max_context_length=self._get_int("sglang", "max_context_length"),
            mem_fraction_static=self._get_float("sglang", "mem_fraction_static"),
            kv_cache_dtype=self._get("sglang", "kv_cache_dtype"),
            max_total_tokens=self._get_int("sglang", "max_total_tokens"),
            max_running_requests=self._get_int("sglang", "max_running_requests"),
            max_prefill_tokens=self._get_int("sglang", "max_prefill_tokens"),
            chunked_prefill_size=self._get_int("sglang", "chunked_prefill_size"),
            schedule_policy=self._get("sglang", "schedule_policy"),
            enable_cache_report=self._get_bool("sglang", "enable_cache_report"),
            mamba_full_memory_ratio=self._get_float(
                "sglang", "mamba_full_memory_ratio"
            ),
            mamba_ssm_dtype=self._get("sglang", "mamba_ssm_dtype"),
            max_mamba_cache_size=self._get_int("sglang", "max_mamba_cache_size"),
            skip_server_warmup=self._get_bool("sglang", "skip_server_warmup"),
            quantization=self._get("sglang", "quantization"),
            moe_runner_backend=self._get("sglang", "moe_runner_backend"),
            trust_remote_code=self._get_bool("sglang", "trust_remote_code", True),
            speculative_algorithm=self._get(
                "sglang", "speculative_algorithm"
            ),  # null → None
            speculative_num_steps=self._get_int("sglang", "speculative_num_steps"),
            speculative_eagle_topk=self._get_int("sglang", "speculative_eagle_topk"),
            speculative_num_draft_tokens=self._get_int(
                "sglang", "speculative_num_draft_tokens"
            ),
            enable_linear_replayssm_spec=self._get_bool(
                "sglang", "enable_linear_replayssm_spec"
            ),
            speculative_draft_model_path=self._get(
                "sglang", "speculative_draft_model_path"
            ),
            prefill_chunk_size=self._get_int("sglang", "prefill_chunk_size"),
        )

    def _parse_vllm_config(self) -> VLLMConfig:
        """解析 vLLM 配置"""
        return VLLMConfig(
            # 基础配置
            host=self._get("vllm", "host", "127.0.0.1"),
            port=self._get_int("vllm", "port", 30000),
            tensor_parallel_size=self._get_int("vllm", "tensor_parallel_size", 1),
            pipeline_parallel_size=self._get_int("vllm", "pipeline_parallel_size", 1),
            distributed_executor_backend=self._get(
                "vllm", "distributed_executor_backend"
            ),
            max_model_len=self._get_int("vllm", "max_model_len"),
            gpu_memory_utilization=self._get_float("vllm", "gpu_memory_utilization"),
            dtype=self._get("vllm", "dtype"),
            quantization=self._get("vllm", "quantization"),
            # 模型加载配置
            tokenizer=self._get("vllm", "tokenizer"),
            revision=self._get("vllm", "revision"),
            tokenizer_revision=self._get("vllm", "tokenizer_revision"),
            load_format=self._get("vllm", "load_format", "auto"),
            download_dir=self._get("vllm", "download_dir"),
            trust_remote_code=self._get_bool("vllm", "trust_remote_code", True),
            enforce_eager=self._get_bool("vllm", "enforce_eager"),
            # 批处理与内存配置
            max_num_seqs=self._get_int("vllm", "max_num_seqs"),
            max_num_batched_tokens=self._get_int("vllm", "max_num_batched_tokens"),
            block_size=self._get_int("vllm", "block_size"),
            enable_prefix_caching=self._get_bool("vllm", "enable_prefix_caching"),
            kv_cache_dtype=self._get("vllm", "kv_cache_dtype"),
            swap_space=self._get_int("vllm", "swap_space"),
            cpu_offload_gb=self._get_int("vllm", "cpu_offload_gb"),
            # Attention 配置
            attention_backend=self._get("vllm", "attention_backend"),
            disable_flash_attn=self._get_bool("vllm", "disable_flash_attn"),
            flash_attn_version=self._get_int("vllm", "flash_attn_version"),
            # 性能优化配置
            enable_chunked_prefill=self._get_bool("vllm", "enable_chunked_prefill"),
            max_seq_len_to_capture=self._get_int("vllm", "max_seq_len_to_capture"),
            disable_sliding_window=self._get_bool("vllm", "disable_sliding_window"),
            disable_log_stats=self._get_bool("vllm", "disable_log_stats"),
            disable_log_requests=self._get_bool("vllm", "disable_log_requests"),
            max_logprobs=self._get_int("vllm", "max_logprobs"),
            enable_prompt_logprobs=self._get_bool("vllm", "enable_prompt_logprobs"),
            guided_decoding_backend=self._get("vllm", "guided_decoding_backend"),
            disable_custom_all_reduce=self._get_bool(
                "vllm", "disable_custom_all_reduce"
            ),
            # 推理模式配置
            enable_reasoning=self._get_bool("vllm", "enable_reasoning"),
            reasoning_parser=self._get("vllm", "reasoning_parser"),
            chat_template=self._get("vllm", "chat_template"),
            tool_call_parser=self._get("vllm", "tool_call_parser"),
            tool_parser_plugin=self._get("vllm", "tool_parser_plugin"),
            enable_auto_tool_choice=self._get_bool("vllm", "enable_auto_tool_choice"),
            served_model_name=self._get("vllm", "served_model_name"),
            # LoRA 配置
            enable_lora=self._get_bool("vllm", "enable_lora"),
            max_loras=self._get_int("vllm", "max_loras"),
            max_lora_rank=self._get_int("vllm", "max_lora_rank"),
            lora_extra_vocab_size=self._get_int("vllm", "lora_extra_vocab_size"),
            max_cpu_loras=self._get_int("vllm", "max_cpu_loras"),
            fully_sharded_loras=self._get_bool("vllm", "fully_sharded_loras"),
            # 推测解码配置
            speculative_model=self._get("vllm", "speculative_model"),
            num_speculative_tokens=self._get_int("vllm", "num_speculative_tokens"),
            speculative_max_model_len=self._get_int(
                "vllm", "speculative_max_model_len"
            ),
            speculative_disable_by_batch_size=self._get_int(
                "vllm", "speculative_disable_by_batch_size"
            ),
            ngram_prompt_lookup_max=self._get_int("vllm", "ngram_prompt_lookup_max"),
            ngram_prompt_lookup_min=self._get_int("vllm", "ngram_prompt_lookup_min"),
            # 多模态配置
            enable_multi_modal=self._get_bool("vllm", "enable_multi_modal"),
            disable_mm_preprocessor_cache=self._get_bool(
                "vllm", "disable_mm_preprocessor_cache"
            ),
            # 调度器配置
            preemption_mode=self._get("vllm", "preemption_mode"),
            scheduler_delay_factor=self._get_float("vllm", "scheduler_delay_factor"),
            # 其他配置
            model_loader_extra_config=self._get("vllm", "model_loader_extra_config"),
            ignore_patterns=self._get("vllm", "ignore_patterns"),
            max_parallel_loading_workers=self._get_int(
                "vllm", "max_parallel_loading_workers"
            ),
        )

    def _parse_cycle_steps(self, raw_steps: list) -> List[CycleStepConfig]:
        """递归解析 cycle 步骤列表"""
        steps = []
        for raw in raw_steps:
            if not isinstance(raw, dict):
                continue
            if "wait" in raw:
                steps.append(
                    CycleStepConfig(
                        step_type="wait",
                        wait_seconds=float(raw["wait"]),
                    )
                )
            elif "message" in raw:
                steps.append(
                    CycleStepConfig(
                        step_type="message",
                        message=str(raw["message"]),
                        photo=raw.get("photo", []),
                    )
                )
            elif "cycle" in raw:
                cycle_data = raw["cycle"]
                if isinstance(cycle_data, dict):
                    steps.append(
                        CycleStepConfig(
                            step_type="cycle",
                            cycle_count=int(cycle_data.get("count", 1)),
                            cycle_steps=self._parse_cycle_steps(
                                cycle_data.get("steps", [])
                            ),
                        )
                    )
        return steps

    def _parse_cycle_scheduled_messages(
        self, raw_messages: list
    ) -> List[ScheduledMessageConfig]:
        """Parse only the independent, top-level scheduled track templates."""
        if not isinstance(raw_messages, list):
            raise ValueError("cycle.default_scheduled_messages must be a list")
        messages: List[ScheduledMessageConfig] = []
        for raw in raw_messages:
            if not isinstance(raw, dict):
                raise ValueError("cycle.default_scheduled_messages entries must be maps")
            messages.append(
                ScheduledMessageConfig(
                    id=raw.get("id", ""),
                    time=raw.get("time", ""),
                    recurrence=raw.get("recurrence", "once"),
                    day_count=raw.get("day_count", 1),
                    message=raw.get("message", ""),
                    photo=raw.get("photo", []),
                )
            )
        return messages

    def _parse_compact_config(self) -> CompactConfig:
        """解析 compact (/compact) 配置（位于 misc.yaml 的 compact: 段）"""
        raw = self._data.get("compact", {}) or {}
        return CompactConfig(
            enabled=raw.get("enabled", True),
            auto_trigger=raw.get("auto_trigger", True),
            strategy=raw.get("strategy", "performance"),
            performance_ratio=float(raw.get("performance_ratio", 0.5)),
            utilization_summary_reserve=int(raw.get("utilization_summary_reserve", 20000)),
            utilization_buffer=int(raw.get("utilization_buffer", 13000)),
            max_summary_tokens=int(raw.get("max_summary_tokens", 3072)),
            temperature=float(raw.get("temperature", 0.3)),
            top_p=float(raw.get("top_p", 0.9)),
            extra_body=raw.get(
                "extra_body",
                {"chat_template_kwargs": {"enable_thinking": False}},
            ),
            source_token_budget=int(raw.get("source_token_budget", 6000)),
            cycle_min_steps_between_compacts=int(
                raw.get("cycle_min_steps_between_compacts", 0)
            ),
            image_token_estimate_per_image=int(
                raw.get("image_token_estimate_per_image", 512)
            ),
            diagnostics_enabled=bool(raw.get("diagnostics_enabled", True)),
            prompt_shape=str(raw.get("prompt_shape", "cache_first_tail_with_fallback")),
            prompt_control_enabled=bool(raw.get("prompt_control_enabled", True)),
            reliable_fallback_enabled=bool(raw.get("reliable_fallback_enabled", False)),
            validation_min_summary_chars=int(raw.get("validation_min_summary_chars", 200)),
            validation_min_summary_prompt_ratio=float(
                raw.get("validation_min_summary_prompt_ratio", 0.002)
            ),
            validation_require_summary_sections=bool(
                raw.get("validation_require_summary_sections", True)
            ),
        )

    def _parse_computer_use_config(self) -> ComputerUseConfig:
        """Parse the fail-closed computer-use subtree from agent_operation."""
        raw = self._data.get("computer_use")
        if isinstance(raw, Mapping):
            raw = dict(raw)
            for name in ("helper_binary", "calibration_report_path", "operator_session_safety_config_path"):
                value = raw.get(name)
                if (isinstance(value, str) and value.strip() and "\x00" not in value
                        and not Path(value).is_absolute()):
                    raw[name] = str(PROJECT_ROOT / value)
        return ComputerUseConfig.from_mapping(raw)

    def _parse_cycle_config(self) -> CycleConfig:
        """解析 Cycle 配置"""
        raw_seq = self._get("cycle", "default_sequence", [])
        raw_scheduled_messages = self._get("cycle", "default_scheduled_messages", [])
        return CycleConfig(
            enabled=self._get_bool("cycle", "enabled", False),
            max_depth=self._get_int("cycle", "max_depth", 5),
            max_duration=self._get_int("cycle", "max_duration", 3_600),
            show_thumbnails=self._get_bool("cycle", "show_thumbnails", True),
            timeout_policy=str(
                self._get("cycle", "timeout_policy", "exempt_cycle_run")
            ),
            scheduled_track_max_duration_seconds=self._get_int(
                "cycle", "scheduled_track_max_duration_seconds", 2_592_000
            ),
            max_scheduled_message_days=self._get_int(
                "cycle", "max_scheduled_message_days", 30
            ),
            event_journal_retention_seconds=self._get_int(
                "cycle", "event_journal_retention_seconds", 86_400
            ),
            event_journal_max_records=self._get_int(
                "cycle", "event_journal_max_records", 20_000
            ),
            default_sequence=self._parse_cycle_steps(raw_seq) if raw_seq else [],
            default_scheduled_messages=self._parse_cycle_scheduled_messages(
                raw_scheduled_messages
            )
            if raw_scheduled_messages
            else [],
        )

    def _parse_camera_config(self) -> CameraConfig:
        """解析摄像头配置（camera 段现位于 config-vision.yaml，与来源文件无关）"""
        usb = self._data.get("camera", {}).get("usb", {})
        builtin = self._data.get("camera", {}).get("builtin", {})
        common = self._data.get("camera", {}).get("common", {})
        return CameraConfig(
            usb_device_open_delay=self._get_float(
                "camera.usb", "device_open_delay", 0.5
            ),
            usb_warmup_frames=self._get_int("camera.usb", "warmup_frames", 5),
            builtin_device_open_delay=self._get_float(
                "camera.builtin", "device_open_delay", 1.0
            ),
            builtin_warmup_frames=self._get_int("camera.builtin", "warmup_frames", 8),
            max_capture_attempts=self._get_int(
                "camera.common", "max_capture_attempts", 3
            ),
            min_brightness=self._get_float("camera.common", "min_brightness", 10.0),
            capture_dimension=self._get_int("camera.common", "capture_dimension", 1280),
        )

    def _parse_vision_automation_config(self) -> VisionAutomationConfig:
        """Parse vision automation configuration without coercing invalid values."""
        raw = self._data.get("vision_automation")
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError("vision_automation must be a mapping")
        try:
            parsed = VisionAutomationConfig(**raw)
        except TypeError as exc:
            raise ValueError(f"invalid vision_automation configuration: {exc}") from exc

        max_dimension = self._parse_model_config().max_image_dimension
        if (
            max_dimension is not None
            and max_dimension > 0
            and parsed.minimum_image_dimension_px > max_dimension
        ):
            raise ValueError(
                "vision_automation.minimum_image_dimension_px must not exceed "
                "vision_upload.max_image_dimension"
                " (fallback model.max_image_dimension)"
            )
        return parsed

# 全局配置实例（延迟加载）
_config_loader: Optional[ConfigLoader] = None
_app_config: Optional[AppConfig] = None


def get_config() -> AppConfig:
    """
    获取全局配置实例

    Returns:
        AppConfig: 应用配置对象

    使用示例:
        from backend.config import get_config
        config = get_config()
        print(config.model.model_path)
    """
    global _config_loader, _app_config

    if _app_config is None:
        _config_loader = ConfigLoader()
        _app_config = _config_loader.load()

    return _app_config


def reload_config() -> AppConfig:
    """
    重新加载配置

    Returns:
        AppConfig: 重新加载后的配置对象
    """
    global _config_loader, _app_config

    if _config_loader is None:
        _config_loader = ConfigLoader()

    _app_config = _config_loader.load(reload=True)
    return _app_config


if __name__ == "__main__":
    # 测试配置加载
    config = get_config()
    print(f"模型路径: {config.model.model_path}")
    print(f"服务端口: {config.server.port}")
