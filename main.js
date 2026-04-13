const { app, BrowserWindow, ipcMain, Menu, nativeImage } = require('electron');
const path = require('path');
const { spawn, execSync, execFile } = require('child_process');
const fs = require('fs');
const os = require('os');

// node-pty only on Mac (server mode); ssh2 on Windows (client mode)
let pty;
let Client;
const IS_WIN = process.platform === 'win32';
const IS_MAC = process.platform === 'darwin';

if (!IS_WIN) {
  pty = require('node-pty');
} else {
  Client = require('ssh2').Client;
}

app.setName('Pentacle');

// ── Platform & Config ─────────────────────────────────────────

let CONFIG = null;
try {
  CONFIG = JSON.parse(fs.readFileSync(path.join(__dirname, 'pentacle.config.json'), 'utf8'));
} catch {}

const IS_CLIENT = !!(CONFIG && CONFIG.remote);

const LOCAL_TMUX = IS_WIN ? 'tmux' : (IS_MAC ? '/opt/homebrew/bin/tmux' : 'tmux');
const REMOTE_TMUX = CONFIG?.remote?.tmux || '/opt/homebrew/bin/tmux';

// WSL SSH config for local sessions (Windows only)
const WSL_SSH_PORT = 2222;

// Login shell (-lc) sources .profile/.bashrc so claude and other user-installed
// CLIs land on PATH. LANG/LC_ALL are forced to en_US.UTF-8 so any tmux server
// started through this path inherits UTF-8 locale — without it, wcwidth('❯')
// returns -1 and tmux prints `__` for the lifetime of the server.
const WSL_ENV_PREFIX = 'export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8; ';

function wslExec(cmd) {
  if (IS_WIN) {
    return require('child_process').execFileSync('wsl', ['-d', 'Ubuntu', '--', 'bash', '-lc', WSL_ENV_PREFIX + cmd], { encoding: 'utf8' });
  }
  return execSync(cmd, { encoding: 'utf8' });
}

function wslExecAsync(cmd, callback) {
  if (IS_WIN) {
    execFile('wsl', ['-d', 'Ubuntu', '--', 'bash', '-lc', WSL_ENV_PREFIX + cmd], callback);
  } else {
    execFile('bash', ['-c', cmd], callback);
  }
}

// ── API Config ────────────────────────────────────────────────

const API_URL = 'http://localhost:7777';
const REMOTE_API_PORT = 7778;
let sshTunnelProcess = null;

function startSshTunnel() {
  if (!IS_CLIENT || !CONFIG.remote) return;

  const tunnelCmd = `ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -N -L ${REMOTE_API_PORT}:localhost:7777 ${CONFIG.remote.user}@${CONFIG.remote.host}`;

  if (IS_WIN) {
    sshTunnelProcess = spawn('wsl', ['-d', 'Ubuntu', '--', 'bash', '-c', tunnelCmd], { stdio: 'ignore' });
  } else {
    sshTunnelProcess = spawn('bash', ['-c', tunnelCmd], { stdio: 'ignore' });
  }

  sshTunnelProcess.on('exit', (code) => {
    console.log('SSH tunnel exited with code', code);
    setTimeout(startSshTunnel, 5000);
  });
}

function stopSshTunnel() {
  if (sshTunnelProcess) { sshTunnelProcess.kill(); sshTunnelProcess = null; }
}

// ── Ensure WSL sshd is running (Windows only) ─────────────────

function ensureWslSshd() {
  if (!IS_WIN) return;
  try {
    wslExec(`pgrep -f "sshd.*${WSL_SSH_PORT}" || /usr/sbin/sshd -p ${WSL_SSH_PORT}`);
  } catch {}
}

// ── SSH Helper (Windows) ──────────────────────────────────────

function getSshKey() {
  return fs.readFileSync(path.join(os.homedir(), '.ssh', 'id_ed25519'));
}

function sshConnect(host, port, user) {
  return new Promise((resolve, reject) => {
    const conn = new Client();
    conn.on('ready', () => resolve(conn));
    conn.on('error', (err) => reject(err));
    conn.connect({ host, port, username: user, privateKey: getSshKey() });
  });
}

function sshExec(conn, cmd) {
  return new Promise((resolve, reject) => {
    conn.exec(cmd, (err, stream) => {
      if (err) { conn.end(); return reject(err); }
      let out = '';
      stream.on('data', (d) => { out += d.toString(); });
      stream.on('close', () => { conn.end(); resolve(out); });
    });
  });
}

