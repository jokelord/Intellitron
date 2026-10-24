import type { VisionAutomationServerConfig } from "./config.ts";
import type { CaptureSource } from "./capture-source.ts";
import type {
  HistoricalImageBlockContext,
  HistoricalImageBlockDescriptor,
} from "./history-omission.ts";
import type { HistoricalImageSourceAlias, Message } from "../message-types.ts";
import type { RequestStatusResult } from "./send-contract.ts";
import type { SourceDescriptor } from "./types.ts";
import type { SessionAttachmentToken } from "../session-hub-client.ts";
import { abortStoppedConversation } from "../conversation/conversation-stop.ts";

export const VISION_PAGE_GENERATION_HIGH_WATER_KEY =
  "llm-deployment.vision-page-generation.v1";

export interface VisionPageGenerationStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export function projectVisionTurnImages(
  images: readonly string[],
  automationManualImageCount?: number,
): {
  bubbleImages: string[];
  consumedManualImages: string[];
} {
  const bubbleImages = [...images];
  const hasValidManualCount = typeof automationManualImageCount === "number"
    && Number.isInteger(automationManualImageCount)
    && automationManualImageCount >= 0;
  return {
    bubbleImages,
    consumedManualImages: hasValidManualCount
      ? bubbleImages.slice(0, automationManualImageCount)
      : [...bubbleImages],
  };
}

export function createDurableVisionPageGenerationAllocator(
  storage?: VisionPageGenerationStorage,
  now: () => number = Date.now,
): () => number {
  let localHighWater = -1;
  return () => {
    let storedHighWater = -1;
    try {
      const raw = storage?.getItem(VISION_PAGE_GENERATION_HIGH_WATER_KEY) ?? null;
      if (raw !== null) {
        const parsed = Number(raw);
        if (Number.isSafeInteger(parsed) && parsed >= 0) storedHighWater = parsed;
      }
    } catch {
      // The closure remains monotonic when browser storage is unavailable.
    }

    const timestamp = now();
    const safeTimestamp = Number.isSafeInteger(timestamp) && timestamp >= 0 ? timestamp : 0;
    const highWater = Math.max(localHighWater, storedHighWater);
    if (highWater >= Number.MAX_SAFE_INTEGER) {
      throw new RangeError("Vision page generation high-water is exhausted");
    }
    const generation = Math.max(highWater + 1, safeTimestamp);
    localHighWater = generation;
    try {
      storage?.setItem(VISION_PAGE_GENERATION_HIGH_WATER_KEY, String(generation));
    } catch {
      // Keep the in-memory high-water for this page when persistence fails.
    }
    return generation;
  };
}

export function createAutomationHubLifecycle(input: {
  requestId: string;
  onAccepted(requestId: string): void | Promise<void>;
  onStableTerminal(result: RequestStatusResult & { status: "terminal" }): void | Promise<void>;
}): {
  onRequestAccepted(event: { requestId: string }): Promise<void>;
  onStableTerminal(result: RequestStatusResult): Promise<void>;
} {
  let accepted = false;
  let terminal = false;
  return {
    async onRequestAccepted(event) {
      if (event.requestId !== input.requestId || accepted) return;
      // Claim ownership before awaiting page callbacks. Callbacks can re-enter
      // this lifecycle synchronously (for example through a state projection).
      accepted = true;
      await input.onAccepted(event.requestId);
    },
    async onStableTerminal(result) {
      if (result.requestId !== input.requestId || result.status !== "terminal" || terminal) return;
      // As with acceptance, settle before awaiting to make duplicate terminal
      // notifications harmless even when the callback re-enters us.
      terminal = true;
      await input.onStableTerminal(result as RequestStatusResult & { status: "terminal" });
    },
  };
}

interface PendingMutation<T> {
  value: T;
  accepted: boolean;
}

export class AcceptanceMutationRegistry<T> {
  private readonly pending = new Map<string, PendingMutation<T>>();

  stage(requestId: string, create: () => T): T {
    const existing = this.pending.get(requestId);
    if (existing !== undefined) return existing.value;
    const value = create();
    this.pending.set(requestId, { value, accepted: false });
    return value;
  }

  has(requestId: string): boolean {
    return this.pending.has(requestId);
  }

