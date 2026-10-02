#!/usr/bin/env node
'use strict';

// Proof driver for the compact-first compose bar + view-pills changes.
// Reuses the hermetic web_gate harness: seeds an
// isolated chat-stream-v2 daemon, serves the built web bundle, drives it in real
// headless Chrome over CDP, and captures before/after screenshots + numeric
// evidence for the composer states, the pill order/default, and the settings.
//
//   node test/e2e/compose_pills_proof.js --out <dir> [--label red|green] [--python <py>]
//
// Fully isolated: nothing touches the production daemon or any shared session.

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn, execFileSync } = require('child_process');

const cdp = require('./lib/cdp');
const gate = require('./web_gate');
const { main: startHost } = require('../../server');

const ROOT = path.join(__dirname, '..', '..');
const CHROME = process.env.PENTACLE_CHROME
  || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

function parseArgs(argv) {
  const args = { out: null, label: 'run', python: process.env.PENTACLE_PYTHON || 'python3', timeoutMs: 30000 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--out') args.out = argv[++i];
    else if (a === '--label') args.label = argv[++i];
    else if (a === '--python') args.python = argv[++i];
    else if (a === '--timeout') args.timeoutMs = Number(argv[++i]);
    else throw new Error(`unknown argument: ${a}`);
  }
  if (!args.out) throw new Error('--out <dir> is required');
  fs.mkdirSync(args.out, { recursive: true });
  return args;
}

async function shot(session, dir, name, evidence) {
  const file = path.join(dir, `${name}.png`);
  await session.screenshot(file);
  evidence.shots.push(name);
  return file;
}

function assertProofMetrics(metrics) {
  const failures = [];
  let checks = 0;
  const check = (label, condition, actual) => {
    checks += 1;
    if (!condition) failures.push(`${label}: ${JSON.stringify(actual)}`);
  };
  const segmentPairs = (segments) => Array.isArray(segments)
    ? segments.map(({ value, active }) => `${value}:${active}`)
    : null;
  const expectSegments = (label, segments, expected) => {
    check(label, JSON.stringify(segmentPairs(segments)) === JSON.stringify(expected), segmentPairs(segments));
  };

  check('fresh default view is Chat', metrics.defaultViewMode === 'chat', metrics.defaultViewMode);
  check('fresh Chat surface is visible', metrics.defaultViewSurface === 'chat-surface-visible', metrics.defaultViewSurface);
  check('view pill order', JSON.stringify(metrics.pillOrder) === JSON.stringify(['status', 'chat', 'terminal']), metrics.pillOrder);
  check('view pill labels', JSON.stringify(metrics.pillLabels) === JSON.stringify(['Status', 'Chat', 'Terminal']), metrics.pillLabels);

  const expectCompose = (label, state, expanded, minHeight, maxHeight, sendVisible) => {
    check(`${label} metric present`, !!state && typeof state === 'object', state);
    if (!state || typeof state !== 'object') return;
    check(`${label} expanded state`, state.expandedClass === expanded, state.expandedClass);
    check(`${label} height ${minHeight}-${maxHeight}px`, Number.isFinite(state.composeHeight) && state.composeHeight >= minHeight && state.composeHeight <= maxHeight, state.composeHeight);
    for (const control of ['plus', 'input', 'mic']) check(`${label} ${control} visible`, state[control]?.visible === true, state[control]);
    check(`${label} Send visibility`, state.send?.visible === sendVisible, state.send);
  };
  expectCompose('empty composer', metrics.compose_empty, false, 40, 60, false);
  expectCompose('short-line composer', metrics.compose_shortLine, false, 40, 60, false);
  expectCompose('wrapped composer', metrics.compose_longWrap, true, 110, 160, true);
  expectCompose('cleared composer', metrics.compose_backToCompact, false, 40, 60, false);

  const settings = metrics.settings;
  check('settings metrics present', !!settings, settings);
  if (settings) {
    check('settings list present', settings.listExists === true, settings.listExists);
    check('Chat UI toggle removed', settings.hasChatUiToggle === false, settings.hasChatUiToggle);
    check('Default view setting present', settings.hasDefaultViewSetting === true && settings.flagLabels?.includes('Default view'), settings);
    expectSegments('fresh Default view selects Chat', settings.defaultViewSegments, ['chat:true', 'terminal:false']);
  }

  const explicitChat = metrics.explicitDefaultChat;
  const explicitTerminal = metrics.explicitDefaultTerminal;
  check('explicit Chat setting persisted', explicitChat?.persistedDefaultChatView === true, explicitChat);
  expectSegments('explicit Chat setting selected', explicitChat?.defaultViewSegments, ['chat:true', 'terminal:false']);
  check('explicit Terminal setting persisted', explicitTerminal?.persistedDefaultChatView === false, explicitTerminal);
  expectSegments('explicit Terminal setting selected', explicitTerminal?.defaultViewSegments, ['chat:false', 'terminal:true']);

  const migration = metrics.migration;
  check('legacy chatUi key removed', migration?.legacyChatUiRemoved === true, migration);
  check('legacy chatUi:false migrates to Terminal default', migration?.defaultChatViewAfter === false, migration);
  check('migrated fresh attach lands on Terminal', metrics.migrationViewMode === 'terminal', metrics.migrationViewMode);
  const migrationSettings = metrics.migrationSettings;
  check('migrated settings still remove Chat UI toggle', migrationSettings?.hasChatUiToggle === false, migrationSettings);
  expectSegments('migrated Default view selects Terminal', migrationSettings?.defaultViewSegments, ['chat:false', 'terminal:true']);
  const reachability = metrics.migrationChatReachability;
  check('Chat remains reachable after migration', reachability?.chatPillPresent === true && reachability?.composerReachable === true, reachability);
  check('Terminal remains available after migration', reachability?.terminalPillPresent === true, reachability);

  if (failures.length) throw new Error(`proof metric assertion failures:\n- ${failures.join('\n- ')}`);
  return {
    asserted: true,
    checks,
    result: 'all expected values matched',
  };
}

