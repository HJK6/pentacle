'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { fork } = require('node:child_process');
const { once } = require('node:events');
const { rollback, createLibraryReader } = require('../../../lib/design_library.cjs');
const { stageFixture, project, prototype, phone, legacy, route, label } = require('./next_design_fixture.cjs');
const { PNG } = require('../../fixtures/design_library.cjs');
const { createHash } = require('node:crypto');

const design = '/next/design';
const embed = '/next/embed/design';
const stage = '.design-stage';
const cameraExpression = 'document.querySelector(".design-world").style.transform';
const nextButton = 'button[aria-label="Next screen"]';
const screensButton = 'button[aria-label="Screens"]';
const backButton = '.design-bar [aria-label="Back"]';
const fixtureIdentity = {
  'Tailscale-User-Login': 'tester@example.test',
  'X-Forwarded-Proto': 'https',
  'X-Forwarded-For': 'fd7a:115c:a1e0::1234:5678',
};

function check(ctx, description, expression) {
  return ctx.session.eval(expression).then(result => ctx.report.ok(description, Boolean(result)));
}

function wait(ctx, expression, label, timeoutMs = ctx.timeoutMs) {
  return ctx.session.waitFor(expression, { timeoutMs, label });
}

function atPath(ctx, pathname) {
  return wait(ctx, 'location.pathname === ' + JSON.stringify(pathname), pathname);
}

async function open(ctx, address, width = 1440, origin = new URL(ctx.url).origin) {
  await ctx.session.send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false });
  await ctx.session.send('Page.navigate', { url: origin + address });
  await wait(ctx, 'document.readyState === "complete" && !!document.querySelector(".design-viewer")', 'Design entry');
}

async function homeReady(ctx) {
  await wait(ctx, '!!document.querySelector(".design-home:not([hidden]) .design-tile")', 'Design tiles');
}

async function boardReady(ctx) {
  await wait(ctx, '!!document.querySelector(".design-board:not([hidden]) .design-screen")', 'Design board');
}

const click = (ctx, selector) => ctx.session.click(selector);

async function key(ctx, value) {
  await ctx.session.send('Input.dispatchKeyEvent', { type: 'keyDown', key: value });
  await ctx.session.send('Input.dispatchKeyEvent', { type: 'keyUp', key: value });
}

async function point(ctx, selector, x = 0.5, y = 0.5) {
  return ctx.session.eval(`(() => {
    const rect = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
    return { x: rect.left + rect.width * ${x}, y: rect.top + rect.height * ${y} };
  })()`);
}

async function mouse(ctx, position, type, extra) {
  await ctx.session.send('Input.dispatchMouseEvent', { ...position, type, ...extra });
}

async function hit(ctx, position) {
  await mouse(ctx, position, 'mousePressed', { button: 'left', clickCount: 1 });
  await mouse(ctx, position, 'mouseReleased', { button: 'left', clickCount: 1 });
}

async function drag(ctx, position, offsetX, offsetY) {
  await mouse(ctx, position, 'mousePressed', { button: 'left', buttons: 1, clickCount: 1 });
  const end = { x: position.x + offsetX, y: position.y + offsetY };
  await mouse(ctx, end, 'mouseMoved', { button: 'left', buttons: 1 });
  await mouse(ctx, end, 'mouseReleased', { button: 'left', clickCount: 1 });
}

async function movedCamera(ctx, before) {
  await wait(ctx, cameraExpression + ' !== ' + JSON.stringify(before), 'camera moves');
}

async function layoutHome(ctx) {
  await open(ctx, design);
  await homeReady(ctx);
  const { session } = ctx;
  await check(ctx, 'Home shows a named project, a prototype, count and an unread dot',
    'document.querySelector(".design-project-title").textContent === "Delivery" && !!document.querySelector(".design-tile-prototype") && /2 screens/.test(document.querySelector(".design-tile-meta").textContent) && !!document.querySelector(".design-unread")');
  await click(ctx, '.design-card [aria-label="Full screen"]');
  await wait(ctx, '!document.querySelector(".design-search").hidden', 'wide Design search');
  await session.type('.design-search', 'Phone');
  await check(ctx, 'search filters by design', 'document.querySelectorAll(".design-tile").length === 1 && document.querySelector(".design-tile-name").textContent === "Phone orders"');
  await session.type('.design-search', '');
  await click(ctx, '.design-tabs button[title="Archive"]');
  await check(ctx, 'tab filters by project', 'document.querySelectorAll(".design-tile").length === 1 && document.querySelector(".design-tile-name").textContent === "Old orders"');
  await click(ctx, '.design-tabs button[title="All"]');
  await click(ctx, '.design-tile[title="' + label + '"]');
  await boardReady(ctx);
  await click(ctx, backButton);
  await homeReady(ctx);
  await check(ctx, 'opening the board clears its dot', `!document.querySelector('.design-tile[title="Delivery board"] .design-unread')`);
  await session.screenshot(path.join(ctx.report.dir, 'next-design-home.png'));
}