  isAccepted(requestId: string): boolean {
    return this.pending.get(requestId)?.accepted === true;
  }

  accept(requestId: string, apply: (value: T) => void): boolean {
    const mutation = this.pending.get(requestId);
    if (mutation === undefined || mutation.accepted) return false;
    apply(mutation.value);
    mutation.accepted = true;
    return true;
  }

  delete(requestId: string): boolean {
    return this.pending.delete(requestId);
  }

  clear(): void {
    this.pending.clear();
  }
}

export interface VisionSourceAcquisition {
  source: SourceDescriptor;
  acquire: () => Promise<CaptureSource>;
}

export interface VisionSourceAcquisitionFailure {
  source: SourceDescriptor;
  error: unknown;
}

export interface AvailableVisionSources {
  sources: readonly CaptureSource[];
  failures: readonly VisionSourceAcquisitionFailure[];
}

export async function acquireAvailableVisionSources(
  acquisitions: readonly VisionSourceAcquisition[],
  signal: AbortSignal,
  isCurrent: () => boolean,
): Promise<AvailableVisionSources> {
  const settled = await Promise.allSettled(acquisitions.map((entry) => (
    Promise.resolve().then(entry.acquire)
  )));
  const sources: CaptureSource[] = [];
  const failures: VisionSourceAcquisitionFailure[] = [];
  settled.forEach((result, index) => {
    if (result.status === "fulfilled") sources.push(result.value);
    else failures.push({ source: acquisitions[index].source, error: result.reason });
  });
  if (signal.aborted || !isCurrent()) {
    sources.forEach((source) => source.dispose());
    throw signal.reason instanceof Error
      ? signal.reason
      : new Error("Vision source acquisition became stale or was aborted");
  }
  if (sources.length === 0) {
    throw new Error(failures.length === 0
      ? "No Vision automation source is available"
      : `No Vision automation source could start: ${failures.map((failure) => (
          failure.error instanceof Error ? failure.error.message : String(failure.error)
        )).join("; ")}`);
  }
  return { sources, failures };
}

export function assertBoundAgentSessionId(expected: string, returned?: string): string {
  if (returned !== undefined && returned !== expected) {
    throw new Error(`Agent session identity violation: expected ${expected}, received ${returned}`);
  }
  return expected;
}

export interface AgentSessionIdentity {
  readonly sessionId: string;
  readonly token: symbol;
}

export class AgentSessionOwnership {
  private current: AgentSessionIdentity | null = null;

  bind(sessionId: string): AgentSessionIdentity {
    if (this.current?.sessionId === sessionId) return this.current;
    const identity = Object.freeze({ sessionId, token: Symbol("agent-session") });
    this.current = identity;
    return identity;
  }

  detach(): AgentSessionIdentity | null {
    const identity = this.current;
    this.current = null;
    return identity;
  }

  canDelete(identity: AgentSessionIdentity): boolean {
    return this.current?.sessionId !== identity.sessionId
      || this.current.token === identity.token;
  }

  get sessionId(): string | null {
    return this.current?.sessionId ?? null;
  }
}

export function abortPageTransportBeforeRetirement(
  controller: AbortController | null,
  retire: () => Promise<void>,
): Promise<void> {
  controller?.abort(new DOMException("Vision page context replaced", "AbortError"));
  return retire();
}

export function disposeAutomationTerminalEntry<T extends { gate: AutomationTerminalGate }>(
  terminalEntries: Map<string, T>,
  requestId: string,
  expectedGate?: AutomationTerminalGate,
): boolean {
  const entry = terminalEntries.get(requestId);
  if (entry === undefined || (expectedGate !== undefined && entry.gate !== expectedGate)) return false;
  entry.gate.dispose();
  terminalEntries.delete(requestId);
  return true;
}

/** A known pre-accept cleanup rejection must not enter request-status recovery. */
export function settleKnownCleanupRejection<T extends { gate: AutomationTerminalGate }>(input: {
  isCurrentAttempt: boolean;
  terminalEntries: Map<string, T>;
  requestId: string;
  stage: { finishAttempt(): void };
  message: string;
  onVisibleError(message: string): void;
}): boolean {
  if (!input.isCurrentAttempt) return false;
  input.stage.finishAttempt();
  disposeAutomationTerminalEntry(input.terminalEntries, input.requestId);
  input.onVisibleError(input.message);
  return true;
}

