import type { AgentToolConfigurationCandidate } from './agent-tool-configuration.ts';

export interface CycleWaitStep { type: 'wait'; wait: number; }
export interface CycleMessageStep { type: 'message'; message: string; photo?: number[]; }
export interface CycleCycleStep { type: 'cycle'; cycle_count: number; cycle_steps: CycleStep[]; }
export type CycleStep = CycleWaitStep | CycleMessageStep | CycleCycleStep;

/** A fixed wall-clock delivery on the top-level scheduled track, never a CycleStep. */
export type ScheduledRecurrence = 'once' | 'daily';
export interface ScheduledMessage {
  id: string;
  time: string;
  recurrence: ScheduledRecurrence;
  day_count: number;
  message: string;
  photo?: number[];
}

export interface CycleConfigResponse {
  enabled?: boolean;
  max_depth?: number;
  max_duration?: number;
  show_thumbnails?: boolean;
  default_sequence?: CycleStep[];
  timeout_policy?: string;
  scheduled_track_max_duration_seconds?: number;
  max_scheduled_message_days?: number;
  event_journal_retention_seconds?: number;
  event_journal_max_records?: number;
  default_scheduled_messages?: ScheduledMessage[];
}

export interface CycleConfig {
  enabled: boolean;
  max_depth: number;
  show_thumbnails: boolean;
  default_sequence: CycleStep[];
  timeout_policy?: string;
  scheduled_track_max_duration_seconds?: number;
  max_scheduled_message_days?: number;
  event_journal_retention_seconds?: number;
  event_journal_max_records?: number;
  default_scheduled_messages: ScheduledMessage[];
}

export interface CycleStartRequest {
  session_id?: string;
  cycle_run_id?: string;
  steps: CycleStep[];
  scheduled_messages?: ScheduledMessage[];
  /** Explicit IANA browser timezone; required whenever scheduled_messages is non-empty. */
  timezone?: string;
  agent_mode: boolean;
  show_agent_steps?: boolean;
  enable_thinking?: boolean;
  enable_local_memory?: boolean;
  share_context: boolean;
  tool_configuration?: AgentToolConfigurationCandidate;
}
export interface CycleStopRequest { session_id?: string; cycle_run_id?: string; }
export interface Camera { index: number; device: string; name?: string; }
export interface CameraListResponse { cameras: Camera[]; }

export interface CycleTimelineEvent {
  type: string;
  cycle_run_id?: string;
  event_sequence?: number;
  track?: 'ordinary' | 'scheduled';
  path?: number[];
  step_type?: string;
  detail?: Record<string, unknown>;
  camera_index?: number;
  image_base64?: string;
  image_omitted?: boolean;
  omission_reason?: string;
  content?: string;
  role?: string;
  duration?: number;
  total_duration?: number;
  error?: string;
  reason?: string;
  status?: string;
  session_id?: string;
  request_fingerprint?: string;
  show_agent_steps?: boolean;
  schedule_id?: string;
  occurrence_index?: number;
  scheduled_for?: string;
  queued_at?: string;
  lateness_seconds?: number;
  next_scheduled_for?: string;
  truncated_through?: number;
  [key: string]: unknown;
}
export type CycleSSEEvent = CycleTimelineEvent;

export interface CycleEditorState {
  steps: CycleStep[];
  scheduledMessages?: ScheduledMessage[];
  shareContext: boolean;
  showAgentSteps: boolean;
}
export type CycleExecutionStatus = 'idle' | 'running' | 'completed' | 'stopped' | 'error' | 'expired';
export interface CycleExecutionState { status: CycleExecutionStatus; sessionId: string | null; currentPath: number[]; events: CycleSSEEvent[]; }

export interface CycleProgressSummary {
  ordinaryCompleted: number;
  scheduledQueued: number;
  scheduledDelivered: number;
  failed: number;
  cumulativeLatenessSeconds: number;
  nextScheduledFor?: string;
  terminalReason?: string;
}

