# Session-Scoped Modular Agent Tool Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add separately configurable `manager_agent` and `basic_agent` business-tool lists whose runtime tools, API schemas, system-prompt listings, and modular usage guidance are frozen as one immutable snapshot under the exact Agent session ID at first backend acceptance.

**Architecture:** Build one validated backend business-tool catalog and prompt compiler, then resolve every first-request browser/YAML candidate into an immutable `AgentToolConfigurationSnapshot`. Stage that snapshot before the first Agent is constructed, publish it as Agent-only `request_accepted` Session Hub metadata, and reuse it for every later profile build, child, Compact/Answer Now/Stop continuation, provider call, Cycle step, and recovery. The frontend keeps browser defaults and per-session state separate: `localStorage` stores only future-context defaults, while a page-local reducer owns exact-ID `pending`/`locked` records plus the strict OFF/ON session pair.

**Tech Stack:** Python 3, FastAPI, Pydantic, smolagents/Jinja2, pytest, Next.js 14, React 18, TypeScript, Tailwind, Node `node:test`.

**Authoritative Spec:** `docs/superpowers/specs/2026-09-14-session-scoped-modular-agent-tool-configuration-design.md`

**Repository Constraints:** Do not run any git command without the user's explicit permission. Do not create a worktree or branch. Do not dispatch sub-agents until the user separately instructs us to execute this plan. Use `apply_patch` for manual edits. Follow TDD: add the focused failing test, run it and confirm the expected failure, implement the smallest coherent slice, then rerun. Do not modify dependency package source.

---

## Global invariants

- The configurable list contains only these built-in business tools, in this canonical order: `read_file`, `write_file`, `edit_file`, `glob`, `grep`, `exec_command`, `browser_command`, `check_process`, `list_skills`, `read_skill`, `web_search`, `web_fetch`, `search_and_fetch`, `wikipedia_search_and_fetch`.
- Dynamic MCP tools are ignored. `wikipedia_search`, `wikipedia_fetch`, and `wikipedia_summary` remain untouched and unselectable. `final_answer`, `switch_agent`, the managed `basic_agent` proxy, and mandatory manager `dispatching_parallel_agent` remain special protocol/control mechanisms outside the business-tool equality.
- For one profile snapshot, selected business tools must equal runtime business tools, schema-visible business tools, prompt-listed business tools, and per-tool guidance owners. Unselected tools must not leak through prompt or schema.
- Selection controls exposure only. `OperationCapability`, `PathPolicy`, `CommandPolicy`, `EnvironmentBuilder`, `ProcessSupervisor`, child capability derivation, targeted abort, and all fail-closed checks remain authoritative.
- A first-request candidate is staged before Agent construction. A successful backend `request_accepted` publication is the permanent lock point. A pre-accept failure rolls back only that exact provisional session.
- Session state is keyed only by canonical `session_id`. No current-tab, recent-session, global-snapshot, or bookmark fallback may substitute another ID.
- Agent OFF/ON is a strict pair in the current page context. If the pair owns an accepted Agent ID, ON must reuse exactly that ID. It must not allocate another ID or restore a different bookmark/session. Only explicit Clear/disposal removes the pair.
- `localStorage` key `llm-chat-settings` stores `managerAgentTools` and `basicAgentTools` as future-session defaults only; it never stores a session ID, lock, fingerprint, or frozen snapshot.
- Tool configuration is never inserted into LLM message history, timeline events, routing state, UI bubbles, child task text, or per-turn dynamic system text. After the intentional initial profile/tool-schema boundary, the Prefix-Cache Covenant resumes unchanged.
- Text and Vision pages must remain behaviorally synchronized. SGLang and vLLM must receive equivalent messages and selected tool-schema shapes.

## Resolved interfaces

Implement the backend domain in `backend/agent/tool_configuration.py` with these stable concepts (names may be imported directly by tests and API models):

```python
TOOL_CONFIGURATION_SCHEMA_VERSION = 1

@dataclass(frozen=True, slots=True)
class ProfileToolPromptPlan:
    tool_names: tuple[str, ...]
    business_tool_protocol: str
    tool_modules: tuple[str, ...]
    combination_modules: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class AgentToolConfigurationSnapshot:
    schema_version: int
    catalog_revision: str
    manager_agent: ProfileToolPromptPlan
    basic_agent: ProfileToolPromptPlan
    fingerprint: str

class AgentToolConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: int
    catalog_revision: str
    manager_agent_tools: list[str]
    basic_agent_tools: list[str]
```

`AgentToolConfigurationSnapshot` must contain only immutable nested values. Do not place mutable dictionaries/lists inside it. Provide explicit helpers:

```python
catalog.resolve_candidate(candidate: AgentToolConfigurationRequest | None) -> AgentToolConfigurationSnapshot
catalog.assert_compatible(snapshot, candidate: AgentToolConfigurationRequest | None) -> None
catalog.public_metadata() -> dict[str, object]
snapshot.public_metadata(*, locked: bool = True) -> dict[str, object]
```

Errors use one exception with structured fields:

```python
class AgentToolConfigurationError(ValueError):
    code: Literal[
        "invalid_tool_configuration",
        "tool_catalog_changed",
        "tool_configuration_locked",
        "tool_prompt_render_failed",
    ]
    status_code: int
    message: str
```

The HTTP detail shape is always:

```json
{"code":"tool_configuration_locked","message":"Tool configuration is already frozen for this Agent session.","session_id":"4a065f1d-71cb-4ce8-b4dd-cf9850e115cf"}
```

The frontend domain in `frontend/src/lib/agent-tool-configuration.ts` uses:

