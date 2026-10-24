import type { AgentStep, TimelineEvent } from './api.ts';
import { createClientStepId } from './api.ts';
import { applyCompactProjection } from './compact-projection.ts';
import {
  appendProjectedChatChunk,
  collectTrackedAssistantMessageIds,
  completeRunningDispatchStep,
  finalizeRunningDispatchStep,
  finalizeTrackedAssistantMessage,
  finalizeTrackedAssistantMessages,
  getActiveTrackedAssistantMessageId,
  getContentTargetAssistantMessageId,
  mergeDispatchStep,
  retractTrackedAssistantTailText,
  stripTrailingCanonicalSwitchAgentControl,
  updateTrackedAssistantMessage,
} from './dispatch-message.ts';
import {
  mergeManagerChildAgentUpdateIntoMessages,
  type AgentRunState,
  type Message,
} from './message-types.ts';
import { projectTimelineEvent } from './timeline-projection.ts';
import { applyRetrievedLocalMemories } from './retrieved-local-memory.ts';
import type { SessionAttachmentToken } from './session-hub-client.ts';
import type { RequestAcceptedEvent } from './vision-automation/send-contract.ts';
import { stageChatHistoryImageOmission } from './vision-automation/history-omission.ts';
import { resolveCanonicalVisionAutomationImageBlock } from './vision-automation/page-integration.ts';

export type TextAgentSessionRunRef = { current: AgentRunState | null };
export type TextMessageSetter = (update: (previous: Message[]) => Message[]) => void;

export interface TextAgentSessionHandlerEnvironment {
  activeRunRef: TextAgentSessionRunRef;
  setMessages: TextMessageSetter;
  setAgentSessionId: (sessionId: string | undefined) => void;
  onAgentToolConfigurationAccepted?: (event: RequestAcceptedEvent) => void;
  /** Shared live/recovery busy projection; pages keep their refs in sync. */
  setAgentBusy?: (busy: boolean) => void;
  resetStreamingState: () => void;
  clearStoppedState: () => void;
  clearAnswerNowFlag: () => void;
  clearStopFlag: () => void;
  getTerminalBehavior: (run: AgentRunState) => {
    suppressErrorMessage: boolean;
    resetStreamingState: boolean;
    resetStoppedState: boolean;
    clearAnswerNowFlag: boolean;
  };
  newlyCompactMarkedRef: { current: Set<string> };
  defaultReasoningCollapsed: boolean;
  now: () => Date;
  createId: (prefix: string) => string;
}

/**
 * A non-2xx Answer Now response is known before its replacement generation
 * exists.  Finish only that local replacement UI and render its exact error;
 * session ownership deliberately remains with the original Hub run.
 */
export function settleKnownAnswerNowRejection(input: {
  isCurrentTransport?: boolean;
  transportToken?: SessionAttachmentToken;
  isCurrentToken?: (token: SessionAttachmentToken) => boolean;
  run: AgentRunState | null;
  handlers: { onPayloadError(error: Error): void };
  rejection: Error;
  clearAnswerNowFlag(): void;
  setAnswerNowLoading(value: boolean): void;
  onVisibleError?(message: string): void;
}): boolean {
  if (input.transportToken
    ? !(input.isCurrentToken?.(input.transportToken) ?? false)
    : input.isCurrentTransport !== true) return false;
  if (input.run) input.run.answerNowWasClicked = false;
  input.clearAnswerNowFlag();
  input.handlers.onPayloadError(input.rejection);
  input.setAnswerNowLoading(false);
  input.onVisibleError?.(input.rejection.message);
  return true;
}

type TurnStartedTimelineEvent = TimelineEvent & {
  session_id?: string;
  turn_index?: number;
  user_content?: string;
  images?: string[];
  historical_image_omission?: boolean;
  vision_automation_manual_image_count?: number;
  historical_image_placeholder?: string;
};

