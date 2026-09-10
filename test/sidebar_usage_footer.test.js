const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');

function read(relativePath) {
  return fs.readFileSync(path.join(root, relativePath), 'utf8');
}

test('sidebar limits and machine stats footers use explicit ids', () => {
  const html = read('renderer/index.html');
  const app = read('renderer/app.js');

  assert.match(html, /id="limits-footer"/);
  assert.match(html, /id="machine-stats-footer"/);
  assert.doesNotMatch(html, /id="usage-footer"/);

  assert.match(app, /getElementById\('limits-footer'\)/);
  assert.match(app, /getElementById\('machine-stats-footer'\)/);
  assert.doesNotMatch(app, /getElementById\('usage-footer'\)/);
});

test('titlebar keeps settings top-right and renders machine chip mount', () => {
  const html = read('renderer/index.html');
  const app = read('renderer/app.js');
  const css = read('renderer/styles.css');

  assert.match(html, /id="titlebar-machines"/);
  assert.match(html, /<div class="titlebar-right">\s*<button class="settings-btn"/);
  assert.match(app, /function renderTitlebarMachines\(\)/);
  assert.match(app, /renderTitlebarMachines\(\)/);
  assert.match(css, /\.titlebar-machine\.color-royal-blue/);
});

test('renderer repaints three label-percent-bar-reset cards without freshness presentation', () => {
  const app = read('renderer/app.js');
  const css = read('renderer/styles.css');
  const paintStart = app.indexOf('function paintLimits(limits)');
  const renderStart = app.indexOf('function renderLimits(limits, health)', paintStart);

  assert.notEqual(paintStart, -1);
  assert.notEqual(renderStart, -1);
  assert.match(app, /function renderLimits\(limits, health\)/);
  assert.match(app, /renderLimits\(\s*payload\.limits,\s*/);
  assert.match(app, /pct == null \? '\u2014' : `\$\{pct\}%`/);
  assert.match(app, /style="width:\$\{pct == null \? 0 : pct\}%"/);
  const footerSource = app.slice(paintStart, renderStart);
  assert.match(footerSource, /data-limit-id/);
  assert.match(footerSource, /usage-label/);
  assert.match(footerSource, /usage-bar/);
  assert.match(footerSource, /usage-resets/);
  assert.doesNotMatch(footerSource, /As of:|Last attempt:|fresh|stale|error|Upstream|Probed|usage-freshness|aria-label/i);
  assert.doesNotMatch(app, /limitsFreshnessTimer|renderedLimitsHealth|claudeUsageFreshnessState|claudeHealthErrorLabel|usageStamp/);
  assert.doesNotMatch(css, /\.usage-freshness\b/);
});

test('usage footer renders all three percent cards and ignores health presentation fields', () => {
  const app = read('renderer/app.js');
  const paintStart = app.indexOf('function paintLimits(limits)');
  const renderStart = app.indexOf('function renderLimits(limits, health)', paintStart);
  const footerSource = app.slice(paintStart, renderStart);
  const footer = { innerHTML: '' };
  const usageSection = { style: {} };
  const elements = {
    'usage-section': usageSection,
    'limits-footer': footer,
  };
  const paintLimits = vm.runInNewContext(`(${footerSource.trim()})`, {
    document: {
      getElementById(id) {
        return elements[id] || null;
      },
    },
    esc(value) {
      return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    },
    usageBarClass() {
      return 'usage-bar-fill-normal';
    },
    usageResetText(value) {
      return String(value);
    },
  });

  paintLimits([
    { id: 'claude', label: 'Claude', pct: 37, resets_text: 'Claude reset', upstream_reported_at: '2026-08-28T15:00:00Z', probed_at: '2026-08-28T15:00:01Z' },
    { id: 'fable', label: 'Fable', pct: 53, resets_text: 'Fable reset' },
    { id: 'codex', label: 'Codex', pct: 20, resets_text: 'Codex reset', upstream_reported_at: '2026-08-28T15:00:02Z', probed_at: '2026-08-28T15:00:03Z' },
  ]);

  assert.equal(usageSection.style.display, '');
  assert.equal((footer.innerHTML.match(/class="usage-compact-item"/g) || []).length, 3);
  for (const [id, label, pct, reset] of [
    ['claude', 'Claude', 37, 'Claude reset'],
    ['fable', 'Fable', 53, 'Fable reset'],
    ['codex', 'Codex', 20, 'Codex reset'],
  ]) {
    assert.match(footer.innerHTML, new RegExp(`data-limit-id="${id}"`));
    assert.match(footer.innerHTML, new RegExp(`<span>${label}</span>\\s*<span>${pct}%</span>`));
    assert.match(footer.innerHTML, new RegExp(`width:${pct}%`));
    assert.match(footer.innerHTML, new RegExp(reset));
  }
  assert.doesNotMatch(footer.innerHTML, /As of:|Last attempt:|fresh|stale|error|Upstream|Probed|usage-freshness|aria-label/i);
});

test('limits health is validated and shown as text without a polling timer', () => {
  const app = read('renderer/app.js');

  assert.match(app, /function renderLimits\(limits, health\)/);
  assert.match(app, /limits_health/);
  const renderStart = app.indexOf('function renderLimits(limits, health)');
  const machineStatsStart = app.indexOf('// ── Machine Stats Footer', renderStart);
  const renderSource = app.slice(renderStart, machineStatsStart);
  assert.match(renderSource, /validatedLimitsHealth/);
  assert.match(renderSource, /banner.textContent/);
  assert.doesNotMatch(renderSource, /setInterval|fresh|stale|As of|Last attempt|Upstream|Probed|aria-label/i);
  assert.doesNotMatch(app, /limitsFreshnessTimer|renderedLimitsHealth/);
});

test('limits panel has a single source: the daemon frame, no local IPC path', () => {
  const main = read('main.js');
  const preload = read('preload.js');
  const app = read('renderer/app.js');

  // The local IPC limits path is deleted; the daemon hello/limits.update frame is the only source.
  assert.doesNotMatch(main, /limits:get|limits-state:get|getLimitsData|getLimitsStateData/);
  assert.doesNotMatch(preload, /limits:get|limits-state:get|getLimits\b|getLimitsState/);
  assert.doesNotMatch(app, /getLimitsState|getLimitsData|fetchUsage/);
  // The panel never repolls or clears itself on an interval; it repaints from daemon frames only.
  assert.match(app, /renderLimits\(\s*payload\.limits,\s*/);
});

test('desktop reskin preserves bridge colors for filters, avatars, stats, and source tags', () => {
  const css = read('renderer/styles.css');
  const app = read('renderer/app.js');

  for (const token of ['--bridge-red: #f47067', '--bridge-royal-blue: #1d4ed8', '--bridge-forest-green: #166534', '--bridge-orange: #f0883e']) {
    assert.match(css, new RegExp(token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  }
  for (const klass of ['color-red', 'color-royal-blue', 'color-forest-green', 'color-orange']) {
    assert.match(css, new RegExp(`source-filter-btn\\.${klass}`));
    assert.match(css, new RegExp(`s-machine-avatar\\.${klass}`));
    assert.match(css, new RegExp(`machine-stat-card\\.${klass}`));
    assert.match(css, new RegExp(`s-source-tag\\.${klass}`));
  }
  assert.match(css, /\.s-machine-avatar\.color-royal-blue \{ color: #fff; border-color: var\(--bridge-royal-blue\); background: var\(--bridge-royal-blue\); \}/);
  assert.doesNotMatch(css, /s-machine-avatar\.color-royal-blue[^{]*\{[^}]*color-mix/);
  assert.deepEqual(require('../renderer/host_presentation').PALETTE, ['forest-green', 'royal-blue', 'red', 'orange']);
  assert.match(app, /s-machine-avatar color-\$\{machineColor\}/);
  assert.doesNotMatch(app, /s-machine-label color-\$\{machineColor\}/);
  assert.match(app, /configuredHostIds\.length \? configuredHostIds : visibleHostIds/);
  assert.doesNotMatch(app, /hostIds\.length < 2/);
});

test('desktop reskin keeps general chrome on cosmic tokens, not bridge colors', () => {
  const css = read('renderer/styles.css');
  const chatUi = read('renderer/chat_ui_state.js');

  assert.match(css, /\.sb-btn-new[\s\S]*background: var\(--pc-chip-bg\)/);
  assert.match(css, /\.sidebar-search-row[\s\S]*display: flex/);
  assert.match(css, /#usage-section \.sidebar-section-body[\s\S]*grid-template-columns: 1fr 1fr/);
  assert.match(css, /\.usage-compact-item/);
  assert.match(css, /\.sidebar[\s\S]*background: var\(--pc-side\)/);
  assert.match(css, /\.session-item \.s-name[\s\S]*color: var\(--pc-text\)/);
  assert.match(css, /\.slot-chat-compose-send[\s\S]*border-radius: 50%/);
  assert.match(chatUi, /surface: 'var\(--pc-chip-bg\)'/);
  assert.match(chatUi, /border: 'var\(--pc-line\)'/);
  assert.doesNotMatch(chatUi, /#ff7ab8|#4da3ff|#ff4d5e|#8f3d68|#2f6ca5|#a83242/);
});

test('sidebar working rows render spinner only and chat retains elapsed timer', () => {
  const app = read('renderer/app.js');
  const css = read('renderer/styles.css');
  const cosmicCss = read('renderer/cosmic_chat_surface.css');
  const cosmicComponents = read('renderer/src/cosmic_components.ts');

  assert.match(app, /class="activity-indicator working" aria-label="In progress"/);
  assert.doesNotMatch(app, /class="s-working-timer" data-sidebar-working-name=/);
  assert.doesNotMatch(app, /\.s-working-timer\[data-sidebar-working-name\]/);
  assert.match(app, /timerEl\.textContent = chatUi\.formatElapsed/);
  assert.doesNotMatch(app, /background terminal/);
  assert.doesNotMatch(app, /\/ps to view/);
  assert.doesNotMatch(css, /\.s-working-timer/);
  assert.match(css, /\.slot-chat-status-timer[\s\S]*font-variant-numeric: tabular-nums/);
  assert.match(css, /\.slot-chat-status-timer[\s\S]*min-width: 8ch/);
  assert.match(css, /\.slot-chat-status-timer[\s\S]*text-align: right/);
  assert.match(css, /\.slot-chat-status-badge[\s\S]*width: max-content/);
  assert.match(css, /\.slot-chat-status-badge[\s\S]*min-width: max-content/);
  assert.match(css, /\.slot-chat-status-badge\.is-working \.slot-chat-status-dot[\s\S]*background: transparent/);
  assert.match(css, /\.slot-chat-status-badge\.is-working \.slot-chat-status-dot[\s\S]*width: 14px/);
  assert.match(css, /\.activity-spinner[\s\S]*animation: activity-spin calc\(var\(--activity-spinner-period-ms, 1050\) \* 1ms\) linear infinite/);
  assert.match(cosmicCss, /\.cosmic \.slot-chat-status-badge\.is-working \.slot-chat-status-dot[\s\S]*background: transparent/);
  assert.match(cosmicComponents, /attrs: \{ class: 'activity-spinner' \}/);
  assert.match(app, /const ACTIVITY_SPINNER_PERIOD_MS = 1050/);
  assert.match(app, /function syncActivitySpinnerPhase\(root = document\)/);
  assert.match(app, /if \(spinner\.style\.animationDelay\) return/);
  assert.match(app, /animationDelay = `-\$\{phaseMs\}ms`/);
  assert.match(app, /syncActivitySpinnerPhase\(document\)/);
});

test('D19 regression: chat composer sits close to the slot bottom edge', () => {
  const css = read('renderer/styles.css');

  assert.match(css, /\.slot-chat-shell[\s\S]*padding: 10px 12px 4px/);
  assert.match(css, /\.slot-chat-shell[\s\S]*gap: 8px/);
  assert.match(css, /\.slot-chat-compose[\s\S]*padding-top: 7px/);
});

test('machine stats chrome projects the daemon fleet without a local collector toggle', () => {
  const html = read('renderer/index.html');
  const app = read('renderer/app.js');
  const css = read('renderer/styles.css');

  assert.doesNotMatch(app, /All Machines/);
  assert.doesNotMatch(app, />Current</);
  assert.doesNotMatch(html, /machine-stats-header-action/);
  assert.match(app, /function renderHostsStats\(hosts = state\.chatStream\.hostsStats\)/);
  assert.match(app, /data-machine-stats-host/);
  assert.match(app, /sampled_at/);
  assert.doesNotMatch(app, /fetchMachineStats|getMachineStats|setInterval\(fetchMachineStats/);
  assert.doesNotMatch(html, /machine-stats-toggle/);
  assert.doesNotMatch(app, /machine-stats-toggle/);
  assert.doesNotMatch(css, /machine-stats-toggle/);
  assert.doesNotMatch(app, /class="machine-stats-title"/);
  assert.match(css, /\.sidebar-section-header[\s\S]*padding: 6px 12px/);
  assert.match(css, /#usage-section \.sidebar-section-body[\s\S]*gap: 7px 12px/);
  assert.match(css, /#usage-section \.sidebar-section-body[\s\S]*padding: 8px 12px 9px/);
  assert.match(css, /#machine-stats-section \.sidebar-section-body[\s\S]*padding: 0 12px 9px/);
  assert.match(css, /\.machine-stat-card[\s\S]*margin-top: 4px/);
});

