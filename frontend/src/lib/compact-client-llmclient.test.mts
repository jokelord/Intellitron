/**
 * Regression: llmClient.runCompact must hit the absolute backend URL,
 * not a relative path.
 *
 * Why: the Next.js dev frontend runs on :3000 and the backend on :8090.
 * A relative URL '/v1/compact/run' would hit :3000 (no such route) and
 * 404, which was the actual bug the user hit before this regression
 * test was added. The compact-client's runCompactSSE documents this
 * requirement; this test pins it via the public LLMClient method.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

test("regression: LLMClient.runCompact posts to baseUrl + /v1/compact/run (absolute URL)", async () => {
  const orig = globalThis.fetch;
  let observedUrl: string | undefined;
  let observedMethod: string | undefined;
  (globalThis as { fetch: typeof fetch }).fetch = (async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ) => {
    observedUrl = typeof input === "string" ? input : input.toString();
    observedMethod = init?.method;
    return new Response("data: [DONE]\n\n", {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  }) as typeof fetch;
  try {
    const { LLMClient } = await import("./api.ts");
    const client = new LLMClient("http://localhost:8090");
    await client.runCompact(
      {
        mode: "chat",
        session_id: null,
        messages: [{ role: "user", content: "hi" }],
        custom_instructions: null,
      },
      () => {}
    );
    assert.equal(observedMethod, "POST");
    assert.equal(
      observedUrl,
      "http://localhost:8090/v1/compact/run",
      "runCompact must use the absolute baseUrl, never a relative path"
    );
  } finally {
    (globalThis as { fetch: typeof fetch }).fetch = orig;
  }
});
