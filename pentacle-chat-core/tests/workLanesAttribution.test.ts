import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { normalizeWorkLanesInventory } from '../src';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/work-lanes-inventory.json', import.meta.url), 'utf8'));

function frameWith(member: Record<string, unknown>) {
  const lane = structuredClone(fixture.inventory_frame.lanes[1]);
  lane.members = [{ spec_id: 'spec_demo__span', title: 'Span', status: 'in_progress', ...member }];
  return { ...fixture.inventory_frame, lanes: [lane] };
}

test('member seats and open questions normalize additively', () => {
  const inventory = normalizeWorkLanesInventory(frameWith({
    seats: [
      { stream_id: 'host-a:lead', role: 'lead', host: 'host-a', visibility: 'hidden', parent_stream_id: 'host-a:fd',
        activity: 'working', last_activity: '2026-10-09T00:00:00Z' },
      { stream_id: 'host-a:qa', role: 'qa', activity: 'sleeping' },
      { role: 'worker' },
    ],
    seats_total: 9,
    open_questions: { count: 2, first_question_id: 'q-1' },
  }));
  const member = inventory!.lanes[0].progress!.members[0];
  assert.deepEqual(member.seats.map((s) => [s.stream_id, s.activity]), [['host-a:lead', 'working'], ['host-a:qa', 'unknown']]);
  assert.equal(member.seats_total, 9);
  assert.deepEqual(member.open_questions, { count: 2, first_question_id: 'q-1' });
});

test('an older daemon without attribution reads as no seats and no questions', () => {
  const member = normalizeWorkLanesInventory(frameWith({}))!.lanes[0].progress!.members[0];
  assert.deepEqual(member.seats, []);
  assert.equal(member.seats_total, 0);
  assert.deepEqual(member.open_questions, { count: 0, first_question_id: null });
});

test('a lead_lost fault presents as active, not paused', () => {
  const lane = normalizeWorkLanesInventory(fixture.inventory_frame)!.lanes.find((l) => l.lane_id === 'wl-paused-0004')!;
  assert.equal(lane.state, 'active');
  assert.equal(lane.state_reason, 'lead_lost');
});
