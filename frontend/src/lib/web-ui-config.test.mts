import { test } from "node:test";
import assert from "node:assert/strict";

import {
  AURORA_GLOW_SKIN_ID,
  DEFAULT_WEBUI_SKIN_ID,
  MESSAGE_TEMPLATE_IMMEDIATE_SEND_STORAGE_KEY,
  MESSAGE_TEMPLATES_STORAGE_KEY,
  WEBUI_SKINS,
  applyWebUISkinOverride,
  buildWebUIStyleVars,
  getWebUIAvatarSize,
  getWebUIAvatarPath,
  getGlassJellyConfig,
  getWebUIFoldThinkingAfterFinalAnswer,
  getWebUIFooterBackgroundAlpha,
  getWebUIHeaderBackgroundAlpha,
  getWebUIHeaderSubtitle,
  getWebUIMessageTemplateDefaults,
  getWebUIPageHeaderDisplay,
  getWebUIPageTitle,
  getWebUIVisionWelcomeConfig,
  getWebUISkin,
  isAuroraGlowSkin,
  normalizeWebUISkinId,
  readStoredWebUIMessageTemplates,
  readStoredWebUIMessageTemplateImmediateSend,
  readStoredWebUISkinId,
  resolveWebUIMessageTemplates,
  clearStoredWebUIMessageTemplates,
  writeStoredWebUIMessageTemplates,
  writeStoredWebUIMessageTemplateImmediateSend,
  writeStoredWebUISkinId,
} from "./web-ui-config.ts";

function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
    removeItem: (key: string) => {
      values.delete(key);
    },
    values,
  };
}

const yamlMessageTemplateDefaults = ["yaml one", "yaml two"];

test("message templates use YAML when the browser key is absent", () => {
  const storage = memoryStorage();
  const stored = readStoredWebUIMessageTemplates(storage);

  assert.deepEqual(stored, { kind: "absent", items: null });
  assert.deepEqual(
    resolveWebUIMessageTemplates(yamlMessageTemplateDefaults, stored),
    yamlMessageTemplateDefaults,
  );
  assert.equal(storage.values.has(MESSAGE_TEMPLATES_STORAGE_KEY), false);
});

test("a valid browser list replaces the complete YAML template list", () => {
  const storage = memoryStorage({
    [MESSAGE_TEMPLATES_STORAGE_KEY]: JSON.stringify([" browser only ", "line one\nline two"]),
  });
  const stored = readStoredWebUIMessageTemplates(storage);

  assert.deepEqual(stored, {
    kind: "valid",
    items: [" browser only ", "line one\nline two"],
  });
  assert.deepEqual(
    resolveWebUIMessageTemplates(yamlMessageTemplateDefaults, stored),
    [" browser only ", "line one\nline two"],
  );
});

test("a valid empty browser list intentionally hides YAML templates", () => {
  const storage = memoryStorage({ [MESSAGE_TEMPLATES_STORAGE_KEY]: "[]" });
  const stored = readStoredWebUIMessageTemplates(storage);

  assert.deepEqual(stored, { kind: "valid", items: [] });
  assert.deepEqual(resolveWebUIMessageTemplates(yamlMessageTemplateDefaults, stored), []);
});

test("invalid template cache falls back to YAML without rewriting diagnostic data", () => {
  for (const encoded of ["{\"items\":[]}", "not-json", "[\"ok\",4]", "[\"  \"]"]) {
    const storage = memoryStorage({ [MESSAGE_TEMPLATES_STORAGE_KEY]: encoded });
    const stored = readStoredWebUIMessageTemplates(storage);

    assert.equal(stored.kind, "invalid");
    assert.deepEqual(
      resolveWebUIMessageTemplates(yamlMessageTemplateDefaults, stored),
      yamlMessageTemplateDefaults,
    );
    assert.equal(storage.values.get(MESSAGE_TEMPLATES_STORAGE_KEY), encoded);
  }
});

test("template defaults preserve valid text and reject malformed arrays", () => {
  assert.deepEqual(getWebUIMessageTemplateDefaults(), []);
  assert.deepEqual(
    getWebUIMessageTemplateDefaults({
      components: { message_templates: { items: [" leading ", "second\nline"] } },
    }),
    [" leading ", "second\nline"],
  );
  assert.deepEqual(
    getWebUIMessageTemplateDefaults({
      components: {
        message_templates: { items: ["valid", 4] as unknown as string[] },
      },
    }),
    [],
  );
});

