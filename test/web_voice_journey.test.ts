import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { JSDOM } from 'jsdom';
import { bindComposerMic, createBrowserRecorder } from '../renderer/web_voice';
import { ChatStoreController } from '../renderer/src/chat_store_controller';
import { renderStreamTranscript } from '../renderer/src/shared_transcript_view';

// Synthetic devices, clocks, blobs and daemon frames only. This mounts the real
// composer and shared transcript renderer; no browser/microphone/backend opens.
const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve: (value?: any) => void = () => {}; const promise = new Promise<any>(r => { resolve = r; }); return { promise, resolve }; };
const css = fs.readFileSync('renderer/chat_v3.css', 'utf8');
const streamId = 'fixture:voice';
const session = { stream_id: streamId, host: 'fixture', session_name: 'voice', provider: 'claude', online: true };
const chrome = { header: '#102a4a', accent: '#4da3ff', surface: '#0c1827', border: '#2f6ca5', title: 'fixture' };

function fixture(t: any, overrides: Record<string, any> = {}) {
  const dom = new JSDOM(`<style>${css}</style><div class="cosmic"><div id="dock"><div id="panel"></div>
    <div class="slot-chat-compose"><textarea>Unsent draft</textarea><button id="mic"></button></div></div>
    <div class="slot-chat-scroll"><div id="transcript"></div></div></div>`, { pretendToBeVisual: true });
  const doc = dom.window.document;
  const panel = doc.querySelector('#panel') as HTMLElement;
  const button = doc.querySelector('#mic') as HTMLButtonElement;
  const transcript = doc.querySelector('#transcript') as HTMLElement;
  const store = new ChatStoreController();
  store.applyFrame({ type: 'snapshot', sessions: [session], events: [] });
  const calls: any[] = []; const intervals = new Map(); let now = 0; let level = 0; let nextId = 0;
  const blob = new Blob(['Synthetic audio'], { type: 'audio/wav' });
  const pending = deferred(); let currentStreamId = streamId;
  store.setSendBridge(async args => { calls.push(['wire', args]); return { ok: true }; });
  const render = () => renderStreamTranscript(streamId, transcript, { store: store as never, chrome });
  store.subscribe(render);
  const recorder = overrides.recorder || {
    async start() { calls.push(['start']); },
    poll() { return { level, durationMs: now }; },
    async stop() { calls.push(['stop']); return { blob, durationS: now / 1000 }; },
    async discard() { calls.push(['discard']); },
  };
  const controller = bindComposerMic({ web: true, env: {
    isSecureContext: true, navigator: { mediaDevices: { getUserMedia() {} } },
    performance: { now: () => now }, crypto: { randomUUID: () => 'stable-take' },
    setInterval(fn: any, ms: number) { const id = ++nextId; intervals.set(id, { fn, ms }); return id; },
    clearInterval(id: number) { intervals.delete(id); },
  }, button, panel, composer: doc.querySelector('.slot-chat-compose'), takeMount: doc.querySelector('.slot-chat-scroll'),
    getStreamId: () => currentStreamId, recorder,
    upload: async (audio: Blob) => { calls.push(['upload', audio]); return overrides.upload ? overrides.upload(audio) : { ok: true, blob_sha: 'a'.repeat(64) }; },
    transcribe: async (payload: any) => { calls.push(['transcribe', payload]); return overrides.transcribe ? overrides.transcribe(payload) : pending.promise; },
    send: (args: any) => { calls.push(['send', args]); return store.sendTurn(args.streamId, args.text, [], { meta: args.meta }); },
  });
  t.after(async () => { await controller.cancel(); pending.resolve({ ok: true, text: 'Late fixture cleanup' }); dom.window.close(); });
  return { dom, doc, panel, button, transcript, store, render, controller, calls, intervals, blob, pending,
    style: (el: Element) => dom.window.getComputedStyle(el),
    async start() { button.click(); await flush(); },
    async stop() { button.click(); await flush(); },
    sample(value: number, elapsedMs?: number) { level = value; now = elapsedMs ?? now + 90; for (const { fn } of [...intervals.values()]) fn(); },
    navigate(value: string) { currentStreamId = value; },
  };
}