// ── PTY / SSH Session Manager ─────────────────────────────────

class SessionManager {
  constructor() {
    this.slots = [null, null, null, null]; // { stream/pty, conn?, sessionName, remote }
    this.webContents = null;
  }

  setWebContents(wc) { this.webContents = wc; }

  async create(slot, sessionName, remote = false) {
    this.kill(slot);

    if (IS_WIN) {
      // Windows: use ssh2 for everything
      return this._createSsh(slot, sessionName, remote);
    } else {
      // Mac: use node-pty
      return this._createPty(slot, sessionName, remote);
    }
  }

  async _createSsh(slot, sessionName, remote) {
    const host = remote ? CONFIG.remote.host : 'localhost';
    const port = remote ? 22 : WSL_SSH_PORT;
    const user = remote ? CONFIG.remote.user : 'root';
    const tmux = remote ? REMOTE_TMUX : LOCAL_TMUX;

    // Disable tmux mouse via a separate connection
    try {
      const mouseConn = await sshConnect(host, port, user);
      await sshExec(mouseConn, `${tmux} set-option -t "${sessionName}" mouse off`);
    } catch {}

    const conn = await sshConnect(host, port, user);
    // Force UTF-8 on the remote attach client. macOS sshd doesn't forward
    // LC_CTYPE, so the ssh session inherits C locale → tmux's client-side
    // wcwidth returns -1 for chars like U+23F5 (⏵) and it substitutes `_`.
    // `-u` forces UTF-8 mode; LANG/LC_ALL fix wcwidth for any non-ASCII glyph.
    const cmd = `LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 ${tmux} -u attach-session -t "${sessionName}"`;

    return new Promise((resolve, reject) => {
      conn.exec(cmd, { pty: { term: 'xterm-256color', cols: 80, rows: 24 } }, (err, stream) => {
        if (err) { conn.end(); return reject(err); }

        const entry = { stream, conn, sessionName, remote };
        this.slots[slot] = entry;

        // Use StringDecoder via setEncoding so UTF-8 sequences split across SSH
        // packet boundaries are buffered and reassembled — without this, `❯`
        // (3 bytes: e2 9d af) can be chopped in half and decoded as U+FFFD.
        stream.setEncoding('utf8');
        stream.stderr.setEncoding('utf8');

        stream.on('data', (data) => {
          if (this.slots[slot] === entry && this.webContents && !this.webContents.isDestroyed()) {
            this.webContents.send('pty:data', slot, data);
          }
        });

        stream.stderr.on('data', (data) => {
          if (this.slots[slot] === entry && this.webContents && !this.webContents.isDestroyed()) {
            this.webContents.send('pty:data', slot, data);
          }
        });

        stream.on('close', () => {
          if (this.slots[slot] === entry) this.slots[slot] = null;
          conn.end();
          if (this.webContents && !this.webContents.isDestroyed()) {
            this.webContents.send('pty:exit', slot, 0);
          }
        });

        resolve(true);
      });
    });
  }

  _createPty(slot, sessionName, remote) {
    let shell, args;

    if (IS_CLIENT && remote) {
      shell = 'bash';
      args = ['-c', `ssh -o StrictHostKeyChecking=no -t ${CONFIG.remote.user}@${CONFIG.remote.host} '${REMOTE_TMUX} attach-session -t "${sessionName}"'`];
    } else {
      shell = LOCAL_TMUX;
      args = ['attach-session', '-t', sessionName];
    }

    const p = pty.spawn(shell, args, {
      name: 'xterm-256color',
      cols: 80,
      rows: 24,
      cwd: process.env.HOME,
      env: { ...process.env, TERM: 'xterm-256color' },
    });

    // Disable tmux mouse
    const tmux = (IS_CLIENT && remote) ? REMOTE_TMUX : LOCAL_TMUX;
    if (IS_CLIENT && remote) {
      const mouseCmd = `ssh -o StrictHostKeyChecking=no ${CONFIG.remote.user}@${CONFIG.remote.host} '${tmux} set-option -t "${sessionName}" mouse off'`;
      execFile('bash', ['-c', mouseCmd], { stdio: 'ignore' }, () => {});
    } else {
      try { execSync(`${tmux} set-option -t "${sessionName}" mouse off`, { stdio: 'ignore' }); } catch {}
    }

    const entry = { pty: p, sessionName, remote };
    this.slots[slot] = entry;

    p.onData((data) => {
      if (this.slots[slot] === entry && this.webContents && !this.webContents.isDestroyed()) {
        this.webContents.send('pty:data', slot, data);
      }
    });

    p.onExit(({ exitCode }) => {
      if (this.slots[slot] === entry) this.slots[slot] = null;
      if (this.webContents && !this.webContents.isDestroyed()) {
        this.webContents.send('pty:exit', slot, exitCode);
      }
    });
    return true;
  }

