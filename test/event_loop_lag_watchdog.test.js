const assert = require('node:assert/strict');
const test = require('node:test');

const {
  createEventLoopLagDetector,
  formatEventLoopLagDiagnostic,
} = require('../main/event_loop_lag_watchdog');

test('event-loop lag detector logs once for a sustained stall episode', () => {
  const detector = createEventLoopLagDetector({ lagThresholdMs: 3000, sustainedMs: 5000 });
  const decisions = [
    detector.sample({ now: 1_000, lagMs: 3500 }),
    detector.sample({ now: 4_000, lagMs: 3400 }),
    detector.sample({ now: 6_000, lagMs: 3600 }),
    detector.sample({ now: 8_000, lagMs: 5000 }),
    detector.sample({ now: 10_000, lagMs: 7000 }),
  ];

  assert.deepEqual(
    decisions.map((d) => d.shouldLog),
    [false, false, true, false, false],
  );
});

test('event-loop lag detector treats one long delayed tick as sustained', () => {
  const detector = createEventLoopLagDetector({ lagThresholdMs: 3000, sustainedMs: 3000 });

  assert.equal(detector.sample({ now: 1_000, lagMs: 3000 }).shouldLog, true);
  assert.equal(detector.sample({ now: 2_000, lagMs: 3400 }).shouldLog, false);
});

test('event-loop lag detector ignores brief blips and rearms after recovery', () => {
  const detector = createEventLoopLagDetector({ lagThresholdMs: 3000, sustainedMs: 5000 });

  assert.equal(detector.sample({ now: 1_000, lagMs: 4500 }).shouldLog, false);
  assert.equal(detector.sample({ now: 2_000, lagMs: 10 }).shouldLog, false);
  assert.equal(detector.sample({ now: 3_000, lagMs: 4500 }).shouldLog, false);
  assert.equal(detector.sample({ now: 9_000, lagMs: 4500 }).shouldLog, true);
  assert.equal(detector.sample({ now: 10_000, lagMs: 4500 }).shouldLog, false);
  assert.equal(detector.sample({ now: 11_000, lagMs: 20 }).shouldLog, false);
  assert.equal(detector.sample({ now: 12_000, lagMs: 4500 }).shouldLog, false);
  assert.equal(detector.sample({ now: 18_000, lagMs: 4500 }).shouldLog, true);
});

test('event-loop lag diagnostic carries lag and cheap context', () => {
  const line = formatEventLoopLagDiagnostic({
    now: Date.UTC(2026, 4, 30, 12, 0, 0),
    lagMs: 6123.9,
    sustainedForMs: 5010.2,
    context: {
      memory: { rss: 10, heapTotal: 20, heapUsed: 15, external: 2, arrayBuffers: 1 },
      activeHandles: 3,
      activeRequests: 4,
      lastActivity: { marker: 'tmux:maybe-title-session', at: '2026-05-30T11:59:59.000Z' },
    },
  });

  assert.match(line, /^\[watchdog\] event-loop lag sustained /);
  assert.match(line, /"timestamp":"2026-05-30T12:00:00.000Z"/);
  assert.match(line, /"lag_ms":6124/);
  assert.match(line, /"active_handles":3/);
  assert.match(line, /"marker":"tmux:maybe-title-session"/);
});
