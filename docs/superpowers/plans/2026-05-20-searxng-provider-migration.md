# SearXNG Provider Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the active search provider path with self-hosted SearXNG, migrate active config/docs/tests away from old provider-specific fields, and make `scripts/2_start_backend.sh` start SearXNG only after backend readiness while keeping backend usable even when SearXNG is unavailable.

**Architecture:** Keep `search_and_fetch` and `web_fetch` as the upper-layer search-plus-fetch pipeline, but replace the old provider-specific search implementation with a single SearXNG JSON client. Move SearXNG lifecycle ownership into `scripts/2_start_backend.sh`, using backend `/health` as the readiness gate and Docker Compose as the deployment mechanism for a localhost SearXNG instance on port `8080`, while the repository backend moves to `8090` in both the root workspace and `./.worktrees/searXNG/`.

**Tech Stack:** Python 3.12, FastAPI, bash, Docker Compose, pytest, curl/httpx, existing config loader, existing page-fetch extraction stack.

---

## File Map

### Create
- `deploy/searxng/docker-compose.yml`
  - Repository-local Compose template for SearXNG core + valkey on port `8080`.
- `deploy/searxng/.env.example`
  - Beginner-editable environment template matching the official container deployment pattern.
- `deploy/searxng/core-config/settings.yml.example`
  - Project-focused SearXNG settings template with `json` enabled for `/search`.
- `backend/tools/start_backend_searxng.py`
  - Tiny pure-Python helper module for backend-managed SearXNG startup policy and degraded-status messaging, so the orchestration rules can be locked by tests before shell edits.
- `docs/searxng-backend-managed-setup.md`
  - Beginner tutorial explaining prerequisites, first startup, delayed startup behavior, progress output, and shutdown behavior.
- `tests/test_searxng_search.py`
  - Focused tests for SearXNG JSON search normalization and error handling.
- `tests/test_start_backend_searxng.py`
  - Focused tests for backend readiness gating, delayed SearXNG startup, degraded behavior, and shutdown ownership.
- `tests/test_searxng_docs.py`
  - Focused tests for README/tutorial hygiene and required user-facing messaging.

### Modify
- `.gitignore`
  - Add `/.worktree/` so the requested implementation path can be ignored safely; active implementation now proceeds in `./.worktrees/searXNG/`.
- `backend/config.py`
  - Replace the old generic search provider fields in `WebToolsConfig` and `_parse_web_tools_config()` with the SearXNG-specific model.
- `config/config-misc.yaml`
  - Replace old provider-selection fields with the new `web_tools.provider` and `web_tools.searxng.*` config block.
- `backend/tools/web_tools.py`
  - Remove active old-provider search logic from the runtime path and implement SearXNG JSON search, normalization, and explicit error mapping while preserving fetch behavior.
- `backend/tools/search_and_fetch.py`
  - Keep the outer behavior intact, adapting only if needed for the new normalized search output or error strings.
- `tests/test_web_tools_config.py`
  - Replace config parsing coverage for the old provider model with SearXNG config parsing coverage.
- `tests/test_web_fetch.py`
  - Remove now-obsolete old-provider search tests while keeping fetch robustness coverage intact.
- `scripts/2_start_backend.sh`
  - Add backend readiness polling, delayed SearXNG startup, visible pull/start progress, degraded messaging, and owned-stack shutdown handling.
- `README.md`
  - Add one concise current-state note that backend startup now auto-manages a local SearXNG service and that detailed setup is documented elsewhere.
- `config/config-server.yaml`
  - Root workspace backend defaults must move to `8090`, including frontend API base URL.
- `scripts/3_start_frontend.sh`
  - Root workspace frontend startup script must inherit the new backend default URL on `8090`.
- `frontend/src/lib/api.ts`
  - Root workspace frontend fallback backend URL/port must move to `8090`.

### Verification targets
- `tests/test_web_tools_config.py`
- `tests/test_web_fetch.py`
- `tests/test_searxng_search.py`
- `tests/test_start_backend_searxng.py`
- `tests/test_searxng_docs.py`
- `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_web_tools_config.py tests/test_web_fetch.py tests/test_searxng_search.py tests/test_start_backend_searxng.py tests/test_searxng_docs.py -q`
- Focused manual verification of `scripts/2_start_backend.sh` behavior in a worktree at `./.worktrees/searXNG/`

---

### Task 1: Prepare the requested implementation worktree path safely

**Files:**
- Modify: `.gitignore:183-184`

- [ ] **Step 1: Write the failing test**

Create a tiny guard test that enforces the new ignored path exists in gitignore by adding this to a temporary focused test file `tests/test_worktree_ignore.py`:

```python
from pathlib import Path


def test_project_gitignore_ignores_dot_worktree_directory():
    gitignore = Path('.gitignore').read_text(encoding='utf-8')
    assert '/.worktree/' in gitignore
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_worktree_ignore.py -q
```

Expected: FAIL because `/.worktree/` is not in `.gitignore` yet.

- [ ] **Step 3: Write minimal implementation**

Update `.gitignore` so the relevant block becomes:

