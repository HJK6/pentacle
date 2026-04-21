/**
 * Dashboard Hub WebSocket client for Pentacle main process (§5.1).
 *
 * Connects to ws://<hub-host>:7780/live?token=<read-token> on Pentacle start.
 * Hub URL + read token come from pentacle.config.js:
 *   dashboardHub: {
 *     url: 'ws://bartimaeuss-mac-mini:7780',
 *     readTokenPath: '~/.dashboard-hub/read-token',
 *   }
 *
 * On snapshot → updates in-memory Map<id, envelope> AND queues a debounced
 * write to disk cache.
 *
 * Disk cache: ~/.pentacle/dashboard-cache.json (macOS/Linux) or
 *             %APPDATA%\Pentacle\dashboard-cache.json (Windows).
 *
 * Exposes:
 *   hubClient.get(id)         → envelope or null
 *   hubClient.connected       → boolean
 *   hubClient.init(cfg, app)  → call once at app startup
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const WebSocket = require('ws');

// ── Cache path ─────────────────────────────────────────────────────────────

function _cachePath(app) {
  try {
    if (process.platform === 'win32') {
      return path.join(app.getPath('userData'), 'dashboard-cache.json');
    }
    return path.join(os.homedir(), '.pentacle', 'dashboard-cache.json');
  } catch (_) {
    return path.join(os.tmpdir(), 'pentacle-dashboard-cache.json');
  }
}

// ── Token reading ──────────────────────────────────────────────────────────

function _readToken(tokenPath) {
  try {
    const expanded = tokenPath.replace(/^~/, os.homedir());
    return fs.readFileSync(expanded, 'utf8').trim();
  } catch (_) {
    return null;
  }
}

// ── Backoff ────────────────────────────────────────────────────────────────

function _backoff(attempt) {
  // 1s → 2s → 5s → 30s max, +0–30% jitter
  const base = Math.min(30, [1, 2, 5, 10, 30][Math.min(attempt, 4)]);
  const jitter = base * Math.random() * 0.30;
  return (base + jitter) * 1000;
}

// ── Client class ───────────────────────────────────────────────────────────

class DashboardHubClient {
  constructor() {
    this._memory = new Map();          // id → envelope
    this._ws = null;
    this.connected = false;
    this._cfg = null;
    this._app = null;
    this._cachePath = null;
    this._debounceTimer = null;
    this._reconnectAttempt = 0;
    this._destroyed = false;
    this._pingInterval = null;
    this._lastPong = Date.now();
  }

  // ── Public API ────────────────────────────────────────────────────────────

  get(id) {
    return this._memory.get(id) || null;
  }

  init(cfg, app) {
    this._cfg = cfg;
    this._app = app;
    this._cachePath = _cachePath(app);

    // Load disk cache for cold-start
    this._loadCache();

    this._connect();

    // Flush cache on app quit
    app.on('before-quit', () => {
      this._destroyed = true;
      this._flushCacheNow();
      if (this._ws) {
        try { this._ws.terminate(); } catch (_) {}
      }
    });
  }

  // ── Connection ────────────────────────────────────────────────────────────

  _connect() {
    if (this._destroyed) return;

    const hubCfg = this._cfg.dashboardHub;
    if (!hubCfg || !hubCfg.url) {
      console.warn('[DashboardHub] No dashboardHub.url in config — skipping');
      return;
    }

    const token = _readToken(hubCfg.readTokenPath || '~/.dashboard-hub/read-token');
    if (!token) {
      console.warn('[DashboardHub] No read token found — retrying in 30s');
      setTimeout(() => this._connect(), 30000);
      return;
    }

    const wsUrl = `${hubCfg.url.replace(/^http/, 'ws')}/live?token=${encodeURIComponent(token)}`;
    console.log('[DashboardHub] Connecting to', wsUrl.replace(/token=[^&]+/, 'token=***'));

    let ws;
    try {
      ws = new WebSocket(wsUrl);
    } catch (e) {
      console.error('[DashboardHub] WebSocket constructor error:', e.message);
      this._scheduleReconnect();
      return;
    }
    this._ws = ws;

    ws.on('open', () => {
      console.log('[DashboardHub] Connected');
      this._reconnectAttempt = 0;
      this._lastPong = Date.now();

      // Send atomic hello+subscribe
      ws.send(JSON.stringify({
        type: 'hello',
        client: 'pentacle',
        capabilities: ['full-snapshot'],
        subscribe: { all: true },
      }));

      // Ping/pong watchdog
      this._pingInterval = setInterval(() => {
        if (!this.connected) return;
        if (Date.now() - this._lastPong > 70000) {
          console.warn('[DashboardHub] Pong timeout — reconnecting');
          ws.terminate();
          return;
        }
      }, 30000);
    });

    ws.on('message', (rawData) => {
      // Any message from server counts as keep-alive — server pings every 25s
      // and sends snapshots on every update. Only pong-on-pong would be too narrow.
      this._lastPong = Date.now();

      let msg;
      try { msg = JSON.parse(rawData.toString()); } catch (_) { return; }

      if (msg.type === 'snapshot' && msg.envelope) {
        const env = msg.envelope;
        this._memory.set(env.dashboard_id, env);
        this._scheduleDebounce();
      } else if (msg.type === 'welcome') {
        this.connected = true;
        console.log('[DashboardHub] Welcome received — dashboards:', msg.dashboards);
      } else if (msg.type === 'ping') {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'pong' }));
      } else if (msg.type === 'error') {
        console.error('[DashboardHub] Server error:', msg.code, msg.message);
      }
    });

    ws.on('close', (code, reason) => {
      console.log('[DashboardHub] Disconnected:', code, reason?.toString());
      this.connected = false;
      if (this._pingInterval) { clearInterval(this._pingInterval); this._pingInterval = null; }
      this._scheduleReconnect();
    });

    ws.on('error', (err) => {
      console.error('[DashboardHub] WS error:', err.message);
      // close event fires after error — reconnect handled there
    });
  }

  _scheduleReconnect() {
    if (this._destroyed) return;
    const delay = _backoff(this._reconnectAttempt++);
    console.log(`[DashboardHub] Reconnecting in ${Math.round(delay / 1000)}s (attempt ${this._reconnectAttempt})`);
    setTimeout(() => this._connect(), delay);
  }

  // ── Disk cache ────────────────────────────────────────────────────────────

  _scheduleDebounce() {
    if (this._debounceTimer) return;
    this._debounceTimer = setTimeout(() => {
      this._debounceTimer = null;
      this._flushCacheNow();
    }, 2000);
  }

  _flushCacheNow() {
    if (this._debounceTimer) {
      clearTimeout(this._debounceTimer);
      this._debounceTimer = null;
    }
    if (!this._cachePath) return;
    try {
      const obj = {};
      for (const [id, env] of this._memory) obj[id] = env;
      const payload = JSON.stringify(obj);
      const tmp = this._cachePath + '.tmp';
      const dir = path.dirname(this._cachePath);
      fs.mkdirSync(dir, { recursive: true });
      // Open, write, fsync, close the tmp file so its contents are durable
      // before we rename over the final path (spec §5.1).
      const fd = fs.openSync(tmp, 'w');
      try {
        fs.writeSync(fd, payload, 0, 'utf8');
        try { fs.fsyncSync(fd); } catch (_) { /* fsync unsupported → continue */ }
      } finally {
        fs.closeSync(fd);
      }
      // Atomic rename. On Windows (NTFS) rename-over-existing can hit EBUSY —
      // retry a few times with a short spin.
      let retries = 5;
      while (retries-- > 0) {
        try { fs.renameSync(tmp, this._cachePath); break; }
        catch (e) {
          if (e.code === 'EBUSY' && retries > 0) {
            const until = Date.now() + 50;
            while (Date.now() < until) {}
          } else { throw e; }
        }
      }
      // fsync the parent directory so the rename itself is durable on macOS/Linux.
      // Windows does not expose directory fsync — skip there.
      if (process.platform !== 'win32') {
        try {
          const dfd = fs.openSync(dir, 'r');
          try { fs.fsyncSync(dfd); } catch (_) { /* ignore */ }
          finally { fs.closeSync(dfd); }
        } catch (_) { /* directory fsync not supported — accept */ }
      }
    } catch (e) {
      console.error('[DashboardHub] Cache flush error:', e.message);
    }
  }

  _loadCache() {
    if (!this._cachePath) return;
    try {
      const raw = fs.readFileSync(this._cachePath, 'utf8');
      const obj = JSON.parse(raw);
      for (const [id, env] of Object.entries(obj)) {
        this._memory.set(id, env);
      }
      console.log('[DashboardHub] Loaded cache:', this._memory.size, 'dashboards');
    } catch (_) {
      // No cache on first run — ok
    }
  }
}

module.exports = new DashboardHubClient();
