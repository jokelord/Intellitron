# Filesystem Operation Boundaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Make file writes, edits, image inputs, and session temporary paths obey one tested capability-aware filesystem policy.

**Architecture:** Add a shared PathPolicy and OperationCapability adapter, then route write_file, edit_file, load_local_images, and session image cleanup through it. Existing read/search validators remain the behavioral reference, while all mutating paths gain no-follow-symlink, regular-file, root-containment, size, and race-aware checks.

**Tech Stack:** Python 3.12, pathlib, os.open/openat-compatible primitives, dataclasses, pytest, existing uv-managed .sglang environment.

**Spec:** docs/superpowers/specs/2026-09-13-agent-operation-security-hardening-design.md

## Global Constraints

- Do not modify API authentication, listener binding, CORS, or the remote safety topic.
- Do not change the configured root set as part of this plan; enforce the resolved roots consistently.
- Do not modify third-party dependency source.
- Use the project environment: ./.sglang/bin/python -m pytest.
- Do not use prompt text, UI state, or task prose as filesystem authorization.
- Do not use git commands without separate user permission.

## File Map

- Create: backend/security/path_policy.py — canonical path and filesystem capability checks.
- Modify: backend/tools/file_ops.py — write/edit policy and atomic write integration.
- Modify: backend/skills/load_local_images/load_local_images.py — image input and session output policy.
- Modify: backend/agent/agent_runner.py — safe session image directory allocation and cleanup.
- Modify: backend/api/agent.py — session ID validation at request boundaries.
- Test: tests/test_path_policy.py — unit and race-resistant path policy coverage.
- Test: tests/test_file_ops_security.py — write/edit security behavior.
- Test: tests/test_load_local_images_security.py — image input and output directory behavior.
- Test: tests/test_agent_session_path_security.py — session ID and cleanup behavior.

## Interfaces

The plan produces these interfaces for later plans:

    class PathPolicyError(ValueError):
        code: str

    @dataclass(frozen=True)
    class ResolvedPath:
        requested: str
        resolved: str
        root: str
        operation: str

    class PathPolicy:
        def resolve_existing(self, path: str, operation: str) -> ResolvedPath: ...
        def validate_create(self, path: str, operation: str) -> ResolvedPath: ...
        def validate_session_dir(self, session_id: str) -> str: ...
        def validate_image_input(self, path: str) -> ResolvedPath: ...

The exact constructor must accept already-resolved configuration and must not read YAML on every tool call.

## Task 1: Shared path policy

**Files:**
- Create: backend/security/path_policy.py
- Test: tests/test_path_policy.py

**Interfaces:**
- Consumes: resolved allowed roots and image roots from the runtime policy adapter; the capability integration plan supplies the AgentSession binding.
- Produces: ResolvedPath and PathPolicyError contracts used by file, image, and session callers.

- [ ] Write failing tests for absolute paths inside and outside a configured root, .. traversal, nonexistent parent directories, existing directories, devices/FIFO/socket fixtures, and malformed paths.
- [ ] Write failing tests for final-component symlink escape, parent-component symlink escape, allowed in-root symlink behavior, and a replacement between validation and open using a deterministic test double.
- [ ] Write failing tests for session IDs containing slash, backslash, dot-dot, whitespace, shell metacharacters, empty strings, and overlong values; assert validate_session_dir always returns a child of the fixed base directory.
- [ ] Implement canonical root containment with resolved path comparisons and explicit operation modes for existing read/modify, create, image input, and session cleanup.
- [ ] Implement regular-file and no-follow-symlink checks; use descriptor-relative or equivalent no-follow operations for the final open path where the platform supports them.
- [ ] Implement fixed-base session directory allocation without interpolating an unvalidated session ID into a recursive cleanup target.
- [ ] Run ./.sglang/bin/python -m pytest tests/test_path_policy.py -q and confirm all new negative tests pass.

## Task 2: Secure write_file and edit_file

**Files:**
- Modify: backend/tools/file_ops.py
- Test: tests/test_file_ops_security.py
- Test: tests/test_file_ops.py

**Interfaces:**
- Consumes: PathPolicy from Task 1 and the current file tool function signatures.
- Produces: policy-denied results with stable error codes, explicit overwrite behavior, bounded content handling, and atomic writes.