```gitignore
/.worktrees/
/.worktree/
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_worktree_ignore.py -q
```

Expected: PASS.

- [ ] **Step 5: Remove the temporary test after locking the ignore rule into git history**

Delete `tests/test_worktree_ignore.py` after the change is committed, because its sole purpose is to force the ignore rule addition before implementation work starts.

- [ ] **Step 6: Create the implementation worktree**

Run:

```bash
git worktree add "./.worktrees/searXNG" -b "feature/searxng-provider-migration"
```

Expected: a clean isolated worktree at `./.worktrees/searXNG/`.

- [ ] **Step 7: Commit**

```bash
git add .gitignore
git commit -m "chore: ignore requested searxng worktree path"
```

### Task 2: Replace the old web-tools config model with SearXNG config

**Files:**
- Modify: `backend/config.py:283-316`
- Modify: `backend/config.py:941-994`
- Modify: `config/config-misc.yaml:122-184`
- Modify: `tests/test_web_tools_config.py`

- [ ] **Step 1: Write the failing test**

Replace `tests/test_web_tools_config.py` web-tools coverage with the following focused tests:

```python
"""Tests for config parsing defaults."""

from backend.config import AgentConfig, ConfigLoader, WebToolsConfig


class TestParseWebToolsConfig:
    def _make_loader_with_data(self, web_tools_data: dict) -> ConfigLoader:
        loader = ConfigLoader.__new__(ConfigLoader)
        loader._data = {"web_tools": web_tools_data}
        loader._agent_prompts_data = {}
        return loader

    def test_parse_searxng_provider_defaults(self):
        loader = self._make_loader_with_data({})

        cfg = loader._parse_web_tools_config()

        assert isinstance(cfg, WebToolsConfig)
        assert cfg.provider == "searxng"
        assert cfg.searxng_base_url == "http://127.0.0.1:8080"
        assert cfg.searxng_search_path == "/search"
        assert cfg.searxng_format == "json"
        assert cfg.searxng_language == "all"
        assert cfg.searxng_safe_search == 0
        assert cfg.searxng_categories == []
        assert cfg.searxng_engines == []
        assert cfg.searxng_api_token is None
        assert cfg.searxng_verify_tls is True

    def test_parse_searxng_provider_custom_values(self):
        loader = self._make_loader_with_data(
            {
                "provider": "searxng",
                "searxng": {
                    "base_url": "https://search.internal:8080",
                    "search_path": "/search",
                    "timeout": 21,
                    "format": "json",
                    "language": "zh-CN",
                    "safe_search": 2,
                    "categories": ["general"],
                    "engines": ["engine-a", "engine-b"],
                    "api_token": "secret-token",
                    "verify_tls": False,
                },
                "search_timeout": 15,
                "wikipedia_search_timeout": 23,
                "fetch_transport": "curl_cffi",
                "fetch_fallback_transport": "curl",
                "fetch_impersonate": "chrome",
                "fetch_retry_count": 3,
                "fetch_retry_backoff_seconds": 1.5,
                "fetch_min_text_chars": 180,
                "fetch_enable_readability_fallback": True,
                "fetch_block_markers": ["captcha", "just a moment"],
            }
        )

        cfg = loader._parse_web_tools_config()

        assert cfg.provider == "searxng"
        assert cfg.searxng_base_url == "https://search.internal:8080"
        assert cfg.searxng_timeout == 21
        assert cfg.searxng_language == "zh-CN"
        assert cfg.searxng_safe_search == 2
        assert cfg.searxng_categories == ["general"]
        assert cfg.searxng_engines == ["engine-a", "engine-b"]
        assert cfg.searxng_api_token == "secret-token"
        assert cfg.searxng_verify_tls is False
        assert cfg.search_timeout == 15
        assert cfg.wikipedia_search_timeout == 23
        assert cfg.fetch_transport == "curl_cffi"
        assert cfg.fetch_fallback_transport == "curl"
        assert cfg.fetch_impersonate == "chrome"
        assert cfg.fetch_retry_count == 3
        assert cfg.fetch_retry_backoff_seconds == 1.5
        assert cfg.fetch_min_text_chars == 180
        assert cfg.fetch_enable_readability_fallback is True
        assert cfg.fetch_block_markers == ["captcha", "just a moment"]


class TestParseAgentConfig:
    def _make_loader_with_data(self, agent_data: dict) -> ConfigLoader:
        loader = ConfigLoader.__new__(ConfigLoader)
        loader._data = {"agent": agent_data}
        loader._agent_prompts_data = {}
        return loader

    def test_code_agent_mode_maps_to_tool_calling(self):
        loader = self._make_loader_with_data({"agent_mode": "code"})

        cfg = loader._parse_agent_config()

        assert isinstance(cfg, AgentConfig)
        assert cfg.agent_mode == "tool_calling"

    def test_parse_agent_python_executable(self):
        loader = self._make_loader_with_data(
            {"python_executable": "/opt/custom-agent/bin/python"}
        )

        cfg = loader._parse_agent_config()

        assert cfg.python_executable == "/opt/custom-agent/bin/python"

    def test_agent_python_executable_defaults_to_agent_uv_path(self):
        loader = self._make_loader_with_data({})

        cfg = loader._parse_agent_config()

        assert cfg.python_executable == "~/.agent/bin/python"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_web_tools_config.py -q
```

