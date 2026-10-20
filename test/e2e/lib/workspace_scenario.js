'use strict';
const fs = require('fs');
const path = require('path');
const { reloadDashboardPage } = require('./dashboard_scenario');
const { decodePng, diffImages } = require('./png_diff');

// Pane-count workspace (default on) and the optional chat-first look
// (features.chatFirstStyle). Real Chrome against the hermetic gate daemon,
// whose inventory includes one real assistant composite.
const WORKSPACE_KEY = 'pentacle.workspace.v1';
const SETTINGS_KEY = 'pentacle.settings.v1';
const EMPTY_FOUR = JSON.stringify({ paneCount: 4, activeSlot: 0 });

async function waitFor(ctx, expression, label, timeoutMs = 20000) {
  const { session, cdp } = ctx;
  const deadline = Date.now() + timeoutMs;
  let last;
  for (;;) {
    try { last = await session.eval(`(() => { try { return (${expression}); } catch (e) { return '__ERR__' + e.message; } })()`); } catch (e) { last = String(e.message); }
    if (last === true) return;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${label} (last: ${JSON.stringify(last)})`);
    await cdp.sleep(200);
  }
}

// Reload with the given localStorage values (null removes a key) and wait for
// the app and its daemon inventory.
// The page persists its live workspace on beforeunload, so the values are
// written by a new-document script that runs before the app on the reload.
async function reloadWith(ctx, values) {
  const { identifier } = await ctx.session.send('Page.addScriptToEvaluateOnNewDocument', {
    source: `for (const [k, v] of Object.entries(${JSON.stringify(values)})) {
      if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); }`,
  });
  try {
    await reloadDashboardPage(ctx, { keepWorkspace: true });
  } finally {
    await ctx.session.send('Page.removeScriptToEvaluateOnNewDocument', { identifier });
  }
  await waitFor(ctx, "document.querySelectorAll('#session-list .session-item').length > 0", 'sidebar inventory after reload');
}

function layoutProbe() {
  return `(() => {
    const grid = document.querySelector('.grid');
    const cells = [0, 1, 2, 3].map(slot => {
      const cell = document.getElementById('cell-' + slot);
      const rect = cell.getBoundingClientRect();
      return { slot, shown: getComputedStyle(cell).display !== 'none' && rect.width > 0,
        x: Math.round(rect.left), y: Math.round(rect.top), w: Math.round(rect.width), h: Math.round(rect.height),
        occupied: cell.classList.contains('occupied'), label: cell.querySelector('.cell-label')?.textContent || '',
        chat: (() => { const layer = cell.querySelector('.slot-chat-layer'); return !!layer && getComputedStyle(layer).display !== 'none'; })() };
    });
    const g = grid.getBoundingClientRect();
    return { paneCount: grid.dataset.paneCount, pressed: document.querySelector('[data-pane-count-button][aria-pressed="true"]')?.dataset.paneCountButton || null,
      grid: { x: Math.round(g.left), y: Math.round(g.top), w: Math.round(g.width), h: Math.round(g.height) },
      cells, visibleResizers: [...document.querySelectorAll('.grid-col-resizer')].filter(r => getComputedStyle(r).display !== 'none').length,
      sessions: document.querySelectorAll('#session-list .session-item').length,
      saved: localStorage.getItem('${WORKSPACE_KEY}') };
  })()`;
}

async function screenshot(ctx, name) {
  const { data } = await ctx.session.send('Page.captureScreenshot', { format: 'png' });
  const file = path.join(ctx.report.dir, `${name}.png`);
  fs.writeFileSync(file, Buffer.from(data, 'base64'));
  ctx.report.note(`screenshot ${file}`);
}

// Runs first, on the gate's fresh browser profile: no saved workspace.
async function defaultPaneAssistant(ctx) {
  const { report } = ctx;
  await waitFor(ctx, "document.querySelector('#cell-0')?.classList.contains('occupied') === true", 'first-visit assistant pane');
  const first = await ctx.session.eval(layoutProbe());
  report.note(`default-pane-assistant first visit: ${JSON.stringify({ paneCount: first.paneCount, cells: first.cells.map(c => [c.slot, c.shown, c.occupied, c.label, c.chat]) })}`);
  await screenshot(ctx, 'workspace-first-visit');
  report.ok('a first visit shows exactly one pane, selector at 1',
    first.paneCount === '1' && first.pressed === '1' && first.cells.filter(c => c.shown).length === 1 && first.cells[0].shown, first);
  report.ok('that pane holds the assistant composite chat in Chat view',
    first.cells[0].occupied && first.cells[0].chat && first.cells.slice(1).every(c => !c.occupied), first.cells[0]);
  await ctx.cdp.sleep(1500);
  const later = await ctx.session.eval(layoutProbe());
  report.ok('opening the default assistant spawned nothing (inventory unchanged)', later.sessions === first.sessions, { before: first.sessions, after: later.sessions });

  // A saved choice always wins: four panes, nothing open, then reload.
  await reloadWith(ctx, { [WORKSPACE_KEY]: JSON.stringify({ paneCount: 4, activeSlot: 0 }) });
  await ctx.cdp.sleep(1500);
  const saved = await ctx.session.eval(layoutProbe());
  report.ok('a saved four-pane choice wins on reload and the assistant is not forced back',
    saved.paneCount === '4' && saved.cells.every(c => c.shown) && saved.cells.every(c => !c.occupied), saved);
}

async function clickPaneCount(ctx, count) {
  await ctx.session.eval(`document.querySelector('[data-pane-count-button="${count}"]').click()`);
  await ctx.cdp.sleep(250);
  return ctx.session.eval(layoutProbe());
}

const rowFor = name => `#session-list .session-item[data-name=${JSON.stringify(name)}]`;
// The gate's other seeded sessions (local and local2 are both local hosts).
const EXTRA_SESSIONS = ['web-gate-survivor', 'web-gate-second-host'];

async function paneCountSelector(ctx) {
  const { report, fixture, tmux } = ctx;
  if (!fixture) { report.note('pane-count-selector: hermetic-only scenario skipped'); return; }
  // Real tmux sessions behind the seeded sessions, so each pane holds a live
  // terminal attachment (createPty maps session_name to the local tmux session).
  const names = [fixture.sessionName, ...EXTRA_SESSIONS];
  for (const name of names) {
    try { tmux(['kill-session', '-t', `=${name}`], { stdio: 'ignore' }); } catch {}
    tmux(['new-session', '-d', '-s', name, 'sh']);
  }
  try {
    await paneJourney(ctx, names);
  } finally {
    for (const name of names) { try { tmux(['kill-session', '-t', `=${name}`], { stdio: 'ignore' }); } catch {} }
  }
}

async function paneJourney(ctx, [fixtureName, survivor, secondHost]) {
  const { report, tmux } = ctx;
  const clients = name => { try { return tmux(['list-clients', '-t', `=${name}`, '-F', 'x']).split('\n').filter(Boolean).length; } catch { return 0; } };
  await ctx.session.eval(`(() => { window.__workspaceKills = [];
    const kill = window.cc.killPty.bind(window.cc);
    window.cc.killPty = (...args) => { window.__workspaceKills.push(args[0]); return kill(...args); };
    return true; })()`);
  await ctx.session.eval(`document.querySelector(${JSON.stringify(rowFor(fixtureName))}).click()`);
  await waitFor(ctx, "document.querySelector('#cell-0')?.classList.contains('occupied') === true", 'seeded session in the first empty visible pane');
  const attachDeadline = Date.now() + 15000;
  while (clients(fixtureName) < 1 && Date.now() < attachDeadline) await ctx.cdp.sleep(200);
  const attached = clients(fixtureName);
  report.ok('the seeded session holds a live tmux attachment', attached >= 1, { attached });

  const four = await clickPaneCount(ctx, 4);
  report.ok('four panes are the classic 2x2 grid with column resizers', four.cells.every(c => c.shown) && four.visibleResizers > 0
    && four.cells[0].y === four.cells[1].y && four.cells[2].y > four.cells[0].y, four);
  const three = await clickPaneCount(ctx, 3);
  const [left, topRight, bottomRight] = [0, 1, 2].map(slot => three.cells[slot]);
  report.ok('three panes: one tall pane left, two stacked right; no column resizers', three.cells.filter(c => c.shown).length === 3
    && left.shown && topRight.shown && bottomRight.shown && !three.cells[3].shown && three.visibleResizers === 0
    && left.x < topRight.x && topRight.x === bottomRight.x && bottomRight.y > topRight.y && left.h > topRight.h * 1.8, three);
  const two = await clickPaneCount(ctx, 2);
  report.ok('two panes side by side', two.cells.filter(c => c.shown).length === 2 && two.cells[0].y === two.cells[1].y && two.cells[0].x < two.cells[1].x, two);

  // Focus pane 2 and reduce to one pane: the session in pane 1 is hidden, not closed.
  await ctx.session.eval("document.getElementById('cell-1').dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))");
  const one = await clickPaneCount(ctx, 1);
  report.ok('one pane shows the focused pane; the hidden pane keeps its session', one.cells.filter(c => c.shown).length === 1
    && one.cells[1].shown && !one.cells[0].shown && one.cells[0].occupied && one.cells[0].label === four.cells[0].label, one);
  await ctx.cdp.sleep(1000);
  const hidden = { clients: clients(fixtureName), kills: await ctx.session.eval('window.__workspaceKills.slice()') };
  report.ok('the hidden pane keeps its tmux client attached (no detach)', hidden.clients >= 1 && hidden.kills.length === 0, hidden);
  const back = await clickPaneCount(ctx, 2);
  report.ok('raising the count reveals the still-attached session', back.cells[0].shown && back.cells[0].occupied
    && clients(fixtureName) >= 1, back);

  await ctx.session.eval("document.querySelector('#cell-0 .cell-maximize').click()");
  await ctx.cdp.sleep(250);
  await ctx.session.eval("document.querySelector('#cell-0 .cell-maximize').click()");
  const restored = await ctx.session.eval(layoutProbe());
  report.ok('maximize then restore returns to the chosen pane count', restored.paneCount === '2' && restored.cells.filter(c => c.shown).length === 2, restored);
  report.ok('the choice is persisted', JSON.parse(restored.saved || '{}').paneCount === 2, restored.saved);

  // Phone width: one readable pane; the sidebar is a drawer behind the toggle.
  await ctx.session.send('Emulation.setDeviceMetricsOverride', { width: 420, height: 860, deviceScaleFactor: 1, mobile: true });
  try {
    await waitFor(ctx, "document.body.classList.contains('web-sidebar-narrow')", 'narrow layout');
    const sidebarShown = () => ctx.session.eval("getComputedStyle(document.querySelector('.sidebar')).display !== 'none'");
    const closed = await sidebarShown();
    await ctx.session.eval("document.getElementById('sidebar-toggle').click()");
    const opened = await sidebarShown();
    await screenshot(ctx, 'workspace-phone-drawer');
    await ctx.session.eval("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))");
    const escaped = await sidebarShown();
    const narrow = await ctx.session.eval(layoutProbe());
    report.ok('phone width: drawer closed by default, opens from the toggle, Escape closes it', !closed && opened && !escaped, { closed, opened, escaped });
    report.ok('phone width shows one pane', narrow.cells.filter(c => c.shown).length === 1, narrow.cells);
  } finally {
    await ctx.session.send('Emulation.clearDeviceMetricsOverride', {});
  }

  // Two visible panes, both full: a selection replaces the focused pane, never
  // a hidden empty one.
  await ctx.session.eval(`document.querySelector(${JSON.stringify(rowFor(survivor))}).click()`);
  await waitFor(ctx, "document.querySelector('#cell-1')?.classList.contains('occupied') === true", 'second session in the empty visible pane');
  await ctx.session.eval("document.getElementById('cell-0').dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))");
  const full = await ctx.session.eval(layoutProbe());
  await ctx.session.eval(`document.querySelector(${JSON.stringify(rowFor(secondHost))}).click()`);
  await waitFor(ctx, `document.querySelector('#cell-0 .cell-label')?.textContent !== ${JSON.stringify(full.cells[0].label)}`, 'focused pane replaced');
  const replaced = await ctx.session.eval(layoutProbe());
  report.ok('with every visible pane full, a selection replaces the focused pane and leaves hidden panes alone',
    replaced.cells[0].occupied && replaced.cells[0].label !== full.cells[0].label && replaced.cells[1].label === full.cells[1].label
    && !replaced.cells[2].occupied && !replaced.cells[3].occupied && !replaced.cells[2].shown && !replaced.cells[3].shown,
    { before: full.cells, after: replaced.cells });

  // Leave the classic four-pane grid with nothing attached for later scenarios.
  await clickPaneCount(ctx, 4);
  for (const slot of [0, 1]) {
    await ctx.session.eval(`document.querySelector('#cell-${slot} .cell-close')?.click()`);
    await waitFor(ctx, `!document.querySelector('#cell-${slot}').classList.contains('occupied')`, `pane ${slot + 1} detached`);
  }
}

function styleProbe() {
  return `(() => {
    const root = document.documentElement;
    const css = getComputedStyle(root);
    return { chatFirst: root.classList.contains('chat-first-style'), theme: root.dataset.theme, density: root.dataset.density,
      bg: css.getPropertyValue('--bg').trim(), text: css.getPropertyValue('--pc-text').trim(),
      title: document.title, chats: document.getElementById('view-chats').textContent,
      search: document.getElementById('session-search').placeholder,
      empty: document.querySelector('#term-1 .cell-empty')?.textContent.trim(),
      brand: !!document.querySelector('.workspace-brand'), footer: !!document.querySelector('.workspace-sidebar-footer'),
      titlebarHeight: Math.round(document.querySelector('.titlebar').getBoundingClientRect().height),
      bodyFont: getComputedStyle(document.body).fontFamily };
  })()`;
}

// Everything the flag-off look is compared on against main: every CSS custom
// property any stylesheet declares (resolved on the root), theme/density,
// title, default view, sidebar labels, empty-pane copy and computed styles of
// the main chrome. `regions` names the default-on parts that may differ.
function classicProbe() {
  return `(() => {
    const names = new Set();
    const walk = list => { for (const rule of list) {
      if (rule.style) for (const prop of rule.style) if (prop.startsWith('--')) names.add(prop);
      if (rule.cssRules) walk(rule.cssRules);
    } };
    for (const sheet of document.styleSheets) { try { walk(sheet.cssRules); } catch { /* cross-origin */ } }
    const rootStyle = document.documentElement.style;
    for (let i = 0; i < rootStyle.length; i++) if (rootStyle[i].startsWith('--')) names.add(rootStyle[i]);
    const css = getComputedStyle(document.documentElement);
    const vars = Object.fromEntries([...names].sort().map(name => [name, css.getPropertyValue(name).trim()]));
    const PROPS = ['color', 'background-color', 'font-family', 'font-size', 'font-weight', 'line-height', 'height',
      'border-bottom-color', 'padding-top', 'padding-left'];
    const SELECTORS = ['body', '.titlebar', '.sidebar', '#session-search', '#view-chats', '#btn-new',
      '#session-list .session-item', '.grid', '#cell-0', '#header-0', '#header-0 .cell-label', '#term-0 .cell-empty', '#term-1 .cell-empty'];
    const styles = Object.fromEntries(SELECTORS.map(selector => {
      const el = document.querySelector(selector);
      return [selector, el ? Object.fromEntries(PROPS.map(prop => [prop, getComputedStyle(el).getPropertyValue(prop)])) : null];
    }));
    const labels = [...document.querySelectorAll('.sidebar button, .sidebar input, .sidebar [role="tab"], .sidebar h1, .sidebar h2, .sidebar h3')]
      .filter(el => !el.closest('#session-list'))
      .map(el => (el.textContent || '').trim() || el.placeholder || el.getAttribute('aria-label') || '');
    const box = el => { const r = el && el.getBoundingClientRect(); return r ? { x: Math.floor(r.left), y: Math.floor(r.top), w: Math.ceil(r.width), h: Math.ceil(r.height) } : null; };
    return { theme: document.documentElement.dataset.theme, density: document.documentElement.dataset.density, title: document.title,
      defaultView: document.querySelector('#settings-default-view-row .settings-segment-btn[aria-pressed="true"]')?.dataset.value || null,
      emptyCopy: [0, 1, 2, 3].map(slot => document.querySelector('#term-' + slot + ' .cell-empty')?.textContent.trim() || null),
      labels, vars, styles, regions: { titlebar: box(document.querySelector('.titlebar')),
        // Live data that differs between any two runs, main included.
        live: [...document.querySelectorAll('#session-list .activity-indicator.working')].map(box)
          .map(r => r && { x: r.x - 8, y: r.y - 8, w: r.w + 16, h: r.h + 16 })
          .concat([box(document.getElementById('machine-stats-section'))]) } };
  })()`;
}

function probeDifferences(main, candidate) {
  const out = [];
  for (const key of ['theme', 'density', 'title', 'defaultView']) if (main[key] !== candidate[key]) out.push(`${key}: ${main[key]} -> ${candidate[key]}`);
  for (const key of ['emptyCopy', 'labels']) if (JSON.stringify(main[key]) !== JSON.stringify(candidate[key])) out.push(`${key}: ${JSON.stringify(main[key])} -> ${JSON.stringify(candidate[key])}`);
  for (const name of new Set([...Object.keys(main.vars), ...Object.keys(candidate.vars)])) {
    if ((main.vars[name] || '') !== (candidate.vars[name] || '')) out.push(`var ${name}: ${main.vars[name] || ''} -> ${candidate.vars[name] || ''}`);
  }
  for (const selector of new Set([...Object.keys(main.styles), ...Object.keys(candidate.styles)])) {
    if (JSON.stringify(main.styles[selector]) !== JSON.stringify(candidate.styles[selector])) out.push(`style ${selector}`);
  }
  return out;
}

// Flag off at a fixed viewport: record the probe and a screenshot, and when
// PENTACLE_FLAG_OFF_BASELINE_DIR names a main run's report directory, require
// the probe to equal main's and every changed pixel to fall in a named region:
// the titlebar (default-on: its sidebar toggle and pane selector shift its
// contents) or live data (working spinners' animation phase, machine stats).
async function flagOffAgainstMain(ctx, theme, density) {
  const { report } = ctx;
  const name = `flag-off-${theme}-${density}`;
  // Machine stats arrive asynchronously; let the section settle so the probe
  // and the screenshot see the same layout.
  await waitFor(ctx, "getComputedStyle(document.getElementById('machine-stats-section')).display !== 'none'", 'machine stats shown', 10000)
    .catch(() => report.note(`${name}: machine stats section not shown`));
  await ctx.cdp.sleep(1000);
  const probe = await ctx.session.eval(classicProbe());
  const { data } = await ctx.session.send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(path.join(report.dir, `${name}.json`), JSON.stringify(probe, null, 2));
  fs.writeFileSync(path.join(report.dir, `${name}.png`), Buffer.from(data, 'base64'));
  const baselineDir = process.env.PENTACLE_FLAG_OFF_BASELINE_DIR;
  if (!baselineDir) { report.note(`${name}: recorded (no main baseline supplied)`); return; }
  const main = JSON.parse(fs.readFileSync(path.join(baselineDir, `${name}.json`), 'utf8'));
  const differences = probeDifferences(main, probe);
  report.ok(`flag off (${theme}/${density}) equals main: palette variables, theme, title, default view, labels, copy, chrome styles`,
    differences.length === 0, { differences: differences.slice(0, 40), count: differences.length });
  const regions = [{ name: 'titlebar (sidebar toggle, pane selector)', ...probe.regions.titlebar },
    ...probe.regions.live.filter(Boolean).map((rect, index) => ({ name: `live data ${index + 1}`, ...rect }))];
  const pixels = diffImages(decodePng(fs.readFileSync(path.join(baselineDir, `${name}.png`))), decodePng(Buffer.from(data, 'base64')), regions);
  fs.writeFileSync(path.join(report.dir, `${name}.diff.json`), JSON.stringify({ baselineDir, regions, pixels }, null, 2));
  report.ok(`flag off (${theme}/${density}) screenshot differs from main only inside named default-on regions`,
    !pixels.sizeMismatch && pixels.outside === 0, { regions, pixels });
}

const CLASSIC = { dark: '#0c1310', light: '#c8d0c6' };
const CHAT_FIRST = { dark: '#22221f', light: '#fcfbf8' };

async function chatFirstStyle(ctx) {
  const { report } = ctx;
  const observed = [];
  try {
    await ctx.session.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
    try {
      for (const theme of ['dark', 'light']) for (const density of ['comfortable', 'compact']) {
        await reloadWith(ctx, { [WORKSPACE_KEY]: EMPTY_FOUR, [SETTINGS_KEY]: JSON.stringify({ appearance: { theme, density }, features: {} }) });
        const off = await ctx.session.eval(styleProbe());
        observed.push({ flag: false, theme, density, off });
        report.ok(`flag off (${theme}/${density}): classic palette, copy and chrome`, !off.chatFirst && off.theme === theme
          && off.density === density && off.bg === CLASSIC[theme] && off.chats === 'Agents' && off.search === 'Search chats'
          && off.empty === 'Click a session or bot to attach' && !off.brand && !off.footer, off);
        await flagOffAgainstMain(ctx, theme, density);
      }
    } finally {
      await ctx.session.send('Emulation.clearDeviceMetricsOverride', {});
    }
    await reloadWith(ctx, { [WORKSPACE_KEY]: EMPTY_FOUR, [SETTINGS_KEY]: JSON.stringify({ features: { chatFirstStyle: true } }) });
    const fresh = await ctx.session.eval(styleProbe());
    observed.push({ flag: true, theme: 'unset', fresh });
    report.ok('flag on with no saved theme: chat-first Light look, copy and chrome', fresh.chatFirst && fresh.theme === 'light'
      && fresh.bg === CHAT_FIRST.light && fresh.title === 'Pentacle' && fresh.chats === 'Chats' && fresh.search === 'Search conversations'
      && fresh.brand && fresh.footer && fresh.titlebarHeight === 56 && /Segoe UI|system-ui|BlinkMacSystemFont/.test(fresh.bodyFont), fresh);
    await screenshot(ctx, 'style-on-light');
    for (const theme of ['dark', 'light']) for (const density of ['comfortable', 'compact']) {
      await reloadWith(ctx, { [WORKSPACE_KEY]: EMPTY_FOUR, [SETTINGS_KEY]: JSON.stringify({ appearance: { theme, density }, features: { chatFirstStyle: true } }) });
      const on = await ctx.session.eval(styleProbe());
      observed.push({ flag: true, theme, density, on });
      report.ok(`flag on (${theme}/${density}): saved theme and density win`, on.chatFirst && on.theme === theme
        && on.density === density && on.bg === CHAT_FIRST[theme], on);
      if (theme === 'dark' && density === 'comfortable') await screenshot(ctx, 'style-on-dark');
    }
  } finally {
    fs.writeFileSync(path.join(ctx.report.dir, 'chat-first-style-observations.json'), JSON.stringify(observed, null, 2));
    await reloadWith(ctx, { [WORKSPACE_KEY]: EMPTY_FOUR, [SETTINGS_KEY]: null });
  }
}

module.exports = { defaultPaneAssistant, paneCountSelector, chatFirstStyle, WORKSPACE_KEY, waitFor, reloadWith, layoutProbe };
