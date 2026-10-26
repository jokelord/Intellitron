# Compact v2 refactor E2E latency review — diagnosis report

> Reviewer: independent code review
> Target: 13 commits from cf76cea..26f5555 (`v2 compact prompt turn-shape`)
> User symptom (unchanged after v2): E2E compact on 40k context > 5 min, SGLang mostly in `Decode batch` stage
> Verdict on implementer's claims: **two of the three are false; one is partial**

---

## Section A — Verification of the implementer's claims

### Claim 1: "v2 path makes compact's messages byte-equal to the previous chat calls' messages (except 1 trailing user row)"

**VERDICT: FALSE in production for any session whose history contains tool calls, observation rounds, or `<tool_call>`-style assistant turns.**

The v2 builder (`backend/compact/prompt_builder.py:78-109`) does in fact `list(body_messages)` with **0 transformation** — that much is correct. The flaw is upstream: the messages it receives are not the same shape that the previous chat call sent to SGLang.

**Path the previous chat call takes** (e.g. `AgentRunner._run_basic_stream` → `LocalLLMModel.__call__`):

1. `LocalLLMModel.generate` / `generate_stream` (`backend/agent/local_llm_model.py:269-308, 399-440`) calls `get_clean_message_list(...)` with `role_conversions=tool_role_conversions` (line 300, 430).
2. `get_clean_message_list` is `smolagents.models:272-337`. Two things happen here that the v2 path does not reproduce:
   - **Role conversion** (line 297-298): `TOOL_CALL → ASSISTANT` and `TOOL_RESPONSE → USER`. Without this, `write_memory_to_messages` would emit raw `tool_call`/`tool_response` rows that the chat template rejects (line 144: `raise_exception('Unexpected message role.')`).
   - **Adjacent-message merging** (line 315-325): when two consecutive messages have the same role, their content lists are concatenated (text-merged with `"\n"`) and emitted as a single message. This is critical: a `model_output` assistant row followed by a `TOOL_CALL` row (line 1344 + 1362 of `write_memory_to_messages`) collapses into a single `<|im_start|>assistant\n[model_output]\nCalling tools: [...]<|im_end|>` block, because both convert to `assistant` and are adjacent.
3. `_inject_runtime_context` (line 309) appends `runtime_context` to the system message — happens to be a no-op in this project (the `LocalLLMModel` is constructed without `runtime_context` at `agent_runner.py:2328`, so `self.runtime_context = None`).
4. SGLangProvider.generate (`backend/llm_service.py:397`) also calls `_inject_runtime_context` (no-op for both paths).
5. SGLangProvider strips images from non-last user messages via `_strip_old_images` (`llm_service.py:542-586`) and re-encodes oversized images via `_normalize_and_log_images` (`llm_service.py:434-532`). Neither runs on the compact path.

**Path the v2 compact call takes** (`AgentRunner._run_compact_then_continue` at `agent_runner.py:4563-4609` → `CompactOrchestrator.run_compact` at `orchestrator.py:53-86` → `LLMService.chat_stream_async`):

1. `chat_messages = list(session.agent.write_memory_to_messages())` (line 4582). The `write_memory_to_messages` output is a `list[ChatMessage]` of smolagents objects. For the **live** action step (the one in the current turn — line 1593-1618, "stripped.observations_images = None" path), it emits **4 distinct ChatMessages per action step**: `ASSISTANT(model_output)`, `TOOL_CALL("Calling tools: [...]")`, `TOOL_RESPONSE("Observation: ...")` and (if error) another `TOOL_RESPONSE("Error: ...")`.
2. `_chat_messages_to_backend_messages` (`agent_runner.py:2277-2318`) converts each ChatMessage to a `Message` independently. It does:
   - `TOOL_CALL → assistant` and `TOOL_RESPONSE → user` role translation (line 2298-2301) — **good, equivalent to smolagents `tool_role_conversions`**.
   - **No adjacent-message merging.** Each `TOOL_CALL → assistant` row stays as its own Message after the prior `ASSISTANT(model_output) → assistant` row. So the v2 path produces:
     ```
     [system, user(task), assistant(model_output), assistant("Calling tools: [...]"),
      user("Observation: ..."), assistant(model_output), assistant("Calling tools: ..."),
      user("Observation: ..."), ...]
     ```
     while the previous chat call produced:
     ```
     [system, user(task), assistant("model_output\nCalling tools: [...]"),
      user("Observation: ..."), assistant("model_output\nCalling tools: [...]"),
      user("Observation: ..."), ...]
     ```
   - **No `_strip_old_images` and no image re-encoding.** The compact path loses the chat-mode image-strip optimization (negligible for compact, since the previous chat call also sent 0 images for tool-calling turns).
