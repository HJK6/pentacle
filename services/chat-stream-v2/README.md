# Chat stream v2

This directory contains small, testable building blocks for turning provider
observations into chat-stream events.  The public examples are deliberately
local and deterministic: they do not connect to a fleet, deploy services, or
send data to a remote endpoint.

## What is included

- `boot_ready.py` contains pure predicates for recognizing an idle provider
  prompt and for distinguishing a submitted prompt from an editable draft.
- `claude_jsonl_norm.py` and `codex_rollout_norm.py` normalize provider JSONL
  records into the same event vocabulary.
- `context_adapters.py` extracts usage readings and classifies context levels.
- `session_names.py` provides a bounded classifier for synthetic session names.
- `machines.example.json` documents the shape of a local configuration without
  assuming a particular username, directory layout, or host.

The normalizers accept ordinary Python dictionaries, return ordinary Python
dictionaries, and perform no I/O.  A caller can therefore choose its own input
source, persistence layer, and transport while testing the transformation logic
in isolation.

## Local setup

Run the checks from the repository root with a supported Python interpreter:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r services/chat-stream-v2/requirements.txt
python3 -m pytest services/chat-stream-v2/tests
```

The test fixtures contain synthetic session identifiers, timestamps, pane text,
and short example messages.  They are examples of input shapes, not captured
provider sessions.  When adding a fixture, use the same convention and avoid
real user text, hostnames, paths, credentials, or service URLs.

## Run the daemon

The daemon (`main.py`) is a self-contained CLI. A local, non-live run binds
loopback on a configurable port and keeps all state ephemeral:

```sh
cp machines.example.json machines.local.json      # edit for your machine names/paths
export HOME="$(mktemp -d)"                          # throwaway HOME for all local state
export PENTACLE_MACHINES_FILE="$PWD/machines.local.json"
python3 main.py --host 127.0.0.1 --port 0 --db :memory:   # --port 0 picks a free port (never collides with a running daemon)
```

- `--host 127.0.0.1` keeps the daemon off any live/public interface; `--port`
  is yours to choose (use `--port 0` to pick a free port). Never bind a
  production port.
- `--db :memory:` uses an ephemeral session store ("example memory"); pass a
  file path under the throwaway `HOME` to persist a local example instead.
- Peer machines come from `machines.local.json` via `PENTACLE_MACHINES_FILE`
  (or inline `PENTACLE_MACHINES_JSON`); with a single local entry the daemon
  runs standalone with no remote transport.

Optional spawn/transport flags (`--spawn-command`, `--claude-bin`,
`--codex-bin`, `--ssh-bin`, …) stay empty until you configure your own agent
tools; the daemon starts and serves without them.

## Event shape

Normalized events have a compact common shape:

```json
{
  "host": "hosta",
  "provider": "codex",
  "session_id": "sample-session",
  "session_name": "codex-sample",
  "stream_id": "hosta:codex-sample",
  "timestamp": "2030-01-01T00:00:00Z",
  "kind": "USER",
  "text": "A synthetic example",
  "raw": {}
}
```

`raw` keeps provider-specific details needed by a caller that wants to inspect
the original shape.  The public normalizers add stable per-record indexes so a
caller can deduplicate a replay without depending on a database.

## Configuration boundary

Use an environment variable or an application-owned configuration file to
select a local working directory.  The example machine file is only a schema
reference; executable locations, workspace roots, and provider authentication
remain deployment-specific choices made by the application owner.
