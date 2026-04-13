// Host abstraction for multi-host client/server mode.
//
// LocalHost  — execSync/execFile + node-pty. Mac-mini host mode and the
//              client's own local tmux (mac/linux client).
// Ssh2Host   — ssh2.Client for both command (conn.exec) and attach. For
//              remote-from-client and for Windows's "local" (WSL sshd:2222).
//
// Ssh2Host keeps two long-lived client connections: a control lane for
// low-latency calls (display-message, send-keys, scroll, resize) and a
// background lane for heavy calls (capture-pane, list-windows). Big capture
// output on the bg lane cannot head-of-line block a scroll on the ctrl lane.
//
// Attach uses a third, per-attach client since interactive streams are long-
// lived. `LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 tmux -u attach-session` is the
// canonical `__` / ⏵⏵ render fix (macOS sshd doesn't forward LC_CTYPE, so
// the remote shell inherits C locale and tmux's client-side wcwidth returns
// -1 for U+23F5). `stream.setEncoding('utf8')` handles byte-boundary splits.

const { execSync, execFile } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

// ── LocalHost ─────────────────────────────────────────────────────────
class LocalHost {
  constructor({ tmuxBin, env } = {}) {
    this.id = 'local';
    this.isRemote = false;
    this.tmuxBin = tmuxBin || '/opt/homebrew/bin/tmux';
    this.env = env || { ...process.env, TERM: 'xterm-256color' };
    this._pty = null;
  }

  _ensurePty() {
    if (!this._pty) this._pty = require('node-pty');
    return this._pty;
  }

  tmuxSync(args, opts = {}) {
    // args: array of argv. Use execFileSync via execSync string for consistency.
    const cmd = [this.tmuxBin, ...args.map(a => /^[A-Za-z0-9._%:@/+,-]+$/.test(String(a)) ? a : JSON.stringify(a))].join(' ');
    return execSync(cmd, { encoding: 'utf8', timeout: 3000, env: this.env, ...opts });
  }

  tmux(args) {
    return new Promise((resolve, reject) => {
      execFile(this.tmuxBin, args, { encoding: 'utf8', env: this.env, timeout: 5000 }, (err, stdout) => {
        if (err) return reject(err);
        resolve(stdout);
      });
    });
  }

  tmuxSilent(args) { execFile(this.tmuxBin, args, { env: this.env }, () => {}); }

  // `exec(cmd)` — run a non-tmux command on this host. Host-mode only uses
  // this for agent-tmux delegation (mac-mini-local).
  exec(argv) {
    return new Promise((resolve, reject) => {
      const [bin, ...args] = argv;
      execFile(bin, args, { encoding: 'utf8', env: this.env, timeout: 15000 }, (err, stdout, stderr) => {
        if (err) return reject(new Error(stderr || err.message));
        resolve(stdout);
      });
    });
  }

  async attach(sessionName, cols, rows) {
    let paneId;
    try {
      // display-message takes target-pane, not target-session — `=exact` prefix
      // silently yields empty output there, so use bare session name.
      paneId = execSync(`${this.tmuxBin} display-message -t ${JSON.stringify(sessionName)} -p "#{pane_id}"`, {
        encoding: 'utf8', env: this.env, timeout: 3000,
      }).trim();
    } catch {
      return null;
    }

    const pty = this._ensurePty();
    const p = pty.spawn(this.tmuxBin, ['attach-session', '-t', sessionName], {
      name: 'xterm-256color',
      cols: cols || 80,
      rows: rows || 24,
      cwd: process.env.HOME,
      env: this.env,
    });

    return {
      paneId,
      onData: (cb) => p.onData(cb),
      onExit: (cb) => p.onExit(({ exitCode }) => cb(exitCode)),
      write: (d) => { try { p.write(d); } catch {} },
      resize: (c, r) => { try { p.resize(c, r); } catch {} },
      kill: () => { try { p.kill(); } catch {} },
    };
  }

  destroy() {}
}

