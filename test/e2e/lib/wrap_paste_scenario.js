'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const seeded = require('../fixtures/wrap-paste.json');
const { reloadDashboardPage } = require('./dashboard_scenario');
const { wrapSourceEvent, assertFreshWrapFixture, buildWrapSourceProofScript, pasteFixture } = require('./render_proofs');

async function wrapPasteProof(ctx) {
  const { session, report, cdp, fixture, wrapProof, timeoutMs, url } = ctx;
  if (!fixture && !wrapProof) { report.note('wrap/paste needs its seeded fixture or an explicit served proof identity'); return; }
  const identity = wrapProof || seeded;
  const streamId = identity.streamId;
  const input = '#cell-0 .slot-chat-compose-input';
  const readEvents = () => session.eval(`(window.PentacleChatStore.getState().events || []).filter(row => row.stream_id === ${JSON.stringify(streamId)})`);
  const result = { fixtureId: identity.fixtureId, streamId, phase: identity.phase || 'seeded', geometry: [] };
  const target = new URL(url);
  if (wrapProof) {
    const colon = streamId.indexOf(':');
    target.searchParams.set('pentacle-chat-popout', JSON.stringify({ stream_id: streamId,
      host: streamId.slice(0, colon), session_name: streamId.slice(colon + 1), title: 'Disposable served proof' }));
  }
  await session.send('Page.navigate', { url: target.href });
  await session.waitFor('!!window.cc && !!window.PentacleChatStore', { timeoutMs });
  await session.waitFor(`window.PentacleChatStore.getState().sessions.some(row => row.stream_id === ${JSON.stringify(streamId)})`, { timeoutMs });
  if (!wrapProof) {
    await session.waitFor(`document.querySelector('.session-item[data-stream-id="${streamId}"]')`, { timeoutMs });
    await session.click(`.session-item[data-stream-id="${streamId}"]`);
    await session.click('#cell-0 .cell-view-toggle[data-mode="chat"]');
  }
  await session.waitFor(`document.querySelector('${input}')`, { timeoutMs });
  const history = await session.eval(`window.cc.requestStreamEvents({ streamId: ${JSON.stringify(streamId)}, limit: ${wrapProof ? 1 : 500} })`, { awaitPromise: true });
  if (!history?.ok) throw new Error('fixture history could not be inspected before mutation');
  if (wrapProof && (history.received !== 0 || history.exhausted !== true)) throw new Error('fixture chat must be fresh before mutation');
  const build = async () => session.eval('(async () => ({ injected: window.__PENTACLE_CONFIG__?.buildId, served: (await window.cc.getBuild()).buildId }))()');
  let source, primaryError;
  try {
    if (wrapProof) {
      result.build = await build();
      report.ok('served proof matches the explicitly bound build before mutation',
        result.build.injected === wrapProof.expectedBuildId && result.build.served === wrapProof.expectedBuildId, result.build);
      // Recheck fresh state at the stateful phase boundary; a rehearsal cannot be reused.
      assertFreshWrapFixture(identity, await readEvents());
      const requestUrl = 'https://example.com/' + 'abcdefghij'.repeat(60);
      const requestWord = 'LongWord' + 'abcdefghij'.repeat(60);
      const prompt = `WWP_RENDER_PROOF ${identity.fixtureId}\nReply with this URL and word in separate paragraphs, then the word as inline code and the URL as a Markdown link. Keep each string at least 300 characters. No tools.\n\n${requestUrl}\n\n${requestWord}`;
      await session.eval(`(() => { const e = document.querySelector('${input}'); e.value = ${JSON.stringify(prompt)}; e.dispatchEvent(new Event('input', { bubbles: true })); })()`);
      await session.click('#cell-0 .slot-chat-compose-send');
      const deadline = Date.now() + 120000;
      let lastError;
      while (!source && Date.now() < deadline) {
        const rows = await readEvents();
        const reply = rows.filter(row => row.kind === 'ASSIST_TEXT').at(-1);
        if (reply) {
          try { source = wrapSourceEvent(rows, streamId, reply.event_id || reply.message_id || reply.daemon_seq); }
          catch (error) { lastError = error; }
        }
        if (!source) await cdp.sleep(200);
      }
      if (!source) throw lastError || new Error('fixture provider source event missing');
      result.sourceSha = identity.sourceSha;
    } else {
      await session.waitFor(`(window.PentacleChatStore.getState().events || []).some(row => row.stream_id === ${JSON.stringify(streamId)} && row.kind === 'ASSIST_TEXT')`, { timeoutMs });
      const rows = await readEvents(), reply = rows.find(row => row.kind === 'ASSIST_TEXT');
      source = wrapSourceEvent(rows, streamId, reply.event_id || reply.message_id || reply.daemon_seq);
    }
    result.source = source;
    await session.waitFor(`document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes(${JSON.stringify(source.word)})`, { timeoutMs });
    for (const theme of ['default', 'chat-first']) for (const [width, height] of [[1440, 900], [390, 844]]) {
      await session.eval(`document.documentElement.classList.toggle('chat-first-style', ${theme === 'chat-first'})`);
      await session.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
      await cdp.sleep(100);
      const observed = await session.eval(buildWrapSourceProofScript('#cell-0 .slot-chat-list', source));
      result.geometry.push({ theme, width, ...observed });
      await session.screenshot(path.join(report.dir, `wrap-${theme}-${width}.png`));
      report.ok(`named source wraps and copies exactly: ${theme} ${width}`,
        observed.records.length === 3 && observed.records.every(row => row.chars >= 300 && row.fits && row.lines > 1 && row.copyExact)
          && observed.hrefExact && observed.pageFits, observed);
    }
    await session.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
    const caption = `wrap-paste ${identity.fixtureId}`;
    const image = await session.eval(`(${pasteFixture.toString()})(${JSON.stringify({ draft: 'before SELECT after', selection: [7, 13], text: caption, expected: 'before ' + caption + ' after' })})`);
    report.ok('mixed clipboard image/text preserves selection replacement and caret',
      image.prevented && image.textCorrect && image.inputs === 1 && image.caret === 7 + caption.length, image);
    await session.waitFor("document.querySelectorAll('#cell-0 .slot-chat-attachment-chip').length === 1", { timeoutMs });
    await session.screenshot(path.join(report.dir, 'wrap-paste-preview.png'));
    if (wrapProof) {
      const digest = createHash('sha256').update(Buffer.from(image.sourcePngBase64, 'base64')).digest('hex');
      result.image = { sha256: digest, bytes: image.files[0].bytes, mime: image.files[0].mime };
      await session.click('#cell-0 .slot-chat-compose-send');
      await session.waitFor("document.querySelectorAll('#cell-0 .slot-chat-attachment-chip').length === 0", { timeoutMs });
      await session.eval('window.__wrapOldDocument = true');
      await session.send('Page.reload');
      await session.waitFor("window.__wrapOldDocument !== true && document.readyState !== 'loading'", { timeoutMs });
      result.reloadedBuild = await build();
      report.ok('reload keeps exact served build identity', result.reloadedBuild.injected === identity.expectedBuildId && result.reloadedBuild.served === identity.expectedBuildId);
      await session.waitFor(`document.querySelector('#cell-0 .slot-chat-list')?.textContent.includes(${JSON.stringify(caption)})`, { timeoutMs });
      const attachment = await session.eval(`(window.PentacleChatStore.getState().events || []).filter(row => row.stream_id === ${JSON.stringify(streamId)} && row.kind === 'USER').flatMap(row => row.attachments || []).find(a => a.key === ${JSON.stringify(digest)})`);
      report.ok('served image survives reload with exact durable source metadata',
        attachment?.mime === 'image/png' && attachment.bytes === result.image.bytes && attachment.sha256 === digest && !attachment.uri);
      await session.waitFor(`(() => { const b = [...document.querySelectorAll('#cell-0 .slot-chat-media-button')].find(b => b.dataset.attachmentKey === ${JSON.stringify(digest)}); return b?.querySelector('img')?.naturalWidth === 3; })()`, { timeoutMs });
      await session.eval(`[...document.querySelectorAll('#cell-0 .slot-chat-media-button')].find(b => b.dataset.attachmentKey === ${JSON.stringify(digest)}).click()`);
      await session.waitFor("document.querySelector('.slot-chat-image-viewer.is-open img')?.naturalWidth === 3", { timeoutMs });
      await session.screenshot(path.join(report.dir, 'wrap-paste-reloaded-viewer.png'));
    } else {
      await session.click('#cell-0 .slot-chat-attachment-remove');
      await session.waitFor("document.querySelectorAll('#cell-0 .slot-chat-attachment-chip').length === 0", { timeoutMs });
    }
    result.status = 'PASS';
  } catch (error) {
    primaryError = error;
    throw error;
  } finally {
    try {
      if (!wrapProof) {
        result.cleanup = { workspaceReset: false };
        await reloadDashboardPage(ctx);
        result.cleanup.workspaceReset = true;
      }
    } catch (error) {
      result.cleanup.error = error.message;
      if (primaryError) throw new AggregateError([primaryError, error], 'wrap proof failed; workspace reset also failed');
      throw error;
    } finally {
      fs.writeFileSync(path.join(report.dir, 'wrap-paste-proof.json'), JSON.stringify(result, null, 2));
    }
  }
}
module.exports = { wrapPasteProof };
