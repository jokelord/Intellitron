import { test } from "node:test";
import assert from "node:assert/strict";

import {
  getMermaidMoveButtonClass,
  getMermaidMoveIconClass,
  getNextMermaidZoom,
  getMermaidViewportClass,
  MERMAID_MAX_ZOOM,
  MERMAID_MIN_ZOOM,
  MERMAID_CONTAINER_CLASS,
  MERMAID_CONTENT_CLASS,
} from "./mermaid-layout.ts";

test("Mermaid move viewport stays shrinkable while panning is enabled", () => {
  const className = getMermaidViewportClass(true);

  assert.match(className, /\bmin-w-0\b/);
  assert.match(className, /\bw-full\b/);
  assert.match(className, /\boverflow-hidden\b/);
});

test("Mermaid containers do not force assistant bubble width from SVG intrinsic width", () => {
  assert.match(MERMAID_CONTAINER_CLASS, /\bmin-w-0\b/);
  assert.match(MERMAID_CONTAINER_CLASS, /\bw-full\b/);
  assert.match(MERMAID_CONTENT_CLASS, /\bmax-w-none\b/);
});

test("inactive Mermaid move button matches standard icon button size", () => {
  const className = getMermaidMoveButtonClass(false);

  assert.match(className, /\bh-7\b/);
  assert.match(className, /\bw-7\b/);
  assert.match(className, /\brounded-md\b/);
  assert.match(className, /\bbg-white\b/);
  assert.match(className, /\btext-slate-700\b/);
  assert.doesNotMatch(className, /\bbg-blue/);
});

test("active Mermaid move button uses distinct blue background at same size", () => {
  const className = getMermaidMoveButtonClass(true);

  assert.match(className, /\bh-7\b/);
  assert.match(className, /\bw-7\b/);
  assert.match(className, /\brounded-md\b/);
  assert.match(className, /\bbg-blue-100\b/);
  assert.match(className, /\btext-blue-700\b/);
  assert.match(className, /\bdark:bg-blue-900\b/);
  assert.match(className, /\bdark:text-blue-200\b/);
  assert.doesNotMatch(className, /\bbg-white\b/);
  assert.doesNotMatch(className, /\btext-white\b/);
  assert.doesNotMatch(className, /\bdark:text-white\b/);
});

test("Mermaid move icon matches standard icon button icon size", () => {
  const className = getMermaidMoveIconClass();

  assert.match(className, /\bh-3\.5\b/);
  assert.match(className, /\bw-3\.5\b/);
});

test("Mermaid zoom controls clamp to 10 percent and 600 percent", () => {
  assert.equal(MERMAID_MIN_ZOOM, 0.1);
  assert.equal(MERMAID_MAX_ZOOM, 6);
  assert.equal(getNextMermaidZoom(0.1, -0.1), 0.1);
  assert.equal(getNextMermaidZoom(6, 0.1), 6);
  assert.equal(getNextMermaidZoom(1, 0.1), 1.1);
});
