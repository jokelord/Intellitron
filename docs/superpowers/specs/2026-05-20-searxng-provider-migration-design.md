# SearXNG Provider Migration Design

## Summary

Replace the repository's current direct web-search provider integration with a single self-hosted SearXNG provider.
The repository will no longer expose old provider names in current configs, current docs, runtime defaults, or active tests.
SearXNG will not be started as a user-facing standalone script. Instead, `scripts/2_start_backend.sh` will start the backend first, wait until the backend is fully ready, then start the SearXNG Docker Compose stack. When the user presses `Ctrl+C` in that same backend terminal, the script must shut down both the backend and the SearXNG stack cleanly.

## Goals

- Replace the repository's active search-provider path with self-hosted SearXNG only.
- Remove old provider names from current config files, active code defaults, README, and active tests.
- Introduce a SearXNG-specific config model under `web_tools.searxng`.
- Keep `search_and_fetch` behavior intact at the product level: search first, then concurrently fetch result pages.
- Make SearXNG lifecycle backend-managed and delayed until backend readiness is confirmed.
- Provide a beginner-friendly deployment tutorial and one-command backend-driven startup flow.
- Preserve robust error handling, explicit diagnostics, and safe degradation when SearXNG is unavailable.

## Non-Goals

- Do not modify `踩坑记录.md`.
- Do not rewrite or sanitize historical design/archive documents.
- Do not keep compatibility fallbacks to older external search providers.
- Do not make SearXNG a hard prerequisite for non-search backend functionality.
- Do not redesign frontend UX around search.

## Current State

### Active search integration

- `backend/tools/web_tools.py` currently contains direct provider-specific search code and parsing logic.
- `backend/config.py` currently models generic search engine / protocol / method fields.
- `config/config-misc.yaml` currently expresses search provider selection using old provider-specific fields.
- `backend/tools/search_and_fetch.py` depends on `web_search()` returning a markdown list of search results.

### Backend startup behavior

- `scripts/2_start_backend.sh` currently starts uvicorn directly in the foreground.
- The script already traps `SIGINT`, `SIGTERM`, and `SIGHUP` and terminates backend child processes.
- The repository already exposes a backend health endpoint at `GET /health` in `backend/main.py`.

### Existing fetch stack

- `web_fetch()` still provides value after the provider migration.
- `trafilatura` and `readability-lxml` are still actively used for result-page text extraction.
- `chardet` is not directly imported by repository code today and should be re-evaluated during implementation rather than silently preserved forever.

## Proposed Architecture

### Provider model

The repository will support a single active search provider: `searxng`.

The search flow becomes:

1. `web_search()` calls the configured self-hosted SearXNG instance.
2. SearXNG returns JSON results from `/search` with `format=json`.
3. The backend normalizes the JSON payload into the repository's internal search-result representation.
4. `search_and_fetch` reuses the normalized results and continues its concurrent result-page fetching behavior.

This keeps the upper layers stable while replacing the underlying search provider contract.

### Startup orchestration model

`scripts/2_start_backend.sh` becomes the orchestrator for both backend and SearXNG lifecycle.

Required behavior:

1. Start the backend process first.
2. Poll the backend readiness endpoint until the backend is confirmed ready.
3. Only after backend readiness succeeds, start the SearXNG Docker Compose stack.
4. Poll SearXNG readiness separately.
5. If the user interrupts the backend terminal with `Ctrl+C`, shut down both:
   - backend process tree
   - SearXNG Compose stack started by this session

### Readiness definition

Backend readiness must be explicit and deterministic.

- Primary readiness check: `GET http://<backend-host>:<backend-port>/health`
- The script must not treat "uvicorn process exists" as sufficient readiness.

SearXNG readiness must also be explicit.

- Preferred readiness check: successful HTTP response from the configured SearXNG base URL and search path using a lightweight probe.
- The probe should confirm that the service is actually reachable on the configured port, not just that containers exist.

## Configuration Design

### New active config model

The active config shape under `web_tools` becomes:

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

### Config rules

- `provider` must default to `searxng`.
- `base_url` must default to `http://127.0.0.1:8080`.
- The backend port is `8090`; SearXNG must use `8080` by default.
- `format` must be fixed to `json` in the repository-controlled flow.
- Old generic provider-selection fields must be removed from active config parsing:
  - `search_engine`
  - `protocol`
  - `method`
  - `fallback_engine`
  - `fallback_protocol`
  - `fallback_method`

### Fallback policy

There is no fallback to older providers.

Accepted fallback behavior is limited to degradation within the SearXNG path itself:

- If SearXNG is unreachable, backend continues running.
- Search endpoints/tools return explicit failure states/messages tied to SearXNG unavailability.
- Non-search backend features remain available.

This is a deliberate product choice to keep the migration semantically clean.

## SearXNG Deployment Design

### Deployment mode

Use the official containerized deployment pattern based on Docker Compose.

Repository assets to add:

- `deploy/searxng/docker-compose.yml`
- `deploy/searxng/.env.example`
- `deploy/searxng/core-config/settings.yml.example`
- project-specific guide document for first-time users

### Default ports

- Frontend dev: `3000`
- Backend API: `8090`
- Inference engine: `30000`
- SearXNG: `8080`

`8080` is the default SearXNG port for this repository because:

- it matches the official default port many SearXNG users already deploy
- it is easy to remember for local development
- it avoids forcing existing self-hosted SearXNG users to remap ports just for this repository

### SearXNG instance requirements

The repository-managed SearXNG instance must:

- expose `/search`
- enable `json` in `search.formats`
- persist config and cache volumes according to the official container pattern
- be suitable for localhost or trusted-network use by default

### Beginner-friendly operations model

Users should not need to directly understand Docker Compose before getting value.

The main operational workflow is:

1. Ensure Docker / Docker Compose is installed.
2. Run `scripts/2_start_backend.sh`.
3. The script starts backend first.
4. After backend readiness passes, the script auto-starts SearXNG.
5. The user uses the application as usual.
6. The user presses `Ctrl+C` in the backend terminal to stop both services cleanly.

The terminal sequencing must be explicit to the user:

1. backend becomes ready
2. the terminal tells the user they can now go start the frontend
3. the terminal tells the user SearXNG is still being prepared
4. if SearXNG is delayed or unavailable, the terminal tells the user that only network search is affected and all non-search functionality remains available

The dedicated tutorial must explain:

- what extra service is being started and why
- why SearXNG uses port `8080`
- first-run prerequisites
- how startup works now
- how to confirm SearXNG is healthy
- how to read logs if startup fails
- how to recover from image pull failures or port collisions

Required terminal UX after backend readiness:

- print that the backend is ready
- print a generic message that the user can now go start the frontend
- do not hardcode a frontend script filename in that message
- print that SearXNG preparation is continuing and may take time on first run
- print that if SearXNG is not yet ready, only search is affected and non-search functionality remains available

Required terminal UX during first-time SearXNG image acquisition:

- show visible download / pull / startup progress in the backend terminal
- do not leave the terminal silently waiting during long image download

## Backend Orchestration Details

### Process ownership

`scripts/2_start_backend.sh` owns the lifecycle of the SearXNG stack it starts.

Implementation should track whether the current backend session actually started the SearXNG stack.
Shutdown logic must only attempt to stop the stack when it is owned by this backend session.

This avoids accidental shutdown of user-managed unrelated containers.

### Startup order

Required order:

1. environment setup
2. backend port cleanup / availability checks
3. backend process launch
4. backend readiness polling via `/health`
5. SearXNG config preparation
6. `docker compose up -d`
7. SearXNG readiness polling
8. user-facing success or degraded-status messaging

### Shutdown order

Required order on `Ctrl+C` or termination:

1. emit shutdown message
2. terminate backend child processes gracefully
3. wait briefly for backend exit
4. bring down the owned SearXNG Compose stack gracefully
5. exit cleanly

### Failure policy

Backend startup and SearXNG startup have different severity classes.

#### Fatal startup errors

The script must exit with a clear error if any of these occur before backend launch:

- backend configuration is unreadable
- backend port cannot be freed or bound
- required Python environment is missing
- backend process cannot be launched

#### Degraded runtime startup errors

The script must keep backend alive but surface explicit errors if any of these occur after backend readiness:

- Docker is unavailable
- Docker Compose command is unavailable
- SearXNG compose files are missing or invalid
- image pull fails
- SearXNG port `8080` is occupied and cannot be resolved automatically
- SearXNG readiness probe never succeeds

In these cases:

- backend remains available
- search functionality is marked unavailable
- terminal output explicitly says the user can still go start the frontend
- terminal output explicitly says that only network search is affected
- terminal output explicitly says that non-search functionality remains available
- logs and terminal output must clearly explain the failure class and next action

## Search API Integration Design

### Request contract

Repository calls to SearXNG must target the configured base URL and search path with `format=json`.

Supported parameters to propagate from repository config/runtime:

- `q`
- `format=json`
- `language`
- `safesearch`
- `categories` when configured
- `engines` when configured

