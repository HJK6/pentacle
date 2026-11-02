// CDP client for the desktop E2E walk harness.
// spec_pentacle_desktop_chat_ui_e2e_walk_harness_2026_05_25
//
// Connects to a real Electron app over the Chrome DevTools Protocol (launched
// with --remote-debugging-port) and provides the primitives walks need:
//   - connect to a page target (by url/title match) + raw send(method, params)
//   - Runtime.evaluate (read state/DOM, dispatch real DOM clicks/typing)
//   - Page.captureScreenshot (render to PNG; NO macOS Screen Recording needed)
//   - an ORDERED [HARNESS] beacon stream captured via Runtime.consoleAPICalled,
//     with awaitBeacon / beaconsSince for positive + negative-window assertions
//   - multi-target listing (so a later walk can attach to the meeting window).
//
// Runs on plain Node using the repo's bundled `ws`. No build step.

const http = require('http');
const WebSocket = require('ws');

function httpJson(port, path) {
  return new Promise((resolve, reject) => {
    http
      .get({ host: '127.0.0.1', port, path }, (r) => {
        let d = '';
        r.on('data', (c) => (d += c));
        r.on('end', () => {
          try {
            resolve(JSON.parse(d));
          } catch (e) {
            reject(new Error(`bad JSON from ${path}: ${e.message}`));
          }
        });
      })
      .on('error', reject);
  });
}

/** List all CDP targets (pages, webviews, the separate meeting BrowserWindow, etc.). */
async function listTargets(port) {
  return httpJson(port, '/json/list');
}