test("template persistence writes and clears only the dedicated browser key", () => {
  const storage = memoryStorage({ "llm-chat-settings": "keep" });

  assert.equal(
    writeStoredWebUIMessageTemplates(["first", " second\nline "], storage),
    true,
  );
  assert.equal(
    storage.values.get(MESSAGE_TEMPLATES_STORAGE_KEY),
    JSON.stringify(["first", " second\nline "]),
  );
  assert.equal(storage.values.get("llm-chat-settings"), "keep");
  assert.equal(clearStoredWebUIMessageTemplates(storage), true);
  assert.equal(storage.values.has(MESSAGE_TEMPLATES_STORAGE_KEY), false);
  assert.equal(storage.values.get("llm-chat-settings"), "keep");
});

test("clearing a browser override restores the currently loaded YAML defaults", () => {
  const storage = memoryStorage();
  assert.equal(writeStoredWebUIMessageTemplates(["browser custom"], storage), true);
  assert.deepEqual(
    resolveWebUIMessageTemplates(
      ["current YAML"],
      readStoredWebUIMessageTemplates(storage),
    ),
    ["browser custom"],
  );

  assert.equal(clearStoredWebUIMessageTemplates(storage), true);
  const afterClear = readStoredWebUIMessageTemplates(storage);
  assert.deepEqual(afterClear, { kind: "absent", items: null });
  assert.deepEqual(resolveWebUIMessageTemplates(["current YAML"], afterClear), ["current YAML"]);
  assert.equal(storage.values.has(MESSAGE_TEMPLATES_STORAGE_KEY), false);
});

test("template storage exceptions become safe results instead of escaping", () => {
  const throwingStorage = {
    getItem: () => {
      throw new Error("blocked");
    },
    setItem: () => {
      throw new Error("blocked");
    },
    removeItem: () => {
      throw new Error("blocked");
    },
  };

  assert.deepEqual(readStoredWebUIMessageTemplates(throwingStorage), {
    kind: "unavailable",
    items: null,
  });
  assert.equal(writeStoredWebUIMessageTemplates(["item"], throwingStorage), false);
  assert.equal(clearStoredWebUIMessageTemplates(throwingStorage), false);
});

test("immediate-send storage reads true and false and defaults false for missing or invalid values", () => {
  for (const [encoded, expected] of [
    ["true", true],
    ["false", false],
    [null, false],
    ["1", false],
    ["\"true\"", false],
    ["not-json", false],
  ]) {
    const storage = memoryStorage(encoded === null ? {} : {
      [MESSAGE_TEMPLATE_IMMEDIATE_SEND_STORAGE_KEY]: encoded,
    });
    assert.equal(readStoredWebUIMessageTemplateImmediateSend(storage), expected);
  }
});

test("immediate-send persistence uses its own key without overwriting template arrays", () => {
  const templates = JSON.stringify(["keep this template"]);
  const storage = memoryStorage({
    [MESSAGE_TEMPLATES_STORAGE_KEY]: templates,
    "llm-chat-settings": "keep settings",
  });

  assert.equal(writeStoredWebUIMessageTemplateImmediateSend(true, storage), true);
  assert.equal(storage.values.get(MESSAGE_TEMPLATE_IMMEDIATE_SEND_STORAGE_KEY), "true");
  assert.equal(storage.values.get(MESSAGE_TEMPLATES_STORAGE_KEY), templates);
  assert.equal(storage.values.get("llm-chat-settings"), "keep settings");

  assert.equal(writeStoredWebUIMessageTemplateImmediateSend(false, storage), true);
  assert.equal(storage.values.get(MESSAGE_TEMPLATE_IMMEDIATE_SEND_STORAGE_KEY), "false");
  assert.equal(storage.values.get(MESSAGE_TEMPLATES_STORAGE_KEY), templates);
});

test("immediate-send storage exceptions safely fall back false and report failed writes", () => {
  const throwingStorage = {
    getItem: () => { throw new Error("blocked"); },
    setItem: () => { throw new Error("blocked"); },
    removeItem: () => { throw new Error("blocked"); },
  };

  assert.equal(readStoredWebUIMessageTemplateImmediateSend(throwingStorage), false);
  assert.equal(writeStoredWebUIMessageTemplateImmediateSend(true, throwingStorage), false);
});

