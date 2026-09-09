// Unit tests for the shared chat-core markdown parser (`parseMarkdown` /
// `parseInline`). Bundled by scripts/run-tests.js (esbuild) so the
// bare `pentacle-chat-core` specifier + the package .ts sources resolve, then
// run under `node --test`. The parser is platform-agnostic: these assert the
// TOKEN tree only (HTML/RN rendering + escaping are tested per-platform).

import test from 'node:test';
import assert from 'node:assert/strict';

import { parseMarkdown, parseInline, type MdInline, type MdBlock } from 'pentacle-chat-core';

// Compact textual shape of an inline tree for readable assertions.
function inlineShape(nodes: MdInline[]): string {
  return nodes
    .map((n) => {
      switch (n.type) {
        case 'text':
          return JSON.stringify(n.value);
        case 'strong':
          return `B(${inlineShape(n.children)})`;
        case 'em':
          return `I(${inlineShape(n.children)})`;
        case 'code':
          return `C(${JSON.stringify(n.value)})`;
        case 'link':
          return `L[${n.href}](${inlineShape(n.children)})`;
        case 'break':
          return 'BR';
      }
    })
    .join('+');
}

test('headings: ## Done -> heading level 2', () => {
  const blocks = parseMarkdown('## Done');
  assert.equal(blocks.length, 1);
  const h = blocks[0];
  assert.equal(h.type, 'heading');
  assert.equal((h as Extract<MdBlock, { type: 'heading' }>).level, 2);
  assert.equal(inlineShape((h as Extract<MdBlock, { type: 'heading' }>).children), '"Done"');
});

test('headings: levels 1..6, and no-space "#x" is NOT a heading', () => {
  for (let lvl = 1; lvl <= 6; lvl++) {
    const b = parseMarkdown(`${'#'.repeat(lvl)} Title`)[0] as Extract<MdBlock, { type: 'heading' }>;
    assert.equal(b.type, 'heading');
    assert.equal(b.level, lvl);
  }
  const notHeading = parseMarkdown('#nospace')[0];
  assert.equal(notHeading.type, 'paragraph');
});

test('bold: **x** and __x__ -> strong', () => {
  assert.equal(inlineShape(parseInline('**bold**')), 'B("bold")');
  assert.equal(inlineShape(parseInline('__bold__')), 'B("bold")');
  assert.equal(inlineShape(parseInline('a **b** c')), '"a "+B("b")+" c"');
});

test('italic: *x* and _x_ -> em', () => {
  assert.equal(inlineShape(parseInline('*it*')), 'I("it")');
  assert.equal(inlineShape(parseInline('_it_')), 'I("it")');
});

test('underscore emphasis is boundary-only: snake_case is plain text', () => {
  assert.equal(inlineShape(parseInline('foo_bar_baz')), '"foo_bar_baz"');
  assert.equal(inlineShape(parseInline('do _this_ now')), '"do "+I("this")+" now"');
});

test('inline code: `x` -> code; markup inside code is inert', () => {
  assert.equal(inlineShape(parseInline('`code`')), 'C("code")');
  assert.equal(inlineShape(parseInline('`a*b*c`')), 'C("a*b*c")');
  assert.equal(inlineShape(parseInline('`<script>`')), 'C("<script>")');
});

test('nested emphasis: **bold _and italic_**', () => {
  assert.equal(inlineShape(parseInline('**bold _and italic_**')), 'B("bold "+I("and italic"))');
});

test('adjacent emphasis: **a** **b**', () => {
  assert.equal(inlineShape(parseInline('**a** **b**')), 'B("a")+" "+B("b")');
});

test('links: safe href -> link; text parsed inline', () => {
  assert.equal(inlineShape(parseInline('[docs](https://x.io)')), 'L[https://x.io]("docs")');
  assert.equal(inlineShape(parseInline('[**bold**](https://x.io)')), 'L[https://x.io](B("bold"))');
  assert.equal(inlineShape(parseInline('see [home](/path) ok')), '"see "+L[/path]("home")+" ok"');
});

