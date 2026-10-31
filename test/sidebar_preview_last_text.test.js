const test = require('node:test');
const assert = require('node:assert/strict');
const { projectChatStreamSessionsToDesktop } = require('../renderer/sidebar_filter');
const { sanitizeSidebarDetail } = require('../renderer/chat_ui_state');

// Rows shaped like a live inventory readback: the
// presence `preview` is the tmux pane line, `last_text` the latest conversation message.
const PROVIDER_ROWS = [
  { stream_id: 'local:row-claude', host: 'local', session_name: 'row-claude', provider: 'claude', visibility: 'default',
    preview: '  ⏵⏵ bypass permissions on (shift+tab to cycle)', last_text: 'I made the change and left it uncommitted.', last_kind: 'ASSIST_TEXT' },
  { stream_id: 'local:row-codex', host: 'local', session_name: 'row-codex', provider: 'codex', visibility: 'default',
    preview: '  GPT-6.1-Sol high · ~/agent-workspace', last_text: 'All 12 cells are proven, and the spec is closed.', last_kind: 'ASSIST_TEXT' },
  { stream_id: 'local:row-worked', host: 'local', session_name: 'row-worked', provider: 'codex', visibility: 'default',
    preview: '  Worked for 2m 3s • 3:39 PM', last_text: 'Verified: this is an intentional resource.', last_kind: 'ASSIST_TEXT' },
];
const COMPOSITE_ROW = {
  stream_id: 'local:assistant', host: 'local', session_name: 'assistant', provider: 'composite', session_kind: 'assistant_composite',
  visibility: 'visible', preview: '', last_text: "Got it. Pinning now.", last_kind: 'ASSIST_TEXT',
};

const shown = (rows) => projectChatStreamSessionsToDesktop(rows, () => 'local')
  .map((p) => [p.stream_id, sanitizeSidebarDetail(p.preview)]);

test('idle provider rows preview sanitized last_text, never the pane line', () => {
  assert.deepEqual(shown(PROVIDER_ROWS), [
    ['local:row-claude', 'I made the change and left it uncommitted.'],
    ['local:row-codex', 'All 12 cells are proven, and the spec is closed.'],
    ['local:row-worked', 'Verified: this is an intentional resource.'],
  ]);
});

test('a pane-line preview with no last_text shows nothing', () => {
  assert.deepEqual(shown([{ ...PROVIDER_ROWS[2], last_text: '' }]), [['local:row-worked', '']]);
});

test('markdown in last_text renders as its words', () => {
  assert.deepEqual(shown([{ ...PROVIDER_ROWS[0], last_text: 'Result **Gate** passed with `code`' }]),
    [['local:row-claude', 'Result Gate passed with code']]);
});

test('composite row previews its last_text like any other row', () => {
  assert.deepEqual(shown([COMPOSITE_ROW]), [['local:assistant', COMPOSITE_ROW.last_text]]);
});
