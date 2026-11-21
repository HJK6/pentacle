'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');

const {
  createNewChatForm,
  mountNewChatForm,
} = require('../renderer/next/new_chat');

const catalog = {
  catalog_version: 'fixture-catalog-v7',
  profiles: {
    desktop_manual: {
      claude: ['claude-default', 'medium'],
      codex: ['gpt-default', 'high'],
      gemini: ['gemini-default', 'low'],
    },
  },
  models: {
    claude: {
      'claude-default': { efforts: ['low', 'medium', 'high'] },
      'claude-fast': { efforts: ['low', 'medium'] },
    },
    codex: {
      'gpt-default': { efforts: ['low', 'high'] },
      'gpt-fast': { efforts: ['low', 'medium', 'high'] },
    },
    gemini: {
      'gemini-default': { efforts: ['low', 'medium'] },
    },
  },
  account_status: {
    accounts: [
      { host: 'host-a', provider: 'claude', alias: 'ours', active: true, enabled: true, health: 'healthy' },
      { host: 'host-a', provider: 'claude', alias: 'spare', active: true, enabled: true, health: 'healthy' },
      { host: 'host-b', provider: 'claude', alias: 'remote', active: true, enabled: true, health: 'healthy' },
      { host: 'host-a', provider: 'codex', alias: 'codex-ours', active: true, enabled: true, health: 'healthy' },
      { host: 'host-a', provider: 'claude', alias: 'disabled', active: false, enabled: true, health: 'healthy' },
      { host: 'host-a', provider: 'claude', alias: 'unhealthy', active: true, enabled: true, health: 'stale' },
    ],
  },
};

const accountLabels = {
  claude: {
    ours: 'operator@example.test',
    spare: 'second@example.test',
    remote: 'remote-user@example.test',
  },
  codex: { 'codex-ours': 'operator@example.test' },
};

const machineOptions = [
  { id: 'host-a', label: 'Host A' },
  { id: 'host-b', label: 'Host B' },
];

function formOptions(overrides = {}) {
  return {
    catalog,
    machineOptions,
    localHostId: 'host-a',
    accountLabels,
    spawn: async () => ({ ok: false, error: 'unused fixture spawn' }),
    ...overrides,
  };
}

function chooseClaude(form, { model = 'claude-default', account = 'ours' } = {}) {
  assert.equal(form.select('provider', 'claude'), true);
  assert.equal(form.select('model', model), true);
  assert.equal(form.select('account', account), true);
}

