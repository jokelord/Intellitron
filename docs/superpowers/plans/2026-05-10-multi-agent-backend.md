# Multi-Agent Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the current single-agent backend into a shared-memory multi-agent runtime where every turn starts with `coordinator_agent`, optionally switches to `basic_agent`, and preserves the existing API, final-answer behavior, and text/vision compatibility.

**Architecture:** Keep one session and one shared main memory per conversation. Add agent profiles (`coordinator_agent`, `basic_agent`, plus disabled placeholders) to `config/config-agent-prompts.yaml`, introduce runtime profile selection in `AgentRunner`, add `switch_agent` command parsing ahead of normal tool parsing, and route each new turn through a non-persistent coordinator pass before optionally handing the turn to the existing tool-calling flow. Preserve current SSE/API shapes and reuse as much of `SmartToolCallingAgent`, `write_memory_to_messages()`, and stop/answer-now machinery as possible.

**Tech Stack:** Python 3.12, FastAPI, smolagents, dataclasses, pytest, unittest.mock, existing SSE streaming stack

---

## File Structure

| File | Responsibility | New/Modify |
|------|---------------|------------|
| `backend/config.py` | Expand `AgentConfig` and replace old single-agent prompt config model with multi-agent profile-aware config parsing | Modify |
| `config/config-agent-prompts.yaml` | Restructure prompts into `coordinator_agent`, `basic_agent`, `explorer_agent`, `auditor_agent`, `vlm_instructions` | Modify |
| `config/config-misc.yaml` | Add runtime multi-agent knobs such as entry agent, enabled profiles, max switches per turn | Modify |
| `backend/agent/local_llm_model.py` | Parse `switch_agent` commands before tool calls; add command normalization helpers and tests | Modify |
| `backend/agent/agent_runner.py` | Introduce profile-based agent construction, shared-memory session state, coordinator routing pass, turn reset to coordinator, and compatibility for stream/sync/answer-now | Modify |
| `tests/test_multi_tool_parsing.py` | Add `switch_agent` parsing tests using the real parser surface | Modify |
| `tests/test_agent_session_context.py` | Add session state, shared-memory, and turn-entry tests | Modify |
| `tests/test_get_tools_no_load_images.py` | Assert coordinator has no tools and basic keeps required tools (or move to new focused profile-visibility test file if needed) | Modify |
| `tests/test_agent_stop_api.py` | Add multi-agent stop/answer-now expectations where necessary | Modify |
| `tests/test_agent_api_images.py` | Add image-forwarding / vision handoff coverage for coordinator -> basic | Modify |
| `tests/test_multi_agent_runner.py` | Focused runner-level unit tests for coordinator direct answer, switch path, disabled profile, and routing isolation | New |
| `tests/test_multi_agent_config.py` | Focused config parsing tests for prompt/profile structure and agent runtime knobs | New |

---

### Task 1: Replace single-agent prompt config with multi-agent config parsing

**Files:**
- Modify: `backend/config.py`
- Modify: `config/config-agent-prompts.yaml`
- Modify: `config/config-misc.yaml`
- Test: `tests/test_multi_agent_config.py`

- [ ] Step 1: Write failing config tests for multi-agent prompt parsing and runtime knobs.

```python
# tests/test_multi_agent_config.py
from pathlib import Path

from backend.config import ConfigLoader


def test_agent_prompts_parse_multi_agent_profiles(tmp_path: Path):
    overall = tmp_path / "config-overall.yaml"
    prompts = tmp_path / "config-agent-prompts.yaml"
    misc = tmp_path / "config-misc.yaml"

    overall.write_text(
        "misc: config-misc.yaml\nagent_prompts: config-agent-prompts.yaml\n",
        encoding="utf-8",
    )
    misc.write_text(
        "agent:\n  entry_agent_name: coordinator_agent\n  enabled_agent_profiles:\n    - coordinator_agent\n    - basic_agent\n  max_agent_switches_per_turn: 2\n",
        encoding="utf-8",
    )
    prompts.write_text(
        "coordinator_agent:\n"
        "  instructions: coordinator instructions\n"
        "  prompt_templates:\n"
        "    system_prompt: coordinator system\n"
        "basic_agent:\n"
        "  instructions: basic instructions\n"
        "  prompt_templates:\n"
        "    system_prompt: basic system\n"
        "explorer_agent:\n"
        "  instructions: explorer instructions\n"
        "  prompt_templates:\n"
        "    system_prompt: explorer system\n"
        "auditor_agent:\n"
        "  instructions: auditor instructions\n"
        "  prompt_templates:\n"
        "    system_prompt: auditor system\n"
        "vlm_instructions: vlm note\n",
        encoding="utf-8",
    )

    config = ConfigLoader(config_path=overall).load(reload=True)

    assert config.agent.entry_agent_name == "coordinator_agent"
    assert config.agent.enabled_agent_profiles == ["coordinator_agent", "basic_agent"]
    assert config.agent.max_agent_switches_per_turn == 2
    assert config.agent_prompts.coordinator_agent["instructions"] == "coordinator instructions"
    assert config.agent_prompts.basic_agent["instructions"] == "basic instructions"
    assert config.agent_prompts.vlm_instructions == "vlm note"


def test_existing_basic_prompt_sections_survive_migration(tmp_path: Path):
    overall = tmp_path / "config-overall.yaml"
    prompts = tmp_path / "config-agent-prompts.yaml"

    overall.write_text(
        "agent_prompts: config-agent-prompts.yaml\n",
        encoding="utf-8",
    )
    prompts.write_text(
        "basic_agent:\n"
        "  instructions: basic instructions\n"
        "  prompt_templates:\n"
        "    system_prompt: basic system\n"
        "    planning:\n"
        "      initial_plan: plan body\n"
        "    managed_agent:\n"
        "      task: task body\n"
        "      report: report body\n"
        "    final_answer:\n"
        "      pre_messages: pre body\n"
        "      post_messages: post body\n",
        encoding="utf-8",
    )

    config = ConfigLoader(config_path=overall).load(reload=True)

    basic_prompt_templates = config.agent_prompts.basic_agent["prompt_templates"]
    assert basic_prompt_templates["planning"]["initial_plan"] == "plan body"
    assert basic_prompt_templates["managed_agent"]["task"] == "task body"
    assert basic_prompt_templates["final_answer"]["pre_messages"] == "pre body"
```

