import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import {
  isCompleteSlashCommand,
  parseCompactCustomInstruction,
  SLASH_COMMANDS,
} from "./slash-commands.ts";

const testDir = new URL(".", import.meta.url).pathname;

test("regression: '/compact' is recognized as a complete slash command (no trailing space)", () => {
  const cmd = isCompleteSlashCommand("/compact", SLASH_COMMANDS);
  assert.ok(cmd, "expected to recognize '/compact'");
  assert.equal(cmd!.name, "/compact");
});

test("regression: '/compact ' (with trailing space) is recognized as a complete slash command", () => {
  const cmd = isCompleteSlashCommand("/compact ", SLASH_COMMANDS);
  assert.ok(cmd, "expected to recognize '/compact ' (Tab+Enter case)");
  assert.equal(cmd!.name, "/compact");
});

test("regression: '/compact with args' (command + args) is recognized as a complete slash command", () => {
  const cmd = isCompleteSlashCommand("/compact with args", SLASH_COMMANDS);
  assert.ok(cmd, "expected to recognize '/compact with args'");
  assert.equal(cmd!.name, "/compact");
});

test("/compact parser returns null custom instructions for empty args", () => {
  assert.deepEqual(parseCompactCustomInstruction(""), {
    ok: true,
    customInstructions: null,
  });
  assert.deepEqual(parseCompactCustomInstruction("   "), {
    ok: true,
    customInstructions: null,
  });
});

test("/compact parser extracts angle-bracket custom instruction", () => {
  assert.deepEqual(parseCompactCustomInstruction(" <focus on code> "), {
    ok: true,
    customInstructions: "focus on code",
  });
});

test("/compact parser preserves inner literal angle brackets", () => {
  assert.deepEqual(parseCompactCustomInstruction("<focus with <literal> brackets>"), {
    ok: true,
    customInstructions: "focus with <literal> brackets",
  });
});

test("/compact parser rejects non-envelope args", () => {
  assert.deepEqual(parseCompactCustomInstruction("focus on code"), {
    ok: false,
    reason: "invalid_custom_instruction",
  });
});

test("/compact parser rejects empty envelope", () => {
  assert.deepEqual(parseCompactCustomInstruction("<>"), {
    ok: false,
    reason: "invalid_custom_instruction",
  });
});

test("regression: '/xyz' (unknown command) is NOT recognized as a complete slash command", () => {
  const cmd = isCompleteSlashCommand("/xyz", SLASH_COMMANDS);
  assert.equal(cmd, null);
});

test("regression: 'compact' (no leading slash) is NOT recognized as a complete slash command", () => {
  const cmd = isCompleteSlashCommand("compact", SLASH_COMMANDS);
  assert.equal(cmd, null);
});

test("regression: 'hello' is NOT recognized as a complete slash command", () => {
  const cmd = isCompleteSlashCommand("hello", SLASH_COMMANDS);
  assert.equal(cmd, null);
});

test("regression: '/com' (partial command, not a full name) is NOT recognized as complete", () => {
  // Per spec: only the exact full command name triggers invocation.
  // Partial prefixes still let the popup stay open (filter behavior).
  const cmd = isCompleteSlashCommand("/com", SLASH_COMMANDS);
  assert.equal(cmd, null);
});

test("regression: empty string is NOT recognized as a complete slash command", () => {
  const cmd = isCompleteSlashCommand("", SLASH_COMMANDS);
  assert.equal(cmd, null);
});

test("regression: send buttons route complete slash commands before handleSend", async () => {
  const commands = await readFile(new URL('./conversation/use-conversation-commands.ts', import.meta.url), 'utf8');
  assert.match(commands, /const matched = isCompleteSlashCommand\(input\);[\s\S]*?const args = input\.slice\(matched\.name\.length\);[\s\S]*?handleSelectSlashCommand\(matched, args\);\s*return;[\s\S]*?send\(\);/);
  const composer = await readFile(new URL('../components/conversation/ConversationComposer.tsx', import.meta.url), 'utf8');
  assert.match(composer, /onClick=\{onSubmit\}/);
  for (const pagePath of [path.join(testDir, '../app/page.tsx'), path.join(testDir, '../app/vision/page.tsx')]) {
    const source = await readFile(pagePath, 'utf8');
    assert.match(source, /useConversationCommands\(/);
    assert.match(source, /onSubmit=\{handleSubmit\}/);
    assert.doesNotMatch(source, /onClick=\{handleSend\}/);
  }
});

test("manual /compact pages parse args and forward customInstructions", async () => {
  const commands = await readFile(new URL('./conversation/use-conversation-commands.ts', import.meta.url), 'utf8');
  assert.match(commands, /parseCompactCustomInstruction\(args\)/);
  assert.match(commands, /customInstructions:\s*parsedCustom\.customInstructions/);
  assert.match(commands, /const parsedCustom = parseCompactCustomInstruction\(args\);[\s\S]*?if \(!parsedCustom\.ok\) \{[\s\S]*?reason: 'invalid_custom_instruction',[\s\S]*?return;[\s\S]*?const body = buildCompactRequestBody/);
  for (const pagePath of [path.join(testDir, '../app/page.tsx'), path.join(testDir, '../app/vision/page.tsx')]) assert.match(await readFile(pagePath, 'utf8'), /useConversationCommands\(/);
});