function clickTestId(root, testId) {
  const button = root.querySelector(`[data-testid="${testId}"]`);
  assert.ok(button, `button ${testId} exists`);
  button.click();
  return button;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

test('provider tiles use catalog counts and selections reveal catalog-backed choices', () => {
  const form = createNewChatForm(formOptions());

  assert.deepEqual(form.getChoices().providers, [
    { id: 'claude', label: 'Claude', modelCount: 2 },
    { id: 'codex', label: 'Codex', modelCount: 2 },
  ]);
  assert.equal(form.getState().machine, 'host-a', 'configured local host is preselected');
  assert.equal(form.getState().canStart, false);

  form.select('provider', 'claude');
  assert.deepEqual(form.getChoices().models, ['claude-default', 'claude-fast']);
  assert.deepEqual(form.getChoices().accounts, [
    { id: 'ours', label: 'operator' },
    { id: 'spare', label: 'second' },
  ]);
  assert.equal(form.getState().canStart, false);

  form.select('model', 'claude-default');
  assert.equal(form.getState().effort, 'medium', 'model choice applies the provider profile effort');
  assert.deepEqual(form.getChoices().efforts, ['low', 'medium', 'high']);
  assert.equal(form.getState().canStart, false, 'account is also required');

  form.select('account', 'ours');
  assert.equal(form.getState().canStart, true);
  form.select('machine', 'host-b');
  assert.equal(form.getState().machine, 'host-b');
  assert.equal(form.getState().account, null, 'a machine change clears an account unavailable there');
  assert.deepEqual(form.getChoices().accounts, [{ id: 'remote', label: 'remote-user' }]);
  assert.equal(form.getState().canStart, false);
});

test('changing provider clears model, effort and account while preserving machine', () => {
  const form = createNewChatForm(formOptions());
  chooseClaude(form);
  form.select('effort', 'high');
  form.select('machine', 'host-b');
  form.select('account', 'remote');
  assert.equal(form.getState().canStart, true);

  form.select('provider', 'codex');
  assert.deepEqual({
    provider: form.getState().provider,
    model: form.getState().model,
    effort: form.getState().effort,
    account: form.getState().account,
    machine: form.getState().machine,
  }, {
    provider: 'codex', model: null, effort: null, account: null, machine: 'host-b',
  });
  assert.equal(form.getState().canStart, false);
});

test('mounted form shows only Claude and Codex tiles, counts and the selected machine', () => {
  const dom = new JSDOM('<main id="form"></main>');
  const root = dom.window.document.getElementById('form');
  const view = mountNewChatForm(root, formOptions());

  assert.equal(root.querySelector('.page.setup .scol h1').textContent, 'New chat');
  assert.deepEqual([...root.querySelectorAll('[data-set="provider"] b')].map(node => node.textContent), ['Claude', 'Codex']);
  assert.deepEqual([...root.querySelectorAll('[data-set="provider"] span')].map(node => node.textContent), ['2 models', '2 models']);
  assert.equal(root.querySelector('[data-set="provider"][data-val="gemini"]'), null);
  assert.equal(root.querySelector('[data-testid="start-chat"]').disabled, true);

  clickTestId(root, 'set-provider-claude');
  assert.ok(root.querySelector('[data-set="model"]'));
  assert.ok(root.querySelector('[data-set="account"]'));
  assert.ok(root.querySelector('[data-set="host"][data-val="host-a"].on'));
  assert.equal(root.textContent.includes('ours'), false, 'account aliases never appear in the view');
  assert.equal(root.textContent.includes('operator'), true, 'configured username is shown');

  view.destroy();
  assert.equal(root.childElementCount, 0);
});

test('start sends one exact desktop spawn request and records the stream before mounting the chat', async () => {
  const events = [];
  let request;
  let form;
  form = createNewChatForm(formOptions({
    spawn: async value => {
      request = value;
      return { ok: true, streamId: 'host-a:spawn-1', session: { stream_id: 'host-a:spawn-1', state: 'ready' } };
    },
    onAdmitted: ({ streamId, state }) => {
      events.push(`admitted:${streamId}`);
      assert.equal(state.streamId, streamId);
    },
    onStarted: ({ session, streamId }) => events.push(`started:${streamId}:${session.state}`),
  }));
  chooseClaude(form);

  const result = await form.start();
  assert.deepEqual(request, {
    hostId: 'host-a',
    provider: 'claude',
    model: 'claude-default',
    effort: 'medium',
    account: 'ours',
    spawnProfile: 'desktop_manual',
    catalogVersion: 'fixture-catalog-v7',
    resolutionSource: 'profile_default',
  });
  assert.deepEqual(events, ['admitted:host-a:spawn-1', 'started:host-a:spawn-1:ready']);
  assert.equal(result.phase, 'started');
  assert.equal(result.streamId, 'host-a:spawn-1');
  assert.deepEqual(result.admittedStreamIds, ['host-a:spawn-1']);
});

test('Waiting disables every control and prevents a second spawn request', async () => {
  const arrival = deferred();
  let spawnCalls = 0;
  const dom = new JSDOM('<main id="form"></main>');
  const root = dom.window.document.getElementById('form');
  const view = mountNewChatForm(root, formOptions({
    spawn: () => { spawnCalls += 1; return arrival.promise; },
  }));
  clickTestId(root, 'set-provider-claude');
  clickTestId(root, 'set-model-claude-default');
  clickTestId(root, 'set-account-ours');
  assert.equal(root.querySelector('[data-testid="start-chat"]').disabled, false);

  const pending = view.start();
  assert.equal(view.getState().phase, 'waiting');
  assert.equal(root.querySelector('[data-testid="start-chat"]').textContent, 'Starting');
  assert.ok([...root.querySelectorAll('button')].every(button => button.disabled));
  await view.start();
  assert.equal(spawnCalls, 1);

  arrival.resolve({ ok: false, error: 'try later' });
  await pending;
  assert.equal(view.getState().phase, 'form');
  assert.equal(root.querySelector('[data-testid="start-chat"]').disabled, false);
  view.destroy();
});

test('resolved refusal and rejected transport restore the selected form with a one-line reason', async (t) => {
  for (const scenario of [
    { name: 'daemon refusal', spawn: async () => ({ ok: false, error: 'quota is full' }) },
    { name: 'transport rejection', spawn: async () => { throw new Error('socket unavailable'); } },
  ]) {
    await t.test(scenario.name, async () => {
      const dom = new JSDOM('<main id="form"></main>');
      const root = dom.window.document.getElementById('form');
      const view = mountNewChatForm(root, formOptions({ spawn: scenario.spawn }));
      clickTestId(root, 'set-provider-claude');
      clickTestId(root, 'set-model-claude-default');
      clickTestId(root, 'set-account-ours');

      await view.start();
      assert.equal(view.getState().phase, 'form');
      assert.equal(view.getState().provider, 'claude');
      assert.equal(view.getState().model, 'claude-default');
      assert.equal(view.getState().account, 'ours');
      assert.equal(view.getState().canStart, true);
      assert.equal(root.querySelector('[role="alert"]').textContent,
        scenario.name === 'daemon refusal' ? 'quota is full' : 'socket unavailable');
      assert.equal(root.querySelector('[data-testid="start-chat"]').disabled, false);

      clickTestId(root, 'set-effort-high');
      assert.equal(view.getState().error, '', 'the next choice clears the refusal line');
      assert.equal(root.querySelector('[role="alert"]'), null);
      view.destroy();
    });
  }
});

test('starting another attempt clears the previous refusal line', async () => {
  const retry = deferred();
  let calls = 0;
  const form = createNewChatForm(formOptions({
    spawn: () => {
      calls += 1;
      return calls === 1 ? Promise.resolve({ ok: false, error: 'first refusal' }) : retry.promise;
    },
  }));
  chooseClaude(form);

  await form.start();
  assert.equal(form.getState().error, 'first refusal');
  const pendingRetry = form.start();
  assert.equal(form.getState().phase, 'waiting');
  assert.equal(form.getState().error, '');
  retry.resolve({ ok: false, error: 'second refusal' });
  await pendingRetry;
  assert.equal(form.getState().error, 'second refusal');
});

test('ordinary ready reply wins over an earlier starting list frame and starts immediately', async () => {
  const started = [];
  const form = createNewChatForm(formOptions({
    spawn: async () => ({
      ok: true,
      streamId: 'host-a:ordinary-ready',
      session: { stream_id: 'host-a:ordinary-ready', state: 'ready', title: 'Ready chat' },
    }),
    onStarted: value => started.push(value),
  }));
  chooseClaude(form);
  form.updateSessions([{ stream_id: 'host-a:ordinary-ready', state: 'starting' }]);

  const result = await form.start();
  assert.equal(result.phase, 'started');
  assert.equal(result.session.title, 'Ready chat');
  assert.equal(started.length, 1);
  assert.equal(started[0].session.state, 'ready');
});

test('ordinary starting reply waits for the existing session list to report ready', async () => {
  const started = [];
  const form = createNewChatForm(formOptions({
    spawn: async () => ({
      ok: true,
      streamId: 'host-a:ordinary-starting',
      session: { stream_id: 'host-a:ordinary-starting', state: 'starting' },
    }),
    onStarted: value => started.push(value),
  }));
  chooseClaude(form);

  await form.start();
  assert.equal(form.getState().phase, 'waiting');
  assert.equal(form.getState().streamId, 'host-a:ordinary-starting');
  assert.equal(started.length, 0);
  form.updateSessions([{ stream_id: 'host-a:ordinary-starting', state: 'ready', title: 'Started' }]);
  assert.equal(form.getState().phase, 'started');
  assert.equal(started[0].session.title, 'Started');
});

test('queued reply follows queued and starting states until ready or started', async () => {
  const started = [];
  const form = createNewChatForm(formOptions({
    spawn: async () => ({
      ok: true,
      type: 'spawn.ok',
      state: 'queued',
      stream_id: 'host-a:queued-1',
      streamId: 'host-a:queued-1',
      request_id: 'request-1',
      spawn_request_id: 'spawn-request-1',
      queue_handle: 'queue-1',
    }),
    onStarted: value => started.push(value),
  }));
  chooseClaude(form);

  await form.start();
  assert.equal(form.getState().phase, 'waiting');
  assert.equal(form.getState().streamId, 'host-a:queued-1');
  assert.deepEqual(form.getState().admittedStreamIds, ['host-a:queued-1']);
  form.updateSessions([]);
  assert.equal(form.getState().phase, 'waiting', 'an absent stream before its first list row is not failure');
  form.updateSessions([{ stream_id: 'host-a:queued-1', bootstrap_state: 'queued' }]);
  assert.equal(form.getState().phase, 'waiting');
  form.updateSessions([{ stream_id: 'host-a:queued-1', state: 'starting' }]);
  assert.equal(form.getState().phase, 'waiting');
  form.updateSessions([{ stream_id: 'host-a:queued-1', state: 'started', title: 'Started chat' }]);
  assert.equal(form.getState().phase, 'started');
  assert.equal(started.length, 1);
  assert.equal(started[0].streamId, 'host-a:queued-1');
  assert.equal(started[0].session.title, 'Started chat');
});

test('a listed session with another or no status is treated as started', async (t) => {
  for (const [name, status] of [['other status', 'running'], ['missing status', undefined]]) {
    await t.test(name, async () => {
      const form = createNewChatForm(formOptions({
        spawn: async () => ({ ok: true, state: 'queued', streamId: 'host-a:other-status' }),
      }));
      chooseClaude(form);
      await form.start();

      form.updateSessions([{
        stream_id: 'host-a:other-status',
        ...(status ? { state: status } : {}),
      }]);
      assert.equal(form.getState().phase, 'started');
    });
  }
});

test('queued to starting to failed restores the form with the session reason', async () => {
  const started = [];
  const form = createNewChatForm(formOptions({
    spawn: async () => ({ ok: true, state: 'queued', streamId: 'host-a:failed-1' }),
    onStarted: value => started.push(value),
  }));
  chooseClaude(form);

  await form.start();
  form.updateSessions([{ stream_id: 'host-a:failed-1', state: 'starting' }]);
  form.updateSessions([{ stream_id: 'host-a:failed-1', state: 'failed', reason: 'Provider refused startup' }]);
  assert.equal(form.getState().phase, 'form');
  assert.equal(form.getState().error, 'Provider refused startup');
  assert.equal(form.getState().streamId, 'host-a:failed-1');
  assert.equal(form.getState().provider, 'claude');
  assert.equal(form.getState().canStart, true);
  assert.equal(started.length, 0);
});

test('queued to starting to absent restores the form with the fallback reason', async () => {
  const form = createNewChatForm(formOptions({
    spawn: async () => ({ ok: true, state: 'queued', stream_id: 'host-a:gone-1' }),
  }));
  chooseClaude(form);

  await form.start();
  form.updateSessions([{ stream_id: 'host-a:gone-1', state: 'starting' }]);
  form.updateSessions([]);
  assert.equal(form.getState().phase, 'form');
  assert.equal(form.getState().error, 'Failed to start chat');
  assert.equal(form.getState().streamId, 'host-a:gone-1');
});

test('mount consumes the existing store subscription and leaving does not cancel a waiting spawn', async () => {
  const arrival = deferred();
  const listeners = new Set();
  const storeState = { sessions: [] };
  const store = {
    getState: () => storeState,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
  const admitted = [];
  const started = [];
  let spawnCalls = 0;
  const dom = new JSDOM('<main id="form"></main>');
  const root = dom.window.document.getElementById('form');
  const view = mountNewChatForm(root, formOptions({
    store,
    spawn: () => { spawnCalls += 1; return arrival.promise; },
    onAdmitted: value => admitted.push(value.streamId),
    onStarted: value => started.push(value),
  }));
  view.select('provider', 'claude');
  view.select('model', 'claude-default');
  view.select('account', 'ours');
  const pending = view.start();
  assert.equal(view.getState().phase, 'waiting');
  assert.equal(spawnCalls, 1);

  view.leave();
  assert.equal(root.childElementCount, 0);
  assert.equal(listeners.size, 0, 'leaving unsubscribes from the shared store');
  assert.equal(spawnCalls, 1, 'leaving sends no cancellation or second request');
  arrival.resolve({ ok: true, streamId: 'host-a:left-1', session: { stream_id: 'host-a:left-1', state: 'ready' } });
  await pending;
  assert.deepEqual(admitted, ['host-a:left-1'], 'the admitted id remains available to cleanup');
  assert.equal(started.length, 0, 'a form left while waiting does not mount a chat card');
  assert.equal(view.getState().streamId, 'host-a:left-1');

  const fresh = mountNewChatForm(root, formOptions());
  assert.equal(fresh.getState().phase, 'form');
  assert.equal(fresh.getState().provider, null, 'reopening creates a fresh form');
  fresh.destroy();
});

test('store list frames drive a queued form to ready without a new daemon client', async () => {
  const listeners = new Set();
  const storeState = { sessions: [] };
  const store = {
    getState: () => storeState,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
  };
  const started = [];
  const dom = new JSDOM('<main id="form"></main>');
  const root = dom.window.document.getElementById('form');
  const view = mountNewChatForm(root, formOptions({
    store,
    spawn: async () => ({ ok: true, state: 'queued', streamId: 'host-a:store-1' }),
    onStarted: value => started.push(value),
  }));
  view.select('provider', 'claude');
  view.select('model', 'claude-default');
  view.select('account', 'ours');

  await view.start();
  assert.equal(view.getState().phase, 'waiting');
  for (const sessions of [
    [{ stream_id: 'host-a:store-1', state: 'queued' }],
    [{ stream_id: 'host-a:store-1', state: 'starting' }],
    [{ stream_id: 'host-a:store-1', state: 'ready', title: 'Store chat' }],
  ]) {
    storeState.sessions = sessions;
    for (const listener of listeners) listener();
  }
  assert.equal(view.getState().phase, 'started');
  assert.equal(started.length, 1);
  assert.equal(started[0].session.title, 'Store chat');
  view.destroy();
});