function caption(container: ParentNode, duration: string) {
  const nodes = container.querySelectorAll('.slot-chat-voice-caption');
  assert.equal(nodes.length, 1, 'actual user-row renderer has one voice caption');
  assert.ok(nodes[0].querySelector('svg.voice-mic-glyph'), 'caption contains mic glyph');
  assert.equal(nodes[0].textContent, duration);
  assert.match(nodes[0].getAttribute('aria-label') || '', /Transcribed from voice/);
}

test('stopped take paints right-aligned mobile pending bubble from ALL levels, spinner and discard', async t => {
  const u = fixture(t); await u.start();
  // First 14 samples have already left the live 46-bar window by stop.
  for (let i = 0; i < 60; i++) u.sample(i < 14 ? 1 : 0);
  assert.equal(u.panel.querySelectorAll('.voice-recording-bar').length, 46);
  await u.stop();
  assert.deepEqual([...u.panel.children].map(el => el.className), ['voice-pending-caption', 'voice-pending-bubble'], 'mobile pending caption sits above bubble');
  assert.equal(u.panel.parentElement?.className, 'slot-chat-scroll');
  assert.ok(u.panel.classList.contains('is-pending'));
  assert.equal(u.style(u.panel).alignItems, 'flex-end');
  assert.equal(u.style(u.panel).marginLeft, 'auto');
  const bubble = u.panel.querySelector('.voice-pending-bubble')!;
  assert.ok(bubble?.querySelector('.voice-pending-play path'), 'play glyph is decorative');
  assert.equal(bubble.querySelector('button'), null, 'pending bubble does not promise playback');
  const bars = [...bubble.querySelectorAll('.voice-bubble-bar')];
  assert.equal(bars.length, 30);
  assert.deepEqual(bars.map(bar => Number.parseFloat((bar as HTMLElement).style.height)), Array(7).fill(22).concat(Array(23).fill(2)));
  assert.equal(u.style(bars[0]).width, '2.5px');
  assert.equal(u.style(bars[0]).opacity, '1'); assert.equal(u.style(bars[7]).opacity, '0.4');
  assert.equal(u.style(bubble.querySelector('.voice-pending-bars')!).height, '22px');
  assert.equal(u.style(bubble.querySelector('.voice-pending-bars')!).gap, '2px');
  assert.equal(bubble.querySelector('.voice-pending-duration')?.textContent, '0:05');
  const label = u.panel.querySelector('.voice-transcribing-label')!;
  assert.equal(label.textContent, 'TRANSCRIBING');
  assert.equal(u.style(label).fontSize, '9px'); assert.equal(u.style(label).letterSpacing, '1px');
  assert.match(u.style(label).fontFamily, /mono/);
  assert.ok(u.panel.querySelector('.voice-pending-spinner'));
  assert.ok(u.panel.querySelector('button[aria-label="Discard voice message"] svg'));
  assert.equal(u.calls.filter(c => c[0] === 'send').length, 0);
  assert.equal(u.doc.querySelector('textarea')!.value, 'Unsent draft');
});

test('no-sample pending waveform has 30 mobile fallback bars and bounded styling', async t => {
  const u = fixture(t); await u.start(); await u.stop();
  const bars = [...u.panel.querySelectorAll('.voice-bubble-bar')] as HTMLElement[];
  assert.equal(bars.length, 30);
  assert.ok(bars.every(bar => bar.style.height === '4px' && Math.abs(Number(bar.style.opacity) - .52) < 1e-8));
});

