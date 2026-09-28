const test = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');

const piControl = require('../renderer/dashboards/pi-control');

function makeResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return body; },
  };
}

function tick() {
  return new Promise((resolve) => setImmediate(resolve));
}

class MockEventSource {
  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.closed = false;
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    MockEventSource.instances.push(this);
  }

  open() {
    this.readyState = 1;
    if (this.onopen) this.onopen({});
  }

  message(payload) {
    if (this.onmessage) this.onmessage({ data: JSON.stringify(payload) });
  }

  error() {
    if (this.onerror) this.onerror(new Error('sse failed'));
  }

  close() {
    this.closed = true;
    this.readyState = 2;
  }
}

function makeHarness(options = {}) {
  MockEventSource.instances = [];
  const dom = new JSDOM('<!doctype html><main id="root"></main>', { url: 'http://pentacle.test/' });
  const outputs = options.outputs || [{
    output_id: 'o1',
    title: 'First output',
    agent_id: 'codex-dev',
    content_type: 'markdown',
    created_at: '2026-05-16T12:00:00Z',
    tags: ['smoke'],
  }];
  const devices = options.devices || [{
    device_id: 'pi-dash-node1',
    profile_id: 'foreclosure-live',
    profile_state: {},
    updated_at: '2026-05-16T12:01:00Z',
    connected: true,
  }];
  const payloads = {
    o1: {
      ref: outputs[0],
      payload: '# First output\n\n- ready',
    },
    ...(options.payloads || {}),
  };
  const requests = [];
  const timers = [];
  const runtime = {
    hubUrl: 'http://hub.test',
    readToken: 'read-secret',
    writeToken: 'write-secret',
    EventSource: MockEventSource,
    confirmAction: () => Promise.resolve(true),
    now: options.now || (() => new Date('2026-05-16T12:02:00Z').getTime()),
    setTimeout: (fn, delay) => {
      timers.push({ fn, delay });
      return timers.length;
    },
    clearTimeout: () => {},
    fetch: async (url, init = {}) => {
      const parsed = new URL(url);
      const method = init.method || 'GET';
      const body = init.body ? JSON.parse(init.body) : null;
      requests.push({
        method,
        path: parsed.pathname,
        url,
        headers: init.headers || {},
        body,
      });
      if (method === 'GET' && parsed.pathname === '/outputs') return makeResponse(outputs.slice());
      if (method === 'GET' && parsed.pathname === '/control/devices') return makeResponse(devices.slice());
      if (method === 'GET' && parsed.pathname.startsWith('/outputs/')) {
        return makeResponse(payloads[decodeURIComponent(parsed.pathname.split('/').pop())] || payloads.o1);
      }
      return makeResponse({ ok: true });
    },
  };
  dom.window.__PI_CONTROL_TEST_RUNTIME = runtime;
  dom.window.DASHBOARDS = [];
  dom.window.selectDashboardCalls = [];
  dom.window.selectDashboard = (id) => dom.window.selectDashboardCalls.push(id);

  const container = dom.window.document.getElementById('root');
  const refs = piControl.mount(container);
  return { dom, container, refs, runtime, requests, timers, outputs, devices };
}

function textButton(container, text) {
  return Array.from(container.querySelectorAll('button')).find((button) => button.textContent.trim() === text);
}

function click(el, dom) {
  el.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true, cancelable: true }));
}

test('registers as a push-driven dashboard without pollInterval', () => {
  assert.equal(piControl.dashboard.id, 'pi-control');
  assert.equal(Object.prototype.hasOwnProperty.call(piControl.dashboard, 'pollInterval'), false);
  assert.equal(Object.prototype.hasOwnProperty.call(piControl.dashboard, 'pollFn'), false);
});

test('foreclosure panel renders disambiguation label and in-app dashboard link', async () => {
  const { dom, container } = makeHarness();
  await tick();

  assert.match(container.textContent, new RegExp(piControl._test.DISAMBIGUATION_LABEL));
  const link = Array.from(container.querySelectorAll('a')).find((a) => a.textContent === 'Open foreclosure dashboard');
  assert.ok(link);
  assert.equal(link.getAttribute('href'), '#foreclosure-pipeline');

  click(link, dom);
  assert.deepEqual(dom.window.selectDashboardCalls, ['foreclosure-pipeline']);
});

test('show-on-pi foreclosure posts the expected active profile body', async () => {
  const { dom, container, requests } = makeHarness();
  await tick();

  click(textButton(container, 'Show on Pi'), dom);
  await tick();

  const post = requests.find((req) => req.method === 'POST' && req.path === '/control/set-active-profile');
  assert.ok(post);
  assert.equal(post.headers.Authorization, 'Bearer write-secret');
  assert.deepEqual(post.body, {
    device_id: 'pi-dash-node1',
    profile_id: 'foreclosure-live',
    profile_state: {},
  });
});

