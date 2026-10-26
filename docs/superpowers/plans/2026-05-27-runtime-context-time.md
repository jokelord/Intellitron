# Runtime Context Time Injection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add minute-level runtime time context as a separate signal from `system prompt`, expose its enablement from `config/config-misc.yaml`, skip `coordinator_1`, and remove stale `coordinator_agent` configuration usage.

**Architecture:** Add a small runtime-context helper in the backend, surface the enable flag through `/config`, and thread the runtime context through the existing chat and agent call paths without mutating the user/system prompt string. Keep `coordinator_1` as the only profile that never receives the time context. Remove the unused `coordinator_agent` top-level YAML entry, then report any remaining backend references without deleting them.

**Tech Stack:** Python, FastAPI, smolagents, Next.js 14, React, TypeScript, pytest, node:test.

---

## File Map

### Backend files to modify
- `backend/config.py`
  - Add a misc-config field for the time-runtime-context enable flag and parse it from `config/config-misc.yaml`.
  - Stop exposing `coordinator_agent` as a first-class agent prompt profile.
- `backend/main.py`
  - Expose the time-runtime-context enable flag in `/config`.
- `backend/agent/agent_runner.py`
  - Generate minute-level runtime context, attach it to `basic_agent` / `coordinator_2`, skip `coordinator_1`, and keep prompt strings untouched.
- `backend/llm_service.py`
  - Accept runtime-context metadata where needed without changing existing prompt behavior.
- `backend/api/chat.py`
  - Thread the runtime-context signal for non-agent chat requests.
- `backend/api/agent.py`
  - Thread the runtime-context signal for agent requests if required by the final implementation shape.

### Frontend files to modify
- `frontend/src/lib/api.ts`
  - Extend `ConfigResponse` and request helpers to carry the new runtime-context enable signal if the frontend needs it.
- `frontend/src/app/page.tsx`
  - Keep `systemPrompt` unchanged; consume the runtime-context signal separately for non-agent mode.
- `frontend/src/app/vision/page.tsx`
  - Mirror the same behavior as the text page.

### Config files to modify
- `config/config-misc.yaml`
  - Add the new time-runtime-context enable flag in the misc section.
- `config/config-agent-prompts.yaml`
  - Remove the top-level `coordinator_agent` entry.

### Tests to modify/create
- `tests/test_runtime_config_limits.py`
  - Verify `/config` exposes the new misc flag.
- `tests/test_multi_agent_config.py`
  - Verify the misc flag parses and `coordinator_agent` is no longer required in prompt config parsing.
- `tests/test_get_tools_no_load_images.py`
  - Update profile construction tests if they still assume `coordinator_agent` is a supported prompt profile.
- `tests/test_multi_agent_runner.py`
  - Verify time runtime context is attached to `coordinator_2` / `basic_agent` and skipped for `coordinator_1`.
- `frontend/src/lib/api.test.mts`
  - Add request/config helper coverage if the frontend API shape changes.

---

### Task 1: Add Misc Config Flag And Surface It Through `/config`

**Files:**
- Modify: `backend/config.py`
- Modify: `backend/main.py`
- Modify: `config/config-misc.yaml`
- Test: `tests/test_runtime_config_limits.py`
- Test: `tests/test_multi_agent_config.py`

- [ ] **Step 1: Write the failing test**

Add a config endpoint assertion and a config parser assertion.

```python
def test_config_endpoint_exposes_runtime_time_context_flag(monkeypatch):
    class FakeService:
        active_backend = "sglang"
        is_ready = True

    config.misc.enable_runtime_time_context = True
    monkeypatch.setattr("backend.main.get_llm_service", lambda: FakeService())

    client = TestClient(app)
    response = client.get("/config")

    assert response.status_code == 200
    assert response.json()["misc"]["enable_runtime_time_context"] is True
```

```python
def test_parse_misc_config_exposes_runtime_time_context_flag():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {"misc": {"enable_runtime_time_context": True}}
    loader._agent_prompts_data = {}

    cfg = loader._parse_misc_config()

    assert cfg.enable_runtime_time_context is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_runtime_config_limits.py::test_config_endpoint_exposes_runtime_time_context_flag tests/test_multi_agent_config.py::test_parse_misc_config_exposes_runtime_time_context_flag -q`

Expected: FAIL because `MiscConfig` and `/config` do not yet expose the flag.

- [ ] **Step 3: Write minimal implementation**

Add the misc config field and expose it from `/config`.

```python
@dataclass
class MiscConfig:
    """Phase 1 杂项配置"""

    enable_runtime_time_context: bool = False
```

```python
def _parse_misc_config(self) -> MiscConfig:
    return MiscConfig(
        enable_runtime_time_context=self._get_bool(
            "misc", "enable_runtime_time_context", False
        )
        or False,
    )
```

```python
"misc": {
    "enable_runtime_time_context": config.misc.enable_runtime_time_context,
},
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_runtime_config_limits.py::test_config_endpoint_exposes_runtime_time_context_flag tests/test_multi_agent_config.py::test_parse_misc_config_exposes_runtime_time_context_flag -q`

Expected: PASS

### Task 2: Generate Minute-Level Runtime Context In Backend

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Modify: `backend/llm_service.py`
- Test: `tests/test_multi_agent_runner.py`

- [ ] **Step 1: Write the failing test**

Add a profile-specific assertion for runtime context visibility.