async function boardJourney(ctx) {
  await open(ctx, design + route);
  await boardReady(ctx);
  const { session } = ctx;
  let camera = await session.eval(cameraExpression);
  await mouse(ctx, await point(ctx, stage, 0.1, 0.4), 'mouseWheel', { deltaX: 0, deltaY: -160 });
  await movedCamera(ctx, camera);
  camera = await session.eval(cameraExpression);
  await drag(ctx, await point(ctx, stage, 0.05, 0.65), 35, 35);
  await movedCamera(ctx, camera);
  await key(ctx, '0');
  await click(ctx, '[data-view="home"] .design-screen-hit');
  await atPath(ctx, design + route + '/home');
  await key(ctx, 'ArrowRight');
  await atPath(ctx, design + route + '/' + prototype);
  await click(ctx, '[aria-label="Previous screen"]');
  await atPath(ctx, design + route + '/home');
  await click(ctx, screensButton);
  await check(ctx, 'screen outline groups the screens by section', 'document.querySelector(".design-outline:not([hidden]) h3").textContent === "Order flow"');
  await key(ctx, 'Escape');
  await check(ctx, 'first Escape closes the screen list', 'document.querySelector(".design-outline").hidden');
  await key(ctx, 'Escape');
  await atPath(ctx, design + route);
  await key(ctx, 'Escape');
  await homeReady(ctx);
  await click(ctx, '.design-tile[title="' + label + '"]');
  await boardReady(ctx);
  camera = await session.eval(cameraExpression);
  await click(ctx, '.design-section .design-node-title');
  await movedCamera(ctx, camera);
  camera = await session.eval(cameraExpression);
  await click(ctx, '.design-note .design-node-title');
  await movedCamera(ctx, camera);
  await click(ctx, '.design-fit');
  await ctx.cdp.sleep(350);
  const offset = 'document.querySelector(".design-screen[data-view=home]").style.left';
  const original = await session.eval(offset);
  await drag(ctx, await point(ctx, '[data-move-view="home"]'), 35, 24);
  await check(ctx, 'dragging a screen name moves it and offers Reset layout', offset + ' !== ' + JSON.stringify(original) + ' && !document.querySelector(".design-reset").hidden');
  await click(ctx, '.design-reset');
  await check(ctx, 'Reset layout restores the authored positions', offset + ' === ' + JSON.stringify(original));
  await session.screenshot(path.join(ctx.report.dir, 'next-design-board.png'));
}

function readFrame(ctx) {
  return ctx.session.eval(`new Promise(resolve => {
    const frame = document.querySelector('iframe.design-live');
    if (!frame) { resolve(null); return; }
    const source = frame.contentWindow;
    const expire = setTimeout(() => finish(null), 250);
    function finish(result) {
      clearTimeout(expire);
      window.removeEventListener('message', receive);
      resolve(result);
    }
    function receive(event) {
      if (event.source === source && event.origin === 'null' && event.data?.type === 'design-fixture-state') {
        finish(event.data);
      }
    }
    window.addEventListener('message', receive);
    source.postMessage('read-design-fixture', '*');
  })`);
}

async function activeFrame(ctx) {
  await wait(ctx, 'document.querySelector("iframe.design-live")?.dataset.liveInput === "true"', 'active prepared frame');
  const end = Date.now() + ctx.timeoutMs;
  do {
    const state = await readFrame(ctx);
    if (state) return state;
  } while (Date.now() < end);
  assert.fail('prepared sandboxed fixture responded');
}

async function usePrototype(ctx, exercise) {
  const { button } = await activeFrame(ctx);
  await ctx.cdp.sleep(330);
  await hit(ctx, await point(ctx, '.design-live', ...button));
  const current = await readFrame(ctx);
  ctx.report.ok('prototype runs in the current screen', current?.state === 'Session');
  if (exercise) await exercise(button);
}

