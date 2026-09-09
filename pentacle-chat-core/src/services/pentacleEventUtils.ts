type StreamEventLike = {
  daemon_seq: number;
  stream_id?: string;
  client_origin?: boolean;
  optimistic_id?: string;
};

type ProgressiveEventLike = {
  daemon_seq: number;
  text?: string;
  raw?: Record<string, unknown>;
};

// Stage 5b — progressive duplicate-seq merge.
//
// Daemon streams may re-emit a row at the same `daemon_seq` while the assistant
// is still composing it (e.g. claude-jsonl streams an ASSIST text that grows
// token by token). The reducer used to drop any duplicate-seq event entirely,
// which froze the visible row at its first-observed text and re-introduced
// Bug C ("reply doesn't render until nav-away") for streams that progressively
// extend a row.
//
// `mergeProgressiveUpdate` returns a merged event (preserving the prior event's
// identity — same daemon_seq and provenance fields — with `text` taken from the
// incoming update) iff:
//   - both events carry the same finite `daemon_seq`, AND
//   - the incoming text is a strict prefix-extension of the prior text
//     (`incoming.text.startsWith(prior.text)`).
//
// Returns null otherwise — same-seq with non-prefix text is a true duplicate
// (the daemon resent a stale snapshot of the row) and the reducer drops it.
// Different-seq events never merge through this helper; they append normally.
export function mergeProgressiveUpdate<P extends ProgressiveEventLike>(
  prior: P,
  incoming: ProgressiveEventLike,
): P | null {
  const priorSeq = Number(prior?.daemon_seq);
  const incomingSeq = Number(incoming?.daemon_seq);
  if (!Number.isFinite(priorSeq) || !Number.isFinite(incomingSeq)) return null;
  if (priorSeq !== incomingSeq) return null;
  const priorText = typeof prior?.text === 'string' ? prior.text : '';
  const incomingText = typeof incoming?.text === 'string' ? incoming.text : '';
  if (!incomingText.startsWith(priorText)) return null;
  if (incomingText === priorText && (incoming.raw === undefined || incoming.raw === prior.raw)) {
    return prior;
  }
  return { ...prior, text: incomingText, raw: incoming.raw ?? prior.raw };
}

export function dedupeRecentEvents<T extends StreamEventLike>(events: T[], limit: number) {
  const seen = new Set<number>();
  const seenOptimisticIds = new Set<string>();
  const deduped: T[] = [];

  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event?.client_origin === true) {
      const optimisticId = String(event.optimistic_id || '');
      if (!optimisticId || seenOptimisticIds.has(optimisticId)) {
        continue;
      }
      seenOptimisticIds.add(optimisticId);
      deduped.push(event);
      if (deduped.length >= limit) {
        break;
      }
      continue;
    }
    const seq = Number(event?.daemon_seq);
    if (!Number.isFinite(seq)) {
      continue;
    }
    if (seen.has(seq)) {
      continue;
    }
    seen.add(seq);
    deduped.push(event);
    if (deduped.length >= limit) {
      break;
    }
  }

  deduped.reverse();
  return deduped;
}

export function dedupeRecentEventsByStream<T extends StreamEventLike>(events: T[], limitPerStream: number) {
  const seenSeqByStream = new Map<string, Set<number>>();
  const seenOptimisticIds = new Set<string>();
  const counts = new Map<string, number>();
  const deduped: T[] = [];

  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    const streamId = String(event.stream_id || 'unknown');
    const currentCount = counts.get(streamId) || 0;
    if (currentCount >= limitPerStream) {
      continue;
    }
    if (event?.client_origin === true) {
      const optimisticId = String(event.optimistic_id || '');
      if (!optimisticId || seenOptimisticIds.has(optimisticId)) {
        continue;
      }
      seenOptimisticIds.add(optimisticId);
      counts.set(streamId, currentCount + 1);
      deduped.push(event);
      continue;
    }
    const seq = Number(event?.daemon_seq);
    if (!Number.isFinite(seq)) {
      continue;
    }
    let seenSeq = seenSeqByStream.get(streamId);
    if (!seenSeq) {
      seenSeq = new Set<number>();
      seenSeqByStream.set(streamId, seenSeq);
    }
    if (seenSeq.has(seq)) continue;
    seenSeq.add(seq);
    counts.set(streamId, currentCount + 1);
    deduped.push(event);
  }

  deduped.reverse();
  return deduped;
}
