export type AssistantReasoningFields = {
  reasoning_content?: string;
  reasoningCollapsed?: boolean;
  reasoningDetected?: boolean;
  reasoningParseState?: 'outside' | 'inside_think';
  reasoningBuffer?: string;
};

type AssistantReasoningMessage = AssistantReasoningFields & {
  content?: unknown;
};

type AssistantReasoningState = Required<AssistantReasoningFields> & {
  content: string;
};

type CreateStateOptions = {
  defaultReasoningCollapsed: boolean;
};

type AppendAssistantDeltaOptions = {
  contentDelta?: string;
  reasoningDelta?: string;
  defaultReasoningCollapsed: boolean;
};

type FinalizeAssistantReasoningOptions = {
  defaultReasoningCollapsed: boolean;
};

const OPEN_TAG = '<think>';
const CLOSE_TAG = '</think>';
const OPEN_PREFIXES = Array.from({ length: OPEN_TAG.length - 1 }, (_, i) => OPEN_TAG.slice(0, i + 1));
const CLOSE_PREFIXES = Array.from({ length: CLOSE_TAG.length - 1 }, (_, i) => CLOSE_TAG.slice(0, i + 1));

export function createAssistantReasoningState(
  options: CreateStateOptions,
): AssistantReasoningState {
  return {
    content: '',
    reasoning_content: '',
    reasoningCollapsed: options.defaultReasoningCollapsed,
    reasoningDetected: false,
    reasoningParseState: 'outside',
    reasoningBuffer: '',
  };
}

function longestTrailingPrefix(value: string, prefixes: string[]): string {
  for (let i = prefixes.length - 1; i >= 0; i -= 1) {
    if (value.endsWith(prefixes[i])) {
      return prefixes[i];
    }
  }

  return '';
}

function ensureReasoningDefaults<T extends AssistantReasoningMessage>(
  message: T,
  defaultReasoningCollapsed: boolean,
): T & AssistantReasoningState {
  return {
    ...message,
    content: typeof message.content === 'string' ? message.content : '',
    reasoning_content: typeof message.reasoning_content === 'string' ? message.reasoning_content : '',
    reasoningCollapsed: message.reasoningCollapsed ?? defaultReasoningCollapsed,
    reasoningDetected: message.reasoningDetected ?? false,
    reasoningParseState: message.reasoningParseState ?? 'outside',
    reasoningBuffer: message.reasoningBuffer ?? '',
  };
}

function hasVisibleAnswerText(value: string): boolean {
  return value.trim().length > 0;
}

export function appendAssistantDelta<T extends AssistantReasoningMessage>(
  message: T,
  options: AppendAssistantDeltaOptions,
): T & AssistantReasoningState {
  const next = ensureReasoningDefaults(message, options.defaultReasoningCollapsed);

  if (options.reasoningDelta) {
    next.reasoning_content += options.reasoningDelta;
    next.reasoningDetected = true;
  }

  let pending = `${next.reasoningBuffer}${options.contentDelta ?? ''}`;
  next.reasoningBuffer = '';

  while (pending.length > 0) {
    if (next.reasoningParseState === 'outside') {
      if (hasVisibleAnswerText(next.content)) {
        next.content += pending;
        break;
      }

      const openIndex = pending.indexOf(OPEN_TAG);
      if (openIndex !== -1) {
        next.content += pending.slice(0, openIndex);

        if (hasVisibleAnswerText(next.content)) {
          next.content += pending.slice(openIndex);
          break;
        }

        next.reasoningDetected = true;
        pending = pending.slice(openIndex + OPEN_TAG.length);
        next.reasoningParseState = 'inside_think';
        continue;
      }

      const openPrefix = longestTrailingPrefix(pending, OPEN_PREFIXES);
      if (openPrefix) {
        next.content += pending.slice(0, pending.length - openPrefix.length);
        next.reasoningBuffer = openPrefix;
      } else {
        next.content += pending;
      }
      break;
    }

    const closeIndex = pending.indexOf(CLOSE_TAG);
    if (closeIndex !== -1) {
      next.reasoning_content += pending.slice(0, closeIndex);
      pending = pending.slice(closeIndex + CLOSE_TAG.length);
      next.reasoningParseState = 'outside';
      continue;
    }

    const closePrefix = longestTrailingPrefix(pending, CLOSE_PREFIXES);
    if (closePrefix) {
      next.reasoning_content += pending.slice(0, pending.length - closePrefix.length);
      next.reasoningBuffer = closePrefix;
    } else {
      next.reasoning_content += pending;
    }
    break;
  }

  return next;
}

export function finalizeAssistantReasoning<T extends AssistantReasoningMessage>(
  message: T,
  options: FinalizeAssistantReasoningOptions,
): T & AssistantReasoningState {
  const next = ensureReasoningDefaults(message, options.defaultReasoningCollapsed);

  if (next.reasoningBuffer) {
    if (next.reasoningParseState === 'inside_think') {
      next.reasoning_content += next.reasoningBuffer;
      next.reasoningDetected = true;
    } else {
      next.content += next.reasoningBuffer;
    }
    next.reasoningBuffer = '';
  }

  next.reasoningParseState = 'outside';
  return next;
}
