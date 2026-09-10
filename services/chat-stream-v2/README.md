# Chat stream daemon

The daemon launches configured Claude/Codex CLI sessions in tmux, ingests their native JSONL transcripts, persists session and coordination state, and serves WebSocket clients. It includes the `agent-orch` coordination RPCs, assets, schedules, notifications and authenticated desktop/mobile transport.

Use the complete [local startup recipe](../../README.md#local-setup) first. It aligns the desktop and daemon host identity (`local`), native transcript directory, loopback endpoint and desktop credential. Python 3.11+ and tmux are required; provider CLIs require their own accounts and authentication.

## Private configuration

`PENTACLE_MACHINES_FILE` selects a private machine JSON file; `PENTACLE_MACHINES_JSON` supplies the same shape inline. [machines.example.json](machines.example.json) demonstrates a single local machine. Set executable locations and working directories for your machine. `projects_root` must point to the actual Claude `.claude/projects` directory. A daemon's `--local-host` must match its machine name and the host used by clients.

Without peer configuration, the daemon runs locally. To keep specs and QA records outside the checkout, configure the documented memory root for the orchestration CLI and daemon. Do not put transcripts, credentials, runtime databases or real work receipts in the public source tree.

`python services/chat-stream-v2/main.py --help` lists runtime options. `--port 0 --db :memory:` is useful for a temporary probe; ephemeral sessions alone do not isolate every optional service database. The desktop smoke explicitly sets separate session, notification, asset and blob paths and uses its own tmux socket.

## Desktop and mobile credentials

`tools/operator_auth_cli.py issue --client-kind pentacle` issues a desktop credential; `--client-kind pentacle-mobile` issues a separate mobile credential. The JSON output's `code` field is secret. Store desktop credentials in a mode-0600 file under a mode-0700 directory and point `chatStream.tokenPath` at its canonical path. Do not place a v2 credential inline in the desktop config. The CLI's `list`, `revoke` and `rotate` commands manage credentials in the daemon's registry.

For the mobile app, create a **single-use enrollment link** on the daemon host,
as the same OS user running the daemon. This is separate from the raw credential
envelope returned by `operator_auth_cli.py issue`; that envelope cannot be used
as an eight-character enrollment code.

```sh
python services/chat-stream-v2/tools/mobile_enrollment_cli.py \
  --ws-url ws://127.0.0.1:7791 --label simulator
```

The JSON contains a secret `url` that expires after ten minutes and can be used
once. Open it in the installed mobile app. The app exchanges the code with this
daemon and stores the resulting credential in SecureStore. Create the link only
after the app is built and the daemon is reachable. The issuer writes the
existing private `~/.config/pentacle-mobile/enrollment-codes.json` registry with
owner-only permissions; no daemon restart is needed. `--ttl-seconds` accepts
1–3600 seconds. Do not commit or publish the link.

For a physical phone, replace loopback with the daemon host's reachable LAN/VPN
endpoint and bind the listener to the appropriate interface. Keep access on a
private network or authenticated tunnel; use WSS if traffic crosses an untrusted
network. See [network access](../../docs/REMOTE_AUTH.md) and the complete
[mobile setup guide](https://github.com/HJK6/pentacle-mobile/blob/main/docs/FRIEND_SETUP.md).
An SSH tunnel can retain a loopback daemon listener; forwarded connections
inherit the tunnel endpoint's trust boundary. Restrict tunnel access to the
operator.

## Tests and supported integrations

```sh
python -m pip install -r services/chat-stream-v2/requirements.txt -e services/agent-orch
python -m pytest services/chat-stream-v2/tests
python -m pytest services/agent-orch/tests
python services/chat-stream-v2/tools/run_gate.py merge
```

Run the final merge gate from a clean checkout; it produces a source-bound evidence file outside the repository. The macOS preflight requires the [127.0.0.2 loopback alias](deploy/loopback-alias/README.md). See [public runtime boundaries](../../docs/public_release.md) for unsupported private integrations and the real desktop smoke. Fixtures must use invented content and valid public wire identifiers.
