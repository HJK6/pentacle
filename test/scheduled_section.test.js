const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const WebSocket = require('ws');

const {
  escapeHtml,
  formatRelative,
  hasScheduledContent,
  scheduledSectionHtml,
  editFieldsFromForm,
} = require('../renderer/scheduled_section');
const { registerScheduleIpcHandlers } = require('../main/schedule_ipc_bridge');
const { projectRecurringRow, projectRecurringInventory } = require('../main/schedule_projection');

const NOW = Date.parse('2026-10-20T20:00:00Z');

function rule(overrides = {}) {
  return {
    recurring_id: '11111111-1111-4111-8111-111111111111',
    name: 'Nightly retro',
    state: 'active',
    tz: 'America/Chicago',
    recurrence: { every: 'daily', at_time: '02:00', days: [], tz: 'America/Chicago' },
    recurrence_text: 'daily at 02:00 America/Chicago',
    next_fire_at_utc: '2026-10-21T07:00:00.000000Z',
    last_result: 'missed',
    last_result_reason: 'late',
    missed_count: 1,
    ...overrides,
  };
}

const oneShot = { schedule_id: 'sched-1', state: 'pending', prompt_preview: 'Check the dishwasher', fires_at_utc: '2026-10-20T22:00:00Z' };

function loadClientWithFakeWebSocket(FakeWebSocket) {
  const clientPath = require.resolve('../main/chat_stream_client');
  const wsPath = require.resolve('ws');
  const originalClientCache = require.cache[clientPath];
  const originalWsCache = require.cache[wsPath];
  delete require.cache[clientPath];
  require.cache[wsPath] = {
    id: wsPath,
    filename: wsPath,
    loaded: true,
    exports: FakeWebSocket,
  };
  const loaded = require('../main/chat_stream_client');
  delete require.cache[clientPath];
  if (originalClientCache) {
    require.cache[clientPath] = originalClientCache;
  }
  if (originalWsCache) {
    require.cache[wsPath] = originalWsCache;
  } else {
    delete require.cache[wsPath];
  }
  return loaded;
}

function makeFakeWebSocketClass() {
  class FakeWebSocket extends EventEmitter {
    constructor(url) {
      super();
      this.url = url;
      this.readyState = WebSocket.OPEN;
      this.sent = [];
      this.terminateCalls = 0;
      FakeWebSocket.instances.push(this);
    }

    send(raw) {
      this.sent.push(JSON.parse(raw));
    }

    ping() {}

    terminate() {
      this.terminateCalls += 1;
    }
  }
  FakeWebSocket.instances = [];
  FakeWebSocket.CONNECTING = WebSocket.CONNECTING;
  FakeWebSocket.OPEN = WebSocket.OPEN;
  FakeWebSocket.CLOSING = WebSocket.CLOSING;
  FakeWebSocket.CLOSED = WebSocket.CLOSED;
  return FakeWebSocket;
}

function openClient() {
  const frames = [];
  const FakeWebSocket = makeFakeWebSocketClass();
  const client = loadClientWithFakeWebSocket(FakeWebSocket);
  client._readToken = () => '';
  client.init(
    { chatStream: { heartbeatMs: 30000 } },
    (frame) => frames.push(frame),
  );
  const ws = FakeWebSocket.instances[0];
  ws.emit('open');
  sendFrame(ws, { type: 'welcome' });
  return { client, emitted: frames, frames, ws, FakeWebSocket };
}

function sendFrame(ws, frame) {
  ws.emit('message', Buffer.from(JSON.stringify(frame)));
}


