import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { applyPentacleSessionInventory, initialPentacleStreamState,
  decodeChildAgents, decodeThreadRead, validSpawnObjective,
  type PentacleSessionSummary } from '../src/index.ts';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/daemon_updates_v1.json', import.meta.url), 'utf8'));

test('committed D1/M1 inventory fixture passes through the real reducer unchanged', () => {
  for (const item of fixture.inventory_cases) {
    const state = applyPentacleSessionInventory(initialPentacleStreamState, item.frame.sessions as PentacleSessionSummary[]);
    for (const input of item.frame.sessions) {
      const projected = state.sessions.find(row => row.stream_id === input.stream_id);
      assert.ok(projected);
      assert.deepEqual(projected.agents, input.agents);
    }
  }
});

test('same committed thread success/error fixture decodes with raw UTC and nullable fields', () => {
  for (const item of [...fixture.thread_cases, ...fixture.error_cases]) {
    assert.deepEqual(decodeThreadRead(item.response), item.response);
  }
  assert.equal(decodeThreadRead({type: 'thread.read.ok', ok: true}), null);
  const response = fixture.thread_cases[0].response;
  assert.equal(decodeThreadRead({...response, rows: [{...response.rows[0], ts: 123}]}), null);
  assert.equal(decodeChildAgents([{stream_id: 'bad'}]), undefined);
});

test('successful thread fixture cursors bind the same parent and child as their request', () => {
  for (const item of fixture.thread_cases) {
    for (const encoded of [item.request.cursor, item.response.next_cursor]) {
      if (!encoded) continue;
      const cursor = JSON.parse(Buffer.from(encoded, 'base64url').toString('utf8'));
      assert.equal(cursor.parent, item.response.parent_stream_id, item.label);
      assert.equal(cursor.child, item.response.child_stream_id, item.label);
      assert.equal(cursor.v, 1);
      assert.ok(Number.isInteger(cursor.before_seq) && cursor.before_seq > 0);
    }
  }
});

test('objective producer fixture and Unicode boundaries share daemon admission semantics', () => {
  for (const item of fixture.spawn_cases) {
    assert.equal(validSpawnObjective(item.request.objective), item.expected_error !== 'objective_required');
  }
  assert.equal(validSpawnObjective('🌈'.repeat(120)), true);
  for (const value of ['', '  ', 'x\ny', 'x\u2028y', '🌈'.repeat(121), null]) {
    assert.equal(validSpawnObjective(value), false);
  }
});