```ts
export interface AgentToolConfigurationCandidate {
  schema_version: number;
  catalog_revision: string;
  manager_agent_tools: string[];
  basic_agent_tools: string[];
}

export interface AgentToolConfigurationSnapshot extends AgentToolConfigurationCandidate {
  locked: true;
  fingerprint: string;
}

export type AgentToolSessionRecord =
  | { kind: 'pending'; sessionId: string; requestId: string; candidate: AgentToolConfigurationCandidate }
  | { kind: 'locked'; sessionId: string; snapshot: AgentToolConfigurationSnapshot };

export interface AgentToolSessionState {
  pairedSessionId: string | null;
  sessions: Readonly<Record<string, AgentToolSessionRecord>>;
}
```

All reducer actions carry both `sessionId` and, when relevant, `requestId`; stale actions return the identical state object. `begin` may allocate a pair only when `pairedSessionId === null`; OFF/ON itself never clears or replaces it. `clear` removes only the named session and clears the pair only when it names that same session.

## File structure

**Create**

- `backend/agent/tool_configuration.py`: catalog, immutable snapshots, prompt-plan resolution, fingerprints, structured errors, and public metadata.
- `tests/test_agent_tool_configuration.py`: configuration parsing, catalog validation, normalization, combinations, fingerprints, and prompt provenance tests.
- `tests/test_agent_tool_session_admission.py`: exact-ID staging, acceptance/rollback, lock conflicts, profile/child reuse, and concurrency tests.
- `frontend/src/lib/agent-tool-configuration.ts`: browser-default normalization, recommendations, candidate wire translation, exact-ID state reducer, and OFF/ON pairing rules.
- `frontend/src/lib/agent-tool-configuration.test.mts`: pure frontend domain and state-machine tests.
- `frontend/src/components/AgentAdvancedSettingsPanel.tsx`: shared Advanced-tab content used by both pages.

**Modify**

- `config/config-agent-operation.yaml`: `agent.default_profile_tools` defaults.
- `config/config-agent-prompts.yaml`: generic protocol, 14 single-tool modules, declarative combination modules, and short Chinese Jinja/YAML slot comments.
- `backend/config.py`: parse and validate raw default lists and prompt-module structures.
- `backend/main.py`: expose active runner catalog metadata/defaults through `/config`.
- `backend/vision/contracts.py`: carry the immutable tool snapshot in `AgentRequestAdmissionStage`.
- `backend/agent/agent_runner.py`: selected built-in registry, prompt variables, snapshot-owned profile construction, exact-ID admission, children, and lifecycle metadata.
- `backend/agent/local_llm_model.py`: retain only special defensive hiding; stop hiding selected `web_search` and `wikipedia_search_and_fetch`.
- `backend/session_hub_runtime.py`: allow validated Agent acceptance metadata without altering Chat acceptance.
- `backend/api/agent.py`: request model, staged candidate, error mapping, lifecycle fingerprint, and accepted metadata publication.
- `backend/cycle/models.py`, `backend/api/cycle.py`, `backend/cycle/executor.py`: capture one Cycle-start candidate and use unified staged admission per Agent context.
- `frontend/src/lib/message-types.ts`: add the two cached future-default arrays to `ChatSettings`.
- `frontend/src/lib/vision-automation/send-contract.ts`: parse optional Agent snapshot metadata from `request_accepted`.
- `frontend/src/lib/session-hub-client.ts`: preserve structured rejection codes.
- `frontend/src/lib/api.ts`: typed Agent/Cycle wire translation and accepted snapshot propagation.
- `frontend/src/lib/text-agent-session-handlers.ts`: surface accepted/recovered tool snapshots to the page-owned reducer.
- `frontend/src/lib/cycle-types.ts`: typed Cycle candidate.
- `frontend/src/app/page.tsx`, `frontend/src/app/vision/page.tsx`: tabs, shared panel, candidate send, exact-ID lifecycle/recovery, strict OFF/ON pair, and Clear.
- Existing focused regressions including `tests/test_multi_agent_config.py`, `tests/test_browser_command_visibility.py`, `tests/test_manager_agent_dispatch.py`, `tests/test_multi_agent_runner.py`, `tests/test_agent_session_recovery_api.py`, `tests/test_agent_stop_api.py`, `tests/test_compact_agent_cache_first.py`, `tests/test_cycle_api.py`, `tests/test_cycle_executor.py`, `tests/test_agent_security_config_integration.py`, `frontend/src/lib/api.test.mts`, `frontend/src/lib/text-agent-session-handlers.test.mts`, `frontend/src/lib/vision-agent-session-recovery.test.mts`, and `frontend/src/lib/timeline-projection.test.mts`.

---

### Task 1: Parse defaults and compile the canonical catalog

**Files:**

- Create: `backend/agent/tool_configuration.py`
- Create: `tests/test_agent_tool_configuration.py`
- Modify: `backend/config.py`
- Modify: `config/config-agent-operation.yaml`
- Modify: `tests/test_multi_agent_config.py`

- [ ] **Step 1: Add failing config/domain tests**

Test the approved manager/basic defaults, explicit empty lists, duplicate rejection, unknown/special/MCP tool rejection, unavailable built-in rejection, deterministic catalog order, stable fingerprinting, stale catalog revision, and `wikipedia_search_and_fetch` default-off behavior. Include this exact default assertion:

```python
assert config.agent.default_profile_tools == {
    "manager_agent": (
        "read_file", "write_file", "edit_file", "glob", "grep",
        "exec_command", "list_skills", "read_skill", "web_fetch",
        "search_and_fetch",
    ),
    "basic_agent": (
        "read_file", "write_file", "edit_file", "glob", "grep",
        "exec_command", "browser_command", "check_process", "list_skills",
        "read_skill", "web_search", "web_fetch", "search_and_fetch",
    ),
}
```

Require backend input duplicates to fail instead of silently deduplicating. Require candidate list order to be normalized to catalog order before hashing.

- [ ] **Step 2: Run the tests and confirm the expected failure**

Run:

