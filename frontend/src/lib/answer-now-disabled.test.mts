import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const root = new URL("../../", import.meta.url);

test("Text and Vision pages do not showcase Answer Now", async () => {
  for (const relative of ["src/app/page.tsx", "src/app/vision/page.tsx"]) {
    const source = await readFile(new URL(relative, root), "utf8");

    assert.doesNotMatch(source, /Answer Now button/);
    assert.doesNotMatch(source, /onClick=\{handleAnswerNow\}/);
    assert.doesNotMatch(source, /title="立即返回答案"/);
  }
});