3. `CompactOrchestrator.run_compact` (line 96-106) wraps each `Message` in a `Message(...)` constructor again (defensive but a no-op).
4. `chat_stream_async` calls `chat()` → `SGLangProvider.generate` → HTTP POST to SGLang.

**Token-sequence consequences (most important):**

The Qwen3.5 chat template (`~/Project/LLM-weight/Qwen3___5-35B-A3B-GPTQ-Int4/chat_template.jinja:46-154`) renders the assistant role with conditional `<|im_start|>assistant\n[content]<|im_end|>\n` (line 89-103), and the `<tool_call>` XML is **only emitted** when the message dict has a `tool_calls` field (line 105-129). The previous chat call's assistant message had a `tool_calls` field (smolagents puts it there from the model's structured output), so the rendered tokens were:

```
<|im_start|>assistant\n[model_output text]<tool_call>\n<function=name>\n<parameter=...>value</parameter>\n</function></tool_call><|im_end|>\n
```

The v2 compact call's assistant message is `Message(role="assistant", content="Calling tools:\n[{...JSON...}]")` with **no `tool_calls` field**. The rendered tokens are:

```
<|im_start|>assistant\nCalling tools:\n[{"id": ..., "type": "function", "function": {"name": ..., "arguments": ...}}]<|im_end|>\n
```

The two token sequences are **completely different for every assistant turn that used tools**. The Qwen3 tokenizer will tokenize `<tool_call>\n<function=name>\n<parameter=...>value` and `Calling tools:\n[{...}]` to entirely different token IDs. SGLang's radix cache key on the **literal token-id sequence** (`radix_cache.py:191-198` `_key_match_page_size1` — `if k0 != k1: break`).

**Net effect**: the v2 path's "byte-equal" claim is false at the SGLang-cache level for the dominant case (any session with tool calls). The v1 path was false at the cache level always. **The v2 improvement is real but much smaller than the implementer claimed — typically 0% on the assistant turn tokens and partial on the user turn tokens (those that don't carry images, since `_strip_old_images` is not replicated).**

The single test that "verifies" the equivalence is `tests/test_compact_v1_v2_equivalence.py:46-71` — it only checks that body **substrings** appear in v1 vs. v2 outputs. It does **not** check token-equality with the previous chat call's messages. It does **not** render through the Qwen3 chat template. It does **not** invoke `get_clean_message_list`. It would pass for any v2 builder that contains all the body content, regardless of structure. This is a substring-coverage test, not an SGLang-cache-byte-equal test, and the docstring (line 3-4) explicitly says so — but the spec (§9.1 [V2]) misnames it "equivalence test" and the implementer appears to have taken it as proof of byte-equality.

The chat-template render test (`tests/test_compact_prompt_builder_v2_template_compat.py`) renders the v2 output through the Qwen3 template to confirm `multi_step_tool` does not raise. It does not compare the rendered string to the previous chat call's rendered string. So it also passes despite the byte-unequality.

### Claim 2: "SGLang radix cache will reuse all prior chat KV blocks (system + history)"

**VERDICT: PARTIALLY TRUE — system message is reused, history is largely not reused.**

Per Claim 1, the assistant rows with `<tool_call>` JSON rendered text differ from the previous call's `<tool_call>` XML. Same for the user rows after `_strip_old_images` (which replaces non-last user images with `[图片×N]` text — see `llm_service.py:580-583`). The compact path emits the user row's content as the **raw flattened string**, which may differ from what the previous chat call sent (since the previous call may have stripped images).