Expected: FAIL because `WebToolsConfig` and `_parse_web_tools_config()` still expose the old provider fields.

- [ ] **Step 3: Write minimal implementation**

Update `backend/config.py` so `WebToolsConfig` becomes:

```python
@dataclass
class WebToolsConfig:
    """Web 工具配置"""

    provider: str = "searxng"
    enable_wikipedia: bool = True
    proxy: Optional[str] = None

    searxng_base_url: str = "http://127.0.0.1:8080"
    searxng_search_path: str = "/search"
    searxng_timeout: int = 15
    searxng_format: str = "json"
    searxng_language: str = "all"
    searxng_safe_search: int = 0
    searxng_categories: List[str] = field(default_factory=list)
    searxng_engines: List[str] = field(default_factory=list)
    searxng_api_token: Optional[str] = None
    searxng_verify_tls: bool = True

    search_timeout: int = 10
    wikipedia_search_timeout: int = 10

    fetch_transport: str = "auto"
    fetch_fallback_transport: Optional[str] = "curl"
    fetch_impersonate: Optional[str] = "chrome"
    fetch_max_workers: int = 3
    fetch_retry_count: int = 2
    fetch_retry_backoff_seconds: float = 1.0
    fetch_min_text_chars: int = 200
    fetch_enable_readability_fallback: bool = True
    fetch_block_markers: List[str] = field(default_factory=list)
```

Update `_parse_web_tools_config()` to parse the nested `searxng` block instead of the old search-engine keys:

```python
    def _parse_web_tools_config(self) -> WebToolsConfig:
        """解析 Web 工具配置"""
        search_timeout = self._get_int("web_tools", "search_timeout", 10)
        searxng = self._get("web_tools", "searxng", {}) or {}

        def _clean_list(value):
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]
            if value:
                return [item.strip() for item in str(value).split(",") if item.strip()]
            return []

        return WebToolsConfig(
            provider=self._get("web_tools", "provider", "searxng"),
            enable_wikipedia=self._get_bool("web_tools", "enable_wikipedia", True),
            proxy=self._get("web_tools", "proxy", None),
            searxng_base_url=str(searxng.get("base_url", "http://127.0.0.1:8080")).rstrip("/"),
            searxng_search_path=str(searxng.get("search_path", "/search")),
            searxng_timeout=int(searxng.get("timeout", 15)),
            searxng_format=str(searxng.get("format", "json")),
            searxng_language=str(searxng.get("language", "all")),
            searxng_safe_search=int(searxng.get("safe_search", 0)),
            searxng_categories=_clean_list(searxng.get("categories", [])),
            searxng_engines=_clean_list(searxng.get("engines", [])),
            searxng_api_token=searxng.get("api_token"),
            searxng_verify_tls=bool(searxng.get("verify_tls", True)),
            search_timeout=search_timeout,
            wikipedia_search_timeout=self._get_int(
                "web_tools", "wikipedia_search_timeout", search_timeout
            ),
            fetch_transport=self._get("web_tools", "fetch_transport", "auto"),
            fetch_fallback_transport=self._get(
                "web_tools", "fetch_fallback_transport", "curl"
            ),
            fetch_impersonate=self._get("web_tools", "fetch_impersonate", "chrome"),
            fetch_max_workers=self._get_int("web_tools", "fetch_max_workers", 3),
            fetch_retry_count=self._get_int("web_tools", "fetch_retry_count", 2),
            fetch_retry_backoff_seconds=self._get_float(
                "web_tools", "fetch_retry_backoff_seconds", 1.0
            ),
            fetch_min_text_chars=self._get_int("web_tools", "fetch_min_text_chars", 200),
            fetch_enable_readability_fallback=self._get_bool(
                "web_tools", "fetch_enable_readability_fallback", True
            ),
            fetch_block_markers=_clean_list(self._get("web_tools", "fetch_block_markers", [])),
        )
```

Update `config/config-misc.yaml` so the active block becomes:

```yaml
web_tools:
  provider: searxng

  searxng:
    base_url: http://127.0.0.1:8080
    search_path: /search
    timeout: 15
    format: json
    language: all
    safe_search: 0
    categories: []
    engines: []
    api_token: null
    verify_tls: true

  proxy: "http://127.0.0.1:10808"
  enable_wikipedia: true

  search_timeout: 15
  wikipedia_search_timeout: 15

  fetch_transport: curl_cffi
  fetch_fallback_transport: curl
  fetch_impersonate: chrome
  fetch_retry_count: 1
  fetch_retry_backoff_seconds: 1.0
  fetch_min_text_chars: 200
  fetch_enable_readability_fallback: true
  fetch_block_markers:
    - captcha
    - just a moment
    - enable javascript
    - access denied
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_web_tools_config.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/config.py config/config-misc.yaml tests/test_web_tools_config.py
git commit -m "refactor: model web tools around searxng provider"
```

