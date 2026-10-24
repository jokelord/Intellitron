import { test } from "node:test";
import assert from "node:assert/strict";

import { retryDynamicImport } from "./retry-dynamic-import.ts";

test("retryDynamicImport retries transient chunk load failures", async () => {
  let attempts = 0;

  const result = await retryDynamicImport(
    async () => {
      attempts += 1;
      if (attempts < 3) {
        throw new Error("Loading chunk failed");
      }
      return { default: "loaded" };
    },
    { attempts: 3, delayMs: 0 },
  );

  assert.deepEqual(result, { default: "loaded" });
  assert.equal(attempts, 3);
});

test("retryDynamicImport surfaces the last error after exhausting retries", async () => {
  let attempts = 0;

  await assert.rejects(
    () => retryDynamicImport(
      async () => {
        attempts += 1;
        throw new Error(`failed-${attempts}`);
      },
      { attempts: 2, delayMs: 0 },
    ),
    /failed-2/,
  );

  assert.equal(attempts, 2);
});
