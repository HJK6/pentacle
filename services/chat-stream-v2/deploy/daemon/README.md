# Start the daemon at login

These units start the `chat_streamd` v2 daemon when you log in and restart it if it
exits, on macOS (launchd) and Linux (systemd user unit, including WSL with systemd
enabled). They are opt-in: nothing here is installed or started unless you run the
installer.

Not covered yet:

- **Windows without systemd in WSL.** There is no Task Scheduler unit.
- **Restoring the assistant.** After a reboot the daemon is back, but a protected
  assistant whose pane died still needs the explicit recovery in
  [`docs/assistant.md`](../../../../docs/assistant.md). The daemon does not restart it.

## Before you install

- Create the daemon's virtual environment in the checkout the service will run from
  (`services/chat-stream-v2/.venv`) and install `services/chat-stream-v2/requirements.txt`
  and `services/agent-orch` into it.
- Install `tmux` and log in to your provider CLIs as the same user.
- Use a dedicated checkout, not a development worktree: the service runs whatever is
  checked out there.

## Install

Run the installer with any Python 3.11+ from the checkout. All three steps are
separate; nothing starts until `--enable`.

```sh
cd services/chat-stream-v2/deploy
python3 install_daemon_service.py --release-checkout ~/repos/pentacle --print    # look first
python3 install_daemon_service.py --release-checkout ~/repos/pentacle            # write the unit
python3 install_daemon_service.py --enable                                       # start now and at login
```

The install step writes one file and starts nothing:

| Platform | Unit file |
|---|---|
| macOS | `~/Library/LaunchAgents/com.pentacle.chat-streamd-v2.plist` |
| Linux | `~/.config/systemd/user/pentacle-chat-streamd-v2.service` |

If a unit file is already there with different content, the installer stops and
changes nothing. Pass `--replace` to overwrite it; the old file is kept for
`--rollback`.

Options:

| Option | Default | Meaning |
|---|---|---|
| `--release-checkout` | required | Checkout the daemon runs from. |
| `--python` | `<checkout>/services/chat-stream-v2/.venv/bin/python` | Daemon interpreter. |
| `--tmux-bin` | `tmux` on your `PATH` | Its directory leads the unit's `PATH`. |
| `--state-dir` | `~/.local/share/pentacle-stream` | Session, notification and asset stores and blobs. |
| `--spawn-cwd` | your home directory | Directory new agent sessions start in. |
| `--port` | `7791` | Loopback port the daemon listens on. |
| `--local-host` | the machine's hostname | This machine's name in your machines file. |

Paths containing spaces, quotes, backslashes, `%`, `$` or `;` are refused, because the
two unit formats quote them differently.

Both units run the same command, on `127.0.0.1` only. To listen on another interface
or pass other daemon options, edit the installed unit; `--print` shows the starting
point and `main.py --help` lists the options.

### Linux: start at boot, and settings

```sh
loginctl enable-linger "$USER"                                    # start without an interactive login
systemd-analyze --user verify ~/.config/systemd/user/pentacle-chat-streamd-v2.service
```

Put `PENTACLE_*` settings, one `KEY=VALUE` per line, in `~/.config/pentacle/daemon.env`.
The unit reads the file if it exists. On macOS, add them to the plist's
`EnvironmentVariables` instead.

## Check that it is running

```sh
agent-orch list          # exits 0 and prints a JSON list (empty on a new installation)
```

If your CLI is not already configured for this daemon, set
`AGENT_ORCH_WS_URL=ws://127.0.0.1:7791` first.

Logs:

| Platform | Where |
|---|---|
| macOS | `~/Library/Logs/pentacle/chat-streamd-v2/daemon.out.log` and `daemon.err.log` |
| Linux | `journalctl --user -u pentacle-chat-streamd-v2 -f` |

Service state: `launchctl print gui/$(id -u)/com.pentacle.chat-streamd-v2` on macOS,
`systemctl --user status pentacle-chat-streamd-v2` on Linux.

## Remove it

Stop the service first, then roll back:

```sh
launchctl bootout gui/$(id -u)/com.pentacle.chat-streamd-v2      # macOS
systemctl --user disable --now pentacle-chat-streamd-v2.service  # Linux
python3 install_daemon_service.py --rollback
```

Rollback restores what was at the unit path before the first install: the earlier
file, or nothing. It does not stop the service. It changes a file only when the
service manager explicitly reports the service stopped (launchd no longer knows the
label; systemd reports the unit `inactive` or `failed`). If the service is running,
or its state cannot be read, rollback exits with an error, prints the stop command,
and changes nothing.

## Using it with `deploy.py` (macOS)

[`deploy.py`](../deploy.py) restarts a LaunchAgent with this label and refuses to run
when none is loaded, so this installer is the first-time setup that `deploy.py` builds
on. The installed plist meets its checks when you keep the default `--python`: the
interpreter is inside the release checkout, and the log directory exists. A later
`deploy.py` run adds its own environment keys to the plist; after that, re-running
this installer needs `--replace`.
