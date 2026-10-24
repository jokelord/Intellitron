import {
  MAX_AUTOMATION_SOURCE_COUNT,
  MAX_MANUAL_IMAGE_COUNT,
  MAX_REAL_IMAGES_PER_TURN,
} from "./bundle-sampler.ts";
import type { AgentToolConfigurationSnapshot } from "../agent-tool-configuration.ts";

export type RequestLifecycleStatus = "unknown" | "accepted" | "running" | "terminal";

export interface VisionAutomationTransportBinding {
  page_generation: number;
  mode: "chat" | "agent";
  top_level_context_id: string;
  automation_task_id: string;
  capture_cycle_id: string;
  cleanup_token?: string;
}

export interface AgentVisionAutomationAbortRequest {
  session_id: string;
  binding: Omit<VisionAutomationTransportBinding, "cleanup_token"> & { cleanup_token: null };
  cleanup_token: string;
}

export interface AgentVisionAutomationAbortResponse {
  status: "cleanup_pending";
  session_id: string;
}

export interface AgentVisionAutomationAbortCancellationResponse {
  status: "cleanup_cancelled";
  session_id: string;
}

export interface AgentVisionAutomationAbortInput {
  sessionId: string;
  pageGeneration: number;
  automationTaskId: string;
  captureCycleId: string;
  cleanupToken: string;
}

export interface VisionAutomationTransportBindingInput {
  pageGeneration: number;
  mode: "chat" | "agent";
  topLevelContextId: string;
  automationTaskId: string;
  captureCycleId: string;
  cleanupToken?: string;
}

export interface VisionAutomationTransportMetadata {
  history_image_policy: "aggregate_prior_image_blocks";
  manual_image_count: number;
  source_count: number;
  retained_tick_count: number;
  binding: VisionAutomationTransportBinding;
}

export interface VisionAutomationRequestContractInput {
  requestId: string;
  manualImageCount: number;
  sourceCount: number;
  retainedTickCount: number;
  binding: VisionAutomationTransportBindingInput;
}

export interface VisionAutomationRequestContract {
  requestId: string;
  visionAutomation: VisionAutomationTransportMetadata;
}

export interface RequestAcceptedEvent {
  type: "request_accepted";
  requestId: string;
  sessionId?: string;
  toolConfiguration?: AgentToolConfigurationSnapshot;
}

export interface RequestStatusResult {
  requestId: string;
  status: RequestLifecycleStatus;
}

export class RequestAcceptanceIdentityError extends Error {}

const REQUEST_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._~:-]*$/;
const MIN_REQUEST_ID_LENGTH = 8;
const MAX_REQUEST_ID_LENGTH = 128;
const AUTOMATION_LIFECYCLE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._~:-]{15,127}$/;
const MAX_SAFE_PAGE_GENERATION = Number.MAX_SAFE_INTEGER;

export function isValidRequestId(value: string): boolean {
  return value.length >= MIN_REQUEST_ID_LENGTH
    && value.length <= MAX_REQUEST_ID_LENGTH
    && REQUEST_ID_PATTERN.test(value);
}

export function isValidAutomationLifecycleId(value: string): boolean {
  return AUTOMATION_LIFECYCLE_ID_PATTERN.test(value);
}

function buildTransportBinding(
  binding: VisionAutomationTransportBindingInput,
): VisionAutomationTransportBinding {
  if (!Number.isSafeInteger(binding.pageGeneration)
    || binding.pageGeneration < 0
    || binding.pageGeneration > MAX_SAFE_PAGE_GENERATION) {
    throw new RangeError("pageGeneration must be a non-negative safe integer");
  }
  if (binding.mode !== "chat" && binding.mode !== "agent") {
    throw new TypeError("mode must be chat or agent");
  }
  const contextBytes = new TextEncoder().encode(binding.topLevelContextId).byteLength;
  if (binding.topLevelContextId !== binding.topLevelContextId.trim()
    || contextBytes < 1
    || contextBytes > 256
    || Array.from(binding.topLevelContextId).some((character) => {
      const code = character.codePointAt(0) ?? 0;
      return code < 0x20 || code === 0x7f;
    })) {
    throw new TypeError("topLevelContextId must be a bounded trimmed string without controls");
  }
  const lifecycleIds: Array<[string, string | undefined]> = [
    ["automationTaskId", binding.automationTaskId],
    ["captureCycleId", binding.captureCycleId],
    ["cleanupToken", binding.cleanupToken],
  ];
  lifecycleIds.forEach(([name, value]) => {
    if (value !== undefined && !isValidAutomationLifecycleId(value)) {
      throw new TypeError(`${name} must use the high-entropy lifecycle grammar`);
    }
  });
  return {
    page_generation: binding.pageGeneration,
    mode: binding.mode,
    top_level_context_id: binding.topLevelContextId,
    automation_task_id: binding.automationTaskId,
    capture_cycle_id: binding.captureCycleId,
    ...(binding.cleanupToken === undefined ? {} : { cleanup_token: binding.cleanupToken }),
  };
}

function requireInteger(name: string, value: number, minimum: number, maximum: number): void {
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw new RangeError(`${name} must be an integer between ${minimum} and ${maximum}`);
  }
}

