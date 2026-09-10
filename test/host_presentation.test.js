const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');
const host = require('../renderer/host_presentation');

test('example-only hosts have labels, first-letter badges and stable indexed palette', () => {
  const config = { chatStream: { hosts: ['local', 'workstation', 'build-box', 'desk', 'fifth'] } };
  assert.equal(host.hostLabel(config, 'workstation'), 'Workstation');
  assert.equal(host.hostLabel(config, 'build-box'), 'Build-box');
  assert.equal(host.initial(' Workstation '), 'W');
  assert.equal(host.initial('🖥 desk'), '🖥');
  assert.deepEqual(config.chatStream.hosts.map(id => host.hostColor(config, id)), [...host.PALETTE, host.PALETTE[0]]);
});

test('local identity uses config precedence and exact mapping, never label substrings', () => {
  const config = { chatStream: { localHost: 'laptop', hostMap: { remote: 'coordinator' } } };
  assert.equal(host.localIdentity(config), 'laptop');
  assert.equal(host.hostLabel(config, 'local'), 'Laptop');
  config.chatStream.hostMap.local = 'mapped';
  assert.equal(host.localIdentity(config), 'mapped');
  config.localHostId = 'explicit';
  assert.equal(host.localIdentity(config), 'explicit');
  assert.equal(host.hostLabel(config, 'local'), 'Explicit');
  assert.equal(host.streamHost(config, 'local'), 'mapped');
  assert.equal(host.streamHost(config, 'remote'), 'coordinator');
  assert.equal(host.streamHost(config, 'abracadabra-hostc'), 'abracadabra-hostc');
  assert.equal(host.hostLabel({}, ''), 'Local');
});

test('explicit names and palette tokens override defaults for desktop or mapped identity', () => {
  const config = { chatStream: { hosts: ['local', 'remote'], hostMap: { remote: 'server' } }, hostNames: { server: 'Build SERVER' }, hostColors: { server: 'orange' } };
  assert.equal(host.hostLabel(config, 'remote'), 'Build SERVER');
  assert.equal(host.hostColor(config, 'remote'), 'orange');
  config.hostNames.remote = 'Alias'; config.hostColors.remote = 'red';
  assert.equal(host.hostLabel(config, 'remote'), 'Alias');
  assert.equal(host.hostColor(config, 'remote'), 'red');
  config.hostColors.remote = 'invalid-token'; delete config.hostColors.server;
  assert.equal(host.hostColor(config, 'remote'), 'royal-blue');
});

test('configuration banner handles empty, single and multiple warnings safely', () => {
  const source = fs.readFileSync(require.resolve('../renderer/app.js'), 'utf8');
  const start = source.indexOf('function renderConfigWarnings()');
  const code = source.slice(start, source.indexOf('\n}', start) + 2);
  const document = new JSDOM('<div id="config-warning-banner"></div>').window.document;
  const CONFIG = { configWarnings: [] };
  const context = { CONFIG, loadedConfig: {}, document };
  vm.runInNewContext(code, context);
  const banner = document.getElementById('config-warning-banner');
  context.renderConfigWarnings(); assert.equal(banner.hidden, true);
  CONFIG.configWarnings = [{ code: 'one', message: '<img src=x>' }];
  context.renderConfigWarnings(); assert.equal(banner.textContent, '<img src=x>');
  assert.equal(banner.querySelector('img'), null); assert.equal(banner.hidden, false);
  CONFIG.configWarnings.push({ code: 'two', message: 'Second warning' });
  context.renderConfigWarnings(); assert.match(banner.textContent, /Second warning/);
});

test('limits renderer retains valid values, shows escaped provider error, and clears it on recovery', () => {
  const source = fs.readFileSync(require.resolve('../renderer/app.js'), 'utf8');
  const start = source.indexOf('function renderLimits(limits, health)');
  const code = source.slice(start, source.indexOf('\n}', start) + 2);
  const document = new JSDOM('<div id="limits-health"></div>').window.document;
  const context = { document, limitsContract: require('../main/limits_contract'), paintLimits(rows) { context.rows = rows; } };
  vm.runInNewContext(code, context);
  const fixture = require('./fixtures/limits_provider_failure.json');
  context.renderLimits(fixture.failed.limits, fixture.failed.limits_health);
  assert.deepEqual(context.rows.map(row => row.pct), [17, 10, 26]);
  const banner = document.getElementById('limits-health');
  assert.match(banner.textContent, /probe denied <retry> & retained/);
  assert.equal(banner.querySelector('retry'), null); assert.equal(banner.hidden, false);
  const previousRows = context.rows;
  context.renderLimits(fixture.healthy.limits, { broken: true });
  assert.equal(context.rows, previousRows); assert.equal(banner.hidden, false);
  context.renderLimits(fixture.healthy.limits, fixture.healthy.limits_health);
  assert.equal(banner.hidden, true); assert.equal(banner.textContent, '');
});

test('local presentation overrides do not change transport and own their colour', () => {
  const config = { localHostId: 'display-machine', chatStream: { localHost: 'legacy-local' }, hostColors: { 'display-machine': 'red', 'transport-machine': 'royal-blue' } };
  assert.equal(host.streamHost(config, 'local'), 'legacy-local');
  config.chatStream.hostMap = { local: 'transport-machine' };
  assert.equal(host.streamHost(config, 'local'), 'transport-machine');
  assert.equal(host.hostColor(config, 'local'), 'red');
  config.hostColors.local = 'orange';
  assert.equal(host.hostColor(config, 'local'), 'orange');
});