test('links: unsafe scheme emits NO link token (degrades to text)', () => {
  // The security property: a javascript:/data: URL must never reach a renderer
  // as an href. The visible link text is preserved; we assert there is no
  // 'link' node rather than exact text (simple paren-matching can leave a
  // harmless ")" remnant on pathological nested-paren hrefs).
  const noLink = (s: string) =>
    assert.equal(parseInline(s).some((n) => n.type === 'link'), false, `link leaked for: ${s}`);
  noLink('[click](javascript:alert(1))');
  noLink('[x](data:text/html;base64,AAAA)');
  noLink('[y](vbscript:msgbox)');
  // sanity: the visible text survives
  assert.ok(inlineShape(parseInline('[click](javascript:alert)')).includes('"click"'));
});

test('escape-first: raw HTML is a text leaf, never markup', () => {
  const nodes = parseInline('<script>alert(1)</script>');
  assert.equal(nodes.length, 1);
  assert.equal(nodes[0].type, 'text');
  assert.equal((nodes[0] as Extract<MdInline, { type: 'text' }>).value, '<script>alert(1)</script>');
});

test('fenced code block keeps raw text + language, no inline parsing', () => {
  const blocks = parseMarkdown('```js\nconst x = **1**;\n```');
  assert.equal(blocks.length, 1);
  const cb = blocks[0] as Extract<MdBlock, { type: 'code_block' }>;
  assert.equal(cb.type, 'code_block');
  assert.equal(cb.lang, 'js');
  assert.equal(cb.text, 'const x = **1**;');
});

test('unordered + ordered lists group consecutive items', () => {
  const ul = parseMarkdown('- one\n- two\n- three')[0] as Extract<MdBlock, { type: 'list' }>;
  assert.equal(ul.type, 'list');
  assert.equal(ul.ordered, false);
  assert.equal(ul.items.length, 3);
  assert.equal(inlineShape(ul.items[0]), '"one"');

  const ol = parseMarkdown('1. a\n2. b')[0] as Extract<MdBlock, { type: 'list' }>;
  assert.equal(ol.type, 'list');
  assert.equal(ol.ordered, true);
  assert.equal(ol.items.length, 2);
});

test('thematic break --- / *** / ___ -> hr (not a list)', () => {
  for (const hr of ['---', '***', '___']) {
    assert.equal(parseMarkdown(hr)[0].type, 'hr');
  }
});

test('paragraph: soft line breaks preserved as hard breaks', () => {
  const p = parseMarkdown('line one\nline two')[0] as Extract<MdBlock, { type: 'paragraph' }>;
  assert.equal(p.type, 'paragraph');
  assert.equal(inlineShape(p.children), '"line one"+BR+"line two"');
});

test('tables: valid GFM pipe table emits table token with alignment and inline cells', () => {
  const blocks = parseMarkdown([
    '| Name | Score | Note |',
    '| :--- | ---: | :---: |',
    '| **Ada** | 42 | `ok` |',
    '| Lin | 7 | [docs](https://example.com) |',
  ].join('\n'));
  assert.equal(blocks.length, 1);
  const table = blocks[0] as Extract<MdBlock, { type: 'table' }>;
  assert.equal(table.type, 'table');
  assert.deepEqual(table.align, ['left', 'right', 'center']);
  assert.deepEqual(table.header.map(inlineShape), ['"Name"', '"Score"', '"Note"']);
  assert.equal(table.rows.length, 2);
  assert.deepEqual(table.rows[0].map(inlineShape), ['B("Ada")', '"42"', 'C("ok")']);
  assert.deepEqual(table.rows[1].map(inlineShape), ['"Lin"', '"7"', 'L[https://example.com]("docs")']);
});

