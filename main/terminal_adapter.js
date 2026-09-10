'use strict';
// Terminal attachment only: the daemon owns session creation, inventory and titles.
const os = require('node:os');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const run = promisify(execFile);

function registerTerminalIpc(ipcMain, config, client, { pty = null, execute = run, platform = process.platform } = {}) {
  const slots = new Map();
  const quote = (value) => "'" + String(value).replace(/'/g, "'\\''") + "'";
  function target(host, args) {
    const local = config.chatStream?.localHost || 'local';
    const tmux = config.tmux || 'tmux';
    if (host === 'local' || host === local) return { file: tmux, args };
    const remote = config.hosts?.[host] || (host === 'remote' ? config.remote : null);
    if (!remote?.host) throw new Error(`No terminal transport configured for ${host}`);
    return { file: platform === 'win32' ? 'ssh.exe' : 'ssh', args: ['-tt', '-p', String(remote.port || 22), '--',
      `${remote.user ? remote.user + '@' : ''}${remote.host}`, 'LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 ' + [remote.tmux || 'tmux', ...args].map(quote).join(' ')] };
  }
  function key(event, slot) { return `${event.sender.id}:${slot}`; }
  function close(event, slot) { const id = key(event, slot); const record = slots.get(id); slots.delete(id); record?.process?.kill(); }
  ipcMain.handle('pty:create', async (event, slot, sessionName, host = 'local', cols = 80, rows = 24) => {
    if (!sessionName || !Number.isInteger(slot)) throw new Error('A session name and numeric slot are required');
    close(event, slot);
    const record = { process: null, sessionName, host, paneId: null };
    slots.set(key(event, slot), record);
    const lookup = target(host, ['display-message', '-p', '-t', `=${sessionName}:`, '#{pane_id}']);
    const { stdout } = await execute(lookup.file, lookup.args, { timeout: 5000 });
    if (slots.get(key(event, slot)) !== record) throw new Error('Terminal attachment was superseded');
    const paneId = stdout.trim();
    if (!/^%\d+$/.test(paneId)) throw new Error('The requested tmux pane is unavailable');
    const command = target(host, ['-u', 'attach-session', '-t', `=${sessionName}`]);
    const native = pty || require('node-pty');
    const proc = native.spawn(command.file, command.args, { name: 'xterm-256color', cols: Math.max(1, cols), rows: Math.max(1, rows),
      cwd: os.homedir(), env: { ...process.env, LANG: 'en_US.UTF-8', LC_ALL: 'en_US.UTF-8', TERM: 'xterm-256color' } });
    record.process = proc;
    record.paneId = paneId;
    proc.onData((data) => { if (slots.get(key(event, slot)) === record && !event.sender.isDestroyed()) event.sender.send('pty:data', slot, data); });
    proc.onExit(({ exitCode }) => { if (slots.get(key(event, slot)) === record) { slots.delete(key(event, slot)); if (!event.sender.isDestroyed()) event.sender.send('pty:exit', slot, exitCode); } });
    event.sender.once('destroyed', () => { if (slots.get(key(event, slot)) === record) close(event, slot); });
    return paneId;
  });
  ipcMain.handle('pty:kill', (event, slot) => { close(event, slot); return { ok: true }; });
  ipcMain.handle('pty:check-session', async (_event, sessionName, host = 'local') => {
    const command = target(host, ['has-session', '-t', `=${sessionName}`]);
    try { await execute(command.file, command.args, { timeout: 5000 }); return true; } catch { return false; }
  });
  ipcMain.handle('pty:new-session', async (_event, provider, host = 'local') => {
    const response = await client.spawnSession({ provider, host });
    const session = response.session || response;
    const sessionName = session?.session_name || response.session_name;
    if (!sessionName) throw new Error('Daemon has not finished creating the session; open it from the sidebar when ready');
    return { sessionName, hostId: session?.host || host };
  });
  ipcMain.on('pty:write', (event, slot, data) => slots.get(key(event, slot))?.process?.write(String(data)));
  ipcMain.handle('pty:paste', (event, slot, data) => {
    const id = key(event, slot);
    const record = slots.get(id);
    if (!record?.paneId || !record.process || typeof data !== 'string' || !data) return false;
    const current = () => slots.get(id) === record && !event.sender.isDestroyed();
    const operation = (record.pasteTail || Promise.resolve()).then(async () => {
      if (!current()) return false;
      const command = target(record.host, ['copy-mode', '-q', '-t', record.paneId]);
      await execute(command.file, command.args, { timeout: 5000 });
      if (!current()) return false;
      record.process.write(data);
      return true;
    });
    record.pasteTail = operation.catch(() => {});
    return operation;
  });
  ipcMain.on('pty:resize', (event, slot, cols, rows) => { if (Number.isInteger(cols) && cols > 0 && Number.isInteger(rows) && rows > 0) slots.get(key(event, slot))?.process?.resize(cols, rows); });
  function tmuxAction(event, slot, ...commands) {
    const record = slots.get(key(event, slot));
    if (!record?.paneId) return;
    const command = target(record.host, commands.flatMap((args, index) => [
      ...(index ? [';'] : []), args[0], '-t', record.paneId, ...args.slice(1),
    ]));
    void execute(command.file, command.args, { timeout: 5000 }).catch((error) => console.warn('Terminal command failed:', error.message));
  }
  ipcMain.on('pty:tmux-send', (event, slot, ...keys) => tmuxAction(event, slot, ['send-keys', ...keys.map(String)]));
  ipcMain.on('pty:exit-copy-mode', (event, slot) => tmuxAction(event, slot, ['send-keys', '-X', 'cancel']));
  // Copy commands require copy-mode; keep entry and scrolling in one tmux call
  // so both operations target this window's current pane, including over SSH.
  ipcMain.on('pty:scroll', (event, slot, direction, lines = 1) => tmuxAction(event, slot,
    ['copy-mode', '-e'],
    ['send-keys', '-X', '-N', String(Math.max(1, Math.min(100, Number(lines) || 1))), direction === 'up' ? 'scroll-up' : 'scroll-down']));
  return () => { for (const record of slots.values()) record.process?.kill(); slots.clear(); };
}
module.exports = { registerTerminalIpc };
