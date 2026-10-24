"use client";

import { useState, type ReactNode } from "react";

export type CollapsibleBubbleContentLabels = {
  collapsedNotice?: string;
  expandAll?: string;
  collapse?: string;
};

type CollapsibleBubbleContentProps = {
  content: string;
  limit: number;
  children: (displayContent: string) => ReactNode;
  labels?: CollapsibleBubbleContentLabels;
  streaming?: boolean;
};

const DEFAULT_LABELS: Required<CollapsibleBubbleContentLabels> = {
  collapsedNotice: "Content collapsed",
  expandAll: "Expand all",
  collapse: "Collapse",
};

function getDisplayLimit(limit: number): number {
  return Number.isInteger(limit) && limit > 0 ? limit : 20_000;
}

export default function CollapsibleBubbleContent({
  content,
  limit,
  children,
  labels,
  streaming = false,
}: CollapsibleBubbleContentProps) {
  const [expanded, setExpanded] = useState(false);
  const codePoints = Array.from(content);
  const isCollapsed = !streaming && !expanded && codePoints.length > getDisplayLimit(limit);
  const displayContent = isCollapsed
    ? codePoints.slice(0, getDisplayLimit(limit)).join("")
    : content;
  const copy = { ...DEFAULT_LABELS, ...labels };

  return (
    <>
      {children(displayContent)}
      {!streaming && codePoints.length > getDisplayLimit(limit) && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-gray-500 dark:text-gray-400" role="status">
          {isCollapsed && <span>{copy.collapsedNotice}</span>}
          <button
            type="button"
            onClick={() => setExpanded((current) => !current)}
            aria-expanded={expanded}
            className="rounded border border-gray-300 px-2 py-1 font-medium text-gray-700 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-800"
          >
            {expanded ? copy.collapse : copy.expandAll}
          </button>
        </div>
      )}
    </>
  );
}
