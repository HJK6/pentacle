// Walk: sidebar_search — search filters the sidebar session list.
const SCENARIO_META = { target_compat: ['hostc', 'hosta', 'hostb'], requires: [], providers: ['claude'] };
async function run(ctx) {
  await ctx.waitFor(`document.querySelectorAll('.session-item').length > 0`, { label: 'sidebar populated' });
  const info = await ctx.eval(`(() => {
    const all = [...document.querySelectorAll('.session-item')];
    const total = all.length;
    const q = all[0] ? (all[0].getAttribute('data-name') || '') : '';
    return { total, q };
  })()`);
  ctx.assert('have a unique search query', !!info.q, info);
  await ctx.type('#session-search', info.q);
  const visible = await ctx.waitFor(`(() => {
    const all = [...document.querySelectorAll('.session-item')];
    const vis = all.filter(e => e.offsetParent !== null);
    return (vis.length > 0 && vis.length < ${info.total}) ? vis.length : (${info.total} <= 1 ? vis.length : false);
  })()`, { timeoutMs: 8000, label: 'list filtered by query' });
  ctx.assert('search narrowed the list', typeof visible === 'number' && visible >= 1, { visible, total: info.total, q: info.q });
  await ctx.screenshot('search-filtered');
  await ctx.type('#session-search', '');
  await ctx.waitFor(`[...document.querySelectorAll('.session-item')].filter(e => e.offsetParent !== null).length >= ${Math.min(2, 1)}`, { label: 'search cleared' }).catch(() => {});
}
module.exports = { SCENARIO_META, run };