The system message is the one part that is most likely to be byte-equal:
- Both paths start from `memory.system_prompt.to_messages()` (line 1238 of `agent_runner.py`).
- Both paths get a `Message(role="system", content=<list-of-dicts-with-text>)` (system.to_messages returns `[{"type": "text", "text": <system_prompt_str>}]` at `smolagents/memory.py:206`).
- The previous chat call preserves the list-of-dicts; the compact call flattens it to a plain str (`agent_runner.py:2305-2311`). SGLang's `build_message_content` (`llm_service.py:386-395`) sends both correctly; the chat template renders both to the same tokens (text part is just a string, no role-boundary differences). **So the system message tokens should match the previous chat call's system tokens.**

The mamba state, which the spec §1.4 cites as the second leg of reuse, is at the **mamba_chunk_size boundary**. The mamba state at the end of system is one checkpoint; at the end of the first assistant turn is another. The mamba branching point (`mamba_radix_cache.py:1035-1044`) is the largest multiple of `mamba_cache_chunk_size` ≤ matched-prefix length. If the system message ends at a chunk-aligned position (likely but not guaranteed), the mamba state is reused. If the assistant tokens differ, the mamba state at the end of the first user turn may or may not be reused depending on the chunk alignment.

**Net effect**: prefix-cache hit rate for the v2 path is approximately the system message's tokens (~5-10k, depending on tool schema and runtime_context). This is real reuse, and better than v1 which got 0. But the spec's promise of "80-95% prefix cache hit" is wildly optimistic given that the assistant rows with `tool_calls` produce different token sequences.

### Claim 3: "Only the trailing user row needs prefill (~150-300 tokens)"

**VERDICT: FALSE in the dominant case.**

The trailing compact-instr user message is small (it is a few hundred tokens of `compact_user_instruction` from `config/config-agent-prompts.yaml` line ~401-424). That message does need prefill. **But it is not the only thing that needs prefill.** Every assistant row with `Calling tools: [...]` text is a token-sequence mismatch with the previous chat call's `<tool_call>` XML, so the radix tree branches at the first divergent token, and everything from that point forward (including all subsequent user/assistant turns) is a cache miss. The SGLang scheduler will prefill all of those.

The prefill token count is therefore **bounded below by the total number of divergent tokens across the message list**, not by the trailing user message. For a 40k context with ~10 assistant turns each carrying `<tool_call>` JSON, the diverging prefix can be 25-35k tokens even under v2.

---

## Section B — The real reason v2 does not help in production

The implementer's diagnosis was that v1 sent `[system(compact 指令), user<整段历史>]` which differed from previous chat calls and forced a full re-prefill. The conclusion was: make compact's input byte-equal to the previous chat calls. The implementation: pass `write_memory_to_messages()` output to `build_compact_messages_v2()` and append 1 user row.

The actual reason v2 doesn't help is that **`write_memory_to_messages()` output is structurally not what was sent to SGLang in the previous chat call**. There are three concrete divergences that prevent byte-equality:

1. **No adjacent-message merging** (`agent_runner.py:2277-2318` vs. `smolagents/models.py:315-325`). The compact path emits the live action step's `ASSISTANT(model_output)` + `TOOL_CALL(Calling tools: [...])` as 2 separate `Message(role="assistant", ...)` rows. The previous chat call emitted them as 1 merged row (after `get_clean_message_list` ran `tool_role_conversions` + merging). Two `<|im_start|>assistant\n...<|im_end|>` blocks tokenize differently from one merged block.

2. **No `<tool_call>` XML rendering** (chat template `chat_template.jinja:105-129` only renders `tool_calls` from the message dict, but `write_memory_to_messages` strips the `tool_calls` field and inlines the JSON as text — `agent_runner.py:1267-1325`). The previous chat call's assistant message had a real `tool_calls` field, so the chat template rendered the structured `<tool_call>\n<function=name>\n<parameter=...>` XML. The compact path's assistant message is just text, so the chat template renders plain text. The two tokenize to completely different token IDs.

