import { dispatchAgentSessionPayload, type AgentSessionPayloadDispatchResult } from './api.ts';
import {
  createTextAgentSessionHandlers,
  type TextAgentSessionHandlerEnvironment,
  type TextAgentSessionRunRef,
} from './text-agent-session-handlers.ts';
import type { AgentRunState, Message } from './message-types.ts';
import type { CycleSSEEvent } from './cycle-types.ts';
import { projectTimelineEvent, type TimelineEvent } from './timeline-projection.ts';
import { applyRetrievedLocalMemories } from './retrieved-local-memory.ts';

export interface CycleAgentProjectionOptions {
  setMessages: (update: (previous: Message[]) => Message[]) => void;
  now: () => Date;
  createId: (prefix: string) => string;
}

export interface CycleAgentProjection {
  readonly runRef: TextAgentSessionRunRef;
  readonly handlers: ReturnType<typeof createTextAgentSessionHandlers>;
  beginTurn(assistantMessageId: string): AgentRunState;
  beginMessageTurn(userContent: string, visible: boolean, binding?: CycleSSEEvent): { userMessageId: string; assistantMessageId?: string };
  dispatch(event: CycleSSEEvent, visible: boolean): Promise<AgentSessionPayloadDispatchResult | 'ignored'>;
  finish(sessionId?: string): Promise<void>;
  fail(error: Error): Promise<void>;
  stop(): void;
  clear(options?: { finalize?: boolean }): void;
}

function mergeCycleProjectedBubbles(messages: Message[], run: AgentRunState): Message[] {
  if (run.projectedAssistantMessageIds.size === 0) return messages;
  const primaryIndex = messages.findIndex((message) => message.id === run.assistantMessageId && message.role === 'assistant');
  if (primaryIndex < 0) {
    run.projectedAssistantMessageIds.clear();
    return messages;
  }
  const projectedIds = new Set(run.projectedAssistantMessageIds);
  const projected = messages.filter((message) => projectedIds.has(message.id) && message.role === 'assistant');
  if (projected.length === 0) {
    run.projectedAssistantMessageIds.clear();
    return messages;
  }
  const primary = messages[primaryIndex];
  const merged = projected.reduce((current, message) => ({
    ...current,
    content: `${typeof current.content === 'string' ? current.content : ''}${typeof message.content === 'string' ? message.content : ''}`,
    reasoning_content: `${current.reasoning_content ?? ''}${message.reasoning_content ?? ''}` || undefined,
    agentSteps: [...(current.agentSteps ?? []), ...(message.agentSteps ?? [])],
    isStreaming: current.isStreaming || message.isStreaming,
  }), primary);
  run.projectedAssistantMessageIds.clear();
  return messages
    .filter((message) => !projectedIds.has(message.id))
    .map((message) => message.id === primary.id ? merged : message);
}

export function updateCycleVisibility(current: boolean, event: CycleSSEEvent): boolean {
  return event.type === 'cycle_started' && typeof event.show_agent_steps === 'boolean'
    ? event.show_agent_steps
    : current;
}

/** Cycle envelopes identify a dispatch independently from the inner Agent session. */
function cycleMemoryStepIdentity(event: CycleSSEEvent): string | null {
  const runId = event.cycle_run_id ?? event.session_id;
  if (typeof runId !== 'string' || !runId
    || !Array.isArray(event.path) || !event.path.every((part) => Number.isSafeInteger(part) && part >= 0)
    || (event.track !== 'ordinary' && event.track !== 'scheduled')) return null;
  if (event.track === 'scheduled' && (typeof event.schedule_id !== 'string' || !event.schedule_id
    || !Number.isSafeInteger(event.occurrence_index) || (event.occurrence_index ?? 0) < 1)) return null;
  return JSON.stringify([runId, event.track, event.path, event.schedule_id ?? null, event.occurrence_index ?? null]);
}

