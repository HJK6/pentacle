function escapeHtml(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function extractWorkingTime(text) {
  const raw = String(text || '').trim().replace(/^[•⎿]\s+/, '');
  const match = raw.match(/^Working \(([^)]*)/);
  if (!match) return '';
  return match[1].split('•')[0].trim();
}

function isCodeLikeEvent(event) {
  const text = String(event?.text || '');
  if (event?.kind === 'TOOL-OUT') return true;
  return /^(Edited|Wrote|Created|Deleted|Updated|Applied patch|Move to:|Add File:|Update File:|Delete File:)\b/.test(text);
}

function renderChatBody(text, isUser = false) {
  const raw = String(text || '');
  if (isUser) return escapeHtml(raw);
  const commandMatch = raw.match(/^Ran ([^\n]+)(?:\n|$)/);
  if (commandMatch) {
    return `<div class="slot-chat-line">Ran ${escapeHtml(commandMatch[1])}</div>`;
  }
  const lines = raw.split('\n');
  const rendered = [];
  let inList = false;

  const closeList = () => {
    if (inList) {
      rendered.push('</div>');
      inList = false;
    }
  };

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) {
      closeList();
      rendered.push('<div class="slot-chat-spacer"></div>');
      continue;
    }
    if (/^[•⎿]?\s*Working \([^)]*\).*\/ps to view.*\/stop to close/i.test(trimmed)) {
      continue;
    }
    if (/^[•⎿]?\s*Waiting for background terminal \([^)]*\).*\/ps to view.*\/stop to close/i.test(trimmed)) {
      continue;
    }
    if (/^Messages to be submitted after next tool call/i.test(trimmed)) {
      closeList();
      rendered.push('<div class="slot-chat-annotation is-pending">Queued for next tool call</div>');
      continue;
    }
    if (/^[↳→]\s*/.test(trimmed)) {
      closeList();
      rendered.push(`<div class="slot-chat-pending-body">${escapeHtml(trimmed.replace(/^[↳→]\s*/, ''))}</div>`);
      continue;
    }
    if (/^[─━═-]{6,}$/.test(trimmed) || /worked for \d+/i.test(trimmed)) {
      closeList();
      const label = trimmed.replace(/^[─━═-]+\s*/, '').replace(/\s*[─━═-]+$/, '');
      rendered.push(`<div class="slot-chat-annotation is-timing">${escapeHtml(label)}</div>`);
      continue;
    }
    if (/^(Explored|Ran|Viewed Image|Edited|Read|Search|Searched|Updated|Monitor|Waited for background terminal)$/i.test(trimmed)) {
      closeList();
      rendered.push(`<div class="slot-chat-annotation is-label">${escapeHtml(trimmed)}</div>`);
      continue;
    }
    if (/^[└├│]/.test(trimmed)) {
      if (!inList) {
        rendered.push('<div class="slot-chat-loglist">');
        inList = true;
      }
      rendered.push(`<div class="slot-chat-logitem">${escapeHtml(trimmed)}</div>`);
      continue;
    }
    closeList();
    rendered.push(`<div class="slot-chat-line">${escapeHtml(trimmed)}</div>`);
  }

  closeList();
  return rendered.join('');
}

function renderEventContent(event, isUser = false) {
  if (isUser) return renderChatBody(event?.text || '', true);
  if (isCodeLikeEvent(event)) {
    const text = String(event?.text || '').trim();
    const firstLine = text.split('\n')[0]?.trim() || 'Change made';
    return `<div class="slot-chat-change-pill">${escapeHtml(firstLine)}</div>`;
  }
  return renderChatBody(event?.text || '', false);
}