### Task 3: Replace active search provider logic with SearXNG JSON search

**Files:**
- Modify: `backend/tools/web_tools.py`
- Create: `tests/test_searxng_search.py`
- Modify: `backend/tools/search_and_fetch.py` if needed

- [ ] **Step 1: Write the failing test**

Create `tests/test_searxng_search.py`:

```python
from unittest.mock import MagicMock, patch


class TestSearXNGSearch:
    @patch("backend.tools.web_tools._get_config")
    @patch("backend.tools.web_tools.httpx.Client")
    def test_web_search_uses_searxng_json_results(self, mock_client_cls, mock_get_config):
        from backend.tools.web_tools import web_search

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "results": [
                {
                    "title": "Example Result",
                    "url": "https://example.com/article",
                    "content": "Example snippet",
                }
            ]
        }
        response.raise_for_status.return_value = None
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.get.return_value = response
        mock_client_cls.return_value = client
        mock_get_config.return_value = {
            "provider": "searxng",
            "proxy": None,
            "searxng_base_url": "http://127.0.0.1:8080",
            "searxng_search_path": "/search",
            "searxng_timeout": 15,
            "searxng_format": "json",
            "searxng_language": "all",
            "searxng_safe_search": 0,
            "searxng_categories": [],
            "searxng_engines": [],
            "searxng_api_token": None,
            "searxng_verify_tls": True,
        }

        result = web_search("llm deployment", max_results=5, timeout=10, use_fallback=False)

        assert "## Search Results" in result
        assert "[Example Result](https://example.com/article)" in result
        assert "Example snippet" in result

    @patch("backend.tools.web_tools._get_config")
    @patch("backend.tools.web_tools.httpx.Client")
    def test_web_search_reports_invalid_json_as_search_failure(self, mock_client_cls, mock_get_config):
        from backend.tools.web_tools import web_search

        response = MagicMock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.side_effect = ValueError("invalid json")
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.get.return_value = response
        mock_client_cls.return_value = client
        mock_get_config.return_value = {
            "provider": "searxng",
            "proxy": None,
            "searxng_base_url": "http://127.0.0.1:8080",
            "searxng_search_path": "/search",
            "searxng_timeout": 15,
            "searxng_format": "json",
            "searxng_language": "all",
            "searxng_safe_search": 0,
            "searxng_categories": [],
            "searxng_engines": [],
            "searxng_api_token": None,
            "searxng_verify_tls": True,
        }

        result = web_search("llm deployment", max_results=5, timeout=10, use_fallback=False)

        assert result == "No search results found. The search service may be temporarily unavailable."

    @patch("backend.tools.web_tools._get_config")
    @patch("backend.tools.web_tools.httpx.Client")
    def test_web_search_skips_malformed_results_but_keeps_valid_ones(self, mock_client_cls, mock_get_config):
        from backend.tools.web_tools import web_search

        response = MagicMock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "results": [
                {"title": "Broken result", "content": "missing url"},
                {
                    "title": "Valid result",
                    "url": "https://example.com/valid",
                    "content": "valid snippet",
                },
            ]
        }
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.get.return_value = response
        mock_client_cls.return_value = client
        mock_get_config.return_value = {
            "provider": "searxng",
            "proxy": None,
            "searxng_base_url": "http://127.0.0.1:8080",
            "searxng_search_path": "/search",
            "searxng_timeout": 15,
            "searxng_format": "json",
            "searxng_language": "all",
            "searxng_safe_search": 0,
            "searxng_categories": [],
            "searxng_engines": [],
            "searxng_api_token": None,
            "searxng_verify_tls": True,
        }

        result = web_search("llm deployment", max_results=5, timeout=10, use_fallback=False)

        assert "[Valid result](https://example.com/valid)" in result
        assert "Broken result" not in result
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_searxng_search.py -q
```

Expected: FAIL because `web_search()` still uses the old provider-specific implementation.

- [ ] **Step 3: Write minimal implementation**

At the top of `backend/tools/web_tools.py`, add the missing import:

```python
import httpx
```

Replace the old provider-specific runtime path with a SearXNG-only configuration helper and request path:

