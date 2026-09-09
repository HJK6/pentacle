import type { PentacleEvent } from '../types/pentacle';

export type PentacleEventCase =
  | 'user-message'
  | 'assistant-message'
  | 'assistant-progress'
  | 'tool-command'
  | 'tool-output'
  | 'tool-use'
  | 'tool-result'
  | 'tool-batch'
  | 'collapsed-tool'
  | 'code-block'
  | 'explore-action'
  | 'edit-action'
  | 'write-action'
  | 'thinking'
  | 'system'
  | 'system-notice'
  | 'terminal-divider'
  | 'turn-summary'
  | 'context-compacted'
  | 'draft'
  | 'working-status'
  | 'queued-draft'
  | 'transient-noise'
  | 'codex-helper-suggestion'
  | 'peer-agent-message'
  | 'agent-question-ask'
  | 'agent-question-answer'
  | 'unknown';

export type PentacleDisplayRule =
  | 'bubble:user'
  | 'bubble:agent'
  | 'bubble:assistant'
  | 'activity:progress'
  | 'activity:command'
  | 'activity:tool-output'
  | 'activity:tool-batch'
  | 'activity:collapsed-tool'
  | 'activity:code-block'
  | 'activity:explored'
  | 'activity:file-change'
  | 'activity:thinking'
  | 'activity:system'
  | 'activity:question'
  | 'activity:turn-summary'
  | 'terminal:divider'
  | 'system:compacted'
  | 'draft:composer'
  | 'hidden:status'
  | 'hidden:noise'
  | 'hidden:helper';

export type PentacleEventTone = 'user' | 'agent' | 'assistant' | 'tool' | 'thinking' | 'system';

export type PentacleDisclosurePresentation = {
  mode: 'collapsed-preview';
  previewText: string;
  previewTail: string;
  expandable: boolean;
  expandedText: string;
};

export type PentacleInterpretedEvent = {
  event: PentacleEvent;
  caseId: PentacleEventCase;
  displayRule: PentacleDisplayRule;
  tone: PentacleEventTone;
  label: string;
  text: string;
  hidden: boolean;
  reason: string;
  disclosure?: PentacleDisclosurePresentation;
  /**
   * Set for `agent-question-answer` rows so a client can join this transcript row to the durable
   * notification it echoes and render only one representation of the same answer.
   */
  notificationId?: string;
};

export type PentacleInterpretOptions = {
  revealScrollbackFallback?: boolean;
};

export function normalizedEventText(text: string) {
  return String(text || '').replace(/\s+/g, ' ').trim();
}

const PLUMBING_PREVIEW_CHAR_LIMIT = 72;
const COLLAPSED_DISCLOSURE_CASES = new Set<PentacleEventCase>([
  'peer-agent-message',
  'tool-command',
  'tool-output',
  'tool-use',
  'tool-result',
  'collapsed-tool',
  'explore-action',
  'edit-action',
  'write-action',
]);

export function collapsedDisclosurePresentation(text: string, expandedText = text): PentacleDisclosurePresentation {
  const lines = String(text || '').split(/\r?\n/);
  const firstLine = (lines[0] || '').trim();
  const rawExpandedText = String(expandedText || '');
  const expandedLines = rawExpandedText.split(/\r?\n/);
  const hiddenLineCount = Math.max(0, expandedLines.length - 1);
  const longLine = firstLine.length > PLUMBING_PREVIEW_CHAR_LIMIT;
  const previewText = longLine
    ? `${firstLine.slice(0, PLUMBING_PREVIEW_CHAR_LIMIT - 1).trimEnd()}…`
    : firstLine;
  return {
    mode: 'collapsed-preview',
    previewText,
    previewTail: hiddenLineCount > 0
      ? `… +${hiddenLineCount} line${hiddenLineCount === 1 ? '' : 's'}`
      : longLine ? '… more' : '',
    expandable: hiddenLineCount > 0 || longLine || rawExpandedText !== text,
    expandedText: rawExpandedText,
  };
}

const TERMINAL_DIVIDER_LOOKBACK_LIMIT = 3;

type DividerCoalesceItem = {
  displayRule: PentacleDisplayRule;
  text: string;
};

export function stripClaudeExpandHint(text: string) {
  return String(text || '')
    .split('\n')
    .map((line) => line
      .replace(/\s*\(ctrl\+o to expand\)\.?\s*$/i, '')
      .replace(/\s*\(↓ to manage\)\.?\s*$/i, '')
      .trimEnd())
    .join('\n')
    .trim();
}

export function isClaudeStatusText(text: string) {
  const normalized = normalizedEventText(text).replace(/^⏺\s*/, '').trim();
  return /^[✢✽✶✻✳·]\s+/.test(normalized) && (
    /…|\.\.\./.test(normalized) ||
    /\s+\([^)]*\d/.test(normalized) ||
    /\bfor\s+\d/.test(normalized)
  );
}

function hasClaudePaneMarker(text: string) {
  return /(^|\n)\s*(⏺|⎿|[✢✽✶✻✳·]\s)/.test(String(text || ''));
}

function isClaudeToolOutputText(text: string) {
  const lines = String(text || '').split('\n');
  const firstLine = lines.find((line) => line.trim());
  const hasAssistantMarker = lines.some((line) => /^⏺\s*/.test(line.trim()));
  if (hasAssistantMarker) return false;
  return Boolean(firstLine && /^⎿\s*/.test(firstLine.trim()));
}

export function cleanClaudePaneText(text: string) {
  const lines: string[] = [];
  for (const rawLine of String(text || '')
    .replace(/\u00a0/g, ' ')
    .split('\n')
    .map((line) => line.trim())
    .map((line) => stripClaudeExpandHint(line.replace(/^⏺\s*/, '').replace(/^⎿\s*/, '').trim()))) {
    if (!rawLine) {
      if (lines.length && lines[lines.length - 1] !== '') lines.push('');
      continue;
    }
    if (isClaudeStatusText(rawLine)) continue;
    if (/^\(?No output\)?$/i.test(rawLine)) continue;
    if (/^Running…(?:\s*\([^)]*\))?$/i.test(rawLine)) continue;
    if (/^\(ctrl\+b\b/i.test(rawLine)) continue;
    if (/^Shell cwd was reset to\b/i.test(rawLine)) continue;
    lines.push(rawLine);
  }
  while (lines[lines.length - 1] === '') lines.pop();
  return lines.join('\n').trim();
}

