'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { execFileSync } = require('node:child_process');
const WebSocket = require('ws');
const { reloadDashboardPage } = require('./dashboard_scenario');

const ROOT = path.resolve(__dirname, '../../..');
const STREAM = 'local:web-gate-seam';
const sha256 = value => createHash('sha256').update(value).digest('hex');
const pin = root => execFileSync('git', ['-C', root, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();

// Capture only real synthetic application frames. Welcome/auth/proof frames
// never enter evidence. Keep raw response bytes for the mobile adapter replay.
async function captureWire(port, frames, phase, runtimes) {
  const ws = new WebSocket(`ws://127.0.0.1:${port}`);
  ws.on('message', bytes => {
    const raw = bytes.toString();
    const frame = JSON.parse(raw);
    if (frame.type === 'welcome') runtimes.push({ phase, runtime_sha: frame.runtime_sha });
    if (['snapshot', 'session.inventory', 'list_sessions.ok', 'request_stream_events.ok',
      'request_stream_events.chunk', 'chat.event'].includes(frame.type)) frames.push({ phase, raw });
  });
  await new Promise((resolve, reject) => {
    ws.once('open', resolve); ws.once('error', reject);
  });
  ws.send(JSON.stringify({ type: 'hello', client: 'client-seam-test',
    subscribe: { all: true, include_subagents: true, events_mode: 'summary', exclude_event_types: ['hosts.stats'] } }));
  return ws;
}

async function installedClientSeam(ctx) {
  const { session, report, cdp, runtime, timeoutMs } = ctx;
  if (!runtime.fixtureScratch || !runtime.fixtureDaemonPort) throw new Error('client seam requires the isolated candidate daemon');
  const { waitForValue } = require('./web_scenarios');
  const wait = (expression, predicate, label) => waitForValue(session, cdp, expression, predicate, { timeoutMs, label });
  const frames = [];
  const sockets = [];
  const capturePath = path.join(report.dir, 'client-seam-wire.json');
  const fixture = JSON.parse(fs.readFileSync(path.join(runtime.fixtureScratch, 'client-seam-fixture.json'), 'utf8'));
  const consumer = process.env.PENTACLE_WEB_GATE_CONSUMER_ROOT || ROOT;
  const identity = {
    schema: 'pentacle.client-seam.v1', producer_sha: pin(ROOT), consumer_sha: pin(consumer),
    producer_sources: Object.fromEntries(['services/chat-stream-v2/session_summary.py', 'test/e2e/lib/web_gate_daemon.py',
      'test/e2e/lib/seed_web_gate.py'].map(file => [file, sha256(fs.readFileSync(path.join(ROOT, file)))])),
    consumer_assets: Object.fromEntries(['bundle.js', 'chat_core.bundle.js'].map(file =>
      [file, sha256(fs.readFileSync(path.join(consumer, 'renderer/dist/web', file)))])),
    stream_id: STREAM, expected: { text: fixture.text, update: fixture.update },
    frames,
    producer_runtime: [],
  };
  const waitFrame = async (type, phase, predicate = () => true) => {
    const until = Date.now() + timeoutMs;
    while (Date.now() < until) {
      const found = frames.find(item => item.phase === phase && JSON.parse(item.raw).type === type && predicate(JSON.parse(item.raw)));
      if (found) return JSON.parse(found.raw);
      await cdp.sleep(30);
    }
    throw new Error(`HARNESS_ERROR: no real ${type} in phase ${phase}`);
  };
  const action = async name => {
    const done = path.join(runtime.fixtureScratch, 'client-seam-command.done');
    fs.rmSync(done, { force: true });
    const command = path.join(runtime.fixtureScratch, 'client-seam-command.json');
    fs.writeFileSync(command + '.tmp', JSON.stringify({ action: name }));
    fs.renameSync(command + '.tmp', command);
    const until = Date.now() + timeoutMs;
    while (!fs.existsSync(done) && Date.now() < until) await cdp.sleep(30);
    if (!fs.existsSync(done) || fs.readFileSync(done, 'utf8') !== name) throw new Error(`HARNESS_ERROR: ingest action ${name} failed`);
  };
  let hook;
  try {
    const first = await captureWire(runtime.fixtureDaemonPort, frames, 'initial', identity.producer_runtime); sockets.push(first);
    const snapshot = await waitFrame('snapshot', 'initial');
    report.ok('running candidate daemon advertises the pinned producer source',
      identity.producer_runtime[0]?.runtime_sha === identity.producer_sha, identity.producer_runtime);
    first.send(JSON.stringify({ type: 'list_sessions', request_id: 'client-seam-list' }));
    const listed = await waitFrame('list_sessions.ok', 'initial');
    report.ok('real summary snapshot and list carry the seeded multiline message without history',
      !(snapshot.events || []).length && typeof snapshot.sessions.find(row => row.stream_id === STREAM)?.last_text === 'string'
      && listed.active.find(row => row.stream_id === STREAM)?.last_text === snapshot.sessions.find(row => row.stream_id === STREAM)?.last_text);

    hook = await session.send('Page.addScriptToEvaluateOnNewDocument', { source: `(() => {
      const Native = window.WebSocket;
      window.__seamSockets = [];
      window.WebSocket = function(...args) {
        const socket = new Native(...args);
        if (String(args[0]).endsWith('/cc')) window.__seamSockets.push(socket);
        return socket;
      };
      window.WebSocket.prototype = Native.prototype;
      for (const key of ['CONNECTING','OPEN','CLOSING','CLOSED']) window.WebSocket[key] = Native[key];
    })();` });
    await reloadDashboardPage(ctx);
    await wait(`!!window.PentacleChatStore?.getState().sessions.find(row => row.stream_id === ${JSON.stringify(STREAM)})`, Boolean, 'real seam inventory in browser');
    await session.eval(`(() => {
      const stream = ${JSON.stringify(STREAM)};
      const store = window.PentacleChatStore;
      const container = document.createElement('div');
      container.style.cssText = 'position:fixed;inset:20px;width:800px;overflow:auto;z-index:100000;background:white;color:black';
      document.body.appendChild(container);
      const probe = window.__clientSeam = { container, phase: 'summary', samples: [], mutations: [] };
      probe.sample = () => {
        const items = store.selectSessionDetail(stream, {visibleCount:'all'}).transcriptItems;
        return { phase: probe.phase, rows: container.querySelectorAll('.slot-chat-row').length,
          headings: [...container.querySelectorAll('.slot-chat-md-heading')].map(node => node.textContent),
          listItems: [...container.querySelectorAll('.slot-chat-md-list li')].map(node => node.textContent),
          code: container.querySelector('.slot-chat-md-code code')?.textContent || '',
          items: items.map(item => ({text:item.text, key:item.eventKey})),
          fallbacks: items.filter(item => item.eventKey === 'uncorrelated_fallback').length };
      };
      probe.render = () => {
        window.PentacleChatView.renderStreamTranscript(stream, container, {visibleCount:'all'});
        probe.samples.push(probe.sample());
      };
      probe.observer = new MutationObserver(() => probe.mutations.push(probe.sample()));
      probe.observer.observe(container, {childList:true, subtree:true, characterData:true});
      probe.unsubscribe = store.subscribe(probe.render);
      probe.render();
      return true;
    })()`);
    const before = await session.eval('window.__clientSeam.sample()');
    fs.writeFileSync(path.join(report.dir, 'client-seam-before.json'), JSON.stringify(before, null, 2));
    await session.screenshot(path.join(report.dir, 'client-seam-before.png'));
    const formatted = value => value.rows === 1 && value.headings[0] === 'Summary fallback probe'
      && JSON.stringify(value.listItems) === JSON.stringify(['First item', 'Second item'])
      && value.code === 'const first = 1;\n  const second = 2;';
    report.ok('candidate daemon summary renders multiline Markdown in the real web consumer before history', formatted(before), before);
    await session.eval(`window.__clientSeam.phase = 'history'; window.cc.requestStreamEvents({streamId:${JSON.stringify(STREAM)}, limit:20})`, { awaitPromise: true });
    await wait('window.__clientSeam.sample().fallbacks === 0 && window.__clientSeam.sample().items.length > 0', Boolean, 'real history replaces summary');
    const held = await session.eval('window.__clientSeam.sample()');
    report.ok('real browser history preserves the message, formatting and committed identity', formatted(held)
      && held.items.filter(item => item.text === fixture.text).length === 1 && held.items[0].key !== 'uncorrelated_fallback', held);
    first.send(JSON.stringify({ type: 'request_stream_events', stream_id: STREAM, limit: 20, request_id: 'client-seam-history' }));
    await waitFrame('request_stream_events.ok', 'initial');

    await session.eval("window.__clientSeam.phase = 'update'");
    await action('update');
    await waitFrame('session.inventory', 'initial', frame => frame.sessions.some(row => row.stream_id === STREAM && row.last_text === fixture.update));
    await wait(`window.__clientSeam.sample().items.some(item => item.text === ${JSON.stringify(fixture.update)})`, Boolean, 'live ingested update reaches browser');
    const updated = await session.eval('window.__clientSeam.sample()');
    report.ok('live candidate ingest preserves browser message order',
      JSON.stringify(updated.items.map(item => item.text)) === JSON.stringify([fixture.text, fixture.update]), updated);

    await session.eval("window.__clientSeam.phase = 'reconnect'; window.__seamSockets.at(-1).close()");
    await wait('window.__seamSockets.length >= 2 && window.__seamSockets.at(-1).readyState === 1', Boolean, 'real browser socket reconnect');
    await session.eval(`window.cc.requestStreamEvents({streamId:${JSON.stringify(STREAM)}, limit:20})`, { awaitPromise: true });
    const second = await captureWire(runtime.fixtureDaemonPort, frames, 'reconnect', identity.producer_runtime); sockets.push(second);
    await waitFrame('snapshot', 'reconnect');
    report.ok('reconnected candidate daemon retains the pinned runtime source',
      identity.producer_runtime.at(-1)?.runtime_sha === identity.producer_sha, identity.producer_runtime);
    second.send(JSON.stringify({ type: 'request_stream_events', stream_id: STREAM, limit:20, request_id:'client-seam-reconnect-history' }));
    await waitFrame('request_stream_events.ok', 'reconnect');
    await action('replay');
    await waitFrame('chat.event', 'reconnect');
    await cdp.sleep(150);
    const after = await session.eval('window.__clientSeam.sample()');
    report.ok('reconnect and real daemon replay keep stable browser message keys without duplicates',
      JSON.stringify(after.items) === JSON.stringify(updated.items), { updated, after });
    const observation = await session.eval('({samples:window.__clientSeam.samples, mutations:window.__clientSeam.mutations})');
    const historyMutations = observation.mutations.filter(item => item.phase === 'history');
    report.ok('MutationObserver sees no preview fallback or duplicate flicker after history arrives',
      historyMutations.length > 0 && historyMutations.every(item => item.fallbacks === 0 && item.rows === 1), historyMutations);
    fs.writeFileSync(path.join(report.dir, 'client-seam-dom.json'), JSON.stringify({ before, held, updated, after, ...observation }, null, 2));
    await session.screenshot(path.join(report.dir, 'client-seam.png'));
    const history = JSON.parse(frames.find(item => item.phase === 'initial' && JSON.parse(item.raw).type === 'request_stream_events.ok').raw);
    for (const mode of ['missing', 'malformed', 'legacy']) {
      const negative = structuredClone(snapshot);
      const row = negative.sessions.find(item => item.stream_id === STREAM);
      const negativeStream = `${STREAM}-negative-${mode}`;
      row.stream_id = negativeStream;
      negative.sessions = [...snapshot.sessions, row];
      negative.events = [];
      if (mode === 'missing') { delete row.last_text; delete row.last_kind; }
      if (mode === 'malformed') { row.last_text = null; row.last_kind = null; }
      if (mode === 'legacy') row.last_text = row.last_text.replace(/\s+/g, ' ');
      const negativeObservation = await session.eval(`(() => {
        window.PentacleChatStore.applyFrame(${JSON.stringify(negative)});
        const select = () => window.PentacleChatStore.selectSessionDetail(${JSON.stringify(negativeStream)}, {visibleCount:'all'}).transcriptItems.map(item => item.text);
        const preview = select();
        window.PentacleChatStore.applyFrame({type:'stream_events', stream_id:${JSON.stringify(negativeStream)},
          events:${JSON.stringify(history.events.map(event => ({ ...event, stream_id: negativeStream })))}});
        return {preview, history:select()};
      })()`);
      report.ok(`synthetic ${mode} optional-summary control recovers exact real history`,
        (mode === 'legacy' ? negativeObservation.preview.length === 1 : negativeObservation.preview.length === 0)
        && JSON.stringify(negativeObservation.history) === JSON.stringify([fixture.text]), negativeObservation);
    }
  } finally {
    fs.writeFileSync(capturePath, JSON.stringify(identity, null, 2));
    await Promise.all(sockets.map(socket => new Promise(resolve => {
      if (socket.readyState === WebSocket.CLOSED) { resolve(); return; }
      socket.once('close', resolve); socket.close();
    })));
    if (hook?.identifier) await session.send('Page.removeScriptToEvaluateOnNewDocument', { identifier: hook.identifier });
    await session.eval('(() => { const p = window.__clientSeam; if(p) { p.unsubscribe(); p.observer.disconnect(); p.container.remove(); delete window.__clientSeam; } return true; })()');
    await reloadDashboardPage(ctx);
  }
}

module.exports = { installedClientSeam };
