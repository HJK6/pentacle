// Walk: send_two_turns_codex
// public-ui-regression (Phase B)
//
// Same acceptance proof as send_two_turns_claude, against a real codex agent.
const { sendTwoTurns } = require('../lib/flows');

const SCENARIO_META = { target_compat: ['hostc', 'hosta', 'hostb'], requires: ['codex'], providers: ['codex'] };

async function run(ctx) {
  await sendTwoTurns(ctx, 'codex');
}

module.exports = { SCENARIO_META, run };

