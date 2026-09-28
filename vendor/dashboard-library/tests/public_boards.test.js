// Projected synthetic witnesses: helpers
{
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const TD = require('../dist/dashboard-library.js');

test('DEFAULT_STATUSES is the 8-status post-migration set', () => {
  assert.equal(TD.DEFAULT_STATUSES.length, 8);
  assert.deepEqual(
    TD.DEFAULT_STATUSES.map((s) => s.name),
    ['backlog', 'analysis', 'ready_for_dev', 'in_progress', 'needs_qa', 'blocked', 'completed', 'deprecated'],
  );
});

test('selectVisibleStatuses hides terminal columns by default, showAll reveals', () => {
  const vis = TD.selectVisibleStatuses(TD.DEFAULT_STATUSES, false).map((s) => s.name);
  assert.deepEqual(vis, ['backlog', 'analysis', 'ready_for_dev', 'in_progress', 'needs_qa', 'blocked']);
  const all = TD.selectVisibleStatuses(TD.DEFAULT_STATUSES, true).map((s) => s.name);
  assert.equal(all.length, 8);
});

test('partitionRows buckets by row.status; status_unknown → Other; synthetic → unresolved', () => {
  const rows = [
    { spec_id: 'a', status: 'backlog' },
    { spec_id: 'b', status: 'in_progress' },
    { spec_id: 'c', status: 'made_up', status_unknown: true },
    { spec_id: 'd', status: 'also_unconfigured' }, // not in statuses → Other
    { spec_id: 'e', synthetic: true },
  ];
  const { byStatus, otherStatusRows, unresolved } = TD.partitionRows(rows, TD.DEFAULT_STATUSES);
  assert.equal(byStatus.backlog.length, 1);
  assert.equal(byStatus.in_progress.length, 1);
  assert.equal(otherStatusRows.length, 2);
  assert.equal(unresolved.length, 1);
});

test('partitionRows keys off row.status ONLY (canonical desktop form, no lifecycle alias)', () => {
  // A row carrying only the legacy `lifecycle` (no status) must NOT be placed
  // into a column — proving the Pi `row.status || row.lifecycle` drift is gone.
  const rows = [{ spec_id: 'x', lifecycle: 'in_progress' }];
  const { byStatus, otherStatusRows } = TD.partitionRows(rows, TD.DEFAULT_STATUSES);
  assert.equal(byStatus.in_progress.length, 0);
  assert.equal(otherStatusRows.length, 0); // no status at all → skipped
});

test('sortForList orders next_action-present first, then by id', () => {
  const rows = [
    { spec_id: 'zeta' },
    { spec_id: 'alpha', next_action: 'do x' },
    { spec_id: 'beta' },
  ];
  const sorted = TD.sortForList(rows).map((r) => r.spec_id);
  assert.equal(sorted[0], 'alpha'); // has next_action
  assert.deepEqual(sorted.slice(1), ['beta', 'zeta']); // remainder alpha-sorted
});

test('escapeHtml neutralizes markup', () => {
  assert.equal(TD.escapeHtml('<b>&"\''), '&lt;b&gt;&amp;&quot;&#39;');
});

test('VERSION is stamped (not the placeholder)', () => {
  assert.notEqual(TD.VERSION, '__VERSION__');
  assert.ok(TD.VERSION.length > 0);
});

}

