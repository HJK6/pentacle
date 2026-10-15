'use strict';
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
    foreignPage = await openPage(ctx, foreignUrl, /pentacle-foreign-origin/);
    observed.bridge = await foreignPage.page.eval(probeSocketSource(`ws://127.0.0.1:${hostPort}/cc`, null));
    observed.daemon = await foreignPage.page.eval(probeSocketSource(`ws://127.0.0.1:${daemonPort}`, hello));
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
  const { bridge, daemon, rebind, rebindUrl } = observed;
  report.ok('a foreign-origin page cannot open the unauthenticated /cc bridge', bridge.opened === false && bridge.types.length === 0, bridge);
  report.ok('a foreign-origin page gets authentication_required from the daemon, never a snapshot',
    daemon.errorCodes.includes('authentication_required') && !daemon.types.includes('snapshot') && !daemon.types.includes('hello'), daemon);
  report.ok('a DNS-rebinding hostname is refused by the loopback web host',
    rebind.href.startsWith(rebindUrl) && rebind.body.includes('origin_refused') && rebind.hasBridge === false, rebind);
}

module.exports = { crossOriginBoundary, REBIND_HOST };
