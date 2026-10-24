"use client";

import React from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import type { Message } from "../lib/message-types";
import CollapsibleBubbleContent from "./CollapsibleBubbleContent";

type Props = {
  message: Message;
  collapsed?: boolean;
  onToggleCollapse?: () => void;
  onAbort?: (summaryId: string) => void;
  bubbleCharacterLimit?: number;
};

function formatTokens(n: number | undefined): string {
  if (n == null) return "—";
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

const markdownComponents: Components = {
  a({ children, ...props }) {
    return (
      <a {...props} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    );
  },
};

export function CompactSummaryPanel({
  message,
  collapsed,
  onToggleCollapse,
  onAbort,
  bubbleCharacterLimit = 20_000,
}: Props) {
  const meta = message.compactMetadata;
  const status = message.compactStatus;
  const isStreaming = status === "streaming";
  const id = message.compactionBoundaryId || message.id;
  const content = typeof message.content === "string" ? message.content : "";

  return (
    <div
      data-testid="compact-summary-panel"
      className="w-full my-4 border border-amber-200 dark:border-amber-700/40 rounded-xl bg-amber-50/50 dark:bg-amber-900/10 overflow-hidden"
    >
      <div className="flex items-center justify-between px-4 py-2 bg-amber-100/60 dark:bg-amber-900/20 text-xs">
        <div className="flex items-center space-x-2">
          {isStreaming ? (
            <span
              data-testid="compact-spinner"
              className="inline-block w-3 h-3 border-2 border-amber-600 border-t-transparent rounded-full animate-spin"
            />
          ) : status === "completed" ? (
            <span className="text-amber-700">✓</span>
          ) : status === "failed" ? (
            <span className="text-red-600">✗</span>
          ) : null}
          <span className="font-semibold">
            {isStreaming
              ? "Compressing conversation context..."
              : status === "failed"
                ? "Compression failed"
                : "Context compressed"}
          </span>
          {meta?.trigger === "auto" && (
            <span className="text-amber-700 dark:text-amber-300">
              · auto (
              {meta.strategy === "utilization"
                ? "high utilization"
                : "high performance"}
              )
            </span>
          )}
          {meta?.mode && (
            <span className="text-gray-500">· {meta.mode}</span>
          )}
          {meta?.originalTokens != null && meta?.summaryTokens != null && (
            <span className="text-gray-600 dark:text-gray-400">
              · {formatTokens(meta.originalTokens)} →{" "}
              {formatTokens(meta.summaryTokens)} tokens
            </span>
          )}
        </div>
        <div className="flex items-center space-x-2">
          {isStreaming && onAbort && (
            <button
              onClick={() => onAbort(id)}
              className="text-red-600 hover:text-red-700"
            >
              Cancel
            </button>
          )}
          {onToggleCollapse && (
            <button
              onClick={onToggleCollapse}
              className="text-xs text-amber-700 hover:text-amber-800"
            >
              {collapsed ? "Expand" : "Collapse"}
            </button>
          )}
        </div>
      </div>
      {status === "failed" ? (
        <div className="px-4 py-3 text-red-600 dark:text-red-400 text-sm">
          Compression failed: {meta?.failureReason || "unknown reason"}
        </div>
      ) : (
        !collapsed && (
          <div className="px-4 py-3 prose prose-sm dark:prose-invert max-w-none">
            <CollapsibleBubbleContent content={content || (isStreaming ? "..." : "")} limit={bubbleCharacterLimit}>
              {(displayContent) => (
                <ReactMarkdown components={markdownComponents}>
                  {displayContent}
                </ReactMarkdown>
              )}
            </CollapsibleBubbleContent>
            {isStreaming && (
              <span className="animate-pulse">▌</span>
            )}
          </div>
        )
      )}
    </div>
  );
}
