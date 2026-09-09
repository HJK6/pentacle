// Shared, platform-agnostic markdown parser for chat assistant prose.
//
// Why this lives in chat-core: the desktop renderer (HTML strings) and the
// mobile renderer (React-Native <Text>) must agree on how assistant markdown
// is interpreted. Putting the *parse* here — and leaving *rendering* to each
// platform — keeps a single source of truth for the grammar while honoring the
// drift-guard rule that chat-core must not import any DOM/RN platform API.
//
// Contract (IMPORTANT — escape-first safety):
//   `parseMarkdown` returns a token tree whose leaf text/value/href fields are
//   the RAW source substrings. It NEVER emits HTML. The platform renderer is
//   responsible for safety:
//     - desktop wraps every leaf string in escapeHtml() before innerHTML, so a
//       `<script>` payload can never be injected;
//     - mobile passes leaf strings to <Text>, which is inert by construction.
//   `href` on links is pre-filtered here to a safe-scheme allow-list; an unsafe
//   URL degrades to plain text (the link wrapper is dropped), so no renderer
//   ever receives a `javascript:` href.
//
// Scope (deliberately a small subset — NOT full CommonMark; see the hardening
// spec's Non-Goals). Supported:
//   block:  ATX headings (#..######), fenced code (```lang), unordered lists
//           (-,*,+), ordered lists (1. / 1)), thematic break (---,***,___),
//           GFM-style pipe tables, paragraphs (soft line breaks preserved as
//           hard breaks).
//   inline: **strong** / __strong__, *em* / _em_, `code`, [text](href).
// Intentionally unsupported (rendered literally): blockquotes, nested lists,
// reference links, images, setext headings, HTML passthrough.
//
// Reversibility: this is the `parseMarkdown` boundary. A future swap to a full
// CommonMark library would replace this file and keep the MdBlock/MdInline
// shape (or adapt the two renderers), without touching call sites elsewhere.

export type MdInline =
  | { type: 'text'; value: string }
  | { type: 'strong'; children: MdInline[] }
  | { type: 'em'; children: MdInline[] }
  | { type: 'code'; value: string }
  | { type: 'link'; href: string; children: MdInline[] }
  | { type: 'break' };

export type MdBlock =
  | { type: 'heading'; level: 1 | 2 | 3 | 4 | 5 | 6; children: MdInline[] }
  | { type: 'paragraph'; children: MdInline[] }
  | { type: 'code_block'; text: string; lang: string | null }
  | { type: 'list'; ordered: boolean; items: MdInline[][] }
  | {
      type: 'table';
      align: ('left' | 'center' | 'right' | null)[];
      header: MdInline[][];
      rows: MdInline[][][];
    }
  | { type: 'hr' };

