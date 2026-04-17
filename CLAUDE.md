# Pentacle — Agent Onboarding Playbook

Terse, factual runbook for a Claude Code session starting cold on this repo.

## What Pentacle is

Electron desktop app: 4-slot terminal grid, each slot attached to a tmux session via node-pty (local) or SSH (remote). A sidebar lists tmux sessions from an HTTP API, lets you create/trash/rename/restore them. Designed for running multiple Claude Code or Codex sessions side by side.

## Decide: HOST vs CLIENT mode

The code uses one rule: `isClient = !!CONFIG.remote` (`hosts.js`, `main.js`).

| Config shape | Mode |
|---|---|
| No `remote:` block | **HOST** — local tmux, local API server on 7777, local node-pty |
| `remote:` block present | **CLIENT** — SSH tunnel to remote's 7777, remote tmux attaches |
| `localWsl:` only, no `remote:` | Still HOST (local-only; additive, not mode-switching) |

**Bare Windows HOST mode is unsupported.** tmux does not run natively on Windows. Windows users must either:
- (a) Run Pentacle inside WSL as HOST, or
- (b) Run Pentacle on Windows as CLIENT with `remote` pointing at a Linux/macOS machine. Add `localWsl` to also show WSL sessions in the sidebar.

## First-run: HOST mode (macOS / Linux)

1. Install tmux: `brew install tmux` (mac) or `sudo apt install tmux` (Ubuntu).
2. Install Python 3.9+: `brew install python` or `sudo apt install python3`.
3. Install `claude` CLI: `npm install -g @anthropic-ai/claude-code` (or per Anthropic docs).
4. Install `codex` CLI: `npm install -g @openai/codex` (optional, for Codex sessions).
5. `npm install` — rebuilds node-pty against Electron's ABI via `scripts/postinstall.js`.
6. `npm start` — auto-copies `pentacle.config.example.js` → `pentacle.config.js` on first run.
7. The vendored server (`server/server.py`) starts automatically on port 7777.

## First-run: CLIENT mode (macOS / Linux → remote host)

1. Confirm SSH key auth works: `ssh -p 22 user@host` succeeds without a password prompt.
2. Ensure the HOST machine has Pentacle running (its API server must be up on 7777).
3. In `pentacle.config.js`, uncomment the `remote:` block and fill in `host`, `user`, `apiPort`.
4. `npm install && npm start`.

## First-run: CLIENT mode (Windows + WSL)

1. Install WSL: `wsl --install -d Ubuntu` in PowerShell (admin).
2. Inside WSL: `sudo apt install openssh-server tmux python3 build-essential`.
3. Configure sshd on port 2222: edit `/etc/ssh/sshd_config` → `Port 2222`, then `sudo service ssh start`.
4. Create a non-root user inside WSL with `sudo` access. Install `claude` + `codex` for that user.
5. In Windows PowerShell: `ssh-keygen -t ed25519`. Copy `~\.ssh\id_ed25519.pub` content into WSL's `~/.ssh/authorized_keys`.
6. Test: `ssh -p 2222 <wsl-user>@<wsl-ip>` from PowerShell — must succeed without a password.
7. In `pentacle.config.js`: uncomment the `remote:` block (pointing at your mac/Linux host) and the `localWsl:` block (pointing at WSL). If no remote host, use only `localWsl:` with HOST mode inside WSL instead.
8. `npm install && npm start` from Windows.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Blank sidebar | API server didn't start | Check `python3` on PATH; check port 7777 free (`lsof -i :7777`); check tmux installed |
| "tmux not found" in server log | tmux not installed | `brew install tmux` / `sudo apt install tmux` |
| WSL IP drift (connection refused) | Auto-detection failed | Pin `localWsl.host` to the WSL eth0 IP in config |
| SSH auth fails on tunnel | No key or host key mismatch | Pre-populate known_hosts: `ssh user@host` manually first; check `~/.ssh/authorized_keys` on host |
| SSH permission denied (publickey) | Public key not in host's authorized_keys | Copy your public key: `ssh-copy-id user@host` or append `~/.ssh/id_ed25519.pub` to `~/.ssh/authorized_keys` on the host |
| node-pty rebuild fails on Linux | Missing build tools | `sudo apt install build-essential python3-dev`, then `npm install` again |
| Sessions disappear after reboot | tmux server killed on reboot | Install tmux-resurrect; without it tmux sessions don't survive reboots |
| macOS Gatekeeper blocks app | Unsigned Electron build | Right-click → Open, or `xattr -dr com.apple.quarantine Pentacle.app`. Avoid by using `npm start` |
| Windows SmartScreen flags app | Unsigned Electron build | Use `npm start` instead of packaged build |
| Firewall prompt on 7777 | Localhost listener | Allow localhost binds; the server binds 127.0.0.1 only |

### SSH trust note

Pentacle tunnels with `StrictHostKeyChecking=no` (`main.js`). This avoids first-connect prompts but accepts any host key. For better security: run `ssh user@host` manually once to populate `~/.ssh/known_hosts`, or set `StrictHostKeyChecking=accept-new` in `~/.ssh/config` for the host. Changing the Pentacle default is deferred to v2.

## Crash recovery

Two layers (both automatic):

1. **Pentacle slot-state** (`~/.config/Pentacle/.slot-state.json`) — restored on next launch if tmux sessions still exist.
2. **tmux session survival** — tmux sessions survive Pentacle restarts and user logouts by default. They do NOT survive machine reboots without [tmux-resurrect](https://github.com/tmux-plugins/tmux-resurrect).

## Config-only customizations

Edit `pentacle.config.js` only — no JS knowledge required for:
- Changing app name, colors, terminal palette
- Toggling features on/off (`features.mic`, `features.usage`, `features.botsTab`, etc.)
- Pointing `apiServer.script` at a different server
- Adding `remote:` or `localWsl:` blocks to switch modes

## Enabling Bartimaeus-style dashboards

Set `features.dashboards: true` in `pentacle.config.js` AND provide AWS credentials in `~/.aws/credentials`. The 0DTE and Amaterasu dashboards query Vamshi-specific DynamoDB/S3 resources — they return errors harmlessly if creds are absent.

## Builds

| Command | What it does |
|---------|-------------|
| `npm start` | Dev mode — supported on macOS, Linux, Windows+WSL |
| `npm run build:mac` | Build + install to /Applications (macOS only) |
| `npm run build:linux` | Best-effort packaged build (dir output) |
| `npm run build:win` | Best-effort packaged build (dir output) |
| `./deploy-mac.sh` | Kill running app, rebuild, relaunch (macOS convenience script) |

Packaged builds are best-effort. `npm start` is the supported distribution for v1.
