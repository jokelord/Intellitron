/**
 * LLM Client API Library
 *
 * 提供与后端 LLM 服务的通信接口，支持：
 * - OpenAI 兼容的聊天 API
 * - Agent 工具调用（SSE 流式）
 * - 图片上传和处理
 * - Cycle 可编程序列执行
 */

import {
  buildAgentVisionAutomationAbortRequest,
  isValidRequestId,
  parseRequestAcceptedEvent,
  parseRequestStatusResponse,
  type RequestAcceptedEvent,
  type RequestStatusResult,
  type AgentVisionAutomationAbortRequest,
  type AgentVisionAutomationAbortCancellationResponse,
  type AgentVisionAutomationAbortResponse,
  type VisionAutomationTransportMetadata,
} from './vision-automation/send-contract.ts';
import {
  readSessionHubStream,
  type SessionHubKnownRejection,
  type SessionHubPayloadHandlers,
  type SessionHubReadResult,
} from './session-hub-client.ts';
import type {
  AgentToolConfigurationCandidate,
  AgentBusinessToolCatalog,
  AgentToolConfigurationSnapshot,
} from './agent-tool-configuration.ts';
import type { CycleConfigResponse } from './cycle-types.ts';

// ============================================================
// 类型定义
// ============================================================

/**
 * 聊天消息
 */
export interface ChatMessage {
  role: 'system' | 'user' | 'assistant';
  content: string | Array<{ type: string; text?: string; image_url?: { url: string } }>;
  reasoning_content?: string;
  /**
   * Optional client-side metadata. Reserved keys for /compact:
   * - pre_compact: true    — this message has been summarized out
   * - compact_summary: true + summary_id — this message IS a compact
   *   summary that should be replaced with the envelope
   *
   * The backend's `filter_messages_for_llm` consumes these markers
   * before sending the messages to the LLM. External OpenAI clients
   * that don't send `metadata` are also valid (filter is a no-op).
   *
   * Not sent to SGLang itself; the wire-level /v1/chat/completions
   * request includes it but the LLM service strips it before
   * constructing the OpenAI payload.
   */
  metadata?: Record<string, unknown>;
}

/**
 * Agent 执行步骤（思考、工具调用、观察结果）
 */
export interface AgentStep {
  id?: string;
  step_type: 'thought' | 'tool_call' | 'observation' | 'orchestration' | 'dispatch';
  content?: string;
  raw_tool_call?: string;
  raw_tool_call_source?: 'model_text' | 'provider_payload';
  name?: string;
  input?: string | Record<string, unknown>;
  partial?: boolean;
  dispatch_status?: 'running' | 'completed' | 'error' | 'aborted';
  dispatch_stage?: 'routing' | 'handoff' | 'direct_answer';
  dispatch_kind?: 'routing' | 'secondary_handoff';
  dispatch_target_agent?: string;
  dispatch_message?: string;
  dispatch_id?: string;
  display_slot?: 'before_body' | 'after_body';
  // Orchestration fields
  orchestration_type?: 'plan' | 'update' | 'done';
  stages?: OrchestrationStage[];
  orchestration_update?: OrchestrationUpdate;
  orchestration_summary?: OrchestrationSummary;
}

export interface TimelineEvent {
  kind: string;
  source?: string;
  message_id?: string;
  text?: string;
  partial?: boolean;
  display?: string;
  dispatch_id?: string;
  dispatch_kind?: 'routing' | 'secondary_handoff';
  display_slot?: 'before_body' | 'after_body';
  [key: string]: unknown;
}

export interface ChildAgentStreamEvent {
  type: string;
  [key: string]: unknown;
}

export interface AgentSessionStreamEvent {
  event_sequence: number;
  type: string;
  [key: string]: unknown;
}

export interface RetainedAgentSession {
  session_id: string;
  created_at: string;
  last_activity: string;
  run_in_progress: boolean;
}

export interface AgentSessionPayloadHandlers {
  requestId?: string;
  onRequestAccepted?: (event: RequestAcceptedEvent) => void | Promise<void>;
  onSessionStart?: (sessionId: string) => void | Promise<void>;
  onStep?: (step: AgentStep) => void | Promise<void>;
  onContent?: (content: string) => void | Promise<void>;
  onTimelineEvent?: (event: TimelineEvent) => void | Promise<void>;
  onDone?: (sessionId?: string) => void | Promise<void>;
  onPayloadError?: (error: Error) => void | Promise<void>;
}

export interface AgentSessionStreamCallbacks extends AgentSessionPayloadHandlers {
  onEvent?: (event: AgentSessionStreamEvent) => void;
  onComplete?: () => void;
  onDetached?: (error?: Error) => void;
  onError?: (error: Error) => void;
}

export type AgentSessionPayloadDispatchResult =
  | 'accepted'
  | 'session_start'
  | 'agent_step'
  | 'orchestration_plan'
  | 'orchestration_update'
  | 'orchestration_done'
  | 'timeline_event'
  | 'content'
  | 'done'
  | 'error'
  | 'ignored';

function createSessionPayloadStepId(
  payload: Record<string, unknown>,
  fallbackPrefix: string,
): string {
  if (Number.isInteger(payload.event_sequence) && (payload.event_sequence as number) > 0) {
    const identity = [
      typeof payload.step_type === 'string' ? payload.step_type : undefined,
      typeof payload.call_id === 'string' ? payload.call_id : undefined,
      typeof payload.dispatch_id === 'string' ? payload.dispatch_id : undefined,
    ].filter((part): part is string => part !== undefined);
    return [fallbackPrefix, payload.event_sequence, ...identity].join('-');
  }
  return createClientStepId(fallbackPrefix);
}

/**
 * Projects one sequenced Agent Session Hub payload into the existing page
 * callbacks. Both the live POST stream and the recovery GET stream use this
 * translation so their event semantics cannot drift.
 */
export async function dispatchAgentSessionPayload(
  payload: Record<string, unknown>,
  handlers: AgentSessionPayloadHandlers,
): Promise<AgentSessionPayloadDispatchResult> {
  const accepted = parseRequestAcceptedEvent(payload, handlers.requestId);
  if (accepted !== null) {
    await handlers.onRequestAccepted?.(accepted);
    return 'accepted';
  }

  if (payload.type === 'session_start') {
    await handlers.onSessionStart?.(payload.session_id as string);
    return 'session_start';
  }

  if (payload.type === 'agent_step') {
    await handlers.onStep?.({
      id: payload.step_id as string | undefined || createSessionPayloadStepId(payload, 'agent_step'),
      step_type: payload.step_type as AgentStep['step_type'],
      content: payload.content as string | undefined,
      raw_tool_call: payload.raw_tool_call as string | undefined,
      ...(payload.raw_tool_call_source === 'model_text' || payload.raw_tool_call_source === 'provider_payload'
        ? { raw_tool_call_source: payload.raw_tool_call_source } : {}),
      name: payload.name as string | undefined,
      input: payload.input as string | Record<string, unknown> | undefined,
      partial: payload.partial as boolean | undefined,
      dispatch_status: payload.dispatch_status as AgentStep['dispatch_status'],
      dispatch_stage: payload.dispatch_stage as AgentStep['dispatch_stage'],
      dispatch_kind: payload.dispatch_kind as AgentStep['dispatch_kind'],
      dispatch_target_agent: payload.dispatch_target_agent as string | undefined,
      dispatch_message: payload.dispatch_message as string | undefined,
      dispatch_id: payload.dispatch_id as string | undefined,
      display_slot: payload.display_slot as AgentStep['display_slot'],
    });
    return 'agent_step';
  }

  if (payload.type === 'orchestration_plan') {
    await handlers.onStep?.({
      id: payload.step_id as string | undefined || createSessionPayloadStepId(payload, 'orchestration_plan'),
      step_type: 'orchestration',
      orchestration_type: 'plan',
      stages: payload.stages as OrchestrationStage[] | undefined,
    });
    return 'orchestration_plan';
  }

  if (payload.type === 'orchestration_update') {
    await handlers.onStep?.({
      id: payload.step_id as string | undefined || createSessionPayloadStepId(payload, 'orchestration_update'),
      step_type: 'orchestration',
      orchestration_type: 'update',
      orchestration_update: {
        call_id: payload.call_id as string,
        status: payload.status as OrchestrationUpdate['status'],
        result_preview: payload.result_preview as string | undefined,
        duration_ms: payload.duration_ms as number | undefined,
      },
    });
    return 'orchestration_update';
  }

  if (payload.type === 'orchestration_done') {
    await handlers.onStep?.({
      id: payload.step_id as string | undefined || createSessionPayloadStepId(payload, 'orchestration_done'),
      step_type: 'orchestration',
      orchestration_type: 'done',
      orchestration_summary: {
        total: (payload.total_tools ?? payload.total) as number,
        completed: payload.completed as number,
        failed: payload.failed as number,
        skipped: payload.skipped as number,
        timeout: payload.timeout as number,
        duration_ms: payload.duration_ms as number,
      },
    });
    return 'orchestration_done';
  }

  if (payload.type === 'timeline_event') {
    if (payload.event && handlers.onTimelineEvent) {
      await handlers.onTimelineEvent(payload.event as TimelineEvent);
    }
    return 'timeline_event';
  }

  if (payload.type === 'content') {
    if (payload.content && handlers.onContent) {
      await handlers.onContent(payload.content as string);
    }
    return 'content';
  }

  if (payload.type === 'done') {
    await handlers.onDone?.(payload.session_id as string | undefined);
    return 'done';
  }

  if (payload.type === 'error') {
    await handlers.onPayloadError?.(new Error(
      payload.error as string || payload.content as string || 'Unknown error',
    ));
    return 'error';
  }

  return 'ignored';
}

