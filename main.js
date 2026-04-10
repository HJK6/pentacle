const { app, BrowserWindow, ipcMain, Menu, nativeImage } = require('electron');
const path = require('path');
const pty = require('node-pty');
const { spawn, execFile, execFileSync, execSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const { DynamoDBClient } = require('@aws-sdk/client-dynamodb');
const { DynamoDBDocumentClient, QueryCommand, ScanCommand } = require('@aws-sdk/lib-dynamodb');
const CONFIG = require('./pentacle.config.js');

// ── DynamoDB client for the 0DTE dashboard ──────────────────────────────────
// Reads AWS credentials from the standard chain: ~/.aws/credentials, env vars,
// or instance profile. The dashboard is read-only (Query + Scan), no PutItem.
const _ddbRaw = new DynamoDBClient({ region: 'us-east-1' });
const _ddb = DynamoDBDocumentClient.from(_ddbRaw);
const _DASHBOARD_TABLE = '0dte-snapshots';
// Cache the trader list for 60s to avoid scanning the table on every poll.
let _tradersCache = { ts: 0, traders: [] };
const _TRADERS_CACHE_TTL_MS = 60_000;

// Set app identity so Ghost OS and macOS recognize us properly
app.setName(CONFIG.appName);

const MAX_SLOTS = 4;

// Force UTF-8 locale in the Electron process itself BEFORE any tmux interaction.
// Electron launched from Dock/Finder has no LANG set (defaults to C locale).
// If a tmux server starts under C locale, wcwidth('❯') returns -1 and the char
// renders as __ for the lifetime of that server (can't be fixed without restart).
// Setting this early ensures any tmux server we start inherits UTF-8.
process.env.LANG = 'en_US.UTF-8';
process.env.LC_ALL = 'en_US.UTF-8';

const TMUX = '/opt/homebrew/bin/tmux';
const API_URL = CONFIG.apiServer.url;

const PTY_ENV = { ...process.env, TERM: 'xterm-256color' };

let meetingWindow = null;

// ── PTY Manager ────────────────────────────────────────────────

class PtyManager {
  constructor() {
    this.ptys = new Array(MAX_SLOTS).fill(null);
    this.webContents = null;
  }

  setWebContents(wc) {
    this.webContents = wc;
  }

  create(slot, sessionName, cols, rows) {
    this.kill(slot);

    // Verify session exists before spawning PTY
    try {
      execSync(`${TMUX} has-session -t "${sessionName}"`, { stdio: 'ignore' });
    } catch {
      console.warn(`[pty:create] session not found: ${sessionName}`);
      return null;
    }

    // Resolve immutable pane ID BEFORE spawning PTY. Session names are mutable
    // (the API server auto-renames them), but %pane_id (e.g. %5) is stable for
    // the lifetime of the pane. All tmux commands (scroll, send-keys, copy-mode)
    // should target pane ID, not session name.
    let paneId;
    try {
      paneId = execSync(`${TMUX} display-message -t "${sessionName}" -p "#{pane_id}"`, { encoding: 'utf8' }).trim();
    } catch {
      console.warn(`[pty:create] failed to resolve pane_id for: ${sessionName}`);
      return null;
    }

    const p = pty.spawn(TMUX, ['attach-session', '-t', sessionName], {
      name: 'xterm-256color',
      cols: cols || 80,
      rows: rows || 24,
      cwd: process.env.HOME,
      env: PTY_ENV,
    });

    // Keep tmux mouse OFF so xterm.js handles click-drag selection natively.
    // Scroll is handled separately via pty:scroll IPC → tmux copy-mode commands.
    // Target by pane ID (immune to session renames).
    try {
      execSync(`${TMUX} set-option -t "${paneId}" mouse off`, { stdio: 'ignore' });
    } catch (e) {
      console.warn(`[pty:create] mouse off failed for ${paneId}:`, e.message);
    }

    const entry = { pty: p, sessionName, paneId };
    this.ptys[slot] = entry;

    p.onData((data) => {
      // Guard: only forward if this PTY is still the active one for this slot
      if (this.ptys[slot] === entry && this.webContents && !this.webContents.isDestroyed()) {
        this.webContents.send('pty:data', slot, data);
      }
    });

    p.onExit(({ exitCode }) => {
      // Only process exit if this PTY is still the active one for this slot.
      // When kill() is called (e.g. during slot replacement), it sets
      // this.ptys[slot] = null before the async onExit fires. Without this
      // guard, the stale exit event would reach the renderer and detach the
      // NEW session that replaced this one.
      if (this.ptys[slot] !== entry) return;
      this.ptys[slot] = null;
      if (this.webContents && !this.webContents.isDestroyed()) {
        this.webContents.send('pty:exit', slot, exitCode);
      }
    });
    return paneId;
  }

  write(slot, data) {
    if (this.ptys[slot]) {
      this.ptys[slot].pty.write(data);
    }
  }

  resize(slot, cols, rows) {
    if (this.ptys[slot]) {
      try {
        this.ptys[slot].pty.resize(cols, rows);
      } catch (e) {
        // ignore resize errors on dead ptys
      }
    }
  }

  kill(slot) {
    if (this.ptys[slot]) {
      // Do NOT restore tmux mouse on kill. Mouse off is set once in create()
      // and stays off for the session's lifetime. Toggling mouse on/off during
      // kill/create cycles was the root cause of recurring scroll failures —
      // session-scoped tmux state got stuck in the wrong mode when
      // attach/detach/reconnect sequences interleaved.
      try {
        this.ptys[slot].pty.kill();
      } catch (e) {
        // already dead
      }
      this.ptys[slot] = null;
    }
  }

  killAll() {
    for (let i = 0; i < MAX_SLOTS; i++) this.kill(i);
  }
}

const ptyManager = new PtyManager();

// ── Ensure API Server ──────────────────────────────────────────

async function ensureApiServer() {
  try {
    await fetch(`${API_URL}/api/sessions`);
    return;
  } catch {
    // Server not running — start it
    const serverPath = path.join(process.env.HOME, CONFIG.apiServer.script);
    const pythonPath = path.join(process.env.HOME, CONFIG.apiServer.python);
    const server = spawn(pythonPath, [serverPath], {
      detached: true,
      stdio: 'ignore',
    });
    server.unref();

    // Wait up to 4 seconds
    for (let i = 0; i < 8; i++) {
      await new Promise((r) => setTimeout(r, 500));
      try {
        await fetch(`${API_URL}/api/sessions`);
        return;
      } catch {}
    }
    console.error('Failed to start API server');
  }
}

// ── Context Menu ───────────────────────────────────────────────

function showContextMenu(win, sessionName, displayName) {
  const template = [
    { label: `Open in Slot 1`, click: () => win.webContents.send('assign-slot', 0, sessionName) },
    { label: `Open in Slot 2`, click: () => win.webContents.send('assign-slot', 1, sessionName) },
    { label: `Open in Slot 3`, click: () => win.webContents.send('assign-slot', 2, sessionName) },
    { label: `Open in Slot 4`, click: () => win.webContents.send('assign-slot', 3, sessionName) },
    { type: 'separator' },
    { label: 'Rename...', click: () => win.webContents.send('action', 'rename', sessionName, displayName) },
    { label: 'Trash', click: () => win.webContents.send('action', 'trash', sessionName) },
  ];
  Menu.buildFromTemplate(template).popup({ window: win });
}

// ── App ────────────────────────────────────────────────────────

let mainWindow;
const SLOT_STATE_FILE = path.join(app.getPath('userData'), '.slot-state.json');

function saveSlotState() {
  try {
    const slots = ptyManager.ptys.map(p => p ? p.sessionName : null);
    require('fs').writeFileSync(SLOT_STATE_FILE, JSON.stringify(slots));
  } catch {}
}

function loadSlotState() {
  try {
    return JSON.parse(require('fs').readFileSync(SLOT_STATE_FILE, 'utf8'));
  } catch { return [null, null, null, null]; }
}

// ── Auto-start Mic Server ─────────────────────────────────────
//
// Mic server runs out of /Applications/MicServer.app which has its own TCC
// microphone permission (com.bartimaeus.mic-server). We MUST launch it via
// `open -a` rather than spawning python directly — a bare python child has
// no TCC identity and macOS silently returns zero audio. The .app's Swift
// launcher inherits TCC, sets MIC_SERVER_START_MODE=on, and execs python.
async function ensureMicServer() {
  if (!CONFIG.features.mic) return;
  const micUrl = CONFIG.micServerUrl || 'http://127.0.0.1:7780';
  try {
    await fetch(`${micUrl}/status`);
    console.log(`[${CONFIG.appName}] Mic server already running`);
    return;
  } catch {}

  console.log(`[${CONFIG.appName}] Starting mic server via MicServer.app...`);
  const launcher = spawn('/usr/bin/open', ['-a', '/Applications/MicServer.app'], {
    detached: true,
    stdio: 'ignore',
  });
  launcher.unref();

  for (let i = 0; i < 20; i++) {
    await new Promise(r => setTimeout(r, 500));
    try {
      await fetch(`${micUrl}/status`);
      console.log(`[${CONFIG.appName}] Mic server started`);
      return;
    } catch {}
  }
  console.error(`[${CONFIG.appName}] Failed to start mic server`);
}

// ── Auto-refresh Usage Data ───────────────────────────────────
function refreshUsageData() {
  if (!CONFIG.features.usage) return;
  const pythonPath = path.join(process.env.HOME, CONFIG.apiServer.python);
  const scriptPath = path.join(process.env.HOME, 'telegram-claude-bot/abilities/check_usage.py');
  const env = { ...process.env, PATH: `/opt/homebrew/bin:${process.env.PATH}` };

  const proc = spawn(pythonPath, [scriptPath, '--save'], {
    detached: true,
    stdio: 'ignore',
    env,
  });
  proc.unref();
  console.log(`[${CONFIG.appName}] Usage refresh triggered`);
}

function refreshCodexUsageData() {
  if (!CONFIG.features.usage) return;
  const pythonPath = path.join(process.env.HOME, CONFIG.apiServer.python);
  const scriptPath = path.join(process.env.HOME, 'telegram-claude-bot/abilities/check_codex_usage.py');
  const env = { ...process.env, PATH: `/opt/homebrew/bin:${process.env.PATH}` };

  const proc = spawn(pythonPath, [scriptPath, '--save'], {
    detached: true,
    stdio: 'ignore',
    env,
  });
  proc.unref();
  console.log(`[${CONFIG.appName}] Codex usage refresh triggered`);
}

app.whenReady().then(async () => {
  // Application menu — needed for Cmd+C/V/X/A to work in Electron
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { role: 'appMenu' },
    { role: 'editMenu' },
  ]));

  await ensureApiServer();

  // Ensure tmux server has UTF-8 locale for proper Unicode width calculations.
  // Without this, ❯ and other Unicode chars render as __ in terminals.
  // CRITICAL: set-environment only affects programs INSIDE tmux, not the tmux
  // server's own wcwidth(). If the server started under C locale (e.g. from
  // another app or before Pentacle set LANG), it must be restarted.
  try {
    // Check if tmux server is running and what locale it inherited
    const serverPid = execSync(`${TMUX} display-message -p "#{pid}"`, { encoding: 'utf8' }).trim();
    // Read the server process's actual environment to check its locale
    const serverEnv = execSync(`ps -p ${serverPid} -o command= -E 2>/dev/null || true`, { encoding: 'utf8' });
    // More reliable: check if tmux server can handle ❯ correctly by testing wcwidth
    // If LANG was C/POSIX when server started, wcwidth('❯') returns -1
    const langCheck = execSync(`${TMUX} display-message -p "#{client_termname}"`, { encoding: 'utf8', timeout: 2000 }).trim();
    // The definitive test: ask tmux to print ❯ and see if it measures correctly
    // Instead, just check if the server's start environment had LANG set
    const serverStartEnv = execSync(`/bin/ps eww -p ${serverPid} 2>/dev/null || true`, { encoding: 'utf8' });
    const hasUtf8 = /LANG=.*[Uu][Tt][Ff]/.test(serverStartEnv);
    if (!hasUtf8) {
      console.log(`[${CONFIG.appName}] tmux server started without UTF-8 locale — restarting for proper Unicode support`);
      // Save session list before killing
      try {
        execSync(`${TMUX} kill-server`, { stdio: 'ignore', timeout: 3000 });
      } catch {}
      // Small delay for server to fully exit
      execSync('sleep 0.3');
      // Start a fresh tmux server (inherits our UTF-8 env from process.env)
      execSync(`${TMUX} new-session -d -s _app_keepalive`, { env: PTY_ENV, timeout: 3000 });
      console.log(`[${CONFIG.appName}] tmux server restarted with UTF-8 locale`);
    } else {
      // Server already has UTF-8 — just set global env as extra safety
      execSync(`${TMUX} set-environment -g LANG "en_US.UTF-8"`, { stdio: 'ignore' });
      execSync(`${TMUX} set-environment -g LC_ALL "en_US.UTF-8"`, { stdio: 'ignore' });
    }
  } catch {
    // tmux server may not be running yet — that's fine, PTY_ENV handles new sessions
    // Start one now so it inherits our UTF-8 locale
    try {
      execSync(`${TMUX} new-session -d -s _app_keepalive`, { env: PTY_ENV, timeout: 3000 });
    } catch {}
  }

  mainWindow = new BrowserWindow({
    width: 1600,
    height: 1000,
    minWidth: 900,
    minHeight: 600,
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 15, y: 15 },
    backgroundColor: CONFIG.dark.bg,
    icon: nativeImage.createFromPath(path.join(__dirname, 'assets', 'icon.icns')),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: false,
      nodeIntegration: true,
    },
  });

  ptyManager.setWebContents(mainWindow.webContents);

  // Disable renderer cache so code changes take effect immediately
  await mainWindow.webContents.session.clearCache();


  mainWindow.loadFile('renderer/index.html');

  // Restore previous slots after renderer is fully ready
  const savedSlots = loadSlotState();
  mainWindow.webContents.on('did-finish-load', () => {
    // Wait for renderer JS to initialize (fetchSessions, event handlers, etc.)
    // did-finish-load fires when HTML is parsed, but app.js may still be executing
    setTimeout(() => {
      let delay = 0;
      savedSlots.forEach((sessionName, slot) => {
        if (sessionName) {
          const { execSync } = require('child_process');
          try {
            execSync(`${TMUX} has-session -t "${sessionName}"`, { stdio: 'ignore' });
            setTimeout(() => {
              mainWindow.webContents.send('assign-slot', slot, sessionName);
            }, delay);
            delay += 500;
          } catch {}
        }
      });
    }, 1000);

    // Auto-start mic server if not already running
    ensureMicServer();

    // Auto-refresh usage data on startup
    refreshUsageData();
    refreshCodexUsageData();
  });

  // IPC handlers
  ipcMain.handle('pty:create', (_, slot, sessionName, cols, rows) => {
    const result = ptyManager.create(slot, sessionName, cols, rows);
    saveSlotState();
    return result;
  });

  ipcMain.on('pty:write', (_, slot, data) => {
    ptyManager.write(slot, data);
  });

  ipcMain.on('pty:resize', (_, slot, cols, rows) => {
    ptyManager.resize(slot, cols, rows);
  });

  ipcMain.handle('pty:kill', (_, slot) => {
    ptyManager.kill(slot);
    saveSlotState();
  });

  // Check if a tmux session still exists (for auto-reconnect after PTY death)
  ipcMain.handle('pty:check-session', (_, sessionName) => {
    try {
      execSync(`${TMUX} has-session -t "${sessionName}"`, { stdio: 'ignore' });
      return true;
    } catch { return false; }
  });

  ipcMain.handle('pty:new-session', (_, agent) => {
    // Delegate to the shared `agent-tmux` CLI so Pentacle and Python callers
    // (run_pipeline.py, orchestrator, stage agents) all go through the same
    // tmux-spawn primitive. See agent-dashboard/modules/AgentTmux.py for the
    // real implementation and agent-dashboard/bin/agent-tmux for the CLI.
    //
    // Behavior preserved: the CLI uses the same shell-first pattern, so the
    // tmux session survives even if the agent command crashes on startup.
    const sessionName = `${agent}-${process.pid}-${Date.now()}`;
    try {
      const workDir = CONFIG.workingDirectory.replace(/^~/, process.env.HOME);
      const agentConfig = CONFIG.agents[agent] || CONFIG.agents.claude;
      let command;
      if (agentConfig.binary) {
        const bin = agentConfig.binary.replace(/^~/, process.env.HOME);
        const flags = agentConfig.command.split(' ').slice(1).join(' ');
        command = `${bin}${flags ? ' ' + flags : ''}`;
      } else {
        command = agentConfig.command;
      }
      // Pass everything as explicit args to avoid shell quoting issues.
      execFileSync(
        '/Users/bartimaeus/agent-dashboard/bin/agent-tmux',
        ['create-session',
         '--name', sessionName,
         '--workdir', workDir,
         '--command', command],
        { env: PTY_ENV, stdio: ['ignore', 'pipe', 'pipe'] }
      );
    } catch (e) {
      console.error('Failed to create tmux session:', e.message);
      return null;
    }
    return sessionName;
  });

  ipcMain.on('context-menu', (_, sessionName, displayName) => {
    showContextMenu(mainWindow, sessionName, displayName);
  });

  // Scroll tmux scrollback via copy-mode.
  // Receives the immutable tmux pane ID (e.g. %5) from the renderer — not the
  // session name, which can be renamed at any time. Pane IDs are stable for the
  // lifetime of the pane, so scroll works even after session renames.
  // Uses tmux ";" command separator for atomic copy-mode + send-keys in one call.
  ipcMain.on('pty:scroll', (_, paneId, direction, lines) => {
    if (!paneId) return;
    const count = Math.min(lines || 1, 50);
    const cmd = direction === 'up' ? 'scroll-up' : 'scroll-down';
    execFile(TMUX, [
      'copy-mode', '-e', '-t', paneId, ';',
      'send-keys', '-t', paneId, '-X', '-N', String(count), cmd,
    ], (err) => {
      if (err) console.warn(`[scroll:fail] pane=${paneId} dir=${direction} err=${err.message}`);
    });
  });

  // Exit copy-mode so typing goes to the shell
  // Send tmux key notation directly to a pane (bypasses terminal input parser)
  ipcMain.on('pty:tmux-send', (_, slot, ...keys) => {
    const entry = ptyManager.ptys[slot];
    if (!entry) return;
    execFile(TMUX, ['send-keys', '-t', entry.paneId, ...keys], { stdio: 'ignore' }, () => {});
  });

  // Expose config to renderer
  ipcMain.handle('get-config', () => CONFIG);

  ipcMain.handle('mic:start-server', async () => {
    if (!CONFIG.features.mic) return false;
    const micUrl = CONFIG.micServerUrl || 'http://127.0.0.1:7780';
    // Check if mic server is already running
    try {
      await fetch(`${micUrl}/status`);
      return true; // already running
    } catch {}

    // Launch via MicServer.app — bare python child has no TCC identity and
    // produces silent zero audio. See ensureMicServer() comment above.
    const launcher = spawn('/usr/bin/open', ['-a', '/Applications/MicServer.app'], {
      detached: true,
      stdio: 'ignore',
    });
    launcher.unref();

    // Wait up to 10 seconds for it to come up (Swift launcher + python load)
    for (let i = 0; i < 20; i++) {
      await new Promise(r => setTimeout(r, 500));
      try {
        await fetch(`${micUrl}/status`);
        return true;
      } catch {}
    }
    return false;
  });

  ipcMain.on('meeting:open', () => {
    if (meetingWindow && !meetingWindow.isDestroyed()) {
      meetingWindow.focus();
      return;
    }
    meetingWindow = new BrowserWindow({
      width: 600,
      height: 500,
      minWidth: 400,
      minHeight: 300,
      titleBarStyle: 'hiddenInset',
      trafficLightPosition: { x: 12, y: 12 },
      backgroundColor: CONFIG.dark.bg,
      webPreferences: {
        contextIsolation: false,
        nodeIntegration: true,
      },
    });
    meetingWindow.loadFile('renderer/meeting.html');
    meetingWindow.on('closed', () => { meetingWindow = null; });
  });

  ipcMain.on('meeting:close', () => {
    if (meetingWindow && !meetingWindow.isDestroyed()) {
      meetingWindow.close();
      meetingWindow = null;
    }
  });

  ipcMain.on('pty:exit-copy-mode', (_, slot) => {
    const entry = ptyManager.ptys[slot];
    if (!entry) return;
    execFile(TMUX, ['send-keys', '-t', entry.paneId, '-X', 'cancel'], { stdio: 'ignore' }, () => {});
  });

  // ── Activity Detection ──────────────────────────────────────
  // Capture tmux pane content for a session and classify as working/waiting/idle
  const SPINNER_RE = /[✻✢·⊹*❋◆◇◈⟡⟢⟣☼◉] [A-Z][a-z]+…/;
  const WORKING_STRINGS = ['Running…', 'ctrl+b ctrl+b', 'to run in background'];

  function classifyPane(content) {
    const lines = content.split('\n');
    const bottomLines = lines.slice(-4).map(l => l.trim()).filter(l => l);
    const isClaude = bottomLines.some(l => l.includes('⏵⏵ bypass') || l.includes('⏵⏵ auto-accept'));
    if (!isClaude) return 'idle';

    // Find content above the ❯ prompt line
    let contentLines = [];
    for (let i = lines.length - 1; i >= 0; i--) {
      if (lines[i].includes('❯')) {
        contentLines = lines.slice(Math.max(0, i - 15), i);
        break;
      }
    }
    if (!contentLines.length) contentLines = lines.slice(-20);
    const contentText = contentLines.join('\n');

    const isWorking = SPINNER_RE.test(contentText) || WORKING_STRINGS.some(p => contentText.includes(p));
    return isWorking ? 'working' : 'waiting';
  }

  ipcMain.handle('pty:detect-activity', async () => {
    // Get all tmux sessions and classify each
    const states = {};
    try {
      const result = execSync(`${TMUX} list-windows -a -F "#{session_name}:#{window_index}"`, { encoding: 'utf8', timeout: 3000 });
      const targets = result.trim().split('\n').filter(t => t);

      for (const target of targets) {
        try {
          const cap = execSync(`${TMUX} capture-pane -t "${target}" -p`, { encoding: 'utf8', timeout: 2000 });
          states[target] = classifyPane(cap);
        } catch { states[target] = 'idle'; }
      }
    } catch {}
    return states;
  });

  // Capture pane content for sidebar summaries
  ipcMain.handle('pty:capture-all-panes', async () => {
    const panes = {};
    try {
      const result = execSync(`${TMUX} list-windows -a -F "#{session_name}:#{window_index}"`, { encoding: 'utf8', timeout: 3000 });
      const targets = result.trim().split('\n').filter(t => t);

      for (const target of targets) {
        try {
          const cap = execSync(`${TMUX} capture-pane -t "${target}" -p -S -500`, { encoding: 'utf8', timeout: 2000 });
          panes[target] = cap;
        } catch {}
      }
    } catch {}
    return panes;
  });

  // ── Dashboard Data ──────────────────────────────────────────
  ipcMain.handle('dashboard:pipeline-stats', async () => {
    return new Promise((resolve) => {
      const env = {
        ...process.env,
        PYTHONPATH: '/Users/bartimaeus/land-bot',
        DATABASE_URL: 'postgresql://bartimaeus@localhost:5432/altum',
      };
      const proc = spawn('/Users/bartimaeus/.venvs/global/bin/python', [
        '/Users/bartimaeus/land-bot/scripts/pipeline_stats.py',
      ], { env });
      let stdout = '';
      let stderr = '';
      const timer = setTimeout(() => { proc.kill('SIGKILL'); }, 15000);
      proc.stdout.on('data', d => { stdout += d; });
      proc.stderr.on('data', d => { stderr += d; });
      proc.on('close', (code) => {
        clearTimeout(timer);
        if (code === 0) {
          try { resolve(JSON.parse(stdout.trim())); }
          catch (e) { resolve({ error: 'Invalid JSON: ' + stdout.slice(0, 100) }); }
        } else { resolve({ error: (stderr || '').slice(0, 200) || `Stats script exit ${code}` }); }
      });
      proc.on('error', (e) => { clearTimeout(timer); resolve({ error: e.message }); });
    });
  });

  // ── 0DTE dashboard: read latest snapshot for one trader from DynamoDB ──
  // Replaces the legacy Python subprocess that hit the bot's local HTTP API.
  // The Trader process publishes snapshots to 0dte-snapshots every 5s; this
  // handler queries the latest one for the requested trader_id. Multi-trader
  // support is built in via the trader_id partition key.
  //
  // Returns { snapshot, age_sec, error } where snapshot is the raw DynamoDB
  // item (positions_by_account, circuit, today_stats, last_scan, etc.) and
  // age_sec is how stale the snapshot is. The renderer uses age to color a
  // freshness indicator. error is set if the query fails.
  ipcMain.handle('dashboard:0dte-stats', async (_evt, traderId) => {
    if (!traderId) {
      return { error: 'no trader_id provided' };
    }
    try {
      const resp = await _ddb.send(new QueryCommand({
        TableName: _DASHBOARD_TABLE,
        KeyConditionExpression: 'trader_id = :t',
        ExpressionAttributeValues: { ':t': traderId },
        ScanIndexForward: false,
        Limit: 1,
      }));
      const items = resp.Items || [];
      if (items.length === 0) {
        return { snapshot: null, age_sec: null, trader_id: traderId };
      }
      const snapshot = items[0];
      const ageSec = Math.max(0, (Date.now() - Number(snapshot.snapshot_ts)) / 1000);
      return { snapshot, age_sec: ageSec, trader_id: traderId };
    } catch (e) {
      return { error: `dynamodb query failed: ${e.message || e}` };
    }
  });

  // ── 0DTE dashboard: discover known traders ────────────────────────────
  // Scans the 0dte-snapshots table with a projection of just trader_id and
  // dedupes. Cached for 60s to avoid scanning on every poll. Returns the
  // list sorted alphabetically.
  //
  // For a small table (a handful of traders, ~thousands of snapshots) this
  // is fine — the Scan reads each item but the projection keeps it small.
  // If the table grows large we'll add a GSI.
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
        for (const it of (resp.Items || [])) {
          if (it.trader_id) traders.add(it.trader_id);
        }
        lastKey = resp.LastEvaluatedKey;
      } while (lastKey);
      const list = Array.from(traders).sort();
      _tradersCache = { ts: now, traders: list };
      return { traders: list, cached: false };
    } catch (e) {
      return { error: `dynamodb scan failed: ${e.message || e}`, traders: _tradersCache.traders };
    }
  });

  // ── Image Paste ─────────────────────────────────────────────
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
  // Save slot state BEFORE killing PTYs — killAll nulls the entries
  saveSlotState();
  ptyManager.killAll();
  app.quit();
});

app.on('before-quit', () => {
  saveSlotState();
  ptyManager.killAll();
  if (meetingWindow && !meetingWindow.isDestroyed()) {
    meetingWindow.close();
  }
});
