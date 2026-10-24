import type React from "react";

export interface WebUIButtonSizeConfig {
  width_px?: number;
  height_px?: number;
}

export interface WebUIGlassJellyConfig {
  enabled?: boolean;
  intensity?: number;
  blur_px?: number;
  chromatic_aberration?: number;
  refraction_strength?: number;
  tint_a?: string;
  tint_b?: string;
  opacity?: number;
  animated?: boolean;
  max_dpr?: number;
}

export interface WebUIConfig {
  content?: {
    bubble_character_limit?: number;
  };
  layout?: {
    header_background_alpha?: number;
    footer_background_alpha?: number;
  };
  skin?: {
    id?: string;
    name?: string;
  };
  components?: {
    input_bar?: {
      min_height_px?: number;
      max_rows?: number;
      line_height_px?: number;
      vertical_padding_px?: number;
      border_radius_px?: number;
    };
    action_buttons?: {
      send?: WebUIButtonSizeConfig;
      stop?: WebUIButtonSizeConfig;
      answer_now?: WebUIButtonSizeConfig;
    };
    mermaid?: {
      min_height_px?: number;
    };
    page_header?: {
      display?: boolean;
      text_title?: string;
      vision_title?: string;
      subtitle?: string;
    };
    message_avatar?: {
      user_image_path?: string;
      assistant_image_path?: string;
      size_px?: number;
    };
    vision_welcome?: {
      title?: string;
      description?: string;
      icon_image_path?: string;
    };
    agent_steps?: {
      fold_after_final_answer?: boolean;
    };
    message_templates?: {
      items?: string[];
    };
  };
  effects?: {
    aurora_glow?: WebUIGlassJellyConfig;
    glass_jelly?: WebUIGlassJellyConfig;
  };
}

type WebUIStyleVars = React.CSSProperties & Record<`--webui-${string}`, string>;

export interface WebUISkinOption {
  id: string;
  label: string;
  description?: string;
  requiresReload: boolean;
}

type WebUISkinStorage = Pick<Storage, "getItem" | "setItem">;

export interface WebUIMessageTemplateStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export type WebUIMessageTemplateStorageState =
  | { kind: "absent"; items: null }
  | { kind: "valid"; items: string[] }
  | { kind: "invalid"; items: null }
  | { kind: "unavailable"; items: null };

export const DEFAULT_WEBUI_SKIN_ID = "default-v1";
export const AURORA_GLOW_SKIN_ID = "aurora-glow-r3f-v1";
export const WEBUI_SKIN_STORAGE_KEY = "llm-webui-skin-id";
export const MESSAGE_TEMPLATES_STORAGE_KEY = "llm-message-templates";
export const MESSAGE_TEMPLATE_IMMEDIATE_SEND_STORAGE_KEY = "llm-message-template-immediate-send";
export const DEFAULT_WEBUI_BUBBLE_CHARACTER_LIMIT = 20_000;
export const DEFAULT_WEBUI_HEADER_BACKGROUND_ALPHA = 0.7;
export const DEFAULT_WEBUI_FOOTER_BACKGROUND_ALPHA = 0.6;

function clampAlpha(value: number): number {
  return Math.max(0, Math.min(1, value));
}

function backgroundAlpha(
  config: WebUIConfig | undefined,
  key: "header_background_alpha" | "footer_background_alpha",
  fallback: number,
): number {
  const value: unknown = config?.layout?.[key];
  if (typeof value === "number") {
    return Number.isFinite(value) ? clampAlpha(value) : fallback;
  }
  if (typeof value === "string") {
    const parsed = Number(value.trim());
    return Number.isFinite(parsed) ? clampAlpha(parsed) : fallback;
  }
  return fallback;
}

export function getWebUIHeaderBackgroundAlpha(config: WebUIConfig | undefined): number {
  return backgroundAlpha(config, "header_background_alpha", DEFAULT_WEBUI_HEADER_BACKGROUND_ALPHA);
}

export function getWebUIFooterBackgroundAlpha(config: WebUIConfig | undefined): number {
  return backgroundAlpha(config, "footer_background_alpha", DEFAULT_WEBUI_FOOTER_BACKGROUND_ALPHA);
}

