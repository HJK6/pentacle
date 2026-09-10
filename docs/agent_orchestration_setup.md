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
      "provider_bin": "example-provider",
      "cwd": "/tmp/pentacle-example/work",
      "projects_root": "/tmp/pentacle-example/projects",
      "label": "Example host"
    }
  ]
}
```

Save a local copy outside the repository, for example at `$XDG_CONFIG_HOME/pentacle-example/machines.json`, with owner-only permissions. Absolute paths make the fixture deterministic. A remote adapter may use the same schema with an explicitly configured `example.local` host, but this public guide does not prescribe a managed service or deployment path.

The daemon resolves the machines file from the documented environment variables, then the configured local path, then a synthesized local entry. Unknown hosts should return a typed `unsupported_host` response.

## Install and configure the CLI

Install the direct CLI from the checkout and verify that the invoking shell can find it:

```bash
bash services/agent-orch/install.sh --dry-run
command -v agent-orch
```

Use a loopback websocket for local development:

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
