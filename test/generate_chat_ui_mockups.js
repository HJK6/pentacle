const fs = require('node:fs');
const path = require('node:path');

const chatUi = require('../renderer/chat_ui_state');

const root = path.join(__dirname, '..');
const fixturesPath = path.join(__dirname, 'fixtures', 'slot_chat_state_subjects.json');
const outDir = path.join(__dirname, 'artifacts');
const outPath = path.join(outDir, 'chat_ui_mockups.html');

const fixtures = JSON.parse(fs.readFileSync(fixturesPath, 'utf8'));

fs.mkdirSync(outDir, { recursive: true });
fs.writeFileSync(outPath, buildHtml(fixtures), 'utf8');
console.log(outPath);

function buildHtml(data) {
  const sections = [
    renderEventSection(data.render_event_cases || []),
    renderBodySection(data.body_cases || []),
    renderSummarySection(data.summary_cases || []),
    renderComposerSection(data.composer_cases || []),
    renderWorkingTimeSection(data.working_time_cases || []),
    renderStatusSection(data.status_cases || []),
    renderDraftPreviewSection(data.draft_preview_cases || []),
    renderCombinedSection(data),
  ].join('\n');

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Chat UI Mockups</title>
  <link rel="stylesheet" href="../../renderer/styles.css">
  <style>
    :root {
      color-scheme: dark;
      --mock-bg: #09100d;
      --mock-panel: rgba(14, 21, 18, 0.98);
      --mock-panel-2: rgba(18, 28, 23, 0.98);
      --mock-border: rgba(53, 80, 67, 0.9);
      --mock-text: #eaf5ef;
      --mock-dim: #9ab1a3;
      --mock-accent: #8de0bb;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        radial-gradient(circle at top left, rgba(41, 84, 64, 0.26), transparent 34%),
        radial-gradient(circle at top right, rgba(17, 53, 87, 0.22), transparent 28%),
        linear-gradient(180deg, #08100c, #0b130f 42%, #0a110e);
      color: var(--mock-text);
      font-family: "SF Pro Text", "IBM Plex Sans", "Segoe UI", sans-serif;
      padding: 28px;
    }
    .mock-page {
      max-width: 1560px;
      margin: 0 auto;
    }
    .mock-hero {
      display: flex;
      justify-content: space-between;
      align-items: end;
      gap: 24px;
      margin-bottom: 28px;
      padding: 20px 22px;
      border: 1px solid var(--mock-border);
      border-radius: 18px;
      background: linear-gradient(180deg, rgba(12, 21, 17, 0.98), rgba(9, 16, 13, 0.98));
      box-shadow: 0 18px 48px rgba(0, 0, 0, 0.28);
    }
    .mock-hero h1 {
      margin: 0 0 6px;
      font-size: 28px;
      line-height: 1.1;
      letter-spacing: -0.03em;
    }
    .mock-hero p {
      margin: 0;
      max-width: 820px;
      color: var(--mock-dim);
      font-size: 14px;
      line-height: 1.5;
    }
    .mock-hero-meta {
      font-size: 12px;
      color: var(--mock-accent);
      text-transform: uppercase;
      letter-spacing: 0.12em;
      white-space: nowrap;
    }
    .mock-section {
      margin-bottom: 30px;
    }
    .mock-section-header {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 12px;
    }
    .mock-section-header h2 {
      margin: 0;
      font-size: 18px;
      letter-spacing: -0.02em;
    }
    .mock-section-header p {
      margin: 0;
      color: var(--mock-dim);
      font-size: 13px;
    }
    .mock-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
      gap: 16px;
    }
    .mock-card {
      border: 1px solid var(--mock-border);
      border-radius: 18px;
      overflow: hidden;
      background: linear-gradient(180deg, rgba(15, 23, 19, 0.98), rgba(10, 16, 13, 0.98));
      box-shadow: 0 12px 36px rgba(0, 0, 0, 0.22);
    }
    .mock-card-head {
      padding: 16px 18px 12px;
      border-bottom: 1px solid rgba(46, 70, 58, 0.75);
      background: linear-gradient(180deg, rgba(18, 30, 24, 0.98), rgba(13, 21, 17, 0.98));
    }
    .mock-kicker {
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid rgba(79, 122, 101, 0.75);
      color: var(--mock-accent);
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.11em;
      text-transform: uppercase;
    }
    .mock-card-head h3 {
      margin: 10px 0 6px;
      font-size: 16px;
      letter-spacing: -0.02em;
    }
    .mock-expected {
      margin: 0;
      color: #d8e8df;
      font-size: 13px;
      line-height: 1.5;
    }
    .mock-card-body {
      display: grid;
      grid-template-columns: minmax(0, 0.94fr) minmax(0, 1.06fr);
      gap: 0;
    }
    .mock-column {
      min-width: 0;
      padding: 16px 18px 18px;
    }
    .mock-column + .mock-column {
      border-left: 1px solid rgba(38, 59, 49, 0.7);
      background: rgba(9, 16, 13, 0.42);
    }
    .mock-label {
      margin: 0 0 10px;
      color: var(--mock-dim);
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }
    .mock-code {
      margin: 0;
      border-radius: 12px;
      border: 1px solid rgba(56, 85, 71, 0.72);
      background: rgba(7, 12, 10, 0.96);
      color: #deebe4;
      font: 11px/1.55 "SF Mono", "Iosevka", "Fira Code", monospace;
      padding: 12px 13px;
      white-space: pre-wrap;
      word-break: break-word;
      min-height: 96px;
    }
    .mock-shell {
      border: 1px solid rgba(48, 73, 61, 0.84);
      border-radius: 14px;
      background: linear-gradient(180deg, rgba(10, 17, 14, 0.98), rgba(8, 13, 11, 0.98));
      padding: 14px;
      min-height: 96px;
    }
    .mock-chat-shell {
      padding: 14px;
      border: 1px solid rgba(44, 67, 56, 0.78);
      border-radius: 16px;
      background: linear-gradient(180deg, rgba(12, 19, 16, 0.98), rgba(9, 14, 12, 0.98));
    }
    .mock-chat-shell .slot-chat-status {
      padding-top: 0;
      margin-bottom: 10px;
    }
    .mock-chat-shell .slot-chat-draft-preview-host:empty {
      display: none;
    }
    .mock-chat-shell .slot-chat-draft-preview-host:not(:empty) {
      display: block;
      margin-bottom: 10px;
    }
    .mock-chat-shell .slot-chat-message:last-child {
      margin-bottom: 0;
    }
    .mock-summary-value {
      border-radius: 12px;
      border: 1px solid rgba(56, 85, 71, 0.72);
      background: rgba(7, 12, 10, 0.96);
      min-height: 46px;
      padding: 12px 13px;
      color: #deebe4;
      font-size: 12px;
      line-height: 1.5;
    }
    .mock-summary-value.is-empty {
      color: #6f877b;
      font-style: italic;
    }
    .mock-inline {
      display: flex;
      align-items: flex-start;
      gap: 14px;
      flex-wrap: wrap;
    }
    .mock-inline > * {
      flex: 0 0 auto;
    }
    .mock-composer {
      min-width: 220px;
      max-width: 320px;
    }
    .mock-composer textarea {
      width: 100%;
      min-height: 54px;
    }
    .mock-note {
      margin-top: 8px;
      color: #91a69a;
      font-size: 12px;
      line-height: 1.45;
    }
  </style>
</head>
<body>
  <div class="mock-page">
    <section class="mock-hero">
      <div>
        <h1>Chat UI Mockups</h1>
        <p>Fixture-driven visual spec page generated from the current production chat renderer. Every preview below is built from the same production helpers used by the desktop UI, so these cards double as design review material and regression targets.</p>
      </div>
      <div class="mock-hero-meta">Generated from fixture cases</div>
    </section>
    ${sections}
  </div>
</body>
</html>`;
}

function renderEventSection(cases) {
  return renderSection(
    'Transcript Events',
    'Single event rendering: command rows, edit rows, and plain assistant text.',
    cases.map((item) => renderCard({
      kind: 'event',
      name: item.name,
      expected: item.expected_output,
      inputLabel: 'Source event',
      inputBody: escapeJson(item.event),
      previewLabel: 'Production preview',
      previewBody: wrapChatMessage(chatUi.renderEventContent(item.event, item.event?.kind === 'USER'))
    }))
  );
}

function renderBodySection(cases) {
  return renderSection(
    'Transcript Bodies',
    'Multi-line transcript body transformations using the production body renderer.',
    cases.map((item) => renderCard({
      kind: 'body',
      name: item.name,
      expected: item.expected_output,
      inputLabel: 'Source body',
      inputBody: item.text,
      previewLabel: 'Production preview',
      previewBody: wrapChatShell(`<div class="slot-chat-message"><div class="slot-chat-message-body">${chatUi.renderChatBody(item.text, false)}</div></div>`)
    }))
  );
}

function renderSummarySection(cases) {
  return renderSection(
    'Sidebar Summaries',
    'What the sidebar detail should keep versus suppress.',
    cases.map((item) => {
      const value = chatUi.sanitizeSidebarDetail(item.input);
      return renderCard({
        kind: 'summary',
        name: item.name,
        expected: item.expected_output,
        inputLabel: 'Source detail',
        inputBody: item.input,
        previewLabel: 'Expected visible output',
        previewBody: `<div class="mock-summary-value ${value ? '' : 'is-empty'}">${value ? escapeHtml(value) : 'Hidden in sidebar'}</div>`
      });
    })
  );
}

function renderComposerSection(cases) {
  return renderSection(
    'Composer Ownership',
    'The chat composer should mirror the remote in-progress draft until the user takes ownership and edits it locally.',
    cases.map((item) => {
      const value = chatUi.deriveComposerInputValue(item.local_draft, item.remote_draft, item.local_draft_touched);
      return renderCard({
        kind: 'composer',
        name: item.name,
        expected: item.expected_output,
        inputLabel: 'Draft state input',
        inputBody: JSON.stringify({
          remote_draft: item.remote_draft || '',
          local_draft: item.local_draft || '',
          local_draft_touched: !!item.local_draft_touched,
        }, null, 2),
        previewLabel: 'Composer preview',
        previewBody: `<div class="mock-inline"><div class="mock-composer"><textarea class="slot-chat-compose-input${item.remote_draft && !item.local_draft_touched ? ' is-remote-draft' : ''}${item.remote_pending && !item.local_draft_touched ? ' is-remote-pending' : ''}" readonly>${escapeHtml(value)}</textarea><div class="mock-note">${item.local_draft_touched ? 'Composer stays under local edit control.' : (value ? 'Composer mirrors the remote in-progress draft.' : 'Composer is empty.')}</div></div></div>`
      });
    })
  );
}

function renderWorkingTimeSection(cases) {
  return renderSection(
    'Working Time Extraction',
    'Time parsing cases that feed the working badge timer.',
    cases.map((item) => {
      const value = chatUi.extractWorkingTime(item.input);
      return renderCard({
        kind: 'timer',
        name: item.name,
        expected: item.expected_output,
        inputLabel: 'Source line',
        inputBody: item.input,
        previewLabel: 'Extracted timer',
        previewBody: wrapShell(`<span class="slot-chat-status-badge is-working"><span class="slot-chat-status-dot"></span><span class="slot-chat-status-timer">${escapeHtml(value)}</span></span>`)
      });
    })
  );
}

function renderStatusSection(cases) {
  return renderSection(
    'Status Bar Mockups',
    'Badge-level previews for working, waiting, idle, and pending combinations.',
    cases.map((item) => renderCard({
      kind: 'status',
      name: item.name,
      expected: item.expected_output,
      inputLabel: 'Source state',
      inputBody: escapeJson({
        activity: item.activity,
        workingLabel: item.workingLabel,
        pending: item.pending,
      }),
      previewLabel: 'Production preview',
      previewBody: wrapShell(`<div class="slot-chat-status is-${escapeHtml(item.activity)}">${chatUi.renderStatusBadges(item)}</div>`)
    }))
  );
}

function renderDraftPreviewSection(cases) {
  return renderSection(
    'Draft Preview Mockups',
    'Remote draft-state previews rendered with production markup.',
    cases.map((item) => renderCard({
      kind: 'draft',
      name: item.name,
      expected: item.expected_output,
      inputLabel: 'Source state',
      inputBody: escapeJson(item),
      previewLabel: 'Production preview',
      previewBody: wrapShell(chatUi.renderDraftPreview(item))
    }))
  );
}

function renderCombinedSection(data) {
  const combined = [
    {
      name: 'working-chat-with-clean-transcript',
      expected_output: 'The status bar carries the working state and timer. The transcript keeps normal assistant text, preserves the Explored block, and hides the raw Working(...) line.',
      status: data.status_cases?.find((x) => x.name === 'working-with-timer-and-pending') || { activity: 'working', workingLabel: '2m 08s', pending: true },
      draft: data.draft_preview_cases?.find((x) => x.name === 'working-draft-preview') || { remoteDraft: 'Checking renderer state now.', remotePending: false, activity: 'working', timestampLabel: '2:46 AM' },
      body: `I’m checking the renderer path against the websocket state now.\n\nExplored\n└ Read app.js\n  Search effectiveDraft|shouldMirrorRemoteDraft|draftPreviewEl in app.js\n\n• Working (2m 08s • esc to interrupt) · 3 background terminals running · /ps to view · /stop to close`
    },
    {
      name: 'queued-chat-before-send',
      expected_output: 'Queued state should read clearly before send: pending badge, queued draft preview, and a readable pending transcript block.',
      status: data.status_cases?.find((x) => x.name === 'waiting-without-timer') || { activity: 'waiting', workingLabel: '', pending: false },
      draft: data.draft_preview_cases?.find((x) => x.name === 'queued-draft-preview') || { remoteDraft: 'fix the timer and keep the working chip visible', remotePending: true, activity: 'waiting', timestampLabel: '2:45 AM' },
      body: 'Messages to be submitted after next tool call (press esc to interrupt and send immediately)\\n↳ fix the timer and keep the working chip visible'
    }
  ];

  return renderSection(
    'Full Chat Shell Mockups',
    'Higher-level assembled screens using the same renderer pieces as production.',
    combined.map((item) => renderCard({
      kind: 'shell',
      name: item.name,
      expected: item.expected_output,
      inputLabel: 'Source state',
      inputBody: escapeJson({
        status: item.status,
        draft: item.draft,
        body: item.body,
      }),
      previewLabel: 'Full shell preview',
      previewBody: wrapChatShell(
        `<div class="slot-chat-status is-${escapeHtml(item.status.activity)}">${chatUi.renderStatusBadges(item.status)}</div>` +
        `<div class="slot-chat-draft-preview-host">${chatUi.renderDraftPreview(item.draft)}</div>` +
        `<div class="slot-chat-message"><div class="slot-chat-message-body">${chatUi.renderChatBody(item.body, false)}</div></div>`
      )
    }))
  );
}

function renderSection(title, description, cards) {
  return `
    <section class="mock-section">
      <div class="mock-section-header">
        <h2>${escapeHtml(title)}</h2>
        <p>${escapeHtml(description)}</p>
      </div>
      <div class="mock-grid">
        ${cards.join('\n')}
      </div>
    </section>`;
}

function renderCard({ kind, name, expected, inputLabel, inputBody, previewLabel, previewBody }) {
  return `
    <article class="mock-card">
      <header class="mock-card-head">
        <span class="mock-kicker">${escapeHtml(kind)}</span>
        <h3>${escapeHtml(name)}</h3>
        <p class="mock-expected">${escapeHtml(expected || '')}</p>
      </header>
      <div class="mock-card-body">
        <section class="mock-column">
          <div class="mock-label">${escapeHtml(inputLabel)}</div>
          <pre class="mock-code">${escapeHtml(inputBody || '')}</pre>
        </section>
        <section class="mock-column">
          <div class="mock-label">${escapeHtml(previewLabel)}</div>
          ${previewBody}
        </section>
      </div>
    </article>`;
}

function wrapShell(inner) {
  return `<div class="mock-shell">${inner}</div>`;
}

function wrapChatShell(inner) {
  return `<div class="mock-chat-shell">${inner}</div>`;
}

function wrapChatMessage(inner) {
  return wrapChatShell(`<div class="slot-chat-message"><div class="slot-chat-message-body">${inner}</div></div>`);
}

function escapeJson(value) {
  return JSON.stringify(value, null, 2);
}

function escapeHtml(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