```bash
./.sglang/bin/python -m pytest \
  tests/test_agent_tool_configuration.py \
  tests/test_multi_agent_config.py -q
```

Expected: FAIL because the config fields, catalog, request type, and snapshot compiler do not exist.

- [ ] **Step 3: Implement parsing and immutable domain types**

Add `AgentConfig.default_profile_tools: dict[str, tuple[str, ...]]`. Parse only `manager_agent` and `basic_agent`; reject any other profile key. Preserve `[]` as `()` and validate raw types before constructing tuples.

In `tool_configuration.py`, define the constant canonical name tuple and a `BusinessToolCatalog` that is constructed from prompt config plus the actually registered built-in wrappers. Do not include MCP tools. Treat `web_tools.enable_wikipedia=false` as making only `wikipedia_search_and_fetch` unavailable; do not reinterpret command/browser security flags as catalog availability.

Compute `catalog_revision` as SHA-256 over canonical JSON containing schema version, ordered public metadata, generic protocol, each tool module, each combination module, and generated wrapper schema/description signatures. Compute snapshot fingerprint over schema version, catalog revision, normalized manager/basic tuples, and both resolved immutable prompt plans.

- [ ] **Step 4: Add YAML defaults and rerun**

Add `agent.default_profile_tools` exactly as approved. Do not add any session ID or lock fields to YAML.

Run the Task 1 command again. Expected: PASS.

### Task 2: Refactor prompts into provenance-safe modules

**Files:**

- Modify: `config/config-agent-prompts.yaml`
- Modify: `backend/config.py`
- Modify: `backend/agent/tool_configuration.py`
- Modify: `tests/test_agent_tool_configuration.py`
- Modify: `tests/test_multi_agent_config.py`

- [ ] **Step 1: Add failing module-ownership tests**

Test all of the following before changing the prompts:

- manager/basic profile base text and generic protocol contain no configurable business-tool identifier;
- every one of 14 business tools has exactly one module with `order`, Chinese `label`, concise UI `description`, and non-empty `prompt`;
- a single-tool module may name only its owner;
- a combination may name only tools in `requires_all`;
- combination IDs and tool orders are unique;
- every requirement names a selectable available built-in;
- for all $2^{14}$ selection masks, the compiler returns tool modules in catalog order and a combination iff `requires_all` is a subset of the selection;
- an empty selection produces an empty generic protocol and no tool/combination modules.

- [ ] **Step 2: Run and confirm prompt tests fail**

Run:

```bash
./.sglang/bin/python -m pytest \
  tests/test_agent_tool_configuration.py \
  tests/test_multi_agent_config.py -q
```

Expected: FAIL because tool-specific instructions are currently mixed into the two profile prompts.

- [ ] **Step 3: Perform the sentence-by-sentence prompt audit**

Create these top-level prompt-config sections:

```yaml
business_tool_protocol: |-
  Emit a canonical framed tool call only when a selected tool is needed.
  Calls in one turn run concurrently and therefore must be independent.
  Wait for system-supplied observations before issuing dependent work.

business_tool_modules:
  read_file:
    order: 10
    label: 读取文件
    description: 读取允许范围内的文件内容。
    prompt: |-
      使用 read_file 读取现有文件；读取结果是系统返回的观察，不要预先猜测。

tool_prompt_combinations:
  - id: file_create_vs_edit
    requires_all: [write_file, edit_file]
    prompt: |-
      创建全新文件使用 write_file；修改、追加或删除已有文件内容使用 edit_file。
```

Classify every current manager/basic sentence into exactly one of: profile behavior, generic protocol, one tool module, one combination module, or special control protocol. Remove manager's unconditional “not a direct executor/read-only/no write/no shell” rules. Keep delegation judgment and mandatory dispatch rules.

Use these required combination IDs and semantics:

- `file_create_vs_edit` — `write_file + edit_file`;
- `read_before_create` — `read_file + write_file`;
- `read_before_edit` — `read_file + edit_file`;
- `path_then_content_search` — `glob + grep`;
- `skill_discovery_and_read` — `list_skills + read_skill`;
- `exec_gui_skill` — `exec_command + read_skill`;
- `exec_process_followup` — `exec_command + check_process`;
- `coding_write_edit_verify` — `write_file + edit_file + exec_command`;
- pairwise, non-contradictory search modules for `web_search + web_fetch`, `web_search + search_and_fetch`, `web_search + wikipedia_search_and_fetch`, `search_and_fetch + web_fetch`, `search_and_fetch + wikipedia_search_and_fetch`, and `web_fetch + wikipedia_search_and_fetch`.

Do not add a browser combination module. Keep browser-only guidance inside `browser_command` and do not name `list_skills`/`read_skill` there.

Place short Chinese non-rendering comments immediately above the four template slots:

```jinja2
{# 注入通用业务工具调用协议；没有已选业务工具时为空 #}
{{ business_tool_protocol }}
{# 按工具目录固定顺序注入已选工具的独立使用技巧 #}
{% for module in selected_tool_prompt_modules %}{{ module }}{% endfor %}
{# 仅当 requires_all 中的工具全部选中时注入组合规则 #}
{% for module in selected_combination_prompt_modules %}{{ module }}{% endfor %}
{# 仅列出当前 profile 已选并可调用的一般业务工具 #}
{% for tool in selected_business_tools.values() %}- {{ tool.to_tool_calling_prompt() }}{% endfor %}
```

Manager's dispatch format/listing remains in a separate special-control section and may name `dispatching_parallel_agent`. Basic/manager profile base prompts may still generically say “tool” or “Skill,” but may not hardcode any business-tool name.

- [ ] **Step 4: Add structural validation and rerun**

Extend `_parse_agent_prompts_config()` and catalog startup validation. Structural validation is the enforcement mechanism for “tips/arguments/examples do not leak”: rendered base/generic content is tool-agnostic, every injected segment has an explicit owner set, and only selected owner sets may be compiled. Also scan generated tool descriptions/schema JSON so a tool wrapper cannot name a different business tool.