for (const failure of ['upload', 'transcribe', 'empty']) test(`${failure} failure keeps amber take; Retry retains blob/request identity and sends once`, async t => {
  let fail = true;
  const u = fixture(t, {
    upload: () => fail && failure === 'upload' ? { ok: false } : { ok: true, blob_sha: 'b'.repeat(64) },
    transcribe: () => fail ? failure === 'empty' ? { ok: true, text: '   ' } : { ok: false, error_code: 'backend_unavailable' } : { ok: true, text: ' Synthetic retry transcript ' },
  });
  await u.start(); u.sample(.5, 2500); await u.stop();
  assert.ok(u.panel.classList.contains('is-failed'));
  const error = u.panel.querySelector('.voice-pending-error')!;
  assert.match(error.textContent || '', failure === 'upload' ? /upload failed/i : failure === 'empty' ? /Nothing was recognized/ : /unavailable/);
  assert.equal(error.getAttribute('role'), 'alert');
  const errorRule = [...u.doc.styleSheets[0].cssRules].find((rule: any) => rule.selectorText === '.voice-pending-error') as any;
  assert.equal(errorRule.style.color, 'var(--cosmic-amber, #ffbf69)');
  assert.equal(u.panel.querySelector('.voice-pending-spinner'), null);
  const retry = u.panel.querySelector('.voice-pending-retry') as HTMLButtonElement;
  assert.equal(retry.textContent, 'Retry'); assert.equal(u.style(retry).textTransform, 'uppercase');
  fail = false; retry.click(); retry.click(); await flush();
  assert.equal(u.panel.hidden, true);
  const uploads = u.calls.filter(c => c[0] === 'upload');
  assert.equal(uploads.length, failure === 'upload' ? 2 : 1);
  assert.ok(uploads.every(c => c[1] === u.blob), 'retry reuses identical Blob object');
  const transcriptions = u.calls.filter(c => c[0] === 'transcribe');
  assert.ok(transcriptions.every(c => c[1].request_id === 'transcribe-stable-take' && c[1].blob_sha === 'b'.repeat(64)));
  assert.equal(u.calls.filter(c => c[0] === 'send').length, 1);
  assert.equal(u.calls.filter(c => c[0] === 'wire').length, 1);
  caption(u.transcript, '0:02');
});

for (const stage of ['upload', 'transcribe']) test(`Discard during deferred ${stage} deletes pending take and suppresses late completion`, async t => {
  const held = deferred();
  const u = fixture(t, stage === 'upload' ? { upload: () => held.promise } : { transcribe: () => held.promise });
  await u.start(); u.sample(.4, 1300); await u.stop();
  const discard = u.panel.querySelector('.voice-pending-discard') as HTMLButtonElement;
  assert.ok(discard); discard.click(); await flush();
  assert.equal(u.panel.hidden, true); assert.equal(u.controller.snapshot().phase, 'idle');
  held.resolve(stage === 'upload' ? { ok: true, blob_sha: 'c'.repeat(64) } : { ok: true, text: 'Late transcript' });
  await flush();
  assert.equal(u.calls.filter(c => c[0] === 'send').length, 0);
  assert.equal(u.transcript.querySelectorAll('.slot-chat-row.is-user').length, 0);
  if (stage === 'upload') assert.equal(u.calls.filter(c => c[0] === 'transcribe').length, 0);
});

test('tab-hidden interruption stops once, visibly labels duration, and keeps captured destination', async t => {
  const u = fixture(t); await u.start(); u.sample(.6, 65000); u.navigate('fixture:other');
  Object.defineProperty(u.doc, 'hidden', { configurable: true, value: true });
  u.doc.dispatchEvent(new u.dom.window.Event('visibilitychange')); await flush();
  assert.equal(u.controller.snapshot().phase, 'transcribing');
  assert.equal(u.panel.querySelector('.voice-pending-interrupted')?.textContent, 'interrupted at 1:05');
  u.doc.dispatchEvent(new u.dom.window.Event('visibilitychange')); await flush();
  assert.equal(u.calls.filter(c => c[0] === 'stop').length, 1); assert.equal(u.intervals.size, 0);
  u.pending.resolve({ ok: true, text: 'Interrupted synthetic transcript' }); await flush();
  assert.equal(u.calls.find(c => c[0] === 'send')[1].streamId, streamId);
  caption(u.transcript, '1:05');
});

