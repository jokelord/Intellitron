# vLLM Multimodal Request Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make SGLang and vLLM submit the same configured sampling controls and cache-preserving multimodal message shape, while ensuring cache-first compaction never rewrites its captured source.

**Architecture:** Introduce one pure message-preparation function in `backend/llm_service.py`. It deep-copies `Message` content and deterministically normalizes only oversized data-URL images; it never removes, replaces, summarizes, or reorders historical image parts. Normal chat and agent paths prepare messages before compact-source capture, while `preserve_prompt_shape=True` bypasses preparation for cache-first compact calls. Both providers receive sampling fields from the common inference configuration but retain their API-specific output-limit spelling.

**Tech Stack:** Python 3, FastAPI, Pydantic configuration, Pillow, httpx, pytest.

## Global Constraints

- Preserve every historical `image_url` content part exactly as an image part. Never emit `[图片×N]` or any other replacement marker.
- Cache-first compact source must not be trimmed, normalized, reordered, or otherwise rewritten; only the final compact-control user message may be appended.
- Do not put frontend bubbles, timeline events, routing deltas, or UI state into LLM prompt bodies.
- Preserve the existing `max_new_tokens -> max_tokens` mapping for vLLM and `max_new_tokens` field for SGLang.
- Keep the existing `extra_body` merge order: it is applied after default sampling fields and may intentionally override them.
- Keep image normalization best-effort: malformed, remote, or undecodable URLs remain unchanged; Pillow import/decoding failures log diagnostics and do not fail the LLM request.
- Do not run Git commands or commit unless the user explicitly grants Git permission.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/llm_service.py` | Define the pure multimodal preparation function; route the explicit `preserve_prompt_shape` policy through `LLMService` and provider calls; make SGLang and vLLM emit identical sampling controls. Remove the SGLang-only nested historical-image stripping/normalization functions. |
| `backend/api/chat.py` | Prepare normal chat messages before auto-compact derives its cache-first source, then mark the final prepared request as prompt-shape preserving when sending it to `LLMService`. |
| `backend/agent/local_llm_model.py` | Prepare agent messages after runtime-context injection but before diagnostics and `last_compactable_llm_messages` capture; pass the prepared message list as prompt-shape preserving to `LLMService`. |
| `backend/compact/orchestrator.py` | Pass `preserve_prompt_shape=True` only for the cache-first plan. Reliable fallback uses normal one-time image preparation because it has no prefix-cache equivalence guarantee. |
| `docs/superpowers/specs/2026-06-02-prefix-cache-stability-v2-design.md` | Replace stale historical-image placeholder rules with the current cache-preserving image-history and single-normalization constraints. |
| `tests/test_llm_backend_selection.py` | Extend provider request-body assertions for sampling controls, output-token mapping, and policy forwarding. |
| `tests/test_multimodal_request_preparation.py` | New focused unit tests for copy safety, historical-image preservation, deterministic resize behavior, malformed-image passthrough, and cache-preserving bypass. |
| `tests/test_compact_orchestrator_cache_first.py` | Verify compact orchestration forwards the preservation policy and does not alter an image-bearing cache-first source. |

## Task 1: Lock Down Shared Preparation Semantics

**Files:**
- Create: `tests/test_multimodal_request_preparation.py`
- Modify: `backend/llm_service.py: module-level helpers before SGLangProvider`

**Interfaces:**
- Consumes: `Message(role: str, content: str | list[dict])`, `max_image_dimension: int | None`, and an optional `DebugLogger`.
- Produces: `prepare_multimodal_messages(messages: list[Message], *, max_image_dimension: int | None, debug: DebugLogger | None = None) -> list[Message]`.
- Contract: the returned list and every mutable content part are copied; it preserves all roles, part order, text, historical `image_url` parts, and remote URLs. It resizes only decodable, oversized `data:` image URLs.

- [ ] **Step 1: Write the failing tests for message preservation and copy safety**

Create `tests/test_multimodal_request_preparation.py` with a `make_data_url(width, height)` helper that creates an RGB JPEG in memory using Pillow. Add this test shape:

```python
def test_prepare_multimodal_messages_preserves_historical_images_and_input():
    old_image = make_data_url(4, 4)
    current_image = make_data_url(4, 4)
    original = [
        Message(role="user", content=[
            {"type": "image_url", "image_url": {"url": old_image}},
            {"type": "text", "text": "first image"},
        ]),
        Message(role="assistant", content="I see it."),
        Message(role="user", content=[
            {"type": "image_url", "image_url": {"url": current_image}},
            {"type": "text", "text": "second image"},
        ]),
    ]

    prepared = prepare_multimodal_messages(original, max_image_dimension=1280)

    assert prepared is not original
    assert prepared[0].content[0]["type"] == "image_url"
    assert prepared[0].content[0]["image_url"]["url"] == old_image
    assert prepared[2].content[0]["image_url"]["url"] == current_image
    assert original[0].content[0]["image_url"]["url"] == old_image