export function createClientStepId(prefix: string = 'step'): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

export function getSavedChatSettings<T extends object>(defaults: T, storageKey: string = 'llm-chat-settings'): T {
  if (typeof window === 'undefined') {
    return defaults;
  }

  const saved = localStorage.getItem(storageKey);
  if (!saved) {
    return defaults;
  }

  try {
    return { ...defaults, ...JSON.parse(saved) };
  } catch {
    return defaults;
  }
}

/**
 * 解析浏览器缓存中的设置对象；缓存不存在或内容非法时返回 null。
 */
export function parseCachedChatSettings(storageKey: string = 'llm-chat-settings'): Record<string, unknown> | null {
  if (typeof window === 'undefined') {
    return null;
  }

  const saved = localStorage.getItem(storageKey);
  if (!saved) {
    return null;
  }

  try {
    const parsed = JSON.parse(saved);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return null;
    }
    return parsed as Record<string, unknown>;
  } catch {
    return null;
  }
}

/**
 * 合并服务端默认值与浏览器缓存设置。
 *
 * 缓存中实际存在的键（以键出现为准，值可以是 false / 0 / 空字符串）优先于服务端默认值；
 * 缓存不存在的键回落到服务端默认值。
 */
export function mergeServerDefaultsWithCache<T extends object>(
  serverDefaults: T,
  cached: Record<string, unknown> | null | undefined,
): T {
  const merged: T = { ...serverDefaults };
  if (!cached) {
    return merged;
  }
  for (const key of Object.keys(cached)) {
    if (key in merged) {
      (merged as Record<string, unknown>)[key] = cached[key];
    }
  }
  return merged;
}

/** 编排执行阶段 */
export interface OrchestrationStage {
  stage_id: number;
  parallel: boolean;
  tools: OrchestrationTool[];
}

/** 编排中的单个工具 */
export interface OrchestrationTool {
  call_id: string;
  name: string;
  args: Record<string, unknown>;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'skipped' | 'timeout';
  result_preview?: string;
  duration_ms?: number;
}

/** 编排进度更新 */
export interface OrchestrationUpdate {
  call_id: string;
  status: 'running' | 'completed' | 'failed' | 'skipped' | 'timeout';
  result_preview?: string;
  duration_ms?: number;
}

/** 编排完成汇总 */
export interface OrchestrationSummary {
  total: number;
  completed: number;
  failed: number;
  skipped: number;
  timeout: number;
  duration_ms: number;
}

/**
 * Agent 对话请求体
 */
export interface AgentChatRequest {
  session_id?: string;
  message: string;
  images?: string[];
  mode?: string;
  context_messages?: ChatMessage[];
  enable_thinking?: boolean;
  enable_local_memory?: boolean;
  request_id?: string;
  vision_automation?: VisionAutomationTransportMetadata;
  tool_configuration?: AgentToolConfigurationCandidate;
}

/**
 * 配置响应
 */
export interface ConfigResponse {
  local_memory?: {
    default_enabled: false;
    available: boolean;
    status: 'disabled' | 'waiting_startup' | 'indexing' | 'ready' | 'degraded';
    indexed_files: number;
    indexed_chunks: number;
    last_sync: string | null;
    error_code: string | null;
    applied: { min_similarity: number; top_k: number };
  };
  model?: {
    model_name: string;
    model_type: 'text' | 'vision';
    tensor_parallel_size?: number;
    max_context_length?: number;
  };
  vision?: {
    enabled: boolean;
    max_image_size: number;
    supported_formats: string;
    max_image_tokens: number;
    max_image_dimension?: number;
    camera_max_dimension?: number;
    capture_dimension?: number;
    automation?: {
      enabled: boolean;
      interval_seconds: number;
      max_frames_per_source: number;
      thumbnail_size_px: number;
      capture_during_inference: boolean;
      lock_first_bundle: boolean;
      tick_timeout_seconds: number;
      consecutive_tick_failure_limit: number;
      max_encoded_payload_mib: number;
      max_encoded_payload_bytes: number;
      initial_jpeg_quality: number;
      minimum_jpeg_quality: number;
      minimum_image_dimension_px: number;
      base64_encode_concurrency: number;
      max_request_body_mib: number;
      max_request_body_bytes: number;
      request_status_ttl_seconds: number;
    };
  };
  inference?: {
    active_backend: 'sglang' | 'vllm' | 'openai_proxy' | 'mock' | null;
    supports_targeted_abort: boolean;
    temperature: number;
    top_p: number;
    top_k: number;
    max_tokens: number;
    repetition_penalty: number;
    stream: boolean;
  };
  sglang?: {
    chat_template?: string;
  };
  server?: {
    host: string;
    port: number;
  };
  frontend?: {
    dev_port?: number;
    api_base_url?: string;
    chat_stream_idle_timeout_seconds?: number;
    agent_stream_idle_timeout_seconds?: number;
    cycle_stream_idle_timeout_seconds?: number;
  };
  asr?: {
    enabled: boolean;
    sample_rate_hz: number;
    capture_frame_ms: number;
    input_gain_db: number;
    input_gain_min_db: number;
    input_gain_max_db: number;
    input_gain_step_db: number;
    clip_limit: number;
    auto_send: boolean;
    streaming: boolean;
    websocket_path: string;
    public_websocket_url?: string | null;
    require_secure_context: boolean;
    abort_timeout_seconds: number;
    max_buffered_frames: number;
    dictation_magic_send: {
      commands: string[];
      poll_interval_ms: number;
    };
    browser_constraints: {
      echo_cancellation: boolean;
      noise_suppression: boolean;
      auto_gain_control: boolean;
    };
  };
  webUI?: {
    content?: {
      bubble_character_limit?: number;
    };
    skin?: {
      id?: string;
      name?: string;
    };
    layout?: {
      header_background_alpha?: number;
      footer_background_alpha?: number;
    };
    components?: {
      input_bar?: {
        min_height_px?: number;
        max_rows?: number;
        line_height_px?: number;
        vertical_padding_px?: number;
        border_radius_px?: number;
      };
      action_buttons?: {
        send?: { width_px?: number; height_px?: number };
        stop?: { width_px?: number; height_px?: number };
        answer_now?: { width_px?: number; height_px?: number };
      };
      mermaid?: {
        min_height_px?: number;
      };
      page_header?: {
        display?: boolean;
        text_title?: string;
        vision_title?: string;
        subtitle?: string;
      };
      message_avatar?: {
        user_image_path?: string;
        assistant_image_path?: string;
        size_px?: number;
      };
      vision_welcome?: {
        title?: string;
        description?: string;
        icon_image_path?: string;
      };
      agent_steps?: {
        fold_after_final_answer?: boolean;
      };
      message_templates?: {
        items?: string[];
      };
    };
    effects?: {
      aurora_glow?: {
        enabled?: boolean;
        intensity?: number;
        blur_px?: number;
        chromatic_aberration?: number;
        refraction_strength?: number;
        tint_a?: string;
        tint_b?: string;
        opacity?: number;
        animated?: boolean;
        max_dpr?: number;
      };
      glass_jelly?: {
        enabled?: boolean;
        intensity?: number;
        blur_px?: number;
        chromatic_aberration?: number;
        refraction_strength?: number;
        tint_a?: string;
        tint_b?: string;
        opacity?: number;
        animated?: boolean;
        max_dpr?: number;
      };
    };
  };
  platform?: {
    current: string;
  };
  runtime_prompts?: {
    enable_runtime_time_context?: boolean;
  };
  agent?: {
    enabled: boolean;
    mode: string;
    entry_agent_name?: string;
    business_tool_catalog?: AgentBusinessToolCatalog['tools'];
    default_profile_tools?: AgentBusinessToolCatalog['default_profile_tools'];
    tool_configuration_schema_version?: number;
    tool_catalog_revision?: string;
    schema_version?: number;
    catalog_revision?: string;
    business_tools?: AgentBusinessToolCatalog['tools'];
    tools?: AgentBusinessToolCatalog['tools'];
    tool_configuration?: {
      schema_version: number;
      catalog_revision: string;
      tools: AgentBusinessToolCatalog['tools'];
      defaults: AgentBusinessToolCatalog['default_profile_tools'];
    };
  };
  cycle?: CycleConfigResponse;
}

