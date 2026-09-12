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

## Context crossing notifications

`NudgeJob` sends `context_advisory` and `context_handoff` notices to an open,
reachable seat and its current live parent, including hidden and working seats.
Parentless seats also get a daemon-owned operator notification through `Notify`.
Title/card reminders keep their existing visible, idle, operator-engaged filter.
Context notices do not refuse spawns or implement a `handoff_planned` exemption.

Claude defaults follow the fleet handoff policy: advisory at
`min(400000, 70% of model window)`, handoff at `min(600000, 85%)`.
`PENTACLE_CONTEXT_ADVISORY_ABS`, `PENTACLE_CONTEXT_HANDOFF_ABS`, and the matching
`_PCT` overrides remain supported. Codex keeps its reported-window 50%/75% lines.

Telemetry ingestion atomically stores the reading and threshold episodes in
`v2_nudge_state.basis`. Each kind has a source-generation-bound epoch, immutable
crossing snapshot, and per-recipient delivery records. A reading below a kind's
threshold rearms its next crossing, even between sweeps. A handoff supersedes
an unsent advisory until context falls below advisory again. Restart preserves
an episode; a reopened source generation starts a new one. Close prunes its state.

Only valid readings no older than 1800 seconds and no earlier than session
creation qualify. Future/malformed readings, offline/dead sources and routing
mismatches are suppressed. An unavailable parent is deferred; it is not treated
as a parentless seat. The retired park API is not restored.

Each recipient uses a stable tell ID and frozen body. A delivered receipt prevents
resending; a committed but unconfirmed paste stays pending and is reconciled
read-only. A crash after a persisted attempt without a receipt is indeterminate:
the owner must investigate it, and the job does not blindly paste again. Proven
pre-input route refusals retry the same ID after the existing cooldown. Operator
cards use the same stable ID as their notification dedup key.

Context delivery attempts share the existing cadence, per-pass cap, cooldown,
and whole-pass error backoff with title/card work. Context gets the available
slots first. `--disable-nudges` disables the combined job. Tagged logs use
`subsystem=context_nudge bug_ref=context_notifications_handoff_proof` and retain
source, kind, epoch, target, outcome and tell ID.

The installed handoff verifier runs without daemon mutation:
`python3 services/chat-stream-v2/tools/context_handoff_proof.py <evidence-directory>`.
It checks delivered successor evidence, source/witness generations, inherited
Fable 5.1/high tuple, predecessor closure and witness parentage. The evidence
producer owns the three disposable seats and must verify all three are closed
and their panes absent after capture. A successful spawn reply alone is
insufficient: existing handoff post-steps are best-effort on reparent failure.
