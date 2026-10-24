# Tool Feedback, Global Fetch Budget, and Memory Retention Design

## Goal

Correct malformed local-LLM multi-tool output by feeding explicit parser feedback into the next observation, keep read-only/search tools parallel, and reduce accidental backend memory retention without breaking SGLang cache-first compact behavior.

## Constraints

- Do not make read-only/search tools serial.
- Treat `web_tools.fetch_max_workers=150` as a process-wide budget shared across sessions.
- Do not preserve malformed multi-tool transcripts by repairing them into partial tool calls.
- Do not break compact cache-first: keep `session.last_compactable_llm_messages` semantics unchanged.
- Do not remove historical image visibility from LLM memory in this change.
- Do not use git commands unless explicitly permitted.

## Design

### Malformed Multi-Tool Feedback

Add a narrow malformed-transcript detector in `LocalLLMModel` for `Calling tools:` arrays. The detector flags a top-level tool-call object that repeats `id`, `type`, or `function`, which is the known shape where two intended calls were merged into one object. `SmartToolCallingAgent._step_stream()` checks generated content before normal parsing and raises `AgentParsingError` with a corrective message. smolagents already turns `AgentParsingError` into an `ActionStep.error`; `write_memory_to_messages()` then feeds it back as a tool-response observation on the next step.

Valid multi-call arrays remain accepted. Existing narrow single-call repairs can remain for non-duplicate cases, but duplicate-key multi-call loss must not be repaired or silently collapsed.

### Global Fetch Budget

Keep orchestrator read-only stages parallel. Change `search_and_fetch` so each invocation draws fetch workers from a process-wide shared budget. `fetch_max_workers=150` becomes total concurrent page-fetch slots across all sessions and all parallel tool calls, not per invocation. Each `search_and_fetch` can submit many futures, but every `_fetch_single` execution must acquire one global slot before doing transport work.

This protects multi-session parallelism without changing the model-visible behavior that searches are parallel. It does not impose a single global serial lock.

### Configuration

Update the active infer config from `mem_fraction_static: 0.83` to `0.78` in `config/qwen3__5-config-infer.yaml`, because `config/config-overall.yaml` points to that infer file. Update `config/config-misc.yaml` to `fetch_max_workers: 150`.

### Memory Retention

Do not change LLM prompt construction. Do not clear historical images in `memory.steps` in this change.

Safe first fixes:

- `compaction_archive`: stop storing full archived `memory.steps`; store lightweight diagnostics such as summary id, timestamp, archived step count, and summary text. This does not affect cache-first because cache-first uses `session.last_compactable_llm_messages`, not `compaction_archive`.
- `last_llm_prompt_snapshot`: keep assignment behavior but make snapshots lightweight by capping stored canonical text and redacting base64 image data. This does not affect cache-first because prompt snapshots are diagnostics, not LLM input or compact source.
- Session TTL cleanup: add cleanup-on-create or periodic cleanup that skips active sessions and preserves manager child context while its parent session is alive. This does not alter prompt shape or cache-first payload for active sessions.

## Tests

- Parser test: malformed duplicate-key `Calling tools:` transcript is rejected and includes corrective feedback.
- Parser test: valid two-element `Calling tools:` transcript still returns two calls.
- Agent test: malformed transcript raises `AgentParsingError` before executing any tool.
- Fetch test: parallel `search_and_fetch` invocations never exceed the global `fetch_max_workers` budget.
- Compact test: memory replacement no longer archives full `memory.steps`, while `last_compactable_llm_messages` remains untouched until successful compact clearing.
