'use strict';
const { execFileSync } = require('node:child_process');
const path = require('path');
function isolated(run) { return async ctx => { try { await run(ctx); } finally { await ctx.session.send('Emulation.clearDeviceMetricsOverride'); await ctx.session.send('Page.navigate', { url: ctx.url }); await ctx.session.waitFor("document.readyState === 'complete' && !!document.getElementById('session-list')", { timeoutMs: ctx.timeoutMs }); } }; }
async function visit(ctx, width = 1440) {
  await ctx.session.send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false });
  await ctx.session.send('Page.navigate', { url: ctx.url + 'next/' });
  await ctx.session.waitFor("!!window.cc && !!window.PentacleChatStore && document.querySelectorAll('.rail-button').length === 7", { timeoutMs: ctx.timeoutMs });
  await ctx.session.eval("document.documentElement.dataset.theme='dark'; localStorage.setItem('pentacle.next.theme','dark'); true");
  await ctx.session.waitFor("window.cc.getChatStreamState().then(s => s.connected === true && (s.sessions || []).length > 0)", { timeoutMs: ctx.timeoutMs });
}
async function homeRail(ctx) {
  const { session, report } = ctx;
  for (const width of [1440, 1010, 440]) {
    await visit(ctx, width);
    const expectedCount = await session.eval(`(async () => {
      const reply = await window.cc.promptList({ open: true });
      if (!reply?.ok || !Array.isArray(reply.questions)) throw new Error('home rail open-question read failed');
      const open = reply.questions.filter(question => question.state === 'open');
      if (open.some(question => !question.question_id)) throw new Error('open question has no ID');
      return new Set(open.map(question => question.question_id)).size;
    })()`);
    const expectedLabel = expectedCount ? `Questions (${expectedCount})` : 'Questions';
    const expectedNames = ['Assistant Fixture', 'Lanes', 'Dashboards', 'Chats', expectedLabel, 'Limits and machines', 'Settings'];
    await session.waitFor(`document.querySelectorAll('.rail-button')[4]?.dataset.count === ${JSON.stringify(String(expectedCount))}`,
      { timeoutMs: ctx.timeoutMs, label: `home rail hydrates the daemon's ${expectedCount} open questions at ${width}` });
    const info = await session.eval(`(() => {
      const rail = document.querySelector('.rail');
      const buttons = [...rail.querySelectorAll('.rail-button')];
      return {
        names: buttons.map(button => button.getAttribute('aria-label')),
        counts: buttons.filter(button => button.hasAttribute('data-count')).map(button => [button.getAttribute('aria-label'), button.dataset.count]),
        text: rail.innerText, input: !!rail.querySelector('input'),
        overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
        assistant: document.querySelector('.pane .thread-inner').dataset.streamId,
        lit: rail.querySelector('.brand').classList.contains('on'),
        chatLit: rail.querySelector('.chats-button').classList.contains('on'),
      };
    })()`);
    report.ok(`rail has seven icon-only controls at ${width}`, JSON.stringify(info.names) === JSON.stringify(expectedNames)
      && JSON.stringify(info.counts) === JSON.stringify([[expectedLabel, String(expectedCount)]])
      && !info.text.trim() && !info.input && !info.overflow && info.assistant === 'local:web-gate-assistant' && info.lit && !info.chatLit,
      { ...info, expectedCount });
    if (width === 1440) await session.screenshot(path.join(report.dir, 'next-home.png'));
    if (width === 440) await session.screenshot(path.join(report.dir, 'next-phone-home.png'));
    // The host now serves a newer build: one more control, above Limits, until the host serves this page's build again.
    const served = build => session.eval(`window.cc.getBuild = async () => ({ buildId: ${build} }); document.dispatchEvent(new Event('visibilitychange')); true`);
    await served("'a-newer-build'");
    await session.waitFor("!!document.querySelector('.rail .update-button')", { timeoutMs: ctx.timeoutMs, label: `update control appears at ${width}` });
    const update = await session.eval(`(() => {
      const rail = document.querySelector('.rail'), control = rail.querySelector('.update-button'), box = control.getBoundingClientRect(), frame = rail.getBoundingClientRect();
      return { names: [...rail.querySelectorAll('.rail-button')].map(button => button.getAttribute('aria-label')),
        next: control.nextElementSibling?.getAttribute('aria-label'), accent: (() => {
          const probe = document.createElement('i'); probe.style.color = 'var(--accent)'; rail.append(probe);
          const same = getComputedStyle(probe).color === getComputedStyle(control).color; probe.remove(); return same; })(),
        inside: box.left >= frame.left && box.right <= frame.right && box.top >= frame.top && box.bottom <= frame.bottom && box.width > 0,
        overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth };
    })()`);
    report.ok(`a newer build adds an eighth rail control above Limits at ${width}`, update.names.length === 8 && update.names.includes('Load the latest version')
      && update.next === 'Limits and machines' && update.accent && update.inside && !update.overflow, update);
    if (width === 440) await session.screenshot(path.join(report.dir, 'next-phone-update.png'));
    await served('window.__PENTACLE_CONFIG__.buildId');
    await session.waitFor("!document.querySelector('.rail .update-button') && document.querySelectorAll('.rail-button').length === 7", { timeoutMs: ctx.timeoutMs, label: `update control leaves at ${width}` });
  }
  await session.send('Emulation.clearDeviceMetricsOverride');
}
// Runs in the page. Real transcript rows that are wider than any pane: an unbroken
// token, a five-column table, a long code line and opened agent rows.
function wideTranscriptLayout() {
  const inner = document.querySelector('.card .thread-inner'), thread = inner.parentElement;
  const real = inner.innerHTML; // put back at the end, so no later render has to repaint mid-check
  const long = 'x'.repeat(400);
  const item = (id, text, extra = {}) => ({ id, text, displayRule: 'bubble:assistant', timestamp: '2026-01-01T00:00:00Z', ...extra });
  const table = ['| Stage | What ships | Earliest date | Owner | Notes |', '|---|---|---|---|---|',
    `| 0 | First switch of the release | Today, late morning | lead | ${long.slice(0, 60)} |`].join('\n');
  inner.innerHTML = window.PentacleChatView.renderTranscriptTimelineHtml({ streamId: 'local:layout', transcriptItems: [
    item('a', `A long token ${long}\n\n${table}\n\n\`\`\`\n${long}\n\`\`\``),
    item('b', `GATE — ${long}`, { displayRule: 'bubble:agent', tone: 'agent', label: 'v2-abc123' }),
    item('c', long, { displayRule: 'bubble:agent', tone: 'agent', label: 'Daemon' }),
  ] });
  for (const details of inner.querySelectorAll('details')) details.open = true;
  const edge = inner.getBoundingClientRect().right + 1;
  const wide = [...inner.querySelectorAll('*')].filter(el => !el.closest('.slot-chat-md-table') && el.getBoundingClientRect().right > edge)
    .map(el => String(el.className || el.tagName));
  const wrap = inner.querySelector('.slot-chat-md-table-wrap');
  const result = { sideways: thread.scrollWidth - thread.clientWidth, wide, width: Math.round(inner.getBoundingClientRect().width),
    table: !!wrap && getComputedStyle(wrap.querySelector('th')).borderBottomStyle === 'solid',
    agents: inner.querySelectorAll('.slot-chat-v3-subagent').length,
    agentRow: getComputedStyle(inner.querySelector('.slot-chat-v3-subagent > summary')).display,
    agentTime: getComputedStyle(inner.querySelector('.slot-chat-v3-subagent > summary time')).display };
  // Headers and short cells keep to one line while the table's frame is wider than 520 px.
  // A cell stretches to its row, so count the line boxes of its text instead.
  const lines = el => { const range = document.createRange(); range.selectNodeContents(el); return new Set([...range.getClientRects()].map(box => Math.round(box.top))).size; };
  const short = [...wrap.querySelectorAll('td.is-short')];
  Object.assign(result, { frame: wrap.clientWidth, short: short.map(td => td.textContent),
    headLines: Math.max(...[...wrap.querySelectorAll('th')].map(lines)), shortLines: Math.max(...short.map(lines)),
    shortWrap: short.map(td => getComputedStyle(td).whiteSpace) });
  inner.innerHTML = real;
  return result;
}
// Runs in the page. "Loading messages…" alone in a chat sits mid-pane, not at the foot.
function loneHistoryLine() {
  const inner = document.querySelector('.card .thread-inner'), thread = inner.parentElement;
  const real = inner.innerHTML;
  inner.innerHTML = '<div class="next-history-note" role="status">Loading messages…</div>';
  const note = inner.firstElementChild.getBoundingClientRect(), pane = thread.getBoundingClientRect();
  const result = { offset: Math.round((note.top + note.bottom) / 2 - (pane.top + pane.bottom) / 2), pane: Math.round(pane.height) };
  inner.innerHTML = real;
  return result;
}
// Runs in the page. A reader at the latest message opens a long agent row there:
// the row they clicked stays where it was instead of the chat jumping to its end.
async function openedRowStays() {
  const inner = document.querySelector('.card .thread-inner'), thread = inner.parentElement;
  const real = inner.innerHTML;
  const frames = async count => { for (let i = 0; i < count; i++) await new Promise(resolve => requestAnimationFrame(resolve)); };
  const item = (id, text, extra = {}) => ({ id, text, displayRule: 'bubble:assistant', timestamp: '2026-01-01T00:00:00Z', ...extra });
  inner.innerHTML = window.PentacleChatView.renderTranscriptTimelineHtml({ streamId: 'local:layout', transcriptItems: [
    item('a', Array.from({ length: 60 }, (_, i) => `Line ${i}`).join('\n\n') + '\n\n```\ncode\n```\n\n- item'),
    item('b', Array.from({ length: 200 }, (_, i) => `Report line ${i}`).join('\n'), { displayRule: 'bubble:agent', tone: 'agent', label: 'v2-abc123' }),
  ] });
  thread.scrollTop = thread.scrollHeight;
  await frames(3);
  const summary = inner.querySelector('.slot-chat-v3-subagent > summary');
  // A real update can repaint the transcript between frames; the caller then asks again.
  if (!summary) return { repainted: true };
  const gap = el => Math.round(el.getBoundingClientRect().top - el.previousElementSibling.getBoundingClientRect().bottom);
  const result = { atLatest: thread.scrollHeight - thread.scrollTop - thread.clientHeight < 2, before: Math.round(summary.getBoundingClientRect().top),
    codeGap: gap(inner.querySelector('.slot-chat-md-code')), listGap: gap(inner.querySelector('.slot-chat-md-list')) };
  summary.click();
  await frames(4);
  result.after = Math.round(summary.getBoundingClientRect().top);
  result.grew = thread.scrollHeight - thread.scrollTop - thread.clientHeight;
  if (!summary.isConnected) return { repainted: true };
  inner.innerHTML = real;
  return result;
}
// Runs in the page. An opened group of agent reports, and of answers, stays open
// when the transcript repaints with one more of each.
function openGroupGrows() {
  const inner = document.querySelector('.card .thread-inner');
  const real = inner.innerHTML;
  const view = window.PentacleChatView;
  const agent = n => ({ id: 'agent-' + n, text: 'GATE — step ' + n, displayRule: 'bubble:agent', tone: 'agent', label: 'v2-gate' + n, timestamp: '2026-01-01T00:00:0' + n + 'Z' });
  const answer = n => ({ id: 'answer-' + n, text: 'Operator answered: ' + n, isUser: true, eventCase: 'agent-question-answer', timestamp: '2026-01-01T00:01:0' + n + 'Z' });
  const html = (from, to) => view.renderTranscriptTimelineHtml({ streamId: 'local:layout', transcriptItems: [
    ...Array.from({ length: to - from + 1 }, (_, i) => agent(from + i)), ...Array.from({ length: to - from + 1 }, (_, i) => answer(from + i)) ] });
  const groups = () => [inner.querySelector('details.slot-chat-v3-subagent-group'), inner.querySelector('details.slot-chat-v3-answers')];
  // One report opened on its own, then a second one forms a group around it.
  view.replaceTranscriptHtml(inner, html(2, 2));
  inner.querySelector('details.slot-chat-v3-subagent').open = true;
  view.replaceTranscriptHtml(inner, html(2, 3));
  const child = inner.querySelector('details.slot-chat-v3-subagent-group details.slot-chat-v3-subagent pre');
  // A closed details may still report its content's size, so check every enclosing one is open.
  const shown = el => { for (let d = el.closest('details'); d; d = d.parentElement.closest('details')) if (!d.open) return false; return true; };
  const formed = !!child && shown(child);
  for (const details of groups()) details.open = true;
  view.replaceTranscriptHtml(inner, html(2, 4));
  const appended = groups().map(details => !!details?.open);
  view.replaceTranscriptHtml(inner, html(1, 4));
  const prepended = groups().map(details => !!details?.open);
  const result = { label: groups()[0]?.querySelector('summary')?.getAttribute('aria-label') || '', formed, appended, prepended };
  inner.innerHTML = real;
  return result;
}
const shortCells = layout => JSON.stringify(layout.short) === JSON.stringify(['0', 'lead']) && layout.headLines === 1 && layout.shortLines === 1
  && layout.shortWrap.every(value => value === (layout.frame > 520 ? 'nowrap' : 'normal'));
