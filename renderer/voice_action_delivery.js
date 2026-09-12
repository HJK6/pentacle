'use strict';

// A local service may propose a typed action; the enrolled client retains the
// normal daemon catalog, authentication and idempotent spawn admission path.
function prepareVoiceSpawn(capture, response) {
  const action = capture?.action;
  const catalog = response?.catalog || response;
  const allowed = new Set(['version', 'route', 'task', 'host', 'provider', 'model', 'effort', 'effort_source']);
  if (!action || ![1, 2].includes(action.version) || action.route !== 'spawn_agent'
    || Object.keys(action).some(key => !allowed.has(key))
    || typeof capture.id !== 'string' || !/^[a-zA-Z0-9-]{1,64}$/.test(capture.id)
    || typeof action.task !== 'string' || (!action.task.trim() && action.version !== 2) || action.task.length > 4000
    || typeof action.host !== 'string' || !/^[a-zA-Z0-9_-]{1,64}$/.test(action.host)) {
    throw new Error('Invalid local spawn action');
  }
  const model = catalog?.models?.[action.provider]?.[action.model];
  const effort = action.effort;
  if (!catalog?.catalog_version || !model?.efforts?.includes(effort)) {
    throw new Error('Requested voice spawn configuration is unavailable');
  }
  return {
    host: action.host, provider: action.provider, model: action.model, effort,
    spawnProfile: 'desktop_manual', catalogVersion: catalog.catalog_version,
    resolutionSource: 'explicit_override',
    objective: Array.from(action.task.trim() || `Voice-opened ${action.model} agent`).slice(0, 120).join(''),
    initialPrompt: action.task.trim() ? `Operator voice request:\n${action.task.trim()}` : '',
    idempotencyKey: `voice:${capture.id}`,
  };
}

function spawnOutcome(response) {
  if (response?.state === 'queued') return 'queued';
  const stream = response?.streamId || response?.stream_id || response?.session?.stream_id;
  return response?.ok !== false && stream && (response?.state === 'ready' || response?.session?.bootstrap_state === 'ready') ? 'spawned' : 'unconfirmed';
}
module.exports = { prepareVoiceSpawn, spawnOutcome };
