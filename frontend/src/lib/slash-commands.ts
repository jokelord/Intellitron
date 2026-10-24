/**
 * Slash command registry and pure detection helpers.
 *
 * The popup is visible only when the first character of the input is "/"
 * (per user spec, strict option A). Future commands (e.g. /clear, /export)
 * are added by appending to SLASH_COMMANDS — single source of truth.
 */
import type { Message } from "./message-types";

export type SlashCommandMode = "text" | "vision";

export type SlashCommandContext = {
  mode: SlashCommandMode;
  agentMode: boolean;
  cycleRunning: boolean;
  isLoading: boolean;
  sessionId?: string;
  agentSessionId?: string;
  llmClient: unknown;
  setMessages: React.Dispatch<React.SetStateAction<Message[]>>;
  setInput: (s: string) => void;
};

export type SlashCommandRunResult =
  | { kind: "consumed" }
  | { kind: "fallthrough"; text: string };

export type SlashCommand = {
  name: string;
  description: string;
  category?: string;
  disabledWhen?: (ctx: SlashCommandContext) => string | null;
  run: (ctx: SlashCommandContext, args: string) => Promise<SlashCommandRunResult>;
};

export type SlashMenuState = {
  visible: boolean;
  query: string;
  selectedIndex: number;
};

export type CompactCustomInstructionParseResult =
  | { ok: true; customInstructions: string | null }
  | { ok: false; reason: "invalid_custom_instruction" };

export function parseCompactCustomInstruction(
  args: string
): CompactCustomInstructionParseResult {
  const trimmed = args.trim();
  if (!trimmed) return { ok: true, customInstructions: null };
  if (!trimmed.startsWith("<") || !trimmed.endsWith(">")) {
    return { ok: false, reason: "invalid_custom_instruction" };
  }
  const inner = trimmed.slice(1, -1).trim();
  if (!inner) return { ok: false, reason: "invalid_custom_instruction" };
  return { ok: true, customInstructions: inner };
}

/**
 * Bridge the pure `detectSlashCommand()` output (which uses `active`) into
 * the `slashMenu` state shape used by both pages (which uses `visible`).
 *
 * Centralizing the mapping here prevents the regression where the onChange
 * spread writes `active` into state without ever updating `visible`,
 * which would leave the popup permanently hidden (see
 * `slash-commands-state.test.mts` for the regression suite).
 */
export function mergeSlashCommandState(
  prev: SlashMenuState,
  detected: { active: boolean; query: string }
): SlashMenuState {
  return {
    visible: detected.active,
    query: detected.query,
    selectedIndex: prev.selectedIndex,
  };
}

async function _runCompactCommand(
  ctx: SlashCommandContext,
  args: string
): Promise<SlashCommandRunResult> {
  // The page-level timeline handler observes the /v1/compact/run SSE events
  // and projects them into a CompactSummaryPanel via projectTimelineEvent.
  // Here we just acknowledge consumption so the input is cleared.
  // For the page integration, the actual run lives in compact-client.ts.
  return { kind: "consumed" };
}

export const SLASH_COMMANDS: SlashCommand[] = [
  {
    name: "/compact",
    description:
      "Summarize the current conversation to free up context (older bubbles remain visible in the UI).",
    category: "session",
    disabledWhen: (ctx) => {
      if (ctx.isLoading) return "A request is in progress";
      if (ctx.cycleRunning)
        return "Cycle is running, manual compact is disabled";
      return null;
    },
    run: _runCompactCommand,
  },
];

export function detectSlashCommand(
  input: string,
  cursorPos: number
): { active: boolean; query: string } {
  if (!input.startsWith("/")) return { active: false, query: "" };
  if (cursorPos > input.length) return { active: false, query: "" };
  // Once the user types a space, the command line is "over" — the rest is arguments.
  const firstSpace = input.indexOf(" ");
  if (firstSpace !== -1 && cursorPos > firstSpace) {
    return { active: false, query: "" };
  }
  const queryEnd = firstSpace === -1 ? input.length : firstSpace;
  const query = input.slice(1, Math.min(cursorPos, queryEnd));
  return { active: true, query };
}

export function filterCommands(
  query: string,
  commands: SlashCommand[] = SLASH_COMMANDS
): SlashCommand[] {
  if (!query) return commands;
  const q = query.toLowerCase();
  return commands.filter((c) =>
    c.name.slice(1).toLowerCase().startsWith(q)
  );
}

/**
 * Returns the registered slash command if the input is "complete" — i.e. the
 * user has typed the full command name (with optional trailing space or
 * arguments). Used by the Enter handler to decide between invoking a command
 * and sending a plain message.
 *
 * The leading slash is required; partial prefixes ("/com") do NOT match here.
 * That preserves the existing popup filter semantics where "/com" keeps the
 * popup open and is not yet considered a finished command line.
 */
export function isCompleteSlashCommand(
  input: string,
  commands: SlashCommand[] = SLASH_COMMANDS
): SlashCommand | null {
  if (!input.startsWith("/")) return null;
  const body = input.slice(1);
  if (body.length === 0) return null;
  // After the command name, the only legal separator is a single space
  // (followed by optional arguments). Anything else is not a match.
  for (const cmd of commands) {
    const name = cmd.name.slice(1);
    if (body === name) return cmd;
    if (body.startsWith(name + " ")) return cmd;
  }
  return null;
}