async function chatCard(ctx) {
  const { session, report } = ctx; await visit(ctx);
  await session.click('.chats-button');
  report.ok('open chats popover lights its rail control', await session.eval("document.querySelector('.chats-button').classList.contains('open')"));
  await session.waitFor(`!!document.querySelector('.popover [data-stream-id="local:web-gate-1"]')`, { timeoutMs: ctx.timeoutMs });
  await session.click('.popover [data-stream-id="local:web-gate-1"]');
  await session.waitFor("!document.querySelector('.card').hidden && document.querySelector('.card-title').textContent.length>0", { timeoutMs: ctx.timeoutMs });
  report.ok('opening a seeded chat displays its card beside Assistant', await session.eval("!document.querySelector('.card').hidden && !document.querySelector('.pane').hidden"));
  report.ok('the visible chat lights the chats control', await session.eval("document.querySelector('.chats-button').classList.contains('on')"));
  report.ok('the two chat panes share the stage evenly', await session.eval("Math.abs(document.querySelector('.pane').getBoundingClientRect().width-document.querySelector('.card').getBoundingClientRect().width)<20"));
  await session.click('[aria-label="Full screen"]'); report.ok('full screen hides Assistant', await session.eval("document.querySelector('.pane').hidden"));
  await session.screenshot(path.join(report.dir, 'next-full.png'));
  await session.send('Input.dispatchKeyEvent', { type:'keyDown', key:'Escape', code:'Escape' });
  report.ok('Escape returns to the split view', await session.eval("!document.querySelector('.pane').hidden"));
  await session.click('[aria-label="Full screen"]'); await session.click('.rail .brand');
  report.ok('logo brings Assistant out over full screen', await session.eval("!!document.querySelector('.assistant-overlay:not([hidden])') && !!document.querySelector('.chat-card:not([hidden]) [aria-label=\"Leave full screen\"]')"));
  await session.click('.assistant-keep');
  await session.click('.chats-button'); await session.click('.popover [data-stream-id="local:web-gate-survivor"]');
  report.ok('a second chat replaces the first after Assistant was last used', await session.eval("document.querySelector('.card .thread-inner').dataset.streamId === 'local:web-gate-survivor'"));
  await session.screenshot(path.join(report.dir, 'next-chat.png'));
  for (const width of [1440, 760, 440]) {
    await session.send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false });
    const layout = await session.eval(`(${wideTranscriptLayout})()`);
    report.ok(`wide tables, code and agent rows fit the chat without sideways scroll at ${width}`,
      layout.sideways === 0 && !layout.wide.length && layout.table && layout.agents === 2 && layout.agentRow === 'flex', layout);
    report.ok(`table headers and short words stay whole, and short cells may wrap only in a narrow frame, at ${width}`, shortCells(layout) && (width > 440 || layout.frame <= 520), layout);
    report.ok(`agent row times show on wide screens and hide on a phone at ${width}`, width < 600 ? layout.agentTime === 'none' : layout.agentTime !== 'none', layout);
    if (width === 760) await session.screenshot(path.join(report.dir, 'next-chat-wide-content.png'));
  }
  await session.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await session.click('.card [aria-label="Full screen"]');
  const full = await session.eval(`(${wideTranscriptLayout})()`);
  report.ok('in a frame wider than 520 px, short table cells keep to one line', full.frame > 520 && shortCells(full), full);
  await session.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape' });
  const lone = await session.eval(`(${loneHistoryLine})()`);
  report.ok('a history line alone in a chat sits mid-pane', lone.pane > 300 && Math.abs(lone.offset) < 24, lone);
  await session.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  let opened;
  for (let attempt = 0; attempt < 3; attempt++) {
    opened = await session.eval(`(${openedRowStays})()`);
    if (!opened.repainted) break;
  }
  report.ok('an agent row opened at the latest message stays where it was clicked', opened.atLatest && opened.grew > 400 && Math.abs(opened.after - opened.before) < 2, opened);
  report.ok('code and lists stand apart from the text above them', opened.codeGap >= 8 && opened.listGap >= 8, opened);
  const grows = await session.eval(`(${openGroupGrows})()`);
  report.ok('an opened agent row stays shown as a group forms, and open groups stay open as items join at either end', /4 subagent updates/.test(grows.label) && grows.formed && [...grows.appended, ...grows.prepended].every(Boolean), grows);
  await session.click('[aria-label="Close"]'); report.ok('closing the card returns to Assistant', await session.eval("document.querySelector('.card').hidden && !document.querySelector('.pane').hidden"));
  await session.click('.chats-button'); report.ok('closing the card leaves the session in the list', await session.eval("!!document.querySelector('.popover [data-stream-id=\"local:web-gate-survivor\"]')"));
  report.ok('closing the card does not close its daemon session', await session.eval("window.cc.getChatStreamState().then(s=>(s.sessions||[]).some(x=>x.stream_id==='local:web-gate-survivor' && x.status!=='closed' && !x.closed_at))"));
}
module.exports = { homeRail: isolated(homeRail), chatCard: isolated(chatCard) };
async function attachmentThumbnail(ctx) {
  const { session, report, runtime } = ctx;
  const scratch = runtime?.fixtureScratch;
  if (!ctx.fixture || !runtime.fixtureDaemonPort || !scratch ||
      !path.basename(scratch).startsWith('pentacle-web-gate-')) {
    throw new Error('thumbnail proof requires a disposable web-gate fixture');
  }
  const seeded = JSON.parse(execFileSync(ctx.python, [
    path.join(__dirname, 'seed_web_gate.py'), '--db', path.join(scratch, 'sessions.db'),
    '--blob-root', path.join(scratch, 'blobs'), '--next-thumbnail',
  ], { encoding: 'utf8' }));
  await visit(ctx);
  await session.click('.rail .chats-button');
  await session.click(`.popover [data-stream-id="${seeded.stream_id}"]`);
  const thumbnail = JSON.stringify(`.card .slot-chat-media-button[data-attachment-key="${seeded.digest}"]`);
  await session.waitFor(`!!document.querySelector(${thumbnail})`, { timeoutMs: ctx.timeoutMs, label: 'persisted attachment shown in chat' });
  await session.eval(`document.querySelector(${thumbnail}).scrollIntoView({ block: 'center' }); true`);
  const loaded = `(() => {
    const node = document.querySelector(${thumbnail});
    const image = node?.querySelector('img');
    return image?.naturalWidth === 48 && image.naturalHeight === 32 &&
      !node.querySelector('.slot-chat-media-loading');
  })()`;
  try {
    await session.waitFor(loaded, { timeoutMs: Math.min(ctx.timeoutMs, 5000), label: 'ordinary PNG decodes through the Next attachment bridge' });
    report.ok('persisted PNG is decoded and its loading label clears', true);
  } catch (error) {
    const evidence = await session.eval(`(() => {
      const node = document.querySelector(${thumbnail});
      const image = node?.querySelector('img');
      return { route: location.pathname, alt: image?.alt, width: image?.naturalWidth,
        height: image?.naturalHeight, pending: image?.hasAttribute('data-needs-blob'),
        loading: node?.querySelector('.slot-chat-media-loading')?.textContent };
    })()`);
    report.ok('persisted PNG is decoded and its loading label clears', false, evidence);
    throw error;
  } finally {
    await session.screenshot(path.join(report.dir, 'next-ordinary-image.png'));
  }
}
module.exports.attachmentThumbnail = isolated(attachmentThumbnail);
async function fixedLanesWidth(ctx) {
  const { session, report } = ctx;
  await visit(ctx);
  await session.click('.rail .chats-button');
  await session.click('.popover [data-stream-id="local:web-gate-1"]');
  const readPanes = `(() => {
    const stage = document.querySelector('.stage');
    const bounds = node => node?.getBoundingClientRect().toJSON();
    const chat = stage.querySelector('.card:not(.lanes-card):not(.design-card)');
    return { assistant: bounds(stage.querySelector('.pane:not([hidden])')),
      chat: bounds(chat), lanes: bounds(stage.querySelector('.lanes-card:not([hidden])')) };
  })()`;
  const before = await session.eval(readPanes);
  if (!before.assistant?.width || !before.chat?.width || !before.lanes?.width ||
      before.chat.right > before.lanes.left) throw new Error('three visible adjacent panes required');
  const x = (before.chat.right + before.lanes.left) / 2;
  const y = before.chat.top + 100;
  const delta = 44;
  const hit = await session.eval(`(() => {
    const target = document.elementFromPoint(${x}, ${y});
    return { className: target?.className ?? null, handle: !!target?.closest('.grip,[role="separator"]') };
  })()`);
  await session.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y });
  await session.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 });
  await session.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: x - delta, y, button: 'left', buttons: 1 });
  await session.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: x - delta, y, button: 'left', buttons: 0, clickCount: 1 });
  const after = await session.eval(readPanes);
  await session.screenshot(path.join(report.dir, 'next-lanes-fixed-width.png'));
  const fixed = !hit.handle && before.lanes.width === 344 && after.lanes.width === before.lanes.width &&
    after.chat.width === before.chat.width && after.assistant.width === before.assistant.width &&
    after.chat.right <= after.lanes.left;
  report.ok('Lanes stays fixed with no resize handle at the chat boundary', fixed, { before, after, hit });
  if (!fixed) throw new Error('chat/Lanes boundary must leave Lanes fixed at 344 px');
}
module.exports.fixedLanesWidth = isolated(fixedLanesWidth);
async function newChat(ctx) {
  const { session, report } = ctx; await visit(ctx);
  const catalogFlag = path.join(ctx.runtime.fixtureScratch, 'next-catalog-fixture'); require('fs').writeFileSync(catalogFlag, 'enabled');
  try {
  await session.eval(`(() => {
    window.__nextRequests=[]; window.__pentacleNextAdmitted=[]; window.__nextReply={ok:false,error:'Fixture refusal'};
    window.cc.chatSpawnV2=async req=>{window.__nextRequests.push(req);const reply=window.__nextReply;if(reply.ok&&reply.streamId)window.__pentacleNextAdmitted.push(reply.streamId);return reply}; return true;
  })()`);
  await session.click('.chats-button'); await session.click('[aria-label="New chat"]');
  await session.waitFor("!!document.querySelector('[data-testid=\"set-provider-claude\"]')", { timeoutMs: ctx.timeoutMs });
  report.ok('provider tiles show catalog counts and Start is disabled', await session.eval("document.querySelector('[data-testid=\"set-provider-claude\"]').textContent.includes('1 model') && document.querySelector('[data-testid=\"set-provider-codex\"]').textContent.includes('1 model') && document.querySelector('[data-testid=\"start-chat\"]').disabled"));
  await session.click('[data-testid="set-provider-claude"]'); await session.click('[data-testid="set-model-fixture-model"]'); await session.click('[data-testid="set-account-ours"]');
  report.ok('account uses the current-web alias fallback and permits Start', await session.eval("document.querySelector('[data-testid=\"set-account-ours\"]').textContent==='ours' && !document.querySelector('[data-testid=\"start-chat\"]').disabled"));
  await session.click('[data-testid="start-chat"]'); await session.waitFor("document.querySelector('.start-error')?.textContent === 'Fixture refusal'", { timeoutMs: ctx.timeoutMs });
  report.ok('a resolved refusal retains the choices and displays its reason', await session.eval("document.querySelector('[data-testid=\"set-provider-claude\"]').getAttribute('aria-checked')==='true' && document.querySelector('[data-testid=\"set-model-fixture-model\"]').getAttribute('aria-checked')==='true' && document.querySelector('[data-testid=\"set-account-ours\"]').getAttribute('aria-checked')==='true' && window.__nextRequests.length === 1"));
  await session.eval(`window.__nextReply={ok:true,type:'spawn.ok',state:'queued',streamId:'local:not-listed',stream_id:'local:not-listed',request_id:'fixture',spawn_request_id:'fixture',queue_handle:'fixture'}; true`);
  await session.click('[data-testid="start-chat"]'); await session.waitFor("document.querySelector('[data-testid=\"start-chat\"]')?.textContent === 'Starting'", { timeoutMs: ctx.timeoutMs });
  await session.eval("document.querySelector('[data-testid=\"start-chat\"]').click(),true");
  report.ok('queued admission holds the disabled form and records its stream', await session.eval("document.querySelector('[data-testid=\"start-chat\"]').disabled && window.__pentacleNextAdmitted.includes('local:not-listed') && window.__nextRequests.length === 2"));
  await session.click('.card [aria-label="Close"]'); await session.click('.chats-button'); await session.click('[aria-label="New chat"]');
  await session.waitFor("!!document.querySelector('[data-testid=\"set-provider-claude\"]')", { timeoutMs: ctx.timeoutMs });
  report.ok('leaving and reopening shows a fresh form', await session.eval("!!document.querySelector('[data-testid=\"set-provider-claude\"]') && !document.querySelector('.start-error')"));
  await session.screenshot(path.join(report.dir, 'next-new-chat.png'));
  await session.click('[data-testid="set-provider-claude"]'); await session.click('[data-testid="set-model-fixture-model"]'); await session.click('[data-testid="set-account-ours"]');
  await session.eval(`window.__nextReply={ok:true,type:'spawn.ok',state:'queued',streamId:'local:web-gate-1',stream_id:'local:web-gate-1',request_id:'fixture2',spawn_request_id:'fixture2',queue_handle:'fixture2'}; true`);
  await session.click('[data-testid="start-chat"]');
  await session.waitFor("!document.querySelector('.card').hidden && !!document.querySelector('.card [data-next-composer]')", { timeoutMs: ctx.timeoutMs });
  report.ok('an admitted listed session opens its ordinary chat and is recorded', await session.eval("window.__pentacleNextAdmitted.includes('local:web-gate-1') && !!document.querySelector('.card [aria-label=\"Terminal\"]')"));
  await session.click('.chats-button'); await session.click('[aria-label="New chat"]'); await session.waitFor("!!document.querySelector('[data-testid=\"set-provider-claude\"]')", { timeoutMs: ctx.timeoutMs }); await session.click('[data-testid="set-provider-claude"]'); await session.click('[data-testid="set-model-fixture-model"]'); await session.click('[data-testid="set-account-ours"]');
  await session.eval(`window.__nextReply={ok:true,streamId:'local:web-gate-empty',session:{...window.PentacleChatStore.getState().sessions.find(s=>s.stream_id==='local:web-gate-empty'),state:'ready'}}; true`);
  await session.click('[data-testid="start-chat"]');
  const empty = '.chat-card:not([hidden]):has(.thread-inner[data-stream-id="local:web-gate-empty"])';
  const stillOpen = '.chat-card:not([hidden]):has(.thread-inner[data-stream-id="local:web-gate-1"])';
  await session.waitFor(`window.__pentacleNextAdmitted.includes('local:web-gate-empty') && !!document.querySelector(${JSON.stringify(empty + ' [data-next-composer]')})`, {timeoutMs:ctx.timeoutMs});
  report.ok('an ordinary ready reply opens the empty session and records its ID', await session.eval(`(() => {
    const card = document.querySelector(${JSON.stringify(empty)});
    return !!card?.querySelector('.card-title').textContent.trim() &&
      !card.querySelector('.thread-inner .slot-chat-row') && !card.querySelector('.new-chat-host:not([hidden])') &&
      !!document.querySelector(${JSON.stringify(stillOpen)});
  })()`));
  } finally { require('fs').rmSync(catalogFlag, { force:true }); }
}
module.exports.newChat = isolated(newChat);
async function limitsSettings(ctx) {
  const { session, report } = ctx; const statsFlag = path.join(ctx.runtime.fixtureScratch, 'next-stats-fixture'); require('fs').writeFileSync(statsFlag, 'enabled');
  try { await visit(ctx);
  await session.click('[aria-label="Limits and machines"]');
  report.ok('limits popover opens with usage rows and lights its control', await session.eval("!document.querySelector('.popover').hidden && document.querySelector('.popover').textContent.includes('Claude') && document.querySelector('[aria-label=\"Limits and machines\"]').classList.contains('open')"));
  await session.waitFor("!!document.querySelector('[data-machine-host=\"fixture-machine\"]')", { timeoutMs: ctx.timeoutMs });
  report.ok('machine CPU, RAM and disk figures use separate mock table columns', await session.eval("(() => {const row=document.querySelector('[data-machine-host=\"fixture-machine\"]');const cells=[...row.querySelectorAll('[role=cell]')].map(x=>x.textContent);return JSON.stringify(cells)===JSON.stringify(['24%','25%','40%'])&&!!document.querySelector('.limits-view h3')})()"));
  await session.screenshot(path.join(report.dir,'next-repair-stats-1440.png'));
  await session.send('Emulation.setDeviceMetricsOverride', {width:440,height:956,deviceScaleFactor:1,mobile:false});
  try {
    await session.waitFor(`window.innerWidth === 440
      && document.documentElement.scrollWidth === document.documentElement.clientWidth
      && !!document.querySelector('[data-machine-host="fixture-machine"]')`,
      { timeoutMs: ctx.timeoutMs, label: '440 px stats table without horizontal overflow' });
  } catch (error) {
    if (!error.message.startsWith('waitFor timed out')) throw error;
    const detail = await session.eval(`(() => {
      const { scrollWidth, clientWidth } = document.documentElement;
      const machineRowPresent = !!document.querySelector('[data-machine-host="fixture-machine"]');
      let widestElement = null;
      for (const element of document.body.querySelectorAll('*')) {
        const width = element.getBoundingClientRect().width;
        if (!width || (widestElement && width <= widestElement.width)) continue;
        const selector = element.id ? '#' + CSS.escape(element.id)
          : element.localName + [...element.classList].map(name => '.' + CSS.escape(name)).join('');
        widestElement = { selector, width };
      }
      return { scrollWidth, clientWidth, machineRowPresent, widestElement };
    })()`);
    report.ok('phone stats table does not overflow', false, detail);
  }
  report.ok('phone stats table does not overflow', true);
  await session.screenshot(path.join(report.dir,'next-repair-stats-440.png'));
  await session.send('Emulation.setDeviceMetricsOverride', {width:1440,height:900,deviceScaleFactor:1,mobile:false});
  await session.waitFor("window.innerWidth===1440", {timeoutMs:ctx.timeoutMs});
  await session.send('Input.dispatchKeyEvent', { type:'keyDown', key:'Escape', code:'Escape' });
  report.ok('Escape closes the popover', await session.eval("document.querySelector('.popover').hidden"));
  await session.click('[aria-label="Limits and machines"]'); await session.click('.stage');
  report.ok('clicking away closes the popover', await session.eval("document.querySelector('.popover').hidden"));
  await session.click('[aria-label="Settings"]'); report.ok('settings presents three tiles and the selected theme', await session.eval("document.querySelector('.theme-heading').textContent==='Theme' && document.querySelectorAll('.theme-choice').length===3 && document.querySelector('[aria-label=dark]').getAttribute('aria-pressed')==='true'")); await session.screenshot(path.join(report.dir, 'next-settings.png')); await session.click('[aria-label="light"]');
  const light = await session.eval("getComputedStyle(document.body).backgroundColor");
  await session.click('[aria-label="cosmic"]');
  const cosmic = await session.eval("(() => {const label=document.createElement('span');label.className='status-age';document.body.append(label);const result={bg:getComputedStyle(document.body).backgroundColor,title:getComputedStyle(document.querySelector('.card-title')).fontFamily,body:getComputedStyle(document.body).fontFamily,label:getComputedStyle(label).fontFamily,lit:getComputedStyle(document.querySelector('.rail-button.on')).color,idle:getComputedStyle(document.querySelector('.chats-button')).color};label.remove();return result})()");
  report.ok('light and cosmic themes change the page and cosmic type', light !== cosmic.bg && cosmic.bg !== 'rgba(0, 0, 0, 0)' && cosmic.title.includes('Rajdhani') && !cosmic.body.includes('Rajdhani') && cosmic.label.includes('JetBrains Mono') && cosmic.lit !== cosmic.bg && cosmic.idle !== cosmic.bg, { light, cosmic });
  await session.send('Page.reload'); await session.waitFor("document.documentElement.dataset.theme === 'cosmic'", { timeoutMs: ctx.timeoutMs });
  report.ok('theme persists across reload', true);
  } finally { require('fs').rmSync(statsFlag, { force:true }); }
}
module.exports.limitsSettings = isolated(limitsSettings);
async function composer(ctx) {
  const { session, report, cdp } = ctx; await visit(ctx);
  await session.click('.chats-button'); await session.click('.popover [data-stream-id="local:web-gate-1"]');
  await session.waitFor("!!document.querySelector('.card [data-next-message]')", { timeoutMs: ctx.timeoutMs });
  await session.eval(`(() => { window.__nextSends=[]; window.PentacleChatStore.setSendBridge(async args => { window.__nextSends.push(args); if(window.__nextSends.length===1) await new Promise(resolve=>window.__releaseNextSend=resolve); return {ok:true}; }); const e=document.querySelector('.card [data-next-message]'); e.value='draft to keep'; e.dispatchEvent(new Event('input',{bubbles:true})); return true; })()`);
  report.ok('typing replaces the microphone with send', await session.eval("document.querySelector('.card [data-next-mic]').hidden && !document.querySelector('.card [data-next-send]').hidden"));
  await session.click('[aria-label="Close"]'); await session.click('.chats-button'); await session.click('.popover [data-stream-id="local:web-gate-1"]');
  report.ok('the draft survives closing and reopening', await session.eval("document.querySelector('.card [data-next-message]').value === 'draft to keep'"));
  const shiftPass = await session.eval(`(() => { const e=document.querySelector('.card [data-next-message]');const k=new KeyboardEvent('keydown',{key:'Enter',shiftKey:true,bubbles:true,cancelable:true}); return e.dispatchEvent(k); })()`);
  report.ok('Shift+Enter remains a line break', shiftPass);
  await session.eval(`(() => { const e=document.querySelector('.card [data-next-message]');e.value='x'.repeat(10000);e.dispatchEvent(new Event('input',{bubbles:true}));return true;})()`);
  report.ok('long text stays within the 160 px cap', await session.eval("document.querySelector('.card [data-next-message]').getBoundingClientRect().height <= 160"));
  await session.eval(`(() => { const e=document.querySelector('.card [data-next-message]'); e.value='a\\nb'; e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true})); return true; })()`);
  await session.waitFor("window.__nextSends.length === 1", { timeoutMs: ctx.timeoutMs });
  report.ok('Enter sends through the shared store', await session.eval("window.__nextSends[0].streamId === 'local:web-gate-1' && window.__nextSends[0].text === 'a\\nb'"));
  report.ok('the pending send is visible in the transcript', await session.eval("!!document.querySelector('.card .slot-chat-send-status.is-sending') || !!document.querySelector('.card .slot-chat-send-status.is-queued')"));
  await session.eval('window.__releaseNextSend(),true');
  await session.eval(`(() => {const data=Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a1sAAAAASUVORK5CYII='),c=>c.charCodeAt(0));const dt=new DataTransfer();dt.items.add(new File([data],'fixture.png',{type:'image/png'}));const input=document.querySelector('.card [data-next-file-input]');input.files=dt.files;input.dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
  await session.waitFor("!!document.querySelector('.card .next-composer-attachment')",{timeoutMs:ctx.timeoutMs});
  await session.click('.card [data-next-send]'); await session.waitFor("window.__nextSends.length===2",{timeoutMs:ctx.timeoutMs});
  report.ok('attachment uploads and is included in the outbound turn',await session.eval("window.__nextSends[1].attachments.length === 1 && !!window.__nextSends[1].attachments[0].key"));
  await cdp.sleep(100);

  const png = await session.eval(`(() => {
    const canvas = document.createElement('canvas');
    canvas.width = 12;
    canvas.height = 8;
    const drawing = canvas.getContext('2d');
    drawing.fillStyle = '#37658b';
    drawing.fillRect(0, 0, canvas.width, canvas.height);
    const encoded = canvas.toDataURL('image/png').split(',')[1];
    const bytes = Uint8Array.from(atob(encoded), character => character.charCodeAt(0));
    const clipboardData = new DataTransfer();
    clipboardData.items.add(new File([bytes], 'synthetic-clipboard.png', { type: 'image/png' }));
    const input = document.querySelector('.card [data-next-message]');
    input.focus();
    input.dispatchEvent(new ClipboardEvent('paste', { clipboardData, bubbles: true, cancelable: true }));
    return encoded;
  })()`);
  await session.waitFor("!!document.querySelector('.card .next-composer-attachment-preview[alt=\"synthetic-clipboard.png\"]')",
    { timeoutMs: ctx.timeoutMs, label: 'clipboard image enters the Next composer tray' });
  await session.click('.card [data-next-send]');
  await session.waitFor('window.__nextSends.length === 3', { timeoutMs: ctx.timeoutMs });
  const bytes = Buffer.from(png, 'base64');
  const key = require('node:crypto').createHash('sha256').update(bytes).digest('hex');
  const sent = await session.eval('window.__nextSends[2]');
  report.ok('pasted PNG uploads through the fixture daemon and enters the ordinary outbound turn',
    sent.streamId === 'local:web-gate-1' && sent.text === '' && sent.attachments.length === 1
      && sent.attachments[0].key === key && sent.attachments[0].mime === 'image/png'
      && sent.attachments[0].bytes === bytes.length);
  const displayed = JSON.stringify(`.card .slot-chat-media-button[data-attachment-key="${key}"]`);
  await session.waitFor(`(() => {
    const button = document.querySelector(${displayed});
    const image = button?.querySelector('img');
    return image?.naturalWidth === 12 && image.naturalHeight === 8
      && !button.querySelector('.slot-chat-media-loading') && !document.querySelector('.card .next-composer-attachment');
  })()`, { timeoutMs: ctx.timeoutMs, label: 'sent clipboard image decodes in the transcript and clears the draft' });
  report.ok('sent clipboard PNG is displayed and the draft attachment is cleared', true);
}
module.exports.composer = isolated(composer);
async function terminalStatus(ctx) {
  const { session, report, tmux, cdp } = ctx; await visit(ctx);
  const name = `ptest-web-next-${process.pid}-${Date.now().toString(36)}`;
  tmux(['new-session','-d','-s',name,'sh','-c','stty raw -echo; exec cat']);
  let ptyOutput = '';
  const observeFrame = raw => {
    try {
      const event = JSON.parse(raw);
      if (event.method !== 'Network.webSocketFrameReceived') return;
      const message = JSON.parse(event.params.response.payloadData);
      if (message.event === 'pty:data' && message.args?.[0] === 0) ptyOutput += message.args[1];
    } catch (_) { /* Ignore non-JSON and unrelated WebSocket frames. */ }
  };
  try {
    await session.send('Network.enable'); session.ws.on('message', observeFrame);
    await session.eval(`(() => {const original=window.cc.createPty;window.__nextPtyReady=false;window.cc.createPty=async(...args)=>{const pane=await original(...args);window.__nextPtyReady=!!pane;return pane};return true})()`);
    await session.click('.chats-button'); await session.click('.popover [data-stream-id="local:web-gate-survivor"]');
    report.ok('a session without a status card has no status button', await session.eval("document.querySelector('.card [aria-label=\"Status\"]').hidden"));
    await session.click('.card [aria-label="Close"]');
    await session.eval(`(() => { const store=window.PentacleChatStore; const rows=store.getState().sessions.map(s=>s.stream_id==='local:web-gate-1'?{...s,session_name:${JSON.stringify(name)},status_card:{goal:'Fixture goal',plan:[{text:'First',status:'done'},{text:'Second',status:'active'}],update:'Fixture update',updated_at:new Date().toISOString()}}:s);store.applyFrame({type:'session.inventory',sessions:rows});return true;})()`);
    await session.click('.chats-button'); await session.click('.popover [data-stream-id="local:web-gate-1"]');
    await session.click('.card [aria-label="Terminal"]');
    await session.waitFor("!!document.querySelector('.terminal .xterm-screen')", { timeoutMs:ctx.timeoutMs });
    await session.waitFor("window.__nextPtyReady === true", { timeoutMs:ctx.timeoutMs });
    if (process.env.PENTACLE_NEXT_REQUIRE_WEBGL === '1') {
      report.ok('terminal uses an active WebGL renderer', await session.eval("[...document.querySelectorAll('.terminal canvas')].some(canvas => !!canvas.getContext('webgl2'))"));
    }
    const waitForOutput = async (predicate, label) => {
      const deadline = Date.now() + ctx.timeoutMs;
      while (!predicate() && Date.now() < deadline) await cdp.sleep(50);
      if (!predicate()) throw new Error(`PTY ${label} timed out`);
    };
    await waitForOutput(() => ptyOutput.length > 0, 'attach output');
    await session.eval("window.cc.writePty(0, 'NEXT_TERMINAL_PROOF\\r'), true");
    await waitForOutput(() => ptyOutput.includes('NEXT_TERMINAL_PROOF'), 'sentinel output');
    report.ok('the new card displays a real tmux terminal', true);
    // The frames prove delivery to the page, and the canvas renderer leaves no text to read.
    // Reopen the terminal on the DOM renderer and require the output on screen.
    await session.eval(`(() => { const original = HTMLCanvasElement.prototype.getContext; window.__nextRestoreContext = () => { HTMLCanvasElement.prototype.getContext = original; }; HTMLCanvasElement.prototype.getContext = function (type, ...rest) { return type === 'webgl2' ? null : original.call(this, type, ...rest); }; window.__nextPtyReady = false; return true; })()`);
    await session.click('.card [aria-label="Terminal"]'); await session.click('.card [aria-label="Terminal"]');
    await session.waitFor("window.__nextPtyReady === true && !!document.querySelector('.terminal .xterm-rows')", { timeoutMs:ctx.timeoutMs });
    await session.eval("window.cc.writePty(0, 'NEXT_TERMINAL_SHOWN\\r'), true");
    await session.waitFor("document.querySelector('.terminal .xterm-rows').textContent.includes('NEXT_TERMINAL_SHOWN')", { timeoutMs:ctx.timeoutMs, label:'the first card terminal shows its session output' });
    report.ok('the terminal shows what its session printed', true);
    await session.eval("window.__nextRestoreContext(), true");
    report.ok('terminal is inset and the composer is hidden', await session.eval("document.querySelector('.card .composer-host').hidden && getComputedStyle(document.querySelector('.terminal')).borderRadius === '14px' && parseFloat(getComputedStyle(document.querySelector('.terminal')).marginLeft) === 12"));
    await session.screenshot(path.join(report.dir, 'next-terminal.png'));
    await session.click('.card [aria-label="Status"]');
    report.ok('status replaces terminal and retains composer', await session.eval("!document.querySelector('.status').hidden && document.querySelector('.terminal').hidden && document.querySelector('.status').textContent.includes('Fixture goal') && document.querySelector('.status').textContent.includes('Fixture update') && document.querySelector('.status').textContent.includes('Step 2 of 2') && document.querySelector('.status').textContent.includes('updated') && document.querySelectorAll('.status li').length===2 && !!document.querySelector('.card [data-next-composer]')"));
    await session.screenshot(path.join(report.dir, 'next-status.png'));
    await session.send('Emulation.setDeviceMetricsOverride', { width:440, height:956, deviceScaleFactor:1, mobile:false }); await session.screenshot(path.join(report.dir, 'next-phone-status.png'));
    await session.click('.card [aria-label="Close"]');
  } finally { session.ws.off('message', observeFrame); try { tmux(['kill-session','-t',`=${name}`]); } catch (_) {} }
}
module.exports.terminalStatus = isolated(terminalStatus);
async function voice(ctx) {
  const {session, report, cdp} = ctx; await visit(ctx);
  await session.click('.chats-button'); await session.click('.popover [data-stream-id="local:web-gate-1"]');
  await session.eval(`(() => { window.__nextVoice={media:[],sends:[]}; const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices); const transcribe=window.cc.chatTranscribeBlob; window.__nextVoice.restore=async()=>{window.__nextVoice.release?.();navigator.mediaDevices.getUserMedia=original;window.cc.chatTranscribeBlob=transcribe;for(const m of window.__nextVoice.media){m.osc.stop();m.stream.getTracks().forEach(t=>t.stop());await m.context.close()}}; navigator.mediaDevices.getUserMedia=async()=>{const context=new AudioContext();await context.resume();const osc=context.createOscillator(),gain=context.createGain(),dest=context.createMediaStreamDestination();gain.gain.value=.05;osc.connect(gain).connect(dest);osc.start();setTimeout(()=>gain.gain.value=.9,500);window.__nextVoice.media.push({context,osc,stream:dest.stream});return dest.stream};window.cc.chatTranscribeBlob=async payload=>{await new Promise(resolve=>window.__nextVoice.release=resolve);return transcribe(payload)};window.cc.chatOutboundCommit=async()=>({ok:false,error_code:'unsupported'});window.PentacleChatStore.setSendBridge(async args=>{window.__nextVoice.sends.push(args);return {ok:true}});return true})()`);
  try {
    await session.send('Runtime.evaluate',{expression:"document.querySelector('.card [data-next-mic]').click()",userGesture:true});
    await session.waitFor("!!document.querySelector('.card [data-next-voice-bars]')",{timeoutMs:ctx.timeoutMs});
    await cdp.sleep(1350);
    const sample=await session.eval("({bars:[...document.querySelectorAll('.card [data-next-voice-bars] .next-voice-bar')].map(x=>x.style.height),time:document.querySelector('.card [data-next-voice-time]').textContent})");
    report.ok('live waveform varies and timer advances',sample.bars.length>1&&sample.bars.length<=110&&new Set(sample.bars).size>1&&sample.time!=='0:00',sample);
    await cdp.sleep(7000); report.ok('live waveform retains at most 110 bars', await session.eval("document.querySelectorAll('.card [data-next-voice-bars] .next-voice-bar').length===110"));
    await session.click('.card [data-next-voice-cancel]'); report.ok('cancel discards the take',await session.eval("!document.querySelector('.card [data-next-voice-pending]') && window.__nextVoice.sends.length===0"));
    await session.send('Runtime.evaluate',{expression:"document.querySelector('.card [data-next-mic]').click()",userGesture:true}); await session.waitFor("!!document.querySelector('.card [data-next-voice-bars]')",{timeoutMs:ctx.timeoutMs}); await cdp.sleep(1100);
    await session.click('.card [data-next-voice-send]'); await session.waitFor("!!document.querySelector('[data-next-voice-pending]') && !!window.__nextVoice.release",{timeoutMs:ctx.timeoutMs});
    report.ok('sending take shows 22 bars, duration and Sending',await session.eval("document.querySelector('[data-next-voice-pending]').textContent.includes('Sending') && document.querySelectorAll('[data-next-voice-pending] [data-next-voice-bar]').length === 22 && document.querySelector('[data-next-voice-duration]').textContent !== '0:00'"));
    require('fs').writeFileSync(path.join(ctx.runtime.fixtureScratch, 'asr-release'), 'release');
    await session.eval('window.__nextVoice.release(),true'); await session.waitFor("window.__nextVoice.sends.length===1",{timeoutMs:ctx.timeoutMs});
    report.ok('transcribed voice reaches the shared send bridge',await session.eval("window.__nextVoice.sends[0].text.includes('Hermetic server transcript')"));
    await session.send('Runtime.evaluate',{expression:"document.querySelector('.card [data-next-mic]').click()",userGesture:true}); await session.waitFor("!!document.querySelector('.card [data-next-voice-bars]')",{timeoutMs:ctx.timeoutMs});
    const takeCard = '.chat-card:not([hidden]):has(.thread-inner[data-stream-id="local:web-gate-1"])';
    const otherCard = '.chat-card:not([hidden]):has(.thread-inner[data-stream-id="local:web-gate-survivor"])';
    await useFluid(session, takeCard + ' .card-title');
    await chooseChat(session, 'local:web-gate-survivor');
    await session.waitFor(`!!document.querySelector(${JSON.stringify(otherCard)})`, {timeoutMs:ctx.timeoutMs});
    report.ok('adding a second card keeps the first take recording', await session.eval(
      `!!document.querySelector(${JSON.stringify(takeCard + ' [data-next-voice-bars]')}) &&
        window.__nextVoice.media.at(-1).stream.getTracks().every(t=>t.readyState==='live') &&
        window.__nextVoice.sends.length===1`));
    await session.click(otherCard + ' [aria-label="Close"]');
    await useFluid(session, '[data-pane=assistant] .thread');
    await chooseChat(session, 'local:web-gate-survivor');
    await session.waitFor(`!document.querySelector(${JSON.stringify(takeCard)}) &&
      !!document.querySelector(${JSON.stringify(otherCard)}) &&
      window.__nextVoice.media.at(-1).stream.getTracks().every(t=>t.readyState==='ended')`,{timeoutMs:ctx.timeoutMs});
    report.ok('changing chats stops and discards an in-progress take', await session.eval("window.__nextVoice.sends.length===1 && !document.querySelector('[data-next-voice-pending]')"));
  } finally { await session.eval('window.__nextVoice.restore()'); require('fs').rmSync(path.join(ctx.runtime.fixtureScratch, 'asr-release'), { force: true }); }
}
module.exports.voice=isolated(voice);
async function narrowPhone(ctx) {
  const {session,report}=ctx;
  for (const width of [1010,702,440]) {
    await visit(ctx,width); await session.click('.chats-button'); await session.click('.popover [data-stream-id="local:web-gate-1"]');
    const result=await session.eval("({overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth,assistant:!document.querySelector('.pane').hidden,full:getComputedStyle(document.querySelector('.fullscreen')).display})");
    report.ok(`chat layout fits at ${width}`,!result.overflow && (width>599 || (!result.assistant&&result.full==='none')),result);
    if(width===440){await session.screenshot(path.join(report.dir, 'next-phone-chat.png'));await session.click('.chats-button');await session.screenshot(path.join(report.dir, 'next-phone-chats.png'));await session.click('.popover [data-stream-id="local:web-gate-survivor"]');await session.click('.card [aria-label="Close"]');report.ok('phone closes newest card to Assistant',await session.eval("!document.querySelector('.pane').hidden && document.querySelector('.card').hidden"));await session.click('.chats-button');await session.click('.popover [data-stream-id="local:web-gate-1"]');await session.click('.rail .brand');report.ok('phone logo returns to the assistant',await session.eval("!document.querySelector('.pane').hidden && document.querySelector('.card').hidden"));}
  }
  await session.send('Emulation.clearDeviceMetricsOverride');
}
module.exports.narrowPhone=isolated(narrowPhone);
async function lanesQuestions(ctx) {
  const { session, report } = ctx;
  const fixture = require('../../../pentacle-chat-core/tests/fixtures/work-lanes-inventory.json').goals_v1;
  const lane = fixture.inventory_frame.lanes[0];
  const member = { ...fixture.show_projection.members[0], title:'Fixture release spec' };
  lane.members[0].title = member.title;
  const notice = { notification_id:'next-fixture-notice', producer:'agent_question.v1', state:'open', title:'Fixture decision', body:'Choose an answer', actions:[{kind:'yes_no'}], question:{question_id:'next-fixture-question',state:'open',producer_stream_id:'local:web-gate-1',spec_id:member.spec_id,response_mode:'single_choice',options:[{label:'Keep',value:'keep'},{label:'Change',value:'change'}]} };
  const capture = await session.send('Page.addScriptToEvaluateOnNewDocument', { source:`(() => { const Native=window.WebSocket; window.WebSocket=class extends Native { constructor(...args) { super(...args); window.__fixtureSocket=this; } }; })()` });
  try {
    await visit(ctx);
    await session.eval(`(() => {
      const lane=${JSON.stringify(lane)}, projection=${JSON.stringify(fixture.show_projection)}, member=${JSON.stringify(member)};
      window.cc.workLanesShow=async (_lane, options={}) => ({type:'work_lanes.show.ok',lane:{lane_id:lane.lane_id,members:[member.spec_id]},projection:{...projection,lane_id:lane.lane_id,members:[member],members_total:1,members_next:null,...(options.goalId?{selected_goal:lane.goals.find(g=>g.goal_id===options.goalId)}:{})},members:[member],members_total:1,members_next:null,detail_revision:lane.detail_revision});
      window.__fixtureAnswers=[];
      window.cc.notificationResolve=async (...args)=>{window.__fixtureAnswers.push(args);return {ok:true};};
      window.cc.promptList=async()=>({ok:true,questions:window.__fixtureAnswers.length?[{question_id:'next-fixture-question',state:'answered',answered_at:'2026-11-21T00:00:00Z',answer:{selections:['keep']},envelope:{spec_id:member.spec_id,options:[{label:'Keep',value:'keep'}]}}]:[]});
      const emit=frame=>window.__fixtureSocket.dispatchEvent(new MessageEvent('message',{data:JSON.stringify({event:'chat-stream:frame',args:[frame]})}));
      emit(${JSON.stringify(fixture.inventory_frame)}); emit({type:'notification',notification:${JSON.stringify(notice)}}); return true;
    })()`);
    await session.waitFor("document.querySelector('.next-lanes-row-title')?.textContent==='Mobile release' && document.querySelector('[data-count=\"1\"]')", { timeoutMs:ctx.timeoutMs });
    report.ok('an unversioned lane push reaches the card and one question reaches the rail', await session.eval("document.querySelector('.next-lanes-row-title').textContent==='Mobile release' && document.querySelector('[data-count=\"1\"]')!==null"));
    report.ok('desktop lanes card keeps the mock width', await session.eval("Math.abs(document.querySelector('.lanes-card').getBoundingClientRect().width-344)<2"));
    await session.screenshot(path.join(report.dir,'next-lanes-fixture.png'));
    await session.click('.next-lanes-row-open'); await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level==='lane' && !!document.querySelector('.next-lanes-stop-open')", {timeoutMs:ctx.timeoutMs});
    await session.screenshot(path.join(report.dir,'next-lane-fixture.png'));
    await session.click('.next-lanes-stop-open'); await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level==='goal'", {timeoutMs:ctx.timeoutMs});
    await session.screenshot(path.join(report.dir,'next-goal-fixture.png'));
    await session.click('.next-lanes-back'); await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level==='lane'", {timeoutMs:ctx.timeoutMs});
    await session.click('.next-lanes-back'); await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level==='all'", {timeoutMs:ctx.timeoutMs});
    report.ok('back navigation returns from goal to lane and all lanes', true);
    await session.click('.next-lanes-close');
    await session.click('[aria-label="Questions (1)"]');
    report.ok('question list identifies its lane and goal', await session.eval("document.querySelector('.next-question-heading-link')?.textContent.includes('Mobile release') && document.querySelector('.next-question-heading-link')?.textContent.includes('Finish fixture 0')"));
    await session.screenshot(path.join(report.dir,'next-questions-fixture.png'));
    await session.click('.next-question-heading-link');
    await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level==='spec'", { timeoutMs:ctx.timeoutMs });
    report.ok('question heading reopens lanes at its spec and question card', await session.eval("!document.querySelector('.lanes-card').hidden && !!document.querySelector('.next-question-card')"));
    report.ok('spec back control names its goal', await session.eval("document.querySelector('.next-lanes-back')?.textContent.includes('Finish fixture 0')"));
    await session.screenshot(path.join(report.dir,'next-spec-fixture.png'));
    await session.send('Emulation.setDeviceMetricsOverride', {width:440,height:956,deviceScaleFactor:1,mobile:false});
    await session.waitFor("window.innerWidth===440 && document.querySelector('.pane').hidden", {timeoutMs:ctx.timeoutMs});
    report.ok('phone spec occupies the stage without horizontal overflow', await session.eval("!document.querySelector('.lanes-card').hidden && document.querySelector('.pane').hidden && document.documentElement.scrollWidth===document.documentElement.clientWidth"));
    await session.screenshot(path.join(report.dir,'next-phone-spec-fixture.png'));
    await session.click('.next-question-card .next-question-option');
    await session.click('.next-question-card .next-question-send');
    await session.waitFor("document.querySelector('.next-question-card')?.textContent.includes('Answer sent')", {timeoutMs:ctx.timeoutMs});
    report.ok('one answer is sent and the receipt lowers the rail count', await session.eval("window.__fixtureAnswers.length===1 && window.__fixtureAnswers[0][2].selections[0]==='keep' && document.querySelector('[aria-label=Questions]')?.dataset.count==='0'"));
    await session.screenshot(path.join(report.dir,'next-answered-fixture.png'));
    await session.waitFor("document.querySelector('.next-lanes-scroll')?.textContent.includes('You said') && document.querySelector('.next-lanes-scroll')?.textContent.includes('Keep')", {timeoutMs:ctx.timeoutMs});
    report.ok('spec reads the stored answer back', await session.eval("document.querySelector('.next-lanes-scroll').textContent.includes('You said')"));
    await session.click('.next-lanes-back'); await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level==='goal'", {timeoutMs:ctx.timeoutMs});
    await session.click('.next-lanes-member-title'); await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level==='spec' && document.querySelector('.next-lanes-scroll').textContent.includes('You said')", {timeoutMs:ctx.timeoutMs});
    report.ok('returning to the spec retains its stored answer', await session.eval("document.querySelector('.next-lanes-scroll').textContent.includes('Keep') && window.__fixtureAnswers.length===1"));
  } finally { await session.send('Page.removeScriptToEvaluateOnNewDocument', { identifier:capture.identifier }); }
}
module.exports.lanesQuestions=isolated(lanesQuestions);
async function scrollAndFocus(ctx) {
  const {session,report}=ctx;
  for (const width of [1440,440]) {
    await visit(ctx,width);
    const baseline=await session.eval(`(() => {const input=document.querySelector('.pane .next-composer-input'), outer=input.closest('.next-composer'), scroller=document.querySelector('.pane .thread'); const filler=document.createElement('div');filler.style.height='2000px';filler.style.flex='none';filler.textContent='Scroll fixture';scroller.append(filler);scroller.scrollTop=500;const before=getComputedStyle(outer).boxShadow;input.focus();return {outline:getComputedStyle(input).outlineStyle,before,after:getComputedStyle(outer).boxShadow,scrollbar:getComputedStyle(scroller).scrollbarWidth,width:getComputedStyle(scroller,'::-webkit-scrollbar').width,overflow:scroller.scrollHeight>scroller.clientHeight};})()`);
    report.ok(`composer focus uses the outer cue at ${width}`,baseline.outline==='none' && baseline.before!==baseline.after,baseline);
    report.ok(`scrollbar is native auto with a 14 px styled box at ${width}`,baseline.overflow && baseline.scrollbar==='auto' && baseline.width==='14px',baseline);
    await session.screenshot(path.join(report.dir,`next-repair-scroll-focus-${width}.png`));
  }
}
module.exports.scrollAndFocus=isolated(scrollAndFocus);
async function newChatParity(ctx) {
  const {session,report}=ctx;
  const catalog={catalog_version:'parity-fixture',profiles:{desktop_manual:{claude:['allowed-claude','low'],codex:['allowed-codex','low'],cursor:['auto','default'],gemini:['hidden-gemini','default']}},
    models:{claude:{'allowed-claude':{efforts:['low','high']},'hidden-claude':{efforts:['low']}},codex:{'allowed-codex':{efforts:['low','medium']},'hidden-codex':{efforts:['low']}},cursor:{auto:{efforts:['default']}},gemini:{'hidden-gemini':{efforts:['default']}}},
    available_models:{claude:{'allowed-claude':{efforts:['low','high']}},codex:{'allowed-codex':{efforts:['low','medium']}},cursor:{auto:{efforts:['default']}}},
    account_status:{accounts:[{host:'local',provider:'claude',alias:'fixture-account',active:true,enabled:true,health:'healthy'}],defaults:[{host:'local',provider:'claude',alias:'fixture-account'}]}};
  const install=`window.cc.chatSpawnCatalog=async()=>({ok:true,catalog:${JSON.stringify(catalog)}});window.cc.chatSpawnV2=async()=>{throw Error('unexpected spawn')};true`;
  for(const width of [1440,440]) {
    await session.send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
    await session.send('Page.navigate',{url:ctx.url});
    await session.waitFor("!!window.cc && !!document.querySelector('#btn-new')",{timeoutMs:ctx.timeoutMs});
    await session.eval("new Promise(resolve=>setTimeout(resolve,500))");
    await session.eval(install);
    if(width<600) await session.click('#sidebar-toggle');
    await session.click('#btn-new');
    const machines=await session.eval("[...document.querySelectorAll('#new-session-location [data-loc]')].map(x=>({id:x.dataset.loc,label:x.querySelector('.new-session-option-label').textContent.trim()}))");
    await session.click('#new-session-location [data-loc=local]');
    await session.waitFor("document.querySelectorAll('#spawn-provider option').length===3 && /^(allowed-|auto)/.test(document.querySelector('#spawn-model option')?.value||'')",{timeoutMs:ctx.timeoutMs});
    const expected=await session.eval("[...document.querySelectorAll('#spawn-provider option')].map(x=>x.value)");
    const oldChoices={};
    for(const provider of expected){
      await session.eval(`(()=>{const e=document.querySelector('#spawn-provider');e.value=${JSON.stringify(provider)};e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
      oldChoices[provider]=await session.eval("({models:[...document.querySelectorAll('#spawn-model option')].map(x=>x.value),efforts:[...document.querySelectorAll('#spawn-effort option')].map(x=>x.value),accounts:[...document.querySelectorAll('#spawn-account option')].map(x=>x.value)})");
    }
    await session.screenshot(path.join(report.dir,`current-repair-choices-${width}.png`));
    await visit(ctx,width); await session.eval(install);
    await session.click('.chats-button'); await session.click('[aria-label="New chat"]');
    await session.waitFor("!!document.querySelector('[data-set=provider]')",{timeoutMs:ctx.timeoutMs});
    const actual=await session.eval("[...document.querySelectorAll('[data-set=provider]')].map(x=>x.dataset.val)");
    report.ok(`new chat providers match the current web at ${width}`,JSON.stringify(actual)===JSON.stringify(expected),{actual,expected});
    for(const provider of expected){
      await session.click(`[data-testid="set-provider-${provider}"]`);
      const models=await session.eval("[...document.querySelectorAll('[data-set=model]')].map(x=>x.dataset.val)");
      await session.click(`[data-testid="set-model-${oldChoices[provider].models[0]}"]`);
      const next=await session.eval("({efforts:[...document.querySelectorAll('[data-set=effort]')].map(x=>x.dataset.val),accounts:[...document.querySelectorAll('[data-set=account]')].map(x=>x.dataset.val),machines:[...document.querySelectorAll('[data-set=host]')].map(x=>({id:x.dataset.val,label:x.textContent.trim()}))})");
      report.ok(`new chat ${provider} choices match the current web at ${width}`,JSON.stringify(models)===JSON.stringify(oldChoices[provider].models)&&JSON.stringify(next.efforts)===JSON.stringify(oldChoices[provider].efforts)&&JSON.stringify(next.accounts)===JSON.stringify(oldChoices[provider].accounts)&&JSON.stringify(next.machines)===JSON.stringify(machines),{models,next,expected:oldChoices[provider],machines});
    }
    await session.screenshot(path.join(report.dir,`next-repair-choices-${width}.png`));
  }
}
module.exports.newChatParity=isolated(newChatParity);

