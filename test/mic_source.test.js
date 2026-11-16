const test = require('node:test');
const assert = require('node:assert/strict');

const { MIC_SOURCE_STORAGE_KEY, micSourceStorage, computeMicSourceState, micRoomSourceLabel } = require('../renderer/mic-state');
const { audioConstraint, createBrowserRecorder } = require('../renderer/web_voice');

const input = (deviceId, label = '') => ({ kind: 'audioinput', deviceId, label });
const memoryStorage = (initial = {}) => {
  const values = new Map(Object.entries(initial));
  return { getItem: key => (values.has(key) ? values.get(key) : null), setItem: (key, value) => values.set(key, String(value)), removeItem: key => values.delete(key), values };
};

test('default is the room Source with no stored value, and This device needs a secure context and an input', () => {
  const fresh = computeMicSourceState({ secureContext: true, audioInputs: [input('a', 'Mic A')], stored: micSourceStorage.read(memoryStorage()) });
  assert.equal(fresh.active, 'room');
  assert.deepEqual(fresh.options, ['room', 'device']);
  assert.equal(fresh.deviceLost, false);
  assert.deepEqual(computeMicSourceState({ secureContext: true, audioInputs: [] }).options, ['room']);
  assert.deepEqual(computeMicSourceState({ secureContext: false, audioInputs: [input('a')] }).options, ['room']);
  assert.deepEqual(computeMicSourceState({ secureContext: true, audioInputs: [{ kind: 'audiooutput', deviceId: 'speaker' }] }).options, ['room']);
});

test('picker lists System default first, then real inputs; aliases are skipped and placeholders label unnamed inputs', () => {
  const view = computeMicSourceState({ secureContext: true, audioInputs: [input('default', 'Default - Mic A'), input('a', 'Mic A'), input('b')], stored: { source: 'device', deviceId: 'b' } });
  assert.deepEqual(view.devices, [{ deviceId: '', label: 'System default' }, { deviceId: 'a', label: 'Mic A' }, { deviceId: 'b', label: 'Microphone 2' }]);
  assert.equal(view.active, 'device');
  assert.equal(view.selectedDeviceId, 'b');
  assert.equal(view.activeLabel, 'Microphone 2');
  const systemDefault = computeMicSourceState({ secureContext: true, audioInputs: [input('a', 'Mic A')], stored: { source: 'device', deviceId: '' } });
  assert.equal(systemDefault.selectedDeviceId, '');
  assert.equal(systemDefault.activeLabel, 'System default');
});

test('a stored device missing from a known enumeration is lost and falls back to the room Source', () => {
  const lost = computeMicSourceState({ secureContext: true, audioInputs: [input('a', 'Mic A')], stored: { source: 'device', deviceId: 'b' } });
  assert.equal(lost.deviceLost, true);
  assert.equal(lost.active, 'room');
  assert.equal(lost.selectedDeviceId, '');
  const noInputs = computeMicSourceState({ secureContext: true, audioInputs: [], stored: { source: 'device', deviceId: '' } });
  assert.equal(noInputs.deviceLost, true);
  assert.equal(noInputs.active, 'room');
  // Before permission the browser hides ids, so loss cannot be judged; keep the choice.
  const hidden = computeMicSourceState({ secureContext: true, audioInputs: [input('')], stored: { source: 'device', deviceId: 'b' } });
  assert.equal(hidden.deviceLost, false);
  assert.equal(hidden.selectedDeviceId, 'b');
  assert.equal(hidden.activeLabel, 'Selected microphone');
});