async function prototypes(ctx) {
  const address = design + route + '/' + prototype;
  await open(ctx, address);
  await usePrototype(ctx, async xy => {
    const before = await ctx.session.eval(cameraExpression);
    await mouse(ctx, await point(ctx, '.design-live', ...xy), 'mouseWheel', { deltaX: 0, deltaY: -160 });
    await check(ctx, 'wheel inside active frame leaves the board camera put', cameraExpression + ' === ' + JSON.stringify(before));
    await ctx.session.eval(`(() => {
      const stray = document.createElement("iframe");
      stray.id = "stray-fixture";
      stray.sandbox = "allow-scripts";
      stray.srcdoc = '<script>parent.postMessage("ArrowLeft","*")<'+ '/script>';
      document.body.append(stray);
    })()`);
    await ctx.cdp.sleep(200);
    await atPath(ctx, address);
    await ctx.session.eval('document.getElementById("stray-fixture").remove()');
    await key(ctx, 'ArrowLeft');
    await atPath(ctx, design + route + '/home');
  });
  await click(ctx, nextButton);
  await atPath(ctx, address);
  await usePrototype(ctx);
  await click(ctx, '[aria-label="Start over"]:not([hidden])');
  const restarted = await activeFrame(ctx);
  ctx.report.ok('Start over restores the first state', restarted.state === 'Chats');

  const old = design + '/' + legacy + '/old-orders';
  await open(ctx, old + '/home');
  await usePrototype(ctx);
  await key(ctx, 'ArrowRight');
  await atPath(ctx, old + '/home');
  await check(ctx, 'old frame leaves all board controls available',
    '[...document.querySelectorAll(".design-step button, .design-fit, .design-board-bar [aria-label=Back]")].every(b => b.getBoundingClientRect().width > 0)');
  await click(ctx, nextButton);
  await atPath(ctx, old + '/detail');
  await click(ctx, '[aria-label="Previous screen"]');
  await usePrototype(ctx);
  await hit(ctx, await point(ctx, stage, 0.015, 0.15));
  await key(ctx, 'ArrowRight');
  await atPath(ctx, old + '/detail');
  await key(ctx, 'Escape');
  await atPath(ctx, old);
}

async function cardJourney(ctx) {
  for (const width of [1440, 1010, 702]) {
    await open(ctx, '/next/', width);
    await click(ctx, '[aria-label="Dashboards"]');
    await homeReady(ctx);
    await check(ctx, 'Design occupies the dash card and lights the rail at ' + width,
      '!document.querySelector(".design-card").hidden && document.querySelector("[aria-label=Dashboards]").classList.contains("on") && !document.querySelector(".design-card .card-head") && document.querySelector(".design-menu").hidden');
    await click(ctx, '.design-card [aria-label="Full screen"]');
    await check(ctx, 'Design full screen hides the other panes at ' + width, 'document.querySelector(".pane").hidden && document.querySelector(".lanes-card").hidden');
    for (const theme of ['dark', 'light', 'cosmic']) {
      await ctx.session.eval('document.documentElement.dataset.theme=' + JSON.stringify(theme));
      await check(ctx, 'Design uses the surface token in ' + theme, 'getComputedStyle(document.querySelector(".design-card")).backgroundColor === getComputedStyle(document.documentElement).getPropertyValue("--surface").trim().replace(/#([0-9a-f]{6})/, (_, hex) => "rgb(" + hex.match(/../g).map(a=>parseInt(a,16)).join(", ") + ")")');
    }
    await click(ctx, '.design-card [aria-label="Close"]');
    await atPath(ctx, '/next/');
    await click(ctx, '[aria-label="Dashboards"]');
    await homeReady(ctx);
    await click(ctx, '[aria-label="Dashboards"]');
    await atPath(ctx, '/next/');
  }
}

async function refreshJourney(ctx) {
  const scratch = ctx.runtime.fixtureScratch;
  const library = path.join(scratch, 'design-library');
  const first = createLibraryReader(library).selected(project).manifest.revision_id;
  const { session } = ctx;
  const init = await session.send('Page.addScriptToEvaluateOnNewDocument', { source: `
    const nativeFetch = window.fetch.bind(window);
    let failed = false;
    window.fetch = (resource, ...args) => {
      if (resource === '/api/designs' && !failed) {
        failed = true;
        return Promise.resolve(new Response(null, {status:503}));
      }
      return nativeFetch(resource, ...args);
    };
  ` });
  try {
    await open(ctx, design);
    await wait(ctx, 'document.querySelector(".design-empty")?.textContent.includes("Design did not load")', 'failed first load');
    await session.send('Page.removeScriptToEvaluateOnNewDocument', { identifier: init.identifier });
    await click(ctx, '[title="Try again"]');
    await homeReady(ctx);
    const newer = await stageFixture(scratch, { generation: 'b', additional: false });
    await wait(ctx, `!!document.querySelector('.design-tile[title="Delivery update"] .design-unread')`, 'new design appears on Home', 15000);
    rollback({ library, project, revision: first });
    await open(ctx, design + route + '/' + prototype);
    await activeFrame(ctx);
    const camera = await session.eval(cameraExpression);
    rollback({ library, project, revision: newer.revision });
    await wait(ctx, '!document.querySelector(".design-update").hidden', 'new board version pill', 15000);
    await check(ctx, 'board keeps its prior content until Show', 'document.querySelector(".design-note").innerText === "Delivery note"');
    await click(ctx, '[aria-label="Show new version"]');
    await check(ctx, 'Show keeps camera and selected screen', cameraExpression + ' === ' + JSON.stringify(camera) + ' && !!document.querySelector(".is-current[data-view=prototype]")');
    await check(ctx, 'Show uses the published board', 'document.querySelector(".design-note").innerText === "New delivery note"');
  } finally {
    await session.send('Page.removeScriptToEvaluateOnNewDocument', { identifier: init.identifier }).catch(() => {});
    rollback({ library, project, revision: first });
  }
}

