"""
配置管理模块

负责加载和解析 config/config.ini 配置文件，
提供类型安全的配置访问接口。
"""

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


def _get_project_root() -> Path:
    """
    获取项目根目录路径
    
    Returns:
        Path: 项目根目录的绝对路径
    """
    # 从当前文件向上查找，找到包含 config 目录的位置
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "config" / "config.ini").exists():
            return current
        current = current.parent
    # 默认返回 backend 的父目录
    return Path(__file__).resolve().parent.parent


# 项目根目录
PROJECT_ROOT = _get_project_root()
# 配置文件路径
CONFIG_PATH = PROJECT_ROOT / "config" / "config.ini"


@dataclass
class ModelConfig:
    """模型配置"""
    model_path: Optional[str] = None
    model_name: Optional[str] = None
    backend: Optional[str] = None
    tensor_parallel_size: Optional[int] = None
    max_context_length: Optional[int] = None


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
    sglang: SGLangConfig = field(default_factory=SGLangConfig)
    vllm: VLLMConfig = field(default_factory=VLLMConfig)


class ConfigLoader:
    """
    配置加载器
    
    负责从 INI 文件加载配置并转换为类型安全的配置对象。
    
    使用示例:
        loader = ConfigLoader()
        config = loader.load()
        print(config.model.model_path)
    """
    
    def __init__(self, config_path: Optional[Path] = None):
        """
        初始化配置加载器
        
        Args:
            config_path: 配置文件路径，默认为 config/config.ini
        """
        self.config_path = config_path or CONFIG_PATH
        self._parser = configparser.ConfigParser()
        self._config: Optional[AppConfig] = None
    
    def load(self, reload: bool = False) -> AppConfig:
        """
        加载配置文件
        
        Args:
            reload: 是否强制重新加载
            
        Returns:
            AppConfig: 应用配置对象
            
        Raises:
            FileNotFoundError: 配置文件不存在
            configparser.Error: 配置文件格式错误
        """
        if self._config is not None and not reload:
            return self._config
        
        if not self.config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {self.config_path}")
        
        self._parser.read(self.config_path, encoding="utf-8")
        
        # 解析各模块配置
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
            sglang=self._parse_sglang_config(),
            vllm=self._parse_vllm_config(),
        )
        
        # 如果启用了配置调试，打印加载的配置
        if self._config.debug.enabled and self._config.debug.debug_config:
            self._print_debug_info()
        
        return self._config
    
    def _get(self, section: str, key: str, default: Optional[str] = None) -> Optional[str]:
        """安全获取配置值"""
        try:
            return self._parser.get(section, key)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default
    
    def _get_bool(self, section: str, key: str, default: Optional[bool] = None) -> Optional[bool]:
        """获取布尔配置值"""
        try:
            return self._parser.getboolean(section, key)
        except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
            return default
    
    def _get_int(self, section: str, key: str, default: Optional[int] = None) -> Optional[int]:
        """获取整数配置值"""
        try:
            return self._parser.getint(section, key)
        except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
            return default
    
    def _get_float(self, section: str, key: str, default: Optional[float] = None) -> Optional[float]:
        """获取浮点数配置值"""
        try:
            return self._parser.getfloat(section, key)
        except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
            return default
    
    def _parse_model_config(self) -> ModelConfig:
        """解析模型配置"""
        return ModelConfig(
            model_path=self._get("model", "model_path"),
            model_name=self._get("model", "model_name"),
            backend=self._get("model", "backend"),
            tensor_parallel_size=self._get_int("model", "tensor_parallel_size"),
            max_context_length=self._get_int("model", "max_context_length"),
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
        """解析 KV Cache 配置"""
        return KVCacheConfig(
            enable_block_reuse=self._get_bool("kv_cache", "enable_block_reuse", False),
            max_cache_size_mb=self._get_int("kv_cache", "max_cache_size_mb", 0),
        )
    
    def _parse_server_config(self) -> ServerConfig:
        """解析服务器配置"""
        return ServerConfig(
            host=self._get("server", "host", "0.0.0.0"),
            port=self._get_int("server", "port", 8000),
            workers=self._get_int("server", "workers", 1),
            api_key=self._get("server", "api_key", ""),
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
        """解析平台配置"""
        current = self._get("platform", "current", "desktop")
        
        # 解析 GPU 设备列表
        gpu_devices_str = self._get("platform.desktop", "gpu_devices", "0") or "0"
        gpu_devices = [int(x.strip()) for x in gpu_devices_str.split(",") if x.strip()]
        
        return PlatformConfig(
            current=current,
            use_dla=self._get_bool("platform.jetson_thor", "use_dla", False),
            power_mode=self._get("platform.jetson_thor", "power_mode", "MAXN"),
            memory_optimize=self._get_bool("platform.jetson_thor", "memory_optimize", True),
            multi_gpu=self._get_bool("platform.desktop", "multi_gpu", False),
            gpu_devices=gpu_devices,
        )
    
    def _parse_logging_config(self) -> LoggingConfig:
        """解析日志配置"""
        return LoggingConfig(
            level=self._get("logging", "level", "INFO"),
            file_path=self._get("logging", "file_path", ""),
            format=self._get("logging", "format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s"),
            colorize=self._get_bool("logging", "colorize", True),
        )
    
    def _parse_container_config(self) -> ContainerConfig:
        """解析 TensorRT-LLM 容器配置"""
        return ContainerConfig(
            enabled=self._get_bool("container", "enabled", False),
            api_url=self._get("container", "api_url", "http://localhost:8080/v1"),
            model_name=self._get("container", "model_name", ""),
            timeout=self._get_int("container", "timeout", 300),
            max_retries=self._get_int("container", "max_retries", 3),
        )
    
    def _parse_sglang_config(self) -> SGLangConfig:
        """解析 SGLang 配置"""
        return SGLangConfig(
            host=self._get("sglang", "host", "0.0.0.0"),
            port=self._get_int("sglang", "port", 30000),
            reasoning_parser=self._get("sglang", "reasoning_parser", "qwen3-thinking"),
            chat_template=self._get("sglang", "chat_template", "chatml"),
            tool_call_parser=self._get("sglang", "tool_call_parser", "qwen"),
            disable_cuda_graph=self._get_bool("sglang", "disable_cuda_graph", True),
            fp8_gemm_backend=self._get("sglang", "fp8_gemm_backend", "triton"),
            tensor_parallel_size=self._get_int("sglang", "tensor_parallel_size", 1),
            max_context_length=self._get_int("sglang", "max_context_length", 1048576),
            mem_fraction_static=self._get_float("sglang", "mem_fraction_static", 0.893),
            max_total_tokens=self._get_int("sglang", "max_total_tokens", 0),
            max_running_requests=self._get_int("sglang", "max_running_requests", 0),
            max_prefill_tokens=self._get_int("sglang", "max_prefill_tokens", 0),
            chunked_prefill_size=self._get_int("sglang", "chunked_prefill_size", 0),
            mamba_full_memory_ratio=self._get_float("sglang", "mamba_full_memory_ratio", 0.9),
            mamba_ssm_dtype=self._get("sglang", "mamba_ssm_dtype", "bfloat16"),
            max_mamba_cache_size=self._get_int("sglang", "max_mamba_cache_size", 0),
            skip_server_warmup=self._get_bool("sglang", "skip_server_warmup", False),
            quantization=self._get("sglang", "quantization", "modelopt_fp4"),
            moe_runner_backend=self._get("sglang", "moe_runner_backend", "auto"),
            trust_remote_code=self._get_bool("sglang", "trust_remote_code", True),
            speculative_algorithm=self._get("sglang", "speculative_algorithm", ""),
            speculative_draft_model_path=self._get("sglang", "speculative_draft_model_path", ""),
        )
    
    def _parse_vllm_config(self) -> VLLMConfig:
        """解析 vLLM 配置"""
        return VLLMConfig(
            # 基础配置
            host=self._get("vllm", "host", "127.0.0.1"),
            port=self._get_int("vllm", "port", 30000),
            tensor_parallel_size=self._get_int("vllm", "tensor_parallel_size", 1),
            pipeline_parallel_size=self._get_int("vllm", "pipeline_parallel_size", 1),
            distributed_executor_backend=self._get("vllm", "distributed_executor_backend", "mp"),
            max_model_len=self._get_int("vllm", "max_model_len", 262144),
            gpu_memory_utilization=self._get_float("vllm", "gpu_memory_utilization", 0.85),
            dtype=self._get("vllm", "dtype", "auto"),
            quantization=self._get("vllm", "quantization", ""),
            # 模型加载配置
            tokenizer=self._get("vllm", "tokenizer", ""),
            revision=self._get("vllm", "revision", ""),
            tokenizer_revision=self._get("vllm", "tokenizer_revision", ""),
            load_format=self._get("vllm", "load_format", "auto"),
            download_dir=self._get("vllm", "download_dir", ""),
            trust_remote_code=self._get_bool("vllm", "trust_remote_code", True),
            enforce_eager=self._get_bool("vllm", "enforce_eager", True),
            # 批处理与内存配置
            max_num_seqs=self._get_int("vllm", "max_num_seqs", 256),
            max_num_batched_tokens=self._get_int("vllm", "max_num_batched_tokens", 0),
            block_size=self._get_int("vllm", "block_size", 16),
            enable_prefix_caching=self._get_bool("vllm", "enable_prefix_caching", True),
            kv_cache_dtype=self._get("vllm", "kv_cache_dtype", "auto"),
            swap_space=self._get_int("vllm", "swap_space", 4),
            cpu_offload_gb=self._get_int("vllm", "cpu_offload_gb", 0),
            # Attention 配置
            attention_backend=self._get("vllm", "attention_backend", "FLASH_ATTN"),
            disable_flash_attn=self._get_bool("vllm", "disable_flash_attn", False),
            # 性能优化配置
            enable_chunked_prefill=self._get_bool("vllm", "enable_chunked_prefill", True),
            max_seq_len_to_capture=self._get_int("vllm", "max_seq_len_to_capture", 0),
            disable_sliding_window=self._get_bool("vllm", "disable_sliding_window", False),
            disable_log_stats=self._get_bool("vllm", "disable_log_stats", False),
            disable_log_requests=self._get_bool("vllm", "disable_log_requests", False),
            max_logprobs=self._get_int("vllm", "max_logprobs", 20),
            enable_prompt_logprobs=self._get_bool("vllm", "enable_prompt_logprobs", False),
            guided_decoding_backend=self._get("vllm", "guided_decoding_backend", "outlines"),
            disable_custom_all_reduce=self._get_bool("vllm", "disable_custom_all_reduce", False),
            # 推理模式配置
            enable_reasoning=self._get_bool("vllm", "enable_reasoning", False),
            reasoning_parser=self._get("vllm", "reasoning_parser", ""),
            chat_template=self._get("vllm", "chat_template", ""),
            tool_call_parser=self._get("vllm", "tool_call_parser", ""),
            enable_auto_tool_choice=self._get_bool("vllm", "enable_auto_tool_choice", False),
            served_model_name=self._get("vllm", "served_model_name", ""),
            # LoRA 配置
            enable_lora=self._get_bool("vllm", "enable_lora", False),
            max_loras=self._get_int("vllm", "max_loras", 1),
            max_lora_rank=self._get_int("vllm", "max_lora_rank", 16),
            lora_extra_vocab_size=self._get_int("vllm", "lora_extra_vocab_size", 256),
            max_cpu_loras=self._get_int("vllm", "max_cpu_loras", 0),
            fully_sharded_loras=self._get_bool("vllm", "fully_sharded_loras", False),
            # 推测解码配置
            speculative_model=self._get("vllm", "speculative_model", ""),
            num_speculative_tokens=self._get_int("vllm", "num_speculative_tokens", 0),
            speculative_max_model_len=self._get_int("vllm", "speculative_max_model_len", 0),
            speculative_disable_by_batch_size=self._get_int("vllm", "speculative_disable_by_batch_size", 0),
            ngram_prompt_lookup_max=self._get_int("vllm", "ngram_prompt_lookup_max", 0),
            ngram_prompt_lookup_min=self._get_int("vllm", "ngram_prompt_lookup_min", 0),
            # 多模态配置
            enable_multi_modal=self._get_bool("vllm", "enable_multi_modal", False),
            disable_mm_preprocessor_cache=self._get_bool("vllm", "disable_mm_preprocessor_cache", False),
            # 调度器配置
            preemption_mode=self._get("vllm", "preemption_mode", "swap"),
            scheduler_delay_factor=self._get_float("vllm", "scheduler_delay_factor", 0.0),
            # 其他配置
            model_loader_extra_config=self._get("vllm", "model_loader_extra_config", ""),
            ignore_patterns=self._get("vllm", "ignore_patterns", ""),
            max_parallel_loading_workers=self._get_int("vllm", "max_parallel_loading_workers", 0),
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
