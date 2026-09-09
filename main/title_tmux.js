'use strict';

const DEFAULT_MISSING_SESSION_TTL_MS = 10 * 60 * 1000;
const DEFAULT_MISSING_SESSION_MAX_SIZE = 500;

function cacheKey(host, sessionName) {
  const hostId = host && host.id ? host.id : 'local';
  return `${hostId}:${sessionName}`;
}

function isMissingTmuxSessionError(error) {
  const msg = String(error && (error.stderr || error.message || error) || '').toLowerCase();
  return msg.includes("can't find session")
    || msg.includes('no such session')
    || msg.includes('session not found');
}

function createTitleTmuxHelpers({
  LocalHost,
  execFileSync,
  logger = console,
  nowMs = () => Date.now(),
  missingSessionTtlMs = DEFAULT_MISSING_SESSION_TTL_MS,
  missingSessionMaxSize = DEFAULT_MISSING_SESSION_MAX_SIZE,
} = {}) {
  if (!LocalHost) throw new Error('LocalHost is required');
  if (typeof execFileSync !== 'function') throw new Error('execFileSync is required');

  const missingSessions = new Map();

  function pruneMissingSessions() {
    const now = nowMs();
    for (const [key, entry] of missingSessions) {
      if (!entry || now >= entry.expiresAt) missingSessions.delete(key);
    }
    const maxSize = Math.max(1, Number(missingSessionMaxSize) || DEFAULT_MISSING_SESSION_MAX_SIZE);
    while (missingSessions.size > maxSize) {
      const oldestKey = missingSessions.keys().next().value;
      if (oldestKey === undefined) break;
      missingSessions.delete(oldestKey);
    }
  }

  function isSessionMissingCached(host, sessionName) {
    if (!host || !sessionName) return false;
    const key = cacheKey(host, sessionName);
    const entry = missingSessions.get(key);
    if (!entry) return false;
    if (nowMs() >= entry.expiresAt) {
      missingSessions.delete(key);
      return false;
    }
    return true;
  }

  function markSessionMissing(host, sessionName, error) {
    if (!host || !sessionName) return false;
    pruneMissingSessions();
    const key = cacheKey(host, sessionName);
    const alreadyCached = isSessionMissingCached(host, sessionName);
    missingSessions.set(key, {
      firstSeenAt: alreadyCached ? missingSessions.get(key).firstSeenAt : nowMs(),
      lastSeenAt: nowMs(),
      expiresAt: nowMs() + missingSessionTtlMs,
      error: String(error && (error.message || error) || '').slice(0, 300),
    });
    pruneMissingSessions();
    return !alreadyCached;
  }

  function markSessionLive(host, sessionName) {
    if (!host || !sessionName) return;
    pruneMissingSessions();
    missingSessions.delete(cacheKey(host, sessionName));
  }

  async function firstTmuxWindowTarget(host, sessionName) {
    if (!host || !sessionName) return '';
    if (isSessionMissingCached(host, sessionName)) return '';
    try {
      const args = ['list-windows', '-t', `=${sessionName}`, '-F', '#{window_id}'];
      const raw = host instanceof LocalHost
        ? execFileSync(host.tmuxBin, args, { encoding: 'utf8', env: host.env, timeout: 3000 })
        : await host.tmux(args, { lane: 'bg' });
      markSessionLive(host, sessionName);
      const target = String(raw || '').split('\n').map((line) => line.trim()).find(Boolean);
      return target || '';
    } catch (e) {
      const missing = isMissingTmuxSessionError(e);
      const shouldLog = missing ? markSessionMissing(host, sessionName, e) : true;
      if (shouldLog) logger.warn(`[title] window lookup failed for ${sessionName}: ${e.message || e}`);
      return '';
    }
  }

  async function renameTmuxWindow(host, sessionName, title) {
    if (!host || !sessionName || !title) return false;
    if (isSessionMissingCached(host, sessionName)) return false;
    const target = await firstTmuxWindowTarget(host, sessionName);
    if (!target) return false;
    try {
      if (host instanceof LocalHost) {
        execFileSync(host.tmuxBin, ['rename-window', '-t', target, title], { env: host.env, timeout: 3000 });
      } else {
        await host.tmux(['rename-window', '-t', target, title]);
      }
      markSessionLive(host, sessionName);
      return true;
    } catch (e) {
      if (isMissingTmuxSessionError(e)) markSessionMissing(host, sessionName, e);
      logger.warn(`[title] rename-window failed for ${sessionName} target=${target}: ${e.message || e}`);
      return false;
    }
  }

  return {
    firstTmuxWindowTarget,
    renameTmuxWindow,
    markSessionLive,
    markSessionMissing,
    isSessionMissingCached,
    pruneMissingSessions,
    missingSessionCount: () => missingSessions.size,
  };
}

module.exports = {
  DEFAULT_MISSING_SESSION_TTL_MS,
  DEFAULT_MISSING_SESSION_MAX_SIZE,
  createTitleTmuxHelpers,
  isMissingTmuxSessionError,
};
