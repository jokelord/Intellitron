"use client";

import React from 'react';
import type { AgentStep } from '@/lib/api';
import { getManagerDispatchesAfterOrchestrationStep, type ChatSettings, type Message } from '@/lib/message-types';
import type { WebUIConfig } from '@/lib/web-ui-config';
import { getAssistantRenderableText } from '@/lib/answer-now-ui';
import { getChatMessageBubbleWidthClass } from '@/lib/chat-message-layout';
import { CompactSummaryPanel } from '@/components/CompactSummaryPanel';
import ChatAvatar from '@/components/ChatAvatar';
import HistoricalImageSummaryCard from '@/components/HistoricalImageSummaryCard';
import ManagerParallelDispatchCard from '@/components/ManagerParallelDispatchCard';
import AgentStepDisplay from '@/components/AgentStepDisplay';
import AssistantReasoningSection from '@/components/AssistantReasoningSection';
import CollapsibleBubbleContent from '@/components/CollapsibleBubbleContent';
import MarkdownRenderer from '@/components/MarkdownRenderer';
import AssistantBubbleActions from '@/components/AssistantBubbleActions';
import { VoiceReplayPlayer } from '@/components/VoiceReplayPlayer';
import RetrievedLocalMemories from '@/components/RetrievedLocalMemories';

export interface ConversationMessageListProps {
  messages: readonly Message[];
  contentRef?: React.Ref<HTMLElement>;
  webUIConfig?: WebUIConfig;
  settings: Pick<ChatSettings, 'agentMode' | 'showReasoning'>;
  bubbleCharacterLimit: number;
  foldThinkingAfterFinalAnswer: boolean;
  stoppedMessageIds: ReadonlySet<string>;
  collapsedCompact: ReadonlySet<string>;
  onToggleCompact(id: string): void;
  onToggleReasoning(id: string): void;
  onOpenImage(src: string): void;
  getUserText(message: Message): string;
  emptyContent: React.ReactNode;
}

