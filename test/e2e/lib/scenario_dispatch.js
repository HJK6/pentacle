'use strict';

async function dispatchScenario(name, fn, ctx) {
  try {
    await fn(ctx);
    return false;
  } catch (error) {
    ctx.report.note(`scenario ${name} FAILED: ${error && error.message}`);
    try {
      await ctx.session.send('Emulation.clearDeviceMetricsOverride');
      if (ctx.session.eval) await ctx.session.eval('new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))');
      ctx.report.note(`scenario ${name} viewport cleanup: cleared`);
    }
    catch (cleanup) { ctx.report.note(`scenario ${name} viewport cleanup FAILED: ${cleanup && cleanup.message}`); }
    return true;
  }
}
module.exports = { dispatchScenario };
