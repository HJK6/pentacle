const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const app = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'index.html'), 'utf8');

test('manual spawn UI uses catalog-backed Codex Sol high factory defaults and versioned preferences', () => {
  assert.match(app, /SPAWN_PREFERENCES_KEY = 'pentacle\.spawnPreferences\.v1'/);
  assert.match(app, /defaultProvider: 'codex'/);
  assert.match(app, /catalog\.profiles\?\.desktop_manual/);
  assert.match(app, /chatSpawnCatalog/);
});

function extractFunction(name) {
  const src = app.match(new RegExp(`function ${name}\\([^]*?\\n}`));
  assert.ok(src, `${name} function found in renderer/app.js`);
  return src[0];
}

function loadModelLabel() {
  return new Function(`${extractFunction('modelLabel')}; return modelLabel;`)();
}

test('modelLabel uses the explicit desktop catalog map and raw fallback', () => {
  const src = app.match(/function modelLabel\(model\) \{[\s\S]*?\n\}/);
  assert.ok(src, 'modelLabel function found in renderer/app.js');
  const modelLabel = loadModelLabel();
  assert.equal(modelLabel('claude-opus-4-8'), 'Opus 4.8');
  assert.equal(modelLabel('claude-opus-5'), 'Opus 5');
  assert.equal(modelLabel('claude-sonnet-5'), 'Sonnet 5');
  assert.equal(modelLabel('claude-fable-5'), 'Fable 5');
  assert.equal(modelLabel('claude-fable-5-1'), 'Fable 5.1');
  assert.equal(modelLabel('gpt-5.6-sol'), '5.6 Sol');
  assert.equal(modelLabel('gpt-5.6-terra'), '5.6 Terra');
  assert.equal(modelLabel('gpt-6-astra'), '6 Astra');
  assert.equal(modelLabel('claude-opus-6'), 'claude-opus-6');
  assert.equal(modelLabel('gpt-'), 'gpt-');
  assert.equal(modelLabel(''), '');
});

function renderProfileForTest(selection, models, emissions, catalogOverride = null, providerSelections = []) {
  const dom = new JSDOM('<main><h1 id="new-session-title"></h1><p id="new-session-subtitle"></p><div id="profile"></div></main>');
  const catalog = catalogOverride || {
    profiles: { desktop_manual: { [selection.provider]: [selection.model, selection.effort] } },
    models: { [selection.provider]: models },
  };
  const renderSpawnProfileOptions = new Function(
    'document', 'esc', 'modelLabel', 'modelFamily', 'updateNewSessionStatus',
    'catalogTuple', 'catalogProviders', 'providerLabelForHero', 'saveSpawnPreference',
    'selectionForProvider', 'renderNewSessionModal',
    'newSessionSelection', 'newSessionCatalog', 'newSessionError',
    `${extractFunction('renderSpawnProfileOptions')}; return renderSpawnProfileOptions;`,
  )(
    dom.window.document,
    String,
    loadModelLabel(),
    (model) => (String(model).startsWith('gpt-') ? 'GPT' : 'Opus'),
    () => {},
    (provider, candidate) => {
      emissions.push({ provider, model: candidate.model });
      return { provider, model: candidate.model, effort: candidate.effort };
    },
    (value) => Object.keys(value.profiles?.desktop_manual || {}).filter((provider) => {
      const tuple = value.profiles.desktop_manual[provider];
      return value.models?.[provider]?.[tuple?.[0]]?.efforts?.includes(tuple?.[1]);
    }),
    (provider) => (provider === 'claude' ? 'Claude' : provider === 'codex' ? 'Codex' : provider),
    () => {},
    (provider) => {
      providerSelections.push(provider);
      const tuple = catalog.profiles.desktop_manual[provider];
      return { provider, model: tuple[0], effort: tuple[1] };
    },
    () => {},
    selection,
    catalog,
    '',
  );
  const container = dom.window.document.getElementById('profile');
  renderSpawnProfileOptions(container);
  return { dom, container };
}

