import { createClientStepId, type AgentStep, type ChildAgentStreamEvent } from './api.ts';

export type ChildTranscriptStatus = 'connecting' | 'running' | 'completed' | 'failed' | 'aborted' | 'timeout';

export interface ChildTranscriptTurn {
  id: string;
  childName: string;
  assignedTask: string;
  steps: AgentStep[];
  content: string;
  status: ChildTranscriptStatus;
  error: string | null;
  reusedContext?: boolean;
  priorStepCount?: number;
}

export interface ChildTranscriptState {
  childName: string;
  turns: ChildTranscriptTurn[];
  truncationNotice: string | null;
  status: ChildTranscriptStatus;
  error: string | null;
}

function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function numberValue(value: unknown): number | undefined {
  return typeof value === 'number' ? value : undefined;
}

function recordValue(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

export function childAgentStreamEventToStep(event: ChildAgentStreamEvent): AgentStep | null {
  if (event.type === 'agent_step') {
    return {
      id: stringValue(event.step_id) ?? createClientStepId(stringValue(event.step_type) ?? 'step'),
      step_type: stringValue(event.step_type) as AgentStep['step_type'],
      content: stringValue(event.content),
      raw_tool_call: stringValue(event.raw_tool_call),
      ...(event.raw_tool_call_source === 'model_text' || event.raw_tool_call_source === 'provider_payload'
        ? { raw_tool_call_source: event.raw_tool_call_source } : {}),
      name: stringValue(event.name),
      input: recordValue(event.input) ?? stringValue(event.input),
      partial: typeof event.partial === 'boolean' ? event.partial : undefined,
      dispatch_status: stringValue(event.dispatch_status) as AgentStep['dispatch_status'],
      dispatch_stage: stringValue(event.dispatch_stage) as AgentStep['dispatch_stage'],
      dispatch_kind: stringValue(event.dispatch_kind) as AgentStep['dispatch_kind'],
      dispatch_target_agent: stringValue(event.dispatch_target_agent),
      dispatch_message: stringValue(event.dispatch_message),
      dispatch_id: stringValue(event.dispatch_id),
      display_slot: stringValue(event.display_slot) as AgentStep['display_slot'],
    };
  }

  if (event.type === 'orchestration_plan') {
    return {
      id: stringValue(event.step_id) ?? createClientStepId('orchestration'),
      step_type: 'orchestration',
      orchestration_type: 'plan',
      stages: Array.isArray(event.stages) ? event.stages as AgentStep['stages'] : undefined,
    };
  }

  if (event.type === 'orchestration_update') {
    return {
      id: stringValue(event.step_id) ?? createClientStepId('orchestration'),
      step_type: 'orchestration',
      orchestration_type: 'update',
      orchestration_update: {
        call_id: stringValue(event.call_id) ?? '',
        status: stringValue(event.status) as NonNullable<AgentStep['orchestration_update']>['status'],
        result_preview: stringValue(event.result_preview),
        duration_ms: numberValue(event.duration_ms),
      },
    };
  }

  if (event.type === 'orchestration_done') {
    return {
      id: stringValue(event.step_id) ?? createClientStepId('orchestration'),
      step_type: 'orchestration',
      orchestration_type: 'done',
      orchestration_summary: {
        total: numberValue(event.total_tools) ?? numberValue(event.total) ?? 0,
        completed: numberValue(event.completed) ?? 0,
        failed: numberValue(event.failed) ?? 0,
        skipped: numberValue(event.skipped) ?? 0,
        timeout: numberValue(event.timeout) ?? 0,
        duration_ms: numberValue(event.duration_ms) ?? 0,
      },
    };
  }

  return null;
}

function makeTurn(event: ChildAgentStreamEvent, fallbackIndex: number): ChildTranscriptTurn {
  const childName = stringValue(event.child_name) ?? 'Child agent';
  return {
    id: stringValue(event.turn_id) ?? stringValue(event.seq) ?? `turn-${fallbackIndex}`,
    childName,
    assignedTask: stringValue(event.task) ?? stringValue(event.assigned_task) ?? '',
    steps: [],
    content: '',
    status: 'running',
    error: null,
    reusedContext: typeof event.reused_context === 'boolean' ? event.reused_context : undefined,
    priorStepCount: numberValue(event.prior_step_count),
  };
}

function updateLastTurn(
  state: ChildTranscriptState,
  updater: (turn: ChildTranscriptTurn) => ChildTranscriptTurn,
): ChildTranscriptState {
  const turns = state.turns.length > 0
    ? state.turns
    : [makeTurn({ type: 'manager_assigned_task' }, 0)];
  const lastIndex = turns.length - 1;
  return {
    ...state,
    turns: turns.map((turn, index) => (index === lastIndex ? updater(turn) : turn)),
  };
}

export function applyChildTranscriptEvent(
  state: ChildTranscriptState,
  event: ChildAgentStreamEvent,
): ChildTranscriptState {
  if (event.type === 'manager_assigned_task') {
    const turn = makeTurn(event, state.turns.length);
    return {
      ...state,
      childName: turn.childName,
      turns: [...state.turns, turn],
      status: 'running',
      error: null,
    };
  }

  if (event.type === 'truncated') {
    const dropped = typeof event.dropped_events === 'number' ? event.dropped_events : undefined;
    return {
      ...state,
      truncationNotice: stringValue(event.message)
        ?? `Earlier child agent events were truncated${dropped ? ` (${dropped} events)` : ''}.`,
    };
  }

  if (event.type === 'content') {
    const content = stringValue(event.content) ?? '';
    return {
      ...updateLastTurn(state, (turn) => ({
        ...turn,
        content: `${turn.content}${content}`,
        status: 'running',
      })),
      status: 'running',
    };
  }

  if (event.type === 'done') {
    const finalReport = stringValue(event.final_report);
    return {
      ...updateLastTurn(state, (turn) => ({
        ...turn,
        content: finalReport || turn.content,
        status: 'completed',
      })),
      status: 'completed',
    };
  }

  if (event.type === 'error') {
    const error = stringValue(event.error) ?? stringValue(event.content) ?? 'Child agent failed';
    const status = event.status === 'timeout' ? 'timeout' : 'failed';
    return {
      ...updateLastTurn(state, (turn) => ({ ...turn, error, status })),
      status,
      error,
    };
  }

  if (event.type === 'aborted') {
    const error = stringValue(event.error) ?? stringValue(event.content) ?? null;
    return {
      ...updateLastTurn(state, (turn) => ({ ...turn, error, status: 'aborted' })),
      status: 'aborted',
      error,
    };
  }

  const step = childAgentStreamEventToStep(event);
  if (!step) {
    return state;
  }

  return {
    ...updateLastTurn(state, (turn) => ({
      ...turn,
      steps: mergeChildTranscriptStep(turn.steps, step),
      status: 'running',
    })),
    status: 'running',
  };
}

export function mergeChildTranscriptStep(steps: AgentStep[], step: AgentStep): AgentStep[] {
  const lastStep = steps.length > 0 ? steps[steps.length - 1] : null;
  if (step.step_type === 'thought' && step.raw_tool_call && !step.content && lastStep?.step_type === 'thought') {
    return [
      ...steps.slice(0, -1),
      {
        ...lastStep,
        raw_tool_call: step.raw_tool_call,
        raw_tool_call_source: step.raw_tool_call_source,
        partial: step.partial ?? lastStep.partial,
      },
    ];
  }
  if (step.step_type === 'thought' && lastStep?.step_type === 'thought' && lastStep.partial) {
    return [
      ...steps.slice(0, -1),
      {
        ...lastStep,
        content: `${lastStep.content ?? ''}${step.content ?? ''}`,
        raw_tool_call: step.raw_tool_call ?? lastStep.raw_tool_call,
        raw_tool_call_source: step.raw_tool_call ? step.raw_tool_call_source : lastStep.raw_tool_call_source,
        partial: step.partial ?? false,
      },
    ];
  }

  if (step.step_type !== 'orchestration') {
    return [...steps, step];
  }

  if (step.orchestration_type === 'plan') {
    return [...steps, step];
  }

  if (step.orchestration_type === 'update' && step.orchestration_update) {
    for (let index = steps.length - 1; index >= 0; index -= 1) {
      const candidate = steps[index];
      if (candidate.step_type === 'orchestration' && candidate.orchestration_type === 'plan' && candidate.stages) {
        const updatedStages = candidate.stages.map((stage) => ({
          ...stage,
          tools: stage.tools.map((tool) => (
            tool.call_id === step.orchestration_update!.call_id
              ? { ...tool, ...step.orchestration_update!, status: step.orchestration_update!.status }
              : tool
          )),
        }));
        return [
          ...steps.slice(0, index),
          { ...candidate, stages: updatedStages },
          ...steps.slice(index + 1),
        ];
      }
    }
    return steps;
  }

  if (step.orchestration_type === 'done' && step.orchestration_summary) {
    for (let index = steps.length - 1; index >= 0; index -= 1) {
      const candidate = steps[index];
      if (candidate.step_type === 'orchestration' && candidate.orchestration_type === 'plan') {
        return [
          ...steps.slice(0, index),
          { ...candidate, orchestration_summary: step.orchestration_summary },
          ...steps.slice(index + 1),
        ];
      }
    }
    return steps;
  }

  return steps;
}