// Projected synthetic witnesses: specs_board
{
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const TD = require('../dist/dashboard-library.js');

const specs = TD.boards.specs;
const sampleStatuses = TD.DEFAULT_STATUSES;
const sampleState = {
  view: 'specs',
  statuses: sampleStatuses,
  specs: [
    { spec_id: 'a', title: 'Alpha spec', status: 'backlog', repo: 'pentacle', next_action: 'do x' },
    { spec_id: 'b', title: 'Beta spec', status: 'in_progress', frontmatter_drift: true },
    { spec_id: 'c', title: 'Done spec', status: 'completed' },
  ],
  epics: [{ id: 'epic_x', title: 'Epic X', status: 'active', members: ['a', 'b'] }],
};

test('specs board registered with manifest', () => {
  assert.ok(specs);
  assert.equal(specs.id, 'specs');
  const ids = specs.manifest.elements.map((e) => e.id);
  assert.ok(ids.includes('specs-epics-toggle') && ids.includes('kanban') && ids.includes('spec-drive'));
});

test('spec-drive is interactiveOnly (desktop wires it; Pi read-only)', () => {
  assert.equal(TD.elementVisibleInMode(TD.manifestElement(specs.manifest, 'spec-drive'), 'interactive'), true);
  assert.equal(TD.elementVisibleInMode(TD.manifestElement(specs.manifest, 'spec-drive'), 'display'), false);
});

test('specs view renders a kanban with cards keyed by spec id, terminal hidden by default', () => {
  const html = TD.renderBoard(null, specs, sampleState, { mode: 'display' });
  assert.match(html, /data-role="kanban"/);
  assert.match(html, /data-spec-id="a"/);
  assert.match(html, /Alpha spec/);
  assert.match(html, /class="chip drift"/); // beta has drift
  // completed is terminal/default-hidden -> column absent unless showAll
  assert.doesNotMatch(html, /data-status="completed"/);
  // toggle present on both surfaces (the Pi's missing piece)
  assert.match(html, /data-role="specs-toggle"/);
});

test('specs view groups multiple same-epic specs within one lane', () => {
  const html = TD.renderBoard(null, specs, {
    ...sampleState,
    specs: [
      { spec_id: 'a', title: 'Alpha spec', status: 'backlog', epic: 'epic_ops', epic_title: 'Ops Cleanup' },
      { spec_id: 'b', title: 'Beta spec', status: 'backlog', epic: 'epic_ops', epic_title: 'Ops Cleanup' },
      { spec_id: 'c', title: 'Gamma spec', status: 'backlog', epic: 'epic_solo' },
      { spec_id: 'd', title: 'Loose spec', status: 'backlog' },
      { spec_id: 'e', title: 'Later lane spec', status: 'in_progress', epic: 'epic_ops', epic_title: 'Ops Cleanup' },
    ],
  }, { mode: 'display' });

  assert.match(html, /class="card epic-group-row" data-epic-id="epic_ops"/);
  assert.match(html, /Ops Cleanup/);
  assert.match(html, /2 specs/);
  assert.match(html, /<li>Alpha spec<\/li>/);
  assert.match(html, /<li>Beta spec<\/li>/);
  assert.doesNotMatch(html, /data-spec-id="a"/);
  assert.doesNotMatch(html, /data-spec-id="b"/);
  assert.match(html, /data-spec-id="c"/);
  assert.match(html, /data-spec-id="d"/);
  assert.match(html, /data-spec-id="e"/);
});

test('showAll reveals terminal columns', () => {
  const html = TD.renderBoard(null, specs, { ...sampleState, showAll: true }, { mode: 'display' });
  assert.match(html, /data-status="completed"/);
});

test('epics view renders epic cards from state.epics', () => {
  const html = TD.renderBoard(null, specs, { ...sampleState, view: 'epics' }, { mode: 'interactive' });
  assert.match(html, /data-epic-id="epic_x"/);
  assert.match(html, /Epic X/);
  assert.match(html, /2 specs/);
  assert.doesNotMatch(html, /data-role="kanban"/); // kanban not shown in epics view
});

test('display and interactive render the same kanban (drive wiring is adapter-side, not markup)', () => {
  const disp = TD.renderBoard(null, specs, sampleState, { mode: 'display' });
  const inter = TD.renderBoard(null, specs, sampleState, { mode: 'interactive' });
  // card markup identical; the difference is only the data-mode attr + adapter-attached clicks
  assert.equal(disp.replace(/data-mode="display"/, 'M'), inter.replace(/data-mode="interactive"/, 'M'));
});

}