module.exports.melody = isolated(async ctx => {
  const { session, report } = ctx;
  await visit(ctx);
  await session.click('[aria-label="Dashboards"]');
  await session.click('.popover [aria-label="Melody"]');
  const key = '.melody-card [data-midi="60"]';
  await session.waitFor(`!!document.querySelector('${key}')`, { timeoutMs: ctx.timeoutMs });
  const point = await session.eval(`(() => {
    const r = document.querySelector('${key}').getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.bottom - 8 };
  })()`);
  await session.eval(`document.querySelector('${key}').focus()`);
  await session.send('Input.dispatchKeyEvent', { type: 'keyDown', key: ' ', code: 'Space' });
  await session.send('Input.dispatchMouseEvent', { type: 'mousePressed', ...point, button: 'left', clickCount: 1 });
  await session.send('Input.dispatchKeyEvent', { type: 'keyUp', key: ' ', code: 'Space' });
  await session.send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...point, button: 'left', clickCount: 1 });
  await session.waitFor("document.querySelectorAll('.melody-history-note:not(:disabled)').length === 2", { timeoutMs: ctx.timeoutMs });
  report.ok('A second attack during a held note leaves an explicit zero gap',
    await session.eval("document.querySelector('.melody-history-gap').textContent === '0'"));
  await session.click('.melody-history-note');
  await session.click('.melody-history-note');
  report.ok('Played notes append with the displayed default gap',
    await session.eval("[...document.querySelectorAll('.melody-note')].map(n => n.textContent).join(',') === 'C4,C4' && document.querySelector('.melody-gap-caption').textContent === '250'"));
  await session.click('.melody-gap-caption');
  await session.type('[aria-label="Previous gap in milliseconds"]', '0');
  await session.click('.melody-editor [data-action="raise"]');
  report.ok('The edited pitch and zero-length rest are visible',
    await session.eval("document.querySelector('.melody-note.is-selected').textContent === 'C♯4' && document.querySelector('.melody-gap-caption').textContent === '0'"));
  await session.click('.melody-clear-history');
  report.ok('Clearing history keeps the arrangement',
    await session.eval("!document.querySelector('.melody-history-note') && document.querySelectorAll('.melody-note').length === 2"));
});

