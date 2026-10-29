'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');
const {
  READY_TIMEOUT_MS, READY_MESSAGE, OPEN_EXTERNAL_MESSAGE, SANDBOX,
  validateEngineUrl, validateExternalUrl, createDashboard,
} = require('../renderer/dashboards/travel_engine');

const rendererDir = path.resolve(__dirname, '..', 'renderer');
const readDashboardScript = () => fs.readFileSync(path.join(rendererDir, 'dashboards', 'travel_engine.js'), 'utf8');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

test('dashboard readiness timeout is ten seconds', () => {
  assert.equal(READY_TIMEOUT_MS, 10_000);
});

test('travel service URL accepts only a safe cross-origin HTTP(S) root', () => {
  const httpPage = { protocol: 'http:', origin: 'http://pentacle.example' };
  assert.deepEqual(validateEngineUrl('http://127.0.0.1:8777', httpPage), {
    ok: true, url: 'http://127.0.0.1:8777/', origin: 'http://127.0.0.1:8777',
  });
  assert.equal(validateEngineUrl('https://travel.example/', httpPage).ok, true);
  for (const value of [null, '', 'ftp://travel.example', 'https://user:pass@travel.example',
    'http://travel.example/app', 'http://travel.example?', 'http://travel.example#',
    'http://travel.example/a/..']) {
    assert.equal(validateEngineUrl(value, httpPage).ok, false, `${value} must be rejected`);
  }
  assert.equal(validateEngineUrl('http://pentacle.example', httpPage).ok, false, 'same origin is rejected');
  assert.equal(validateEngineUrl('http://travel.example', { protocol: 'https:', origin: 'https://pentacle.example' }).ok,
    false, 'HTTP is rejected from an HTTPS Pentacle page');
  assert.equal(validateEngineUrl('http://travel.example', { protocol: 'file:', origin: 'null' }).ok,
    true, 'HTTP is available to the Electron file page');
});

test('external message destinations must be absolute credential-free HTTP(S) URLs', () => {
  assert.equal(validateExternalUrl('https://airline.example/book?a=1'), 'https://airline.example/book?a=1');
  for (const value of [null, '/book', '//airline.example/book', 'javascript:alert(1)',
    'file:///etc/passwd', 'https://user:pass@airline.example/book']) {
    assert.equal(validateExternalUrl(value), null, `${value} must be rejected`);
  }
});

test('desktop preload exposes only the configured Travel Engine URL', () => {
  const source = fs.readFileSync(path.resolve(__dirname, '..', 'preload.js'), 'utf8');
  const start = source.indexOf('// Synchronous host metadata');
  const end = source.indexOf('\nwindow.cc = {', start);
  assert.ok(start >= 0 && end > start, 'preload host metadata block found');
  const run = config => {
    const context = {
      __dirname: path.resolve(__dirname, '..'),
      loadConfig: () => ({ config }),
      os: { hostname: () => 'fixture-host' },
      process: { platform: 'test' },
      window: {},
    };
    vm.runInNewContext(source.slice(start, end), context);
    return context.window.HOST;
  };
  assert.deepEqual(JSON.parse(JSON.stringify(run({ travelEngine: { url: ' http://127.0.0.1:8777 ', token: 'private' } }).travelEngineConfig)),
    { url: 'http://127.0.0.1:8777' });
  assert.equal(run({}).travelEngineConfig, null);
});

function loadDashboard(config = null, pageUrl = 'http://pentacle.example/') {
  const dom = new JSDOM('<!doctype html><body><div id="dashboard"></div></body>', {
    url: pageUrl,
    runScripts: 'dangerously',
  });
  const { window } = dom;
  const opened = [];
  window.HOST = { travelEngineConfig: config };
  window.cc = { openExternal: url => { opened.push(url); return Promise.resolve({ ok: true }); } };
  const registry = fs.readFileSync(path.join(rendererDir, 'dashboards', 'registry.js'), 'utf8');
  const registryTag = window.document.createElement('script');
  registryTag.textContent = registry;
  window.document.body.appendChild(registryTag);
  const dashboardTag = window.document.createElement('script');
  dashboardTag.textContent = readDashboardScript();
  window.document.body.appendChild(dashboardTag);
  return { dom, window, container: window.document.getElementById('dashboard'), opened };
}