// Projected synthetic witnesses: foreclosure_board
{
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const TD = require('../dist/dashboard-library.js');

const fc = TD.boards.foreclosure;
const T = fc._test;

test('foreclosure board registered with manifest', () => {
  assert.ok(fc);
  assert.equal(fc.id, 'foreclosure');
  assert.equal(typeof fc.mount, 'function');
  assert.equal(typeof fc.update, 'function');
  assert.equal(typeof fc.unmount, 'function');
  const ids = fc.manifest.elements.map((e) => e.id);
  assert.ok(ids.includes('pipeline-flow'));
  assert.ok(ids.includes('batch-select'));
  assert.ok(ids.includes('skiptrace-gate'));
  assert.ok(ids.includes('payment-gate'));
  assert.ok(ids.includes('stage-drilldown'));
});

test('batch-select / skiptrace-gate / stage-drilldown are interactiveOnly (Pi hides them)', () => {
  for (const id of ['batch-select', 'skiptrace-gate', 'payment-gate', 'stage-drilldown']) {
    const el = TD.manifestElement(fc.manifest, id);
    assert.equal(TD.elementVisibleInMode(el, 'interactive'), true, `${id} visible interactive`);
    assert.equal(TD.elementVisibleInMode(el, 'display'), false, `${id} hidden display`);
  }
});

test('display surfaces (pipelines/pills/summary) are always visible', () => {
  for (const id of ['pipeline-tabs', 'pipeline-flow', 'distribution-pills', 'skiptrace-summary']) {
    const el = TD.manifestElement(fc.manifest, id);
    assert.equal(TD.elementVisibleInMode(el, 'display'), true, `${id} visible display`);
    assert.equal(TD.elementVisibleInMode(el, 'interactive'), true, `${id} visible interactive`);
  }
});

test('_rollup: error > running > waiting_email > pending; all-complete -> complete', () => {
  const rows = (states) => states.map((state) => ({ state }));
  assert.equal(T._rollup(rows(['complete', 'complete', 'complete'])), 'complete');
  assert.equal(T._rollup(rows(['complete', 'running'])), 'running');
  assert.equal(T._rollup(rows(['running', 'failed'])), 'failed');
  assert.equal(T._rollup(rows(['waiting_email', 'waiting'])), 'waiting_email');
  assert.equal(T._rollup(rows(['waiting', 'complete'])), 'waiting');
  assert.equal(T._rollup([]), 'waiting');
});

test('_skiptraceExecuted requires definitive paid or delivered evidence', () => {
  assert.equal(T._skiptraceExecuted({}), false);
  assert.equal(T._skiptraceExecuted({ skipmatrix_submit: { state: 'complete' } }), false);
  assert.equal(T._skiptraceExecuted({ skipmatrix_paid_confirm: { state: 'complete' } }), true);
  assert.equal(T._skiptraceExecuted({ skipmatrix_results: { state: 'complete' } }), true);
  assert.equal(T._skiptraceExecuted({ skipmatrix_results: { state: 'waiting' } }), false);
});

test('_stateMeta falls back to pending icon for unknown states', () => {
  assert.equal(T._stateMeta('complete').cls, 'success');
  assert.equal(T._stateMeta('running').cls, 'running');
  const unknown = T._stateMeta('something_new');
  assert.equal(unknown.cls, 'pending');
  assert.equal(unknown.icon, '○');
});

test('_skiptraceGate only recognizes open/closed', () => {
  assert.equal(T._skiptraceGate({ pipeline_summary: { skiptrace_gate: 'open' } }), 'open');
  assert.equal(T._skiptraceGate({ pipeline_summary: { skiptrace_gate: 'closed' } }), 'closed');
  assert.equal(T._skiptraceGate({ pipeline_summary: { skiptrace_gate: 'weird' } }), null);
  assert.equal(T._skiptraceGate({}), null);
});

test('_qualifiedWaitingCount prefers actual count, falls back to nested staging_by_state', () => {
  assert.equal(T._qualifiedWaitingCount({ qualified_actual_count: 269 }), 269);
  assert.equal(T._qualifiedWaitingCount({ staging_by_state: { FL: { early: 3, forecl: 2 }, GA: 5 } }), 10);
  assert.equal(T._qualifiedWaitingCount({}), 0);
});

test('_selectedBatch prefers the dataset pin, then default, then state-machine batch', () => {
  const root = { dataset: { selectedBatch: '2026-05-Z' } };
  assert.equal(T._selectedBatch(root, { default_batch: '2026-05-B' }), '2026-05-Z');
  assert.equal(T._selectedBatch({ dataset: {} }, { default_batch: '2026-05-B' }), '2026-05-B');
  assert.equal(T._selectedBatch({ dataset: {} }, { pipeline_summary: { state_machine_batch: '2026-05-A' } }), '2026-05-A');
});

test('_applyOptimisticGate pins the just-toggled gate over a stale poll, then releases', () => {
  const refs = { root: { _gateOptimistic: { batch: '2026-05-B', gate: 'open', expiresAt: Date.now() + 30000 } } };
  const stale = { batch: '2026-05-B', pipeline_summary: { state_machine_batch: '2026-05-B', skiptrace_gate: 'closed' } };
  const out = T._applyOptimisticGate(refs, stale);
  assert.equal(out.pipeline_summary.skiptrace_gate, 'open', 'optimistic value pinned');
  // A confirming poll (server caught up) clears the guard.
  const confirming = { batch: '2026-05-B', pipeline_summary: { state_machine_batch: '2026-05-B', skiptrace_gate: 'open' } };
  T._applyOptimisticGate(refs, confirming);
  assert.equal(refs.root._gateOptimistic, null, 'guard cleared once server agrees');
});

test('_paymentGate only recognizes open/closed', () => {
  assert.equal(T._paymentGate({ pipeline_summary: { payment_gate: 'open' } }), 'open');
  assert.equal(T._paymentGate({ pipeline_summary: { payment_gate: 'closed' } }), 'closed');
  assert.equal(T._paymentGate({ pipeline_summary: { payment_gate: 'weird' } }), null);
  assert.equal(T._paymentGate({}), null);
});

test('_applyOptimisticPaymentGate pins the just-toggled payment gate, then releases', () => {
  const refs = { root: { _payGateOptimistic: { batch: '2026-05-B', gate: 'open', expiresAt: Date.now() + 30000 } } };
  const stale = { batch: '2026-05-B', pipeline_summary: { state_machine_batch: '2026-05-B', payment_gate: 'closed' } };
  const out = T._applyOptimisticPaymentGate(refs, stale);
  assert.equal(out.pipeline_summary.payment_gate, 'open', 'optimistic value pinned');
  const confirming = { batch: '2026-05-B', pipeline_summary: { state_machine_batch: '2026-05-B', payment_gate: 'open' } };
  T._applyOptimisticPaymentGate(refs, confirming);
  assert.equal(refs.root._payGateOptimistic, null, 'guard cleared once server agrees');
});

test('payment and skiptrace optimistic guards are independent (one toggle never clobbers the other)', () => {
  const refs = { root: {
    _gateOptimistic: { batch: 'B', gate: 'open', expiresAt: Date.now() + 30000 },
    _payGateOptimistic: { batch: 'B', gate: 'closed', expiresAt: Date.now() + 30000 },
  } };
  const data = { batch: 'B', pipeline_summary: { state_machine_batch: 'B', skiptrace_gate: 'closed', payment_gate: 'open' } };
  let out = T._applyOptimisticGate(refs, data);
  out = T._applyOptimisticPaymentGate(refs, out);
  assert.equal(out.pipeline_summary.skiptrace_gate, 'open', 'skiptrace guard applied');
  assert.equal(out.pipeline_summary.payment_gate, 'closed', 'payment guard applied, skiptrace untouched');
});

test('_applyOptimisticGate expires the guard after the window', () => {
  const refs = { root: { _gateOptimistic: { batch: 'b', gate: 'open', expiresAt: Date.now() - 1 } } };
  const out = T._applyOptimisticGate(refs, { batch: 'b', pipeline_summary: { skiptrace_gate: 'closed' } });
  assert.equal(out.pipeline_summary.skiptrace_gate, 'closed', 'expired guard no longer overrides');
  assert.equal(refs.root._gateOptimistic, null);
});

}