export function displayTextForEvent(text: string) {
  const cleaned = cleanClaudePaneText(text);
  if (cleaned) return cleaned;
  if (isClaudeStatusText(text) || hasClaudePaneMarker(text) || /^Shell cwd was reset to\b/i.test(normalizedEventText(text))) {
    return '';
  }
  return String(text || '').trim();
}

export function normalizeWorkingLabel(text: string) {
  const raw = normalizedEventText(text).replace(/^⏺\s*/, '').trim();
  if (/^(Working|Waiting for background terminal)\b/i.test(raw)) {
    return raw;
  }
  const hasSpinner = /^[✢✽✶✻✳·]\s+/.test(raw);
  if (!hasSpinner) return raw;
  const withoutSpinner = raw.replace(/^[✢✽✶✻✳·]\s+/, '').trim();
  const activeMatch = withoutSpinner.match(/^(.+?)(?:…|\.\.\.)?\s*\(([^)]*)\)$/);
  if (activeMatch) {
    const action = activeMatch[1].trim();
    const elapsed = activeMatch[2].split('·')[0].trim();
    return [action, elapsed].filter(Boolean).join(' ');
  }
  const completedMatch = withoutSpinner.match(/^(.+?)\s+for\s+(.+)$/);
  if (completedMatch) {
    return `${completedMatch[1].trim()} ${completedMatch[2].trim()}`;
  }
  return withoutSpinner;
}

export function collapseCodeBlocks(text: string) {
  return text.replace(/```[\s\S]*?```/g, '[code hidden]');
}