```

Add a second failing test with a `20x10` source and `max_image_dimension=8`; decode the returned data URL and assert `image.size == (8, 4)`. Assert that a `https://example.test/image.jpg` part and an invalid `data:image/jpeg;base64,not-base64` part are left unchanged.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `./.sglang/bin/python -m pytest tests/test_multimodal_request_preparation.py -v`

Expected: collection fails because `prepare_multimodal_messages` is not importable from `backend.llm_service`.

- [ ] **Step 3: Implement the pure normal-image preparation helper**

At module scope in `backend/llm_service.py`, add `prepare_multimodal_messages`. Use `copy.deepcopy` for each message content before processing so callers' `Message` objects and nested `image_url` dictionaries are never mutated. For each copied `image_url` item:

```python
if not isinstance(url, str) or not url.startswith("data:"):
    continue
if max_image_dimension is None or max_image_dimension <= 0:
    continue
```

Decode only a valid data URL, inspect dimensions, and leave an image unchanged unless either dimension exceeds the configured limit. Resize proportionally with `PIL.Image.Resampling.LANCZOS`, convert alpha/palette/non-RGB images to RGB, and serialize as deterministic JPEG with `quality=85, optimize=False`. On any per-image exception, leave the copied URL unchanged and emit a diagnostic through `debug.inference(...)` when `debug` is supplied.

Do not add an image-history policy, an agent-task exemption, or a placeholder marker: preservation of every historical image is unconditional.

- [ ] **Step 4: Run the focused tests and verify success**

Run: `./.sglang/bin/python -m pytest tests/test_multimodal_request_preparation.py -v`

Expected: PASS. The test proves that old image parts remain `image_url` values, resize is bounded, invalid data is non-fatal, and the source input remains unchanged.

- [ ] **Step 5: Do not commit without explicit user approval**

Do not run `git add`, `git commit`, or any other Git command. Report the modified files and test output instead.

## Task 2: Make Provider Request Bodies Sampling-Complete And Remove SGLang-Only Image Mutation

**Files:**
- Modify: `backend/llm_service.py: SGLangProvider.generate and VLLMProvider.generate`
- Modify: `tests/test_llm_backend_selection.py: provider request-body tests`

**Interfaces:**
- Consumes: `config.inference.temperature`, `top_p`, `top_k`, `repetition_penalty`, and `max_tokens`/`max_new_tokens`.
- Produces: SGLang request body with `max_new_tokens`; vLLM request body with `max_tokens`; both contain `temperature`, `top_p`, `top_k`, and `repetition_penalty` whenever configured.
- Invariant: neither provider performs image stripping, placeholder insertion, image resizing, or any mutation of its `messages` argument.

- [ ] **Step 1: Extend request-body tests before implementation**

In `tests/test_llm_backend_selection.py`, extend `test_sglang_provider_preserves_zero_temperature_and_merges_extra_body` and `test_vllm_provider_preserves_zero_temperature_and_merges_extra_body` with explicit configuration values:

```python
config.inference.top_k = 37
config.inference.repetition_penalty = 1.0
```

Then assert:

```python
assert request_data["temperature"] == 0.0
assert request_data["top_p"] == config.inference.top_p
assert request_data["top_k"] == 37
assert request_data["repetition_penalty"] == 1.0
```

Keep the existing backend-specific assertions:

```python
assert sglang_request["max_new_tokens"] == 150
assert vllm_request["max_tokens"] == 150
```

Add a regression test that passes an oversized `data:` image as the latest user image to each provider, sets `config.model.max_image_dimension = 8`, patches `_sync_generate`, and verifies the original nested `image_url` string remains unchanged after `generate()` returns. The current SGLang branch rewrites this nested URL during its final normalization; the test must therefore fail before the provider-local transformation is removed.

- [ ] **Step 2: Run the provider request-body tests and verify failure**

Run: `./.sglang/bin/python -m pytest tests/test_llm_backend_selection.py -k 'provider_preserves_zero_temperature or provider_does_not_mutate_image_messages' -v`

Expected: vLLM lacks `top_k` and `repetition_penalty`; SGLang lacks `top_k`; the oversized-image mutation regression fails until SGLang's nested image normalization is removed.

- [ ] **Step 3: Build equivalent sampling request fields in both providers**

Construct both provider request bodies with the same sampling block before `extra_body` is merged:

```python
"temperature": config.inference.temperature if temperature is None else temperature,
"top_p": config.inference.top_p if top_p is None else top_p,
"top_k": config.inference.top_k,
"repetition_penalty": config.inference.repetition_penalty,
```