const fluidA = 'local:web-gate-1';
const fluidB = 'local:web-gate-survivor';
const pane = key => `.stage>[data-pane="${key}"]:not([hidden])`;
const activeChats = '.stage>.chat-card:not([hidden])';
const bounds = (s, selector) => s.eval(`document.querySelector(${JSON.stringify(selector)})?.getBoundingClientRect().toJSON()`);
const railClick = (s, name) => name === 'Chats' ? s.click('.rail .chats-button') : s.click(`.rail [aria-label="${name}"]`);
const chooseChat = async (s, id) => { await railClick(s, 'Chats'); await s.click(`.popover [data-stream-id="${id}"]`); };
const chooseMelody = async s => { await railClick(s, 'Dashboards'); await s.click('.popover [aria-label=Melody]'); };
const closeFluid = (s, key) => s.click(`${pane(key)} [aria-label=Close]`);
const esc = s => s.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape' });
const mouse = (s, type, x, y, button = 'left') => s.send('Input.dispatchMouseEvent',
  { type, x, y, button, buttons: type === 'mouseReleased' ? 0 : button === 'left' ? 1 : 2, clickCount: type === 'mouseMoved' ? 0 : 1 });
const dragFluid = async (s, a, b) => {
  await mouse(s, 'mousePressed', ...a);
  await mouse(s, 'mouseMoved', ...b);
  await mouse(s, 'mouseReleased', ...b);
};
function fluidCheck(ctx, label, ok, measured) {
  ctx.report.ok(label, ok, measured);
  if (!ok) throw new Error(label);
}
const fluidRead = s => s.eval(`(() => {
  const elements = [...document.querySelectorAll('.stage>[data-pane]:not([hidden])')];
  const order = elements.filter(el => !el.classList.contains('assistant-overlay')).sort((a,b) => a.getBoundingClientRect().left-b.getBoundingClientRect().left);
  return { keys: order.map(el => el.dataset.pane), sizes: Object.fromEntries(order.map(el => [el.dataset.pane,el.getBoundingClientRect().width])),
    overlay: !!document.querySelector('.assistant-overlay:not([hidden])'), pressed: document.querySelector('.rail .brand').getAttribute('aria-pressed'),
    lastFocus: document.activeElement?.closest('.stage>[data-pane]')?.dataset.pane || null,
    streams: Object.fromEntries(order.filter(el => el.classList.contains('chat-card')).map(el => [el.dataset.pane,el.querySelector('.thread-inner').dataset.streamId])),
    zone: document.querySelector('.fluid-zone:not([hidden])')?.dataset.type || null };
})()`);
const useFluid = async (s, selector) => {
  const r = await bounds(s,selector);
  const point = [r.left+r.width/2, r.top+r.height/2];
  await mouse(s,'mousePressed',...point);
  await mouse(s,'mouseReleased',...point);
};
async function fluidVisit(ctx, width = 1440, lanes = false) {
  const { session: s } = ctx;
  await visit(ctx, width);
  await s.waitFor("document.querySelector('.rail .brand').getAttribute('aria-label') === 'Assistant Fixture'", { timeoutMs: ctx.timeoutMs });
  if (!lanes && await s.eval("!document.querySelector('.lanes-card').hidden")) await railClick(s, 'Lanes');
}
const noMotion = (s, reduce = true) => s.send('Emulation.setEmulatedMedia', { features: reduce ? [{ name: 'prefers-reduced-motion', value: 'reduce' }] : [] });

