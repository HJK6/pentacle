// ── Pentacle Configuration ────────────────────────────────────
// Edit this file to customize the app name, colors, and features.
// All values have sensible defaults — override only what you need.

module.exports = {
  // ── Mode (host vs client) ─────────────────────────────────────
  // Pentacle runs in one of two modes:
  //
  // HOST — no `remote` block. Local node-pty, local API server on 7777,
  //        mic server + usage refresh active (mac-mini today).
  //
  // CLIENT — `remote` block present. SSHs to the given host for attaching
  //          remote sessions, SSH-tunnels the host's API on `apiPort`
  //          (default 7778) → localhost, so the sidebar/session list work
  //          unchanged. Mic + usage poll are disabled on clients.
  //
  // Three config shapes:
  //
  //   (A) Mac-mini HOST — leave as-is, no remote/localWsl blocks.
  //
  //   (B) Macbook CLIENT — add `remote` pointing at the mac-mini. Use a
  //       Tailscale IP/MagicDNS for roaming (falls back to LAN when home).
  //       `local` uses the macbook's own tmux via node-pty.
  //
  //         remote: {
  //           host: '100.x.x.x',       // or 'mac-mini.tail-xxxx.ts.net'
  //           user: 'bartimaeus',
  //           tmux: '/opt/homebrew/bin/tmux',
  //           apiPort: 7778,
  //           port: 22,
  //         },
  //
  //   (C) Windows CLIENT — `remote` block + `localWsl` block. Local sessions
  //       run inside WSL over sshd on port 2222. Pentacle auto-detects WSL's
  //       eth0 IP (localhost forwarding is unreliable) and starts a static
  //       sshd if one isn't already listening. WSL needs: a non-root user,
  //       claude + codex + tmux + wslu installed for that user, and their
  //       Windows-side SSH public key in ~/.ssh/authorized_keys.
  //
  //         remote: {
  //           host: '192.168.4.195',
  //           user: 'bartimaeus',
  //           tmux: '/opt/homebrew/bin/tmux',
  //           apiPort: 7778,
  //         },
  //         localWsl: {
  //           distro: 'Ubuntu',
  //           sshPort: 2222,
  //           user: 'vamsh',             // non-root so --dangerously-skip-permissions works
  //           tmux: 'tmux',
  //           // host: '172.23.x.x',     // optional; auto-detected if omitted
  //         },

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
      // Pentacle checks once per day whether this CLI is outdated and, if so,
      // runs `npm install -g @openai/codex` on the target host before launch.
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
    sourceTags: false,       // Show source host tag (e.g. "Amaterasu") on sessions
  },

  // Display names for host IDs — shown as source tags when features.sourceTags is true.
  // Keys match HOSTS registry IDs (e.g. 'local', 'remote'). Add more as needed.
  // hostNames: {
  //   local: 'MyMachine',
  //   remote: 'RemoteHost',
  // },

  // Source tag colors per host ID. Valid: red, purple, yellow, green, blue, orange.
  // Unknown hosts fall back to green.
  // hostColors: {
  //   local: 'red',
  //   remote: 'purple',
  // },

  // Mic server URL (only used if features.mic is true)
  micServerUrl: 'http://127.0.0.1:7780',

  // Python binary for mic server (non-macOS). Defaults to 'python' on Windows, 'python3' elsewhere.
  // macOS uses /Applications/MicServer.app when present (TCC requirement), falls back to this.
  // micServerPython: 'python',

  // Wake word shown in mic UI (only used if features.mic is true)
  wakeWord: 'Hey Bart',
};
