// Unit tests for the harness's pure verdict/taxonomy logic.
// spec_pentacle_desktop_chat_ui_e2e_walk_harness_2026_05_25 (Phase E)
// Run: node --test test/e2e/lib/verdict.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const { STATUS, EXIT_CODES, isStatus, exitCodeFor, computeVerdict, aggregateMatrix } = require('./verdict');

test('exitCodeFor maps each status; PASS/WAIVED=0, FAIL=1, SETUP_FAIL=2, SKIPPED=3', () => {
  assert.equal(exitCodeFor(STATUS.PASS), 0);
  assert.equal(exitCodeFor(STATUS.WAIVED), 0);
  assert.equal(exitCodeFor(STATUS.FAIL), 1);
  assert.equal(exitCodeFor(STATUS.SETUP_FAIL), 2);
  assert.equal(exitCodeFor(STATUS.SKIPPED), 3);
  assert.throws(() => exitCodeFor('NONSENSE'));
});

test('computeVerdict: all steps ok -> PASS', () => {
  const v = computeVerdict({ steps: [{ name: 'a', ok: true }, { name: 'b', ok: true }] });
  assert.equal(v.status, STATUS.PASS);
  assert.equal(v.reason, null);
});

test('computeVerdict: any failing step -> FAIL with the failing names', () => {
  const v = computeVerdict({ steps: [{ name: 'a', ok: true }, { name: 'b', ok: false }, { name: 'c', ok: false }] });
  assert.equal(v.status, STATUS.FAIL);
  assert.match(v.reason, /b/);
  assert.match(v.reason, /c/);
});

test('computeVerdict precedence: skip > waive > setup_fail > step-fail', () => {
  assert.equal(computeVerdict({ skipReason: 's', waived: 'w', setupError: 'e', steps: [{ name: 'x', ok: false }] }).status, STATUS.SKIPPED);
  assert.equal(computeVerdict({ waived: 'w', setupError: 'e', steps: [{ name: 'x', ok: false }] }).status, STATUS.WAIVED);
  assert.equal(computeVerdict({ setupError: 'e', steps: [{ name: 'x', ok: false }] }).status, STATUS.SETUP_FAIL);
  assert.equal(computeVerdict({ steps: [{ name: 'x', ok: false }] }).status, STATUS.FAIL);
});

test('aggregateMatrix: pass-rate = PASS/(PASS+FAIL), skips/setup/waived excluded from denom', () => {
  const agg = aggregateMatrix([
    { status: STATUS.PASS }, { status: STATUS.PASS }, { status: STATUS.PASS },
    { status: STATUS.FAIL },
    { status: STATUS.SKIPPED }, { status: STATUS.SKIPPED },
    { status: STATUS.WAIVED }, { status: STATUS.SETUP_FAIL },
  ]);
  assert.equal(agg.counts.PASS, 3);
  assert.equal(agg.counts.FAIL, 1);
  assert.equal(agg.counts.SKIPPED, 2);
  assert.equal(agg.passRate, 3 / 4);
  assert.equal(agg.exitCode, EXIT_CODES.FAIL); // any FAIL -> exit 1
});

test('aggregateMatrix: no FAIL but a SETUP_FAIL -> exit 2', () => {
  const agg = aggregateMatrix([{ status: STATUS.PASS }, { status: STATUS.SETUP_FAIL }, { status: STATUS.SKIPPED }]);
  assert.equal(agg.exitCode, EXIT_CODES.SETUP_FAIL);
  assert.equal(agg.passRate, 1); // 1 PASS / (1 PASS + 0 FAIL)
});

test('aggregateMatrix: all skipped -> passRate null, exit 0', () => {
  const agg = aggregateMatrix([{ status: STATUS.SKIPPED }, { status: STATUS.SKIPPED }]);
  assert.equal(agg.passRate, null);
  assert.equal(agg.exitCode, 0);
});

test('isStatus validates the taxonomy', () => {
  assert.ok(isStatus('PASS'));
  assert.ok(!isStatus('MAYBE'));
});
