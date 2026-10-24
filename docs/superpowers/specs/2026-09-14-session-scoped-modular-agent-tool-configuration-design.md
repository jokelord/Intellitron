# Session-Scoped Modular Agent Tool Configuration — SPEC/PRD

> Status: approved; implementation plan requested on 2026-09-14
> Date: 2026-09-14
> Scope: Agent business-tool selection, prompt modularization, settings UI, session admission, Cycle, and Session Hub recovery
> Depends on: the Prefix-Cache Covenant and Agent security rules in `AGENTS.md`
> Implementation plan: to be written only after this specification is reviewed and approved

## 1. Summary

Add a configurable business-tool list for `manager_agent` and `basic_agent`. YAML provides server defaults; browser `localStorage` overrides those defaults only for future Agent contexts. The first accepted Agent request freezes an immutable, session-scoped tool configuration. Every later profile build, handoff, manager child, Compact, Answer Now, Stop continuation, Cycle step, and Session Hub restore uses the snapshot bound to that exact `session_id`.

Selecting a business tool has one uniform meaning: it is present in the runtime tool collection, API tool schema, system-prompt tool catalog, and its modular usage guidance. An unselected business tool must be absent from all four. Tool selection changes model-visible capability only; it never grants filesystem, command, browser, image, or other side-effect authority. Existing `OperationCapability`, `PathPolicy`, `CommandPolicy`, `EnvironmentBuilder`, and `ProcessSupervisor` remain authoritative.

The settings modal gains an Advanced tab. It contains the Agent mode toggle, the existing non-Agent custom system prompt, and separate manager/basic business-tool checklists. Checklists are editable only for a blank Agent context while Agent mode is enabled. A successful first-request admission locks them. Clearing the conversation disposes that context and unlocks configuration for a new one.

## 2. Goals

1. Let users choose separate business-tool sets for `manager_agent` and `basic_agent` before a new Agent context starts.
2. Preserve browser-cache-over-YAML precedence by key presence, including valid empty arrays.
3. Freeze tool lists and all tool-guidance modules per top-level Agent session after first-request acceptance.
4. Make the selected business-tool set the single source for runtime tools, schemas, prompt guidance, and visible tool descriptions.
5. Refactor scattered tool instructions into shared per-tool modules and declarative cross-tool modules.
6. Allow manager to directly write, edit, and execute commands when those tools are selected, while retaining manager dispatch responsibilities.
7. Prevent cross-session state confusion across text, vision, Cycle, Session Hub recovery, multiple tabs, profile switches, and manager children.
8. Preserve prefix-cache behavior after the intentional first-request/profile tool-schema boundary.
9. Preserve all existing backend security and capability enforcement.

## 3. Non-goals

- No support for dynamically loaded MCP tools in this iteration.
- No changes to `wikipedia_search`, `wikipedia_fetch`, or `wikipedia_summary`; they are not selectable and their code is out of scope.
- No changes to the special handling of `final_answer`, `switch_agent`, or the managed `basic_agent` handoff proxy.
- `dispatching_parallel_agent` is not a selectable business tool. It remains a mandatory manager runtime control tool.
- No per-session configuration is stored in `localStorage`; browser storage contains defaults for future contexts only.
- No tool-prompt text editor in the Web UI.
- No automatic dependency selection and no blocking dependency validation in the UI.
- No browser-related dependency recommendation or browser cross-tool prompt module in the first iteration.
- No permission, root, allowlist, environment, or sandbox configuration through the tool checklist.
- No tool-list changes after a session is locked, including after Compact or Stop.

## 4. Terminology

- **Business tool**: a normal user-selectable function tool such as `read_file`, `write_file`, or `exec_command`.
- **Special protocol/control tool**: internal final-answer, routing, handoff, or manager-dispatch machinery outside the selectable business-tool model.
- **Candidate configuration**: browser/YAML-resolved manager/basic selections before first Agent acceptance.
- **Frozen snapshot**: the immutable configuration bound to one top-level Agent `session_id`.
- **Catalog revision**: a stable identifier for the configured business-tool catalog and prompt-module definitions.
- **Configuration fingerprint**: a deterministic hash of the canonical snapshot inputs, including schema version, catalog revision, normalized profile selections, and frozen prompt-plan revision/content hash.
- **Blank Agent context**: no Agent request has been accepted for the current top-level Agent session. Ordinary non-Agent Chat turns do not make an Agent context non-blank.

