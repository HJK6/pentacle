'use strict';

// Port of the mobile RecordingStrip / VoiceRecordFace contract. All levels,
// capture devices and clocks here are synthetic; no microphone is opened.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const { bindComposerMic, createBrowserRecorder } = require('../renderer/web_voice');
const flush = () => new Promise(resolve => setImmediate(resolve));
const css = fs.readFileSync(path.join(__dirname, '../renderer/chat_v3.css'), 'utf8');

function fixture(t) {
  const dom = new JSDOM(`<style>${css}</style><div class="cosmic"><div id="dock">
    <div id="panel"></div><div class="slot-chat-compose is-expanded">
      <textarea class="slot-chat-compose-input">Unsent draft</textarea>
      <div class="slot-chat-plus-wrap"><button aria-label="Add image">+</button></div>
      <div class="slot-chat-attachment-tray" style="display:flex">Synthetic image</div>
      <button class="slot-chat-compose-mic"></button><button class="slot-chat-compose-send">Send</button>
    </div></div><div id="takes"></div></div>`);
  const doc = dom.window.document;
  const button = doc.querySelector('.slot-chat-compose-mic');
  const panel = doc.querySelector('#panel');
  const composer = doc.querySelector('.slot-chat-compose');
  const intervals = new Map(); const calls = []; let now = 0; let level = 0; let nextId = 0;
  const env = { isSecureContext: true, navigator: { mediaDevices: { getUserMedia() {} } },
    performance: { now: () => now },
    setInterval(fn, ms) { const id = ++nextId; intervals.set(id, { fn, ms }); return id; },
    clearInterval(id) { intervals.delete(id); },
  };
  const controller = bindComposerMic({ web: true, env, button, panel, composer, takeMount: doc.querySelector('#takes'),
    getStreamId: () => 'fixture:origin',
    recorder: {
      async start() { calls.push('start'); },
      poll() { calls.push('poll'); return { level, durationMs: now }; },
      async stop() { calls.push('stop'); return { blob: new Blob(['synthetic'], { type: 'audio/wav' }), durationS: now / 1000 }; },
      async discard() { calls.push('discard'); },
    },
    async upload() { calls.push('upload'); return { ok: true, blob_sha: 'a'.repeat(64) }; },
    async transcribe() { calls.push('transcribe'); return { ok: true, text: 'Synthetic transcript' }; },
    send() { calls.push('send'); return 'optimistic-fixture'; },
  });
  t.after(async () => { await controller.cancel(); dom.window.close(); });
  return { dom, doc, button, panel, composer, intervals, calls, controller,
    style: el => dom.window.getComputedStyle(el),
    async start() { button.click(); await flush(); },
    sample(value, elapsedMs) { level = value; now = elapsedMs ?? now + 90; for (const { fn } of [...intervals.values()]) fn(); },
  };
}

test('recording samples at mobile 90ms cadence, keeps last 46 bars and preserves existing grow-in nodes', async t => {
  const u = fixture(t); await u.start();
  assert.deepEqual([...u.intervals.values()].map(item => item.ms), [90]);
  assert.equal(u.panel.querySelectorAll('.voice-recording-bar').length, 0);
  u.sample(.5); const first = u.panel.querySelector('.voice-recording-bar');
  assert.ok(first, 'first real sample appears in the strip');
  u.sample(1); assert.equal(u.panel.querySelector('.voice-recording-bar'), first, 'old bars must not restart their animation');
  for (let i = 2; i < 60; i++) u.sample(i / 60);
  const bars = [...u.panel.querySelectorAll('.voice-recording-bar')];
  assert.equal(bars.length, 46); assert.equal(bars[0].dataset.sample, '14'); assert.equal(bars.at(-1).dataset.sample, '59');
  assert.equal(u.calls.filter(call => call === 'poll').length, 60);
});