function recoveredUserContent(event: TurnStartedTimelineEvent): Message['content'] {
  const images = event.images ?? [];
  if (images.length === 0) {
    if (event.historical_image_placeholder === undefined) return event.user_content ?? '';
    return [
      { type: 'text', text: event.user_content ?? '' },
      { type: 'text', text: event.historical_image_placeholder },
    ];
  }
  return [
    ...images.map((url) => ({ type: 'image_url', image_url: { url } })),
    { type: 'text', text: event.user_content ?? '' },
  ];
}

function recoveredDisplayImages(event: TurnStartedTimelineEvent): string[] | undefined {
  const images = event.images ?? [];
  return images.length > 0 ? images : undefined;
}

function omitRecoveredVisionHistory(
  messages: Message[],
  event: TurnStartedTimelineEvent,
  currentUserId: string,
): Message[] {
  if (event.historical_image_omission !== true) return messages;
  const historical = messages.filter((message) => message.id !== currentUserId);
  const staged = stageChatHistoryImageOmission(
    historical,
    resolveCanonicalVisionAutomationImageBlock,
  );
  if (staged.omittedBlockCount === 0) return messages;
  const omittedById = new Map(staged.commit(historical).map((message) => [message.id, message]));
  return messages.map((message) => omittedById.get(message.id) ?? message);
}

function currentRun(runRef: TextAgentSessionRunRef): AgentRunState | null {
  return runRef.current;
}

function activeMessageId(run: AgentRunState): string | null {
  return getActiveTrackedAssistantMessageId(
    run.assistantMessageId,
    run.projectedAssistantMessageIds,
    run.executionAssistantMessageId,
  ) ?? null;
}

function trackedIds(run: AgentRunState): string[] {
  return collectTrackedAssistantMessageIds(
    run.assistantMessageId,
    run.projectedAssistantMessageIds,
    run.executionAssistantMessageId,
  );
}

function replaceTrackedRunId(run: AgentRunState, previousId: string, canonicalId: string): void {
  if (previousId === canonicalId) return;
  if (run.assistantMessageId === previousId) run.assistantMessageId = canonicalId;
  if (run.coordinatorMessageId === previousId) run.coordinatorMessageId = canonicalId;
  if (run.executionAssistantMessageId === previousId) run.executionAssistantMessageId = canonicalId;
  run.projectedAssistantMessageIds = new Set(Array.from(run.projectedAssistantMessageIds, (id) => (
    id === previousId ? canonicalId : id
  )));
}

function reconcileOptimisticTurn(
  environment: TextAgentSessionHandlerEnvironment,
  run: AgentRunState,
  event: TurnStartedTimelineEvent,
): AgentRunState {
  const sessionId = event.session_id ?? 'session';
  const turnIndex = event.turn_index ?? 0;
  const baseId = `${sessionId}-turn-${turnIndex}`;
  const userId = `${baseId}-user`;
  const assistantId = `${baseId}-assistant`;
  const previousAssistantId = run.assistantMessageId;
  replaceTrackedRunId(run, previousAssistantId, assistantId);
  if (environment.activeRunRef.current && environment.activeRunRef.current !== run) {
    replaceTrackedRunId(environment.activeRunRef.current, previousAssistantId, assistantId);
  }

  environment.setAgentSessionId(event.session_id);
  environment.setMessages((previous) => {
    const previousAssistantIndex = previous.findIndex((message) => message.id === previousAssistantId);
    const canonicalAssistantIndex = previous.findIndex((message) => message.id === assistantId);
    const assistantIndex = previousAssistantIndex >= 0 ? previousAssistantIndex : canonicalAssistantIndex;
    const optimisticUserId = assistantIndex > 0 && previous[assistantIndex - 1].role === 'user'
      ? previous[assistantIndex - 1].id
      : null;
    let sawCanonicalUser = false;
    let sawCanonicalAssistant = false;
    const reconciled = previous.flatMap((message) => {
      if (message.id === userId || (optimisticUserId && message.id === optimisticUserId)) {
        if (sawCanonicalUser) return [];
        sawCanonicalUser = true;
        return [{
          ...message,
          id: userId,
          content: recoveredUserContent(event),
          images: recoveredDisplayImages(event),
        }];
      }
      if (message.id === assistantId || message.id === previousAssistantId) {
        if (sawCanonicalAssistant) return [];
        sawCanonicalAssistant = true;
        return [{ ...message, id: assistantId }];
      }
      return [message];
    });
    const withUser = sawCanonicalUser
      ? reconciled
      : [...reconciled, {
          id: userId,
          role: 'user' as const,
          content: recoveredUserContent(event),
          images: recoveredDisplayImages(event),
          timestamp: environment.now(),
        }];
    const withAssistant = sawCanonicalAssistant
      ? withUser
      : [...withUser, {
          id: assistantId,
          role: 'assistant' as const,
          content: '',
          timestamp: environment.now(),
          isStreaming: true,
          agentSteps: [],
        }];
    return omitRecoveredVisionHistory(withAssistant, event, userId);
  });
  return run;
}

