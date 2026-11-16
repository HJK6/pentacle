'use strict';
const fs = require('node:fs');
const path = require('node:path');

// The seeded second-host session's latest message is Markdown (seed_web_gate.py
// MARKDOWN_PREVIEW). Its one-line sidebar preview, served from the live daemon
// summary, must show the words without Markdown markers.
const STREAM_ID = 'local2:web-gate-second-host';
const EXPECTED = 'Result Gate passed with italics, inline code and a link';

async function sidebarPreviewPlainText(ctx) {
  const { session, report, cdp, timeoutMs, fixture } = ctx;
  if (!fixture) {
    report.note('no fixture: skipping the seeded sidebar preview assertion (observational run)');
    return;
  }
  const deadline = Date.now() + timeoutMs;
  let observed = null;
  for (;;) {
    observed = await session.eval(`(() => {
      const row = document.querySelector('#session-list .session-item[data-stream-id=${JSON.stringify(STREAM_ID)}]');
      const preview = row && row.querySelector('.s-preview');
      return row ? { preview: preview ? preview.textContent : null } : null;
    })()`);
    if (observed && observed.preview) break;
    if (Date.now() > deadline) break;
    await cdp.sleep(300);
  }
  fs.writeFileSync(path.join(report.dir, 'sidebar-preview-observation.json'), JSON.stringify(observed, null, 2));
  report.ok('sidebar preview shows Markdown as plain text', observed?.preview === EXPECTED,
    { want: EXPECTED, observed });
}

module.exports = { sidebarPreviewPlainText };