export const WEBUI_SKINS: WebUISkinOption[] = [
  {
    id: DEFAULT_WEBUI_SKIN_ID,
    label: "Default Skin v1",
    description: "默认界面皮肤",
    requiresReload: false,
  },
  {
    id: AURORA_GLOW_SKIN_ID,
    label: "Aurora Glow R3F v1",
    description: "炫光 WebGL 皮肤",
    requiresReload: false,
  },
];

const WEBUI_SKIN_BY_ID = new Map(WEBUI_SKINS.map((skin) => [skin.id, skin]));

function getDefaultStorage(): WebUISkinStorage | undefined {
  if (typeof window === "undefined") return undefined;
  return window.localStorage;
}

function getDefaultWebUIStorage(): WebUIMessageTemplateStorage | undefined {
  if (typeof window === "undefined") return undefined;
  return window.localStorage;
}

export function normalizeWebUIMessageTemplateItems(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  if (!value.every((item) => typeof item === "string" && item.trim().length > 0)) {
    return null;
  }
  return value.slice() as string[];
}

export function getWebUIMessageTemplateDefaults(config?: WebUIConfig): string[] {
  return normalizeWebUIMessageTemplateItems(
    config?.components?.message_templates?.items ?? [],
  ) ?? [];
}

export function readStoredWebUIMessageTemplates(
  storage: WebUIMessageTemplateStorage | undefined = getDefaultWebUIStorage(),
): WebUIMessageTemplateStorageState {
  if (!storage) return { kind: "unavailable", items: null };

  let raw: string | null;
  try {
    raw = storage.getItem(MESSAGE_TEMPLATES_STORAGE_KEY);
  } catch {
    return { kind: "unavailable", items: null };
  }
  if (raw === null) return { kind: "absent", items: null };

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { kind: "invalid", items: null };
  }
  const items = normalizeWebUIMessageTemplateItems(parsed);
  return items === null
    ? { kind: "invalid", items: null }
    : { kind: "valid", items };
}

export function resolveWebUIMessageTemplates(
  yamlDefaults: readonly string[],
  stored: WebUIMessageTemplateStorageState,
): string[] {
  return stored.kind === "valid" ? stored.items.slice() : Array.from(yamlDefaults);
}

export function writeStoredWebUIMessageTemplates(
  items: readonly string[],
  storage: WebUIMessageTemplateStorage | undefined = getDefaultWebUIStorage(),
): boolean {
  if (!storage) return false;
  try {
    storage.setItem(MESSAGE_TEMPLATES_STORAGE_KEY, JSON.stringify(Array.from(items)));
    return true;
  } catch {
    return false;
  }
}

export function readStoredWebUIMessageTemplateImmediateSend(
  storage: WebUIMessageTemplateStorage | undefined = getDefaultWebUIStorage(),
): boolean {
  if (!storage) return false;
  try {
    const raw = storage.getItem(MESSAGE_TEMPLATE_IMMEDIATE_SEND_STORAGE_KEY);
    if (raw === null) return false;
    return JSON.parse(raw) === true;
  } catch {
    return false;
  }
}

export function writeStoredWebUIMessageTemplateImmediateSend(
  enabled: boolean,
  storage: WebUIMessageTemplateStorage | undefined = getDefaultWebUIStorage(),
): boolean {
  if (!storage) return false;
  try {
    storage.setItem(MESSAGE_TEMPLATE_IMMEDIATE_SEND_STORAGE_KEY, JSON.stringify(enabled));
    return true;
  } catch {
    return false;
  }
}

export function clearStoredWebUIMessageTemplates(
  storage: WebUIMessageTemplateStorage | undefined = getDefaultWebUIStorage(),
): boolean {
  if (!storage) return false;
  try {
    storage.removeItem(MESSAGE_TEMPLATES_STORAGE_KEY);
    return true;
  } catch {
    return false;
  }
}

export function normalizeWebUISkinId(value: unknown): string {
  if (typeof value !== "string") return DEFAULT_WEBUI_SKIN_ID;
  const trimmed = value.trim();
  return WEBUI_SKIN_BY_ID.has(trimmed) ? trimmed : DEFAULT_WEBUI_SKIN_ID;
}

export function getWebUISkin(value: unknown): WebUISkinOption {
  return WEBUI_SKIN_BY_ID.get(normalizeWebUISkinId(value)) ?? WEBUI_SKINS[0];
}