// ── Ssh2Host ──────────────────────────────────────────────────────────
class Ssh2Host {
  constructor({ id, host, port, user, tmuxBin, privateKey, isRemote = true } = {}) {
    this.id = id;
    this.isRemote = !!isRemote;
    this.host = host;
    this.port = port || 22;
    this.user = user;
    this.tmuxBin = tmuxBin || 'tmux';
    this.privateKey = privateKey;
    // Dual lanes — ctrl is for snappy interactive calls, bg is for big captures.
    this._lanes = { ctrl: { client: null, ready: null }, bg: { client: null, ready: null } };
    this._envPrefix = 'LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 ';
  }

  _Client() { const { Client } = require('ssh2'); return new Client(); }

  _openLaneClient() {
    const conn = this._Client();
    return new Promise((resolve, reject) => {
      conn.once('ready', () => resolve(conn));
      conn.once('error', reject);
      conn.connect({
        host: this.host, port: this.port, username: this.user,
        privateKey: this.privateKey, readyTimeout: 10000, keepaliveInterval: 30000,
      });
    });
  }

  async _lane(name) {
    const lane = this._lanes[name];
    const alive = lane.client && !lane.client._sock?.destroyed;
    if (alive) return lane.client;
    if (lane.ready) return lane.ready;
    lane.ready = (async () => {
      try {
        const conn = await this._openLaneClient();
        const invalidate = () => { lane.client = null; lane.ready = null; };
        conn.once('end', invalidate);
        conn.once('close', invalidate);
        conn.once('error', invalidate);
        lane.client = conn;
        return conn;
      } finally {
        lane.ready = null;
      }
    })();
    return lane.ready;
  }

