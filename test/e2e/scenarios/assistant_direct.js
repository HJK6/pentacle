'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { WebSocketServer } = require('ws');
const { writeDesktopConfig } = require('../lib/disposable_web_daemon_config');

const sourceId = 'mock-host:assistant';
const targetId = 'mock-host:live';
const generation = 'disposable-generation-1';
const source = { host: 'mock-host', session_name: 'assistant', stream_id: sourceId,
  display_name: 'Assistant', session_kind: 'assistant_composite', session_generation: 'source-generation',
  provider: 'composite', visibility: 'visible', online: true, capabilities: { pane: false, terminal: false } };
const target = { host: 'mock-host', session_name: 'live', stream_id: targetId,
  display_name: 'Disposable target', session_kind: 'ordinary', session_generation: generation,
  provider: 'codex', visibility: 'default', online: true, capabilities: { pane: false, terminal: false } };
const noiseId = 'mock-host:history-pressure';
const noise = { ...target, session_name: 'history-pressure', stream_id: noiseId,
  display_name: 'Disposable history pressure' };
const extraSessions = [1, 2].map(index => ({ ...target,
  session_name: `history-extra-${index}`, stream_id: `mock-host:history-extra-${index}`,
  display_name: `Disposable history extra ${index}` }));

async function startDaemon({ artifactsDir, includeReport = false }) {
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await new Promise(resolve => server.once('listening', resolve));
  const authDir = path.join(artifactsDir, 'fixture-auth');
  fs.mkdirSync(authDir, { recursive: true, mode: 0o700 });
  const tokenPath = path.join(authDir, 'token');
  fs.writeFileSync(tokenPath, 'assistant-direct-disposable-only', { mode: 0o600 });
  const baseConfig = writeDesktopConfig({ dir: artifactsDir,
    wsUrl: `ws://127.0.0.1:${server.address().port}`, tokenPath });
  const configFile = path.join(artifactsDir, 'assistant-direct.private.config.js');
  fs.writeFileSync(configFile, `const base = require(${JSON.stringify(baseConfig)});\nmodule.exports = { ...base, features: { ...base.features, assistantDirectTarget: ${JSON.stringify({ sourceStreamId: sourceId, streamId: targetId, generation })} } };\n`);
  const requests = [];
  const events = [];
  const assistantEvents = [];
  const noiseEvents = [];
  const extraEvents = new Map(extraSessions.map(session => [session.stream_id, []]));
  let historyPressure = false;
  const uploads = new Map();
  const blobs = new Map();
  let durableQuestion = null;
  let targetHistoryReplyDelayMs = 0;
  const reportBody = JSON.stringify({ schema_version: 1, title: 'Disposable popout report', sections: [{
    id: 'report-section', title: 'Findings', status: 'in_progress', blocks: [{
      id: 'report-block', type: 'para', runs: [{ type: 'text', text: 'Disposable report body for popout proof.' }],
    }],
  }] });
  const report = { asset_id: 'disposable-report', title: 'Disposable popout report',
    content_type: 'report', review_status: 'pending_review', updated_at: '2026-09-26T00:00:00Z',
    session_key: { host: target.host, session_name: target.session_name, stream_id: targetId } };
  const comments = [];
  let seq = 0;
  function event(kind, text, extra = {}) {
    seq++;
    return { ...target,
      kind, text, timestamp: new Date().toISOString(), daemon_seq: seq,
      event_id: `direct-event-${seq}`, message_id: `direct-event-${seq}`, ...extra };
  }
  events.push(event('ASSIST_TEXT', 'Existing disposable transcript'));
  const send = (socket, frame) => socket.send(JSON.stringify(frame));
  const sessions = () => historyPressure ? [source, target, noise, ...extraSessions] : [source, target];
  const snapshotEvents = () => historyPressure
    ? [...events, ...noiseEvents, ...[...extraEvents.values()].flat()]
      .sort((a, b) => a.daemon_seq - b.daemon_seq).slice(-500)
    : [];
  const inventory = () => { for (const socket of server.clients) send(socket, { type: 'snapshot', sessions: sessions(),
    events: snapshotEvents(), schedules: [], notifications: durableQuestion ? [durableQuestion] : [] }); };
  server.on('connection', socket => {
    send(socket, { type: 'welcome', protocol: 2 });
    socket.on('message', data => {
      const msg = JSON.parse(data.toString());
      requests.push(msg);
      if (msg.type === 'hello') {
        send(socket, { type: 'snapshot', sessions: sessions(), events: snapshotEvents(), schedules: [],
          notifications: durableQuestion ? [durableQuestion] : [], capabilities: { assistant_composite_v1: true } });
        if (includeReport) send(socket, { type: 'asset.update', ...report });
      } else if (msg.type === 'asset.list') {
        send(socket, { type: 'asset.list.ok', request_id: msg.request_id,
          assets: includeReport ? [{ ...report }] : [] });
      } else if (msg.type === 'asset.get' && includeReport) {
        send(socket, { type: 'asset.get.ok', request_id: msg.request_id,
          asset: { ...report, body: reportBody } });
      } else if (msg.type === 'asset.comments.list' && includeReport) {
        send(socket, { type: 'asset.comments.list.ok', request_id: msg.request_id, comments: comments.slice() });
      } else if (msg.type === 'asset.comment.add' && includeReport) {
        const comment = { comment_id: `comment-${comments.length + 1}`, section_id: msg.section_id,
          block_id: msg.block_id, body: msg.body, excerpt: msg.excerpt || '',
          author: 'disposable@proof', created_at: new Date().toISOString(), resolved: false };
        comments.push(comment);
        send(socket, { type: 'asset.comment.add.ok', request_id: msg.request_id, comment });
        for (const client of server.clients) send(client, { type: 'asset.update', ...report });
      } else if (msg.type === 'asset.review.set' && includeReport) {
        report.review_status = msg.review_status;
        report.updated_at = '2026-09-26T00:01:00Z';
        send(socket, { type: 'asset.review.set.ok', request_id: msg.request_id, asset: { ...report } });
        for (const client of server.clients) send(client, { type: 'asset.update', ...report });
      } else if (msg.type === 'request_stream_events') {
        const reply = () => send(socket, { type: 'request_stream_events.ok', request_id: msg.request_id,
          stream_id: msg.stream_id, events: msg.stream_id === targetId ? events : msg.stream_id === sourceId ? assistantEvents
            : msg.stream_id === noiseId ? noiseEvents : extraEvents.get(msg.stream_id) || [], has_more: false });
        if (msg.stream_id === targetId && targetHistoryReplyDelayMs > 0) setTimeout(reply, targetHistoryReplyDelayMs);
        else reply();
      } else if (msg.type === 'upload_blob_init') {
        uploads.set(msg.request_id, []);
        send(socket, { type: 'upload_blob.init.ok', request_id: msg.request_id });
      } else if (msg.type === 'upload_blob_chunk') {
        const parts = uploads.get(msg.request_id);
        if (!parts) return send(socket, { type: 'upload_blob.error', request_id: msg.request_id, error: 'No upload init' });
        parts.push(Buffer.from(msg.data_b64 || '', 'base64'));
        if (msg.final) {
          const content = Buffer.concat(parts);
          const blobSha = crypto.createHash('sha256').update(content).digest('hex');
          blobs.set(blobSha, content);
          uploads.delete(msg.request_id);
          send(socket, { type: 'upload_blob.ok', request_id: msg.request_id, blob_sha: blobSha, size_bytes: content.length });
        }
      } else if (msg.type === 'fetch_blob') {
        const content = blobs.get(msg.blob_sha);
        if (!content) return send(socket, { type: 'fetch_blob.error', request_id: msg.request_id, error: 'Blob missing' });
        send(socket, { type: 'fetch_blob.ok', request_id: msg.request_id, blob_sha: msg.blob_sha,
          size_bytes: content.length, content_b64: content.toString('base64'), final: true });
      } else if (msg.type === 'question.dismiss') {
        if (msg.host !== target.host || msg.session_name !== target.session_name || msg.question_key !== target.question?.question_key) {
          send(socket, { type: 'question.dismiss.error', request_id: msg.request_id, error: 'Wrong question route' });
        } else {
          delete target.question;
          send(socket, { type: 'question.dismiss.ok', request_id: msg.request_id, action_committed: true });
          inventory();
        }
      } else if (msg.type === 'notification.resolve') {
        if (!durableQuestion || msg.notification_id !== durableQuestion.notification_id) {
          send(socket, { type: 'notification.resolve.error', request_id: msg.request_id, error: 'Wrong durable question route' });
        } else {
          durableQuestion = { ...durableQuestion, state: 'answered', question: { ...durableQuestion.question, state: 'answered' } };
          send(socket, { type: 'notification.resolve.ok', request_id: msg.request_id, notification: durableQuestion });
          inventory();
        }
      } else if (msg.type === 'send') {
        if (msg.host !== target.host || msg.session_name !== target.session_name) {
          send(socket, { type: 'send.result', request_id: msg.request_id, stream_id: sourceId,
            ok: false, error: 'Composite fallback forbidden' });
          return;
        }
        const user = event('USER', msg.text, { optimistic_id: msg.optimistic_id,
          reply_to_message_id: msg.reply_to_message_id, attachments: msg.attachments });
        events.push(user);
        send(socket, { type: 'send.result', request_id: msg.request_id, optimistic_id: msg.optimistic_id,
          stream_id: targetId, action_committed: true, delivery: 'committed_pending', message_id: user.message_id });
        send(socket, { type: 'chat.event', event: user });
        const answer = event('ASSIST_TEXT', `Echo: ${msg.text || '[image]'}`, { reply_to_message_id: user.message_id });
        events.push(answer);
        send(socket, { type: 'chat.event', event: answer });
      } else if (msg.request_id) {
        send(socket, { type: `${msg.type}.ok`, request_id: msg.request_id,
          items: [], assets: [], questions: [], notifications: [] });
      }
    });
  });
  return { configFile, requests, events, noiseEvents, extraSessions, sourceId, targetId, noiseId, generation,
    report, reportBody, comments,
    disconnect() { for (const socket of server.clients) socket.terminate(); },
    emitEvent(row) { for (const socket of server.clients) send(socket, { type: 'chat.event', event: row }); },
    seedAssistantHistory(text) {
      if (requests.length) throw new Error('seed assistant history before the web host connects');
      assistantEvents.push(event('ASSIST_TEXT', text, source));
    },
    seedHistoryPressure({ targetRows = 205, noiseRows = 430 } = {}) {
      if (requests.length) throw new Error('seed history before the web host connects');
      historyPressure = true;
      for (let i = 0; i < targetRows; i++) events.push(event(i % 2 ? 'ASSIST_TEXT' : 'USER', `Target history ${i}`));
      for (let i = 0; i < noiseRows; i++) noiseEvents.push(event('ASSIST_TEXT', `Pressure history ${i}`, noise));
      for (const session of extraSessions) for (let i = 0; i < 140; i++)
        extraEvents.get(session.stream_id).push(event('ASSIST_TEXT', `Extra ${session.session_name} history ${i}`, session));
    },
    appendTarget(text) {
      const row = event('ASSIST_TEXT', text);
      events.push(row);
      for (const socket of server.clients) send(socket, { type: 'chat.event', event: row });
      return row;
    },
    setTargetHistoryReplyDelay(ms) { targetHistoryReplyDelayMs = Math.max(0, Number(ms) || 0); },
    setGeneration(value) { target.session_generation = value; inventory(); },
    setPaneQuestion(question) { target.question = question; inventory(); },
    setDurableQuestion(notification) { durableQuestion = notification; inventory(); },
    stop() { for (const socket of server.clients) socket.terminate(); return new Promise(resolve => server.close(resolve)); } };
}

module.exports = { startDaemon, sourceId, targetId, generation };
