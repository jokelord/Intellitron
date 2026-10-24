import assert from "node:assert/strict";
import { createRequire } from "node:module";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import type { WebUIConfig } from "../lib/web-ui-config.ts";

const require = createRequire(import.meta.url);
const testDir = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(testDir, "../..");
const { createRenderer, installTask6TypeScriptHook } = require("../lib/vision-automation/react-component-renderer.cjs");

installTask6TypeScriptHook(frontendRoot);

const React = require("react");
const GlassJellySurface = require("./GlassJellySurface.tsx").default;

interface ProbeCounters {
  getContext: number;
  loseContext: number;
}

interface BrowserStubs {
  probeCalls: ProbeCounters;
  restore(): void;
}

function installBrowserStubs(): BrowserStubs {
  const probeCalls: ProbeCounters = { getContext: 0, loseContext: 0 };
  const glStub = {
    getExtension(name: string): { loseContext(): void } | null {
      if (name !== "WEBGL_lose_context") return null;
      return { loseContext: () => { probeCalls.loseContext += 1; } };
    },
  };
  const canvasStub = {
    getContext(): typeof glStub {
      probeCalls.getContext += 1;
      return glStub;
    },
  };
  const matchMediaResult = {
    matches: true,
    addEventListener() {},
    removeEventListener() {},
  };
  const globalScope = globalThis as unknown as {
    document?: unknown;
    window?: unknown;
    documentPrevious?: unknown;
    windowPrevious?: unknown;
  };
  const documentStub = {
    createElement(tag: string): unknown {
      return tag === "canvas" ? canvasStub : {};
    },
  };
  const windowStub = {
    matchMedia: () => matchMediaResult,
    devicePixelRatio: 1,
    localStorage: {
      getItem: () => "0",
      setItem() {},
      removeItem() {},
    },
  };
  const documentPrevious = globalScope.document;
  const windowPrevious = globalScope.window;
  globalScope.document = documentStub;
  globalScope.window = windowStub;
  return {
    probeCalls,
    restore() {
      globalScope.document = documentPrevious;
      globalScope.window = windowPrevious;
    },
  };
}

function renderElement(config?: WebUIConfig, zone: "header" | "footer" = "header") {
  return React.createElement(GlassJellySurface, { config, zone });
}

test("GlassJellySurface releases the WebGL probe context immediately", () => {
  const { probeCalls, restore } = installBrowserStubs();
  try {
    const renderer = createRenderer(renderElement(undefined, "header"));
    assert.ok(probeCalls.getContext >= 1, "expected the WebGL probe to run on mount");
    assert.ok(probeCalls.loseContext >= 1, "expected the probe context to be released via WEBGL_lose_context");
    renderer.unmount();
  } finally {
    restore();
  }
});

test("GlassJellySurface does not re-probe WebGL on re-renders with an unchanged config identity", () => {
  const { probeCalls, restore } = installBrowserStubs();
  try {
    const config = { effects: {} } as unknown as WebUIConfig;
    const renderer = createRenderer(renderElement(config, "header"));
    const probesAfterMount = probeCalls.getContext;
    for (let round = 0; round < 5; round += 1) {
      renderer.update(renderElement(config, "header"));
    }
    assert.equal(
      probeCalls.getContext,
      probesAfterMount,
      "parent re-renders must not re-run the WebGL probe effect when config identity is stable",
    );
    renderer.unmount();
  } finally {
    restore();
  }
});

test("GlassJellySurface re-probes WebGL when the config identity changes", () => {
  const { probeCalls, restore } = installBrowserStubs();
  try {
    const configA = { effects: {} } as unknown as WebUIConfig;
    const renderer = createRenderer(renderElement(configA, "header"));
    const probesAfterMount = probeCalls.getContext;
    const configB = { effects: { aurora_glow: { enabled: true } } } as unknown as WebUIConfig;
    renderer.update(renderElement(configB, "header"));
    assert.ok(
      probeCalls.getContext > probesAfterMount,
      "a new config object identity must re-run the gate effect so config updates stay live",
    );
    renderer.unmount();
  } finally {
    restore();
  }
});
