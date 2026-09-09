# Pentacle

Pentacle is a desktop workspace for coding-agent sessions, shared work specifications and review evidence. Optional services add structured chat, questions and coordination across machines.

## Run from source

Install Node.js and npm, tmux, and the agent command-line tools you intend to use. Configure those tools with your own accounts. The Python services require Python 3.11 or newer.

```sh
npm install
mkdir -p "$HOME/.config/pentacle-private"
cp pentacle.config.example.js "$HOME/.config/pentacle-private/pentacle.config.js"
PENTACLE_CONFIG="$HOME/.config/pentacle-private/pentacle.config.js" npm start
```

No lockfile is published; `npm install` resolves dependencies (a `package-lock.json` is regenerated as your repository's first follow-up commit). Edit your private configuration to set the workspace and agent commands for your machine. Optional service features are disabled in the example until their backends are configured. See the [daemon setup](services/chat-stream-v2/README.md) and [orchestration CLI](services/agent-orch/README.md) for those components. Keep credentials, runtime databases and machine-specific configuration outside the source tree.

## Build and test

The desktop shell itself needs no separate compile step to run (`npm start` runs it directly). Build the renderer bundle and run the JavaScript checks and Python service tests from a throwaway `HOME`:

```sh
npm run build:renderer       # bundle the renderer (no bundling is needed for `npm start`)
npm test                     # renderer/main unit checks
python3 -m pytest services/chat-stream-v2/tests   # daemon unit tests (Python 3.11+)
```

## Daemon and CLI

Install the orchestration CLI and start a standalone, non-live daemon (loopback, caller-chosen free port, ephemeral memory) from a throwaway `HOME`:

```sh
bash services/agent-orch/install.sh                 # install the agent-orch CLI
export PENTACLE_MACHINES_FILE="$PWD/services/chat-stream-v2/machines.local.json"
python3 services/chat-stream-v2/main.py --host 127.0.0.1 --port 0 --db :memory:
```

`--port 0` picks a free port so it never collides with any running daemon; `--db :memory:` keeps state ephemeral. Full daemon options are in the [daemon setup](services/chat-stream-v2/README.md) and CLI usage in [services/agent-orch/README.md](services/agent-orch/README.md). All commands run offline against the source tree; none require a live service, network access, or a production port.

## Use the full development process

Start with [PROCESS.md](PROCESS.md) and the [workspace setup guide](process/README.md). The bundled process includes:

- A root [AGENTS.md](AGENTS.md) for common agent instructions and role baselines.
- Spec and summary templates, lifecycle directories, schemas, catalog generation, validation and search tools.
- [Development guidelines](process/docs/config/development_process.md) and [QA guidelines](process/docs/config/qa_guidelines.md) covering failing-journey diagnosis, independent checks, evidence and closure.
- [Agent coordination](process/docs/config/agent_orchestration.md) and [private configuration](process/docs/config/private_configuration.md) guidance.

Copy the process workspace to a private location before adding real specifications or receipts. You can follow the spec and QA workflow using the bundled Python tools without running Pentacle; the orchestration commands require the separately configured daemon and CLI.


## Known test gaps

The published default test suites are not fully green yet; these failures are behavioral/environment drift and descoped-private-feature coverage, not privacy, install, or start/build issues (install, `npm run build:renderer`, the agent-orch CLI install, and a free-port daemon start all pass). They are being greened in the open as the first follow-up.

- **JS (`npm test`), ~42:** renderer interpreter / display-rule drift; the generated `publicdashdefs` dist (built by the dashboards `npm run build`, not shipped); ChatStreamClient limits; spawn/asset/schedule IPC; governance; harness-telemetry; singleton lifecycles.
- **Daemon (`pytest`), ~75 (+6):** coverage for descoped private features (attestations, blob-prompt, spawn-catalog models); casing/API drift; tests needing `working_state` fixtures or the predeploy workflow; env-sensitive headless tests.

Tests that require a live provider CLI are skipped by default; set `PENTACLE_LIVE_TESTS=1` to run them.