  write(slot, data) {
    const e = this.slots[slot];
    if (!e) return;
    if (e.stream) e.stream.write(data);
    else if (e.pty) e.pty.write(data);
  }

  resize(slot, cols, rows) {
    const e = this.slots[slot];
    if (!e) return;
    try {
      if (e.stream) e.stream.setWindow(rows, cols, 0, 0);
      else if (e.pty) e.pty.resize(cols, rows);
    } catch {}
  }

  kill(slot) {
    const e = this.slots[slot];
    if (!e) return;

    const { sessionName, remote } = e;
    const tmux = remote ? REMOTE_TMUX : LOCAL_TMUX;

    // Re-enable tmux mouse
    if (IS_WIN) {
      if (remote) {
        const cmd = `ssh -o StrictHostKeyChecking=no ${CONFIG.remote.user}@${CONFIG.remote.host} '${tmux} set-option -t "${sessionName}" mouse on'`;
        wslExecAsync(cmd, () => {});
      } else {
        wslExecAsync(`${tmux} set-option -t "${sessionName}" mouse on`, () => {});
      }
    } else {
      if (IS_CLIENT && remote) {
        const cmd = `ssh -o StrictHostKeyChecking=no ${CONFIG.remote.user}@${CONFIG.remote.host} '${tmux} set-option -t "${sessionName}" mouse on'`;
        execFile('bash', ['-c', cmd], { stdio: 'ignore' }, () => {});
      } else {
        try { execSync(`${tmux} set-option -t "${sessionName}" mouse on`, { stdio: 'ignore' }); } catch {}
      }
    }

    if (e.stream) {
      try { e.stream.write('\x02d'); } catch {} // tmux detach
      setTimeout(() => {
        try { e.stream?.close(); } catch {}
        try { e.conn?.end(); } catch {}
      }, 200);
    } else if (e.pty) {
      try { e.pty.kill(); } catch {}
    }

    this.slots[slot] = null;
  }

  killAll() {
    for (let i = 0; i < 4; i++) this.kill(i);
  }
}

const sessionManager = new SessionManager();

// ── Session Listing ───────────────────────────────────────────

function parseTmuxList(raw) {
  return raw.trim().split('\n').filter(Boolean).map(line => {
    const [name, attached, created] = line.split('|');
    return {
      name, display_name: name,
      attached: parseInt(attached) > 0,
      type: name.startsWith('claude') ? 'claude' : name.match(/^[A-Z]/) ? 'agent' : 'other',
      created: parseInt(created) * 1000,
    };
  });
}

async function listLocalSessions() {
  try {
    const raw = wslExec(`${LOCAL_TMUX} ls -F '#{session_name}|#{session_attached}|#{session_created}'`);
    return parseTmuxList(raw).map(s => ({ ...s, source: 'local' }));
  } catch { return []; }
}

// ── Ensure API Server / SSH Tunnel ────────────────────────────

async function ensureApiServer() {
  if (IS_CLIENT) {
    if (IS_WIN) ensureWslSshd();
    startSshTunnel();
    for (let i = 0; i < 10; i++) {
      await new Promise((r) => setTimeout(r, 500));
      try { await fetch(`http://localhost:${REMOTE_API_PORT}/api/sessions`); console.log('SSH tunnel ready'); return; } catch {}
    }
    console.error('SSH tunnel not ready after 5s');
    return;
  }
  try { await fetch(`${API_URL}/api/sessions`); return; } catch {
    const serverPath = path.join(process.env.HOME, '.tmux/cmdcenter/server.py');
    const pythonPath = path.join(process.env.HOME, '.venvs/global/bin/python');
    const server = spawn(pythonPath, [serverPath], { detached: true, stdio: 'ignore' });
    server.unref();
    for (let i = 0; i < 8; i++) {
      await new Promise((r) => setTimeout(r, 500));
      try { await fetch(`${API_URL}/api/sessions`); return; } catch {}
    }
    console.error('Failed to start API server');
  }
}

// ── Context Menu ──────────────────────────────────────────────