```python
def _get_config() -> dict:
    """Get web_tools config from config file."""
    try:
        config = get_config()
        return {
            "provider": config.web_tools.provider,
            "proxy": config.web_tools.proxy,
            "searxng_base_url": config.web_tools.searxng_base_url,
            "searxng_search_path": config.web_tools.searxng_search_path,
            "searxng_timeout": config.web_tools.searxng_timeout,
            "searxng_format": config.web_tools.searxng_format,
            "searxng_language": config.web_tools.searxng_language,
            "searxng_safe_search": config.web_tools.searxng_safe_search,
            "searxng_categories": config.web_tools.searxng_categories,
            "searxng_engines": config.web_tools.searxng_engines,
            "searxng_api_token": config.web_tools.searxng_api_token,
            "searxng_verify_tls": config.web_tools.searxng_verify_tls,
            "fetch_transport": config.web_tools.fetch_transport,
            "fetch_impersonate": config.web_tools.fetch_impersonate,
        }
    except Exception:
        return {
            "provider": "searxng",
            "proxy": None,
            "searxng_base_url": "http://127.0.0.1:8080",
            "searxng_search_path": "/search",
            "searxng_timeout": 15,
            "searxng_format": "json",
            "searxng_language": "all",
            "searxng_safe_search": 0,
            "searxng_categories": [],
            "searxng_engines": [],
            "searxng_api_token": None,
            "searxng_verify_tls": True,
            "fetch_transport": "auto",
            "fetch_impersonate": "chrome",
        }


def _normalize_searxng_result(item: dict) -> Optional[dict]:
    title = str(item.get("title") or "").strip()
    url = str(item.get("url") or item.get("link") or "").strip()
    description = str(item.get("content") or item.get("snippet") or "").strip()
    if not title or not url:
        return None
    return {
        "title": title,
        "url": url,
        "description": description,
    }


def _searxng_search(query: str, max_results: int, timeout: int, check_abort=None) -> List[dict]:
    cfg = _get_config()
    base_url = str(cfg.get("searxng_base_url") or "http://127.0.0.1:8080").rstrip("/")
    search_path = str(cfg.get("searxng_search_path") or "/search")
    if not search_path.startswith("/"):
        search_path = "/" + search_path
    url = f"{base_url}{search_path}"

    headers = {}
    api_token = cfg.get("searxng_api_token")
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    params = {
        "q": query,
        "format": "json",
        "language": cfg.get("searxng_language", "all"),
        "safesearch": cfg.get("searxng_safe_search", 0),
    }
    categories = cfg.get("searxng_categories") or []
    if categories:
        params["categories"] = ",".join(categories)
    engines = cfg.get("searxng_engines") or []
    if engines:
        params["engines"] = ",".join(engines)

    proxies = None
    if cfg.get("proxy"):
        proxies = {"http://": cfg["proxy"], "https://": cfg["proxy"]}

    if check_abort and check_abort():
        raise AbortError("Search aborted by user")

    with httpx.Client(
        timeout=timeout or int(cfg.get("searxng_timeout") or 15),
        verify=bool(cfg.get("searxng_verify_tls", True)),
        proxy=cfg.get("proxy") or None,
        headers=headers or None,
    ) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()

    results = []
    for item in payload.get("results", [])[:max_results]:
        normalized = _normalize_searxng_result(item)
        if normalized:
            results.append(normalized)
    return results
```

Update `web_search()` to only use the new provider path and keep the public markdown contract unchanged:

```python
@tool
def web_search(
    query: str,
    max_results: int = 10,
    timeout: int = 30,
    use_fallback: bool = True,
    check_abort: callable = None,
) -> str:
    """[Internal] Low-level web search backend. Do NOT call this directly."""
    del use_fallback
    timeout = timeout or 30

    if check_abort and check_abort():
        return "搜索已被用户中止"

    try:
        results = _searxng_search(query, max_results=max_results, timeout=timeout, check_abort=check_abort)
    except AbortError:
        return "搜索已被用户中止"
    except Exception:
        return "No search results found. The search service may be temporarily unavailable."

    if not results:
        return "No search results found. The search service may be temporarily unavailable."

    output = "## Search Results\n\n"
    for r in results[:max_results]:
        output += f"- [{r['title']}]({r['url']})\n"
        if r["description"]:
            output += f"  {r['description']}\n"
        output += "\n"
    return output
```