function createRecoveredTurn(
  environment: TextAgentSessionHandlerEnvironment,
  runRef: TextAgentSessionRunRef,
  event: TurnStartedTimelineEvent,
): AgentRunState {
  const sessionId = event.session_id ?? 'session';
  const turnIndex = event.turn_index ?? 0;
  const baseId = `${sessionId}-turn-${turnIndex}`;
  const userId = `${baseId}-user`;
  const assistantId = `${baseId}-assistant`;
  const run: AgentRunState = {
    assistantMessageId: assistantId,
    projectedAssistantMessageIds: new Set<string>(),
    stopWasClicked: false,
    answerNowWasClicked: false,
  };
  runRef.current = run;
  environment.activeRunRef.current = run;
  environment.setAgentSessionId(event.session_id);
  environment.setMessages((previous) => {
    const retired = previous.map((message) => message.role === 'assistant'
      ? { ...message, isStreaming: false }
      : message);
    const withUser = retired.some((message) => message.id === userId)
      ? retired
      : [...retired, {
          id: userId,
          role: 'user' as const,
          content: recoveredUserContent(event),
          images: recoveredDisplayImages(event),
          timestamp: environment.now(),
        }];
    const withAssistant = withUser.some((message) => message.id === assistantId)
      ? withUser
      : [...withUser, {
          id: assistantId,
          role: 'assistant' as const,
          content: '',
          timestamp: environment.now(),
          isStreaming: true,
          agentSteps: [],
        }];
    return omitRecoveredVisionHistory(withAssistant, event, userId);
  });
  return run;
}

function mergeCanonicalDispatchStep(previousStep: AgentStep, nextStep: AgentStep): AgentStep {
  const merged = mergeDispatchStep(previousStep, nextStep);
  return !previousStep.dispatch_id && nextStep.dispatch_id
    ? { ...merged, id: nextStep.id, dispatch_id: nextStep.dispatch_id }
    : merged;
}

