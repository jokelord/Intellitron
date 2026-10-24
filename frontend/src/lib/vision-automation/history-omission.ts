import type {
  HistoricalImageSourceAlias,
  HistoricalImageSummary,
  Message,
} from "../message-types.ts";

export interface HistoricalImageBlockDescriptor {
  sources: readonly HistoricalImageSourceAlias[];
  relativeRange?: {
    startSeconds: number;
    endSeconds: number;
  };
}

export interface HistoricalImageBlockContext {
  message: Message;
  messageIndex: number;
  blockIndex: number;
  imageCount: number;
}

export type HistoricalImageBlockResolver = (
  context: HistoricalImageBlockContext,
) => HistoricalImageBlockDescriptor | undefined;

export interface StagedChatHistoryOmission {
  readonly stagedMessages: readonly Message[];
  readonly omittedBlockCount: number;
  commit(currentMessages?: readonly Message[]): Message[];
  discard(): void;
}

type ContentPart = {
  type: string;
  text?: string;
  image_url?: { url: string };
};

interface MessageOmissionPlan {
  content: Message["content"];
  summaries: HistoricalImageSummary[];
  sourceVersion: MessageImageVersion;
}

interface ReferenceArrayVersion<T> {
  readonly reference: readonly T[];
  readonly entries: readonly T[];
}

interface ContentPartVersion {
  readonly reference: ContentPart;
  readonly keys: readonly string[];
  readonly values: readonly unknown[];
  readonly imageUrlReference?: { url: string };
  readonly imageUrlValue?: string;
}

interface MessageImageVersion {
  readonly content: Message["content"];
  readonly contentParts?: readonly ContentPartVersion[];
  readonly images?: ReferenceArrayVersion<string>;
  readonly summaries?: ReferenceArrayVersion<HistoricalImageSummary>;
}

function snapshotArray<T>(values: readonly T[] | undefined): ReferenceArrayVersion<T> | undefined {
  return values === undefined
    ? undefined
    : Object.freeze({ reference: values, entries: Object.freeze(Array.from(values)) });
}

function snapshotContentPart(part: ContentPart): ContentPartVersion {
  const keys = Object.freeze(Object.keys(part));
  return Object.freeze({
    reference: part,
    keys,
    values: Object.freeze(keys.map((key) => (part as Record<string, unknown>)[key])),
    imageUrlReference: part.image_url,
    imageUrlValue: part.image_url?.url,
  });
}

function snapshotMessageImageVersion(message: Message): MessageImageVersion {
  const content = message.content;
  return Object.freeze({
    content,
    contentParts: Array.isArray(content)
      ? Object.freeze((content as ContentPart[]).map(snapshotContentPart))
      : undefined,
    images: snapshotArray(message.images),
    summaries: snapshotArray(message.historicalImageSummaries),
  });
}

function matchesArrayVersion<T>(
  current: readonly T[] | undefined,
  version: ReferenceArrayVersion<T> | undefined,
): boolean {
  if (version === undefined) return current === undefined;
  return current === version.reference
    && current.length === version.entries.length
    && version.entries.every((entry, index) => Object.is(current[index], entry));
}

function matchesContentPartVersion(current: ContentPart, version: ContentPartVersion): boolean {
  if (current !== version.reference) return false;
  const keys = Object.keys(current);
  return keys.length === version.keys.length
    && version.keys.every((key, index) => (
      keys[index] === key
      && Object.is((current as Record<string, unknown>)[key], version.values[index])
    ))
    && current.image_url === version.imageUrlReference
    && current.image_url?.url === version.imageUrlValue;
}

function matchesMessageImageVersion(message: Message, version: MessageImageVersion): boolean {
  if (message.content !== version.content) return false;
  if (version.contentParts !== undefined) {
    if (!Array.isArray(message.content) || message.content.length !== version.contentParts.length) {
      return false;
    }
    if (!version.contentParts.every((part, index) => (
      matchesContentPartVersion((message.content as ContentPart[])[index], part)
    ))) return false;
  }
  return matchesArrayVersion(message.images, version.images)
    && matchesArrayVersion(message.historicalImageSummaries, version.summaries);
}

function sourceOrder(source: HistoricalImageSourceAlias): number {
  if (source === "manual") return 0;
  if (source === "desktop") return 1;
  return 2 + Number(source.slice(7));
}

function normalizeSources(
  sources: readonly HistoricalImageSourceAlias[],
): HistoricalImageSourceAlias[] {
  return Array.from(new Set(sources)).sort(
    (left, right) => sourceOrder(left) - sourceOrder(right),
  );
}

function validateSummary(summary: HistoricalImageSummary): void {
  if (!Number.isInteger(summary.count) || summary.count < 1) {
    throw new RangeError("Historical image count must be a positive integer");
  }
  for (const source of summary.sources) {
    if (source !== "manual" && source !== "desktop" && !/^camera_[1-9]\d*$/.test(source)) {
      throw new TypeError(`Invalid historical image source alias: ${source}`);
    }
  }
  const range = summary.relativeRange;
  if (range !== undefined && (
    !Number.isFinite(range.startSeconds)
    || !Number.isFinite(range.endSeconds)
    || range.startSeconds < 0
    || range.endSeconds < range.startSeconds
  )) {
    throw new RangeError("Historical image relative range is invalid");
  }
  if (summary.contentPartIndex !== undefined && (
    !Number.isInteger(summary.contentPartIndex) || summary.contentPartIndex < 0
  )) {
    throw new RangeError("Historical image placeholder part index is invalid");
  }
}