export type CycleRecoveryCode = 'expired' | 'unavailable' | 'conflict' | 'request';

export class CycleRecoveryError extends Error {
  readonly code: CycleRecoveryCode;
  readonly status: number;
  readonly retryable: boolean;

  constructor(
    code: CycleRecoveryCode,
    message: string,
    status: number,
    retryable: boolean,
  ) {
    super(message);
    this.name = 'CycleRecoveryError';
    this.code = code;
    this.status = status;
    this.retryable = retryable;
  }
}

function cycleErrorCode(detail: unknown): string {
  if (typeof detail === 'string') {
    try { return cycleErrorCode(JSON.parse(detail)); } catch { return detail; }
  }
  if (!detail || typeof detail !== 'object') return '';
  const value = detail as Record<string, unknown>;
  for (const key of ['code', 'error', 'reason', 'status', 'detail']) {
    if (typeof value[key] === 'string') return value[key] as string;
    if (value[key] && typeof value[key] === 'object') {
      const nested = cycleErrorCode(value[key]);
      if (nested) return nested;
    }
  }
  return '';
}

export function parseCycleRecoveryError(status: number, detail: unknown): CycleRecoveryError {
  const code = cycleErrorCode(detail).toLowerCase();
  if (status === 410 || (/(expired|horizon|retention)/.test(code) && !/unavailable.*expired/.test(code))) {
    return new CycleRecoveryError('expired', 'The Cycle run has expired and is no longer available.', status, false);
  }
  if (status === 404 || /unavailable|not.?found|backend.?restart|unknown/.test(code)) {
    return new CycleRecoveryError('unavailable', 'The Cycle run is unavailable. The backend may have restarted or its retained events may have expired.', status, false);
  }
  if (status === 409 || /conflict|fingerprint|identity/.test(code)) {
    return new CycleRecoveryError('conflict', 'The Cycle run identity or request fingerprint conflicts with the existing run.', status, false);
  }
  return new CycleRecoveryError('request', `Cycle request failed (${status}).`, status, status >= 500);
}

/**
 * 模型信息
 */
export interface ModelInfo {
  id: string;
  context_length?: number;
}

/**
 * 图片处理响应
 */
export interface ImageProcessResponse {
  image: string;
  width: number;
  height: number;
  original_size: number;
  processed_size: number;
  format: string;
}

/**
 * 聊天流式选项
 */
export interface ChatStreamOptions {
  temperature?: number;
  top_p?: number;
  top_k?: number;
  repetition_penalty?: number;
  max_tokens?: number;
  request_id?: string;
  enable_thinking?: boolean;
  enable_local_memory?: boolean;
  chat_history_start_ref?: string;
  vision_automation?: VisionAutomationTransportMetadata;
  /** 提供 Hub journal ID：推理脱离页面连接继续，可经 chatSessionEvents 恢复。 */
  /** Canonical Session Hub context ID for Chat requests. */
  session_id?: string;
}

export interface ChatSessionResumeHandlers {
  onContent: (fullText: string) => void;
  onReasoning?: (fullText: string) => void;
  onComplete: () => void;
  onDetached?: () => void;
  onError: (error: Error) => void;
}

export interface RequestLifecycleCallbacks {
  onRequestAccepted?: (event: RequestAcceptedEvent) => void | Promise<void>;
  /** A non-2xx response received before any request_accepted SSE envelope. */
  onKnownRejection?: (rejection: KnownRequestRejection) => void | Promise<void>;
  onTransportEof?: () => void | Promise<void>;
  /** The local subscriber ended before a Hub `done` record was observed. */
  onDetached?: (error?: Error, context?: RequestLifecycleDetachContext) => void | Promise<void>;
  onStableTerminal?: (result: RequestStatusResult) => void | Promise<void>;
  pollIntervalMs?: number;
}

export interface RequestLifecycleDetachContext {
  intentionalInterruption: boolean;
}

export interface KnownRequestRejection extends SessionHubKnownRejection {}

function stableJsonDetail(value: unknown): string | null {
  if (value === null || typeof value === 'boolean' || typeof value === 'number' || typeof value === 'string') {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    const entries = value.map(stableJsonDetail);
    return entries.some((entry) => entry === null) ? null : `[${entries.join(',')}]`;
  }
  if (typeof value === 'object') {
    const entries = Object.keys(value as Record<string, unknown>).sort().map((key) => {
      const serialized = stableJsonDetail((value as Record<string, unknown>)[key]);
      return serialized === null ? null : `${JSON.stringify(key)}:${serialized}`;
    });
    return entries.some((entry) => entry === null) ? null : `{${entries.join(',')}}`;
  }
  return null;
}

async function readKnownRequestRejection(
  response: Response,
  requestKind: 'Agent' | 'Chat',
): Promise<KnownRequestRejection> {
  let detail = `${requestKind} request rejected`;
  let code: string | undefined;
  try {
    const payload: unknown = await response.clone().json();
    if (typeof payload === 'object' && payload !== null && 'detail' in payload) {
      const rawDetail = (payload as Record<string, unknown>).detail;
      if (typeof rawDetail === 'object' && rawDetail !== null && !Array.isArray(rawDetail)) {
        const rawCode = (rawDetail as Record<string, unknown>).code;
        if (typeof rawCode === 'string' && rawCode.length > 0) code = rawCode;
      }
      detail = typeof rawDetail === 'string'
        ? rawDetail
        : stableJsonDetail(rawDetail) ?? detail;
    }
  } catch {
    // Do not surface a response's HTML/text body. The HTTP status is enough.
  }
  const retryable = response.status === 503
    || (response.status === 409 && /\bbusy\b/i.test(detail));
  return {
    status: response.status,
    detail,
    message: `${requestKind} request rejected (${response.status}): ${detail}`,
    retryable,
    ...(code === undefined ? {} : { code }),
  };
}

// ============================================================
// API 基础配置
// ============================================================

interface ApiBaseUrlOptions {
  configuredBaseUrl?: string;
  location?: Pick<Location, 'protocol' | 'hostname'>;
}

