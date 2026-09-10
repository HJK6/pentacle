# Pentacle desktop topology

Use this guide to choose where the Electron desktop attaches to tmux and where it reaches the chat daemon. All examples are local or synthetic.

## Desktop modes

| Mode | Configuration | Terminal attachment |
|---|---|---|
| Host | no `remote` block | local tmux through node-pty |
| Client | a `remote` block | SSH tmux through the configured adapter |

A `localWsl` block adds WSL on Windows. A `peers` array adds optional terminal hosts. None of these settings changes the websocket source for structured chat.

## Daemon ownership

`chatStream.url` chooses the websocket independently of terminal mode.

- `autoStart: true` starts a disposable local daemon owned by the desktop.
- `autoStart: false` connects to a daemon that the developer started separately.

For public development, use `ws://127.0.0.1:7791` and a temporary store. The daemon's `--help` output is the option authority.

## Host mode

```js
chatStream: {
  url: 'ws://127.0.0.1:7791',
  autoStart: true,
  localHost: 'coordinator',
  binds: ['127.0.0.1'],
  machinesFile: '/tmp/pentacle-example/machines.json',
},
```

## Client and WSL fixtures

An adapter test may use the reserved names `example.local` and `10.0.0.0`:

```js
remote: { host: 'example.local', user: 'example', port: 22, tmux: '/usr/bin/tmux' },
chatStream: { url: 'ws://127.0.0.1:7791', autoStart: false },
```

Run SSH and WSL checks only in an isolated test environment. Do not put a private key, real host, or real user in a committed config.

## Verification

| Symptom | First check |
|---|---|
| Sidebar is empty | Check the loopback URL and `agent-orch list`. |
| Structured chat is unavailable | Enable `features.chatUi` and inspect the daemon health result. |
| Terminal attach fails | Test tmux through the selected adapter outside the UI. |
| Local daemon exits | Run the documented command directly and inspect its typed startup error. |
| WSL fixture fails | Verify the distro, user, tmux binary, and script path inside the fixture. |

No managed remote, deployment, installation, or rollback path is part of this public setup.
