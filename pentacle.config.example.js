// ── Pentacle Configuration ────────────────────────────────────
// Edit this file to customize the app name, colors, and features.
// All values have sensible defaults — override only what you need.

module.exports = {
  // ── Mode (host vs client) ─────────────────────────────────────
  // Pentacle runs in one of two modes:
  //
  // HOST — no `remote` block. Local node-pty, local tmux.
  //
  // CLIENT — `remote` block present. SSHs to the given host for attaching
  //          remote sessions. Session metadata (sidebar, trash, agent_id)
  //          comes from chat_streamd over WebSocket — see chatStream
  //          config below.
  //
  // Optional features are independent of HOST/CLIENT mode. A client can set
  // features.mic=true and route mic API calls to the stream host with
  // mic.useStreamHost=true. Usage bars are controlled by features.usage and
  // read usage snapshots from chat_streamd when the daemon is configured to
  // publish them.
  //
  // Three config shapes:
  //
  //   (A) Local HOST — leave as-is, no remote/localWsl blocks.
  //
  //   (B) Laptop CLIENT — add `remote` pointing at the host machine.
  //       Use a stable DNS name, VPN address, or LAN address.
  //       `local` uses the laptop's own tmux via node-pty.
  //
  //         remote: {
  //           host: 'host.example',
  //           user: 'agentuser',
  //           tmux: '/opt/homebrew/bin/tmux',
  //           port: 22,
  //         },
  //
  //   (C) Windows CLIENT — `remote` block + `localWsl` block. Local sessions
  //       run inside WSL through direct wsl.exe calls, so new sessions inherit
  //       the Windows token that launched Pentacle. WSL needs: a non-root user,
  //       claude + codex + tmux + wslu installed for that user.
  //
  //         remote: {
  //           host: 'host.example',
  //           user: 'agentuser',
  //           tmux: '/opt/homebrew/bin/tmux',
  //         },
  //         localWsl: {
  //           distro: 'Ubuntu',
  //           user: 'agentuser',         // non-root so agent CLIs can use the user's config
  //           tmux: 'tmux',
  //         },
  //
  //   (D) HOST + peers — additive. A HOST can also surface tmux sessions from
  //       other SSH-reachable machines in its sidebar without flipping into
  //       CLIENT mode. Useful when you want one sidebar that shows chats from
  //       this machine AND other peers you own.
  //
  //         peers: [
  //           {
  //             id: 'workstation',       // shows up as host id; used by hostNames/hostColors
  //             host: 'workstation.example',
  //             user: 'agentuser',
  //             port: 22,
  //             tmux: '/usr/bin/tmux',
  //           },
  //         ],

  // ── App Identity ──────────────────────────────────────────────
  appName: 'Pentacle',             // Window title, titlebar text, process name
  appId: 'com.pentacle.app',       // macOS bundle identifier

  // ── Paths ─────────────────────────────────────────────────────
  // Where new agent sessions start (~ is expanded automatically)
  workingDirectory: '~/agent-workspace',

  // Agent commands — what "New Session" launches
  agents: {
    claude: {
      label: 'Claude',
      // Resolved from PATH. Set `binary` only if you need a specific path.
      command: 'claude --dangerously-skip-permissions',
    },
    codex: {
      label: 'Codex',
      // Override these per machine when a content-addressed Codex path is
      // available. The location-specific command is used for each new session.
      command: 'codex',
      commandLocal: 'codex',
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

  // Pentacle is dark-only — there is no light theme.

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
  // Features that require extra infrastructure default to false for fresh installs.
  // Set to true only after confirming the required backend is running.
  features: {
    mic: false,              // Mic panel + voice record (requires mic server + TCC on macOS)
    usage: false,            // Usage bars in sidebar (requires chat_streamd at chatStream.url with PENTACLE_*_USAGE_SCRIPT configured)
    chatUi: false,           // Slot-level structured chat view + websocket chat controls (rendered via the shared pentacle-chat-core view)
    inputBar: true,          // Per-slot input bar — works with any terminal, no extra deps
    dashboards: false,       // Dashboards view (requires custom dashboard files and IPC handlers)
    sourceTags: false,       // Show source host tag on sessions
    rawTmux: false,          // Show unmanaged (non-agent) tmux sessions in the sidebar; always shown when chat_streamd is unreachable
  },

  // ── UI Review Dashboard ───────────────────────────────────────
  // The UI Review dashboard indexes static HTML review artifacts from repos.
  // Defaults:
  //   repoRoots: ['~/repos']
  //   artifactDirs: ['~/agent-workspace/ui-review']
  // For each repo root, Pentacle scans:
  //   <repo>/test/artifacts/*.html
  //   <repo>/.ui-review/*.html
  // uiReview: {
  //   repoRoots: ['~/repos'],
  //   artifactDirs: ['~/agent-workspace/ui-review'],
  //   localFallback: false, // packaged apps normally use Dashboard Hub data/cache
  // },

  // Display names for host IDs — shown as source tags when features.sourceTags is true.
  // Keys match HOSTS registry IDs (e.g. 'local', 'remote'). Add more as needed.
  // hostNames: {
  //   local: 'MyMachine',
  //   remote: 'RemoteHost',
  // },

  // Source tag colors per host ID. Valid: red, purple, yellow, green, blue, orange, royal-blue, forest-green, deep-raspberry.
  // Unknown hosts fall back to green.
  // hostColors: {
  //   local: 'red',
  //   remote: 'purple',
  // },

  // Structured chat stream daemon.
  // Real machine topology belongs in an ignored JSON file, not source code.
  // See services/chat-stream-v2/machines.example.json.
  // chatStream: {
  //   url: 'ws://127.0.0.1:7791',
  //   autoStart: true,
  //   hosts: ['local'],
  //   machinesFile: '~/.config/pentacle-stream/machines.json',
  //   recentLimit: 5000,
  // },

  // Dashboard Hub — websocket source for the Dashboards view (only used if
  // features.dashboards is true). Omit this block to leave the hub client
  // uninitialised; the Dashboards view will then show "no data yet from hub"
  // for every panel even if features.dashboards is on. The read token is a
  // plain text file provisioned by your dashboard service. Keep its path and
  // credentials in your private configuration.
  // dashboardHub: {
  //   url: 'http://hub-host.example:7781',
  //   readTokenPath: '~/.dashboard-hub/read-token',
  // },

  // Machine stats in the sidebar. By default Pentacle runs a lightweight
  // built-in macOS/Linux shell probe on each configured host. Override a host
  // when a machine needs a custom collector.
  // machineStats: {
  //   hostIds: ['local', 'remote', 'workstation'],
  //   currentHostId: 'local', // compact view starts here; "See all" expands the rest
  //   defaults: {
  //     shell: '/bin/bash',
  //     format: 'kv', // `kv` key=value lines, or `json`
  //   },
  //   hosts: {
  //     local: {},
  //     remote: {},
  //     workstation: {
  //       showGpu: true,
  //       // Return cpu_pct, mem_pct, mem_total_gb, disk_pct,
  //       // disk_total_gb, and optionally gpu_pct.
  //       // command: 'python3 ~/agent-workspace/pentacle_stats.py',
  //       // format: 'json',
  //     },
  //   },
  // },

  // Mic server URL (only used if features.mic is true)
  micServerUrl: 'http://127.0.0.1:7780',

  // Mic routing and always-on policy. Defaults are local-only and conservative.
  // See docs/ARCHITECTURE.md "Shared mic-server topology" and
  // mic-server/README.md for the HTTP/env contract.
  // mic: {
  //   // Route mic API calls to the chatStream.url host on port 7780.
  //   // Default false: use micServerUrl and spawn/probe a local mic-server
  //   // when features.mic is true. When true, this desktop skips local
  //   // mic-server auto-spawn and expects the stream host to expose :7780.
  //   useStreamHost: false,
  //
  //   // Default true: set false when launchd/systemd is the sole mic-server
  //   // owner so Pentacle never spawns a local mic-server.
  //   autoSpawn: true,
  //
  //   // Gate always-on in this renderer and in any locally spawned mic-server.
  //   // Default false: hide always-on UI and pass MIC_ALWAYS_ON_ENABLED=false
  //   // to local mic-server spawns, causing POST /mode/on to return 403.
  //   alwaysOnEnabled: false,
  //
  //   // Optional local mic-server bind host. When this desktop spawns a
  //   // mic-server, main.js passes this as MIC_BIND_HOST (default 127.0.0.1).
  //   // bindHost: '127.0.0.1',
  // },
  // For mic-server auto-start attribution, set MIC_SERVER_HOST_ID in the
  // parent environment or LaunchAgent/service (e.g. "local"). main.js passes
  // process.env through to spawned mic-server processes; it does not derive
  // MIC_SERVER_HOST_ID from chatStream.hostMap.

  // Python binary for mic server (non-macOS). Defaults to 'python' on Windows, 'python3' elsewhere.
  // macOS uses /Applications/MicServer.app when present (TCC requirement), falls back to this.
  // micServerPython: 'python',

  // Wake word shown in mic UI (only used if features.mic is true)
  wakeWord: 'Hey Pentacle',
};
