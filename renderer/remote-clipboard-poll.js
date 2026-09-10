function createRemoteClipboardPoller({
  fetchSince,
  writeClipboard,
  intervalMs = 1000,
  setIntervalFn = setInterval,
  clearIntervalFn = clearInterval,
  consoleRef = console,
} = {}) {
  let timer = null;
  let idx = 0;
  let running = false;
  let inFlight = false;
  let activeDrain = null;
  let stopDrain = null;
  let stopped = true;
  const canWrite = typeof writeClipboard === 'function';

  if (!canWrite) {
    consoleRef.warn('[mic] remote clipboard disabled: window.cc.writeClipboard is unavailable');
  }

  async function drainOnce() {
    try {
      const data = await fetchSince(idx);
      const lines = Array.isArray(data && data.lines) ? data.lines : [];
      for (const line of lines) {
        await writeClipboard(line);
      }
      if (data && Number.isFinite(Number(data.total))) {
        idx = Number(data.total);
      } else {
        idx += lines.length;
      }
    } catch (e) {
      consoleRef.warn('[mic] remote clipboard poll failed:', e && e.message ? e.message : e);
    }
  }

  async function tick() {
    if (!running || inFlight || !canWrite || typeof fetchSince !== 'function') return;
    inFlight = true;
    activeDrain = drainOnce();
    try {
      await activeDrain;
    } finally {
      activeDrain = null;
      inFlight = false;
    }
  }

  function start() {
    if (!canWrite || timer) return;
    running = true;
    stopped = false;
    stopDrain = null;
    idx = 0;
    tick();
    timer = setIntervalFn(tick, intervalMs);
  }

  function stop() {
    if (stopped) return stopDrain || Promise.resolve();
    stopped = true;
    running = false;
    if (timer) {
      clearIntervalFn(timer);
      timer = null;
    }
    if (!canWrite || typeof fetchSince !== 'function') {
      idx = 0;
      return Promise.resolve();
    }
    stopDrain = (activeDrain || Promise.resolve())
      .catch(() => {})
      .then(() => drainOnce())
      .finally(() => {
        idx = 0;
      });
    return stopDrain;
  }

  return { start, stop };
}

module.exports = {
  createRemoteClipboardPoller,
};
