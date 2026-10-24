# vLLM Multimodal Request Parity Design

## Goal

Make normal SGLang and vLLM requests use the same multimodal message preparation and sampling configuration while preserving every historical image part for prefix-cache reuse. Compaction cache-first requests must send their captured source prompt unchanged.

## Scope

- Send configured `temperature`, `top_p`, `top_k`, and `repetition_penalty` to both SGLang and vLLM.
- Preserve the existing output-token field mapping: SGLang receives `max_new_tokens`; vLLM receives OpenAI-compatible `max_tokens`.
- Remove historical-image stripping entirely. A prior user-turn `image_url` must remain an image part; it must never become a `[图片×N]` placeholder.
- Move data-URL image normalization out of `SGLangProvider` into one shared, non-mutating helper.
- Use the helper before normal-chat auto-compact source construction and before agent compactable-payload capture.
- Mark cache-first compact calls as prompt-shape preserving so their captured messages are sent unchanged. Reliable fallback remains eligible for the normal one-time image normalization because it is not a prefix-cache reuse path.

## Non-Goals

- Do not change model weights, vLLM launch configuration, frontend message state, compact source budgets, or cache-first prompt shape.
- Do not remove, summarize, reorder, or replace historical image parts in normal chat, agent, reliable compact, or cache-first compact messages.

## Data Flow

1. A shared helper returns copied `Message` objects and copied multimodal content. It never mutates caller-owned messages.
2. For normal requests, it preserves all message roles, text parts, image parts, and image ordering. It only normalizes oversized data-URL images against `model.max_image_dimension`.
3. Normal chat invokes the helper before its auto-compact source is derived. Agent calls invoke it before prompt diagnostics and `last_compactable_llm_messages` capture.
4. Providers receive already-prepared normal messages. Direct callers that do not prepare messages retain a default preparation path in `LLMService`.
5. Compact orchestration passes an explicit prompt-shape-preserving flag through `chat_stream_async` and `chat` only for the cache-first plan. That path bypasses normalization. Its cache-first body therefore remains byte-for-byte equivalent in message content to the captured normal source, apart from the appended compact-control tail. The reliable fallback uses normal preparation because it has no prefix-cache equivalence guarantee.

## Provider Requests

- Both providers emit every configured sampling value: `temperature`, `top_p`, `top_k`, and `repetition_penalty` (including the explicit neutral value `1.0`).
- Extra-body values keep their existing precedence because they are merged after the default request body.
- vLLM retains its context-budget capping after the request body is built.

## Errors And Diagnostics

- Invalid, remote, or undecodable image URLs remain intact rather than failing a request. A diagnostic log identifies normalization failures.
- An absent Pillow installation skips normalization but preserves message content.
- The helper logs image count and normalization details consistently for both providers.

## Tests

- Assert SGLang and vLLM request bodies contain their correctly named output limit plus all configured sampling controls.
- Assert normal historical images remain `image_url` parts and oversized images are normalized for vLLM as well as SGLang.
- Assert cache-first compact calls pass the preservation flag and therefore do not call the mutating transformation.
- Assert preprocessing does not mutate input `Message` objects and that agent compactable payloads retain the exact prepared normal-request shape.