All requirements concerning selectable, empty, default, or frozen tool lists refer only to business tools. Special protocol/control tools are never included in those lists or counts.

## 5. Approved Product Decisions

1. Use a session-owned immutable snapshot architecture.
2. Permanent locking occurs when the backend successfully accepts the first Agent request, not merely when the user clicks Send.
3. The frontend temporarily disables editing while first-request admission is pending and unlocks again on a known pre-accept rejection.
4. Tool configuration is keyed exclusively by canonical top-level Agent `session_id`.
5. Different session IDs have independent lock state and snapshots.
6. Ordinary Chat turns do not freeze Agent tool settings. Chat-to-Agent transition freezes the current candidate on the first accepted Agent request.
7. Agent OFF-to-ON returns to the same existing Agent session and frozen snapshot. It must not create a new configurable Agent session merely to bypass the lock.
   OFF and ON are a paired state transition for the current page context: when the pair already owns an accepted Agent session ID, ON must use that exact ID. It must not select another recovered/recent session, fall back to a global session pointer, or allocate a replacement ID.
8. Clearing the conversation is the user-facing disposal/unlock operation.
9. An existing locked session receiving a different configuration fails explicitly with `409 tool_configuration_locked`.
10. Manager and basic share one canonical prompt module per business tool.
11. Empty business-tool lists are valid. Manager still owns its mandatory dispatch control tool.
12. All selectable business tools use symmetric visibility: selected means runtime + schema + prompt guidance + tool listing; unselected means absent from all four.
13. `wikipedia_search_and_fetch` is selectable but disabled by default.
14. Agent-mode Cycle uses the captured browser candidate and follows the same per-session locking model.

## 6. Business-Tool Catalog and Defaults

### 6.1 Selectable catalog

The first release supports these built-in business tools:

- `read_file`
- `write_file`
- `edit_file`
- `glob`
- `grep`
- `exec_command`
- `browser_command`
- `check_process`
- `list_skills`
- `read_skill`
- `web_search`
- `web_fetch`
- `search_and_fetch`
- `wikipedia_search_and_fetch`

MCP-discovered tools and all special protocol/control tools are excluded.

### 6.2 YAML defaults

`config/config-agent-operation.yaml` owns default selections:

```yaml
agent:
  default_profile_tools:
    manager_agent:
      - read_file
      - write_file
      - edit_file
      - glob
      - grep
      - exec_command
      - list_skills
      - read_skill
      - web_fetch
      - search_and_fetch

    basic_agent:
      - read_file
      - write_file
      - edit_file
      - glob
      - grep
      - exec_command
      - browser_command
      - check_process
      - list_skills
      - read_skill
      - web_search
      - web_fetch
      - search_and_fetch
```

`wikipedia_search_and_fetch` is present in the catalog but absent from both defaults.

### 6.3 Startup validation

Configuration loading must fail fast when:

- a default list contains duplicates, unknown tools, unavailable tools, or special protocol/control tools;
- a selectable tool has no UI metadata or canonical prompt module;
- a prompt module names an unregistered or non-selectable tool;
- a combination requirement names an unknown/non-selectable tool;
- a required field or catalog revision is invalid;
- the business-tool registry, configurable catalog, and prompt-module catalog disagree.

Silent omission is forbidden because it would make the UI, schema, and model prompt disagree.

## 7. Prompt Architecture

### 7.1 Profile base prompts

Profile prompts contain identity and role behavior only.

- Manager retains task decomposition, direct-versus-delegated execution judgment, child briefing, blackboard, and synthesis rules.
- Basic retains general execution-agent identity and response-quality rules.
- Manager's current unconditional statements that it is not a direct executor and cannot write or execute commands must be removed. Its behavior must be compatible with the selected business tools.
- Profile base prompts must not unconditionally name configurable business tools.
- Special final-answer/routing/handoff rules remain in their existing profile/runtime scope.

