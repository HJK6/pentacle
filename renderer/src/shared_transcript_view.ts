// Phase 4 (desktop_chat_ui_mobile_parity): shared-core transcript view renderer.
//
// This is the desktop transcript rendering path (Phase 7 cutover: the ONLY
// path). Given a `streamId` + a container element, it reads
// `window.PentacleChatStore.selectSessionDetail(streamId)` (the shared
// pentacle-chat-core chat-model selector) and renders the resulting
// `transcriptItems` into the desktop `slot-chat-*` DOM so `renderer/styles.css`
// supplies the base styles. Core disclosure metadata selects compact expandable
// tool and subagent surfaces; view options can add resolved durable answers.
//
// app.js delegates a slot's transcript rendering to
// `renderTranscriptTimelineHtml` (via window.PentacleChatView) unconditionally.
//
// displayRule -> DOM map:
//   bubble:user      -> <article.slot-chat-row.is-user><div.slot-chat-user-bubble> (text ESCAPED)
//   bubble:assistant -> <article.slot-chat-row><div.slot-chat-assistant-card> (parsed blocks, ESCAPED)
//   bubble:agent / tool disclosure -> compact preview and native details
//   activity:code-block -> exact preformatted code
//   other activity:* -> compact activity row (ESCAPED)
//   terminal:divider -> <div.slot-chat-terminal-divider>
//   system:compacted -> <div.slot-chat-compacted>
//   hidden:*         -> NOT rendered (the selector already drops these before
//                       they reach transcriptItems; we additionally guard here)
//   draft:composer   -> NOT rendered (drafts live outside the committed transcript)

import type {
  PentacleSessionDetail,
  PentacleSendState,
  PentacleTranscriptItem,
  MdBlock,
  MdInline,
  ChatAttachment,
} from 'pentacle-chat-core';
import { parseMarkdown, parsePentacleQuestionAnswerText, interpretPentacleEvent } from 'pentacle-chat-core';

// Chrome colors are a DESKTOP concern (legacy `chat_ui_state.js#hostChrome`),
// not part of the shared core's host theme. app.js already computes chrome via
// `chatUi.hostChrome(...)` for the legacy path; it passes the same object in
// here so the new path is chromatically identical.
export type ViewChrome = {
  header: string;
  accent: string;
  surface: string;
  border: string;
  title: string;
};

export type TranscriptRenderOptions = {
  showTurnDuration?: boolean;
  streamId?: string;
  resolvedQuestions?: readonly ResolvedQuestionRecord[];
};

export type ResolvedQuestionRecord = {
  notification_id?: string;
  state?: string;
  resolved_at?: string;
  resolution?: { at?: string };
  question?: {
    question_id?: string;
    state?: string;
    answered_at?: string;
    options?: { label?: string; value?: unknown }[];
    answer?: { selections?: unknown[]; text?: string; custom_text?: string; note?: string; value?: unknown; at?: string };
  };
};

// Durable answers can arrive without an event echo (including on reload).
// Interleave by immutable resolution time, then suppress only a matching identity.
export function withResolvedQuestionAnswers(items: readonly PentacleTranscriptItem[], records: readonly ResolvedQuestionRecord[] = []): PentacleTranscriptItem[] {
  const result = [...items];
  const echoed = new Set(items.filter(item => item.eventCase === 'agent-question-answer').map(item => item.notificationId));
  const seen = new Set<string>();
  const time = (record: ResolvedQuestionRecord) => record.resolved_at || record.question?.answered_at || record.resolution?.at || record.question?.answer?.at || '';
  const ordered = [...records].sort((a, b) => (Date.parse(time(a)) || Infinity) - (Date.parse(time(b)) || Infinity) || String(a.notification_id).localeCompare(String(b.notification_id)));
  for (const record of ordered) {
    const id = record.notification_id;
    const question = record.question;
    const answer = question?.answer;
    if (!id || !answer || echoed.has(id) || seen.has(id) || record.state === 'open' || question?.state === 'open') continue;
    seen.add(id);
    const values = answer.selections || (answer.value !== undefined ? [answer.value] : []);
    const selections = values.map(value => question?.options?.find(option => String(option.value) === String(value))?.label || String(value));
    const text = answer.custom_text || answer.text || '';
    if (!text && !selections.length) continue;
    const timestamp = time(record);
    const interpreted = interpretPentacleEvent({
      daemon_seq: Number.NaN, host: '', provider: '', stream_id: '', session_id: '', session_name: '', timestamp,
      kind: 'USER', text: JSON.stringify({ type: 'notification.answer', answer: { notification_id: id, text, selections, note: answer.note } }),
    });
    const row: PentacleTranscriptItem = {
      id: `answer:${id}:${question?.question_id || ''}`, timestamp, timestampLabel: '', label: interpreted.label,
      tone: interpreted.tone, provider: '', source: 'durable-question', text: interpreted.text,
      kind: 'USER', isUser: false, eventCase: interpreted.caseId, displayRule: interpreted.displayRule, notificationId: id,
    };
    const ms = Date.parse(timestamp);
    let at = Number.isFinite(ms) ? result.findIndex(item => Date.parse(item.timestamp || '') > ms) : -1;
    if (!Number.isFinite(ms)) {
      const ask = result.findIndex(item => item.notificationId === id);
      if (ask >= 0) at = ask + 1;
    }
    result.splice(at < 0 ? result.length : at, 0, row);
  }
  return result;
}