async function touch(ctx, type, points) {
  await ctx.session.send('Input.dispatchTouchEvent', { type, touchPoints: points.map(([x, y], id) => ({ x, y, id })) });
}

async function gestureState(ctx, points) {
  return ctx.session.eval(`(() => {
    const stage = document.querySelector('.design-stage');
    const world = document.querySelector('.design-world');
    const bounds = stage.getBoundingClientRect().toJSON();
    const matrix = new DOMMatrixReadOnly(getComputedStyle(world).transform);
    return {
      width: innerWidth,
      bounds,
      scale: matrix.a,
      x: matrix.e,
      y: matrix.f,
      points: ${JSON.stringify(points)}.map(([x, y]) => {
        const target = document.elementFromPoint(x, y);
        return { x, y, target: target?.className, onStage: stage.contains(target) };
      }),
      pointers: window.designPointerTrace.slice(),
    };
  })()`);
}

async function traceGestures(ctx) {
  await ctx.session.eval(`(() => {
    window.designPointerTrace = [];
    const stage = document.querySelector('.design-stage');
    for (const type of ['pointerdown','pointermove','pointerup','pointercancel','gotpointercapture','lostpointercapture']) {
      document.addEventListener(type, event => {
        if (window.designPointerTrace.length >= 40) return;
        window.designPointerTrace.push({
          type, id: event.pointerId, button: event.button, buttons: event.buttons,
          pointer: event.pointerType, primary: event.isPrimary, x: event.clientX, y: event.clientY,
          target: event.target.className, onStage: stage.contains(event.target),
        });
      }, { capture: true });
    }
  })()`);
}

async function checkGesture(ctx, width, kind, start, end) {
  await ctx.session.eval('window.designPointerTrace.length=0');
  const before = await gestureState(ctx, start);
  let passed = false;
  try {
    await touch(ctx, 'touchStart', start);
    await touch(ctx, 'touchMove', end);
    // Release a stopped pan so Chrome does not treat the next tap as a fling cancel.
    if (kind === 'pan') await ctx.cdp.sleep(100);
    await touch(ctx, 'touchEnd', []);
    const deltaScale = 'Math.abs(m.a-(' + before.scale + '))';
    const deltaPosition = 'Math.hypot(m.e-(' + before.x + '),m.f-(' + before.y + '))';
    const assertion = kind === 'pinch' ? deltaScale + ' > 0.001' :
      deltaPosition + ' > 0.5 && ' + deltaScale + ' < 0.001';
    await wait(ctx, `(() => {
      const world = document.querySelector('.design-world');
      const m = new DOMMatrixReadOnly(getComputedStyle(world).transform);
      return ${assertion};
    })()`, 'phone ' + kind + ' changes ' + (kind === 'pinch' ? 'scale' : 'translation') + ' at ' + width, 3000);
    passed = true;
  } finally {
    const after = await gestureState(ctx, end);
    ctx.report.note('E6 ' + width + ' ' + kind + ' ' + JSON.stringify({ passed, before, after }));
  }
}

