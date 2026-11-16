const test = require('node:test');
const assert = require('node:assert/strict');
const {
  offlineHostStatus, filterSidebarSessions, collectSourceFilterHostIds,
  nextChatStreamSessions, projectChatStreamSessionsToDesktop,
} = require('../renderer/sidebar_filter');

test('offlineHostStatus follows the optional L3 contract', () => {
  assert.equal(offlineHostStatus({}), null);
  assert.equal(offlineHostStatus({ host_status: 'online' }), null);
  assert.equal(offlineHostStatus({ host_status: 'offline' }), 'Offline');
  assert.equal(offlineHostStatus({ host_status_reason: 'unreachable' }), 'Offline');
  assert.equal(offlineHostStatus({ host_status: 'offline', host_status_since: '2026-07-31T06:00:00Z' }), 'Offline since 2026-07-31T06:00:00Z');
});

test('filters positively on each daemon row visibility, independent of prior inventory', () => {
  const rows = [
    { name: 'visible', hostId: 'hosta', visibility: 'default' },
    { name: 'nested', hostId: 'hosta', visibility: 'nested' },
    { name: 'hidden', hostId: 'hosta', visibility: 'hidden' },
    { name: 'missing', hostId: 'hosta' },
  ];
  for (const prior of [[], [{ name: 'hidden', visibility: 'default' }]]) {
    assert.deepEqual(filterSidebarSessions(rows, prior).map((row) => row.name), ['visible']);
  }
});

test('returns a fresh array and handles missing daemon rows safely', () => {
  const rows = [{ name: 'a', visibility: 'default' }];
  const out = filterSidebarSessions(rows);
  out.push({ name: 'b' });
  assert.equal(rows.length, 1);
  assert.deepEqual(filterSidebarSessions(), []);
  assert.deepEqual(filterSidebarSessions(null), []);
});

test('source-filter chrome reflects visible daemon rows only', () => {
  const rows = [
    { name: 'nested', hostId: 'hostb', visibility: 'nested' },
    { name: 'visible', hostId: 'hosta', visibility: 'default' },
  ];
  assert.deepEqual(collectSourceFilterHostIds(filterSidebarSessions(rows)), ['hosta']);
  assert.deepEqual(collectSourceFilterHostIds([{ name: 'x', hostId: 'hostc' }]), ['hostc']);
});

test('inventory replaces absent rows; status-only payload preserves rows', () => {
  const current = [{ stream_id: 'hosta:hidden', visibility: 'hidden' }];
  const incoming = [{ stream_id: 'hosta:new', visibility: 'default' }];
  assert.deepEqual(nextChatStreamSessions(current, { sessions: incoming }), incoming);
  assert.deepEqual(nextChatStreamSessions(current, { connected: false }), current);
  assert.deepEqual(nextChatStreamSessions(current, {}), current);
  assert.deepEqual(nextChatStreamSessions(undefined, undefined), []);
});

test('daemon inventory lifecycle is projected verbatim', () => {
  const ready = { stream_id: 'hosta:v2-ready', session_generation: 'a', state: 'ready', bootstrap_state: 'ready' };
  const delayed = { stream_id: 'hosta:v2-ready', session_generation: 'a', state: 'starting', bootstrap_state: 'starting' };
  const [projected] = nextChatStreamSessions([ready], { sessions: [delayed] });
  assert.deepEqual(projected, delayed);
});

test('a new generation may start after a prior generation', () => {
  const prior = { stream_id: 'hosta:v2-reused', session_generation: 'a', state: 'ready' };
  const replacement = { stream_id: 'hosta:v2-reused', session_generation: 'b', state: 'starting' };
  assert.deepEqual(nextChatStreamSessions([prior], { sessions: [replacement] }), [replacement]);
});

test('failed terminal frames notify once per stream generation with the daemon reason', () => {
  const notified = new Set();
  const reasons = [];
  const options = {
    spawnFailureNotifications: notified,
    onSpawnFailure: (session) => reasons.push(session.reason),
  };
  const failed = {
    stream_id: 'hosta:v2-spawn', session_generation: 'a', state: 'failed', reason: 'boot deadline expired',
  };
  nextChatStreamSessions([], { sessions: [failed] }, options);
  nextChatStreamSessions([], {
    sessions: [{ ...failed, session_generation: 'b', reason: 'credential unavailable' }],
  }, options);
  nextChatStreamSessions([], { sessions: [failed] }, options);
  assert.deepEqual(reasons, ['boot deadline expired', 'credential unavailable']);
});

test('reconnect after degraded status renders only daemon-visible rows', () => {
  const prior = [{ stream_id: 'hosta:old', visibility: 'default' }];
  assert.deepEqual(nextChatStreamSessions(prior, { connected: false }), prior);
  const incoming = [
    { stream_id: 'hosta:visible', session_name: 'visible', host: 'hosta', visibility: 'default' },
    { stream_id: 'hosta:hidden', session_name: 'hidden', host: 'hosta', visibility: 'hidden' },
    { stream_id: 'hosta:nested', session_name: 'nested', host: 'hosta', visibility: 'nested' },
  ];
  const rows = projectChatStreamSessionsToDesktop(nextChatStreamSessions(prior, { sessions: incoming }), () => 'hosta');
  assert.deepEqual(filterSidebarSessions(rows).map((row) => row.name), ['visible']);
});

test('projection maps daemon fields including visibility', () => {
  const [row] = projectChatStreamSessionsToDesktop([{
    stream_id: 'hosta:claude-hosta-1', host: 'hosta', session_name: 'claude-hosta-1',
    display_name: 'hosta claude', last_text: 'last line', attached: true,
    provider: 'claude', visibility: 'default', agent_id: 'uuid-1', role: 'persistent-assistant',
  }], () => 'remote');
  assert.equal(row.name, 'claude-hosta-1');
  assert.equal(row.hostId, 'remote');
  assert.equal(row.preview, 'last line');
  assert.equal(row.visibility, 'default');
  assert.equal(row.role, 'persistent-assistant');
});

test('projected rows without visibility fail closed', () => {
  const rows = projectChatStreamSessionsToDesktop([
    { session_name: 'missing', host: 'hosta' },
    { session_name: 'visible', host: 'hosta', visibility: 'default' },
  ], () => 'hosta');
  assert.equal(rows[0].visibility, undefined);
  assert.deepEqual(filterSidebarSessions(rows).map((row) => row.name), ['visible']);
});

test('projection skips invalid entries and tolerates empty input', () => {
  assert.deepEqual(projectChatStreamSessionsToDesktop(null, () => 'remote'), []);
  const out = projectChatStreamSessionsToDesktop([{ host: 'hosta' }, { session_name: 'codex-hosta-1', host: 'hosta' }], () => 'remote');
  assert.equal(out.length, 1);
  assert.equal(out[0].name, 'codex-hosta-1');
});