- [ ] Step 2: Run the new config tests and verify they fail because `AgentConfig` / `AgentPromptsConfig` still use the old single-agent shape.

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_multi_agent_config.py -v`
Expected: FAIL with missing `entry_agent_name`, missing `enabled_agent_profiles`, or missing `coordinator_agent` / `basic_agent` prompt fields.

- [ ] Step 3: Expand `AgentConfig` with explicit multi-agent runtime fields.

```python
# backend/config.py
@dataclass
class AgentConfig:
    """Agent 工具调用配置"""

    enabled: Optional[bool] = None
    agent_mode: str = "tool_calling"
    max_steps: Optional[int] = None
    session_ttl_minutes: Optional[int] = None
    exec_timeout: Optional[int] = None
    work_dir: Optional[str] = None
    python_executable: str = "~/.agent/bin/python"
    command_blacklist: Optional[List[dict]] = None
    playwright_profile_dir: Optional[str] = None
    search_whitelist_paths: List[str] = field(default_factory=list)
    search_exclude_globs: List[str] = field(default_factory=list)
    glob_default_limit: int = 100
    glob_max_limit: int = 1000
    grep_default_head_limit: int = 200
    grep_max_head_limit: int = 1000
    grep_max_columns: int = 400
    entry_agent_name: str = "coordinator_agent"
    enabled_agent_profiles: List[str] = field(
        default_factory=lambda: ["coordinator_agent", "basic_agent"]
    )
    max_agent_switches_per_turn: int = 2
```

- [ ] Step 4: Replace the old prompt config dataclass with a multi-agent-aware structure.

```python
# backend/config.py
@dataclass
class AgentPromptsConfig:
    """多 Agent 提示词配置（从 config-agent-prompts.yaml 加载）"""

    coordinator_agent: dict = field(default_factory=dict)
    basic_agent: dict = field(default_factory=dict)
    explorer_agent: dict = field(default_factory=dict)
    auditor_agent: dict = field(default_factory=dict)
    vlm_instructions: Optional[str] = None
```

- [ ] Step 5: Update `_parse_agent_config()` and `_parse_agent_prompts_config()` to read the new fields and preserve old safe defaults.

```python
# backend/config.py
def _parse_agent_config(self) -> AgentConfig:
    default_search_whitelist_paths = [str(PROJECT_ROOT), "/tmp"]
    default_search_exclude_globs = [
        ".git",
        "__pycache__",
        "node_modules",
        ".next",
        ".sglang",
    ]
    raw_agent_mode = self._get("agent", "agent_mode", "tool_calling")
    return AgentConfig(
        enabled=self._get_bool("agent", "enabled", True),
        agent_mode=normalize_agent_mode(raw_agent_mode),
        max_steps=self._get_int("agent", "max_steps", 20),
        session_ttl_minutes=self._get_int("agent", "session_ttl_minutes", 60),
        exec_timeout=self._get_int("agent", "exec_timeout", 60),
        work_dir=self._get("agent", "work_dir"),
        python_executable=self._get("agent", "python_executable", "~/.agent/bin/python")
        or "~/.agent/bin/python",
        command_blacklist=self._get("agent", "command_blacklist"),
        playwright_profile_dir=self._get("agent", "playwright_profile_dir"),
        search_whitelist_paths=self._get_str_list(
            "agent", "search_whitelist_paths", default_search_whitelist_paths
        ),
        search_exclude_globs=self._get_str_list(
            "agent", "search_exclude_globs", default_search_exclude_globs
        ),
        glob_default_limit=self._get_int("agent", "glob_default_limit", 100),
        glob_max_limit=self._get_int("agent", "glob_max_limit", 1000),
        grep_default_head_limit=self._get_int("agent", "grep_default_head_limit", 200),
        grep_max_head_limit=self._get_int("agent", "grep_max_head_limit", 1000),
        grep_max_columns=self._get_int("agent", "grep_max_columns", 400),
        entry_agent_name=self._get("agent", "entry_agent_name", "coordinator_agent"),
        enabled_agent_profiles=self._get_str_list(
            "agent",
            "enabled_agent_profiles",
            ["coordinator_agent", "basic_agent"],
        ),
        max_agent_switches_per_turn=self._get_int(
            "agent", "max_agent_switches_per_turn", 2
        ),
    )