for (const visibleAgain of [false, true]) test(`tab hidden during capture start is honored before polling${visibleAgain ? ', even if visible again' : ''}`, async t => {
  const started = deferred(); let stops = 0; let discards = 0;
  const u = fixture(t, { recorder: {
    start: () => started.promise,
    async stop() { stops++; return { blob: u.blob, durationS: 2 }; },
    async discard() { discards++; },
  } });
  await u.start(); assert.equal(u.controller.snapshot().phase, 'starting');
  Object.defineProperty(u.doc, 'hidden', { configurable: true, value: true });
  u.doc.dispatchEvent(new u.dom.window.Event('visibilitychange'));
  if (visibleAgain) {
    Object.defineProperty(u.doc, 'hidden', { configurable: true, value: false });
    u.doc.dispatchEvent(new u.dom.window.Event('visibilitychange'));
  }
  started.resolve(); await flush();
  assert.equal(u.controller.snapshot().phase, 'transcribing');
  assert.equal(stops, 1); assert.equal(discards, 0); assert.equal(u.intervals.size, 0);
  assert.equal(u.panel.querySelector('.voice-pending-interrupted')?.textContent, 'interrupted at 0:02');
  (u.panel.querySelector('.voice-pending-discard') as HTMLButtonElement).click(); await flush();
  u.pending.resolve({ ok: true, text: 'Discarded interruption' }); await flush();
  assert.equal(u.calls.filter(c => c[0] === 'send').length, 0);
});

test('Discard during hidden pending capture start still releases late capture without stopping or sending', async t => {
  const started = deferred(); let stops = 0; let discards = 0;
  const u = fixture(t, { recorder: {
    start: () => started.promise,
    async stop() { stops++; return { blob: u.blob, durationS: 2 }; },
    async discard() { discards++; },
  } });
  await u.start();
  Object.defineProperty(u.doc, 'hidden', { configurable: true, value: true });
  u.doc.dispatchEvent(new u.dom.window.Event('visibilitychange'));
  await u.controller.cancel(); started.resolve(); await flush();
  assert.equal(stops, 0); assert.equal(discards, 1); assert.equal(u.intervals.size, 0);
  assert.equal(u.controller.snapshot().phase, 'idle'); assert.equal(u.panel.hidden, true);
  assert.equal(u.calls.filter(c => ['upload', 'transcribe', 'send'].includes(c[0])).length, 0);
});

for (const mode of ['MP4', 'WAV', 'already-ended MP4']) test(`synthetic media-track ended interrupts ${mode} capture and cleanup does not stop/send twice`, async t => {
  const events = new EventTarget(); let now = 0; let trackStops = 0; let recorderStops = 0; let contextCloses = 0;
  let mediaRecorder: any;
  const track = { readyState: 'live', addEventListener: events.addEventListener.bind(events), removeEventListener: events.removeEventListener.bind(events),
    stop() { trackStops++; this.readyState = 'ended'; events.dispatchEvent(new Event('ended')); } };
  class AudioContext {
    sampleRate = 16000; destination = {};
    async resume() {} async close() { contextCloses++; }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createAnalyser() { return { fftSize: 0, getFloatTimeDomainData(samples: Float32Array) { samples.fill(.1); }, disconnect() {} }; }
    createScriptProcessor() { return { connect() {}, disconnect() {}, onaudioprocess: null }; }
  }
  class MediaRecorder {
    static isTypeSupported() { return mode !== 'WAV'; }
    state = 'inactive'; ondataavailable: any; onstop: any;
    constructor() { mediaRecorder = this; }
    start() { this.state = 'recording'; }
    stop() { recorderStops++; this.state = 'inactive'; this.ondataavailable({ data: new Blob(['Synthetic MP4']) }); this.onstop(); }
  }
  const recorder = createBrowserRecorder({ AudioContext, MediaRecorder, performance: { now: () => now }, navigator: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [track] }) } } });
  const u = fixture(t, { recorder }); await u.start(); now = 7123;
  if (mode === 'already-ended MP4') mediaRecorder.stop();
  track.readyState = 'ended'; events.dispatchEvent(new Event('ended')); await flush();
  assert.equal(u.controller.snapshot().phase, 'transcribing');
  assert.equal(u.panel.querySelector('.voice-pending-interrupted')?.textContent, 'interrupted at 0:07');
  assert.equal(trackStops, 1); assert.equal(recorderStops, mode === 'WAV' ? 0 : 1); assert.equal(contextCloses, 1);
  u.pending.resolve({ ok: true, text: 'Track ended synthetic transcript' }); await flush();
  events.dispatchEvent(new Event('ended')); await flush();
  assert.equal(u.calls.filter(c => c[0] === 'send').length, 1); assert.equal(recorderStops, mode === 'WAV' ? 0 : 1);
});

