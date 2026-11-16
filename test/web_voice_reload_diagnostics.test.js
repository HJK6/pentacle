'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const take = '123e4567-e89b-42d3-a456-426614174000';
const origin = 'local:reload-fixture';
const unit = { voice_operation: { operation_id: take, origin_stream_id: origin, origin_generation: 'synthetic-generation' },
  send_mandate: { request_id: 'voice-send-synthetic', text: 'DO_NOT_LOG_TRANSCRIPT', meta: { secret: 'DO_NOT_LOG_METADATA' } } };

// Execute the shipped scenario/probe. Only browser/navigation, SQLite and media
// counterparts are synthetic; no browser/daemon/window is acquired by this test.
async function exercise({ reply, thrown, pending = false, capable = true, dbError = false } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'reload-diagnostic-test-'));
  fs.writeFileSync(path.join(dir, 'sessions.db'), '');
  const events = [], queries = [];
  let clock = 0, page, clicks = 0, resets = 0, originalCalls = 0;
  const NativeDate = Date;
  class Clock extends NativeDate { constructor(...args) { super(...(args.length ? args : [clock])); } static now() { return clock; } }
  const fixtureReply = reply === undefined ? { ok: true, server_send: true, operation_id: take, request_id: 'outbound.commit-rpc' } : reply;
  function freshPage() {
    clicks = 0;
    page = vm.createContext({ window: { PentacleChatStore: {}, cc: {
      getChatStreamState: async () => ({ connected: true, state_version: 7, runtime_sha: 'a'.repeat(40), build_sha: 'b'.repeat(40),
        capabilities: { outbound_commit_v1: capable }, events: ['DO_NOT_LOG_EVENTS'], auth: 'DO_NOT_LOG_AUTH' }),
      chatOutboundCommit: async () => { originalCalls++; if (pending) return new Promise(() => {}); if (thrown) throw thrown; return fixtureReply; },
    } }, navigator: { mediaDevices: {} }, document: { querySelector: () => ({ click() {
      if (++clicks !== 2) return;
      Promise.resolve(page.window.cc.chatOutboundCommit(unit)).then(() => {
        fs.writeFileSync(path.join(dir, 'asr-calls.log'), 'one call\n');
      }, () => { fs.writeFileSync(path.join(dir, 'asr-calls.log'), 'one call\n'); });
    } }) } });
  }
  freshPage();
  const customRequire = name => {
    if (name === './workspace_scenario') return { WORKSPACE_KEY: 'synthetic-workspace',
      reloadWith: async () => { resets++; freshPage(); }, waitFor: async () => {} };
    if (name === 'child_process') return { execFileSync(_command, args) {
      const sql = args.at(-1); queries.push(sql);
      if (dbError && !sql.includes('count(')) throw new Error('DO_NOT_LOG_DB_ERROR');
      if (sql.includes('count(')) return resets < 4 ? '0' : '1';
      return '[]';
    } };
    return require(name);
  };
  const filename = path.join(root, 'test/e2e/lib/web_voice_reload_scenario.js');
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(filename, 'utf8'), { require: customRequire, module, Date: Clock, console }, { filename });
  const report = {
    diagnostic(name, detail) { events.push({ kind: 'diagnostic', name, detail: JSON.parse(JSON.stringify(detail)) }); },
    note(name) { events.push({ kind: 'note', name }); },
    ok(name, pass, detail) { events.push({ kind: 'assert', name, pass, detail }); assert.equal(!!pass, true, name); },
  };
  const session = { eval: async expression => vm.runInContext(expression, page),
    send: async (_method, args) => vm.runInContext(args.expression, page) };
  let error;
  try { await module.exports.webVoiceReloadAfterCommit({ session, report,
    cdp: { sleep: async ms => { clock += ms; for (let i = 0; i < 12; i++) await Promise.resolve(); } },
    fixture: { streamId: origin }, runtime: { fixtureScratch: dir } }); } catch (e) { error = e; }
  const result = { error, events, queries, resets, originalCalls, released: fs.existsSync(path.join(dir, 'asr-release')) };
  fs.rmSync(dir, { recursive: true, force: true });
  return result;
}
function diagnosticBeforeAssertion(result) {
  const assertion = result.events.findIndex(e => e.kind === 'assert' && e.name === 'the real commit ACK precedes reload');
  const diagnostic = result.events.findIndex(e => e.kind === 'diagnostic' && e.detail.stage === 'before-reload');
  assert.ok(diagnostic >= 0 && diagnostic < assertion, 'correlated metadata is recorded before the throwing ACK assertion');
  assert.ok(result.events.some(e => e.kind === 'diagnostic' && e.detail.stage === 'finally'));
  return result.events[diagnostic].detail;
}