Run the Task 2 command. Expected: PASS.

### Task 3: Make runtime tools, schemas, and prompts one selected set

**Files:**

- Modify: `backend/agent/agent_runner.py`
- Modify: `backend/agent/local_llm_model.py`
- Modify: `tests/test_browser_command_visibility.py`
- Modify: `tests/test_get_tools_no_load_images.py`
- Modify: `tests/test_manager_agent_dispatch.py`
- Modify: `tests/test_agent_tool_configuration.py`

- [ ] **Step 1: Replace asymmetry assertions with failing symmetry tests**

For empty, default, all-selected, each singleton, and representative combination selections, assert:

```python
selected == runtime_business_names
selected == schema_business_names
selected == prompt_plan.tool_names
selected == prompt_listed_business_names
```

Assert every unselected business name is absent from the rendered prompt and serialized API schema. Explicitly cover selected and unselected forms of `browser_command`, `web_search`, and `wikipedia_search_and_fetch`. Assert empty manager still has `dispatching_parallel_agent`; empty basic has no callable business schema.

- [ ] **Step 2: Run and confirm old hidden-tool behavior fails**

Run:

```bash
./.sglang/bin/python -m pytest \
  tests/test_browser_command_visibility.py \
  tests/test_get_tools_no_load_images.py \
  tests/test_manager_agent_dispatch.py \
  tests/test_agent_tool_configuration.py -q
```

Expected: FAIL on the current `browser_command`/`web_search`/Wikipedia asymmetries and static manager allowlist.

- [ ] **Step 3: Implement selected built-in construction**

Add a built-in-only registry helper that returns wrappers keyed by name without MCP tools. Keep the old aggregate helper only where legacy tests/callers still require it. Change `_filter_tools_for_profile()` to use a supplied snapshot plan for manager/basic; coordinators remain business-tool-empty. Add mandatory manager dispatch after resolving selected business tools.

Pass `ProfileToolPromptPlan` into `SmartToolCallingAgent`. In `initialize_system_prompt()`, derive and validate `selected_business_tools`, protocol, single-tool modules, combination modules, and special control tools. Fail closed if actual business wrappers differ from the plan.

Replace the two business asymmetry hidden sets with a defensive special-only hidden set containing `final_answer` and the three untouched split Wikipedia tools. Remove `browser_command`, `web_search`, and `wikipedia_search_and_fetch` from defensive hiding in both `SmartToolCallingAgent` and `LocalLLMModel`. Do not make `final_answer` visible again.

- [ ] **Step 4: Rerun symmetry tests**

Run the Task 3 command. Expected: PASS.

### Task 4: Bind the immutable snapshot during exact-session admission

**Files:**

- Create: `tests/test_agent_tool_session_admission.py`
- Modify: `backend/vision/contracts.py`
- Modify: `backend/agent/agent_runner.py`
- Modify: `tests/test_agent_session_context.py`
- Modify: `tests/test_multi_agent_runner.py`

- [ ] **Step 1: Add failing admission tests**

Cover:

- a new exact session stages a snapshot before `_build_profile_agent()`;
- pre-accept rollback deletes a newly created provisional session and snapshot;
- successful registration leaves the same immutable snapshot object on `AgentSession`;
- omitted later candidates use the frozen snapshot;
- an exact later candidate is idempotent;
- a different later candidate raises `tool_configuration_locked`;
- while one same-ID admission is pending, a different candidate gets `tool_configuration_locked`, while an identical concurrent turn gets the existing busy conflict;
- session A rollback/acceptance cannot change session B;
- preparing a new turn, switching profiles, Stop continuation, and rebuilding an agent never reread current defaults.

- [ ] **Step 2: Run and confirm admission tests fail**

Run:

```bash
./.sglang/bin/python -m pytest \
  tests/test_agent_tool_session_admission.py \
  tests/test_agent_session_context.py \
  tests/test_multi_agent_runner.py -q
```

Expected: FAIL because `AgentSession` and `AgentRequestAdmissionStage` do not carry tool snapshots.

- [ ] **Step 3: Thread the snapshot through runner construction**

Add `_tool_configuration_snapshot` to `AgentRequestAdmissionStage`. Add required `tool_configuration_snapshot` to `AgentSession` and validate it in `__post_init__` without weakening capability checks.

Change signatures coherently:

```python
stage_agent_request_admission(
    session_id,
    mode,
    vision_automation_metadata,
    inbound_image_count=None,
    tool_configuration=None,
) -> AgentRequestAdmissionStage
_create_session(session_id, resolved_mode, tool_snapshot) -> AgentSession
_build_profile_agent(
    profile_name,
    *,
    session=None,
    runtime_context=None,
    include_legacy_vlm_note=False,
    tool_snapshot=None,
) -> MultiStepAgent
```

For a new ID, resolve the request candidate or YAML defaults exactly once into the stage. For an existing session, use its snapshot and validate an explicitly supplied candidate. Compare against an existing pending stage under `_agent_admission_lock` before returning busy so a conflicting first candidate receives the specified locked error.

Every rebuild path (`prepare_session_for_new_turn`, `_activate_profile_agent`, child reset, legacy entry build) must take the owning session snapshot. No existing session path calls `resolve_candidate(None)`.

- [ ] **Step 4: Make acceptance/rollback failure-atomic and rerun**

The provisional session may be placed in `SessionStore` so the worker can register, but it remains owned by the pending admission. Publish acceptance only after the constructed agent, capability binding, run scope, and prompt plan all exist. If the registration/acceptance callback raises, finish the exact run scope and delete only the newly created provisional session. Presence of a snapshot on another ID never authorizes reuse.

Run the Task 4 command. Expected: PASS.

### Task 5: Add Agent request/error/Session Hub metadata contracts