// Projected synthetic witnesses: chat_stream_board
{
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const TD = require('../dist/dashboard-library.js');

const cs = TD.boards['chat-stream'];

test('chat-stream board registered with manifest + lifecycle', () => {
  assert.ok(cs);
  assert.equal(cs.id, 'chat-stream');
  assert.equal(typeof cs.mount, 'function');
  assert.equal(typeof cs.update, 'function');
  const ids = cs.manifest.elements.map((e) => e.id);
  assert.ok(ids.includes('sessions') && ids.includes('timeline') && ids.includes('stats'));
});

test('chat-stream elements are display-safe (session select is local nav, not interactiveOnly)', () => {
  for (const e of cs.manifest.elements) {
    assert.equal(TD.elementVisibleInMode(e, 'display'), true, `${e.id} visible on Pi`);
  }
});

test('_groupSessions groups events by stream and sorts by recency', () => {
  const events = [
    { stream_id: 'a', host: 'samplehost', provider: 'claude', timestamp: '2026-05-26T10:00:00Z', text: 'hi', kind: 'USER' },
    { stream_id: 'a', host: 'samplehost', provider: 'claude', timestamp: '2026-05-26T10:05:00Z', text: 'reply', kind: 'ASSIST' },
    { stream_id: 'b', host: 'otherhost', provider: 'codex', timestamp: '2026-05-26T11:00:00Z', text: 'go', kind: 'TOOL' },
  ];
  const sessions = cs._test._groupSessions(events);
  assert.equal(sessions.length, 2);
  assert.equal(sessions[0].key, 'b', 'most-recent session first');
  const a = sessions.find((s) => s.key === 'a');
  assert.equal(a.events.length, 2);
  assert.equal(a.lastText, 'reply');
});

test('_kindColor / _hostColor map known kinds and neutral host fallback', () => {
  assert.equal(cs._test._kindColor('ASSIST'), '#56d364');
  assert.equal(cs._test._hostColor('samplehost'), '#2dd4bf');
});

}

