"""
日志工具模块

提供统一的日志管理和调试输出功能，
支持根据配置文件中的 debug 开关控制输出。
"""

import logging
import sys
import time
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Optional

# 尝试导入 colorama 用于彩色输出
try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init()
    HAS_COLORAMA = True
except ImportError:
    HAS_COLORAMA = False


class ColorFormatter(logging.Formatter):
    """
    彩色日志格式化器
    
    根据日志级别为输出添加不同颜色。
    """
    
    COLORS = {
        logging.DEBUG: Fore.CYAN if HAS_COLORAMA else "",
        logging.INFO: Fore.GREEN if HAS_COLORAMA else "",
        logging.WARNING: Fore.YELLOW if HAS_COLORAMA else "",
        logging.ERROR: Fore.RED if HAS_COLORAMA else "",
        logging.CRITICAL: Fore.RED + Style.BRIGHT if HAS_COLORAMA else "",
    }
    RESET = Style.RESET_ALL if HAS_COLORAMA else ""
    
    def __init__(self, fmt: str, colorize: bool = True):
        """
        初始化格式化器
        
        Args:
            fmt: 日志格式字符串
            colorize: 是否启用彩色输出
        """
        super().__init__(fmt)
        self.colorize = colorize and HAS_COLORAMA
    
    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录"""
        if self.colorize:
            color = self.COLORS.get(record.levelno, "")
            record.levelname = f"{color}{record.levelname}{self.RESET}"
            record.msg = f"{color}{record.msg}{self.RESET}"
        return super().format(record)


def get_logger(
    name: str,
    level: str = "INFO",
    log_file: Optional[str] = None,
    colorize: bool = True,
    fmt: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
) -> logging.Logger:
    """
    获取配置好的日志记录器
    
    Args:
        name: 日志记录器名称
        level: 日志级别 (DEBUG/INFO/WARNING/ERROR/CRITICAL)
        log_file: 日志文件路径，为空则仅输出到控制台
        colorize: 是否启用彩色输出
        fmt: 日志格式字符串
        
    Returns:
        logging.Logger: 配置好的日志记录器
        
    使用示例:
        logger = get_logger("my_module", level="DEBUG")
        logger.info("这是一条信息")
        logger.debug("这是调试信息")
    """
    logger = logging.getLogger(name)
    
    # 避免重复添加 handler
    if logger.handlers:
        return logger
    
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # 控制台 handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ColorFormatter(fmt, colorize=colorize))
    logger.addHandler(console_handler)
    
    # 文件 handler（如果指定了日志文件）
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(fmt))
        logger.addHandler(file_handler)
    
    return logger


class DebugLogger:
    """
    调试日志器
    
    根据配置文件中的 debug 开关，有选择性地输出调试信息。
    每个调试类别对应一个开关，只有开关开启时才会输出。
    
    使用示例:
        from backend.config import get_config
        config = get_config()
        debug = DebugLogger(config.debug)
        
        debug.model_loading("模型加载中...")
        debug.inference("推理耗时: 1.5s")
    """
    
    # 调试类别前缀和颜色
    CATEGORIES = {
        "model_loading": ("🔧 [MODEL]", Fore.MAGENTA if HAS_COLORAMA else ""),
        "inference": ("⚡ [INFER]", Fore.CYAN if HAS_COLORAMA else ""),
        "api": ("🌐 [API]", Fore.BLUE if HAS_COLORAMA else ""),
        "memory": ("💾 [MEM]", Fore.YELLOW if HAS_COLORAMA else ""),
        "config": ("⚙️ [CONFIG]", Fore.GREEN if HAS_COLORAMA else ""),
        "frontend": ("🖥️ [FRONT]", Fore.WHITE if HAS_COLORAMA else ""),
    }
    RESET = Style.RESET_ALL if HAS_COLORAMA else ""
    
    def __init__(self, debug_config):
        """
        初始化调试日志器
        
        Args:
            debug_config: DebugConfig 实例
        """
        self.config = debug_config
        self._enabled = debug_config.enabled
    
    def _log(self, category: str, message: str, *args, **kwargs):
        """
        内部日志输出方法
        
        Args:
            category: 调试类别
            message: 日志消息
        """
        if not self._enabled:
            return
        
        # 检查对应类别的开关是否开启
        switch_name = f"debug_{category}"
        if not getattr(self.config, switch_name, False):
            return
        
        prefix, color = self.CATEGORIES.get(category, ("🔍 [DEBUG]", ""))
        timestamp = time.strftime("%H:%M:%S")
        
        formatted_message = message.format(*args, **kwargs) if args or kwargs else message
        print(f"{color}[{timestamp}] {prefix} {formatted_message}{self.RESET}")
    
    def model_loading(self, message: str, *args, **kwargs):
        """模型加载调试信息"""
        self._log("model_loading", message, *args, **kwargs)
    
    def inference(self, message: str, *args, **kwargs):
        """推理过程调试信息"""
        self._log("inference", message, *args, **kwargs)
    
    def api(self, message: str, *args, **kwargs):
        """API请求调试信息"""
        self._log("api", message, *args, **kwargs)
    
    def memory(self, message: str, *args, **kwargs):
        """内存监控调试信息"""
        self._log("memory", message, *args, **kwargs)
    
    def config_debug(self, message: str, *args, **kwargs):
        """配置加载调试信息"""
        self._log("config", message, *args, **kwargs)
    
    def frontend(self, message: str, *args, **kwargs):
        """前端调试信息"""
        self._log("frontend", message, *args, **kwargs)


@contextmanager
def timer(name: str = "操作", logger: Optional[logging.Logger] = None):
    """
    计时上下文管理器
    
    Args:
        name: 操作名称
        logger: 日志记录器，为空则打印到控制台
        
    使用示例:
        with timer("模型推理"):
            result = model.generate(prompt)
    """
    start = time.perf_counter()
    yield
    elapsed = time.perf_counter() - start
    
    message = f"{name} 耗时: {elapsed:.3f}s"
    if logger:
        logger.info(message)
    else:
        print(message)


def timed(name: Optional[str] = None):
    """
    计时装饰器
    
    Args:
        name: 操作名称，默认使用函数名
        
    使用示例:
        @timed("推理")
        def inference(prompt):
            return model.generate(prompt)
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            op_name = name or func.__name__
            start = time.perf_counter()
            result = func(*args, **kwargs)
            elapsed = time.perf_counter() - start
            print(f"⏱️ {op_name} 耗时: {elapsed:.3f}s")
            return result
        return wrapper
    return decorator


def get_memory_usage() -> dict:
    """
    获取当前内存使用情况
    
    Returns:
        dict: 包含内存使用信息的字典
    """
    import os
    
    result = {
        "process_memory_mb": 0,
        "gpu_memory_mb": 0,
        "gpu_memory_total_mb": 0,
    }
    
    # 获取进程内存
    try:
        import psutil
        process = psutil.Process(os.getpid())
        result["process_memory_mb"] = process.memory_info().rss / 1024 / 1024
    except ImportError:
        pass
    
    # 获取 GPU 显存
    try:
        import torch  # type: ignore[import-not-found]
        if torch.cuda.is_available():
            result["gpu_memory_mb"] = torch.cuda.memory_allocated() / 1024 / 1024
            result["gpu_memory_total_mb"] = torch.cuda.get_device_properties(0).total_memory / 1024 / 1024
    except ImportError:
        pass
    
    return result


if __name__ == "__main__":
    # 测试日志功能
    logger = get_logger("test", level="DEBUG")
    logger.debug("这是调试信息")
    logger.info("这是普通信息")
    logger.warning("这是警告信息")
    logger.error("这是错误信息")
    
    # 测试计时器
    with timer("测试操作"):
        time.sleep(0.5)