test('spawn picker displays mapped labels while retaining raw option values and emitted selections', () => {
  const claudeEmissions = [];
  const claude = renderProfileForTest(
    { provider: 'claude', model: 'claude-opus-4-8', effort: 'high' },
    { 'claude-opus-4-8': { efforts: ['high'] }, 'claude-fable-5': { efforts: ['high'] } },
    claudeEmissions,
  );
  const claudeOptions = [...claude.container.querySelectorAll('#spawn-model option')];
  assert.deepEqual(claudeOptions.map((option) => [option.value, option.textContent]), [
    ['claude-opus-4-8', 'Opus 4.8'],
    ['claude-fable-5', 'Fable 5'],
  ]);
  const claudeSelect = claude.container.querySelector('#spawn-model');
  claudeSelect.value = 'claude-fable-5';
  claudeSelect.dispatchEvent(new claude.dom.window.Event('change'));
  assert.deepEqual(claudeEmissions, [{ provider: 'claude', model: 'claude-fable-5' }]);

  const codexEmissions = [];
  const codex = renderProfileForTest(
    { provider: 'codex', model: 'gpt-5.6-sol', effort: 'high' },
    { 'gpt-5.6-sol': { efforts: ['high'] }, 'gpt-5.6-terra': { efforts: ['high'] } },
    codexEmissions,
  );
  const codexOptions = [...codex.container.querySelectorAll('#spawn-model option')];
  assert.deepEqual(codexOptions.map((option) => [option.value, option.textContent]), [
    ['gpt-5.6-sol', '5.6 Sol'],
    ['gpt-5.6-terra', '5.6 Terra'],
  ]);
  const codexSelect = codex.container.querySelector('#spawn-model');
  codexSelect.value = 'gpt-5.6-terra';
  codexSelect.dispatchEvent(new codex.dom.window.Event('change'));
  assert.deepEqual(codexEmissions, [{ provider: 'codex', model: 'gpt-5.6-terra' }]);
});

test('spawn picker enumerates catalog-backed providers and exposes Claude/Fable without fabricating providers', () => {
  const providerSelections = [];
  const catalog = {
    profiles: {
      desktop_manual: {
        claude: ['claude-opus-4-8', 'high'],
        codex: ['gpt-5.6-sol', 'high'],
        unsupported: ['missing-model', 'high'],
      },
    },
    models: {
      claude: {
        'claude-opus-4-8': { efforts: ['high'] },
        'claude-fable-5': { efforts: ['low', 'medium', 'high', 'xhigh', 'max'] },
      },
      codex: { 'gpt-5.6-sol': { efforts: ['high'] } },
      unsupported: { 'different-model': { efforts: ['high'] } },
    },
  };
  const picker = renderProfileForTest(
    { provider: 'codex', model: 'gpt-5.6-sol', effort: 'high' },
    catalog.models.codex,
    [],
    catalog,
    providerSelections,
  );
  const provider = picker.container.querySelector('#spawn-provider');
  assert.equal(provider.tagName, 'SELECT');
  assert.deepEqual([...provider.options].map((option) => [option.value, option.textContent]), [
    ['claude', 'Claude'],
    ['codex', 'Codex'],
  ]);
  provider.value = 'claude';
  provider.dispatchEvent(new picker.dom.window.Event('change'));
  assert.deepEqual(providerSelections, ['claude']);
  assert.ok(catalog.models.claude['claude-fable-5'].efforts.includes('high'));
});

test('spawn picker stays catalog-driven — model list and efforts derive from the fetched catalog', () => {
  // Drift protection (AC-3) is the catalog-driven fetch itself: models come from spawn_catalog_get,
  // never a client-side literal. Positively assert the derivation chain so a regression that
  // reintroduces a hardcoded list (in any order) fails: catalog ← IPC response, model options ←
  // Object.keys(entries), efforts ← the selected catalog entry.
  assert.match(app, /newSessionCatalog = response\.catalog/);
  assert.match(app, /const entries = newSessionCatalog\.models\?\.\[selection\.provider\]/);
  assert.match(app, /const models = Object\.keys\(entries\)/);
  assert.match(app, /const efforts = entries\[selection\.model\]\?\.efforts/);
});