const HEADING_RE = /^(#{1,6})\s+(.*)$/;
const FENCE_RE = /^```(.*)$/;
const HR_RE = /^(?:-{3,}|\*{3,}|_{3,})$/;
const UL_RE = /^[-*+]\s+(.*)$/;
const OL_RE = /^\d+[.)]\s+(.*)$/;
const TABLE_DELIMITER_RE = /^\s*:?-{1,}:?\s*$/;

// Only these URL schemes (plus scheme-relative/relative/anchor) are allowed on
// a link href; anything else (notably `javascript:`/`data:`) degrades to text.
const SAFE_HREF_RE = /^(?:https?:\/\/|mailto:|\/|#|\.\.?\/|[^:]*$)/i;

function isListLine(trimmed: string): boolean {
  return UL_RE.test(trimmed) || OL_RE.test(trimmed);
}

function isOrderedLine(trimmed: string): boolean {
  return OL_RE.test(trimmed);
}

function isBlockStart(trimmed: string): boolean {
  return (
    FENCE_RE.test(trimmed) ||
    HEADING_RE.test(trimmed) ||
    HR_RE.test(trimmed) ||
    isListLine(trimmed)
  );
}

function safeHref(raw: string): string | null {
  const href = raw.trim();
  if (!href) return null;
  // Reject any whitespace (newline/tab/space) — this also defeats
  // "java\nscript:"-style scheme obfuscation. Hyphens and other URL-legal
  // characters stay allowed; scheme legality is gated by SAFE_HREF_RE.
  if (/\s/.test(href)) return null;
  return SAFE_HREF_RE.test(href) ? href : null;
}

function splitTableRow(line: string): string[] | null {
  const trimmed = line.trim();
  if (!trimmed.includes('|')) return null;
  let body = trimmed;
  if (body.startsWith('|')) body = body.slice(1);
  if (body.endsWith('|')) body = body.slice(0, -1);
  const cells = body.split('|').map((cell) => cell.trim());
  return cells.length > 0 ? cells : null;
}

function tableAlign(delimiter: string): 'left' | 'center' | 'right' | null {
  const trimmed = delimiter.trim();
  const left = trimmed.startsWith(':');
  const right = trimmed.endsWith(':');
  if (left && right) return 'center';
  if (left) return 'left';
  if (right) return 'right';
  return null;
}

function parseTableAt(lines: string[], index: number): { block: Extract<MdBlock, { type: 'table' }>; next: number } | null {
  if (index + 1 >= lines.length) return null;
  const headerCells = splitTableRow(lines[index]);
  const delimiterCells = splitTableRow(lines[index + 1]);
  if (!headerCells || !delimiterCells) return null;
  if (headerCells.length !== delimiterCells.length) return null;
  if (!delimiterCells.every((cell) => TABLE_DELIMITER_RE.test(cell))) return null;

  const align = delimiterCells.map(tableAlign);
  const rows: MdInline[][][] = [];
  let next = index + 2;
  while (next < lines.length) {
    const bodyCells = splitTableRow(lines[next]);
    if (!bodyCells || bodyCells.length !== headerCells.length) break;
    rows.push(bodyCells.map(parseInline));
    next++;
  }

  return {
    block: {
      type: 'table',
      align,
      header: headerCells.map(parseInline),
      rows,
    },
    next,
  };
}

// Parse inline markup within a single logical run of text. Single left-to-right
// pass; at each index we try the constructs in fixed precedence (code span →
// link → strong → em) so e.g. `*` inside a code span is never read as emphasis.
// Closing-delimiter lookups use indexOf (no backtracking regex) so input length
// bounds the work — no catastrophic-backtracking surface.
export function parseInline(src: string): MdInline[] {
  const out: MdInline[] = [];
  let textStart = 0;
  let i = 0;

  const flushText = (end: number): void => {
    if (end > textStart) out.push({ type: 'text', value: src.slice(textStart, end) });
  };

  while (i < src.length) {
    const c = src[i];

    // 1) inline code span: `...` (no nested backticks).
    if (c === '`') {
      const close = src.indexOf('`', i + 1);
      if (close > i + 1) {
        flushText(i);
        out.push({ type: 'code', value: src.slice(i + 1, close) });
        i = close + 1;
        textStart = i;
        continue;
      }
    }

    // 2) link: [text](href)
    if (c === '[') {
      const closeText = src.indexOf(']', i + 1);
      if (closeText > i && src[closeText + 1] === '(') {
        const closeHref = src.indexOf(')', closeText + 2);
        if (closeHref > closeText + 1) {
          const linkText = src.slice(i + 1, closeText);
          const href = safeHref(src.slice(closeText + 2, closeHref));
          flushText(i);
          if (href) {
            out.push({ type: 'link', href, children: parseInline(linkText) });
          } else {
            // Unsafe/empty href — keep the visible text, drop the link.
            out.push(...parseInline(linkText));
          }
          i = closeHref + 1;
          textStart = i;
          continue;
        }
      }
    }

    // 3) strong / em with `*` (intra-word allowed) or `_` (boundary-only, so
    //    snake_case identifiers are not mangled).
    if (c === '*' || c === '_') {
      const isUnderscore = c === '_';
      const prev = i > 0 ? src[i - 1] : '';
      const boundaryOk = !isUnderscore || !/\w/.test(prev);
      if (boundaryOk) {
        const isDouble = src[i + 1] === c;
        const marker = isDouble ? c + c : c;
        const start = i + marker.length;
        const close = src.indexOf(marker, start);
        if (close > start) {
          const after = src[close + marker.length] ?? '';
          const afterOk = !isUnderscore || !/\w/.test(after);
          const inner = src.slice(start, close);
          // Require non-empty, non-whitespace-only content.
          if (afterOk && inner.trim().length > 0) {
            flushText(i);
            const children = parseInline(inner);
            out.push(isDouble ? { type: 'strong', children } : { type: 'em', children });
            i = close + marker.length;
            textStart = i;
            continue;
          }
        }
      }
    }

    i++;
  }

  flushText(src.length);
  return out;
}

