const { app, BrowserWindow, ipcMain, Menu, nativeImage } = require('electron');
const path = require('path');
const { spawn, execFile, execFileSync, execSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const { DynamoDBClient } = require('@aws-sdk/client-dynamodb');
const { DynamoDBDocumentClient, QueryCommand, ScanCommand } = require('@aws-sdk/lib-dynamodb');
const { S3Client, ListObjectsV2Command, GetObjectCommand, HeadObjectCommand } = require('@aws-sdk/client-s3');
const { SQSClient, GetQueueAttributesCommand } = require('@aws-sdk/client-sqs');

// ── File logging ────────────────────────────────────────────────────────────
// Mirror console.{log,info,warn,error} and uncaught exceptions to a log file
// under the platform userData dir, so post-mortem debugging works on any
// machine. Rotates at 5 MB (keeps one .1 backup). Best-effort — any failure
// inside this block is swallowed so logging never breaks app startup.
//
// Log locations:
//   Windows  %APPDATA%\Pentacle\logs\pentacle.log
//   macOS    ~/Library/Application Support/Pentacle/logs/pentacle.log
//   Linux    ~/.config/Pentacle/logs/pentacle.log
{
  try {
    const logDir = path.join(app.getPath('userData'), 'logs');
    fs.mkdirSync(logDir, { recursive: true });
    const logPath = path.join(logDir, 'pentacle.log');
    const MAX_BYTES = 5 * 1024 * 1024;
    try {
      const st = fs.statSync(logPath);
      if (st.size > MAX_BYTES) fs.renameSync(logPath, logPath + '.1');
    } catch {}
    const stream = fs.createWriteStream(logPath, { flags: 'a' });
    stream.write(`\n[${new Date().toISOString()}] --- Pentacle start ${process.platform} node=${process.versions.node} electron=${process.versions.electron} pid=${process.pid} ---\n`);
    const fmt = (args) => args.map((a) => {
      if (typeof a === 'string') return a;
      if (a && a.stack) return a.stack;
      try { return JSON.stringify(a); } catch { return String(a); }
    }).join(' ');
    for (const level of ['log', 'info', 'warn', 'error']) {
      const orig = console[level].bind(console);
      console[level] = (...args) => {
        try { stream.write(`[${new Date().toISOString()}] [${level}] ${fmt(args)}\n`); } catch {}
        orig(...args);
      };
    }
    process.on('uncaughtException', (e) => {
      try { stream.write(`[${new Date().toISOString()}] [uncaught] ${e && e.stack || e}\n`); } catch {}
    });
    process.on('unhandledRejection', (e) => {
      try { stream.write(`[${new Date().toISOString()}] [unhandledRejection] ${e && e.stack || e}\n`); } catch {}
    });
  } catch { /* logging is best-effort */ }
}

// ── Config bootstrap ────────────────────────────────────────────────────────
// Auto-copy example → config on first run (dev mode only).
// Never overwrites an existing config.
{
  const cfgPath = path.join(__dirname, 'pentacle.config.js');
  const examplePath = path.join(__dirname, 'pentacle.config.example.js');
  if (!fs.existsSync(cfgPath) && fs.existsSync(examplePath)) {
    try { fs.copyFileSync(examplePath, cfgPath); } catch (_e) { /* fall through to error window */ }
  }
}

let CONFIG;
let _configLoadError = null;
try {
  CONFIG = require('./pentacle.config.js');
} catch (e) {
  _configLoadError = e;
}

if (_configLoadError) {
  // Config missing or invalid — show a minimal error window.
  // Register the handler before any CONFIG-dependent code can run.
  app.whenReady().then(() => {
    const w = new BrowserWindow({ width: 700, height: 320, webPreferences: { contextIsolation: true } });
    const msg = String(_configLoadError.message || _configLoadError).slice(0, 300).replace(/</g, '&lt;');
    w.loadURL(
      'data:text/html,' + encodeURIComponent(
        '<body style="font-family:sans-serif;padding:32px;background:#0c1310;color:#b5ccba">' +
        '<h2>Could not load pentacle.config.js</h2>' +
        '<p>Copy <code>pentacle.config.example.js</code> to <code>pentacle.config.js</code> and relaunch.</p>' +
        `<pre style="font-size:12px;color:#f47067">${msg}</pre></body>`
      )
    );
    w.on('closed', () => app.quit());
  });
  app.on('window-all-closed', () => app.quit());
  // Use a fake minimal CONFIG so the rest of the module doesn't crash on property access.
  // app.whenReady() will show the error window; the normal app window is never created.
  CONFIG = { appName: 'Pentacle', apiServer: {}, features: {}, dark: { bg: '#0c1310' }, agents: {} };
}

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

// ── S3 + SQS clients for the Amaterasu OCR dashboard ─────────────────────────
// Kept for back-compat; dashboard data now flows through the Dashboard Hub
// (see hubClient + IPC handlers below).
const _s3 = new S3Client({ region: 'us-east-1' });
const _sqs = new SQSClient({ region: 'us-east-1' });
const _AMATERASU_BUCKET = 'amaterasu-botcomm-data';
const _AMATERASU_QUEUE = 'https://sqs.us-east-1.amazonaws.com/841672795586/amaterasu-inbox';

// ── Dashboard Hub client ─────────────────────────────────────────────────────
// Single long-lived WS connection per Pentacle instance. Handles reconnect +
// disk cache. Exposes .get(id) → envelope. Keep startup resilient if an older
// checkout/build is missing this optional module.
let hubClient;
try {
  hubClient = require('./main/dashboard_hub_client');
} catch (e) {
  console.warn(`[DashboardHub] disabled: ${e.message}`);
  hubClient = {
    connected: false,
    init() {},
    get() { return null; },
  };
}

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

// ── Python binary resolution ────────────────────────────────────
function resolvePython() {
  // 1. CONFIG.apiServer.python if set and file exists (Vamshi's venv path).
  const cfgPy = CONFIG.apiServer && CONFIG.apiServer.python;
  if (cfgPy) {
    const abs = path.isAbsolute(cfgPy) ? cfgPy : path.join(os.homedir(), cfgPy);
    if (fs.existsSync(abs)) return abs;
  }
  // 2. Auto-detect: python3 on POSIX, python on Windows.
  const candidate = IS_WIN ? 'python' : 'python3';
  try {
    execFileSync(candidate, ['--version'], { stdio: 'ignore', timeout: 3000 });
    return candidate;
  } catch {}
  // 3. Give up — caller will skip server launch and log an error.
  return null;
}

// ── Script path resolution ──────────────────────────────────────
function resolveServerScript() {
  const script = (CONFIG.apiServer && CONFIG.apiServer.script) || 'server/server.py';
  if (path.isAbsolute(script)) return script;
  // Try repo-relative first (vendored server), then $HOME-relative (Vamshi's server).
  const repoRel = path.join(__dirname, script);
  if (fs.existsSync(repoRel)) return repoRel;
  return path.join(os.homedir(), script);
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

  const pythonBin = resolvePython();
  if (!pythonBin) {
    console.error('[api] python3/python not found on PATH — cannot start API server; sidebar will be empty');
    return;
  }
  // When packaged, server script is unpacked from app.asar (see package.json asarUnpack).
  const serverPath = resolveServerScript()
    .replace(`${path.sep}app.asar${path.sep}`, `${path.sep}app.asar.unpacked${path.sep}`);
  if (!fs.existsSync(serverPath)) {
    console.error(`[api] server script not found: ${serverPath}`);
    return;
  }
  console.log(`[api] starting: ${pythonBin} ${serverPath}`);
  const spawnOpts = { detached: true, stdio: 'ignore' };
  if (IS_WIN) spawnOpts.windowsHide = true;
  const server = spawn(pythonBin, ['-u', serverPath], spawnOpts);
  server.unref();
  for (let i = 0; i < 8; i++) {
    await new Promise((r) => setTimeout(r, 500));
    try { await fetch(`${API_URL}/api/sessions`); return; } catch {}
  }
  console.error('[api] server did not become reachable in time');
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
  // When packaged, mic-server is unpacked from app.asar (see package.json asarUnpack).
  // Python can't execute scripts from inside the asar archive, so redirect to the
  // unpacked copy. When running from source (npm start), __dirname has no 'app.asar'
  // segment and the replace is a no-op.
  const micScript = path
    .join(__dirname, 'mic-server', 'mic_server.py')
    .replace(`${path.sep}app.asar${path.sep}`, `${path.sep}app.asar.unpacked${path.sep}`);
  const pythonCmd = CONFIG.micServerPython || (IS_WIN ? 'python' : 'python3');
  const env = { ...process.env, MIC_SERVER_START_MODE: 'on' };
  console.log(`[${CONFIG.appName}] Starting mic server: ${pythonCmd} ${micScript}`);
  const proc = spawn(pythonCmd, ['-u', micScript], { detached: true, stdio: 'ignore', env });
  proc.unref();
}

// ── Usage refresh ───────────────────────────────────────────────
// The usage footer reads from the host serving the Python API: local on the
// mac-mini host, or remote when Pentacle is in client mode. Refresh the cache
// on that host so the renderer can always fetch current limits.
function usageRefreshHost() {
  if (!CONFIG.features.usage) return null;
  if (IS_CLIENT) return hostRemote || null;
  if (IS_DARWIN) return hostLocal || null;
  return null;
}

function homeDirForHost(host) {
  if (host === hostRemote && CONFIG.remote?.user) return `/Users/${CONFIG.remote.user}`;
  if (host === hostLocal && IS_WIN && CONFIG.localWsl?.user) return `/home/${CONFIG.localWsl.user}`;
  return os.homedir();
}

async function refreshUsageScript(scriptName) {
  const host = usageRefreshHost();
  if (!host) return;
  const cfgPy = CONFIG.apiServer && CONFIG.apiServer.python;
  if (!cfgPy) return;
  const home = homeDirForHost(host);
  const pythonPath = path.isAbsolute(cfgPy)
    ? cfgPy
    : path.posix.join(home, String(cfgPy).replace(/\\/g, '/'));
  const scriptPath = path.posix.join(home, 'telegram-claude-bot/abilities', scriptName);

  if (host instanceof LocalHost) {
    if (!fs.existsSync(pythonPath) || !fs.existsSync(scriptPath)) return;
    const env = { ...process.env, PATH: `/opt/homebrew/bin:${process.env.PATH}` };
    const proc = spawn(pythonPath, [scriptPath, '--save'], { detached: true, stdio: 'ignore', env });
    proc.unref();
    return;
  }

  const shellScript = `
export PATH="/opt/homebrew/bin:$PATH"
[ -x ${JSON.stringify(pythonPath)} ] || exit 0
[ -f ${JSON.stringify(scriptPath)} ] || exit 0
nohup ${JSON.stringify(pythonPath)} ${JSON.stringify(scriptPath)} --save >/dev/null 2>&1 &
`.trim();
  try {
    await host.exec(['/bin/bash', '-lc', shellScript], { lane: 'bg' });
  } catch {}
}

function refreshUsageData() {
  void refreshUsageScript('check_usage.py');
}

function refreshCodexUsageData() {
  void refreshUsageScript('check_codex_usage.py');
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

async function createMainWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
    return mainWindow;
  }

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

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  sessionManager.setWebContents(mainWindow.webContents);
  await mainWindow.webContents.session.clearCache();
  mainWindow.loadFile('renderer/index.html');

  const savedSlots = loadSlotState();
  mainWindow.webContents.on('did-finish-load', () => {
    setTimeout(() => {
      let delay = 0;
      savedSlots.forEach((s, slot) => {
        if (!s || !s.name) return;
        const host = HOSTS[s.hostId] || hostLocal;
        if (!host) return;
        (async () => {
          try {
            if (host instanceof LocalHost) {
              execSync(`${host.tmuxBin} has-session -t ${JSON.stringify('=' + s.name)}`, { stdio: 'ignore', env: host.env });
            } else {
              await host.tmux(['has-session', '-t', '=' + s.name]);
            }
            setTimeout(() => {
              if (mainWindow && !mainWindow.isDestroyed()) {
                mainWindow.webContents.send('assign-slot', slot, s.name, s.hostId || 'local');
              }
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

  return mainWindow;
}

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

function homeDirForTargetHost(location, targetHost) {
  if (targetHost?.id === 'remote' && CONFIG.remote?.user) {
    return `/Users/${CONFIG.remote.user}`;
  }

  if (process.platform === 'win32' && targetHost?.id === 'local') {
    const wslUser = CONFIG.localWsl?.user || 'root';
    return wslUser === 'root' ? '/root' : `/home/${wslUser}`;
  }

  const peer = Array.isArray(CONFIG.peers) ? CONFIG.peers.find((p) => p && p.id === targetHost?.id) : null;
  if (peer?.user) {
    const isLikelyMac = String(peer.tmux || '').includes('/opt/homebrew/') || String(peer.host || '').includes('.local');
    return isLikelyMac ? `/Users/${peer.user}` : `/home/${peer.user}`;
  }

  return process.env.HOME || '~';
}

function resolveAgentWorkDir(location, targetHost) {
  const configured = CONFIG.workingDirectory || '~/agent-workspace';
  if (!String(configured).startsWith('~')) return configured;

  return configured.replace(/^~/, homeDirForTargetHost(location, targetHost));
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
  // Error window was already registered above — skip normal startup.
  if (_configLoadError) return;

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
  await createMainWindow();

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
    const targetHost = HOSTS[location] || hostLocal;
    if (!targetHost) {
      console.warn(`[new-session] unknown host: ${location}`);
      return null;
    }
    const isRemoteHost = targetHost.id !== 'local';
    const agentConfig = CONFIG.agents[agent] || CONFIG.agents.claude;
    // Optional per-location override. Useful for WSL-as-root where
    // `claude --dangerously-skip-permissions` is refused — user can set
    // `agents.claude.commandLocal = 'claude'` to sidestep the flag.
    const locationCommand = getCodexCommandForLocation(agentConfig, location);

    if (agent === 'codex' && targetHost) {
      const updateResult = await ensureCodexUpdatedForHost(targetHost, targetHost.id || location, locationCommand);
      maybeRefreshCodexUsageAfterUpdate(updateResult);
    }

    // Shell-first fallback creator. Creates an interactive bash session with
    // no command, then send-keys the agent command. Session survives the
    // agent exiting (shell stays alive). Used whenever agent-tmux CLI isn't
    // available on the target host.
    async function shellFirstCreate(host, workDir, command, { autoAcceptStartup = true } = {}) {
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
      const envPreludeParts = [
        // Remote macOS shells launched via tmux can miss Homebrew and user-local
        // bins under non-interactive bash login shells. Normalize PATH first so
        // agent commands like `codex` and `claude` resolve consistently.
        'export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"',
      ];
      if (isWsl) envPreludeParts.push('export BROWSER=wslview');
      const envPrelude = `${envPreludeParts.join('; ')}; `;
      const cdLine = workDir ? `cd ${JSON.stringify(workDir)} 2>/dev/null; clear; ` : '';
      // Plain name here — tmux's `=exact` prefix applies to session targets
      // (has-session, display-message) but not pane targets (send-keys).
      await host.tmux(['send-keys', '-t', sessionName, `${envPrelude}${cdLine}${command}`, 'Enter']);
      // Some agents can show startup trust prompts that block the session.
      // Keep the delayed Enter opt-in so Codex sessions do not accidentally
      // submit half-written input once the session is live.
      if (autoAcceptStartup) {
        setTimeout(async () => {
          try { await host.tmux(['send-keys', '-t', sessionName, '', 'Enter']); } catch {}
        }, 3000);
      }
    }

    try {
      if (targetHost.id === 'remote' && IS_CLIENT && hostRemote) {
        const workDir = resolveAgentWorkDir(location, targetHost);
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
          await shellFirstCreate(hostRemote, workDir, command, { autoAcceptStartup: agent !== 'codex' });
        }
        return { sessionName, hostId: targetHost.id };
      } else if (targetHost.id === 'local' && !IS_CLIENT && IS_DARWIN) {
        // Host mode on the mac-mini — call agent-tmux CLI locally.
        const workDir = resolveAgentWorkDir(location, targetHost);
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
          await shellFirstCreate(hostLocal, workDir, command, { autoAcceptStartup: agent !== 'codex' });
        }
        return { sessionName, hostId: targetHost.id };
      } else {
        // General shell-first path for local clients and SSH peers.
        const workDir = resolveAgentWorkDir(location, targetHost);
        // Create the working directory if it doesn't exist yet (fresh install).
        if (isRemoteHost) {
          try { await targetHost.exec(['/bin/bash', '-lc', `mkdir -p ${JSON.stringify(workDir)}`]); } catch {}
        } else {
          try { fs.mkdirSync(workDir, { recursive: true }); } catch {}
        }
        await shellFirstCreate(targetHost, workDir, locationCommand, { autoAcceptStartup: agent !== 'codex' });
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
    hostname: os.hostname(),
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

  // Kill a tmux session on a specific host. Used by the trash flow so that
  // trashing actually removes the session from tmux (not just flips a
  // DynamoDB flag) — otherwise the session lives on forever, slot-state
  // rehydration finds it on the next launch, and it keeps reappearing on
  // every machine that has it pinned to a slot.
  ipcMain.handle('tmux:kill-session', async (_, hostId, sessionName) => {
    const host = HOSTS[hostId] || hostLocal;
    if (!host || !sessionName) return false;
    try {
      if (host instanceof LocalHost) {
        execSync(`${host.tmuxBin} kill-session -t ${JSON.stringify('=' + sessionName)}`,
          { stdio: 'ignore', timeout: 3000, env: host.env });
      } else {
        await host.tmux(['kill-session', '-t', '=' + sessionName]);
      }
      return true;
    } catch {
      // Session already gone, or tmux server down — either way the desired
      // end-state is reached.
      return false;
    }
  });

  // Enumerate tmux sessions on every registered host. The Python API only
  // serves one host (remote in client mode, local in host mode), so sessions
  // on the "other" host (e.g. WSL local when Pentacle runs as a Windows
  // client) are invisible to the sidebar unless we list them here directly.
  ipcMain.handle('tmux:list-sessions-by-host', async () => {
    const fmt = '#{session_name}|#{session_attached}|#{session_created}|#{window_name}';
    const out = {};
    for (const [id, host] of Object.entries(HOSTS)) {
      try {
        let raw;
        if (host instanceof LocalHost) {
          raw = execSync(`${host.tmuxBin} list-sessions -F ${JSON.stringify(fmt)}`,
            { encoding: 'utf8', timeout: 3000, env: host.env });
        } else {
          raw = await host.tmux(['list-sessions', '-F', fmt], { lane: 'bg' });
        }
        out[id] = raw.trim().split('\n').filter(Boolean).map((line) => {
          const parts = line.split('|');
          const [name, attached, created] = parts;
          const windowName = parts.slice(3).join('|'); // window name may contain |
          return { name, attached: attached === '1', created: Number(created) || 0, windowName };
        });
      } catch {
        // `tmux list-sessions` exits non-zero when no server is running — treat as empty.
        out[id] = [];
      }
    }
    return out;
  });

  // ── Dashboards — Dashboard Hub (see dashboard-hub spec §5.2) ────────────────
  // Hub client is initialised once per Pentacle instance, maintains a WS
  // connection to Bart, and caches envelopes in memory + on disk. IPC handlers
  // below are thin shims that extract the right envelope and add staleness
  // metadata (_transport_stale / _data_stale / _age_sec / _updated_at).
  if (CONFIG.dashboardHub) {
    hubClient.init(CONFIG, app);
  }

  function _hubEnvelope(dashboardId) {
    const env = hubClient.get(dashboardId);
    if (!env) return { error: 'no data yet from hub', _transport_stale: !hubClient.connected };
    const ageSec = (Date.now() - new Date(env.server_received_at).getTime()) / 1000;
    const ttl = env.freshness_ttl_sec != null ? env.freshness_ttl_sec : 300;
    return {
      ...env.data,
      _updated_at: env.updated_at,
      _server_received_at: env.server_received_at,
      _age_sec: ageSec,
      _transport_stale: !hubClient.connected,
      _data_stale: ageSec > ttl,
    };
  }

  ipcMain.handle('dashboard:pipeline-stats', (_evt, _batch) => {
    // v1: ignores batch arg — always returns hub's current snapshot.
    return _hubEnvelope('bart.foreclosure');
  });

  ipcMain.handle('dashboard:0dte-stats', (_evt, traderId) => {
    const env = hubClient.get('bart.0dte');
    if (!env) return { error: 'no data yet from hub', _transport_stale: !hubClient.connected };
    const ageSec = (Date.now() - new Date(env.server_received_at).getTime()) / 1000;
    const ttl = env.freshness_ttl_sec != null ? env.freshness_ttl_sec : 300;
    const base = {
      _updated_at: env.updated_at,
      _server_received_at: env.server_received_at,
      _age_sec: ageSec,
      _transport_stale: !hubClient.connected,
      _data_stale: ageSec > ttl,
    };
    if (!traderId) return { error: 'no trader_id provided', ...base };
    const snapshots = env.data?.snapshots || {};
    const snapshot = snapshots[traderId] || null;
    const snapshotTs = snapshot?.snapshot_ts ? Number(snapshot.snapshot_ts) : null;
    const snapshotAge = snapshotTs ? Math.max(0, (Date.now() - snapshotTs) / 1000) : null;
    return { snapshot, age_sec: snapshotAge, trader_id: traderId, ...base };
  });

  ipcMain.handle('dashboard:0dte-list-traders', () => {
    const env = hubClient.get('bart.0dte');
    if (!env) return { traders: [], error: 'no data yet from hub', _transport_stale: !hubClient.connected };
    const traders = env.data?.traders || [];
    return { traders, cached: false };
  });

  ipcMain.handle('dashboard:amaterasu-ocr-stats', () => {
    return _hubEnvelope('amaterasu.ocr');
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

app.on('activate', () => {
  if (_configLoadError) return;
  void createMainWindow();
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
