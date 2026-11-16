const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const { registerScheduleIpcHandlers } = require('../main/schedule_ipc_bridge');

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

  const source = fs.readFileSync(path.join(__dirname, '..', 'main', 'cc_handlers.js'), 'utf8');
  assert.match(source, /registerScheduleIpcHandlers\(target, chatStreamClient, normalizeChatStreamError\)/);
});
