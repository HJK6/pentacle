// Renderer-owned additive metadata until the shared core exposes voice fields.
// Never infer a voice message from its text or from a neighboring event.
export type VoiceMetadata = { duration_s: number };

export function validVoiceMetadata(value: unknown): VoiceMetadata | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const duration = (value as VoiceMetadata).duration_s;
  return typeof duration === 'number' && Number.isFinite(duration) && duration >= 0
    ? { duration_s: duration } : undefined;
}