def _parse_agent_prompts_config(self) -> AgentPromptsConfig:
    data = self._agent_prompts_data
    return AgentPromptsConfig(
        coordinator_agent=data.get("coordinator_agent") or {},
        basic_agent=data.get("basic_agent") or {},
        explorer_agent=data.get("explorer_agent") or {},
        auditor_agent=data.get("auditor_agent") or {},
        vlm_instructions=data.get("vlm_instructions"),
    )
```

- [ ] Step 6: Rewrite `config/config-agent-prompts.yaml` into the new multi-agent structure.

Use these content rules while editing the YAML:

```yaml
coordinator_agent:
  instructions: |-
    你是 coordinator agent。
    你当前只向模型披露两位 agent：你自己和 basic_agent。
    如果用户需求只是普通闲聊、简单知识问答、无需工具调用，你可以直接回答用户。
    如果用户需求需要文件读写、搜索、命令执行、复杂技能、或者你拿不准应如何处理，请输出一条纯粹的 switch_agent 指令切到 basic_agent。
    你的回复只能是两类之一：
    1. 面向用户的最终自然语言答复
    2. 一条切换到 basic_agent 的指令
    切换时不要输出与切换无关的长文本，不要输出工具调用 JSON。
  prompt_templates:
    system_prompt: |-
      {%- if custom_instructions %}
      {{custom_instructions}}

      {%- endif %}
      现在，根据以下用户需求，选择是直接回答，还是切换到 basic_agent。

basic_agent:
  instructions: |-
    将当前文件顶层 `agent_instructions` 的完整行为规则迁移到这里，并根据 PRD 做两类改写：
    1. 把原本“全能单 agent”的措辞改写为“basic agent 的工作规则”
    2. 保留现有文件读写、glob/grep、skills、exec_command、安全约束、final answer 重复渲染规避等要求
  prompt_templates:
    system_prompt: |-
      将现有 `toolcalling_agent.system_prompt` 全量迁移到这里，并把其中针对单 agent 的表述改写为仅面向 basic agent 的表述。
    planning:
      将现有 `toolcalling_agent.planning` 整块原样迁移到这里，字段名保持不变。
    managed_agent:
      将现有 `toolcalling_agent.managed_agent` 整块原样迁移到这里，字段名保持不变。
    final_answer:
      将现有 `toolcalling_agent.final_answer` 整块原样迁移到这里，字段名保持不变。

explorer_agent:
  instructions: |-
    占位，当前版本未启用。
  prompt_templates:
    system_prompt: |-
      占位，当前版本未启用。

auditor_agent:
  instructions: |-
    占位，当前版本未启用。
  prompt_templates:
    system_prompt: |-
      占位，当前版本未启用。
```

- [ ] Step 7: Add runtime knobs to `config/config-misc.yaml`.

```yaml
# config/config-misc.yaml
agent:
  entry_agent_name: coordinator_agent
  enabled_agent_profiles:
    - coordinator_agent
    - basic_agent
  max_agent_switches_per_turn: 2
```

- [ ] Step 8: Re-run the focused config tests until green.

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_multi_agent_config.py -v`
Expected: PASS

### Task 2: Add `switch_agent` command parsing before normal tool parsing

**Files:**
- Modify: `backend/agent/local_llm_model.py`
- Modify: `tests/test_multi_tool_parsing.py`

- [ ] Step 1: Add failing parser tests for canonical and tolerated `switch_agent` formats.

```python
# tests/test_multi_tool_parsing.py
from backend.agent.local_llm_model import LocalLLMModel
from unittest.mock import MagicMock


def _make_model():
    return LocalLLMModel(llm_service=MagicMock(), model_id="test-model")


def test_switch_agent_canonical_array_format():
    model = _make_model()
    content = '[{"id":"call_0","type":"agent","function":{"name":"switch_agent","arguments":"basic_agent"}}]'
    cmd = model._parse_switch_agent_command(content)
    assert cmd is not None
    assert cmd.target_agent == "basic_agent"


def test_switch_agent_accepts_function_style_payload():
    model = _make_model()
    content = '{"id":"call_0","type":"function","function":{"name":"switch_agent","arguments":{"agent_name":"basic_agent"}}}'
    cmd = model._parse_switch_agent_command(content)
    assert cmd is not None
    assert cmd.target_agent == "basic_agent"


def test_switch_agent_ignores_plain_tool_call_payloads():
    model = _make_model()
    content = '{"name":"read_file","arguments":{"filePath":"/tmp/a.txt"}}'
    assert model._parse_switch_agent_command(content) is None
```