test("buildWebUIStyleVars maps configured action button sizes", () => {
  const style = buildWebUIStyleVars({
    skin: { id: "default-v1", name: "Default" },
    components: {
      input_bar: {
        min_height_px: 56,
        max_rows: 6,
        line_height_px: 24,
        vertical_padding_px: 24,
        border_radius_px: 16,
      },
      action_buttons: {
        send: { width_px: 60, height_px: 52 },
        stop: { width_px: 62, height_px: 54 },
        answer_now: { width_px: 64, height_px: 56 },
      },
      mermaid: {
        min_height_px: 420,
      },
    },
  });

  assert.equal(style["--webui-send-button-width"], "60px");
  assert.equal(style["--webui-send-button-height"], "52px");
  assert.equal(style["--webui-stop-button-width"], "62px");
  assert.equal(style["--webui-stop-button-height"], "54px");
  assert.equal(style["--webui-answer-now-button-width"], "64px");
  assert.equal(style["--webui-answer-now-button-height"], "56px");
  assert.equal(style["--webui-input-min-height"], "56px");
  assert.equal(style["--webui-input-max-height"], "168px");
  assert.equal(style["--webui-input-border-radius"], "16px");
  assert.equal(style["--webui-mermaid-min-height"], "420px");
});

test("getWebUIPageTitle maps configured text and vision titles", () => {
  const config = {
    components: {
      page_header: {
        text_title: "自定义文本模式",
        vision_title: "自定义视觉模式",
        subtitle: "Custom Device",
      },
      message_avatar: {
        user_image_path: "/avatars/user.png",
        assistant_image_path: "/avatars/assistant.png",
        size_px: 44,
      },
      vision_welcome: {
        title: "自定义欢迎",
        description: "自定义说明",
        icon_image_path: "/welcome.png",
      },
    },
  };

  assert.equal(getWebUIPageTitle(config, "text"), "自定义文本模式");
  assert.equal(getWebUIPageTitle(config, "vision"), "自定义视觉模式");
  assert.equal(getWebUIHeaderSubtitle(config), "Custom Device");
  assert.equal(getWebUIAvatarPath(config, "user"), "/avatars/user.png");
  assert.equal(getWebUIAvatarPath(config, "assistant"), "/avatars/assistant.png");
  assert.equal(getWebUIAvatarSize(config), 44);
  assert.deepEqual(getWebUIVisionWelcomeConfig(config), {
    title: "自定义欢迎",
    description: "自定义说明",
    iconImagePath: "/welcome.png",
  });
  assert.equal(getWebUIPageTitle(undefined, "text"), "LLM-Test");
  assert.equal(getWebUIPageTitle(undefined, "vision"), "LLM-Test 视觉模式");
  assert.equal(getWebUIHeaderSubtitle(undefined), "NVIDIA Jetson Thor");
  assert.equal(getWebUIAvatarPath(undefined, "user"), undefined);
  assert.equal(getWebUIAvatarSize(undefined), 32);
});

test("getWebUIAvatarPath maps public file paths to browser URLs", () => {
  const config = (pathValue: string) => ({
    components: {
      message_avatar: {
        user_image_path: pathValue,
      },
    },
  });

  assert.equal(getWebUIAvatarPath(config("me.jpg"), "user"), "/me.jpg");
  assert.equal(getWebUIAvatarPath(config("public/me.jpg"), "user"), "/me.jpg");
  assert.equal(getWebUIAvatarPath(config("frontend/public/me.jpg"), "user"), "/me.jpg");
  assert.equal(getWebUIAvatarPath(config("~/Project/LLM-deployment/frontend/public/me.jpg"), "user"), "/me.jpg");
  assert.equal(getWebUIAvatarPath(config("/avatars/me.jpg"), "user"), "/avatars/me.jpg");
  assert.equal(getWebUIAvatarPath(config("https://example.com/me.jpg"), "user"), "https://example.com/me.jpg");
});

test("getWebUIAvatarSize clamps configured size", () => {
  assert.equal(getWebUIAvatarSize({ components: { message_avatar: { size_px: 12 } } }), 24);
  assert.equal(getWebUIAvatarSize({ components: { message_avatar: { size_px: 128 } } }), 96);
});

test("isAuroraGlowSkin only enables the R3F skin when id and effect flag match", () => {
  assert.equal(
    isAuroraGlowSkin({
      skin: { id: "aurora-glow-r3f-v1", name: "Aurora Glow" },
      effects: { aurora_glow: { enabled: true } },
    }),
    true,
  );
  assert.equal(
    isAuroraGlowSkin({
      skin: { id: "aurora-glow-r3f-v1", name: "Aurora Glow" },
      effects: { aurora_glow: { enabled: false } },
    }),
    false,
  );
  assert.equal(
    isAuroraGlowSkin({
      skin: { id: "default-v1", name: "Default" },
      effects: { aurora_glow: { enabled: true } },
    }),
    false,
  );
});