**Files:**

- Modify: `backend/session_hub_runtime.py`
- Modify: `backend/api/agent.py`
- Modify: `backend/main.py`
- Modify: `tests/test_agent_session_recovery_api.py`
- Modify: `tests/test_request_lifecycle_integration.py`
- Modify: `tests/test_agent_stop_api.py`
- Modify: `tests/test_runtime_config_limits.py`

- [ ] **Step 1: Add failing API and recovery tests**

Test that:

- `AgentChatRequest.tool_configuration` accepts the strict model and rejects extra/malformed fields;
- lifecycle payload fingerprint changes when either profile selection changes;
- invalid input is `422 invalid_tool_configuration`;
- stale catalog is `409 tool_catalog_changed`;
- frozen mismatch is `409 tool_configuration_locked`;
- an accepted Agent journal record contains exact `session_id` and `tool_configuration` snapshot metadata;
- Session Hub replay from sequence zero returns that same metadata for the exact recovered ID;
- Agent A recovery never returns Agent B metadata;
- Chat `request_accepted` payloads remain unchanged;
- `/config` exposes only safe ordered catalog metadata, defaults, schema version, and the active runner catalog revision—never prompt bodies.

- [ ] **Step 2: Run and confirm contract tests fail**

Run:

```bash
./.sglang/bin/python -m pytest \
  tests/test_agent_session_recovery_api.py \
  tests/test_request_lifecycle_integration.py \
  tests/test_agent_stop_api.py \
  tests/test_runtime_config_limits.py -q
```

Expected: FAIL because the request and acceptance metadata fields do not exist.

- [ ] **Step 3: Implement strict request and error mapping**

Add `tool_configuration: AgentToolConfigurationRequest | None` to `AgentChatRequest`. Passing it through `model_dump()` automatically makes request-ID idempotency sensitive to the candidate; add the regression rather than duplicating hashing logic.

Create one API helper that maps `AgentToolConfigurationError` to the stable detail object. Use it in strict staged admission and the standalone-router compatibility path. Refactor the no-registry path to use the same stage plus a no-op successful registration callback, so it cannot bypass selection validation.

- [ ] **Step 4: Publish snapshot metadata at the lock event**

Extend `SessionProducerPublisher.accept()` with optional acceptance metadata. Reject attempts to override reserved `type`, `request_id`, `session_id`, or `status` fields. Agent producer startup passes:

```python
{"tool_configuration": session.tool_configuration_snapshot.public_metadata(locked=True)}
```

Chat callers pass nothing. This is the canonical lock/recovery payload: the metadata is journaled with `request_accepted`, is filtered by exact request/session ownership on live callbacks, and is replayed from zero by Session Hub recovery. Do not duplicate it into timeline events or LLM messages.

Make `/config` read metadata from the active `app.state.agent_runner` catalog. The endpoint currently reloads YAML; do not advertise a freshly reloaded catalog revision that the still-running AgentRunner cannot accept. If no runner exists, compile a temporary catalog from the returned config solely for disabled/test-mode metadata.

- [ ] **Step 5: Rerun API/recovery tests**

Run the Task 5 command. Expected: PASS.

### Task 6: Preserve snapshots across children, Compact, Answer Now, Stop, security, and providers

**Files:**

- Modify: `backend/agent/agent_runner.py`
- Modify: `tests/test_manager_agent_dispatch.py`
- Modify: `tests/test_agent_child_capabilities.py`
- Modify: `tests/test_agent_security_config_integration.py`
- Modify: `tests/test_compact_agent_cache_first.py`
- Modify: `tests/test_agent_stop_api.py`
- Modify: `tests/test_multi_agent_runner.py`
- Modify: `tests/test_local_llm_tool_protocol.py`

- [ ] **Step 1: Add failing inheritance and preservation tests**

Assert:

- new and reused `(manager_session_id, child_id)` contexts use the parent's exact frozen `basic_agent` plan/fingerprint;
- a reused child whose fingerprint does not match its current parent is rejected before execution;
- one manager's child cannot use another manager's browser/default state;
- manager direct `write_file`/`edit_file`/`exec_command` wrappers are exposed by default but still fail closed on missing/mismatched capability or policy rejection;
- successful/manual/auto Compact keeps the same prompt/tool snapshot and clears only compactable memory fields;
- Answer Now uses the active frozen prompt/schema plus its existing no-tool tail and does not mutate the snapshot;
- Stop and the next turn preserve the snapshot;
- SGLang and vLLM adapter payloads receive identical selected tool schema lists;
- parser validation reports an unselected tool as unknown and never executes it.

- [ ] **Step 2: Run and confirm focused regressions fail**

Run:

```bash
./.sglang/bin/python -m pytest \
  tests/test_manager_agent_dispatch.py \
  tests/test_agent_child_capabilities.py \
  tests/test_agent_security_config_integration.py \
  tests/test_compact_agent_cache_first.py \
  tests/test_agent_stop_api.py \
  tests/test_multi_agent_runner.py \
  tests/test_local_llm_tool_protocol.py -q
```

- [ ] **Step 3: Implement preservation without new authority**

When creating a child shell, assign the parent's immutable snapshot object and derive its capability exactly as today. Build/reset its agent from `snapshot.basic_agent`. Store/validate the expected fingerprint in child admission metadata, not child task text.

Do not add configuration reads to Compact, Answer Now, Stop, model adapters, or parsers. Their correct implementation is to keep using the session's already-built agent and snapshot-owned rebuild paths. Keep the Answer Now tail and rejected-tool-call behavior unchanged.

- [ ] **Step 4: Rerun and inspect security failures**

Run the Task 6 command. Expected: PASS, including unchanged fail-closed error codes from security layers.

### Task 7: Route Cycle through the same admission model

**Files:**