test('synthetic levels render mobile bar geometry, opacity, 180ms grow-in and right alignment', async t => {
  const u = fixture(t); await u.start();
  for (const level of [.5, 1, 0, -1, 2, NaN]) u.sample(level);
  const bars = [...u.panel.querySelectorAll('.voice-recording-bar')];
  assert.equal(bars.length, 6);
  assert.deepEqual(bars.map(bar => Number.parseFloat(u.style(bar).height)), [13, 26, 1, 1, 26, 1]);
  const opacity = [.725, 1, .45, .45, 1, .45];
  bars.forEach((bar, i) => assert.ok(Math.abs(Number.parseFloat(u.style(bar).opacity) - opacity[i]) < 1e-9));
  assert.equal(u.style(bars[0]).width, '2.5px'); assert.equal(u.style(bars[0]).borderRadius, '2px');
  assert.match(u.style(bars[0]).animation, /voice-bar-grow 180ms ease-out/);
  const area = u.style(u.panel.querySelector('.voice-recording-bars'));
  assert.equal(area.height, '26px'); assert.equal(area.gap, '2px'); assert.equal(area.justifyContent, 'flex-end');
  assert.match(css, /@keyframes voice-bar-grow\s*\{\s*from\s*\{\s*transform:\s*scaleY\(0\.2\)/);
});

test('recording uses the capsule tint, no nested frame, hidden attach and preserved draft', async t => {
  const u = fixture(t); await u.start();
  assert.ok(u.composer.classList.contains('is-voice-recording'));
  assert.equal(u.panel.parentElement, u.composer);
  assert.equal(u.style(u.panel).borderWidth, '0px'); assert.equal(u.style(u.panel).padding, '0px');
  assert.equal(u.style(u.composer).display, 'flex', 'recording overrides expanded composer grid');
  assert.equal(u.style(u.composer).borderColor, 'rgba(61, 255, 102, 0.4)');
  assert.equal(u.style(u.composer).backgroundColor, 'rgba(61, 255, 102, 0.055)');
  for (const selector of ['.slot-chat-plus-wrap', '.slot-chat-compose-input', '.slot-chat-compose-send']) {
    assert.equal(u.style(u.doc.querySelector(selector)).display, 'none', selector);
  }
  assert.equal(u.doc.querySelector('textarea').value, 'Unsent draft');
  await u.controller.cancel();
  assert.equal(u.composer.classList.contains('is-voice-recording'), false);
  assert.equal(u.doc.querySelector('textarea').value, 'Unsent draft'); assert.equal(u.panel.hidden, true);
  assert.equal(u.panel.parentElement.id, 'dock'); assert.equal(u.intervals.size, 0);
});

test('strip has discard X, 1s step blink and tabular m:ss timer; discard never uploads', async t => {
  const u = fixture(t); await u.start(); u.sample(.4, 61000);
  const timer = u.panel.querySelector('.voice-recording-timer');
  assert.ok(timer); assert.equal(timer.textContent, '1:01');
  assert.equal(u.style(timer).fontVariantNumeric, 'tabular-nums'); assert.equal(u.style(timer).fontSize, '12px');
  const dot = u.panel.querySelector('.voice-recording-dot');
  assert.equal(u.style(dot).width, '7px'); assert.equal(u.style(dot).height, '7px');
  assert.match(u.style(dot).animation, /voice-record-blink 1s steps\(1, end\) infinite/);
  assert.match(css, /@keyframes voice-record-blink[\s\S]*?50%\s*\{\s*opacity:\s*0\.25/);
  const discard = u.panel.querySelector('[aria-label="Discard recording"]');
  assert.ok(discard?.querySelector('svg'), 'discard is the outline X glyph');
  discard.click(); await flush();
  assert.equal(u.calls.includes('discard'), true); assert.equal(u.calls.includes('upload'), false); assert.equal(u.panel.hidden, true);
});

test('stop-and-send is the mobile 40px green circle with ink mic and staggered pulse rings', async t => {
  const u = fixture(t); await u.start();
  const style = u.style(u.button);
  assert.equal(u.button.getAttribute('aria-label'), 'Stop and send');
  assert.equal(style.width, '40px'); assert.equal(style.height, '40px'); assert.equal(style.borderRadius, '50%');
  // JSDOM does not resolve CSS custom properties; inspect the declarations.
  // The browser gate checks the final computed green on the mounted composer.
  const rule = [...u.doc.styleSheets[0].cssRules].find(rule => rule.selectorText === '.cosmic .slot-chat-compose-mic.is-recording');
  assert.equal(rule.style.background, 'var(--cosmic-green, #3dff66)');
  assert.equal(rule.style.color, 'var(--cosmic-ink, #080b0a)');
  const rings = [...u.button.querySelectorAll('.voice-record-ring')]; assert.equal(rings.length, 2);
  assert.match(u.style(rings[0]).animation, /voice-record-pulse 1\.4s ease-out infinite/);
  assert.equal(u.style(rings[1]).animationDelay, '0.7s'); assert.equal(u.style(rings[1]).opacity, '0');
  assert.ok(u.button.querySelector('svg rect[height="11"]'));
  u.sample(.3, 1500); u.button.click(); u.button.click(); await flush();
  assert.deepEqual(u.calls.filter(call => call !== 'poll'), ['start', 'stop', 'upload', 'transcribe', 'send']);
  assert.equal(u.button.querySelectorAll('.voice-record-ring').length, 0); assert.equal(u.intervals.size, 0);
});

function audioFixture({ mp4 = true } = {}) {
  const calls = []; let sample = .001; let now = 0; let processor;
  const source = { connect(node) { calls.push(['connect', node]); }, disconnect() { calls.push(['source-disconnect']); } };
  const analyser = { fftSize: 0, getFloatTimeDomainData(buffer) { buffer.fill(sample); }, disconnect() { calls.push(['analyser-disconnect']); } };
  class AudioContext {
    sampleRate = 16000; destination = {};
    createMediaStreamSource() { return source; }
    createAnalyser() { return analyser; }
    createScriptProcessor() { processor = { connect() {}, disconnect() { calls.push(['processor-disconnect']); } }; return processor; }
    async resume() { calls.push(['resume']); }
    async close() { calls.push(['close']); }
  }
  class MediaRecorder {
    static isTypeSupported(type) { return mp4 && type === 'audio/mp4'; }
    state = 'inactive';
    start() { this.state = 'recording'; }
    stop() { this.state = 'inactive'; this.ondataavailable({ data: new Blob(['synthetic']) }); this.onstop(); }
  }
  const env = { AudioContext, MediaRecorder, performance: { now: () => now },
    navigator: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop() { calls.push(['track-stop']); } }] }) } } };
  return { recorder: createBrowserRecorder(env), calls, analyser, source,
    setSample(value, time = now) { sample = value; now = time; },
    capture() { processor.onaudioprocess({ inputBuffer: { getChannelData: () => new Float32Array([0, .1, -.1]) } }); },
  };
}