module.exports.fluidRail = isolated(async ctx => {
  const { session: s } = ctx;
  try {
    await fluidVisit(ctx);
    await railClick(s, 'Chats');
    const left = {rail: await bounds(s,'.rail'), popover: await bounds(s,'.popover')};
    fluidCheck(ctx, 'left rail is 60 px, and list is to its right', left.rail.left === 0 && left.rail.width === 60 && left.popover.left >= left.rail.right, left);
    await railClick(s, 'Settings');
    await s.click('.sidebar-choice[aria-label=Top]');
    const top = {rail: await bounds(s,'.rail'), settings: await bounds(s,'.popover')};
    fluidCheck(ctx, 'Top is a 48 px full-width rail with settings below and at right',
      top.rail.top === 0 && top.rail.height === 48 && top.rail.width === 1440 && top.settings.top >= top.rail.bottom && top.settings.right >= 1420, top);
    await railClick(s, 'Chats');
    const list = {anchor: await bounds(s, '.rail .chats-button'), list: await bounds(s,'.popover')};
    fluidCheck(ctx, 'Top list drops under its own button', list.list.top >= list.anchor.bottom && Math.abs(list.anchor.left-list.list.left) <= 2, list);
    await s.send('Page.reload');
    await s.waitFor("document.documentElement.dataset.rail==='top' && !!document.querySelector('.assistant-head')", { timeoutMs:ctx.timeoutMs });
    await railClick(s, 'Settings');
    fluidCheck(ctx, 'Top persisted through reload', await s.eval("localStorage.getItem('pentacle.next.rail')==='top' && document.querySelector('.sidebar-choice[aria-label=Top]').getAttribute('aria-pressed')==='true'"));
  } finally {
    if (await s.eval("document.documentElement.dataset.rail==='top'")) {
      if (await s.eval("!document.querySelector('.sidebar-choice[aria-label=Left]')")) await railClick(s, 'Settings');
      await s.click('.sidebar-choice[aria-label=Left]');
    }
    fluidCheck(ctx, 'Left is restored', (await bounds(s,'.rail')).width === 60);
  }
});