async function run(args) {
  const scratch = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'compose-proof-')));
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'compose-proof-chrome-'));
  const runtime = {};
  const evidence = { label: args.label, at: new Date().toISOString(), shots: [], metrics: {} };
  let host = null; let chrome = null; let session = null; let daemon = null;

  const cleanup = async () => {
    try { if (runtime.backingTmux) execFileSync('tmux', ['kill-session', '-t', `=${runtime.backingTmux}`], { stdio: 'ignore' }); } catch {}
    try { if (session) session.close(); } catch {}
    try { if (chrome) chrome.kill('SIGTERM'); } catch {}
    try { if (host) await host.close(); } catch {}
    const dproc = runtime.daemonProc || (daemon && daemon.proc);
    try { await gate.stopOwnedProcess(dproc); } catch (e) { console.error('daemon cleanup:', e.message); }
    try { fs.rmSync(scratch, { recursive: true, force: true }); } catch {}
    try { fs.rmSync(userDataDir, { recursive: true, force: true }); } catch {}
  };

  try {
    daemon = await gate.startDaemon({ python: args.python, timeoutMs: args.timeoutMs }, scratch, runtime);
    const profile = gate.writeProfile(scratch, daemon.port);
    const hostPort = await gate.freePort();
    host = await startHost(['--profile', profile, '--port', String(hostPort)]);
    const url = `http://127.0.0.1:${host.port}/`;
    console.log(`[proof] daemon :${daemon.port}  host ${url}`);

    const cdpPort = await gate.freePort();
    chrome = spawn(CHROME, [
      '--headless=new', `--remote-debugging-port=${cdpPort}`, `--user-data-dir=${userDataDir}`,
      '--no-first-run', '--no-default-browser-check', '--disable-gpu', '--window-size=1600,1000', url,
    ], { stdio: ['ignore', 'pipe', 'pipe'] });
    session = await cdp.connect(cdpPort, { match: new RegExp(`127\\.0\\.0\\.1:${host.port}`) });

    const fixture = gate.FIXTURE; // local:web-gate-1
    const streamId = fixture.streamId;

    // Back the seeded chat-stream session with a REAL local tmux session of the
    // same name, so assigning it to a slot completes the terminal attach
    // (createPty) instead of early-detaching. That lets the real UI path
    // (activateSidebarRow -> assignToSlot -> attachSession) apply the configured
    // default view, which is what we want to prove.
    try {
      execFileSync('tmux', ['new-session', '-d', '-s', fixture.sessionName, 'sh', '-c', 'stty raw -echo; exec cat'], { stdio: 'ignore' });
      runtime.backingTmux = fixture.sessionName;
      console.log(`[proof] backing tmux session ${fixture.sessionName} created`);
    } catch (e) { console.error('[proof] tmux backing session failed (default-view proof may be partial):', e.message); }

    // Wait for the sidebar + daemon connection + the seeded row.
    await session.waitFor('!!document.getElementById("session-list")', { timeoutMs: args.timeoutMs, label: 'sidebar' });
    await session.waitFor('window.cc.getChatStreamState().then(s => s && s.connected === true)', { timeoutMs: args.timeoutMs, label: 'connected' });
    await session.waitFor(
      `window.cc.getChatStreamState().then(s => (s.sessions||[]).some(x => x.stream_id === ${JSON.stringify(streamId)}))`,
      { timeoutMs: args.timeoutMs, label: 'seeded session in inventory' });
    await session.waitFor(
      `!!document.querySelector('#session-list .session-item[data-stream-id="${streamId}"]')`,
      { timeoutMs: args.timeoutMs, label: 'seeded sidebar row' });

    // Assign the seeded session to a slot (the real operator flow).
    await session.eval(`document.querySelector('#session-list .session-item[data-stream-id="${streamId}"]').click()`);
    // Find which slot it landed in.
    const slot = await session.waitFor(`(() => {
      for (let i = 0; i < 4; i++) {
        const cell = document.getElementById('cell-' + i);
        if (cell && cell.classList.contains('occupied')) return i;
      }
      return false;
    })()`, { timeoutMs: args.timeoutMs, label: 'slot occupied' });
    console.log(`[proof] session assigned to slot ${slot}`);

    // ── DEFAULT-VIEW proof: what mode did a fresh assign land on? ─────────────
    // attachSession is async (awaits an rAF + createPty) and updateSlotViewMode
    // runs at its end, so let the attach fully settle before reading the landed
    // view. An early read can catch a transient pre-attach 'terminal' state.
    await session.waitFor(`!!document.querySelector('#cell-${slot} .cell-view-toggle.active')`, { timeoutMs: 8000, label: 'a pill is active' });
    await cdp.sleep(2500);
    evidence.metrics.defaultViewMode = await session.eval(
      `(document.querySelector('#cell-${slot} .cell-view-toggle.active')?.dataset.mode) || null`);
    // Also record which surface is actually displayed, as corroboration.
    evidence.metrics.defaultViewSurface = await session.eval(`(() => {
      const chatMount = document.querySelector('#cell-${slot} .slot-chat-layer');
      const shown = chatMount && getComputedStyle(chatMount).display !== 'none';
      return shown ? 'chat-surface-visible' : 'chat-surface-hidden';
    })()`);
    await shot(session, args.out, 'default-view-landing', evidence);

    // ── PILL ORDER proof ─────────────────────────────────────────────────────
    await session.waitFor(`!!document.querySelector('#cell-${slot} .cell-view-toggle-group')`, { timeoutMs: args.timeoutMs, label: 'pill group' });
    evidence.metrics.pillOrder = await session.eval(
      `Array.from(document.querySelectorAll('#cell-${slot} .cell-view-toggle')).map(b => b.dataset.mode)`);
    evidence.metrics.pillLabels = await session.eval(
      `Array.from(document.querySelectorAll('#cell-${slot} .cell-view-toggle')).map(b => b.textContent.trim())`);
    await shot(session, args.out, 'pills-order', evidence);

    // ── COMPOSE BAR proof ────────────────────────────────────────────────────
    // Switch to chat view so the composer renders.
    await session.waitFor(`!!document.querySelector('#cell-${slot} [data-mode="chat"]')`, { timeoutMs: args.timeoutMs, label: 'chat pill' });
    await session.eval(`document.querySelector('#cell-${slot} [data-mode="chat"]').click()`);
    await session.waitFor(`!!document.querySelector('#cell-${slot} .slot-chat-compose-input')`, { timeoutMs: args.timeoutMs, label: 'composer' });

    const inputSel = `#cell-${slot} .slot-chat-compose-input`;
    const composeSel = `#cell-${slot} .slot-chat-compose`;
    const measure = () => session.eval(`(() => {
      const c = document.querySelector(${JSON.stringify(composeSel)});
      const i = document.querySelector(${JSON.stringify(inputSel)});
      const mic = document.querySelector('#cell-${slot} .slot-chat-compose-mic');
      const send = document.querySelector('#cell-${slot} .slot-chat-compose-send');
      const plus = document.querySelector('#cell-${slot} .slot-chat-attach');
      const vis = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return { w: Math.round(r.width), h: Math.round(r.height), display: s.display, visible: r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' }; };
      return {
        composeHeight: c ? Math.round(c.getBoundingClientRect().height) : null,
        expandedClass: c ? c.classList.contains('is-expanded') : null,
        plus: vis(plus), input: vis(i), mic: vis(mic), send: vis(send),
      };
    })()`);

    const typeInput = (text) => session.eval(`(() => {
      const el = document.querySelector(${JSON.stringify(inputSel)});
      el.focus(); el.value = ${JSON.stringify(text)};
      el.dispatchEvent(new Event('input', { bubbles: true }));
      return true;
    })()`);

    // 1) Empty → compact (the "no blank space" default the operator complained about).
    await typeInput('');
    await cdp.sleep(250);
    evidence.metrics.compose_empty = await measure();
    await shot(session, args.out, 'compose-1-compact-empty', evidence);

    // 2) Short one-line draft → still compact.
    await typeInput('hi there');
    await cdp.sleep(250);
    evidence.metrics.compose_shortLine = await measure();
    await shot(session, args.out, 'compose-2-compact-typed', evidence);

    // 3) Long draft that no longer fits one line → expanded (current stacked layout).
    await typeInput('This is a deliberately long first message that will not fit on a single line inside the compose bar and therefore must wrap onto multiple lines, which should trigger the expanded stacked composer layout with the bottom action row.');
    await cdp.sleep(300);
    evidence.metrics.compose_longWrap = await measure();
    await shot(session, args.out, 'compose-3-expanded', evidence);

    // 4) Clear again → shrinks back to compact.
    await typeInput('');
    await cdp.sleep(300);
    evidence.metrics.compose_backToCompact = await measure();
    await shot(session, args.out, 'compose-4-back-to-compact', evidence);

    // ── SETTINGS proof: enable/disable-chat toggle gone; default-view control present.
    await session.eval(`document.getElementById('settings-btn')?.click()`);
    await cdp.sleep(500);
    evidence.metrics.settings = await session.eval(`(() => {
      const list = document.getElementById('settings-list');
      const flagLabels = Array.from(document.querySelectorAll('#settings-list .settings-row-label')).map(x => x.textContent.replace('Reload to apply','').trim());
      const hasChatUiToggle = flagLabels.some(l => /chat ui/i.test(l));
      const row = document.getElementById('settings-default-view-row');
      const hasDefaultViewSetting = !!row;
      const defaultViewSegments = Array.from(row ? row.querySelectorAll('.settings-segment-btn') : []).map(b => ({ value: b.dataset.value, active: b.classList.contains('active') }));
      return { flagLabels, hasChatUiToggle, hasDefaultViewSetting, defaultViewSegments, listExists: !!list };
    })()`);
    await shot(session, args.out, 'settings-panel', evidence);

    // Exercise both explicit values through the real segmented control and
    // require the persisted value plus selected segment to agree.
    await session.eval(`document.querySelector('#settings-default-view-row [data-value="chat"]')?.click()`);
    await cdp.sleep(150);
    evidence.metrics.explicitDefaultChat = await session.eval(`(() => {
      let rec = {}; try { rec = JSON.parse(localStorage.getItem('pentacle.settings.v1') || '{}'); } catch {}
      const row = document.getElementById('settings-default-view-row');
      return { persistedDefaultChatView: rec.features?.defaultChatView, defaultViewSegments: Array.from(row ? row.querySelectorAll('.settings-segment-btn') : []).map(b => ({ value: b.dataset.value, active: b.classList.contains('active') })) };
    })()`);
    await session.eval(`document.querySelector('#settings-default-view-row [data-value="terminal"]')?.click()`);
    await cdp.sleep(150);
    evidence.metrics.explicitDefaultTerminal = await session.eval(`(() => {
      let rec = {}; try { rec = JSON.parse(localStorage.getItem('pentacle.settings.v1') || '{}'); } catch {}
      const row = document.getElementById('settings-default-view-row');
      return { persistedDefaultChatView: rec.features?.defaultChatView, defaultViewSegments: Array.from(row ? row.querySelectorAll('.settings-segment-btn') : []).map(b => ({ value: b.dataset.value, active: b.classList.contains('active') })) };
    })()`);

    // ── MIGRATION proof: a legacy `chatUi:false` (chat disabled) must NOT hide
    // chat; it migrates to a Terminal-first default while chat stays available.
    await session.eval(`document.getElementById('settings-close')?.click()`);
    await session.eval(`localStorage.setItem('pentacle.settings.v1', JSON.stringify({ features: { chatUi: false } }))`);
    await session.send('Page.reload', {});
    await session.waitFor('!!document.getElementById("session-list")', { timeoutMs: args.timeoutMs, label: 'reloaded' });
    await cdp.sleep(1200);
    evidence.metrics.migration = await session.eval(`(() => {
      let rec = {}; try { rec = JSON.parse(localStorage.getItem('pentacle.settings.v1') || '{}'); } catch {}
      const f = rec.features || {};
      return {
        legacyChatUiRemoved: !('chatUi' in f),
        defaultChatViewAfter: f.defaultChatView,
      };
    })()`);
    // Chat must still be reachable: open settings and confirm the chat-enable
    // toggle is gone and the Default view chooser reflects the migrated Terminal.
    await session.eval(`document.getElementById('settings-btn')?.click()`);
    await cdp.sleep(400);
    evidence.metrics.migrationSettings = await session.eval(`(() => {
      const flagLabels = Array.from(document.querySelectorAll('#settings-list .settings-row-label')).map(x => x.textContent.replace('Reload to apply','').trim());
      const row = document.getElementById('settings-default-view-row');
      const seg = Array.from(row ? row.querySelectorAll('.settings-segment-btn') : []).map(b => ({ value: b.dataset.value, active: b.classList.contains('active') }));
      return { hasChatUiToggle: flagLabels.some(l => /chat ui/i.test(l)), defaultViewSegments: seg };
    })()`);
    await shot(session, args.out, 'migration-settings', evidence);

    // Re-open the seeded session after the migration reload. This exercises the
    // Terminal-first default while proving the Chat pill and composer still work.
    await session.eval(`document.getElementById('settings-close')?.click()`);
    await session.waitFor(`!!document.querySelector('#session-list .session-item[data-stream-id="${streamId}"]')`, { timeoutMs: args.timeoutMs, label: 'session row after migration' });
    await session.eval(`document.querySelector('#session-list .session-item[data-stream-id="${streamId}"]').click()`);
    const migrationSlot = await session.waitFor(`(() => {
      for (let i = 0; i < 4; i++) {
        const cell = document.getElementById('cell-' + i);
        if (cell && cell.classList.contains('occupied')) return i;
      }
      return false;
    })()`, { timeoutMs: args.timeoutMs, label: 'session assigned after migration' });
    await session.waitFor(`!!document.querySelector('#cell-${migrationSlot} .cell-view-toggle.active')`, { timeoutMs: args.timeoutMs, label: 'migrated default view selected' });
    await cdp.sleep(500);
    evidence.metrics.migrationViewMode = await session.eval(`document.querySelector('#cell-${migrationSlot} .cell-view-toggle.active')?.dataset.mode || null`);
    const migratedPills = await session.eval(`(() => ({
      chatPillPresent: !!document.querySelector('#cell-${migrationSlot} [data-mode="chat"]'),
      terminalPillPresent: !!document.querySelector('#cell-${migrationSlot} [data-mode="terminal"]'),
    }))()`);
    await session.eval(`document.querySelector('#cell-${migrationSlot} [data-mode="chat"]')?.click()`);
    await session.waitFor(`!!document.querySelector('#cell-${migrationSlot} .slot-chat-compose-input')`, { timeoutMs: args.timeoutMs, label: 'Chat composer reachable after migration' });
    evidence.metrics.migrationChatReachability = { ...migratedPills, composerReachable: true };
    await shot(session, args.out, 'migration-chat-reachable', evidence);

    evidence.assertions = assertProofMetrics(evidence.metrics);

    fs.writeFileSync(path.join(args.out, 'evidence.json'), JSON.stringify(evidence, null, 2));
    console.log('[proof] evidence:\n' + JSON.stringify(evidence.metrics, null, 2));
    console.log(`[proof] OK — ${evidence.shots.length} screenshots in ${args.out}`);
    return 0;
  } catch (e) {
    console.error(`[proof] FAIL — ${e && e.stack ? e.stack : e}`);
    try { if (session) await shot(session, args.out, 'FAIL-state', evidence); } catch {}
    fs.writeFileSync(path.join(args.out, 'evidence.json'), JSON.stringify({ ...evidence, error: String(e && e.message || e) }, null, 2));
    return 1;
  } finally {
    await cleanup();
  }
}

if (require.main === module) {
  run(parseArgs(process.argv.slice(2))).then((c) => process.exit(c));
}
module.exports = { run, parseArgs };