- [ ] Step 2: Run the parser tests and verify they fail because `_parse_switch_agent_command` does not exist yet.

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_multi_tool_parsing.py -v`
Expected: FAIL with `AttributeError: 'LocalLLMModel' object has no attribute '_parse_switch_agent_command'`

- [ ] Step 3: Add a simple normalized switch command dataclass and parser helpers in `local_llm_model.py`.

```python
# backend/agent/local_llm_model.py
from dataclasses import dataclass


@dataclass
class SwitchAgentCommand:
    target_agent: str
    raw_payload: dict | list | str


def _extract_switch_agent_target(payload) -> str | None:
    if isinstance(payload, list) and len(payload) == 1:
        payload = payload[0]
    if not isinstance(payload, dict):
        return None

    function_payload = payload.get("function")
    if isinstance(function_payload, dict):
        if function_payload.get("name") != "switch_agent":
            return None
        arguments = function_payload.get("arguments")
    elif payload.get("name") == "switch_agent":
        arguments = payload.get("arguments")
    else:
        return None

    if isinstance(arguments, str):
        stripped = arguments.strip()
        if stripped:
            return stripped
    if isinstance(arguments, dict):
        candidate = arguments.get("agent_name") or arguments.get("target_agent")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None
```

- [ ] Step 4: Implement `_parse_switch_agent_command()` so it tolerates canonical JSON arrays, function-style JSON, and inline wrapper text.

```python
# backend/agent/local_llm_model.py
def _parse_switch_agent_command(self, content: str | None) -> SwitchAgentCommand | None:
    if not content:
        return None

    stripped = content.strip()
    candidates: list[str] = [stripped]

    if "[" in stripped:
        start = stripped.find("[")
        extracted = self._extract_balanced_json_array(stripped, start)
        if extracted:
            candidates.append(extracted)
    if "{" in stripped:
        start = stripped.find("{")
        extracted = self._extract_balanced_json(stripped, start)
        if extracted:
            candidates.append(extracted)

    for candidate in candidates:
        parsed = self._try_parse_json(candidate)
        target = _extract_switch_agent_target(parsed)
        if target:
            return SwitchAgentCommand(target_agent=target, raw_payload=parsed)
    return None
```

- [ ] Step 5: Keep normal tool parsing intact while making switch parsing available to `AgentRunner`.

```python
# backend/agent/local_llm_model.py
# Do not remove _parse_tool_calls_from_text().
# Export the new parser helper as part of the model instance so AgentRunner can call it
# before falling back to tool parsing / plain text handling.
```

- [ ] Step 6: Re-run the parser tests until green.

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_multi_tool_parsing.py -v`
Expected: PASS

### Task 3: Add profile-based agent construction and visibility filtering

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Modify: `tests/test_get_tools_no_load_images.py`

- [ ] Step 1: Add failing tests for coordinator/basic tool visibility and profile prompt selection.

```python
# tests/test_get_tools_no_load_images.py
from unittest.mock import MagicMock

from backend.agent.agent_runner import AgentRunner
from backend.config import AppConfig


def _make_runner():
    config = AppConfig()
    config.agent.enabled_agent_profiles = ["coordinator_agent", "basic_agent"]
    config.agent_prompts.coordinator_agent = {
        "instructions": "coord",
        "prompt_templates": {"system_prompt": "coord sys"},
    }
    config.agent_prompts.basic_agent = {
        "instructions": "basic",
        "prompt_templates": {"system_prompt": "basic sys"},
    }
    return AgentRunner(MagicMock(), config)


def test_profile_tool_filter_coordinator_has_no_tools():
    runner = _make_runner()
    agent = runner._build_profile_agent("coordinator_agent")
    assert list(agent.tools.keys()) == []


def test_profile_tool_filter_basic_keeps_core_tools():
    runner = _make_runner()
    agent = runner._build_profile_agent("basic_agent")
    tool_names = set(agent.tools.keys())
    assert {"read_file", "write_file", "edit_file", "glob", "grep", "list_skills", "read_skill"}.issubset(tool_names)
```

- [ ] Step 2: Run the focused tests and verify they fail because `_build_profile_agent()` does not exist and coordinator/basic are not profile-aware.

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_get_tools_no_load_images.py -v`
Expected: FAIL with missing `_build_profile_agent` or mismatched prompt config shape.

- [ ] Step 3: Add profile-aware helper methods to `AgentRunner` for prompt lookup, enabled-profile validation, and allowed-tool filtering.

```python
# backend/agent/agent_runner.py
PROFILE_TOOL_ALLOWLIST = {
    "coordinator_agent": set(),
    "basic_agent": None,
}


def _get_profile_prompt_bundle(self, profile_name: str) -> dict:
    bundle = getattr(self.config.agent_prompts, profile_name, None)
    if not isinstance(bundle, dict) or not bundle:
        raise ValueError(f"Missing prompt bundle for profile: {profile_name}")
    return bundle


def _ensure_profile_enabled(self, profile_name: str) -> None:
    if profile_name not in self.config.agent.enabled_agent_profiles:
        raise ValueError(f"Agent profile not enabled: {profile_name}")