function echo(overrides: Record<string, any> = {}) {
  return { daemon_seq: 10, host: 'fixture', provider: 'claude', session_id: 'voice', session_name: 'voice', stream_id: streamId,
    timestamp: new Date().toISOString(), kind: 'USER', text: 'Synthetic transcript', meta: { voice: { duration_s: 7.25 } }, ...overrides };
}

test('ordinary optimistic send, acknowledged row, exact echo and fresh history all render durable green caption', async t => {
  const u = fixture(t); await u.start(); u.sample(.5, 7250); await u.stop();
  u.pending.resolve({ ok: true, text: 'Synthetic transcript' }); await flush();
  assert.equal(u.panel.hidden, true); caption(u.transcript, '0:07');
  const wire = u.calls.find(c => c[0] === 'wire')[1];
  assert.deepEqual(wire.meta, { voice: { duration_s: 7.25 } });
  assert.equal((wire.attachments ?? []).length, 0, 'only transcript travels via ordinary send');
  const item = u.store.selectSessionDetail(streamId)!.transcriptItems.find(i => i.optimisticId === wire.optimisticId) as any;
  assert.deepEqual(item.voice, { duration_s: 7.25 });
  const captionRule = [...u.doc.styleSheets[0].cssRules].find((rule: any) => rule.selectorText === '.slot-chat-voice-caption') as any;
  assert.equal(captionRule.style.color, 'var(--cosmic-green, #3dff66)');
  assert.equal(u.style(u.transcript.querySelector('.slot-chat-voice-caption')!).fontSize, '9px');
  assert.match(u.style(u.transcript.querySelector('.slot-chat-voice-caption')!).fontFamily, /mono/);
  u.store.applyFrame({ type: 'send.result', request_id: wire.requestId, delivery: 'landed' }); caption(u.transcript, '0:07');
  const event = echo({ optimistic_id: wire.optimisticId, request_id: wire.requestId, message_id: 'voice-message-10' });
  u.store.applyFrame({ type: 'chat.event', event });
  assert.equal(u.transcript.querySelectorAll('.slot-chat-row.is-user').length, 1); caption(u.transcript, '0:07');
  assert.equal(u.store.getState().optimisticSends?.[wire.optimisticId], undefined);
  // Simulate a reload with a brand-new store, then actual stream_events backfill.
  const reloaded = new ChatStoreController();
  reloaded.applyFrame({ type: 'snapshot', sessions: [session], events: [], events_mode: 'summary' });
  reloaded.applyFrame({ type: 'stream_events', stream_id: streamId, events: [JSON.parse(JSON.stringify(event))] });
  renderStreamTranscript(streamId, u.transcript, { store: reloaded as never, chrome });
  assert.equal(u.transcript.querySelectorAll('.slot-chat-row.is-user').length, 1); caption(u.transcript, '0:07');
  assert.equal(u.calls.filter(c => c[0] === 'transcribe').length, 1);
});

test('duplicate text uses stable event identities; ordinary and invalid metadata never inherit a voice caption', t => {
  const u = fixture(t);
  const invalid = [undefined, null, { voice: {} }, { voice: { duration_s: '7' } }, { voice: { duration_s: -1 } }, { voice: { duration_s: Infinity } }];
  const events = [echo(), ...invalid.map((meta, index) => echo({ daemon_seq: 11 + index, message_id: `plain-${index}`, meta }))];
  // Same stream, same text and timestamp: only authoritative per-event metadata distinguishes these.
  u.store.applyFrame({ type: 'stream_events', stream_id: streamId, events });
  const rows = [...u.transcript.querySelectorAll('.slot-chat-row.is-user')];
  assert.equal(rows.length, events.length);
  assert.equal(rows[0].querySelectorAll('.slot-chat-voice-caption').length, 1);
  for (const row of rows.slice(1)) assert.equal(row.querySelector('.slot-chat-voice-caption'), null);
  caption(u.transcript, '0:07');
});

