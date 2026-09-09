// Host abstraction for multi-host client/server mode.
//
// LocalHost  — execSync/execFile + node-pty. Mac-mini host mode and the
//              client's own local tmux (mac/linux client).
// WslHost    — direct wsl.exe calls on Windows. Used for Windows's "local"
//              host so sessions inherit the Windows token that launched
//              Pentacle instead of being born from the long-running WSL sshd.
// Ssh2Host   — ssh2.Client for both command (conn.exec) and attach. For
//              remote-from-client and SSH peers.
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

const { execSync, execFile, execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

function shellQuote(arg) {
  return "'" + String(arg).replace(/'/g, "'\\''") + "'";
}

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
  exec(argv, opts = {}) {
    return new Promise((resolve, reject) => {
      const [bin, ...args] = argv;
      const child = execFile(bin, args, { encoding: 'utf8', env: this.env, timeout: 60000 }, (err, stdout, stderr) => {
        if (err) return reject(new Error(stderr || err.message));
        resolve(stdout);
      });
      if (opts.input !== undefined) child.stdin.end(String(opts.input));
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

// ── WslHost ───────────────────────────────────────────────────────────
class WslHost {
  constructor({ distro, user, tmuxBin, env } = {}) {
    this.id = 'local';
    this.isRemote = false;
    this.distro = distro || 'Ubuntu';
    this.user = user || null;
    this.tmuxBin = tmuxBin || 'tmux';
    this.env = env || { ...process.env, TERM: 'xterm-256color' };
    this._pty = null;
    this._envArgs = ['env', 'LANG=en_US.UTF-8', 'LC_ALL=en_US.UTF-8', 'TERM=xterm-256color'];
  }

  _ensurePty() {
    if (!this._pty) this._pty = require('node-pty');
    return this._pty;
  }

  _wslArgs(argv) {
    const args = ['-d', this.distro];
    if (this.user) args.push('-u', this.user);
    return [...args, '--', ...argv.map((arg) => {
      const value = String(arg);
      // wsl.exe treats argv entries beginning with # like comments and drops
      // them, but tmux format strings commonly start with #{...}.
      return value.startsWith('#') ? `\\${value}` : value;
    })];
  }

  _tmuxArgv(args) {
    const line = [...this._envArgs, this.tmuxBin, ...args].map(shellQuote).join(' ');
    return ['/bin/bash', '-lc', line];
  }

  tmuxSync(args, opts = {}) {
    return execFileSync('wsl.exe', this._wslArgs(this._tmuxArgv(args)), {
      encoding: 'utf8',
      timeout: 3000,
      env: this.env,
      ...opts,
    });
  }

  tmux(args) {
    return new Promise((resolve, reject) => {
      execFile('wsl.exe', this._wslArgs(this._tmuxArgv(args)), {
        encoding: 'utf8',
        env: this.env,
        timeout: 5000,
      }, (err, stdout, stderr) => {
        if (err) return reject(new Error(stderr || err.message));
        resolve(stdout);
      });
    });
  }

  tmuxSilent(args) {
    execFile('wsl.exe', this._wslArgs(this._tmuxArgv(args)), { env: this.env }, () => {});
  }

  exec(argv, opts = {}) {
    return new Promise((resolve, reject) => {
      const child = execFile('wsl.exe', this._wslArgs(argv), {
        encoding: 'utf8',
        env: this.env,
        timeout: 60000,
      }, (err, stdout, stderr) => {
        if (err) return reject(new Error(stderr || err.message));
        resolve(stdout);
      });
      if (opts.input !== undefined) child.stdin.end(String(opts.input));
    });
  }

  async attach(sessionName, cols, rows) {
    let paneId;
    try {
      paneId = this.tmuxSync(['display-message', '-t', sessionName, '-p', '#{pane_id}']).trim();
    } catch {
      return null;
    }

    const pty = this._ensurePty();
    const p = pty.spawn('wsl.exe', this._wslArgs([
      ...this._envArgs,
      this.tmuxBin,
      '-u',
      'attach-session',
      '-t',
      sessionName,
    ]), {
      name: 'xterm-256color',
      cols: cols || 80,
      rows: rows || 24,
      cwd: os.homedir(),
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

  _isLaneClientAlive(client) {
    return !!client && !client._sock?.destroyed;
  }

  _closeClient(client) {
    if (!client) return;
    try { client.end(); } catch {}
    try { client.destroy?.(); } catch {}
  }

  _invalidateLane(name, { client, ready, closeClient = false } = {}) {
    const lane = this._lanes[name];
    if (!lane) return false;
    if (client && lane.client !== client) {
      if (closeClient) this._closeClient(client);
      return false;
    }
    if (ready && lane.ready !== ready) return false;
    const doomed = client || lane.client;
    if (!client || lane.client === client) lane.client = null;
    if (!ready || lane.ready === ready) lane.ready = null;
    if (closeClient) this._closeClient(doomed);
    return true;
  }

  _trackLaneClient(name, conn) {
    const invalidate = () => {
      this._invalidateLane(name, { client: conn });
    };
    conn.once('end', invalidate);
    conn.once('close', invalidate);
    conn.once('error', invalidate);
  }

  _openLaneClient() {
    const conn = this._Client();
    // Persistent 'error' listener: ssh2 can emit multiple 'error' events for a
    // single failure (e.g. banner timeout → 'error' + socket 'error' + proto
    // errors on close). A `once` listener catches the first, and the rest hit
    // Node's EventEmitter with no handler → uncaught exception → Electron
    // main-process crash. Keep the listener permanent and swallow late errors.
    const onError = (err) => {
      if (settled) {
        if (process.env.PENTACLE_DEBUG) {
          console.warn(`[${this.id}] ssh2 late error:`, err && err.message);
        }
        return;
      }
      settled = true;
      reject(err);
    };
    let settled = false;
    let resolve, reject;
    const p = new Promise((res, rej) => { resolve = res; reject = rej; });
    conn.on('error', onError);
    conn.once('ready', () => {
      if (settled) return;
      settled = true;
      // Interactive terminal attaches write individual keystrokes through this
      // SSH connection. Disable Nagle so a single typed byte is flushed
      // immediately instead of waiting for TCP coalescing/delayed ACK behavior.
      try { conn.setNoDelay(true); } catch {}
      resolve(conn);
    });
    try {
      conn.connect({
        host: this.host, port: this.port, username: this.user,
        privateKey: this.privateKey, readyTimeout: 10000, keepaliveInterval: 30000,
      });
    } catch (err) {
      onError(err);
    }
    return p;
  }

  async _lane(name) {
    const lane = this._lanes[name];
    if (this._isLaneClientAlive(lane.client)) return lane.client;
    if (lane.client) this._invalidateLane(name, { client: lane.client, closeClient: true });
    if (lane.ready) return lane.ready;
    let readyPromise;
    readyPromise = (async () => {
      try {
        const conn = await this._openLaneClient();
        if (lane.ready && lane.ready !== readyPromise) {
          this._closeClient(conn);
          return lane.ready;
        }
        this._trackLaneClient(name, conn);
        lane.client = conn;
        return conn;
      } catch (err) {
        this._invalidateLane(name, { ready: readyPromise });
        throw err;
      } finally {
        if (lane.ready === readyPromise) lane.ready = null;
      }
    })();
    lane.ready = readyPromise;
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
    return this._runLaneCommand(lane, this._buildCmd(args), {}, { exitPrefix: 'tmux exit' });
  }

  tmuxSync() { throw new Error('Ssh2Host.tmuxSync unsupported — use tmux() async'); }

  tmuxSilent(args) {
    this.tmux(args).catch((e) => {
      if (process.env.PENTACLE_DEBUG) console.warn(`[${this.id}] tmuxSilent fail:`, e.message);
    });
  }

  // Non-tmux remote exec (agent-tmux CLI, locale probes, etc.)
  async exec(cmdLine, { lane = 'ctrl', input, timeout } = {}) {
    const line = Array.isArray(cmdLine)
      ? cmdLine.map(a => /^[A-Za-z0-9._%:@/+,-]+$/.test(String(a)) ? a : "'" + String(a).replace(/'/g, "'\\''") + "'").join(' ')
      : cmdLine;
    return this._runLaneCommand(lane, line, {}, { input, timeout, exitPrefix: 'exit' });
  }

  async _runLaneCommand(lane, line, execOptions = {}, { input, timeout, exitPrefix = 'exit' } = {}) {
    const conn = await this._lane(lane);
    return new Promise((resolve, reject) => {
      let stream = null;
      let timer = null;
      let settled = false;
      const transportFail = (err) => {
        const reason = err instanceof Error ? err : new Error(String(err || 'ssh transport closed'));
        this._invalidateLane(lane, { client: conn, closeClient: true });
        finish(reject, reason);
      };
      const cleanup = () => {
        if (timer) clearTimeout(timer);
        conn.removeListener('error', onClientError);
        conn.removeListener('end', onClientEnd);
        conn.removeListener('close', onClientClose);
        if (stream) stream.removeListener('error', onStreamError);
      };
      const finish = (fn, value) => {
        if (settled) return;
        settled = true;
        cleanup();
        fn(value);
      };
      const onClientError = (err) => transportFail(err);
      const onClientEnd = () => transportFail(new Error('ssh client ended'));
      const onClientClose = () => transportFail(new Error('ssh client closed'));
      const onStreamError = (err) => transportFail(err);
      conn.once('error', onClientError);
      conn.once('end', onClientEnd);
      conn.once('close', onClientClose);
      try {
        conn.exec(line, execOptions, (err, s) => {
          if (err) return transportFail(err);
          stream = s;
          stream.once('error', onStreamError);
          timer = timeout ? setTimeout(() => {
            try { stream.close(); } catch {}
            finish(reject, new Error('timeout'));
          }, timeout) : null;
          let out = '', errOut = '';
          stream.on('data', (d) => { out += d.toString('utf8'); });
          stream.stderr?.on('data', (d) => { errOut += d.toString('utf8'); });
          stream.on('close', (code) => {
            if (code === 0) finish(resolve, out);
            else finish(reject, new Error(errOut.trim() || `${exitPrefix} ${code}`));
          });
          if (input !== undefined) {
            stream.end(String(input));
          }
        });
      } catch (err) {
        transportFail(err);
      }
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
      let settled = false;
      const cleanup = () => {
        conn.removeListener('error', onClientError);
        conn.removeListener('end', onClientEnd);
        conn.removeListener('close', onClientClose);
      };
      const finish = (fn, value) => {
        if (settled) return;
        settled = true;
        cleanup();
        fn(value);
      };
      const fail = (err) => {
        this._closeClient(conn);
        finish(reject, err instanceof Error ? err : new Error(String(err || 'ssh attach closed')));
      };
      const onClientError = (err) => fail(err);
      const onClientEnd = () => fail(new Error('ssh attach client ended'));
      const onClientClose = () => fail(new Error('ssh attach client closed'));
      conn.once('error', onClientError);
      conn.once('end', onClientEnd);
      conn.once('close', onClientClose);
      try {
        conn.exec(attachCmd, { pty: { term: 'xterm-256color', cols: cols || 80, rows: rows || 24 } }, (err, s) => {
          if (err) return fail(err);
          finish(resolve, s);
        });
      } catch (err) {
        fail(err);
      }
    });

    stream.setEncoding('utf8');
    stream.stderr.setEncoding('utf8');

    const listeners = { data: [], exit: [] };
    let exited = false;
    const finishAttach = (code = 0) => {
      if (exited) return;
      exited = true;
      listeners.exit.forEach((cb) => cb(typeof code === 'number' ? code : 0));
      try { stream.close(); } catch {}
      this._closeClient(conn);
    };
    stream.on('data', (d) => listeners.data.forEach((cb) => cb(d)));
    stream.stderr.on('data', (d) => listeners.data.forEach((cb) => cb(d)));
    stream.once('error', () => finishAttach(1));
    stream.once('close', (code) => finishAttach(code));
    conn.once('error', () => finishAttach(1));
    conn.once('end', () => finishAttach(0));
    conn.once('close', () => finishAttach(0));

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
    const localSsh = CONFIG && CONFIG.localSsh;
    if (localSsh && localSsh.host) {
      // Reach THIS machine's own WSL tmux over SSH (WSL sshd) instead of via
      // wsl.exe. Under `.wslconfig networkingMode=mirrored`, wsl.exe process-launch
      // intermittently fails ("Catastrophic failure / Wsl/Service/E_UNEXPECTED")
      // after a host network transition, which breaks opening local chats; the
      // sshd path (hostAddressLoopback → 127.0.0.1:2222) is unaffected. id stays
      // 'local' so every host.id==='local' branch (commandLocal, no-tildify,
      // Windows-local machine stats) keeps treating it as the local host.
      const wsl = (CONFIG && CONFIG.localWsl) || {};
      hosts.local = new Ssh2Host({
        id: 'local',
        host: localSsh.host,
        port: localSsh.port || 2222,
        user: localSsh.user || wsl.user || null,
        tmuxBin: localSsh.tmux || wsl.tmux || 'tmux',
        privateKey,
        isRemote: false,
      });
    } else {
      const wsl = (CONFIG && CONFIG.localWsl) || {};
      hosts.local = new WslHost({
        distro: wsl.distro || 'Ubuntu',
        user: wsl.user || null,
        tmuxBin: wsl.tmux || 'tmux',
        env: { ...process.env, TERM: 'xterm-256color' },
      });
    }
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

  // Peers — additional SSH hosts for multi-machine chat visibility without
  // flipping into CLIENT mode. Each peer's tmux sessions show up in the sidebar
  // alongside local ones; attach works via SSH like `remote` does.
  const peers = Array.isArray(CONFIG && CONFIG.peers) ? CONFIG.peers : [];
  for (const p of peers) {
    if (!p || !p.id || !p.host || !p.user) continue;
    if (hosts[p.id]) continue; // don't clobber local/remote
    hosts[p.id] = new Ssh2Host({
      id: p.id,
      host: p.host,
      port: p.port || 22,
      user: p.user,
      tmuxBin: p.tmux || 'tmux',
      privateKey,
      isRemote: true,
    });
  }

  return { hosts, defaultId: hasRemote ? 'remote' : 'local', isClient: hasRemote };
}

module.exports = { LocalHost, WslHost, Ssh2Host, buildHostRegistry };
