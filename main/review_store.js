'use strict';

// The two writers share a transaction lock, but never write each other's file.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { TextDecoder } = require('node:util');

const ITEM_CAP = 1024 * 1024;
const ACK_CAP = 8 * 1024 * 1024;
const ID = /^ri_[0-9a-f]{32}$/;
const SOURCE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const RUN = /^[0-9a-f]{32}$/;
const PROJECTS = Object.freeze(['Agenteach', 'DUMBO', 'Pentacle', 'Personal', 'Other']);
const validProject = value => PROJECTS.includes(value);
const FORBIDDEN = /[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069<>]/u;
const sleepArray = new Int32Array(new SharedArrayBuffer(4));

class ReviewError extends Error {
  constructor(code, message = code, status = 503, exit = 4) {
    super(message); this.code = code; this.status = status; this.exit = exit;
  }
}
const invalid = (code = 'invalid_input') => { throw new ReviewError(code, code, 422, 2); };
const conflict = (code = 'conflict') => { throw new ReviewError(code, code, 409, 3); };
const storeInvalid = () => { throw new ReviewError('store_invalid'); };
const own = (x, k) => Object.prototype.hasOwnProperty.call(x, k);
const object = x => x !== null && typeof x === 'object' && !Array.isArray(x);
const keys = (x, required, optional = []) => object(x) && required.every(k => own(x, k))
  && Object.keys(x).every(k => required.includes(k) || optional.includes(k));
