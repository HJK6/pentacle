'use strict';

// Negative-proof unit tests for the render-level reply assertion in
// `sendOneTurn` (render-telemetry QA layer — defects 1 & 2).
//
// These drive the REAL `sendOneTurn` flow with a fake `ctx` over a synthetic
// beacon trace (no live app), so the gate proves the reply leg's CONTRACT:
//   • Defect 1 (self-fulfilling beacon): a storm of `chat:event_rendered`
//     beacons (the harness's own polling, zero new content, zero ASSIST paint)
//     must NOT satisfy the reply await. Pre-fix it did (the leg awaited
//     `chat:event_rendered`); post-fix `sendOneTurn` must FAIL.
//   • Defect 2 (store-level reply): optimistic insert + reconcile succeed and the
//     store ASSIST_TEXT count rises, but NO `chat:slot_painted` carrying a new
//     ASSIST_TEXT `lastRowDigest` is emitted → `sendOneTurn` must FAIL (render
//     never happened). Pre-fix it passed on store-count + `chat:event_rendered`.
// A positive control proves the leg PASSES when the ASSIST_TEXT paint IS emitted,
// so the proofs measure the render gate — not a flow that can never succeed.

const test = require('node:test');
const assert = require('node:assert/strict');
const { sendOneTurn } = require('./flows');

const SLOT = 1;
const STREAM = 'hostc:provider_a:fake-stream';

// Build a fake `ctx` over a FIXED synthetic beacon trace. beaconSeq() returns 0
// (the pre-send baseline `sendOneTurn` captures), and every synthetic beacon has
// seq > 0, so the `b.seq > before` predicates match. awaitBeacon resolves from
// the fixed trace or REJECTS immediately when no match exists (the trace is
// complete — no real timeout wait). `assistTextAfter` feeds the store-kind leg.
function makeCtx({ beacons, assistTextAfter = 0 }) {
  const calls = { asserts: [], screenshots: 0 };
  return {
    ctx: {
      beaconSeq: () => 0,
      beaconsSince: () => beacons,
      log: () => {},
      type: async () => true,
      click: async () => true,
      screenshot: async (name) => { calls.screenshots += 1; return `/tmp/${name}.png`; },
      eval: async (code) => {
        // diag probe (turn phase + composer disabled state)
        if (/getTurnPhase/.test(code)) {
          return { phase: 'idle', sendDisabled: false, inputDisabled: false };
        }
        // baseline ASSIST_TEXT store count before send
        return 0;
      },
      waitFor: async (expr, opts) => {
        const label = (opts && opts.label) || '';
        // composer-enabled gate
        if (/composer send button enabled/.test(label)) return true;
        // lane B store-kind ASSIST_TEXT reply leg: resolve to the after-count,
        // mirroring the live `waitFor` that returns the increased count or throws.
        if (/ASSIST_TEXT reply item rendered/.test(label)) {
          if (assistTextAfter > 0) return assistTextAfter;
          throw new Error(`waitFor timed out: ${label}`);
        }
        return true;
      },
      awaitBeacon: async (pred, opts) => {
        const hit = beacons.find(pred);
        if (hit) return hit;
        throw new Error(`awaitBeacon timed out: ${(opts && opts.label) || 'beacon'}`);
      },
      assert: (name, cond, detail) => {
        calls.asserts.push({ name, cond });
        if (!cond) throw new Error(`assert failed: ${name} ${detail ? JSON.stringify(detail) : ''}`);
        return cond;
      },
    },
    calls,
  };
}

const SEND_ARGS = { slot: SLOT, streamId: STREAM, text: 'Reply with exactly: OK', label: 'turn1' };

const optimisticInsert = { name: 'chat.compose.optimistic_insert', seq: 1, slot: SLOT, streamId: STREAM };
const reconciled = { name: 'chat.compose.optimistic_reconciled', seq: 2, slot: SLOT, streamId: STREAM };
// User-echo paint: a real paint, but the last row is the USER message, not a reply.
const userPaint = {
  name: 'chat:slot_painted', seq: 3, slot: SLOT, streamId: STREAM,
  data: { paintedStreamId: STREAM, lastRowDigest: { kind: 'USER', displayRule: 'bubble:user', text_prefix: 'Reply with exactly: OK' } },
};
const assistPaint = {
  name: 'chat:slot_painted', seq: 5, slot: SLOT, streamId: STREAM,
  data: { paintedStreamId: STREAM, rowCount: 2, kinds: { USER: 1, ASSIST_TEXT: 1 }, lastRowDigest: { kind: 'ASSIST_TEXT', displayRule: 'bubble:assistant', text_prefix: 'OK' } },
};

test('DEFECT 1 negative proof: a chat:event_rendered probe storm (no ASSIST paint) does NOT satisfy the reply await', async () => {
  // Simulate the harness's own polling: many fresh `chat:event_rendered` beacons
  // for already-rendered content, plus the user-echo paint. No assistant paint.
  const storm = Array.from({ length: 8 }, (_, i) => ({
    name: 'chat:event_rendered', seq: 10 + i, slot: SLOT, streamId: STREAM,
    data: { kind: i % 2 ? 'USER' : 'ASSIST_TEXT' },
  }));
  const beacons = [optimisticInsert, reconciled, userPaint, ...storm];
  const { ctx } = makeCtx({ beacons, assistTextAfter: 1 });
  await assert.rejects(
    () => sendOneTurn(ctx, SEND_ARGS),
    /chat:slot_painted ASSIST_TEXT digest/,
    'event_rendered storm must not satisfy the render-level reply await',
  );
});

test('DEFECT 2 negative proof: store ASSIST_TEXT rises but no ASSIST_TEXT paint → sendOneTurn FAILS', async () => {
  // Optimistic + reconcile succeed; store ASSIST_TEXT count rises (lane B leg
  // would pass); user-echo paints; AND a `chat:event_rendered` fires — so the
  // PRE-FIX reply leg (await event_rendered + store-count) RESOLVED and SUCCEEDED
  // here: a genuine false pass. But NO ASSIST_TEXT paint is emitted, so the
  // POST-FIX render-level leg must FAIL — a true behavioral flip, not just an
  // absent-old-beacon rejection.
  const eventRendered = {
    name: 'chat:event_rendered', seq: 4, slot: SLOT, streamId: STREAM,
    data: { kind: 'ASSIST_TEXT' },
  };
  const beacons = [optimisticInsert, reconciled, userPaint, eventRendered];
  const { ctx } = makeCtx({ beacons, assistTextAfter: 1 });
  await assert.rejects(
    () => sendOneTurn(ctx, SEND_ARGS),
    /chat:slot_painted ASSIST_TEXT digest/,
    'a store-count rise without a rendered ASSIST_TEXT paint must fail the reply leg',
  );
});

test('positive control: ASSIST_TEXT paint + store-count rise → sendOneTurn SUCCEEDS', async () => {
  const beacons = [optimisticInsert, reconciled, userPaint, assistPaint];
  const { ctx, calls } = makeCtx({ beacons, assistTextAfter: 1 });
  await sendOneTurn(ctx, SEND_ARGS);
  assert.ok(calls.asserts.some((a) => a.name === 'turn1: agent reply rendered' && a.cond));
});
