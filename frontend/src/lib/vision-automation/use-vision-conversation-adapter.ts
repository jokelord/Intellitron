/** The route can render before config arrives; no input media capability exists until confirmed. */
export function hasVisionConversationCapability(config: {
  model?: { model_type?: string };
  vision?: { enabled?: boolean };
} | null | undefined): boolean {
  return config?.model?.model_type === 'vision' && config.vision?.enabled === true;
}
