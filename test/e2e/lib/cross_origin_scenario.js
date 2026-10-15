'use strict';
const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const WebSocket = require('ws');

// Browser-isolation boundary for the unauthenticated loopback web host and the
// loopback daemon exemption. A page on a different local origin, and a page
// reached through a DNS-rebinding hostname, must not reach either operator
// surface; the served same-origin app and Origin-less native clients must.
// Hermetic only: it needs the gate's own daemon port, CDP port and the Chrome
// host-resolver rule web_gate.js installs for REBIND_HOST.
const REBIND_HOST = 'pentacle-rebind.test';

// Evaluated inside a page: open one socket, optionally send a hello, and
// summarize what the page could observe within the window.
function probeSocketSource(wsUrl, hello) {
  return `new Promise((resolve) => {
    const out = { opened: false, types: [], errorCodes: [], closeCode: null };
    let ws;
    try { ws = new WebSocket(${JSON.stringify(wsUrl)}); } catch (e) { resolve({ ...out, ctorError: String(e) }); return; }
    const finish = () => { try { ws.close(); } catch {} resolve(out); };
    ws.onopen = () => { out.opened = true; ${hello ? `ws.send(${JSON.stringify(JSON.stringify(hello))});` : ''} };
    ws.onmessage = (event) => {
      try { const frame = JSON.parse(event.data); out.types.push(frame.type); if (frame.error_code) out.errorCodes.push(frame.error_code); }
      catch { out.types.push('unparsed'); }
    };
    ws.onclose = (event) => { out.closeCode = event.code; resolve(out); };
    setTimeout(finish, 2500);
  })`;
}

function nativeDaemonProbe(port, hello) {
  return new Promise((resolve) => {
    const out = { opened: false, types: [], errorCodes: [] };
    const ws = new WebSocket(`ws://127.0.0.1:${port}`); // Node ws sends no Origin
    const timer = setTimeout(() => { try { ws.close(); } catch {} resolve(out); }, 2500);
    ws.on('open', () => { out.opened = true; ws.send(JSON.stringify(hello)); });
    ws.on('message', (raw) => {
      try { const frame = JSON.parse(String(raw)); out.types.push(frame.type); if (frame.error_code) out.errorCodes.push(frame.error_code); }
      catch { out.types.push('unparsed'); }
    });
    ws.on('error', () => { clearTimeout(timer); resolve(out); });
  });
}

// Open the /cc bridge from the page and keep any socket it gets open, so the
// host's connection count is read while a refused-or-not socket would still
// be counted.
function holdBridgeSource(wsUrl) {
  return `new Promise((resolve) => {
    const out = { opened: false, types: [], errorCodes: [], closeCode: null };
    let ws;
    try { ws = new WebSocket(${JSON.stringify(wsUrl)}); } catch (e) { resolve({ ...out, ctorError: String(e) }); return; }
    window.__pentacleBridgeProbe = ws;
    ws.onopen = () => { out.opened = true; setTimeout(() => resolve(out), 300); };
    ws.onmessage = (event) => {
      try { const frame = JSON.parse(event.data); out.types.push(frame.type); if (frame.error_code) out.errorCodes.push(frame.error_code); }
      catch { out.types.push('unparsed'); }
    };
    ws.onclose = (event) => { out.closeCode = event.code; resolve(out); };
    setTimeout(() => resolve(out), 2500);
  })`;
}

function hostHealth(port) {
  return new Promise((resolve, reject) => {
    http.get({ host: '127.0.0.1', port, path: '/api/health' }, (res) => {
      let body = '';
      res.on('data', (chunk) => { body += chunk; });
      res.on('end', () => { try { resolve(JSON.parse(body)); } catch (e) { reject(e); } });
    }).on('error', reject);
  });
}

// A cross-origin fetch from the page. The page itself only sees an opaque
// response, so the status comes from the browser's network log: a refusal
// must be the host's own 403, not a generic CORS failure (which has no
// status). Chrome's ORB usually withholds the no-cors body; when it does not,
// the body must be the host's origin_refused.
async function browserFetch(page, url) {
  const seen = { status: null, body: null, failed: null };
  let requestId = null;
  let finished;
  const done = new Promise((resolve) => { finished = resolve; });
  const listener = (raw) => {
    let msg;
    try { msg = JSON.parse(raw); } catch { return; }
    if (msg.method === 'Network.responseReceived' && msg.params.response.url === url) {
      requestId = msg.params.requestId;
      seen.status = msg.params.response.status;
    } else if (msg.method === 'Network.loadingFinished' && msg.params.requestId === requestId) {
      finished();
    } else if (msg.method === 'Network.loadingFailed' && (requestId === null || msg.params.requestId === requestId)) {
      seen.failed = msg.params.errorText;
      finished();
    }
  };
  page.ws.on('message', listener);
  try {
    await page.send('Network.enable');
    seen.page = await page.eval(`fetch(${JSON.stringify(url)}, { mode: 'no-cors', cache: 'no-store' })
      .then((r) => ({ type: r.type, status: r.status })).catch((e) => ({ error: String(e) }))`);
    await Promise.race([done, new Promise((resolve) => setTimeout(resolve, 3000))]);
    if (requestId) {
      const { body } = await page.send('Network.getResponseBody', { requestId }).catch(() => ({ body: null }));
      seen.body = typeof body === 'string' ? body.slice(0, 80) : null;
    }
  } finally {
    page.ws.off('message', listener);
  }
  return seen;
}

