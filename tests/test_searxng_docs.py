from pathlib import Path

import yaml


def _load_yaml(path: str):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _engines_by_name(settings: dict) -> dict:
    return {engine["name"]: engine for engine in settings.get("engines", [])}


def _assert_active_engine_policy(settings: dict) -> None:
    assert settings["use_default_settings"]["engines"]["keep_only"] == [
        "google",
        "startpage",
        "mojeek",
        "360search",
        "sogou",
        "chinaso news",
    ]
    assert settings["valkey"]["url"] == "redis://valkey:6379/0"

    engines = _engines_by_name(settings)
    proxy_endpoint = "http://host.docker.internal:10808"
    for engine_name in ("google", "startpage", "mojeek"):
        assert engines[engine_name]["disabled"] is False
        assert engines[engine_name]["proxies"] == {"all://": [proxy_endpoint]}

    for engine_name in ("360search", "sogou", "chinaso news"):
        assert engines[engine_name]["disabled"] is False
        assert "proxies" not in engines[engine_name]


def test_live_searxng_settings_use_explicit_engine_proxy_policy():
    settings = _load_yaml("deploy/searxng/core-config/settings.yml")
    assert "proxies" not in settings["outgoing"]
    _assert_active_engine_policy(settings)


def test_searxng_compose_uses_host_gateway_and_latest_version_defaults():
    compose = _load_yaml("deploy/searxng/docker-compose.yml")
    assert "host.docker.internal:host-gateway" in compose["services"]["core"]["extra_hosts"]

    for env_path in ("deploy/searxng/.env", "deploy/searxng/.env.example"):
        env_text = Path(env_path).read_text(encoding="utf-8")
        assert "SEARXNG_VERSION=latest" in env_text


def test_readme_mentions_backend_managed_searxng_without_old_provider_names():
    text = Path("README.md").read_text(encoding="utf-8")
    assert "SearXNG" in text
    assert ("duck" + "duckgo") not in text.lower()
    assert ("d" + "dg") not in text.lower()


def test_readme_uses_active_ports_and_script_names():
    text = Path("README.md").read_text(encoding="utf-8")
    assert "后端:8090" in text
    assert "scripts/2_start_backend.sh" in text
    assert "scripts/3_start_frontend.sh" in text
    assert "scripts/start_backend.sh" not in text
    assert "scripts/start_frontend.sh" not in text


def test_searxng_tutorial_mentions_delayed_startup_and_frontend_can_start_early():
    text = Path("docs/searxng-backend-managed-setup.md").read_text(encoding="utf-8")
    assert "backend" in text
    assert "8080" in text
    assert "前端" in text
    assert "搜索" in text
    assert "非搜索" in text


def test_deploy_templates_match_hardened_local_default_and_curated_engines():
    env_text = Path("deploy/searxng/.env.example").read_text(encoding="utf-8")
    settings = _load_yaml("deploy/searxng/core-config/settings.yml.example")
    settings_text = Path("deploy/searxng/core-config/settings.yml.example").read_text(
        encoding="utf-8"
    )

    assert "SEARXNG_HOST=127.0.0.1" in env_text
    assert "SEARXNG_PORT=8080" in env_text
    assert settings["valkey"]["url"] == "redis://valkey:6379/0"
    assert "360search" in settings_text
    assert "sogou" in settings_text
    assert "chinaso" in settings_text
    assert "duckduckgo" not in settings_text
    assert "startpage" not in settings_text
    assert "brave" not in settings_text


def test_live_settings_match_curated_engine_policy_and_proxy_routing():
    settings = _load_yaml("deploy/searxng/core-config/settings.yml")
    _assert_active_engine_policy(settings)


def test_tutorial_mentions_external_instance_and_local_dc_support():
    text = Path("docs/searxng-backend-managed-setup.md").read_text(encoding="utf-8")
    assert "./dc" in text
    assert "已运行" in text or "外部" in text


def test_tutorial_mentions_reuse_first_managed_fallback_and_guided_setup():
    text = Path("docs/searxng-backend-managed-setup.md").read_text(encoding="utf-8")
    assert "复用" in text
    assert "托管启动" in text
    assert "./dc up -d" in text
    assert "./dc logs -f core" in text
    assert "./dc down && ./dc up -d" in text


def test_tutorial_mentions_curated_engines_and_proxy_policy():
    text = Path("docs/searxng-backend-managed-setup.md").read_text(encoding="utf-8")
    assert "cn.bing.com" in text
    assert "Baidu" in text or "baidu" in text
    assert "360" in text
    assert "Sogou" in text or "sogou" in text
    assert "ChinaSo" in text or "chinaso" in text
    assert "中国系引擎直连" in text
    assert "外网引擎" in text


def test_agent_prompts_explain_final_max_results_without_exposing_pagination_internals():
    text = Path("config/config-agent-prompts.yaml").read_text(encoding="utf-8")
    assert "max_results" in text
    assert "最终得到的结果数量" in text
    assert "后端会自动处理内部搜索扩展与结果整理" in text
    assert "pageno" not in text
