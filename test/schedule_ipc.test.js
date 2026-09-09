const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const { registerScheduleIpcHandlers } = require('../main/schedule_ipc_bridge');

test('preload schedule API forwards renderer calls to schedule IPC channels', async () => {
  const calls = [];
  const ipcRenderer = {
    invoke(channel, ...args) {
      calls.push({ channel, args });
      return Promise.resolve({ ok: true, channel, args });
    },
    send() {},
    on() {},
    removeAllListeners() {},
  };
  const context = {
    require(name) {
      if (name === 'electron') return { clipboard: { writeText() {} }, ipcRenderer };
      if (name === './config-loader') return { loadConfig: () => ({ config: {} }) };
      return require(name);
    },
    window: {},
    process,
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8'), context, {
    filename: 'preload.js',
  });

  await context.window.cc.scheduleRun('sched-1');
  await context.window.cc.scheduleCancel('sched-2');
  await context.window.cc.scheduleReschedule('sched-3', { at: '2026-05-21T00:00:00Z' });
  await context.window.cc.scheduleGet('sched-4');

  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    { channel: 'chat-stream:schedule-run', args: ['sched-1'] },
    { channel: 'chat-stream:schedule-cancel', args: ['sched-2'] },
    { channel: 'chat-stream:schedule-reschedule', args: [{ scheduleId: 'sched-3', firesAtUtc: '2026-05-21T00:00:00Z' }] },
    { channel: 'chat-stream:schedule-get', args: ['sched-4'] },
  ]);
});

test('main schedule IPC handlers forward arguments to chatStreamClient', async () => {
  const handlers = new Map();
  const calls = [];
  const ipcMain = {
    handle(channel, handler) {
      handlers.set(channel, handler);
    },
  };
  const chatStreamClient = {
    scheduleRun(scheduleId) {
      calls.push(['run', scheduleId]);
      return Promise.resolve({ schedule_id: scheduleId });
    },
    scheduleCancel(scheduleId) {
      calls.push(['cancel', scheduleId]);
      return Promise.resolve({ schedule_id: scheduleId });
    },
    scheduleReschedule(scheduleId, firesAtUtc) {
      calls.push(['reschedule', scheduleId, firesAtUtc]);
      return Promise.resolve({ schedule: { schedule_id: scheduleId, fires_at_utc: firesAtUtc } });
    },
    scheduleGet(scheduleId) {
      calls.push(['get', scheduleId]);
      return Promise.resolve({ schedule: { schedule_id: scheduleId, state: 'pending' } });
    },
  };

  registerScheduleIpcHandlers(ipcMain, chatStreamClient, (error) => String(error.message || error));

  assert.equal((await handlers.get('chat-stream:schedule-run')(null, 'sched-1')).reply.schedule_id, 'sched-1');
  assert.equal((await handlers.get('chat-stream:schedule-cancel')(null, 'sched-2')).reply.schedule_id, 'sched-2');
  assert.deepEqual(
    (await handlers.get('chat-stream:schedule-reschedule')(null, {
      scheduleId: 'sched-3',
      firesAtUtc: '2026-05-21T00:00:00Z',
    })).schedule,
    { schedule_id: 'sched-3', fires_at_utc: '2026-05-21T00:00:00Z' },
  );
  assert.deepEqual(
    (await handlers.get('chat-stream:schedule-get')(null, 'sched-4')).schedule,
    { schedule_id: 'sched-4', state: 'pending' },
  );
  assert.deepEqual(calls, [
    ['run', 'sched-1'],
    ['cancel', 'sched-2'],
    ['reschedule', 'sched-3', '2026-05-21T00:00:00Z'],
    ['get', 'sched-4'],
  ]);

  const mainJs = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  assert.match(mainJs, /registerScheduleIpcHandlers\(ipcMain, chatStreamClient, normalizeChatStreamError\)/);
});