test("getGlassJellyConfig and style vars expose safe fallback values", () => {
  const config = {
    skin: { id: "aurora-glow-r3f-v1", name: "Aurora Glow" },
    effects: {
      aurora_glow: {
        enabled: true,
        intensity: 0.9,
        blur_px: 24,
        chromatic_aberration: 0.025,
        refraction_strength: 0.06,
        tint_a: "#f8fafc",
        tint_b: "#38bdf8",
        opacity: 0.8,
        animated: false,
        max_dpr: 1.25,
      },
    },
  };

  assert.deepEqual(getGlassJellyConfig(config), {
    enabled: true,
    intensity: 0.9,
    blur_px: 24,
    chromatic_aberration: 0.025,
    refraction_strength: 0.06,
    tint_a: "#f8fafc",
    tint_b: "#38bdf8",
    opacity: 0.8,
    animated: false,
    max_dpr: 1.25,
  });

  const style = buildWebUIStyleVars(config);
  assert.equal(style["--webui-glass-blur"], "24px");
  assert.equal(style["--webui-glass-opacity"], "0.8");
  assert.equal(style["--webui-glass-intensity"], "0.9");
  assert.equal(style["--webui-glass-tint-a"], "#f8fafc");
  assert.equal(style["--webui-glass-tint-b"], "#38bdf8");
});

test("aurora glow helpers accept the legacy glass_jelly effect key", () => {
  const config = {
    skin: { id: "aurora-glow-r3f-v1", name: "Aurora Glow" },
    effects: { glass_jelly: { enabled: true, opacity: 0.61 } },
  };

  assert.equal(isAuroraGlowSkin(config), true);
  assert.equal(getGlassJellyConfig(config).opacity, 0.61);
});

test("normalizeWebUISkinId accepts only registered skin ids", () => {
  assert.equal(normalizeWebUISkinId("default-v1"), DEFAULT_WEBUI_SKIN_ID);
  assert.equal(normalizeWebUISkinId("aurora-glow-r3f-v1"), AURORA_GLOW_SKIN_ID);
  assert.equal(normalizeWebUISkinId("glass-jelly-r3f-v1"), DEFAULT_WEBUI_SKIN_ID);
  assert.equal(normalizeWebUISkinId(""), DEFAULT_WEBUI_SKIN_ID);
  assert.equal(normalizeWebUISkinId(undefined), DEFAULT_WEBUI_SKIN_ID);
});

test("applyWebUISkinOverride returns a config with normalized effective skin metadata", () => {
  const baseConfig = {
    skin: { id: "server-skin", name: "Server Skin" },
    effects: { aurora_glow: { enabled: true, opacity: 0.66 } },
  };

  const auroraConfig = applyWebUISkinOverride(baseConfig, AURORA_GLOW_SKIN_ID);
  assert.equal(auroraConfig.skin?.id, AURORA_GLOW_SKIN_ID);
  assert.equal(auroraConfig.skin?.name, getWebUISkin(AURORA_GLOW_SKIN_ID).label);
  assert.equal(auroraConfig.effects?.aurora_glow?.opacity, 0.66);
  assert.equal(isAuroraGlowSkin(auroraConfig), true);

  const fallbackConfig = applyWebUISkinOverride(baseConfig, "unknown-skin-v1");
  assert.equal(fallbackConfig.skin?.id, DEFAULT_WEBUI_SKIN_ID);
  assert.equal(fallbackConfig.skin?.name, getWebUISkin(DEFAULT_WEBUI_SKIN_ID).label);
  assert.equal(isAuroraGlowSkin(fallbackConfig), false);
});

test("current skin registry entries do not require reload", () => {
  assert.deepEqual(
    WEBUI_SKINS.map((skin) => [skin.id, skin.requiresReload]),
    [
      [DEFAULT_WEBUI_SKIN_ID, false],
      [AURORA_GLOW_SKIN_ID, false],
    ],
  );
});

