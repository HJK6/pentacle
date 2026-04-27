// Merlin (MacBook Pro) — Pentacle config
// Shape (B): local HOST tmux + `remote` block to Bartimaeus (Mac mini) over
// Tailscale. Swap IP to Tailscale MagicDNS if/when enabled.

module.exports = {
  appName: 'Pentacle',
  appId: 'com.pentacle.merlin',

  remote: {
    host: '100.80.28.24',
    user: 'bartimaeus',
    tmux: '/opt/homebrew/bin/tmux',
    apiPort: 7778,
    port: 22,
  },

  // Additive peers — machines whose tmux sessions show up in the sidebar
  // alongside local + remote. Does NOT flip this machine into CLIENT mode.
  peers: [
    {
      id: 'amaterasu',
      host: '100.104.128.92',   // Tailscale IP (WSL sshd on 22)
      user: 'vamsh',
      port: 22,
      tmux: '/usr/bin/tmux',
    },
  ],

  workingDirectory: '~/agent-workspace',

  apiServer: {
    url: 'http://localhost:7777',
    script: '.tmux/cmdcenter/server.py',
    python: '.venvs/global/bin/python',
  },

  agents: {
    codex: {
      label: 'Codex',
      command: '/opt/homebrew/bin/codex --dangerously-bypass-approvals-and-sandbox',
    },
  },

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

  features: {
    mic: true,
    usage: true,
    botsTab: true,
    inputBar: true,
    dashboards: true,
    sourceTags: true,
  },

  uiReview: {
    repoRoots: ['~/repos'],
    artifactDirs: ['~/agent-workspace/ui-review'],
    localFallback: true,
  },

  chatStream: {
    url: 'ws://100.80.28.24:7791',
    autoStart: false,
    recentLimit: 5000,
  },

  // Source tag display — shared convention across the fleet:
  // Bartimaeus=forest green, Amaterasu=red, Merlin=royal blue. On each machine its OWN
  // sessions live under `local` but render with the machine's real name.
  hostNames: {
    local: 'Merlin',
    remote: 'Bartimaeus',
    amaterasu: 'Amaterasu',
  },

  hostColors: {
    local: 'royal-blue',
    remote: 'forest-green',
    amaterasu: 'red',
  },

  machineStats: {
    hostIds: ['local', 'remote', 'amaterasu'],
    // Per-host overrides can provide `{ shell, command, format }`.
    // `format: "kv"` expects key=value lines; `format: "json"` expects JSON.
    hosts: {
      local: {},
      remote: {},
      amaterasu: {},
    },
  },

  micServerUrl: 'http://127.0.0.1:7780',
  micServerPython: '/Users/vgujju/.venvs/global/bin/python3',
  wakeWord: 'Hey Bart',
};