def _filter_tools_for_profile(self, profile_name: str, tools: list):
    allowlist = PROFILE_TOOL_ALLOWLIST.get(profile_name)
    if allowlist is None:
        return tools
    return [tool for tool in tools if getattr(tool, "name", None) in allowlist]
```

- [ ] Step 4: Replace `_build_agent()` with a profile-based builder that loads prompts from the new config layout and applies VLM instructions only to coordinator.

```python
# backend/agent/agent_runner.py
def _build_profile_agent(self, profile_name: str) -> MultiStepAgent:
    self._ensure_profile_enabled(profile_name)
    prompt_bundle = self._get_profile_prompt_bundle(profile_name)

    instructions = prompt_bundle.get("instructions")
    prompt_templates = dict(prompt_bundle.get("prompt_templates") or {})
    if (
        profile_name == "coordinator_agent"
        and self.config.model.model_type == "vision"
        and self.config.agent_prompts.vlm_instructions
    ):
        vlm_note = self.config.agent_prompts.vlm_instructions
        instructions = f"{instructions}\n\n{vlm_note}" if instructions else vlm_note

    tools = self._filter_tools_for_profile(profile_name, get_tools_from_config(self.config))
    kwargs = {
        "tools": tools,
        "model": self.model,
        "max_steps": self.config.agent.max_steps,
        "verbosity_level": 1,
        "stream_outputs": True,
        "agent_python_executable": self.config.agent.python_executable,
    }
    if prompt_templates:
        kwargs["prompt_templates"] = prompt_templates
    if instructions:
        kwargs["instructions"] = instructions
    return SmartToolCallingAgent(**kwargs)
```

- [ ] Step 5: Keep `_build_agent(mode)` as a compatibility shim only if needed by nearby tests, but make it delegate to the configured entry profile.

```python
# backend/agent/agent_runner.py
def _build_agent(self, mode: str) -> MultiStepAgent:
    _ = normalize_agent_mode(mode)
    return self._build_profile_agent(self.config.agent.entry_agent_name)
```

- [ ] Step 6: Re-run the visibility tests until green.

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_get_tools_no_load_images.py -v`
Expected: PASS

### Task 4: Refactor `AgentSession` around shared memory and turn-level agent switching

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Modify: `tests/test_agent_session_context.py`
- Test: `tests/test_multi_agent_runner.py`

- [ ] Step 1: Add failing tests for shared-memory session state and coordinator-first turn entry.

```python
# tests/test_multi_agent_runner.py
from unittest.mock import MagicMock, patch

from backend.agent.agent_runner import AgentRunner
from backend.config import AppConfig


def _make_runner():
    config = AppConfig()
    config.agent.entry_agent_name = "coordinator_agent"
    config.agent.enabled_agent_profiles = ["coordinator_agent", "basic_agent"]
    config.agent_prompts.coordinator_agent = {
        "instructions": "coord",
        "prompt_templates": {"system_prompt": "coord sys"},
    }
    config.agent_prompts.basic_agent = {
        "instructions": "basic",
        "prompt_templates": {"system_prompt": "basic sys"},
    }
    return AgentRunner(MagicMock(), config)


def test_new_session_starts_with_entry_profile_name():
    runner = _make_runner()
    with patch.object(runner, "_build_profile_agent", return_value=MagicMock()) as mock_build:
        session = runner.get_or_create_session(None)
    assert session.current_agent_name == "coordinator_agent"
    mock_build.assert_called_once_with("coordinator_agent")


def test_reused_session_resets_turn_agent_to_coordinator():
    runner = _make_runner()
    first_agent = MagicMock()
    second_agent = MagicMock()
    with patch.object(runner, "_build_profile_agent", side_effect=[first_agent, second_agent]):
        session = runner.get_or_create_session("s1")
        session.current_agent_name = "basic_agent"
        session.active_agent = second_agent
        reset_session = runner.prepare_session_for_new_turn("s1")
    assert reset_session.current_agent_name == "coordinator_agent"
```

- [ ] Step 2: Run the session tests and verify they fail because `current_agent_name`, `active_agent`, and `prepare_session_for_new_turn()` do not exist yet.

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_agent_session_context.py tests/test_multi_agent_runner.py -v`
Expected: FAIL with missing session fields / helper methods.

- [ ] Step 3: Expand `AgentSession` to carry shared-session semantics instead of “one fixed agent forever”.

```python
# backend/agent/agent_runner.py
@dataclass
class AgentSession:
    session_id: str
    agent: MultiStepAgent
    mode: str
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    aborted: bool = False
    answer_now: bool = False
    accumulated_content: str = ""
    accumulated_thought: str = ""
    has_run: bool = False
    answering_now: bool = False
    current_agent_name: str = "coordinator_agent"
    active_agent: MultiStepAgent | None = None
    turn_switch_count: int = 0
    has_shared_history: bool = False

    def __post_init__(self):
        if self.active_agent is None:
            self.active_agent = self.agent
