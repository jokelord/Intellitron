"""Helpers for backend-managed SearXNG startup orchestration."""

from urllib.parse import urlparse


def should_start_searxng_after_backend_ready(
    backend_ready: bool,
    docker_available: bool,
) -> bool:
    return backend_ready and docker_available


def detect_searxng_compose_command(
    has_local_dc: bool,
    docker_compose_available: bool,
    docker_compose_legacy_available: bool,
) -> str | None:
    if has_local_dc:
        return "./dc"
    if docker_compose_available:
        return "docker compose"
    if docker_compose_legacy_available:
        return "docker-compose"
    return None


def choose_searxng_start_mode(
    backend_ready: bool,
    searxng_reachable: bool,
    docker_available: bool,
    compose_command: str | None,
) -> str:
    if not backend_ready:
        return "degraded"
    if searxng_reachable:
        return "external"
    if docker_available and compose_command:
        return "managed"
    return "degraded"


def get_searxng_bind_port(base_url: str) -> int:
    parsed = urlparse(base_url)
    if not parsed.hostname:
        raise ValueError(f"Invalid SEARXNG_BASE_URL: {base_url!r}")
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme == "https":
        return 443
    return 80


def should_manage_searxng_locally(base_url: str) -> bool:
    parsed = urlparse(base_url)
    return parsed.hostname in {"127.0.0.1", "localhost", "0.0.0.0"}


def build_searxng_setup_guidance() -> str:
    return "\n".join(
        [
            "建议按教程完成本地部署：",
            "1. 进入 deploy/searxng 并确保 ./dc 可执行：chmod +x ./dc",
            "2. 如镜像拉取失败，先为 Docker daemon 配置代理",
            "3. 准备 docker-compose.yml、.env、core-config/settings.yml",
            "4. 启动：./dc up -d",
            "5. 查看日志：./dc logs -f core",
            "6. 修改配置后重启：./dc down && ./dc up -d",
        ]
    )


def classify_searxng_failure_message(reason: str) -> str:
    return (
        f"⚠️ SearXNG 未就绪：{reason}。\n"
        "你现在仍然可以去开启前端；当前仅网络搜索功能受影响，非搜索功能保持可用。\n"
        f"{build_searxng_setup_guidance()}"
    )
