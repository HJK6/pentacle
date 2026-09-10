# Agent orchestration setup

Pentacle orchestration consists of a websocket daemon and the one-shot `agent-orch` CLI. The daemon owns session inventory, transport, persistence, and RPC dispatch. Each CLI invocation connects, performs one operation, prints a typed response, and exits.

## Local prerequisites

Install Python dependencies, tmux, and a provider CLI in a checkout-local environment:

```bash
python3 -m venv .venv-dev
.venv-dev/bin/pip install -r services/chat-stream-v2/requirements.txt
.venv-dev/bin/pip install -e services/agent-orch
```

Inspect supported daemon options with:

```bash
.venv-dev/bin/python services/chat-stream-v2/main.py --help
```

## Machines file

Start from a synthetic local-only configuration. Host ids are public labels; they do not identify a real machine.

```json
{
  "machines": [
    {
      "name": "coordinator",
      "ssh_target": null,
      "tmux_bin": "tmux",
      "claude_bin": "/absolute/path/to/claude",
      "codex_bin": "/absolute/path/to/codex",
      "cwd": "/tmp/pentacle-example/work",
      "projects_root": "/tmp/pentacle-example/projects",
      "label": "Example host"
    }
  ]
}
```

Replace the provider paths with installed executables and create the configured
working and projects directories. Save the file outside the repository with
owner-only permissions, for example `/tmp/pentacle-example/machines.json`.
Select it explicitly in the shell that starts the daemon:

```bash
unset PENTACLE_MACHINES_JSON
export PENTACLE_MACHINES_FILE=/tmp/pentacle-example/machines.json
```

Resolution is inline `PENTACLE_MACHINES_JSON`, then `PENTACLE_MACHINES_FILE`,
then `~/.config/pentacle-stream/machines.json`, then a synthesized `local` entry.
`XDG_CONFIG_HOME` does not select this file. Start the daemon as in
[developer onboarding](developer_onboarding.md), changing `--local-host local`
to `--local-host coordinator` for this example. Unknown hosts return the typed
`unsupported_host` response.

## Install and configure the CLI

The editable pip install above installs the CLI in `.venv-dev/bin`. Activate that
environment and verify that the invoking shell can find it:

```bash
source .venv-dev/bin/activate
command -v agent-orch
```

For a persistent CLI config, save the following as `~/.agent-orch/config.json`
without overwriting an existing private setup. Alternatively set
`AGENT_ORCH_HOST_ID=coordinator` and `AGENT_ORCH_WS_URL=ws://127.0.0.1:7791`
in the current shell. Use a loopback websocket for local development:

```json
{
  "chat_stream": {"url": "ws://127.0.0.1:7791"},
  "local_host_id": "coordinator"
}
```

Keep local configuration and any optional credential material outside version control. The protocol's authentication boundaries are documented separately; an unauthenticated local development daemon may be used for fixture tests.

## Smoke checks

```bash
agent-orch list
agent-orch reconcile status --json
```

Then run one scratch spawn, send a fixture prompt, inspect the typed result, and close the session. Use `--request-id` when retrying an operation after an uncertain response.

## Troubleshooting

| Symptom | Check |
|---|---|
| `unknown_local_host` | The CLI host id must match a machine entry. |
| `unsupported_host` | Add the synthetic host to the local machines file. |
| `host_offline` | Check the configured transport and executable paths. |
| CLI not found in a pane | Install the CLI and ensure its bin directory is on `PATH`. |
| Desktop disconnected | Verify `chatStream.url` and the daemon process. |

All examples in this guide are local, deterministic fixtures. No private remote, promote, restart, or deploy procedure is part of the public setup.