test('storage persists the device choice, clears for the room Source, and tolerates bad or unavailable storage', () => {
  const storage = memoryStorage();
  assert.equal(micSourceStorage.write(storage, { source: 'device', deviceId: 'b' }), true);
  assert.deepEqual(JSON.parse(storage.getItem(MIC_SOURCE_STORAGE_KEY)), { source: 'device', deviceId: 'b' });
  assert.deepEqual(micSourceStorage.read(storage), { source: 'device', deviceId: 'b' });
  assert.equal(micSourceStorage.write(storage, null), true);
  assert.equal(storage.getItem(MIC_SOURCE_STORAGE_KEY), null);
  micSourceStorage.write(storage, { source: 'room' });
  assert.equal(storage.values.has(MIC_SOURCE_STORAGE_KEY), false);
  assert.deepEqual(micSourceStorage.read(memoryStorage({ [MIC_SOURCE_STORAGE_KEY]: '{not json' })), { source: 'room', deviceId: '' });
  assert.deepEqual(micSourceStorage.read(memoryStorage({ [MIC_SOURCE_STORAGE_KEY]: '{"source":"other"}' })), { source: 'room', deviceId: '' });
  const throwing = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); }, removeItem() { throw new Error('denied'); } };
  assert.deepEqual(micSourceStorage.read(throwing), { source: 'room', deviceId: '' });
  assert.equal(micSourceStorage.write(throwing, { source: 'device', deviceId: 'b' }), false);
  assert.deepEqual(micSourceStorage.read(null), { source: 'room', deviceId: '' });
});

test('the room Source is labelled with the web host machine name, else Room mic', () => {
  assert.equal(micRoomSourceLabel({ hostNames: { local: 'Desk', other: 'Other' }, localHost: 'local' }), 'Desk');
  assert.equal(micRoomSourceLabel({ hostNames: { other: 'Other' }, localHost: 'local' }), 'Room mic');
  assert.equal(micRoomSourceLabel({ hostNames: { local: '  ' }, localHost: 'local' }), 'Room mic');
  assert.equal(micRoomSourceLabel({}), 'Room mic');
});

function recorderEnv(getUserMedia) {
  class Recorder {
    static isTypeSupported(type) { return type === 'audio/mp4'; }
    start() {} stop() { this.ondataavailable({ data: new Blob(['mp4']) }); this.onstop(); }
  }
  class AudioContext {
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createAnalyser() { return { getFloatTimeDomainData(samples) { samples.fill(0); }, disconnect() {} }; }
    async resume() {} async close() {}
  }
  return { AudioContext, MediaRecorder: Recorder, navigator: { mediaDevices: { getUserMedia } } };
}

test('constraint builder: browser default without a device, exact deviceId with one', () => {
  assert.equal(audioConstraint(null), true);
  assert.equal(audioConstraint(''), true);
  assert.deepEqual(audioConstraint('b'), { deviceId: { exact: 'b' } });
});

test('recorder passes the chosen device to getUserMedia and reports start', async () => {
  const constraints = []; let deviceId = null; let starts = 0;
  const env = recorderEnv(async constraint => { constraints.push(constraint); return { getTracks: () => [{ stop() {} }] }; });
  const recorder = createBrowserRecorder(env, { getDeviceId: () => deviceId, onCaptureStart: () => starts++ });
  await recorder.start(); await recorder.stop();
  deviceId = 'b';
  await recorder.start(); await recorder.stop();
  assert.deepEqual(constraints, [{ audio: true }, { audio: { deviceId: { exact: 'b' } } }]);
  assert.equal(starts, 2);
});

test('recorder reports a vanished chosen device but not permission errors', async () => {
  const failures = []; let error;
  const env = recorderEnv(async () => { throw error; });
  const recorder = createBrowserRecorder(env, { getDeviceId: () => 'b', onDeviceFailure: id => failures.push(id) });
  error = Object.assign(new Error('gone'), { name: 'OverconstrainedError' });
  await assert.rejects(recorder.start(), /gone/);
  error = Object.assign(new Error('denied'), { name: 'NotAllowedError' });
  await assert.rejects(recorder.start(), /denied/);
  assert.deepEqual(failures, ['b']);
  const defaultRecorder = createBrowserRecorder(env, { onDeviceFailure: id => failures.push(id) });
  error = Object.assign(new Error('none'), { name: 'NotFoundError' });
  await assert.rejects(defaultRecorder.start(), /none/);
  assert.deepEqual(failures, ['b']);
});