const CODEX_HELPER_EXACT = new Set([
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

const CODEX_HELPER_VERB_PREFIX = /^(summarize|explore|inspect|review|find|run|use|write|implement|improve|explain)\b.+/;

function matchesCodexHelperLine(line: string) {
  if (!line) return false;
  if (CODEX_HELPER_EXACT.has(line)) return true;
  return CODEX_HELPER_VERB_PREFIX.test(line) && line.split(/\s+/).length <= 8;
}

export function isCodexHelperSuggestion(text: string) {
  if (!text) return false;
  // The pane parser sometimes accumulates adjacent splash lines onto a single
  // USER event (the prompt line + the rest of the suggestion list). Match
  // against the first non-empty line so we still catch the suggestion even
  // when extra noise was appended afterward.
  const firstLine = String(text)
    .split('\n')
    .map((line) => normalizedEventText(line).toLowerCase())
    .find((line) => line.length > 0);
  return matchesCodexHelperLine(firstLine || '');
}

export type PeerAgentMessage = {
  fromStreamId: string;
  anchorId?: string;
  body: string;
};

// Agent-orch / inter-agent delivery envelope, e.g.
//   "[from host_c:claude-host_c-1b7d6cb6] [tell:abc]\nbody"
//   "[from host_b:codex-...] [handoff:x]\nbody"            (any anchor word)
//   "[from <daemon-liveness>] [tell:...]\nbody"          (non-stream-id markers)
//   "[from host_c:claude-...]\nbody"                     (no anchor)
// The trailing line break is the discriminator that keeps human text safe: a real
// agent-orch delivery prepends "[from <id>]" and puts the body on the next line,
// whereas ordinary prose like "[from 3:30pm] meeting" has no newline and stays a
// user bubble. The only broadening over the original is the anchor word: it was
// hard-coded to tell|send, so other anchors (handoff/reply/inbox/…) leaked
// through and rendered as the operator's own (right-aligned) bubble — the
// "messages sent to subagents show as from me" bug. The from-target stays
// permissive (\S+?) so non-stream-id markers like <daemon-liveness> still match.
// The trailing `(?:[^\S\r\n]+enqueued_at=\S*)?` accepts the one metadata stamp the daemon appends
// after the anchor, on the same line as the header: ` enqueued_at=<iso>`, which `_stamped_peer_text`
// (daemon transport) adds for QUEUED and redelivered tells but not
// for directly delivered ones. Without it, every queued tell failed to parse and fell through to
// the raw `bubble:user` branch — which is how raw notification.answer JSON reached the transcript
// (public-notification-answer-raw-json-bubble).
//
// Deliberately pinned to the literal `enqueued_at` key AND the daemon's ISO-8601 UTC grammar
// rather than a generic `key=value` run: this pattern changes attribution and visibility, so every
// character of slack here can swallow a real human message. `enqueued_at=tomorrow` or a bare
// `enqueued_at=` is a human writing prose, not the
// daemon — the daemon always emits `datetime.now(timezone.utc).isoformat()` with `+00:00` replaced
// by `Z` (daemon transport :145). If the daemon adds another stamp, widen this to
// that specific key and grammar, and add a fixture for it.
// Exactly the daemon's lexical form, not a family of things that resemble it:
//   * ONE ASCII space — `_stamped_peer_text` interpolates `f" enqueued_at={enqueued_at}"`
//     (daemon transport). Tabs and runs of spaces are human typing.
//   * A semantically valid UTC timestamp — month 01-12, day 01-31, hour 00-23, min/sec 00-59.
//     `2026-13-45T99:99:99Z` has the right shape but no daemon ever emitted it.
//   * Microseconds either absent or exactly six digits — Python's `datetime.isoformat()` emits
//     six, and omits the fraction entirely when microsecond == 0 (daemon transport).
//
// KNOWN, ACCEPTED COLLISION: a human who types the exact wire form — `[from <token>]` optionally
// followed by a `[word:...]` anchor and a byte-exact UTC stamp, then a newline — is still read as
// peer traffic and hidden. That is unavoidable without provenance on the event itself (the wire
// format is the only signal we get), and it is the pre-existing behaviour of this pattern for the
// unstamped `[from X]\n…` case too. Narrowing the grammar shrinks the collision surface to
// effectively zero prose; eliminating it needs a daemon-set marker, not a better regex.
const ENQUEUED_AT_STAMP = String.raw`enqueued_at=\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{6})?Z`;
const PEER_STAMP_ENQUEUED_AT = String.raw` ${ENQUEUED_AT_STAMP}`;
const PEER_AGENT_MESSAGE = new RegExp(
  // Separators are single ASCII spaces, exactly as the daemon emits them
  // (`f"[from {from_stream_id}]{anchor}{queued}\n{text}"`, daemon transport where
  // `anchor` itself begins with one space). The previous `\s+` also accepted tabs, NBSP, runs of
  // spaces and even a newline after `[from` — none of which the daemon produces, all of which are
  // a human typing. That slack only became reachable once stamped headers parsed, so narrowing it
  // is part of this change rather than incidental cleanup.
  String.raw`^\[from (\S+?)\](?: \[([a-z_]+):([^\]\r\n]+)\])?(?:${PEER_STAMP_ENQUEUED_AT})?(?:\r\n|\n|\r)([\s\S]*)$`,
);

export function parsePeerAgentMessage(text: string): PeerAgentMessage | null {
  const match = PEER_AGENT_MESSAGE.exec(String(text || ''));
  if (!match) return null;
  return {
    fromStreamId: match[1],
    ...(match[3] ? { anchorId: `${match[2]}:${match[3]}` } : {}),
    body: match[4],
  };
}

export function isPeerAgentMessage(text: string) {
  return parsePeerAgentMessage(text) !== null;
}

type AgentQuestionAskDisplay = {
  title: string;
  questionId: string;
  notificationId?: string;
};

type AgentQuestionAnswerDisplay = {
  answer?: string;
  note?: string;
  notificationId?: string;
};

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function stringValue(value: unknown) {
  return typeof value === 'string' ? value.trim() : '';
}

function parseJsonObject(text: string): Record<string, unknown> | null {
  const trimmed = String(text || '').trim();
  if (!trimmed) return null;
  try {
    return objectValue(JSON.parse(trimmed));
  } catch {
    return null;
  }
}

function stripTellPrefix(text: string) {
  return String(text || '')
    .replace(/^\s*\[tell:[^\]\n]+\]\s*/, '')
    .trim();
}

function parseNotificationAnswerDisplay(text: string): AgentQuestionAnswerDisplay | null {
  const peer = parsePeerAgentMessage(text);
  const body = stripTellPrefix(peer?.body ?? text);
  const payload = parseJsonObject(body);
  if (payload?.type !== 'notification.answer') return null;
  const answer = objectValue(payload.answer);
  if (!answer) return null;
  const freeText = stringValue(answer.text);
  const label = stringValue(answer.label);
  const selections = Array.isArray(answer.selections)
    ? answer.selections.map(stringValue).filter(Boolean)
    : [];
  const value = objectValue(answer.value);
  const valueAnswer = stringValue(value?.answer);
  const fallbackValue = stringValue(answer.value);
  const actionKind = stringValue(answer.action_kind);
  const labelAnswer = label && label.toLowerCase() !== actionKind.toLowerCase() ? label : '';
  // A bare yes/no carries its meaning only in `choice` (daemon transport), with no free
  // text, selections or value to fall back on.
  const choiceAnswer = typeof answer.choice === 'boolean' ? (answer.choice ? 'Yes' : 'No') : '';
  const displayAnswer = freeText ||
    labelAnswer ||
    selections.join(', ') ||
    valueAnswer ||
    fallbackValue ||
    choiceAnswer;
  const note = stringValue(answer.note);
  // Accept the id from either position: the daemon nests it inside `answer`
  // (daemon transport) but the envelope has also been observed with it at the top level.
  const notificationId = stringValue(answer.notification_id) || stringValue(payload.notification_id);
  // Deliberately NOT `return null` when nothing is displayable. This parser is what keeps a
  // recognized protocol envelope out of the generic `bubble:user` branch, so bailing here is
  // what surfaced raw JSON (or, via the peer branch, silently dropped the operator's answer).
  // An envelope we recognize always renders as an answer row, even if sparse.
  return {
    ...(displayAnswer ? { answer: displayAnswer } : {}),
    ...(note ? { note } : {}),
    ...(notificationId ? { notificationId } : {}),
  };
}

function containsNotificationAnswerPayload(text: string) {
  return String(text || '').split('\n').some((line) => parseJsonObject(line)?.type === 'notification.answer');
}

function parsePromptAskOkDisplay(text: string): AgentQuestionAskDisplay | null {
  let payload = parseJsonObject(text);
  if (!payload) {
    // A durable ask can succeed while CLI delivery diagnostics surround its JSON
    // result. Accept one complete, validated result line; never extract brace
    // fragments or choose between multiple successful asks in one tool result.
    const results = String(text || '').split('\n').map(parseJsonObject).filter((row) => {
      const question = objectValue(row?.question);
      return row?.type === 'prompt.ask.ok' && row.ok === true &&
        Boolean(stringValue(question?.question_id)) &&
        Boolean(stringValue(question?.notification_id));
    });
    if (results.length === 1) payload = results[0];
  }
  if (payload?.type !== 'prompt.ask.ok') return null;
  const question = objectValue(payload.question);
  const notification = objectValue(payload.notification);
  const envelope = objectValue(question?.envelope);
  const title = stringValue(notification?.title) ||
    stringValue(envelope?.title) ||
    stringValue(question?.title) ||
    'Question';
  const questionId = stringValue(question?.question_id) ||
    stringValue(envelope?.question_id) ||
    stringValue(payload.question_id);
  // The notification_id joins this ask row to its resolved answer projection so
  // the answer keeps the question's original chronological position. Read it from
  // wherever the daemon carries it, preferring the durable notification envelope.
  const notificationId = stringValue(notification?.notification_id) ||
    stringValue(question?.notification_id) ||
    stringValue(envelope?.notification_id) ||
    stringValue(payload.notification_id);
  return { title, questionId, ...(notificationId ? { notificationId } : {}) };
}

function formatPromptAskOk(display: AgentQuestionAskDisplay) {
  const suffix = display.questionId ? ` (${display.questionId})` : '';
  return `Asked: ${display.title}${suffix}`;
}

function formatNotificationAnswer(display: AgentQuestionAnswerDisplay) {
  const note = display.note ? `\nNote: ${display.note}` : '';
  if (!display.answer) {
    // A sparse envelope carries neither an answer nor a note. "Operator answered with note:"
    // with nothing after it reads as truncated output, so state only what is known.
    return note ? `Operator answered with note:${note}` : 'Operator answered.';
  }
  return `Operator answered: ${display.answer}${note}`;
}

export function isWorkingStatusText(text: string) {
  const normalized = normalizedEventText(text);
  return (
    isClaudeStatusText(normalized) ||
    /^Working\s*\(/.test(normalized) ||
    /^•\s*Working\s*\(/.test(normalized) ||
    /^Running…(?:\s*\([^)]*\))?$/i.test(normalized.replace(/^⎿\s*/, '').trim()) ||
    /^Booting MCP server\b/i.test(normalized) ||
    /\(\d+s\s*•\s*esc to interrupt\)$/i.test(normalized) ||
    /◦\s*Working\s*\(/.test(normalized)
  );
}

export function isTerminalDividerText(text: string) {
  const normalized = normalizedEventText(text);
  return (
    /^[-─━═]*\s*Worked for\s+\d+(?:h|m|s)(?:\s+\d+(?:m|s))?\s*[-─━═]*$/i.test(normalized) ||
    /^[-─━═]{8,}$/.test(normalized)
  );
}

export function terminalDividerLabel(text: string) {
  return normalizedEventText(text)
    .replace(/^[-─━═]+\s*/, '')
    .replace(/\s*[-─━═]+$/, '')
    .trim();
}

export function isContextCompactedText(text: string) {
  return /^Context Compacted\b/i.test(normalizedEventText(text));
}

export function stripTerminalPromptPrefix(text: string) {
  return String(text || '').replace(/^\s*[❯›]\s?/, '');
}

export function isDaemonNotificationText(text: string) {
  const normalized = String(text || '').trim();
  return /^\[child_report_ready\](?:\s|$)/i.test(normalized) ||
    /^Agent\s+"[^"]+"\s+finished\s*·/i.test(normalized);
}

export function isOrchestrationReceiptAck(text: string) {
  const normalized = String(text || '').trim();
  const type = normalized.match(/"type"\s*:\s*"([^"]+)"/i)?.[1] || '';
  if (type === 'prompt.ask.ok') return false;
  return /\.ok$/i.test(type) ||
    (/"delivery_status"\s*:/i.test(normalized) && !/"(?:error|reason)"\s*:\s*"[^"\s]/i.test(normalized));
}