// Credentialed browser hello. The proof is computed in Node from the isolated
// gate credential and the page's own welcome nonce; only that single-use proof
// enters the page, never the proof key.
function operatorHello(tokenPath, welcome) {
  const prefix = 'pentacle-auth-v2:';
  const envelope = fs.readFileSync(tokenPath, 'utf8').trim();
  if (!envelope.startsWith(prefix)) throw new Error('isolated gate credential is not an operator v2 envelope');
  const record = JSON.parse(Buffer.from(envelope.slice(prefix.length), 'base64url').toString('utf8'));
  const operator = welcome && welcome.auth && welcome.auth.operator;
  if (!operator || typeof operator.nonce !== 'string') throw new Error('daemon welcome offered no operator nonce');
  const transcript = `pentacle-operator-v2\0chat-streamd\0${operator.nonce}\0${record.credential_id}\0${record.client_kind}`;
  return {
    type: 'hello', client: record.client_kind, subscribe: { exclude_event_types: ['hosts.stats'] },
    auth_v2: {
      scheme: operator.scheme, credential_id: record.credential_id,
      proof: crypto.createHmac('sha256', Buffer.from(record.proof_key, 'base64url')).update(transcript, 'utf8').digest('base64url'),
    },
  };
}

async function credentialedDaemonProbe(page, wsUrl, tokenPath) {
  const welcome = await page.eval(`new Promise((resolve) => {
    const probe = { frames: [], closeCode: null };
    window.__pentacleAuthProbe = probe;
    const ws = new WebSocket(${JSON.stringify(wsUrl)});
    probe.ws = ws;
    ws.onmessage = (event) => {
      let frame;
      try { frame = JSON.parse(event.data); } catch { frame = { type: 'unparsed' }; }
      probe.frames.push(frame);
      if (frame.type === 'welcome') resolve(frame);
    };
    ws.onclose = (event) => { probe.closeCode = event.code; resolve(null); };
    setTimeout(() => resolve(null), 2500);
  })`);
  if (!welcome) return { welcome: false, types: [], errorCodes: [] };
  const hello = operatorHello(tokenPath, welcome);
  return page.eval(`new Promise((resolve) => {
    const probe = window.__pentacleAuthProbe;
    const start = probe.frames.length;
    const frames = () => probe.frames.slice(start);
    const summary = () => {
      const snapshot = frames().find((frame) => frame.type === 'snapshot');
      return { welcome: true, types: frames().map((frame) => frame.type),
        errorCodes: frames().filter((frame) => frame.error_code).map((frame) => frame.error_code),
        snapshotSessions: snapshot && Array.isArray(snapshot.sessions) ? snapshot.sessions.length : null,
        closeCode: probe.closeCode };
    };
    let listed = false;
    const finish = () => { clearInterval(tick); clearTimeout(timer); try { probe.ws.close(); } catch {} resolve(summary()); };
    const tick = setInterval(() => {
      const types = frames().map((frame) => frame.type);
      if (!listed && types.includes('snapshot')) { listed = true; probe.ws.send(JSON.stringify({ type: 'list_sessions' })); }
      if (types.includes('list_sessions.ok') || types.includes('hello.error') || probe.closeCode !== null) finish();
    }, 50);
    const timer = setTimeout(finish, 4000);
    probe.ws.send(${JSON.stringify(JSON.stringify(hello))});
  })`);
}

async function openPage(ctx, url, match) {
  const { session, cdp, cdpPort } = ctx;
  const { targetId } = await session.send('Target.createTarget', { url });
  const page = await cdp.connect(cdpPort, { match, timeoutMs: 15000 });
  // cdp.connect falls back to ANY page when nothing matches; probing the
  // served app by mistake would invert this scenario's evidence.
  if (page.target.id !== targetId) {
    page.close();
    await session.send('Target.closeTarget', { targetId }).catch(() => {});
    throw new Error(`attached to ${page.target.url}, not the created target ${url}`);
  }
  // A just-created target starts on about:blank and then navigates, which
  // replaces the execution context; wait for the requested document itself.
  const deadline = Date.now() + 15000;
  let state = null;
  for (;;) {
    try {
      state = await page.eval('({ href: location.href, ready: document.readyState })');
      if (state.ready !== 'loading' && state.href.startsWith(url)) break;
    } catch { /* context not ready */ }
    if (Date.now() > deadline) { page.close(); throw new Error(`${url} did not load (last: ${JSON.stringify(state)})`); }
    await cdp.sleep(200);
  }
  return { page, close: async () => { page.close(); await session.send('Target.closeTarget', { targetId }).catch(() => {}); } };
}

