// Copy this file outside the repository and select it with PENTACLE_CONFIG.
// Start the daemon and issue desktop.token using README.md's local setup.
// The daemon controls provider executables, working directories and spawning.
// Optional private backends remain disabled until their adapters are configured.
// For a remote terminal host, add hosts: { workstation: { host: 'host.example',
// user: 'operator', port: 22, tmux: '/usr/bin/tmux' } }, include workstation in
// chatStream.hosts, and use the daemon's matching machine identity.
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
    "chatUi": false,
    "inputBar": true,
    "usage": false,
    "dashboards": false,
    "mic": false,
    "sourceTags": false,
    "rawTmux": false
  },
  "chatStream": {
    "url": "ws://127.0.0.1:7791",
    "hosts": [
      "local"
    ],
    "localHost": "local",
    "tokenPath": "~/.config/pentacle/desktop.token"
  },
  "tmux": "tmux"
};