for (const mp4 of [true, false]) test(`${mp4 ? 'MP4' : 'WAV'} recorder feeds live RMS metering normalized to mobile -60..0dB range and releases audio graph`, async () => {
  const u = audioFixture({ mp4 }); await u.recorder.start();
  try {
    assert.equal(typeof u.recorder.poll, 'function');
    assert.equal(u.analyser.fftSize, 2048);
    assert.ok(u.calls.some(([name, node]) => name === 'connect' && node === u.analyser));
    for (const [sample, expected] of [[0, 0], [.001, 0], [.01, 1 / 3], [.1, 2 / 3], [1, 1]]) {
      u.setSample(sample, 1234); const poll = u.recorder.poll();
      assert.ok(Math.abs(poll.level - expected) < 1e-6, `sample ${sample}: ${poll.level}`);
      assert.equal(poll.durationMs, 1234);
    }
    if (!mp4) u.capture();
    const audio = await u.recorder.stop(); assert.equal(audio.blob.type, mp4 ? 'audio/mp4' : 'audio/wav');
    assert.equal(u.calls.filter(([name]) => name === 'track-stop').length, 1);
    assert.equal(u.calls.filter(([name]) => name === 'close').length, 1);
    assert.equal(u.calls.filter(([name]) => name === 'analyser-disconnect').length, 1);
  } finally { await u.recorder.discard(); }
});

test('meter initialization failure closes the context and releases the microphone before Retry', async () => {
  let stopped = 0; let closed = 0;
  class AudioContext {
    async resume() {}
    createMediaStreamSource() { return { disconnect() {} }; }
    createAnalyser() { throw new Error('Synthetic meter failure'); }
    async close() { closed++; }
  }
  class MediaRecorder { static isTypeSupported() { return true; } start() {} }
  const recorder = createBrowserRecorder({ AudioContext, MediaRecorder,
    navigator: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop() { stopped++; } }] }) } } });
  await assert.rejects(recorder.start(), /Synthetic meter failure/);
  assert.equal(stopped, 1); assert.equal(closed, 1);
  await recorder.discard(); assert.equal(stopped, 1); assert.equal(closed, 1);
});