- Modify: `backend/cycle/models.py`
- Modify: `backend/api/cycle.py`
- Modify: `backend/cycle/executor.py`
- Modify: `tests/test_cycle_api.py`
- Modify: `tests/test_cycle_executor.py`
- Modify: `tests/test_compact_cycle_integration.py`

- [ ] **Step 1: Add failing Cycle tests**

Cover:

- `agent_mode=false` accepts a well-formed optional field as irrelevant, does not pass it onward, and never stages an Agent snapshot;
- `agent_mode=true` deep-copies one valid candidate at Cycle start;
- `share_context=true` uses one Cycle Agent ID and one locked snapshot across all steps;
- `share_context=false` creates one independent canonical Agent ID per message step, each admitted from the same Cycle-start candidate copy;
- changing the original request object/defaults after start does not affect later steps;
- a failed step admission rolls back only that step ID;
- Cycle IDs/snapshots never overwrite normal page Agent sessions;
- Cycle auto-Compact retains the relevant step/session snapshot.

- [ ] **Step 2: Run and confirm Cycle tests fail**

Run:

```bash
./.sglang/bin/python -m pytest \
  tests/test_cycle_api.py \
  tests/test_cycle_executor.py \
  tests/test_compact_cycle_integration.py -q
```

- [ ] **Step 3: Implement typed candidate capture and staged runs**

Add optional `tool_configuration` to `CycleStartRequest` and pass it through `cycle_start()` to `CycleExecutor.start()`. Validate that it is absent/irrelevant when Agent mode is off. At `start()`, make one immutable/deep copy.

For every Agent message step, call `stage_agent_request_admission(llm_session_id, None, None, len(images or ()), tool_configuration=cycle_candidate)` and pass the stage to `run_stream()` with a Cycle-owned registration callback. Do not call legacy `run_stream(session_id, message, None, images=images)` directly. For `share_context=false`, use `stage.session_id` as the exact generated identity and retain the current targeted-stop bookkeeping.

Thread the captured candidate through recursive `_execute_steps()`/`_execute_message_step()` parameters; never reread YAML or frontend state mid-Cycle.

- [ ] **Step 4: Rerun Cycle tests**

Run the Task 7 command. Expected: PASS.

### Task 8: Implement frontend defaults, typed wire data, and exact-ID reducer

**Files:**

- Create: `frontend/src/lib/agent-tool-configuration.ts`
- Create: `frontend/src/lib/agent-tool-configuration.test.mts`
- Modify: `frontend/src/lib/message-types.ts`
- Modify: `frontend/src/lib/vision-automation/send-contract.ts`
- Modify: `frontend/src/lib/session-hub-client.ts`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/lib/cycle-types.ts`
- Modify: `frontend/src/lib/api.test.mts`
- Modify: `frontend/src/lib/message-types.test.mts`
- Modify: `frontend/src/lib/session-hub-client.test.mts`

- [ ] **Step 1: Add failing pure-domain and client tests**

Test:

- cache key presence wins independently for manager/basic, including `[]`;
- missing keys use server defaults;
- non-array malformed values fall back independently;
- duplicate/stale IDs are removed in catalog order and return one non-blocking notice;
- exactly the three approved suggestions, independently per profile;
- candidate JSON uses snake_case and contains no session/lock data;
- accepted metadata parses exact `sessionId`, locked snapshot, versions, and fingerprint;
- structured HTTP errors preserve `code` and are retryable only under existing busy/503 rules;
- `agentChatStream()` explicitly sends `tool_configuration`;
- `startCycle()` accepts `CycleStartRequest` rather than `any` and sends one candidate;
- reducer `begin`, `accept`, `reject`, `recover`, and `clear` mutate only the exact ID/request;
- stale A callbacks cannot lock/unlock B;
- OFF/ON leaves `pairedSessionId` unchanged;
- `begin` reuses the pair and never invokes the allocator while a pair exists;
- only `clear(pairedId)` permits later allocation.

- [ ] **Step 2: Run and confirm frontend domain tests fail**

Run from `frontend/`:

```bash
node --test --experimental-strip-types \
  src/lib/agent-tool-configuration.test.mts \
  src/lib/api.test.mts \
  src/lib/message-types.test.mts \
  src/lib/session-hub-client.test.mts
```

- [ ] **Step 3: Implement future defaults and session state separately**

Add `managerAgentTools: string[]` and `basicAgentTools: string[]` to `ChatSettings`/`DEFAULT_SETTINGS`; these are cached defaults, not active-session truth.

Implement pure helpers:

```ts
resolveAgentToolDefaults(serverAgentConfig, rawCache)
buildAgentToolConfigurationCandidate(resolvedDefaults, serverAgentConfig)
getAgentToolRecommendations(selectedTools)
reduceAgentToolSessionState(state, action)
resolvePairedAgentSessionId(state, allocate)
```

`resolvePairedAgentSessionId` must return the exact existing pair before it can call `allocate`. `recover` may pair only the explicitly recovered Agent ID passed by the current recovery attachment, and only when the current pair is null or already that same ID; a different existing pair must reject the recovery mutation until explicit Clear/disposal. The reducer may not inspect localStorage/bookmarks itself.

Extend `RequestAcceptedEvent` with optional `sessionId` and `toolConfiguration`. Validate metadata strictly when present. Existing Chat acceptance without metadata remains valid.

- [ ] **Step 4: Rerun the frontend domain tests**

Run the Task 8 Node command. Expected: PASS. Full page type-check is deliberately run after the shared page integration begins in Task 9 rather than accepting a known intermediate type failure.

### Task 9: Build the shared Advanced settings panel

**Files:**

- Create: `frontend/src/components/AgentAdvancedSettingsPanel.tsx`
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`
- Modify: `frontend/src/lib/timeline-projection.test.mts`

- [ ] **Step 1: Add failing two-page source/contract tests**

Assert both pages:

- render `常规设置` and `高级设置` tabs;
- use the same `AgentAdvancedSettingsPanel` component;
- place the Agent toggle and non-Agent system prompt in Advanced;
- do not retain a second copy of those controls in General;
- pass separate manager/basic selections and one exact session view;
- disable checklists unless Agent is ON and the current session view is editable;
- display locked snapshot selections and the Clear-to-reconfigure message;
- do not clear an Agent ID in the toggle callback.

- [ ] **Step 2: Run and confirm the UI contract fails**

Run from `frontend/`:

```bash
node --test --experimental-strip-types src/lib/timeline-projection.test.mts
```

- [ ] **Step 3: Implement the shared panel and tabs**

Give the component controlled props only; it must not read localStorage, allocate IDs, or infer recovery state. Recommended props:

```ts
interface AgentAdvancedSettingsPanelProps {
  agentMode: boolean;
  systemPrompt: string;
  catalog: AgentBusinessToolCatalogItem[];
  managerTools: string[];
  basicTools: string[];
  sessionRecord: AgentToolSessionRecord | null;
  disabled?: boolean;
  onAgentModeChange(value: boolean): void;
  onSystemPromptChange(value: string): void;
  onProfileToolsChange(profile: 'manager_agent' | 'basic_agent', tools: string[]): void;
}
```

Render warnings as non-blocking text boxes for exactly the three approved cases. Never auto-select a recommendation. When locked, derive checkmarks from `snapshot`, not `managerTools`/`basicTools`. When Agent is ON, disable the Chat system prompt with the existing explanation; when OFF, leave it editable and disable the tool checklists.

Reset-to-server-defaults may update `pendingSettings` future defaults only when no current locked snapshot is being displayed. Cancel must not mutate reducer/session state.

- [ ] **Step 4: Rerun UI contract and type-check**

Run:

```bash
node --test --experimental-strip-types src/lib/timeline-projection.test.mts
npm run type-check
```

### Task 10: Integrate Text page admission, recovery, OFF/ON pairing, and Clear

**Files:**

- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/lib/text-agent-session-handlers.ts`
- Modify: `frontend/src/lib/text-agent-session-handlers.test.mts`
- Modify: `frontend/src/lib/agent-tool-configuration.test.mts`
- Modify: `frontend/src/lib/timeline-projection.test.mts`

- [ ] **Step 1: Add failing Text lifecycle tests**

Pin these sequences:

1. blank Agent Send allocates ID A once, enters `pending(A, request)`, and sends the captured candidate;
2. matching `request_accepted(A, request)` locks A with backend metadata;
3. known pre-accept rejection unlocks/removes only provisional A;
4. rejection of a later A turn leaves A locked;
5. recovery for explicit A binds only A and displays A's backend snapshot while cached defaults remain unchanged;
6. stale acceptance/recovery for B cannot alter the page paired with A;
7. Agent ON(A) → OFF(Chat turns) → ON uses A, does not consult another bookmark, and does not call `createAgentSessionId()`;
8. transport detach/Stop/Answer Now error does not remove the A pair;
9. Clear targets A, removes only A after disposal initiation, and then permits one new allocation.

- [ ] **Step 2: Run and confirm Text tests fail**

Run from `frontend/`:

```bash
node --test --experimental-strip-types \
  src/lib/agent-tool-configuration.test.mts \
  src/lib/text-agent-session-handlers.test.mts \
  src/lib/timeline-projection.test.mts
```

- [ ] **Step 3: Wire the reducer at exact lifecycle boundaries**

Keep a reducer state/ref so async callbacks always compare current `sessionId` and `requestId`. On first Send, resolve/reuse the paired ID before attaching transport, capture `settings.managerAgentTools/basicAgentTools` once, dispatch `begin`, and send `tool_configuration`. Later locked turns omit it (or send the identical candidate only in the explicit idempotency test path).

On `request_accepted`, first verify live attachment, expected request ID, and accepted session ID, then dispatch `accept`. Recovery handlers dispatch `recover` from replayed accepted metadata only when the recovery attachment still owns that exact ID.

Remove `setAgentSessionId(undefined)` from Agent OFF handling and from generic transport-detach paths as a pairing operation. A transport may detach while the page pair remains. Keep active transport ownership and paired context ownership as separate concepts.

On Clear, resolve/delete the exact paired Agent ID and dispatch `clear` for that same ID. A Chat bookmark/ID may still be cleared through its existing path, but it must never replace the Agent pair target.

- [ ] **Step 4: Rerun Text tests and type-check**

Run the Task 10 Node command and `npm run type-check`. Expected: PASS.

### Task 11: Integrate Vision page and Vision Automation without cross-session mutation

**Files:**

- Modify: `frontend/src/app/vision/page.tsx`
- Modify: `frontend/src/lib/vision-automation/page-integration.ts`
- Modify: `frontend/src/lib/vision-automation/page-integration.test.mts`
- Modify: `frontend/src/lib/vision-agent-session-recovery.test.mts`
- Modify: `frontend/src/lib/timeline-projection.test.mts`

- [ ] **Step 1: Add failing Vision ownership tests**

Repeat all Text invariants and additionally assert:

- `AgentSessionOwnership` transport detach does not clear the page's Agent OFF/ON pair;
- Vision automation acceptance may lock only the binding's exact `contextId` and page generation;
- stale capture/restart/cleanup callbacks cannot lock, unlock, or recover another ID;
- an armed task cannot change Agent mode through settings, and tool checklist editing follows pending/locked rules;
- Answer Now/cleanup callbacks preserve the paired ID and snapshot;
- Clear/disposal uses the exact Vision pair and invalidates automation ownership before exposing a new editable context.

- [ ] **Step 2: Run and confirm Vision tests fail**

Run from `frontend/`:

```bash
node --test --experimental-strip-types \
  src/lib/vision-automation/page-integration.test.mts \
  src/lib/vision-agent-session-recovery.test.mts \
  src/lib/timeline-projection.test.mts
