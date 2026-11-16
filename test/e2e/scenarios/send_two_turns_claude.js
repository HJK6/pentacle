// Walk: send_two_turns_claude
// public-ui-regression (Phase B)
//
// THE acceptance proof that send works end-to-end with a real claude agent:
// spawn a freshly-spawned THROWAWAY local claude chat and drive two REAL turns
// through the composer, asserting optimistic_insert -> reconciled -> reply from
// telemetry. Runner force-deletes the throwaway in teardown.
const { sendTwoTurns } = require('../lib/flows');

const SCENARIO_META = { target_compat: ['hostc', 'hosta', 'hostb'], requires: [], providers: ['claude'] };

async function run(ctx) {
  await sendTwoTurns(ctx, 'claude');
}

module.exports = { SCENARIO_META, run };