async function phoneWidth(ctx, width) {
  const phoneAddress = design + '/' + project + '/phone-orders';
  await ctx.session.send('Page.addScriptToEvaluateOnNewDocument', { source: `
    (() => {
      window.designPullRequests = 0;
      const original = fetch.bind(window);
      window.fetch = (url, ...args) => {
        if (url === '/api/designs') window.designPullRequests++;
        return original(url, ...args);
      };
    })()
  ` });
  await open(ctx, phoneAddress, width);
  await boardReady(ctx);
  await check(ctx, 'phone fits the board and only shows fit in the zoom pill at ' + width,
    'document.querySelector(".design-card").classList.contains("design-phone") && !document.querySelector(".is-current") && [...document.querySelectorAll(".design-zoom button")].filter(b=>b.getBoundingClientRect().width>0).length===1');
  const a = await point(ctx, stage, 0.11, 0.42);
  const b = await point(ctx, stage, 0.75, 0.42);
  await traceGestures(ctx);
  await checkGesture(ctx, width, 'pinch', [[a.x, a.y], [b.x, b.y]], [[a.x - 20, a.y], [b.x + 25, b.y]]);
  await checkGesture(ctx, width, 'pan', [[a.x, a.y]], [[a.x + 32, a.y + 35]]);
  await click(ctx, '.design-fit');
  await ctx.cdp.sleep(350);
  const framePoint = await point(ctx, '[data-view="' + phone + '"] .design-screen-hit');
  await touch(ctx, 'touchStart', [[framePoint.x, framePoint.y]]);
  await touch(ctx, 'touchEnd', []);
  await atPath(ctx, phoneAddress + '/' + phone);
  await click(ctx, nextButton);
  await atPath(ctx, phoneAddress + '/phone-detail');
  await click(ctx, screensButton);
  await check(ctx, 'phone screen list opens at ' + width, '!document.querySelector(".design-outline").hidden');
  await ctx.session.screenshot(path.join(ctx.report.dir, 'next-design-phone-' + width + '.png'));
  await click(ctx, backButton);
  await homeReady(ctx);
  await ctx.session.eval('window.designPullRequests=0');
  const start = await point(ctx, '.design-home', 0.5, 0.20);
  await touch(ctx, 'touchStart', [[start.x, start.y]]);
  await touch(ctx, 'touchMove', [[start.x, start.y + 140]]);
  await touch(ctx, 'touchEnd', []);
  await wait(ctx, 'window.designPullRequests === 1', 'pull down refreshes Home at ' + width);
}

async function phoneJourney(ctx) {
  // Give each width its own input state. On a reused target, Chrome accepted
  // touch commands after the resize and navigation but delivered no pointers.
  for (const width of [440, 390]) {
    const address = 'about:blank#design-phone-' + width;
    const { targetId } = await ctx.session.send('Target.createTarget', { url: address });
    let page;
    try {
      page = await ctx.cdp.connect(ctx.cdpPort, {
        match: new RegExp('design-phone-' + width), excludeTargetId: ctx.session.target.id,
      });
      assert.equal(page.target.id, targetId, 'owned phone browser target');
      await page.send('Page.bringToFront');
      await phoneWidth({ ...ctx, session: page }, width);
    } finally {
      page?.close();
      assert.ok((await ctx.session.send('Target.closeTarget', { targetId })).success, 'phone target closed');
    }
  }
}

async function linkAddresses(ctx) {
  for (const [address, expected] of [
    [design, design],
    [design + route, design + route],
    [design + route + '/prototype#old-fragment', design + route + '/prototype'],
    [design + route + '/missing', design + route],
    [design + '/delivery/missing', design],
  ]) {
    await open(ctx, address);
    if (expected === design) await homeReady(ctx);
    else await boardReady(ctx);
    await atPath(ctx, expected);
    await check(ctx, 'canonical workspace address and shipped assets for ' + address,
      `!location.hash && !!window.cc && !!document.querySelector('link[href="/next/style.css"]') && performance.getEntriesByType("resource").some(x=>x.name.endsWith("/next/bundle.js"))`);
  }
}

async function designServer(ctx, token = null) {
  const profile = path.join(ctx.runtime.fixtureScratch, 'next-design-identity.cjs');
  const library = path.join(ctx.runtime.fixtureScratch, 'design-library');
  fs.writeFileSync(profile, 'module.exports=' + JSON.stringify({ dashboards: { designLibraryRoot: library } }));
  const args = token ? [profile, token] : [profile];
  const proc = fork(path.join(__dirname, 'next_design_identity_child.cjs'), args, { stdio: ['ignore', 'ignore', 'inherit', 'ipc'] });
  const { stopOwnedProcess } = require('../web_gate');
  try {
    const [ready] = await Promise.race([
      once(proc, 'message'),
      once(proc, 'exit').then(() => { throw new Error('Design fixture exited before bind'); }),
    ]);
    assert.ok(Number.isInteger(ready.port));
    return { proc, origin: 'http://127.0.0.1:' + ready.port };
  } catch (error) {
    await stopOwnedProcess(proc);
    throw error;
  }
}

