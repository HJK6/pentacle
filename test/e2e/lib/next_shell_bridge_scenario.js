'use strict';

const assert = require('node:assert/strict');

function installShellFixture() {
  if (location.pathname !== '/next/') return;
  const received = { ready: [], attention: [] };
  const listeners = {};
  window.pentacleShell = {
    version: 1,
    ready: value => received.ready.push(value),
    setAttention: value => received.attention.push(value),
    onNavigate: callback => { listeners.navigate = callback; },
    onShowing: callback => { listeners.showing = callback; },
  };
  window.__shellFixture = {
    received,
    go(view) { listeners.showing(false); listeners.navigate(view); listeners.showing(true); },
    hide() { listeners.showing(false); },
    reply(stream, sequence) {
      const store = window.PentacleChatStore;
      const row = store.getState().sessions.find(row => row.stream_id === stream);
      store.applyFrame({ type: 'chat.event', event: {
        ...row, stream_id: stream, daemon_seq: sequence, timestamp: new Date().toISOString(),
        kind: 'ASSIST_TEXT', text: 'Synthetic shell reply ' + sequence,
      } });
    },
  };
}

async function nextShellBridge({ session, report, url, fixture, timeoutMs }) {
  if (!fixture) throw new Error('Shell bridge scenario requires the hermetic fixture');
  const { identifier } = await session.send('Page.addScriptToEvaluateOnNewDocument', {
    source: '(' + installShellFixture.toString() + ')()',
  });
  const wait = (expression, label) => session.waitFor(expression, { timeoutMs, label });
  const counts = '(window.__shellFixture.received.attention.at(-1) || window.__shellFixture.received.ready[0])';
  const go = view => session.eval('window.__shellFixture.go(' + JSON.stringify(view) + ')');
  const reply = (stream, sequence) => session.eval('window.__shellFixture.reply(' + JSON.stringify(stream) + ', ' + sequence + ')');
  const assistant = 'local:web-gate-assistant';
  try {
    await session.send('Emulation.setDeviceMetricsOverride', { width: 420, height: 680, deviceScaleFactor: 1, mobile: false });
    await session.send('Page.navigate', { url: url + 'next/' });
    await wait("window.__shellFixture?.received.ready.length === 1 && !!document.querySelector('.pane .thread-inner')?.dataset.streamId", 'version 1 first inventory');
    const first = await session.eval('window.__shellFixture.received.ready[0]');
    assert.deepEqual([first.version, first.chats, first.assistant], [1, 0, 0]);
    report.ok('version 1 handshake carries the first inventory at single-pane width',
      await session.eval('innerWidth === 420 && document.querySelector(".brand").classList.contains("on")'), first);

    await reply(assistant, 290000001);
    await wait(counts + '.assistant === 1', 'hidden shell still reports unread Assistant');
    const destinations = {
      lanes: "document.querySelector('.lanes-card').hidden === false",
      dashboards: "document.querySelector('.rail-button[aria-label=Dashboards]').getAttribute('aria-pressed') === 'true'",
      chats: "document.querySelector('.chats-button').getAttribute('aria-expanded') === 'true'",
      questions: "document.querySelector('.questions-popover')?.hidden === false",
    };
    for (const [view, condition] of Object.entries(destinations)) {
      for (let repeat = 0; repeat < 2; repeat++) {
        await go(view);
        await wait(condition, view + ' stays open');
        await session.eval('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
        assert.equal(await session.eval(counts + '.assistant'), 1);
      }
    }
    report.ok('each shell destination is idempotent and keeps the unread Assistant', true);

    await go('assistant');
    await wait(counts + '.assistant === 0', 'showing Assistant clears only its count');
    assert.equal(await session.eval('document.querySelector(".popover").hidden'), true);
    await session.eval('window.__shellFixture.hide()');
    await reply(fixture.streamId, 290000002);
    await wait(counts + '.chats === 1', 'hidden unread chat');
    await go('chats');
    await wait(destinations.chats, 'messages list opened');
    assert.equal(await session.eval(counts + '.chats'), 1);
    const row = '.popover .next-chat-pick[data-stream-id="' + fixture.streamId + '"]';
    await wait('!!document.querySelector(' + JSON.stringify(row) + ')', 'seeded unread chat row');
    await session.click(row);
    await wait(counts + '.chats === 0', 'opening the unread chat clears its count');
    report.ok('opening the Assistant or a chat from Messages republishes counts with no new traffic', true);
  } finally {
    await session.send('Page.removeScriptToEvaluateOnNewDocument', { identifier });
    await session.send('Emulation.clearDeviceMetricsOverride');
    await session.send('Page.navigate', { url });
    await wait("document.readyState === 'complete' && !!document.getElementById('session-list')", 'ordinary web restored');
  }
}

module.exports = { nextShellBridge };
