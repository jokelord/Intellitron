"""
配置管理模块

负责从 config/config-overall.yaml 及其引用的4个模块化子配置文件加载配置，
提供类型安全的配置访问接口。

子配置文件路由（由 config-overall.yaml 声明）：
  model  → config-model.yaml   （模型参数）
  infer  → config-infer.yaml   （推理/KV Cache/SGLang/vLLM/容器）
  server → config-server.yaml  （服务端/前端地址）
  misc   → config-misc.yaml    （debug/platform/logging）
"""

import os
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


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
# 总配置文件路径（引用4个模块化子配置）
CONFIG_PATH = PROJECT_ROOT / "config" / "config-overall.yaml"


@dataclass
class ModelConfig:
    """模型配置"""

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
        None  # 图片预处理最大边长(px), 0=不缩放（文件上传路径，后端处理）
    )
    camera_max_dimension: Optional[int] = (
        None  # 摄像头拍照前端 canvas 预缩最大边长(px), 0=不缩放
    )


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


@dataclass
class FrontendConfig:
    """前端配置"""

    dev_port: Optional[int] = None
    api_base_url: Optional[str] = None


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

    # Engine providers
    search_engine: str = "duckduckgo"
    fallback_engine: str = "bing"

    # Technical protocol
    protocol: str = "curl"  # curl | requests
    method: str = "POST"  # GET | POST

    # Fallback protocol/method
    fallback_protocol: str = "curl"
    fallback_method: str = "GET"

    # Other settings
    enable_wikipedia: bool = True
    proxy: Optional[str] = None


@dataclass
class AgentConfig:
    """Agent 工具调用配置"""

    enabled: Optional[bool] = None
    agent_mode: str = "tool_calling"  # "tool_calling" | "code"
    max_steps: Optional[int] = None
    session_ttl_minutes: Optional[int] = None
    exec_timeout: Optional[int] = None
    work_dir: Optional[str] = None
    python_authorized_imports: Optional[List[str]] = None
    command_blacklist: Optional[List[dict]] = (
        None  # [{"pattern": "...", "reason": "..."}]
    )


@dataclass
class AgentPromptsConfig:
    """Agent 提示词配置（从 config-agent-prompts.yaml 加载）"""

    # 自定义指令（注入到 system_prompt 模板的 {{ custom_instructions }} 位置）
    agent_instructions: Optional[str] = None
    # VLM 专用指令（仅当 model_type=vision 时由 _build_agent() 动态追加到 instructions）
    vlm_instructions: Optional[str] = None
    # ToolCallingAgent 的完整 prompt_templates 字典
    toolcalling_agent: Optional[dict] = None
    # CodeAgent 的完整 prompt_templates 字典
    code_agent: Optional[dict] = None


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
class CycleConfig:
    """Cycle（可编程序列）配置"""

    enabled: bool = False
    max_depth: int = 5
    max_duration: int = 3600
    default_sequence: List[CycleStepConfig] = field(default_factory=list)


