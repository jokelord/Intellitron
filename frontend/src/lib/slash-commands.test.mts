import { test } from "node:test";
import assert from "node:assert/strict";
import {
  detectSlashCommand,
  filterCommands,
  SLASH_COMMANDS,
} from "./slash-commands.ts";

test("detectSlashCommand activates only when first char is /", () => {
  assert.deepEqual(detectSlashCommand("/compact", 8), {
    active: true,
    query: "compact",
  });
  assert.deepEqual(detectSlashCommand("hello /compact", 14), {
    active: false,
    query: "",
  });
  assert.deepEqual(detectSlashCommand("/c", 2), { active: true, query: "c" });
  assert.deepEqual(detectSlashCommand("/", 1), { active: true, query: "" });
});

test("detectSlashCommand stops at first space (mid-arg not active)", () => {
  assert.deepEqual(detectSlashCommand("/compact foo", 8), {
    active: true,
    query: "compact",
  });
  assert.deepEqual(detectSlashCommand("/compact foo", 12), {
    active: false,
    query: "",
  });
});

test("filterCommands matches by prefix after slash", () => {
  const matches = filterCommands("com");
  assert.ok(matches.some((c) => c.name === "/compact"));
});

test("filterCommands returns all when query empty", () => {
  assert.equal(filterCommands("").length, SLASH_COMMANDS.length);
});

test("filterCommands with no match returns empty array", () => {
  assert.deepEqual(filterCommands("xyz"), []);
});

test("SLASH_COMMANDS contains /compact by default", () => {
  assert.ok(SLASH_COMMANDS.some((c) => c.name === "/compact"));
});
