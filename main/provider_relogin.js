'use strict';

const os = require('node:os');
const { randomUUID } = require('node:crypto');
const { stripVTControlCharacters } = require('node:util');
const { quote, executionHost, executionCommand, executionHosts } = require('./execution_host');

const BUG_REF = 'spec_pentacle__provider_relogin_buttons_2026_09';
const MAX_OUTPUT = 65536;
const PROVIDERS = {
  codex: { login: ['codex', 'login'], verify: ['codex', 'login', 'status'], marker: 'Successfully logged in',
    origins: ['https://auth.openai.com'], browser: '/bin/true' },
  claude: { login: ['claude', '--debug-file', '/dev/null', 'auth', 'login', '--claudeai'],
    verify: ['claude', '--debug-file', '/dev/null', 'auth', 'status', '--json'], marker: 'Login successful',
    origins: ['https://claude.ai', 'https://console.anthropic.com', 'https://platform.claude.com'], browser: '/bin/echo' },
};

function terminalText(text) {
  // ConPTY titles can contain drive paths which Node's VT stripper only
  // partially removes. OSC payloads are metadata, never login output. Strip
  // their complete bodies first, including an unfinished tail. Each call
  // receives the accumulated raw buffer, so split terminators stay intact.
  return stripVTControlCharacters(text.replace(/(?:\x1b\]|\x9d)[\s\S]*?(?:\x07|\x1b\\|\x9c|$)/g, ''));
}

function authorizationUrl(text, provider) {
  // Only complete output lines: a chunk boundary can split a query parameter.
  const lines = text.split('\n').slice(0, -1);
  for (const line of lines) {
    const raw = line.trim();
    if (!raw.startsWith('https://') || raw.length > 16384 || /\s/.test(raw)) continue;
    try {
      const url = new URL(raw);
      if (PROVIDERS[provider].origins.includes(url.origin) && url.pathname === '/oauth/authorize'
        && !url.username && !url.password && !url.hash && url.searchParams.get('state')
        && url.searchParams.get('client_id') && url.searchParams.get('code_challenge')) return raw;
    } catch { /* untrusted output is never reflected as an error */ }
  }
  return null;
}

function verified(text, provider) {
  if (provider === 'codex') return text.split(/\r?\n/).some(line => line.trim() === 'Logged in using ChatGPT');
  try {
    const start = text.indexOf('{'), end = text.lastIndexOf('}');
    const value = JSON.parse(text.slice(start, end + 1));
    return value.loggedIn === true && value.authMethod === 'claude.ai' && value.apiProvider === 'firstParty';
  } catch { return false; }
}

function loginCommand(host, provider, phase, nonce, platform) {
  const spec = PROVIDERS[provider];
  // The wrapper waits for the foreground child before acknowledging its exit.
  // Ignoring INT in the wrapper lets Ctrl-C stop the child and still produces
  // that receipt. A proxy exit without this target receipt is NOT clean cancel.
  const script = `set +x; unset DEBUG CLAUDE_DEBUG CLAUDE_CODE_DEBUG; `
    + `export RUST_LOG=off DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 BROWSER=${quote(spec.browser)}; `
    + `stty -echo || exit 125; trap ':' INT; ${spec[phase].map(quote).join(' ')}; relogin_rc=$?; `
    + `printf '\\n__PENTACLE_RELOGIN_${nonce}:%s\\n' "$relogin_rc"; exit "$relogin_rc"`;
  return executionCommand(host, ['/bin/bash', '-lc', script], platform);
}