function showContextMenu(win, sessionName, displayName) {
  Menu.buildFromTemplate([
    { label: 'Open in Slot 1', click: () => win.webContents.send('assign-slot', 0, sessionName) },
    { label: 'Open in Slot 2', click: () => win.webContents.send('assign-slot', 1, sessionName) },
    { label: 'Open in Slot 3', click: () => win.webContents.send('assign-slot', 2, sessionName) },
    { label: 'Open in Slot 4', click: () => win.webContents.send('assign-slot', 3, sessionName) },
    { type: 'separator' },
    { label: 'Rename...', click: () => win.webContents.send('action', 'rename', sessionName, displayName) },
    { label: 'Trash', click: () => win.webContents.send('action', 'trash', sessionName) },
  ]).popup({ window: win });
}

// ── Tmux Exec Helpers ─────────────────────────────────────────

function tmuxExec(sessionName, remote, tmuxArgs) {
  const tmux = remote ? REMOTE_TMUX : LOCAL_TMUX;
  const fullCmd = `${tmux} ${tmuxArgs}`;

  if (IS_WIN) {
    if (remote) {
      wslExecAsync(`ssh -o StrictHostKeyChecking=no ${CONFIG.remote.user}@${CONFIG.remote.host} '${fullCmd}'`, () => {});
    } else {
      wslExecAsync(fullCmd, () => {});
    }
  } else if (IS_CLIENT && remote) {
    execFile('bash', ['-c', `ssh -o StrictHostKeyChecking=no ${CONFIG.remote.user}@${CONFIG.remote.host} '${fullCmd}'`], { stdio: 'ignore' }, () => {});
  } else {
    execFile(tmux, tmuxArgs.split(' '), { stdio: 'ignore' }, () => {});
  }
}

// ── App ───────────────────────────────────────────────────────

let mainWindow;
let meetingWindow = null;
const SLOT_STATE_FILE = path.join(__dirname, '.slot-state.json');

function saveSlotState() {
  try {
    const slots = sessionManager.slots.map(e => e ? { name: e.sessionName, remote: e.remote } : null);
    fs.writeFileSync(SLOT_STATE_FILE, JSON.stringify(slots));
  } catch {}
}

function loadSlotState() {
  try {
    const data = JSON.parse(fs.readFileSync(SLOT_STATE_FILE, 'utf8'));
    return data.map(s => typeof s === 'string' ? { name: s, remote: false } : s);
  } catch { return [null, null, null, null]; }
}