```

- [ ] Step 4: Add a small helper that resets turn-scoped agent state to the entry profile without destroying shared history.

```python
# backend/agent/agent_runner.py
def prepare_session_for_new_turn(self, session_id: str) -> AgentSession:
    session = self.sessions.get(session_id)
    if not session:
        raise KeyError(session_id)
    session.current_agent_name = self.config.agent.entry_agent_name
    session.turn_switch_count = 0
    session.active_agent = self._build_profile_agent(session.current_agent_name)
    session.agent = session.active_agent
    session.clear_abort()
    session.answer_now = False
    return session
```

- [ ] Step 5: Update `get_or_create_session()` to initialize the entry profile and preserve shared history on reuse.

```python
# backend/agent/agent_runner.py
def get_or_create_session(self, session_id: str | None, mode: str | None = None) -> AgentSession:
    resolved_mode = normalize_agent_mode(mode or self.config.agent.agent_mode)
    if session_id:
        session = self.sessions.get(session_id)
        if session:
            if session.aborted:
                logger.info(f"Session {session_id} 存在残留 abort 标志，清理后继续复用")
            session.clear_abort()
            session.answer_now = False
            abort_manager.clear(session_id)
            session_request_manager.cleanup(session_id)
            return session

    sid = session_id or str(uuid.uuid4())
    agent = self._build_profile_agent(self.config.agent.entry_agent_name)
    session = AgentSession(
        session_id=sid,
        agent=agent,
        mode=resolved_mode,
        current_agent_name=self.config.agent.entry_agent_name,
        active_agent=agent,
    )
    if abort_manager.is_aborted(sid):
        session.abort()
    self.sessions.put(session)
    return session
```

- [ ] Step 6: Re-run the focused session tests until green.

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_agent_session_context.py tests/test_multi_agent_runner.py -v`
Expected: PASS

### Task 5: Implement coordinator routing pass and stream/sync handoff to basic

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Modify: `tests/test_multi_agent_runner.py`
- Modify: `tests/test_agent_api_images.py`

- [ ] Step 1: Add failing runner tests for direct-answer and switch-to-basic flows.

```python
# tests/test_multi_agent_runner.py
import asyncio
from unittest.mock import MagicMock, patch


def test_coordinator_direct_answer_does_not_switch():
    runner = _make_runner()
    coordinator_agent = MagicMock()
    coordinator_agent.run.return_value = "你好，我可以直接回答这个问题。"
    with patch.object(runner, "_build_profile_agent", return_value=coordinator_agent):
        result = asyncio.run(runner.run_sync(None, "你好"))
    assert result["answer"] == "你好，我可以直接回答这个问题。"


def test_coordinator_switches_then_basic_handles_task():
    runner = _make_runner()
    coordinator_agent = MagicMock()
    basic_agent = MagicMock()
    coordinator_agent.run.return_value = '[{"id":"call_0","type":"agent","function":{"name":"switch_agent","arguments":"basic_agent"}}]'
    basic_agent.run.return_value = "已完成文件读取。"
    with patch.object(runner, "_build_profile_agent", side_effect=[coordinator_agent, basic_agent]):
        result = asyncio.run(runner.run_sync(None, "请读取 README"))
    assert result["answer"] == "已完成文件读取。"
```

- [ ] Step 2: Run the runner tests and verify they fail because routing still sends the user message straight into the old single-agent flow.

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_multi_agent_runner.py tests/test_agent_api_images.py -v`
Expected: FAIL with wrong answer path or no switch behavior.

- [ ] Step 3: Add a non-persistent coordinator routing helper that never commits the wrapped prompt to shared memory.

```python
# backend/agent/agent_runner.py
def _build_coordinator_turn_message(self, user_message: str) -> str:
    return (
        "现在，根据以下用户需求，选择是直接回答，还是切换到 basic_agent。\n"
        f"user: {user_message}"
    )


def _run_coordinator_routing_pass(self, session: AgentSession, user_message: str, pil_images=None):
    coordinator = self._build_profile_agent("coordinator_agent")
    session.current_agent_name = "coordinator_agent"
    session.active_agent = coordinator
    session.agent = coordinator
    wrapped_message = self._build_coordinator_turn_message(user_message)
    result = coordinator.run(wrapped_message, reset=not session.has_shared_history, images=pil_images)
    return str(result) if result is not None else ""
```

- [ ] Step 4: Add a small helper that interprets the coordinator result as `switch` or final answer.

```python
# backend/agent/agent_runner.py
def _classify_coordinator_output(self, text: str):
    command = self.model._parse_switch_agent_command(text)
    if command is not None:
        return {"type": "switch", "target": command.target_agent}
    if text.strip():
        return {"type": "final", "answer": text.strip()}
    return {"type": "invalid", "error": "coordinator_invalid_response"}