async function standalone(ctx) {
  const tokenFile = path.join(ctx.runtime.fixtureScratch, 'design-fixture-token');
  fs.writeFileSync(tokenFile, 'synthetic-design-fixture', { mode: 0o600 });
  const { stopOwnedProcess } = require('../web_gate');
  const token = await designServer(ctx, tokenFile);
  try {
    const login = await fetch(token.origin + '/login', {
      method: 'POST', redirect: 'manual', body: new URLSearchParams({ token: 'synthetic-design-fixture' }),
    });
    assert.equal(login.status, 302);
    const cookie = login.headers.get('set-cookie').split(';', 1)[0];
    const options = { redirect: 'manual', headers: { cookie } };
    const [shell, standalone] = await Promise.all([
      fetch(token.origin + '/next/', options),
      fetch(token.origin + embed + route + '/' + prototype, options),
    ]);
    ctx.report.ok('same valid token cookie opens normal Next and gets standalone 404', shell.status === 200 && standalone.status === 404);
  } finally {
    await stopOwnedProcess(token.proc);
  }
  const { proc, origin } = await designServer(ctx);
  const { session } = ctx;
  const events = [];
  function record(raw) {
    let event;
    try { event = JSON.parse(raw); } catch { return; }
    if (event.method === 'Network.webSocketCreated') {
      const socket = new URL(event.params.url);
      if (socket.host === new URL(origin).host) events.push(socket.pathname);
    }
  }
  session.ws.on('message', record);
  try {
    const rejected = await fetch(origin + embed + route + '/' + prototype);
    ctx.report.ok('fixture identity is required', rejected.status === 401);
    for (const invalid of [embed + '/', embed + '/delivery', embed + route + '?x=1', embed + route + '/%70rototype']) {
      const reply = await fetch(origin + invalid, { headers: fixtureIdentity });
      ctx.report.ok('identity rejects malformed standalone address ' + invalid, reply.status === 404);
    }
    await session.send('Network.enable');
    await session.send('Network.setExtraHTTPHeaders', { headers: fixtureIdentity });
    for (const width of [390, 834]) {
      for (const [address, expected] of [
        [embed, embed],
        [embed + route, embed + route],
        [embed + route + '/' + prototype + '#old', embed + route + '/' + prototype],
        [embed + route + '/missing', embed + route],
        [embed + '/delivery/missing', embed],
      ]) {
        await open(ctx, address, width, origin);
        if (expected === embed) await homeReady(ctx);
        else await boardReady(ctx);
        await atPath(ctx, expected);
        await check(ctx, 'standalone fills the viewport with cosmic phone styling at ' + width + ' ' + address,
          'document.documentElement.dataset.theme==="cosmic" && document.querySelector("#app").classList.contains("design-phone") && document.querySelector("#app").clientWidth===innerWidth && document.querySelector("#app").clientHeight===innerHeight && !location.hash && !window.cc && !document.querySelector(".rail,.design-card-actions,form")');
        if (expected.endsWith('/' + prototype)) {
          await activeFrame(ctx);
          await check(ctx, 'standalone uses the existing prepared document policy and same-origin assets',
            `(() => {
              const frame = document.querySelector('iframe.design-live');
              const resources = performance.getEntriesByType('resource');
              return frame.getAttribute('sandbox') === 'allow-scripts' && frame.srcdoc.includes("connect-src 'none'") &&
                resources.every(item => new URL(item.name).origin === location.origin) &&
                resources.some(item => item.name.endsWith('/next/design-embed.bundle.js')) &&
                resources.some(item => item.name.includes('/design-media/')) &&
                resources.some(item => item.name.includes('/api/designs/'));
            })()`);
          await session.screenshot(path.join(ctx.report.dir, 'next-design-embed-screen-' + width + '.png'));
          const history = await session.eval('window.history.length');
          await click(ctx, '[aria-label="Previous screen"]');
          await atPath(ctx, embed + route + '/home');
          await click(ctx, backButton);
          await atPath(ctx, embed);
          await check(ctx, 'standalone navigation replaces history', 'window.history.length === ' + history);
          await session.screenshot(path.join(ctx.report.dir, 'next-design-embed-' + width + '.png'));
        }
      }
    }
    ctx.report.ok('standalone opens no websocket', events.length === 0);
  } finally {
    session.ws.off('message', record);
    await session.send('Network.setExtraHTTPHeaders', { headers: {} });
    await session.send('Network.disable');
    const cleanup = await stopOwnedProcess(proc);
    ctx.report.ok('identity fixture process exited', cleanup.exited && !cleanup.forced, cleanup);
  }
}

const linkAssistant = 'link-fixture:assistant';
const linkChat = 'link-fixture:review';
const linkSelector = path => `[data-next-design-link="${path}"]`;
const pinnedPhone = design + '/' + project + '/phone-orders/' + phone;
const pinnedScreen = design + route + '/' + prototype;

