# Agent setup: metrics, shared memory and questions

Operational instructions for the setup agent. Complete the base
[agent setup](../AGENT_SETUP.md) first. Keep account credentials and machine
configuration outside the public repositories; the human READMEs stay brief.

## Claude account limits

The daemon publishes a local state file; it does not query provider accounts.
The included `scripts/check_claude_usage.py` reads the **account-period
percentage used** from Claude OAuth. On macOS it reuses Claude Code's standard
`Claude Code-credentials` Keychain item without printing or copying the token;
on other platforms set `CLAUDE_CODE_OAUTH_TOKEN` in the collector's protected
environment. Enterprise monthly `spend.percent` is preferred. The authenticated
CLI `/usage` screen remains a fallback for labeled monthly and older weekly
accounts. This is account quota, not session cost or requests per minute. It
does not change the quota. The Fable row is optional and the client hides it
when no Fable quota is reported.

1. As the same OS user that will run the collector, verify Claude login and
   complete the trust prompt **in the exact directory you will pass as
   `PENTACLE_USAGE_CWD`** (the probe launches the CLI there and never
   auto-accepts trust; if that directory is untrusted it fails with
   `cwd '<dir>' is not a trusted Claude workspace ...`). If `PENTACLE_USAGE_CWD`
   is unset the probe uses `$HOME` (never the launchd cwd `/`), which must then be
   trusted. Install tmux. Then, from this repository:

   ```sh
   export PENTACLE_USAGE_CWD="$HOME/workspace"
   .venv/bin/python scripts/check_claude_usage.py --json
   ```

   Note: an account relog can drop a directory's trust; re-trust the
   `PENTACLE_USAGE_CWD` folder (or repoint the variable at a trusted one) after
   switching Claude accounts.

   Set `PENTACLE_USAGE_CLAUDE_BIN` and `PENTACLE_USAGE_TMUX_BIN` to absolute paths
   if the service PATH needs them. If OAuth does not expose a supported account
   period, the bounded fallback opens its own tmux server, sends only `/usage`
   and closes that server. It never accepts trust/login prompts or sends a model
   prompt. No private browser service is needed. On managed networks, also set
   `SSL_CERT_FILE` to the organization's trusted CA bundle in the collector
   environment.
2. Verify `week_all_pct` and `week_all_resets` against the labeled account-period
   observation. The legacy wire-field names remain stable for compatibility.
   Optional `week_fable_pct`/`week_fable_resets` remain `null` unless explicitly
   reported. Never substitute Sonnet, session usage or zero for an unknown pool.
3. Write `usage_state.json` **beside the daemon's actual persistent DB**. The
   Codex probe prefers a weekly window and also supports the account-period
   `individualLimit` returned by current business accounts. For the
   base README's `~/.config/pentacle/sessions.db`:

   ```sh
   .venv/bin/python services/chat-stream-v2/tools/collect_usage_state.py \
     --state "$HOME/.config/pentacle/usage_state.json" \
     --shared-scripts "$PWD/scripts" --skip-codex
   ```

   `--skip-codex` makes this a Claude-only setup without a missing Codex helper;
   it preserves unknown/previous Codex state. A Codex account probe is a separate
   optional integration. Keep the usage publisher enabled; do not pass
   `--disable-usage-state-publisher` to the daemon.

   For a Codex-only installation, omit `--skip-codex` and pass `--skip-claude`.
   This preserves unknown/previous Claude and Fable state without requiring a
   Claude login or trusted Claude workspace.
4. Install one user-owned scheduled job running that exact command every five
   minutes, with absolute paths and explicit PATH, HOME and
   `PENTACLE_USAGE_CWD`. Use a macOS LaunchAgent with `RunAtLoad=true` and
   `StartInterval=300`, or a Linux user systemd service/timer with
   `OnBootSec=30s` and `OnUnitActiveSec=300s`. Reuse an existing owner/job rather
   than creating overlapping pollers.
5. Enable `features.usage: true` in the private desktop config and restart the
   desktop. The enrolled mobile app reads the same limits in Settings; do not
   put provider credentials on the phone.

Acceptance requires fresh Claude health `ok` in the state file and the observed
percentage/reset in the actual client. The collector can exit zero while
recording a provider error. If login expires or `/usage` changes, retain the
last good value with its stale/error status; inspect the current UI and repair
the probe. Do not fabricate zero or repeatedly rebuild clients.

## Machine stats

The daemon samples its local host every 30 seconds without provider login:
system-wide CPU utilization, one-minute load average (retained on the wire for
older consumers), used/total RAM, used/total space on the home filesystem, and
uptime. `cpu_usage_pct` is a measured 0–100% value or `null` when the probe is
unavailable; older satellites may omit it. The web card shows `--` for missing,
null, or stale CPU values and never treats load average as utilization. Use a
consistent `--local-host` identity and matching client `hosts` key. Enrolled
clients receive `hosts.stats`.

Check that sample timestamps advance and CPU/RAM/disk values agree with the host
OS. On Linux, CPU is the busy fraction between two `/proc/stat` reads; on macOS,
it is `100 - idle` from the second `top` sample. Samples older than 90 seconds
are stale. Desktop machine stats do not depend on the Claude usage toggle.
Mobile Settings preserves distinct configured host
names and offline entries.