test('dashboard self-registers and gives an actionable setup state when unconfigured', () => {
  const { dom, window, container } = loadDashboard();
  const dashboard = window.DASHBOARDS.find(board => board.id === 'travel-engine');
  assert.ok(dashboard);
  assert.equal(dashboard.name, 'Travel Engine');
  const refs = dashboard.mount(container);
  assert.match(container.textContent, /travelEngine\.url/);
  assert.equal(container.querySelector('iframe'), null);
  dashboard.unmount(refs);
  dom.window.close();
});

test('mounted frame has the exact sandbox, clipboard grant, title, and referrer policy', () => {
  const { dom, window, container } = loadDashboard({ url: 'http://travel.example:8777' });
  const dashboard = window.DASHBOARDS.find(board => board.id === 'travel-engine');
  const refs = dashboard.mount(container);
  const frame = container.querySelector('iframe');
  assert.ok(frame);
  assert.equal(frame.getAttribute('sandbox'), SANDBOX);
  assert.equal(frame.getAttribute('allow'), 'clipboard-write');
  assert.equal(frame.title, 'Travel Engine');
  assert.equal(frame.getAttribute('referrerpolicy'), 'no-referrer');
  assert.equal(frame.src, 'http://travel.example:8777/');
  dashboard.unmount(refs);
  dom.window.close();
});

test('same-origin service configuration produces an explanatory state without a frame', () => {
  const { dom, window, container } = loadDashboard({ url: 'http://pentacle.example' });
  const dashboard = window.DASHBOARDS.find(board => board.id === 'travel-engine');
  const refs = dashboard.mount(container);
  assert.match(container.textContent, /different origin/);
  assert.equal(container.querySelector('iframe'), null);
  dashboard.unmount(refs);
  dom.window.close();
});

test('ready and external messages require the mounted frame, exact origin, and valid shape', async () => {
  const { dom, window, container, opened } = loadDashboard({ url: 'http://travel.example:8777' });
  const dashboard = window.DASHBOARDS.find(board => board.id === 'travel-engine');
  const refs = dashboard.mount(container);
  const frame = container.querySelector('iframe');
  const send = (source, origin, data) => window.dispatchEvent(new window.MessageEvent('message', { source, origin, data }));

  send(window, 'http://travel.example:8777', { type: READY_MESSAGE, version: 1 });
  send(frame.contentWindow, 'http://wrong.example', { type: READY_MESSAGE, version: 1 });
  send(frame.contentWindow, 'http://travel.example:8777', { type: READY_MESSAGE, version: 2 });
  assert.equal(frame.hidden, true, 'invalid ready messages do not reveal the page');

  send(frame.contentWindow, 'http://travel.example:8777', { type: READY_MESSAGE, version: 1 });
  assert.equal(frame.hidden, false);
  send(window, 'http://travel.example:8777', { type: OPEN_EXTERNAL_MESSAGE, url: 'https://airline.example/book' });
  send(frame.contentWindow, 'http://wrong.example', { type: OPEN_EXTERNAL_MESSAGE, url: 'https://airline.example/book' });
  send(frame.contentWindow, 'http://travel.example:8777', { type: OPEN_EXTERNAL_MESSAGE });
  send(frame.contentWindow, 'http://travel.example:8777', { type: OPEN_EXTERNAL_MESSAGE, url: 'javascript:alert(1)' });
  send(frame.contentWindow, 'http://travel.example:8777', { type: OPEN_EXTERNAL_MESSAGE, url: 'https://user:pass@airline.example/book' });
  send(frame.contentWindow, 'http://travel.example:8777', { type: OPEN_EXTERNAL_MESSAGE, url: 'https://airline.example/book' });
  await pause(0);
  assert.deepEqual(opened, ['https://airline.example/book']);
  dashboard.unmount(refs);
  dom.window.close();
});

test('unavailable timeout exposes Retry and a late valid ready message recovers', async () => {
  const { dom, window, container } = loadDashboard({ url: 'http://travel.example:8777' });
  const dashboard = createDashboard({ windowRef: window, timeoutMs: 10 });
  const refs = dashboard.mount(container);
  const frame = container.querySelector('iframe');
  await pause(20);
  assert.match(container.textContent, /Travel Engine is unavailable/);
  assert.ok(container.querySelector('.travel-engine-retry'));
  container.querySelector('.travel-engine-retry').click();
  await pause(5);
  window.dispatchEvent(new window.MessageEvent('message', {
    source: frame.contentWindow,
    origin: 'http://travel.example:8777',
    data: { type: READY_MESSAGE, version: 1 },
  }));
  assert.equal(frame.hidden, false);
  assert.equal(refs.status.hidden, true);
  dashboard.unmount(refs);
  dom.window.close();
});