Only include `top_k` or `repetition_penalty` when its configuration value is not `None`; include neutral values such as `0` and `1.0` rather than filtering them by truthiness. Retain `max_new_tokens` only for SGLang and `max_tokens` only for vLLM.

Delete the nested `_strip_old_images` function, the nested `_normalize_and_log_images` function, their calls, and their SGLang-only message dump block. Both providers should build their `messages_data` directly from the immutable `Message` list they receive.

- [ ] **Step 4: Run provider tests and verify success**

Run: `./.sglang/bin/python -m pytest tests/test_llm_backend_selection.py -k 'provider_preserves_zero_temperature or provider_does_not_mutate_image_messages' -v`

Expected: PASS. SGLang and vLLM request data include the same sampling controls, use their own correct max-token field, and do not mutate callers' image messages.

- [ ] **Step 5: Do not commit without explicit user approval**

Do not run Git commands. Keep this task limited to provider request construction and removal of the SGLang-only transformation.

## Task 3: Prepare Normal Sources Before Cache Capture And Preserve Compact Shapes

**Files:**
- Modify: `backend/llm_service.py: LLMProviderInterface.generate, LLMService.chat, LLMService.chat_async, LLMService.chat_stream_async, and all provider implementations`
- Modify: `backend/api/chat.py: _generate_stream_response`
- Modify: `backend/agent/local_llm_model.py: LocalLLMModel.generate and LocalLLMModel.generate_stream`
- Modify: `backend/compact/orchestrator.py: CompactOrchestrator._chat_stream_async`
- Modify: `tests/test_llm_backend_selection.py`
- Modify: `tests/test_compact_orchestrator_cache_first.py`

**Interfaces:**
- Adds `preserve_prompt_shape: bool = False` to every `generate`, `chat`, `chat_async`, and `chat_stream_async` boundary that forwards a request to a provider.
- `LLMService.chat(..., preserve_prompt_shape=False)` calls `prepare_multimodal_messages` with `config.model.max_image_dimension`; when `True`, it forwards the supplied `Message` objects unchanged.
- `CompactOrchestrator._chat_stream_async(..., preserve_prompt_shape=...)` forwards `True` only when `plan.shape == "cache_first_tail"`; reliable fallback forwards `False`.

- [ ] **Step 1: Write failing policy-propagation and cache-first tests**

Extend `SequencedLLM.chat_stream_async` in `tests/test_compact_orchestrator_cache_first.py` to accept `preserve_prompt_shape=False` and append it into each recorded call. Add an image-bearing cache-first source:

```python
image = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}
cache_source = [
    Message(role="system", content="ACTIVE"),
    Message(role="user", content=[image, {"type": "text", "text": "history"}]),
]
```

Assert after `run_compact()` that:

```python
assert llm.calls[0]["preserve_prompt_shape"] is True
assert llm.calls[0]["messages"][:-1] == cache_source
```

In `tests/test_llm_backend_selection.py`, create a loaded `LLMService` with a fake provider that records `messages` and `preserve_prompt_shape`. Add a test that calls `service.chat(..., preserve_prompt_shape=False)` with an oversized image and verifies the provider receives a resized copy while the original remains unchanged. Add a second call with `preserve_prompt_shape=True` and assert the provider receives the original data URL unchanged.

- [ ] **Step 2: Run policy tests and verify failure**

Run: `./.sglang/bin/python -m pytest tests/test_compact_orchestrator_cache_first.py tests/test_llm_backend_selection.py -k 'preserve_prompt_shape or cache_first' -v`

Expected: FAIL because the public service and compact orchestrator do not accept or forward `preserve_prompt_shape`.

- [ ] **Step 3: Thread the explicit policy through service and providers**

Add the keyword-only/defaulted parameter consistently, preserving backward compatibility for existing callers:

```python
preserve_prompt_shape: bool = False,
```

In `LLMService.chat`, choose the messages once before `_last_stream_request` is recorded:

```python
prepared_messages = messages if preserve_prompt_shape else prepare_multimodal_messages(
    messages,
    max_image_dimension=self.config.model.max_image_dimension,
    debug=self.debug,
)
```

Record `prepared_messages` in `_last_stream_request` and forward them to the provider. Providers accept the parameter for interface consistency but do not transform messages themselves.

In `backend/api/chat.py`, call `prepare_multimodal_messages` immediately after `_prepare_chat_messages_for_llm(...)` and before auto-compact computes `compact_source_messages`. Use those prepared messages for the normal call and call `service.chat_stream_async(..., preserve_prompt_shape=True)`.

In `LocalLLMModel.generate` and `generate_stream`, call the helper immediately after `_inject_runtime_context(...)`, before prompt diagnostics, `_clear_compactable_payload_for_current_call`, and `_remember_compactable_payload`. Add `"preserve_prompt_shape": True` to the constructed `chat_kwargs`, including the coordinator-routing retry copy. This makes `last_compactable_llm_messages` precisely match the normal provider prompt.

