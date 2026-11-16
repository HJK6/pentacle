const test = require('node:test');
const assert = require('node:assert/strict');

const {
  renderStatusView,
  planProgress,
  specLifecycleTone,
} = require('../renderer/chat_ui_state');

const NOW = Date.parse('2026-07-29T12:00:00.000Z');

function session(overrides = {}) {
  return {
    model: 'claude-opus-4-8',
    effort: 'high',
    context_tokens: 40000,
    model_context_window: 1000000,
    status_card: {
      goal: 'Deliver the sample change',
      plan: [
        { text: 'Set up', status: 'done' },
        { text: 'Implement', status: 'active' },
        { text: 'QA', status: 'pending' },
      ],
      update: 'Updated the sidebar',
      updates: [
        { ts: '2026-07-29T11:00:00Z', text: 'Started' },
        { ts: '2026-07-29T11:30:00Z', text: 'Updated the sidebar' },
      ],
      specs: [
        { id: 'spec_a', label: 'Check A', ok: true, status: 'in_progress' },
        { id: 'spec_b', label: 'Check B', ok: false, status: 'blocked', note: 'needs review' },
      ],
      handoff_planned: true,
      updated_at: '2026-07-29T11:59:00Z',
    },
    spec_issues: [{ obligation_id: 'o1', detail: 'missing check' }],
    ...overrides,
  };
}

test('planProgress counts done/total and finds active index', () => {
  const p = planProgress(session().status_card.plan);
  assert.deepEqual(p, { total: 3, done: 1, activeIndex: 1 });
  assert.deepEqual(planProgress([]), { total: 0, done: 0, activeIndex: -1 });
});

test('specLifecycleTone maps ok/attention/unknown', () => {
  assert.equal(specLifecycleTone({ ok: true }), 'is-ok');
  assert.equal(specLifecycleTone({ ok: false }), 'is-attention');
  assert.equal(specLifecycleTone({}), 'is-unknown');
  assert.equal(specLifecycleTone(null), 'is-unknown');
});

test('full status view renders every present field', () => {
  const html = renderStatusView(session(), { nowMs: NOW });
  assert.match(html, /data-status-view="1"/);
  assert.match(html, /claude-opus-4-8/); // model
  assert.match(html, /effort: high/); // effort
  assert.match(html, /Deliver the sample change/); // goal
  // full plan: all three steps present, not just the active one
  assert.match(html, /Set up/);
  assert.match(html, /Implement/);
  assert.match(html, /QA/);
  assert.match(html, /1\/3 done/); // plan progress
  assert.match(html, /Updated the sidebar/); // latest update
  assert.match(html, /Started/); // update history entry
  assert.match(html, /ctx 40k/); // context indicator
  assert.match(html, /handoff planned/); // handoff badge
  assert.match(html, /Check A/); // check lifecycle
  assert.match(html, /needs review/); // spec note
  assert.match(html, /missing check/); // spec issue
});

test('update history is newest-first and scroll-preservable', () => {
  const html = renderStatusView(session(), { nowMs: NOW });
  assert.match(html, /data-status-scroll="updates"/);
  // Newest ("Updated the sidebar" @ 11:30) must appear before older ("Started" @ 11:00).
  const iNew = html.indexOf('11:30:00');
  const iOld = html.indexOf('11:00:00');
  assert.ok(iNew !== -1 && iOld !== -1 && iNew < iOld, 'newest update should sort first');
});

test('check lifecycle tones reflect ok flag', () => {
  const html = renderStatusView(session(), { nowMs: NOW });
  assert.match(html, /slot-status-view-spec is-ok[^>]*data-spec-id="spec_a"/);
  assert.match(html, /slot-status-view-spec is-attention[^>]*data-spec-id="spec_b"/);
});

test('return paths to transcript and update log are present and labelled', () => {
  const html = renderStatusView(session(), { nowMs: NOW });
  assert.match(html, /data-status-return="transcript"[^>]*aria-label="Back to transcript"/);
  assert.match(html, /data-status-return="updates"[^>]*aria-label="Jump to update log"/);
});

test('missing fields render nothing rather than placeholders', () => {
  const html = renderStatusView({ status_card: { updated_at: '2026-07-29T11:59:00Z' } }, { nowMs: NOW });
  assert.doesNotMatch(html, /is-model-effort/);
  assert.doesNotMatch(html, /is-plan/);
  assert.doesNotMatch(html, /is-updates/);
  assert.doesNotMatch(html, /is-spec-lifecycle/);
  // The view shell + return paths still render.
  assert.match(html, /data-status-view="1"/);
  assert.match(html, /data-status-return="transcript"/);
});

test('empty session is safe', () => {
  const html = renderStatusView({}, { nowMs: NOW });
  assert.match(html, /data-status-view="1"/);
});