function registerProviderRelogin(target, config, {
  pty, platform = process.platform, env = process.env, cwd = os.homedir(),
  timeoutMs = 300000, verifyTimeoutMs = 15000, cleanupMs = 3000,
  log = value => console.info(JSON.stringify(value)),
} = {}) {
  const attempts = new Map();
  const reservations = new Map();
  const hooked = new WeakSet();
  let shuttingDown = false;

  function emit(record, state, reason = null, url = null) {
    record.state = state;
    const event = { id: record.id, host: record.host.id, provider: record.provider, state, reason };
    // Never pass an exception, raw output, URL, code or account identity to log.
    try { log({ subsystem: 'provider_relogin', bug_ref: BUG_REF,
      host: record.host.id, provider: record.provider, state, reason }); } catch {}
    try { if (!record.sender.isDestroyed()) record.sender.send('provider-relogin:state', { ...event, ...(url ? { url } : {}) }); } catch {}
  }

  function terminal(record, state, reason, clean) {
    clearTimeout(record.timer);
    clearTimeout(record.cleanupTimer);
    record.buffer = '';
    record.url = null;
    record.process = null;
    record.done = true;
    if (clean) {
      if (attempts.get(record.sender.id) === record) attempts.delete(record.sender.id);
      if (reservations.get(record.host.key) === record) reservations.delete(record.host.key);
    }
    emit(record, state, reason);
    record.resolve?.({ ok: clean, state });
  }

  function stop(record, state = 'cancelled', reason = 'operator_cancel') {
    if (!record || record.done) return Promise.resolve({ ok: !record || record.state !== 'cleanup_failed' });
    if (record.stopping) return record.completion;
    record.stopping = { state, reason };
    clearTimeout(record.timer);
    record.url = null;
    emit(record, 'stopping', reason);
    try { record.process?.write('\x03'); } catch {}
    // Install before returning; callback identity guards make late exits inert.
    if (!record.done) record.cleanupTimer = setTimeout(() => {
      const proc = record.process;
      terminal(record, 'cleanup_failed', 'target_exit_unconfirmed', false);
      try { proc?.kill(); } catch {}
    }, cleanupMs);
    return record.completion;
  }

  function launch(record, phase) {
    const nonce = randomUUID().replaceAll('-', '');
    const marker = `__PENTACLE_RELOGIN_${nonce}:`;
    record.buffer = '';
    record.url = null;
    record.phase = phase;
    const command = loginCommand(record.host, record.provider, phase, nonce, platform);
    let proc;
    try {
      proc = (pty || require('node-pty')).spawn(command.file, command.args, {
        name: 'dumb', cols: 4096, rows: 24, cwd,
        env: { ...env, TERM: 'dumb', NO_COLOR: '1' },
      });
    } catch {
      terminal(record, 'failed', 'process_start_failed', true);
      return;
    }
    record.process = proc;
    record.timer = setTimeout(() => stop(record, 'timed_out', phase === 'verify' ? 'verification_timeout' : 'login_timeout'),
      phase === 'verify' ? Math.min(verifyTimeoutMs, Math.max(1, record.deadline - Date.now())) : Math.max(1, record.deadline - Date.now()));
    proc.onData(data => {
      if (record.process !== proc || record.done) return;
      if (record.buffer.length + data.length > MAX_OUTPUT) {
        stop(record, 'failed', 'output_limit');
        return;
      }
      record.buffer += data;
      if (record.stopping || phase !== 'login') return;
      const text = terminalText(record.buffer);
      const url = authorizationUrl(text, record.provider);
      if (url && url !== record.url) {
        record.url = url;
        emit(record, 'awaiting_browser', null, url);
      }
    });
    proc.onExit(({ exitCode, signal }) => {
      if (record.process !== proc || record.done) return;
      clearTimeout(record.timer);
      clearTimeout(record.cleanupTimer);
      const text = terminalText(record.buffer);
      const match = text.match(new RegExp(`(?:^|\\n)${marker}(\\d+)\\r?(?:\\n|$)`));
      const clean = Boolean(match) && !signal && Number(match[1]) === exitCode;
      record.process = null;
      record.buffer = '';
      record.url = null;
      if (!clean) return terminal(record, 'cleanup_failed', 'target_exit_unconfirmed', false);
      if (record.stopping) return terminal(record, record.stopping.state, record.stopping.reason, true);
      if (exitCode !== 0) return terminal(record, 'failed', phase === 'verify' ? 'verification_failed' : 'login_failed', true);
      if (phase === 'verify') return terminal(record, verified(text.slice(0, match.index), record.provider) ? 'succeeded' : 'failed',
        verified(text.slice(0, match.index), record.provider) ? null : 'verification_failed', true);
      if (!text.includes(PROVIDERS[record.provider].marker)) return terminal(record, 'failed', 'login_not_confirmed', true);
      emit(record, 'verifying');
      launch(record, 'verify');
    });
  }

  function get(event, id) {
    const record = attempts.get(event.sender.id);
    return record && record.id === id ? record : null;
  }

  target.handle('provider-relogin:hosts', () => ({ ok: true, hosts: executionHosts(config) }));
  target.handle('provider-relogin:start', (event, request = {}) => {
    if (shuttingDown || event.sender.isDestroyed()) return { ok: false, reason: 'client_closed' };
    if (!request || !Object.hasOwn(PROVIDERS, request.provider) || typeof request.host !== 'string'
      || !/^[A-Za-z0-9_-]{16,80}$/.test(request.id || '') || request.available !== true)
      return { ok: false, reason: 'invalid_request' };
    let host;
    try { host = executionHost(config, request.host); } catch { return { ok: false, reason: 'host_unavailable' }; }
    if (attempts.has(event.sender.id) || reservations.has(host.key)) return { ok: false, reason: 'already_running' };
    const record = { id: request.id, host, provider: request.provider, sender: event.sender,
      deadline: Date.now() + timeoutMs, buffer: '', url: null, process: null, done: false };
    record.completion = new Promise(resolve => { record.resolve = resolve; });
    attempts.set(event.sender.id, record);
    reservations.set(host.key, record);
    if (!hooked.has(event.sender)) {
      hooked.add(event.sender);
      event.sender.once('destroyed', () => stop(attempts.get(event.sender.id), 'cancelled', 'client_closed'));
      // Electron reload keeps WebContents alive, so destroyed alone is not enough.
      event.sender.on?.('did-start-navigation', (_e, _url, inPlace, isMainFrame) => {
        if (!inPlace && isMainFrame) stop(attempts.get(event.sender.id), 'cancelled', 'client_closed');
      });
    }
    emit(record, 'starting');
    launch(record, 'login');
    return { ok: true, id: record.id };
  });
  target.handle('provider-relogin:code', (event, id, code) => {
    const record = get(event, id);
    if (!record || record.done || record.stopping || record.provider !== 'claude'
      || record.state !== 'awaiting_browser' || record.codeSubmitted || typeof code !== 'string'
      || !code.length || code.length > 2048 || /[\s\x00-\x1f\x7f]/.test(code)) return { ok: false, reason: 'code_unavailable' };
    record.codeSubmitted = true;
    try { record.process.write(code + '\r'); return { ok: true }; }
    catch { stop(record, 'failed', 'code_delivery_failed'); return { ok: false, reason: 'code_delivery_failed' }; }
  });
  target.handle('provider-relogin:cancel', (event, id) => stop(get(event, id)));

  return async function shutdown() {
    shuttingDown = true;
    return Promise.all([...attempts.values()].map(record => stop(record, 'cancelled', 'shutdown')));
  };
}

module.exports = { registerProviderRelogin, authorizationUrl, verified, loginCommand };