async function seedDesignLinks(ctx) {
  const { revision } = await ctx.runtime.nextDesignFixture;
  const pngKey = createHash('sha256').update(PNG).digest('hex');
  const target = (design_id, view_id) => ({ project_id: project, design_id, ...(view_id ? { view_id } : {}) });
  const link = (target, title, revision_id = revision) => ({
    dashboard: 'design', ...(target ? { target, revision_id, title, subtitle: label } : {}),
  });
  const session = (stream_id, session_name, session_kind, display_name) => ({
    stream_id, session_name, session_kind, display_name, host: 'link-fixture', visibility: 'default',
    last_activity: '2026-01-01T12:00:00Z',
  });
  const sessions = [
    session(linkAssistant, 'assistant', 'assistant_composite', 'Bart'),
    session(linkChat, 'review', 'session', 'Design review fixture'),
  ];
  const event = (seq, stream, dashboard_link, withoutAttachment = false) => ({
    stream_id: stream, host: 'link-fixture', session_id: stream, session_name: stream.split(':')[1],
    kind: 'ASSIST', provider: 'codex', daemon_seq: seq, timestamp: '2026-01-01T12:00:00Z',
    text: seq === 1 ? 'Review the order prototype.' : '',
    raw: { dashboard_link },
    ...(withoutAttachment ? {} : { attachments: [{ mime: 'image/png', key: pngKey, bytes: PNG.length }] }),
  });
  const events = [
    event(1, linkAssistant, link(target('delivery-board', prototype), 'Order prototype')),
    event(2, linkAssistant, link(null), true),
    event(3, linkAssistant, link(target('delivery-board', 'missing-screen'), 'Removed screen')),
    event(4, linkAssistant, link(target('missing-board'), 'Removed board')),
    event(5, linkChat, link(target('phone-orders', phone), 'Phone order', 'f'.repeat(64))),
  ];
  const image = PNG.toString('base64');
  await ctx.session.eval(`(() => {
    const previous = window.cc.chatFetchBlob;
    window.cc.chatFetchBlob = key => key === ${JSON.stringify(pngKey)}
      ? Promise.resolve({ ok: true, content_b64: ${JSON.stringify(image)} }) : previous(key);
    window.PentacleChatStore.applyFrame({type:'snapshot', sessions:${JSON.stringify(sessions)}, events:[], drafts:{}});
    for (const event of ${JSON.stringify(events)}) window.PentacleChatStore.applyFrame({type:'chat.event', event});
    return true;
  })()`);
}

async function designLinkJourney(ctx) {
  const preview = linkSelector(pinnedScreen);
  for (const width of [1440, 1010, 702]) {
    await open(ctx, '/next/', width);
    await seedDesignLinks(ctx);
    await wait(ctx, `document.querySelector('${preview} img')?.naturalWidth > 0`, 'recorded preview loaded');
    await check(ctx, `preview caption, titles and size at ${width} px`, `(() => {
      const card = document.querySelector('${preview}');
      const size = card.getBoundingClientRect().width;
      return innerWidth === ${width} && size > 0 && size <= 296.5 &&
        card.closest('article.slot-chat-row').textContent.includes('Review the order prototype.') &&
        card.querySelector('strong').textContent === 'Order prototype' &&
        card.textContent.includes('Design · Delivery board') &&
        new URL(card.querySelector('img').src).pathname.includes('/design-preview/');
    })()`);
  }
  await ctx.session.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await click(ctx, preview);
  await atPath(ctx, pinnedScreen);
  await boardReady(ctx);
  await check(ctx, 'Bart and the selected screen are visible, with the link outlined',
    `!document.querySelector('.pane').hidden &&
      document.querySelector('${preview}').getAttribute('aria-pressed') === 'true' &&
      !!document.querySelector('.design-screen.is-current[data-view="${prototype}"]')`);
  await click(ctx, '.design-step [aria-label="Previous screen"]');
  await atPath(ctx, design + route + '/home');
  await check(ctx, 'stepping clears the outline', `document.querySelector('${preview}').getAttribute('aria-pressed') === 'false'`);
  await click(ctx, preview);
  await atPath(ctx, pinnedScreen);
  await click(ctx, preview);
  await atPath(ctx, '/next/');
  await check(ctx, 'second click closes the exact target', 'document.querySelector(".design-card").hidden');

  await click(ctx, linkSelector(design + route + '/missing-screen'));
  await atPath(ctx, design + route);
  await boardReady(ctx);
  await check(ctx, 'a missing screen opens its board', '!document.querySelector(".design-screen.is-current")');
  await click(ctx, linkSelector(design + '/' + project + '/missing-board'));
  await atPath(ctx, design);
  await check(ctx, 'a missing board opens Home with its notice',
    'document.querySelector(".design-home").textContent.includes("This design is no longer available.")');
  await click(ctx, '.design-card [aria-label="Close"]');
  await click(ctx, linkSelector(design));
  await atPath(ctx, design);
  await homeReady(ctx);
  await click(ctx, linkSelector(design));
  await atPath(ctx, '/next/');

  await click(ctx, '.rail-button[aria-label="Chats"]');
  await click(ctx, `.popover [data-stream-id="${linkChat}"]`);
  const phonePreview = linkSelector(pinnedPhone);
  await wait(ctx, `document.querySelector('${phonePreview} img')?.src.startsWith('data:image/png') &&
    document.querySelector('${phonePreview} img').naturalWidth > 0`, 'attached PNG fallback');
  await click(ctx, phonePreview);
  await atPath(ctx, pinnedPhone);
  await boardReady(ctx);
  await check(ctx, 'Design follows the other chat while Bart steps aside',
    `document.querySelector('.pane').hidden && !document.querySelector('.card:not(.design-card):not(.lanes-card)').hidden`);
  await click(ctx, '.design-card [aria-label="Close"]');
  await check(ctx, 'Bart returns after Design closes', '!document.querySelector(".pane").hidden');

  await ctx.session.send('Page.navigate', { url: ctx.url });
  await wait(ctx, 'document.readyState === "complete" && !!document.getElementById("session-list")', 'old web workspace');
  await seedDesignLinks(ctx);
  await check(ctx, 'the same event in the old web bundle remains caption plus ordinary image', `(() => {
    const element = document.createElement('div');
    window.PentacleChatView.renderStreamTranscript(${JSON.stringify(linkAssistant)}, element);
    return !element.querySelector('[data-next-design-link]') && !!element.querySelector('.slot-chat-media-img') &&
      element.textContent.includes('Review the order prototype.');
  })()`);
}

