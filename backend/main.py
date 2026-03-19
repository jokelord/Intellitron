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
from contextlib import asynccontextmanager
from pathlib import Path

# 确保项目根目录在 Python 路径中
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api import chat_router
from backend.api.agent import router as agent_router
from backend.config import get_config, PROJECT_ROOT
from backend.llm_service import get_llm_service
from backend.utils.logger import DebugLogger, get_logger

# 获取配置和日志
config = get_config()
logger = get_logger(__name__, level=config.logging.level)
debug = DebugLogger(config.debug)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理

    启动时初始化 LLM 服务，关闭时释放资源。
    """
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

    # 初始化 Agent 服务
    app.state.agent_runner = None
    if config.agent.enabled:
        try:
            from backend.agent.agent_runner import AgentRunner
            agent_runner = AgentRunner(llm_service=service, config=config)
            app.state.agent_runner = agent_runner
            logger.info(f"Agent 服务已启用 (mode={config.agent.agent_mode})")
        except Exception as e:
            logger.warning(f"Agent 服务初始化失败: {e}")
            logger.warning("Agent 端点将不可用")
    else:
        logger.info("Agent 服务未启用")

    yield

    # 关闭时
    logger.info("正在关闭服务...")
    # 清理 Agent sessions
    if hasattr(app.state, 'agent_runner') and app.state.agent_runner:
        app.state.agent_runner.sessions.cleanup_expired(0)  # 清理所有
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


# ============================================================
# 全局异常处理
# ============================================================


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理器"""
    logger.error(f"未处理的异常: {type(exc).__name__}: {exc}")
    debug.api(f"请求路径: {request.url.path}, 异常: {exc}")

    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "message": str(exc),
                "type": type(exc).__name__,
            }
        },
    )


# ============================================================
# API 路由
# ============================================================

# 注册聊天 API 路由
app.include_router(chat_router)

# 注册 Agent API 路由（按配置启用）
if config.agent.enabled:
    app.include_router(agent_router)


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


@app.get("/health", tags=["health"])
async def health_check():
    """
    健康检查端点

    返回服务状态信息，用于负载均衡器或容器编排。
    """
    service = get_llm_service()

    return {
        "status": "healthy" if service.is_ready else "degraded",
        "model_loaded": service.is_ready,
        "model_name": config.model.model_name,
        "platform": config.platform.current,
    }


@app.get("/config", tags=["config"])
async def get_current_config():
    """
    获取当前配置

    返回模型和推理相关的配置信息，用于前端初始化设置。
    """
    # 判断是否为视觉模型
    is_vision_model = config.model.model_type == "vision"

    return {
        "model": {
            "model_name": config.model.model_name,
            "model_type": config.model.model_type or "text",
            "tensor_parallel_size": config.model.tensor_parallel_size,
            "max_context_length": config.model.max_context_length,
        },
        "vision": {
            "enabled": is_vision_model,
            "max_image_size": config.model.max_image_size,
            "supported_formats": config.model.supported_formats,
            "max_image_tokens": config.model.max_image_tokens,
            "max_image_dimension": config.model.max_image_dimension,
            "camera_max_dimension": config.model.camera_max_dimension,
        },
        "inference": {
            "temperature": config.inference.temperature,
            "top_p": config.inference.top_p,
            "top_k": config.inference.top_k,
            "max_tokens": config.inference.max_tokens,
            "repetition_penalty": config.inference.repetition_penalty,
            "stream": config.inference.stream,
        },
        "sglang": {
            "chat_template": config.sglang.chat_template,
        },
        "server": {
            "host": config.server.host,
            "port": config.server.port,
        },
        "platform": {
            "current": config.platform.current,
        },
        "agent": {
            "enabled": config.agent.enabled,
            "mode": config.agent.agent_mode,
        },
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