```python
def test_profile_runtime_context_skips_coordinator_1_but_reaches_basic_and_coordinator_2():
    runner = _make_runner()
    coordinator_1 = runner._build_profile_agent("coordinator_1")
    coordinator_2 = runner._build_profile_agent("coordinator_2")
    basic = runner._build_profile_agent("basic_agent")

    assert getattr(coordinator_1, "runtime_context", None) is None
    assert getattr(coordinator_2, "runtime_context", None)
    assert getattr(basic, "runtime_context", None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py::test_profile_runtime_context_skips_coordinator_1_but_reaches_basic_and_coordinator_2 -q`

Expected: FAIL because runtime context does not exist yet.

- [ ] **Step 3: Write minimal implementation**

Add a backend helper and attach runtime context per profile.

```python
def _build_runtime_time_context() -> str:
    now = datetime.now()
    return now.strftime("Current local time: %A, %Y-%m-%d %H:%M")
```

```python
def _profile_uses_runtime_time_context(profile_name: str) -> bool:
    return profile_name in {"coordinator_2", "basic_agent"}
```

```python
if _profile_uses_runtime_time_context(profile_name) and self.config.misc.enable_runtime_time_context:
    kwargs["runtime_context"] = _build_runtime_time_context()
```

```python
request_data["runtime_context"] = runtime_context
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py::test_profile_runtime_context_skips_coordinator_1_but_reaches_basic_and_coordinator_2 -q`

Expected: PASS

### Task 3: Remove `coordinator_agent` From Prompt Config

**Files:**
- Modify: `config/config-agent-prompts.yaml`
- Modify: `backend/config.py`
- Modify: `backend/agent/agent_runner.py`
- Test: `tests/test_multi_agent_config.py`
- Test: `tests/test_get_tools_no_load_images.py`

- [ ] **Step 1: Write the failing test**

Update the real-config test to stop requiring `coordinator_agent` as a top-level prompt profile.

```python
def test_real_config_files_use_multi_agent_prompt_layout():
    cfg = ConfigLoader().load(reload=True)

    assert cfg.agent.entry_agent_name == "coordinator_1"
    assert cfg.agent.enabled_agent_profiles == ["coordinator_1", "coordinator_2", "basic_agent"]
    assert cfg.agent_prompts.coordinator_1
    assert cfg.agent_prompts.coordinator_2
    assert cfg.agent_prompts.basic_agent
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_config.py::test_real_config_files_use_multi_agent_prompt_layout -q`

Expected: FAIL until `coordinator_agent` is removed from YAML and parser assumptions are updated.

- [ ] **Step 3: Write minimal implementation**

Remove the YAML top-level `coordinator_agent` entry and drop first-class parsing/storage if it is no longer used.

```yaml
# remove coordinator_agent section entirely
```

```python
known_keys = {
    "coordinator_1",
    "coordinator_2",
    "basic_agent",
    "explorer_agent",
    "auditor_agent",
    "vlm_instructions",
}
```

```python
PROFILE_TOOL_ALLOWLIST = {
    "coordinator_1": set(),
    "coordinator_2": set(),
    "basic_agent": None,
    "explorer_agent": set(),
    "auditor_agent": set(),
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_config.py::test_real_config_files_use_multi_agent_prompt_layout tests/test_get_tools_no_load_images.py -q`

Expected: PASS

### Task 4: Keep Frontend Settings Stable And Carry Runtime Context Separately

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`
- Test: `frontend/src/lib/api.test.mts`

- [ ] **Step 1: Write the failing test**

Add a client-side API test to ensure config data can surface the new misc flag without changing `systemPrompt`.

```ts
test('getConfig exposes runtime time context flag without mutating system prompt defaults', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  globalThis.fetch = async () => new Response(JSON.stringify({
    misc: { enable_runtime_time_context: true },
    inference: { temperature: 0.6, top_p: 0.95, top_k: 50, max_tokens: 4096, repetition_penalty: 1.0, stream: true },
  }), { status: 200, headers: { 'Content-Type': 'application/json' } });

  const data = await llmClient.getConfig();
  assert.equal(data.misc?.enable_runtime_time_context, true);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test --experimental-strip-types frontend/src/lib/api.test.mts`

Expected: FAIL because `ConfigResponse` and/or `getConfig` do not yet include `misc`.

- [ ] **Step 3: Write minimal implementation**

Extend the TS response type and keep `systemPrompt` untouched.

```ts
misc?: {
  enable_runtime_time_context?: boolean;
};
```

```ts
// do not rewrite settings.systemPrompt; carry runtime context separately
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test --experimental-strip-types frontend/src/lib/api.test.mts`

Expected: PASS

### Task 5: Verification Sweep And Dead-Code Report

**Files:**
- Modify: none unless a test fix is needed
- Test: `tests/test_multi_agent_runner.py`, `tests/test_multi_agent_config.py`, `tests/test_runtime_config_limits.py`, `frontend/src/lib/api.test.mts`

- [ ] **Step 1: Run backend tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_runtime_config_limits.py tests/test_multi_agent_config.py tests/test_multi_agent_runner.py tests/test_get_tools_no_load_images.py -q`

- [ ] **Step 2: Run frontend tests**

Run: `node --test --experimental-strip-types frontend/src/lib/api.test.mts`

- [ ] **Step 3: Report remaining coordinator_agent references**

If backend code still references `coordinator_agent`, report the exact files and line ranges to the user without deleting anything.
