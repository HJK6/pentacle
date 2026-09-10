# Chat stream daemon

The daemon launches configured Claude/Codex CLI sessions in tmux, ingests their native JSONL transcripts, persists session and coordination state, and serves WebSocket clients. It includes the `agent-orch` coordination RPCs, assets, schedules, notifications and authenticated desktop/mobile transport.

Use the complete [local startup recipe](../../README.md#local-setup) first. It aligns the desktop and daemon host identity (`local`), native transcript directory, loopback endpoint and desktop credential. Python 3.11+ and tmux are required; provider CLIs require their own accounts and authentication.

## Private configuration

`PENTACLE_MACHINES_FILE` selects a private machine JSON file; `PENTACLE_MACHINES_JSON` supplies the same shape inline. [machines.example.json](machines.example.json) demonstrates a single local machine. Set executable locations and working directories for your machine. `projects_root` must point to the actual Claude `.claude/projects` directory. A daemon's `--local-host` must match its machine name and the host used by clients.

Without peer configuration, the daemon runs locally. To keep specs and QA records outside the checkout, configure the documented memory root for the orchestration CLI and daemon. Do not put transcripts, credentials, runtime databases or real work receipts in the public source tree.

`python services/chat-stream-v2/main.py --help` lists runtime options. `--port 0 --db :memory:` is useful for a temporary probe; ephemeral sessions alone do not isolate every optional service database. The desktop smoke explicitly sets separate session, notification, asset and blob paths and uses its own tmux socket.

## Desktop and mobile credentials

`tools/operator_auth_cli.py issue --client-kind pentacle` issues a desktop credential; `--client-kind pentacle-mobile` issues a separate mobile credential. The JSON output's `code` field is secret. Store desktop credentials in a mode-0600 file under a mode-0700 directory and point `chatStream.tokenPath` at its canonical path. Do not place a v2 credential inline in the desktop config. The CLI's `list`, `revoke` and `rotate` commands manage credentials in the daemon's registry.

For mobile, make the daemon reachable through your private LAN/VPN or a tunnel and configure the app's WebSocket endpoint and mobile credential. Loopback is the default. Use the [transport admission policy](../../docs/REMOTE_AUTH.md) when exposing a listener beyond loopback. An SSH tunnel can retain a loopback daemon listener; forwarded connections inherit the tunnel endpoint's trust boundary. Keep tunnel access restricted to the operator.

## Tests and supported integrations

```sh
python -m pip install -r services/chat-stream-v2/requirements.txt -e services/agent-orch
python -m pytest services/chat-stream-v2/tests
python -m pytest services/agent-orch/tests
python services/chat-stream-v2/tools/run_gate.py merge
```

Run the final merge gate from a clean checkout; it produces a source-bound evidence file outside the repository. The macOS preflight requires the [127.0.0.2 loopback alias](deploy/loopback-alias/README.md). See [public runtime boundaries](../../docs/public_release.md) for unsupported private integrations and the real desktop smoke. Fixtures must use invented content and valid public wire identifiers.