test('section lists rules first, in their zone with relative time, then pending one-shots', () => {
  const html = scheduledSectionHtml({
    recurring: [rule()],
    schedules: [oneShot, { ...oneShot, schedule_id: 'sched-done', state: 'fired' }],
    nowMs: NOW, localTz: 'America/Chicago',
  });
  assert.ok(html.indexOf('data-recurring-id') < html.indexOf('data-schedule-id'));
  assert.match(html, /Nightly retro/);
  assert.match(html, /daily at 02:00 America\/Chicago/);
  assert.match(html, /Next: Wed, Oct 21, 02:00 CDT \(in 11h 0m\)/);
  assert.match(html, /Last: missed: late/);
  assert.match(html, /Check the dishwasher/);
  assert.doesNotMatch(html, /sched-done/);
  assert.match(html, /data-scheduled-action="pause"[^>]*>Pause/);
  assert.match(html, /data-scheduled-action="edit"/);
  // One-shot rows are read-only: only recurring rules carry actions.
  const oneShotRow = html.slice(html.indexOf('scheduled-oneshot'));
  assert.doesNotMatch(oneShotRow, /data-scheduled-action/);
});

test('paused rule offers Resume and shows no next run; hidden rules are still listed', () => {
  const html = scheduledSectionHtml({ recurring: [rule({ state: 'paused', visibility: 'hidden' })], schedules: [], nowMs: NOW });
  assert.match(html, /data-scheduled-action="resume"[^>]*>Resume/);
  assert.match(html, /Next: paused/);
  assert.match(html, /scheduled-badge-paused/);
});

test('edit form carries the five editable fields and escapes rule text', () => {
  const html = scheduledSectionHtml({
    recurring: [rule({ name: '<img src=x onerror=1>', recurrence: { every: 'weekly', at_time: '08:00', days: ['mon', 'wed'], tz: 'UTC' } })],
    schedules: [], nowMs: NOW, editingId: '11111111-1111-4111-8111-111111111111',
  });
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /name="name"/);
  assert.match(html, /<option value="weekly" selected>/);
  assert.match(html, /name="at_time" type="time" value="08:00"/);
  assert.match(html, /name="day" value="mon" checked/);
  assert.match(html, /name="day" value="tue">/);
  assert.match(html, /name="tz" type="text" value="UTC"/);
  assert.match(html, /data-scheduled-action="save"/);
  assert.equal(escapeHtml('<&">'), '&lt;&amp;&quot;&gt;');
});

test('edit fields drop days unless the rule is weekly', () => {
  const form = (every) => ({
    elements: { name: { value: ' Retro ' }, every: { value: every }, at_time: { value: '03:30' }, tz: { value: 'UTC' } },
    querySelectorAll: () => [{ value: 'mon' }, { value: 'fri' }],
  });
  assert.deepEqual(editFieldsFromForm(form('weekly')),
    { name: 'Retro', every: 'weekly', at_time: '03:30', tz: 'UTC', days: ['mon', 'fri'] });
  assert.deepEqual(editFieldsFromForm(form('daily')).days, []);
});

test('content and relative-time helpers', () => {
  assert.equal(hasScheduledContent({ recurring: [], schedules: [{ state: 'fired' }] }), false);
  assert.equal(hasScheduledContent({ recurring: [], schedules: [oneShot] }), true);
  assert.equal(hasScheduledContent({ recurring: [rule()], schedules: [] }), true);
  assert.equal(formatRelative('2026-10-20T20:30:00Z', NOW), 'in 30m');
  assert.equal(formatRelative('2026-10-20T19:00:00Z', NOW), '1h 0m ago');
  assert.equal(formatRelative('2026-10-23T20:00:00Z', NOW), 'in 3d');
});

test('projection keeps valid rules without the prompt body and orders by next fire', () => {
  assert.equal(projectRecurringRow({ recurring_id: 'x', state: 'weird' }), null);
  assert.equal(projectRecurringRow(null), null);
  assert.equal(Object.hasOwn(projectRecurringRow({ ...rule(), initial_prompt_b64: 'c2VjcmV0' }), 'initial_prompt_b64'), false);
  const rows = projectRecurringInventory([
    rule({ recurring_id: 'b', next_fire_at_utc: '2026-10-22T07:00:00Z' }),
    rule({ recurring_id: 'a', next_fire_at_utc: '2026-10-21T07:00:00Z' }),
    { recurring_id: 'bad', state: 'nope' },
  ]);
  assert.deepEqual(rows.map((row) => row.recurring_id), ['a', 'b']);
});

