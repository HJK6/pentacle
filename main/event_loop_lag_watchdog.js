'use strict';

const DEFAULT_INTERVAL_MS = 1000;
const DEFAULT_LAG_THRESHOLD_MS = 3000;
const DEFAULT_SUSTAINED_MS = 3000;

function createEventLoopLagDetector({
  lagThresholdMs = DEFAULT_LAG_THRESHOLD_MS,
  sustainedMs = DEFAULT_SUSTAINED_MS,
} = {}) {
  let stalledSince = null;
  let inEpisode = false;

  return {
    sample({ now, lagMs }) {
      const sampleNow = Number(now);
      const sampleLag = Number(lagMs);
      if (!Number.isFinite(sampleNow) || !Number.isFinite(sampleLag)) {
        return { shouldLog: false, inStall: inEpisode, sustainedForMs: 0 };
      }

      if (sampleLag < lagThresholdMs) {
        stalledSince = null;
        inEpisode = false;
        return { shouldLog: false, inStall: false, sustainedForMs: 0 };
      }

      if (stalledSince === null) stalledSince = sampleNow;
      const sustainedForMs = Math.max(0, sampleNow - stalledSince);
      const shouldLog = !inEpisode && (sustainedForMs >= sustainedMs || sampleLag >= sustainedMs);
      if (shouldLog) inEpisode = true;
      return { shouldLog, inStall: true, sustainedForMs: Math.max(sustainedForMs, sampleLag) };
    },
  };
}

function collectEventLoopLagContext({ processRef = process, getLastActivity } = {}) {
  const memory = typeof processRef.memoryUsage === 'function' ? processRef.memoryUsage() : null;
  const activeHandles = typeof processRef._getActiveHandles === 'function'
    ? processRef._getActiveHandles().length
    : null;
  const activeRequests = typeof processRef._getActiveRequests === 'function'
    ? processRef._getActiveRequests().length
    : null;
  let lastActivity = null;
  try {
    lastActivity = typeof getLastActivity === 'function' ? getLastActivity() : null;
  } catch {
    lastActivity = null;
  }
  return { memory, activeHandles, activeRequests, lastActivity };
}

function formatEventLoopLagDiagnostic({ now, lagMs, sustainedForMs, context }) {
  const timestamp = new Date(now).toISOString();
  const details = {
    timestamp,
    lag_ms: Math.round(lagMs),
    sustained_ms: Math.round(sustainedForMs),
    memory: context && context.memory ? context.memory : null,
    active_handles: context ? context.activeHandles : null,
    active_requests: context ? context.activeRequests : null,
    last_activity: context ? context.lastActivity : null,
  };
  return `[watchdog] event-loop lag sustained ${JSON.stringify(details)}`;
}

function startEventLoopLagWatchdog({
  logger = console,
  intervalMs = DEFAULT_INTERVAL_MS,
  lagThresholdMs = DEFAULT_LAG_THRESHOLD_MS,
  sustainedMs = DEFAULT_SUSTAINED_MS,
  nowMs = () => Date.now(),
  setIntervalFn = setInterval,
  clearIntervalFn = clearInterval,
  processRef = process,
  getLastActivity,
} = {}) {
  const detector = createEventLoopLagDetector({ lagThresholdMs, sustainedMs });
  let expected = nowMs() + intervalMs;
  const timer = setIntervalFn(() => {
    const now = nowMs();
    const lagMs = Math.max(0, now - expected);
    expected = now + intervalMs;
    const decision = detector.sample({ now, lagMs });
    if (!decision.shouldLog) return;
    const context = collectEventLoopLagContext({ processRef, getLastActivity });
    logger.warn(formatEventLoopLagDiagnostic({
      now,
      lagMs,
      sustainedForMs: decision.sustainedForMs,
      context,
    }));
  }, intervalMs);

  if (timer && typeof timer.unref === 'function') timer.unref();
  return {
    stop() {
      clearIntervalFn(timer);
    },
  };
}

module.exports = {
  DEFAULT_INTERVAL_MS,
  DEFAULT_LAG_THRESHOLD_MS,
  DEFAULT_SUSTAINED_MS,
  createEventLoopLagDetector,
  collectEventLoopLagContext,
  formatEventLoopLagDiagnostic,
  startEventLoopLagWatchdog,
};
