#!/usr/bin/env node
'use strict';

// ── Pentacle web-mode smoke (lane 1 of spec_pentacle__web_mode_2026_09) ──────
//
//   node test/e2e/web_smoke.js --profile <config.js> [--port 0] [--keep]
//
// Boots the headless web host, drives the served page in real headless Chrome
// over CDP, and proves the whole seam end to end: window.cc over the websocket,
// a live tmux attach with typing / resize / kill, the sidebar rendered from the
// daemon inventory, and a chat transcript for a live session.
//
// Terminal work only ever touches a local `ptest-web-*` tmux session; nothing is
// spawned, closed or sent on a shared daemon. Lane 3 wires this into the walk
// harness; today it is run by hand and its log is the lane's evidence.

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn, execFileSync } = require('child_process');

const cdp = require('./lib/cdp');
const { main: startHost } = require('../../server');

const ROOT = path.join(__dirname, '..', '..');
const CHROME_CANDIDATES = ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'];

function parseArgs(argv) {
  const args = { profile: null, port: 0, cdpPort: 9333, keep: false, timeoutMs: 30000 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--profile') args.profile = argv[++i];
    else if (a === '--port') args.port = Number(argv[++i]);
    else if (a === '--cdp-port') args.cdpPort = Number(argv[++i]);
    else if (a === '--keep') args.keep = true;
    else throw new Error(`unknown argument: ${a}`);
  }
  if (!args.profile) throw new Error('--profile <config.js> is required');
  return args;
}

function resolveChrome() {
  for (const bin of CHROME_CANDIDATES) {
    try { execFileSync('which', [bin], { stdio: 'ignore' }); return bin; } catch { /* next */ }
  }
  throw new Error(`no Chrome/Chromium on PATH (tried ${CHROME_CANDIDATES.join(', ')})`);
}

function tmux(args, opts = {}) {
  return execFileSync('tmux', args, { encoding: 'utf8', ...opts }).trim();
}

class Report {
  constructor(dir) {
    this.dir = dir;
    this.steps = [];
    fs.mkdirSync(dir, { recursive: true });
  }
  ok(name, passed, detail = null) {
    this.steps.push({ name, ok: !!passed, detail });
    console.log(`   ${passed ? '✓' : '✗'} ${name}${passed ? '' : `  ${JSON.stringify(detail)}`}`);
    if (!passed) throw new Error(`assertion failed: ${name}`);
  }
  note(text) { console.log(`   · ${text}`); }
  write(extra = {}) {
    const verdict = {
      scenario: 'web_smoke',
      at: new Date().toISOString(),
      status: this.steps.every((s) => s.ok) ? 'PASS' : 'FAIL',
      steps: this.steps,
      ...extra,
    };
    fs.writeFileSync(path.join(this.dir, 'verdict.json'), JSON.stringify(verdict, null, 2));
    return verdict;
  }
}

