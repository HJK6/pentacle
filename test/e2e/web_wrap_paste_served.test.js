'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { startDaemon, targetId } = require('./scenarios/assistant_direct');
const { freezeWebDist } = require('../../server/web_build_id');
const { withRuntimeDirectory } = require('./lib/runtime_directory');
const { run, parseArgs, resolveChrome } = require('./web_gate');
const seeded = require('./fixtures/wrap-paste.json');

function skipReason() {
  if (!fs.existsSync(path.join(__dirname, '../../renderer/dist/web/web.html'))) return 'web build missing';
  try { resolveChrome(); } catch (error) { return error.message; }
  return false;
}

test('served adapter uses actual shortened source and refuses a dirty chat before another send', { skip: skipReason(), timeout: 180000 }, async () => {
  const out = fs.mkdtempSync(path.join(os.tmpdir(), 'served-wrap-paste-'));
  const prior = process.env.PENTACLE_WEB_GATE_REPORT_ROOT;
  process.env.PENTACLE_WEB_GATE_REPORT_ROOT = path.join(process.env.PENTACLE_GATE_EVIDENCE_DIR || out, 'served-proof');
  try {
    await withRuntimeDirectory(async () => {
      const daemon = await startDaemon({ artifactsDir: out, replyText: seeded.text });
      daemon.events.length = 0;
      let host;
      try {
        host = await require('../../server').main(['--profile', daemon.configFile, '--bind', '127.0.0.1', '--port', '0']);
        const proof = { phase: 'rehearsal', fixtureId: 'synthetic-served-rehearsal', streamId: targetId,
          expectedBuildId: freezeWebDist(path.join(__dirname, '../../renderer/dist/web')).buildId,
          sourceSha: '0'.repeat(40) };
        const args = { ...parseArgs(['--scenario', 'web-wrap-paste']), servedUrl: host.url, wrapProof: proof };
        assert.equal(await run(args), 0, 'real served UI/host with synthetic provider counterpart');
        const replies = daemon.events.filter(row => row.kind === 'ASSIST_TEXT');
        assert.equal(replies[0].text, seeded.text, 'source differs from requested 600-character strings');
        const sends = daemon.requests.filter(row => row.type === 'send').length;
        assert.equal(sends, 2, 'render request and image send');
        assert.equal(await run({ ...args, wrapProof: { ...proof, fixtureId: 'dirty-fixture-control' } }), 1);
        assert.equal(daemon.requests.filter(row => row.type === 'send').length, sends, 'dirty fixture refusal precedes mutation');
      } finally {
        fs.writeFileSync(path.join(process.env.PENTACLE_GATE_EVIDENCE_DIR || out, 'served-fixture-state.json'), JSON.stringify({ sends: daemon.requests.filter(row => row.type === 'send'), events: daemon.events }, null, 2));
        await host?.close(); await daemon.stop();
      }
    });
  } finally {
    if (prior === undefined) delete process.env.PENTACLE_WEB_GATE_REPORT_ROOT;
    else process.env.PENTACLE_WEB_GATE_REPORT_ROOT = prior;
    // Test-owned synthetic assets only; no shared runtime or profile lives here.
    fs.rmSync(out, { recursive: true, force: true });
  }
});