// Parse a prose chunk (possibly multi-line) into block tokens. The caller is
// expected to have already stripped the chat-specific non-prose lines (tool
// labels, tree-char logs, "Working (...)" noise, dividers) — markdown applies
// only to the remaining prose.
export function parseMarkdown(input: string): MdBlock[] {
  const lines = String(input ?? '').replace(/\r\n?/g, '\n').split('\n');
  const blocks: MdBlock[] = [];
  let i = 0;

  while (i < lines.length) {
    const trimmed = lines[i].trim();

    if (!trimmed) {
      i++;
      continue;
    }

    // Fenced code block.
    const fence = trimmed.match(FENCE_RE);
    if (fence) {
      const lang = fence[1].trim() || null;
      const buf: string[] = [];
      i++;
      while (i < lines.length && lines[i].trim() !== '```') {
        buf.push(lines[i]);
        i++;
      }
      i++; // consume the closing fence (or run past EOF if unterminated)
      blocks.push({ type: 'code_block', text: buf.join('\n'), lang });
      continue;
    }

    // ATX heading.
    const heading = trimmed.match(HEADING_RE);
    if (heading) {
      blocks.push({
        type: 'heading',
        level: heading[1].length as 1 | 2 | 3 | 4 | 5 | 6,
        children: parseInline(heading[2].trim()),
      });
      i++;
      continue;
    }

    // Thematic break (checked before lists so `***`/`---` aren't read as items).
    if (HR_RE.test(trimmed)) {
      blocks.push({ type: 'hr' });
      i++;
      continue;
    }

    // List (group consecutive items of the same ordered/unordered kind; one
    // level only — indentation/nesting is intentionally flattened).
    if (isListLine(trimmed)) {
      const ordered = isOrderedLine(trimmed);
      const items: MdInline[][] = [];
      while (i < lines.length) {
        const t = lines[i].trim();
        if (!isListLine(t) || isOrderedLine(t) !== ordered) break;
        const content = t.replace(ordered ? OL_RE : UL_RE, '$1');
        items.push(parseInline(content));
        i++;
      }
      blocks.push({ type: 'list', ordered, items });
      continue;
    }

    // GFM-style pipe table. Recognition is intentionally conservative: a
    // header row must be followed immediately by a same-width delimiter row.
    const table = parseTableAt(lines, i);
    if (table) {
      blocks.push(table.block);
      i = table.next;
      continue;
    }

    // Paragraph: gather consecutive non-blank, non-block-start lines. Soft line
    // breaks are preserved as explicit hard breaks (chat content is
    // line-oriented; collapsing to spaces would lose intent).
    const para: string[] = [];
    while (i < lines.length) {
      const t = lines[i].trim();
      if (!t || isBlockStart(t) || parseTableAt(lines, i)) break;
      para.push(t);
      i++;
    }
    const children: MdInline[] = [];
    para.forEach((line, idx) => {
      if (idx > 0) children.push({ type: 'break' });
      children.push(...parseInline(line));
    });
    blocks.push({ type: 'paragraph', children });
  }

  return blocks;
}
