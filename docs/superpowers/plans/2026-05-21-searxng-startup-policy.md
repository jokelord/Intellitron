# SearXNG Startup Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `scripts/2_start_backend.sh` reuse existing SearXNG instances first, always attempt repository-managed startup when none is reachable, and print tutorial-grade setup guidance when managed startup fails.

**Architecture:** Keep the shell script as the runtime orchestrator, but move policy decisions and failure-message formatting into `backend/tools/start_backend_searxng.py` so tests can pin behavior without shell-only assertions. Update docs and tests around the same policy instead of creating a separate startup path.

**Tech Stack:** Bash, Python 3.12, pytest, YAML docs/configs

---

### Task 1: Add Failing Tests for Guided Failure Output

**Files:**
- Modify: `tests/test_start_backend_searxng.py`
- Modify: `tests/test_searxng_docs.py`

- [ ] **Step 1: Write the failing test for tutorial guidance text**

```python
def test_failure_message_includes_guided_setup_steps():
    message = classify_searxng_failure_message("Docker Compose 不可用")
    assert "./dc" in message
    assert "chmod +x ./dc" in message
    assert "./dc up -d" in message
    assert "./dc logs -f core" in message
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_start_backend_searxng.py -q`
Expected: FAIL because the current failure message does not yet embed the guided setup steps.

- [ ] **Step 3: Add doc expectation for the new operator policy**

```python
def test_tutorial_mentions_reuse_first_managed_fallback_and_guided_setup():
    text = Path("docs/searxng-backend-managed-setup.md").read_text(encoding="utf-8")
    assert "复用" in text
    assert "托管启动" in text
    assert "./dc up -d" in text
    assert "./dc logs -f core" in text
```
```

- [ ] **Step 4: Run tests to verify the new expectations fail before implementation**

Run: `~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_start_backend_searxng.py tests/test_searxng_docs.py -q`
Expected: FAIL on the newly added assertions.

### Task 2: Implement Tutorial-Guided Failure Messaging

**Files:**
- Modify: `backend/tools/start_backend_searxng.py`
- Test: `tests/test_start_backend_searxng.py`

- [ ] **Step 1: Add a helper that renders compact guided setup steps**

```python
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
```

- [ ] **Step 2: Compose the failure message from reason + availability note + guidance**

```python
def classify_searxng_failure_message(reason: str) -> str:
    return (
        f"⚠️ SearXNG 未就绪：{reason}。\n"
        "你现在仍然可以去开启前端；当前仅网络搜索功能受影响，非搜索功能保持可用。\n"
        f"{build_searxng_setup_guidance()}"
    )
```

- [ ] **Step 3: Run focused tests**

Run: `~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_start_backend_searxng.py -q`
Expected: PASS

### Task 3: Make the Shell Policy Match Reuse-First Plus Managed-Fallback

**Files:**
- Modify: `scripts/2_start_backend.sh`
- Modify: `backend/tools/start_backend_searxng.py`
- Test: `tests/test_start_backend_searxng.py`

- [ ] **Step 1: Remove the branch that refuses managed startup just because the configured target is non-local**

```bash
if is_searxng_reachable; then
    echo "✅ 检测到已运行的 SearXNG 实例: ${SEARXNG_BASE_URL}"
elif "$SHARED_WORKSPACE_ROOT/.sglang/bin/python" - <<'PYEOF'
...
PYEOF
then
    if start_searxng_stack; then
        ...
    fi
else
    print_searxng_failure_message "Docker 不可用，跳过 SearXNG 自动启动"
fi
```

- [ ] **Step 2: Keep session-owned cleanup behavior unchanged**

```bash
stop_searxng_stack_if_owned() {
    [ "$SEARXNG_STACK_OWNED_BY_SESSION" -eq 1 ] || return 0
    ...
}
```

- [ ] **Step 3: Ensure helper coverage still models compose selection and bind-port parsing**

```python
def choose_searxng_start_mode(...):
    if not backend_ready:
        return "degraded"
    if searxng_reachable:
        return "external"
    if docker_available and compose_command:
        return "managed"
    return "degraded"
```

- [ ] **Step 4: Run helper tests**

Run: `~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_start_backend_searxng.py -q`
Expected: PASS

### Task 4: Sync Active Docs With the New Operator Story

**Files:**
- Modify: `docs/searxng-backend-managed-setup.md`
- Modify: `README.md`
- Test: `tests/test_searxng_docs.py`

- [ ] **Step 1: Update the setup doc to state the exact policy**

```md
1. backend 先启动
2. backend ready 后立刻提示可以开启前端
3. 若已有可用 SearXNG，则直接复用
4. 若没有可用实例，则脚本尝试本仓库托管启动
5. 若托管失败，则打印教程式部署指引
```

- [ ] **Step 2: Keep README aligned with the same operator promise**

```md
当前版本的联网搜索能力优先复用已运行的 SearXNG；若本机没有可用实例，后端脚本会尝试托管启动，并在失败时打印部署指引。
```

- [ ] **Step 3: Run doc tests**

Run: `~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_searxng_docs.py -q`
Expected: PASS

### Task 5: Full Regression and Runtime Verification

**Files:**
- Verify: `tests/test_web_tools_config.py`
- Verify: `tests/test_searxng_search.py`
- Verify: `tests/test_start_backend_searxng.py`
- Verify: `tests/test_searxng_docs.py`
- Verify: `tests/test_search_and_fetch.py`
- Verify: `tests/test_web_fetch.py`
- Verify: `scripts/2_start_backend.sh`

- [ ] **Step 1: Run the impacted regression suite**

Run: `~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_web_tools_config.py tests/test_searxng_search.py tests/test_start_backend_searxng.py tests/test_searxng_docs.py tests/test_search_and_fetch.py tests/test_web_fetch.py tests/test_cycle_config.py tests/test_cycle_executor.py tests/test_agent_session_context.py tests/test_multi_agent_runner.py -q`
Expected: PASS

- [ ] **Step 2: Recheck shell syntax**

Run: `bash -n scripts/2_start_backend.sh && bash -n scripts/3_start_frontend.sh`
Expected: no output

- [ ] **Step 3: Smoke test reuse path**

Run: `timeout 35s bash scripts/2_start_backend.sh`
Expected: backend ready message first, then `✅ 检测到已运行的 SearXNG 实例`

- [ ] **Step 4: Smoke test managed path**

Run: `./dc down` in `deploy/searxng/`, then `timeout 90s bash scripts/2_start_backend.sh`
Expected: backend ready message first, then compose startup output, then `✅ SearXNG 已可用`

- [ ] **Step 5: Commit**

```bash
git add README.md backend/tools/start_backend_searxng.py docs/searxng-backend-managed-setup.md scripts/2_start_backend.sh tests/test_searxng_docs.py tests/test_start_backend_searxng.py
git commit -m "feat: guide searxng startup recovery"
```