export function formatHistoricalImagePlaceholder(
  input: Omit<HistoricalImageSummary, "placeholder">,
): string {
  const summary = { ...input, sources: normalizeSources(input.sources) };
  validateSummary({ ...summary, placeholder: "" });
  const sources = summary.sources.length > 0 ? summary.sources.join(",") : "none";
  const relativeRange = summary.relativeRange === undefined
    ? "unavailable"
    : `${summary.relativeRange.startSeconds.toFixed(2)}..${summary.relativeRange.endSeconds.toFixed(2)}s`;
  return `[historical images omitted: count=${summary.count}; sources=${sources}; relative_range=${relativeRange}]`;
}

const HISTORICAL_IMAGE_PLACEHOLDER = /^\[historical images omitted: count=[1-9]\d*; sources=(?:none|(?:manual|desktop|camera_[1-9]\d*)(?:,(?:manual|desktop|camera_[1-9]\d*))*)?; relative_range=(?:unavailable|\d+\.\d{2}\.\.\d+\.\d{2}s)\]$/;

export function isHistoricalImagePlaceholder(value: string): boolean {
  return HISTORICAL_IMAGE_PLACEHOLDER.test(value);
}

function isImagePart(part: ContentPart): boolean {
  return part.type === "image_url";
}

export function stageChatHistoryImageOmission(
  messages: readonly Message[],
  resolveBlock: HistoricalImageBlockResolver = () => undefined,
): StagedChatHistoryOmission {
  const plans = new Map<string, MessageOmissionPlan>();
  let omittedBlockCount = 0;
  const stagedMessages = messages.map((message, messageIndex) => {
    if (message.role !== "user") return message;
    if (!Array.isArray(message.content)) {
      if (typeof message.content !== "string" || !message.images?.length) return message;
      const count = message.images.length;
      const descriptor = resolveBlock({
        message,
        messageIndex,
        blockIndex: 0,
        imageCount: count,
      });
      const sources = normalizeSources(descriptor?.sources ?? []);
      const relativeRange = descriptor?.relativeRange === undefined
        ? undefined
        : Object.freeze({
            startSeconds: descriptor.relativeRange.startSeconds,
            endSeconds: descriptor.relativeRange.endSeconds,
          });
      const summaryBase = { count, sources, relativeRange };
      const summaries = [{
        ...summaryBase,
        placeholder: formatHistoricalImagePlaceholder(summaryBase),
      }];
      if (plans.has(message.id)) throw new Error(`Duplicate message ID in omission source: ${message.id}`);
      plans.set(message.id, {
        content: message.content,
        summaries,
        sourceVersion: snapshotMessageImageVersion(message),
      });
      omittedBlockCount += 1;
      return { ...message, images: undefined };
    }
    const parts = message.content as ContentPart[];
    const nextParts: ContentPart[] = [];
    const summaries: HistoricalImageSummary[] = [];
    let index = 0;
    while (index < parts.length) {
      if (!isImagePart(parts[index])) {
        nextParts.push(parts[index]);
        index += 1;
        continue;
      }
      const blockStart = index;
      while (index < parts.length && isImagePart(parts[index])) index += 1;
      const count = index - blockStart;
      const descriptor = resolveBlock({
        message,
        messageIndex,
        blockIndex: summaries.length,
        imageCount: count,
      });
      const sources = normalizeSources(descriptor?.sources ?? []);
      const relativeRange = descriptor?.relativeRange === undefined
        ? undefined
        : Object.freeze({
            startSeconds: descriptor.relativeRange.startSeconds,
            endSeconds: descriptor.relativeRange.endSeconds,
          });
      const summaryBase = {
        count,
        sources,
        relativeRange,
      };
      const placeholder = formatHistoricalImagePlaceholder(summaryBase);
      const contentPartIndex = nextParts.length;
      summaries.push(Object.freeze({ ...summaryBase, placeholder, contentPartIndex }));
      nextParts.push({ type: "text", text: placeholder });
      omittedBlockCount += 1;
    }
    if (summaries.length === 0) return message;
    const content = nextParts as Message["content"];
    if (plans.has(message.id)) throw new Error(`Duplicate message ID in omission source: ${message.id}`);
    plans.set(message.id, {
      content,
      summaries,
      sourceVersion: snapshotMessageImageVersion(message),
    });
    return { ...message, content, images: undefined };
  });

  let sourceMessages: readonly Message[] | null = messages;
  let discarded = false;
  return {
    stagedMessages,
    omittedBlockCount,
    commit(currentMessages) {
      if (discarded) throw new Error("Staged history omission was discarded");
      if (sourceMessages === null) throw new Error("Staged history omission was already committed");
      const base = currentMessages ?? sourceMessages;
      const seenIds = new Set<string>();
      for (const message of base) {
        if (seenIds.has(message.id)) throw new Error(`Duplicate current message ID: ${message.id}`);
        seenIds.add(message.id);
        const plan = plans.get(message.id);
        if (plan !== undefined && !matchesMessageImageVersion(message, plan.sourceVersion)) {
          throw new Error(`Cannot commit omission against stale message version: ${message.id}`);
        }
      }
      for (const messageId of Array.from(plans.keys())) {
        if (!seenIds.has(messageId)) {
          throw new Error(`Cannot commit omission against stale missing message: ${messageId}`);
        }
      }
      const committed = base.map((message) => {
        const plan = plans.get(message.id);
        if (plan === undefined) return message;
        return {
          ...message,
          content: plan.content,
          images: undefined,
          historicalImageSummaries: [
            ...(message.historicalImageSummaries ?? []),
            ...plan.summaries,
          ],
        };
      });
      sourceMessages = null;
      plans.clear();
      return committed;
    },
    discard() {
      discarded = true;
      sourceMessages = null;
      plans.clear();
    },
  };
}