@dataclass
class SGLangConfig:
    """SGLang 配置"""

    host: Optional[str] = None
    port: Optional[int] = None
    reasoning_parser: Optional[str] = None
    chat_template: Optional[str] = None
    tool_call_parser: Optional[str] = None
    disable_cuda_graph: Optional[bool] = None
    fp8_gemm_backend: Optional[str] = None
    tensor_parallel_size: Optional[int] = None
    max_context_length: Optional[int] = None
    mem_fraction_static: Optional[float] = None
    max_total_tokens: Optional[int] = None
    max_running_requests: Optional[int] = None
    max_prefill_tokens: Optional[int] = None
    chunked_prefill_size: Optional[int] = None
    mamba_full_memory_ratio: Optional[float] = None
    mamba_ssm_dtype: Optional[str] = None
    max_mamba_cache_size: Optional[int] = None
    skip_server_warmup: Optional[bool] = None
    quantization: Optional[str] = None
    moe_runner_backend: Optional[str] = None
    trust_remote_code: Optional[bool] = None
    speculative_algorithm: Optional[str] = None
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
    debug: DebugConfig = field(default_factory=DebugConfig)
    platform: PlatformConfig = field(default_factory=PlatformConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    container: ContainerConfig = field(default_factory=ContainerConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    agent_prompts: AgentPromptsConfig = field(default_factory=AgentPromptsConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    web_tools: WebToolsConfig = field(default_factory=WebToolsConfig)
    sglang: SGLangConfig = field(default_factory=SGLangConfig)
    vllm: VLLMConfig = field(default_factory=VLLMConfig)
    cycle: CycleConfig = field(default_factory=CycleConfig)


class ConfigLoader:
    """
    配置加载器

    负责从 config-overall.yaml 及其引用的子配置文件加载配置，
    转换为类型安全的配置对象。

    sub-file 路由由 config-overall.yaml 顶层键决定：
      model / infer / server / misc → 对应子 YAML 路径

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
        self._data = {}
        for key in ("model", "infer", "server", "misc"):
            rel_path = overall.get(key)
            if rel_path:
                sub_path = PROJECT_ROOT / rel_path
                if sub_path.exists():
                    sub_data = self._load_yaml_file(sub_path)
                    self._data.update(sub_data)

        # 2.5 加载 agent_prompts 配置（独立文件，不合并到 _data）
        self._agent_prompts_data: dict = {}
        agent_prompts_rel = overall.get("agent_prompts")
        if agent_prompts_rel:
            agent_prompts_path = PROJECT_ROOT / agent_prompts_rel
            if agent_prompts_path.exists():
                self._agent_prompts_data = self._load_yaml_file(agent_prompts_path)

        # 3. 解析各模块配置
        self._config = AppConfig(
            model=self._parse_model_config(),
            inference=self._parse_inference_config(),
            kv_cache=self._parse_kv_cache_config(),
            server=self._parse_server_config(),
            frontend=self._parse_frontend_config(),
            debug=self._parse_debug_config(),
            platform=self._parse_platform_config(),
            logging=self._parse_logging_config(),
            container=self._parse_container_config(),
            agent=self._parse_agent_config(),
            agent_prompts=self._parse_agent_prompts_config(),
            mcp=self._parse_mcp_config(),
            web_tools=self._parse_web_tools_config(),
            sglang=self._parse_sglang_config(),
            vllm=self._parse_vllm_config(),
            cycle=self._parse_cycle_config(),
        )

        if self._config.debug.enabled and self._config.debug.debug_config:
            self._print_debug_info()

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

    def _parse_model_config(self) -> ModelConfig:
        """解析模型配置"""
        return ModelConfig(
            model_path=self._get("model", "model_path"),
            model_name=self._get("model", "model_name"),
            tensor_parallel_size=self._get_int("model", "tensor_parallel_size"),
            max_context_length=self._get_int("model", "max_context_length"),
            model_type=self._get("model", "model_type", "text"),
            max_image_size=self._get_int("model", "max_image_size", 10485760),
            supported_formats=self._get(
                "model", "supported_formats", "png,jpg,jpeg,gif,webp"
            ),
            max_image_tokens=self._get_int("model", "max_image_tokens", 8192),
            max_image_dimension=self._get_int("model", "max_image_dimension", 1280),
            camera_max_dimension=self._get_int("model", "camera_max_dimension", 768),
        )

    def _parse_inference_config(self) -> InferenceConfig:
        """解析推理配置"""
        max_tokens = self._get_int("inference", "max_tokens", 131072)
        return InferenceConfig(
            temperature=self._get_float("inference", "temperature", 0.1),
            top_p=self._get_float("inference", "top_p", 0.95),
            top_k=self._get_int("inference", "top_k", 0),
            max_tokens=max_tokens,
            max_new_tokens=max_tokens,  # 兼容 SGLang API
            repetition_penalty=self._get_float("inference", "repetition_penalty", 0.9),
            stream=self._get_bool("inference", "stream", True),
            timeout=self._get_int("inference", "timeout", 5400),
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
            port=self._get_int("server", "port", 8000),
            workers=self._get_int("server", "workers", 1),
            api_key=self._get("server", "api_key"),  # null → None（不启用认证）
            cors_origins=self._get("server", "cors_origins", "*"),
        )

    def _parse_frontend_config(self) -> FrontendConfig:
        """解析前端配置"""
        return FrontendConfig(
            dev_port=self._get_int("frontend", "dev_port", 3000),
            api_base_url=self._get("frontend", "api_base_url", "http://localhost:8000"),
        )

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

    def _parse_container_config(self) -> ContainerConfig:
        """解析容器配置"""
        return ContainerConfig(
            enabled=self._get_bool("container", "enabled", False),
            api_url=self._get("container", "api_url", "http://localhost:8080/v1"),
            model_name=self._get("container", "model_name"),  # null → None
            timeout=self._get_int("container", "timeout", 300),
            max_retries=self._get_int("container", "max_retries", 3),
        )

    def _parse_agent_config(self) -> AgentConfig:
        """解析 Agent 配置"""
        return AgentConfig(
            enabled=self._get_bool("agent", "enabled", True),
            agent_mode=self._get("agent", "agent_mode", "tool_calling"),
            max_steps=self._get_int("agent", "max_steps", 20),
            session_ttl_minutes=self._get_int("agent", "session_ttl_minutes", 60),
            exec_timeout=self._get_int("agent", "exec_timeout", 60),
            work_dir=self._get("agent", "work_dir"),  # null → None（使用 cwd）
            python_authorized_imports=self._get("agent", "python_authorized_imports"),
            command_blacklist=self._get(
                "agent", "command_blacklist"
            ),  # [{"pattern": "...", "reason": "..."}]
        )

    def _parse_agent_prompts_config(self) -> AgentPromptsConfig:
        """解析 Agent 提示词配置（从独立 YAML 加载）"""
        data = self._agent_prompts_data
        return AgentPromptsConfig(
            agent_instructions=data.get("agent_instructions"),
            vlm_instructions=data.get("vlm_instructions"),
            toolcalling_agent=data.get("toolcalling_agent"),
            code_agent=data.get("code_agent"),
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
        return WebToolsConfig(
            # Engine providers
            search_engine=self._get("web_tools", "search_engine", "duckduckgo"),
            fallback_engine=self._get("web_tools", "fallback_engine", "bing"),
            # Technical protocol
            protocol=self._get("web_tools", "protocol", "curl"),
            method=self._get("web_tools", "method", "POST"),
            # Fallback protocol/method
            fallback_protocol=self._get("web_tools", "fallback_protocol", "curl"),
            fallback_method=self._get("web_tools", "fallback_method", "GET"),
            # Other settings
            enable_wikipedia=self._get_bool("web_tools", "enable_wikipedia", True),
            proxy=self._get("web_tools", "proxy", None),
        )

    def _parse_sglang_config(self) -> SGLangConfig:
        """解析 SGLang 配置"""
        return SGLangConfig(
            host=self._get("sglang", "host", "127.0.0.1"),
            port=self._get_int("sglang", "port", 30000),
            reasoning_parser=self._get("sglang", "reasoning_parser"),  # null → None
            chat_template=self._get("sglang", "chat_template"),  # null → None
            tool_call_parser=self._get("sglang", "tool_call_parser"),
            disable_cuda_graph=self._get_bool("sglang", "disable_cuda_graph", False),
            fp8_gemm_backend=self._get("sglang", "fp8_gemm_backend", "triton"),
            tensor_parallel_size=self._get_int("sglang", "tensor_parallel_size", 1),
            max_context_length=self._get_int("sglang", "max_context_length"),
            mem_fraction_static=self._get_float("sglang", "mem_fraction_static"),
            max_total_tokens=self._get_int("sglang", "max_total_tokens"),
            max_running_requests=self._get_int("sglang", "max_running_requests"),
            max_prefill_tokens=self._get_int("sglang", "max_prefill_tokens"),
            chunked_prefill_size=self._get_int("sglang", "chunked_prefill_size"),
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

    def _parse_cycle_config(self) -> CycleConfig:
        """解析 Cycle 配置"""
        raw_seq = self._get("cycle", "default_sequence", [])
        return CycleConfig(
            enabled=self._get_bool("cycle", "enabled", False),
            max_depth=self._get_int("cycle", "max_depth", 5),
            max_duration=self._get_int("cycle", "max_duration", 3600),
            default_sequence=self._parse_cycle_steps(raw_seq) if raw_seq else [],
        )

    def _print_debug_info(self):
        """打印配置调试信息"""
        if self._config is None:
            print("[DEBUG] 配置未加载")
            return

        print("\n" + "=" * 60)
        print("[DEBUG] 配置加载完成")
        print("=" * 60)
        print(f"配置文件路径: {self.config_path}")
        if self._config.model:
            print(f"模型路径: {self._config.model.model_path}")
            print(f"模型名称: {self._config.model.model_name}")
            print(f"张量并行数: {self._config.model.tensor_parallel_size}")
        if self._config.platform:
            print(f"当前平台: {self._config.platform.current}")
        if self._config.server:
            print(f"服务地址: {self._config.server.host}:{self._config.server.port}")
        print("=" * 60 + "\n")


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
