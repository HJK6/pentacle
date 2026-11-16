const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(require('node:path').join(__dirname, '../renderer/app.js'), 'utf8');

function sourceFunction(name) {
  const start = source.indexOf(`function ${name}(`);
  const end = source.indexOf('\n}', start) + 2;
  assert.ok(start >= 0 && end > start, `production function ${name} exists`);
  return source.slice(start, end);
}

function sourceArray(name) {
  const start = source.indexOf(`const ${name} = [`);
  const end = source.indexOf('\n];', start) + 3;
  assert.ok(start >= 0 && end > start, `production array ${name} exists`);
  return source.slice(start, end);
}

async function loadStartup({ savedFeatures, loadedFeatures = {} }) {
  let record = { features: { ...savedFeatures } };
  const context = {
    CONFIG: { features: {} },
    window: { cc: { getConfig: async () => ({ features: { ...loadedFeatures }, hostIds: ['workstation'] }) } },
    IS_CLIENT: false,
    HOST_IDS: ['local'],
    _perfRecord() {},
    loadSettingsRecord: () => JSON.parse(JSON.stringify(record)),
    saveSettingsOverride(key, value) {
      record.features ||= {};
      record.features[key] = value;
    },
    saveSettingsRecord(next) { record = JSON.parse(JSON.stringify(next)); },
    loadSettingsOverrides: () => ({ ...record.features }),
    renderConfigWarnings() {},
  };
  vm.runInNewContext([
    sourceArray('SETTINGS_FLAGS'),
    sourceFunction('migrateRemovedChatUiSetting'),
    sourceFunction('applyFeatureOverrides'),
    sourceFunction('chatUiEnabled'),
    sourceFunction('defaultChatViewMode'),
  ].join('\n'), context);
  vm.runInNewContext('migrateRemovedChatUiSetting()', context);
  const start = source.indexOf('const CFG_READY =');
  await vm.runInNewContext(source.slice(start, source.indexOf('})();', start) + 5) + ';CFG_READY', context);
  return { context, record };
}

test('legacy Chat toggle migrates to the default view while Chat stays available and async config applies the host roster', async () => {
  const legacyDisabled = await loadStartup({ savedFeatures: { chatUi: false }, loadedFeatures: { chatUi: false } });
  assert.equal(legacyDisabled.context.chatUiEnabled(), true);
  assert.equal(legacyDisabled.context.defaultChatViewMode(), 'terminal');
  assert.equal(Object.hasOwn(legacyDisabled.record.features, 'chatUi'), false);
  assert.deepEqual(Array.from(legacyDisabled.context.HOST_IDS), ['workstation']);

  const absent = await loadStartup({ savedFeatures: {}, loadedFeatures: {} });
  assert.equal(absent.context.chatUiEnabled(), true);
  assert.equal(absent.context.defaultChatViewMode(), 'chat');
  assert.deepEqual(Array.from(absent.context.HOST_IDS), ['workstation']);

  const explicitChat = await loadStartup({ savedFeatures: { chatUi: false, defaultChatView: true }, loadedFeatures: { chatUi: false } });
  assert.equal(explicitChat.context.chatUiEnabled(), true);
  assert.equal(explicitChat.context.defaultChatViewMode(), 'chat');
  assert.deepEqual(Array.from(explicitChat.context.HOST_IDS), ['workstation']);

  const explicitTerminal = await loadStartup({ savedFeatures: { chatUi: true, defaultChatView: false }, loadedFeatures: { chatUi: true } });
  assert.equal(explicitTerminal.context.chatUiEnabled(), true);
  assert.equal(explicitTerminal.context.defaultChatViewMode(), 'terminal');
  assert.deepEqual(Array.from(explicitTerminal.context.HOST_IDS), ['workstation']);
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
  // lastIndexOf: the STARTUP snapshot pull is the last getChatStreamState().then
  // in the file. The web reconnect re-sync uses the same idiom earlier
  // (spec_pentacle__web_reconnect_input_frozen_2026_09), so anchor on the last
  // occurrence to keep targeting the startup paint.
  const start = source.lastIndexOf('window.cc.getChatStreamState().then((snapshot) => {');
  await vm.runInNewContext(source.slice(start, source.indexOf('\n  });', start) + 6), context);
  assert.equal(context.rows, limits);
  assert.equal(context.health, 'ok');
});
