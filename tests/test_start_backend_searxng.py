from backend.tools.start_backend_searxng import (
    classify_searxng_failure_message,
    choose_searxng_start_mode,
    detect_searxng_compose_command,
    get_searxng_bind_port,
    should_manage_searxng_locally,
    should_start_searxng_after_backend_ready,
)


def test_searxng_never_starts_before_backend_ready():
    assert should_start_searxng_after_backend_ready(False, docker_available=True) is False


def test_searxng_may_start_once_backend_ready_and_docker_is_available():
    assert should_start_searxng_after_backend_ready(True, docker_available=True) is True


def test_searxng_cannot_start_without_docker_even_after_backend_ready():
    assert should_start_searxng_after_backend_ready(True, docker_available=False) is False


def test_failure_message_mentions_frontend_and_non_search_availability():
    message = classify_searxng_failure_message("docker compose unavailable")
    assert "前端" in message
    assert "搜索" in message
    assert "非搜索" in message


def test_failure_message_includes_guided_setup_steps():
    message = classify_searxng_failure_message("Docker Compose 不可用")
    assert "./dc" in message
    assert "chmod +x ./dc" in message
    assert "./dc up -d" in message
    assert "./dc logs -f core" in message
    assert "./dc down && ./dc up -d" in message


def test_existing_searxng_instance_skips_managed_startup():
    assert (
        choose_searxng_start_mode(
            backend_ready=True,
            searxng_reachable=True,
            docker_available=True,
            compose_command="docker compose",
        )
        == "external"
    )


def test_managed_startup_uses_detected_compose_command_when_instance_absent():
    assert (
        choose_searxng_start_mode(
            backend_ready=True,
            searxng_reachable=False,
            docker_available=True,
            compose_command="./dc",
        )
        == "managed"
    )


def test_startup_degrades_when_no_existing_instance_and_no_compose_command():
    assert (
        choose_searxng_start_mode(
            backend_ready=True,
            searxng_reachable=False,
            docker_available=True,
            compose_command=None,
        )
        == "degraded"
    )


def test_detect_searxng_compose_command_prefers_local_dc():
    assert (
        detect_searxng_compose_command(
            has_local_dc=True,
            docker_compose_available=True,
            docker_compose_legacy_available=True,
        )
        == "./dc"
    )


def test_detect_searxng_compose_command_falls_back_to_docker_compose_plugin():
    assert (
        detect_searxng_compose_command(
            has_local_dc=False,
            docker_compose_available=True,
            docker_compose_legacy_available=False,
        )
        == "docker compose"
    )


def test_get_searxng_bind_port_defaults_to_80_for_http_without_explicit_port():
    assert get_searxng_bind_port("http://127.0.0.1/search") == 80


def test_get_searxng_bind_port_defaults_to_443_for_https_without_explicit_port():
    assert get_searxng_bind_port("https://searx.example.com/search") == 443


def test_get_searxng_bind_port_uses_explicit_port_when_present():
    assert get_searxng_bind_port("http://127.0.0.1:8080/search") == 8080


def test_get_searxng_bind_port_raises_for_missing_host():
    try:
        get_searxng_bind_port("not-a-url")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "SEARXNG_BASE_URL" in str(exc)


def test_should_manage_searxng_locally_accepts_loopback_hosts():
    assert should_manage_searxng_locally("http://127.0.0.1:8080") is True
    assert should_manage_searxng_locally("http://localhost:8080") is True
    assert should_manage_searxng_locally("http://0.0.0.0:8080") is True


def test_should_manage_searxng_locally_rejects_non_local_hosts():
    assert should_manage_searxng_locally("https://searx.example.com") is False
