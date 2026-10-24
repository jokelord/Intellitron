/**
 * SSE event parser for /v1/compact/run.
 *
 * Used by both pages' timeline event handlers to interpret compact_* events
 * arriving on the manual /compact endpoint or auto-compact streams.
 */

import { getChatRequestHistoryStartRef } from './local-memory.ts';
import { buildCompactMetadata, buildMergedSystemContent, projectChatMessagesWithTransportRefs, type Message } from "./message-types.ts";

export type CompactClientEvent =
  | {
      kind: "compact_started";
      summary_message_id: string;
      text?: string;
      partial?: boolean;
      metadata?: Record<string, unknown>;
    }
  | {
      kind: "compact_delta";
      summary_message_id: string;
      text: string;
      partial: boolean;
      metadata?: Record<string, unknown>;
    }
  | {
      kind: "compact_completed";
      summary_message_id: string;
      text: string;
      partial: false;
      metadata?: Record<string, unknown>;
    }
  | {
      kind: "compact_failed";
      summary_message_id: string;
      text?: string;
      partial?: boolean;
      metadata?: Record<string, unknown>;
    };

export function parseCompactSSEEvents(raw: string): CompactClientEvent[] {
  const events: CompactClientEvent[] = [];
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("data:")) continue;
    const payload = trimmed.slice(5).trim();
    if (payload === "[DONE]") continue;
    try {
      events.push(JSON.parse(payload));
    } catch {
      // ignore malformed lines
    }
  }
  return events;
}

export type RunCompactSSEOptions = {
  url: string;
  body: unknown;
  signal?: AbortSignal;
  onEvent: (ev: unknown) => void;
};

/**
 * Build the wire request body for POST /v1/compact/run.
 *
 * CRITICAL: the `mode` field must reflect the runtime context that
 * actually drives the next LLM call.
 *
 *   - `mode='chat'`  → the request lands on the CHAT compact orchestrator,
 *     which resolves accepted user tails through the Chat session and
 *     opaque metadata.chat_turn_ref references. UI memory showcases are
 *     excluded from the request; the server owns canonical model text.
 *
 *   - `mode='agent'` → the request lands on
 *     AgentRunner.run_compact_for_session, which REPLACES
 *     `session.agent.memory.steps` with the envelope step. This is
 *     the source of truth for the next /v1/agent/chat request, NOT
 *     the React `messages` state.
 *
 * Hard-coding mode='chat' in agent mode was the bug that produced
 * SGLang's "compact 3k → next chat 40k" pattern (the React state
 * showed a compact_summary bubble, the user thought it worked, but
 * the next /v1/agent/chat still saw the uncompacted memory.steps).
 *
 * The session_id is forwarded in BOTH modes so the backend can route
 * / correlate correctly. Chat resolves frozen canonical user tails;
 * Agent looks up its memory session.
 */
export interface BuildCompactRequestBodyArgs {
  agentMode: boolean;
  sessionId?: string | null;
  chatHistoryStartRef?: string;
  messages: Message[];
  customInstructions?: string | null;
  /**
   * System prompt text to prepend as the first message in the wire body.
   * Mirrors the pattern in `handleSend` (page.tsx:438-454 and
   * vision/page.tsx:431-447) so the /compact endpoint sees the same
   * system context the next /v1/chat/completions (or /v1/agent/chat)
   * call will see. Required by the v2 compact prompt builder
   * (`backend/compact/prompt_builder.py:98-99`), which raises
   * ValueError if `body_messages[0].role !== "system"`.
   *
   * Empty string or null/undefined → treated as absent. Whitespace-only
   * is also treated as absent (trim() → empty).
   */
  systemPrompt?: string | null;
  /**
   * Runtime time context string. When both `systemPrompt` and
   * `runtimeContext` are non-empty (after trim), they are MERGED into a
   * single system message separated by `"\n\n"` — `systemPrompt` first,
   * then `runtimeContext` — mirroring the push order in `handleSend`
   * (page.tsx:438-454, vision/page.tsx:431-447). SGLang rejects requests
   * with multiple consecutive system messages ("System message must be
   * at the beginning"), so we cannot send them as two separate system
   * messages. The backend's `_inject_runtime_context`
   * (`backend/llm_service.py:80-101`) does the same merge for non-compact
   * chat paths; the v2 compact builder does NOT inject runtime_context
   * (per spec §6.1 I3), so the frontend must produce the merged form.
   *
   * Pass `buildRuntimeTimeContext()`'s output here when
   * `runtimeContextConfig.enable_runtime_time_context` is true.
   *
   * Empty string or null/undefined → treated as absent. Whitespace-only
   * is also treated as absent.
   */
  runtimeContext?: string | null;
}

