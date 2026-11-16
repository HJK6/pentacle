'use strict';
const { createHash } = require('crypto');

async function chatFileDelivery({ session, report, fixture }) {
  if (!fixture) { report.note('no isolated fixture: file delivery gate unavailable'); return; }
  const result = await session.eval(`(async () => {
    await window.cc.requestStreamEvents({ streamId: ${JSON.stringify(fixture.streamId)}, limit: 30 });
    const root = document.createElement('div'); root.id = 'web-gate-files'; document.body.appendChild(root);
    try {
      window.PentacleChatView.renderStreamTranscript(${JSON.stringify(fixture.streamId)}, root);
      await window.PentacleChatView.hydrateFileAttachments(root);
      const files = [];
      for (const name of ['web-gate-file.pdf','web-gate-file.zip','web-gate-expired.pdf']) {
        const a = Array.from(root.querySelectorAll('.slot-chat-file-download')).find(n => n.download === name);
        if (!a) throw new Error('missing file anchor: ' + name);
        const item = { name, state:a.dataset.fileState, href:a.getAttribute('href'), status:a.parentElement.textContent };
        if (a.dataset.fileState === 'ready') {
          const bytes = await (await fetch(a.href)).arrayBuffer();
          item.digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),x=>x.toString(16).padStart(2,'0')).join('');
        }
        files.push(item);
      }
      return {files, activePreview:root.querySelectorAll('iframe,object,embed').length};
    } finally { root.remove(); }
  })()`, { awaitPromise: true });
  for (const [name, body] of [
    ['web-gate-file.pdf', Buffer.from('%PDF synthetic web download')],
    ['web-gate-file.zip', Buffer.concat([Buffer.from([80,75,3,4]),Buffer.from(' synthetic web download')])],
  ]) {
    const file = result.files.find(f => f.name === name);
    report.ok(`file delivery authenticated named download hash matches: ${name}`,
      file?.state === 'ready' && file.href?.startsWith('blob:') && file.digest === createHash('sha256').update(body).digest('hex'));
  }
  const expired = result.files.find(f => f.name === 'web-gate-expired.pdf');
  report.ok('missing file has honest unavailable state and no dead download link',
    expired?.state === 'unavailable' && expired.href === null && expired.status.includes('File expired or unavailable'));
  report.ok('file delivery renders no active preview', result.activePreview === 0);
}
module.exports = { chatFileDelivery };
