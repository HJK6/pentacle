'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { reportChecks } = require('./web_voice_scenario');

// The same DOM reader is serialized into Chromium and tested with synthetic DOM.
function readDashboardObservation(doc = document) {
  const panel = doc.getElementById('panel-dashboards');
  const content = doc.getElementById('dashboard-content');
  const modeler = content.querySelector('[data-modeler-state]');
  const frame = modeler?.querySelector('iframe');
  return {
    visible: panel.style.display !== 'none' && content.style.display !== 'none',
    ids: Array.from(doc.querySelectorAll('#dashboard-list [data-dashboard-id]'), el => el.dataset.dashboardId),
    state: modeler?.dataset.modelerState || null,
    text: modeler?.textContent || '',
    frameUrl: frame?.getAttribute('src') || null,
    sandbox: frame?.getAttribute('sandbox') || null,
    allow: frame?.getAttribute('allow') || null,
    loading: frame?.getAttribute('loading') || null,
    referrerPolicy: frame?.getAttribute('referrerpolicy') || null,
    open: !!modeler?.querySelector('[data-modeler-open]'),
    reload: !!modeler?.querySelector('[data-modeler-reload]'),
  };
}
function listChecks(observation, retiredRegistered) {
  return [
    ['Dashboards view is visible', observation.visible, observation],
    ['retired production adapters registered in the synthetic profile', retiredRegistered, { retiredRegistered }],
    ['retired boards are absent from the dashboard list', !observation.ids.includes('foreclosure-pipeline') && !observation.ids.includes('scraper-bot'), { ids: observation.ids }],
    ['3D Modeler is present in the dashboard list', observation.ids.includes('modeler-3d'), { ids: observation.ids }],
  ];
}
function viewerChecks(observation, url = null) {
  return [
    [url ? 'synthetic viewer navigation reaches loaded' : 'unconfigured viewer shows its configuration key',
      url ? observation.state === 'loaded' && observation.frameUrl === url
        : observation.state === 'unconfigured' && observation.text.includes('dashboards.modeler3d.url') && observation.frameUrl === null, observation],
    ['modeler always exposes open-window and reload controls', observation.open && observation.reload, observation],
    ...(url ? [['viewer uses the approved iframe attributes', observation.sandbox === 'allow-scripts allow-same-origin'
      && observation.allow === 'xr-spatial-tracking; fullscreen' && observation.loading === 'lazy'
      && observation.referrerPolicy === 'no-referrer', observation]] : []),
  ];
}
function configChecks(actual, expected) {
  return [['synthetic viewer URL reaches the renderer through its configured profile', actual === expected, { actual }]];
}
function chatChecks(observation) {
  return [
    ['returning to Chats keeps the same chat nodes and stream', observation.sameNodes && observation.sameStream, observation],
    ['returning to Chats preserves the transcript and unsent draft', observation.sameTranscript && observation.sameDraft, observation],
    ['returning to Chats hides dashboards and removes its iframe', observation.chatsVisible && observation.dashboardsHidden && observation.frames === 0, observation],
  ];
}
// Navigation can invalidate a CDP execution context between otherwise healthy
// evaluations, including a navigation from the previous scenario that is still
// settling when this one arms its marker. Retry only that lifecycle boundary,
// never arbitrary CDP failures.
const TRANSIENT_NAVIGATION_ERROR = /Execution context was destroyed|Cannot find context with specified id|Inspected target navigated or closed/;
async function reloadDashboardPage({ session, cdp, timeoutMs = 30000 }) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      await session.eval('window.__dashboardReloadMarker = true');
      break;
    } catch (error) {
      if (!TRANSIENT_NAVIGATION_ERROR.test(String(error?.message || error)) || Date.now() >= deadline) throw error;
      await cdp.sleep(100);
    }
  }
  await session.send('Page.reload', {});
  do {
    try {
      const ready = await session.eval("window.__dashboardReloadMarker !== true && document.readyState === 'complete' && typeof window.focusStreamId === 'function'");
      if (ready === true) return;
    } catch (error) {
      if (!TRANSIENT_NAVIGATION_ERROR.test(String(error?.message || error))) throw error;
    }
    if (Date.now() >= deadline) break;
    await cdp.sleep(100);
  } while (Date.now() < deadline);
  throw new Error('Timed out waiting for the fresh dashboard document after reload');
}
async function webDashboardsRevamp(ctx) {
  const { session, report, fixture, configureModeler, modelerFixtureUrl } = ctx;
  if (!fixture) { report.note('no hermetic profile: skipping dashboard fixture journey'); return; }
  if (!configureModeler || !modelerFixtureUrl) throw new Error('Hermetic dashboard fixture hooks are missing');
  const adapters = ['foreclosure.js', 'scraper-bot.js'].map(file => fs.readFileSync(path.join(__dirname, '../../../renderer/dashboards', file), 'utf8'));
  async function ready() {
    await reloadDashboardPage(ctx);
    await session.waitFor(`window.cc.getChatStreamState().then(s => s.connected && s.sessions.some(x => x.stream_id === ${JSON.stringify(fixture.streamId)}))`);
    // Real adapter manifests with synthetic profile configuration; without this,
    // retired-absent would pass vacuously because they are not bundled by default.
    for (const source of adapters) await session.eval(source);
    await session.eval(`window.focusStreamId(${JSON.stringify(fixture.streamId)})`);
    await session.waitFor("!!document.querySelector('[id^=header-] [data-mode=chat]')");
    await session.eval("document.querySelectorAll('[id^=header-] [data-mode=chat]').forEach(button => button.click())");
    await session.waitFor(`!!document.querySelector('.slot-chat-list[data-stream-id="${fixture.streamId}"]')?.textContent.includes(${JSON.stringify(fixture.transcript[0].text)})`);
    await session.eval(`(() => {
      const list = document.querySelector('.slot-chat-list[data-stream-id="${fixture.streamId}"]');
      const input = list.closest('.slot-chat-shell').querySelector('.slot-chat-compose-input');
      const previousDraft = input.value;
      input.value = 'unsent dashboard round-trip';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      window.__dashboardChatBefore = { list, input, grid: document.querySelector('.grid'), previousDraft,
        transcript: list.textContent, streamId: list.dataset.streamId, draft: input.value };
    })()`);
  }
  async function returnToChat() {
    await session.click('#view-chats');
    const observation = await session.eval(`(() => {
      const before = window.__dashboardChatBefore;
      const list = document.querySelector('.slot-chat-list[data-stream-id="${fixture.streamId}"]');
      const input = list?.closest('.slot-chat-shell').querySelector('.slot-chat-compose-input');
      const result = { sameNodes: list === before.list && input === before.input && document.querySelector('.grid') === before.grid,
        sameStream: list?.dataset.streamId === before.streamId, sameTranscript: list?.textContent === before.transcript,
        sameDraft: input?.value === before.draft, chatsVisible: document.querySelector('.grid').style.display !== 'none',
        dashboardsHidden: document.getElementById('panel-dashboards').style.display === 'none' && document.getElementById('dashboard-content').style.display === 'none',
        frames: document.querySelectorAll('#dashboard-content iframe').length };
      if (input) { input.value = before.previousDraft; input.dispatchEvent(new Event('input', { bubbles: true })); }
      delete window.__dashboardChatBefore;
      return result;
    })()`);
    reportChecks(report, chatChecks(observation));
  }
  let configured = false;
  try {
    await ready();
    await session.click('#view-dashboards');
    const retiredRegistered = await session.eval("['foreclosure-pipeline', 'scraper-bot'].every(id => window.DASHBOARDS.some(d => d.id === id && d.retired === true))");
    reportChecks(report, listChecks(await session.eval(`(${readDashboardObservation.toString()})()`), retiredRegistered));
    await session.click('[data-dashboard-id="modeler-3d"]');
    reportChecks(report, viewerChecks(await session.eval(`(${readDashboardObservation.toString()})()`)));
    await returnToChat();

    // This rewrites only the scratch hermetic profile and restarts its web host.
    // No DOM/adapter override can substitute for the actual config bridge.
    configured = true;
    await configureModeler(true);
    await ready();
    const urlFromBridge = await session.eval('window.cc.getConfig().then(config => config.dashboards?.modeler3d?.url)');
    reportChecks(report, configChecks(urlFromBridge, modelerFixtureUrl));
    await session.click('#view-dashboards');
    await session.click('[data-dashboard-id="modeler-3d"]');
    await session.waitFor("document.querySelector('[data-modeler-state]')?.dataset.modelerState === 'loaded'");
    reportChecks(report, viewerChecks(await session.eval(`(${readDashboardObservation.toString()})()`), modelerFixtureUrl));
    if (report.dir) await session.screenshot(path.join(report.dir, 'dashboards-synthetic-modeler.png'));
    await returnToChat();
  } finally {
    if (configured) await configureModeler(false);
    await reloadDashboardPage(ctx);
  }
}
module.exports = { readDashboardObservation, listChecks, viewerChecks, configChecks, chatChecks, reloadDashboardPage, webDashboardsRevamp };
