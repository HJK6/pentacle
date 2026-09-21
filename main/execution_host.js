'use strict';

// Shared by terminal attachment and provider sign-in. Configuration, never RPC
// input, supplies executable transports and SSH destinations.
const quote = value => "'" + String(value).replace(/'/g, "'\\''") + "'";

function executionHost(config, id) {
  const local = config.chatStream?.localHost || 'local';
  if (id === 'local' || id === local) {
    const wsl = config.localWsl;
    return { id: local, key: `local:${local}`, kind: wsl?.distro ? 'wsl' : 'local',
      wsl, tmux: wsl?.distro ? wsl.tmux || 'tmux' : config.tmux || 'tmux' };
  }
  const peers = {};
  for (const p of Array.isArray(config.peers) ? config.peers : []) {
    if (!p?.id || !p.host || !p.user || p.id === 'local' || p.id === 'remote' || peers[p.id]) continue;
    peers[p.id] = p;
  }
  const remote = Object.hasOwn(config.hosts || {}, id) ? config.hosts[id]
    : id === 'remote' ? config.remote : peers[id];
  if (!remote?.host) throw new Error(`No terminal transport configured for ${id}`);
  return { id, kind: 'ssh', remote, tmux: remote.tmux || 'tmux',
    key: `ssh:${remote.user || ''}@${remote.host}:${remote.port || 22}` };
}

function executionCommand(host, argv, platform = process.platform) {
  if (host.kind === 'local') return { file: argv[0], args: argv.slice(1) };
  const line = argv.map(quote).join(' ');
  if (host.kind === 'wsl') {
    const args = ['-d', String(host.wsl.distro)];
    if (host.wsl.user) args.push('-u', String(host.wsl.user));
    return { file: 'wsl.exe', args: [...args, '--', '/bin/bash', '-lc', line] };
  }
  const remote = host.remote;
  return { file: platform === 'win32' ? 'ssh.exe' : 'ssh', args: ['-tt', '-p', String(remote.port || 22), '--',
    `${remote.user ? remote.user + '@' : ''}${remote.host}`, `LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 ${line}`] };
}

function executionHosts(config) {
  const local = config.chatStream?.localHost || 'local';
  const ids = new Set([local, ...(config.chatStream?.hosts || []), ...Object.keys(config.hosts || {}),
    ...(config.peers || []).map(p => p?.id), ...(config.remote ? ['remote'] : [])]);
  ids.delete('local');
  ids.add(local);
  return [...ids].filter(id => typeof id === 'string' && id).map(id => {
    try { return { id, label: id, available: true, kind: executionHost(config, id).kind }; }
    catch { return { id, label: id, available: false }; }
  });
}

module.exports = { quote, executionHost, executionCommand, executionHosts };