export const getApiBaseUrl = (options: ApiBaseUrlOptions = {}): string => {
  const configuredBaseUrl = options.configuredBaseUrl ?? process.env.NEXT_PUBLIC_API_BASE_URL;
  const browserLocation = options.location ?? (typeof window !== 'undefined' ? window.location : undefined);

  if (browserLocation) {
    if (configuredBaseUrl) {
      try {
        const configuredUrl = new URL(configuredBaseUrl);
        const configuredIsLoopback = ['localhost', '127.0.0.1'].includes(
          configuredUrl.hostname,
        );
        const pathname = configuredUrl.pathname === '/' ? '' : configuredUrl.pathname.replace(/\/$/, '');

        if (!configuredIsLoopback) {
          return configuredBaseUrl;
        }

        return pathname ? `/api/backend${pathname}` : '/api/backend';
      } catch {
        return configuredBaseUrl;
      }
    }

    return '/api/backend';
  }

  return configuredBaseUrl || 'http://localhost:8090';
};

const API_BASE_URL = getApiBaseUrl();
const DEFAULT_CHAT_STREAM_IDLE_TIMEOUT_SECONDS = 0;
const DEFAULT_AGENT_STREAM_IDLE_TIMEOUT_SECONDS = 0;
const DEFAULT_CYCLE_STREAM_IDLE_TIMEOUT_SECONDS = 0;

type StreamKind = 'chat' | 'agent' | 'cycle';

interface StreamIdleTimeouts {
  chat: number;
  agent: number;
  cycle: number;
}

// ============================================================
// LLM Client 类
// ============================================================

export class LLMClient {
  private baseUrl: string;
  private streamIdleTimeouts: StreamIdleTimeouts;

  constructor(baseUrl?: string) {
    this.baseUrl = baseUrl || API_BASE_URL;
    this.streamIdleTimeouts = {
      chat: DEFAULT_CHAT_STREAM_IDLE_TIMEOUT_SECONDS,
      agent: DEFAULT_AGENT_STREAM_IDLE_TIMEOUT_SECONDS,
      cycle: DEFAULT_CYCLE_STREAM_IDLE_TIMEOUT_SECONDS,
    };
  }

  private createInactivityTimeout(
    controller: AbortController,
    kind: StreamKind,
    onTimeout?: () => void,
  ) {
    let timeoutId: ReturnType<typeof setTimeout> | undefined;
    const timeoutMs = this.streamIdleTimeouts[kind] * 1000;

    const clear = () => {
      if (timeoutId !== undefined) {
        clearTimeout(timeoutId);
        timeoutId = undefined;
      }
    };

    const refresh = () => {
      clear();
      if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
        return;
      }
      timeoutId = setTimeout(() => {
        onTimeout?.();
        controller.abort();
      }, timeoutMs);
    };

    refresh();