Change `CompactOrchestrator._chat_stream_async` to accept `preserve_prompt_shape: bool`, and include that value in `kwargs` only when the inspected `chat_stream_async` signature accepts the named parameter or accepts `**kwargs`; retain compatibility with test doubles that do not accept it. Call it from `_run_attempt` with `preserve_prompt_shape=(plan.shape == "cache_first_tail")`. The real `LLMService` must receive `True` for cache-first and `False` for reliable fallback, so only the cache-first source is protected by the source-equivalence contract.

- [ ] **Step 4: Run policy tests and verify success**

Run: `./.sglang/bin/python -m pytest tests/test_compact_orchestrator_cache_first.py tests/test_llm_backend_selection.py -k 'preserve_prompt_shape or cache_first' -v`

Expected: PASS. The cache-first source reaches the fake service unchanged, normal service calls receive canonical copies, and the preservation path bypasses resizing.

- [ ] **Step 5: Do not commit without explicit user approval**

Do not run Git commands. Report that cache-first source preservation is protected by a direct regression test.

## Task 4: Verify Normal Chat And Agent Ordering Against Prefix-Cache Requirements

**Files:**
- Modify: `tests/test_multimodal_request_preparation.py`
- Modify: `tests/test_llm_backend_selection.py`
- Modify: `tests/test_compact_orchestrator_cache_first.py`
- Modify: `docs/superpowers/specs/2026-06-02-prefix-cache-stability-v2-design.md`

**Interfaces:**
- Consumes: the completed preparation helper, the `preserve_prompt_shape` service policy, and existing compact cache-first tests.
- Produces: regression coverage that normal prompt preparation preserves prior images across turns and that cache-first does no second transformation.

- [ ] **Step 1: Add end-to-end shape assertions before final verification**

Add a two-turn fixture with a first user message containing a data URL, an assistant response, and a second user text-only message. Prepare the full second-turn history twice. Assert both prepared histories are equal, the first-turn image stays an `image_url`, and its normalized data URL is identical between runs. This locks deterministic normal-request prefix construction rather than merely checking image presence.

Add an assertion to the cache-first test that the image-bearing `cache_source` object itself is unchanged after compact execution, including nested URL string equality. This proves the compact path did not mutate the source after it was captured.

- [ ] **Step 2: Run the new regression tests and verify failure if ordering or copying regresses**

Run: `./.sglang/bin/python -m pytest tests/test_multimodal_request_preparation.py tests/test_compact_orchestrator_cache_first.py -v`

Expected before completing this task: the tests fail if the helper is non-deterministic, strips old images, mutates nested content, or if compact re-prepares the cache-first source.

- [ ] **Step 3: Make only the minimal deterministic fixes required by the tests**

If the two preparation outputs differ, ensure the JPEG save call remains exactly `format="JPEG", quality=85, optimize=False` and does not include time-varying metadata. If the original source changes, replace shallow copies with `copy.deepcopy` at the message-content boundary. If cache-first compact touches the source, ensure it calls `preserve_prompt_shape=True` and no provider contains a second image-processing branch. The reliable fallback must retain `preserve_prompt_shape=False`.

Do not add historical-image deletion, a frontend cleanup protocol, source-budget gating, or fallback-specific cache-first rewriting.

- [ ] **Step 4: Run the full focused regression suite**

Run: `./.sglang/bin/python -m pytest tests/test_multimodal_request_preparation.py tests/test_llm_backend_selection.py tests/test_compact_orchestrator_cache_first.py tests/test_compact_chat_cache_first.py -v`

Expected: PASS. The suite demonstrates provider request parity, cache-first source preservation, normal history image retention, and existing cache-first no-trimming behavior.

- [ ] **Step 5: Run the project’s relevant static/syntax checks**

Run: `./.sglang/bin/python -m compileall -q backend`

Expected: exit code `0` with no syntax errors.

- [ ] **Step 6: Reconcile the durable prefix-cache specification**

Update `docs/superpowers/specs/2026-06-02-prefix-cache-stability-v2-design.md` so its dynamic-tail table, `TaskStep.task_images` rule, and implementation constraints no longer describe `_strip_old_images` or `[图片×N]`. State all of the following explicitly:

```text
- Historical image_url parts remain in the dynamic tail and are never converted to text.
- Normal requests apply at most one deterministic data-URL resize before the source is captured.
- Cache-first compact requests use the captured source unchanged and bypass all image preparation.
```

Do not revise unrelated v2 rules about runtime context, tool traces, routing, or context packs.

- [ ] **Step 7: Do not commit without explicit user approval**

Do not use Git. Summarize the exact tests run and their results for the user.
