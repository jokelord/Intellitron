import assert from "node:assert/strict";
import test from "node:test";

import {
  buildVisionAutomationRequestContract,
  isValidAutomationLifecycleId,
  isValidRequestId,
  parseRequestAcceptedEvent,
  requestStatusConfirmsAcceptance,
} from "./send-contract.ts";

const binding = {
  pageGeneration: 7,
  mode: "agent" as const,
  topLevelContextId: "agent-session-0001",
  automationTaskId: "task_01J0123456789ABCDEFGH",
  captureCycleId: "cycle_01J0123456789ABCDEFG",
};

test("request contract carries the exact shared Chat and Agent metadata shape", () => {
  const contract = buildVisionAutomationRequestContract({
    requestId: "request-123",
    manualImageCount: 2,
    sourceCount: 3,
    retainedTickCount: 4,
    binding,
  });

  assert.deepEqual(contract, {
    requestId: "request-123",
    visionAutomation: {
      history_image_policy: "aggregate_prior_image_blocks",
      manual_image_count: 2,
      source_count: 3,
      retained_tick_count: 4,
      binding: {
        page_generation: 7,
        mode: "agent",
        top_level_context_id: "agent-session-0001",
        automation_task_id: "task_01J0123456789ABCDEFGH",
        capture_cycle_id: "cycle_01J0123456789ABCDEFG",
      },
    },
  });
});

test("request IDs use the backend lifecycle grammar and length bounds", () => {
  assert.equal(isValidRequestId("request-123"), true);
  assert.equal(isValidRequestId("A2345678._~:-"), true);
  assert.equal(isValidRequestId("short"), false);
  assert.equal(isValidRequestId("_request-123"), false);
  assert.equal(isValidRequestId("request/123"), false);
  assert.equal(isValidRequestId(`r${"x".repeat(128)}`), false);
  assert.throws(() => buildVisionAutomationRequestContract({
    requestId: "bad/id-123",
    manualImageCount: 0,
    sourceCount: 0,
    retainedTickCount: 0,
    binding,
  }), /requestId/);
});

test("binding validation matches backend bounds and high-entropy lifecycle grammar", () => {
  assert.equal(isValidAutomationLifecycleId("task_01J0123456789ABCDEFGH"), true);
  assert.equal(isValidAutomationLifecycleId("short"), false);
  assert.throws(() => buildVisionAutomationRequestContract({
    requestId: "request-123",
    manualImageCount: 0,
    sourceCount: 0,
    retainedTickCount: 0,
    binding: { ...binding, pageGeneration: -1 },
  }), /pageGeneration/);
  assert.throws(() => buildVisionAutomationRequestContract({
    requestId: "request-123",
    manualImageCount: 0,
    sourceCount: 0,
    retainedTickCount: 0,
    binding: { ...binding, automationTaskId: "short" },
  }), /automationTaskId/);
  assert.throws(() => buildVisionAutomationRequestContract({
    requestId: "request-123",
    manualImageCount: 0,
    sourceCount: 0,
    retainedTickCount: 0,
    binding: { ...binding, topLevelContextId: " agent-session-0001" },
  }), /topLevelContextId/);
});

test("cleanup token is nested only when explicitly supplied", () => {
  const contract = buildVisionAutomationRequestContract({
    requestId: "request-123",
    manualImageCount: 0,
    sourceCount: 0,
    retainedTickCount: 0,
    binding: { ...binding, cleanupToken: "cleanup_01J0123456789ABCDEFG" },
  });
  assert.equal(
    contract.visionAutomation.binding.cleanup_token,
    "cleanup_01J0123456789ABCDEFG",
  );
  assert.doesNotMatch(JSON.stringify(contract), /pageInstanceId|contextId|cleanupToken/);
});

test("request contract rejects impossible declared image counts", () => {
  assert.throws(() => buildVisionAutomationRequestContract({
    requestId: "request-123",
    manualImageCount: 10,
    sourceCount: 1,
    retainedTickCount: 1,
    binding,
  }), /manualImageCount/);
  assert.throws(() => buildVisionAutomationRequestContract({
    requestId: "request-123",
    manualImageCount: 9,
    sourceCount: 242,
    retainedTickCount: 1,
    binding,
  }), /sourceCount/);
  assert.throws(() => buildVisionAutomationRequestContract({
    requestId: "request-123",
    manualImageCount: 9,
    sourceCount: 241,
    retainedTickCount: 2,
    binding,
  }), /250/);
});

test("parseRequestAcceptedEvent validates the bound request ID", () => {
  assert.deepEqual(
    parseRequestAcceptedEvent(
      { type: "request_accepted", request_id: "request-123" },
      "request-123",
    ),
    { type: "request_accepted", requestId: "request-123" },
  );
  assert.equal(parseRequestAcceptedEvent({ type: "content", content: "x" }, "request-123"), null);
  assert.throws(
    () => parseRequestAcceptedEvent(
      { type: "request_accepted", request_id: "other" },
      "request-123",
    ),
    /does not match/,
  );
});

test("accepted, running, and terminal status disambiguate acceptance but unknown does not", () => {
  assert.equal(requestStatusConfirmsAcceptance("unknown"), false);
  assert.equal(requestStatusConfirmsAcceptance("accepted"), true);
  assert.equal(requestStatusConfirmsAcceptance("running"), true);
  assert.equal(requestStatusConfirmsAcceptance("terminal"), true);
});