// Projected synthetic witnesses: ui_review_board
{
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const TD = require('../dist/dashboard-library.js');

const ui = TD.boards['ui-review'];

test('ui-review board registered with manifest + lifecycle', () => {
  assert.ok(ui);
  assert.equal(ui.id, 'ui-review');
  assert.equal(typeof ui.mount, 'function');
  const ids = ui.manifest.elements.map((e) => e.id);
  assert.ok(ids.includes('stats') && ids.includes('list') && ids.includes('preview') && ids.includes('filters'));
});

test('filter controls are interactiveOnly (passive Pi wall hides search/dropdowns)', () => {
  const f = TD.manifestElement(ui.manifest, 'filters');
  assert.equal(TD.elementVisibleInMode(f, 'interactive'), true);
  assert.equal(TD.elementVisibleInMode(f, 'display'), false);
  // list + preview stay visible on the Pi
  assert.equal(TD.elementVisibleInMode(TD.manifestElement(ui.manifest, 'list'), 'display'), true);
  assert.equal(TD.elementVisibleInMode(TD.manifestElement(ui.manifest, 'preview'), 'display'), true);
});

test('_matchesFilters matches on text query + facets', () => {
  const item = { title: 'Login screen', repo: 'pentacle', machine: 'bart', tags: ['auth', 'mobile'] };
  assert.equal(ui._test._matchesFilters(item, { query: 'login' }), true);
  assert.equal(ui._test._matchesFilters(item, { query: 'logout' }), false);
  assert.equal(ui._test._matchesFilters(item, { repo: 'pentacle' }), true);
  assert.equal(ui._test._matchesFilters(item, { repo: 'altum' }), false);
  assert.equal(ui._test._matchesFilters(item, { tag: 'auth' }), true);
  assert.equal(ui._test._matchesFilters(item, { tag: 'desktop' }), false);
});

test('_artifactKey prefers artifactKey then id/url; _unique sorts + dedupes', () => {
  assert.equal(ui._test._artifactKey({ artifactKey: 'k', id: 'i' }), 'k');
  assert.equal(ui._test._artifactKey({ url: 'u' }), 'u');
  assert.deepEqual(ui._test._unique(['b', 'a', 'b', null, 'a']), ['a', 'b']);
});

}

