'use strict';
const assert = require('node:assert/strict');
// Sanitized mobile parity vectors. The fixed day is synthetic, 28 years after the oracle
// fixture, preserving its weekdays, leap-year boundaries, ids, ordering and metadata.
const TODAY = '2054-10-06';
const item = over => ({ priority: 'med', due_date: null, position: over.id, category: null,
  scope: 'private', created_by: 'app', done_at: null, routine_id: null, ...over });
const event = over => ({ time: null, who: 'self', location: null, star: false,
  scope: 'private', created_by: 'app', ...over });
const emptyLists = () => ({ tasks: [], grocery: [], meals: [], chores: [], study: [] });
function fixtureSnapshot() {
  const task = (id, over = {}) => item({ id, list: 'tasks', label: `Synthetic task ${id}`, ...over });
  const dated = (id, date, over = {}) => event({ id, date, title: `Synthetic event ${id}`, ...over });
  return { today: TODAY, month: '2054-10', server_now: `${TODAY}T23:39:00Z`, people: { partner: 'Partner Fixture' },
    lists: {
      tasks: [task(1, { due_date: TODAY, created_by: 'assistant' }), task(2, { priority: 'hi', created_by: 'assistant' }),
        task(3), task(4, { priority: 'hi', due_date: '2054-10-20', scope: 'shared', created_by: 'partner_assistant' }),
        task(5, { priority: 'lo' }), task(6, { due_date: '2054-10-07' })],
      grocery: [10, 11, 12, 13].map(id => item({ id, list: 'grocery', label: `Synthetic grocery ${id}`,
        ...(id === 10 ? { due_date: TODAY, scope: 'shared', created_by: 'partner_assistant' } : {}) })),
      meals: [30, 31, 32].map(id => item({ id, list: 'meals', label: `Synthetic meal ${id}` })),
      chores: [item({ id: 20, list: 'chores', label: 'Synthetic recurring chore', due_date: '2054-10-04', routine_id: 7 }),
        item({ id: 21, list: 'chores', label: 'Synthetic chore' })], study: [],
    },
    events: [dated(101, TODAY, { who: 'both', scope: 'shared', created_by: 'partner_assistant' }),
      dated(102, TODAY, { time: '09:30' }), dated(103, TODAY, { time: '14:30', who: 'both', created_by: 'assistant' }),
      dated(104, TODAY, { time: '18:00', created_by: 'assistant' }), dated(105, '2054-10-05', { time: '12:00' }),
      dated(106, '2054-10-07', { time: '11:00', who: 'partner', scope: 'shared', created_by: 'partner_assistant' }),
      dated(107, '2054-10-08', { time: '16:00' }), dated(108, '2054-10-08', { time: '08:00' }),
      dated(109, '2054-10-09', { time: '19:30', who: 'both' }), dated(110, '2054-10-13', { time: '10:00', created_by: 'assistant' }),
      dated(111, '2054-10-14', { time: '10:00' }), dated(112, '2054-10-15', { time: '15:00', who: 'partner' }),
      dated(113, '2054-10-21', { time: '09:00', created_by: 'assistant' }), dated(114, '2054-10-21', { time: '14:00' })],
  };
}
const FIELDS = { 'household.snapshot': [[], ['month']], 'household.item.add': [['label', 'list']],
  'household.item.done': [['item_id']], 'household.item.remove': [['item_id']],
  'household.event.add': [['date', 'time', 'title', 'who']], 'household.event.remove': [['event_id']] };
function guard(verb, fields) {
  assert.ok(FIELDS[verb], `Unexpected verb: ${verb}`);
  const keys = Object.keys(fields).sort();
  assert.ok(FIELDS[verb].some(allowed => JSON.stringify(allowed) === JSON.stringify(keys)), `Unexpected ${verb} fields: ${keys}`);
}
const rpcError = error_code => ({ ok: false, ...(error_code ? { error_code } : {}), error: 'Synthetic failure' });
class FakeHousehold {
  constructor(snapshot = fixtureSnapshot()) { this.snapshot = structuredClone(snapshot); this.calls = []; this.overrides = new Map(); this.nextId = 1000; }
  handle = async (verb, fields = {}) => {
    guard(verb, fields); this.calls.push({ verb, fields: structuredClone(fields) });
    return this.overrides.has(verb) ? this.overrides.get(verb)(fields, this) : this.apply(verb, fields);
  };
  async apply(verb, fields) {
    const s = this.snapshot; let reply = {};
    if (verb === 'household.snapshot') {
      const month = fields.month || s.today.slice(0, 7);
      const last = new Date(Date.parse(`${s.today}T00:00:00Z`) + 7 * 86400000).toISOString().slice(0, 10);
      reply = { ...structuredClone(s), month, events: structuredClone(s.events.filter(row =>
        row.date.slice(0, 7) === month || (row.date >= s.today && row.date <= last))) };
    }
    else if (verb === 'household.item.add') { const row = item({ id: this.nextId++, list: fields.list, label: fields.label }); s.lists[fields.list].push(row); reply = { item: row }; }
    else if (verb === 'household.item.done' || verb === 'household.item.remove') {
      const list = Object.keys(s.lists).find(key => s.lists[key].some(row => row.id === fields.item_id));
      if (!list) return rpcError('not_found');
      s.lists[list] = s.lists[list].filter(row => row.id !== fields.item_id);
    } else if (verb === 'household.event.add') { const row = event({ id: this.nextId++, ...fields }); s.events.push(row); reply = { event: row }; }
    else if (verb === 'household.event.remove') s.events = s.events.filter(row => row.id !== fields.event_id);
    return { ok: true, reply: { type: `${verb}.ok`, server_now: s.server_now, ...reply } };
  }
  reads() { return this.calls.filter(c => c.verb === 'household.snapshot'); }
  mutations() { return this.calls.filter(c => c.verb !== 'household.snapshot'); }
}
const flush = async () => { for (let n = 0; n < 20; n++) await Promise.resolve(); };
function fakeClock() {
  let now = 0, id = 0; const timers = new Map();
  return { now: () => now, timers,
    setTimeout(fn, delay) { const key = ++id; timers.set(key, { at: now + delay, fn }); return key; },
    clearTimeout(key) { timers.delete(key); },
    async advance(ms) { const end = now + ms; await flush();
      while (true) { const due = [...timers].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at || a[0] - b[0]);
        if (!due.length) break; const [key, timer] = due[0]; now = timer.at; timers.delete(key); timer.fn(); await flush(); }
      now = end; await flush(); },
  };
}
module.exports = { TODAY, item, event, emptyLists, fixtureSnapshot, FakeHousehold, guard, rpcError, fakeClock, flush };
