'use strict';
// Config-trio mechanism regression (public-safe, generic hosts):
//   spec_pentacle__web_thoth_host_color_yellow_2026_09
//   spec_pentacle__web_remove_retired_bart_machine_2026_09
//   spec_pentacle__web_usage_limits_enabled_2026_09
// These three ship as gitignored web-profile changes. This test locks the
// config->behavior MECHANISM they depend on so a future profile edit cannot
// silently break it: a configured `yellow` host resolves to yellow/ibis; a host
// removed from the roster keeps rendering (historical sessions) but is not
// offered as a live execution target; and `features.usage` is loaded verbatim.
// The served /api/config payload and the rendered UI (accent/ibis, usage
// section) are verified separately by the live Thoth headless proof (see spec
// Validation) — that is the authoritative acceptance for those surfaces.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const { loadConfig } = require(path.join(ROOT, 'config-loader'));
const hp = require(path.join(ROOT, 'renderer', 'host_presentation'));
const { executionHosts } = require(path.join(ROOT, 'main', 'execution_host'));

// A generic overlay mirroring the config-trio target SHAPE with no fleet names:
// local host is yellow/ibis; two peers keep distinct colors; `legacy` is a
// retired machine that is NOT in the roster (the Bart-removal end state).
function writeGenericProfile(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-config-trio-'));
  // Placeholder-cleanup baseline: remove the stand-in after the test so it does
  // not leak into the OS temp area across runs.
  if (t && typeof t.after === 'function') t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, 'web.profile.js');
  fs.writeFileSync(file, `'use strict';
module.exports = {
  appName: 'Pentacle', appId: 'com.pentacle.web',
  features: { mic: false, usage: true, chatUi: true, inputBar: true, dashboards: true, sourceTags: true },
  hosts: { builder: { host: 'builder.example', user: 'op', port: 22 }, desk: { host: 'desk.example', user: 'op', port: 22 } },
  chatStream: {
    localHost: 'primary',
    hosts: ['primary', 'builder', 'desk'],
    hostMap: { local: 'primary', remote: 'primary', primary: 'primary', builder: 'builder', desk: 'desk' },
  },
  hostNames: { local: 'Primary', primary: 'Primary', builder: 'Builder', desk: 'Desk' },
  hostColors: { local: 'yellow', primary: 'yellow', builder: 'red', desk: 'royal-blue' },
};
`);
  return { dir, file };
}

test('config-trio: loader reads usage:true and the yellow host colors verbatim', (t) => {
  const { file } = writeGenericProfile(t);
  const { config, warnings } = loadConfig(ROOT, { PENTACLE_CONFIG: file });
  assert.equal(config.features.usage, true, 'usage enabled');
  assert.deepEqual(config.hostColors, { local: 'yellow', primary: 'yellow', builder: 'red', desk: 'royal-blue' });
  assert.deepEqual(config.chatStream.hosts, ['primary', 'builder', 'desk']);
  // The retired host is absent from every roster surface.
  assert.equal(Object.hasOwn(config.hosts, 'legacy'), false);
  assert.equal(Object.hasOwn(config.hostColors, 'legacy'), false);
  assert.equal(Object.hasOwn(config.chatStream.hostMap, 'legacy'), false);
  assert.deepEqual(warnings.map((w) => w.code), [], 'no config warnings');
});

test('config-trio (yellow): local and canonical identity both resolve yellow/ibis; peers unchanged', (t) => {
  const { file } = writeGenericProfile(t);
  const { config } = loadConfig(ROOT, { PENTACLE_CONFIG: file });
  assert.deepEqual([hp.hostColor(config, 'local'), hp.hostSigil(config, 'local')], ['yellow', 'ibis']);
  assert.deepEqual([hp.hostColor(config, 'primary'), hp.hostSigil(config, 'primary')], ['yellow', 'ibis']);
  assert.equal(hp.ACCENTS.yellow, '#ffd60a');
  // No other host's color changes (C2 for the yellow spec).
  assert.deepEqual([hp.hostColor(config, 'builder'), hp.hostSigil(config, 'builder')], ['red', 'sun']);
  assert.deepEqual([hp.hostColor(config, 'desk'), hp.hostSigil(config, 'desk')], ['royal-blue', 'mage']);
});

test('config-trio (retired host): a removed machine still renders history but is not a live target', (t) => {
  const { file } = writeGenericProfile(t);
  const { config } = loadConfig(ROOT, { PENTACLE_CONFIG: file });
  // Historical session on the retired host renders gracefully (no throw), by its
  // capitalised identity and the indexed-palette fallback color — never hidden.
  assert.doesNotThrow(() => hp.hostLabel(config, 'legacy'));
  assert.equal(hp.hostLabel(config, 'legacy'), 'Legacy');
  assert.ok(hp.hostColor(config, 'legacy'), 'a color always resolves');
  assert.ok(hp.hostSigil(config, 'legacy'), 'a sigil always resolves');
  // But the retired host is NOT offered as a live execution target
  // (executionHosts is the provider-relogin / host-picker source).
  const ids = executionHosts(config).map((h) => h.id);
  assert.equal(ids.includes('legacy'), false, 'retired host absent from execution targets');
  assert.deepEqual(ids.sort(), ['builder', 'desk', 'primary']);
});