async function run(args) {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const report = new Report(path.join(__dirname, 'runs', stamp, 'web_smoke'));
  const sessionName = `ptest-web-${process.pid}-${Date.now().toString(36)}`;
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-web-smoke-'));
  let host = null;
  let chrome = null;
  let session = null;

  const cleanup = async () => {
    try { if (session) session.close(); } catch {}
    try { if (chrome && !args.keep) chrome.kill('SIGTERM'); } catch {}
    try { tmux(['kill-session', '-t', `=${sessionName}`], { stdio: 'ignore' }); } catch {}
    try { if (host) await host.close(); } catch {}
  };

  try {
    host = await startHost(['--profile', args.profile, '--port', String(args.port)]);
    const url = `http://127.0.0.1:${host.port}/`;
    report.note(`host ${url}  profile ${args.profile}`);

    const chromeBin = resolveChrome();
    chrome = spawn(chromeBin, [
      '--headless=new',
      `--remote-debugging-port=${args.cdpPort}`,
      `--user-data-dir=${userDataDir}`,
      '--no-first-run', '--no-default-browser-check', '--disable-gpu',
      '--window-size=1600,1000',
      url,
    ], { stdio: ['ignore', 'pipe', 'pipe'] });
    const chromeLog = [];
    chrome.stdout.on('data', (d) => chromeLog.push(String(d)));
    chrome.stderr.on('data', (d) => chromeLog.push(String(d)));

    session = await cdp.connect(args.cdpPort, { match: new RegExp(`127\\.0\\.0\\.1:${host.port}|Terminal Dashboard`) });

    // ── the page and the transport ──────────────────────────────────────────
    await session.waitFor('!!(window.cc && window.HOST)', { timeoutMs: args.timeoutMs, label: 'window.cc installed' });
    report.ok('window.cc and window.HOST install before the renderer runs', true);

    const injected = await session.eval('({ hostname: window.HOST.hostname, platform: window.HOST.platform, hasConfig: !!window.__PENTACLE_CONFIG__ })');
    report.ok('the host injected the computed config', injected.hasConfig && !!injected.hostname, injected);

    const viaCc = await session.eval('window.cc.getConfig().then(c => ({ appName: c.appName, hostIds: c.hostIds, localHostId: c.localHostId }))', { awaitPromise: true });
    const viaHttp = await fetch(`${url}api/config`).then((r) => r.json());
    report.ok('getConfig() over the websocket matches GET /api/config',
      viaCc.appName === viaHttp.appName && JSON.stringify(viaCc.hostIds) === JSON.stringify(viaHttp.hostIds), { viaCc, viaHttp });

    const refused = await session.eval(
      "window.cc.openMeeting(), window.cc.perfState().then(() => 'ok', e => 'err:' + e.code)", { awaitPromise: true });
    report.note(`native-channel probe: ${refused}`);

    // ── sidebar from the live daemon ────────────────────────────────────────
    await session.waitFor('!!document.getElementById("session-list")', { timeoutMs: args.timeoutMs, label: 'sidebar mounted' });
    const streamState = await waitForValue(session,
      'window.cc.getChatStreamState()',
      (s) => s && s.connected === true,
      { timeoutMs: args.timeoutMs, label: 'daemon connection over the websocket' },
    );
    report.ok('the daemon connection is established over the websocket', streamState.connected === true,
      { sessions: (streamState.sessions || []).length });

    // A daemon with no sessions has nothing to render; say so instead of
    // asserting an empty sidebar means success.
    const liveSessions = (streamState.sessions || []).filter((x) => x.stream_id);
    let rows = [];
    if (liveSessions.length === 0) {
      report.note('daemon reports no sessions — skipping the sidebar and transcript checks');
    } else {
      rows = await waitForValue(session,
        '[...document.querySelectorAll("#session-list .session-item")].map(el => ({ name: el.dataset.name, host: el.dataset.host, streamId: el.dataset.streamId }))',
        (r) => Array.isArray(r) && r.length > 0,
        { timeoutMs: args.timeoutMs, label: 'sidebar rows rendered' },
      );
      report.ok('the sidebar renders rows from the live daemon', rows.length > 0, { rows: rows.length, first: rows[0] });
    }

    // ── local tmux slot: attach, type, resize, kill ─────────────────────────
    tmux(['new-session', '-d', '-s', sessionName, 'sh', '-c', 'stty raw -echo; exec cat']);
    report.ok('local ptest tmux session created', tmux(['has-session', '-t', `=${sessionName}`]) === '');

    const known = await session.eval(`window.cc.checkSession(${JSON.stringify(sessionName)}, 'local')`, { awaitPromise: true });
    report.ok('checkSession sees the local session over the websocket', known === true, { known });

    await session.eval(`(() => {
      window.__smoke = { data: '', exits: [] };
      window.cc.onPtyData((slot, data) => { if (slot === 0) window.__smoke.data += data; });
      window.cc.onPtyExit((slot, code) => window.__smoke.exits.push([slot, code]));
      return true;
    })()`);
    const paneId = await session.eval(`window.cc.createPty(0, ${JSON.stringify(sessionName)}, 'local', 80, 24)`, { awaitPromise: true });
    report.ok('createPty attaches the slot and returns a pane id', /^%\d+$/.test(String(paneId)), { paneId });

    await session.eval("window.cc.writePty(0, 'echo WEBOK\\r'), true");
    const buffer = await waitForValue(session, 'window.__smoke.data', (d) => String(d).includes('WEBOK'),
      { timeoutMs: args.timeoutMs, label: 'WEBOK echoed back to the browser' });
    report.ok('typing round-trips browser → host → tmux → browser', String(buffer).includes('WEBOK'));
    fs.writeFileSync(path.join(report.dir, 'pty-buffer.txt'), String(buffer));

    const widthBefore = Number(tmux(['display', '-p', '-t', String(paneId), '#{pane_width}']));
    await session.eval('window.cc.resizePty(0, 120, 40), true');
    const widthAfter = await pollUntil(
      () => Number(tmux(['display', '-p', '-t', String(paneId), '#{pane_width}'])),
      (w) => w === 120, args.timeoutMs, 'tmux pane resized',
    );
    report.ok('resizePty resizes the real tmux pane', widthAfter === 120 && widthBefore !== widthAfter, { widthBefore, widthAfter });

    // The attach must go away and the tmux SESSION must survive: tmux reports
    // one attached client while the slot is live and none once it is released.
    const clientsBefore = Number(tmux(['list-clients', '-t', `=${sessionName}`, '-F', 'x']).split('\n').filter(Boolean).length);
    await session.eval('window.cc.killPty(0)', { awaitPromise: true });
    const clientsAfter = await pollUntil(
      () => tmux(['list-clients', '-t', `=${sessionName}`, '-F', 'x']).split('\n').filter(Boolean).length,
      (n) => n === 0, args.timeoutMs, 'the tmux client to detach',
    );
    const sessionAlive = (() => {
      try { tmux(['has-session', '-t', `=${sessionName}`]); return true; } catch { return false; }
    })();
    report.ok('killPty releases the attach without killing the tmux session',
      clientsBefore > 0 && clientsAfter === 0 && sessionAlive, { clientsBefore, clientsAfter, sessionAlive });

    // ── chat transcript for a live session ──────────────────────────────────
    // The daemon answers requestStreamEvents with a receipt ({ok, count, …}) and
    // delivers the events themselves as chat-stream:frame pushes, so assert both
    // halves: a non-zero count in the reply and frames arriving on the socket.
    const candidates = liveSessions.slice(0, 12);
    let live = null;
    let receipt = null;
    for (const candidate of candidates) {
      receipt = await session.eval(
        `window.cc.requestStreamEvents({ streamId: ${JSON.stringify(candidate.stream_id)}, limit: 20 })`,
        { awaitPromise: true },
      );
      if (receipt && receipt.ok && Number(receipt.count) > 0) { live = candidate; break; }
    }
    if (candidates.length === 0) {
      report.note('no live session to load a transcript for');
    } else {
      report.ok('a live session\'s transcript events load over the websocket', !!live,
        { tried: candidates.length, streamId: live && live.stream_id, count: receipt && receipt.count });
    }

    if (live) {
    // Render the transcript through the renderer's production path
    // (PentacleChatView.renderStreamTranscript is the only chat render path
    // since the Phase 7 cutover) against the live events the store received
    // over this websocket. Deliberately NOT via a sidebar click: attaching a
    // slot would join another machine's live tmux pane and resize it.
    const painted = await waitForValue(session, `(() => {
      const container = document.getElementById('web-smoke-transcript') || (() => {
        const el = document.createElement('div');
        el.id = 'web-smoke-transcript';
        document.body.appendChild(el);
        return el;
      })();
      const detail = window.PentacleChatView.renderStreamTranscript(${JSON.stringify(live.stream_id)}, container);
      return { items: (detail && detail.transcriptItems || []).length, text: (container.innerText || '').trim() };
    })()`, (v) => v && v.items > 0 && v.text.length > 0,
      { timeoutMs: args.timeoutMs, label: 'chat transcript painted' });
    report.ok('the chat transcript paints for a live session',
      painted.items > 0 && painted.text.length > 0,
      { streamId: live.stream_id, items: painted.items, chars: painted.text.length });
    fs.writeFileSync(path.join(report.dir, 'chat-transcript.txt'), painted.text.slice(0, 20000));
    }

    fs.writeFileSync(path.join(report.dir, 'chrome.log'), chromeLog.join(''));
    fs.writeFileSync(path.join(report.dir, 'console.log'), (session.consoleLines || []).join('\n'));
    const verdict = report.write({ profile: args.profile, url, tmuxSession: sessionName });
    console.log(`\n◀ web_smoke: ${verdict.status}\n  artifacts: ${report.dir}`);
    return verdict.status === 'PASS' ? 0 : 1;
  } catch (e) {
    console.error(`\n◀ web_smoke: FAIL — ${e && e.message}`);
    try { report.write({ error: String(e && e.stack ? e.stack : e) }); } catch {}
    console.error(`  artifacts: ${report.dir}`);
    return 1;
  } finally {
    await cleanup();
  }
}

// cdp.js's waitFor only distinguishes truthy from falsy; these waits need a
// predicate over the evaluated value (a non-empty list, a string containing a
// token), so poll eval directly.
async function waitForValue(session, expression, predicate, { timeoutMs = 30000, intervalMs = 300, label = expression } = {}) {
  const deadline = Date.now() + timeoutMs;
  let last;
  for (;;) {
    try { last = await session.eval(`(async () => (${expression}))()`); } catch (e) { last = undefined; }
    if (predicate(last)) return last;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${label} (last: ${JSON.stringify(last)?.slice(0, 300)})`);
    await cdp.sleep(intervalMs);
  }
}

async function pollUntil(read, predicate, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  let last;
  for (;;) {
    last = read();
    if (predicate(last)) return last;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${label} (last: ${JSON.stringify(last)})`);
    await cdp.sleep(200);
  }
}

if (require.main === module) {
  run(parseArgs(process.argv.slice(2))).then((code) => process.exit(code));
}

module.exports = { run, parseArgs };
