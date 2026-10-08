'use strict';
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

const LISTS = ['tasks', 'grocery', 'meals', 'chores', 'study'];
const dayOffset = (day, amount) => new Date(Date.parse(`${day}T12:00:00Z`) + amount * 86400000).toISOString().slice(0, 10);
function chicagoDay(now = new Date()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', { timeZone: 'America/Chicago', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(now).map(x => [x.type, x.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}
// Use the pinned daemon's vocabulary without copying any private identity literal
// into test source. Request constants remain in harness memory; bearer tokens
// are never retained in the call log, only their presence is recorded.
function readCosmoVocabulary() {
  const source = fs.readFileSync(path.join(__dirname, '../../../services/chat-stream-v2/household.py'), 'utf8');
  return Object.fromEntries(Object.entries({ assistant: 'COSMO_ASSISTANT', app: 'COSMO_APP', partnerWho: 'COSMO_PARTNER_WHO', bothWho: 'COSMO_BOTH_WHO' }).map(([key, name]) => {
    const match = source.match(new RegExp(`^${name} = ("[^"\\n]+")`, 'm'));
    if (!match) throw new Error(`Fixture cannot read daemon constant ${name}`);
    return [key, JSON.parse(match[1])];
  }));
}
function createSeed(today, vocabulary) {
  let id = 1;
  const lists = Object.fromEntries(LISTS.map(list => [list, [0, 1].map(i => ({ id: id++, list,
    label: `Synthetic ${list} ${i + 1}`, priority: i === 0 ? 'hi' : 'lo', due_date: null,
    position: i, category: null, scope: 'operator', created_by: vocabulary.app,
    done_at: null, routine_id: null }))]));
  lists.tasks[0].due_date = dayOffset(today, -1);
  lists.meals[0].due_date = dayOffset(today, 1);
  lists.grocery[0].created_by = vocabulary.assistant;
  Object.assign(lists.grocery[1], { priority: 'hi', due_date: today, scope: 'shared', created_by: 'partner_assistant' });
  const event = (eventId, date, time, title, who, extra = {}) => ({ id: eventId, date, time, title, who,
    location: null, star: false, scope: 'operator', created_by: vocabulary.app, ...extra });
  const assistantDay = today.endsWith('-01') ? dayOffset(today, 1) : `${today.slice(0, 7)}-01`;
  const events = [
    event(101, today, null, 'Synthetic all-day', 'operator'),
    event(102, today, '14:30', 'Synthetic timed', 'operator', { created_by: vocabulary.assistant }),
    event(103, dayOffset(today, 1), null, 'Synthetic partner', vocabulary.partnerWho),
    event(104, dayOffset(today, 2), '09:00', 'Synthetic shared', vocabulary.bothWho, { scope: 'shared' }),
    event(105, assistantDay, '10:00', 'Synthetic assistant day', 'operator', { created_by: vocabulary.assistant }),
  ];
  return { today, month: today.slice(0, 7), assistantDay, lists, events, server_now: `${today}T12:00:00Z` };
}
function keysAre(value, keys) { return value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).sort().join(',') === [...keys].sort().join(','); }
function createFakeCosmo({ today = chicagoDay(), vocabulary = readCosmoVocabulary(), sleep = ms => new Promise(resolve => setTimeout(resolve, ms)) } = {}) {
  const seed = createSeed(today, vocabulary);
  const calls = [];
  const delays = [];
  let nextId = 1000;
  const result = (status, body = null) => ({ status, body });
  async function dispatch({ method, url, body = null, authorization = '' }) {
    const parsed = new URL(url, 'http://127.0.0.1');
    const route = parsed.pathname;
    if (method === 'GET' && route === '/__fixture/calls') return result(200, { calls: structuredClone(calls) });
    if (method === 'POST' && route === '/__fixture/delay') {
      if (!keysAre(body, ['method', 'path', 'ms']) || body.method !== 'POST' || body.path !== '/events' || !Number.isInteger(body.ms) || body.ms < 0 || body.ms > 10000) return result(400);
      delays.push({ ...body }); return result(200, { ok: true });
    }
    calls.push({ method, path: route, query: Object.fromEntries(parsed.searchParams), body: body === null ? null : structuredClone(body), authPresent: /^Bearer \S+$/.test(authorization) });
    if (!/^Bearer \S+$/.test(authorization)) return result(401);
    const delay = delays.findIndex(x => x.method === method && x.path === route);
    if (delay >= 0) await sleep(delays.splice(delay, 1)[0].ms);
    const listMatch = route.match(/^\/lists\/(tasks|grocery|meals|chores|study)\/items$/);
    if (listMatch) {
      const list = listMatch[1];
      if (parsed.search) return result(400);
      if (method === 'GET' && body === null) return result(200, { items: structuredClone(seed.lists[list]), server_now: seed.server_now });
      if (method === 'POST') {
        if (!keysAre(body, ['label']) || typeof body.label !== 'string' || !body.label.trim()) return result(400);
        const row = { id: nextId++, list, label: body.label, priority: 'med', due_date: null, position: seed.lists[list].length, category: null, scope: 'operator', created_by: vocabulary.app, done_at: null, routine_id: null };
        seed.lists[list].push(row); return result(201, { ...row, server_now: seed.server_now });
      }
    }
    const itemMatch = route.match(/^\/lists\/items\/(\d+)(\/done)?$/);
    if (itemMatch && ((method === 'PATCH' && itemMatch[2]) || (method === 'DELETE' && !itemMatch[2]))) {
      if (body !== null || parsed.search) return result(400);
      const row = Object.values(seed.lists).flat().find(x => x.id === Number(itemMatch[1]));
      if (!row) return result(404);
      if (method === 'PATCH') { row.done_at = seed.server_now; return result(200, { ...row, server_now: seed.server_now }); }
      seed.lists[row.list] = seed.lists[row.list].filter(x => x.id !== row.id); return result(204);
    }
    if (route === '/events') {
      if (method === 'GET') {
        if (body !== null || parsed.searchParams.size !== 2 || !keysAre(Object.fromEntries(parsed.searchParams), ['from', 'to'])) return result(400);
        const from = parsed.searchParams.get('from'), to = parsed.searchParams.get('to');
        return result(200, { items: structuredClone(seed.events.filter(x => x.date >= from && x.date <= to)), server_now: seed.server_now });
      }
      if (method === 'POST') {
        if (parsed.search || !keysAre(body, ['date', 'time', 'title', 'who']) || !['operator', vocabulary.partnerWho, vocabulary.bothWho].includes(body.who)) return result(400);
        const row = { id: nextId++, ...body, location: null, star: false, scope: 'operator', created_by: vocabulary.app };
        seed.events.push(row); return result(201, { ...row, server_now: seed.server_now });
      }
    }
    const eventMatch = route.match(/^\/events\/(\d+)$/);
    if (eventMatch && method === 'DELETE') {
      if (body !== null || parsed.search) return result(400);
      if (!seed.events.some(x => x.id === Number(eventMatch[1]))) return result(404);
      seed.events = seed.events.filter(x => x.id !== Number(eventMatch[1])); return result(204);
    }
    return result(404);
  }
  return { seed, calls, vocabulary, dispatch };
}
async function startFakeCosmo(options) {
  const fixture = createFakeCosmo(options);
  const server = http.createServer(async (req, res) => {
    try {
      const chunks = []; let bytes = 0;
      for await (const chunk of req) { bytes += chunk.length; if (bytes > 16384) { res.writeHead(413).end(); return; } chunks.push(chunk); }
      const raw = Buffer.concat(chunks).toString('utf8');
      const reply = await fixture.dispatch({ method: req.method, url: req.url, body: raw ? JSON.parse(raw) : null, authorization: req.headers.authorization || '' });
      if (!res.destroyed) { res.writeHead(reply.status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(reply.body === null ? '' : JSON.stringify(reply.body)); }
    } catch { if (!res.destroyed) res.writeHead(400).end(); }
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  return { ...fixture, url: `http://127.0.0.1:${server.address().port}`,
    close: () => new Promise((resolve, reject) => { server.close(error => error ? reject(error) : resolve()); server.closeAllConnections(); }) };
}
module.exports = { LISTS, chicagoDay, dayOffset, readCosmoVocabulary, createSeed, createFakeCosmo, startFakeCosmo };