test("stored skin helpers treat storage values as untrusted", () => {
  const store = new Map<string, string>();
  const storage = {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => {
      store.set(key, value);
    },
  };

  assert.equal(readStoredWebUISkinId(storage), DEFAULT_WEBUI_SKIN_ID);

  writeStoredWebUISkinId(AURORA_GLOW_SKIN_ID, storage);
  assert.equal(readStoredWebUISkinId(storage), AURORA_GLOW_SKIN_ID);

  writeStoredWebUISkinId("stale-skin-v1", storage);
  assert.equal(readStoredWebUISkinId(storage), DEFAULT_WEBUI_SKIN_ID);

  const throwingStorage = {
    getItem: () => {
      throw new Error("storage unavailable");
    },
    setItem: () => {
      throw new Error("storage unavailable");
    },
  };
  assert.equal(readStoredWebUISkinId(throwingStorage), DEFAULT_WEBUI_SKIN_ID);
  assert.doesNotThrow(() => writeStoredWebUISkinId(AURORA_GLOW_SKIN_ID, throwingStorage));
});

test("getWebUIFoldThinkingAfterFinalAnswer defaults to true when absent or invalid", () => {
  const invalidValue = "false" as unknown as boolean;

  assert.equal(getWebUIFoldThinkingAfterFinalAnswer(undefined), true);
  assert.equal(getWebUIFoldThinkingAfterFinalAnswer({}), true);
  assert.equal(getWebUIFoldThinkingAfterFinalAnswer({ components: {} }), true);
  assert.equal(getWebUIFoldThinkingAfterFinalAnswer({ components: { agent_steps: {} } }), true);
  assert.equal(
    getWebUIFoldThinkingAfterFinalAnswer({
      components: { agent_steps: { fold_after_final_answer: invalidValue } },
    }),
    true,
  );
});

test("getWebUIFoldThinkingAfterFinalAnswer honors an explicit boolean", () => {
  assert.equal(
    getWebUIFoldThinkingAfterFinalAnswer({
      components: { agent_steps: { fold_after_final_answer: true } },
    }),
    true,
  );
  assert.equal(
    getWebUIFoldThinkingAfterFinalAnswer({
      components: { agent_steps: { fold_after_final_answer: false } },
    }),
    false,
  );
});

test("getWebUIPageHeaderDisplay defaults to false unless explicitly boolean true", () => {
  assert.equal(getWebUIPageHeaderDisplay(undefined), false);
  assert.equal(getWebUIPageHeaderDisplay({}), false);
  assert.equal(getWebUIPageHeaderDisplay({ components: {} }), false);
  assert.equal(getWebUIPageHeaderDisplay({ components: { page_header: {} } }), false);
  assert.equal(getWebUIPageHeaderDisplay({ components: { page_header: { display: false } } }), false);

  const invalidValue = "true" as unknown as boolean;
  assert.equal(
    getWebUIPageHeaderDisplay({ components: { page_header: { display: invalidValue } } }),
    false,
  );

  assert.equal(getWebUIPageHeaderDisplay({ components: { page_header: { display: true } } }), true);
});

test("background alpha getters fall back to defaults for missing or invalid values", () => {
  assert.equal(getWebUIHeaderBackgroundAlpha(undefined), 0.7);
  assert.equal(getWebUIFooterBackgroundAlpha(undefined), 0.6);
  assert.equal(getWebUIHeaderBackgroundAlpha({}), 0.7);
  assert.equal(getWebUIFooterBackgroundAlpha({}), 0.6);
  assert.equal(getWebUIHeaderBackgroundAlpha({ layout: {} }), 0.7);
  assert.equal(getWebUIFooterBackgroundAlpha({ layout: {} }), 0.6);

  const numericStringValue = "0.5";
  assert.equal(getWebUIHeaderBackgroundAlpha({ layout: { header_background_alpha: numericStringValue } }), 0.5);
  assert.equal(getWebUIFooterBackgroundAlpha({ layout: { footer_background_alpha: Number.NaN } }), 0.6);

  assert.equal(getWebUIHeaderBackgroundAlpha({ layout: { header_background_alpha: "0.42" } }), 0.42);
  assert.equal(getWebUIFooterBackgroundAlpha({ layout: { footer_background_alpha: " not-a-number " } }), 0.6);
  assert.equal(getWebUIHeaderBackgroundAlpha({ layout: { header_background_alpha: 2 } }), 1);
  assert.equal(getWebUIFooterBackgroundAlpha({ layout: { footer_background_alpha: -1 } }), 0);

  assert.equal(getWebUIHeaderBackgroundAlpha({ layout: { header_background_alpha: 0.42 } }), 0.42);
  assert.equal(getWebUIFooterBackgroundAlpha({ layout: { footer_background_alpha: 0.33 } }), 0.33);
});