module.exports.fluidBartSwitch = isolated(async ctx => {
  const {session: s} = ctx;
  const mark = () => s.click('.rail .brand');
  const read = () => fluidRead(s);
  await fluidVisit(ctx);
  const name = await s.eval("({head: document.querySelector('.assistant-head').innerText.trim(), label: document.querySelector('.rail .brand').getAttribute('aria-label')})");
  fluidCheck(ctx, 'Assistant head contains only its name', name.head === 'Assistant Fixture' && name.label === name.head, name);
  await mark();
  fluidCheck(ctx, 'mark alone leaves an empty stage', (await read()).keys.length === 0 && (await read()).pressed === 'false');
  await mark();
  await chooseChat(s, fluidA);
  await mark();
  const aside = await read();
  fluidCheck(ctx, 'one chat takes all the room while Assistant is aside', aside.keys.join() === 'chat' && aside.sizes.chat === 1368 && !aside.overlay, aside);
  await mark();
  const resumed = await read();
  fluidCheck(ctx, 'one chat permits return and focuses Assistant', resumed.keys.join() === 'assistant,chat' && resumed.lastFocus === 'assistant' && !resumed.overlay, resumed);
  await useFluid(s,`${pane('chat')} .card-title`);
  await chooseChat(s, fluidB);
  await mark();
  const overlay = await bounds(s,'.assistant-overlay');
  const rail = await bounds(s,'.rail');
  fluidCheck(ctx, 'two chats stay open under a 480 px focusable overlay', (await read()).keys.join() === 'chat,chat2' &&
    (await read()).lastFocus === 'assistant' && (await read()).pressed === 'true' && overlay?.width === 480 && overlay.left === rail.right, overlay);
  await mark(); fluidCheck(ctx,'mark dismisses overlay', !(await read()).overlay);
  await mark(); await esc(s); fluidCheck(ctx,'Escape dismisses overlay', !(await read()).overlay);
  await mark(); await useFluid(s,`${pane('chat2')} .card-title`); fluidCheck(ctx,'outside press dismisses overlay', !(await read()).overlay);
  await mark(); await s.click('[aria-label="Keep Assistant Fixture on screen"]');
  fluidCheck(ctx,'Keep preserves the chat used last', (await read()).keys.join() === 'assistant,chat2' && (await read()).streams.chat2 === fluidB);
  await useFluid(s,`${pane('chat2')} .card-title`); await chooseMelody(s); await mark();
  fluidCheck(ctx,'mark overlays chat in front of dashboard', (await read()).overlay && (await read()).keys.join() === 'chat2,dash');
  await s.click('[aria-label="Keep Assistant Fixture on screen"]');
  fluidCheck(ctx,'Keep restores Assistant without losing the waiting chat', (await read()).keys.join() === 'assistant,dash');
  await s.click('.melody-card [aria-label="Close dashboard"]');
  fluidCheck(ctx,'waiting chat returns beside Assistant', (await read()).keys.join() === 'assistant,chat2');
  await s.click(`${pane('chat2')} [aria-label="Full screen"]`); await mark();
  fluidCheck(ctx,'mark overlays while card remains full screen', (await read()).overlay && (await read()).keys.join() === 'chat2');
  await s.click('[aria-label="Keep Assistant Fixture on screen"]');
  fluidCheck(ctx,'Keep exits full screen without closing the chat', (await read()).keys.join() === 'assistant,chat2');
  await mark(); await closeFluid(s,'chat2');
  fluidCheck(ctx,'closing last seat brings Assistant back', (await read()).keys.join() === 'assistant');
});