### 7.2 Generic business-tool protocol

A shared protocol module describes only mechanics common to every business tool:

- canonical framed tool-call syntax and strict JSON shape;
- system-supplied observations;
- same-turn independent concurrency;
- future-turn sequencing for dependencies and write/execute conflicts;
- error analysis without blind retries;
- final natural-language response after work completes;
- prohibition on fabricated observations.

It contains no concrete business-tool name or fixed business-tool example. Basic omits it when its business-tool selection is empty. Manager keeps its separate mandatory dispatch protocol regardless of an empty business-tool selection.

### 7.3 Canonical per-tool modules

`config/config-agent-prompts.yaml` stores one shared module per selectable business tool. Manager and basic use the same module. A single-tool module may name only its own business tool; references to other business tools belong in combination modules.

The existing mixed instructions must be classified by the following ownership rules. The implementation plan must audit every current sentence and move it to exactly one base, single-tool, or combination module:

- `read_file`: file-reading purpose and observation handling.
- `write_file`: new-file-only behavior, JSON content escaping, and safe-write expectations.
- `edit_file`: modification/append/delete semantics and empty `search_text` append behavior.
- `glob`: filename/path-pattern search rules and result limits.
- `grep`: content search, `files_with_matches`, include filters, offset/head-limit pagination.
- `exec_command`: restricted-shell safety, timeout selection, direct argv expectations, Python/uv runtime paths, and no virtual-environment/source behavior.
- `browser_command`: its own direct browser-operation guidance only.
- `check_process`: process-status checking behavior.
- `list_skills`: when discovering SOPs is useful.
- `read_skill`: Skill reading and the rule that a Skill never grants capability.
- `web_search`: direct search behavior.
- `web_fetch`: known-URL retrieval behavior.
- `search_and_fetch`: broad composite search/fetch behavior and result interpretation.
- `wikipedia_search_and_fetch`: focused Wikipedia behavior.

Tool descriptions/docstrings must also be audited. A selected tool's generated prompt/schema description must not name an unselected business tool.

### 7.4 Declarative combination modules

The first release supports only `requires_all`:

```yaml
tool_prompt_combinations:
  - id: file_create_vs_edit
    requires_all: [write_file, edit_file]
    prompt: |
      ...

  - id: skill_discovery_and_read
    requires_all: [list_skills, read_skill]
    prompt: |
      ...
```

No nested expressions or `any_of` are required. A complex rule is split into small deterministic modules. Initial combinations must cover:

- `write_file + edit_file`;
- `read_file + write_file`;
- `read_file + edit_file`;
- `glob + grep`;
- `list_skills + read_skill`;
- `exec_command + read_skill`;
- `exec_command + check_process`;
- `write_file + edit_file + exec_command`;
- search-tool priority rules for the exact tools they mention.

Browser cross-tool combinations are deferred.

### 7.5 Deterministic Jinja2 rendering

The profile system-prompt templates expose explicit module slots. Each slot must have a short Chinese YAML/Jinja2 maintenance comment describing its source and injection condition. Comments must not render into the model prompt.

```jinja2
{# 注入通用业务工具调用协议；没有已选业务工具时为空 #}
{{ business_tool_protocol }}

{# 按工具目录固定顺序注入已选工具的独立使用技巧 #}
{% for module in selected_tool_prompt_modules %}
{{ module }}
{% endfor %}

{# 仅当 requires_all 中的工具全部选中时注入组合规则 #}
{% for module in selected_combination_prompt_modules %}
{{ module }}
{% endfor %}

{# 仅列出当前 profile 已选并可调用的一般业务工具 #}
{% for tool in selected_business_tools %}
- {{ tool.to_tool_calling_prompt() }}
{% endfor %}
```

Rendering follows the server catalog order, never client order or unordered-set iteration. The same canonical selection drives runtime tools, API schemas, per-tool modules, combination modules, and the available-tool listing.

### 7.6 Hard visibility invariant

For every profile and session:

```text
selected business tools
  == runtime business tools
  == schema-visible business tools
  == prompt-listed business tools
  == tools whose guidance modules were injected
```