```

- [ ] Step 5: Add explicit commit helpers so coordinator writes only original user text plus retained final answer, never the wrapped routing prompt.

```python
# backend/agent/agent_runner.py
def _commit_direct_answer_to_shared_memory(self, session: AgentSession, user_message: str, answer: str):
    session.active_agent.memory.steps.append(TaskStep(task=user_message))
    session.active_agent.memory.steps.append(
        ActionStep(
            step_number=len([s for s in session.active_agent.memory.steps if isinstance(s, ActionStep)]) + 1,
            timing=Timing(start_time=0.0, end_time=0.0),
            model_output=answer,
            tool_calls=None,
            observations=None,
            action_output=answer,
            is_final_answer=True,
        )
    )
    session.has_shared_history = True
```

- [ ] Step 6: Update `run_sync()` to perform coordinator routing first, then either return directly or rebuild `basic_agent` and run the original tool-calling path with the raw user message.

```python
# backend/agent/agent_runner.py
async def run_sync(self, session_id: str, message: str, mode: str | None = None, images: list[str] | None = None) -> dict:
    session = self.get_or_create_session(session_id, mode)
    if session.has_shared_history:
        session = self.prepare_session_for_new_turn(session.session_id)

    pil_images = self._decode_request_images(images)
    coordinator_result = self._run_coordinator_routing_pass(session, message, pil_images=pil_images)
    decision = self._classify_coordinator_output(coordinator_result)

    if decision["type"] == "final":
        self._commit_direct_answer_to_shared_memory(session, message, decision["answer"])
        return {"session_id": session.session_id, "answer": decision["answer"], "steps": []}

    if decision["type"] != "switch" or decision["target"] != "basic_agent":
        raise ValueError(decision.get("error", "invalid_agent_switch"))

    session.turn_switch_count += 1
    session.current_agent_name = "basic_agent"
    session.active_agent = self._build_profile_agent("basic_agent")
    session.agent = session.active_agent
    return await self._run_basic_sync(session, message, pil_images)
```

- [ ] Step 7: Extract the old single-agent `run_sync()` body into `_run_basic_sync()` and reuse it unchanged as much as possible.

```python
# backend/agent/agent_runner.py
async def _run_basic_sync(self, session: AgentSession, message: str, pil_images):
    agent = session.active_agent

    def _run():
        current_session_context.set(session.session_id)
        for _tool_obj in agent.tools.values():
            _tool_obj._injected_session_id = session.session_id
        try:
            _should_reset = not session.has_shared_history
            session.has_run = True
            session.has_shared_history = True
            return agent.run(message, reset=_should_reset, images=pil_images)
        finally:
            current_session_context.clear()

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _run)
    return {
        "session_id": session.session_id,
        "answer": str(result) if result is not None else "",
        "steps": self._collect_sync_steps(agent),
    }
```

- [ ] Step 8: Mirror the same decision flow in `run_stream()` while preserving existing SSE event shapes.

```python
# backend/agent/agent_runner.py
# In run_stream():
# 1. get/create session
# 2. reset entry profile for the new turn if shared history exists
# 3. run coordinator routing pass
# 4. if final: emit session_start/content/done only
# 5. if switch to basic: delegate to a new _run_basic_stream(session, message, pil_images)
#    that contains the old streaming body with only minimal references renamed from session.agent
#    to session.active_agent
```

- [ ] Step 9: Add vision-handoff assertions in the API image test so coordinator -> basic still forwards images.

```python
# tests/test_agent_api_images.py
def test_stream_or_sync_multi_agent_path_keeps_images_on_basic_handoff():
    from backend.agent.agent_runner import AgentRunner
    from backend.config import AppConfig

    config = AppConfig()
    config.model.model_type = "vision"
    config.agent.entry_agent_name = "coordinator_agent"
    config.agent.enabled_agent_profiles = ["coordinator_agent", "basic_agent"]
    config.agent_prompts.coordinator_agent = {
        "instructions": "coord",
        "prompt_templates": {"system_prompt": "coord sys"},
    }
    config.agent_prompts.basic_agent = {
        "instructions": "basic",
        "prompt_templates": {"system_prompt": "basic sys"},
    }

    runner = AgentRunner(MagicMock(), config)
    coordinator_agent = MagicMock()
    basic_agent = MagicMock()
    coordinator_agent.run.return_value = '[{"id":"call_0","type":"agent","function":{"name":"switch_agent","arguments":"basic_agent"}}]'
    basic_agent.run.return_value = "done"

    with patch.object(runner, "_decode_request_images", return_value=["img"]), patch.object(
        runner, "_build_profile_agent", side_effect=[coordinator_agent, basic_agent]
    ):
        result = asyncio.run(runner.run_sync(None, "看图并读取文件", images=["data:image/jpeg;base64,abc"]))

    assert result["answer"] == "done"
    basic_agent.run.assert_called_once_with("看图并读取文件", reset=True, images=["img"])
```

- [ ] Step 10: Re-run the new runner tests until green.

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_multi_agent_runner.py tests/test_agent_api_images.py -v`
Expected: PASS

### Task 6: Make stop/answer-now and shared-history helpers work with the active profile model

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Modify: `tests/test_agent_stop_api.py`
- Modify: `tests/test_agent_session_context.py`

- [ ] Step 1: Add failing tests for `answer-now` prompt generation and stop behavior using `active_agent` / shared history.