async function crossOriginBoundary(ctx) {
  const { session, report, url, runtime, cdpPort } = ctx;
  const daemonPort = runtime && runtime.fixtureDaemonPort;
  if (!daemonPort || !cdpPort) {
    report.note('cross-origin-boundary: hermetic-only scenario skipped (no fixture daemon/CDP port)');
    return;
  }
  const hostPort = Number(new URL(url).port);
  const hello = { type: 'hello', client: 'pentacle', subscribe: { exclude_event_types: ['hosts.stats'] } };

  // Positive controls: the served app's own origin and an Origin-less native tool.
  const sameOrigin = await session.eval(probeSocketSource(`ws://127.0.0.1:${hostPort}/cc`, null));
  report.ok('the served same-origin page still opens the /cc bridge', sameOrigin.opened === true, sameOrigin);
  const native = await nativeDaemonProbe(daemonPort, hello);
  report.ok('an Origin-less native loopback client is still admitted to the daemon',
    native.opened && native.types.includes('hello') && native.types.includes('snapshot') && native.errorCodes.length === 0, native);

  const foreign = http.createServer((req, res) => {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' }).end('<!doctype html><title>pentacle-foreign-origin</title><p>foreign</p>');
  });
  await new Promise((resolve) => foreign.listen(0, '127.0.0.1', resolve));
  const foreignUrl = `http://127.0.0.1:${foreign.address().port}/pentacle-foreign-origin`;
  let foreignPage = null;
  let rebindPage = null;
  const observed = {};
  try {
    observed.bridgeConnectionsBefore = (await hostHealth(hostPort)).connections;
    foreignPage = await openPage(ctx, foreignUrl, /pentacle-foreign-origin/);
    observed.fetch = await browserFetch(foreignPage.page, `http://127.0.0.1:${hostPort}/`);
    observed.bridge = await foreignPage.page.eval(holdBridgeSource(`ws://127.0.0.1:${hostPort}/cc`));
    observed.bridgeConnectionsAfter = (await hostHealth(hostPort)).connections;
    await foreignPage.page.eval('(() => { try { window.__pentacleBridgeProbe.close(); } catch {} return true; })()');
    observed.daemon = await foreignPage.page.eval(probeSocketSource(`ws://127.0.0.1:${daemonPort}`, hello));
    // The same foreign-origin page, now holding a valid operator credential.
    observed.credentialed = runtime.fixtureOperatorTokenPath
      ? await credentialedDaemonProbe(foreignPage.page, `ws://127.0.0.1:${daemonPort}`, runtime.fixtureOperatorTokenPath)
      : null;
    // DNS rebinding: an attacker hostname that resolves to loopback (Chrome
    // host-resolver rule) is same-origin with itself, so only the Host check
    // can refuse it.
    const rebindUrl = `http://${REBIND_HOST}:${hostPort}/`;
    rebindPage = await openPage(ctx, rebindUrl, new RegExp(REBIND_HOST.replace(/\./g, '\\.')));
    observed.rebind = await rebindPage.page.eval(`new Promise((resolve) => setTimeout(() => resolve({
      href: location.href, body: (document.body && document.body.innerText || '').slice(0, 80), hasBridge: !!window.cc,
    }), 1500))`);
    observed.rebindUrl = rebindUrl;
  } finally {
    if (rebindPage) await rebindPage.close();
    if (foreignPage) await foreignPage.close();
    await new Promise((resolve) => foreign.close(resolve));
  }
  // Record every vector before asserting, so a failing run shows all of them.
  report.note(`cross-origin-boundary observed: ${JSON.stringify(observed)}`);
  const { fetch: foreignFetch, bridge, daemon, credentialed, rebind, rebindUrl } = observed;
  report.ok('a foreign-origin fetch gets the host\'s own 403 refusal',
    foreignFetch.status === 403 && (!foreignFetch.body || foreignFetch.body.includes('origin_refused')), foreignFetch);
  report.ok('a foreign-origin page cannot open the unauthenticated /cc bridge', bridge.opened === false && bridge.types.length === 0, bridge);
  report.ok('no bridge connection was added by the foreign-origin page',
    Number.isInteger(observed.bridgeConnectionsBefore) && observed.bridgeConnectionsAfter === observed.bridgeConnectionsBefore,
    { before: observed.bridgeConnectionsBefore, after: observed.bridgeConnectionsAfter });
  report.ok('a foreign-origin page gets authentication_required from the daemon, never a snapshot',
    daemon.errorCodes.includes('authentication_required') && !daemon.types.includes('snapshot') && !daemon.types.includes('hello'), daemon);
  report.ok('a foreign-origin page with a valid operator credential is admitted to the daemon',
    !!credentialed && credentialed.types.includes('hello') && credentialed.snapshotSessions > 0
      && credentialed.types.includes('list_sessions.ok') && credentialed.errorCodes.length === 0, credentialed);
  report.ok('a DNS-rebinding hostname is refused by the loopback web host',
    rebind.href.startsWith(rebindUrl) && rebind.body.includes('origin_refused') && rebind.hasBridge === false, rebind);
}

module.exports = { crossOriginBoundary, REBIND_HOST };