export interface CompactRequestBody {
  mode: "chat" | "agent";
  session_id: string | null;
  chat_history_start_ref?: string;
  messages: Array<{
    role: "system" | "user" | "assistant";
    content: unknown;
    metadata?: Record<string, unknown>;
  }>;
  custom_instructions: string | null;
}

export function buildCompactRequestBody(
  args: BuildCompactRequestBodyArgs
): CompactRequestBody {
  const sourceMessages = !args.agentMode && args.sessionId
    ? projectChatMessagesWithTransportRefs(args.messages, args.sessionId) : args.messages;
  const userMessages = sourceMessages.map((m) => {
    const metadata = buildCompactMetadata(m);
    return {
      role: m.role,
      content: m.content,
      ...(metadata ? { metadata } : {}),
    };
  }) as CompactRequestBody["messages"];

  // Build the merged system message content. SGLang rejects requests
  // with multiple consecutive system messages, so we MUST merge
  // systemPrompt and runtimeContext into a single system message when
  // both are non-empty (after trim). Order: systemPrompt first, then
  // runtimeContext, separated by "\n\n" — mirroring the push order in
  // handleSend (page.tsx:438-454). Whitespace-only inputs are treated
  // as absent.
  const systemContent = buildMergedSystemContent(args.systemPrompt, args.runtimeContext);

  const prependedSystem: CompactRequestBody["messages"] = systemContent
    ? [{ role: "system", content: systemContent }]
    : [];

  return {
    mode: args.agentMode ? "agent" : "chat",
    session_id: args.sessionId ?? null,
    ...(!args.agentMode && args.sessionId ? {
      chat_history_start_ref: args.chatHistoryStartRef ?? getChatRequestHistoryStartRef(userMessages as import('./api.ts').ChatMessage[], args.sessionId, false),
    } : {}),
    messages: [...prependedSystem, ...userMessages],
    custom_instructions: args.customInstructions ?? null,
  };
}

/**
 * POSTs to the manual /v1/compact/run endpoint and streams compact_* SSE
 * events back. Each parsed JSON event (one per `data: ` line) is delivered
 * to `onEvent`. The promise resolves when the server emits `data: [DONE]`
 * (or the stream ends).
 *
 * IMPORTANT: `url` must be an ABSOLUTE URL. The frontend dev server runs
 * on a different origin (port 3000) from the backend (port 8090), so a
 * relative URL like '/v1/compact/run' would hit the dev server and 404.
 * Use `${llmClient.baseUrl}/v1/compact/run`.
 *
 * Critically, this actually reads `response.body` — the previous
 * `fetch(...).catch(() => {})` swallowed the response and the projection
 * never ran, which is why clicking /compact or pressing Enter on a fully
 * typed `/compact` looked like a no-op.
 */
export async function runCompactSSE(
  opts: RunCompactSSEOptions
): Promise<void> {
  const res = await fetch(opts.url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(opts.body),
    signal: opts.signal,
  });
  if (!res.ok) {
    throw new Error(`compact SSE failed: status=${res.status}`);
  }
  if (!res.body) {
    throw new Error("compact SSE: empty response body");
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  // Sentinel avoids splitting the trailing buffer prematurely when the
  // final block lacks a trailing blank line.
  buffer += "\n\n";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        for (const line of block.split("\n")) {
          const trimmed = line.trim();
          if (!trimmed.startsWith("data:")) continue;
          const payload = trimmed.slice(5).trim();
          if (payload === "" || payload === "[DONE]") continue;
          try {
            opts.onEvent(JSON.parse(payload));
          } catch {
            // ignore malformed lines
          }
        }
      }
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // already released
    }
  }
}
