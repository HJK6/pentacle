# Pentacle desktop topology

The desktop's WebSocket connection and terminal transport are configured
separately. Start with [developer onboarding](developer_onboarding.md) for a
local daemon with scratch stores, and [desktop configuration](desktop_config.md)
for the complete supported configuration contract.

## Local desktop

Save a private JavaScript module and select it with `PENTACLE_CONFIG`:

```js
module.exports = {
  appName: 'Pentacle',
  chatStream: {
    url: 'ws://127.0.0.1:7791',
    localHost: 'local',
    hosts: ['local'],
  },
};
```

The desktop connects to the daemon started separately. Set daemon bind addresses
with `--bind`, identity with `--local-host`, and machine configuration with
`PENTACLE_MACHINES_FILE` or `PENTACLE_MACHINES_JSON`. Match the desktop local
identity to the daemon identity, or provide an explicit `chatStream.hostMap`.
The daemon owns provider executables and session working directories.

## Remote terminal transport

A synthetic remote example adds the following keys to that module:

```js
hosts: { workstation: { host: 'example.local', user: 'example', port: 22, tmux: '/usr/bin/tmux' } },
chatStream: {
  url: 'ws://127.0.0.1:7791',
  localHost: 'local',
  hosts: ['local', 'workstation'],
},
```

Configure the matching daemon machine separately. Public main attaches local
terminal IDs through local tmux; other IDs need an entry in `hosts` or the
legacy `remote` transport for the literal `remote` ID. Display labels do not
configure transport. The retained `hosts.js` helper's `localWsl` and `peers`
inputs do not configure public main's terminal path.

Use remote examples only with an explicitly configured environment. Keep
credentials and private topology outside the repository.

## Verification

| Symptom | First check |
|---|---|
| Sidebar is empty | Check the WebSocket URL and `agent-orch list`. |
| Structured chat is unavailable | Enable `features.chatUi` and inspect the daemon health result. |
| Terminal attach fails | Check the selected ID and test its tmux transport outside the UI. |
| Local daemon exits | Run the daemon command directly and inspect its startup error. |