Leave `search_and_fetch.py` unchanged unless the new output shape forces a minimal adaptation.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_searxng_search.py -q
```

Expected: PASS.

- [ ] **Step 5: Remove obsolete old-provider search tests**

Delete the now-obsolete `TestWebSearchTransport` cases in `tests/test_web_fetch.py` that exercise old-provider search request paths, but keep the fetch robustness tests intact.

- [ ] **Step 6: Run the search-related suite**

Run:

```bash
PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_web_fetch.py tests/test_searxng_search.py tests/test_web_tools_config.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/tools/web_tools.py backend/tools/search_and_fetch.py tests/test_web_fetch.py tests/test_searxng_search.py tests/test_web_tools_config.py
git commit -m "refactor: route active web search through searxng"
```

### Task 4: Add backend-managed delayed SearXNG startup orchestration

**Files:**
- Modify: `scripts/2_start_backend.sh`
- Create: `deploy/searxng/docker-compose.yml`
- Create: `deploy/searxng/.env.example`
- Create: `deploy/searxng/core-config/settings.yml.example`
- Create: `tests/test_start_backend_searxng.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_start_backend_searxng.py` with script-logic extraction tests that define the desired orchestration API before shell editing. Add this file:

```python
from backend.tools.start_backend_searxng import (
    classify_searxng_failure_message,
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
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_start_backend_searxng.py -q
```

Expected: FAIL because `backend/tools/start_backend_searxng.py` does not exist yet.

- [ ] **Step 3: Write minimal implementation helper module**

Create `backend/tools/start_backend_searxng.py`:

```python
"""Helpers for backend-managed SearXNG startup orchestration."""


def should_start_searxng_after_backend_ready(
    backend_ready: bool,
    docker_available: bool,
) -> bool:
    return backend_ready and docker_available


def classify_searxng_failure_message(reason: str) -> str:
    return (
        f"⚠️ SearXNG 未就绪：{reason}。"
        "你现在仍然可以去开启前端；当前仅网络搜索功能受影响，非搜索功能保持可用。"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_start_backend_searxng.py -q
```

Expected: PASS.

- [ ] **Step 5: Add the deployment assets**

Create `deploy/searxng/docker-compose.yml`:

```yaml
name: searxng

services:
  core:
    container_name: searxng-core
    image: docker.io/searxng/searxng:${SEARXNG_VERSION:-latest}
    restart: always
    ports:
      - ${SEARXNG_HOST:-127.0.0.1}:${SEARXNG_PORT:-8080}:8080
    env_file: ./.env
    volumes:
      - ./core-config/:/etc/searxng/:Z
      - core-data:/var/cache/searxng/

  valkey:
    container_name: searxng-valkey
    image: docker.io/valkey/valkey:9-alpine
    command: valkey-server --save 30 1 --loglevel warning
    restart: always
    volumes:
      - valkey-data:/data/

volumes:
  core-data:
  valkey-data:
```

Create `deploy/searxng/.env.example`:

```dotenv
# Backend-managed local SearXNG instance
SEARXNG_VERSION=latest
SEARXNG_HOST=127.0.0.1
SEARXNG_PORT=8080
```

Create `deploy/searxng/core-config/settings.yml.example`:

```yaml
use_default_settings: true

general:
  debug: false
  instance_name: "LLM-deployment local search"

search:
  safe_search: 0
  autocomplete: ""
  favicon_resolver: ""
  default_lang: ""
  ban_time_on_fail: 5
  max_ban_time_on_fail: 120
  formats:
    - html
    - json

server:
  secret_key: "change-this-in-real-deployments"
  limiter: false
  image_proxy: false

redis:
  url: redis://valkey:6379/0
```

- [ ] **Step 6: Edit `scripts/2_start_backend.sh` to orchestrate delayed startup**

Make the following focused structural changes:

1. Add variables near the top after `cd "$PROJECT_ROOT"`:

```bash
SEARXNG_DEPLOY_DIR="$PROJECT_ROOT/deploy/searxng"
SEARXNG_ENV_FILE="$SEARXNG_DEPLOY_DIR/.env"
SEARXNG_SETTINGS_FILE="$SEARXNG_DEPLOY_DIR/core-config/settings.yml"
SEARXNG_BASE_URL_DEFAULT="http://127.0.0.1:8080"
SEARXNG_STACK_OWNED_BY_SESSION=0
BACKEND_PID=""
```

2. Add helper functions before `cleanup()`:

```bash
docker_compose_cmd() {
    if docker compose version >/dev/null 2>&1; then
        printf 'docker compose'
        return 0
    fi
    if command -v docker-compose >/dev/null 2>&1; then
        printf 'docker-compose'
        return 0
    fi
    return 1
}

wait_for_backend_ready() {
    local url="http://127.0.0.1:${BACKEND_PORT}/health"
    local attempts=0
    while [ "$attempts" -lt 60 ]; do
        if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
        attempts=$((attempts + 1))
    done
    return 1
}

wait_for_searxng_ready() {
    local url="${SEARXNG_BASE_URL_OVERRIDE:-$SEARXNG_BASE_URL_DEFAULT}/search?q=ping&format=json"
    local attempts=0
    while [ "$attempts" -lt 120 ]; do
        if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
        attempts=$((attempts + 1))
    done
    return 1
}

prepare_searxng_files() {
    mkdir -p "$SEARXNG_DEPLOY_DIR/core-config"
    [ -f "$SEARXNG_ENV_FILE" ] || cp "$SEARXNG_DEPLOY_DIR/.env.example" "$SEARXNG_ENV_FILE"
    [ -f "$SEARXNG_SETTINGS_FILE" ] || cp "$SEARXNG_DEPLOY_DIR/core-config/settings.yml.example" "$SEARXNG_SETTINGS_FILE"
}

start_searxng_stack() {
    local compose_cmd
    if ! command -v docker >/dev/null 2>&1; then
        echo "⚠️ Docker 不可用；你现在仍然可以去开启前端。当前仅网络搜索功能受影响，非搜索功能保持可用。"
        return 1
    fi
    if ! compose_cmd="$(docker_compose_cmd)"; then
        echo "⚠️ Docker Compose 不可用；你现在仍然可以去开启前端。当前仅网络搜索功能受影响，非搜索功能保持可用。"
        return 1
    fi

    prepare_searxng_files
    echo "⏳ 正在准备 SearXNG（首次可能需要下载容器镜像，时间较长）..."
    (cd "$SEARXNG_DEPLOY_DIR" && $compose_cmd up -d --pull always) || return 1
    SEARXNG_STACK_OWNED_BY_SESSION=1
    return 0
}

stop_searxng_stack_if_owned() {
    [ "$SEARXNG_STACK_OWNED_BY_SESSION" -eq 1 ] || return 0
    local compose_cmd
    compose_cmd="$(docker_compose_cmd)" || return 0
    (cd "$SEARXNG_DEPLOY_DIR" && $compose_cmd down) || true
}
```

3. Replace the current trap/launch tail with foreground orchestration that preserves `Ctrl+C` ownership:

```bash
cleanup() {
    echo ""
    echo "🛑 接收到终止信号，正在关闭后端服务..."
    if [ -n "$BACKEND_PID" ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
        kill -TERM "$BACKEND_PID" 2>/dev/null || true
        wait "$BACKEND_PID" 2>/dev/null || true
    fi
    echo "🛑 正在关闭 SearXNG 服务..."
    stop_searxng_stack_if_owned
    exit 0
}

trap cleanup SIGINT SIGTERM SIGHUP

python3 -m uvicorn backend.main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" --workers "$BACKEND_WORKERS" &
BACKEND_PID=$!

if wait_for_backend_ready; then
    echo "✅ 后端已启动: http://127.0.0.1:${BACKEND_PORT}"
    echo "👉 现在可以去开启前端了。"
    echo "ℹ️ 如果 SearXNG 尚未就绪，仅网络搜索功能受影响，非搜索功能保持可用。"

    if start_searxng_stack; then
        if wait_for_searxng_ready; then
            echo "✅ SearXNG 已可用: ${SEARXNG_BASE_URL_OVERRIDE:-$SEARXNG_BASE_URL_DEFAULT}"
        else
            echo "⚠️ SearXNG 启动后未通过健康检查；你现在仍然可以去开启前端。当前仅网络搜索功能受影响，非搜索功能保持可用。"
        fi
    else
        echo "⚠️ SearXNG 启动失败；你现在仍然可以去开启前端。当前仅网络搜索功能受影响，非搜索功能保持可用。"
    fi
else
    echo "❌ 后端健康检查未通过，终止启动流程。"
    kill -TERM "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
    exit 1
fi

wait "$BACKEND_PID"
stop_searxng_stack_if_owned
```

This keeps backend startup in the foreground while still making delayed SearXNG startup visible in the same terminal.

- [ ] **Step 7: Run tests to verify the helper module still passes**

Run:

```bash
PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_start_backend_searxng.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add scripts/2_start_backend.sh deploy/searxng/docker-compose.yml deploy/searxng/.env.example deploy/searxng/core-config/settings.yml.example backend/tools/start_backend_searxng.py tests/test_start_backend_searxng.py
git commit -m "feat: delay searxng startup until backend is ready"
```

### Task 5: Document the new backend-managed search service flow

**Files:**
- Modify: `README.md`
- Create: `docs/searxng-backend-managed-setup.md`

- [ ] **Step 1: Write the failing doc expectation test**

Create `tests/test_searxng_docs.py`:

```python
from pathlib import Path


def test_readme_mentions_backend_managed_searxng_without_old_provider_names():
    text = Path("README.md").read_text(encoding="utf-8")
    assert "SearXNG" in text
    assert "duckduckgo" not in text.lower()
    assert "ddg" not in text.lower()


def test_searxng_tutorial_mentions_delayed_startup_and_frontend_can_start_early():
    text = Path("docs/searxng-backend-managed-setup.md").read_text(encoding="utf-8")
    assert "backend" in text
    assert "8080" in text
    assert "前端" in text
    assert "搜索" in text
    assert "非搜索" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_searxng_docs.py -q
```

Expected: FAIL because the new tutorial file does not exist yet and README has not been updated.

- [ ] **Step 3: Update README minimally**

Add one concise current-state note near the deployment/startup section of `README.md` in Chinese:

```md
> 当前版本的联网搜索能力依赖一个由后端启动脚本自动托管的本地 SearXNG 服务。首次启动后端时，脚本可能需要额外下载搜索服务容器镜像；在此过程中，你仍然可以先开启前端并使用除网络搜索之外的功能。详细说明见 `docs/searxng-backend-managed-setup.md`。
```

- [ ] **Step 4: Write the beginner tutorial**

Create `docs/searxng-backend-managed-setup.md` with this content:

```md
# Backend 托管的 SearXNG 启动说明

## 这是什么

本项目当前的联网搜索能力依赖一个本地自部署的 SearXNG 服务。
你不需要单独手动启动它：`scripts/2_start_backend.sh` 会在后端完全启动后，再自动拉起这个搜索服务。

## 默认端口

- 后端 API: `8090`
- 前端开发端口: `3000`
- 推理服务: `30000`
- SearXNG: `8080`

SearXNG 使用官方默认端口 `8080`，后端改为 `8090` 以避免冲突。

## 第一次使用前的准备

你需要先安装：

- Docker
- Docker Compose（`docker compose` 或 `docker-compose` 任一可用即可）

## 如何启动

直接像平时一样启动后端：

```bash
bash scripts/2_start_backend.sh
```

启动顺序是：

1. 后端先启动
2. 后端健康检查通过
3. 终端提示你现在可以去开启前端了
4. 后端脚本继续自动准备并启动 SearXNG

## 为什么第一次可能很慢

如果你的机器以前没有拉取过相关容器镜像，后端终端里会出现较长时间的下载输出。
这是正常现象。

即使此时搜索服务还没准备好，只要后端已经启动成功，你就已经可以去开启前端。

## 当搜索服务还没准备好时会怎样

如果 SearXNG 仍在下载、启动失败、或者暂时不可用：

- 你仍然可以去开启前端
- 当前仅网络搜索功能受影响
- 所有非搜索功能仍然可以正常使用

## 如何确认搜索服务正常

搜索服务正常后，后端终端会打印 SearXNG 可用提示。

你也可以手动访问：

```bash
curl "http://127.0.0.1:8080/search?q=ping&format=json"
```

## 如何停止

在运行 `scripts/2_start_backend.sh` 的同一个终端按下 `Ctrl+C`。

这会：

1. 安全关闭后端
2. 安全关闭由本次后端会话拉起的 SearXNG 服务

## 常见问题

### 1. 后端已经起来了，但搜索服务还没好

这是允许的。
你现在就可以去开启前端；只有网络搜索不可用，非搜索功能不受影响。

### 2. 提示 Docker 或 Docker Compose 不可用

这说明后端无法自动托管搜索服务。
此时后端仍可用，但网络搜索功能不可用。先安装或修复 Docker / Docker Compose，再重新启动后端。

### 3. 8080 端口冲突

请先释放本地 `8080`，或修改 `deploy/searxng/.env` 中的端口映射后重新启动后端。
```

- [ ] **Step 5: Run doc tests to verify they pass**

Run:

```bash
PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_searxng_docs.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add README.md docs/searxng-backend-managed-setup.md tests/test_searxng_docs.py
git commit -m "docs: explain backend-managed searxng setup"
```

### Task 6: Final cleanup and verification

**Files:**
- Modify: `requirements_for_sglang.txt` only if `chardet` is confirmed removable
- Verify: all files changed above

- [ ] **Step 1: Decide `chardet` status explicitly**

Inspect whether `chardet` is directly imported anywhere in the repository.

Run:

```bash
rg "import chardet|from chardet" -n .
```

Expected: likely no direct hits in active repository code.

If there are still no direct imports and no known startup/runtime dependency path discovered during implementation, remove the line from `requirements_for_sglang.txt`:

```txt
chardet>=7.4.3
```

If a real dependency is discovered, keep it and add a one-line comment explaining why.

- [ ] **Step 2: Run focused verification suite**

Run:

```bash
PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_web_tools_config.py tests/test_web_fetch.py tests/test_searxng_search.py tests/test_start_backend_searxng.py tests/test_searxng_docs.py -q
```

Expected: PASS.

- [ ] **Step 3: Run a manual backend-start smoke test in the implementation worktree**

From `./.worktrees/searXNG/`, run:

```bash
bash scripts/2_start_backend.sh
```

Verify manually in terminal output that:

- backend reaches `/health`
- the terminal says the user can now go start the frontend
- the terminal shows visible SearXNG pull/start progress on first run
- degraded messaging says only search is affected if SearXNG fails
- `Ctrl+C` shuts down backend and the owned SearXNG stack

- [ ] **Step 4: Run a config/doc hygiene scan**

Run:

```bash
rg "duckduckgo|bing|sogou|360|ddg|ddgs" config README.md tests backend/tools/web_tools.py backend/config.py
```

Expected: no hits in active config/README/runtime-path files introduced by this migration work. If tests or runtime files still contain old names, finish removing them before completion.

- [ ] **Step 5: Commit**

```bash
git add backend/config.py backend/tools/web_tools.py backend/tools/search_and_fetch.py scripts/2_start_backend.sh deploy/searxng README.md docs/searxng-backend-managed-setup.md tests requirements_for_sglang.txt
git commit -m "feat: migrate active search flow to backend-managed searxng"
```

---

## Self-Review Checklist

- Spec coverage:
  - SearXNG-only active provider path: Tasks 2, 3
  - New config model: Task 2
  - Backend-managed delayed startup: Task 4
  - `Ctrl+C` shutdown ownership: Task 4
  - Beginner tutorial and README note: Task 5
  - Robust error handling / no old-provider fallback: Tasks 3, 4, 6
  - Terminal UX for frontend-early start and degraded messaging: Task 4, Task 5
  - Worktree path requirement: Task 1 and Task 6
- Placeholder scan:
  - No `TODO`, `TBD`, or “handle appropriately” placeholders remain.
- Type consistency:
  - New config names stay consistent: `provider`, `searxng_base_url`, `searxng_search_path`, `searxng_timeout`, `searxng_format`, `searxng_language`, `searxng_safe_search`, `searxng_categories`, `searxng_engines`, `searxng_api_token`, `searxng_verify_tls`.