```

- [ ] **Step 3: Wire Vision through the same reducer**

Use the same candidate builder and reducer as Text. `effectiveAgentSessionId` must prefer the current automation binding ID when a bound automation submission owns the page; otherwise it must use the exact OFF/ON pair before allocation. Assert binding and pair equality whenever both exist.

Do not reuse `AgentSessionOwnership.detach()` as “forget the Agent context.” It owns current transport/session attachment only. Preserve the paired ID across mode changes, disconnects, Stop, and Answer Now; clear it only in the existing explicit Clear/disposal sequence after invalidating Vision automation page-generation ownership.

Pass the candidate on the first accepted Vision Agent context, including the first automation-triggered request. Matching acceptance metadata locks the exact context. Do not reread localStorage from capture-cycle callbacks.

- [ ] **Step 4: Rerun Vision tests and type-check**

Run the Task 11 Node command and `npm run type-check`. Expected: PASS.

### Task 12: Add frontend Cycle capture and finish cross-layer verification

**Files:**

- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/lib/cycle-types.ts`
- Modify: `frontend/src/lib/api.test.mts`
- Modify: `frontend/src/lib/timeline-projection.test.mts`
- Verify: all files listed above

- [ ] **Step 1: Add failing Cycle-start frontend tests**

Assert both pages capture one candidate before calling `startCycle`, include it only for Agent mode, and never rebuild it from settings/localStorage inside event callbacks. Assert changing settings after start cannot change the request object used by later backend steps.

- [ ] **Step 2: Run the focused frontend tests**

Run from `frontend/`:

```bash
node --test --experimental-strip-types \
  src/lib/agent-tool-configuration.test.mts \
  src/lib/api.test.mts \
  src/lib/timeline-projection.test.mts
```

- [ ] **Step 3: Implement explicit Cycle wire translation**

At `handleCycleStart`, build one candidate from the current future defaults and active catalog before any `await`; pass that immutable copy in `CycleStartRequest.tool_configuration`. Do not use a normal page Agent pair/session ID for Cycle. Keep Text and Vision call bodies identical apart from their existing camera/automation behavior.

- [ ] **Step 4: Run the complete focused backend suite**

From repository root:

```bash
./.sglang/bin/python -m pytest \
  tests/test_agent_tool_configuration.py \
  tests/test_agent_tool_session_admission.py \
  tests/test_multi_agent_config.py \
  tests/test_browser_command_visibility.py \
  tests/test_get_tools_no_load_images.py \
  tests/test_manager_agent_dispatch.py \
  tests/test_multi_agent_runner.py \
  tests/test_agent_session_context.py \
  tests/test_agent_session_recovery_api.py \
  tests/test_agent_stop_api.py \
  tests/test_request_lifecycle_integration.py \
  tests/test_agent_child_capabilities.py \
  tests/test_agent_security_config_integration.py \
  tests/test_compact_agent_cache_first.py \
  tests/test_local_llm_tool_protocol.py \
  tests/test_cycle_api.py \
  tests/test_cycle_executor.py \
  tests/test_compact_cycle_integration.py \
  tests/test_runtime_config_limits.py -q
```

- [ ] **Step 5: Run the complete focused frontend suite**

From `frontend/`:

```bash
node --test --experimental-strip-types \
  src/lib/agent-tool-configuration.test.mts \
  src/lib/api.test.mts \
  src/lib/message-types.test.mts \
  src/lib/session-hub-client.test.mts \
  src/lib/text-agent-session-handlers.test.mts \
  src/lib/vision-agent-session-recovery.test.mts \
  src/lib/vision-automation/page-integration.test.mts \
  src/lib/timeline-projection.test.mts
npm run type-check
npm run build:verify
```

- [ ] **Step 6: Run syntax and broad regression checks**

From repository root:

```bash
./.sglang/bin/python -m compileall backend
./.sglang/bin/python -m pytest tests/test_agent_capabilities.py tests/test_agent_security_end_to_end.py tests/test_tool_wrapper_security.py -q
```

- [ ] **Step 7: Perform the final invariant audit**

Inspect evidence, not just exit codes:

- Compare one empty, one manager-default, one basic-default, and one all-selected rendered prompt/schema fixture.
- Verify no unselected business name or owned prompt segment appears.
- Verify manager dispatch exists outside the selected set.
- Verify old split Wikipedia tools were not edited or exposed.
- Verify localStorage contains only future defaults.
- Verify two simultaneous IDs retain independent snapshot/fingerprint/display state.
- Verify OFF(A) → ON returns A even when another Agent bookmark/session B is present and even when the allocator is instrumented to fail if called.
- Verify no tool configuration appears in canonical Agent memory, timeline projection, routing records, Compact source messages, or child task text.
- Verify security policy/capability objects are unchanged except for receiving the already selected wrapper set.

Do not claim completion if any of these checks lacks a test or direct inspection result.

---

## Future delegation map (do not dispatch yet)

When the user authorizes execution, use at most three implementation workers and preserve context reuse:

1. **Backend catalog/runtime worker:** Tasks 1–7 serially. These tasks share `backend/config.py`, `backend/agent/agent_runner.py`, and the snapshot types; splitting them across simultaneous workers would create conflicts.
2. **Frontend state/UI worker:** Tasks 8–12 serially after the backend wire shapes from Tasks 1 and 5 are stable. This worker owns the shared reducer/component and both pages to prevent Text/Vision drift.
3. **Independent review worker:** Read-only review after integration, focused on prompt/schema leakage, prefix-cache invariants, exact-ID OFF/ON pairing, Cycle `share_context=false`, and unchanged security authority.

The primary agent coordinates checkpoints, resolves cross-layer interface changes, runs the final combined verification, and does not ask workers to use git. Any later git action requires a separate explicit user authorization.