export function isTerminalToolEchoText(text: string) {
  const lines = String(text || '').split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  return /^(?:Ran|Explored|Edited|Read|Searched|Called|Viewed)\b/.test(lines[0] || '') &&
    lines.some((line) => /^└\s*/.test(line));
}

export function isTerminalFurnitureText(text: string, kind = '') {
  const raw = String(text || '').replace(/\u00a0/g, ' ').trim();
  if (!raw) return true;
  const normalizedKind = String(kind || '').toUpperCase();
  const lines = raw.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const firstLine = lines[0] || '';

  if (/^[❯›]\s*$/.test(firstLine) && lines.length === 1) return true;
  if (/^[⏺•●◦]\s*$/.test(firstLine) && lines.length === 1) return true;
  if (normalizedKind === 'USER' || normalizedKind === 'ASSIST_TEXT') return false;
  if (isTerminalDividerText(raw)) return true;
  if (/^How is Claude doing this session\?\s*(?:\(optional\))?(?:\n[0-3][.:]\s*(?:Bad|Fine|Good|Dismiss)\s*)*$/i.test(raw)) return true;
  if (/^(?:[✢✽✶✻✳·]\s*)?(?:[\d,.]+\s+)?(?:weighted\s+)?tokens?\s+(?:left|remaining|used)\s*$/i.test(firstLine)) return true;
  if (/^(?:[✢✽✶✻✳·]\s*)?\d{1,3}%\s+context\s+(?:left|used)\s*$/i.test(firstLine)) return true;
  if (/^(?:⏵⏵\s*)?(?:shift\+tab|tab)\s+to\s+(?:cycle|toggle)\s+(?:permission|mode)(?:\s+mode)?\s*$/i.test(firstLine)) return true;
  if (/^(?:⏵⏵\s*)?(?:permission mode\s*:\s*(?:default|accept edits|plan|bypass permissions)|(?:bypass permissions(?: on)?|accept edits|plan mode)(?:\s*\([^)]*\))?)(?:\s*[·•]\s*(?:esc to interrupt|← for agents?|shift\+tab to cycle))*\s*$/i.test(firstLine)) return true;
  if (/^(?:esc to interrupt|ctrl\+[a-z] to (?:exit|toggle|open|show|expand)|\? (?:for|to) (?:shortcuts|help))\s*$/i.test(firstLine)) return true;
  if (/^[✢✽✶✻✳·]\s+.{1,80}(?:…|\.\.\.|\bfor\s+\d+(?:h|m|s)(?:\s+\d+(?:m|s))?)(?:\s+\([^\n)]*\))?\s*$/i.test(firstLine)) return true;
  if (normalizedKind === 'SYSTEM' && isWorkingStatusText(raw)) return true;
  return false;
}

export function isTransientTranscriptNoise(text: string) {
  const normalized = normalizedEventText(text);
  if (!normalized) return true;
  if (!displayTextForEvent(text)) return true;
  if (isWorkingStatusText(normalized)) return true;
  return false;
}

export function stripWorkingStatus(text: string) {
  return String(text || '')
    .split('\n')
    .filter((line) => !isWorkingStatusText(line) && !isClaudeStatusText(line))
    .join('\n')
    .replace(/\s*◦\s*Working\s*\([^)]*\).*$/gm, '')
    .trim();
}