export function readStoredWebUISkinId(storage: WebUISkinStorage | undefined = getDefaultStorage()): string {
  try {
    return normalizeWebUISkinId(storage?.getItem(WEBUI_SKIN_STORAGE_KEY));
  } catch {
    return DEFAULT_WEBUI_SKIN_ID;
  }
}

export function writeStoredWebUISkinId(
  skinId: unknown,
  storage: WebUISkinStorage | undefined = getDefaultStorage(),
): string {
  const normalized = normalizeWebUISkinId(skinId);
  try {
    storage?.setItem(WEBUI_SKIN_STORAGE_KEY, normalized);
  } catch {
    // localStorage may be blocked or unavailable; rendering should still work.
  }
  return normalized;
}

export function applyWebUISkinOverride(config: WebUIConfig | undefined, skinId: unknown): WebUIConfig {
  const skin = getWebUISkin(skinId);
  return {
    ...(config ?? {}),
    skin: {
      ...(config?.skin ?? {}),
      id: skin.id,
      name: skin.label,
    },
  };
}

function px(value: number | undefined, fallback: number): string {
  return `${Number.isFinite(value) ? value : fallback}px`;
}

function cssNumber(value: number | undefined, fallback: number): number {
  return Number.isFinite(value) ? value! : fallback;
}

function cssColor(value: string | undefined, fallback: string): string {
  return value && value.trim() ? value : fallback;
}

export function isAuroraGlowSkin(config?: WebUIConfig): boolean {
  const effect = config?.effects?.aurora_glow ?? config?.effects?.glass_jelly;
  return config?.skin?.id === AURORA_GLOW_SKIN_ID && effect?.enabled === true;
}

export function getGlassJellyConfig(config?: WebUIConfig): Required<WebUIGlassJellyConfig> {
  const glass = config?.effects?.aurora_glow ?? config?.effects?.glass_jelly ?? {};
  return {
    enabled: glass.enabled === true,
    intensity: cssNumber(glass.intensity, 0.72),
    blur_px: cssNumber(glass.blur_px, 18),
    chromatic_aberration: cssNumber(glass.chromatic_aberration, 0.018),
    refraction_strength: cssNumber(glass.refraction_strength, 0.045),
    tint_a: cssColor(glass.tint_a, "#ffffff"),
    tint_b: cssColor(glass.tint_b, "#76b900"),
    opacity: cssNumber(glass.opacity, 0.72),
    animated: glass.animated !== false,
    max_dpr: cssNumber(glass.max_dpr, 1.5),
  };
}

export function getWebUIPageTitle(config: WebUIConfig | undefined, mode: "text" | "vision"): string {
  const pageHeader = config?.components?.page_header;
  if (mode === "vision") {
    return pageHeader?.vision_title?.trim() || "LLM-Test 视觉模式";
  }
  return pageHeader?.text_title?.trim() || "LLM-Test";
}

export function getWebUIHeaderSubtitle(config: WebUIConfig | undefined): string {
  return config?.components?.page_header?.subtitle?.trim() || "NVIDIA Jetson Thor";
}

export function getWebUIPageHeaderDisplay(config: WebUIConfig | undefined): boolean {
  return config?.components?.page_header?.display === true;
}

export function getWebUIAvatarPath(config: WebUIConfig | undefined, role: "user" | "assistant"): string | undefined {
  const avatars = config?.components?.message_avatar;
  const value = role === "user" ? avatars?.user_image_path : avatars?.assistant_image_path;
  const trimmed = value?.trim();
  if (!trimmed) return undefined;
  if (/^(?:https?:)?\/\//i.test(trimmed) || trimmed.startsWith("data:") || trimmed.startsWith("blob:")) {
    return trimmed;
  }

  const publicMarker = "/public/";
  const normalized = trimmed.replace(/\\/g, "/");
  const publicIndex = normalized.lastIndexOf(publicMarker);
  if (publicIndex >= 0) {
    return `/${normalized.slice(publicIndex + publicMarker.length).replace(/^\/+/, "")}`;
  }
  if (normalized.startsWith("public/")) {
    return `/${normalized.slice("public/".length).replace(/^\/+/, "")}`;
  }
  if (normalized.startsWith("frontend/public/")) {
    return `/${normalized.slice("frontend/public/".length).replace(/^\/+/, "")}`;
  }
  if (normalized.includes("/frontend/public/")) {
    return `/${normalized.split("/frontend/public/").pop()?.replace(/^\/+/, "") ?? ""}`;
  }
  if (normalized.startsWith("/")) {
    return normalized;
  }
  return `/${normalized}`;
}