const DEFAULT_CHROME: ViewChrome = {
  header: '#101a16',
  accent: '#7ef0ba',
  surface: '#101a16',
  border: '#356150',
  title: 'Agent',
};

// Mirror of chat_ui_state.js#escapeHtml — escape ALL user-controlled text so a
// `<script>`-ish payload can never reach innerHTML un-escaped.
function escapeHtml(str: unknown): string {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function renderCopyButton(text: string, label: string, className = ''): string {
  const classes = ['slot-chat-copy-btn', className].filter(Boolean).join(' ');
  return `<button type="button" class="${classes}" data-copy-text="${escapeHtml(text)}" aria-label="${escapeHtml(label)}">Copy</button>`;
}

// Mirror of chat_ui_state.js#splitActivityText: first line is the title, the
// rest is a clamped detail.
function splitActivityText(text: string): { title: string; detail: string } {
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

function summarizeCommandOutput(output: string): string {
  if (!output) return '';
  const lines = String(output).split('\n').map((line) => line.trimEnd());
  if (lines.length <= 3) return lines.join('\n').trim();
  const visible = lines.slice(0, 3).join('\n').trim();
  const hiddenCount = lines.length - 3;
  return `${visible}\n+${hiddenCount} more line${hiddenCount === 1 ? '' : 's'}`;
}

type AssistantBlock =
  | { type: 'edit'; action: string; title: string; meta: string; body: string }
  | { type: 'command'; command: string; output: string }
  | { type: 'text'; text: string };

// Mirror of chat_ui_state.js#parseAssistantBlocks.
function parseAssistantBlocks(text: string): AssistantBlock[] {
  return String(text || '')
    .split(/\n{2,}/)
    .map((chunk) => chunk.trim())
    .filter(Boolean)
    .map((chunk): AssistantBlock => {
      const lines = chunk.split('\n');
      const first = lines[0] || '';
      const rest = lines.slice(1).join('\n').trim();
      if (/^(Edited|Created|Added|Updated|Deleted) /.test(first)) {
        const action = first.split(' ')[0];
        const match = first.match(/^\w+\s+(.+?)\s+\((.+)\)$/);
        return {
          type: 'edit',
          action,
          title: match?.[1] || first.replace(/^\w+\s+/, ''),
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

// Render a shared-core markdown inline token tree to escaped HTML. Every leaf
// (text / code value / link href) is escapeHtml'd here — the parser emits RAW
// substrings and never HTML, so this is the single escape boundary that keeps a
// `<script>` payload inert. Link hrefs are already safe-scheme filtered by the
// parser; we still escape the attribute value.
function renderMarkdownInlineHtml(nodes: MdInline[]): string {
  return nodes
    .map((node) => {
      switch (node.type) {
        case 'text':
          return escapeHtml(node.value);
        case 'strong':
          return `<strong>${renderMarkdownInlineHtml(node.children)}</strong>`;
        case 'em':
          return `<em>${renderMarkdownInlineHtml(node.children)}</em>`;
        case 'code':
          return `<code class="slot-chat-md-icode">${escapeHtml(node.value)}</code>`;
        case 'link':
          return `<a class="slot-chat-md-link" href="${escapeHtml(node.href)}" target="_blank" rel="noopener noreferrer">${renderMarkdownInlineHtml(node.children)}</a>`;
        case 'break':
          return '<br>';
        default:
          return '';
      }
    })
    .join('');
}

// Render shared-core markdown block tokens to desktop `slot-chat-*` HTML.
// Paragraphs reuse `.slot-chat-line` so prose looks identical to the pre-
// markdown renderer; headings/lists/code/hr get dedicated `.slot-chat-md-*`
// classes styled in renderer/styles.css.
function renderMarkdownBlocksHtml(blocks: MdBlock[]): string {
  return blocks
    .map((block) => {
      switch (block.type) {
        case 'heading':
          return `<div class="slot-chat-md-heading slot-chat-md-h${block.level}">${renderMarkdownInlineHtml(block.children)}</div>`;
        case 'paragraph':
          return `<div class="slot-chat-line">${renderMarkdownInlineHtml(block.children)}</div>`;
        case 'code_block':
          return `<pre class="slot-chat-md-code"${block.lang ? ` data-lang="${escapeHtml(block.lang)}"` : ''}>${renderCopyButton(block.text, 'Copy code block', 'slot-chat-code-copy')}<code>${escapeHtml(block.text)}</code></pre>`;
        case 'list': {
          const tag = block.ordered ? 'ol' : 'ul';
          const items = block.items
            .map((item) => `<li>${renderMarkdownInlineHtml(item)}</li>`)
            .join('');
          return `<${tag} class="slot-chat-md-list">${items}</${tag}>`;
        }
        case 'table': {
          const alignStyle = (idx: number) => {
            const align = block.align[idx];
            return align ? ` style="text-align:${align}"` : '';
          };
          const header = block.header
            .map((cell, idx) => `<th${alignStyle(idx)}>${renderMarkdownInlineHtml(cell)}</th>`)
            .join('');
          const rows = block.rows
            .map((row) => `<tr>${row.map((cell, idx) => `<td${alignStyle(idx)}>${renderMarkdownInlineHtml(cell)}</td>`).join('')}</tr>`)
            .join('');
          return `<div class="slot-chat-md-table-wrap"><table class="slot-chat-md-table"><thead><tr>${header}</tr></thead><tbody>${rows}</tbody></table></div>`;
        }
        case 'hr':
          return '<hr class="slot-chat-md-hr">';
        default:
          return '';
      }
    })
    .join('');
}

// Assistant prose renderer. The chat-specific non-prose lines (suppressed
// `Working (...)` / `Waiting` noise, the `Messages to be submitted` annotation,
// `↳`/`→` pending bodies, `------`/`worked for` timing dividers, bare tool
// labels, and `└├│` tree-char log items) are still handled line-by-line exactly
// as before. The REMAINING prose lines (incl. their blank separators) are
// buffered and rendered through the shared markdown parser, so `## Done`,
// `**bold**`, lists, fenced code, etc. render formatted instead of literally.
function shouldShowTurnDuration(options: TranscriptRenderOptions = {}): boolean {
  return options.showTurnDuration === true;
}

function renderChatBody(text: string, options: TranscriptRenderOptions = {}): string {
  const raw = String(text || '');
  if (/[─│┌┐└┘├┤┬┴┼━┃┏┓┗┛┣┫┳┻╋]/.test(raw) && !/^(Explored|Ran|Viewed Image|Edited|Read|Search|Searched|Updated|Monitor|Waited for background terminal)\n/i.test(raw)) return renderCodeBody(raw);
  const commandMatch = raw.match(/^Ran ([^\n]+)(?:\n|$)/);
  if (commandMatch) {
    return `<div class="slot-chat-line">Ran ${escapeHtml(commandMatch[1])}</div>`;
  }
  const lines = raw.split('\n');
  const rendered: string[] = [];
  let inList = false;
  let prose: string[] = [];

  const closeList = () => {
    if (inList) {
      rendered.push('</div>');
      inList = false;
    }
  };
  // Flush buffered prose lines through the markdown parser. Leading/trailing
  // blank lines are trimmed so a single special line between prose runs does
  // not inject empty paragraphs.
  const flushProse = () => {
    if (!prose.length) return;
    const chunk = prose.join('\n').replace(/^\n+/, '').replace(/\n+$/, '');
    prose = [];
    if (chunk.trim()) rendered.push(renderMarkdownBlocksHtml(parseMarkdown(chunk)));
  };

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) {
      // Blank lines belong to the prose run (the markdown parser uses them to
      // split paragraphs) unless we are mid tree-char log list.
      if (inList) {
        closeList();
      } else {
        prose.push('');
      }
      continue;
    }
    if (/^[•⎿]?\s*Working \([^)]*\).*\/ps to view.*\/stop to close/i.test(trimmed)) continue;
    if (/^[•⎿]?\s*Waiting for background terminal \([^)]*\).*\/ps to view.*\/stop to close/i.test(trimmed)) continue;
    if (/^Messages to be submitted after next tool call/i.test(trimmed)) {
      flushProse();
      closeList();
      rendered.push('<div class="slot-chat-annotation is-pending">Queued for next tool call</div>');
      continue;
    }
    if (/^[↳→]\s*/.test(trimmed)) {
      flushProse();
      closeList();
      rendered.push(`<div class="slot-chat-pending-body">${escapeHtml(trimmed.replace(/^[↳→]\s*/, ''))}</div>`);
      continue;
    }
    if (/^[─━═-]{6,}$/.test(trimmed) || /worked for \d+/i.test(trimmed)) {
      flushProse();
      closeList();
      if (!shouldShowTurnDuration(options)) continue;
      const label = trimmed.replace(/^[─━═-]+\s*/, '').replace(/\s*[─━═-]+$/, '');
      rendered.push(`<div class="slot-chat-annotation is-timing">${escapeHtml(label)}</div>`);
      continue;
    }
    if (/^(Explored|Ran|Viewed Image|Edited|Read|Search|Searched|Updated|Monitor|Waited for background terminal)$/i.test(trimmed)) {
      flushProse();
      closeList();
      rendered.push(`<div class="slot-chat-annotation is-label">${escapeHtml(trimmed)}</div>`);
      continue;
    }
    if (/^[└├│]/.test(trimmed)) {
      flushProse();
      if (!inList) {
        rendered.push('<div class="slot-chat-loglist">');
        inList = true;
      }
      rendered.push(`<div class="slot-chat-logitem">${escapeHtml(trimmed)}</div>`);
      continue;
    }
    // Prose line — buffer it for markdown rendering. Keep the raw (untrimmed)
    // line so fenced code-block indentation survives.
    prose.push(line);
  }

  flushProse();
  closeList();
  return rendered.join('');
}

// B1 (chat_send_turn_lifecycle_batch2): the status line under an optimistic
// user bubble. "sending…" while unconfirmed; a failed/cancelled label on
// terminal non-delivery. Kept tiny + text-only (no fabricated timer per the
// operator's ask). Confirmed rows pass sendState undefined and render nothing.
function renderUserSendStatus(sendState: PentacleSendState | 'sent', optimisticId?: string): string {
  const label = sendState === 'sent' ? 'Sent' : sendState === 'queued'
    ? 'queued'
    : sendState === 'sending'
      ? 'sending…'
      : sendState === 'cancelled'
        ? 'Cancelled'
        : 'Failed to send';
  const retry = (sendState === 'failed' || sendState === 'indeterminate') && optimisticId
    ? `<button type="button" class="slot-chat-send-retry" data-optimistic-id="${escapeHtml(optimisticId)}">Retry</button>`
    : '';
  return `<div class="slot-chat-send-status is-${sendState}">${label}${retry}</div>`;
}

type RenderAttachment = ChatAttachment & {
  uri?: string;
  localUri?: string;
  name?: string;
};

function attachmentImageSrc(attachment: RenderAttachment): string {
  return String(attachment.uri || attachment.localUri || '');
}

function renderAttachmentHtml(attachment: RenderAttachment, index: number): string {
  const src = attachmentImageSrc(attachment);
  const key = String(attachment.key || '');
  const alt = attachment.name || `Image attachment ${index + 1}`;
  const width = Number(attachment.width);
  const height = Number(attachment.height);
  const ratioStyle = Number.isFinite(width) && width > 0 && Number.isFinite(height) && height > 0
    ? ` style="aspect-ratio:${width}/${height}"`
    : '';
  return `<button type="button" class="slot-chat-media-button"${src ? ` data-viewer-src="${escapeHtml(src)}"` : ''}${key ? ` data-attachment-key="${escapeHtml(key)}"` : ''} data-attachment-mime="${escapeHtml(attachment.mime || '')}" aria-label="${escapeHtml(alt)}"${ratioStyle}>
    <img class="slot-chat-media-img"${src ? ` src="${escapeHtml(src)}"` : ' data-needs-blob="1"'} alt="${escapeHtml(alt)}" loading="lazy">
    ${!src && key ? '<span class="slot-chat-media-loading">Loading image</span>' : ''}
  </button>`;
}

function renderAttachmentsHtml(attachments: RenderAttachment[] | undefined): string {
  if (!Array.isArray(attachments) || attachments.length === 0) return '';
  return `<div class="slot-chat-media-grid">${attachments.map(renderAttachmentHtml).join('')}</div>`;
}

function renderCodeBody(text: string): string {
  return `<pre class="slot-chat-md-code">${renderCopyButton(text, 'Copy code block', 'slot-chat-code-copy')}<code>${escapeHtml(text)}</code></pre>`;
}

function renderAnswerBody(text: string): string {
  const answer = parsePentacleQuestionAnswerText(text);
  if (!answer) return escapeHtml(text);
  return answer.items.map(item => `<section class="slot-chat-answer-item"><b>${escapeHtml(item.header)}</b><div>${escapeHtml(item.selectedLabels?.join(', ') || item.text || '')}</div>${item.note ? `<p class="slot-chat-answer-note">${escapeHtml(item.note)}</p>` : ''}</section>`).join('');
}

function disclosureKey(item: PentacleTranscriptItem, options: TranscriptRenderOptions, block = 'body'): string {
  return escapeHtml(JSON.stringify([options.streamId || '', item.id, block]));
}

function renderDisclosure(item: PentacleTranscriptItem, options: TranscriptRenderOptions, label: string): string {
  const disclosure = item.disclosure;
  const preview = disclosure?.previewText || item.text;
  const body = disclosure?.expandedText ?? item.text;
  const heading = `<span class="slot-chat-disclosure-label">${escapeHtml(label)}</span><span class="slot-chat-disclosure-preview">${escapeHtml(preview)}</span>${disclosure?.previewTail ? `<span class="slot-chat-disclosure-tail">${escapeHtml(disclosure.previewTail)}</span>` : ''}`;
  return disclosure?.expandable
    ? `<details class="slot-chat-disclosure" data-disclosure-key="${disclosureKey(item, options)}"><summary>${heading}</summary>${renderCodeBody(body)}</details>`
    : `<div class="slot-chat-disclosure">${heading}${renderCopyButton(body, 'Copy message', 'slot-chat-message-copy')}</div>`;
}

// Keys survive same-stream HTML refreshes; a different stream has a separate namespace.
export function replaceTranscriptHtml(container: HTMLElement, html: string): void {
  const open = new Set([...container.querySelectorAll<HTMLDetailsElement>('details[open][data-disclosure-key]')].map(el => el.dataset.disclosureKey));
  container.innerHTML = html;
  for (const el of container.querySelectorAll<HTMLDetailsElement>('details[data-disclosure-key]')) {
    el.open = open.has(el.dataset.disclosureKey);
  }
}

// Render ONE transcript item to an HTML string, mapping displayRule -> desktop
// DOM. Returns '' for items that must not produce a row (hidden:*, draft:*).
function renderTranscriptItemBodyHtml(
  item: PentacleTranscriptItem,
  chrome: ViewChrome = DEFAULT_CHROME,
  options: TranscriptRenderOptions = {},
): string {
  if (!item) return '';
  const rule = String(item.displayRule || '');

  // hidden:* / draft:composer never render. The shared selector already drops
  // hidden rows before they reach transcriptItems, but guard defensively so a
  // future selector change can't leak a row through this path.
  if (rule.startsWith('hidden:') || rule === 'draft:composer') return '';

  if (item.isUser || rule === 'bubble:user') {
    // B1 (chat_send_turn_lifecycle_batch2): an optimistic (client-origin) row
    // carries a sendState — show a "sending…" affordance while unconfirmed and
    // a failed/cancelled affordance on terminal non-delivery, instead of an
    // immediate "sent" bubble. A confirmed row (sendState undefined) renders as
    // an ordinary bubble, exactly as before.
    const sendState = item.sendState;
    const rowClass = sendState ? ` is-${sendState}` : '';
    const receipt = sendState === 'cancelled' || sendState === 'failed' || sendState === 'indeterminate'
      ? sendState : item.receiptCaption || (item.queuedWhileWorking && (sendState === 'queued' || sendState === 'sending') ? 'queued' : sendState);
    const status = receipt ? renderUserSendStatus(receipt, item.optimisticId) : '';
    const attachments = renderAttachmentsHtml((item as PentacleTranscriptItem & { attachments?: RenderAttachment[] }).attachments);
    return `<article class="slot-chat-row is-user${rowClass}" data-copy-kind="message">${attachments}${item.text.trim() ? `<div class="slot-chat-user-bubble">${renderAnswerBody(item.text)}</div>${renderCopyButton(item.text, 'Copy message', 'slot-chat-message-copy')}` : ''}${status}</article>`;
  }
  if (rule === 'terminal:divider') {
    if (!shouldShowTurnDuration(options)) return '';
    return `<div class="slot-chat-terminal-divider"><span></span><b>${escapeHtml(item.text)}</b><span></span></div>`;
  }
  if (rule === 'system:compacted') {
    return `<div class="slot-chat-compacted"><span>↘</span>${escapeHtml(item.text)}</div>`;
  }
  if (rule === 'bubble:agent' || item.tone === 'agent') {
    return `<article class="slot-chat-row is-agent">${renderDisclosure(item, options, `Subagent${item.label ? ` · ${item.label}` : ''}`)}</article>`;
  }
  if (item.tone === 'tool' && item.disclosure?.mode === 'collapsed-preview') {
    return `<article class="slot-chat-row is-tool">${renderDisclosure(item, options, 'Tool result')}</article>`;
  }
  if (rule === 'activity:code-block') return `<article class="slot-chat-row">${renderCodeBody(item.text)}</article>`;
  if (rule === 'activity:tool-batch') return `<article class="slot-chat-row"><div class="slot-chat-activity"><span class="slot-chat-activity-dot"></span><span>${escapeHtml(item.text)}</span></div></article>`;
  if (rule.startsWith('activity:')) {
    if (rule === 'activity:turn-summary' && !shouldShowTurnDuration(options)) return '';
    const activity = splitActivityText(item.text);
    return `<article class="slot-chat-row"><div class="slot-chat-activity" style="--machine:${escapeHtml(chrome.accent)};--machine-surface:${escapeHtml(chrome.surface)};--machine-border:${escapeHtml(chrome.border)};">
      <span class="slot-chat-activity-dot"></span>
      <div class="slot-chat-activity-body">
        <b>${escapeHtml(activity.title)}</b>
        ${activity.detail ? `<p>${escapeHtml(activity.detail)}</p>` : ''}
      </div>
    </div></article>`;
  }
  // bubble:assistant (and any unmapped fallback) -> assistant card.
  const blocks = parseAssistantBlocks(item.text);
  return `<article class="slot-chat-row" data-copy-kind="message"><div class="slot-chat-assistant-card">
    ${blocks.map((block, index) => {
      if (block.type === 'edit') {
        return `<div class="slot-chat-file-card" style="--machine:${escapeHtml(chrome.accent)};--machine-surface:${escapeHtml(chrome.surface)};--machine-border:${escapeHtml(chrome.border)};"><b>${escapeHtml(block.action)} ${escapeHtml(block.title)}</b>${block.meta ? `<p>${escapeHtml(block.meta)}</p>` : ''}${block.body ? `<details class="slot-chat-file-body" data-disclosure-key="${disclosureKey(item, options, String(index))}"><summary><span>File details</span><pre>${escapeHtml(block.body.split('\n').slice(0, 6).join('\n'))}</pre></summary>${renderCodeBody(block.body)}</details>` : ''}</div>`;
      }
      if (block.type === 'command') {
        return `<div class="slot-chat-command-card" style="--machine:${escapeHtml(chrome.accent)};--machine-surface:${escapeHtml(chrome.surface)};--machine-border:${escapeHtml(chrome.border)};"><b>${escapeHtml(block.command)}</b>${block.output ? `<pre>${escapeHtml(block.output)}</pre>` : ''}</div>`;
      }
      return renderChatBody(block.text, options);
    }).join('')}
  </div>${renderCopyButton(item.text, 'Copy message', 'slot-chat-message-copy')}</article>`;
}

export function renderTranscriptItemHtml(
  item: PentacleTranscriptItem,
  chrome: ViewChrome = DEFAULT_CHROME,
  options: TranscriptRenderOptions = {},
): string {
  const html = renderTranscriptItemBodyHtml(item, chrome, options);
  if (!item || !html) return '';
  return html.replace(/^(<(?:article|div)\b[^>]*)(>)/, (_match, open, close) => `${open} data-transcript-key="${escapeHtml(JSON.stringify([options.streamId || '', item.id]))}"${close}`);
}

// Build the full transcript timeline HTML for a session detail, mirroring
// chat_ui_state.js#renderTranscriptTimeline (per-minute timestamp dividers for
// non-user rows).
export function renderTranscriptTimelineHtml(
  detail: PentacleSessionDetail | null | undefined,
  chrome: ViewChrome = DEFAULT_CHROME,
  options: TranscriptRenderOptions = {},
): string {
  options = { ...options, streamId: detail?.streamId || options.streamId };
  const items = withResolvedQuestionAnswers(detail?.transcriptItems || [], options.resolvedQuestions);
  if (!items.length) return '';
  let lastMinute = '';
  return items
    .map((item) => {
      if (item.isUser) return renderTranscriptItemHtml(item, chrome, options);
      const itemHtml = renderTranscriptItemHtml(item, chrome, options);
      if (!itemHtml) return '';
      const showTimestamp = Boolean(item.timestampLabel && item.timestampLabel !== lastMinute);
      if (item.timestampLabel) lastMinute = item.timestampLabel;
      return `${showTimestamp ? `<div class="slot-chat-timestamp">${escapeHtml(item.timestampLabel)}</div>` : ''}${itemHtml}`;
    })
    .join('');
}

type StoreLike = {
  selectSessionDetail: (
    streamId: string,
    options?: { visibleCount?: number | 'all'; includeDraft?: boolean; [k: string]: unknown },
  ) => PentacleSessionDetail | null;
};

export type RenderStreamTranscriptOptions = {
  chrome?: ViewChrome;
  visibleCount?: number | 'all';
  includeDraft?: boolean;
  showTurnDuration?: boolean;
  /** Override the store (tests inject a controller); defaults to the global. */
  store?: StoreLike;
  /**
   * Optional empty-state HTML used when the stream has no transcript rows. When
   * omitted, the container is simply cleared (app.js owns the surrounding hero +
   * empty-state chrome for the live path).
   */
  emptyHtml?: string;
};

function resolveStore(explicit?: StoreLike): StoreLike | null {
  if (explicit) return explicit;
  const g = (typeof window !== 'undefined' ? (window as unknown as { PentacleChatStore?: StoreLike }) : undefined);
  return g?.PentacleChatStore ?? null;
}

// Render a stream's transcript into `container`, reading the detail from the
// shared store. Returns the rendered `PentacleSessionDetail` (or null when the
// stream is unknown). app.js can use the return to decide hero/empty chrome.
export function renderStreamTranscript(
  streamId: string,
  container: HTMLElement | null | undefined,
  options: RenderStreamTranscriptOptions = {},
): PentacleSessionDetail | null {
  if (!container) return null;
  const store = resolveStore(options.store);
  if (!store) {
    if (options.emptyHtml !== undefined) container.innerHTML = options.emptyHtml;
    return null;
  }
  const detail = store.selectSessionDetail(streamId, {
    visibleCount: options.visibleCount ?? 120,
    includeDraft: options.includeDraft ?? false,
  });
  const chrome = options.chrome || DEFAULT_CHROME;
  const html = renderTranscriptTimelineHtml(detail, chrome, { showTurnDuration: options.showTurnDuration });
  if (html) {
    replaceTranscriptHtml(container, html);
  } else if (options.emptyHtml !== undefined) {
    container.innerHTML = options.emptyHtml;
  } else {
    container.innerHTML = '';
  }
  return detail;
}

// ── Per-slot mount/unmount + version-gated subscription ─────────────
//
// A mount binds a `container` to a `streamId` and re-renders ONLY when THAT
// stream's `eventContentVersionByStream[streamId]` changes (so an unrelated
// stream's frame never re-renders this slot). The mount subscribes to the
// store; unmount unsubscribes. app.js drives `setStream`/`render` from its
// existing per-slot lifecycle.

type SubscribableStore = StoreLike & {
  subscribe: (listener: (state: unknown) => void) => () => void;
  getState: () => {
    eventContentVersionByStream?: Record<string, number>;
    // B1 (chat_send_turn_lifecycle_batch2): optimistic-send status transitions
    // (queued→dispatched→acked / failed / cancelled) mutate optimisticSends but
    // do NOT bump eventContentVersionByStream, so the re-render gate must also
    // factor in this stream's optimistic-send signature or a sendState change
    // would never repaint the mount.
    optimisticSends?: Record<string, { stream_id?: string; status?: string; turn_queued?: boolean }>;
  };
};

export type SlotMount = {
  setStream: (streamId: string | null, chrome?: ViewChrome) => void;
  render: () => PentacleSessionDetail | null;
  unmount: () => void;
};

function resolveSubscribableStore(explicit?: SubscribableStore): SubscribableStore | null {
  if (explicit) return explicit;
  const g = (typeof window !== 'undefined' ? (window as unknown as { PentacleChatStore?: SubscribableStore }) : undefined);
  return g?.PentacleChatStore ?? null;
}

// The per-stream re-render gate key: the event content-version PLUS a signature
// of this stream's optimistic-send statuses. A status-only transition (no new
// event) changes the signature so the mount repaints the row's sendState; an
// unrelated stream's frame never changes this stream's key.
function gateKeyForStream(store: SubscribableStore | null, streamId: string | null): string {
  if (!store || !streamId) return '∅';
  const state = store.getState();
  const version = state.eventContentVersionByStream?.[streamId] ?? 0;
  const sends = state.optimisticSends ?? {};
  const sig = Object.keys(sends)
    .filter((id) => sends[id]?.stream_id === streamId)
    .sort()
    // Include turn_queued: activateQueuedSend flips it true→false while leaving
    // status 'queued' (no content-version bump), so without it a queued→sending
    // transition would not repaint until the later dispatched status change.
    .map((id) => `${id}:${sends[id]?.status ?? ''}:${sends[id]?.turn_queued ? 'q' : ''}`)
    .join(',');
  return `${version}|${sig}`;
}

export function mountSlotTranscript(
  container: HTMLElement,
  initialStreamId: string | null = null,
  options: { chrome?: ViewChrome; store?: SubscribableStore; visibleCount?: number | 'all'; includeDraft?: boolean; emptyHtml?: string; showTurnDuration?: boolean } = {},
): SlotMount {
  const store = resolveSubscribableStore(options.store);
  let streamId = initialStreamId;
  let chrome = options.chrome || DEFAULT_CHROME;
  let lastGate = '';

  const doRender = (): PentacleSessionDetail | null => {
    if (!streamId) {
      if (options.emptyHtml !== undefined) container.innerHTML = options.emptyHtml;
      else container.innerHTML = '';
      lastGate = '';
      return null;
    }
    lastGate = gateKeyForStream(store, streamId);
    return renderStreamTranscript(streamId, container, {
      chrome,
      store: store || undefined,
      visibleCount: options.visibleCount,
      includeDraft: options.includeDraft,
      emptyHtml: options.emptyHtml,
      showTurnDuration: options.showTurnDuration,
    });
  };

  const onState = () => {
    if (!streamId) return;
    const gate = gateKeyForStream(store, streamId);
    // Re-render ONLY when THIS stream's content version advanced OR one of its
    // optimistic sends changed status. Frames for other streams change neither
    // key, so unaffected slots do not re-render.
    if (gate === lastGate) return;
    doRender();
  };

  const unsubscribe = store ? store.subscribe(onState) : () => {};

  // Initial paint.
  doRender();

  return {
    setStream(nextStreamId, nextChrome) {
      streamId = nextStreamId;
      if (nextChrome) chrome = nextChrome;
      lastGate = ''; // force a render on stream switch
      doRender();
    },
    render() {
      // Forced render (e.g. chrome changed, view toggled on) regardless of
      // the gate. Resets the gate watermark to current.
      lastGate = gateKeyForStream(store, streamId);
      return renderStreamTranscript(streamId || '', container, {
        chrome,
        store: store || undefined,
        visibleCount: options.visibleCount,
        includeDraft: options.includeDraft,
        emptyHtml: options.emptyHtml,
        showTurnDuration: options.showTurnDuration,
      });
    },
    unmount() {
      unsubscribe();
    },
  };
}
