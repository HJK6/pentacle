'use strict';

const { reloadDashboardPage } = require('./dashboard_scenario');

// Exercise the served renderer with catalog responses derived from the real
// fixture daemon. Only advertised visibility is varied; no provider is launched.
async function providerVisibility(ctx) {
  const { session, report, cdp, timeoutMs } = ctx;
  const original = await session.eval('window.cc.chatSpawnCatalog()', { awaitPromise: true });
  if (!original?.ok || !original.catalog?.models?.gemini) throw new Error('provider visibility requires the fixture catalog');
  const key = 'pentacle.spawnPreferences.v1';
  const saved = await session.eval(`localStorage.getItem('${key}')`);
  let script;
  try {
    for (const hidden of [['gemini'], ['codex'], ['claude', 'codex']]) {
      const catalog = { ...original.catalog, available_models: Object.fromEntries(
        Object.entries(original.catalog.models).filter(([provider]) => !hidden.includes(provider)),
      ) };
      const expected = Object.keys(catalog.available_models);
      const [firstModel, firstDefinition] = Object.entries(catalog.available_models[expected[0]])[0];
      const firstEffort = firstDefinition.efforts[0];
      const prefs = { schemaVersion: 1, revision: 1, defaultProvider: hidden[0], byProvider: {} };
      script = await session.send('Page.addScriptToEvaluateOnNewDocument', { source: `
        localStorage.setItem('${key}', ${JSON.stringify(JSON.stringify(prefs))});
        Object.defineProperty(window, 'cc', { configurable: true,
          get() { return this.__visibilityBridge; },
          set(value) { value.chatSpawnCatalog = async () => (${JSON.stringify({ ok: true, catalog })}); this.__visibilityBridge = value; }
        });
      ` });
      await reloadDashboardPage(ctx, { keepWorkspace: true });
      await session.eval("document.getElementById('btn-new').click(); document.querySelector('#new-session-location [data-loc]').click()");
      const deadline = Date.now() + timeoutMs;
      while (!await session.eval("!!document.querySelector('#spawn-provider option')")) {
        if (Date.now() > deadline) throw new Error('provider visibility picker did not load');
        await cdp.sleep(100);
      }
      const read = () => session.eval(`({
        providers: [...document.querySelectorAll('#spawn-provider option')].map(o => o.value),
        provider: document.getElementById('spawn-provider').value,
        model: document.getElementById('spawn-model').value,
        effort: document.getElementById('spawn-effort').value,
        enabled: !document.getElementById('new-session-spawn').disabled
      })`);
      let value = await read();
      report.ok(`New Chat hides ${hidden.join('/')} and selects an advertised default`,
        JSON.stringify(value.providers) === JSON.stringify(expected) && value.provider === expected[0]
          && value.model === firstModel && value.effort === firstEffort && value.enabled, value);
      await session.eval(`document.getElementById('new-session-cancel').click();
        window.dispatchEvent(new StorageEvent('storage', { key: '${key}', newValue: ${JSON.stringify(JSON.stringify({ ...prefs, revision: 3 }))} }));
        document.getElementById('btn-new').click(); document.querySelector('#new-session-location [data-loc]').click();`);
      value = await read();
      report.ok('a hidden storage preference cannot restore a hidden provider',
        value.provider === expected[0] && value.model === firstModel && value.effort === firstEffort, value);
      await session.eval("document.getElementById('new-session-cancel').click()");
      await session.send('Page.removeScriptToEvaluateOnNewDocument', { identifier: script.identifier });
      script = null;
    }
  } finally {
    if (script) await session.send('Page.removeScriptToEvaluateOnNewDocument', { identifier: script.identifier });
    await session.eval(saved === null ? `localStorage.removeItem('${key}')` : `localStorage.setItem('${key}', ${JSON.stringify(saved)})`);
    await reloadDashboardPage(ctx, { keepWorkspace: true });
  }
}

module.exports = { providerVisibility };
