import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const srcRoot = resolve(__dirname, "..");

test("text and vision input rows share the same existing composer alignment", () => {
  const textPage = readFileSync(resolve(srcRoot, "app/page.tsx"), "utf-8");
  const visionPage = readFileSync(resolve(srcRoot, "app/vision/page.tsx"), "utf-8");

  const composer = readFileSync(resolve(srcRoot, "components/conversation/ConversationComposer.tsx"), "utf-8");
  assert.match(textPage, /<ConversationComposer/);
  assert.match(visionPage, /<ConversationComposer/);
  assert.match(composer, /className="flex items-center space-x-3"/);
  assert.match(composer, /className="chat-input-textarea block w-full/);
});

test("action button dimensions are controlled by webUI variables, not padding utilities", () => {
  const composer = readFileSync(resolve(srcRoot, "components/conversation/ConversationComposer.tsx"), "utf-8");
  const globals = readFileSync(resolve(srcRoot, "app/globals.css"), "utf-8");
  const buttonClassPattern = /className=(?:"[^"]*webui-action-button[^"]*"|\{`webui-action-button[^`]*`\})/g;
  const buttonClasses = [
    ...composer.matchAll(buttonClassPattern),
  ].map((match) => match[0]);

  assert.equal(buttonClasses.length, 3); // Plus / Stop / Send exist once in the shared implementation.
  for (const className of buttonClasses) {
    assert.doesNotMatch(className, /\bpx-\d/);
    assert.doesNotMatch(className, /\bpy-\d/);
  }
  assert.match(globals, /\.webui-action-button\s*\{[\s\S]*padding:\s*0;/);
});

test("header and footer chrome use slimmed padding for a compact layout", () => {
  const textPage = readFileSync(resolve(srcRoot, "app/page.tsx"), "utf-8");
  const visionPage = readFileSync(resolve(srcRoot, "app/vision/page.tsx"), "utf-8");
  const globals = readFileSync(resolve(srcRoot, "app/globals.css"), "utf-8");
  const header = readFileSync(resolve(srcRoot, "components/conversation/ConversationHeader.tsx"), "utf-8");
  const composer = readFileSync(resolve(srcRoot, "components/conversation/ConversationComposer.tsx"), "utf-8");

  for (const [name, page] of [["text", textPage], ["vision", visionPage]]) {
    assert.match(
      header,
      /<header className=\{`sticky top-0 z-20 flex-shrink-0 border-b px-4 py-\[7px\] \$\{glassHeaderZoneClass\}`\}>/,
      `${name} page header must use slimmed vertical padding`,
    );
    assert.match(page, /<ConversationHeader/);
    assert.match(page, /<ConversationComposer/);
    assert.doesNotMatch(header, /border-b px-4 py-3 \$\{glassHeaderZoneClass\}/);
    assert.match(
      composer,
      /<footer[^>]*className=\{`sticky bottom-0 z-20 chat-input-footer flex-shrink-0 border-t px-4 pt-1 \$\{glassFooterZoneClass\}`\}>/,
      `${name} page footer must use slimmed top padding`,
    );
    assert.doesNotMatch(composer, /chat-input-footer flex-shrink-0 border-t px-4 pt-4/);
    assert.match(
      composer,
      /webui-glass-zone-content max-w-4xl mx-auto space-y-1/,
      `${name} page footer stack gap must be compacted`,
    );
  }
  assert.match(
    globals,
    /\.chat-input-footer\s*\{[\s\S]*?padding-bottom:\s*max\(0\.5rem, calc\(0\.5rem \+ env\(safe-area-inset-bottom\)\)\);/,
    "footer bottom padding must keep safe-area protection at a slimmer baseline",
  );
});