    return { refresh, clear, timeoutMs };
  }

  setStreamIdleTimeouts(timeouts: Partial<StreamIdleTimeouts>): void {
    this.streamIdleTimeouts = {
      ...this.streamIdleTimeouts,
      ...timeouts,
    };
  }

  async healthCheck(): Promise<any> {
    const response = await fetch(`${this.baseUrl}/health`, {
      method: 'GET',
      headers: { 'Content-Type': 'application/json' },
    });

    if (!response.ok) throw new Error('Health check failed');
    return response.json();
  }

  async getConfig(): Promise<ConfigResponse> {
    const response = await fetch(`${this.baseUrl}/config`, {
      method: 'GET',
      headers: { 'Content-Type': 'application/json' },
    });

    if (!response.ok) throw new Error('Failed to fetch config');
    const data = await response.json();
    this.setStreamIdleTimeouts({
      chat: data.frontend?.chat_stream_idle_timeout_seconds ?? this.streamIdleTimeouts.chat,
      agent: data.frontend?.agent_stream_idle_timeout_seconds ?? this.streamIdleTimeouts.agent,
      cycle: data.frontend?.cycle_stream_idle_timeout_seconds ?? this.streamIdleTimeouts.cycle,
    });
    return data;
  }

  async listModels(): Promise<ModelInfo[]> {
    const response = await fetch(`${this.baseUrl}/v1/models`, {
      method: 'GET',
      headers: { 'Content-Type': 'application/json' },
    });

    if (!response.ok) throw new Error('Failed to fetch models');
    const data = await response.json();
    return data.data || [];
  }

  async getRequestStatus(requestId: string, signal?: AbortSignal): Promise<RequestStatusResult> {
    if (!isValidRequestId(requestId)) {
      throw new TypeError('requestId must match the backend lifecycle grammar');
    }
    const response = await fetch(
      `${this.baseUrl}/v1/request-status/${encodeURIComponent(requestId)}`,
      { method: 'GET', headers: { Accept: 'application/json' }, cache: 'no-store', signal },
    );
    if (!response.ok) throw new Error(`Request status query failed: ${response.statusText}`);
    return parseRequestStatusResponse(await response.json(), requestId);
  }

  async registerAgentVisionAutomationAbort(
    request: AgentVisionAutomationAbortRequest,
    signal?: AbortSignal,
  ): Promise<AgentVisionAutomationAbortResponse> {
    const validated = buildAgentVisionAutomationAbortRequest({
      sessionId: request.session_id,
      pageGeneration: request.binding.page_generation,
      automationTaskId: request.binding.automation_task_id,
      captureCycleId: request.binding.capture_cycle_id,
      cleanupToken: request.cleanup_token,
    });
    if (request.binding.mode !== 'agent'
      || request.binding.top_level_context_id !== request.session_id
      || request.binding.cleanup_token !== null) {
      throw new TypeError('Agent Vision automation abort binding identity is invalid');
    }
    const response = await fetch(`${this.baseUrl}/v1/agent/vision-automation/abort`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(validated),
      signal,
    });
    if (!response.ok) {
      throw new Error(`Agent Vision automation abort registration failed: ${response.statusText}`);
    }
    const body = await response.json() as Record<string, unknown>;
    if (body.status !== 'cleanup_pending' || body.session_id !== request.session_id) {
      throw new Error('Agent Vision automation abort response identity does not match the request');
    }
    return { status: 'cleanup_pending', session_id: request.session_id };
  }

  async cancelAgentVisionAutomationAbort(
    request: AgentVisionAutomationAbortRequest,
    signal?: AbortSignal,
  ): Promise<AgentVisionAutomationAbortCancellationResponse> {
    const validated = buildAgentVisionAutomationAbortRequest({
      sessionId: request.session_id,
      pageGeneration: request.binding.page_generation,
      automationTaskId: request.binding.automation_task_id,
      captureCycleId: request.binding.capture_cycle_id,
      cleanupToken: request.cleanup_token,
    });
    if (request.binding.mode !== 'agent'
      || request.binding.top_level_context_id !== request.session_id
      || request.binding.cleanup_token !== null) {
      throw new TypeError('Agent Vision automation abort binding identity is invalid');
    }
    const response = await fetch(`${this.baseUrl}/v1/agent/vision-automation/abort/cancel`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(validated),
      signal,
    });
    if (!response.ok) {
      throw new Error(`Agent Vision automation abort cancellation failed: ${response.statusText}`);
    }
    const body = await response.json() as Record<string, unknown>;
    if (body.status !== 'cleanup_cancelled' || body.session_id !== request.session_id) {
      throw new Error('Agent Vision automation abort cancellation identity does not match the request');
    }
    return { status: 'cleanup_cancelled', session_id: request.session_id };
  }

  async deleteAgentSession(
    sessionId: string,
    options: { keepalive?: boolean } = {},
  ): Promise<void> {
    const response = await fetch(
      `${this.baseUrl}/v1/agent/session/${encodeURIComponent(sessionId)}`,
      {
        method: 'DELETE',
        headers: { Accept: 'application/json' },
        ...(options.keepalive === undefined ? {} : { keepalive: options.keepalive }),
      },
    );
    if (!response.ok) {
      throw new Error(`Agent session delete failed: ${response.statusText}`);
    }
  }

  async deleteSessionHub(
    sessionId: string,
    options: { keepalive?: boolean } = {},
  ): Promise<void> {
    if (!sessionId) throw new TypeError('sessionId is required');
    const response = await fetch(
      `${this.baseUrl}/v1/session/${encodeURIComponent(sessionId)}`,
      {
        method: 'DELETE',
        headers: { Accept: 'application/json' },
        ...(options.keepalive === undefined ? {} : { keepalive: options.keepalive }),
      },
    );
    if (!response.ok) throw new Error(`Session Hub delete failed: ${response.statusText}`);
  }

  async listRetainedAgentSessions(
    options: { signal?: AbortSignal } = {},
  ): Promise<RetainedAgentSession[]> {
    const response = await fetch(`${this.baseUrl}/v1/agent/sessions/retained`, {
      headers: { Accept: 'application/json' },
      ...(options.signal === undefined ? {} : { signal: options.signal }),
    });
    if (!response.ok) {
      throw new Error(`Retained agent session lookup failed: ${response.statusText}`);
    }
    const body = await response.json() as { sessions?: RetainedAgentSession[] };
    return Array.isArray(body.sessions) ? body.sessions : [];
  }

  async waitForRequestTerminal(
    requestId: string,
    options: { signal?: AbortSignal; pollIntervalMs?: number } = {},
  ): Promise<RequestStatusResult> {
    const pollIntervalMs = options.pollIntervalMs ?? 250;
    if (!Number.isFinite(pollIntervalMs) || pollIntervalMs < 0) {
      throw new RangeError('pollIntervalMs must be a finite non-negative number');
    }
    while (true) {
      const result = await this.getRequestStatus(requestId, options.signal);
      if (result.status === 'terminal') return result;
      if (result.status === 'unknown') {
        throw new Error(`Request lifecycle status is unknown: ${requestId}`);
      }
      if (pollIntervalMs > 0) {
        await new Promise<void>((resolve, reject) => {
          const cleanup = () => options.signal?.removeEventListener('abort', onAbort);
          const timeout = setTimeout(() => {
            cleanup();
            resolve();
          }, pollIntervalMs);
          const onAbort = () => {
            clearTimeout(timeout);
            cleanup();
            reject(options.signal?.reason ?? new DOMException('Status polling aborted', 'AbortError'));
          };
          if (options.signal?.aborted) onAbort();
          else options.signal?.addEventListener('abort', onAbort, { once: true });
        });
      }
    }
  }

  private async reportStableTerminal(
    requestId: string | undefined,
    signal: AbortSignal,
    callbacks?: RequestLifecycleCallbacks,
  ): Promise<void> {
    if (requestId === undefined || callbacks?.onStableTerminal === undefined) return;
    const result = await this.waitForRequestTerminal(requestId, {
      signal,
      pollIntervalMs: callbacks.pollIntervalMs,
    });
    await callbacks.onStableTerminal(result);
  }

  async chatStream(
    messages: ChatMessage[],
    options: ChatStreamOptions,
    onChunk: (content: string, reasoning?: string) => void,
    onComplete: () => void,
    onError: (error: Error) => void,
    controller?: AbortController,
    onTimelineEvent?: (event: TimelineEvent) => void,
    lifecycleCallbacks?: RequestLifecycleCallbacks,
  ): Promise<void> {
    const requestController = controller ?? new AbortController();
    const inactivityTimeout = this.createInactivityTimeout(requestController, 'chat');
    let lifecycleFinished = false;
    const reportInterrupted = async (error?: Error) => {
      if (lifecycleFinished) return;
      lifecycleFinished = true;
      if (lifecycleCallbacks?.onDetached) await lifecycleCallbacks.onDetached(error);
      else await lifecycleCallbacks?.onTransportEof?.();
    };

    try {
      const response = await fetch(`${this.baseUrl}/v1/chat/completions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: 'qwen3-next',
          messages,
          temperature: options.temperature,
          top_p: options.top_p,
          top_k: options.top_k,
          repetition_penalty: options.repetition_penalty,
          max_tokens: options.max_tokens,
          stream: true,
          request_id: options.request_id,
          enable_thinking: options.enable_thinking,
          enable_local_memory: options.enable_local_memory === true,
          chat_history_start_ref: options.chat_history_start_ref,
          vision_automation: options.vision_automation,
          session_id: options.session_id,
        }),
        signal: requestController.signal,
      });

      if (options.session_id !== undefined) {
        const result = await readSessionHubStream(response, {
          expectedRequestId: options.request_id,
          requestKind: 'Chat',
          onActivity: inactivityTimeout.refresh,
          handlers: {
            onKnownRejection: lifecycleCallbacks?.onKnownRejection,
            onRequestAccepted: async (payload) => {
              const accepted = parseRequestAcceptedEvent(payload, options.request_id);
              if (accepted !== null) await lifecycleCallbacks?.onRequestAccepted?.(accepted);
            },
            onPayload: async (payload) => {
              if (payload.type === 'request_accepted') return;
              if (payload.type === 'timeline_event'
                && typeof payload.event === 'object'
                && payload.event !== null) {
                const event = payload.event as TimelineEvent;
                if (event.kind === 'message_delta'
                  && event.source === 'chat'
                  && event.display === 'chat_bubble') {
                  const text = typeof event.text === 'string' ? event.text : '';
                  onChunk(event.reasoning === true ? '' : text, event.reasoning === true ? text : undefined);
                } else {
                  onTimelineEvent?.(event);
                }
              } else if (payload.type === 'error') {
                onError(new Error(
                  typeof payload.error === 'string'
                    ? payload.error
                    : typeof payload.content === 'string' ? payload.content : 'Unknown error',
                ));
              }
            },
          },
        });
        if (result.kind === 'rejected') {
          lifecycleFinished = true;
          if (!lifecycleCallbacks?.onKnownRejection) onError(new Error(result.rejection.message));
          return;
        }
        if (result.kind === 'terminal') {
          onComplete();
          await this.reportStableTerminal(
            options.request_id,
            requestController.signal,
            lifecycleCallbacks,
          );
          lifecycleFinished = true;
          return;
        }
        if (lifecycleCallbacks?.onDetached) {
          await reportInterrupted(result.error);
        } else {
          if (result.error) onError(result.error);
          await reportInterrupted();
        }
        return;
      }

      if (!response.ok) {
        const rejection = await readKnownRequestRejection(response, 'Chat');
        lifecycleFinished = true;
        if (lifecycleCallbacks?.onKnownRejection) {
          await lifecycleCallbacks.onKnownRejection(rejection);
        } else {
          onError(new Error(rejection.message));
        }
        return;
      }

      if (!response.body) {
        throw new Error('Response body is empty');
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        inactivityTimeout.refresh();

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines[lines.length - 1];

        for (let i = 0; i < lines.length - 1; i++) {
          const line = lines[i].trim();
          if (!line || line === ':ping') continue;

          if (line.startsWith('data: ')) {
            const data = line.slice(6);
            if (data === '[DONE]') {
              onComplete();
              try {
                void Promise.resolve(reader.cancel()).catch(() => undefined);
              } catch {
                // Completion and lifecycle polling do not depend on stream cancellation.
              }
               await this.reportStableTerminal(
                options.request_id,
                requestController.signal,
                 lifecycleCallbacks,
               );
               lifecycleFinished = true;
               return;
            }

            let json: any;
            try {
              json = JSON.parse(data);
            } catch (error) {
              console.warn('Failed to parse SSE message:', error);
              continue;
            }
            const accepted = parseRequestAcceptedEvent(json, options.request_id);
            if (accepted !== null) {
              await lifecycleCallbacks?.onRequestAccepted?.(accepted);
            } else if (typeof json.kind === 'string' && json.kind.startsWith('compact_')) {
              inactivityTimeout.refresh();
              onTimelineEvent?.(json as TimelineEvent);
            } else if (json.choices?.[0]?.delta?.content !== undefined || json.choices?.[0]?.delta?.reasoning_content !== undefined) {
              inactivityTimeout.refresh();
              onChunk(
                json.choices[0].delta.content || '',
                json.choices[0].delta.reasoning_content,
              );
            }
          }
        }
      }

      await reportInterrupted();
    } catch (error) {
      const normalized = error instanceof Error
        ? error.name === 'AbortError' ? new Error('Request timeout or aborted') : error
        : new Error('Unknown error occurred');
      if (options.session_id !== undefined && lifecycleCallbacks?.onDetached) {
        try {
          await reportInterrupted(normalized);
        } catch (lifecycleError) {
          console.error('Chat request lifecycle recovery failed:', lifecycleError);
        }
        return;
      }
      if (error instanceof Error) {
        if (error.name === 'AbortError') {
          onError(new Error('Request timeout or aborted'));
        } else {
          onError(error);
        }
      } else {
        onError(new Error('Unknown error occurred'));
      }
      try {
        await reportInterrupted();
      } catch (lifecycleError) {
        console.error('Chat request lifecycle recovery failed:', lifecycleError);
      }
    } finally {
      inactivityTimeout.clear();
    }
  }

  async agentChatStream(
    request: AgentChatRequest,
    onStep: (step: AgentStep) => void,
    onContent: (content: string) => void,
    onComplete?: (sessionId?: string) => void,
    onError?: (error: Error) => void,
    onSessionStart?: (sessionId: string) => void,
    controller?: AbortController,
    onTimelineEvent?: (event: TimelineEvent) => void,
    lifecycleCallbacks?: RequestLifecycleCallbacks,
    onSessionEvent?: (event: AgentSessionStreamEvent) => void,
  ): Promise<void> {
    const requestController = controller ?? new AbortController();
    const inactivityTimeout = this.createInactivityTimeout(requestController, 'agent');
    let lifecycleFinished = false;
    const reportInterrupted = async (error?: Error) => {
      if (lifecycleFinished) return;
      lifecycleFinished = true;
      await lifecycleCallbacks?.onTransportEof?.();
      await lifecycleCallbacks?.onDetached?.(error);
    };

    try {
      const response = await fetch(`${this.baseUrl}/v1/agent/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: request.session_id,
          message: request.message,
          images: request.images,
          context_messages: request.context_messages,
          enable_thinking: request.enable_thinking,
          enable_local_memory: request.enable_local_memory === true,
          request_id: request.request_id,
          vision_automation: request.vision_automation,
          tool_configuration: request.tool_configuration,
          stream: true,
        }),
        signal: requestController.signal,
      });
      let sessionId: string | undefined;
      const result = await readSessionHubStream(response, {
        expectedRequestId: request.request_id,
        requestKind: 'Agent',
        onActivity: inactivityTimeout.refresh,
        handlers: {
          onKnownRejection: lifecycleCallbacks?.onKnownRejection,
          onRequestAccepted: async (payload) => {
            const accepted = parseRequestAcceptedEvent(payload, request.request_id);
            if (accepted !== null) await lifecycleCallbacks?.onRequestAccepted?.(accepted);
          },
          onPayload: async (payload) => {
            onSessionEvent?.(payload);
            if (payload.type === 'request_accepted') return;
            await dispatchAgentSessionPayload(payload, {
              onSessionStart: (eventSessionId) => {
                sessionId = eventSessionId;
                onSessionStart?.(eventSessionId);
              },
              onStep,
              onContent,
              onTimelineEvent,
              onDone: (eventSessionId) => onComplete?.(sessionId || eventSessionId),
              onPayloadError: onError,
            });
          },
        },
      });
      if (result.kind === 'rejected') {
        lifecycleFinished = true;
        if (!lifecycleCallbacks?.onKnownRejection) onError?.(new Error(result.rejection.message));
        return;
      }
      if (result.kind === 'terminal') {
        await this.reportStableTerminal(
          request.request_id,
          requestController.signal,
          lifecycleCallbacks,
        );
        lifecycleFinished = true;
        return;
      }
      const detachedError = result.error === undefined
        ? undefined
        : result.error.name === 'AbortError'
          ? new Error('Request timeout or aborted')
          : result.error;
      if (lifecycleCallbacks?.onDetached) {
        await reportInterrupted(detachedError);
      } else {
        if (detachedError) onError?.(detachedError);
        await reportInterrupted();
      }
    } catch (error) {
      const normalized = error instanceof Error
        ? (error.name === 'AbortError' ? new Error('Request timeout or aborted') : error)
        : new Error('Unknown error occurred');
      if (lifecycleCallbacks?.onDetached) {
        try {
          await reportInterrupted(normalized);
        } catch (lifecycleError) {
          console.error('Agent request lifecycle recovery failed:', lifecycleError);
        }
        return;
      }
      onError?.(normalized);
      try {
        await reportInterrupted();
      } catch (lifecycleError) {
        console.error('Agent request lifecycle recovery failed:', lifecycleError);
      }
    } finally {
      inactivityTimeout.clear();
    }
  }

  async childAgentStream(
    childRunId: string,
    onEvent: (event: ChildAgentStreamEvent) => void,
    onError?: (error: Error) => void,
    onComplete?: () => void,
    controller?: AbortController,
  ): Promise<void> {
    let completed = false;

    const complete = () => {
      if (!completed) {
        completed = true;
        onComplete?.();
      }
    };

    try {
      const response = await fetch(
        `${this.baseUrl}/v1/agent/child/stream?child_run_id=${encodeURIComponent(childRunId)}`,
        { method: 'GET', signal: controller?.signal },
      );

      if (!response.ok) {
        throw new Error(`Child agent stream failed: ${response.statusText}`);
      }

      if (!response.body) {
        throw new Error('Response body is empty');
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines[lines.length - 1];

        for (let i = 0; i < lines.length - 1; i++) {
          const line = lines[i].trim();
          if (!line || line === ':ping') continue;

          if (line.startsWith('data:')) {
            const data = line.slice(5).trimStart();
            if (data === '[DONE]') {
              complete();
              return;
            }

            const event = JSON.parse(data) as ChildAgentStreamEvent;
            onEvent(event);
          }
        }
      }

      complete();
    } catch (error) {
      const normalized = error instanceof Error ? error : new Error('Unknown error occurred');
      if (onError) {
        onError(normalized);
      } else {
        throw normalized;
      }
    }
  }

  async sessionHubEvents(
    sessionId: string,
    afterSequence: number,
    handlers: SessionHubPayloadHandlers,
    controller?: AbortController,
    requestKind: 'Agent' | 'Chat' = 'Agent',
  ): Promise<SessionHubReadResult> {
    if (!sessionId) throw new TypeError('sessionId is required');
    if (!Number.isInteger(afterSequence) || afterSequence < 0) {
      throw new RangeError('afterSequence must be a non-negative integer');
    }
    const response = await fetch(
      `${this.baseUrl}/v1/session/${encodeURIComponent(sessionId)}/events?after_sequence=${afterSequence}`,
      { method: 'GET', headers: { Accept: 'text/event-stream' }, signal: controller?.signal },
    );
    if (response.status === 404) {
      throw new Error('Session is unavailable or expired');
    }
    return readSessionHubStream(response, {
      afterSequence,
      requestKind,
      handlers,
    });
  }

  async chatSessionEvents(
    sessionId: string,
    afterSequence: number,
    handlers: ChatSessionResumeHandlers,
    controller?: AbortController,
  ): Promise<void> {
    let content = '';
    let reasoning = '';
    try {
      const result = await this.sessionHubEvents(sessionId, afterSequence, {
        onPayload: (payload) => {
          if (payload.type !== 'timeline_event'
            || typeof payload.event !== 'object'
            || payload.event === null) return;
          const event = payload.event as TimelineEvent;
          if (event.kind !== 'message_delta'
            || event.source !== 'chat'
            || event.display !== 'chat_bubble'
            || typeof event.text !== 'string') return;
          if (event.reasoning === true) {
            reasoning += event.text;
            handlers.onReasoning?.(reasoning);
          } else {
            content += event.text;
            handlers.onContent(content);
          }
        },
      }, controller, 'Chat');
      if (result.kind === 'terminal') handlers.onComplete();
      else if (result.kind === 'rejected') handlers.onError(new Error(result.rejection.message));
      else if (result.error) handlers.onError(result.error);
      else handlers.onDetached?.();
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        handlers.onDetached?.();
        return;
      }
      const normalized = error instanceof Error ? error : new Error('Unknown error occurred');
      handlers.onError(normalized);
    }
  }

  async agentSessionEvents(
    sessionId: string,
    afterSequence: number,
    callbacks: AgentSessionStreamCallbacks,
    controller?: AbortController,
  ): Promise<void> {
    try {
      const result = await this.sessionHubEvents(sessionId, afterSequence, {
        onRequestAccepted: async (payload) => {
          const accepted = parseRequestAcceptedEvent(payload);
          if (accepted !== null) await callbacks.onRequestAccepted?.(accepted);
        },
        onPayload: async (payload) => {
          callbacks.onEvent?.(payload);
          if (payload.type !== 'request_accepted') {
            await dispatchAgentSessionPayload(payload, callbacks);
          }
        },
      }, controller, 'Agent');
      if (result.kind === 'terminal') callbacks.onComplete?.();
      else if (result.kind === 'rejected') callbacks.onError?.(new Error(result.rejection.message));
      else if (result.error) callbacks.onError?.(result.error);
      else callbacks.onDetached?.();
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        callbacks.onDetached?.();
        return;
      }
      const normalized = error instanceof Error ? error : new Error('Unknown error occurred');
      if (callbacks.onError) callbacks.onError(normalized);
      else throw normalized;
    }
  }

  async abortChildAgentRun(childRunId: string): Promise<{ status: string; child_run_id: string }> {
    const response = await fetch(`${this.baseUrl}/v1/agent/child/abort`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ child_run_id: childRunId }),
    });
    if (!response.ok) {
      throw new Error(`Failed to abort child agent: ${response.statusText}`);
    }
    return response.json();
  }

  async processImageBinary(file: File, format: string = 'auto'): Promise<ImageProcessResponse> {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('format', format);

    const response = await fetch(`${this.baseUrl}/v1/process-image-binary`, {
      method: 'POST',
      body: formData,
    });

    if (!response.ok) {
      throw new Error(`Image processing failed: ${response.statusText}`);
    }

    return response.json();
  }

  async stopChat(requestId: string): Promise<any> {
    const response = await fetch(`${this.baseUrl}/v1/chat/stop`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_id: requestId }),
    });

    if (!response.ok) throw new Error('Failed to stop chat');
    return response.json();
  }

  async stopAgent(sessionId: string, expectedRequestId?: string): Promise<any> {
    const response = await fetch(`${this.baseUrl}/v1/agent/stop`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: sessionId,
        ...(expectedRequestId === undefined ? {} : { expected_request_id: expectedRequestId }),
      }),
    });

    if (!response.ok) throw new Error('Failed to stop agent');
    return response.json();
  }

  async stopAllAgents(): Promise<any> {
    const response = await fetch(`${this.baseUrl}/v1/agent/stop-all`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });

    if (!response.ok) throw new Error('Failed to stop all agents');
    return response.json();
  }

  async answerNow(sessionId: string): Promise<any> {
    const response = await fetch(`${this.baseUrl}/v1/agent/answer-now`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId }),
    });

    if (!response.ok) throw new Error('Failed to trigger answer now');
    return response.json();
  }

  async answerNowStream(
    sessionId: string,
    requestId: string,
    onContent: (content: string) => void,
    onComplete?: (sessionId?: string) => void,
    onError?: (error: Error) => void,
    onSessionStart?: (sessionId: string) => void,
    controller?: AbortController,
    lifecycleCallbacks?: RequestLifecycleCallbacks,
    sessionHandlers?: AgentSessionPayloadHandlers,
  ): Promise<void> {
    if (!isValidRequestId(requestId)) {
      throw new TypeError('requestId must match the backend lifecycle grammar');
    }
    const requestController = controller ?? new AbortController();
    const inactivityTimeout = this.createInactivityTimeout(requestController, 'agent');
    let lifecycleFinished = false;
    const reportInterrupted = async (error?: Error) => {
      if (lifecycleFinished) return;
      lifecycleFinished = true;
      await lifecycleCallbacks?.onTransportEof?.();
      await lifecycleCallbacks?.onDetached?.(error);
    };

    try {
      const response = await fetch(`${this.baseUrl}/v1/agent/answer-now/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, request_id: requestId }),
        signal: requestController.signal,
      });
      let startedSessionId: string | undefined;
      let replacementAccepted = false;
      let replacementTerminalSessionId: string | undefined;
      let bufferedOriginalTerminalSessionId: string | undefined;
      const result = await readSessionHubStream(response, {
        expectedRequestId: requestId,
        requestKind: 'Agent',
        keepReadingAfterTerminal: true,
        onActivity: inactivityTimeout.refresh,
        handlers: {
          onKnownRejection: async (rejection) => {
            await lifecycleCallbacks?.onKnownRejection?.(rejection);
            // A superseded generation may have reached its stable terminal
            // while replacement admission was unresolved. Re-apply it through
            // the ordinary terminal path only after definitive rejection.
            if (!replacementAccepted && bufferedOriginalTerminalSessionId !== undefined) {
              const originalTerminal = bufferedOriginalTerminalSessionId;
              bufferedOriginalTerminalSessionId = undefined;
              await sessionHandlers?.onDone?.(originalTerminal);
              onComplete?.(originalTerminal);
            }
          },
          onRequestAccepted: async (payload) => {
            const event = parseRequestAcceptedEvent(payload, requestId);
            if (event === null) return;
            replacementAccepted = true;
            await lifecycleCallbacks?.onRequestAccepted?.(event);
            await sessionHandlers?.onRequestAccepted?.(event);
          },
          onPayload: async (payload) => {
            if (payload.type === 'request_accepted') return;
            await dispatchAgentSessionPayload(payload, {
              onSessionStart: async (eventSessionId) => {
                startedSessionId = eventSessionId;
                onSessionStart?.(eventSessionId);
                await sessionHandlers?.onSessionStart?.(eventSessionId);
              },
              onStep: async (step) => { await sessionHandlers?.onStep?.(step); },
              onContent: async (content) => {
                onContent(content);
                await sessionHandlers?.onContent?.(content);
              },
              onTimelineEvent: async (event) => { await sessionHandlers?.onTimelineEvent?.(event); },
              onDone: async (eventSessionId) => {
                const terminalSessionId = startedSessionId || eventSessionId;
                if (!replacementAccepted) {
                  bufferedOriginalTerminalSessionId = terminalSessionId;
                } else if (typeof payload.request_id !== 'string' || payload.request_id === requestId) {
                  await sessionHandlers?.onDone?.(terminalSessionId);
                  replacementTerminalSessionId = terminalSessionId;
                }
              },
              onPayloadError: async (error) => {
                onError?.(error);
                await sessionHandlers?.onPayloadError?.(error);
              },
            });
          },
        },
      });
      if (result.kind === 'rejected') {
        lifecycleFinished = true;
        if (!lifecycleCallbacks?.onKnownRejection) onError?.(new Error(result.rejection.message));
        return;
      }
      if (result.kind === 'terminal' && replacementTerminalSessionId !== undefined) {
        onComplete?.(replacementTerminalSessionId);
        await this.reportStableTerminal(
          requestId,
          requestController.signal,
          lifecycleCallbacks,
        );
        lifecycleFinished = true;
        return;
      }
      const detachedError = result.kind === 'detached' ? result.error : undefined;
      if (lifecycleCallbacks?.onDetached) await reportInterrupted(detachedError);
      else {
        if (detachedError) onError?.(detachedError);
        await reportInterrupted();
      }
    } catch (error) {
      const normalized = error instanceof Error
        ? (error.name === 'AbortError' ? new Error('Request timeout or aborted') : error)
        : new Error('Unknown error occurred');
      if (lifecycleCallbacks?.onDetached) {
        try {
          await reportInterrupted(normalized);
        } catch (lifecycleError) {
          console.error('Answer Now request lifecycle recovery failed:', lifecycleError);
        }
        return;
      }
      onError?.(normalized);
      try {
        await reportInterrupted();
      } catch (lifecycleError) {
        console.error('Answer Now request lifecycle recovery failed:', lifecycleError);
      }
    } finally {
      inactivityTimeout.clear();
    }
  }

  async startCycle(
    request: import('./cycle-types.ts').CycleStartRequest,
    onEvent: (event: import('./cycle-types.ts').CycleSSEEvent) => void,
    onComplete: () => void,
    onError: (error: Error) => void,
  ): Promise<AbortController> {
    const controller = new AbortController();
    const frozenRequest = { ...request, enable_local_memory: request.enable_local_memory === true };
    void this.followCycle(frozenRequest, request.cycle_run_id ?? request.session_id, null, 0,
      onEvent, onComplete, onError, controller.signal);
    return controller;
  }

  /** Rebuild a page from the retained journal after refresh. */
  resumeCycle(
    cycleRunId: string,
    requestFingerprint: string,
    afterSequence: number,
    onEvent: (event: import('./cycle-types.ts').CycleSSEEvent) => void,
    onComplete: () => void,
    onError: (error: Error) => void,
  ): AbortController {
    const controller = new AbortController();
    void this.followCycle(null, cycleRunId, requestFingerprint, afterSequence,
      onEvent, onComplete, onError, controller.signal);
    return controller;
  }

  private async followCycle(
    request: import('./cycle-types.ts').CycleStartRequest | null,
    cycleRunId: string | undefined,
    initialFingerprint: string | null,
    initialCursor: number,
    onEvent: (event: import('./cycle-types.ts').CycleSSEEvent) => void,
    onComplete: () => void,
    onError: (error: Error) => void,
    signal: AbortSignal,
  ): Promise<void> {
    let fingerprint = initialFingerprint;
    let cursor = initialCursor;
    let started = request === null;
    let retry = 0;
    let terminal = false;
    const deliver = (event: import('./cycle-types.ts').CycleSSEEvent) => {
      if (typeof event.event_sequence === 'number') {
        if (event.event_sequence <= cursor) return;
        cursor = event.event_sequence;
      }
      cycleRunId = event.cycle_run_id ?? event.session_id ?? cycleRunId;
      if (event.request_fingerprint) fingerprint = event.request_fingerprint;
      onEvent(event);
      if (['cycle_completed', 'cycle_stopped', 'cycle_error', 'cycle_expired'].includes(event.type)) terminal = true;
    };
    while (!signal.aborted) {
      try {
        let response: Response;
        if (!started && request) {
          response = await fetch(`${this.baseUrl}/v1/cycle/start`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...request, enable_local_memory: request.enable_local_memory === true }), signal,
          });
          if (!response.ok) throw parseCycleRecoveryError(response.status, await response.text());
          started = true;
        } else {
          if (!cycleRunId) throw new Error('Cycle run ID is unavailable for reconnection');
          if (!fingerprint) {
            const snapshot = await this.getCycleStatus(cycleRunId, signal);
            fingerprint = snapshot.request_fingerprint;
          }
          const query = new URLSearchParams({ after_sequence: String(cursor), request_fingerprint: fingerprint });
          response = await fetch(`${this.baseUrl}/v1/cycle/${encodeURIComponent(cycleRunId)}/events?${query}`, { signal });
          if (!response.ok) {
            const detail = await response.text().catch(() => '');
            throw parseCycleRecoveryError(response.status, detail);
          }
        }
        if (!response.body) throw new Error('Cycle SSE response body is empty');
        retry = 0;
        const done = await this.readCycleStream(response.body, signal, deliver);
        if (done || terminal) { onComplete(); return; }
      } catch (error) {
        if (signal.aborted) return;
        const failure = error instanceof Error ? error : new Error(String(error));
        if (failure instanceof CycleRecoveryError && !failure.retryable) { onError(failure); return; }
      }
      if (terminal) { onComplete(); return; }
      // Producer stays alive while a subscriber is gone. Bounded backoff avoids
      // recursive reconnect storms when the proxy repeatedly closes the SSE.
      retry = Math.min(retry + 1, 5);
      await new Promise<void>((resolve) => {
        const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, Math.min(1000 * 2 ** (retry - 1), 10000));
        const abort = () => { clearTimeout(timer); resolve(); };
        signal.addEventListener('abort', abort, { once: true });
      });
    }
  }

  async stopCycle(sessionId: string): Promise<any> {
    const response = await fetch(`${this.baseUrl}/v1/cycle/stop`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cycle_run_id: sessionId }),
    });

    if (!response.ok) throw new Error('Failed to stop cycle');
    return response.json();
  }

  async attachCycle(
    cycleRunId: string,
    requestFingerprint: string,
    afterSequence: number,
    onEvent: (event: import('./cycle-types.ts').CycleSSEEvent) => void,
    onComplete: () => void,
    onError?: (error: Error) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    const query = new URLSearchParams({ after_sequence: String(afterSequence), request_fingerprint: requestFingerprint });
    const response = await fetch(`${this.baseUrl}/v1/cycle/${encodeURIComponent(cycleRunId)}/events?${query.toString()}`, { signal });
    if (!response.ok) {
      const detail = await response.text().catch(() => '');
      throw parseCycleRecoveryError(response.status, detail);
    }
    if (!response.body) throw new Error('Cycle attach response body is empty');
    try {
      const done = await this.readCycleStream(response.body, signal, onEvent);
      if (done) onComplete();
      else if (!signal?.aborted) onError?.(new Error('Cycle SSE disconnected'));
    } catch (error) {
      if (!signal?.aborted) onError?.(error instanceof Error ? error : new Error(String(error)));
    }
  }

  async getCycleStatus(cycleRunId: string, signal?: AbortSignal): Promise<{ cycle_run_id: string; status: string; event_sequence: number; request_fingerprint: string }> {
    const response = await fetch(`${this.baseUrl}/v1/cycle/${encodeURIComponent(cycleRunId)}`, { signal });
    if (!response.ok) throw parseCycleRecoveryError(response.status, await response.text().catch(() => ''));
    return response.json();
  }

  private async readCycleStream(
    body: ReadableStream<Uint8Array>,
    signal: AbortSignal | undefined,
    onEvent: (event: import('./cycle-types.ts').CycleSSEEvent) => void,
  ): Promise<boolean> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      while (!signal?.aborted) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        buffer = buffer.replace(/\r\n/g, '\n');
        const frames = buffer.split('\n\n');
        buffer = frames.pop() ?? '';
        for (const frame of frames) {
          const line = frame.split('\n').find((part) => part.startsWith('data: '));
          if (!line) continue; // heartbeat comments are transport-only.
          const data = line.slice(6);
          if (data === '[DONE]') return true;
          try { onEvent(JSON.parse(data)); } catch { console.warn('Failed to parse Cycle SSE message'); }
        }
      }
      return false;
    } finally {
      void Promise.resolve(reader.cancel()).catch(() => undefined);
    }
  }

  /**
   * Run the manual /v1/compact/run endpoint. Streams compact_* SSE events
   * back to `onEvent`. The promise resolves when the server emits
   * `data: [DONE]`. Throws on non-2xx status or stream errors.
   *
   * The dynamic import keeps the compact-client module out of the
   * initial bundle; it is only needed when the user actually runs
   * /compact.
   */
  async runCompact(
    body: {
      mode: 'chat' | 'agent' | 'cycle';
      session_id?: string | null;
      messages?: Array<{ role: string; content: unknown; metadata?: Record<string, unknown> }> | null;
      custom_instructions?: string | null;
    },
    onEvent: (ev: unknown) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    const { runCompactSSE } = await import('./compact-client.ts');
    return runCompactSSE({
      url: `${this.baseUrl}/v1/compact/run`,
      body,
      onEvent,
      signal,
    });
  }
}

// ============================================================
// 导出实例和函数
// ============================================================

export const llmClient = new LLMClient();

export async function stopChat(requestId: string): Promise<any> {
  return llmClient.stopChat(requestId);
}

export async function stopAgent(sessionId: string, expectedRequestId?: string): Promise<any> {
  return llmClient.stopAgent(sessionId, expectedRequestId);
}

export async function stopAllAgents(): Promise<any> {
  return llmClient.stopAllAgents();
}

export async function answerNow(sessionId: string): Promise<any> {
  return llmClient.answerNow(sessionId);
}

export async function answerNowStream(
  sessionId: string,
  requestId: string,
  onContent: (content: string) => void,
  onComplete?: (sessionId?: string) => void,
  onError?: (error: Error) => void,
  onSessionStart?: (sessionId: string) => void,
  controller?: AbortController,
  lifecycleCallbacks?: RequestLifecycleCallbacks,
  sessionHandlers?: AgentSessionPayloadHandlers,
): Promise<void> {
  return llmClient.answerNowStream(
    sessionId,
    requestId,
    onContent,
    onComplete,
    onError,
    onSessionStart,
    controller,
    lifecycleCallbacks,
    sessionHandlers,
  );
}

export default llmClient;
