'use client';

import CollapsibleBubbleContent from './CollapsibleBubbleContent';

type AssistantReasoningSectionProps = {
  messageId: string;
  reasoning: string;
  collapsed?: boolean;
  defaultExpanded: boolean;
  isStreaming: boolean;
  onToggle: () => void;
  bubbleCharacterLimit?: number;
};

export default function AssistantReasoningSection({
  messageId,
  reasoning,
  collapsed: collapsedProp,
  defaultExpanded,
  isStreaming,
  onToggle,
  bubbleCharacterLimit = 20_000,
}: AssistantReasoningSectionProps) {
  if (!reasoning) {
    return null;
  }

  const panelId = `${messageId}-reasoning`;
  const collapsed = collapsedProp ?? !defaultExpanded;

  return (
    <div className="mb-3 rounded-xl border border-gray-200 bg-gray-50/80 dark:border-gray-700 dark:bg-gray-900/40">
      <button
        type="button"
        aria-expanded={!collapsed}
        aria-controls={panelId}
        onClick={onToggle}
        className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm text-gray-600 dark:text-gray-300"
      >
        <span>{collapsed ? '展开推理过程' : '收起推理过程'}</span>
        <span className="text-xs text-gray-400">{isStreaming ? '流式更新中' : '已完成'}</span>
      </button>
      {!collapsed && (
        <div id={panelId} className="border-t border-gray-200 px-3 py-2 dark:border-gray-700">
          <CollapsibleBubbleContent content={reasoning} limit={bubbleCharacterLimit} streaming={isStreaming}>
            {(displayContent) => (
              <div className="whitespace-pre-wrap text-sm italic text-gray-500 dark:text-gray-400">
                {displayContent}
              </div>
            )}
          </CollapsibleBubbleContent>
        </div>
      )}
    </div>
  );
}
