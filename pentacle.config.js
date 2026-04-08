// ── Pentacle Configuration ────────────────────────────────────
// Edit this file to customize the app name, colors, and features.
// All values have sensible defaults — override only what you need.

module.exports = {
  // ── App Identity ──────────────────────────────────────────────
  appName: 'Pentacle',             // Window title, titlebar text, process name
  appId: 'com.pentacle.app',       // macOS bundle identifier

  // ── Paths ─────────────────────────────────────────────────────
  // Where new agent sessions start (~ is expanded automatically)
  workingDirectory: '~/agent-workspace',

  // API server that manages tmux sessions
  apiServer: {
    url: 'http://localhost:7777',
    // Path to the server script (relative to $HOME)
    script: '.tmux/cmdcenter/server.py',
    python: '.venvs/global/bin/python',
  },

  // Agent commands — what "New Session" launches
  agents: {
    claude: {
      label: 'Claude',
      command: 'claude --dangerously-skip-permissions',
      // Explicit binary path (optional, overrides command)
      binary: '~/.local/bin/claude',
    },
    codex: {
      label: 'Codex',
      command: 'codex',
    },
  },

  // ── Theme: Dark ───────────────────────────────────────────────
  dark: {
    bg:     '#0c1310',
    bg2:    '#121e18',
    bg3:    '#1a2b22',
    fg:     '#b5ccba',
    fgDim:  '#4d6e56',
    blue:   '#3fb950',
    green:  '#56d364',
    red:    '#f47067',
    yellow: '#d4a72c',
    purple: '#a78bfa',
    cyan:   '#2dd4bf',
    border: '#1e3928',
  },

  // ── Theme: Light ──────────────────────────────────────────────
  light: {
    bg:     '#f5f7f6',
    bg2:    '#eaefec',
    bg3:    '#dde5e0',
    fg:     '#2d3b32',
    fgDim:  '#7a9182',
    blue:   '#1a7f37',
    green:  '#1a7f37',
    red:    '#cf222e',
    yellow: '#9a6700',
    purple: '#6639ba',
    cyan:   '#0e7490',
    border: '#c4d4ca',
  },

  // ── Terminal Theme (xterm.js) ─────────────────────────────────
  // ANSI color palette for the terminal emulator
  terminal: {
    background:      '#0c1310',
    foreground:      '#b5ccba',
    cursor:          '#3fb950',
    cursorAccent:    '#0c1310',
    selectionBackground: '#1e4d2b',
    black:           '#0c1310',
    red:             '#f47067',
    green:           '#3fb950',
    yellow:          '#d4a72c',
    blue:            '#58a6ff',
    magenta:         '#a78bfa',
    cyan:            '#2dd4bf',
    white:           '#b5ccba',
    brightBlack:     '#4d6e56',
    brightRed:       '#f47067',
    brightGreen:     '#56d364',
    brightYellow:    '#e0af68',
    brightBlue:      '#79c0ff',
    brightMagenta:   '#b8a0fa',
    brightCyan:      '#56d4c4',
    brightWhite:     '#d6e8da',
  },

  // ── Optional Features ─────────────────────────────────────────
  // Set to false to disable features that require extra infrastructure
  features: {
    mic: true,               // Mic panel, voice record buttons, mic server auto-start
    usage: true,             // Usage bar in sidebar + auto-refresh on startup
    botsTab: true,           // Bots tab in sidebar (shows separate Sessions/Bots tabs)
    inputBar: true,          // Per-slot input bar for composing while scrolled up
    dashboards: true,        // Dashboards view in sidebar (Chats/Dashboards switcher)
  },

  // Mic server URL (only used if features.micServer is true)
  micServerUrl: 'http://127.0.0.1:7780',

  // Wake word shown in mic UI (only used if features.micServer is true)
  wakeWord: 'Hey Bart',
};
