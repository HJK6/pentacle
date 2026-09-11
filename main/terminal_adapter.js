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
    if (host === 'local' || host === local) {
      // On Windows the local tmux lives inside a WSL distro. Run every tmux verb
      // — the pane lookup (execFile) and the interactive attach (node-pty) — as
      // `wsl.exe -d <distro> [-u <user>] -- /bin/bash -lc '<tmux ...>'`. Wrapping
      // the whole command in a single `bash -lc` string keeps tmux format args
      // like #{pane_id} out of wsl.exe's argv (where a leading # is dropped) and
      // gives the same shape to lookup and attach.
      const wsl = config.localWsl;
      if (wsl && wsl.distro) {
        const line = [wsl.tmux || 'tmux', ...args].map(quote).join(' ');
        const wslArgs = ['-d', String(wsl.distro)];
        if (wsl.user) wslArgs.push('-u', String(wsl.user));
        wslArgs.push('--', '/bin/bash', '-lc', line);
        return { file: 'wsl.exe', args: wslArgs };
      }
      return { file: tmux, args };
    }
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
    const record = { process: null, sessionName, host, paneId: null, copyMode: true };
    slots.set(key(event, slot), record);
    try {
      const lookup = target(host, ['display-message', '-p', '-t', `=${sessionName}:`, '#{pane_id}']);
      const { stdout } = await execute(lookup.file, lookup.args, { timeout: 5000 });
      if (slots.get(key(event, slot)) !== record || event.sender.isDestroyed()) throw new Error('Terminal attachment was superseded');
      const paneId = stdout.trim();
      if (!/^%\d+$/.test(paneId)) throw new Error('The requested tmux pane is unavailable');
      // xterm owns pointer selection. Configure this session and window before
      // attaching, without changing defaults for other tmux sessions.
      const setup = target(host, [
        'set-option', '-t', `=${sessionName}:`, 'mouse', 'off', ';',
        'set-option', '-w', '-t', `=${sessionName}:`, 'window-size', 'latest',
      ]);
      await execute(setup.file, setup.args, { timeout: 5000 });
      if (slots.get(key(event, slot)) !== record || event.sender.isDestroyed()) throw new Error('Terminal attachment was superseded');
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
    } catch (error) {
      if (slots.get(key(event, slot)) === record) close(event, slot);
      throw error;
    }
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
  // Keep scroll, mode exit and input in order on this attachment. Live typing
  // stays on the PTY: only history/initial attachment requires a tmux round trip.
  function queue(event, slot, action, scrolling = false) {
    const id = key(event, slot);
    const record = slots.get(id);
    if (!record?.paneId || !record.process) return Promise.resolve(false);
    if (!scrolling) record.queuedScroll = null;
    const current = () => slots.get(id) === record && !event.sender.isDestroyed();
    const operation = (record.actionTail || Promise.resolve()).then(() => current() ? action(record, current) : false);
    record.actionTail = operation.catch(() => {});
    return operation;
  }
  function tmuxCommand(record, ...commands) {
    const command = target(record.host, commands.flatMap((args, index) => [
      ...(index ? [';'] : []), args[0], '-t', record.paneId, ...args.slice(1),
    ]));
    return execute(command.file, command.args, { timeout: 5000 });
  }
  function input(event, slot, action, forceExit = false) {
    return queue(event, slot, async (record, current) => {
      if (record.copyMode || forceExit) {
        record.copyMode = true;
        await tmuxCommand(record, ['copy-mode', '-q']);
        if (!current()) return false;
        record.copyMode = false;
      }
      await action(record);
      return true;
    });
  }
  const reportCommandError = (error) => console.warn('Terminal command failed:', error.message);
  ipcMain.on('pty:write', (event, slot, data) => {
    void input(event, slot, record => record.process.write(String(data))).catch(reportCommandError);
  });
  ipcMain.handle('pty:paste', (event, slot, data) => {
    if (typeof data !== 'string' || !data) return false;
    return input(event, slot, record => record.process.write(data), true);
  });
  ipcMain.on('pty:resize', (event, slot, cols, rows) => { if (Number.isInteger(cols) && cols > 0 && Number.isInteger(rows) && rows > 0) slots.get(key(event, slot))?.process?.resize(cols, rows); });
  ipcMain.on('pty:tmux-send', (event, slot, ...keys) => {
    void input(event, slot, record => tmuxCommand(record, ['send-keys', ...keys.map(String)])).catch(reportCommandError);
  });
  ipcMain.on('pty:exit-copy-mode', (event, slot) => {
    void input(event, slot, () => {}).catch(reportCommandError);
  });
  ipcMain.on('pty:scroll', (event, slot, direction, lines = 1) => {
    const record = slots.get(key(event, slot));
    if (!record?.paneId || !record.process) return;
    const command = direction === 'up' ? 'scroll-up' : 'scroll-down';
    const count = Math.max(1, Math.min(100, Number(lines) || 1));
    // Coalesce pending wheel ticks without moving a scroll across an input
    // barrier. Otherwise a trackpad can queue SSH calls faster than they finish.
    if (record.queuedScroll?.command === command) {
      record.queuedScroll.count += count;
      return;
    }
    const batch = { command, count };
    record.queuedScroll = batch;
    void queue(event, slot, record => {
      if (record.queuedScroll === batch) record.queuedScroll = null;
      record.copyMode = true;
      return tmuxCommand(record, ['copy-mode', '-e'],
        ['send-keys', '-X', '-N', String(batch.count), batch.command]);
    }, true).catch(reportCommandError);
  });
  return () => { for (const record of slots.values()) record.process?.kill(); slots.clear(); };
}
module.exports = { registerTerminalIpc };