test('agent output row actions post clear and clear-all requests', async () => {
  const { dom, container, requests } = makeHarness();
  await tick();

  click(container.querySelector('[data-profile-id="agent-output"]'), dom);
  await tick();

  const clear = Array.from(container.querySelectorAll('button')).find((button) => button.textContent === 'Clear');
  click(clear, dom);
  await tick();
  await tick();

  const clearPost = requests.find((req) => req.method === 'POST' && req.path === '/outputs/clear');
  assert.ok(clearPost);
  assert.equal(clearPost.headers.Authorization, 'Bearer write-secret');
  assert.deepEqual(clearPost.body, { output_id: 'o1' });

  click(container.querySelector('[data-profile-id="agent-output"]'), dom);
  await tick();
  click(textButton(container, 'Clear all'), dom);
  await tick();

  const clearAllPost = requests.find((req) => req.method === 'POST' && req.path === '/outputs/clear-all');
  assert.ok(clearAllPost);
  assert.equal(clearAllPost.headers.Authorization, 'Bearer write-secret');
});

test('show-on-pi output posts selected output profile state', async () => {
  const { dom, container, requests } = makeHarness();
  await tick();
  click(container.querySelector('[data-profile-id="agent-output"]'), dom);
  await tick();

  click(Array.from(container.querySelectorAll('button')).find((button) => button.textContent === 'Show on Pi'), dom);
  await tick();

  const post = requests.findLast((req) => req.method === 'POST' && req.path === '/control/set-active-profile');
  assert.deepEqual(post.body, {
    device_id: 'pi-dash-node1',
    profile_id: 'agent-output',
    profile_state: { selected_output_id: 'o1' },
  });
});

test('outputs.changed SSE updates rendered list without polling and dedupes output_id', async () => {
  const { dom, container, requests } = makeHarness({ outputs: [] });
  await tick();
  click(container.querySelector('[data-profile-id="agent-output"]'), dom);
  await tick();

  const initialOutputGets = requests.filter((req) => req.method === 'GET' && req.path === '/outputs').length;
  MockEventSource.instances[0].message({
    type: 'outputs.changed',
    emitted_at: '2026-05-16T12:03:00Z',
    added: [{
      output_id: 'o2',
      title: 'SSE output',
      agent_id: 'claude-leader',
      content_type: 'markdown',
      created_at: '2026-05-16T12:03:00Z',
      tags: ['sse'],
    }],
    removed: [],
  });
  MockEventSource.instances[0].message({
    type: 'outputs.changed',
    emitted_at: '2026-05-16T12:03:01Z',
    added: [{
      output_id: 'o2',
      title: 'SSE output updated',
      agent_id: 'claude-leader',
      content_type: 'markdown',
      created_at: '2026-05-16T12:03:01Z',
      tags: ['sse'],
    }],
    removed: [],
  });

  assert.match(container.textContent, /SSE output updated/);
  assert.equal(container.querySelectorAll('[data-output-id="o2"]').length, 1);
  assert.equal(requests.filter((req) => req.method === 'GET' && req.path === '/outputs').length, initialOutputGets);
});

test('device_state.changed SSE updates device pill and active view', async () => {
  const { container } = makeHarness();
  await tick();

  MockEventSource.instances[0].message({
    type: 'device_state.changed',
    emitted_at: '2026-05-16T12:03:00Z',
    device_id: 'pi-dash-node1',
    profile_id: 'agent-output',
    profile_state: { selected_output_id: 'o1' },
    updated_at: '2026-05-16T12:03:00Z',
  });

  assert.match(container.querySelector('[data-role="device-pill"]').textContent, /agent-output/);
  assert.ok(container.querySelector('[data-profile-id="agent-output"]').classList.contains('device-active'));
});

test('EventSource uses query-param auth and reconnect refetches initial state', async () => {
  const { container, requests, timers } = makeHarness();
  await tick();
  assert.match(MockEventSource.instances[0].url, /\/stream\/bart\.control\?token=read-secret$/);
  assert.equal(container.innerHTML.includes('read-secret'), false);
  assert.equal(requests.find((req) => req.method === 'GET' && req.path === '/outputs').headers.Authorization, 'Bearer read-secret');

  MockEventSource.instances[0].error();
  assert.equal(MockEventSource.instances[0].closed, true);
  assert.equal(timers.length, 1);
  assert.ok(timers[0].delay <= 30000);

  const initialDeviceGets = requests.filter((req) => req.method === 'GET' && req.path === '/control/devices').length;
  timers[0].fn();
  await tick();
  assert.equal(MockEventSource.instances.length, 2);
  assert.equal(
    requests.filter((req) => req.method === 'GET' && req.path === '/control/devices').length,
    initialDeviceGets + 1,
  );
});

test('token masking helper does not expose query token', () => {
  assert.equal(
    piControl._test.maskTokenInUrl('http://hub/stream/bart.control?token=secret-value&x=1'),
    'http://hub/stream/bart.control?token=***&x=1',
  );
});
