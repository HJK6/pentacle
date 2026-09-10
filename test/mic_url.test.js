const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { loadConfig } = require('../config-loader');
const { resolveMicUrl, shouldSpawnLocalMicServer } = require('../main/mic-url');

function withWarnSpy(fn) {
  const original = console.warn;
  const calls = [];
  console.warn = (...args) => calls.push(args.join(' '));
  return Promise.resolve()
    .then(() => fn(calls))
    .finally(() => {
      console.warn = original;
    });
}

test('test_resolve_mic_url_local', () => {
  const url = resolveMicUrl({
    micServerUrl: 'http://127.0.0.1:7780',
    mic: { useStreamHost: false },
  });
  assert.equal(url, 'http://127.0.0.1:7780');
});

test('test_resolve_mic_url_stream_host', () => {
  const url = resolveMicUrl({
    chatStream: { url: 'ws://10.0.0.0:7791' },
    mic: { useStreamHost: true },
  });
  assert.equal(url, 'http://10.0.0.0:7780');
});

test('test_resolve_mic_url_stream_host_loopback', () => {
  const url = resolveMicUrl({
    chatStream: { url: 'ws://127.0.0.1:7791' },
    mic: { useStreamHost: true },
  });
  assert.equal(url, 'http://127.0.0.1:7780');
});

test('test_resolve_mic_url_stream_host_missing_chat_url_falls_back', async () => {
  await withWarnSpy((calls) => {
    const url = resolveMicUrl({
      micServerUrl: 'http://127.0.0.1:7780',
      mic: { useStreamHost: true },
    });
    assert.equal(url, 'http://127.0.0.1:7780');
    assert.equal(calls.length, 1);
    assert.match(calls[0], /useStreamHost=true but chatStream\.url is unset/);
  });
});

test('test_resolve_mic_url_stream_host_invalid_chat_url_falls_back', async () => {
  await withWarnSpy((calls) => {
    const url = resolveMicUrl({
      micServerUrl: 'http://127.0.0.1:7780',
      chatStream: { url: 'not a url' },
      mic: { useStreamHost: true },
    });
    assert.equal(url, 'http://127.0.0.1:7780');
    assert.equal(calls.length, 1);
    assert.match(calls[0], /useStreamHost=true but chatStream\.url is invalid/);
  });
});

test('test_resolve_mic_url_with_input_rehearsal', () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'pentacle-mic-config-'));
  const configPath = path.join(tmp, 'pentacle.config.js');
  fs.writeFileSync(configPath, `
module.exports = {
  features: { mic: true },
  chatStream: { url: 'ws://10.0.0.0:7791' },
  mic: { useStreamHost: true, alwaysOnEnabled: false },
};
`);
  try {
    const loaded = loadConfig(tmp, {}, 'hostb').config;
    assert.equal(resolveMicUrl(loaded), 'http://10.0.0.0:7780');
    assert.equal(shouldSpawnLocalMicServer(loaded), false);
  } finally {
    delete require.cache[require.resolve(configPath)];
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test('test_should_spawn_local_mic_server_truth_table', () => {
  const cases = [
    ['missing config', undefined, false],
    ['features.mic false', { features: { mic: false } }, false],
    ['default local', { features: { mic: true } }, true],
    ['use stream host', { features: { mic: true }, mic: { useStreamHost: true } }, false],
    ['autoSpawn false', { features: { mic: true }, mic: { autoSpawn: false } }, false],
    ['autoSpawn true', { features: { mic: true }, mic: { autoSpawn: true } }, true],
  ];

  for (const [name, config, expected] of cases) {
    assert.equal(shouldSpawnLocalMicServer(config), expected, name);
  }
});