async function seedFluidChat(s) {
  await s.eval(`(() => {
    const store = window.PentacleChatStore;
    const rows = store.getState().sessions;
    const original = rows.find(r => r.stream_id === '${fluidA}');
    store.applyFrame({type:'session.inventory', sessions:[...rows,{...original, stream_id:'local:web-gate-fluid-third', session_name:'web-gate-fluid-third', display_name:'Fluid third', title:'Fluid third'}]});
    return true;
  })()`);
}
async function recordFluidTerminals(ctx) {
  const {session:s, tmux} = ctx;
  const names = [0, 1].map(n => `ptest-web-fluid-${process.pid}-${Date.now().toString(36)}-${n}`);
  const running = [];
  const output = ['', ''];
  const started = process.hrtime.bigint();
  function observe(raw) {
    try {
      const e = JSON.parse(raw);
      if (e.method !== 'Network.webSocketFrameReceived') return;
      const msg = JSON.parse(e.params.response.payloadData);
      if (msg.event === 'pty:data' && [0,1].includes(msg.args?.[0])) output[msg.args[0]] += msg.args[1];
    } catch (_) { /* Ignore unrelated WebSocket frames. */ }
  }
  try {
    for (const name of names) {
      tmux(['new-session','-d','-s',name,'sh','-c','stty raw -echo; exec cat']);
      running.push(name);
    }
    await s.send('Network.enable');
    s.ws.on('message',observe);
    await s.eval(`(() => {
      const store = window.PentacleChatStore;
      const ids = ${JSON.stringify([fluidA, fluidB])}, names = ${JSON.stringify(names)};
      store.applyFrame({type:'session.inventory',sessions:store.getState().sessions.map(row => ids.includes(row.stream_id) ? {...row,session_name:names[ids.indexOf(row.stream_id)]} : row)});
      window.__fluidPtyReady = {};
      const create = window.cc.createPty;
      window.cc.createPty = async (...args) => {
        const result = await create(...args);
        window.__fluidPtyReady[args[0]] = { ready: !!result, cols: args[3], rows: args[4] };
        return result;
      };
      return true;
    })()`);
    await s.click(`${pane('chat')} [aria-label=Terminal]`);
    await s.waitFor('window.__fluidPtyReady[0]?.ready', {timeoutMs:ctx.timeoutMs});
    fluidCheck(ctx,'first terminal leaves second transcript intact', await s.eval(`!document.querySelector('${pane('chat')}>.terminal').hidden && !document.querySelector('${pane('chat2')}>.thread').hidden`));
    const beforeSecond = process.hrtime.bigint();
    await s.click(`${pane('chat2')} [aria-label=Terminal]`);
    await s.waitFor('window.__fluidPtyReady[1]?.ready', {timeoutMs:ctx.timeoutMs});
    const sentinel = ['FLUID_PTY_ONLY_A','FLUID_PTY_ONLY_B'];
    for (let n=0; n<2; n++) await s.eval(`window.cc.writePty(${n}, ${JSON.stringify(sentinel[n]+'\r')}),true`);
    const limit = Date.now() + ctx.timeoutMs;
    while (output.some((o,i)=>!o.includes(sentinel[i])) && Date.now() < limit) await ctx.cdp.sleep(50);
    const measured = {
      widths: (await fluidRead(s)).sizes,
      terminals: await s.eval('window.__fluidPtyReady'),
      bothMs: Number(process.hrtime.bigint() - started)/1e6,
      secondAttachAndEchoMs: Number(process.hrtime.bigint() - beforeSecond)/1e6,
    };
    fluidCheck(ctx, 'two real side-by-side terminals fit and route data to distinct slots', output.every((o,i)=>o.includes(sentinel[i])&&!o.includes(sentinel[1-i])) &&
      Object.values(measured.terminals).length === 2 && Object.values(measured.terminals).every(x=>x.cols>0&&x.rows>0), measured);
    await s.click(`${pane('chat2')} [aria-label=Terminal]`);
    await s.click(`${pane('chat')} [aria-label=Terminal]`);
  } finally {
    s.ws.off('message',observe);
    for (const name of running) try { tmux(['kill-session','-t',`=${name}`]); } catch (_) {}
  }
}

module.exports.fluidTwoChats = isolated(async ctx => {
  const { session:s } = ctx;
  const third = 'local:web-gate-fluid-third';
  await fluidVisit(ctx); await seedFluidChat(s);
  await chooseChat(s, fluidA);
  await useFluid(s,'.assistant-head');
  await chooseChat(s, fluidB);
  fluidCheck(ctx, 'Assistant-used admission replaces only the prior chat', (await fluidRead(s)).keys.join() === 'assistant,chat' && (await fluidRead(s)).streams.chat === fluidB);
  await chooseChat(s, fluidA);
  fluidCheck(ctx, 'chat-used admission adds a second card', (await fluidRead(s)).streams.chat2 === fluidA);
  await useFluid(s,`${pane('chat2')} .card-title`); await chooseChat(s, third);
  fluidCheck(ctx, 'third chat takes the unused seat', (await fluidRead(s)).streams.chat === third && (await fluidRead(s)).streams.chat2 === fluidA);
  await s.click(`${pane('chat2')} [aria-label="Full screen"]`);
  await chooseChat(s, fluidA);
  fluidCheck(ctx, 'choosing the already-visible chat leaves its full screen in place', (await fluidRead(s)).keys.join() === 'chat2');
  await chooseChat(s, third);
  fluidCheck(ctx, 'choosing another chat exits full screen and preserves both pane assignments', (await fluidRead(s)).streams.chat2 === fluidA && (await fluidRead(s)).streams.chat === third);
  await useFluid(s,`${pane('chat2')} .card-title`);
  await railClick(s,'Chats'); await s.click('.popover [aria-label="New chat"]');
  await railClick(s,'Chats'); await s.click('.popover [aria-label="New chat"]');
  fluidCheck(ctx,'only one new-chat form is admitted',await s.eval("document.querySelectorAll('.chat-card:not([hidden])>.new-chat-host:not([hidden])').length===1"));
  await fluidVisit(ctx);
  await chooseChat(s, fluidA); await chooseChat(s, fluidB);
  await s.eval(`(() => {
    window.__fluidSends = [];
    window.PentacleChatStore.setSendBridge(async args => { window.__fluidSends.push(args); return {ok:true}; });
    for (const [i,input] of [...document.querySelectorAll('${activeChats} [data-next-message]')].entries()) {
      input.focus(); input.value = 'fluid typed ' + i;
      input.dispatchEvent(new Event('input',{bubbles:true}));
      input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
    }
    return true;
  })()`);
  await s.waitFor("window.__fluidSends.length===2", {timeoutMs:ctx.timeoutMs});
  const sendView = await s.eval(`[...document.querySelectorAll('${activeChats} .thread-inner')].map(n=>({stream:n.dataset.streamId,text:n.textContent}))`);
  fluidCheck(ctx,'both real composers keep their stream-specific optimistic transcripts',
    sendView[0].stream === fluidA && sendView[1].stream === fluidB && sendView[0].text.includes('fluid typed 0') &&
    !sendView[0].text.includes('fluid typed 1') && sendView[1].text.includes('fluid typed 1') && !sendView[1].text.includes('fluid typed 0'), sendView);

  await s.eval(`(() => {
    window.__fluidAudio = [];
    navigator.mediaDevices.getUserMedia = async () => {
      const context = new AudioContext(); await context.resume();
      const osc = context.createOscillator(), out = context.createMediaStreamDestination();
      osc.connect(out); osc.start(); window.__fluidAudio.push({context,osc,stream:out.stream}); return out.stream;
    }; return true;
  })()`);
  try {
    for (const [i,key] of ['chat','chat2','assistant'].entries()) {
      if (key === 'assistant') await s.click('.rail .brand');
      await s.send('Runtime.evaluate', {expression:`document.querySelector('${pane(key)} [data-next-mic]').click()`,userGesture:true});
      await s.waitFor(`window.__fluidAudio.length===${i+1} && !!document.querySelector('${pane(key)} [data-next-voice-bars]')`,{timeoutMs:ctx.timeoutMs});
      fluidCheck(ctx,`voice handoff to ${key} stops previous tracks`,await s.eval("window.__fluidAudio.slice(0,-1).every(a=>a.stream.getTracks().every(t=>t.readyState==='ended'))"));
    }
    await s.click('.assistant-overlay [data-next-voice-cancel]'); await s.click('.rail .brand');
  } finally { await s.eval("Promise.all(window.__fluidAudio.map(a=>{a.osc.stop();a.stream.getTracks().forEach(t=>t.stop());return a.context.close()})).then(()=>true)"); }
  await recordFluidTerminals(ctx);
  await useFluid(s,`${pane('chat2')} .card-title`); await chooseMelody(s);
  fluidCheck(ctx,'dashboard replaces unused chat, keeping last-used second seat', (await fluidRead(s)).keys.join() === 'chat2,dash');
  await chooseChat(s,fluidA);
  fluidCheck(ctx,'while dashboard is last used, a new chat replaces chat', (await fluidRead(s)).streams.chat2 === fluidA && (await fluidRead(s)).keys.join() === 'chat2,dash');
  await useFluid(s,`${pane('chat2')} .card-title`); await chooseChat(s,fluidB);
  fluidCheck(ctx,'while chat is last used, a new chat replaces dashboard', (await fluidRead(s)).keys.join() === 'chat,chat2');
  const secondSlot = (await fluidRead(s)).streams.chat2;
  await s.eval(`(() => {
    const link = document.createElement('button'); link.textContent='Synthetic Design link';
    link.dataset.nextDesignLink='/next/design'; link.className='fluid-test-link';
    document.querySelector('${pane('chat2')} .thread-inner').append(link); return true;
  })()`);
  await s.click('.fluid-test-link');
  fluidCheck(ctx,'link followed from second chat preserves that seat and takes other seat', (await fluidRead(s)).keys.join() === 'chat2,dash' && (await fluidRead(s)).streams.chat2 === secondSlot);
  await s.click('.design-card [aria-label="Close"]');
  await useFluid(s,`${pane('chat2')} .card-title`);
  await chooseChat(s, fluidB);
  if ((await fluidRead(s)).keys.join() === 'chat,chat2') {
    await closeFluid(s,'chat');
    fluidCheck(ctx,'closing one chat leaves the other in its arranged seat', (await fluidRead(s)).keys.join() === 'assistant,chat2' && (await fluidRead(s)).streams.chat2 === secondSlot);
  } else {
    fluidCheck(ctx, 'fixture admits two distinct chats for closing control', false);
  }
  await closeFluid(s,'chat2');
  await chooseMelody(s);
  await chooseChat(s,fluidA);
  fluidCheck(ctx,'dashboard-used admission puts chat beside dashboard', (await fluidRead(s)).keys.join() === 'chat,dash');
  await s.click('.melody-card [aria-label="Close dashboard"]');
  await closeFluid(s,'chat');
  await chooseMelody(s);
  await useFluid(s,'.assistant-head');
  await chooseChat(s,fluidA);
  fluidCheck(ctx,'Assistant-used admission replaces dashboard', (await fluidRead(s)).keys.join() === 'assistant,chat');
});

