# SearXNG Startup Policy Design

## Context

The active `ddg -> SearXNG` migration is already using a backend-first startup flow in `scripts/2_start_backend.sh`:

- backend must become ready before SearXNG readiness is considered
- users should be told they can open the frontend as soon as backend health succeeds
- an already running SearXNG instance should be reused instead of being torn down or replaced

The remaining product question is not whether SearXNG can be managed from this repository, but how startup should behave across three real-world operator modes:

1. a developer already has a working SearXNG instance from another project and wants to reuse it
2. a developer wants this repository to manage its own local SearXNG stack under `deploy/searxng/`
3. a developer has never deployed SearXNG and needs actionable setup guidance instead of a vague failure

## Goals

- Preserve backend-first startup and early frontend availability.
- Reuse any reachable SearXNG instance without requiring it to be owned by this repository.
- Keep repository-managed startup as a fallback capability.
- When managed startup fails, emit concrete tutorial-oriented guidance based on the maintained deployment tutorial.
- Never stop or mutate an external SearXNG instance that the current backend session did not start.

## Non-Goals

- Auto-generating or rewriting arbitrary third-party SearXNG deployments outside this repository.
- Requiring all users to adopt `deploy/searxng/` as their only deployment location.
- Blocking backend or frontend startup on SearXNG readiness.

## Desired Runtime Behavior

### 1. Backend First

`scripts/2_start_backend.sh` must continue to:

1. start backend first
2. wait for `GET /health`
3. print that the frontend can be started immediately
4. only then evaluate SearXNG state

If backend never becomes healthy, the script exits as a backend failure. SearXNG behavior is irrelevant in that case.

### 2. Reuse First

The script must probe the configured `SEARXNG_BASE_URL` after backend readiness.

- If the endpoint is reachable and returns a valid search response, the script prints a reuse message and does nothing else.
- Reuse does not require the instance to come from this repository's `deploy/searxng/` directory.
- Reuse mode must not stop containers, rewrite config files, or run `docker compose down` on shutdown.

This applies equally to:

- a SearXNG stack launched from another repository
- a manually managed local SearXNG instance
- a custom instance reachable through `SEARXNG_BASE_URL_OVERRIDE`

### 3. Managed Startup Fallback

If no reachable SearXNG instance exists, `scripts/2_start_backend.sh` must always attempt repository-managed startup.

Startup order preference:

1. `deploy/searxng/./dc`
2. `docker compose`
3. `docker-compose`

The managed attempt must still happen even if it is likely to fail because the system lacks one or more prerequisites. The point is to unify the operator experience into:

- attempt the local managed path first
- if it works, use it
- if it fails, show clear next-step guidance

### 4. Managed Failure Guidance

When managed startup fails, the script must print:

- the immediate reason it could not continue
- a reminder that backend and frontend remain usable except for web search
- a concise guided path aligned with the maintained tutorial under `docs/searXNG及Docker-Compose安装拉取教材.md`

The guidance must mention, at minimum:

- `deploy/searxng/./dc` as the preferred local compose binary path
- `chmod +x ./dc`
- Docker daemon proxy configuration when image pulls fail in mainland-network setups
- required files: `docker-compose.yml`, `.env`, `core-config/settings.yml`
- `./dc up -d`
- `./dc logs -f core`
- `./dc down && ./dc up -d` after config edits

The guidance should be compact enough for terminal output, but explicit enough that a first-time user can act immediately.

### 5. Session Ownership and Cleanup

Only a SearXNG stack started by the current backend script invocation is considered session-owned.

- If the script reused an existing instance, shutdown must leave it untouched.
- If the script launched the stack itself, shutdown should tear down only that stack.

This ownership rule is the core protection against interfering with other developers or other repositories.

## Configuration Semantics

- `SEARXNG_BASE_URL` remains the runtime probe target.
- Repository-managed startup continues to use `deploy/searxng/` as its local stack definition.
- The active curated engine policy in `deploy/searxng/core-config/settings.yml` and `.example` remains unchanged by this startup-policy adjustment.

## Error Handling

Managed startup failure messages should classify at least these cases:

- Docker binary missing
- Docker daemon unavailable
- no compose command available
- deployment files missing or unreadable
- local bind port occupied
- compose up failure
- post-start health check failure

All of them must end in the same operator-facing promise:

- backend is already up
- frontend can still be opened
- only search features are degraded
- follow the printed tutorial steps to finish SearXNG setup

## Tests

The implementation must keep or add automated coverage for:

1. reuse of an already running instance
2. managed startup selection when no instance is reachable
3. failure-message classification and tutorial guidance rendering
4. script behavior remaining backend-first
5. cleanup only for session-owned managed stacks

It must also preserve the existing smoke-tested flows:

- reachable instance present -> reuse
- no instance present -> managed startup through `./dc`

## Acceptance Criteria

- `scripts/2_start_backend.sh` is backend-first and non-blocking for frontend startup.
- Reachable SearXNG instances are always reused.
- Unreachable/missing instances always trigger a repository-managed startup attempt.
- Managed failure output includes actionable tutorial guidance.
- Session-owned cleanup never tears down external instances.
- Active docs and tests match this behavior.
