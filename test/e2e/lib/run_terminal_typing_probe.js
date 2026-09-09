'use strict';

// Serialized into the renderer; all timestamps belong to its monotonic clock.
async function runTerminalTypingProbe(createProbe, { slot, count = 240, durationMs = 60000, drainMs = 10000, cloneRate = 0, firstCodePoint = 0x4e00 }) {
  const term = state.terminals[slot].term;
  const probe = createProbe({ count, firstCodePoint });
  const originalWrite = term.write;
  const store = window.PentacleChatStore;
  const originalApply = store.applyFrame;
  const load = { frameCount: 0, cloneRequests: 0, cloneSettled: 0, cloneErrors: 0 };
  let renderSubscription;
  const start = performance.now();
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  try {
    term.write = function(data, callback) {
      const matched = probe.receive(data);
      return originalWrite.call(this, data, () => {
        probe.parsed(matched);
        if (callback) callback();
      });
    };
    renderSubscription = term.onRender(() => probe.rendered());
    store.applyFrame = function(frame) { load.frameCount += 1; return originalApply.call(this, frame); };
    for (let i = 0; i < count; i += 1) {
      await sleep(Math.max(0, start + i * durationMs / count - performance.now()));
      // Terminal.input uses xterm's normal onData → preload → main → SSH path.
      term.input(probe.send(), true);
      const cloneTarget = Math.floor((performance.now() - start) * cloneRate / 1000);
      while (load.cloneRequests < cloneTarget) {
        load.cloneRequests += 1;
        window.cc.getChatStreamState().catch(() => { load.cloneErrors += 1; })
          .finally(() => { load.cloneSettled += 1; });
      }
    }
    await sleep(drainMs);
    return { ...probe.result(), load, elapsedMs: performance.now() - start,
      config: { count, durationMs, drainMs, cloneRate, firstCodePoint },
      renderEndpoint: 'xterm onRender after write callback (not physical display scanout)' };
  } finally {
    term.write = originalWrite;
    store.applyFrame = originalApply;
    if (renderSubscription) renderSubscription.dispose();
  }
}

module.exports = { runTerminalTypingProbe };