test('tables: delimiter without colons emits null alignment', () => {
  const table = parseMarkdown('A | B\n--- | ---')[0] as Extract<MdBlock, { type: 'table' }>;
  assert.equal(table.type, 'table');
  assert.deepEqual(table.align, [null, null]);
  assert.deepEqual(table.header.map(inlineShape), ['"A"', '"B"']);
  assert.equal(table.rows.length, 0);
});

test('tables: stray pipe without table delimiter remains one unchanged paragraph', () => {
  const input = 'BidsList public listing omits borrower/owner; public mortgage field: $216k | Planet Home Lending';
  assert.deepEqual(parseMarkdown(input), [
    { type: 'paragraph', children: [{ type: 'text', value: input }] },
  ]);
});

test('tables: ragged body rows close the table and remain paragraphs', () => {
  const fewer = parseMarkdown('| A | B |\n| --- | --- |\n| only one |');
  assert.deepEqual(fewer.map((block) => block.type), ['table', 'paragraph']);
  const fewerTable = fewer[0] as Extract<MdBlock, { type: 'table' }>;
  const fewerParagraph = fewer[1] as Extract<MdBlock, { type: 'paragraph' }>;
  assert.equal(fewerTable.rows.length, 0);
  assert.equal(inlineShape(fewerParagraph.children), '"| only one |"');

  const more = parseMarkdown('| A | B |\n| --- | --- |\n| one | two | three |');
  assert.deepEqual(more.map((block) => block.type), ['table', 'paragraph']);
  const moreTable = more[0] as Extract<MdBlock, { type: 'table' }>;
  const moreParagraph = more[1] as Extract<MdBlock, { type: 'paragraph' }>;
  assert.equal(moreTable.rows.length, 0);
  assert.equal(inlineShape(moreParagraph.children), '"| one | two | three |"');
});

test('tables: delimiter-looking row without a header remains paragraph text', () => {
  const blocks = parseMarkdown('| --- | --- |');
  assert.equal(blocks.length, 1);
  const p = blocks[0] as Extract<MdBlock, { type: 'paragraph' }>;
  assert.equal(p.type, 'paragraph');
  assert.equal(inlineShape(p.children), '"| --- | --- |"');
});

test('tables: garbage delimiter row prevents table recognition', () => {
  const blocks = parseMarkdown('| A | B |\n| --- | nope |');
  assert.equal(blocks.length, 1);
  const p = blocks[0] as Extract<MdBlock, { type: 'paragraph' }>;
  assert.equal(p.type, 'paragraph');
  assert.equal(inlineShape(p.children), '"| A | B |"+BR+"| --- | nope |"');
});

test('tables: explicit empty cells parse as empty inline arrays', () => {
  const table = parseMarkdown('| | 2 |\n| --- | --- |\n|  | **x** |')[0] as Extract<MdBlock, { type: 'table' }>;
  assert.equal(table.type, 'table');
  assert.equal(inlineShape(table.header[0]), '');
  assert.equal(inlineShape(table.header[1]), '"2"');
  assert.equal(inlineShape(table.rows[0][0]), '');
  assert.equal(inlineShape(table.rows[0][1]), 'B("x")');
});

test('blank line splits paragraphs', () => {
  const blocks = parseMarkdown('a\n\nb');
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].type, 'paragraph');
  assert.equal(blocks[1].type, 'paragraph');
});

test('mixed document: heading + paragraph with bold + list', () => {
  const blocks = parseMarkdown('## Done\n\nFixed the **idle** badge.\n\n- a\n- b');
  assert.deepEqual(blocks.map((b) => b.type), ['heading', 'paragraph', 'list']);
});

test('robustness: empty / unterminated markers do not throw and round-trip text', () => {
  assert.deepEqual(parseMarkdown(''), []);
  assert.equal(inlineShape(parseInline('just * a lone star')), '"just * a lone star"');
  assert.equal(inlineShape(parseInline('unterminated `code')), '"unterminated `code"');
  assert.equal(inlineShape(parseInline('****')), '"****"');
});