export function disposeAutomationTerminalEntries<T extends { gate: AutomationTerminalGate }>(
  terminalEntries: Map<string, T>,
): void {
  terminalEntries.forEach((entry) => entry.gate.dispose());
  terminalEntries.clear();
}

export function retireAutomationTransportOwnership<T extends { gate: AutomationTerminalGate }>(input: {
  terminalEntries: Map<string, T>;
  transportToken: SessionAttachmentToken;
  isCurrentToken(token: SessionAttachmentToken): boolean;
  retireCurrentTransport(): void;
}): boolean {
  if (!input.isCurrentToken(input.transportToken)) return false;
  disposeAutomationTerminalEntries(input.terminalEntries);
  input.retireCurrentTransport();
  return true;
}

export type AutomationTerminalPollOutcome =
  | { kind: "terminal"; result: RequestStatusResult }
  | { kind: "failed"; error: string }
  | { kind: "cancelled" };

export class AutomationTerminalGate {
  readonly requestId: string;

  private candidate = false;
  private released = false;
  private disposed = false;
  private terminalResult: RequestStatusResult | null = null;
  private polling: Promise<AutomationTerminalPollOutcome> | null = null;
  private error: string | null = null;
  private readonly pollController = new AbortController();

  constructor(requestId: string) {
    this.requestId = requestId;
  }

  markCandidateComplete(): void {
    this.candidate = true;
  }

  markStableTerminal(result: RequestStatusResult): boolean {
    if (result.requestId !== this.requestId || result.status !== "terminal") {
      throw new Error("Automation terminal result does not match the gate request");
    }
    if (this.released || this.disposed) return false;
    this.released = true;
    this.terminalResult = result;
    this.error = null;
    this.cancelPolling("Automation request reached stable terminal");
    return true;
  }

  pollForTerminal(
    waitForTerminal: (requestId: string, signal: AbortSignal) => Promise<RequestStatusResult>,
  ): Promise<AutomationTerminalPollOutcome> {
    if (this.disposed) return Promise.resolve({ kind: "cancelled" });
    if (this.terminalResult !== null) {
      return Promise.resolve({ kind: "terminal", result: this.terminalResult });
    }
    if (this.polling !== null) return this.polling;
    const attempt = Promise.resolve()
      .then(() => waitForTerminal(this.requestId, this.pollController.signal))
      .then((result) => {
        if (result.requestId !== this.requestId || result.status !== "terminal") {
          throw new Error("Request status did not reach this automation gate terminal");
        }
        this.error = null;
        return { kind: "terminal", result } as const;
      })
      .catch((error) => {
        if (this.pollController.signal.aborted) return { kind: "cancelled" } as const;
        this.error = error instanceof Error ? error.message : String(error);
        return { kind: "failed", error: this.error } as const;
      })
      .finally(() => {
        if (this.polling === attempt) this.polling = null;
      });
    this.polling = attempt;
    return attempt;
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.error = null;
    this.cancelPolling("Automation terminal gate retired");
  }

  private cancelPolling(message: string): void {
    this.polling = null;
    if (!this.pollController.signal.aborted) {
      this.pollController.abort(new DOMException(message, "AbortError"));
    }
  }

  get candidateComplete(): boolean {
    return this.candidate;
  }

  get signal(): AbortSignal {
    return this.pollController.signal;
  }

  get modelBusy(): boolean {
    return !this.released;
  }

  get canRetryStatus(): boolean {
    return !this.released && !this.disposed && this.error !== null;
  }

  get pollError(): string | null {
    return this.error;
  }
}

export function isVisionRequestBusy(
  streamingBusy: boolean,
  terminalEntries: Iterable<{ gate: Pick<AutomationTerminalGate, "modelBusy"> }>,
): boolean {
  return streamingBusy || Array.from(terminalEntries).some((entry) => entry.gate.modelBusy);
}

/**
 * Register exactly one live transport owner for a Vision request.
 *
 * The terminal gate and Session Hub lifecycle must validate the same token.
 * Registering a second token for either path immediately makes the first one
 * stale and can strand the page in its loading state after stable terminal.
 */
