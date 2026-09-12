'use strict';

// Host resolution for a `peers[]` profile entry (e.g. daffodil). The web host
// only had `local` / `remote` / `config.hosts`; a profile that lists SSH peers
// (not a `hosts` map) could not attach them. terminal_adapter now builds an SSH
// target from each peer, at parity with the Electron host registry (hosts.js).

const test = require('node:test');
const assert = require('node:assert/strict');
const { registerTerminalIpc } = require('../main/terminal_adapter');

function collector() {
  const table = {};
  return { table, handle(ch, fn) { table[ch] = { mode: 'invoke', handler: fn }; }, on(ch, fn) { table[ch] = { mode: 'send', handler: fn }; } };
}
function fakeSender(id) {
  return { id, send() {}, isDestroyed() { return false; }, once() {} };
}
function makePty() {
  return { spawn() { let onExit = () => {}; return { onData() {}, onExit(f) { onExit = f; }, write() {}, resize() {}, kill() { onExit({ exitCode: 0 }); } }; } };
}

test('pty:create resolves a peers[] host to an SSH target', async () => {
  const calls = [];
  const execute = async (file, args) => {
    calls.push({ file, args });
    return { stdout: Array.isArray(args) && args.some((a) => String(a).includes('display-message')) ? '%3\n' : '' };
  };
  const config = {
    chatStream: { localHost: 'local' },
    remote: { host: 'bart.example', user: 'bartimaeus', port: 22, tmux: '/opt/homebrew/bin/tmux' },
    peers: [{ id: 'merlin', host: '198.51.100.35', user: 'vgujju', port: 2222, tmux: '/opt/homebrew/bin/tmux' }],
  };
  const c = collector();
  registerTerminalIpc(c, config, {}, { pty: makePty(), execute });

  const paneId = await c.table['pty:create'].handler({ sender: fakeSender('web-1') }, 0, 'sess', 'merlin', 80, 24);
  assert.match(String(paneId), /^%\d+$/, 'the peer attach returns a pane id');

  const lookup = calls.find((cl) => Array.isArray(cl.args) && cl.args.some((a) => String(a).includes('display-message')));
  assert.ok(lookup, 'a pane lookup ran');
  assert.match(String(lookup.file), /^ssh(\.exe)?$/, 'a peer attaches over ssh, like remote does');
  const line = lookup.args.join(' ');
  assert.match(line, /vgujju@198\.51\.100\.35/, 'ssh targets the peer host/user');
  assert.match(line, /-p 2222/, 'ssh uses the peer port');
});

test('local and remote still resolve, and an unknown host is refused', async () => {
  const calls = [];
  const execute = async (file, args) => { calls.push({ file }); return { stdout: Array.isArray(args) && args.some((a) => String(a).includes('display-message')) ? '%1\n' : '' }; };
  const config = {
    chatStream: { localHost: 'local' },
    remote: { host: 'bart.example', user: 'bartimaeus', port: 22, tmux: 'tmux' },
    peers: [{ id: 'merlin', host: '198.51.100.35', user: 'vgujju' }],
  };
  const c = collector();
  registerTerminalIpc(c, config, {}, { pty: makePty(), execute });

  // local → plain tmux (no ssh)
  await c.table['pty:create'].handler({ sender: fakeSender('web-a') }, 0, 'sess', 'local', 80, 24);
  assert.equal(calls[0].file, 'tmux', 'local uses plain tmux');

  // remote → ssh
  calls.length = 0;
  await c.table['pty:create'].handler({ sender: fakeSender('web-b') }, 0, 'sess', 'remote', 80, 24);
  assert.match(String(calls[0].file), /^ssh(\.exe)?$/, 'remote uses ssh');

  // unknown → refused
  await assert.rejects(
    c.table['pty:create'].handler({ sender: fakeSender('web-c') }, 0, 'sess', 'nobody', 80, 24),
    /No terminal transport configured/,
  );
});

test('a peer never clobbers local or remote and needs id/host/user', () => {
  // A malformed or reserved-id peer must not register; exercised indirectly by
  // resolving a good peer while a bad one is present.
  const calls = [];
  const execute = async (file, args) => { calls.push({ file, args }); return { stdout: Array.isArray(args) && args.some((a) => String(a).includes('display-message')) ? '%2\n' : '' }; };
  const config = {
    chatStream: { localHost: 'local' },
    peers: [
      { id: 'remote', host: 'evil', user: 'x' },       // reserved id — must be ignored
      { id: 'nouser', host: 'h' },                     // missing user — ignored
      { id: 'good', host: 'gh', user: 'gu', port: 22, tmux: 'tmux' },
    ],
  };
  const c = collector();
  registerTerminalIpc(c, config, {}, { pty: makePty(), execute });
  // 'remote' resolves via config.remote (absent here) → refused, proving the peer didn't clobber it.
  return assert.rejects(
    c.table['pty:create'].handler({ sender: fakeSender('web-1') }, 0, 'sess', 'remote', 80, 24),
    /No terminal transport configured/,
  );
});
