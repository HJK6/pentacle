'use strict';

async function readViewport(session) {
  const viewport = await session.eval('({width:innerWidth,height:innerHeight})');
  if (!viewport || !Number.isFinite(viewport.width) || !Number.isFinite(viewport.height)) {
    throw new Error('viewport measurement unavailable');
  }
  return viewport;
}

async function clearAndSettle(session, baseline, {timeoutMs = 2000, intervalMs = 25} = {}) {
  await session.send('Emulation.clearDeviceMetricsOverride');
  const deadline = Date.now() + timeoutMs;
  let observed;
  do {
    const remaining = Math.max(0, deadline - Date.now());
    if (!remaining) break;
    let timer;
    try {
      observed = await Promise.race([
        readViewport(session),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('viewport restore timed out')), remaining); }),
      ]);
    } finally { clearTimeout(timer); }
    if (observed.width === baseline.width && observed.height === baseline.height) return observed;
    if (Date.now() >= deadline) break;
    await new Promise(resolve => setTimeout(resolve, Math.min(intervalMs, Math.max(0, deadline - Date.now()))));
  } while (true);
  const last = observed ? `${observed.width}x${observed.height}` : 'unavailable';
  throw new Error(`viewport restore timed out: expected ${baseline.width}x${baseline.height}, observed ${last}`);
}
module.exports = { readViewport, clearAndSettle };