export function attachVisionRequestTransport(input: {
  mode: "chat" | "agent";
  contextId: string;
  attachChat(contextId: string): SessionAttachmentToken;
  attachAgent(contextId: string): SessionAttachmentToken;
}): {
  terminalAttachment: SessionAttachmentToken;
  liveChatAttachment: SessionAttachmentToken | null;
  liveAgentAttachment: SessionAttachmentToken | null;
} {
  const attachment = input.mode === "agent"
    ? input.attachAgent(input.contextId)
    : input.attachChat(input.contextId);
  return {
    terminalAttachment: attachment,
    liveChatAttachment: input.mode === "chat" ? attachment : null,
    liveAgentAttachment: input.mode === "agent" ? attachment : null,
  };
}

export function resetStreamingStateAfterStop<T extends {
  gate: AutomationTerminalGate;
  transport: AbortController;
}>(input: {
  terminalEntries: Map<string, T>;
  requestId: string | undefined;
  transport: AbortController | null;
  resetStreamingState(): void;
}): boolean {
  if (input.requestId !== undefined && input.transport !== null) {
    const entry = input.terminalEntries.get(input.requestId);
    if (entry?.transport === input.transport && entry.gate.modelBusy) return false;
  }
  input.resetStreamingState();
  return true;
}

export function installVisionRequestOwnership<T extends {
  gate: AutomationTerminalGate;
  transport: AbortController;
}>(input: {
  terminalEntries: Map<string, T>;
  previousRequestId: string | undefined;
  requestId: string;
  transport: AbortController;
  terminalEntry?: T;
  /** Answer Now may need to restore the superseded accepted request on 4xx. */
  preservePreviousTerminalEntry?: boolean;
  requestIdRef: { current: string | undefined };
  transportRef: { current: AbortController | null };
  setCurrentRequestId(requestId: string): void;
}): void {
  if (input.terminalEntry !== undefined) {
    if (input.previousRequestId !== undefined && !input.preservePreviousTerminalEntry) {
      disposeAutomationTerminalEntry(input.terminalEntries, input.previousRequestId);
    }
    input.terminalEntries.set(input.requestId, input.terminalEntry);
  }
  input.requestIdRef.current = input.requestId;
  input.transportRef.current = input.transport;
  input.setCurrentRequestId(input.requestId);
}

export async function executeVisionStop<T extends {
  gate: AutomationTerminalGate;
  transport: AbortController;
}>(input: {
  mode: "chat" | "agent";
  agentSessionId?: string;
  terminalEntries: Map<string, T>;
  requestIdRef: { current: string | undefined };
  transportRef: { current: AbortController | null };
  finalizeVisual(): void;
  resetStreamingState(): void;
  stopChat(requestId: string): Promise<unknown>;
  stopAgent(sessionId: string, expectedRequestId?: string): Promise<unknown>;
}): Promise<{
  requestId: string | undefined;
  transport: AbortController | null;
  resetImmediately: boolean;
}> {
  const requestId = input.requestIdRef.current;
  const transport = input.transportRef.current;
  // The UI must keep rendering the live response until the server confirms
  // that it has targeted the provider request. A rejected stop is retryable.
  if (input.mode === "chat") {
    if (requestId === undefined) throw new Error("Cannot stop Chat without a request ID");
    await input.stopChat(requestId);
  } else {
    if (input.agentSessionId === undefined) throw new Error("Cannot stop Agent without a session ID");
    if (requestId === undefined) throw new Error("Cannot stop Agent without a request ID");
    await input.stopAgent(input.agentSessionId, requestId);
  }
  abortStoppedConversation(transport);
  if (input.requestIdRef.current !== requestId || input.transportRef.current !== transport) {
    return { requestId, transport, resetImmediately: false };
  }
  input.finalizeVisual();
  const resetImmediately = resetStreamingStateAfterStop({
    terminalEntries: input.terminalEntries,
    requestId,
    transport,
    resetStreamingState: input.resetStreamingState,
  });
  return { requestId, transport, resetImmediately };
}