  _buildCmd(args) {
    const quoted = args.map((a) => {
      const s = String(a);
      if (/^[A-Za-z0-9._%:@/+,-]+$/.test(s)) return s;
      return "'" + s.replace(/'/g, "'\\''") + "'";
    }).join(' ');
    return this._envPrefix + this.tmuxBin + ' ' + quoted;
  }

  async tmux(args, { lane = 'ctrl' } = {}) {
    const conn = await this._lane(lane);
    return new Promise((resolve, reject) => {
      conn.exec(this._buildCmd(args), (err, stream) => {
        if (err) return reject(err);
        let out = '', errOut = '';
        stream.on('data', (d) => { out += d.toString('utf8'); });
        stream.stderr.on('data', (d) => { errOut += d.toString('utf8'); });
        stream.on('close', (code) => {
          if (code === 0) resolve(out);
          else reject(new Error(errOut.trim() || `tmux exit ${code}`));
        });
      });
    });
  }

  tmuxSync() { throw new Error('Ssh2Host.tmuxSync unsupported — use tmux() async'); }

  tmuxSilent(args) {
    this.tmux(args).catch((e) => {
      if (process.env.PENTACLE_DEBUG) console.warn(`[${this.id}] tmuxSilent fail:`, e.message);
    });
  }

  // Non-tmux remote exec (agent-tmux CLI, locale probes, etc.)
  async exec(cmdLine, { lane = 'ctrl' } = {}) {
    const line = Array.isArray(cmdLine)
      ? cmdLine.map(a => /^[A-Za-z0-9._%:@/+,-]+$/.test(String(a)) ? a : "'" + String(a).replace(/'/g, "'\\''") + "'").join(' ')
      : cmdLine;
    const conn = await this._lane(lane);
    return new Promise((resolve, reject) => {
      conn.exec(line, (err, stream) => {
        if (err) return reject(err);
        let out = '', errOut = '';
        stream.on('data', (d) => { out += d.toString('utf8'); });
        stream.stderr.on('data', (d) => { errOut += d.toString('utf8'); });
        stream.on('close', (code) => {
          if (code === 0) resolve(out);
          else reject(new Error(errOut.trim() || `exit ${code}`));
        });
      });
    });
  }

  async attach(sessionName, cols, rows) {
    let paneId;
    // display-message takes target-pane — bare session name resolves to its
    // active pane. `=exact` works on target-session only (has-session, etc).
    try { paneId = (await this.tmux(['display-message', '-t', sessionName, '-p', '#{pane_id}'])).trim(); }
    catch { return null; }

    const conn = await this._openLaneClient();
    const attachCmd = `${this._envPrefix}${this.tmuxBin} -u attach-session -t ${JSON.stringify(sessionName)}`;

    const stream = await new Promise((resolve, reject) => {
      conn.exec(attachCmd, { pty: { term: 'xterm-256color', cols: cols || 80, rows: rows || 24 } }, (err, s) => {
        if (err) { conn.end(); return reject(err); }
        resolve(s);
      });
    });

    stream.setEncoding('utf8');
    stream.stderr.setEncoding('utf8');

    const listeners = { data: [], exit: [] };
    stream.on('data', (d) => listeners.data.forEach((cb) => cb(d)));
    stream.stderr.on('data', (d) => listeners.data.forEach((cb) => cb(d)));
    stream.on('close', (code) => {
      listeners.exit.forEach((cb) => cb(typeof code === 'number' ? code : 0));
      try { conn.end(); } catch {}
    });

    return {
      paneId,
      onData: (cb) => listeners.data.push(cb),
      onExit: (cb) => listeners.exit.push(cb),
      write: (d) => { try { stream.write(d); } catch {} },
      resize: (c, r) => { try { stream.setWindow(r, c, 0, 0); } catch {} },
      kill: () => {
        try { stream.write('\x02d'); } catch {}
        setTimeout(() => { try { stream.close(); } catch {} try { conn.end(); } catch {} }, 200);
      },
    };
  }

  destroy() {
    for (const lane of Object.values(this._lanes)) {
      try { lane.client?.end(); } catch {}
      lane.client = null;
      lane.ready = null;
    }
  }

  // Client-mode startup probe: confirm the remote tmux server was started
  // under UTF-8 locale. Return { ok, lang } or { error } — caller logs a
  // warning but does not auto-restart someone else's server.
  async probeLocale() {
    try {
      const pid = (await this.tmux(['display-message', '-p', '#{pid}'])).trim();
      if (!pid) return { error: 'no pid' };
      const env = await this.exec(['/bin/ps', 'eww', pid]);
      const m = env.match(/\bLANG=([^\s]+)/);
      const lang = m ? m[1] : null;
      const ok = !!(lang && /UTF-?8/i.test(lang));
      return { ok, lang };
    } catch (e) {
      return { error: e.message };
    }
  }
}

// ── Registry ──────────────────────────────────────────────────────────
function buildHostRegistry(CONFIG, { platform } = { platform: process.platform }) {
  const hosts = {};

  const readKey = () => {
    const home = os.homedir();
    for (const name of ['id_ed25519', 'id_rsa']) {
      try { return fs.readFileSync(path.join(home, '.ssh', name)); } catch {}
    }
    return null;
  };
  const privateKey = readKey();

  const isWin = platform === 'win32';
  const hasRemote = !!(CONFIG && CONFIG.remote);

  if (isWin) {
    const wsl = (CONFIG && CONFIG.localWsl) || {};
    hosts.local = new Ssh2Host({
      id: 'local',
      // Prefer explicit host from config, else auto-detected eth0 IP (set in
      // main.js before we're called), else fall back to localhost.
      host: wsl.host || 'localhost',
      port: wsl.sshPort || 2222,
      user: wsl.user || 'root',
      tmuxBin: wsl.tmux || 'tmux',
      privateKey,
      isRemote: false,
    });
  } else {
    const localTmux = CONFIG?.localTmux || (platform === 'darwin' ? '/opt/homebrew/bin/tmux' : 'tmux');
    hosts.local = new LocalHost({
      tmuxBin: localTmux,
      env: { ...process.env, TERM: 'xterm-256color' },
    });
  }

  if (hasRemote) {
    hosts.remote = new Ssh2Host({
      id: 'remote',
      host: CONFIG.remote.host,
      port: CONFIG.remote.port || 22,
      user: CONFIG.remote.user,
      tmuxBin: CONFIG.remote.tmux || '/opt/homebrew/bin/tmux',
      privateKey,
      isRemote: true,
    });
  }

  return { hosts, defaultId: hasRemote ? 'remote' : 'local', isClient: hasRemote };
}

module.exports = { LocalHost, Ssh2Host, buildHostRegistry };
