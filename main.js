const { app, BrowserWindow, ipcMain, Menu, nativeImage } = require('electron');
const path = require('path');
const { spawn, execFile, execFileSync, execSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const { DynamoDBClient } = require('@aws-sdk/client-dynamodb');
const { DynamoDBDocumentClient, QueryCommand, ScanCommand } = require('@aws-sdk/lib-dynamodb');
const CONFIG = require('./pentacle.config.js');
const { LocalHost, Ssh2Host, buildHostRegistry } = require('./hosts.js');

// ── DynamoDB client for the 0DTE dashboard ──────────────────────────────────
// Reads AWS credentials from the standard chain (~/.aws/credentials, env, or
// instance profile). Read-only (Query + Scan). Platform-agnostic — works from
// host or client. Silently no-ops if AWS creds are absent.
const _ddbRaw = new DynamoDBClient({ region: 'us-east-1' });
const _ddb = DynamoDBDocumentClient.from(_ddbRaw);
const _DASHBOARD_TABLE = '0dte-snapshots';
let _tradersCache = { ts: 0, traders: [] };
const _TRADERS_CACHE_TTL_MS = 60_000;

app.setName(CONFIG.appName);

const MAX_SLOTS = 4;
const IS_DARWIN = process.platform === 'darwin';
const IS_WIN = process.platform === 'win32';

// Force UTF-8 locale in the Electron process itself BEFORE any tmux interaction.
// When Pentacle starts a tmux server (host mode), the server inherits this locale,
// so wcwidth() for U+276F (❯), U+23F5 (⏵), and other glyphs is computed correctly.
// On clients, this only affects the local Electron process — the remote tmux server
// is probed separately (see startup block).
process.env.LANG = 'en_US.UTF-8';
process.env.LC_ALL = 'en_US.UTF-8';

// ── WSL IP auto-detect (Windows, first boot only) ──────────────
// WSL2 is supposed to forward `localhost:<port>` to the VM, but on many
// machines that silently breaks after a network change. Falling back to
// WSL's actual eth0 IP avoids the issue entirely. We only probe if the
// user hasn't pinned `localWsl.host` explicitly.
if (IS_WIN && CONFIG.localWsl && !CONFIG.localWsl.host) {
  try {
    // `hostname -I` returns the primary eth0 IPv4 as the first word — simpler
    // than awk-parsing `ip -o addr show` and immune to shell-escape gotchas.
    const raw = execFileSync('wsl', ['-d', CONFIG.localWsl.distro || 'Ubuntu', '--', 'hostname', '-I'],
      { encoding: 'utf8', timeout: 3000 });
    const ip = (raw || '').split(/\s+/).find(w => /^\d+\.\d+\.\d+\.\d+$/.test(w));
    if (ip) {
      CONFIG.localWsl.host = ip;
      console.log(`[wsl] auto-detected IP: ${ip}`);
    }
  } catch {}
}

// ── Host registry ──────────────────────────────────────────────
// Built once at startup from CONFIG. `remote` block present → client mode.
// `localWsl` block on Windows → WSL is the "local" host (Ssh2Host to :2222).
const { hosts: HOSTS, isClient: IS_CLIENT } = buildHostRegistry(CONFIG);
// Convenience shortcuts
const hostLocal = HOSTS.local;
const hostRemote = HOSTS.remote;

// ── API server (host-local) URL + client-mode tunneled URL ─────
// On the host: the Python API server runs locally on 7777.
// On a client: we SSH-tunnel the remote's 7777 to localhost:<apiPort>
// (default 7778), and the renderer hits the tunneled port for session
// list / trash / kill / usage / bots APIs.
const API_PORT = IS_CLIENT ? (CONFIG.remote?.apiPort || 7778) : 7777;
const API_URL = IS_CLIENT ? `http://localhost:${API_PORT}` : (CONFIG.apiServer?.url || 'http://localhost:7777');

// ── PTY / SSH Session Manager ─────────────────────────────────
// Each slot holds an entry: { host, sessionName, paneId, handle, gen }.
// `gen` is incremented on every create to defeat stale async callbacks.
// Pane-ID reverse-map keyed by entry identity (not just paneId) — the guard
// `entry === sessionManager.slots[entry.slot]` catches stale callbacks fired
// after a replacement attach has taken the slot.
class SessionManager {
  constructor() {
    this.slots = new Array(MAX_SLOTS).fill(null);
    this.webContents = null;
    this._gen = 0;
  }

  setWebContents(wc) { this.webContents = wc; }

  async create(slot, sessionName, hostId, cols, rows) {
    this.kill(slot);
    const host = HOSTS[hostId] || hostLocal;
    if (!host) {
      console.warn(`[session:create] unknown hostId=${hostId}`);
      return null;
    }

    // Verify session exists on target host (silent — `has-session` exits non-zero if missing).
    // Use `=<name>` exact-match so no prefix/glob accident — a session named `foo` never
    // accidentally matches `foo-1776054321` if only the latter exists.
    try {
      if (host instanceof LocalHost) execSync(`${host.tmuxBin} has-session -t ${JSON.stringify('=' + sessionName)}`, { stdio: 'ignore', env: host.env });
      else await host.tmux(['has-session', '-t', '=' + sessionName]);
    } catch {
      console.warn(`[session:create] session not found: ${hostId}:${sessionName}`);
      return null;
    }

    const handle = await host.attach(sessionName, cols, rows);
    if (!handle) {
      console.warn(`[session:create] attach failed for ${hostId}:${sessionName}`);
      return null;
    }

    // Mouse off once per attach via pane ID target — never toggle on kill.
    // (Toggle-on-kill was the root cause of recurring scroll failures.)
    host.tmuxSilent(['set-option', '-t', handle.paneId, 'mouse', 'off']);

    const entry = { slot, host, sessionName, paneId: handle.paneId, handle, gen: ++this._gen };
    this.slots[slot] = entry;

    handle.onData((data) => {
      // Identity guard — stale data events from a replaced attach must not
      // clobber the new slot occupant's terminal.
      if (this.slots[slot] !== entry) return;
      if (this.webContents && !this.webContents.isDestroyed()) {
        this.webContents.send('pty:data', slot, data);
      }
    });

    handle.onExit((exitCode) => {
      if (this.slots[slot] !== entry) return;
      this.slots[slot] = null;
      if (this.webContents && !this.webContents.isDestroyed()) {
        this.webContents.send('pty:exit', slot, exitCode);
      }
    });

    return handle.paneId;
  }

  write(slot, data) { this.slots[slot]?.handle.write(data); }
  resize(slot, cols, rows) { this.slots[slot]?.handle.resize(cols, rows); }

  kill(slot) {
    const e = this.slots[slot];
    if (!e) return;
    this.slots[slot] = null;  // null first so onExit's identity check skips
    try { e.handle.kill(); } catch {}
  }

  killAll() { for (let i = 0; i < MAX_SLOTS; i++) this.kill(i); }
}

const sessionManager = new SessionManager();

// ── SSH tunnel (client mode only) ───────────────────────────────
// Forward localhost:<API_PORT> → remote:7777 so the renderer can hit the
// mac-mini's API server through a stable local address.
let sshTunnel = null;

function startSshTunnel() {
  if (!IS_CLIENT || !CONFIG.remote) return;
  const { host, user, port } = CONFIG.remote;
  const args = ['-o', 'StrictHostKeyChecking=no',
    '-o', 'ServerAliveInterval=30',
    '-o', 'ExitOnForwardFailure=yes',
    '-N', '-L', `${API_PORT}:localhost:7777`];
  if (port && port !== 22) { args.push('-p', String(port)); }
  args.push(`${user}@${host}`);

  if (IS_WIN) {
    // Use Windows OpenSSH (ships with Windows 10+). Falls back to WSL ssh only
    // if we can't locate it — but the Windows-side ssh avoids an extra hop.
    sshTunnel = spawn('ssh', args, { stdio: 'ignore', windowsHide: true });
  } else {
    sshTunnel = spawn('ssh', args, { stdio: 'ignore' });
  }

  sshTunnel.on('exit', (code) => {
    console.warn(`[tunnel] exited code=${code}, retrying in 5s`);
    sshTunnel = null;
    setTimeout(startSshTunnel, 5000);
  });
  sshTunnel.on('error', () => {});
}

function stopSshTunnel() {
  if (sshTunnel) { try { sshTunnel.kill(); } catch {} sshTunnel = null; }
}

// ── WSL sshd bootstrap (Windows only) ───────────────────────────
function ensureWslSshd() {
  if (!IS_WIN) return;
  const port = (CONFIG.localWsl && CONFIG.localWsl.sshPort) || 2222;
  const distro = (CONFIG.localWsl && CONFIG.localWsl.distro) || 'Ubuntu';
  // Launch a standalone sshd bound to :port. If one is already listening, kill
  // it first to guarantee it's a fresh static daemon (not systemd's socket-
  // activated one, which drops idle → causes ECONNREFUSED on reconnect after
  // tmux commands complete). We only kill PIDs owned by `sshd` — systemd's
  // listener (pid=1) is always skipped.
  const script = `
    systemctl disable --now ssh.socket ssh.service 2>/dev/null || true
    pkill -x sshd 2>/dev/null || true
    sleep 0.4
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE ":${port}\\b" && exit 0
    mkdir -p /run/sshd
    /usr/sbin/sshd -p ${port} -f /etc/ssh/sshd_config 2>&1 | tee /tmp/pentacle-sshd.log || true
  `.trim();
  try {
    execFileSync('wsl', ['-d', distro, '--', 'bash', '-lc', script],
      { stdio: 'ignore', timeout: 8000 });
  } catch (e) {
    console.warn('[wsl:sshd] failed to ensure sshd:', e.message);
  }
}

// ── Ensure API Server (host mode) ───────────────────────────────
async function ensureApiServer() {
  if (IS_CLIENT) {
    // Wait for tunnel to become reachable
    for (let i = 0; i < 20; i++) {
      try { await fetch(`${API_URL}/api/sessions`); return; } catch {}
      await new Promise(r => setTimeout(r, 500));
    }
    console.warn('[api] tunneled API server not reachable');
    return;
  }

  try { await fetch(`${API_URL}/api/sessions`); return; } catch {}
  const serverPath = path.join(process.env.HOME, CONFIG.apiServer.script);
  const pythonPath = path.join(process.env.HOME, CONFIG.apiServer.python);
  const server = spawn(pythonPath, [serverPath], { detached: true, stdio: 'ignore' });
  server.unref();
  for (let i = 0; i < 8; i++) {
    await new Promise((r) => setTimeout(r, 500));
    try { await fetch(`${API_URL}/api/sessions`); return; } catch {}
  }
  console.error('Failed to start API server');
}

// ── Mic server (cross-platform) ────────────────────────────────
async function ensureMicServer() {
  if (!CONFIG.features.mic) return;
  const micUrl = CONFIG.micServerUrl || 'http://127.0.0.1:7780';
  try { await fetch(`${micUrl}/status`); return; } catch {}

  if (IS_DARWIN) {
    // macOS: use MicServer.app for TCC permissions if available
    const appPath = '/Applications/MicServer.app';
    if (require('fs').existsSync(appPath)) {
      console.log(`[${CONFIG.appName}] Starting mic server via MicServer.app...`);
      const launcher = spawn('/usr/bin/open', ['-a', appPath], { detached: true, stdio: 'ignore' });
      launcher.unref();
    } else {
      _spawnMicServerPython();
    }
  } else {
    // Windows / Linux: spawn Python mic server directly (no TCC needed)
    _spawnMicServerPython();
  }

  for (let i = 0; i < 20; i++) {
    await new Promise(r => setTimeout(r, 500));
    try { await fetch(`${micUrl}/status`); return; } catch {}
  }
  console.error(`[${CONFIG.appName}] Failed to start mic server`);
}

function _spawnMicServerPython() {
  const micScript = path.join(__dirname, 'mic-server', 'mic_server.py');
  const pythonCmd = CONFIG.micServerPython || (IS_WIN ? 'python' : 'python3');
  const env = { ...process.env, MIC_SERVER_START_MODE: 'on' };
  console.log(`[${CONFIG.appName}] Starting mic server: ${pythonCmd} ${micScript}`);
  const proc = spawn(pythonCmd, ['-u', micScript], { detached: true, stdio: 'ignore', env });
  proc.unref();
}

// ── Usage refresh (mac host only) ──────────────────────────────
function refreshUsageData() {
  if (IS_CLIENT || !IS_DARWIN || !CONFIG.features.usage) return;
  const pythonPath = path.join(process.env.HOME, CONFIG.apiServer.python);
  const scriptPath = path.join(process.env.HOME, 'telegram-claude-bot/abilities/check_usage.py');
  const env = { ...process.env, PATH: `/opt/homebrew/bin:${process.env.PATH}` };
  const proc = spawn(pythonPath, [scriptPath, '--save'], { detached: true, stdio: 'ignore', env });
  proc.unref();
}

function refreshCodexUsageData() {
  if (IS_CLIENT || !IS_DARWIN || !CONFIG.features.usage) return;
  const pythonPath = path.join(process.env.HOME, CONFIG.apiServer.python);
  const scriptPath = path.join(process.env.HOME, 'telegram-claude-bot/abilities/check_codex_usage.py');
  const env = { ...process.env, PATH: `/opt/homebrew/bin:${process.env.PATH}` };
  const proc = spawn(pythonPath, [scriptPath, '--save'], { detached: true, stdio: 'ignore', env });
  proc.unref();
}

// ── Context Menu ───────────────────────────────────────────────
function showContextMenu(win, sessionName, displayName, hostId) {
  const hid = hostId || 'local';
  const template = [
    { label: `Open in Slot 1`, click: () => win.webContents.send('assign-slot', 0, sessionName, hid) },
    { label: `Open in Slot 2`, click: () => win.webContents.send('assign-slot', 1, sessionName, hid) },
    { label: `Open in Slot 3`, click: () => win.webContents.send('assign-slot', 2, sessionName, hid) },
    { label: `Open in Slot 4`, click: () => win.webContents.send('assign-slot', 3, sessionName, hid) },
    { type: 'separator' },
    { label: 'Rename...', click: () => win.webContents.send('action', 'rename', sessionName, displayName) },
    { label: 'Trash', click: () => win.webContents.send('action', 'trash', sessionName) },
  ];
  Menu.buildFromTemplate(template).popup({ window: win });
}

// ── App ────────────────────────────────────────────────────────
let mainWindow;
let meetingWindow = null;
const SLOT_STATE_FILE = path.join(app.getPath('userData'), '.slot-state.json');
const CODEX_UPDATE_STATE_FILE = path.join(app.getPath('userData'), '.codex-update-state.json');
const CODEX_UPDATE_INTERVAL_MS = 24 * 60 * 60 * 1000;

function saveSlotState() {
  try {
    const slots = sessionManager.slots.map(e => e ? { name: e.sessionName, hostId: e.host.id } : null);
    fs.writeFileSync(SLOT_STATE_FILE, JSON.stringify(slots));
  } catch {}
}

function loadSlotState() {
  try {
    const data = JSON.parse(fs.readFileSync(SLOT_STATE_FILE, 'utf8'));
    // Backcompat: bare string → { name, hostId: 'local' }
    return (data || []).map(s => {
      if (!s) return null;
      if (typeof s === 'string') return { name: s, hostId: 'local' };
      return s;
    });
  } catch { return new Array(MAX_SLOTS).fill(null); }
}

function loadCodexUpdateState() {
  try {
    return JSON.parse(fs.readFileSync(CODEX_UPDATE_STATE_FILE, 'utf8')) || {};
  } catch {
    return {};
  }
}

function saveCodexUpdateState(state) {
  try {
    fs.writeFileSync(CODEX_UPDATE_STATE_FILE, JSON.stringify(state, null, 2));
  } catch {}
}

function parseKeyValueLines(text) {
  const out = {};
  for (const line of String(text || '').split('\n')) {
    const i = line.indexOf('=');
    if (i <= 0) continue;
    out[line.slice(0, i)] = line.slice(i + 1);
  }
  return out;
}

function getCodexCommandForLocation(agentConfig, location) {
  return (location === 'local' && agentConfig.commandLocal) || agentConfig.command || 'codex';
}

async function ensureCodexUpdatedForHost(host, hostId, command) {
  const state = loadCodexUpdateState();
  const hostState = state[hostId] || {};
  const now = Date.now();
  if (hostState.lastCheckedAt && now - Number(hostState.lastCheckedAt) < CODEX_UPDATE_INTERVAL_MS) {
    return hostState;
  }

  const commandName = String(command || 'codex').trim().split(/\s+/)[0] || 'codex';
  const shell = process.platform === 'win32' ? 'bash' : 'zsh';
  const script = `
set -u
export PATH="/opt/homebrew/bin:$PATH"
command_name=${JSON.stringify(commandName)}
current_raw="$($command_name -V 2>/dev/null || true)"
current="$(printf '%s' "$current_raw" | awk '{print $NF}' | tr -d '\\r')"
latest="$(npm view @openai/codex version 2>/dev/null | tr -d '[:space:]')"
updated=0
install_status=skipped
if [ -n "$latest" ] && [ "$current" != "$latest" ]; then
  if npm install -g @openai/codex >/tmp/pentacle-codex-update.log 2>&1; then
    updated=1
    install_status=ok
  else
    install_status=failed
  fi
fi
final_raw="$($command_name -V 2>/dev/null || true)"
final="$(printf '%s' "$final_raw" | awk '{print $NF}' | tr -d '\\r')"
printf 'command=%s\\n' "$command_name"
printf 'current=%s\\n' "$current"
printf 'latest=%s\\n' "$latest"
printf 'final=%s\\n' "$final"
printf 'updated=%s\\n' "$updated"
printf 'install_status=%s\\n' "$install_status"
`.trim();

  try {
    const raw = await host.exec([`/bin/${shell}`, '-lc', script]);
    const result = {
      ...hostState,
      ...parseKeyValueLines(raw),
      lastCheckedAt: now,
      hostId,
    };
    state[hostId] = result;
    saveCodexUpdateState(state);
    const changed = result.updated === '1';
    const from = result.current || '(unknown)';
    const to = result.final || result.latest || '(unknown)';
    console.log(`[codex:update] host=${hostId} status=${result.install_status || 'unknown'} updated=${changed} ${from} -> ${to}`);
    return result;
  } catch (e) {
    const result = {
      ...hostState,
      hostId,
      lastCheckedAt: now,
      error: e.message || String(e),
    };
    state[hostId] = result;
    saveCodexUpdateState(state);
    console.warn(`[codex:update] host=${hostId} failed: ${result.error}`);
    return result;
  }
}

function maybeRefreshCodexUsageAfterUpdate(result) {
  if (!result || result.updated !== '1') return;
  refreshCodexUsageData();
}

function startBackgroundCodexUpdateChecks() {
  const agentConfig = CONFIG.agents?.codex;
  if (!agentConfig) return;
  const jobs = [];
  if (hostLocal) {
    jobs.push(
      ensureCodexUpdatedForHost(hostLocal, hostLocal.id || 'local', getCodexCommandForLocation(agentConfig, 'local'))
        .then(maybeRefreshCodexUsageAfterUpdate)
    );
  }
  if (hostRemote) {
    jobs.push(
      ensureCodexUpdatedForHost(hostRemote, hostRemote.id || 'remote', agentConfig.command)
        .then(maybeRefreshCodexUsageAfterUpdate)
    );
  }
  Promise.allSettled(jobs).catch(() => {});
}

// Host-mode-only tmux-server UTF-8 ensure. On clients we probe remote and warn.
function ensureLocalTmuxUtf8() {
  if (!(hostLocal instanceof LocalHost)) return;
  try {
    const serverPid = execSync(`${hostLocal.tmuxBin} display-message -p "#{pid}"`, { encoding: 'utf8', env: hostLocal.env }).trim();
    const serverEnv = execSync(`/bin/ps eww -p ${serverPid} 2>/dev/null || true`, { encoding: 'utf8' });
    const hasUtf8 = /LANG=.*[Uu][Tt][Ff]/.test(serverEnv);
    if (!hasUtf8) {
      console.log(`[${CONFIG.appName}] tmux server started without UTF-8 locale — restarting`);
      try { execSync(`${hostLocal.tmuxBin} kill-server`, { stdio: 'ignore', timeout: 3000 }); } catch {}
      execSync('sleep 0.3');
      execSync(`${hostLocal.tmuxBin} new-session -d -s _app_keepalive`, { env: hostLocal.env, timeout: 3000 });
    } else {
      execSync(`${hostLocal.tmuxBin} set-environment -g LANG "en_US.UTF-8"`, { stdio: 'ignore', env: hostLocal.env });
      execSync(`${hostLocal.tmuxBin} set-environment -g LC_ALL "en_US.UTF-8"`, { stdio: 'ignore', env: hostLocal.env });
    }
  } catch {
    try { execSync(`${hostLocal.tmuxBin} new-session -d -s _app_keepalive`, { env: hostLocal.env, timeout: 3000 }); } catch {}
  }
}

async function probeRemoteTmuxUtf8() {
  if (!IS_CLIENT || !hostRemote) return;
  const r = await hostRemote.probeLocale();
  if (r.ok) return;
  if (r.error) {
    console.warn(`[${CONFIG.appName}] remote tmux locale probe failed: ${r.error}`);
  } else {
    console.warn(`[${CONFIG.appName}] remote tmux server LANG=${r.lang || '(unset)'} — not UTF-8. ⏵/❯ may render as __. Fix: SSH to the host and run \`tmux kill-server\`, then reattach.`);
  }
}

// Ensure `window-size latest` on every tmux server we talk to. Without this,
// tmux sizes each window to the SMALLEST attached client — so when a Windows
// or macbook client attaches to a small slot, the mac-mini's own Pentacle
// view shrinks to match. `latest` makes each window track the most-recently-
// active client instead, so whoever is typing gets their correct size and
// the idle client sees a slight reflow until the active one steps away.
function ensureWindowSizeLatest() {
  for (const host of Object.values(HOSTS)) {
    try {
      if (host instanceof LocalHost) {
        // Sync for host mode — cheap, one call at startup.
        execFile(host.tmuxBin, ['set-option', '-g', 'window-size', 'latest'], { env: host.env }, () => {});
      } else {
        host.tmuxSilent(['set-option', '-g', 'window-size', 'latest']);
      }
    } catch {}
  }
}

app.whenReady().then(async () => {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { role: 'appMenu' },
    { role: 'editMenu' },
  ]));

  ensureWslSshd();
  startSshTunnel();
  await ensureApiServer();
  ensureLocalTmuxUtf8();
  probeRemoteTmuxUtf8();  // fire-and-forget; just logs
  ensureWindowSizeLatest();  // stop multi-client size collapse

  mainWindow = new BrowserWindow({
    width: 1600,
    height: 1000,
    minWidth: 900,
    minHeight: 600,
    titleBarStyle: IS_DARWIN ? 'hiddenInset' : 'default',
    trafficLightPosition: IS_DARWIN ? { x: 15, y: 15 } : undefined,
    backgroundColor: CONFIG.dark.bg,
    icon: IS_DARWIN
      ? nativeImage.createFromPath(path.join(__dirname, 'assets', 'icon.icns'))
      : undefined,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: false,
      nodeIntegration: true,
    },
  });

  sessionManager.setWebContents(mainWindow.webContents);
  await mainWindow.webContents.session.clearCache();
  mainWindow.loadFile('renderer/index.html');

  // Restore slots after renderer is ready
  const savedSlots = loadSlotState();
  mainWindow.webContents.on('did-finish-load', () => {
    setTimeout(() => {
      let delay = 0;
      savedSlots.forEach((s, slot) => {
        if (!s || !s.name) return;
        const host = HOSTS[s.hostId] || hostLocal;
        if (!host) return;
        // Best-effort session existence check; skip if missing
        (async () => {
          try {
            if (host instanceof LocalHost) {
              execSync(`${host.tmuxBin} has-session -t ${JSON.stringify('=' + s.name)}`, { stdio: 'ignore', env: host.env });
            } else {
              await host.tmux(['has-session', '-t', '=' + s.name]);
            }
            setTimeout(() => {
              mainWindow.webContents.send('assign-slot', slot, s.name, s.hostId || 'local');
            }, delay);
            delay += 500;
          } catch {}
        })();
      });
    }, 1000);

    ensureMicServer();
    refreshUsageData();
    refreshCodexUsageData();
    startBackgroundCodexUpdateChecks();
  });

  // ── IPC: session management ─────────────────────────────────
  ipcMain.handle('pty:create', async (_, slot, sessionName, hostId, cols, rows) => {
    const result = await sessionManager.create(slot, sessionName, hostId || 'local', cols, rows);
    saveSlotState();
    return result;
  });

  ipcMain.on('pty:write', (_, slot, data) => sessionManager.write(slot, data));
  ipcMain.on('pty:resize', (_, slot, cols, rows) => sessionManager.resize(slot, cols, rows));

  ipcMain.handle('pty:kill', (_, slot) => {
    sessionManager.kill(slot);
    saveSlotState();
  });

  ipcMain.handle('pty:check-session', async (_, sessionName, hostId) => {
    const host = HOSTS[hostId] || hostLocal;
    if (!host) return false;
    try {
      if (host instanceof LocalHost) {
        execSync(`${host.tmuxBin} has-session -t ${JSON.stringify('=' + sessionName)}`, { stdio: 'ignore', env: host.env });
      } else {
        await host.tmux(['has-session', '-t', '=' + sessionName]);
      }
      return true;
    } catch { return false; }
  });

  ipcMain.handle('pty:new-session', async (_, agent, location = 'local') => {
    const sessionName = `${agent}-${process.pid}-${Date.now()}`;
    const wantRemote = location === 'remote' && IS_CLIENT && hostRemote;
    const targetHost = wantRemote ? hostRemote : hostLocal;
    const agentConfig = CONFIG.agents[agent] || CONFIG.agents.claude;
    // Optional per-location override. Useful for WSL-as-root where
    // `claude --dangerously-skip-permissions` is refused — user can set
    // `agents.claude.commandLocal = 'claude'` to sidestep the flag.
    const locationCommand = (location === 'local' && agentConfig.commandLocal) || agentConfig.command;

    if (agent === 'codex' && targetHost) {
      const updateResult = await ensureCodexUpdatedForHost(targetHost, targetHost.id || (wantRemote ? 'remote' : 'local'), locationCommand);
      maybeRefreshCodexUsageAfterUpdate(updateResult);
    }

    // Shell-first fallback creator. Creates an interactive bash session with
    // no command, then send-keys the agent command. Session survives the
    // agent exiting (shell stays alive). Used whenever agent-tmux CLI isn't
    // available on the target host.
    async function shellFirstCreate(host, workDir, command) {
      // Use bash as the session's anchor process. -d detaches immediately.
      // Note: we don't pass -c <workDir> here — some tmux builds error if the
      // path doesn't exist or contains a `~`. Instead we cd + exec in send-keys.
      await host.tmux(['new-session', '-d', '-s', sessionName, 'bash', '-l']);
      // Give bash a beat to become prompt-ready
      await new Promise(r => setTimeout(r, 200));
      // Inside WSL, claude/codex call xdg-open for OAuth and WSL has no
      // display. `wslview` (from the `wslu` apt package) correctly hands URLs
      // to the Windows default browser. `explorer.exe <url>` would
      // misinterpret URL-like strings as paths and open My Documents.
      const isWsl = IS_WIN && host === hostLocal;
      const envPrelude = isWsl ? 'export BROWSER=wslview; ' : '';
      const cdLine = workDir ? `cd ${JSON.stringify(workDir)} 2>/dev/null; clear; ` : '';
      // Plain name here — tmux's `=exact` prefix applies to session targets
      // (has-session, display-message) but not pane targets (send-keys).
      await host.tmux(['send-keys', '-t', sessionName, `${envPrelude}${cdLine}${command}`, 'Enter']);
      // Claude/Codex show startup trust prompts (external imports, directory trust)
      // that block the session. Auto-accept by sending Enter after the agent has
      // time to render the prompt. Harmless if already dismissed — Enter on the
      // ❯ input line is a no-op.
      setTimeout(async () => {
        try { await host.tmux(['send-keys', '-t', sessionName, '', 'Enter']); } catch {}
      }, 3000);
    }

    try {
      if (wantRemote) {
        const remoteHome = `/Users/${CONFIG.remote.user}`;
        const workDir = (CONFIG.workingDirectory || '~/agent-workspace').replace(/^~/, remoteHome);
        const command = agentConfig.command;
        // Prefer the mac-mini's agent-tmux CLI (shell-first pattern, orchestrator
        // hooks). Fall back to inline shell-first on any failure.
        try {
          await hostRemote.exec([
            '/Users/bartimaeus/agent-dashboard/bin/agent-tmux',
            'create-session', '--name', sessionName, '--workdir', workDir, '--command', command,
          ]);
        } catch (e) {
          console.warn(`[new-session] agent-tmux failed (${e.message}); falling back to shell-first`);
          await shellFirstCreate(hostRemote, workDir, command);
        }
        return { sessionName, hostId: 'remote' };
      } else if (!IS_CLIENT && IS_DARWIN) {
        // Host mode on the mac-mini — call agent-tmux CLI locally.
        const workDir = (CONFIG.workingDirectory || '~/agent-workspace').replace(/^~/, process.env.HOME);
        let command;
        if (agentConfig.binary) {
          const bin = agentConfig.binary.replace(/^~/, process.env.HOME);
          const flags = agentConfig.command.split(' ').slice(1).join(' ');
          command = `${bin}${flags ? ' ' + flags : ''}`;
        } else {
          command = agentConfig.command;
        }
        try {
          execFileSync(
            '/Users/bartimaeus/agent-dashboard/bin/agent-tmux',
            ['create-session', '--name', sessionName, '--workdir', workDir, '--command', command],
            { env: hostLocal.env, stdio: ['ignore', 'pipe', 'pipe'] }
          );
        } catch (e) {
          console.warn(`[new-session] agent-tmux local failed (${e.message}); falling back to shell-first`);
          await shellFirstCreate(hostLocal, workDir, command);
        }
        return { sessionName, hostId: 'local' };
      } else {
        // Client local (macbook's own tmux or Windows WSL). Shell-first —
        // no agent-tmux CLI available here.
        const workDir = (CONFIG.workingDirectory || '~').replace(/^~/, process.env.HOME || '~');
        await shellFirstCreate(targetHost, workDir, locationCommand);
        return { sessionName, hostId: targetHost.id };
      }
    } catch (e) {
      console.error(`[new-session] agent=${agent} location=${location}:`, e.message || e, e.stack || '');
      return null;
    }
  });

  ipcMain.on('context-menu', (_, sessionName, displayName, hostId) => {
    showContextMenu(mainWindow, sessionName, displayName, hostId);
  });

  // Scroll via copy-mode. Renderer passes slot + direction + lines; we look up
  // the entry's host and pane-id from the slot (not a separate paneId map, to
  // avoid stale-pane-id races across rapid attach/detach).
  ipcMain.on('pty:scroll', (_, slot, direction, lines) => {
    const entry = sessionManager.slots[slot];
    if (!entry) return;
    const count = Math.min(lines || 1, 50);
    const cmd = direction === 'up' ? 'scroll-up' : 'scroll-down';
    entry.host.tmuxSilent([
      'copy-mode', '-e', '-t', entry.paneId, ';',
      'send-keys', '-t', entry.paneId, '-X', '-N', String(count), cmd,
    ]);
  });

  ipcMain.on('pty:tmux-send', (_, slot, ...keys) => {
    const entry = sessionManager.slots[slot];
    if (!entry) return;
    entry.host.tmuxSilent(['send-keys', '-t', entry.paneId, ...keys]);
  });

  ipcMain.on('pty:exit-copy-mode', (_, slot) => {
    const entry = sessionManager.slots[slot];
    if (!entry) return;
    entry.host.tmuxSilent(['send-keys', '-t', entry.paneId, '-X', 'cancel']);
  });

  // Expose config to renderer, including computed fields
  ipcMain.handle('get-config', () => ({
    ...CONFIG,
    isClient: IS_CLIENT,
    apiUrl: API_URL,
    apiPort: API_PORT,
    platform: process.platform,
    hostIds: Object.keys(HOSTS),
  }));

  // ── IPC: mic / meeting (cross-platform) ──────────────────────
  ipcMain.handle('mic:start-server', async () => {
    if (!CONFIG.features.mic) return false;
    const micUrl = CONFIG.micServerUrl || 'http://127.0.0.1:7780';
    try { await fetch(`${micUrl}/status`); return true; } catch {}

    if (IS_DARWIN && require('fs').existsSync('/Applications/MicServer.app')) {
      const launcher = spawn('/usr/bin/open', ['-a', '/Applications/MicServer.app'], { detached: true, stdio: 'ignore' });
      launcher.unref();
    } else {
      _spawnMicServerPython();
    }

    for (let i = 0; i < 20; i++) {
      await new Promise(r => setTimeout(r, 500));
      try { await fetch(`${micUrl}/status`); return true; } catch {}
    }
    return false;
  });

  ipcMain.on('meeting:open', () => {
    if (meetingWindow && !meetingWindow.isDestroyed()) { meetingWindow.focus(); return; }
    meetingWindow = new BrowserWindow({
      width: 600, height: 500, minWidth: 400, minHeight: 300,
      titleBarStyle: IS_DARWIN ? 'hiddenInset' : 'default',
      trafficLightPosition: IS_DARWIN ? { x: 12, y: 12 } : undefined,
      backgroundColor: CONFIG.dark.bg,
      webPreferences: { contextIsolation: false, nodeIntegration: true },
    });
    meetingWindow.loadFile('renderer/meeting.html');
    meetingWindow.on('closed', () => { meetingWindow = null; });
  });

  ipcMain.on('meeting:close', () => {
    if (meetingWindow && !meetingWindow.isDestroyed()) { meetingWindow.close(); meetingWindow = null; }
  });

  // ── Activity detection / capture (multi-host) ───────────────
  const SPINNER_RE = /[✻✢·⊹*❋◆◇◈⟡⟢⟣☼◉] [A-Z][a-z]+…/;
  const WORKING_STRINGS = ['Running…', 'ctrl+b ctrl+b', 'to run in background'];

  function classifyPane(content) {
    const lines = content.split('\n');
    const bottomLines = lines.slice(-4).map(l => l.trim()).filter(l => l);
    const isClaude = bottomLines.some(l => l.includes('⏵⏵ bypass') || l.includes('⏵⏵ auto-accept'));
    if (!isClaude) return 'idle';
    let contentLines = [];
    for (let i = lines.length - 1; i >= 0; i--) {
      if (lines[i].includes('❯')) { contentLines = lines.slice(Math.max(0, i - 15), i); break; }
    }
    if (!contentLines.length) contentLines = lines.slice(-20);
    const contentText = contentLines.join('\n');
    const isWorking = SPINNER_RE.test(contentText) || WORKING_STRINGS.some(p => contentText.includes(p));
    return isWorking ? 'working' : 'waiting';
  }

  async function hostListTargets(host) {
    try {
      if (host instanceof LocalHost) {
        const r = execSync(`${host.tmuxBin} list-windows -a -F "#{session_name}:#{window_index}"`, { encoding: 'utf8', timeout: 3000, env: host.env });
        return r.trim().split('\n').filter(Boolean);
      }
      const r = await host.tmux(['list-windows', '-a', '-F', '#{session_name}:#{window_index}'], { lane: 'bg' });
      return r.trim().split('\n').filter(Boolean);
    } catch { return []; }
  }

  async function hostCapture(host, target, opts = {}) {
    const args = ['capture-pane', '-t', target, '-p'];
    if (opts.scrollback) args.push('-S', String(opts.scrollback));
    try {
      if (host instanceof LocalHost) {
        return execSync(`${host.tmuxBin} ${args.map(a => JSON.stringify(a)).join(' ')}`, { encoding: 'utf8', timeout: 2000, env: host.env });
      }
      return await host.tmux(args, { lane: 'bg' });
    } catch { return ''; }
  }

  ipcMain.handle('pty:detect-activity', async () => {
    const states = {};
    // Iterate registered hosts; keys prefixed with hostId
    for (const [id, host] of Object.entries(HOSTS)) {
      const targets = await hostListTargets(host);
      for (const target of targets) {
        const cap = await hostCapture(host, target);
        states[`${id}:${target}`] = classifyPane(cap);
      }
    }
    return states;
  });

  ipcMain.handle('pty:capture-all-panes', async () => {
    const panes = {};
    for (const [id, host] of Object.entries(HOSTS)) {
      const targets = await hostListTargets(host);
      for (const target of targets) {
        panes[`${id}:${target}`] = await hostCapture(host, target, { scrollback: -500 });
      }
    }
    return panes;
  });

  // ── Dashboards ──────────────────────────────────────────────
  ipcMain.handle('dashboard:pipeline-stats', async (_evt, batch) => {
    // Paths are hardcoded to macOS bartimaeus home → needs mac host.
    if (IS_CLIENT || !IS_DARWIN) return { error: 'pipeline-stats only available on mac host' };
    return new Promise((resolve) => {
      const env = {
        ...process.env,
        PYTHONPATH: '/Users/bartimaeus/land-bot',
        DATABASE_URL: 'postgresql://bartimaeus@localhost:5432/altum',
      };
      const args = ['/Users/bartimaeus/land-bot/scripts/pipeline_stats.py'];
      if (batch && typeof batch === 'string') args.push('--batch', batch);
      const proc = spawn('/Users/bartimaeus/.venvs/global/bin/python', args, { env });
      let stdout = '', stderr = '';
      const timer = setTimeout(() => { proc.kill('SIGKILL'); }, 15000);
      proc.stdout.on('data', d => { stdout += d; });
      proc.stderr.on('data', d => { stderr += d; });
      proc.on('close', (code) => {
        clearTimeout(timer);
        if (code === 0) {
          try { resolve(JSON.parse(stdout.trim())); }
          catch { resolve({ error: 'Invalid JSON: ' + stdout.slice(0, 100) }); }
        } else { resolve({ error: (stderr || '').slice(0, 200) || `Stats script exit ${code}` }); }
      });
      proc.on('error', (e) => { clearTimeout(timer); resolve({ error: e.message }); });
    });
  });

  // 0DTE dashboard — DynamoDB queries work from any host (AWS is remote)
  ipcMain.handle('dashboard:0dte-stats', async (_evt, traderId) => {
    if (!traderId) return { error: 'no trader_id provided' };
    try {
      const resp = await _ddb.send(new QueryCommand({
        TableName: _DASHBOARD_TABLE,
        KeyConditionExpression: 'trader_id = :t',
        ExpressionAttributeValues: { ':t': traderId },
        ScanIndexForward: false, Limit: 1,
      }));
      const items = resp.Items || [];
      if (items.length === 0) return { snapshot: null, age_sec: null, trader_id: traderId };
      const snapshot = items[0];
      const ageSec = Math.max(0, (Date.now() - Number(snapshot.snapshot_ts)) / 1000);
      return { snapshot, age_sec: ageSec, trader_id: traderId };
    } catch (e) { return { error: `dynamodb query failed: ${e.message || e}` }; }
  });

  ipcMain.handle('dashboard:0dte-list-traders', async () => {
    const now = Date.now();
    if (now - _tradersCache.ts < _TRADERS_CACHE_TTL_MS && _tradersCache.traders.length > 0) {
      return { traders: _tradersCache.traders, cached: true };
    }
    try {
      const traders = new Set();
      let lastKey = undefined;
      do {
        const resp = await _ddb.send(new ScanCommand({
          TableName: _DASHBOARD_TABLE,
          ProjectionExpression: 'trader_id',
          ExclusiveStartKey: lastKey,
        }));
        for (const it of (resp.Items || [])) if (it.trader_id) traders.add(it.trader_id);
        lastKey = resp.LastEvaluatedKey;
      } while (lastKey);
      const list = Array.from(traders).sort();
      _tradersCache = { ts: now, traders: list };
      return { traders: list, cached: false };
    } catch (e) {
      return { error: `dynamodb scan failed: ${e.message || e}`, traders: _tradersCache.traders };
    }
  });

  // Image paste
  ipcMain.handle('pty:save-image', async (_, base64Data) => {
    try {
      const buf = Buffer.from(base64Data, 'base64');
      const tmpPath = path.join(os.tmpdir(), `${CONFIG.appName.toLowerCase()}-paste-${Date.now()}.png`);
      fs.writeFileSync(tmpPath, buf);
      return { ok: true, path: tmpPath };
    } catch (e) {
      return { ok: false, error: e.message };
    }
  });
});

app.on('window-all-closed', () => {
  saveSlotState();
  sessionManager.killAll();
  stopSshTunnel();
  for (const h of Object.values(HOSTS)) h.destroy?.();
  app.quit();
});

app.on('before-quit', () => {
  saveSlotState();
  sessionManager.killAll();
  stopSshTunnel();
  for (const h of Object.values(HOSTS)) h.destroy?.();
  if (meetingWindow && !meetingWindow.isDestroyed()) meetingWindow.close();
});