export function completeAutomationTerminal(
  gate: AutomationTerminalGate,
  result: RequestStatusResult,
  isCurrentTransport: boolean | undefined,
  release: () => void,
  transportToken?: SessionAttachmentToken,
  isCurrentToken?: (token: SessionAttachmentToken) => boolean,
): boolean {
  const current = transportToken
    ? (isCurrentToken?.(transportToken) ?? false)
    : isCurrentTransport === true;
  if (!gate.markStableTerminal(result) || !current) return false;
  release();
  return true;
}

export class CycleStartGuard {
  private current: symbol | null = null;

  begin(): symbol | null {
    if (this.current !== null) return null;
    this.current = Symbol("cycle-start");
    return this.current;
  }

  isCurrent(token: symbol): boolean {
    return this.current === token;
  }

  finish(token: symbol): boolean {
    if (this.current !== token) return false;
    this.current = null;
    return true;
  }

  cancel(): void {
    this.current = null;
  }

  get active(): boolean {
    return this.current !== null;
  }
}

export interface VisionPageLifecycleActions {
  abort(): Promise<void> | void;
  dispose(): Promise<void> | void;
}

export class VisionPageLifecycle {
  generation = 0;
  contextId: string;

  private readonly createContextId: () => string;
  private claimGeneration?: () => number;

  constructor(createContextId: () => string) {
    this.createContextId = createContextId;
    this.contextId = createContextId();
  }

  bindGenerationClaimant(claimGeneration: () => number): void {
    if (this.claimGeneration !== undefined) return;
    if (typeof claimGeneration !== "function") {
      throw new TypeError("Vision page generation claimant must be a function");
    }
    const generation = claimGeneration();
    this.generation = this.validateGenerationClaim(generation, true);
    this.claimGeneration = claimGeneration;
  }

  invalidate(): void {
    this.generation = this.claimGeneration === undefined
      ? this.generation + 1
      : this.claimNextGeneration();
    this.contextId = this.createContextId();
  }

  private claimNextGeneration(): number {
    return this.validateGenerationClaim(this.claimGeneration!(), false);
  }

  private validateGenerationClaim(generation: number, initialClaim: boolean): number {
    if (
      !Number.isSafeInteger(generation)
      || generation < 0
      || (initialClaim ? generation < this.generation : generation <= this.generation)
    ) {
      throw new RangeError("Vision page generation claim must strictly increase");
    }
    return generation;
  }

  async pageHide(actions: VisionPageLifecycleActions): Promise<void> {
    this.invalidate();
    await actions.abort();
  }

  async unmount(actions: VisionPageLifecycleActions): Promise<void> {
    this.invalidate();
    await actions.abort();
    await actions.dispose();
  }
}

export function freezeVisionAutomationConfig(
  config: VisionAutomationServerConfig,
): Readonly<VisionAutomationServerConfig> {
  return Object.freeze({ ...config });
}

export interface VisionAutomationStartState {
  enabled: boolean;
  cycleRunning: boolean;
  startGuardActive: boolean;
  armed: boolean;
  modelBusy: boolean;
  pendingAcceptance: boolean;
}

export function canStartVisionAutomation(state: VisionAutomationStartState): boolean {
  return state.enabled
    && !state.cycleRunning
    && !state.startGuardActive
    && !state.armed
    && !state.modelBusy
    && !state.pendingAcceptance;
}

export interface VisionVoiceResumeState {
  modelBusy: boolean;
  pendingAcceptance: boolean;
  dictationMagicSendPending?: boolean;
  pausedForResponse: boolean;
}

export function canResumeVisionVoiceCapture(state: VisionVoiceResumeState): boolean {
  return !state.modelBusy
    && !state.pendingAcceptance
    && !state.dictationMagicSendPending
    && state.pausedForResponse;
}

export function abortAcceptedVisionInference(controller: AbortController | null): void {
  controller?.abort();
}

export function getVisibleVisionUserText(message: Message): string {
  const content = message.content;
  const generatedPlaceholderIndexes = new Set<number>();
  if (Array.isArray(content)) {
    for (const summary of message.historicalImageSummaries ?? []) {
      const index = summary.contentPartIndex;
      if (index === undefined || !Number.isInteger(index) || index < 0) continue;
      const part = content[index];
      if (part?.type === "text") {
        generatedPlaceholderIndexes.add(index);
      }
    }
  }
  const text = typeof message.content === "string"
    ? message.content
    : Array.isArray(content)
      ? content
          .filter((part, index) => (
            part.type === "text"
            && typeof part.text === "string"
            && !generatedPlaceholderIndexes.has(index)
          ))
          .map((part) => part.text as string)
          .join("")
      : "";
  return text.replace(/^<vision_automation_manifest>[\s\S]*?<\/vision_automation_manifest>\s*/, "");
}