function iso(x) {
  if (typeof x !== 'string') return false;
  const m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d{1,3}))?(Z|[+-]\d\d:\d\d)$/.exec(x);
  if (!m) return false;
  const [year, month, day, hour, minute, second] = m.slice(1, 7).map(Number);
  if (hour > 23 || minute > 59 || second > 59) return false;
  const local = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
  if (local.getUTCFullYear() !== year || local.getUTCMonth() + 1 !== month || local.getUTCDate() !== day) return false;
  return Number.isFinite(Date.parse(x));
}
function plain(x, max, min = 1) {
  return typeof x === 'string' && x === x.trim() && [...x].length >= min && [...x].length <= max && !FORBIDDEN.test(x);
}
function validLink(x) {
  if (typeof x !== 'string' || x.length > 2048) return false;
  try { const u = new URL(x); return u.protocol === 'https:' && !!u.hostname && !u.username && !u.password; } catch { return false; }
}
function validSource(x) {
  return object(x) && Object.keys(x).every(k => ['dispatch_id', 'reply_to_message_id'].includes(k)
    && typeof x[k] === 'string' && SOURCE_ID.test(x[k]));
}
function validItem(x) {
  if (!keys(x, ['id', 'kind', 'title', 'created_at', 'source'],
    ['first_raised_at', 'origin_note', 'link', 'resolved_at', 'resolution_reason', 'smoke', 'project'])) return false;
  if (!ID.test(x.id) || !['review', 'answer', 'input'].includes(x.kind) || !plain(x.title, 120)
    || !iso(x.created_at) || !validSource(x.source)) return false;
  if (own(x, 'first_raised_at') && x.first_raised_at !== null && !iso(x.first_raised_at)) return false;
  if (own(x, 'origin_note') && !plain(x.origin_note, 200)) return false;
  if (own(x, 'link') && !validLink(x.link)) return false;
  if (own(x, 'project') && !validProject(x.project)) return false;
  if (own(x, 'resolved_at') !== own(x, 'resolution_reason')) return false;
  if (own(x, 'resolved_at') && (!iso(x.resolved_at) || !plain(x.resolution_reason, 200))) return false;
  if (own(x, 'smoke') && (!keys(x.smoke, ['run_id']) || !RUN.test(x.smoke.run_id))) return false;
  return true;
}
function validItems(x) {
  if (!keys(x, ['schema', 'updated_at', 'items'], ['seed_marker', 'seed_keys'])
    || x.schema !== 'lews-review-items/1' || !iso(x.updated_at) || !Array.isArray(x.items)
    || x.items.length > 2000 || !x.items.every(validItem)) return false;
  if (new Set(x.items.map(i => i.id)).size !== x.items.length) return false;
  if (own(x, 'seed_marker') !== own(x, 'seed_keys')) return false;
  if (own(x, 'seed_marker')) {
    if (!/^sha256:[0-9a-f]{64}$/.test(x.seed_marker) || !object(x.seed_keys)) return false;
    for (const [id, hash] of Object.entries(x.seed_keys)) {
      if (!ID.test(id) || !/^[0-9a-f]{64}$/.test(hash) || id !== `ri_${hash.slice(0, 32)}`
        || !x.items.some(i => i.id === id)) return false;
      const item = x.items.find(i => i.id === id);
      const sourceText = item.source.dispatch_id ? `dispatch ${item.source.dispatch_id}` : item.origin_note || '';
      const derived = crypto.createHash('sha256').update([item.kind, item.title.normalize('NFC'), item.link || '', sourceText].join('\0')).digest('hex');
      if (derived !== hash) return false;
    }
  }
  return true;
}
function validResult(x) {
  return keys(x, ['changed', 'seq_seen', 'acked', 'acked_at', 'state'])
    && typeof x.changed === 'boolean' && Number.isSafeInteger(x.seq_seen) && x.seq_seen >= 0
    && typeof x.acked === 'boolean' && (x.acked_at === null || iso(x.acked_at))
    && x.state === (x.acked ? 'done' : 'pending');
}
function validAcks(x) {
  if (!keys(x, ['schema', 'updated_at', 'next_seq', 'entries', 'receipts'])
    || x.schema !== 'lews-review-acks/2' || !iso(x.updated_at)
    || !Number.isSafeInteger(x.next_seq) || x.next_seq < 1 || !object(x.entries)
    || !Array.isArray(x.receipts) || Object.keys(x.entries).length > 2000 || x.receipts.length > 20000) return false;
  let histories = 0;
  for (const [id, entry] of Object.entries(x.entries)) {
    if (!ID.test(id) || !keys(entry, ['acked', 'acked_at', 'seq', 'history'])
      || typeof entry.acked !== 'boolean' || (entry.acked_at !== null && !iso(entry.acked_at))
      || !Number.isSafeInteger(entry.seq) || entry.seq < 1 || entry.seq >= x.next_seq
      || !Array.isArray(entry.history) || !entry.history.length) return false;
    histories += entry.history.length;
    if (histories > 50000 || !object(entry.history.at(-1)) || entry.history.at(-1).seq !== entry.seq
      || entry.acked !== (entry.history.at(-1).op === 'ack')) return false;
    let lastSeq = 0, lastAck = null;
    for (const h of entry.history) {
      if (!keys(h, ['seq', 'op', 'at', 'op_id'])
        || !Number.isSafeInteger(h.seq) || h.seq <= lastSeq || h.seq >= x.next_seq
        || !['ack', 'unack'].includes(h.op) || !iso(h.at) || !UUID.test(h.op_id)) return false;
      lastSeq = h.seq;
      if (h.op === 'ack') lastAck = h.at;
    }
    if (entry.acked_at !== lastAck) return false;
  }
  const seen = new Set();
  for (const r of x.receipts) {
    if (!keys(r, ['op_id', 'id', 'op', 'accepted_at', 'result']) || !UUID.test(r.op_id)
      || !ID.test(r.id) || !['ack', 'unack'].includes(r.op) || !iso(r.accepted_at)
      || !validResult(r.result) || seen.has(r.op_id)) return false;
    seen.add(r.op_id);
  }
  return true;
}
function assertDir(dir, create = false) {
  if (!path.isAbsolute(dir)) invalid('directory_must_be_absolute');
  if (create) fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const st = fs.lstatSync(dir);
  if (!st.isDirectory() || st.isSymbolicLink()) storeInvalid();
}
function readFile(dir, name, cap, validate, missing = null) {
  const file = path.join(dir, name);
  let fd;
  try {
    fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  } catch (e) {
    if (e.code === 'ENOENT') return { value: missing, bytes: null, stat: null };
    if (['ELOOP', 'EISDIR'].includes(e.code)) storeInvalid();
    throw new ReviewError('store_unreadable');
  }
  try {
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || stat.size > cap) storeInvalid();
    const bytes = fs.readFileSync(fd);
    if (bytes.length > cap) storeInvalid();
    let value;
    try { value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)); } catch { storeInvalid(); }
    if (!validate(value)) storeInvalid();
    return { value, bytes, stat };
  } catch (e) {
    if (e instanceof ReviewError) throw e;
    throw new ReviewError('store_unreadable');
  } finally { fs.closeSync(fd); }
}
function readItems(dir, now = new Date()) {
  const items = readFile(dir, 'items.json', ITEM_CAP, validItems);
  if (items.value) {
    const updated = Date.parse(items.value.updated_at);
    if (updated > now.getTime() + 300000 || updated > items.stat.mtimeMs + 300000) storeInvalid();
  }
  return items;
}
function emptyAcks(now = new Date().toISOString()) {
  return { schema: 'lews-review-acks/2', updated_at: now, next_seq: 1, entries: {}, receipts: [] };
}
function readAcks(dir) { return readFile(dir, 'acks.json', ACK_CAP, validAcks, emptyAcks()); }
function serialize(value, cap) {
  const bytes = Buffer.from(`${JSON.stringify(value, null, 2)}\n`);
  if (bytes.length > cap) throw new ReviewError('capacity', 'Inbox nearing capacity', 507, 4);
  return bytes;
}
function atomicWrite(dir, name, value, oldBytes, cap) {
  const bytes = serialize(value, cap);
  const target = path.join(dir, name);
  const tmp = path.join(dir, `.${name}.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tmp`);
  try {
    const fd = fs.openSync(tmp, fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY | fs.constants.O_NOFOLLOW, 0o600);
    try { fs.writeFileSync(fd, bytes); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    if (oldBytes !== null) {
      const prev = `${target}.prev`;
      const ptmp = `${tmp}.prev`;
      const pfd = fs.openSync(ptmp, fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY | fs.constants.O_NOFOLLOW, 0o600);
      try { fs.writeFileSync(pfd, oldBytes); fs.fsyncSync(pfd); } finally { fs.closeSync(pfd); }
      fs.renameSync(ptmp, prev);
    }
    fs.renameSync(tmp, target);
    try { const dfd = fs.openSync(dir, fs.constants.O_RDONLY); try { fs.fsyncSync(dfd); } finally { fs.closeSync(dfd); } } catch (e) { if (!['EINVAL', 'ENOTSUP'].includes(e.code)) throw e; }
  } catch (e) {
    try { fs.unlinkSync(tmp); } catch {}
    try { fs.unlinkSync(`${tmp}.prev`); } catch {}
    if (e instanceof ReviewError) throw e;
    throw new ReviewError('write_failed', 'write_failed', 507, 5);
  }
}
function withLock(dir, action) {
  try { assertDir(dir, true); } catch (e) { if (e instanceof ReviewError) throw e; throw new ReviewError('store_unreadable'); }
  const lock = path.join(dir, '.txn.lock');
  const deadline = Date.now() + 5000;
  while (true) {
    try { fs.mkdirSync(lock, 0o700); break; }
    catch (e) {
      if (e.code !== 'EEXIST') throw new ReviewError('store_unreadable');
      if (Date.now() >= deadline) throw new ReviewError('lock_timeout');
      Atomics.wait(sleepArray, 0, 0, 25);
    }
  }
  const owner = { pid: process.pid, nonce: crypto.randomBytes(16).toString('hex'), started_at: new Date().toISOString() };
  try {
    fs.writeFileSync(path.join(lock, 'owner.json'), JSON.stringify(owner), { flag: 'wx', mode: 0o600 });
    return action();
  } finally {
    try { fs.unlinkSync(path.join(lock, 'owner.json')); } catch {}
    try { fs.rmdirSync(lock); } catch {}
  }
}
function usage(items, acks) {
  const i = items && serialize(items, ITEM_CAP).length;
  const a = acks && serialize(acks, ACK_CAP).length;
  const history = acks && Object.values(acks.entries).reduce((n, e) => n + e.history.length, 0);
  return !!((items && (items.items.length >= 1600 || i >= ITEM_CAP * .8))
    || (acks && (Object.keys(acks.entries).length >= 1600 || history >= 40000
      || acks.receipts.length >= 16000 || a >= ACK_CAP * .8)));
}
function snapshot(dir, now) {
  assertDir(dir);
  for (let attempt = 0; attempt < 2; attempt++) {
    const items = readItems(dir, now), acks = readAcks(dir);
    const stat = name => { try { const s = fs.statSync(path.join(dir, name)); return `${s.ino}:${s.size}:${s.mtimeMs}`; } catch { return 'missing'; } };
    const same = (file, data) => stat(file) === (data.stat ? `${data.stat.ino}:${data.stat.size}:${data.stat.mtimeMs}` : 'missing');
    if (same('items.json', items) && same('acks.json', acks)) return { items, acks };
  }
  throw new ReviewError('store_unreadable');
}
function projection(dir, now = new Date()) {
  try {
    const { items, acks } = snapshot(dir, now);
    if (!items.value) return { state: 'missing', read_at: now.toISOString(), updated_at: null, mtime: null, pending_count: 0, items: [] };
    const updated = Date.parse(items.value.updated_at);
    const merged = items.value.items.map((item, index) => {
      const entry = acks.value.entries[item.id];
      const state = item.resolved_at ? 'resolved' : entry?.acked ? 'done' : 'pending';
      return { ...item, project: item.project || 'Other', state, acked: !!entry?.acked,
        acked_at: entry?.acked_at || null, seq: entry?.seq || 0, _position: index };
    });
    merged.sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)
      || (b.first_raised_at ? Date.parse(b.first_raised_at) : -Infinity) - (a.first_raised_at ? Date.parse(a.first_raised_at) : -Infinity)
      || a._position - b._position);
    return { state: 'ok', read_at: now.toISOString(), updated_at: items.value.updated_at,
      mtime: items.stat.mtime.toISOString(), pending_count: merged.filter(x => x.state === 'pending').length,
      stale: now.getTime() - updated > 86400000, warning: usage(items.value, acks.value) ? 'Inbox nearing capacity' : null,
      items: merged.map(({ _position, ...item }) => item) };
  } catch (e) {
    const code = e instanceof ReviewError ? e.code : 'store_unreadable';
    return { state: code === 'store_invalid' ? 'invalid' : 'unavailable', read_at: now.toISOString(),
      updated_at: null, mtime: null, pending_count: 0, items: [], error_code: code };
  }
}
function addItem(dir, data, options = {}) {
  if (!['review', 'answer', 'input'].includes(data.kind) || !plain(data.title, 120)) invalid();
  const source = {};
  for (const key of ['dispatch_id', 'reply_to_message_id']) if (data[key] !== undefined) {
    if (typeof data[key] !== 'string' || !SOURCE_ID.test(data[key])) invalid(); source[key] = data[key];
  }
  if (data.link !== undefined && !validLink(data.link)) invalid();
  if (data.first_raised_at !== undefined && !iso(data.first_raised_at)) invalid();
  if (data.origin_note !== undefined && !plain(data.origin_note, 200)) invalid();
  if (data.smoke_run !== undefined && !RUN.test(data.smoke_run)) invalid();
  if (data.smoke_run !== undefined && data.title !== `Review inbox smoke ${data.smoke_run}`) invalid();
  if (data.project !== undefined && (!validProject(data.project) || data.smoke_run !== undefined)) invalid('invalid_project');
  return withLock(dir, () => {
    const old = readItems(dir);
    const value = old.value || { schema: 'lews-review-items/1', updated_at: new Date().toISOString(), items: [] };
    if (value.items.length >= 2000) throw new ReviewError('capacity', 'Inbox nearing capacity', 507);
    let id;
    for (let n = 0; n < 5; n++) {
      id = `ri_${(options.randomBytes || crypto.randomBytes)(16).toString('hex')}`;
      if (!value.items.some(x => x.id === id)) break;
      id = null;
    }
    if (!id) conflict('id_collision');
    const at = (options.now || (() => new Date()))().toISOString();
    const item = { id, kind: data.kind, title: data.title, created_at: at, source };
    if (data.first_raised_at !== undefined) item.first_raised_at = data.first_raised_at;
    if (data.origin_note !== undefined) item.origin_note = data.origin_note;
    if (data.link !== undefined) item.link = data.link;
    if (data.project !== undefined) item.project = data.project;
    if (data.smoke_run !== undefined) item.smoke = { run_id: data.smoke_run };
    value.items.push(item); value.updated_at = at;
    if (usage(value, null) && !usage(old.value, null)) { /* first threshold write is allowed */ }
    atomicWrite(dir, 'items.json', value, old.bytes, ITEM_CAP);
    return { item, warning: usage(value, null) ? 'Inbox nearing capacity' : null };
  });
}
function resolveItem(dir, id, reason, options = {}) {
  if (!ID.test(id) || !plain(reason, 200)) invalid();
  return withLock(dir, () => {
    const old = readItems(dir);
    if (!old.value) conflict('unknown_id');
    const item = old.value.items.find(x => x.id === id);
    if (!item) conflict('unknown_id');
    if (item.resolved_at) {
      if (item.resolution_reason !== reason) conflict('resolution_conflict');
      return { item, changed: false };
    }
    item.resolved_at = (options.now || (() => new Date()))().toISOString();
    item.resolution_reason = reason; old.value.updated_at = item.resolved_at;
    atomicWrite(dir, 'items.json', old.value, old.bytes, ITEM_CAP);
    return { item, changed: true };
  });
}
function current(item, entry) {
  return { acked: !!entry?.acked, acked_at: entry?.acked_at || null, seq: entry?.seq || 0,
    state: item.resolved_at ? 'resolved' : entry?.acked ? 'done' : 'pending' };
}
function ackItem(dir, request, options = {}) {
  if (!keys(request, ['id', 'op', 'op_id']) || !ID.test(request.id)
    || !['ack', 'unack'].includes(request.op) || !UUID.test(request.op_id)) invalid();
  return withLock(dir, () => {
    const items = readItems(dir), old = readAcks(dir);
    if (!items.value) throw new ReviewError('store_invalid');
    const item = items.value.items.find(x => x.id === request.id);
    if (!item) throw new ReviewError('unknown_id', 'unknown_id', 404, 3);
    const acks = old.value;
    const receipt = acks.receipts.find(x => x.op_id === request.op_id);
    const entry = acks.entries[request.id];
    if (receipt) {
      if (receipt.id !== request.id || receipt.op !== request.op) conflict('op_id_conflict');
      return { ok: true, id: request.id, result: receipt.result, current: current(item, entry), replayed: true,
        warning: usage(items.value, acks) ? 'Inbox nearing capacity' : null };
    }
    if (Object.values(acks.entries).some(e => e.history.some(h => h.op_id === request.op_id))) conflict('op_id_expired');
    if (item.resolved_at) conflict('resolved');
    const at = (options.now || (() => new Date()))().toISOString();
    const desired = request.op === 'ack';
    const changed = !!entry?.acked !== desired;
    const copy = structuredClone(acks);
    let next = copy.entries[request.id];
    if (changed) {
      const seq = copy.next_seq++;
      if (!next) next = copy.entries[request.id] = { acked: desired, acked_at: null, seq, history: [] };
      next.acked = desired; next.seq = seq;
      if (desired) next.acked_at = at;
      next.history.push({ seq, op: request.op, at, op_id: request.op_id });
    }
    const result = { changed, seq_seen: next?.seq || 0, acked: !!next?.acked,
      acked_at: next?.acked_at || null, state: next?.acked ? 'done' : 'pending' };
    copy.receipts.push({ op_id: request.op_id, id: request.id, op: request.op, accepted_at: at, result });
    copy.updated_at = at;
    if (usage(items.value, acks)) {
      copy.receipts = copy.receipts.filter(r => Date.parse(r.accepted_at) > Date.parse(at) - 86400000);
    }
    if (Object.keys(copy.entries).length > 2000 || copy.receipts.length > 20000
      || Object.values(copy.entries).reduce((n, e) => n + e.history.length, 0) > 50000)
      throw new ReviewError('capacity', 'Inbox nearing capacity', 507);
    atomicWrite(dir, 'acks.json', copy, old.bytes, ACK_CAP);
    return { ok: true, id: request.id, result, current: current(item, next), replayed: false,
      warning: usage(items.value, copy) ? 'Inbox nearing capacity' : null };
  });
}
function smokeCleanup(dir, request) {
  if (!keys(request, ['id', 'run_id', 'op_id']) || !ID.test(request.id) || !RUN.test(request.run_id) || !UUID.test(request.op_id)) invalid();
  return withLock(dir, () => {
    const items = readItems(dir), old = readAcks(dir);
    const item = items.value?.items.find(x => x.id === request.id);
    if (!item || item.smoke?.run_id !== request.run_id) conflict('smoke_mismatch');
    const copy = structuredClone(old.value);
    delete copy.entries[request.id];
    copy.receipts = copy.receipts.filter(x => x.id !== request.id);
    copy.updated_at = new Date().toISOString();
    atomicWrite(dir, 'acks.json', copy, old.bytes, ACK_CAP);
    return { ok: true, id: request.id, cleaned: true };
  });
}
function smokeRemove(dir, id, runId) {
  if (!ID.test(id) || !RUN.test(runId)) invalid();
  return withLock(dir, () => {
    const items = readItems(dir), acks = readAcks(dir);
    const item = items.value?.items.find(x => x.id === id);
    if (!item || item.smoke?.run_id !== runId) conflict('smoke_mismatch');
    if (acks.value.entries[id] || acks.value.receipts.some(x => x.id === id)) conflict('smoke_ack_exists');
    items.value.items = items.value.items.filter(x => x.id !== id);
    items.value.updated_at = new Date().toISOString();
    atomicWrite(dir, 'items.json', items.value, items.bytes, ITEM_CAP);
    return { id };
  });
}
function seedItems(dir, raw, options = {}) {
  const bytes = Buffer.isBuffer(raw) ? raw : Buffer.from(raw);
  let seed;
  try { seed = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)); } catch { invalid('invalid_seed'); }
  if (!keys(seed, ['note', 'prepared_at', 'items'], ['schema']) || (own(seed, 'schema') && seed.schema !== 'lews-review-seed/2')
    || !plain(seed.note, 1000) || !iso(seed.prepared_at) || !Array.isArray(seed.items) || seed.items.length > 2000) invalid('invalid_seed');
  const hashes = new Set();
  const rows = seed.items.map(row => {
    if (!keys(row, ['kind', 'title', 'link', 'first_raised_at', 'source'], ['reply_to_message_id', 'project'])
      || !['review', 'answer', 'input'].includes(row.kind) || !plain(row.title, 120)
      || (row.link !== null && !validLink(row.link))
      || (row.first_raised_at !== null && !iso(row.first_raised_at))
      || !plain(row.source, 200, 0)
      || (own(row, 'reply_to_message_id') && (typeof row.reply_to_message_id !== 'string' || !SOURCE_ID.test(row.reply_to_message_id)))
      || (own(row, 'project') && !validProject(row.project))) invalid('invalid_seed');
    const hash = crypto.createHash('sha256').update([row.kind, row.title.normalize('NFC'), row.link || '', row.source].join('\0')).digest('hex');
    if (hashes.has(hash)) conflict('duplicate_seed_row');
    hashes.add(hash);
    return { row, hash, id: `ri_${hash.slice(0, 32)}` };
  });
  if (new Set(rows.map(x => x.id)).size !== rows.length) conflict('seed_id_collision');
  const marker = `sha256:${crypto.createHash('sha256').update(bytes).digest('hex')}`;
  return withLock(dir, () => {
    const old = readItems(dir);
    if (old.value) {
      if (old.value.seed_marker !== marker || old.value.items.length !== rows.length) conflict('seed_changed');
      if (!rows.every(({ id, hash, row }, i) => {
        const item = old.value.items[i];
        if (item?.id !== id || old.value.seed_keys?.[id] !== hash) return false;
        const source = {};
        const match = /^dispatch ([A-Za-z0-9][A-Za-z0-9._:-]{0,159})$/.exec(row.source);
        if (match) source.dispatch_id = match[1];
        if (row.reply_to_message_id) source.reply_to_message_id = row.reply_to_message_id;
        return item.kind === row.kind && item.title === row.title && item.project === row.project
          && (item.link || null) === row.link
          && (item.first_raised_at || null) === row.first_raised_at
          && (item.origin_note || null) === (!match && row.source ? row.source : null)
          && JSON.stringify(item.source) === JSON.stringify(source);
      })) conflict('seed_changed');
      return { changed: false, count: rows.length };
    }
    const at = (options.now || (() => new Date()))().toISOString();
    const value = { schema: 'lews-review-items/1', updated_at: at, seed_marker: marker, seed_keys: {}, items: [] };
    for (const { row, hash, id } of rows) {
      const source = {};
      const match = /^dispatch ([A-Za-z0-9][A-Za-z0-9._:-]{0,159})$/.exec(row.source);
      if (match) source.dispatch_id = match[1];
      if (row.reply_to_message_id) source.reply_to_message_id = row.reply_to_message_id;
      const item = { id, kind: row.kind, title: row.title, created_at: at, first_raised_at: row.first_raised_at, source };
      if (!match && row.source) item.origin_note = row.source;
      if (row.link) item.link = row.link;
      if (row.project !== undefined) item.project = row.project;
      value.items.push(item); value.seed_keys[id] = hash;
    }
    atomicWrite(dir, 'items.json', value, null, ITEM_CAP);
    return { changed: true, count: rows.length };
  });
}
module.exports = { ReviewError, ID, SOURCE_ID, UUID, RUN, PROJECTS, validProject, validItems, validAcks, validLink, plain,
  addItem, resolveItem, ackItem, smokeCleanup, smokeRemove, seedItems, projection, snapshot,
  readItems, readAcks, withLock, usage, ITEM_CAP, ACK_CAP };