```python
# tests/test_agent_session_context.py
def test_answer_now_uses_active_agent_memory_when_session_switched(agent_runner):
    mock_agent = MagicMock()
    mock_agent.memory.steps = [TaskStep(task="请读取文件")]
    session = AgentSession(
        session_id="answer-now-shared",
        agent=mock_agent,
        active_agent=mock_agent,
        current_agent_name="basic_agent",
        mode="tool_calling",
    )
    prompt = agent_runner._build_answer_now_prompt(session)
    assert "请读取文件" in prompt
```

- [ ] Step 2: Run the stop/session tests and verify failures caused by stale references to `session.agent` only.

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_agent_session_context.py tests/test_agent_stop_api.py -v`
Expected: FAIL in at least one shared-history / active-agent assertion.

- [ ] Step 3: Update helper methods to consistently use `session.active_agent or session.agent` when reading memory and current runtime state.

```python
# backend/agent/agent_runner.py
def _get_session_runtime_agent(self, session: AgentSession):
    return session.active_agent or session.agent


def _build_answer_now_prompt(self, session: AgentSession) -> str:
    runtime_agent = self._get_session_runtime_agent(session)
    partial_outputs: list[str] = []
    observations: list[str] = []
    latest_user_task = ""
    if hasattr(runtime_agent, "memory") and hasattr(runtime_agent.memory, "steps"):
        for step in runtime_agent.memory.steps:
            ...
```

- [ ] Step 4: Update stop/abort rollback paths in `run_stream()` and reuse checks so they read from the active runtime agent instead of assuming one fixed agent object for the whole session.

```python
# backend/agent/agent_runner.py
# Replace local `agent = session.agent` assumptions in stream/sync helpers with:
agent = session.active_agent or session.agent
session.agent = agent
```

- [ ] Step 5: Re-run the stop/session tests until green.

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_agent_session_context.py tests/test_agent_stop_api.py -v`
Expected: PASS

### Task 7: Run focused multi-agent verification, then broader regressions

**Files:**
- Modify only if failures require it

- [ ] Step 1: Run the focused multi-agent suite.

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_multi_agent_config.py tests/test_multi_agent_runner.py tests/test_multi_tool_parsing.py tests/test_agent_session_context.py tests/test_get_tools_no_load_images.py tests/test_agent_api_images.py tests/test_agent_stop_api.py -v`
Expected: PASS

- [ ] Step 2: Run nearby regression suites that exercise current agent invariants.

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_write_memory_tool_calls.py tests/test_stream_tools_cycle.py tests/test_orchestrator.py -v`
Expected: PASS

- [ ] Step 3: If regressions fail, fix the implementation with the smallest targeted changes and rerun the failing commands before proceeding.

- [ ] Step 4: Run one final combined verification command and capture evidence for handoff.

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_multi_agent_config.py tests/test_multi_agent_runner.py tests/test_multi_tool_parsing.py tests/test_agent_session_context.py tests/test_get_tools_no_load_images.py tests/test_agent_api_images.py tests/test_agent_stop_api.py tests/test_write_memory_tool_calls.py tests/test_stream_tools_cycle.py tests/test_orchestrator.py -v`
Expected: PASS

- [ ] Step 5: Commit in small checkpoints after each completed task series.

Suggested commit sequence:

```bash
git add backend/config.py config/config-agent-prompts.yaml config/config-misc.yaml tests/test_multi_agent_config.py
git commit -m "feat: add multi-agent prompt config model"

git add backend/agent/local_llm_model.py tests/test_multi_tool_parsing.py
git commit -m "feat: parse switch-agent commands"

git add backend/agent/agent_runner.py tests/test_get_tools_no_load_images.py tests/test_multi_agent_runner.py tests/test_agent_session_context.py tests/test_agent_api_images.py tests/test_agent_stop_api.py
git commit -m "feat: route agent turns through coordinator"
```

---

## Self-Review Notes

### Spec Coverage

This plan explicitly covers:

1. Single session + shared main memory + per-turn current-agent switching
2. Every turn starts with `coordinator_agent`
3. `coordinator_agent` prompt only mentions itself and `basic_agent`
4. No new prompt file; restructure `config/config-agent-prompts.yaml`
5. Merge old `agent_instructions` + `toolcalling_agent` into `basic_agent`
6. `switch_agent` parsing with tolerant formats
7. Coordinator routing pass that does not pollute shared memory
8. text + vision support with `vlm_instructions` only on coordinator
9. Disabled placeholders for `explorer_agent` / `auditor_agent`
10. stop / answer-now compatibility and regression testing

### Placeholder Scan

No `TODO`, `TBD`, or “implement later” placeholders remain in task steps. The one deliberate instruction block in Task 1 Step 6 is a migration recipe for the YAML rewrite, not a deferred requirement.

### Naming Consistency

The plan consistently uses:

1. `entry_agent_name`
2. `enabled_agent_profiles`
3. `max_agent_switches_per_turn`
4. `_build_profile_agent()`
5. `_run_coordinator_routing_pass()`
6. `_classify_coordinator_output()`
7. `prepare_session_for_new_turn()`
8. `SwitchAgentCommand`
