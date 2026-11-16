#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const { headlessChromeArgs } = require('./lib/chrome_launch.cjs');
const L = require('../../lib/design_library.cjs');
const P = require('../../lib/design_prepare.cjs');
const R = require('../../lib/design_record.cjs');
const fixture = require('../fixtures/design_library.cjs');

async function run(argv) {
  const options = require('../../scripts/design-library.cjs').parse(['probe', ...argv]).options;
  if (!options.out) throw new Error('--out is required');
  const out = path.resolve(options.out); fs.mkdirSync(out, { recursive: true });
  let dir = options.dir;
  if (!dir) {
    const drop = options.drop || path.join(out, 'fixture-drop');
    if (!options.drop) fixture.drop(drop);
    const imported = L.importDrop({ drop, staging: path.join(out, 'staging'), dependencies: options.dependencies ? L.readJson(options.dependencies) : {} });
    dir = imported.dir; await P.prepareRevision(dir);
  }
  const m = L.readJson(path.join(dir, 'manifest.json'));
  const browser = await chromium.launch({ ...R.launchOptions(), args: headlessChromeArgs([], { playwright: true }) }), receipts = [];
  try {
    const selected = m.views.filter(v => v.prepared && (options.all === 'true' || m.project_id === 'example' || ['mobile/Pentacle Prototype.html', 'web/Pentacle Desktop Chat v3.html', 'web/Pentacle Report.html', 'archive/Composer Explorations.html'].includes(v.entry)));
    for (const view of selected) {
      const session = await R.openPrepared(browser, { html: fs.readFileSync(path.join(dir, view.prepared), 'utf8'), size: L.viewport(view.viewport) });
      try {
        await R.ready(session, view.ready_selector || 'body');
        const before = await session.frame.locator('body').innerText(); assert.ok(before.trim().length || await session.frame.locator('img,svg,canvas').count(), 'view must render visible content');
        const sandbox = await session.frame.locator('body').evaluate(() => {
          let parentBlocked = false, cookiesBlocked = false;
          try { void parent.document.body; } catch { parentBlocked = true; }
          try { void document.cookie; } catch { cookiesBlocked = true; }
          localStorage.clear(); localStorage.setItem(2, 3); localStorage.setItem('other', 'value');
          const storage = [localStorage.getItem('2'), localStorage.key(0), localStorage.length];
          localStorage.removeItem(2);
          const removed = localStorage.getItem(2) === null; localStorage.clear();
          return { origin: window.origin, parentBlocked, cookiesBlocked, storage, removed, cleared: localStorage.length === 0, claude: typeof window.claude, ipc: typeof window.cc, animations: document.getAnimations().length };
        });
        assert.equal(sandbox.origin, 'null'); assert.equal(sandbox.parentBlocked, true); assert.equal(sandbox.cookiesBlocked, true); assert.deepEqual(sandbox.storage, ['3', '2', 2]); assert.ok(sandbox.removed && sandbox.cleared); assert.equal(sandbox.claude, 'undefined'); assert.equal(sandbox.ipc, 'undefined');
        if (view.entry === 'mobile/Pentacle Prototype.html') {
          await session.frame.getByRole('button', { name: 'Session', exact: true }).click();
          assert.notEqual(await session.frame.locator('body').innerText(), before);
          await session.frame.getByRole('button', { name: 'Working', exact: true }).click();
          const animations = await session.frame.locator('body').evaluate(() => document.getAnimations().length); assert.ok(animations > 0);
        }
        await session.frame.locator('body').evaluate(() => localStorage.setItem('reload-proof', 'temporary'));
        await session.page.locator('iframe').evaluate((frame, html) => { frame.srcdoc = html; }, fs.readFileSync(path.join(dir, view.prepared), 'utf8'));
        await R.ready(session, view.ready_selector || 'body');
        assert.equal(await session.frame.locator('body').evaluate(() => localStorage.getItem('reload-proof')), null);
        R.assertClean(session); await session.page.screenshot({ path: path.join(out, view.id + '.png') });
        receipts.push({ item: view.id, status: 'pass', sandbox, requests: [], errors: [] });
      } finally { await session.context.close(); }
    }
    // Source policy RED: this future Claude call is never silently admitted.
    const red = path.join(out, 'unmocked'); fs.mkdirSync(red, { recursive: true }); fs.writeFileSync(path.join(red, 'index.html'), '<script>window.claude.complete({prompt:"fixture"})</script>');
    await assert.rejects(P.prepareDocument({ sourceRoot: red, entry: 'index.html' }), /unsupported/);
    // A deliberately hostile document tests the runtime boundary separately
    // from source admission. It is never put into the design library.
    const hostile = `<!doctype html><meta http-equiv="Content-Security-Policy" content="${P.CSP}"><script>${P.STORAGE}</script><body><h1>Hostile fixture</h1><form action="https://example.invalid/fixture"><input name="x"></form><script>window.attempts={};try{parent.document.body.textContent='changed'}catch(e){attempts.parentBlocked=true}try{top.location.href='https://example.invalid/fixture'}catch(e){attempts.topBlocked=true}attempts.popupBlocked=window.open('about:blank')===null;fetch('https://example.invalid/fixture').catch(()=>{attempts.fetchBlocked=true});document.forms[0].submit();parent.postMessage({type:'navigate',url:'https://example.invalid'},'*');</script>`;
    const session = await R.openPrepared(browser, { html: hostile, size: { width: 800, height: 600 } });
    try {
      await session.page.waitForTimeout(250);
      const attempts = await session.frame.locator('body').evaluate(() => window.attempts);
      assert.ok(attempts.parentBlocked && attempts.topBlocked && attempts.popupBlocked && attempts.fetchBlocked);
      assert.equal(session.requests.length, 0); assert.equal(session.page.url(), 'about:blank'); assert.equal(await session.page.locator('iframe').count(), 1);
      receipts.push({ item: 'hostile-runtime', status: 'pass', attempts, requests: [] });
    } finally { await session.context.close(); }
    L.atomicJson(path.join(out, 'probe-receipt.json'), { browser: browser.version(), dir, receipts });
    return { passed: true, views: receipts.length - 1, out };
  } finally { await browser.close(); }
}
if (require.main === module) run(process.argv.slice(2)).then(result => console.log(JSON.stringify(result))).catch(error => { console.error(error.stack); process.exitCode = 1; });
module.exports = { run };
