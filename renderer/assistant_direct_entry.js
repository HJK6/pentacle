'use strict';

const UNAVAILABLE = 'Direct assistant target is unavailable or changed. Update the private binding.';

// An optional, private-profile web entry alias. Every field is exact; titles,
// provider names and role labels never participate in target selection.
function resolveAssistantDirectTarget(features, sessions, entryStreamId) {
  const raw = features?.assistantDirectTarget;
  if (raw === undefined || raw === null) return { enabled: false };
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return { enabled: true, error: 'Direct assistant binding is invalid.' };
  const sourceId = String(raw.sourceStreamId || '');
  const targetId = String(raw.streamId || '');
  const generation = String(raw.generation || '');
  if (sourceId && sourceId !== entryStreamId) return { enabled: false };
  if (!sourceId || !targetId || !generation || sourceId === targetId || !targetId.includes(':')) {
    return { enabled: true, error: 'Direct assistant binding is invalid.' };
  }
  const inventory = Array.isArray(sessions) ? sessions : [];
  const source = inventory.find(item => item?.stream_id === sourceId);
  const target = inventory.find(item => item?.stream_id === targetId);
  if (source?.session_kind !== 'assistant_composite' || !target
    || target.session_kind === 'assistant_composite'
    || target.session_generation !== generation
    || target.online !== true || target.host_status === 'offline'
    || target.status === 'closed' || !!target.closed_at) {
    return { enabled: true, error: UNAVAILABLE };
  }
  const colon = targetId.indexOf(':');
  const host = targetId.slice(0, colon);
  const sessionName = targetId.slice(colon + 1);
  if (!sessionName || (target.host && target.host !== host)
    || (target.session_name && target.session_name !== sessionName)) {
    return { enabled: true, error: UNAVAILABLE };
  }
  return { enabled: true, source, target, sourceId, targetId, generation, host, sessionName };
}

module.exports = { resolveAssistantDirectTarget, UNAVAILABLE };