function isSummaryNoiseLine(line) {
  if (!line) return true;
  return (
    /^gpt-[\d.]+/i.test(line) ||
    /tab to queue message/i.test(line) ||
    /^new task\? \/clear to save/i.test(line) ||
    /bypass permissions on/i.test(line) ||
    /^tip: /i.test(line) ||
    /^messages to be submitted/i.test(line) ||
    /^press esc to interrupt/i.test(line) ||
    /^working \(\d+[smh]/i.test(line)
  );
}

function extractSummary(paneContent) {
  if (!paneContent) return '';
  const lines = String(paneContent).split('\n').map((l) => l.trim()).filter(Boolean);
  for (let i = lines.length - 1; i >= Math.max(0, lines.length - 30); i--) {
    const line = lines[i];
    if (isSummaryNoiseLine(line)) continue;
    if (/^(Read|Edit|Write|Bash|Grep|Glob|Agent|TodoWrite)\b/.test(line)) return line.slice(0, 80);
    if (/^(Working|Building|Testing|Fixing|Adding|Updating|Creating|Running|Searching|Installing)\b/i.test(line)) return line.slice(0, 80);
  }
  for (let i = lines.length - 1; i >= Math.max(0, lines.length - 10); i--) {
    const line = lines[i];
    if (line && !isSummaryNoiseLine(line) && !line.startsWith('❯') && !line.startsWith('›') && !line.includes('bypass') && !line.includes('auto-accept') && line.length > 3) {
      return line.slice(0, 80);
    }
  }
  return '';
}

function sanitizeSidebarDetail(text) {
  const value = String(text || '').trim();
  if (!value) return '';
  if (isSummaryNoiseLine(value)) return '';
  if (/^waiting for background terminal\b/i.test(value)) return '';
  return value;
}

function deriveComposerInputValue(localDraft, remoteDraft = '', localDraftTouched = false) {
  if (localDraftTouched) return String(localDraft || '');
  return String(localDraft || remoteDraft || '');
}

function renderStatusBadges({ activity = 'idle', workingLabel = '', pending = false } = {}) {
  const showPending = pending && activity !== 'working';
  const activityBadge = activity === 'working'
    ? `<span class="slot-chat-status-badge is-working" title="Working"><span class="slot-chat-status-dot"></span>${workingLabel ? `<span class="slot-chat-status-timer">${escapeHtml(workingLabel)}</span>` : ''}</span>`
    : activity === 'waiting'
      ? '<span class="slot-chat-status-badge is-waiting" title="Waiting"><span class="slot-chat-status-dot"></span></span>'
      : '<span class="slot-chat-status-badge is-idle" title="Ready"><span class="slot-chat-status-dot"></span></span>';
  const pendingBadge = showPending
    ? '<span class="slot-chat-status-badge is-pending" title="Pending send"><span class="slot-chat-status-dot"></span></span>'
    : '';
  return `
    <div class="slot-chat-status-left">
      ${activityBadge}
      ${pendingBadge}
    </div>`;
}

function renderDraftPreview({ remoteDraft = '', remotePending = false, activity = 'idle', timestampLabel = '' } = {}) {
  if (!remoteDraft) return '';
  const classes = [
    'slot-chat-draft-preview',
    remotePending ? 'is-pending' : '',
    activity === 'working' ? 'is-working' : '',
  ].filter(Boolean).join(' ');
  return `
    <div class="${classes}">
      <div class="slot-chat-draft-preview-head">
        <span class="slot-chat-draft-preview-label">${remotePending ? 'Queued for next tool call' : 'Draft in progress'}</span>
        <span class="slot-chat-draft-preview-time">${escapeHtml(timestampLabel)}</span>
      </div>
      <div class="slot-chat-draft-preview-body">${escapeHtml(remoteDraft)}</div>
    </div>`;
}

module.exports = {
  deriveComposerInputValue,
  escapeHtml,
  extractSummary,
  extractWorkingTime,
  isCodeLikeEvent,
  isSummaryNoiseLine,
  renderChatBody,
  renderDraftPreview,
  renderEventContent,
  renderStatusBadges,
  sanitizeSidebarDetail,
};