function addStep(
  environment: TextAgentSessionHandlerEnvironment,
  run: AgentRunState,
  step: AgentStep,
): void {
  if (step.step_type === 'dispatch' && step.dispatch_kind === 'routing' && step.dispatch_status === 'running') {
    return;
  }
  if (step.step_type === 'dispatch' && step.dispatch_kind === 'secondary_handoff') {
    const previousExecutionId = run.executionAssistantMessageId;
    const newExecutionId = environment.createId('execution');
    run.executionAssistantMessageId = newExecutionId;
    run.secondaryHandoffExecutionBubblePendingAgentSwitch = true;
    environment.setMessages((previous) => {
      const retracted = retractTrackedAssistantTailText(
        previous,
        run.coordinatorMessageId ?? run.assistantMessageId,
        stripTrailingCanonicalSwitchAgentControl,
      );
      const retired = previousExecutionId ? finalizeTrackedAssistantMessage(retracted, previousExecutionId) : retracted;
      return [...retired, {
        id: newExecutionId,
        role: 'assistant',
        content: '',
        timestamp: environment.now(),
        isStreaming: true,
        agentSteps: [],
        bubbleKind: 'execution',
      }];
    });
  }
  environment.setMessages((previous) => updateTrackedAssistantMessage(previous, activeMessageId(run), (message) => {
    const steps = message.agentSteps ?? [];
    const last = steps.length ? steps[steps.length - 1] : null;
    if (step.step_type === 'orchestration' && step.orchestration_type === 'update' && step.orchestration_update) {
      for (let index = steps.length - 1; index >= 0; index -= 1) {
        if (steps[index].step_type === 'orchestration' && steps[index].orchestration_type === 'plan' && steps[index].stages) {
          return {
            ...message,
            agentSteps: [...steps.slice(0, index), {
              ...steps[index],
              stages: steps[index].stages!.map((stage) => ({
                ...stage,
                tools: stage.tools.map((tool) => tool.call_id === step.orchestration_update!.call_id
                  ? { ...tool, ...step.orchestration_update!, status: step.orchestration_update!.status }
                  : tool),
              })),
            }, ...steps.slice(index + 1)],
          };
        }
      }
      return message;
    }
    if (step.step_type === 'orchestration' && step.orchestration_type === 'done' && step.orchestration_summary) {
      for (let index = steps.length - 1; index >= 0; index -= 1) {
        if (steps[index].step_type === 'orchestration' && steps[index].orchestration_type === 'plan') {
          return { ...message, agentSteps: [...steps.slice(0, index), { ...steps[index], orchestration_summary: step.orchestration_summary }, ...steps.slice(index + 1)] };
        }
      }
      return message;
    }
    const normalized = last?.step_type === 'dispatch' && last.dispatch_status === 'running'
      ? [...steps.slice(0, -1), { ...last, dispatch_status: 'completed' as const }]
      : steps;
    const normalizedLast = normalized.length ? normalized[normalized.length - 1] : null;
    if (step.step_type === 'thought' && step.raw_tool_call && !step.content && normalizedLast?.step_type === 'thought') {
      return { ...message, agentSteps: [...normalized.slice(0, -1), { ...normalizedLast, raw_tool_call: step.raw_tool_call, raw_tool_call_source: step.raw_tool_call_source, partial: step.partial ?? normalizedLast.partial }] };
    }
    if (step.step_type === 'thought' && normalizedLast?.step_type === 'thought' && normalizedLast.partial) {
      return { ...message, agentSteps: [...normalized.slice(0, -1), { ...normalizedLast, content: `${normalizedLast.content ?? ''}${step.content ?? ''}`, raw_tool_call: step.raw_tool_call ?? normalizedLast.raw_tool_call, raw_tool_call_source: step.raw_tool_call ? step.raw_tool_call_source : normalizedLast.raw_tool_call_source, partial: step.partial ?? false }] };
    }
    if (step.step_type === 'dispatch') {
      const index = normalized.findLastIndex((candidate) => candidate.step_type === 'dispatch'
        && (candidate.dispatch_id ? candidate.dispatch_id === step.dispatch_id : true)
        && (candidate.display_slot ?? 'after_body') === (step.display_slot ?? 'after_body')
        && (candidate.dispatch_kind ?? 'routing') === (step.dispatch_kind ?? 'routing'));
      if (index >= 0) return { ...message, agentSteps: [...normalized.slice(0, index), mergeCanonicalDispatchStep(normalized[index], step), ...normalized.slice(index + 1)] };
    }
    if (step.step_type !== 'thought' && normalizedLast?.step_type === 'thought' && normalizedLast.partial) {
      return { ...message, agentSteps: [...normalized.slice(0, -1), { ...normalizedLast, partial: false }, step] };
    }
    return { ...message, agentSteps: [...normalized, step] };
  }));
}