export function ConversationMessageList({
  messages, contentRef, webUIConfig, settings, bubbleCharacterLimit,
  foldThinkingAfterFinalAnswer, stoppedMessageIds, collapsedCompact,
  onToggleCompact, onToggleReasoning, onOpenImage, getUserText, emptyContent,
}: ConversationMessageListProps) {
  return (
    <main ref={contentRef} className="flex-1 px-4 py-6">
      <div className="max-w-4xl mx-auto space-y-6">
        {messages.length === 0 ? emptyContent : (
          messages.map((message) => {
            if (message.bubbleKind === 'compact_summary') {
              return (
                <CompactSummaryPanel
                  key={message.id}
                  message={message}
                  bubbleCharacterLimit={bubbleCharacterLimit}
                  collapsed={collapsedCompact.has(message.id)}
                  onToggleCollapse={() => onToggleCompact(message.id)}
                />
              );
            }
            return (
            <div
              key={message.id}
              className={`flex items-start space-x-3 ${message.role === 'user' ? 'flex-row-reverse space-x-reverse' : ''}`}
            >
              {/* 头像 */}
              <div className="flex-shrink-0">
                <ChatAvatar role={message.role === 'user' ? 'user' : 'assistant'} config={webUIConfig} />
              </div>

              {/* 消息内容 */}
              <div
                className={`flex-1 ${getChatMessageBubbleWidthClass(message.role === 'user' ? 'user' : 'assistant')}`}
              >
                <div
                  className={`px-4 py-3 rounded-2xl ${message.role === 'user'
                    ? 'bg-blue-500 text-white'
                    : 'bg-white dark:bg-gray-800 text-gray-900 dark:text-white border border-gray-200 dark:border-gray-700'
                    }`}
                >
                {/* 用户消息中的图片缩略图 */}
                {message.role === 'user' && message.images && message.images.length > 0 && (
                  <div className="flex gap-2 flex-wrap mb-2">
                    {message.images.map((img, idx) => (
                      <img
                        key={idx}
                        src={img}
                        alt={`图片 ${idx + 1}`}
                        className="w-16 h-16 object-cover rounded cursor-pointer hover:opacity-80 transition-opacity"
                        onClick={() => onOpenImage(img)}
                      />
                    ))}
                  </div>
                )}
                {message.role === 'user' && message.historicalImageSummaries && message.historicalImageSummaries.length > 0 && (
                  <div className="mb-2 space-y-2">
                    {message.historicalImageSummaries.map((summary, index) => (
                      <HistoricalImageSummaryCard
                        key={`${message.id}-historical-${index}`}
                        count={summary.count}
                        sources={summary.sources}
                        relativeRange={summary.relativeRange
                          ? `${summary.relativeRange.startSeconds.toFixed(2)}..${summary.relativeRange.endSeconds.toFixed(2)}s`
                          : null}
                      />
                    ))}
                  </div>
                )}

                {/* 文本内容 */}
                {message.role === 'assistant' ? (
                  <div className="markdown-content">
                    {(() => {
                      const agentSteps = message.agentSteps || [];
                      const isCycleMessage = message.bubbleKind === 'cycle';
                      const afterBodySteps = agentSteps.filter((step) => step.display_slot !== 'before_body');
                      const defaultLeadingSteps = agentSteps.filter((step) => step.step_type !== 'dispatch' || step.display_slot === 'before_body');
                      const defaultTrailingSteps = agentSteps.filter((step) => step.step_type === 'dispatch' && step.display_slot === 'after_body');
                      const orderedLeadingSteps = message.bubbleKind === 'execution'
                        ? afterBodySteps
                        : defaultLeadingSteps;
                      const orderedTrailingSteps = message.bubbleKind === 'execution'
                        ? []
                        : defaultTrailingSteps;
                      const managerDispatchByOrchestrationIndex = new Map<number, ReturnType<typeof getManagerDispatchesAfterOrchestrationStep>>();
                      orderedLeadingSteps.forEach((step, index) => {
                        const entries = getManagerDispatchesAfterOrchestrationStep(step, message.managerDispatches);
                        if (entries.length > 0) {
                          managerDispatchByOrchestrationIndex.set(index, entries);
                        }
                      });
                      const renderManagerDispatchAfterStep = (step: AgentStep, index: number) => {
                        const entries = managerDispatchByOrchestrationIndex.get(index);
                        if (!entries) return null;
                        return (
                          <>
                            {entries.map(([dispatchId, children]) => (
                              <ManagerParallelDispatchCard
                                key={dispatchId}
                                dispatchId={dispatchId}
                                children={children}
                              />
                            ))}
                          </>
                        );
                      };

                      return (
                        <>
                          {orderedLeadingSteps.length > 0 && (
                            <AgentStepDisplay
                              steps={orderedLeadingSteps}
                              renderAfterStep={renderManagerDispatchAfterStep}
                              bubbleCharacterLimit={bubbleCharacterLimit}
                              turnComplete={!message.isStreaming}
                              autoFoldOnTurnComplete={foldThinkingAfterFinalAnswer}
                              defaultCollapsed={isCycleMessage ? false : settings.agentMode && !settings.showReasoning}
                            />
                          )}
                          <AssistantReasoningSection
                            messageId={message.id}
                            reasoning={message.reasoning_content || ''}
                            collapsed={message.reasoningCollapsed}
                            defaultExpanded={isCycleMessage || settings.showReasoning}
                            isStreaming={Boolean(message.isStreaming)}
                            bubbleCharacterLimit={bubbleCharacterLimit}
                            onToggle={() => onToggleReasoning(message.id)}
                          />
                          <CollapsibleBubbleContent
                            content={typeof message.content === 'string'
                              ? getAssistantRenderableText(message, stoppedMessageIds.has(message.id) || message.metadata?.cycle_stopped === true)
                              : (stoppedMessageIds.has(message.id) || message.metadata?.cycle_stopped === true ? '已停止' : '思考中...')}
                            limit={bubbleCharacterLimit}
                            streaming={Boolean(message.isStreaming)}
                          >
                            {(displayContent) => <MarkdownRenderer>{displayContent}</MarkdownRenderer>}
                          </CollapsibleBubbleContent>
                          {orderedTrailingSteps.length > 0 && (
                            <AgentStepDisplay
                              steps={orderedTrailingSteps}
                              bubbleCharacterLimit={bubbleCharacterLimit}
                              turnComplete={!message.isStreaming}
                              autoFoldOnTurnComplete={foldThinkingAfterFinalAnswer}
                              defaultCollapsed={isCycleMessage ? false : settings.agentMode && !settings.showReasoning}
                            />
                          )}
                        </>
                      );
                    })()}
                    {message.isStreaming && (
                      <span className="inline-block w-2 h-4 bg-nvidia-500 animate-pulse ml-1" />
                    )}
                  </div>
                ) : (
                  /* 用户消息：提取文本内容 */
                  <>
                    <CollapsibleBubbleContent
                      content={getUserText(message)}
                      limit={bubbleCharacterLimit}
                    >
                      {(displayContent) => <p className="whitespace-pre-wrap">{displayContent}</p>}
                    </CollapsibleBubbleContent>
                    {message.voiceReplay && <VoiceReplayPlayer replay={message.voiceReplay} />}
                    <RetrievedLocalMemories chunks={message.localMemories} />
                  </>
                )}
                </div>
                {message.role === 'assistant' && (
                  <AssistantBubbleActions
                    body={typeof message.content === 'string' ? message.content : ''}
                    isStreaming={Boolean(message.isStreaming)}
                  />
                )}
              </div>
            </div>
            );
          })
        )}

      </div>
    </main>
  );
}

export default ConversationMessageList;