export function getWebUIAvatarSize(config: WebUIConfig | undefined): number {
  const value = config?.components?.message_avatar?.size_px;
  if (!Number.isFinite(value)) return 32;
  return Math.max(24, Math.min(96, Number(value)));
}

export function getWebUIBubbleCharacterLimit(config: WebUIConfig | undefined): number {
  const value = config?.content?.bubble_character_limit;
  return typeof value === "number" && Number.isFinite(value) && Number.isInteger(value) && value > 0
    ? value
    : DEFAULT_WEBUI_BUBBLE_CHARACTER_LIMIT;
}

export const DEFAULT_FOLD_THINKING_AFTER_FINAL_ANSWER = true;

export function getWebUIFoldThinkingAfterFinalAnswer(config: WebUIConfig | undefined): boolean {
  const value = config?.components?.agent_steps?.fold_after_final_answer;
  if (typeof value === "boolean") return value;
  return DEFAULT_FOLD_THINKING_AFTER_FINAL_ANSWER;
}

export function getWebUIVisionWelcomeConfig(config: WebUIConfig | undefined) {
  const welcome = config?.components?.vision_welcome;
  return {
    title: welcome?.title?.trim() || "Welcome to Vision Mode",
    description: welcome?.description?.trim() || "上传图片或使用摄像头拍照，询问关于图片的问题。",
    iconImagePath: getWebUIAvatarPath(
      { components: { message_avatar: { user_image_path: welcome?.icon_image_path } } },
      "user",
    ),
  };
}

export function buildWebUIStyleVars(config?: WebUIConfig): WebUIStyleVars {
  const buttons = config?.components?.action_buttons ?? {};
  const inputBar = config?.components?.input_bar ?? {};
  const mermaid = config?.components?.mermaid ?? {};
  const glass = getGlassJellyConfig(config);
  const lineHeight = Number.isFinite(inputBar.line_height_px) ? inputBar.line_height_px! : 24;
  const verticalPadding = Number.isFinite(inputBar.vertical_padding_px) ? inputBar.vertical_padding_px! : 24;
  const maxRows = Number.isFinite(inputBar.max_rows) ? inputBar.max_rows! : 6;
  const inputMaxHeight = lineHeight * maxRows + verticalPadding;
  return {
    "--webui-input-min-height": px(inputBar.min_height_px, 48),
    "--webui-input-max-height": `${inputMaxHeight}px`,
    "--webui-input-border-radius": px(inputBar.border_radius_px, 16),
    "--webui-header-bg-alpha": String(getWebUIHeaderBackgroundAlpha(config)),
    "--webui-footer-bg-alpha": String(getWebUIFooterBackgroundAlpha(config)),
    "--webui-send-button-width": px(buttons.send?.width_px, 48),
    "--webui-send-button-height": px(buttons.send?.height_px, 48),
    "--webui-stop-button-width": px(buttons.stop?.width_px, 48),
    "--webui-stop-button-height": px(buttons.stop?.height_px, 48),
    "--webui-answer-now-button-width": px(buttons.answer_now?.width_px, 48),
    "--webui-answer-now-button-height": px(buttons.answer_now?.height_px, 48),
    "--webui-mermaid-min-height": px(mermaid.min_height_px, 360),
    "--webui-glass-blur": px(glass.blur_px, 18),
    "--webui-glass-opacity": String(glass.opacity),
    "--webui-glass-intensity": String(glass.intensity),
    "--webui-glass-tint-a": glass.tint_a,
    "--webui-glass-tint-b": glass.tint_b,
  };
}

export function getWebUIInputBarResizeOptions(config?: WebUIConfig) {
  const inputBar = config?.components?.input_bar ?? {};
  const lineHeight = Number.isFinite(inputBar.line_height_px) ? inputBar.line_height_px! : 24;
  const verticalPadding = Number.isFinite(inputBar.vertical_padding_px) ? inputBar.vertical_padding_px! : 24;
  const maxRows = Number.isFinite(inputBar.max_rows) ? inputBar.max_rows! : 6;

  return {
    minHeightPx: Number.isFinite(inputBar.min_height_px) ? inputBar.min_height_px! : 48,
    maxHeightPx: lineHeight * maxRows + verticalPadding,
  };
}