export function interpretPentacleEvent(
  event: PentacleEvent,
  assistantLabel = 'Agent',
  options: PentacleInterpretOptions = {},
): PentacleInterpretedEvent {
  const kind = String(event.kind || '').toUpperCase();
  const text = String(event.text || '');
  const source = String(event.raw?.source || '');
  const transport = String(event.raw?.transport || '');
  const userText = kind === 'USER' ? stripTerminalPromptPrefix(text) : text;

  if (source === 'scrollback_fallback' && !options.revealScrollbackFallback) {
    return interpreted(event, 'transient-noise', 'hidden:noise', 'assistant', assistantLabel, text, true, 'Tagged scrollback fallback is hidden unless explicitly revealed.');
  }

  if (kind === 'TELL') {
    // Typed peer delivery (raw.sender/raw.peer_payload from the daemon envelope).
    // Same visibility rules as the USER-prefix peer branch below: a synthetic peer
    // tell/send is visible; only daemon housekeeping and malformed protocol
    // payloads are suppressed. (Always-hiding typed TELL hid these on mobile.)
    const sender = String(event.raw?.sender || '').trim();
    const rawBody = String(event.raw?.peer_payload || text);
    const body = displayTextForEvent(rawBody).trim();
    const hiddenProtocolPayload = containsNotificationAnswerPayload(body);
    const daemonNotification = /^<?daemon(?:$|:|-|>)/i.test(sender) || isDaemonNotificationText(body);
    return interpreted(
      event,
      'peer-agent-message',
      'bubble:agent',
      'agent',
      peerAgentLabel(sender),
      body,
      hiddenProtocolPayload || daemonNotification,
      hiddenProtocolPayload
        ? 'Malformed notification-answer transport stays suppressed instead of exposing raw protocol JSON.'
        : daemonNotification
          ? 'Daemon liveness, status-sweep, and child completion notifications belong outside conversation.'
          : 'Typed agent-orch peer/subagent delivery rendered as a compact agent-side transcript row.',
      rawBody,
    );
  }

  const notificationAnswer = kind === 'USER' ? parseNotificationAnswerDisplay(userText) : null;
  if (notificationAnswer) {
    const answerRow = interpreted(event, 'agent-question-answer', 'activity:question', 'system', 'Question', formatNotificationAnswer(notificationAnswer), false, 'Agent-orch durable question answer rendered as a compact human transcript row.');
    return notificationAnswer.notificationId
      ? { ...answerRow, notificationId: notificationAnswer.notificationId }
      : answerRow;
  }

  const peerAgentMessage = kind === 'USER' ? parsePeerAgentMessage(userText) : null;
  if (peerAgentMessage) {
    const label = peerAgentLabel(peerAgentMessage.fromStreamId);
    const body = displayTextForEvent(peerAgentMessage.body).trim();
    // Peer deliveries are operationally relevant transcript context. Keep their
    // distinct tone/display rule so clients can render a compact agent card.
    const hiddenProtocolPayload = containsNotificationAnswerPayload(body);
    const daemonNotification = /^<?daemon(?:$|:|-|>)/i.test(peerAgentMessage.fromStreamId) ||
      isDaemonNotificationText(body);
    return interpreted(
      event,
      'peer-agent-message',
      'bubble:agent',
      'agent',
      label,
      body,
      hiddenProtocolPayload || daemonNotification,
      hiddenProtocolPayload
        ? 'Malformed notification-answer transport stays suppressed instead of exposing raw protocol JSON.'
        : daemonNotification
          ? 'Daemon liveness, status-sweep, and child completion notifications belong outside conversation.'
        : 'Agent-orch peer/subagent delivery rendered as a compact agent-side transcript row.',
      peerAgentMessage.body,
    );
  }

  if (kind === 'USER' && isCodexHelperSuggestion(userText)) {
    return interpreted(event, 'codex-helper-suggestion', 'hidden:helper', 'user', '', userText, true, 'Codex starter/helper prompts are not durable user conversation.');
  }

  const promptAsk = kind === 'TOOL_RESULT' ? parsePromptAskOkDisplay(text) : null;
  if (promptAsk) {
    const askRow = interpreted(event, 'agent-question-ask', 'activity:question', 'system', 'Question', formatPromptAskOk(promptAsk), false, 'Agent-orch prompt.ask.ok tool result rendered as a compact human transcript row.');
    return promptAsk.notificationId
      ? { ...askRow, notificationId: promptAsk.notificationId }
      : askRow;
  }

  if (
    source === 'structured'
    || source === 'claude-jsonl'
    || transport === 'claude-jsonl'
    || transport === 'codex-rollout'
    || (source === 'scrollback_fallback' && options.revealScrollbackFallback)
  ) {
    return interpretStructuredEvent(event, assistantLabel, kind, text);
  }
  const displayText = displayTextForEvent(userText);
  const collapsedText = collapseCodeBlocks(stripWorkingStatus(displayText) || displayText).trim();
  const normalized = normalizedEventText(displayText);

  if (kind === 'DRAFT') {
    const pending = Boolean(event.raw?.pending);
    return interpreted(event, pending ? 'queued-draft' : 'draft', 'draft:composer', 'assistant', 'Draft', text, true, 'Draft state is rendered outside committed transcript rows.');
  }

  if (isTerminalFurnitureText(text, kind) && !(kind === 'USER' && event.attachments?.length)) {
    return interpreted(event, 'transient-noise', 'hidden:noise', 'system', '', text, true, 'Terminal UI furniture is not durable conversation.');
  }

  if (isDaemonNotificationText(displayText) || isOrchestrationReceiptAck(displayText)) {
    return interpreted(event, 'transient-noise', 'hidden:noise', 'system', '', displayText, true, 'Orchestration receipts and daemon notifications belong outside conversation.');
  }

  if (isTerminalToolEchoText(displayText)) {
    return interpreted(event, 'tool-output', 'activity:tool-output', 'tool', 'Tool', displayText, false, 'Terminal tool activity renders as a collapsed tool preview.', text);
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

  if (kind === 'ASSIST' && isClaudeToolOutputText(text)) {
    return interpreted(event, 'tool-output', 'activity:tool-output', 'assistant', assistantLabel, collapsedText, false, 'Claude terminal output.', text);
  }

  if (kind === 'USER') {
    return interpreted(event, 'user-message', 'bubble:user', 'user', '', collapsedText, false, 'Durable user message.');
  }

  if (kind === 'THINK' || kind === 'THINKING') {
    return interpreted(event, 'thinking', 'activity:thinking', 'thinking', 'Thinking', collapsedText, false, 'Model thinking/tool-planning activity.');
  }

  if (kind === 'SYSTEM') {
    return interpreted(event, 'system', 'activity:system', 'system', 'System', collapsedText, false, 'System or runtime message.');
  }

  if (kind === 'TOOL') {
    return interpreted(event, classifyToolText(normalized), displayRuleForToolText(normalized), 'tool', 'Tool', collapsedText, false, 'Tool invocation or file action.');
  }

  if (kind === 'TOOL-OUT') {
    return interpreted(event, 'tool-output', 'activity:tool-output', 'tool', 'Tool', collapsedText, false, 'Tool output/result.', text);
  }

  if (kind === 'ASSIST') {
    const provider = String(event.provider || '').toLowerCase();
    const assistCase = classifyAssistantText(normalized, { provider });
    return interpreted(event, assistCase, displayRuleForAssistantCase(assistCase), 'assistant', assistantLabel, collapsedText, false, 'Assistant transcript content.');
  }

  return interpreted(event, 'unknown', 'bubble:assistant', 'assistant', kind || assistantLabel, collapsedText, false, 'Unknown event kind falls back to assistant rendering.');
}

function peerAgentLabel(fromStreamId: string) {
  const parts = String(fromStreamId || '').split(':').filter(Boolean);
  return parts.length >= 2 ? parts.slice(1).join(':') : fromStreamId;
}

function interpretStructuredEvent(
  event: PentacleEvent,
  assistantLabel: string,
  kind: string,
  text: string,
): PentacleInterpretedEvent {
  const raw = event.raw || {};
  const trimmed = stripClaudeExpandHint(String(text || '').trim());
  const toolName = String(raw.tool_name || '').trim();

  if (isDaemonNotificationText(trimmed)) {
    return interpreted(event, 'transient-noise', 'hidden:noise', 'system', '', trimmed, true, 'Daemon notifications belong outside conversation.');
  }

  if (trimmed && kind !== 'TOOL_RESULT' && isTerminalFurnitureText(trimmed, kind)) {
    return interpreted(event, 'transient-noise', 'hidden:noise', 'system', '', trimmed, true, 'Structured terminal UI furniture is not durable conversation.');
  }

  if (kind === 'WORKING') {
    return interpreted(event, 'working-status', 'hidden:status', 'system', 'Working', trimmed, true, 'Structured working state is rendered by the status dock.');
  }

  if (kind === 'TOOL_BATCH_SUMMARY') {
    return interpreted(event, 'tool-batch', 'activity:tool-batch', 'tool', 'Tool', trimmed, false, 'Collapsed Claude tool span summary.');
  }

  if (kind === 'USER') {
    return interpreted(event, 'user-message', 'bubble:user', 'user', '', trimmed, false, 'Durable user message.');
  }

  if (kind === 'ASSIST_TEXT') {
    return interpreted(event, 'assistant-message', 'bubble:assistant', 'assistant', assistantLabel, trimmed, false, 'Structured assistant text.');
  }

  if (kind === 'THINKING') {
    return interpreted(event, 'thinking', 'activity:thinking', 'thinking', 'Thinking', trimmed || 'Thinking', false, 'Structured thinking block.');
  }

  if (kind === 'TOOL_USE') {
    if (toolName === 'Agent') {
      const childCount = Number(raw.child_count || 0);
      const childLabel = childCount > 0 ? `\n${childCount} child ${childCount === 1 ? 'event' : 'events'}` : '';
      return interpreted(event, 'collapsed-tool', 'activity:collapsed-tool', 'tool', 'Tool', `${trimmed}${childLabel}`, false, 'Collapsed Claude Agent tool row.');
    }
    if (toolName === 'Grep' || toolName === 'Glob') {
      return interpreted(event, 'tool-use', displayRuleForToolText(`${toolName} ${trimmed}`.trim()), 'tool', 'Tool', synthesizeSearchToolText(toolName, trimmed, raw), false, 'Structured Claude search tool invocation.');
    }
    return interpreted(event, 'tool-use', displayRuleForToolText(`${toolName} ${trimmed}`.trim()), 'tool', 'Tool', trimmed, false, 'Structured Claude tool invocation.');
  }

  if (kind === 'TOOL_RESULT') {
    if (isOrchestrationReceiptAck(trimmed)) {
      return interpreted(event, 'transient-noise', 'hidden:noise', 'tool', 'Tool', trimmed, true, 'Successful orchestration receipts have no conversation value.');
    }
    const resultText = summarizeToolResult(event, toolName, trimmed);
    const isFileTool = toolName === 'Read' || toolName === 'Write' || toolName === 'Edit';
    const isError = Boolean(raw.is_error);
    if (toolName === 'Read' && !isError) {
      return interpreted(event, 'code-block', 'activity:code-block', 'tool', 'Tool', resultText, false, 'Structured Claude Read result is hidden by the renderer.');
    }
    if (isFileTool) {
      return interpreted(event, 'tool-result', 'activity:tool-output', 'tool', 'Tool', resultText, false, 'Structured Claude file tool summary/result.', text);
    }
    return interpreted(event, 'tool-result', 'activity:tool-output', 'tool', 'Tool', resultText, false, 'Structured Claude tool result.', text);
  }

  if (kind === 'SYSTEM') {
    const subtype = String(raw.subtype || '');
    if (subtype === 'turn-summary') {
      return interpreted(event, 'turn-summary', 'activity:turn-summary', 'system', 'Summary', trimmed, false, 'Structured Claude turn summary divider.');
    }
    if (subtype === 'synthetic-user') {
      // Task notifications, system reminders, piped command output from the
      // Claude Code session — render as a low-key system row, not a user
      // bubble. Slash-command caveat is pure boilerplate and gets suppressed;
      // local-command stdout/stderr carry real output (e.g. a slash command
      // that prints "bye!") so render the inner text. Everything else falls
      // back to the compact tag-name notice.
      if (/^<local-command-caveat>/i.test(trimmed)) {
        return interpreted(event, 'transient-noise', 'hidden:noise', 'system', '', trimmed, true, 'Slash-command caveat preamble is Claude Code boilerplate and is suppressed.');
      }
      const cmdOutput = trimmed.match(/^<local-command-(stdout|stderr)>([\s\S]*?)<\/local-command-\1>\s*$/i);
      if (cmdOutput) {
        const stream = cmdOutput[1].toLowerCase();
        const inner = cmdOutput[2].trim();
        const label = stream === 'stderr' ? 'Error' : 'Output';
        return interpreted(event, 'system-notice', 'activity:system', 'system', label, inner, false, 'Slash-command output rendered as a system row instead of the wrapper tag name.');
      }
      const firstLine = trimmed.split('\n')[0] || 'System notice';
      const compact = firstLine.replace(/^<([a-z-]+)>.*$/i, '$1').replace(/[<>]/g, '').trim();
      return interpreted(event, 'system-notice', 'activity:system', 'system', 'Notice', compact || firstLine, false, 'Synthetic system envelope (task notification, reminder, etc).');
    }
    return interpreted(event, 'system', 'activity:system', 'system', 'System', trimmed, false, 'Structured Claude system message.');
  }

  return interpreted(event, 'unknown', 'bubble:assistant', 'assistant', kind || assistantLabel, trimmed, false, 'Unknown structured Claude event kind falls back to assistant rendering.');
}

function summarizeToolResult(event: PentacleEvent, toolName: string, text: string) {
  const raw = event.raw || {};
  const trimmed = stripClaudeExpandHint(String(text || '').trim());

  if (toolName === 'Bash' && /^Command running in background with ID:/i.test(trimmed)) {
    return 'Running in the background';
  }

  if (/<tool_use_error>File has not been read yet\./i.test(trimmed)) {
    return 'File must be read first';
  }

  const input = raw.tool_input && typeof raw.tool_input === 'object'
    ? raw.tool_input as Record<string, unknown>
    : {};
  const isError = Boolean(raw.is_error);

  if (!isError && toolName === 'Write') {
    const content = typeof input.content === 'string' ? input.content : undefined;
    const filePath = typeof input.file_path === 'string' ? input.file_path : '';
    if (content !== undefined && filePath) {
      const lineCount = content.split('\n').length;
      return `Wrote ${lineCount} ${pluralizeLine(lineCount)} to ${formatToolPath(filePath, raw.cwd)}`;
    }
    return firstNonEmptyLine(trimmed);
  }

  if (!isError && toolName === 'Edit') {
    const oldString = typeof input.old_string === 'string' ? input.old_string : undefined;
    const newString = typeof input.new_string === 'string' ? input.new_string : undefined;
    if (oldString !== undefined && newString !== undefined) {
      const removed = countInputLines(oldString);
      const added = countInputLines(newString);
      if (added === removed) return 'Updated file';
      const parts = [
        added > 0 ? `Added ${added} ${pluralizeLine(added)}` : '',
        removed > 0 ? `${added > 0 ? 'removed' : 'Removed'} ${removed} ${pluralizeLine(removed)}` : '',
      ].filter(Boolean);
      return parts.length ? parts.join(', ').replace(', Removed', ', removed') : 'Updated file';
    }
    return firstNonEmptyLine(trimmed);
  }

  return trimmed;
}

function pluralizeLine(count: number) {
  return count === 1 ? 'line' : 'lines';
}

function countInputLines(text: string) {
  return text.split('\n').length;
}

function firstNonEmptyLine(text: string) {
  return String(text || '').split('\n').map((line) => line.trim()).find(Boolean) || '';
}

function formatToolPath(filePath: string, cwd: unknown) {
  const cleanPath = filePath.trim();
  const base = typeof cwd === 'string' ? cwd.trim() : '';
  if (!cleanPath || !base || !cleanPath.startsWith('/') || !base.startsWith('/')) return cleanPath;
  return relativePosixPath(base, cleanPath);
}

function relativePosixPath(from: string, to: string) {
  const fromParts = from.split('/').filter(Boolean);
  const toParts = to.split('/').filter(Boolean);
  let index = 0;
  while (index < fromParts.length && index < toParts.length && fromParts[index] === toParts[index]) {
    index += 1;
  }
  const up = fromParts.slice(index).map(() => '..');
  const down = toParts.slice(index);
  const relative = [...up, ...down].join('/');
  return relative || '.';
}

function synthesizeSearchToolText(toolName: string, fallbackText: string, raw: Record<string, unknown>) {
  const input = raw.tool_input && typeof raw.tool_input === 'object'
    ? raw.tool_input as Record<string, unknown>
    : {};
  const pattern = stringValue(input.pattern || input.glob || input.query);
  const path = stringValue(input.path || input.directory || input.cwd);
  const include = stringValue(input.include);
  const parts = [
    pattern ? `pattern: ${JSON.stringify(pattern)}` : '',
    path ? `path: ${JSON.stringify(path)}` : '',
    include ? `include: ${JSON.stringify(include)}` : '',
  ].filter(Boolean);
  const body = parts.length
    ? parts.join(' · ')
    : fallbackText.replace(new RegExp(`^${toolName}\\s*:?\\s*`, 'i'), '').trim();
  return `${toolName}: ${body || fallbackText || 'search'}`;
}

export function coalesceInterpretedEvents(events: PentacleInterpretedEvent[]) {
  const result: PentacleInterpretedEvent[] = [];
  const retainedReplayIdentities = new Set<string>();

  const rememberReplayIdentities = (event: PentacleEvent) => {
    for (const identity of replayIdentityTokens(event)) retainedReplayIdentities.add(identity);
  };

  for (const item of events) {
    const existingDividerIndex = findCoalescibleTerminalDividerIndex(result, item);
    if (existingDividerIndex !== -1) {
      // Codex can emit divider rows separated by blank summary/divider artifacts; keep only the newest turn boundary.
      result.splice(existingDividerIndex, result.length - existingDividerIndex, item);
      rememberReplayIdentities(item.event);
      continue;
    }

    const text = normalizedEventText(item.text);
    const identities = replayIdentityTokens(item.event);
    const mayReplay = identities.some((identity) => retainedReplayIdentities.has(identity));
    const startIndex = mayReplay ? 0 : Math.max(0, result.length - 4);
    let existingIndex = -1;
    for (let index = startIndex; index < result.length; index += 1) {
      const existing = result[index];
      if (!existing) continue;
      if (existing.event.kind !== item.event.kind) continue;
      if (existing.displayRule !== item.displayRule) continue;
      const existingText = normalizedEventText(existing.text);
      const distance = result.length - index;
      const exactText = existingText === text;
      if (
        (mayReplay && exactText && canCoalesceExactText(existing, item)) ||
        (!exactText && isProgressiveUpdate(existingText, text) && distance <= 4)
      ) {
        existingIndex = index;
        break;
      }
    }

    if (existingIndex === -1) {
      result.push(item);
      for (const identity of identities) retainedReplayIdentities.add(identity);
      continue;
    }

    const existing = result[existingIndex];
    if (
      text.length > normalizedEventText(existing.text).length &&
      result.length - existingIndex <= 4
    ) {
      result[existingIndex] = item;
      for (const identity of identities) retainedReplayIdentities.add(identity);
    }
  }

  return result;
}

function replayIdentityTokens(event: PentacleEvent): string[] {
  const tokens = new Set<string>();
  for (const value of [
    event.optimistic_id,
    event.jsonl_record_uuid,
    event.raw?.jsonl_record_uuid,
    event.raw?.uuid,
    event.jsonl_resolution_for_record_uuid,
    event.raw?.jsonl_resolution_for_record_uuid,
  ]) {
    const normalized = String(value || '').trim();
    if (normalized) tokens.add(`text:${normalized}`);
  }
  for (const value of [event.daemon_seq, event.correlatedDaemonSeq]) {
    const normalized = Number(value);
    if (Number.isFinite(normalized)) tokens.add(`seq:${normalized}`);
  }
  return [...tokens];
}

function canCoalesceExactText(
  existing: PentacleInterpretedEvent,
  item: PentacleInterpretedEvent,
) {
  return hasSharedReplayIdentity(existing.event, item.event);
}

function hasSharedReplayIdentity(left: PentacleEvent, right: PentacleEvent) {
  if (sameNonEmpty(left.optimistic_id, right.optimistic_id)) return true;
  if (sameNonEmpty(left.jsonl_record_uuid, right.jsonl_record_uuid)) return true;
  if (sameNonEmpty(left.raw?.jsonl_record_uuid, right.raw?.jsonl_record_uuid)) return true;
  if (sameNonEmpty(left.jsonl_record_uuid, right.raw?.jsonl_record_uuid)) return true;
  if (sameNonEmpty(left.raw?.jsonl_record_uuid, right.jsonl_record_uuid)) return true;
  if (sameNonEmpty(left.raw?.uuid, right.raw?.uuid)) return true;
  if (sameNonEmpty(left.jsonl_resolution_for_record_uuid, right.jsonl_record_uuid)) return true;
  if (sameNonEmpty(left.jsonl_record_uuid, right.jsonl_resolution_for_record_uuid)) return true;
  if (sameNonEmpty(left.raw?.jsonl_resolution_for_record_uuid, right.raw?.jsonl_record_uuid)) return true;
  if (sameNonEmpty(left.raw?.jsonl_record_uuid, right.raw?.jsonl_resolution_for_record_uuid)) return true;
  if (sameNonEmpty(left.raw?.jsonl_resolution_for_record_uuid, right.jsonl_record_uuid)) return true;
  if (sameNonEmpty(left.jsonl_record_uuid, right.raw?.jsonl_resolution_for_record_uuid)) return true;
  const leftSeq = Number(left.daemon_seq);
  const rightSeq = Number(right.daemon_seq);
  if (Number.isFinite(leftSeq) && Number.isFinite(rightSeq) && leftSeq === rightSeq) return true;
  const leftCorrelated = Number(left.correlatedDaemonSeq);
  const rightCorrelated = Number(right.correlatedDaemonSeq);
  if (Number.isFinite(leftCorrelated) && Number.isFinite(rightSeq) && leftCorrelated === rightSeq) return true;
  if (Number.isFinite(rightCorrelated) && Number.isFinite(leftSeq) && rightCorrelated === leftSeq) return true;
  return false;
}

function sameNonEmpty(left: unknown, right: unknown) {
  const leftValue = String(left || '').trim();
  return Boolean(leftValue) && leftValue === String(right || '').trim();
}

export function findCoalescibleTerminalDividerIndex(existingItems: DividerCoalesceItem[], candidate: DividerCoalesceItem) {
  if (candidate.displayRule !== 'terminal:divider') return -1;
  const startIndex = Math.max(0, existingItems.length - TERMINAL_DIVIDER_LOOKBACK_LIMIT);
  for (let index = existingItems.length - 1; index >= startIndex; index -= 1) {
    const existing = existingItems[index];
    if (!existing) continue;
    if (existing.displayRule === 'terminal:divider') {
      return index;
    }
    if (!isEmptyDividerCoalesceSpacer(existing)) break;
  }
  return -1;
}

function isEmptyDividerCoalesceSpacer(item: DividerCoalesceItem) {
  return (
    (item.displayRule === 'activity:turn-summary' || item.displayRule === 'terminal:divider') &&
    normalizedEventText(item.text) === ''
  );
}

function interpreted(
  event: PentacleEvent,
  caseId: PentacleEventCase,
  displayRule: PentacleDisplayRule,
  tone: PentacleEventTone,
  label: string,
  text: string,
  hidden: boolean,
  reason: string,
  disclosureText?: string,
): PentacleInterpretedEvent {
  const disclosure = !hidden && COLLAPSED_DISCLOSURE_CASES.has(caseId)
    ? collapsedDisclosurePresentation(text, disclosureText ?? text)
    : undefined;
  return { event, caseId, displayRule, tone, label, text, hidden, reason, ...(disclosure ? { disclosure } : {}) };
}

function classifyToolText(text: string): PentacleEventCase {
  if (/^(Read|Glob|Grep|Search|List)\b/i.test(text)) return 'explore-action';
  if (/^Edit(ed)?\b/i.test(text)) return 'edit-action';
  if (/^Write\b/i.test(text)) return 'write-action';
  if (/^(Bash|Ran)\b/i.test(text)) return 'tool-command';
  return 'tool-command';
}

function displayRuleForToolText(text: string): PentacleDisplayRule {
  const toolCase = classifyToolText(text);
  if (toolCase === 'explore-action') return 'activity:explored';
  if (toolCase === 'edit-action' || toolCase === 'write-action') return 'activity:file-change';
  return 'activity:command';
}

function classifyAssistantText(
  text: string,
  options: { provider?: string } = {},
): PentacleEventCase {
  if (/^(Read|Edit|Write|Bash|Grep|Glob|Agent|TodoWrite)\(/i.test(text)) return 'tool-command';
  if (/^Waited for background terminal\b/i.test(text)) return 'tool-command';
  if (/^Explored\b/i.test(text)) return 'explore-action';
  if (/^(Edited|Updated)\b/i.test(text)) return 'edit-action';
  if (/^(Wrote|Added|Created)\b/i.test(text)) return 'write-action';
  if (/^Ran\b/i.test(text)) return 'tool-command';
  const provider = options.provider || '';
  const allowProgressHeuristic = provider !== 'codex';
  if (
    allowProgressHeuristic &&
    /^(I('|’)m|I am|I’ll|I will|Checking|Inspecting|Reading|Looking)\b/i.test(text) &&
    text.length < 220
  ) {
    return 'assistant-progress';
  }
  return 'assistant-message';
}

function displayRuleForAssistantCase(caseId: PentacleEventCase): PentacleDisplayRule {
  if (caseId === 'explore-action') return 'activity:explored';
  if (caseId === 'edit-action' || caseId === 'write-action') return 'activity:file-change';
  if (caseId === 'tool-command') return 'activity:command';
  if (caseId === 'assistant-progress') return 'activity:progress';
  return 'bubble:assistant';
}

function isProgressiveUpdate(previousText: string, nextText: string) {
  if (previousText.length < 40 || nextText.length < 40) return false;
  return previousText.startsWith(nextText) || nextText.startsWith(previousText);
}