test('positive diagnostics correlate real reply and snapshot cache without copying content', async () => {
  const r = await exercise(); assert.ifError(r.error);
  const d = diagnosticBeforeAssertion(r);
  assert.equal(d.commit.operationId, take);
  assert.equal(d.commit.requestId, 'voice-send-synthetic');
  assert.equal(d.commit.replyOperationId, take);
  assert.equal(d.commit.replyRequestId, 'outbound.commit-rpc');
  assert.equal(d.commit.ok, true); assert.equal(d.commit.serverSend, true);
  assert.equal(d.commit.snapshotBefore.outboundCommitV1, true);
  assert.equal(d.commit.snapshotBefore.source, 'getChatStreamState');
  assert.equal(d.commit.snapshotBefore.stateVersion, 7);
  assert.ok(d.database.path.endsWith('sessions.db'));
  assert.ok(r.queries.some(sql => sql.includes("WHERE operation_id='" + take + "'")));
  assert.ok(r.queries.some(sql => sql.includes("request_id='voice-send-synthetic'")));
  assert.ok(!JSON.stringify(r.events).includes('DO_NOT_LOG'));
  assert.equal(r.originalCalls, 1); assert.equal(r.released, true);
  assert.ok(r.events.some(e => e.kind === 'assert' && e.name.startsWith('control:') && e.pass));
});
for (const [label, options, code] of [
  ['refusal', { capable: false, reply: { ok: false, server_send: false, error_code: 'unsupported', error: 'DO_NOT_LOG_REFUSAL' } }, 'unsupported'],
  ['missing reply', { reply: null }, null],
  ['throw', { thrown: { error_code: 'unsupported', message: 'DO_NOT_LOG_EXCEPTION' } }, 'unsupported'],
  ['unknown refusal text', { reply: { ok: false, error_code: 'DO_NOT_LOG_REFUSAL' } }, 'unrecognized_error'],
]) test(`${label} is diagnosed and fails without a reload/retry-to-green`, async () => {
  const r = await exercise(options); assert.ok(r.error);
  const d = diagnosticBeforeAssertion(r);
  assert.equal(d.commit.operationId, take); assert.equal(d.commit.error, code);
  assert.equal(d.commit.snapshotBefore.outboundCommitV1, options.capable !== false);
  assert.equal(r.originalCalls, 1); assert.equal(r.resets, 3); assert.equal(r.released, true);
  assert.ok(!JSON.stringify(r.events).includes('DO_NOT_LOG'));
});
test('an ok reply for another take cannot authorize reload', async () => {
  const r = await exercise({ reply: { ok: true, server_send: true, operation_id: '123e4567-e89b-42d3-a456-426614174001' } });
  assert.ok(r.error); diagnosticBeforeAssertion(r); assert.equal(r.resets, 3);
});
test('pre-ACK timeout retains pending identity and diagnostics in finally and releases ASR', async () => {
  const r = await exercise({ pending: true }); assert.match(r.error?.message || '', /timed out/);
  const d = r.events.find(e => e.kind === 'diagnostic' && e.detail.stage === 'finally')?.detail;
  assert.ok(d, 'timeout diagnostic survives before ACK'); assert.equal(d.commit.operationId, take);
  assert.equal(d.commit.phase, 'pending'); assert.equal(r.originalCalls, 1); assert.equal(r.released, true);
});
test('diagnostic DB failure does not hide the real commit refusal', async () => {
  const r = await exercise({ dbError: true, reply: { ok: false, error_code: 'unsupported' } });
  assert.match(r.error?.message || '', /real commit ACK/); const d = diagnosticBeforeAssertion(r);
  assert.equal(d.database.readError, 'unavailable'); assert.ok(!JSON.stringify(r.events).includes('DO_NOT_LOG'));
});
test('web gate persists diagnostic metadata independently of throwing assertions', () => {
  const filename = path.join(root, 'test/e2e/web_gate.js');
  const module = { exports: {} };
  const code = fs.readFileSync(filename, 'utf8') + '\nmodule.exports.ReportForTest = Report;';
  const requireStub = name => name.startsWith('.') ? {} : require(name);
  vm.runInNewContext(code, { require: requireStub, module, __dirname: path.dirname(filename), process, console, Buffer }, { filename });
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'reload-report-test-'));
  try {
    const report = new module.exports.ReportForTest(dir);
    assert.equal(typeof report.diagnostic, 'function');
    report.diagnostic('voice reload', { stage: 'finally', operationId: take });
    assert.throws(() => report.ok('real ACK', false));
    const verdict = report.write(); assert.equal(verdict.status, 'FAIL');
    assert.equal(verdict.diagnostics[0].detail.operationId, take);
    assert.equal(JSON.parse(fs.readFileSync(path.join(dir, 'verdict.json'))).diagnostics.length, 1);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});