Unselected business-tool names, arguments, examples, and guidance must not leak through the profile base prompt, generic protocol, another tool's module/description, combination modules, examples, or API schema. Special protocol/control tools are asserted separately and are not members of this equality.

The current business-tool asymmetries are removed:

- `browser_command` is no longer schema-only when selected;
- `web_search` is no longer prompt-only when selected;
- `wikipedia_search_and_fetch` becomes prompt/schema/runtime-visible when selected.

## 8. Session Snapshot and Admission

### 8.1 Request contract

`AgentChatRequest` gains an optional first-request configuration:

```text
tool_configuration:
  schema_version
  catalog_revision
  manager_agent_tools
  basic_agent_tools
```

The Web UI sends the full candidate on the first request. Non-Web/API clients may omit it; omission causes YAML defaults to be resolved and frozen.

The request-lifecycle payload fingerprint must include the candidate configuration. Reusing one `request_id` with a different candidate must not be treated as an idempotent retry.

### 8.2 Canonical snapshot

The admission stage owns an immutable snapshot value containing at least:

- configuration schema version;
- catalog revision;
- normalized manager/basic business-tool tuples in catalog order;
- the frozen per-profile prompt-module plan or its immutable resolved representation;
- a deterministic configuration fingerprint;

The snapshot value itself never mutates. It must exist before constructing the first agent because the first LLM request already needs the selected prompt and schema. Before acceptance it belongs to the provisional admission stage; publishing `request_accepted` atomically binds that same value to `AgentSession` as its frozen snapshot. Locked/provisional lifecycle state is stored outside the immutable value. Any pre-accept validation, construction, transport, or publication failure rolls back the staged state and removes a newly created provisional session.

### 8.3 Existing-session behavior

- Omitted later configuration uses the frozen snapshot.
- Resubmitting the exact configuration/fingerprint is idempotent.
- A different configuration returns HTTP `409` with stable code `tool_configuration_locked`.
- Invalid names, duplicates, special names, or malformed versions return HTTP `422` with stable code `invalid_tool_configuration`.
- A stale catalog revision rejects first admission and instructs the frontend to reload `/config`; it must not silently reinterpret the selection.
- Stop, timeout, disconnect, Answer Now, Compact, or a normal model/tool failure never unlocks or mutates the snapshot.

### 8.4 Profile construction

The static profile allowlist is reduced to non-configurable profile contracts:

- coordinator profiles receive no selectable business tools;
- manager resolves the snapshot's manager tuple and then adds mandatory `dispatching_parallel_agent` through existing runtime logic;
- basic resolves the snapshot's basic tuple;
- special protocol/control tools keep their current handling.

`_build_profile_agent()`, `_activate_profile_agent()`, initial session construction, and child reconstruction/reset must receive or resolve the exact owning snapshot. They may not reread browser state or current YAML defaults for an existing session.

## 9. Manager and Child Semantics

Manager may directly use every selected business tool, including default `write_file`, `edit_file`, and `exec_command`. It still delegates large independent work when useful and retains mandatory dispatch behavior.

Each manager-owned child uses the parent manager session's frozen `basic_agent` selection. New child creation and old child reuse must validate the expected tool-configuration fingerprint. Child capability continues to inherit or narrow the parent's immutable `OperationCapability`; tool names and child task text are not authorization.

No UI exposes independent child tool settings. `(manager_session_id, child_id)` remains the context identity mapping, and a child's configuration cannot be sourced from the active browser tab or another manager session.

## 10. Frontend Product Design

### 10.1 Settings tabs

The existing settings modal gains:

- **常规设置**: sampling, reasoning display, Enter-to-send, skin, and existing general options.
- **高级设置**: Agent mode toggle, ordinary Chat custom system prompt, manager business-tool checklist, and basic business-tool checklist.

The custom system prompt remains non-Agent-only:

- Agent OFF: editable and sent only through the ordinary Chat path.
- Agent ON: disabled with an explanation that Agent profiles use backend prompts.

### 10.2 Frontend state machine

Tool configuration is not represented by one global lock boolean. The current bound Agent context uses:

```text
editable
pending(session_id, candidate)
locked(session_id, snapshot, fingerprint)
```