function finalize(environment: TextAgentSessionHandlerEnvironment, run: AgentRunState): void {
  environment.setMessages((previous) => finalizeTrackedAssistantMessages(
    updateTrackedAssistantMessage(previous, activeMessageId(run), (message) => {
      const steps = message.agentSteps ?? [];
      const completed = completeRunningDispatchStep(steps.length ? steps[steps.length - 1] : null);
      return completed && completed !== steps[steps.length - 1]
        ? { ...message, agentSteps: [...steps.slice(0, -1), completed] }
        : message;
    }),
    trackedIds(run),
  ));
}

export function createTextAgentSessionHandlers(
  environment: TextAgentSessionHandlerEnvironment,
  runRef: TextAgentSessionRunRef,
) {
  let currentTurnIdentity: string | null = null;
  let completedRun: AgentRunState | null = null;
  let continuationPending = false;
  let continuationError: { run: AgentRunState; error: string } | null = null;

  const releaseRun = (run: AgentRunState) => {
    if (runRef.current === run) runRef.current = null;
    currentTurnIdentity = null;
  };

  return {
    onRequestAccepted: (event: RequestAcceptedEvent) => {
      environment.onAgentToolConfigurationAccepted?.(event);
    },
    onStep: (step: AgentStep) => {
      const run = currentRun(runRef);
      if (run) addStep(environment, run, step);
    },
    onContent: (content: string) => {
      const run = currentRun(runRef);
      if (!run) return;
      const target = getContentTargetAssistantMessageId(run.assistantMessageId, run.projectedAssistantMessageIds, run.executionAssistantMessageId);
      if (!target) return;
      environment.setMessages((previous) => updateTrackedAssistantMessage(previous, target, (message) => {
        const steps = message.agentSteps ?? [];
        const completed = completeRunningDispatchStep(steps.length ? steps[steps.length - 1] : null);
        const messageContent = typeof message.content === 'string' ? message.content : '';
        const priorContent = continuationPending && continuationError?.run === run
          ? (() => {
              const suffix = `\n\n[流式中断: ${continuationError.error}]`;
              if (messageContent.endsWith(suffix)) return messageContent.slice(0, -suffix.length);
              return messageContent === `抱歉，Agent 执行出错: ${continuationError.error}` ? '' : messageContent;
            })()
          : messageContent;
        return {
          ...message,
          agentSteps: completed && completed !== steps[steps.length - 1] ? [...steps.slice(0, -1), completed] : message.agentSteps,
          content: `${priorContent}${content}`,
        };
      }));
      if (continuationPending && continuationError?.run === run) continuationError = null;
    },
    onDone: (sessionId?: string) => {
      const run = currentRun(runRef);
      if (!run) return;
      const terminalBehavior = environment.getTerminalBehavior(run);
      if (sessionId) environment.setAgentSessionId(sessionId);
      finalize(environment, run);
      if (environment.activeRunRef.current === run) {
        if (terminalBehavior.resetStreamingState) environment.resetStreamingState();
        if (terminalBehavior.resetStoppedState) environment.clearStoppedState();
        if (terminalBehavior.clearAnswerNowFlag) environment.clearAnswerNowFlag();
        environment.clearStopFlag();
        environment.activeRunRef.current = null;
      }
      completedRun = run;
      environment.setAgentBusy?.(false);
      continuationPending = false;
      continuationError = null;
      releaseRun(run);
    },
    onDetached: () => {
      const run = currentRun(runRef);
      if (!run) return;
      const terminalBehavior = environment.getTerminalBehavior(run);
      finalize(environment, run);
      if (environment.activeRunRef.current === run) {
        if (terminalBehavior.resetStreamingState) environment.resetStreamingState();
        if (terminalBehavior.resetStoppedState) environment.clearStoppedState();
        if (terminalBehavior.clearAnswerNowFlag) environment.clearAnswerNowFlag();
        environment.clearStopFlag();
        environment.activeRunRef.current = null;
      }
      completedRun = null;
      environment.setAgentBusy?.(false);
      continuationPending = false;
      continuationError = null;
      releaseRun(run);
    },
    onPayloadError: (error: Error) => {
      const run = currentRun(runRef);
      if (!run) return;
      const terminalBehavior = environment.getTerminalBehavior(run);
      environment.setMessages((previous) => finalizeTrackedAssistantMessages(updateTrackedAssistantMessage(previous, activeMessageId(run), (message) => {
        const steps = message.agentSteps ?? [];
        const finalized = finalizeRunningDispatchStep(steps.length ? steps[steps.length - 1] : null, {
          stopWasClicked: run.stopWasClicked,
          answerNowWasClicked: run.answerNowWasClicked,
          errorMessage: error.message,
        });
        const nextSteps = finalized && finalized !== steps[steps.length - 1] ? [...steps.slice(0, -1), finalized] : message.agentSteps;
        if (terminalBehavior.suppressErrorMessage) return { ...message, agentSteps: nextSteps };
        const hasContent = typeof message.content === 'string' && message.content.trim().length > 0;
        return { ...message, agentSteps: nextSteps, content: hasContent ? `${message.content}\n\n[流式中断: ${error.message}]` : `抱歉，Agent 执行出错: ${error.message}` };
      }), trackedIds(run)));
      if (environment.activeRunRef.current === run) {
        if (terminalBehavior.resetStreamingState) environment.resetStreamingState();
        if (terminalBehavior.resetStoppedState) environment.clearStoppedState();
        if (terminalBehavior.clearAnswerNowFlag) environment.clearAnswerNowFlag();
        environment.clearStopFlag();
        environment.activeRunRef.current = null;
      }
      completedRun = run;
      environment.setAgentBusy?.(false);
      continuationPending = false;
      continuationError = terminalBehavior.suppressErrorMessage ? null : { run, error: error.message };
      releaseRun(run);
    },
    onSessionStart: (sessionId: string) => {
      const run = currentRun(runRef);
      if (run || environment.activeRunRef.current) {
        environment.setAgentSessionId(sessionId);
        return;
      }
      if (!completedRun) return;
      runRef.current = completedRun;
      environment.activeRunRef.current = completedRun;
      continuationPending = true;
      environment.setAgentBusy?.(true);
      environment.setAgentSessionId(sessionId);
      const target = getContentTargetAssistantMessageId(
        completedRun.assistantMessageId,
        completedRun.projectedAssistantMessageIds,
        completedRun.executionAssistantMessageId,
      );
      environment.setMessages((previous) => updateTrackedAssistantMessage(previous, target, (message) => ({
        ...message,
        isStreaming: true,
      })));
    },
    onTimelineEvent: (timelineEvent: TurnStartedTimelineEvent) => {
      if (timelineEvent.kind === 'turn_started') {
        environment.setAgentBusy?.(true);
        if (continuationPending) {
          const continuationRun = currentRun(runRef);
          if (continuationRun) finalize(environment, continuationRun);
          if (environment.activeRunRef.current === continuationRun) environment.activeRunRef.current = null;
          runRef.current = null;
          completedRun = null;
          continuationPending = false;
          continuationError = null;
          currentTurnIdentity = null;
        }
        const identity = `${timelineEvent.session_id ?? 'session'}:${timelineEvent.turn_index ?? 0}`;
        const existing = currentRun(runRef);
        if (existing && (currentTurnIdentity === null || currentTurnIdentity === identity)) {
          reconcileOptimisticTurn(environment, existing, timelineEvent);
        } else {
          if (existing) finalize(environment, existing);
          createRecoveredTurn(environment, runRef, timelineEvent);
        }
        currentTurnIdentity = identity;
        return;
      }
      const projection = projectTimelineEvent(timelineEvent);
      if (projection.kind === 'local_memory_retrieved') {
        environment.setMessages((previous) => applyRetrievedLocalMemories(previous, projection));
        return;
      }
      const run = currentRun(runRef);
      if (!run) return;
      if (timelineEvent.kind === 'agent_switched' && timelineEvent.source === 'coordinator_2') {
        if (run.secondaryHandoffExecutionBubblePendingAgentSwitch) {
          run.secondaryHandoffExecutionBubblePendingAgentSwitch = false;
          environment.setMessages((previous) => retractTrackedAssistantTailText(previous, run.coordinatorMessageId ?? run.assistantMessageId, stripTrailingCanonicalSwitchAgentControl));
        } else {
          const previousExecutionId = run.executionAssistantMessageId;
          const newExecutionId = environment.createId('execution');
          run.executionAssistantMessageId = newExecutionId;
          environment.setMessages((previous) => {
            const retracted = retractTrackedAssistantTailText(previous, run.coordinatorMessageId ?? run.assistantMessageId, stripTrailingCanonicalSwitchAgentControl);
            const retired = previousExecutionId ? finalizeTrackedAssistantMessage(retracted, previousExecutionId) : retracted;
            return [...retired, { id: newExecutionId, role: 'assistant', content: '', timestamp: environment.now(), isStreaming: true, agentSteps: [], bubbleKind: 'execution' }];
          });
        }
      }
      if (projection.kind === 'noop') return;
      if (projection.kind === 'compact_started' || projection.kind === 'compact_delta' || projection.kind === 'compact_completed' || projection.kind === 'compact_failed') {
        applyCompactProjection(projection, environment.setMessages as never, environment.newlyCompactMarkedRef as never, { insertBeforeMessageId: run.assistantMessageId });
        if (projection.kind === 'compact_started') {
          environment.setMessages((previous) => previous.map((message) => (
            message.id === projection.summaryMessageId
              ? { ...message, timestamp: environment.now() }
              : message
          )));
        }
        return;
      }
      environment.setMessages((previous) => {
        if (projection.kind === 'chat_chunk') {
          if (projection.messageId && projection.messageId !== run.assistantMessageId) {
            run.projectedAssistantMessageIds.add(projection.messageId);
            if (timelineEvent.source === 'coordinator_2') run.coordinatorMessageId = projection.messageId;
          }
          return appendProjectedChatChunk(previous, projection, environment.now, (message) => {
            if (message.id !== run.assistantMessageId) return message;
            const steps = message.agentSteps ?? [];
            const completed = completeRunningDispatchStep(steps.length ? steps[steps.length - 1] : null);
            return completed && completed !== steps[steps.length - 1] ? { ...message, agentSteps: [...steps.slice(0, -1), completed] } : message;
          }, { defaultReasoningCollapsed: environment.defaultReasoningCollapsed });
        }
        if (projection.kind === 'manager_child_agent_update') {
          return mergeManagerChildAgentUpdateIntoMessages(previous, activeMessageId(run), projection);
        }
        if (projection.kind !== 'dispatch_update') return previous;
        const dispatchStep: AgentStep = {
          id: projection.dispatchId || projection.messageId || createClientStepId('dispatch'),
          step_type: 'dispatch', dispatch_id: projection.dispatchId, dispatch_kind: projection.dispatchKind,
          display_slot: projection.displaySlot, dispatch_status: 'running',
          dispatch_stage: projection.dispatchKind === 'secondary_handoff' ? 'handoff' : 'routing',
          dispatch_message: projection.text, partial: projection.partial,
        };
        return updateTrackedAssistantMessage(previous, activeMessageId(run), (message) => {
          const steps = message.agentSteps ?? [];
          const index = steps.findLastIndex((candidate) => candidate.step_type === 'dispatch'
            && (candidate.dispatch_id ? candidate.dispatch_id === dispatchStep.dispatch_id : true)
            && (candidate.display_slot ?? 'after_body') === (dispatchStep.display_slot ?? 'after_body')
            && (candidate.dispatch_kind ?? 'routing') === (dispatchStep.dispatch_kind ?? 'routing'));
          return index >= 0
            ? { ...message, agentSteps: [...steps.slice(0, index), mergeCanonicalDispatchStep(steps[index], dispatchStep), ...steps.slice(index + 1)] }
            : { ...message, agentSteps: [...steps, dispatchStep] };
        });
      });
    },
  };
}
