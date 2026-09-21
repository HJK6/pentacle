'use strict';

const TERMINAL = new Set(['succeeded', 'cancelled', 'failed', 'timed_out', 'cleanup_failed']);
const LABELS = { claude: 'Claude', codex: 'Codex' };

function createProviderRelogin({ document, cc, parent, makeId = () => {
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  return [...bytes].map(b => b.toString(16).padStart(2, '0')).join('');
} }) {
  const section = document.createElement('section');
  section.className = 'provider-relogin';
  section.innerHTML = `
    <h4>Provider sign-in</h4>
    <label>Execution host <select data-relogin="host" aria-label="Sign-in execution host"></select></label>
    <div class="provider-relogin-actions">
      <button type="button" class="sb-btn" data-provider="claude">Re-login Claude</button>
      <button type="button" class="sb-btn" data-provider="codex">Re-login Codex</button>
    </div>
    <p data-relogin="load" role="status"></p>
    <div data-relogin="dialog" role="dialog" aria-label="Provider sign-in" hidden>
      <h4 data-relogin="title"></h4>
      <p data-relogin="warning"></p>
      <p data-relogin="browser-guide"></p>
      <label data-relogin="confirmation"><input type="checkbox" data-relogin="available"> This host is available for sign-in; no other sign-in attempt is running.</label>
      <div class="provider-relogin-actions">
        <button type="button" class="sb-btn sb-btn-blue" data-relogin="start">Start sign-in</button>
        <button type="button" class="sb-btn" data-relogin="cancel">Cancel</button>
      </div>
      <p data-relogin="status" role="status" aria-live="polite"></p>
      <div data-relogin="url-group" hidden>
        <label>Temporary sign-in URL <textarea data-relogin="url" readonly spellcheck="false" autocomplete="off" aria-label="Temporary sign-in URL"></textarea></label>
        <button type="button" class="sb-btn" data-relogin="open">Open on this device</button>
      </div>
      <form data-relogin="code-form" hidden autocomplete="off">
        <label>One-time code <input data-relogin="code" type="password" autocomplete="off" spellcheck="false" aria-label="One-time code"></label>
        <button class="sb-btn" type="submit">Submit code</button>
      </form>
    </div>`;
  parent.appendChild(section);
  const el = name => section.querySelector(`[data-relogin="${name}"]`);
  const buttons = [...section.querySelectorAll('[data-provider]')];
  let selection = null;
  let activeId = null;
  let busy = false;
  let generation = 0;

  function clearSecrets() {
    el('url').value = '';
    el('code').value = '';
    el('url-group').hidden = true;
    el('code-form').hidden = true;
  }
  function controls() {
    el('host').disabled = busy;
    for (const button of buttons) button.disabled = busy || !el('host').value;
    el('start').disabled = busy || !el('available').checked;
    el('available').disabled = busy;
  }
  async function refresh() {
    if (busy) return;
    const epoch = ++generation;
    try {
      const response = await cc.reloginHosts();
      if (epoch !== generation || busy) return;
      const previous = el('host').value;
      el('host').replaceChildren();
      for (const host of response?.hosts || []) {
        const option = document.createElement('option');
        option.value = host.id;
        option.textContent = host.label + (host.available ? '' : ' (unavailable)');
        option.disabled = !host.available;
        el('host').appendChild(option);
      }
      if ([...el('host').options].some(o => o.value === previous && !o.disabled)) el('host').value = previous;
      el('load').textContent = el('host').value ? '' : 'No execution host is available in this profile.';
    } catch {
      if (epoch !== generation) return;
      el('host').replaceChildren();
      el('load').textContent = 'Sign-in controls are unavailable. Reopen Settings after the connection returns.';
    }
    controls();
  }
  function choose(provider) {
    if (busy || !el('host').value) return;
    selection = { provider, host: el('host').value };
    activeId = null;
    clearSecrets();
    el('dialog').hidden = false;
    el('title').textContent = `${LABELS[provider]} sign-in on ${selection.host}`;
    el('warning').textContent = `Starting ${LABELS[provider]} sign-in on ${selection.host} may clear or replace its current login. Cancelling or failing can leave this host signed out. Cancellation does not restore the prior login.`;
    el('browser-guide').textContent = provider === 'codex'
      ? `Open the URL in a browser on ${selection.host}, using Windows for a WSL host. A browser on a different machine cannot reach this login's localhost callback. Nothing opens automatically.`
      : 'Open the temporary URL when ready, then enter the one-time code here. Nothing opens automatically.';
    el('available').checked = false;
    el('confirmation').hidden = false;
    el('start').hidden = false;
    el('cancel').textContent = 'Cancel';
    el('status').textContent = '';
    controls();
  }
  async function close() {
    ++generation;
    const id = activeId;
    activeId = null;
    selection = null;
    busy = false;
    clearSecrets();
    el('dialog').hidden = true;
    controls();
    if (id) { try { await cc.reloginCancel(id); } catch { /* connection cleanup owns termination */ } }
  }
  async function start() {
    if (!selection || busy || !el('available').checked) return;
    let id;
    try { id = makeId(); } catch { el('status').textContent = 'Sign-in could not start on this browser.'; return; }
    activeId = id;
    busy = true;
    controls();
    el('status').textContent = 'Starting sign-in…';
    try {
      const result = await cc.reloginStart({ ...selection, id, available: true });
      if (activeId !== id) return;
      if (!result?.ok) {
        activeId = null; busy = false;
        el('status').textContent = result?.reason === 'already_running'
          ? 'A sign-in attempt or unresolved cleanup already owns this host. Finish it before trying again.'
          : 'Sign-in could not start. Check the selected host and its provider CLI.';
        controls();
      }
    } catch {
      if (activeId !== id) return;
      activeId = null; busy = false; clearSecrets();
      el('status').textContent = 'Connection lost. The attempt is not confirmed; this host may be signed out.';
      controls();
    }
  }
  function state(value) {
    if (value?.state === 'disconnected') {
      const wasActive = busy;
      ++generation; activeId = null; busy = false; clearSecrets(); controls();
      if (wasActive) el('status').textContent = 'Connection lost. Cleanup is not confirmed; this host may be signed out.';
      return;
    }
    if (!value || value.id !== activeId) return;
    clearSecrets();
    const messages = {
      starting: 'Starting sign-in…', awaiting_browser: 'Waiting for you to complete sign-in.',
      verifying: 'Checking the provider login on the selected host…',
      stopping: 'Stopping sign-in and checking cleanup…', succeeded: 'Sign-in verified on the selected host.',
      cancelled: 'Sign-in cancelled. The prior login was not restored; this host may be signed out.',
      timed_out: 'Sign-in timed out. The prior login was not restored; this host may be signed out.',
      failed: 'Sign-in was not verified. The prior login was not restored; this host may be signed out.',
      cleanup_failed: 'Could not confirm the provider stopped. Further attempts are blocked until cleanup is resolved on that host.',
    };
    el('status').textContent = messages[value.state] || 'Sign-in state is unavailable.';
    if (value.state === 'awaiting_browser' && typeof value.url === 'string') {
      el('url').value = value.url;
      el('url-group').hidden = false;
      el('code-form').hidden = selection?.provider !== 'claude';
    }
    if (TERMINAL.has(value.state)) {
      activeId = null; busy = false;
      el('start').hidden = true;
      el('confirmation').hidden = true;
      el('cancel').textContent = 'Done';
      controls();
    }
  }
  for (const button of buttons) button.addEventListener('click', () => choose(button.dataset.provider));
  el('host').addEventListener('change', () => { if (selection && !busy) choose(selection.provider); });
  el('available').addEventListener('change', controls);
  el('start').addEventListener('click', start);
  el('cancel').addEventListener('click', async () => {
    if (!activeId) return close();
    clearSecrets();
    el('status').textContent = 'Stopping sign-in and checking cleanup…';
    try { await cc.reloginCancel(activeId); } catch { state({ state: 'disconnected' }); }
  });
  el('open').addEventListener('click', async () => {
    const url = el('url').value;
    if (!activeId || !url) return;
    try { await cc.openExternal(url); }
    catch { el('status').textContent = 'The browser could not open. Open the displayed URL on the execution host.'; }
  });
  el('code-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!activeId) return;
    const id = activeId;
    const code = el('code').value.trim();
    el('code').value = '';
    el('code-form').hidden = true;
    try {
      const result = await cc.reloginCode(id, code);
      if (id === activeId) el('status').textContent = result?.ok
        ? 'Code submitted. Waiting for provider verification…' : 'Code could not be submitted. Cancel and start a new attempt.';
    } catch { if (id === activeId) state({ state: 'disconnected' }); }
  });
  cc.onReloginState(state);
  document.defaultView?.addEventListener('beforeunload', close);
  controls();
  return { refresh, close, element: section };
}

module.exports = { createProviderRelogin };