- Agent OFF disables the checklists but preserves the relevant candidate or frozen display state.
- Agent ON with a blank Agent context allows editing.
- First Send enters `pending` immediately.
- Matching `request_accepted` for the same session commits `locked`.
- A known pre-accept rejection returns a brand-new context to `editable`.
- A failure on an already locked context leaves it locked.
- Agent OFF-to-ON reuses the same accepted Agent session ID and frozen snapshot.
- Clearing the conversation disposes/unbinds the current context, clears its session-bound state, and exposes the cached future-context defaults again.

All callbacks must verify their starting page generation/mode/session ownership before changing state. A stale callback from another request or session cannot lock/unlock the current context.

### 10.3 `/config` response

The backend publishes only UI-safe metadata:

```text
agent.business_tool_catalog
agent.default_profile_tools.manager_agent
agent.default_profile_tools.basic_agent
agent.tool_configuration_schema_version
agent.tool_catalog_revision
```

Each catalog item includes stable tool ID, Chinese label, concise description, and deterministic display order. Full prompt text is not sent to the browser. Special/legacy tools are absent.

### 10.4 Browser-cache precedence

Continue using `llm-chat-settings`, adding independent top-level keys:

```text
managerAgentTools
basicAgentTools
```

Merge by key presence:

- a present cache key wins over YAML, including `[]`;
- a missing key falls back to the corresponding YAML default;
- malformed non-array data falls back to YAML;
- duplicates are removed in catalog order;
- unknown/stale IDs are removed and produce a non-blocking one-time UI notice;
- manager/basic keys are merged independently.

`localStorage` never stores a session ID or frozen session state. At first Send, the frontend copies the resolved candidate into the exact new `session_id` request. After acceptance, the backend session is authoritative. Session recovery must fetch the snapshot by recovered ID rather than infer it from cache. Changes to browser defaults in another context/tab affect only future sessions.

### 10.5 Dependency suggestions

Each profile independently displays non-blocking text boxes for exactly these first-release cases:

1. `read_skill` selected and `list_skills` missing: suggest selecting `list_skills`.
2. `write_file` selected and `read_file` missing: suggest selecting `read_file`.
3. `edit_file` selected and `read_file` missing: suggest selecting `read_file`.

Suggestions do not auto-select, block Save, alter backend validation, or enter the model prompt.

### 10.6 Locked-state presentation

A locked checklist displays the owning session snapshot, not current browser defaults. It includes an explanation equivalent to: “工具配置已随当前 Agent 上下文锁定；清空对话后可重新配置。” Reset-to-server-defaults may change a blank candidate but must not change a frozen display.

Text and Vision pages must implement equivalent behavior.

## 11. Agent Mode Transitions

Ordinary Chat and Agent retain separate backend context identities. Ordinary Chat turns do not freeze an Agent configuration. If a user switches from Chat to Agent, the first accepted Agent request freezes the then-current candidate and may bootstrap visible prior chat turns using the existing canonical import path.

Once an Agent session has been accepted, switching Agent mode OFF must not discard its local ID merely to unlock configuration. Switching ON again resumes that same ID and snapshot. Intervening Chat turns follow the existing context-message bootstrap/extension behavior; this feature must not invent a new history serialization or insert tool configuration into messages.

The OFF/ON pair must retain the exact bound Agent session ID in page-context state. If that paired ID exists, the ON transition is forbidden from consulting another bookmark, another tab's current session, a “most recent session” fallback, or `createAgentSessionId()`. A new ID may be allocated only when the current page context has never had an accepted Agent session or after explicit clear/disposal has removed the pair.

Only explicit conversation clearing/disposal creates the UI state for a fresh configurable Agent context.

## 12. Cycle Integration

`CycleStartRequest` gains an optional business-tool candidate. The frontend captures the resolved future-context defaults once when Cycle starts; the executor never rereads browser state during a run.

- `agent_mode=false`: tool configuration is irrelevant and not admitted.
- `agent_mode=true, share_context=true`: the first Agent message step creates/accepts the Cycle Agent session and freezes one snapshot for the Cycle session ID.
- `agent_mode=true, share_context=false`: every Agent message step gets an independent canonical session ID and independent admission, but every step starts from the same Cycle-start candidate copy.
- A pre-accept failure for one step leaves no locked provisional session for that step.
- Cycle sessions never modify a normal Chat/Agent session snapshot.