3. **No `_strip_old_images` / `_normalize_and_log_images`** (`llm_service.py:542-587`). For vision sessions, the previous chat call sent user rows with images stripped to `[图片×N]` placeholders for non-last user rows, and oversized images re-encoded. The compact path sends the raw user content. The user rows that have images in the original will tokenize differently between the two paths.

The implementer also missed that **SGLang's "Decode batch" status is a misleading indicator**. The user's `踩坑记录4.md` line 105 already documents this: "chunked prefill 期间的每次 forward 都会被调度器 log 成 'Decode batch'". So "mostly in Decode batch" does not mean "in decode phase"; it means SGLang is doing long-prefill, which it logs as Decode batch under mixed-chunk scheduling. The `gen throughput` of 19-24 tok/s from `踩坑记录4.md` line 40 and the user's most recent observation is the time-average over both prefill (where the model generates 0 tokens because it's still prefill'ing) and decode (where it generates the summary). The prefill phase dominates, not the decode.

---

## Section C — Revised theory of what's actually slow

Cost decomposition for a 40k-context compact call in this project, in the order each component contributes to wall time:

| Component | Token count | Time on Jetson Thor (Blackwell) | Source |
|---|---|---|---|
| **Prefill of all v2-mismatched assistant/user turns** | ~25-35k tokens | 30-90s (chunked prefill, ~3-5 chunks of 8k) | Dominant |
| **Prefill of the compact-instr trailing user row** | ~200 tokens | <1s (depends on chunk alignment) | Trivial |
| **Decode of the summary** | up to `max_summary_tokens: 20000` | `20000 / 24 ≈ 833s ≈ 14 min` (at 24 tok/s) | Variable |
| **Network/serialization overhead** | n/a | <1s | Trivial |
| **mamba state recompute at chunk boundaries** | ≤1 chunk per re-prefill'd chunk | <5s | Minor |

The decode phase can be dominant if `gen throughput` is in the 20-50 tok/s range. The user reports >5 min total, with SGLang "mostly in Decode batch". A 14-min decode at 24 tok/s fits "mostly Decode", but a 5-min total time implies either:
- The summary output is shorter than 20k (truncation), or
- The decode throughput is much higher than 24 tok/s for this particular compact run (e.g. 60-80 tok/s after v2 prefix-cache hit on system), or
- The prefill itself is the dominant phase and "Decode batch" status is a misleading log artifact (consistent with `踩坑记录4.md`).

**Hypothesis that the implementer missed**: the v2 prefix-cache reuse on the system message (~5-10k tokens) reduces the prefill time, but the divergence on assistant turns forces prefill of 25-35k of the prompt, which still dominates. **The 80-95% hit rate promised in the spec is unreachable given Claim 1's failure modes.**

A second, more direct cost: **the decode phase itself, for a 20k-token summary, is 14 min at 24 tok/s**. Even if prefill is 0, the user would still see >5 min wall time. The v2 refactor does not change the decode phase at all — it only affects prefill. The implementer may have been measuring the wrong phase.

A third cost that the implementer did not address: **`max_summary_tokens: 20000` is set in `config/config-misc.yaml:17`**. Generating 20k tokens of summary is intrinsically slow at 24 tok/s. The LLM is asked to write a 9-section structured summary (`<analysis>` + `<summary>` with 9 sections, see `config/config-agent-prompts.yaml::compaction::main_prompt` — copied verbatim from `ref_src/services/compact/prompt.ts:61-143`). The model typically produces 2-3k tokens of analysis plus 2-3k tokens of structured summary, but the architecture allows up to 20k. If for some reason the model writes more verbose output (e.g. includes large code blocks verbatim), the decode phase balloons.