export function buildVisionAutomationRequestContract(
  input: VisionAutomationRequestContractInput,
): VisionAutomationRequestContract {
  if (!isValidRequestId(input.requestId)) {
    throw new TypeError("requestId must match the backend lifecycle grammar");
  }
  requireInteger("manualImageCount", input.manualImageCount, 0, MAX_MANUAL_IMAGE_COUNT);
  requireInteger("sourceCount", input.sourceCount, 0, MAX_AUTOMATION_SOURCE_COUNT);
  requireInteger("retainedTickCount", input.retainedTickCount, 0, MAX_REAL_IMAGES_PER_TURN);
  if (
    (input.sourceCount === 0 && input.retainedTickCount !== 0)
    || (input.sourceCount > 0 && input.retainedTickCount === 0)
  ) {
    throw new RangeError("sourceCount and retainedTickCount must both describe complete bundles");
  }
  const imageCount = input.manualImageCount + input.sourceCount * input.retainedTickCount;
  if (imageCount > MAX_REAL_IMAGES_PER_TURN) {
    throw new RangeError(`Declared real image count cannot exceed ${MAX_REAL_IMAGES_PER_TURN}`);
  }
  return {
    requestId: input.requestId,
    visionAutomation: {
      history_image_policy: "aggregate_prior_image_blocks",
      manual_image_count: input.manualImageCount,
      source_count: input.sourceCount,
      retained_tick_count: input.retainedTickCount,
      binding: buildTransportBinding(input.binding),
    },
  };
}

export function buildAgentVisionAutomationAbortRequest(
  input: AgentVisionAutomationAbortInput,
): AgentVisionAutomationAbortRequest {
  const binding = buildTransportBinding({
    pageGeneration: input.pageGeneration,
    mode: "agent",
    topLevelContextId: input.sessionId,
    automationTaskId: input.automationTaskId,
    captureCycleId: input.captureCycleId,
  });
  if (!isValidAutomationLifecycleId(input.cleanupToken)) {
    throw new TypeError("cleanupToken must use the high-entropy lifecycle grammar");
  }
  return {
    session_id: input.sessionId,
    binding: { ...binding, cleanup_token: null },
    cleanup_token: input.cleanupToken,
  };
}

export function parseRequestAcceptedEvent(
  value: unknown,
  expectedRequestId?: string,
): RequestAcceptedEvent | null {
  if (typeof value !== "object" || value === null) return null;
  const event = value as Record<string, unknown>;
  if (event.type !== "request_accepted") return null;
  if (typeof event.request_id !== "string" || !event.request_id) {
    throw new RequestAcceptanceIdentityError("request_accepted event is missing request_id");
  }
  if (expectedRequestId !== undefined && event.request_id !== expectedRequestId) {
    throw new RequestAcceptanceIdentityError(
      `Accepted request ID ${event.request_id} does not match ${expectedRequestId}`,
    );
  }
  const sessionId = event.session_id;
  if (sessionId !== undefined && (typeof sessionId !== "string" || sessionId.length === 0)) {
    throw new RequestAcceptanceIdentityError("request_accepted session_id must be a non-empty string");
  }
  let toolConfiguration: AgentToolConfigurationSnapshot | undefined;
  if (event.tool_configuration !== undefined) {
    if (typeof event.tool_configuration !== "object" || event.tool_configuration === null || Array.isArray(event.tool_configuration)) {
      throw new RequestAcceptanceIdentityError("request_accepted tool_configuration must be an object");
    }
    const raw = event.tool_configuration as Record<string, unknown>;
    const isStringArray = (value: unknown): value is string[] => Array.isArray(value) && value.every((item) => typeof item === "string");
    if (raw.locked !== true
      || typeof raw.schema_version !== "number"
      || typeof raw.catalog_revision !== "string"
      || typeof raw.fingerprint !== "string"
      || !isStringArray(raw.manager_agent_tools)
      || !isStringArray(raw.basic_agent_tools)) {
      throw new RequestAcceptanceIdentityError("request_accepted tool_configuration metadata is invalid");
    }
    toolConfiguration = {
      locked: true,
      schema_version: raw.schema_version,
      catalog_revision: raw.catalog_revision,
      fingerprint: raw.fingerprint,
      manager_agent_tools: [...raw.manager_agent_tools],
      basic_agent_tools: [...raw.basic_agent_tools],
    };
  }
  return {
    type: "request_accepted",
    requestId: event.request_id,
    ...(sessionId === undefined ? {} : { sessionId }),
    ...(toolConfiguration === undefined ? {} : { toolConfiguration }),
  };
}

export function requestStatusConfirmsAcceptance(status: RequestLifecycleStatus): boolean {
  return status !== "unknown";
}

export function parseRequestStatusResponse(
  value: unknown,
  expectedRequestId: string,
): RequestStatusResult {
  if (typeof value !== "object" || value === null) {
    throw new TypeError("Invalid request status response");
  }
  const response = value as Record<string, unknown>;
  if (response.request_id !== expectedRequestId) {
    throw new RequestAcceptanceIdentityError("Request status identity does not match query");
  }
  if (
    response.status !== "unknown"
    && response.status !== "accepted"
    && response.status !== "running"
    && response.status !== "terminal"
  ) {
    throw new TypeError("Invalid request lifecycle status");
  }
  return { requestId: expectedRequestId, status: response.status };
}