Cycle's internal AgentRunner calls must reuse the unified tool-configuration admission helper rather than bypass it through legacy get/create behavior.

## 13. Compact, Answer Now, Stop, and Provider Equivalence

- Compact uses the session's frozen prompt/schema source and canonical memory. It never rebuilds tools from current YAML or browser defaults.
- Successful Compact is an existing legitimate memory/prefix boundary, not a tool-configuration boundary or unlock event.
- Answer Now preserves the active agent's normal frozen system/runtime/tool schema and canonical history, then adds only its existing no-tools final user tail. Emitted tool calls remain unexecuted as currently required.
- Stop changes lifecycle only. The following turn uses the same snapshot.
- Profile switches use the target profile selection from the same snapshot.
- Equivalent SGLang and vLLM requests use equivalent messages and tool schemas.

After the initial intentional tool/profile boundary, prompt construction is append-only and byte-stable except for the legitimate boundaries already listed in `AGENTS.md`.

## 14. Session Hub Recovery and Multi-Tab Robustness

Reuse existing Session Hub/session metadata channels to return:

- session ID;
- locked status;
- manager/basic selected business-tool IDs;
- schema/catalog revisions;
- configuration fingerprint.

Do not add these values to LLM history, timeline projection, frontend bubbles, routing state, or child task text. Do not create a new top-level Agent SSE protocol solely for tool settings.

Recovery resolves the frozen snapshot from the exact recovered ID. If browser defaults differ, the restored session remains locked to its backend snapshot and the browser defaults remain unchanged for future sessions.

If two tabs accidentally submit different first configurations for the same session ID, admission serialization allows only the first accepted configuration. The other request fails with `409 tool_configuration_locked`. No “current page,” “last active session,” or global snapshot fallback is permitted.

If the backend no longer has the recovered session, use the existing unavailable/expired recovery flow. Never reconstruct a claimed old snapshot from `localStorage`.

## 15. Security Requirements

Tool selection controls exposure, not authority.

- Every `AgentSession` still owns an immutable matching `OperationCapability`.
- File tools still use capability-bound `PathPolicy`, including containment, no-follow, type, size, overwrite, atomicity, and race checks.
- Command/browser tools still use `CommandPolicy`, `EnvironmentBuilder`, and `ProcessSupervisor` with controlled argv/shell mode, environment, cwd, timeout, output bounds, process groups, and targeted abort.
- Skills never grant additional capability.
- Child capability inherits or narrows parent capability.
- Direct library calls and wrapper/session mismatches remain fail-closed.
- Prompt text, UI state, selected tool names, and child task text cannot modify roots, allowlists, environment, or side-effect policy.

The manager's new default write/edit/exec exposure therefore does not weaken backend enforcement.

## 16. Error Contract

Use stable machine-readable codes and user-readable messages:

- `invalid_tool_configuration` (`422`): malformed version/list, duplicates, unknown/unavailable names, or forbidden special names.
- `tool_catalog_changed` (`409`): stale catalog/schema revision; frontend refetches `/config` before retrying.
- `tool_configuration_locked` (`409`): different candidate/fingerprint submitted for an existing locked session.
- `tool_prompt_render_failed` (`500`): server configuration/rendering fault; provisional admission rolls back.

No error path may silently widen tools, silently replace a locked snapshot, or fall back to unrestricted execution.

## 17. Testing Strategy

### 17.1 Backend configuration and rendering

- YAML parsing for defaults, explicit empty lists, duplicates, unknown tools, special tools, missing modules, and invalid combination requirements.
- Deterministic catalog-order normalization and fingerprints.
- Empty, default, all-selected, each-singleton, and combination selections.
- Exhaustive or equivalently structural validation over the feasible 14-tool subset space.
- Equality of selected runtime tools, schema tools, prompt-listed tools, and injected tool modules.
- No unselected business-tool name leakage through base prompts, modules, combination rules, descriptions/docstrings, examples, or schemas.
- Manager mandatory dispatch remains available and outside the business-tool equality.
- Selected business-tool prompt/schema symmetry for `browser_command`, `web_search`, and `wikipedia_search_and_fetch`.