test('recurring IPC handlers forward pause, resume and update to the stream client', async () => {
  const handlers = new Map();
  const calls = [];
  const client = {
    recurringPause: (id) => { calls.push(['pause', id]); return Promise.resolve({ recurring: rule({ state: 'paused' }) }); },
    recurringResume: (id) => { calls.push(['resume', id]); return Promise.resolve({ recurring: rule() }); },
    recurringUpdate: (id, fields) => { calls.push(['update', id, fields]); return Promise.resolve({ recurring: rule() }); },
  };
  registerScheduleIpcHandlers({ handle: (channel, fn) => handlers.set(channel, fn) }, client);
  assert.equal((await handlers.get('chat-stream:recurring-pause')(null, 'r1')).recurring.state, 'paused');
  assert.equal((await handlers.get('chat-stream:recurring-resume')(null, 'r1')).ok, true);
  await handlers.get('chat-stream:recurring-update')(null, { recurringId: 'r1', fields: { at_time: '03:30' } });
  assert.deepEqual(calls, [['pause', 'r1'], ['resume', 'r1'], ['update', 'r1', { at_time: '03:30' }]]);
  const failing = new Map();
  registerScheduleIpcHandlers({ handle: (channel, fn) => failing.set(channel, fn) }, {
    recurringPause: () => Promise.reject({ error: 'not_found' }),
  });
  assert.deepEqual(await failing.get('chat-stream:recurring-pause')(null, 'r1'), { ok: false, error: 'not_found' });
});

test('stream client ingests the snapshot recurring key and recurring lifecycle frames', (t) => {
  const { client, ws, frames } = openClient();
  t.after(() => client.destroy());
  sendFrame(ws, { type: 'snapshot', events: [], sessions: [], schedules: [], recurring: [rule()] });
  assert.deepEqual(client.snapshot().recurring.map((row) => row.recurring_id), [rule().recurring_id]);
  assert.deepEqual(frames.find((frame) => frame.type === 'snapshot').recurring.map((row) => row.name), ['Nightly retro']);

  sendFrame(ws, { type: 'schedule.lifecycle', event: 'recurring_paused', recurring: rule({ state: 'paused' }) });
  assert.equal(client.snapshot().recurring[0].state, 'paused');
  assert.equal(frames.at(-1).type, 'recurring.inventory');
  assert.equal(frames.at(-1).recurring[0].state, 'paused');

  sendFrame(ws, { type: 'schedule.lifecycle', event: 'recurring_deleted', recurring: rule({ state: 'paused' }) });
  assert.deepEqual(client.snapshot().recurring, []);
  assert.equal(frames.at(-1).type, 'recurring.inventory');
  assert.deepEqual(frames.at(-1).recurring, []);
});

test('stream client sends pause, resume and update with only the editable fields', async (t) => {
  const { client, ws } = openClient();
  t.after(() => client.destroy());
  sendFrame(ws, { type: 'snapshot', events: [], sessions: [], schedules: [], recurring: [rule()] });
  const id = rule().recurring_id;
  const reply = (frame, extra = {}) => sendFrame(ws, { type: `${frame.type}.ok`, request_id: frame.request_id, recurring: rule(extra) });
  const pause = client.recurringPause(id);
  let sent = ws.sent.at(-1);
  assert.equal(sent.type, 'schedule.recurring.pause');
  assert.equal(sent.recurring_id, id);
  reply(sent, { state: 'paused' });
  assert.equal((await pause).recurring.state, 'paused');
  const update = client.recurringUpdate(id, { at_time: '03:30', provider: 'claude', prompt: 'x', days: [] });
  sent = ws.sent.at(-1);
  assert.equal(sent.type, 'schedule.recurring.update');
  assert.deepEqual(
    Object.fromEntries(Object.entries(sent).filter(([key]) => !['type', 'request_id', 'recurring_id'].includes(key))),
    { at_time: '03:30', days: [] },
  );
  reply(sent);
  await update;
});
