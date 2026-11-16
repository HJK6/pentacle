// Pure verdict / taxonomy logic for the desktop E2E walk harness.
// spec_pentacle_desktop_chat_ui_e2e_walk_harness_2026_05_25
//
// No I/O, no CDP — pure functions so they are unit-tested directly (Phase E AC:
// "the harness's own pure logic ... is unit-tested"). Mirrors pentacle-mobile's
// PASS/FAIL/SETUP_FAIL/WAIVED/SKIPPED taxonomy + exit codes.

const STATUS = Object.freeze({
  PASS: 'PASS',
  FAIL: 'FAIL',
  SETUP_FAIL: 'SETUP_FAIL',
  WAIVED: 'WAIVED',
  SKIPPED: 'SKIPPED',
});

const ALL_STATUSES = Object.freeze(Object.values(STATUS));

// Exit codes (single-scenario runs). Aligned with mobile: SKIPPED=3, SETUP_FAIL=2.
const EXIT_CODES = Object.freeze({
  PASS: 0,
  WAIVED: 0,
  FAIL: 1,
  SETUP_FAIL: 2,
  SKIPPED: 3,
});

function isStatus(s) {
  return ALL_STATUSES.includes(s);
}

function exitCodeFor(status) {
  if (!isStatus(status)) throw new Error(`unknown status: ${status}`);
  return EXIT_CODES[status];
}

/**
 * Build a single-scenario verdict from recorded steps.
 * steps: [{ name, ok, detail? }]. A scenario is PASS iff every step ok.
 * If `setupError` is set -> SETUP_FAIL; if `skipReason` -> SKIPPED; if `waived`
 * -> WAIVED. Precedence: skip > waive > setup_fail > step-fail > pass.
 */
function computeVerdict({ steps = [], setupError = null, skipReason = null, waived = null } = {}) {
  if (skipReason) return { status: STATUS.SKIPPED, reason: skipReason, steps };
  if (waived) return { status: STATUS.WAIVED, reason: waived, steps };
  if (setupError) return { status: STATUS.SETUP_FAIL, reason: String(setupError), steps };
  const failed = steps.filter((s) => !s.ok);
  if (failed.length > 0) {
    return { status: STATUS.FAIL, reason: failed.map((s) => s.name).join('; '), steps };
  }
  return { status: STATUS.PASS, reason: null, steps };
}

/**
 * Aggregate a matrix of verdicts. Pass-rate = PASS / (PASS + FAIL); skips,
 * setup-fails, and waivers are excluded from the denominator (mobile semantics).
 * Matrix exit code: FAIL if any FAIL, else SETUP_FAIL if any SETUP_FAIL, else 0.
 */
function aggregateMatrix(verdicts) {
  const counts = { PASS: 0, FAIL: 0, SETUP_FAIL: 0, WAIVED: 0, SKIPPED: 0 };
  for (const v of verdicts) {
    if (counts[v.status] === undefined) throw new Error(`unknown status in matrix: ${v.status}`);
    counts[v.status] += 1;
  }
  const denom = counts.PASS + counts.FAIL;
  const passRate = denom === 0 ? null : counts.PASS / denom;
  let exitCode = 0;
  if (counts.FAIL > 0) exitCode = EXIT_CODES.FAIL;
  else if (counts.SETUP_FAIL > 0) exitCode = EXIT_CODES.SETUP_FAIL;
  return { counts, passRate, exitCode, total: verdicts.length };
}

module.exports = {
  STATUS,
  ALL_STATUSES,
  EXIT_CODES,
  isStatus,
  exitCodeFor,
  computeVerdict,
  aggregateMatrix,
};
