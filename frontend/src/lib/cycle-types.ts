// Cycle 可编程序列的 TypeScript 类型定义

// ── Step Types (对应后端 CycleStepRequest) ──

export interface CycleWaitStep {
  type: "wait";
  wait: number;
}

export interface CycleMessageStep {
  type: "message";
  message: string;
  photo?: number[];
}

export interface CycleCycleStep {
  type: "cycle";
  cycle_count: number;
  cycle_steps: CycleStep[];
}

export type CycleStep = CycleWaitStep | CycleMessageStep | CycleCycleStep;

// ── Request/Response Models ──

export interface CycleStartRequest {
  session_id?: string;
  steps: CycleStep[];
  agent_mode: boolean;
  share_context: boolean;
}

export interface CycleStopRequest {
  session_id: string;
}

export interface Camera {
  index: number;
  device: string;
  name?: string;
}

export interface CameraListResponse {
  cameras: Camera[];
}

// ── SSE Event Types ──

export interface CycleStartedEvent {
  type: "cycle_started";
  session_id: string;
  total_steps: number;
}

export interface StepBeginEvent {
  type: "step_begin";
  path: number[];
  step_type: string;
  detail: Record<string, unknown>;
}

export interface PhotoCapturedEvent {
  type: "photo_captured";
  camera_index: number;
  image_base64?: string;
  error?: string;
}

export interface LlmChunkEvent {
  type: "llm_chunk";
  content: string;
  role: string;
}

export interface LlmDoneEvent {
  type: "llm_done";
  content: string;
  path: number[];
}

export interface StepEndEvent {
  type: "step_end";
  path: number[];
  duration: number;
}

export interface CycleCompletedEvent {
  type: "cycle_completed";
  total_duration: number;
}

export interface CycleErrorEvent {
  type: "cycle_error";
  error: string;
  path: number[];
}

export interface CycleStoppedEvent {
  type: "cycle_stopped";
  reason: string;
}

export type CycleSSEEvent =
  | CycleStartedEvent
  | StepBeginEvent
  | PhotoCapturedEvent
  | LlmChunkEvent
  | LlmDoneEvent
  | StepEndEvent
  | CycleCompletedEvent
  | CycleErrorEvent
  | CycleStoppedEvent;

// ── UI State Types ──

export interface CycleEditorState {
  steps: CycleStep[];
  shareContext: boolean;
}

export type CycleExecutionStatus =
  | "idle"
  | "running"
  | "completed"
  | "stopped"
  | "error";

export interface CycleExecutionState {
  status: CycleExecutionStatus;
  sessionId: string | null;
  currentPath: number[];
  events: CycleSSEEvent[];
}

// ── Default Step Factories ──

export function createWaitStep(seconds: number = 5): CycleWaitStep {
  return { type: "wait", wait: seconds };
}

export function createMessageStep(
  message: string = "",
  photo?: number[]
): CycleMessageStep {
  return { type: "message", message, ...(photo ? { photo } : {}) };
}

export function createCycleStep(
  count: number = 3,
  steps: CycleStep[] = []
): CycleCycleStep {
  return { type: "cycle", cycle_count: count, cycle_steps: steps };
}
