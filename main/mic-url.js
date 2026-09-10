const warned = new Set();

function warnOnce(key, message) {
  if (warned.has(key)) return;
  warned.add(key);
  console.warn(message);
}

function resolveMicUrl(CONFIG) {
  const fallback = (CONFIG && CONFIG.micServerUrl) || 'http://127.0.0.1:7780';
  if (!CONFIG || !CONFIG.mic || !CONFIG.mic.useStreamHost) return fallback;
  const raw = CONFIG.chatStream && CONFIG.chatStream.url;
  if (!raw) {
    warnOnce('missing-chat-stream-url', '[mic] useStreamHost=true but chatStream.url is unset; falling back to micServerUrl');
    return fallback;
  }
  try {
    const wsUrl = new URL(raw);
    return `http://${wsUrl.hostname}:7780`;
  } catch (e) {
    warnOnce(`invalid-chat-stream-url:${raw}`, `[mic] useStreamHost=true but chatStream.url is invalid (${raw}); falling back to micServerUrl`);
    return fallback;
  }
}

function shouldSpawnLocalMicServer(CONFIG) {
  return !!(CONFIG && CONFIG.features && CONFIG.features.mic && !(CONFIG.mic && CONFIG.mic.useStreamHost) && !(CONFIG.mic && CONFIG.mic.autoSpawn === false));
}

async function probeMicServer(config, fetchStatus = fetch) {
  if (!config?.features?.mic) return false;
  try {
    const response = await fetchStatus(`${resolveMicUrl(config)}/status`, { signal: AbortSignal.timeout(2000) });
    return response.ok;
  } catch { return false; }
}

module.exports = {
  probeMicServer,
  resolveMicUrl,
  shouldSpawnLocalMicServer,
};