type ContentPart = {
  type?: unknown;
  text?: unknown;
};

const MANUAL_LINE = /^Manual images: (0|[1-9]\d*)(?:, image indexes ([1-9]\d*)-([1-9]\d*))?$/;
const SOURCE_LINE = /^Source ([1-9]\d*): (Desktop|Camera ([1-9]\d*)), image indexes ([1-9]\d*)-([1-9]\d*)$/;
const RELATIVE_VALUE = /^(0|[1-9]\d*)\.\d{2}s$/;

export function resolveCanonicalVisionAutomationImageBlock(
  context: HistoricalImageBlockContext,
): HistoricalImageBlockDescriptor | undefined {
  if (context.blockIndex !== 0 || !Array.isArray(context.message.content)) return undefined;
  const parts = context.message.content as ContentPart[];
  if (parts.length <= context.imageCount
    || parts.slice(0, context.imageCount).some((part) => part?.type !== "image_url")) return undefined;
  const textPart = parts[context.imageCount];
  if (textPart?.type !== "text" || typeof textPart.text !== "string") return undefined;
  const closing = "\n</vision_automation_manifest>";
  if (!textPart.text.startsWith("<vision_automation_manifest>\n")) return undefined;
  const closingIndex = textPart.text.indexOf(closing);
  if (closingIndex === -1) return undefined;
  const lines = textPart.text.slice(0, closingIndex).split("\n");
  if (lines[0] !== "<vision_automation_manifest>" || lines.length < 3) return undefined;

  const manual = MANUAL_LINE.exec(lines[1]);
  if (manual === null) return undefined;
  const manualCount = Number(manual[1]);
  let nextImageIndex = 1;
  if (manualCount === 0) {
    if (manual[2] !== undefined || manual[3] !== undefined) return undefined;
  } else {
    if (Number(manual[2]) !== nextImageIndex || Number(manual[3]) !== manualCount) return undefined;
    nextImageIndex += manualCount;
  }

  const sources: HistoricalImageSourceAlias[] = manualCount > 0 ? ["manual"] : [];
  const sourceCounts: number[] = [];
  let lineIndex = 2;
  let sourceNumber = 1;
  while (lineIndex < lines.length && lines[lineIndex].startsWith("Source ")) {
    const match = SOURCE_LINE.exec(lines[lineIndex]);
    if (match === null || Number(match[1]) !== sourceNumber) return undefined;
    const alias: HistoricalImageSourceAlias = match[2] === "Desktop"
      ? "desktop"
      : `camera_${Number(match[3])}`;
    if (sources.includes(alias)) return undefined;
    const start = Number(match[4]);
    const end = Number(match[5]);
    if (start !== nextImageIndex || end < start) return undefined;
    const count = end - start + 1;
    sources.push(alias);
    sourceCounts.push(count);
    nextImageIndex = end + 1;
    sourceNumber += 1;
    lineIndex += 1;
  }
  if (sourceCounts.length === 0 || lineIndex !== lines.length - 1) return undefined;
  const relativePrefix = "Relative times: ";
  if (!lines[lineIndex].startsWith(relativePrefix)) return undefined;
  const relativeParts = lines[lineIndex].slice(relativePrefix.length).split(", ");
  if (relativeParts.length === 0 || relativeParts.some((value) => !RELATIVE_VALUE.test(value))) {
    return undefined;
  }
  if (sourceCounts.some((count) => count !== relativeParts.length)) return undefined;
  if (nextImageIndex - 1 !== context.imageCount) return undefined;
  const relativeValues = relativeParts.map((value) => Number(value.slice(0, -1)));
  if (relativeValues.some((value, index) => index > 0 && value < relativeValues[index - 1])) {
    return undefined;
  }
  return {
    sources,
    relativeRange: {
      startSeconds: relativeValues[0],
      endSeconds: relativeValues[relativeValues.length - 1],
    },
  };
}
