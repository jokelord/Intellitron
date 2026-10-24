# Agent Capability and Configuration Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Enforce operation capabilities at AgentSession and tool-wrapper boundaries, propagate only inherited or narrower permissions to child Agents, and prove all security configuration reaches runtime behavior.

**Architecture:** Create one resolved capability when a session is admitted, attach it to the session identity, and pass it to every profile Agent and wrapper. Manager dispatch keeps its canonical child_id/task schema; task text never grants permission. File and command plans provide the concrete path and process policy implementations consumed here.

**Tech Stack:** Python 3.12, dataclasses, FastAPI/Pydantic request validation, smolagents wrappers, pytest, existing uv-managed .sglang environment.

**Spec:** docs/superpowers/specs/2026-09-13-agent-operation-security-hardening-design.md

## Global Constraints

- Do not modify API authentication, listener binding, CORS, or the remote safety topic.
- Do not change the configured root set as part of this plan.
- Do not add may_write, reserved_files, subtask, or other permission fields to the canonical dispatching_parallel_agent arguments; the contract remains child_id and task.
- Do not use task prose, prompt content, frontend timeline, or model output as authorization.
- Preserve manager read-only tool behavior and existing SSE/timeline contracts.
- Use ./.sglang/bin/python -m pytest.
- Do not use git commands without separate user permission.

## File Map

- Create: backend/security/capabilities.py — immutable capability and derivation functions.
- Modify: backend/agent/agent_runner.py — attach capabilities to sessions, children, profile Agents, and wrappers.
- Modify: backend/api/agent.py — resolve and validate capability/session admission data.
- Modify: backend/config.py — parse the resolved security policy and validate profile defaults.
- Modify: config/config-agent-operation.yaml — enable explicit policy fields after tests define them.
- Modify: backend/tools/file_ops.py and backend/tools/sys_ops.py only where wrapper signatures need capability injection.
- Test: tests/test_agent_capabilities.py
- Test: tests/test_agent_child_capabilities.py
- Test: tests/test_tool_wrapper_security.py
- Test: tests/test_agent_security_config_integration.py

## Interfaces

The plan consumes or produces:

    def build_session_capability(config, *, session_id, mode) -> OperationCapability

    def derive_child_capability(
        parent: OperationCapability,
        *,
        child_id: str,
    ) -> OperationCapability

    def require_capability(
        capability: OperationCapability,
        operation: str,
        target: object,
    ) -> None

    @dataclass
    class AgentSession:
        capability: OperationCapability

Child derivation must be monotonic: every child field is equal to or narrower than the parent, and no task string is accepted as an input to derivation.

## Task 1: Capability model and session admission

**Files:**
- Create or modify: backend/security/capabilities.py
- Modify: backend/config.py
- Modify: backend/api/agent.py
- Modify: backend/agent/agent_runner.py
- Test: tests/test_agent_capabilities.py
- Test: tests/test_agent_security_config_integration.py

**Interfaces:**
- Consumes: resolved path/command/process policies from the filesystem and command plans.
- Produces: build_session_capability and immutable AgentSession.capability used by all profiles.

- [ ] Write failing tests for capability construction from config, missing required policy, invalid mode, and bounded numeric settings.
- [ ] Write failing tests proving a session cannot be created with a malformed session ID or a capability missing its session identity.
- [ ] Write compatibility tests proving backend-generated canonical UUIDs work for new sessions, existing canonical sessions can be reused, existing legacy safe tokens can be reused, unknown legacy tokens cannot create sessions, and invalid IDs are rejected before session-store or filesystem access.
- [ ] Write failing tests proving capability objects are immutable and cannot be expanded after session creation.
- [ ] Implement one admission-time resolver that builds the capability from validated config and session mode.
- [ ] Attach the capability to every AgentSession creation path, including normal sessions, reused sessions, and manager child sessions.
- [ ] Inventory and update every AgentSession constructor and test fixture so no production path can omit capability accidentally.
- [ ] Make missing capability an explicit internal error for operation tools; do not silently fall back to unrestricted behavior.
- [ ] Run ./.sglang/bin/python -m pytest tests/test_agent_capabilities.py tests/test_agent_security_config_integration.py -q.

## Task 2: Tool wrapper enforcement

**Files:**
- Modify: backend/agent/agent_runner.py
- Modify: backend/tools/file_ops.py
- Modify: backend/tools/sys_ops.py
- Test: tests/test_tool_wrapper_security.py

**Interfaces:**
- Consumes: AgentSession.capability and the path/command enforcement interfaces.
- Produces: wrappers that perform capability checks immediately before invoking side-effecting implementations.