app.whenReady().then(async () => {
  const menuTemplate = [{ role: 'editMenu' }];
  if (IS_MAC) menuTemplate.unshift({ role: 'appMenu' });
  Menu.setApplicationMenu(Menu.buildFromTemplate(menuTemplate));

  await ensureApiServer();

  const windowOpts = {
    width: 1600, height: 1000, minWidth: 900, minHeight: 600,
    backgroundColor: '#0c1310',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: false, nodeIntegration: true,
    },
  };
  if (IS_MAC) {
    windowOpts.titleBarStyle = 'hiddenInset';
    windowOpts.trafficLightPosition = { x: 15, y: 15 };
    windowOpts.icon = nativeImage.createFromPath(path.join(__dirname, 'assets', 'icon.icns'));
  }

  mainWindow = new BrowserWindow(windowOpts);
  sessionManager.setWebContents(mainWindow.webContents);
  await mainWindow.webContents.session.clearCache();
  mainWindow.loadFile('renderer/index.html');

  // Restore slots
  const savedSlots = loadSlotState();
  mainWindow.webContents.on('did-finish-load', () => {
    setTimeout(() => {
      let delay = 0;
      savedSlots.forEach((slotData, slot) => {
        if (slotData?.name) {
          setTimeout(() => mainWindow.webContents.send('assign-slot', slot, slotData.name, slotData.remote || false), delay);
          delay += 500;
        }
      });
    }, 1000);
  });

  // ── IPC Handlers ──────────────────────────────────────────

  ipcMain.handle('get-config', () => ({
    isClient: IS_CLIENT, isWin: IS_WIN, isMac: IS_MAC,
    remote: CONFIG?.remote || null,
    remoteApiPort: REMOTE_API_PORT,
    agents: CONFIG?.agents || {
      claude: { label: 'Claude', command: 'claude --dangerously-skip-permissions' },
      codex: { label: 'Codex', command: 'codex' },
    },
  }));

  ipcMain.handle('list-sessions', async () => await listLocalSessions());

  ipcMain.handle('pty:create', async (_, slot, sessionName, remote = false) => {
    const result = await sessionManager.create(slot, sessionName, remote);
    saveSlotState();
    return result;
  });

  ipcMain.on('pty:write', (_, slot, data) => sessionManager.write(slot, data));
  ipcMain.on('pty:resize', (_, slot, cols, rows) => sessionManager.resize(slot, cols, rows));

  ipcMain.handle('pty:kill', (_, slot) => {
    sessionManager.kill(slot);
    saveSlotState();
  });

  // Create new session — supports local and remote, agent type
  ipcMain.handle('pty:new-session', (_, agent, location = 'local') => {
    const agentConfig = CONFIG?.agents?.[agent] || { command: agent === 'codex' ? 'codex' : 'claude --dangerously-skip-permissions' };
    const sessionName = `${agent}-${process.pid}-${Date.now()}`;
    const command = agentConfig.command;

    try {
      if (location === 'remote' && IS_CLIENT) {
        // Create session on Mac Mini via SSH
        const cmd = `ssh -o StrictHostKeyChecking=no ${CONFIG.remote.user}@${CONFIG.remote.host} '${REMOTE_TMUX} new-session -d -s "${sessionName}" "${command}"'`;
        wslExec(cmd);
      } else if (IS_WIN) {
        const workDir = `/mnt/c/Users/${os.userInfo().username}`;
        wslExec(`${LOCAL_TMUX} new-session -d -s "${sessionName}" -c "${workDir}" "${command}"`);
      } else if (IS_MAC && agentConfig.binary) {
        const bin = agentConfig.binary.replace(/^~/, process.env.HOME);
        const flags = command.split(' ').slice(1).join(' ');
        execSync(`${LOCAL_TMUX} new-session -d -s "${sessionName}" "${bin}${flags ? ' ' + flags : ''}"`, {
          env: { ...process.env, TERM: 'xterm-256color' },
        });
      } else {
        execSync(`${LOCAL_TMUX} new-session -d -s "${sessionName}" "${command}"`, {
          env: { ...process.env, TERM: 'xterm-256color' },
        });
      }
    } catch (e) {
      console.error(`Failed to create ${agent} session on ${location}:`, e.message);
      return null;
    }
    return { sessionName, remote: location === 'remote' };
  });

  ipcMain.on('context-menu', (_, sessionName, displayName) => showContextMenu(mainWindow, sessionName, displayName));

  // Scroll
  ipcMain.on('pty:scroll', (_, slot, direction, lines) => {
    const entry = sessionManager.slots[slot];
    if (!entry) return;
    const session = entry.sessionName;
    const count = Math.min(lines || 1, 50);
    const tmux = entry.remote ? REMOTE_TMUX : LOCAL_TMUX;
    const scrollCmd = direction === 'up' ? 'scroll-up' : 'scroll-down';
    const fullCmd = `${tmux} copy-mode -e -t "${session}" && ${tmux} send-keys -t "${session}" -X -N ${count} ${scrollCmd}`;

    if (IS_WIN) {
      if (entry.remote) {
        wslExecAsync(`ssh -o StrictHostKeyChecking=no ${CONFIG.remote.user}@${CONFIG.remote.host} '${fullCmd}'`, () => {});
      } else {
        wslExecAsync(fullCmd, () => {});
      }
    } else if (IS_CLIENT && entry.remote) {
      execFile('bash', ['-c', `ssh -o StrictHostKeyChecking=no ${CONFIG.remote.user}@${CONFIG.remote.host} '${fullCmd}'`], { stdio: 'ignore' }, () => {});
    } else {
      execFile(tmux, ['copy-mode', '-e', '-t', session], { stdio: 'ignore' }, () => {
        execFile(tmux, ['send-keys', '-t', session, '-X', '-N', String(count), scrollCmd], { stdio: 'ignore' }, () => {});
      });
    }
  });

  // Send tmux keys
  ipcMain.on('pty:tmux-send', (_, slot, ...keys) => {
    const entry = sessionManager.slots[slot];
    if (!entry) return;
    const tmux = entry.remote ? REMOTE_TMUX : LOCAL_TMUX;
    const keysStr = keys.join(' ');

    if (IS_WIN) {
      if (entry.remote) {
        wslExecAsync(`ssh -o StrictHostKeyChecking=no ${CONFIG.remote.user}@${CONFIG.remote.host} "${tmux} send-keys -t '${entry.sessionName}' ${keysStr}"`, () => {});
      } else {
        wslExecAsync(`${tmux} send-keys -t "${entry.sessionName}" ${keysStr}`, () => {});
      }
    } else if (IS_CLIENT && entry.remote) {
      execFile('bash', ['-c', `ssh -o StrictHostKeyChecking=no ${CONFIG.remote.user}@${CONFIG.remote.host} "${tmux} send-keys -t '${entry.sessionName}' ${keysStr}"`], { stdio: 'ignore' }, () => {});
    } else {
      execFile(tmux, ['send-keys', '-t', entry.sessionName, ...keys], { stdio: 'ignore' }, () => {});
    }
  });

  // Exit copy-mode
  ipcMain.on('pty:exit-copy-mode', (_, slot) => {
    const entry = sessionManager.slots[slot];
    if (!entry) return;
    const tmux = entry.remote ? REMOTE_TMUX : LOCAL_TMUX;

    if (IS_WIN) {
      if (entry.remote) {
        wslExecAsync(`ssh -o StrictHostKeyChecking=no ${CONFIG.remote.user}@${CONFIG.remote.host} "${tmux} send-keys -t '${entry.sessionName}' -X cancel"`, () => {});
      } else {
        wslExecAsync(`${tmux} send-keys -t "${entry.sessionName}" -X cancel`, () => {});
      }
    } else if (IS_CLIENT && entry.remote) {
      execFile('bash', ['-c', `ssh -o StrictHostKeyChecking=no ${CONFIG.remote.user}@${CONFIG.remote.host} "${tmux} send-keys -t '${entry.sessionName}' -X cancel"`], { stdio: 'ignore' }, () => {});
    } else {
      execFile(tmux, ['send-keys', '-t', entry.sessionName, '-X', 'cancel'], { stdio: 'ignore' }, () => {});
    }
  });

  // Rename session
  ipcMain.handle('rename-session', async (_, sessionName, newName, remote = false) => {
    try {
      const tmux = remote ? REMOTE_TMUX : LOCAL_TMUX;
      const renameCmd = `${tmux} rename-session -t "${sessionName}" "${newName}"`;
      if (IS_WIN) {
        if (remote) { wslExec(`ssh -o StrictHostKeyChecking=no ${CONFIG.remote.user}@${CONFIG.remote.host} '${renameCmd}'`); }
        else { wslExec(renameCmd); }
      } else if (IS_CLIENT && remote) {
        execSync(`ssh -o StrictHostKeyChecking=no ${CONFIG.remote.user}@${CONFIG.remote.host} '${renameCmd}'`);
      } else {
        execSync(renameCmd, { stdio: 'ignore' });
      }
      return true;
    } catch { return false; }
  });

  // Kill tmux session
  ipcMain.handle('kill-session', async (_, sessionName, remote = false) => {
    try {
      const tmux = remote ? REMOTE_TMUX : LOCAL_TMUX;
      const killCmd = `${tmux} kill-session -t "${sessionName}"`;
      if (IS_WIN) {
        if (remote) { wslExec(`ssh -o StrictHostKeyChecking=no ${CONFIG.remote.user}@${CONFIG.remote.host} '${killCmd}'`); }
        else { wslExec(killCmd); }
      } else if (IS_CLIENT && remote) {
        execSync(`ssh -o StrictHostKeyChecking=no ${CONFIG.remote.user}@${CONFIG.remote.host} '${killCmd}'`);
      } else {
        execSync(killCmd, { stdio: 'ignore' });
      }
      return true;
    } catch { return false; }
  });

  // Meeting window
  ipcMain.on('meeting:open', () => {
    if (meetingWindow && !meetingWindow.isDestroyed()) { meetingWindow.focus(); return; }
    meetingWindow = new BrowserWindow({
      width: 600, height: 500, minWidth: 400, minHeight: 300,
      titleBarStyle: IS_MAC ? 'hiddenInset' : undefined,
      trafficLightPosition: IS_MAC ? { x: 12, y: 12 } : undefined,
      backgroundColor: '#0c1310',
      webPreferences: { contextIsolation: false, nodeIntegration: true },
    });
    meetingWindow.loadFile('renderer/meeting.html');
    meetingWindow.on('closed', () => { meetingWindow = null; });
  });

  ipcMain.on('meeting:close', () => {
    if (meetingWindow && !meetingWindow.isDestroyed()) { meetingWindow.close(); meetingWindow = null; }
  });
});

app.on('window-all-closed', () => { sessionManager.killAll(); stopSshTunnel(); app.quit(); });

app.on('before-quit', () => {
  saveSlotState(); sessionManager.killAll(); stopSshTunnel();
  if (meetingWindow && !meetingWindow.isDestroyed()) meetingWindow.close();
});