For another computer, install the Python dependencies and run its satellite.
Choose a unique host key and configure it in the clients. Set the same strong
`PENTACLE_EVENT_PUSH_SECRET` in the coordinator and satellite's private service
environments. On the satellite:

```sh
export PENTACLE_SATELLITE_WS="ws://<coordinator-private-address>:7791"
export PENTACLE_SATELLITE_HOST="build-server"
export PENTACLE_SATELLITE_NO_AUTOUPDATE=1
.venv/bin/python services/chat-stream-v2/satellite.py
```

Use a private network/tunnel or WSS per [network access](REMOTE_AUTH.md). After
a foreground check, install a service with the same explicit environment.
Keep event pushing enabled and honor any existing coordinator artifact pin.
Confirm the initial sample and subsequent 30-second samples arrive. Do not
print/sync the push secret. A satellite does not copy memory or provider logins.

## Shared memory with Syncthing

Copy the complete `process/` tree to a private directory, such as
`~/pentacle-memory`, preserving any existing workspace. Install its Python venv
and requirements using [process setup](../process/README.md). Keep templates,
schema, scripts and `work/statuses.json` together.

Choose one machine to own Git history and catalog generation. Peers edit shared
source files but do not regenerate catalogs or run competing Git commit/pull
operations. Git is optional history/backup; Syncthing distributes current files.
Never attach private memory to the public Pentacle Git remote.

1. Install Syncthing on every computer and enable supported per-user autostart.
   Verify `syncthing --version` and the local GUI. Follow the official
   [installation](https://docs.syncthing.net/intro/getting-started.html) and
   [autostart](https://docs.syncthing.net/users/autostart.html) instructions.
2. Pair the actual device IDs mutually. Share a dedicated folder ID, for example
   `pentacle-memory`, from the private workspace into an empty directory on each
   new peer. Local absolute paths may differ. Confirm IDs and paths; do not
   merge unrelated populated folders.
3. Use **Send & Receive** on computers whose agents edit memory; use Receive
   Only only for a deliberately read-only consumer. Configure this through the
   local GUI or authenticated local CLI/API, keeping its credentials private.
   See [folder types](https://docs.syncthing.net/users/foldertypes.html).
4. Install root `.stignore` on **each** device before sharing:

   ```text
   .git
   .venv
   __pycache__
   node_modules
   .local
   .cache
   soul.md
   .DS_Store
   *.tmp
   *.swp
   ```

   Add actual machine-only caches/secrets. Keep daemon DBs, credentials,
   provider profiles and native build artifacts outside the memory folder.
   `.stignore` itself does not sync, so configure every peer. Do not ignore
   `work/`, `docs/`, templates or catalogs. See
   [ignore patterns](https://docs.syncthing.net/users/ignoring.html).
5. Set both integrations to each computer's local absolute workspace path:

   ```sh
   export PENTACLE_MEMORY_ROOT="$HOME/pentacle-memory"
   export AGENT_ORCH_MEMORY_REPO="$PENTACLE_MEMORY_ROOT"
   ```

   Put the first in the daemon service environment and the second in the agent
   CLI environment. A shell export does not change a running service; reload
   with its preserved config and verify the effective paths. Give agents the
   repository's root `AGENTS.md`, a local instruction pointing to this private
   workspace, and the workspace's relevant `agents/` role baseline. The copied
   `process/` tree does not itself contain a root `AGENTS.md`.
6. After sync settles, the owner runs `scripts/generate_catalog.py` then
   `scripts/validate_memory_v2.py` with the workspace venv. Peers can validate
   with `--source-only` while catalogs catch up. One agent owns a work item at a
   time; synchronization does not provide file locks or Markdown merging.
7. Verify both directions with an owned scratch note: create on one computer,
   observe identical contents on another, edit there and verify the first sees
   the edit. Remove only the probe afterward. Check all devices/folders report
   up to date and agents resolve the correct workspace.

Enable file versioning, maintain a separate backup and preserve/merge
`sync-conflict` copies instead of deleting them blindly. Versioning protects
changes received from peers, not every local edit; see
[file versioning](https://docs.syncthing.net/users/versioning.html).
The phone reads daemon data and does not need a synced memory filesystem.

## Operator questions go through the active chat

The daemon already launches Claude with `--disallowed-tools AskUserQuestion`.
Do not re-enable that native tool or edit global Claude settings. Verify the
effective launch arguments and give agents the process question/routing rules.
Codex and Claude seats receive the active-chat question rule in their launch
instructions.

Install `agent-orch` as in the base README and make it available on spawned
agents' PATH. When operator input is genuinely required, an authenticated,
open, visible Pentacle seat asks one concise question directly in its active
chat and ends the turn. Do not use `request_user_input`, `agent-orch prompt
ask`, or an Updates card as the operator-question channel. Never treat silence
or elapsed time as approval.

A hidden worker never asks the operator directly. It routes the question to its
visible parent and continues independent work or reports blocked:

```sh
agent-orch tell <visible-parent-stream-id> \
  "BLOCKER: Which absolute workspace path should this task use?"
```

A standalone bootstrap agent cannot gain another seat's authority by claiming
its stream ID; use a real visible Pentacle seat for this workflow. Keep its
provided credentials private. The visible parent owns the final operator
question and synthesis. See
[orchestration](../process/docs/config/agent_orchestration.md) for the full role
and reporting contract; an injected fleet rule may override its portable
question transport.