Optional support may include auth header/token if later required by private instances.

### Normalized result model

SearXNG JSON results must be normalized into the existing internal result structure used by `search_and_fetch`:

- `title`
- `url`
- `description`

Missing or malformed fields must be tolerated on a per-result basis wherever practical.
Bad individual result records should be skipped rather than crashing the whole search response.

### Error mapping

SearXNG failures must map to explicit repository-level failure messages.

Examples:

- unreachable instance
- timeout
- invalid JSON payload
- non-200 HTTP response
- empty usable result set

Errors should distinguish between:

- infrastructure unavailable
- remote search returned no usable results
- repository parsing failure

## Robustness Requirements

### Error handling

The implementation must explicitly consider and classify at least these cases:

- backend readiness timeout
- SearXNG readiness timeout
- Docker executable missing
- Docker Compose plugin missing
- Compose file path missing
- `.env` or `settings.yml` generation failure
- SearXNG port collision
- container startup succeeds but HTTP readiness fails
- search call timeout
- invalid JSON response
- empty search results
- partial result records missing fields
- user interrupt during delayed SearXNG startup

Every class must produce a user-visible, action-oriented message.

### Fallback / degradation stance

No provider fallback is allowed.

Allowed safeguards:

- clear errors
- degraded mode
- safe no-op shutdown when SearXNG was never started
- explicit readiness timeouts
- defensive parsing

Disallowed safeguard:

- silently falling back to any previous provider path

## Documentation Requirements

### README

README should contain only a concise current-state note that the project now relies on an additional self-hosted SearXNG service which is automatically started by the backend startup flow.

README must not reintroduce old provider names.

### Tutorial document

Add a dedicated user tutorial covering:

- prerequisites
- first startup
- how backend now auto-starts SearXNG after backend readiness
- how to verify SearXNG on `8090`
- how `Ctrl+C` stops both backend and SearXNG
- common failures and fixes

## Files In Scope

### To change

- `backend/config.py`
- `backend/tools/web_tools.py`
- `backend/tools/search_and_fetch.py` if needed for minimal adaptation
- `config/config-misc.yaml`
- active config templates that currently expose old provider fields
- `scripts/2_start_backend.sh`
- `README.md`
- new `deploy/searxng/` deployment assets
- tests covering config parsing, provider integration, and startup orchestration

### Explicitly out of scope

- `踩坑记录.md`
- historical design / archive documents
- major responsibilities of `scripts/1_start_sglang.sh`
- major responsibilities of `scripts/3_start_frontend.sh`

## Testing Requirements

### Config parsing tests

- parse new `web_tools.provider`
- parse `web_tools.searxng.*`
- verify new defaults
- verify old removed fields are no longer required for the active path

### Search integration tests

- SearXNG JSON success path
- empty results
- invalid JSON
- timeout
- non-200 response
- malformed partial results tolerated without full failure where possible

### Backend startup orchestration tests

- backend readiness must pass before SearXNG startup is attempted
- no SearXNG startup before backend readiness
- SearXNG startup failure leaves backend alive
- shutdown path attempts SearXNG teardown only when owned by current session

### Documentation/config hygiene checks

- current config files do not contain old provider names
- README does not contain old provider names

## Acceptance Criteria

- Repository defaults to SearXNG as the sole active search provider.
- Current configs, active tests, and README no longer surface old provider names.
- `scripts/2_start_backend.sh` starts backend first, waits for `/health`, then starts SearXNG.
- Default SearXNG base URL is `http://127.0.0.1:8080`.
- `Ctrl+C` in the backend terminal shuts down both backend and the SearXNG stack started by that session.
- If SearXNG startup fails, backend stays available and search degrades cleanly with explicit diagnostics.
- After backend readiness succeeds, the terminal immediately tells the user they can go start the frontend, without hardcoding a specific frontend script filename.
- During first-time SearXNG image download, the terminal shows visible progress instead of silently waiting.
- When SearXNG is unavailable, the terminal explicitly says that only network search is affected and that non-search functionality remains available.
- Tutorial exists for beginners who are unfamiliar with Docker Compose.
- The page-fetch extraction path remains functional, including `trafilatura` and `readability-lxml` integration.

## Implementation Notes For The Follow-Up Plan

- All code implementation work should be performed in `./.worktrees/searXNG/` as requested by the user.
- The plan should explicitly call out whether `chardet` remains necessary after the migration or can be removed.
- The plan should include verification of Docker image source references used by the deployment assets.
- After the spec is accepted, provide the user with the official container image/source links used by the chosen deployment path.
