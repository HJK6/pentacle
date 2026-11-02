'use strict';
const { readViewport, clearAndSettle } = require('./viewport_settle');

async function dispatchScenario(name, fn, ctx) {
  let baseline;
  try {
    baseline = await readViewport(ctx.session);
    await fn(ctx);
    return false;
  } catch (error) {
    ctx.report.note(`scenario ${name} FAILED: ${error && error.message}`);
    try {
      if (!baseline) {
        await ctx.session.send('Emulation.clearDeviceMetricsOverride');
        throw new Error('starting viewport unavailable');
      }
      await clearAndSettle(ctx.session, baseline);
      ctx.report.note(`scenario ${name} viewport cleanup: cleared`);
    } catch (cleanup) { ctx.report.note(`scenario ${name} viewport cleanup FAILED: ${cleanup && cleanup.message}`); }
    return true;
  }
}
module.exports = { dispatchScenario };