test('manual spawn UI exposes four labelled native controls and modal live status', () => {
  for (const label of ['Provider', 'Model', 'Version / variant', 'Effort']) assert.match(app, new RegExp(label));
  assert.match(app, /id="spawn-provider"/);
  assert.match(app, /<select id="spawn-provider"/);
  assert.match(app, /id="spawn-model"/);
  assert.match(app, /id="spawn-effort"/);
  assert.match(html, /role="dialog"/);
  assert.match(html, /role="status" aria-live="polite"/);
});

test('manual spawn preserves errors in the modal and never dismisses on background click', () => {
  assert.match(app, /newSessionError = result\?\.error\?\.message/);
  assert.match(app, /Background clicks never dismiss/);
  assert.match(app, /newSessionSubmitting/);
});

test('spawn modal is always escapable — no close/nav path gates on submit state', () => {
  // Cancel button is never disabled.
  assert.match(app, /Cancel is NEVER disabled/);
  assert.match(app, /if \(cancel\) cancel\.disabled = false;/);
  // hideNewSessionModal always closes (no submit early-return) and aborts submit.
  assert.doesNotMatch(app, /function hideNewSessionModal\(\) \{\s*if \(newSessionSubmitting\) return;/);
  assert.match(app, /function hideNewSessionModal\(\) \{[\s\S]*?abortNewSessionSubmit\(\);/);
  // ESC no longer guards on !newSessionSubmitting.
  assert.doesNotMatch(app, /event\.key === 'Escape' && !newSessionSubmitting/);
  assert.match(app, /if \(event\.key === 'Escape'\) \{/);
  // Back aborts any in-flight submit before navigating.
  assert.match(app, /Back must work even mid-spawn[\s\S]*?abortNewSessionSubmit\(\);/);
});

test('spawn stays escapable while the authoritative result remains pending', () => {
  // Submitting flag is cleared in finally regardless of resolve/reject.
  assert.match(app, /\} finally \{\s*\/\/ Only this submit clears the flag[\s\S]*?newSessionSubmitting = false;/);
  // The old ceiling becomes an honest wait notice, not a false terminal error.
  assert.match(app, /NEW_SESSION_LONG_WAIT_MS/);
  assert.match(app, /waitForNewSessionSpawn\(/);
  assert.match(app, /Still waiting for startup to finish/);
  assert.doesNotMatch(app, /Spawn timed out — the machine did not respond/);
  // Abort token: a late spawn resolving after an operator escape is abandoned.
  assert.match(app, /let newSessionSubmitToken = 0;/);
  assert.match(app, /const myToken = \+\+newSessionSubmitToken;/);
  assert.match(app, /if \(myToken !== newSessionSubmitToken\) return null;/);
  assert.match(app, /function abortNewSessionSubmit\(\) \{\s*newSessionSubmitToken\+\+;\s*newSessionSubmitting = false;/);
  // Double-submit guard.
  assert.match(app, /if \(newSessionSubmitting\) return null; \/\/ guard against overlapping spawns/);
});

test('queued boot admission closes the modal without a client lifecycle row', () => {
  assert.doesNotMatch(app, /\['queued', 'starting'\]\.includes\(lifecycleState\)/);
  assert.match(app, /if \(!result\.session\) \{\s*hideNewSessionModal\(\);\s*return null;/);
  assert.doesNotMatch(app, /awaitQueuedNewSession|chatAwaitSpawn|NEW_SESSION_QUEUE_POLL_MS/);
  assert.doesNotMatch(app, /newSessionQueued/);
});

function spawnWaitHarness() {
  let timer = null;
  const setTimeout = (fn, waitMs) => {
    timer = { fn, waitMs, cleared: false };
    return timer;
  };
  const clearTimeout = (candidate) => { candidate.cleared = true; };
  const waitForNewSessionSpawn = new Function(
    'setTimeout',
    'clearTimeout',
    `${extractFunction('waitForNewSessionSpawn')}; return waitForNewSessionSpawn;`,
  )(setTimeout, clearTimeout);
  return {
    waitForNewSessionSpawn,
    timer: () => timer,
    fire: () => { if (timer && !timer.cleared) timer.fn(); },
  };
}

test('slow spawn shows one non-terminal wait notice and preserves eventual success', async () => {
  const harness = spawnWaitHarness();
  let resolveSpawn;
  const spawn = new Promise((resolve) => { resolveSpawn = resolve; });
  let notices = 0;
  const outcome = harness.waitForNewSessionSpawn(spawn, () => { notices++; }, 30000);

  assert.equal(harness.timer().waitMs, 30000);
  harness.fire();
  assert.equal(notices, 1);
  resolveSpawn({ ok: true, session: { sessionName: 'codex-1' } });
  assert.deepEqual(await outcome, { ok: true, session: { sessionName: 'codex-1' } });
  assert.equal(harness.timer().cleared, true);
});

test('late spawn error remains a real rejection after the wait notice', async () => {
  const harness = spawnWaitHarness();
  let rejectSpawn;
  const spawn = new Promise((_, reject) => { rejectSpawn = reject; });
  let notices = 0;
  const outcome = harness.waitForNewSessionSpawn(spawn, () => { notices++; }, 30000);

  harness.fire();
  assert.equal(notices, 1);
  rejectSpawn(new Error('attestation failed'));
  await assert.rejects(outcome, /attestation failed/);
  assert.equal(harness.timer().cleared, true);
});

test('fast spawn clears the wait notice without firing it', async () => {
  const harness = spawnWaitHarness();
  let notices = 0;
  const outcome = await harness.waitForNewSessionSpawn(
    Promise.resolve({ ok: true, session: { sessionName: 'claude-1' } }),
    () => { notices++; },
    30000,
  );

  harness.fire();
  assert.equal(notices, 0);
  assert.equal(harness.timer().cleared, true);
  assert.equal(outcome.session.sessionName, 'claude-1');
});

test('post-spawn uses selection.provider, not a bare undeclared agent identifier', () => {
  assert.match(app, /const agentConfig = CONFIG\.agents\[selection\.provider\]/);
  assert.match(app, /emit\?\.\('session:spawn', \{[\s\S]*?provider: selection\.provider,[\s\S]*?kind: 'chat'/);
  // The chat-spawn path must not reference a bare `agent` (that identifier is
  // only a real parameter of newTerminalSession).
  assert.doesNotMatch(app, /CONFIG\.agents\[agent\]/);
});

test('manual spawn leaves failure notification to the daemon terminal frame', () => {
  const source = app.slice(app.indexOf('async function newSession('), app.indexOf('\nasync function newTerminalSession('));
  assert.match(source, /newSessionError = result\?\.error\?\.message \|\| result\?\.error/);
  assert.doesNotMatch(source, /showToast/);
  assert.doesNotMatch(app, /aria-label="Starting"/);
});

test('a remembered non-default tuple is sent as an explicit override for daemon validation', () => {
  assert.match(app, /selection\.model === profileDefault\[0\]/);
  assert.match(app, /'explicit_override'/);
});

test('preference writes initialize both providers and reconcile by revision plus writer', () => {
  assert.match(app, /writerId: SPAWN_PREFERENCES_WRITER_ID/);
  assert.match(app, /claude: storedTuple\('claude'/);
  assert.match(app, /codex: storedTuple\('codex'/);
  assert.match(app, /window\.addEventListener\('storage'/);
  assert.match(app, /preferenceIsNewer/);
});

test('modal traps focus and restores an inert accessibility background', () => {
  assert.match(app, /setNewSessionBackgroundInert\(true\)/);
  assert.match(app, /setNewSessionBackgroundInert\(false\)/);
  assert.match(app, /child\.inert = true/);
  assert.match(app, /event\.key !== 'Tab'/);
  assert.match(app, /spawnAck: \{ requested:/);
});
