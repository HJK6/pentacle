function escapeHtml(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function normalizedEventText(text) {
  return String(text || '').replace(/\s+/g, ' ').trim();
}

function collapseCodeBlocks(text) {
  return String(text || '').replace(/```[\s\S]*?```/g, '[code hidden]');
}

function hostTitle(host) {
  if (host === 'bart') return 'Bartimaeus';
  if (host === 'merlin' || host === 'abra') return 'Merlin';
  if (host === 'amaterasu') return 'Amaterasu';
  return host ? String(host).charAt(0).toUpperCase() + String(host).slice(1) : 'Agent';
}

function hostChrome(host) {
  if (host === 'bart') return { header: '#3a1028', accent: '#ff7ab8', surface: '#21101b', border: '#8f3d68', title: 'Bartimaeus' };
  if (host === 'merlin' || host === 'abra') return { header: '#102a4a', accent: '#4da3ff', surface: '#0c1827', border: '#2f6ca5', title: 'Merlin' };
  if (host === 'amaterasu') return { header: '#3a1218', accent: '#ff4d5e', surface: '#211014', border: '#a83242', title: 'Amaterasu' };
  return { header: '#101a16', accent: '#7ef0ba', surface: '#101a16', border: '#356150', title: hostTitle(host) };
}

function isCodexHelperSuggestion(text) {
  const normalized = normalizedEventText(text).toLowerCase();
  if (!normalized) return false;
  const exact = new Set([
    'summarize recent commits',
    'explore the repository structure',
    'find the relevant code',
    'inspect recent changes',
    'review the failing test',
    'run /review on my current changes',
    'run /review on my current changes.',
    'use /skills to list available skills',
    'write tests for @filename',
    'implement {feature}',
    'improve documentation in @filename',
    'find and fix a bug in @filename',
    'explain this codebase',
  ]);
  if (exact.has(normalized)) return true;
  return /^(summarize|explore|inspect|review|find|run|use|write|implement|improve|explain)\b/.test(normalized) && normalized.split(/\s+/).length <= 8;
}

function isWorkingStatusText(text) {
  const normalized = normalizedEventText(text);
  return (
    /^Working\s*\(/.test(normalized) ||
    /^•\s*Working\s*\(/.test(normalized) ||
    /^Booting MCP server\b/i.test(normalized) ||
    /\(\d+s\s*•\s*esc to interrupt\)$/i.test(normalized) ||
    /◦\s*Working\s*\(/.test(normalized)
  );
}

function isTerminalDividerText(text) {
  const normalized = normalizedEventText(text);
  return /^[-─━═]+\s*Worked for\b/i.test(normalized) || /^Worked for\b/i.test(normalized);
}

function terminalDividerLabel(text) {
  return normalizedEventText(text)
    .replace(/^[-─━═]+\s*/, '')
    .replace(/\s*[-─━═]+$/, '')
    .trim();
}

function isContextCompactedText(text) {
  return /^Context Compacted\b/i.test(normalizedEventText(text));
}

function isTransientTranscriptNoise(text) {
  const normalized = normalizedEventText(text);
  if (!normalized) return true;
  if (isWorkingStatusText(normalized)) return true;
  if (normalized === 'Waited for background terminal') return true;
  if (/^Waited for background terminal\s+[·-]/.test(normalized)) return true;
  if (/^Waited for background terminal\s+[-─]\s+Worked for\b/.test(normalized)) return true;
  return false;
}

function stripWorkingStatus(text) {
  return String(text || '')
    .split('\n')
    .filter((line) => !isWorkingStatusText(line))
    .join('\n')
    .replace(/\s*◦\s*Working\s*\([^)]*\).*$/gm, '')
    .trim();
}

function classifyToolText(text) {
  if (/^(Read|Glob|Grep|Search|List)\b/i.test(text)) return 'explore-action';
  if (/^Edit(ed)?\b/i.test(text)) return 'edit-action';
  if (/^Write\b/i.test(text)) return 'write-action';
  if (/^(Bash|Ran)\b/i.test(text)) return 'tool-command';
  return 'tool-command';
}

function displayRuleForToolText(text) {
  const toolCase = classifyToolText(text);
  if (toolCase === 'explore-action') return 'activity:explored';
  if (toolCase === 'edit-action' || toolCase === 'write-action') return 'activity:file-change';
  return 'activity:command';
}

function classifyAssistantText(text) {
  if (/^Explored\b/i.test(text)) return 'explore-action';
  if (/^Edited\b/i.test(text)) return 'edit-action';
  if (/^Wrote\b/i.test(text)) return 'write-action';
  if (/^Ran\b/i.test(text)) return 'tool-command';
  if (/^(I('|’)m|I am|I’ll|I will|Checking|Inspecting|Reading|Looking)\b/i.test(text) && text.length < 220) return 'assistant-progress';
  return 'assistant-message';
}

function displayRuleForAssistantCase(caseId) {
  if (caseId === 'explore-action') return 'activity:explored';
  if (caseId === 'edit-action' || caseId === 'write-action') return 'activity:file-change';
  if (caseId === 'tool-command') return 'activity:command';
  if (caseId === 'assistant-progress') return 'activity:progress';
  return 'bubble:assistant';
}

function interpreted(event, caseId, displayRule, tone, label, text, hidden, reason) {
  return { event, caseId, displayRule, tone, label, text, hidden, reason };
}

function interpretPentacleEvent(event, assistantLabel = 'Agent') {
  const kind = String(event?.kind || '').toUpperCase();
  const text = String(event?.text || '');
  const collapsedText = collapseCodeBlocks(stripWorkingStatus(text) || text).trim();
  const normalized = normalizedEventText(text);

  if (kind === 'DRAFT') {
    const pending = Boolean(event?.raw?.pending);
    return interpreted(event, pending ? 'queued-draft' : 'draft', 'draft:composer', 'assistant', 'Draft', text, true, 'Draft state is rendered outside committed transcript rows.');
  }
  if (kind === 'USER' && isCodexHelperSuggestion(text)) {
    return interpreted(event, 'codex-helper-suggestion', 'hidden:helper', 'user', '', text, true, 'Codex starter/helper prompts are not durable user conversation.');
  }
  if (isTerminalDividerText(text)) {
    return interpreted(event, 'terminal-divider', 'terminal:divider', 'system', '', terminalDividerLabel(text), false, 'Terminal divider rows are rendered as full-width separators.');
  }
  if (isContextCompactedText(text)) {
    return interpreted(event, 'context-compacted', 'system:compacted', 'system', 'Context', collapsedText, false, 'Context compaction is a distinct system milestone.');
  }
  if ((kind === 'ASSIST' || kind === 'TOOL' || kind === 'TOOL-OUT') && isTransientTranscriptNoise(text)) {
    return interpreted(event, isWorkingStatusText(text) ? 'working-status' : 'transient-noise', isWorkingStatusText(text) ? 'hidden:status' : 'hidden:noise', kind === 'ASSIST' ? 'assistant' : 'tool', kind === 'ASSIST' ? assistantLabel : 'Tool', text, true, 'Transient status belongs in the status dock or should be suppressed.');
  }
  if (kind === 'USER') return interpreted(event, 'user-message', 'bubble:user', 'user', '', collapsedText, false, 'Durable user message.');
  if (kind === 'THINK') return interpreted(event, 'thinking', 'activity:thinking', 'thinking', 'Thinking', collapsedText, false, 'Model thinking/tool-planning activity.');
  if (kind === 'SYSTEM') return interpreted(event, 'system', 'activity:system', 'system', 'System', collapsedText, false, 'System or runtime message.');
  if (kind === 'TOOL') return interpreted(event, classifyToolText(normalized), displayRuleForToolText(normalized), 'tool', 'Tool', collapsedText, false, 'Tool invocation or file action.');
  if (kind === 'TOOL-OUT') return interpreted(event, 'tool-output', 'activity:tool-output', 'tool', 'Tool', collapsedText, false, 'Tool output/result.');
  if (kind === 'ASSIST') {
    const assistCase = classifyAssistantText(normalized);
    return interpreted(event, assistCase, displayRuleForAssistantCase(assistCase), 'assistant', assistantLabel, collapsedText, false, 'Assistant transcript content.');
  }
  return interpreted(event, 'unknown', 'bubble:assistant', 'assistant', kind || assistantLabel, collapsedText, false, 'Unknown event kind falls back to assistant rendering.');
}

function isProgressiveUpdate(previousText, nextText) {
  if (previousText.length < 40 || nextText.length < 40) return false;
  return previousText.startsWith(nextText) || nextText.startsWith(previousText);
}

function coalesceInterpretedEvents(events) {
  const result = [];
  for (const item of events) {
    const text = normalizedEventText(item.text);
    const existingIndex = result.findIndex((existing) => {
      if (existing.event.kind !== item.event.kind) return false;
      if (existing.displayRule !== item.displayRule) return false;
      const existingText = normalizedEventText(existing.text);
      if (existingText === text) return true;
      if (!isProgressiveUpdate(existingText, text)) return false;
      return result.length - result.indexOf(existing) <= 4;
    });
    if (existingIndex === -1) {
      result.push(item);
      continue;
    }
    const existing = result[existingIndex];
    if (text.length > normalizedEventText(existing.text).length && result.length - existingIndex <= 4) {
      result[existingIndex] = item;
    }
  }
  return result;
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

function displayTitleForSession(session = {}) {
  const title = String(session.title || session.display_name || '').trim();
  return title || session.session_name || session.stream_id || 'Pentacle chat';
}

function formatClock(timestamp) {
  if (!timestamp) return '';
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

function formatRelative(timestamp) {
  if (!timestamp) return 'No activity yet';
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return 'No activity yet';
  const deltaMs = Date.now() - date.getTime();
  const minute = 60_000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (deltaMs < minute) return 'Just now';
  if (deltaMs < hour) return `${Math.max(1, Math.round(deltaMs / minute))}m ago`;
  if (deltaMs < day) return `${Math.max(1, Math.round(deltaMs / hour))}h ago`;
  return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function resolveLiveFlags(session, draftEvent) {
  const working = Boolean(session?.working || draftEvent?.raw?.working);
  const workingLabel = working
    ? String(session?.working_label || draftEvent?.raw?.working_label || '').trim()
    : '';
  return { working, workingLabel };
}

function sessionStatus(session, draftEvent) {
  if (!session) return 'offline';
  const liveFlags = resolveLiveFlags(session, draftEvent);
  if (!session.online) return 'offline';
  if (liveFlags.working) return 'working';
  if (session.last_event_at) return 'live';
  return 'idle';
}

function statusLabel(status) {
  if (status === 'working') return 'Working';
  if (status === 'live') return 'Live';
  if (status === 'idle') return 'Idle';
  return 'Offline';
}

function transcriptMeta(kind, assistantLabel) {
  if (kind === 'USER') return { tone: 'user', label: '', isUser: true };
  if (kind === 'ASSIST') return { tone: 'assistant', label: assistantLabel, isUser: false };
  if (kind === 'TOOL' || kind === 'TOOL-OUT') return { tone: 'tool', label: 'Tool', isUser: false };
  if (kind === 'THINK') return { tone: 'thinking', label: 'Thinking', isUser: false };
  if (kind === 'SYSTEM') return { tone: 'system', label: 'System', isUser: false };
  return { tone: 'assistant', label: kind, isUser: false };
}

function selectSessionDetail(streamState = {}, streamId, options = {}) {
  const session = (streamState.sessions || []).find((item) => item.stream_id === streamId);
  if (!session) return null;
  const includeTools = Boolean(options.includeTools);
  const includeSystem = Boolean(options.includeSystem);
  const includeDraft = options.includeDraft !== false;
  const visibleCount = Math.max(0, options.visibleCount ?? 120);
  const draftEvent = (streamState.drafts || {})[streamId];
  const assistantLabel = hostTitle(session.host);
  const baseEvents = (streamState.events || []).filter((item) => item.stream_id === streamId && item.kind !== 'DRAFT');
  const interpretedEvents = baseEvents.map((event) => interpretPentacleEvent(event, assistantLabel));
  const filteredEvents = interpretedEvents.filter((item) => {
    if (item.hidden) return false;
    if (!includeTools && (item.tone === 'tool' || item.tone === 'thinking')) return false;
    if (!includeSystem && item.tone === 'system') return false;
    return true;
  });
  const dedupedEvents = coalesceInterpretedEvents(filteredEvents);
  const visibleEvents = dedupedEvents.slice(-visibleCount);
  const transcriptItems = visibleEvents.map((item) => {
    const event = item.event;
    const text = item.text.trim() || ' ';
    return {
      id: String(event.daemon_seq ?? `${event.stream_id}:${event.timestamp}:${event.kind}:${text}`),
      timestampLabel: formatClock(event.timestamp),
      label: item.label,
      tone: item.tone,
      text,
      kind: event.kind,
      isUser: item.tone === 'user',
      eventCase: item.caseId,
      displayRule: item.displayRule,
    };
  });

  const liveFlags = resolveLiveFlags(session, includeDraft ? draftEvent : undefined);
  const status = sessionStatus(session, includeDraft ? draftEvent : undefined);
  const fallbackText = normalizedEventText(session.last_text || '');
  const fallbackKind = session.last_kind || 'ASSIST';
  const fallbackMeta = transcriptMeta(fallbackKind, assistantLabel);
  const fallbackEvent = {
    daemon_seq: -1,
    host: session.host,
    provider: session.provider,
    session_id: streamId,
    session_name: session.session_name,
    stream_id: streamId,
    timestamp: session.last_event_at,
    kind: fallbackKind,
    text: fallbackText,
  };
  const fallbackInterpretation = interpretPentacleEvent(fallbackEvent, assistantLabel);
  const lastTranscriptItem = transcriptItems[transcriptItems.length - 1];
  const shouldAppendFallback = Boolean(
    fallbackText &&
    !fallbackInterpretation.hidden &&
    !isTransientTranscriptNoise(fallbackText) &&
    (
      !lastTranscriptItem ||
      normalizedEventText(lastTranscriptItem.text) !== fallbackText ||
      lastTranscriptItem.kind !== fallbackKind
    ),
  );
  if (shouldAppendFallback) {
    transcriptItems.push({
      id: `fallback:${streamId}`,
      timestampLabel: formatClock(session.last_event_at),
      label: fallbackMeta.label,
      tone: fallbackMeta.tone,
      text: collapseCodeBlocks(fallbackText),
      kind: fallbackKind,
      isUser: fallbackMeta.isUser,
      eventCase: fallbackInterpretation.caseId,
      displayRule: fallbackInterpretation.displayRule,
    });
  }

  return {
    streamId,
    host: session.host,
    title: displayTitleForSession(session),
    hostTitle: hostTitle(session.host),
    providerLabel: String(session.provider || '').toUpperCase(),
    status,
    statusLabel: statusLabel(status),
    summaryLabel: session.last_event_at ? `Updated ${formatRelative(session.last_event_at)}` : 'No activity yet',
    workingLabel: liveFlags.workingLabel,
    draftText: includeDraft ? draftEvent?.text || session.draft || '' : '',
    transcriptItems,
    hiddenCount: Math.max(0, baseEvents.length - filteredEvents.length),
    remainingCount: Math.max(0, dedupedEvents.length - visibleEvents.length),
  };
}

function selectSessionDetailForDesktopSession(streamState = {}, desktopSession = {}, streamHost, options = {}) {
  const session = (streamState.sessions || []).find((item) => (
    item.stream_id === desktopSession.streamId ||
    (item.host === streamHost && item.session_name === desktopSession.name)
  ));
  if (session) return selectSessionDetail(streamState, session.stream_id, options);
  return null;
}

function splitActivityText(text) {
  const lines = String(text || '')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
  const title = lines[0] || 'Activity';
  const detail = lines.slice(1).join('\n');
  return {
    title,
    detail: detail.length <= 220 ? detail : `${detail.slice(0, 217).trim()}...`,
  };
}

function summarizeCommandOutput(output) {
  if (!output) return '';
  const lines = String(output).split('\n').map((line) => line.trimEnd());
  if (lines.length <= 3) return lines.join('\n').trim();
  const visible = lines.slice(0, 3).join('\n').trim();
  const hiddenCount = lines.length - 3;
  return `${visible}\n+${hiddenCount} more line${hiddenCount === 1 ? '' : 's'}`;
}

function parseAssistantBlocks(text) {
  return String(text || '')
    .split(/\n{2,}/)
    .map((chunk) => chunk.trim())
    .filter(Boolean)
    .map((chunk) => {
      const lines = chunk.split('\n');
      const first = lines[0] || '';
      const rest = lines.slice(1).join('\n').trim();
      if (first.startsWith('Edited ')) {
        const match = first.match(/^Edited\s+(.+?)\s+\((.+)\)$/);
        return {
          type: 'edit',
          title: match?.[1] || first.replace(/^Edited\s+/, ''),
          meta: match?.[2] || '',
          body: rest,
        };
      }
      if (first.startsWith('Ran ')) {
        return {
          type: 'command',
          command: first.replace(/^Ran\s+/, ''),
          output: summarizeCommandOutput(rest.replace(/^└\s*/gm, '').trim()),
        };
      }
      return { type: 'text', text: chunk };
    });
}

function renderFormattedAssistantText(text) {
  return renderChatBody(text, false);
}

function renderTranscriptItem(item, chrome = hostChrome('')) {
  if (!item) return '';
  if (item.isUser) {
    return `<article class="slot-chat-row is-user"><div class="slot-chat-user-bubble">${escapeHtml(item.text)}</div></article>`;
  }
  if (item.displayRule === 'terminal:divider') {
    return `<div class="slot-chat-terminal-divider"><span></span><b>${escapeHtml(item.text)}</b><span></span></div>`;
  }
  if (item.displayRule === 'system:compacted') {
    return `<div class="slot-chat-compacted"><span>↘</span>${escapeHtml(item.text)}</div>`;
  }
  if (String(item.displayRule || '').startsWith('activity:')) {
    const activity = splitActivityText(item.text);
    return `<article class="slot-chat-row"><div class="slot-chat-activity" style="--machine:${escapeHtml(chrome.accent)};--machine-surface:${escapeHtml(chrome.surface)};--machine-border:${escapeHtml(chrome.border)};">
      <span class="slot-chat-activity-dot"></span>
      <div class="slot-chat-activity-body">
        <b>${escapeHtml(activity.title)}</b>
        ${activity.detail ? `<p>${escapeHtml(activity.detail)}</p>` : ''}
      </div>
    </div></article>`;
  }
  const blocks = parseAssistantBlocks(item.text);
  return `<article class="slot-chat-row"><div class="slot-chat-assistant-card">
    ${blocks.map((block) => {
      if (block.type === 'edit') {
        return `<div class="slot-chat-file-card" style="--machine:${escapeHtml(chrome.accent)};--machine-surface:${escapeHtml(chrome.surface)};--machine-border:${escapeHtml(chrome.border)};"><b>${escapeHtml(block.title)}</b>${block.meta ? `<p>${escapeHtml(block.meta)}</p>` : ''}</div>`;
      }
      if (block.type === 'command') {
        return `<div class="slot-chat-command-card" style="--machine:${escapeHtml(chrome.accent)};--machine-surface:${escapeHtml(chrome.surface)};--machine-border:${escapeHtml(chrome.border)};"><b>${escapeHtml(block.command)}</b>${block.output ? `<pre>${escapeHtml(block.output)}</pre>` : ''}</div>`;
      }
      return renderFormattedAssistantText(block.text);
    }).join('')}
  </div></article>`;
}

function renderTranscriptTimeline(detail, chrome = hostChrome('')) {
  const items = detail?.transcriptItems || [];
  if (!items.length) return '';
  let lastMachineMinute = '';
  return items.map((item) => {
    if (item.isUser) return renderTranscriptItem(item, chrome);
    const showTimestamp = Boolean(item.timestampLabel && item.timestampLabel !== lastMachineMinute);
    if (item.timestampLabel) lastMachineMinute = item.timestampLabel;
    return `${showTimestamp ? `<div class="slot-chat-timestamp">${escapeHtml(item.timestampLabel)}</div>` : ''}${renderTranscriptItem(item, chrome)}`;
  }).join('');
}

module.exports = {
  deriveComposerInputValue,
  escapeHtml,
  extractSummary,
  extractWorkingTime,
  hostChrome,
  hostTitle,
  interpretPentacleEvent,
  isCodeLikeEvent,
  isSummaryNoiseLine,
  isTransientTranscriptNoise,
  renderChatBody,
  renderDraftPreview,
  renderEventContent,
  renderStatusBadges,
  renderTranscriptItem,
  renderTranscriptTimeline,
  sanitizeSidebarDetail,
  selectSessionDetail,
  selectSessionDetailForDesktopSession,
};
