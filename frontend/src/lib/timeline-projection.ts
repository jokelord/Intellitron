import { isChatHistoryStartRef } from './local-memory.ts';
import type { OrchestrationStage } from './api';

export type ManagerChildStatus = 'pending' | 'running' | 'completed' | 'failed' | 'aborted' | 'timeout';

export interface TimelineEvent {
  kind: string;
  session_id?: string;
  turn_index?: number;
  chunks?: unknown;
  history_start_ref?: unknown;
  source?: string;
  message_id?: string;
  text?: string;
  partial?: boolean;
  display?: string;
  reasoning?: boolean;
  dispatch_id?: string;
  call_id?: string;
  dispatch_kind?: 'routing' | 'secondary_handoff';
  display_slot?: 'before_body' | 'after_body';
  child_run_id?: string;
  child_name?: string;
  status?: ManagerChildStatus;
  assigned_task?: string;
  href?: string;
  final_preview?: string;
  error?: string;
  // Compact-related fields
  summary_message_id?: string;
  trigger?: 'auto' | 'manual';
  mode?: 'agent' | 'chat' | 'cycle';
  original_tokens?: number;
  threshold?: number;
  strategy?: 'performance' | 'utilization';
  metadata?: Record<string, unknown>;
}

export type TimelineProjection =
  | {
      kind: 'local_memory_retrieved';
      sessionId: string;
      turnIndex: number;
      chunks: { text: string }[];
      historyStartRef?: string;
    }
  | {
      kind: 'chat_chunk';
      messageId?: string;
      text: string;
      partial: boolean;
      reasoning?: boolean;
    }
  | {
      kind: 'dispatch_update';
      messageId?: string;
      dispatchId?: string;
      dispatchKind: 'routing' | 'secondary_handoff';
      displaySlot: 'before_body' | 'after_body';
      text: string;
      partial: boolean;
    }
  | {
      kind: 'manager_child_agent_update';
      dispatchId: string;
      callId: string;
      childRunId: string;
      childName: string;
      status: ManagerChildStatus;
      assignedTask?: string;
      href?: string;
      finalPreview?: string;
      error?: string;
    }
  | {
      kind: 'compact_started';
      summaryMessageId: string;
      trigger: 'auto' | 'manual';
      mode: 'agent' | 'chat' | 'cycle';
      originalTokens?: number;
      threshold?: number;
      strategy?: 'performance' | 'utilization';
    }
  | {
      kind: 'compact_delta';
      summaryMessageId: string;
      text: string;
      partial: boolean;
    }
  | {
      kind: 'compact_completed';
      summaryMessageId: string;
      text: string;
      metadata?: Record<string, unknown>;
    }
  | {
      kind: 'compact_failed';
      summaryMessageId: string;
      reason: string;
      error?: string;
    }
  | {
      kind: 'noop';
    };

export function projectTimelineEvent(event: TimelineEvent): TimelineProjection {
  if (event.kind === 'local_memory_retrieved') {
    if (typeof event.session_id !== 'string' || !event.session_id.trim()
      || !Number.isSafeInteger(event.turn_index) || (event.turn_index ?? -1) < 0
      || (event.history_start_ref !== undefined && !isChatHistoryStartRef(event.history_start_ref, event.session_id))
      || !Array.isArray(event.chunks)
      || !event.chunks.every((chunk: unknown) => typeof chunk === 'object' && chunk !== null
        && !Array.isArray(chunk) && typeof (chunk as { text?: unknown }).text === 'string')) {
      return { kind: 'noop' };
    }
    return {
      kind: 'local_memory_retrieved', sessionId: event.session_id, turnIndex: event.turn_index!,
      chunks: event.chunks.map((chunk: { text: string }) => ({ text: chunk.text })),
      ...(event.history_start_ref === undefined ? {} : { historyStartRef: event.history_start_ref as string }),
    };
  }

  if (
    event.kind === 'message_delta'
    && (event.source === 'coordinator_2' || event.source === 'chat')
    && event.display === 'chat_bubble'
  ) {
    return {
      kind: 'chat_chunk',
      messageId: event.message_id,
      text: event.text || '',
      partial: event.partial ?? false,
      reasoning: event.reasoning,
    };
  }

  if (event.kind === 'routing_delta') {
    return { kind: 'noop' };
  }

  if (event.kind === 'dispatch_delta' && event.display === 'dispatch_card') {
    return {
      kind: 'dispatch_update',
      messageId: event.message_id,
      dispatchId: event.dispatch_id,
      dispatchKind: event.dispatch_kind === 'secondary_handoff' ? 'secondary_handoff' : 'routing',
      displaySlot: event.display_slot === 'before_body' ? 'before_body' : 'after_body',
      text: event.text || '',
      partial: event.partial ?? false,
    };
  }

  if (event.kind === 'manager_child_agent_update') {
    return {
      kind: 'manager_child_agent_update',
      dispatchId: event.dispatch_id || '',
      callId: event.call_id || '',
      childRunId: event.child_run_id || '',
      childName: event.child_name || '',
      status: event.status ?? 'pending',
      assignedTask: event.assigned_task,
      href: event.href,
      finalPreview: event.final_preview,
      error: event.error,
    };
  }

  if (event.kind === 'compact_started') {
    return {
      kind: 'compact_started',
      summaryMessageId: event.summary_message_id || '',
      trigger: event.trigger ?? 'manual',
      mode: event.mode ?? 'chat',
      originalTokens: event.original_tokens,
      threshold: event.threshold,
      strategy: event.strategy,
    };
  }

  if (event.kind === 'compact_delta') {
    return {
      kind: 'compact_delta',
      summaryMessageId: event.summary_message_id || '',
      text: event.text || '',
      partial: event.partial ?? true,
    };
  }

  if (event.kind === 'compact_completed') {
    return {
      kind: 'compact_completed',
      summaryMessageId: event.summary_message_id || '',
      text: event.text || '',
      metadata: event.metadata,
    };
  }

  if (event.kind === 'compact_failed') {
    const meta = (event.metadata as { reason?: string; error?: string } | undefined) || {};
    const reason = meta.reason || 'unknown';
    return {
      kind: 'compact_failed',
      summaryMessageId: event.summary_message_id || '',
      reason,
      error: meta.error,
    };
  }

  return { kind: 'noop' };
}

export function getOrdinaryOrchestrationStages(
  stages: OrchestrationStage[],
): OrchestrationStage[] {
  return stages
    .map((stage) => ({
      ...stage,
      tools: stage.tools.filter((tool) => tool.name !== 'dispatching_parallel_agent'),
    }))
    .filter((stage) => stage.tools.length > 0);
}