// Projected synthetic witnesses: notifications_run_command
{
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const TD = require('../dist/dashboard-library.js');

const notifications = TD.boards.notifications;
const T = notifications._test;

const STDOUT_PAYLOAD = 'stdout & "quote" <script>alert("stdout")</script>';
const STDERR_PAYLOAD = 'stderr & "quote" <script>fail("stderr")</script>';
const STREAM_PAYLOAD = 'stream-123 & "quote" <script>spawn("stream")</script>';

function renderNotification(notification, visible = true) {
  const list = {
    innerHTML: '',
    querySelectorAll() { return []; },
  };
  const refs = { list, notifications: [] };
  const ctx = { isVisible: () => visible };
  notifications.update(refs, { notifications: [notification] }, ctx);
  return list.innerHTML;
}

function renderDisplayNotification(notification) {
  const list = { innerHTML: '', querySelectorAll() { return []; } };
  const refs = { list, notifications: [] };
  notifications.update(refs, { notifications: [notification] }, { mode: 'display', isVisible: () => false });
  return list.innerHTML;
}


function baseNotification(overrides = {}) {
  return {
    notification_id: 'n-1',
    severity: 'info',
    state: 'open',
    title: 'Run command',
    body: 'Needs operator approval',
    producer: 'test',
    created_at: '2026-05-29T12:00:00Z',
    actions: [{ kind: 'run_command' }],
    ...overrides,
  };
}

function deferred() {
  const box = {};
  box.promise = new Promise((resolve) => { box.resolve = resolve; });
  return box;
}

function fakeActionDom() {
  const buttons = [{ disabled: false }, { disabled: false }];
  const pending = { textContent: '', style: { display: 'none' } };
  const error = { textContent: '', style: { display: 'none' } };
  const actionsEl = {
    querySelectorAll(selector) {
      return selector === 'button[data-action]' ? buttons : [];
    },
    querySelector(selector) {
      return selector === '[data-role="resolve-working"]' ? pending : null;
    },
    insertAdjacentHTML() {},
  };
  const card = {
    querySelector(selector) {
      if (selector === '[data-role="actions"]') return actionsEl;
      if (selector === '[data-role="resolve-error"]') return error;
      return null;
    },
  };
  return {
    buttons,
    pending,
    error,
    refs: { list: { querySelector: () => card } },
  };
}

test('notifications terminal states include run_command results', () => {
  assert.ok(T.TERMINAL_STATES.includes('done'));
  assert.ok(T.TERMINAL_STATES.includes('failed'));
  assert.equal(T.isTerminal('done'), true);
  assert.equal(T.isTerminal('failed'), true);
});

test('open run_command notification renders Run action button', () => {
  const html = renderNotification(baseNotification());
  assert.match(html, /data-action="run_command"/);
  assert.match(html, />Run<\/button>/);
});

test('display notification uses text-only question options', () => {
  const html = renderDisplayNotification(baseNotification({
    producer: 'agent_question.v1',
    question: { response_mode: 'single_choice', options: [{ label: 'Ship', value: 'ship' }] },
  }));
  assert.doesNotMatch(html, /<input|<textarea|<select|data-action=/);
  assert.match(html, /Ship/);
});


test('notification action buttons render per-button action_id', () => {
  const html = renderNotification(baseNotification({
    actions: [
      { kind: 'run_command', action_id: 'cmd-a', label: 'Run A' },
      { kind: 'run_command', action_id: 'cmd-b', label: 'Run B' },
    ],
  }));

  assert.match(html, /data-action-id="cmd-a"/);
  assert.match(html, /data-action-id="cmd-b"/);
  assert.match(html, /data-action="run_command" data-id="n-1" data-action-id="cmd-a"/);
  assert.match(html, /data-action="run_command" data-id="n-1" data-action-id="cmd-b"/);
});

test('yes_no buttons share their action_id and keep distinct choices', () => {
  const html = renderNotification(baseNotification({
    actions: [{ kind: 'yes_no', action_id: 'answer-1', yes_label: 'Approve', no_label: 'Reject' }],
  }));

  assert.match(html, /data-action-id="answer-1" data-choice="true"/);
  assert.match(html, /data-action-id="answer-1" data-choice="false"/);
});

test('running run_command notification renders status and no action buttons', () => {
  const html = renderNotification(baseNotification({ state: 'running' }));
  assert.match(html, /running…/);
  assert.doesNotMatch(html, /data-role="actions"/);
  assert.doesNotMatch(html, /data-action="run_command"/);
});

test('done run_command notification renders escaped stdout and no action buttons', () => {
  const html = renderNotification(baseNotification({
    state: 'done',
    resolution: { result: { stdout_tail: STDOUT_PAYLOAD } },
  }));
  assert.match(html, /✓ done/);
  assert.match(html, /data-role="stdout-tail"/);
  assert.match(html, /stdout &amp; &quot;quote&quot; &lt;script&gt;alert\(&quot;stdout&quot;\)&lt;\/script&gt;/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&amp;/);
  assert.match(html, /&quot;/);
  assert.doesNotMatch(html, /stdout & "quote"/);
  assert.doesNotMatch(html, /<script>alert\("stdout"\)<\/script>/);
  assert.doesNotMatch(html, /"quote" <script>/);
  assert.doesNotMatch(html, /data-role="actions"/);
});

test('non-command terminal states render decision annotations', () => {
  assert.match(T._statusHtml(baseNotification({
    state: 'answered',
    resolution: { action_kind: 'yes_no', choice: true, by: 'operator', at: '2026-06-19T12:00:00Z' },
  })), /Answered: Yes/);
  assert.match(T._statusHtml(baseNotification({
    state: 'answered',
    resolution: { action_kind: 'yes_no', choice: false },
  })), /Answered: No/);
  assert.match(T._statusHtml(baseNotification({
    state: 'acked',
    resolution: { action_kind: 'ack' },
  })), /Acknowledged/);
  assert.match(T._statusHtml(baseNotification({
    state: 'spawned',
    resolution: { action_kind: 'spawn_worker', spawned_stream_id: STREAM_PAYLOAD },
  })), /Spawned: stream-123 &amp; &quot;quote&quot; &lt;script&gt;spawn\(&quot;stream&quot;\)&lt;\/script&gt;/);
  assert.match(T._statusHtml(baseNotification({
    state: 'resolved',
    resolution: { action_kind: 'dismiss' },
  })), /Dismissed/);
  assert.match(T._statusHtml(baseNotification({
    state: 'resolved',
    resolution: { action_kind: 'resolved' },
  })), /Resolved/);
  assert.match(T._statusHtml(baseNotification({
    state: 'done',
    resolution: { result: { stdout_tail: STDOUT_PAYLOAD } },
  })), /✓ done/);
  assert.match(T._statusHtml(baseNotification({
    state: 'failed',
    resolution: { result: { stderr_tail: STDERR_PAYLOAD } },
  })), /✗ failed/);
});

test('resolved agent question renders escaped free-text answer', () => {
  const html = renderNotification(baseNotification({
    producer: 'agent_question.v1',
    state: 'resolved',
    question: {
      question_id: 'q-text',
      response_mode: 'free_text',
      options: [],
      answer: {
        text: 'use <typed> answer',
      },
    },
  }));

  assert.match(html, /Answered: use &lt;typed&gt; answer/);
  assert.doesNotMatch(html, /No selection recorded/);
  assert.doesNotMatch(html, /use <typed> answer/);
});

test('failed run_command notification renders escaped stderr, investigator, and no actions', () => {
  const html = renderNotification(baseNotification({
    state: 'failed',
    resolution: {
      result: {
        stderr_tail: STDERR_PAYLOAD,
        spawned_stream_id: STREAM_PAYLOAD,
      },
    },
  }));
  assert.match(html, /✗ failed/);
  assert.match(html, /data-role="stderr-tail"/);
  assert.match(html, /stderr &amp; &quot;quote&quot; &lt;script&gt;fail\(&quot;stderr&quot;\)&lt;\/script&gt;/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&amp;/);
  assert.match(html, /&quot;/);
  assert.doesNotMatch(html, /stderr & "quote"/);
  assert.doesNotMatch(html, /<script>fail\("stderr"\)<\/script>/);
  assert.doesNotMatch(html, /"quote" <script>fail/);
  assert.match(html, /data-role="investigating"/);
  assert.match(html, /investigating: stream-123 &amp; &quot;quote&quot; &lt;script&gt;spawn\(&quot;stream&quot;\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /investigating: stream-123 & "quote"/);
  assert.doesNotMatch(html, /<script>spawn\("stream"\)<\/script>/);
  assert.doesNotMatch(html, /"quote" <script>spawn/);
  assert.doesNotMatch(html, /data-role="actions"/);
});

test('terminal and running states suppress action buttons', () => {
  for (const state of ['running', 'done', 'failed']) {
    const html = renderNotification(baseNotification({ state }));
    assert.doesNotMatch(html, /data-role="actions"/, `${state} has no actions wrapper`);
    assert.doesNotMatch(html, /data-action="/, `${state} has no action buttons`);
  }
});

test('click passes action_id, disables buttons, shows pending, and re-enables on resolve failure', async () => {
  const d = deferred();
  const dom = fakeActionDom();
  let received;
  const ctx = {
    actions: {
      resolve(id, action, options) {
        received = { id, action, options };
        return d.promise;
      },
    },
  };
  const el = {
    dataset: {
      id: 'n-1',
      action: 'run_command',
      actionId: 'cmd-a',
    },
  };

  const inFlight = T._onAction(dom.refs, el, ctx);

  assert.deepEqual(received, { id: 'n-1', action: 'run_command', options: { action_id: 'cmd-a' } });
  assert.equal(dom.buttons.every((button) => button.disabled), true);
  assert.equal(dom.pending.textContent, 'Running…');
  assert.equal(dom.pending.style.display, '');

  d.resolve({ ok: false, error: 'denied' });
  await inFlight;

  assert.equal(dom.buttons.every((button) => button.disabled), false);
  assert.equal(dom.pending.style.display, 'none');
  assert.equal(dom.error.style.display, '');
  assert.match(dom.error.textContent, /denied/);
});

test('yes_no click passes both action_id and choice', async () => {
  const dom = fakeActionDom();
  let received;
  const ctx = {
    actions: {
      resolve(id, action, options) {
        received = { id, action, options };
        return Promise.resolve({ ok: true });
      },
      refreshNow() {},
    },
  };
  const el = {
    dataset: {
      id: 'n-1',
      action: 'yes_no',
      actionId: 'answer-1',
      choice: 'false',
    },
  };

  await T._onAction(dom.refs, el, ctx);

  assert.deepEqual(received, { id: 'n-1', action: 'yes_no', options: { action_id: 'answer-1', choice: false } });
  assert.equal(dom.buttons.every((button) => button.disabled), true);
  assert.equal(dom.pending.textContent, 'Working…');
});

}

// Projected synthetic witnesses: zdte_board
{
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const TD = require('../dist/dashboard-library.js');

const z = TD.boards['0dte-trading'];

test('0dte-trading board registered with manifest + lifecycle', () => {
  assert.ok(z);
  assert.equal(z.id, '0dte-trading');
  assert.equal(typeof z.mount, 'function');
  const ids = z.manifest.elements.map((e) => e.id);
  assert.ok(ids.includes('stats') && ids.includes('positions') && ids.includes('trader-select'));
});

test('trader-select is interactiveOnly (Pi shows the selected trader read-only)', () => {
  const t = TD.manifestElement(z.manifest, 'trader-select');
  assert.equal(TD.elementVisibleInMode(t, 'interactive'), true);
  assert.equal(TD.elementVisibleInMode(t, 'display'), false);
  assert.equal(TD.elementVisibleInMode(TD.manifestElement(z.manifest, 'stats'), 'display'), true);
});

test('_fmtMoney / _fmtAge / _decimal / _statusBadge', () => {
  assert.equal(z._test._fmtMoney(120), '+$120');
  assert.equal(z._test._fmtMoney(-50), '$-50');
  assert.equal(z._test._fmtMoney(null), '—');
  assert.equal(z._test._fmtAge(45), '45s');
  assert.equal(z._test._fmtAge(120), '2.0m');
  assert.equal(z._test._decimal({ N: '3.5' }), 3.5);
  assert.equal(z._test._decimal('7'), 7);
  assert.equal(z._test._statusBadge('Filled').cls, 'green');
  assert.equal(z._test._statusBadge('Cancelled').text, 'CXLD');
});

}

// Projected synthetic witnesses: tokens_and_visibility
{
'use strict';
// Code-QA follow-ups: visibleIn coverage + precedence, and TOKENS↔dashboard-library.css
// drift guard (the layer exists to kill drift; the two token sources must agree).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const TD = require('../dist/dashboard-library.js');

test('visibleIn gates by mode membership (both modes)', () => {
  const el = { id: 'x', visibleIn: ['display'] };
  assert.equal(TD.elementVisibleInMode(el, 'display'), true);
  assert.equal(TD.elementVisibleInMode(el, 'interactive'), false);
  const both = { id: 'y', visibleIn: ['display', 'interactive'] };
  assert.equal(TD.elementVisibleInMode(both, 'display'), true);
  assert.equal(TD.elementVisibleInMode(both, 'interactive'), true);
});

test('visibleIn takes precedence over interactiveOnly when both set', () => {
  // documented precedence: visibleIn wins (the explicit general form)
  const el = { id: 'z', interactiveOnly: true, visibleIn: ['display'] };
  assert.equal(TD.elementVisibleInMode(el, 'display'), true);
  assert.equal(TD.elementVisibleInMode(el, 'interactive'), false);
});

test('TOKENS JS object matches dashboard-library.css custom properties (no drift)', () => {
  const css = fs.readFileSync(path.join(__dirname, '..', 'dist', 'dashboard-library.css'), 'utf8');
  const cssVar = (name) => {
    const m = css.match(new RegExp(`--td-${name}\\s*:\\s*([^;]+);`));
    return m ? m[1].trim() : null;
  };
  const map = {
    shellBg: 'shell-bg',
    cardBg: 'card-bg',
    cardBorder: 'card-border',
    textPrimary: 'text-primary',
    textMuted: 'text-muted',
    accent: 'accent',
  };
  for (const [jsKey, cssName] of Object.entries(map)) {
    assert.equal(TD.TOKENS[jsKey], cssVar(cssName), `TOKENS.${jsKey} must equal --td-${cssName}`);
  }
});

}

// Projected synthetic witnesses: stateful_board
{
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const TD = require('../dist/dashboard-library.js');

// A stateful board: builds DOM once in mount, updates in place, cleans up.
function makeStatefulDef() {
  const calls = [];
  const def = {
    id: 'stateful-test',
    manifest: { elements: [{ id: 'live', label: 'Live', interactiveOnly: true }] },
    mount(container, ctx) {
      calls.push('mount:' + ctx.mode);
      const el = { innerHTML: '', tag: 'div' };
      if (container) container.__el = el;
      return { el, container, mounts: 1 };
    },
    update(refs, state, ctx) {
      calls.push('update:' + (state && state.n));
      refs.el.innerHTML = `n=${state && state.n};mode=${ctx.mode};live=${ctx.isVisible('live')}`;
    },
    unmount(refs) { calls.push('unmount'); refs.unmounted = true; },
  };
  return { def, calls };
}

test('defineBoard accepts a stateful board (mount, no render)', () => {
  const { def } = makeStatefulDef();
  assert.doesNotThrow(() => TD.defineBoard(def));
});

test('defineBoard rejects a board with neither render nor mount', () => {
  assert.throws(() => TD.defineBoard({ id: 'x', manifest: { elements: [] } }), /render\(\) \(stateless\) or mount\(\) \(stateful\)/);
});

test('mountBoard/updateBoard/unmountBoard drive the stateful lifecycle with ctx', () => {
  const { def, calls } = makeStatefulDef();
  const container = {};
  const refs = TD.mountBoard(container, def, { n: 1 }, { mode: 'interactive' });
  assert.equal(refs.mounts, 1);
  assert.match(refs.el.innerHTML, /n=1;mode=interactive;live=true/);
  TD.updateBoard(refs, def, { n: 2 });
  assert.match(refs.el.innerHTML, /n=2;mode=interactive/);
  TD.unmountBoard(refs, def);
  assert.equal(refs.unmounted, true);
  assert.deepEqual(calls, ['mount:interactive', 'update:1', 'update:2', 'unmount']);
});

test('display mode hides interactiveOnly element for a stateful board', () => {
  const { def } = makeStatefulDef();
  const refs = TD.mountBoard({}, def, { n: 9 }, { mode: 'display' });
  assert.match(refs.el.innerHTML, /live=false/);
});


}

{
const test = require('node:test');
const assert = require('node:assert/strict');
const library = require('../dist/dashboard-library.js');
test('only public consumer boards are vendored', () => {
  assert.deepEqual(Object.keys(library.boards).sort(),
    ['specs', 'foreclosure', 'chat-stream', 'ui-review', 'notifications', '0dte-trading'].sort());
});
}