export function createWaitStep(seconds = 5): CycleWaitStep { return { type: 'wait', wait: seconds }; }
export function createMessageStep(message = '', photo?: number[]): CycleMessageStep { return { type: 'message', message, ...(photo ? { photo } : {}) }; }
export function createCycleStep(count = 3, steps: CycleStep[] = []): CycleCycleStep { return { type: 'cycle', cycle_count: count, cycle_steps: steps }; }
export function createScheduledMessage(): ScheduledMessage { return { id: `scheduled-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, time: '09:00:00', recurrence: 'once', day_count: 1, message: '' }; }

export function getBrowserCycleTimezone(): string {
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (!timezone || typeof timezone !== 'string') throw new Error('浏览器未提供有效的 IANA 时区');
  return timezone;
}
export function validateScheduledMessages(items: ScheduledMessage[], timezone: string, maxDailyDayCount?: number): string | null {
  if (!items.length) return null;
  if (!timezone) return '浏览器未提供有效的 IANA 时区';
  const configuredMaxDays = typeof maxDailyDayCount === 'number' && Number.isInteger(maxDailyDayCount) && maxDailyDayCount > 0
    ? maxDailyDayCount
    : undefined;
  const seen = new Set<string>();
  for (const item of items) {
    if (!/^([01]\d|2[0-3]):[0-5]\d:[0-5]\d$/.test(item.time)) return '定时时间必须为 HH:mm:ss';
    if (seen.has(item.id)) return '每条定时消息必须有唯一 ID';
    seen.add(item.id);
    if (!item.message.trim() && !(item.photo?.length)) return '定时消息需要文字或图片';
    if (item.recurrence === 'once' && item.day_count !== 1) return '仅1次的循环天数必须为 1';
    if (item.recurrence === 'daily' && (!Number.isInteger(item.day_count) || item.day_count < 1)) return '每天的循环天数必须为正整数';
    if (item.recurrence === 'daily' && configuredMaxDays !== undefined && item.day_count > configuredMaxDays) {
      return `每天的循环天数最多为 ${configuredMaxDays} 天`;
    }
  }
  return null;
}

export function normalizeCycleConfig(input?: CycleConfigResponse): CycleConfig {
  return {
    enabled: input?.enabled === true,
    max_depth: Number.isFinite(input?.max_depth) ? input!.max_depth! : 5,
    show_thumbnails: input?.show_thumbnails !== false,
    default_sequence: Array.isArray(input?.default_sequence) ? input!.default_sequence! : [],
    timeout_policy: input?.timeout_policy,
    scheduled_track_max_duration_seconds: input?.scheduled_track_max_duration_seconds,
    max_scheduled_message_days: input?.max_scheduled_message_days,
    event_journal_retention_seconds: input?.event_journal_retention_seconds,
    event_journal_max_records: input?.event_journal_max_records,
    default_scheduled_messages: Array.isArray(input?.default_scheduled_messages) ? input!.default_scheduled_messages! : [],
  };
}

export function resolveCycleEditorState(
  raw: string | null,
  defaultSteps: CycleStep[],
  defaultScheduledMessages: ScheduledMessage[],
): CycleEditorState {
  try {
    if (raw) {
      const saved = JSON.parse(raw) as Partial<CycleEditorState>;
      if (Array.isArray(saved.steps)) {
        return {
          steps: saved.steps,
          // An older saved editor state intentionally migrates to an empty
          // scheduled track instead of silently adding new server defaults.
          scheduledMessages: Array.isArray(saved.scheduledMessages) ? saved.scheduledMessages : [],
          shareContext: saved.shareContext !== false,
          showAgentSteps: saved.showAgentSteps === true,
        };
      }
    }
  } catch { /* fall through to configured defaults */ }
  return { steps: defaultSteps, scheduledMessages: defaultScheduledMessages, shareContext: true, showAgentSteps: false };
}

function cycleOccurrenceKey(event: CycleSSEEvent, fallback: string): string {
  if (event.schedule_id !== undefined && event.occurrence_index !== undefined) {
    return `${event.schedule_id}:${event.occurrence_index}`;
  }
  return fallback;
}

export function summarizeCycleProgress(events: CycleSSEEvent[], now = new Date()): CycleProgressSummary {
  const queued = new Set<string>();
  const delivered = new Set<string>();
  const failed = events.filter((event) => event.type === 'scheduled_message_failed' || event.type === 'step_failed').length;
  let cumulativeLatenessSeconds = 0;
  let explicitNext: string | undefined;
  const futureCandidates: string[] = [];

  events.forEach((event, index) => {
    const key = cycleOccurrenceKey(event, `${event.type}:${event.event_sequence ?? index}`);
    if (event.type === 'scheduled_message_queued') queued.add(key);
    if (event.type === 'step_end' && event.track === 'scheduled' && !delivered.has(key)) {
      delivered.add(key);
      if (typeof event.lateness_seconds === 'number' && Number.isFinite(event.lateness_seconds)) {
        cumulativeLatenessSeconds += Math.max(0, event.lateness_seconds);
      }
    }
    if (typeof event.next_scheduled_for === 'string' && Number.isFinite(Date.parse(event.next_scheduled_for))) {
      explicitNext = event.next_scheduled_for;
    }
    if (typeof event.scheduled_for === 'string' && Number.isFinite(Date.parse(event.scheduled_for))) {
      const target = Date.parse(event.scheduled_for);
      if (target > now.getTime() && !delivered.has(key)) futureCandidates.push(event.scheduled_for);
    }
  });

  const nextScheduledFor = explicitNext && Date.parse(explicitNext) > now.getTime()
    ? explicitNext
    : futureCandidates.sort((a, b) => Date.parse(a) - Date.parse(b))[0];
  const terminal = [...events].reverse().find((event) => ['cycle_completed', 'cycle_stopped', 'cycle_error', 'cycle_expired'].includes(event.type));
  return {
    ordinaryCompleted: events.filter((event) => event.type === 'step_end' && event.track !== 'scheduled').length,
    scheduledQueued: queued.size,
    scheduledDelivered: delivered.size,
    failed,
    cumulativeLatenessSeconds,
    nextScheduledFor,
    terminalReason: terminal?.reason ?? terminal?.error,
  };
}
