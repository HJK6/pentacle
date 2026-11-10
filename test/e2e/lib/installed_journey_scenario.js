'use strict';
// A selected served-page scenario using the ordinary gate's browser and cleanup.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { pasteFixture } = require('./render_proofs');
const { reloadDashboardPage } = require('./dashboard_scenario');

async function installedJourney(ctx, descriptor) {
  const { session, report } = ctx;
  const observation = { phase: descriptor.phase, inputs: {}, messages: {}, history: {}, status: 'RUNNING' };
  const fixtureIds = descriptor.fixtures.map(f => f.stream_id);
  const input = '#cell-0 .slot-chat-compose-input';
  const list = '#cell-0 .slot-chat-list';
  const wait = expression => session.waitFor(expression, { timeoutMs: 180000 });
  const rows = stream => session.eval(`(window.PentacleChatStore.getState().events || []).filter(e => e.stream_id === ${JSON.stringify(stream)})`);
  const hook = await session.send('Page.addScriptToEvaluateOnNewDocument', { source: `(() => {
    const Native = window.WebSocket;
    window.__installedJourney = { requests: [], dropNext: false, retryRequested: false, sockets: [] };
    const reportOwner = ${JSON.stringify(descriptor.phase === 'report' ? descriptor.fixtures[0].stream_id : null)};
    const reportAsset = ${JSON.stringify(descriptor.asset_id || null)};
    window.__installedJourney.reportRpc = [];
    let socketOrdinal = 0;
    window.WebSocket = class extends Native {
      constructor(...args) {
        super(...args);
        if (!String(args[0]).endsWith('/cc')) return;
        window.__installedJourney.sockets.push(this);
        const socket = ++socketOrdinal, pending = new Map();
        this.addEventListener('message', event => {
          let reply; try { reply = JSON.parse(event.data); } catch (_) { return; }
          const record = pending.get(reply?.id);
          if (!record) return;
          pending.delete(reply.id);
          record.settled_at = Date.now();
          const result = reply.result || {};
          record.state = reply.ok === false || result.ok === false || result.error_code ? 'error' : 'success';
          record.error_code = reply.error?.code || result.error_code || result.error?.code || null;
          const error = reply.error?.message || result.error?.message || (typeof result.error === 'string' ? result.error : null);
          record.error = typeof error === 'string' ? error.slice(0, 1024) : null;
          record.error_truncated = typeof error === 'string' && error.length > 1024;
          const asset = result.asset;
          const ownAsset = asset?.asset_id === reportAsset && asset?.session_key?.stream_id === reportOwner;
          record.response = { type:result.type, asset_id:asset?.asset_id,
            stream_id:asset?.session_key?.stream_id,
            body:ownAsset && typeof asset.body === 'string' ? asset.body.slice(0, 65536) : undefined,
            body_truncated:ownAsset && typeof asset.body === 'string' && asset.body.length > 65536,
            comments_count:Array.isArray(result.comments) ? result.comments.length : undefined,
            assets:Array.isArray(result.assets) ? result.assets.filter(a => a.asset_id === reportAsset && a.session_key?.stream_id === reportOwner)
              .map(a => ({ asset_id:a.asset_id, session_key:a.session_key, content_type:a.content_type })) : undefined };
        });
        this.send = bytes => {
          let frame; try { frame = JSON.parse(bytes); } catch (_) {}
          const capture = window.__installedJourney;
          const a = frame?.args;
          const scope = a?.[0];
          if (reportOwner && ['chat-stream:asset-list','chat-stream:asset-get','chat-stream:asset-comments-list'].includes(frame?.method)
              && scope?.stream_id === reportOwner && (!scope.asset_id || scope.asset_id === reportAsset)) {
            const record = { socket, id:frame.id, method:frame.method, state:'pending', started_at:Date.now(),
              stream_id:reportOwner, asset_id:scope.asset_id || null };
            const records = capture.reportRpc;
            if (records.length >= 128) { records.shift(); capture.reportRpcTruncated = true; }
            if (pending.size >= 128) pending.delete(pending.keys().next().value);
            records.push(record); pending.set(frame.id, record);
          }
          const ours = frame?.method === 'chat-stream:send' && Array.isArray(a)
            && ${JSON.stringify(fixtureIds)}.includes(a[0] + ':' + a[1]);
          if (ours) capture.requests.push({ host:a[0], session:a[1], text:a[2], requestId:a[3], optimisticId:a[4] });
          Native.prototype.send.call(this, bytes);
          if (ours && capture.dropNext) {
            capture.dropNext = false;
            this.close(); // Only this disposable browser's socket, after real send.
            const until = Date.now() + 5000;
            const retry = () => {
              const store = window.PentacleChatStore;
              const pending = store?.getState().optimisticSends?.[a[4]];
              if (pending?.status === 'indeterminate' && store.retryOptimisticSend(a[4])) {
                capture.retryRequested = true; return;
              }
              if (Date.now() < until && !capture.requests.some((r,i) => i > 0 && r.requestId === a[3])) setTimeout(retry, 5);
            };
            setTimeout(retry, 0);
          }
        };
      }
    };
  })()` });
  async function open(fixture) {
    const url = new URL(descriptor.url);
    url.searchParams.set('pentacle-chat-popout', JSON.stringify({ stream_id: fixture.stream_id,
      host: fixture.host, session_name: fixture.session_name, title: 'Disposable installed journey' }));
    await session.send('Page.navigate', { url: url.href });
    await wait(`!!window.cc && !!window.PentacleChatStore && !!document.querySelector('${input}')`);
    const identity = await session.eval('(async () => ({ injected:window.__PENTACLE_CONFIG__.buildId, served:(await window.cc.getBuild()).buildId }))()', { awaitPromise: true });
    assert.equal(identity.injected, descriptor.expected_build_id, 'injected served build drift');
    assert.equal(identity.served, descriptor.expected_build_id, 'actual served build drift');
    return identity;
  }
  async function submit(text, reply, { drop = false, image = false } = {}) {
    if (image) {
      const pasted = await session.eval(`(${pasteFixture.toString()})(${JSON.stringify({ draft: '', text })})`);
      observation.image = { sha256: createHash('sha256').update(Buffer.from(pasted.sourcePngBase64, 'base64')).digest('hex'),
        bytes: pasted.files[0].bytes, mime: pasted.files[0].mime };
      await wait("document.querySelectorAll('#cell-0 .slot-chat-attachment-chip').length === 1");
    } else {
      await session.eval(`(() => { const e=document.querySelector('${input}'); e.value=${JSON.stringify(text)}; e.dispatchEvent(new Event('input',{bubbles:true})); })()`);
    }
    if (drop) await session.eval('window.__installedJourney.dropNext = true');
    await session.click('#cell-0 .slot-chat-compose-send');
    await wait(`(window.PentacleChatStore.getState().events || []).some(e => ['ASSIST_TEXT','ASSIST'].includes(e.kind) && e.text?.trim() === ${JSON.stringify(reply)})`);
    await wait(`document.querySelector('${list}')?.textContent.includes(${JSON.stringify(reply)})`);
  }
  let reportAssetKey = null;
  if (descriptor.phase === 'report') observation.report = { click_result:null, snapshots:[], rpc:[], capture_errors:[] };
  const reportSample = () => `(() => {
    const cell = document.querySelector('#cell-0');
    const tabs = Array.from(document.querySelectorAll('#header-0 .slot-asset-tab')).filter(e =>
      e.dataset.streamId === ${JSON.stringify(descriptor.fixtures[0].stream_id)} && e.dataset.assetId === ${JSON.stringify(descriptor.asset_id)});
    const selected = tabs.filter(e => e.classList.contains('active'));
    const body = cell?.querySelector('.slot-asset-view-body');
    const roots = Array.from(body?.querySelectorAll('.slot-asset-report') || []);
    const visible = e => { const style=getComputedStyle(e), box=e.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && box.width > 0 && box.height > 0; };
    const root = roots.length === 1 ? roots[0] : null;
    const checks = { unique_tab:tabs.length === 1,
      selected:selected.length === 1 && selected[0].dataset.assetKey === ${JSON.stringify(reportAssetKey)},
      visible_body:!!root && visible(root),
      summary:!!root && root.querySelector('h1')?.textContent.trim() === ${JSON.stringify(descriptor.summary)},
      report_id:!!root && Array.from(root.querySelectorAll('h2')).filter(e => e.textContent.trim() === ${JSON.stringify(descriptor.report_id)}).length === 1 };
    checks.pass = Object.values(checks).every(Boolean);
    const store = window.PentacleChatStore?.getState?.() || {};
    return { evaluated_at:Date.now(), checks,
      diagnostics:{ whole_cell_summary:!!cell?.textContent.includes(${JSON.stringify(descriptor.summary)}),
        whole_cell_report_id:!!cell?.textContent.includes(${JSON.stringify(descriptor.report_id)}) },
      bodyDOM:body?.outerHTML || null,
      cellDOM:cell?.outerHTML || null, cellText:cell?.textContent || null,
      tabs:tabs.map(e => ({ stream_id:e.dataset.streamId, asset_id:e.dataset.assetId, asset_key:e.dataset.assetKey, active:e.classList.contains('active') })),
      store_events:(Array.isArray(store.events) ? store.events : []).filter(e => e.stream_id === ${JSON.stringify(descriptor.fixtures[0].stream_id)}),
      private_asset_cache:'unavailable through existing harness surface', private_render_generation:'unavailable through existing harness surface' };
  })()`;
  async function captureReport(label) {
    if (!observation.report) return null;
    try {
      const captured = await session.eval(`(() => { /* reportEvidenceSnapshot:${label} */
        return { last_sample:window.__installedJourney?.reportLastSample || null,
          current:${reportSample()}, rpc:window.__installedJourney?.reportRpc || [],
          rpc_truncated:!!window.__installedJourney?.reportRpcTruncated }; })()`);
      observation.report.snapshots.push({ label, ...captured.current });
      if (captured.last_sample) observation.report.last_sample = captured.last_sample;
      observation.report.rpc = captured.rpc;
      observation.report.rpc_truncated = captured.rpc_truncated;
      observation.report.console = (session.consoleLines || []).slice(-100);
      fs.writeFileSync(path.join(report.dir, 'installed-observation.json'), JSON.stringify(observation, null, 2));
      return captured.current;
    } catch (error) {
      observation.report.capture_errors.push({ label, error:error.message });
      return null; // Diagnostic failure never replaces the primary assertion.
    }
  }
  const project = events => events.filter(e => ['USER','ASSIST_TEXT','ASSIST'].includes(e.kind))
    .map(e => ({ daemon_seq:e.correlatedDaemonSeq ?? e.daemon_seq, kind:e.kind, text:e.text,
      attachments:(e.attachments || []).map(a => ({ key:a.key, mime:a.mime, bytes:a.bytes, sha256:a.sha256 })) }))
    .sort((a,b) => a.daemon_seq - b.daemon_seq);
  try {
    if (descriptor.phase === 'chat') {
      for (let i = 0; i < 2; i++) {
        const fixture = descriptor.fixtures[i];
        await open(fixture);
        const empty = await session.eval(`window.cc.requestStreamEvents({streamId:${JSON.stringify(fixture.stream_id)},limit:1})`, { awaitPromise:true });
        assert.equal(empty.ok, true, 'fresh fixture history read');
        assert.equal(empty.received, 0, 'fixture is dirty before mutation');
        const nonce = 'INSTALLED_' + descriptor.run_id + '_' + i;
        const text = `You are a disposable installed acceptance fixture. This finite run includes nonce replies, an image, and non-authorizing fixture question answers and report notices. The coordinator owns all CLI, report and cleanup actions. Do not message other chats or manage any seat. Use an image viewer only when an image is supplied; run no other tools. Acknowledge later fixture notices briefly without acting on them. For this message reply exactly ${nonce}.`;
        await submit(text, nonce, { drop:i === 0 });
        observation.inputs[String(i)] = [text];
        const first = await rows(fixture.stream_id);
        assert.equal(first.filter(e => e.kind === 'USER' && e.text === text).length, 1);
        const wire = await session.eval('window.__installedJourney.requests');
        const sends = wire.filter(r => r.text === text);
        assert.ok(sends.length >= (i === 0 ? 2 : 1), 'actual retry traversed the supported caller');
        assert.ok(sends.every(r => r.requestId === sends[0].requestId && r.optimisticId === sends[0].optimisticId), 'retry retained logical identity');
        observation.messages[String(i)] = { requests:sends, replies:project(first).filter(e => e.kind !== 'USER') };
        if (i === 0) {
          const imageReply = 'IMAGE_' + descriptor.run_id;
          const caption = `Disposable image fixture. Reply exactly ${imageReply} and do nothing else.`;
          await submit(caption, imageReply, { image:true });
          observation.inputs['0'].push(caption);
          const imageRows = await rows(fixture.stream_id);
          const user = imageRows.find(e => e.kind === 'USER' && e.text === caption);
          assert.ok(user?.attachments?.some(a => (a.sha256 || a.key) === observation.image.sha256
            && a.mime === 'image/png' && a.bytes === observation.image.bytes), 'actual committed image identity');
          observation.messages['0'].replies = project(imageRows).filter(e => e.kind !== 'USER');
          observation.messages['0'].requests = await session.eval('window.__installedJourney.requests');
        }
        observation.history[String(i)] = project(await rows(fixture.stream_id));
      }
      await open(descriptor.fixtures[2]);
      const sentinel = await session.eval(`window.cc.requestStreamEvents({streamId:${JSON.stringify(descriptor.fixtures[2].stream_id)},limit:1})`, { awaitPromise:true });
      assert.equal(sentinel.received, 0, 'unrelated sentinel history is empty');
      assert.ok(!await session.eval(`document.querySelector('${list}')?.textContent.includes(${JSON.stringify(descriptor.run_id)})`), 'no cross-session DOM state');
    } else if (descriptor.phase === 'history') {
      for (let i = 0; i < 2; i++) {
        const fixture = descriptor.fixtures[i];
        await open(fixture);
        const before = descriptor.before[String(i)];
        await reloadDashboardPage(ctx, { keepWorkspace: true });
        const replyText = before.find(e => e.kind !== 'USER').text.trim();
        await wait(`!!window.cc && !!window.PentacleChatStore && document.querySelector('${list}')?.textContent.includes(${JSON.stringify(replyText)})`);
        const pages = [];
        observation.history[String(i)] = { before, pages };
        let cursor;
        for (let page = 0; page < 20; page++) {
          const result = await session.eval(`window.cc.requestStreamEvents(${JSON.stringify({ streamId:fixture.stream_id,
            limit:3, chunkLimit:2, ...(cursor === undefined ? {} : { beforeDaemonSeq:cursor }) })})`, { awaitPromise:true });
          pages.push(result);
          assert.equal(result.ok, true, 'authenticated paged history');
          assert.ok(result.received <= 3, 'history exceeded the supported total request limit');
          if (result.exhausted) break;
          assert.ok(Number.isInteger(result.nextBeforeDaemonSeq) && (cursor === undefined || result.nextBeforeDaemonSeq < cursor), 'page cursor advanced');
          cursor = result.nextBeforeDaemonSeq;
        }
        assert.equal(pages.at(-1)?.exhausted, true, 'bounded history walk exhausted the fixture');
        const after = project(await rows(fixture.stream_id));
        assert.deepEqual(after, before, 'reload/paging preserves ordered durable identities');
        const dom = await session.eval(`Array.from(document.querySelectorAll('${list} .slot-chat-user-bubble')).map(e => e.textContent.replace(/\\s+/g,' ').trim())`);
        for (const ownInput of descriptor.inputs[String(i)]) {
          assert.equal(dom.filter(text => text === ownInput.replace(/\s+/g,' ').trim()).length, 1, 'rendered logical input appears once');
        }
        const optimistic = await session.eval(`Object.values(window.PentacleChatStore.getState().optimisticSends || {}).filter(s => s.stream_id === ${JSON.stringify(fixture.stream_id)})`);
        assert.ok(optimistic.every(s => ['acked','echoed','reconciled'].includes(s.status)), 'optimistic input settled');
        observation.history[String(i)] = { before, after, pages, dom, optimistic };
      }
    } else if (descriptor.phase === 'question') {
      await open(descriptor.fixtures[0]);
      const portal = '#desktop-question-portal-' + descriptor.fixtures[0].stream_id.replace(/[^a-zA-Z0-9_-]/g, '-');
      const card = portal + ' [data-notification-id="' + descriptor.notification_id + '"]';
      await wait("!!document.querySelector('#cell-0 .slot-chat-question-open')");
      await session.click('#cell-0 .slot-chat-question-open');
      await wait(`!!document.querySelector(${JSON.stringify(card)})`);
      const options = card + ' .slot-chat-question-option';
      await wait(`Array.from(document.querySelectorAll(${JSON.stringify(options)})).some(e => e.textContent.includes('Fixture'))`);
      await session.eval(`Array.from(document.querySelectorAll(${JSON.stringify(options)})).find(e => e.textContent.includes('Fixture')).click()`);
      await session.click(portal + ' .slot-chat-question-submit');
      const query = `window.cc.promptList({producer_stream_id:${JSON.stringify(descriptor.fixtures[0].stream_id)},open:false})`;
      await wait(`(async () => { const r=await ${query}; return r.questions?.some(q => q.question_id === ${JSON.stringify(descriptor.question_id)} && q.state !== 'open'); })()`);
      await reloadDashboardPage(ctx, { keepWorkspace: true });
      await wait('!!window.cc && !!window.PentacleChatStore');
      observation.questions = await session.eval(query, { awaitPromise:true });
      observation.answered_notification_id = descriptor.notification_id;
    } else if (descriptor.phase === 'report') {
      await open(descriptor.fixtures[0]);
      const tab = '#header-0 .slot-asset-tab[data-stream-id=' + JSON.stringify(descriptor.fixtures[0].stream_id)
        + '][data-asset-id=' + JSON.stringify(descriptor.asset_id) + ']';
      await wait(`!!document.querySelector(${JSON.stringify(tab)})`);
      const target = await captureReport('before-target-assertion');
      assert.equal(target?.tabs.length, 1, 'report tab must have one matching slot/stream/asset');
      reportAssetKey = target.tabs[0].asset_key;
      assert.ok(reportAssetKey, 'report scoped asset key missing');
      observation.report.click_result = await session.click(tab);
      await captureReport('after-click-before-dispatch-assertion');
      assert.equal(observation.report.click_result, true, 'report tab click dispatch was not proved');
      await captureReport('before-rendered-body-predicate');
      await wait(`(() => { const sample = ${reportSample()};
        window.__installedJourney.reportLastSample = sample; return sample.checks.pass; })()`);
      const rendered = await captureReport('before-rendered-body-assertion');
      assert.equal(rendered?.checks.pass, true, 'selected visible report body must contain summary and report ID');
      observation.rendered = { asset_id:descriptor.asset_id, report_id:descriptor.report_id, summary:descriptor.summary };
      await session.screenshot(path.join(report.dir, 'installed-report.png'));
    } else {
      throw new Error('unknown installed journey phase');
    }
    observation.status = 'PASS';
    report.ok('actual installed ' + descriptor.phase + ' journey', true);
  } catch (error) {
    observation.status = 'FAIL';
    observation.primary_error = error.message;
    await captureReport('failure');
    throw error;
  } finally {
    fs.writeFileSync(path.join(report.dir, 'installed-observation.json'), JSON.stringify(observation, null, 2));
    await session.send('Page.removeScriptToEvaluateOnNewDocument', { identifier:hook.identifier });
  }
}

if (require.main === module) {
  const descriptor = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const { SCENARIOS } = require('./web_scenarios');
  SCENARIOS.push(['installed-journey', ctx => installedJourney(ctx, descriptor)]);
  const { run, parseArgs } = require('../web_gate');
  run({ ...parseArgs(['--scenario','installed-journey']), servedUrl:descriptor.url, timeoutMs:180000 })
    // run() has awaited and attested owned cleanup. Match the gate's CLI
    // lifecycle: imported adapters can retain timers after their work finishes.
    .then(code => process.exit(code)).catch(error => { console.error(error.message); process.exit(1); });
}

module.exports = { installedJourney };