- [ ] Write failing tests calling wrapped write_file, edit_file, exec_command, browser_command, and image loading with a capability that denies the target.
- [ ] Write a failing test that calls the base function through an unrelated wrapper path and proves the actual side-effecting boundary still rejects the request.
- [ ] Write a failing test proving a model-supplied session_id argument is rejected and cannot override the wrapper-bound session capability.
- [ ] Implement explicit capability injection in _create_tool_wrapper and all side-effecting wrappers.
- [ ] Keep read-only manager tools unchanged except for capability attachment and diagnostic context.
- [ ] Ensure policy errors are returned as stable tool observations and do not trigger unrestricted fallback or a second unvalidated execution attempt.
- [ ] Run ./.sglang/bin/python -m pytest tests/test_tool_wrapper_security.py tests/test_manager_agent_dispatch.py -q.

## Task 3: Child Agent inheritance

**Files:**
- Modify: backend/agent/agent_runner.py
- Test: tests/test_agent_child_capabilities.py
- Test: tests/test_manager_agent_dispatch.py

**Interfaces:**
- Consumes: derive_child_capability and current child session creation.
- Produces: child sessions with inherited-or-narrower capability and unchanged dispatch arguments.

- [ ] Write failing tests dispatching a child whose task claims permission to write outside the parent capability; assert the child tool call is denied.
- [ ] Write a failing test proving repeated child reuse retains the original capability and cannot be expanded by a later task.
- [ ] Write a failing test proving duplicate child IDs and malformed IDs fail before any child session or capability is allocated.
- [ ] Implement child capability derivation at the same point as child session allocation, before constructing the child Agent.
- [ ] Keep dispatching_parallel_agent inputs exactly child_id and task; do not parse natural-language paths, command names, or may-write claims into authorization.
- [ ] Ensure manager task text remains available for model context but is never passed to capability derivation.
- [ ] Run ./.sglang/bin/python -m pytest tests/test_agent_child_capabilities.py tests/test_manager_agent_dispatch.py -q.

## Task 4: Profile and runtime configuration integration

**Files:**
- Modify: backend/config.py
- Modify: config/config-agent-operation.yaml
- Modify: backend/agent/agent_runner.py
- Test: tests/test_agent_security_config_integration.py
- Test: tests/test_agent_operation_config.py

**Interfaces:**
- Consumes: capability resolver and all resolved policy values.
- Produces: one runtime policy object shared by profile construction, wrappers, command supervisor, file tools, and child sessions.

- [ ] Write failing tests proving each configured timeout, output limit, command mode, image root, and capability default is visible in the built session policy.
- [ ] Write a failing test proving agent.exec_timeout is used by the actual command invocation rather than only stored in config.
- [ ] Write failing tests proving allow_overwrite_existing defaults to compatibility allow for authorized regular files, can be disabled explicitly, and does not change path or file-type validation.
- [ ] Write failing tests proving read_file.allowed_roots, search_whitelist_paths, file_operation_roots, and image_input_roots retain operation-specific semantics without implicit union.
- [ ] Write a failing test for invalid security configuration that asserts startup/admission failure with a precise field name.
- [ ] Implement config validation and one resolved runtime policy object; remove independent defaults in lower-level tools where they can disagree.
- [ ] Wire profile construction to pass the same capability object to normal, manager, coordinator, and child Agents.
- [ ] Wire the resolved operation-specific roots and compatibility overwrite default into the capability without changing the configured root set.
- [ ] Preserve manager read-only tool allowlist and verify basic child side-effect tools are wrapper-enforced rather than prompt-enforced.
- [ ] Run ./.sglang/bin/python -m pytest tests/test_agent_security_config_integration.py tests/test_agent_operation_config.py -q.

## Task 5: End-to-end security regression

**Files:**
- Modify: backend/agent/agent_runner.py only for integration defects.
- Test: tests/test_agent_security_end_to_end.py
- Test: tests/test_agent_child_capabilities.py
- Test: tests/test_tool_wrapper_security.py

**Interfaces:**
- Consumes: all previous tasks and the public Agent request/dispatch path.
- Produces: regression evidence that canonical tool calls cannot bypass capability enforcement.

- [ ] Write failing end-to-end tests from a canonical parsed tool call through SmartToolCallingAgent.process_tool_calls() to the real wrapper boundary.
- [ ] Cover normal basic Agent, manager dispatch, reused child, stop/abort, and session disposal while a side-effecting operation is pending.
- [ ] Cover existing safe legacy session reuse and rejection of unknown or malformed session IDs without creating or deleting any temporary path.
- [ ] Assert denied operations do not create files, launch processes, create outside image directories, or emit misleading success observations.
- [ ] Assert accepted operations preserve existing event routing and normal tool output.
- [ ] Run the focused end-to-end suite and then all security-focused tests from the three plans.
- [ ] Review every call site of _create_tool_wrapper, _build_profile_agent, AgentSession, and dispatching_parallel_agent with rg.

## Completion Gate

- [ ] Demonstrate that no tool wrapper can execute a side effect without a non-null capability.
- [ ] Demonstrate child capability monotonicity with property-style tests over representative field combinations.
- [ ] Demonstrate the same config object controls timeout, path, command, and output behavior.
- [ ] Confirm manager prompt/schema filtering remains unchanged and no permission is inferred from model text.
- [ ] Record residual dependency on OS-level sandboxing for any capability that still permits shell execution.
