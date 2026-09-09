import { isCodexHelperSuggestion, isTransientTranscriptNoise } from './pentacleEventInterpreter';
import type { PentacleEvent } from '../types/pentacle';

export type PentacleEventDropReason =
  | 'noise_filter'
  | 'dedupe'
  | 'helper_suggestion'
  | 'working_or_draft';

export type StreamFlowCounts = {
  inbound_count: number;
  persisted_count: number;
  dropped_count_by_reason: Record<string, number>;
  source_tag_counts: Record<string, number>;
};

const counts = new Map<string, StreamFlowCounts>();

function get(streamId: string): StreamFlowCounts {
  let current = counts.get(streamId);
  if (!current) {
    current = {
      inbound_count: 0,
      persisted_count: 0,
      dropped_count_by_reason: {},
      source_tag_counts: {},
    };
    counts.set(streamId, current);
  }
  return current;
}

function isClaudeJsonlEvent(event: PentacleEvent) {
  return event.raw?.source === 'claude-jsonl';
}

export function classifyDrop(
  event: PentacleEvent,
  options: { duplicate?: boolean } = {},
): PentacleEventDropReason | null {
  const kind = String(event.kind || '').toUpperCase();
  if (kind === 'DRAFT' || kind === 'WORKING') return 'working_or_draft';
  if (
    !isClaudeJsonlEvent(event) &&
    (kind === 'ASSIST' || kind === 'TOOL' || kind === 'TOOL-OUT') &&
    isTransientTranscriptNoise(event.text)
  ) {
    return 'noise_filter';
  }
  if (kind === 'USER' && isCodexHelperSuggestion(event.text)) return 'helper_suggestion';
  if (options.duplicate) return 'dedupe';
  return null;
}

export function recordInbound(event: PentacleEvent): void {
  const current = get(event.stream_id);
  current.inbound_count += 1;
  const source = String(event.raw?.source || '(none)');
  current.source_tag_counts[source] = (current.source_tag_counts[source] || 0) + 1;
}

export function recordPersistedDelta(streamId: string, delta: number): void {
  if (delta <= 0) return;
  get(streamId).persisted_count += delta;
}

export function recordDrop(streamId: string, reason: string): void {
  const current = get(streamId);
  current.dropped_count_by_reason[reason] = (current.dropped_count_by_reason[reason] || 0) + 1;
}

export function snapshotCounts(): Map<string, StreamFlowCounts> {
  return new Map(Array.from(counts.entries(), ([streamId, value]) => [streamId, {
    ...value,
    dropped_count_by_reason: { ...value.dropped_count_by_reason },
    source_tag_counts: { ...value.source_tag_counts },
  }]));
}

export function reset(): void {
  counts.clear();
}