async function designLinkPhone(ctx) {
  await open(ctx, '/next/', 440);
  await seedDesignLinks(ctx);
  const selector = linkSelector(pinnedScreen);
  await wait(ctx, `document.querySelector('${selector} img')?.naturalWidth > 0`, 'phone Design preview');
  await ctx.session.eval(`document.querySelector('${selector}').scrollIntoView({ block: 'center' })`);
  await ctx.session.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 });
  try {
    const pt = await point(ctx, selector, 0.5, 0.85);
    await touch(ctx, 'touchStart', [[pt.x, pt.y]]);
    await touch(ctx, 'touchEnd', []);
    await atPath(ctx, pinnedScreen);
    await boardReady(ctx);
    await check(ctx, 'one tap opens the full-width Design screen', `innerWidth === 440 &&
      document.querySelector('.pane').hidden &&
      document.querySelector('.design-card').getBoundingClientRect().width === 440 &&
      !!document.querySelector('.design-screen.is-current[data-view="${prototype}"]')`);
  } finally {
    await ctx.session.send('Emulation.setTouchEmulationEnabled', { enabled: false });
  }
  await click(ctx, '.design-card [aria-label="Close"]');
  await atPath(ctx, '/next/');
  await check(ctx, 'closing Design restores the phone chat', '!document.querySelector(".pane").hidden');
}

function isolated(run) {
  return async ctx => {
    assert.ok(ctx.runtime.fixtureScratch, 'Design scenarios require the isolated web fixture');
    // The old gallery runs first with its own exact fixture catalog.
    ctx.runtime.nextDesignFixture ??= stageFixture(ctx.runtime.fixtureScratch);
    await ctx.runtime.nextDesignFixture;
    try { await run(ctx); }
    finally {
      await ctx.session.send('Emulation.clearDeviceMetricsOverride');
      await ctx.session.send('Page.navigate', { url: ctx.url });
      await wait(ctx, 'document.readyState === "complete" && !!document.getElementById("session-list")', 'restore original workspace');
    }
  };
}

module.exports = [
  ['next-design-home', isolated(layoutHome)],
  ['next-design-board', isolated(boardJourney)],
  ['next-design-prototype', isolated(prototypes)],
  ['next-design-card', isolated(cardJourney)],
  ['next-design-refresh', isolated(refreshJourney)],
  ['next-design-phone', isolated(phoneJourney)],
  ['next-design-address', isolated(linkAddresses)],
  ['next-design-embed', isolated(standalone)],
  ['next-design-link', isolated(designLinkJourney)],
  ['next-design-link-phone', isolated(designLinkPhone)],
];