module.exports.fluidMove = isolated(async ctx => {
  const {session:s} = ctx;
  await fluidVisit(ctx);
  await chooseChat(s, fluidA);
  await noMotion(s);
  try {
    const before = await bounds(s,pane('chat'));
    const h = await bounds(s,`${pane('chat')} .card-title`);
    const p = [h.left+60, h.top+25];
    const q = [p[0]-440, p[1]+115];
    await mouse(s,'mousePressed',...p);
    await mouse(s,'mouseMoved',...q);
    const during = {chat:await bounds(s,pane('chat')),assistant:await bounds(s,pane('assistant')),state:await fluidRead(s)};
    fluidCheck(ctx,'carried card follows both axes while its neighbour slides before release',
      Math.abs(during.chat.left-before.left+440)<2 && Math.abs(during.chat.top-before.top-115)<2 &&
      during.assistant.left > during.chat.left && during.state.zone==='slot',during);
    await mouse(s,'mouseReleased',...q);
    const after = await fluidRead(s);
    fluidCheck(ctx,'release commits order immediately under reduced motion', after.keys.join()==='chat,assistant' && !after.zone &&
      await s.eval("!document.querySelector('.fluid-landing')"),after);

    const h2 = await bounds(s,`${pane('chat')} .card-title`);
    const point = [h2.left+60,h2.top+25];
    await dragFluid(s,point,[point[0],point[1]+160]);
    fluidCheck(ctx,'vertical-only drag does not reorder', (await fluidRead(s)).keys.join()==='chat,assistant');
    await mouse(s,'mousePressed',...point);
    await mouse(s,'mouseMoved',point[0]+450,point[1]+110);
    await esc(s);
    await mouse(s,'mouseReleased',point[0]+450,point[1]+110);
    fluidCheck(ctx,'Escape restores before releasing', (await fluidRead(s)).keys.join()==='chat,assistant' && !(await fluidRead(s)).zone);

    await mouse(s,'mousePressed',...point,'right');
    await mouse(s,'mouseMoved',point[0]+450,point[1]+120,'right');
    fluidCheck(ctx,'secondary button does not lift', !(await fluidRead(s)).zone);
    await mouse(s,'mouseReleased',point[0]+450,point[1]+120,'right');

    for (const [control, label] of [
      [`${pane('chat')} [aria-label=Terminal]`, 'header terminal button'],
      [`${pane('chat')} [data-next-message]`, 'composer field outside the header'],
    ]) {
      const r = await bounds(s,control);
      const a = [r.left+r.width/2,r.top+r.height/2];
      await mouse(s,'mousePressed',...a);
      await mouse(s,'mouseMoved',a[0]+60,a[1]+80);
      fluidCheck(ctx,`${label} does not lift`, !(await fluidRead(s)).zone);
      await mouse(s,'mouseReleased',a[0]+60,a[1]+80);
    }
    for (const trigger of ['Chats','Settings']) {
      await railClick(s,trigger);
      await mouse(s,'mousePressed',...point);
      await mouse(s,'mouseMoved',point[0]+36,point[1]+110);
      fluidCheck(ctx,`lifting closes ${trigger} popover`, await s.eval("document.querySelector('.popover').hidden") && !!(await fluidRead(s)).zone);
      await mouse(s,'mouseReleased',point[0]+36,point[1]+110);
    }

    await useFluid(s,`${pane('chat')} .card-title`); await chooseChat(s,fluidB);
    await s.click('.rail .brand');
    const h3=await bounds(s,`${pane('chat2')} .card-title`);
    const from=[h3.left+45,h3.top+26];
    await mouse(s,'mousePressed',...from);
    await mouse(s,'mouseMoved',from[0]+30,from[1]+70);
    fluidCheck(ctx,'lifting closes Assistant overlay', !(await fluidRead(s)).overlay && !!(await fluidRead(s)).zone);
    const rail = await bounds(s,'.rail [aria-label="Settings"]');
    const under = [rail.left+rail.width/2,rail.top+rail.height/2];
    await mouse(s,'mouseMoved',...under);
    await s.eval(`(() => {
      window.__fluidReleaseClicks = [];
      window.__fluidRecordClick = e => window.__fluidReleaseClicks.push(e.target.nodeName);
      document.addEventListener('click', window.__fluidRecordClick);
      return true;
    })()`);
    await mouse(s,'mouseReleased',...under);
    const release = await s.eval(`new Promise(resolve => requestAnimationFrame(() => {
      document.removeEventListener('click', window.__fluidRecordClick);
      resolve({clicks: window.__fluidReleaseClicks, hidden: document.querySelector('.popover').hidden});
      delete window.__fluidRecordClick; delete window.__fluidReleaseClicks;
    }))`);
    fluidCheck(ctx,'drop above settings delivers no click to any page target', release.hidden && release.clicks.length===0,release);

    await s.click(`${pane('chat2')} [aria-label="Full screen"]`);
    const full = await bounds(s,`${pane('chat2')} .card-title`), f=[full.left+35,full.top+22];
    await mouse(s,'mousePressed',...f);
    await mouse(s,'mouseMoved',f[0]+110,f[1]+65);
    fluidCheck(ctx,'nothing lifts in full screen', !(await fluidRead(s)).zone);
    await mouse(s,'mouseReleased',f[0]+110,f[1]+65);
    await esc(s);
    await closeFluid(s,'chat'); await closeFluid(s,'chat2');
    await mouse(s,'mousePressed',90,30);
    await mouse(s,'mouseMoved',290,170);
    fluidCheck(ctx,'nothing lifts when one section is visible', !(await fluidRead(s)).zone);
    await mouse(s,'mouseReleased',290,170);
  } finally { await noMotion(s,false); }
});

module.exports.fluidSides = isolated(async ctx => {
  const {session:s} = ctx;
  await fluidVisit(ctx,1440,true);
  await chooseChat(s,fluidA);
  await noMotion(s);
  try {
    const from = await bounds(s,`${pane('chat')} .card-title`);
    await dragFluid(s,[from.left+55,from.top+25],[1425,140]);
    const half = await fluidRead(s);
    const stage = await bounds(s,'.stage');
    fluidCheck(ctx,'deliberate right drop gives the card the right half', half.keys.at(-1)==='chat' &&
      Math.abs(half.sizes.chat-(stage.width-12)/2)<=12 && half.sizes.lanes===344,{half,stage});
    const lane = await bounds(s,'.next-lanes-head');
    await dragFluid(s,[lane.left+90,lane.top+28],[76,140]);
    const moved=await fluidRead(s);
    fluidCheck(ctx,'lanes move to the left edge without resizing',moved.keys[0]==='lanes'&&moved.sizes.lanes===344,moved);
    const r=await bounds(s,`${pane('chat')} .card-head`);
    // Start inside the top strip, initially move downward, then go upward by >10px.
    await mouse(s,'mousePressed',r.left+90,24);
    await mouse(s,'mouseMoved',r.left+108,22);
    fluidCheck(ctx,'starting in the top strip is not a full-screen drop', (await fluidRead(s)).zone==='slot');
    await mouse(s,'mouseMoved',r.left+108,9);
    fluidCheck(ctx,'moving deliberately up takes full screen', (await fluidRead(s)).zone==='full');
    await mouse(s,'mouseMoved',75,9);
    fluidCheck(ctx,'top wins in the corner', (await fluidRead(s)).zone==='full');
    await mouse(s,'mouseReleased',75,9);
    fluidCheck(ctx,'top drop makes the chat full screen', (await fluidRead(s)).keys.join()==='chat' &&
      await s.eval("!!document.querySelector('.chat-card:not([hidden]) [aria-label=\"Leave full screen\"]')"));
  } finally { await noMotion(s,false); }
});

module.exports.fluidBoundaries = isolated(async ctx => {
  const {session:s} = ctx;
  await fluidVisit(ctx,1440,true);
  await chooseChat(s,fluidA);
  let g=await bounds(s,'.grip:not([hidden])');
  const initial=await fluidRead(s);
  await dragFluid(s,[g.left+g.width/2,g.top+140],[g.left+g.width/2+90,g.top+140]);
  const moved=await fluidRead(s);
  fluidCheck(ctx,'grip changes only its two neighbours', Math.abs(moved.sizes.assistant-initial.sizes.assistant-90)<2 &&
    Math.abs(moved.sizes.chat-initial.sizes.chat+90)<2 && moved.sizes.lanes===344,{initial,moved});
  await s.send('Emulation.setDeviceMetricsOverride',{width:1600,height:900,deviceScaleFactor:1,mobile:false});
  await s.waitFor(`innerWidth===1600 && Math.abs(document.querySelector('[data-pane=chat]').getBoundingClientRect().width - ${moved.sizes.chat}) > 1`,{timeoutMs:ctx.timeoutMs});
  const resized=await fluidRead(s);
  const growth=resized.sizes.assistant+resized.sizes.chat-moved.sizes.assistant-moved.sizes.chat;
  fluidCheck(ctx,'drag shares scale when the viewport changes',
    Math.abs(growth-160)<3 && Math.abs(resized.sizes.assistant/resized.sizes.chat-590/410)<.005 &&
    Math.abs(moved.sizes.assistant/moved.sizes.chat-590/410)<.005 && resized.sizes.lanes===344,{moved,resized,growth});
  g=await bounds(s,'.grip:not([hidden])');
  for (const clickCount of [1,2]) {
    for (const type of ['mousePressed','mouseReleased']) await s.send('Input.dispatchMouseEvent',
      {type, x:g.left+g.width/2, y:g.top+140, button:'left', buttons:type==='mousePressed'?1:0, clickCount});
  }
  const reset=await fluidRead(s);
  fluidCheck(ctx,'double click restores automatic widths',Math.abs(reset.sizes.assistant-reset.sizes.chat)<2,reset);
  const a=await bounds(s,pane('chat')), b=await bounds(s,pane('lanes')), x=(a.right+b.left)/2, y=a.top+160;
  const hit=await s.eval(`!!document.elementFromPoint(${x},${y})?.closest('.grip')`);
  await dragFluid(s,[x,y],[x-60,y]);
  fluidCheck(ctx,'boundary touching lanes has no handle and leaves widths unchanged', !hit && JSON.stringify((await fluidRead(s)).sizes)===JSON.stringify(reset.sizes),reset);
});

module.exports.fluidPhone = isolated(async ctx => {
  const {session:s}=ctx;
  await fluidVisit(ctx,440);
  await railClick(s,'Settings');
  fluidCheck(ctx,'phone shows neither Sidebar nor Assistant head', await s.eval("getComputedStyle(document.querySelector('.sidebar-setting')).display==='none' && getComputedStyle(document.querySelector('.assistant-head')).display==='none'"));
  await esc(s);
  await chooseChat(s,fluidA);
  await chooseChat(s,fluidB);
  const state=await fluidRead(s);
  fluidCheck(ctx,'only the last chat is visible on a 440 px phone',state.keys.join()==='chat' && state.streams.chat===fluidB && !state.overlay,state);
  const r=await bounds(s,`${pane('chat')} .card-title`);
  await mouse(s,'mousePressed',r.left+20,r.top+18);
  await mouse(s,'mouseMoved',r.left+140,r.top+99);
  fluidCheck(ctx,'phone has no active grip, full screen or drag', !(await fluidRead(s)).zone &&
    await s.eval("!document.querySelector('.grip:not([hidden])') && getComputedStyle(document.querySelector('.chat-card:not([hidden]) .fullscreen')).display==='none'"));
  await mouse(s,'mouseReleased',r.left+140,r.top+99);
  await s.click('.rail .brand');
  fluidCheck(ctx,'phone brand still returns to Assistant and closes the chat', (await fluidRead(s)).keys.join()==='assistant');
});