The SGLang `mamba_full_memory_ratio: 0.4` (per the user's report) and `chunked_prefill_size: null` (`config/qwen3__5-config-infer.yaml:90`) together determine how much KV pool is available for the 40k prefill. With `mem_fraction_static: 0.8` (line 51), the model + KV pool = 0.8 of GPU memory. mamba gets 0.4 of the post-model memory, KV gets 0.6. For a 40k-token prefill, this is enough for the 40k KV but tight. If the previous chat calls left KV blocks in the pool that are not at the same prefix, those don't help; new KV blocks for the diverging tokens are allocated fresh. SGLang's eviction policy (`full_lru_list` at `mamba_radix_cache.py:1021`) may evict old blocks.

**Summary of where time goes in 5 min of compact on 40k context:**

1. **Prefill of diverging assistant/user rows** (v2 still fails on these): ~30-90s. The user sees this as SGLang "stuck" with no tokens.
2. **Prefill of trailing user row**: <1s, but interleaved with the above.
3. **Decode of the summary**: variable, 2-14 min depending on length and throughput.
4. **Mamba checkpoint recompute at chunk boundaries during the re-prefill**: minor, <5s per chunk × 4 chunks = ~20s.

If "SGLang is mostly in Decode batch stage" is a misleading label and the system is actually still prefill'ing (per `踩坑记录4.md:105-117`), then prefill dominates and v2 has reduced it but not eliminated it. If "Decode batch" is the actual decode phase, then `max_summary_tokens: 20000` × 24 tok/s = 14 min decode is the dominant cost.

---

## Section D — Recommendation: what to do next (diagnosis only, no code fix yet)

The implementer should do the following, in order, before proposing any code change. The goal is to get the diagnosis right; without a correct diagnosis, the next 1-3 fix attempts will be wasted (per `systematic-debugging` Phase 4.5: "3+ fixes failed → question the architecture").

### Step 1 — Verify the byte-unequality claim with actual data

Add a one-off instrumentation that logs, for each compact LLM call, the actual token-id prefix length that SGLang's radix cache hit. SGLang exposes `MambaRadixCache.match_prefix` which returns `device_indices` and `mamba_branching_seqlen` (`mamba_radix_cache.py:1074-1078`). The number of bytes the SGLang scheduler log reports as "matched" is the actual reuse. The expected outcome: if my Claim 1 is right, the matched prefix is approximately the system message's tokens (~5-10k) and not 80-95% of the 40k prompt.

If the matched prefix is close to the 40k prompt, then the implementer's theory is correct and there's a different problem (e.g. mamba pool exhausted, KV pool exhausted). If it's ~10k, then the byte-unequality claim is the real bottleneck and the fix needs to be at the message-shape level.

### Step 2 — Measure prefill vs. decode cost separately

Add a log line in `CompactOrchestrator.run_compact` (or in the SGLang client wrapper) that records:
- Wall time from `chat_stream_async` start to the first `chunk.content` arriving (this is the prefill phase).
- Wall time from first chunk to last chunk (this is the decode phase).
- Number of tokens in the prompt (via `SGLangProvider._normalize_and_log_images` + `TokenCounter.count_messages`).
- Number of tokens in the accumulated output.

The expected outcome: if prefill is <30s and decode is 4-13 min, the dominant cost is decode (which v2 doesn't affect). If prefill is 1-3 min, then v2's incomplete prefix-cache hit is the dominant cost.

### Step 3 — Distinguish the four candidate cost drivers

| Cost driver | Symptom | Test |
|---|---|---|
| **A. v2 still has 25-35k of diverging tokens to prefill** | First-chunk latency >30s, "Decode batch" status during that period | If matched-prefix in Step 1 is < 20k, this is the issue |
| **B. Decode of long summary is intrinsically slow** | First-chunk latency <10s, then long steady stream of `content` deltas at 20-50 tok/s | If decode phase > 3 min, reduce `max_summary_tokens` or shorten the structured summary template |
| **C. KV pool exhaustion under `mamba_full_memory_ratio: 0.4`** | `full token usage` in SGLang log approaches 1.0; prefill stalls | If usage >0.9, drop `mamba_full_memory_ratio` to 0.3 |
| **D. Mamba state recompute at every chunk boundary because previous chat call ended at non-chunk-aligned position** | prefill throughput is fine for first chunk, then 5-10x slowdown for subsequent chunks | Look for SGLang log "mamba recompute" or "mamba checkpoint miss" |

The first three are the most likely. The fourth is a corner case.

### Step 4 — Once diagnosis is right, choose the matching fix

| If diagnosis is | Then the fix is |
|---|---|
| **A. Diverging tokens force re-prefill** | Architectural: change `write_memory_to_messages` to emit messages in the same shape that `get_clean_message_list` produces for the chat path. Specifically: (i) apply adjacent-message merging in `_chat_messages_to_backend_messages`, (ii) add a "render `Calling tools:` text into a `<tool_call>` XML field on a fake `tool_calls` list" pass that the chat template will then render correctly, (iii) add `_strip_old_images` and image re-encoding to the compact path. This is the most invasive fix and the only one that actually delivers the spec's promised 80-95% hit rate. |
| **B. Decode is dominant** | Behavioral: lower `max_summary_tokens` (e.g. from 20000 to 4000) and tighten the structured summary to be 4-5 sections instead of 9. The 9-section template from `ref_src` was designed for a different model / use case. |
| **C. KV pool exhausted** | Parameter tuning: lower `mamba_full_memory_ratio` from 0.4 to 0.2-0.3 in `config/qwen3__5-config-infer.yaml`. Verify the SGLang launch script (`scripts/1_start_sglang.sh:160`) picks it up. |
| **D. Mamba state recompute at chunk boundaries** | Parameter tuning: set `chunked_prefill_size: 4096` so each prefill chunk fits within one mamba checkpoint (chunk size is typically 64 tokens; prefill chunk 4096 means 64 mamba checkpoints per chunk, but the branching point can be at the end of any 64-token boundary). This doesn't directly fix the issue, but it makes the cost more predictable. |

Most likely a combination of A and C, since the system prompt is the only part that v2 reused, and 30k of divergent tokens still need to be prefill'd, which puts pressure on a KV pool that has only 60% of post-model memory.

### What NOT to do (in this diagnosis phase)

- **Do not** add another message-shape optimization. The current `_chat_messages_to_backend_messages` is a thin adapter; further "fixes" to it will entrench the wrong model of the v2 contract (which is "the compact path's messages list = the previous chat call's messages list").
- **Do not** lower `chunked_prefill_size` blindly. `踩坑记录4.md:332` already notes that 4096 can help, but it's a tuning knob, not a root-cause fix.
- **Do not** change `mamba_full_memory_ratio` without first measuring. The current 0.4 may already be right; the user may be KV-pool-bound, not mamba-bound.
- **Do not** trust the existing v2 tests (`test_compact_prompt_builder_v2_template_compat.py`, `test_compact_v1_v2_equivalence.py`) as proof of byte-equality. They check chat-template rendering does not raise and that body substrings are present, respectively. They do not check token-id equality with the previous chat call. The single source of truth for byte-equality would be a test that calls both `LocalLLMModel.__call__(...)` and the compact path with the same `memory.steps`, captures the messages, renders both through the chat template, and asserts the rendered strings are byte-equal. This test does not exist. Add it (or, more practically, an end-to-end integration test that uses SGLang's `match_prefix` to assert the matched prefix length is within 5% of the prompt length).

---

## Appendix — Concrete code references for the claims in Section A

| Claim | File:line | What it shows |
|---|---|---|
| v2 builder 1:1 reuses `body_messages` | `backend/compact/prompt_builder.py:101-107` | `final = list(body_messages)` (no transformation) |
| v2 builder appends 1 trailing user | `backend/compact/prompt_builder.py:107` | `final.append(Message(role="user", content=...))` |
| chat path uses `get_clean_message_list` with `tool_role_conversions` | `backend/agent/local_llm_model.py:298-302` | role conversion + adjacent merging |
| `tool_role_conversions` mapping | `smolagents/models.py:237-240` | `TOOL_CALL → ASSISTANT`, `TOOL_RESPONSE → USER` |
| adjacent-message merging | `smolagents/models.py:315-325` | when same role, content is merged (text by `"\n"`) |
| live action step emits 4 distinct ChatMessages | `backend/agent/agent_runner.py:1342-1409` | `ASSISTANT(model_output)` + `TOOL_CALL(Calling tools)` + `TOOL_RESPONSE(Observation)` + optional `TOOL_RESPONSE(Error)` |
| `_chat_messages_to_backend_messages` does no merging | `backend/agent/agent_runner.py:2277-2318` | one Message per ChatMessage, only role translation + content flatten |
| Qwen3 chat template renders `tool_calls` field as `<tool_call>` XML | `chat_template.jinja:105-129` | only when `message.tool_calls` is iterable (not for plain text) |
| Qwen3 chat template treats `tool_response` rows as `<tool_response>...</tool_response>` envelopes | `chat_template.jinja:131-142` | special role "tool" |
| v2 emits assistant rows with `Calling tools:` text, no `tool_calls` field | `backend/agent/agent_runner.py:1323-1325` | `return "Calling tools:\n" + json.dumps(serialized_calls, ...)` |
| SGLang radix cache matches on token-id equality | `sglang/srt/mem_cache/radix_cache.py:191-198` | `_key_match_page_size1`: `if k0 != k1: break` |
| SGLang mamba branching point is at chunk-aligned positions | `sglang/srt/mem_cache/mamba_radix_cache.py:1035-1044` | `mamba_cache_chunk_aligned_seqlen = sum // chunk_size * chunk_size` |
| Previous chat path applies `_strip_old_images` | `backend/llm_service.py:586` | non-last user rows get `[图片×N]` placeholders |
| Previous chat path applies image re-encoding | `backend/llm_service.py:434-532` | `_normalize_and_log_images` |
| v2 equivalence test is a coverage test, not a token test | `tests/test_compact_v1_v2_equivalence.py:46-71` | asserts body substrings are present, not token-equality |
| v2 chat-template render test is a smoke test, not a byte-equal test | `tests/test_compact_prompt_builder_v2_template_compat.py:67-97` | renders, checks not raise, checks count of `<|im_start|>` |
| Compact orchestrator calls `chat_stream_async` directly | `backend/compact/orchestrator.py:172-179` | no `LocalLLMModel`, no `get_clean_message_list` |
| Compact path: `runtime_context=None` (no-op for `_inject_runtime_context`) | `backend/compact/orchestrator.py:172-179` + `backend/llm_service.py:1856-1865` | `chat_stream_async(... runtime_context=None)` |
| `max_summary_tokens: 20000` | `config/config-misc.yaml:17` | 20k summary tokens; 14 min at 24 tok/s decode |
| `chunked_prefill_size: null` in active config | `config/qwen3__5-config-infer.yaml:90` | SGLang auto-derives (likely 4096 for 60-90GB GPU) |
| `mamba_full_memory_ratio: 0.4` per user report | n/a (not in current yaml) | needs to be added to yaml or env |
| `mamba_full_memory_ratio` default in SGLang | `sglang/srt/server_args.py:550` | `0.9` (changed in PR #11679) |
| Previous `踩坑记录4.md` symptom: 19.45 tok/s decode throughput at 40k context | `踩坑记录4.md:32` | matches the user's current observation |
| Previous `踩坑记录4.md` observation: "Decode batch" status is misleading during prefill | `踩坑记录4.md:105-117` | the user's "Decode batch" indicator is a label artifact |

---

## Final summary

**The v2 refactor was implemented faithfully to the spec, but the spec's central claim ("v2 makes compact's messages byte-equal to the previous chat calls' messages") is false in production.** The implementer followed the right procedure (use `write_memory_to_messages` instead of `_session_memory_to_messages`), but the result is not byte-equal because the chat path applies transformations that `write_memory_to_messages` does not (`get_clean_message_list`'s adjacent-message merging, the chat template's `tool_calls` XML rendering, and `_strip_old_images`). The v2 path delivers real reuse on the system message (~5-10k tokens) but misses reuse on the assistant turns with tool calls (the dominant case). The cost the user is paying in E2E is dominated by (a) re-prefill of the diverging assistant/user tokens (still ~25-35k) and (b) decode of the 20k-token summary (~14 min at 24 tok/s).

The right next step is **diagnostic instrumentation, not a code change**: measure the actual SGLang radix-cache matched prefix length and the prefill-vs-decode wall time split. Once those are known, the fix can be chosen from the four candidates in Section D, all of which target the actual cost driver rather than the surface-level "shape similarity" that the spec focused on.
