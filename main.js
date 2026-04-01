const { app, BrowserWindow, ipcMain, Menu, nativeImage } = require('electron');
const path = require('path');
const pty = require('node-pty');
const { spawn } = require('child_process');

// Set app identity so Ghost OS and macOS recognize us properly
app.setName('Pentacle');

const TMUX = '/opt/homebrew/bin/tmux';
const API_URL = 'http://localhost:7777';

let meetingWindow = null;

// ── PTY Manager ────────────────────────────────────────────────

class PtyManager {
  constructor() {
    this.ptys = [null, null, null, null];
    this.webContents = null;
  }

  setWebContents(wc) {
    this.webContents = wc;
  }

  create(slot, sessionName) {
    this.kill(slot);
    const p = pty.spawn(TMUX, ['attach-session', '-t', sessionName], {
      name: 'xterm-256color',
      cols: 80,
      rows: 24,
      cwd: process.env.HOME,
      env: { ...process.env, TERM: 'xterm-256color' },
    });

    // Keep tmux mouse OFF so xterm.js handles click-drag selection natively.
    // Scroll is handled separately via pty:scroll IPC → tmux copy-mode commands.
    const { execSync } = require('child_process');
    try {
      execSync(`${TMUX} set-option -t "${sessionName}" mouse off`, { stdio: 'ignore' });
    } catch {}

    const entry = { pty: p, sessionName };
    this.ptys[slot] = entry;

    p.onData((data) => {
      // Guard: only forward if this PTY is still the active one for this slot
      if (this.ptys[slot] === entry && this.webContents && !this.webContents.isDestroyed()) {
        this.webContents.send('pty:data', slot, data);
      }
    });

    p.onExit(({ exitCode }) => {
      if (this.ptys[slot] === entry) {
        this.ptys[slot] = null;
      }
      if (this.webContents && !this.webContents.isDestroyed()) {
        this.webContents.send('pty:exit', slot, exitCode);
      }
    });
    return true;
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
      // Re-enable tmux mouse for other clients
      const { execSync } = require('child_process');
      try {
        execSync(`${TMUX} set-option -t "${this.ptys[slot].sessionName}" mouse on`, { stdio: 'ignore' });
      } catch {}
      try {
        this.ptys[slot].pty.kill();
      } catch (e) {
        // already dead
      }
      this.ptys[slot] = null;
    }
  }

  killAll() {
    for (let i = 0; i < 4; i++) this.kill(i);
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
    const serverPath = path.join(process.env.HOME, '.tmux/cmdcenter/server.py');
    const pythonPath = path.join(process.env.HOME, '.venvs/global/bin/python');
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
const SLOT_STATE_FILE = path.join(__dirname, '.slot-state.json');

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

app.whenReady().then(async () => {
  // Application menu — needed for Cmd+C/V/X/A to work in Electron
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { role: 'appMenu' },
    { role: 'editMenu' },
  ]));

  await ensureApiServer();

  mainWindow = new BrowserWindow({
    width: 1600,
    height: 1000,
    minWidth: 900,
    minHeight: 600,
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 15, y: 15 },
    backgroundColor: '#0c1310',
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
  });

  // IPC handlers
  ipcMain.handle('pty:create', (_, slot, sessionName) => {
    const result = ptyManager.create(slot, sessionName);
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

  ipcMain.handle('pty:new-claude', (_, slot) => {
    // Create a new tmux session running claude code
    const sessionName = `claude-${process.pid}-${Date.now()}`;
    const { execSync } = require('child_process');
    try {
      const claudePath = path.join(process.env.HOME, '.local/bin/claude');
      execSync(`${TMUX} new-session -d -s "${sessionName}" "${claudePath} --dangerously-skip-permissions"`, {
        env: { ...process.env, TERM: 'xterm-256color' },
      });
    } catch (e) {
      console.error('Failed to create tmux session:', e.message);
      return null;
    }
    return sessionName;
  });

  ipcMain.on('context-menu', (_, sessionName, displayName) => {
    showContextMenu(mainWindow, sessionName, displayName);
  });

  // Scroll tmux scrollback via copy-mode (batched — sends -N count to avoid per-line process spawns)
  ipcMain.on('pty:scroll', (_, slot, direction, lines) => {
    const entry = ptyManager.ptys[slot];
    if (!entry) return;
    const { execFile } = require('child_process');
    const session = entry.sessionName;
    const count = Math.min(lines || 1, 50); // cap at 50 lines per batch
    // Enter copy-mode with -e (auto-exit at bottom), then send scroll command with repeat count
    execFile(TMUX, ['copy-mode', '-e', '-t', session], { stdio: 'ignore' }, () => {
      const cmd = direction === 'up' ? 'scroll-up' : 'scroll-down';
      execFile(TMUX, ['send-keys', '-t', session, '-X', '-N', String(count), cmd], { stdio: 'ignore' }, () => {});
    });
  });

  // Exit copy-mode so typing goes to the shell
  // Send tmux key notation directly to a pane (bypasses terminal input parser)
  ipcMain.on('pty:tmux-send', (_, slot, ...keys) => {
    const entry = ptyManager.ptys[slot];
    if (!entry) return;
    const { execFile } = require('child_process');
    execFile(TMUX, ['send-keys', '-t', entry.sessionName, ...keys], { stdio: 'ignore' }, () => {});
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
      backgroundColor: '#0c1310',
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
    const { execFile } = require('child_process');
    execFile(TMUX, ['send-keys', '-t', entry.sessionName, '-X', 'cancel'], { stdio: 'ignore' }, () => {});
  });
});

app.on('window-all-closed', () => {
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