### 17.2 Backend lifecycle

- First admission commit and every pre-accept rollback point.
- Same configuration idempotency and different configuration `409`.
- Concurrent first admission for one session ID.
- Requests omitting configuration use YAML defaults.
- Profile switch retains the same snapshot.
- New/reused child inherits and validates the parent basic fingerprint.
- Stop, Answer Now, manual Compact, and auto-Compact do not mutate configuration.
- Cycle with both `share_context` values.
- Session Hub restore metadata and expired-session behavior.
- Capability missing/mismatch and policy rejection remain fail-closed for newly selectable manager side-effect tools.
- Equivalent SGLang/vLLM prompt and schema shapes.
- Prompt snapshots remain stable after locking.

### 17.3 Frontend

Both `frontend/src/app/page.tsx` and `frontend/src/app/vision/page.tsx` must cover:

- Advanced tab placement and Chat prompt enablement rules.
- Agent OFF/ON plus editable/pending/locked state transitions.
- Cache-key presence precedence, valid empty arrays, malformed data, and stale IDs.
- Independent manager/basic selection.
- Exactly the three approved non-blocking suggestions.
- First request candidate payload.
- Matching acceptance locks the correct session only.
- Known pre-accept rejection unlocks only the matching new session.
- Stale callbacks cannot change another session.
- Agent OFF-to-ON keeps the exact paired Agent ID and snapshot, never substitutes another session, and never allocates a new ID while the pair exists.
- Clear/dispose unlocks a new context.
- Recovery displays backend snapshot without overwriting browser defaults.
- Multi-tab/same-ID conflict handling.
- Cycle-start capture and no mid-Cycle cache reread.
- Existing request wire translation remains explicit.

Run proportional `.sglang/bin/python -m pytest`, Python compilation checks, frontend unit/contract tests, and TypeScript type-check. Do not weaken safety policy to make tests pass.

## 18. Acceptance Criteria

The feature is complete only when all of the following hold:

1. A new Agent context can independently configure manager/basic business tools from the Advanced tab.
2. Browser key-presence values override YAML defaults, including empty arrays.
3. First accepted Agent request freezes a snapshot under the exact session ID; pre-accept failure leaves it editable.
4. Existing sessions cannot be changed, and a mismatch returns `409 tool_configuration_locked`.
5. Clearing is the ordinary unlock/new-context action; Agent OFF-to-ON restores the exact paired ID and cannot bypass locking by selecting or creating another session.
6. Selected business tools are identical across runtime, schema, prompt list, and prompt modules.
7. Unselected business tools do not leak through model-visible prompt/schema content.
8. Manager defaults include `write_file`, `edit_file`, and `exec_command` while backend security remains unchanged.
9. Empty business-tool selections work; manager still has mandatory dispatch.
10. `wikipedia_search_and_fetch` is optional and default-off; the three old Wikipedia tools are untouched.
11. Child, Cycle, Compact, Answer Now, Stop, recovery, and both providers use the correct frozen snapshot.
12. Text and Vision pages remain behaviorally synchronized.
13. Session A and session B cannot overwrite, unlock, render, or execute with each other's tool configuration.

## 19. Expected Implementation Areas

Likely files include, subject to implementation-plan verification:

- `config/config-agent-operation.yaml`
- `config/config-agent-prompts.yaml`
- `backend/config.py`
- `backend/main.py`
- `backend/api/agent.py`
- `backend/agent/agent_runner.py`
- `backend/agent/local_llm_model.py`
- `backend/cycle/models.py`
- `backend/cycle/executor.py`
- `backend/api/cycle.py`
- `frontend/src/lib/api.ts`
- `frontend/src/lib/message-types.ts`
- `frontend/src/app/page.tsx`
- `frontend/src/app/vision/page.tsx`
- relevant backend/frontend contract, security, prompt, session, Cycle, and recovery tests

The implementation plan must verify exact symbols and may introduce focused helper modules/components to avoid enlarging already complex page and runner files. It must not perform unrelated refactoring.