/** Wait until a page target matching `match` (regex on url+title) appears. */
async function waitForPageTarget(port, { match = /index\.html|Pentacle/i, excludeTargetId = null, timeoutMs = 30000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastErr = null;
  while (Date.now() < deadline) {
    try {
      const targets = await listTargets(port);
      const page =
        targets.find((t) => t.type === 'page' && t.id !== excludeTargetId && match.test(String(t.url) + String(t.title))) ||
        targets.find((t) => t.type === 'page' && t.id !== excludeTargetId);
      if (page && page.webSocketDebuggerUrl) return page;
    } catch (e) {
      lastErr = e;
    }
    await sleep(400);
  }
  throw new Error(`no page target on :${port} within ${timeoutMs}ms${lastErr ? ` (${lastErr.message})` : ''}`);
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

class CdpSession {
  constructor(ws, target) {
    this.ws = ws;
    this.target = target;
    this._id = 0;
    this._pending = new Map();
    /** Ordered list of parsed [HARNESS] beacons (arrival order). */
    this.beacons = [];
    /** Raw console lines (for debugging / artifacts). */
    this.consoleLines = [];
    this._beaconListeners = new Set();
    this._consolePending = new Set();

    ws.on('message', (raw) => {
      let msg;
      try {
        msg = JSON.parse(raw);
      } catch {
        return;
      }
      if (msg.id && this._pending.has(msg.id)) {
        const { resolve, reject, timer } = this._pending.get(msg.id);
        if (timer) clearTimeout(timer);
        this._pending.delete(msg.id);
        if (msg.error) reject(new Error(`${msg.error.message || 'CDP error'} (${msg.error.code || ''})`));
        else resolve(msg.result);
        return;
      }
      if (msg.method === 'Runtime.consoleAPICalled') this._onConsole(msg.params);
      if (msg.method === 'Runtime.exceptionThrown') this._onException(msg.params);
    });
  }

  _queueConsole(entry, work) {
    // Reserve the arrival position synchronously, even when handles resolve later.
    this.consoleLines.push(entry);
    const pending = Promise.resolve().then(work).catch(() => { entry.text = '[unavailable]'; });
    this._consolePending.add(pending);
    pending.finally(() => this._consolePending.delete(pending));
  }

  async drainConsole() {
    while (this._consolePending.size) await Promise.all([...this._consolePending]);
  }

  async _consoleArg(arg) {
    if (!arg) return '[unavailable]';
    if ('value' in arg) return typeof arg.value === 'string' ? arg.value : String(arg.value);
    if (arg.subtype === 'null') return 'null';
    if (!arg.objectId) return arg.unserializableValue || arg.description || '[unavailable]';
    try {
      const result = await this.send('Runtime.callFunctionOn', {
        objectId: arg.objectId, returnByValue: true, silent: true,
        functionDeclaration: `function () {
          const seen = new WeakSet();
          let budget = 2048;
          const secrets = new Set(['token','secret','password','dsn','authorization','cookie']);
          function walk(v, depth) {
            if (--budget < 0) return '[truncated]';
            if (typeof v === 'string') { budget -= v.length; return budget < 0 ? '[truncated]' : v; }
            if (v === null || typeof v !== 'object') return typeof v === 'bigint' ? String(v) : v;
            if (seen.has(v)) return '[cyclic]';
            if (depth >= 3) return '[truncated]';
            seen.add(v);
            const out = Array.isArray(v) ? [] : {};
            try {
              for (const key of Object.keys(v)) {
                budget -= key.length + 4;
                if (budget < 0) { out['[truncated]'] = '[truncated]'; break; }
                if (secrets.has(key.toLowerCase())) out[key] = '[redacted]';
                else { try { out[key] = walk(v[key], depth + 1); } catch (_) { out[key] = '[unavailable]'; } }
              }
            } catch (_) { return '[unavailable]'; }
            return out;
          }
          return JSON.stringify(walk(this, 0));
        }`,
      });
      if (result.exceptionDetails || !result.result || typeof result.result.value !== 'string') return '[unavailable]';
      return Buffer.byteLength(result.result.value) > 2048 ? '[truncated]' : result.result.value;
    } catch { return '[unavailable]'; }
  }

  _onConsole(params) {
    const args = params.args || [];
    const entry = { ts: Date.now(), type: params.type, text: args.map(arg => {
      if (arg && 'value' in arg) return String(arg.value);
      if (arg?.subtype === 'null') return 'null';
      return '[unavailable]';
    }).join(' ') };
    // Beacon processing remains synchronous and independent of object extraction.
    const first = args[0] && typeof args[0].value === 'string' ? args[0].value : '';
    if (first.startsWith('[HARNESS] ')) {
      try {
        const beacon = JSON.parse(first.slice('[HARNESS] '.length));
        beacon._arrivedAt = Date.now();
        this.beacons.push(beacon);
        for (const l of this._beaconListeners) l(beacon);
      } catch { /* malformed beacons are still recorded as console entries */ }
    }
    this._queueConsole(entry, async () => {
      const values = await Promise.all(args.map((arg) => this._consoleArg(arg)));
      entry.text = values.join(' ');
      if (Buffer.byteLength(JSON.stringify(entry)) > 8192) entry.text = '[truncated]';
    });
  }

  _onException(params) {
    const details = params.exceptionDetails || {};
    const ex = details.exception || {};
    const entry = { ts: Date.now(), type: 'exception', text: ex.description || ex.value || details.text || '',
      url: details.url || '', line: details.lineNumber ?? null,
      stack_head: (ex.description || details.stackTrace?.callFrames?.[0]?.functionName || '').split('\n').slice(0, 2).join('\n') };
    if (Buffer.byteLength(JSON.stringify(entry)) > 8192) { entry.text = '[truncated]'; entry.stack_head = '[truncated]'; entry.url = '[truncated]'; }
    this._queueConsole(entry, async () => {});
  }

  send(method, params = {}) {
    const id = ++this._id;
    return new Promise((resolve, reject) => {
      // Safety timeout so a lost reply doesn't hang the runner forever; cleared
      // in the message handler when the reply arrives.
      const timer = setTimeout(() => {
        if (this._pending.has(id)) {
          this._pending.delete(id);
          reject(new Error(`CDP ${method} timed out`));
        }
      }, 30000);
      this._pending.set(id, { resolve, reject, timer });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  async enable() {
    await this.send('Runtime.enable');
    await this.send('Page.enable');
  }

  /**
   * Evaluate an expression in the page. Returns the value (returnByValue).
   * Throws on a JS exception or a rejected awaited promise.
   */
  async eval(expression, { awaitPromise = true } = {}) {
    const res = await this.send('Runtime.evaluate', {
      expression,
      returnByValue: true,
      awaitPromise,
      includeCommandLineAPI: false,
    });
    if (res.exceptionDetails) {
      const ex = res.exceptionDetails;
      const msg = (ex.exception && (ex.exception.description || ex.exception.value)) || ex.text || 'eval exception';
      throw new Error(`eval failed: ${msg}`);
    }
    return res.result ? res.result.value : undefined;
  }

  /** Dispatch a real click on the first element matching `selector`. Returns true if found. */
  async click(selector) {
    return this.eval(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      if (!el) return false;
      el.scrollIntoView({ block: 'center' });
      el.click();
      return true;
    })()`);
  }

  /** Set an input/textarea's value via real input events, then optionally fire a keydown. */
  async type(selector, text, { enter = false, ctrlEnter = false } = {}) {
    return this.eval(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      if (!el) return false;
      el.focus();
      el.value = ${JSON.stringify(text)};
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      ${enter ? `el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));` : ''}
      ${ctrlEnter ? `el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',ctrlKey:true,bubbles:true}));` : ''}
      return true;
    })()`);
  }

  /** Capture a PNG screenshot of the page to `filePath`. */
  async screenshot(filePath, { fullPage = false } = {}) {
    const res = await this.send('Page.captureScreenshot', {
      format: 'png',
      captureBeyondViewport: fullPage,
    });
    if (!res || !res.data) throw new Error('screenshot returned no data');
    require('fs').writeFileSync(filePath, Buffer.from(res.data, 'base64'));
    return filePath;
  }

  /** Poll an expression (must return truthy) until satisfied or timeout. */
  async waitFor(expression, { timeoutMs = 15000, intervalMs = 250, label = expression } = {}) {
    const deadline = Date.now() + timeoutMs;
    let last;
    const unsatisfied = (v) =>
      v === false || v === null || v === undefined || (typeof v === 'string' && v.startsWith('__ERR__'));
    while (Date.now() < deadline) {
      last = await this.eval(`(() => { try { return (${expression}); } catch (e) { return '__ERR__' + e.message; } })()`);
      // Note: a numeric 0 (e.g. slot index 0) IS satisfied — only false/null/undefined/__ERR__ are not.
      if (!unsatisfied(last)) return last;
      await sleep(intervalMs);
    }
    throw new Error(`waitFor timed out (${timeoutMs}ms): ${label}${last ? ` last=${JSON.stringify(last)}` : ''}`);
  }

  /** Current max beacon seq seen (open a negative window with this). */
  beaconSeq() {
    return this.beacons.length ? this.beacons[this.beacons.length - 1].seq : 0;
  }

  /** Beacons with seq strictly greater than `afterSeq`. */
  beaconsSince(afterSeq) {
    return this.beacons.filter((b) => b.seq > afterSeq);
  }

  /** Wait until a beacon matching `predicate(beacon)` arrives. Resolves with the beacon. */
  awaitBeacon(predicate, { timeoutMs = 20000, label = 'beacon' } = {}) {
    const existing = this.beacons.find(predicate);
    if (existing) return Promise.resolve(existing);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._beaconListeners.delete(listener);
        reject(new Error(`awaitBeacon timed out (${timeoutMs}ms): ${label}`));
      }, timeoutMs);
      const listener = (b) => {
        if (predicate(b)) {
          clearTimeout(timer);
          this._beaconListeners.delete(listener);
          resolve(b);
        }
      };
      this._beaconListeners.add(listener);
    });
  }

  /**
   * Assert that NO beacon matching `predicate` arrives within `windowMs`
   * (a negative window). Resolves if clean; rejects if one shows up.
   */
  async assertNoBeacon(predicate, { windowMs = 2000, fromSeq = this.beaconSeq(), label = 'unexpected beacon' } = {}) {
    await sleep(windowMs);
    const hit = this.beaconsSince(fromSeq).find(predicate);
    if (hit) throw new Error(`negative window violated: ${label} -> ${JSON.stringify(hit)}`);
    return true;
  }

  close() {
    try {
      this.ws.close();
    } catch {
      /* ignore */
    }
  }
}

/** Open a CDP session against the main page target on `port`. */
async function connect(port, opts = {}) {
  const target = await waitForPageTarget(port, opts);
  const ws = new WebSocket(target.webSocketDebuggerUrl, { perMessageDeflate: false });
  await new Promise((resolve, reject) => {
    ws.once('open', resolve);
    ws.once('error', reject);
  });
  const session = new CdpSession(ws, target);
  await session.enable();
  return session;
}

module.exports = { connect, listTargets, waitForPageTarget, CdpSession, sleep };