- [ ] Write failing tests proving write_file and edit_file reject paths outside capability roots, parent symlinks, final symlinks, directories, and oversized content.
- [ ] Write a failing test proving a write to an existing file is denied unless the explicit overwrite policy is enabled; preserve a separate test for approved overwrite.
- [ ] Write a failing test proving edit_file with an empty search string still requires modify capability and cannot append outside the allowed root.
- [ ] Write a failing test that simulates a target swap after validation and verifies no external target is modified.
- [ ] Implement policy checks before directory creation and before every read/write open; do not call os.makedirs on an unchecked parent.
- [ ] Implement bounded content validation before opening the destination.
- [ ] Write a failing test proving the compatibility default allows overwrite of an authorized existing regular file, while allow_overwrite_existing=false rejects that same operation.
- [ ] Write a failing test proving transient PermissionError during temp-file creation or atomic replace retries with bounded exponential backoff, cleans each failed temp file, and never retries policy denial.
- [ ] Implement temp-file write plus flush/fsync and atomic replace under the validated parent, preserving the existing output strings on success and returning stable policy errors on rejection.
- [ ] Preserve the existing PermissionError retry count and backoff semantics around the complete atomic-write attempt; never retry after path, file-type, size, overwrite, or race policy failure.
- [ ] Keep edit matching behavior unchanged; change only authorization, file-type, size, and write lifecycle behavior.
- [ ] Run ./.sglang/bin/python -m pytest tests/test_file_ops_security.py tests/test_file_ops.py -q.

## Task 3: Secure image loading and session temporary files

**Files:**
- Modify: backend/skills/load_local_images/load_local_images.py
- Modify: backend/agent/agent_runner.py
- Modify: backend/api/agent.py
- Test: tests/test_load_local_images_security.py
- Test: tests/test_agent_session_path_security.py

**Interfaces:**
- Consumes: PathPolicy.validate_image_input and PathPolicy.validate_session_dir.
- Produces: image loader and AgentSession cleanup paths that never accept raw path fragments as authority.

- [ ] Write failing tests showing image input outside the image capability is rejected even when the file extension is supported.
- [ ] Write failing tests for image input symlink escape, image output session traversal, cleanup of a valid session directory, and cleanup of an invalid session ID.
- [ ] Write a failing API/request test that rejects malformed session IDs before a session is created or disposed.
- [ ] Write compatibility tests for backend-generated canonical UUIDs, reuse of an existing legacy safe token, rejection of an unknown legacy token for new-session creation, and rejection of invalid IDs.
- [ ] Replace direct /tmp/agent_images/{session_id} construction with a policy-owned directory allocator and safe cleanup handle.
- [ ] Pass the resolved image capability into the loader without using shell-expanded user input as an authorization mechanism.
- [ ] Keep image count, extension, dimension, and byte limits unchanged unless an existing test proves they must move; this task adds path authorization rather than a media redesign.
- [ ] Ensure cleanup failures are explicit and cannot fall back to recursively deleting a guessed path.
- [ ] Run ./.sglang/bin/python -m pytest tests/test_load_local_images_security.py tests/test_agent_session_path_security.py -q.

## Task 4: Integrated filesystem regression

**Files:**
- Modify: backend/tools/read_file/file_validator.py only if the shared policy adapter needs a behavior-preserving bridge.
- Modify: backend/tools/search_ops.py only if its existing validation must call the shared policy without changing search semantics.
- Test: tests/test_read_file_validation.py
- Test: tests/test_search_ops.py
- Test: tests/test_path_policy_integration.py

**Interfaces:**
- Consumes: the shared PathPolicy and the existing read/search behavior.
- Produces: one cross-tool test matrix proving read, search, write, edit, image load, and cleanup do not disagree about path containment.

- [ ] Write failing integration tests using one configured root and one outside path; assert every relevant tool returns the same containment decision.
- [ ] Add a symlink matrix covering read, search listing, image input, edit, and write; record intentionally different behavior explicitly where content discovery differs from mutation.
- [ ] Run the existing focused suites before changing adapters to identify any compatibility assumptions.
- [ ] Implement only the adapter changes needed to remove duplicate path authority; preserve current read/search limits and result formats.
- [ ] Document and test root resolution semantics: read_file.allowed_roots remains the read source and fallback file-operation source; search_whitelist_paths remains search-only; explicit file/image roots replace only their own operation roots and are never unioned implicitly.
- [ ] Run ./.sglang/bin/python -m pytest tests/test_read_file_validation.py tests/test_search_ops.py tests/test_path_policy_integration.py -q.
- [ ] Run the complete filesystem-focused set and record exact output in the implementation review; do not claim full repository coverage from focused tests.

## Completion Gate

- [ ] Review every call site of write_file, edit_file, load_local_images, session image cleanup, and recursive temporary-directory deletion with rg.
- [ ] Confirm no raw session ID is used in a filesystem path before validation.
- [ ] Confirm no write path performs directory creation or open before policy validation.
- [ ] Confirm the compatibility overwrite default is explicit in resolved policy and can be disabled without changing path validation.
- [ ] Confirm transient PermissionError retry cannot repeat a policy denial or leave an untracked temp file.
- [ ] Confirm all new negative tests are deterministic and do not write outside the workspace or test temporary directory.
- [ ] Report any platform-specific no-follow limitation instead of silently treating canonicalization as race-proof.