test('identical optimistic text with distinct IDs reconciles to two independently captioned echoes', async t => {
  const u = fixture(t);
  const voiceId = u.store.sendTurn(streamId, 'Identical synthetic text', [], { meta: { voice: { duration_s: 9 } } });
  const plainId = u.store.sendTurn(streamId, 'Identical synthetic text');
  await flush();
  assert.notEqual(voiceId, plainId);
  let rows = [...u.transcript.querySelectorAll('.slot-chat-row.is-user')];
  assert.equal(rows.length, 2); caption(u.transcript, '0:09');
  assert.equal(rows[0].querySelectorAll('.slot-chat-voice-caption').length, 1); assert.equal(rows[1].querySelector('.slot-chat-voice-caption'), null);
  // Reversed replies deliberately defeat ordering/text-based association.
  u.store.applyFrame({ type: 'chat.event', event: echo({ daemon_seq: 21, optimistic_id: plainId, text: 'Identical synthetic text', meta: undefined }) });
  u.store.applyFrame({ type: 'chat.event', event: echo({ daemon_seq: 20, optimistic_id: voiceId, text: 'Identical synthetic text', meta: { voice: { duration_s: 9 } } }) });
  rows = [...u.transcript.querySelectorAll('.slot-chat-row.is-user')];
  assert.equal(rows.length, 2); caption(u.transcript, '0:09');
  const items = u.store.selectSessionDetail(streamId)!.transcriptItems as any[];
  assert.deepEqual(items.find(item => item.optimisticId === voiceId).voice, { duration_s: 9 });
  assert.equal(items.find(item => item.optimisticId === plainId).voice, undefined);
});

test('Discard while stop is pending hides the row but holds capture ownership until cleanup settles', async t => {
  const stopped = deferred(); let starts = 0;
  const u = fixture(t, { recorder: { async start() { starts++; }, stop: () => stopped.promise, async discard() {} } });
  await u.start(); await u.stop();
  (u.panel.querySelector('.voice-pending-discard') as HTMLButtonElement).click(); await flush();
  assert.equal(u.panel.hidden, true); assert.equal(u.button.disabled, true);
  await u.controller.cancel(); u.button.click(); await flush();
  assert.equal(starts, 1, 'repeated Discard cannot reopen recorder before prior stop finishes');
  stopped.resolve({ blob: u.blob, durationS: 1 }); await flush();
  assert.equal(u.button.disabled, false); assert.equal(u.controller.snapshot().phase, 'idle');
  assert.equal(u.calls.filter(c => ['upload', 'transcribe', 'send'].includes(c[0])).length, 0);
});

test('ordinary failed text Retry keeps the voice caption and never re-uploads or re-transcribes', async t => {
  const u = fixture(t); let attempts = 0;
  u.store.setSendBridge(async args => { u.calls.push(['wire', args]); attempts++; return attempts === 1 ? { ok: false, error: 'backend_busy' } : { ok: true }; });
  await u.start(); u.sample(.6, 3500); await u.stop(); u.pending.resolve({ ok: true, text: 'Synthetic retry after send' }); await flush();
  caption(u.transcript, '0:03');
  const first = u.calls.find(c => c[0] === 'wire')[1];
  assert.equal(u.store.retryOptimisticSend(first.optimisticId), true); await flush();
  caption(u.transcript, '0:03');
  const wires = u.calls.filter(c => c[0] === 'wire');
  assert.equal(wires.length, 2); assert.equal(wires[1][1].optimisticId, first.optimisticId);
  assert.notEqual(wires[1][1].requestId, first.requestId); assert.deepEqual(wires[1][1].meta, first.meta);
  assert.equal(u.calls.filter(c => c[0] === 'upload').length, 1); assert.equal(u.calls.filter(c => c[0] === 'transcribe').length, 1);
  assert.equal(u.transcript.querySelectorAll('.slot-chat-row.is-user').length, 1);
});
