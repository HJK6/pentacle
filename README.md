# Pentacle

A desktop terminal dashboard for managing multiple AI coding agent sessions. Built with Electron + xterm.js + tmux.

Pentacle gives you a 4-slot grid of terminal panes, each connected to a tmux session. A sidebar lists all active sessions with live activity detection (working/waiting/idle), and you can click to attach any session to any slot. It's designed for running multiple Claude Code or Codex sessions side by side.

![Pentacle Screenshot](assets/pentacle_icon.png)

## Features

- **4-slot terminal grid** — attach any tmux session to any slot, maximize/minimize individual slots
- **Session management** — create, rename, trash, and restore tmux sessions from the sidebar
- **Activity detection** — sessions are classified as working/waiting/idle with visual indicators
- **Auto-reconnect** — if a PTY dies but the tmux session is alive, it reconnects automatically
- **Daily Codex auto-update** — local and remote Codex hosts are checked at most once per day; Pentacle auto-installs a newer `@openai/codex` before launching a Codex session
- **Input bar** — per-slot text input for composing messages while scrolled up reading output
- **Theme toggle** — dark and light themes, fully configurable via config file
- **Image paste** — paste images from clipboard into agent sessions
- **Scroll** — mouse wheel scrolls tmux scrollback via copy-mode

## Prerequisites

- **macOS / Linux / Windows + WSL** — supported for `npm start` (dev mode)
- **Node.js** 18+
- **tmux** (`brew install tmux` / `sudo apt install tmux`)
- **Python 3.9+** (for the API server — `brew install python` / `sudo apt install python3`)

Ships with a reference API server at `server/server.py` (stdlib only, no pip deps). Pentacle starts it automatically. See `server/README.md` for standalone testing.

For the full setup playbook — HOST vs CLIENT mode, WSL setup, troubleshooting — see **[CLAUDE.md](CLAUDE.md)**.

## Setup

```bash
git clone https://github.com/HJK6/pentacle.git
cd pentacle
npm install
npm start     # auto-copies pentacle.config.example.js → pentacle.config.js on first run
```

> **Supported distribution for v1:** `npm start` (dev mode) on macOS / Linux / Windows+WSL.
> Packaged builds are best-effort and currently only polished on macOS.

## Configuration

All customization is in **`pentacle.config.js`**. Edit this file to change:

### App Identity

```js
appName: 'Pentacle',           // Window title, titlebar text
appId: 'com.pentacle.app',     // macOS bundle identifier
```

### Paths

```js
workingDirectory: '~/agent-workspace',  // Where new sessions start
apiServer: {
  url: 'http://localhost:7777',         // Session manager API
  script: 'server/server.py',           // Vendored server (repo-relative).
                                        // Resolved repo-first, $HOME second.
  // python: '.venvs/global/bin/python',  // Optional: pin a specific interpreter.
                                          // Omit to auto-detect python3/python on PATH.
},
```

### Agent Commands

Configure what the "New Session" button launches:

```js
agents: {
  claude: {
    label: 'Claude',
    command: 'claude --dangerously-skip-permissions',
    // binary: '~/.local/bin/claude',  // Optional: pin a specific path.
                                        // Omit to resolve from PATH.
  },
  codex: {
    label: 'Codex',
    command: 'codex',
  },
},
```

Pentacle caches the last Codex update check per host in its app data and only re-checks once every 24 hours. When a newer CLI is available, it runs `npm install -g @openai/codex` before opening the Codex session.

### Themes

Full control over dark and light theme colors (CSS variables) and the terminal ANSI color palette:

```js
dark: { bg: '#0c1310', fg: '#b5ccba', blue: '#3fb950', /* ... */ },
light: { bg: '#f5f7f6', fg: '#2d3b32', blue: '#1a7f37', /* ... */ },
terminal: { background: '#0c1310', foreground: '#b5ccba', cursor: '#3fb950', /* ... */ },
```

### Feature Flags

Optional features default to `false` in the example config — they require extra infrastructure. Turn them on only after confirming the required backend is running.

```js
features: {
  mic: false,        // Mic panel + voice record (requires a mic server)
  usage: false,      // Usage bars (requires /api/usage + /api/codex-usage endpoints)
  botsTab: false,    // Bots tab (requires /api/bots endpoint)
  inputBar: true,    // Per-slot input bar — works with any terminal, no extra deps
  dashboards: false, // Dashboards view (requires your own dashboard files; bundled
                     // 0DTE/Amaterasu dashboards query private AWS resources)
},
```

