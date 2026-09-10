const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(require('node:path').join(__dirname, '../renderer/app.js'), 'utf8');

test('async config preserves saved Chat opt-in and refreshes machine badges', async () => {
  const context = { CONFIG: { features: { chatUi: true } }, window: { cc: { getConfig: async () => ({ features: { chatUi: false }, hostIds: ['workstation'] }) } },
    SETTINGS_FLAGS: [{ key: 'chatUi' }], IS_CLIENT: false, HOST_IDS: ['local'], _perfRecord() {}, loadSettingsOverrides: () => ({ chatUi: true }), renderConfigWarnings() {}, renderTitlebarMachines() { context.badges = [...context.HOST_IDS]; } };
  const start = source.indexOf('const CFG_READY =');
  await vm.runInNewContext(source.slice(start, source.indexOf('})();', start) + 5) + ';CFG_READY', context);
  assert.equal(context.CONFIG.features.chatUi, true);
  assert.deepEqual(context.badges, ['workstation']);
});

test('configured stream host mapping takes priority over legacy aliases', () => {
  const start = source.indexOf('function streamHostForHostId(');
  const context = { hostPresentation: require('../renderer/host_presentation'), CONFIG: { chatStream: { hostMap: { local: 'workstation', remote: 'coordinator' } } }, window: {} };
  vm.runInNewContext(source.slice(start, source.indexOf('\n}', start) + 2), context);
  assert.equal(context.streamHostForHostId('local'), 'workstation');
  assert.equal(context.streamHostForHostId('remote'), 'coordinator');
});

test('renderer paints limits already cached before the window opened', async () => {
  const limits = [{ id: 'claude' }, { id: 'fable' }, { id: 'codex' }];
  const context = { window: { cc: { getChatStreamState: async () => ({ limits, limits_health: 'ok' }) } },
    applyChatStreamState() {}, warmSpawnCatalog() {}, bindChatPopout() {}, IS_CHAT_POPOUT: false,
    renderLimits(rows, health) { context.rows = rows; context.health = health; } };
  const start = source.indexOf('window.cc.getChatStreamState().then((snapshot) => {');
  await vm.runInNewContext(source.slice(start, source.indexOf('\n  });', start) + 6), context);
  assert.equal(context.rows, limits);
  assert.equal(context.health, 'ok');
});
