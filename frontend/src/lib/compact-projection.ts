/**
 * Pure side-effect helper that applies a compact_* projection to the
 * messages state. Shared by both the page-level timeline event handler
 * (auto-compact path) and the manual /v1/compact/run SSE consumer
 * (slash command / click path).
 *
 * The projection comes from `projectTimelineEvent()` and the patch is
 * kept side-effect-free w.r.t. the React tree: it only consumes the
 * provided `setMessages` and the rollback ref.
 *
 * `options.insertBeforeMessageId` is the AGENT-MODE auto-compact
 * hint. The agent-mode timeline handler passes the streaming
 * assistant's id (created at `handleSend` flush time) so that the
 * compact_summary bubble is inserted BEFORE the assistant, yielding
 * the natural UI order
 *
 *   [..., user, compact_summary, streaming_assistant]
 *
 * instead of the legacy
 *
 *   [..., user, streaming_assistant(faded), compact_summary]
 *
 * (the latter confused users because the response bubble was
 * already visible before the compact card, even though the backend
 * emits compact events first). Only messages strictly before the
 * insert point are marked `isPreCompact`; the current user and the
 * streaming assistant stay unmarked because they belong to the
 * post-boundary context.
 *
 * The manual `/compact` slash-command path omits this hint
 * (preserves the legacy "mark all, append at end" semantics),
 * because the slash command is a standalone user action and has no
 * streaming assistant placeholder to anchor against.
 */
import type { MutableRefObject } from "react";
import type { Message } from "./message-types";

export interface ApplyCompactProjectionOptions {
  /**
   * When set and the message with this id exists in the current
   * messages list, the new compact_summary is inserted at that
   * index (i.e. immediately before that message) and only messages
   * strictly before that index are marked `isPreCompact`.
   *
   * If the hint is missing, empty, or refers to a non-existent
   * message id, the projection falls back to the legacy behavior:
   * mark all non-compact_summary messages `isPreCompact` and append
   * the compact_summary at the end.
   */
  insertBeforeMessageId?: string;
}

export type CompactProjection =
  | {
      kind: "compact_started";
      summaryMessageId: string;
      trigger?: string;
      mode?: string;
      originalTokens?: number;
      threshold?: number;
      strategy?: string;
    }
  | {
      kind: "compact_delta";
      summaryMessageId: string;
      text: string;
      partial: boolean;
    }
  | {
      kind: "compact_completed";
      summaryMessageId: string;
      text: string;
      metadata?: Record<string, unknown>;
    }
  | {
      kind: "compact_failed";
      summaryMessageId: string;
      reason: string;
      error?: string;
    };

export function applyCompactProjection(
  projection: CompactProjection,
  setMessages: React.Dispatch<React.SetStateAction<Message[]>>,
  newlyCompactMarkedRef: MutableRefObject<Set<string>>,
  options?: ApplyCompactProjectionOptions
): void {
  if (projection.kind === "compact_started") {
    const sid = projection.summaryMessageId;
    setMessages((prev) => {
      const markedIds = new Set<string>();
      // Locate the anchor message (if any) for the agent-mode
      // auto-compact path. When `insertBeforeMessageId` is provided
      // and points to a real message in the current list, we will:
      //   1. mark ONLY the messages strictly before that index as
      //      isPreCompact (so the current user and the streaming
      //      assistant stay at full opacity — they belong to the
      //      post-boundary context);
      //   2. insert the compact_summary at that index.
      // Otherwise we fall back to the legacy "mark all, append at
      // end" path (manual /compact and stale-hint cases).
      const hint = options?.insertBeforeMessageId;
      const anchorIndex =
        hint && hint.length > 0
          ? prev.findIndex((m) => m.id === hint)
          : -1;
      const useBoundary = anchorIndex >= 0;

      // Build the new compact_summary once so the two branches
      // share the same payload shape (AGENTS.md trap #14: keep
      // message construction deterministic).
      const compactSummary: Message = {
        id: sid,
        role: "system",
        content: "",
        timestamp: new Date(),
        bubbleKind: "compact_summary",
        compactionBoundaryId: sid,
        compactStatus: "streaming",
        compactMetadata: {
          trigger: projection.trigger,
          mode: projection.mode,
          originalTokens: projection.originalTokens,
          threshold: projection.threshold,
          strategy: projection.strategy,
        },
      } as Message;

      // Build the marked pre-boundary array. Existing
      // compact_summary bubbles are NEVER marked (they are
      // historical boundaries, not the messages being summarized
      // right now). With a valid anchor, the anchor message and
      // anything after it stay unmarked.
      const marked = prev.map((m, idx) => {
        if (m.bubbleKind === "compact_summary") return m;
        if (useBoundary && idx >= anchorIndex) return m;
        if (m.isPreCompact !== true) markedIds.add(m.id);
        return { ...m, isPreCompact: true };
      });

      newlyCompactMarkedRef.current = markedIds;

      if (useBoundary) {
        // Insert the compact_summary at the anchor index (i.e.
        // immediately before the streaming assistant). The slice
        // form is intentional: `marked.slice(0, anchorIndex)` is
        // the pre-boundary tail, `marked.slice(anchorIndex)` is
        // the post-boundary head (anchor + everything after).
        return [
          ...marked.slice(0, anchorIndex),
          compactSummary,
          ...marked.slice(anchorIndex),
        ];
      }

      // Legacy fallback: append at the end.
      return [...marked, compactSummary];
    });
    return;
  }
  if (projection.kind === "compact_delta") {
    setMessages((prev) =>
      prev.map((m) =>
        m.compactionBoundaryId === projection.summaryMessageId
          ? {
              ...m,
              content:
                (typeof m.content === "string" ? m.content : "") +
                projection.text,
            }
          : m
      )
    );
    return;
  }
  if (projection.kind === "compact_completed") {
    setMessages((prev) =>
      prev.map((m) =>
        m.compactionBoundaryId === projection.summaryMessageId
          ? {
              ...m,
              content: projection.text,
              compactStatus: "completed",
              compactMetadata: {
                ...m.compactMetadata,
                summaryTokens: (projection.metadata as { summary_tokens?: number })
                  ?.summary_tokens,
              },
            }
          : m
      )
    );
    return;
  }
  if (projection.kind === "compact_failed") {
    const failedId = projection.summaryMessageId;
    const rollbackIds = newlyCompactMarkedRef.current;
    setMessages((prev) =>
      prev
        .filter((m) => m.compactionBoundaryId !== failedId)
        .map((m) =>
          rollbackIds.has(m.id)
            ? { ...m, isPreCompact: false }
            : m
        )
    );
    return;
  }
}