export function createCycleAgentProjection(options: CycleAgentProjectionOptions): CycleAgentProjection {
  let currentUserTurn: { userMessageId: string; stepIdentity: string | null; startSequence?: number; sourceSessionId?: string } | null = null;
  const runRef: TextAgentSessionRunRef = { current: null };
  const environment: TextAgentSessionHandlerEnvironment = {
    activeRunRef: runRef,
    setMessages: (update) => options.setMessages((previous) => {
      const next = update(previous);
      return runRef.current ? mergeCycleProjectedBubbles(next, runRef.current) : next;
    }),
    setAgentSessionId: () => undefined,
    setAgentBusy: () => undefined,
    resetStreamingState: () => undefined,
    clearStoppedState: () => undefined,
    clearAnswerNowFlag: () => undefined,
    clearStopFlag: () => undefined,
    getTerminalBehavior: () => ({
      suppressErrorMessage: true,
      resetStreamingState: false,
      resetStoppedState: false,
      clearAnswerNowFlag: false,
    }),
    newlyCompactMarkedRef: { current: new Set<string>() },
    defaultReasoningCollapsed: false,
    now: options.now,
    createId: options.createId,
  };
  const handlers = createTextAgentSessionHandlers(environment, runRef);

  return {
    runRef,
    handlers,
    beginTurn(assistantMessageId) {
      this.clear();
      const run: AgentRunState = {
        assistantMessageId,
        projectedAssistantMessageIds: new Set<string>(),
        stopWasClicked: false,
        answerNowWasClicked: false,
      };
      runRef.current = run;
      return run;
    },
    beginMessageTurn(userContent, visible, binding) {
      this.clear();
      const userMessageId = options.createId('cycle-user');
      const assistantMessageId = options.createId('cycle-assistant');
      options.setMessages((previous) => [...previous,
        {
          id: userMessageId,
          role: 'user' as const,
          content: userContent,
          timestamp: options.now(),
          images: [],
        },
        ...(visible ? [{
          id: assistantMessageId,
          role: 'assistant' as const,
          content: '',
          timestamp: options.now(),
          isStreaming: true,
          agentSteps: [],
          bubbleKind: 'cycle' as const,
        }] : []),
      ]);
      if (visible) this.beginTurn(assistantMessageId);
      currentUserTurn = {
        userMessageId,
        stepIdentity: binding ? cycleMemoryStepIdentity(binding) : null,
        startSequence: binding && Number.isSafeInteger(binding.event_sequence) ? binding.event_sequence : undefined,
      };
      return { userMessageId, ...(visible ? { assistantMessageId } : {}) };
    },
    async dispatch(event, visible) {
      if (event.type === 'timeline_event' && typeof event.event === 'object' && event.event !== null) {
        const projection = projectTimelineEvent(event.event as TimelineEvent);
        if (projection.kind === 'local_memory_retrieved') {
          const target = currentUserTurn;
          if (!target || !target.stepIdentity || target.stepIdentity !== cycleMemoryStepIdentity(event)
            || (target.startSequence !== undefined && (!Number.isSafeInteger(event.event_sequence) || event.event_sequence! <= target.startSequence))
            || (target.sourceSessionId && target.sourceSessionId !== projection.sessionId)) return 'ignored';
          target.sourceSessionId = projection.sessionId;
          options.setMessages((previous) => applyRetrievedLocalMemories(previous, projection, target.userMessageId));
          return 'timeline_event';
        }
      }
      if (!visible || !runRef.current) return 'ignored';
      if (event.type === 'llm_chunk') {
        return dispatchAgentSessionPayload({ type: 'content', content: event.content ?? '' }, handlers);
      }
      if (event.type === 'llm_done') {
        const finalContent = event.content;
        const run = runRef.current;
        if (run && typeof finalContent === 'string' && finalContent && options.setMessages) {
          options.setMessages((previous) => previous.map((message) => (
            message.id === run.assistantMessageId
              && message.role === 'assistant'
              && typeof message.content === 'string'
              && message.content.length === 0
              ? { ...message, content: finalContent }
              : message
          )));
        }
        await dispatchAgentSessionPayload({ type: 'done', session_id: event.session_id }, handlers);
        return 'done';
      }
      if (event.type === 'agent_step'
        || event.type === 'orchestration_plan'
        || event.type === 'orchestration_update'
        || event.type === 'orchestration_done'
        || event.type === 'timeline_event') {
        return dispatchAgentSessionPayload(event as Record<string, unknown>, handlers);
      }
      return 'ignored';
    },
    async finish(sessionId) {
      const userTurn = currentUserTurn;
      const run = runRef.current;
      if (!run) { currentUserTurn = null; return; }
      await handlers.onDone?.(sessionId);
      if (currentUserTurn === userTurn) currentUserTurn = null;
      if (runRef.current === run) runRef.current = null;
    },
    async fail(error) {
      const userTurn = currentUserTurn;
      const run = runRef.current;
      if (!run) { currentUserTurn = null; return; }
      await handlers.onPayloadError?.(error);
      if (currentUserTurn === userTurn) currentUserTurn = null;
      if (runRef.current === run) runRef.current = null;
    },
    stop() {
      currentUserTurn = null;
      const run = runRef.current;
      if (!run) return;
      handlers.onDetached?.();
      options.setMessages((previous) => previous.map((message) => (
        message.id === run.assistantMessageId && message.role === 'assistant'
          ? { ...message, isStreaming: false, metadata: { ...message.metadata, cycle_stopped: true } }
          : message
      )));
      runRef.current = null;
    },
    clear({ finalize = true } = {}) {
      currentUserTurn = null;
      if (runRef.current && finalize) handlers.onDetached?.();
      runRef.current = null;
    },
  };
}
