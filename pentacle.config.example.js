// Copy this file outside the repository and select it with PENTACLE_CONFIG.
// Start the daemon and issue desktop.token using README.md's local setup.
// The daemon controls provider executables, working directories and spawning.
// Optional private backends remain disabled until their adapters are configured.
// For a remote terminal host, add hosts: { workstation: { host: 'host.example',
// user: 'operator', port: 22, tmux: '/usr/bin/tmux' } }, include workstation in
// chatStream.hosts, and use the daemon's matching machine identity.
// Full reference: docs/desktop_config.md. Missing maps use the defaults below.
// hostNames: { workstation: 'Workstation' }, // default: capitalise host identity
// hostColors: { workstation: 'royal-blue' }, // default: indexed palette, wrapping
// localHostId: 'local', // default: chatStream.hostMap.local, then chatStream.localHost
// micServerUrl: 'http://127.0.0.1:7780', // default: this local endpoint
// mic: { useStreamHost: false, alwaysOnEnabled: false, autoSpawn: true },
//   // mic.useStreamHost derives http://<chatStream URL hostname>:7780.
//   // autoSpawn is compatibility-only; public main probes, never starts a server.
// wakeWord: 'Computer', // no built-in default; optional mic display text
// machineStats: {}, // accepted compatibility key, ignored; daemon hosts.stats frames (hosts payload) supply cards
// artifactDirs: ['/path/to/artifacts'], // default: <working directory>/test/artifacts
// repoRoots: [], // default: no additional artifact repository roots
// hosts: { workstation: { host: 'host.example', user: 'operator', port: 22, tmux: 'tmux' } },
//   // default hosts: {}; port 22, user local OS username, tmux top-level tmux
// remote: { host: 'host.example', user: 'operator', port: 22, tmux: 'tmux' },
//   // default absent; legacy single remote transport and client-mode marker
// dashboardHub: { url: 'http://127.0.0.1:7777' }, // default absent; optional external adapter
// Legacy hosts.js helper inputs (public main uses hosts/tmux instead):
// localTmux: 'tmux', // helper default /opt/homebrew/bin/tmux on macOS, tmux elsewhere
// localSsh: { host: '127.0.0.1', port: 2222, user: 'operator', tmux: 'tmux' }, // default absent
// localWsl: { distro: 'Ubuntu', user: null, tmux: 'tmux' }, // helper defaults shown
// peers: [{ id: 'workstation', host: 'host.example', user: 'operator', port: 22, tmux: 'tmux' }], // default []
// Runtime-only get-config fields: hostIds, isClient, hostname, platform,
// configError and configWarnings are derived by main; do not set them here.
// agents.<provider>: label defaults to provider label; startupMessage is absent;
// startupDelayMs defaults to 1500. Optional startupMessage sends text after spawn.
// appId and dark are retained compatibility inputs; public main does not apply them.
module.exports = {
  "appName": "Pentacle",
  "appId": "com.pentacle.app",
  "agents": {
    "claude": {
      "label": "Claude"
    },
    "codex": {
      "label": "Codex"
    }
  },
  "dark": {
    "bg": "#0c1310",
    "bg2": "#121e18",
    "bg3": "#1a2b22",
    "fg": "#b5ccba",
    "fgDim": "#4d6e56",
    "blue": "#3fb950",
    "green": "#56d364",
    "red": "#f47067",
    "yellow": "#d4a72c",
    "purple": "#a78bfa",
    "cyan": "#2dd4bf",
    "border": "#1e3928"
  },
  "terminal": {
    "background": "#0c1310",
    "foreground": "#b5ccba",
    "cursor": "#3fb950",
    "cursorAccent": "#0c1310",
    "selectionBackground": "#1e4d2b",
    "black": "#0c1310",
    "red": "#f47067",
    "green": "#3fb950",
    "yellow": "#d4a72c",
    "blue": "#58a6ff",
    "magenta": "#a78bfa",
    "cyan": "#2dd4bf",
    "white": "#b5ccba",
    "brightBlack": "#4d6e56",
    "brightRed": "#f47067",
    "brightGreen": "#56d364",
    "brightYellow": "#e0af68",
    "brightBlue": "#79c0ff",
    "brightMagenta": "#b8a0fa",
    "brightCyan": "#56d4c4",
    "brightWhite": "#d6e8da"
  },
  "features": {
    "chatUi": false, // default false; experimental; terminals remain the default
    "inputBar": true, // compatibility-only, currently unused
    "usage": false, // example default false; enables daemon usage/limits subscriptions
    "dashboards": false, // default false; optional dashboard adapters
    "mic": false, // default false; requires an independently installed, running mic endpoint
    "sourceTags": false, // default false; session host tags
    "showTurnDuration": false, // default false; timing annotations in Chat
    "rawTmux": false // compatibility-only, currently unused
  },
  "chatStream": {
    // hostMap: { local: 'laptop', remote: 'workstation' }, // default {}; exact routing aliases
    // recentLimit: 500, // main default/cap 500; renderer omitted default 5000
    // heartbeatMs: 30000, // default 30000; clamped to 5000..120000 ms
    // openedByLocal: false, // default false; true filters by hostMap.local
    // snapshot: true, // default true; false omits the initial inventory snapshot
    // token: '', // legacy inline token; v2 credentials must use tokenPath
    // tokenPath: '~/.config/pentacle/desktop.token', // example path; absent falls back to ~/.config/pentacle-stream/token

    "url": "ws://127.0.0.1:7791",
    "hosts": [
      "local"
    ],
    "localHost": "local",
    "tokenPath": "~/.config/pentacle/desktop.token"
  },
  "tmux": "tmux"
};