| Feature | What it does | What it requires |
|---------|-------------|-----------------|
| `mic` | Mic control panel in sidebar, voice record buttons on each slot | A mic server at `micServerUrl` with `/status`, `/mode/*`, `/copy/*` endpoints |
| `usage` | Claude Code + Codex usage progress bars in sidebar footer, auto-refresh on startup | `/api/usage` and `/api/codex-usage` endpoints on the API server |
| `botsTab` | Separate "Sessions" and "Bots" tabs in sidebar | An `/api/bots` endpoint on the API server |
| `inputBar` | Keyboard icon on each slot header that toggles a text input bar below the terminal | Nothing — works with any terminal session |
| `dashboards` | Chats/Dashboards view switcher in sidebar, dashboard list panel | Dashboard files in `renderer/dashboards/` + IPC handlers in `main.js` |

Set any feature to `false` to hide it completely. The UI adapts — for example, disabling `botsTab` removes the tab bar entirely since there's only one panel. Disabling `dashboards` hides the view switcher and shows only the chat list.

### Dashboards

Dashboards are live data views that poll a backend and render stats, charts, and tables. They live in `renderer/dashboards/` as self-registering JS files.

**File structure:**

```
renderer/dashboards/
  registry.js      — shared utilities, initializes window.DASHBOARDS = []
  0dte.js          — 0DTE Trading dashboard (prebuilt, ships with repo)
  foreclosure.js   — Foreclosure Pipeline (custom, not in repo)
```

**How it works:**
- `registry.js` creates an empty `window.DASHBOARDS` array and shared utilities
- Each dashboard file is an IIFE that pushes its definition to the array
- Only loaded files register — to exclude a dashboard, don't include its `<script>` tag in `index.html`

**Adding a dashboard:**

1. Create `renderer/dashboards/my-dashboard.js` with a self-registering IIFE:

```js
(function() {
  function mount(container) { /* build DOM, return refs */ }
  function update(refs, data) { /* update DOM from polled data */ }
  function unmount(refs) { /* cleanup intervals/listeners */ }

  window.DASHBOARDS.push({
    id: 'my-dashboard',
    name: 'My Dashboard',
    description: 'What it shows',
    color: 'var(--green)',
    mount, update, unmount,
    pollFn: () => window.cc.getMyStats(),
    pollInterval: 10000,
  });
})();
```

2. Add the script to `index.html` (after `registry.js`, before `app.js`):
```html
<script src="dashboards/registry.js"></script>
<script src="dashboards/my-dashboard.js"></script>
<script src="app.js"></script>
```

3. Add an IPC handler in `main.js` and a preload bridge in `preload.js` for the data fetch.

**Prebuilt dashboards:**
- **0DTE Trading** (`0dte.js`) — SPX iron condor pipeline. Shows live market data, P&L, positions, trade history, signal flow. Smart polling: only fetches during market hours, shows countdown timer and "Day Complete" state after close. Requires an IBKR Gateway running on `localhost:7400`.

**Custom dashboards (not in repo):**
- These are specific to your bot's workload. Add the JS file and include it in your `index.html`. Example: the Foreclosure Pipeline dashboard queries a Postgres database for scraper progress.

## Running

```bash
# Development (all platforms)
npm start

# macOS: build + install to /Applications
npm run build:mac

# Or use the deploy script (macOS: kills running app, rebuilds, relaunches)
./deploy-mac.sh
```

## How It Works

1. Pentacle starts a tmux session manager API server (if not already running)
2. The sidebar polls the API for active tmux sessions
3. Click a session to attach it to a terminal slot via `tmux attach-session`
4. Each slot runs a `node-pty` process connected to tmux, rendered by xterm.js with WebGL
5. Activity detection polls tmux pane content to classify sessions as working/waiting/idle
6. The input bar sends text directly to the PTY, supporting multiline input with Ctrl+Enter

## Structured Chat Streaming

Pentacle also has a websocket-backed structured chat mode layered on top of the tmux sessions.

- Daemon: `~/agent-workspace/multi-machine-chat/chat_streamd.py`
- Current hosts: Bart, Abra, Amaterasu
- Current providers: Claude and Codex
- Desktop renderer: slot-level `Chat` / `Terminal` toggle

This is the intended foundation for non-desktop clients too. See [docs/mobile_handoff.md](docs/mobile_handoff.md) for the current handoff for building a mobile app on top of the same session and streaming layer.

## Chat UI Mockups

The structured chat UI has a fixture-driven mockup generator so you can review proposed transcript behavior visually before or alongside live testing.

Generate the mockup page with:

```bash
cd /Users/bartimaeus/pentacle
npm run mockups:chat-ui
```

Output:

- `test/artifacts/chat_ui_mockups.html`

The page is generated from:

- production renderer helpers: `renderer/chat_ui_state.js`
- renderer fixtures: `test/fixtures/slot_chat_state_subjects.json`

Each card shows:

- case label
- source input/state
- expected visible output
- production-rendered preview

This is intended to be the fast review surface for transcript/UI changes before live QA.

## License

MIT
