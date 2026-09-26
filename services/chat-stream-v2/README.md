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

## Remote session interrupt

`send.interrupt` targets a session by `host` and `session_name`. For a configured
remote host, updated web and desktop clients also send the selected open row's
`expected_session_generation`. They capture that value before handing the
request to an asynchronous bridge and retain it for a retry. A remote request
without the selected generation returns `generation_required`; the daemon does
not substitute the current row's generation. The existing local request may
omit it.

For a remote request, the daemon requires an open durable row of the expected
generation and an SSH tmux pane whose session name and PID match that row. It
rechecks the row and exact pane identity before sending one Escape to the
checked pane ID. Unknown hosts, unreachable peers, changed rows or panes, and
missing identity return typed errors without sending a key. A missing pane
returns `pane_unavailable`. A live pane returns `interrupt_unconfirmed` after
the key because a successful tmux send does not prove that the provider stopped
its turn. Installed mobile clients that lack selected-generation propagation
receive `generation_required` for remote targets until their separate client
release; this daemon release does not claim mobile remote interrupt support.

## Tests and supported integrations

```sh
python -m pip install -r services/chat-stream-v2/requirements.txt -e services/agent-orch
python -m pytest services/chat-stream-v2/tests
python -m pytest services/agent-orch/tests
python services/chat-stream-v2/tools/run_gate.py merge
```

Run tests in a harness-owned tmux session on an explicit `tmux -L <test-socket>` namespace, with `env -u TMUX` before Python. The gate runner and preflight refuse an inherited `TMUX`; daemon fixtures clear it and use their own explicit socket. Never run bare `tmux kill-server` against ambient operator state.

Run the final merge gate from a clean checkout; it produces a source-bound evidence file outside the repository. The macOS preflight requires the [127.0.0.2 loopback alias](deploy/loopback-alias/README.md). See [public runtime boundaries](../../docs/public_release.md) for unsupported private integrations and the real desktop smoke. Fixtures must use invented content and valid public wire identifiers.

### Scheduled spawn fleet smoke

`tools/spawn_fleet_smoke.py --dry-run` resolves the full matrix without opening a WebSocket or spawning a session. The no-argument full run requires `PENTACLE_MACHINES_FILE` to point to the same file configured for the daemon. It rejects a missing file or `PENTACLE_MACHINES_JSON`, reads host names from that file, and omits `bart` and `daffodil` if present. An optional `PENTACLE_SMOKE_HOSTS` subset must contain only remaining configured hosts. The dry-run output names the source, selected hosts, excluded names present or absent, and every host/provider/prompt cell.

The launchd template under `deploy/` runs every 12 hours and sets the machine-file path. A full run closes each spawned session, including after cell failure; a failed teardown is reported as a failure even if the provider is over quota. The explicit `<url> <host>` post-deploy canary remains a single Codex promptless cell.

### Router replay harness

`tools/router_replay.py` measures the same `_router_input` builder and SSH
`AssistantRouterAdapter` used by the daemon. It selects a deterministic,
redacted set of Claude transcript sessions, interleaves real turns with the
short-reply and burst cases, and freezes each case's canonical router-input
JSON bytes plus `router_input_sha256`. A run writes only a manifest, report and
run-scoped `misses.jsonl`; it never edits the fixture automatically.

Configure `PENTACLE_ASSISTANT_ROUTER_ENDPOINT` and
`PENTACLE_ASSISTANT_ROUTER_ACTION_PATH`, then run from an isolated harness
session:

```sh
python3 services/chat-stream-v2/tools/router_replay.py \
  --manifest /tmp/router-replay/transcript-manifest.json \
  --seed 20260921 --threads 3 \
  --report /tmp/router-replay/report.json \
  --misses /tmp/router-replay/misses.jsonl
```

Candidate runs must reuse the same manifest and harness configuration and may
add `--baseline-report /tmp/router-replay/baseline.json`; a changed
`harness_config_sha256` or per-case router-input digest is a `HARNESS_ERROR`.
Compare the report before an explicit, reviewed merge:

```sh
python3 services/chat-stream-v2/tools/router_replay.py \
  --misses /tmp/router-replay/candidate-misses.jsonl \
  --merge-misses --fixture services/chat-stream-v2/tests/fixtures/assistant_router_v1_cases.json
```

The router input carries bounded `last_outbound_excerpt` and
`last_outbound_truncated` values for each open lane. They come only from the
newest durable `ASSIST_TEXT` publication associated with its persisted route;
absent output is `null`/`false`. Transcript, manifest, report, fixture and
diagnostic sinks are redacted fail-closed. Keep raw run artifacts outside the
repository and do not put credentials or unredacted transcripts in fixtures.

Lane publication excerpts never replace the current operator input's
`body_excerpt`, `body_truncated` or `original_length`. On a nonzero router
process exit, the durable `router_failure` retains `returncode`, `stdout` and
`stderr` alongside the exception and elapsed time. Each stream captures only
its first 2048 bytes, decoding UTF-8 with invalid/incomplete sequences dropped.
Exit 2 with a bounded stderr JSON object whose `error` is
`assistant_router_failed` records `assistant_router_script_failed`: the remote
script reported an exception, including inference failures. Other exits record
`assistant_router_transport_failed`, an unclassified process/SSH failure rather
than proof of a network outage. Fallback resolution preserves this evidence.

Committed history batches yield between broadcasts so existing socket writers can drain. Each client still has one writer and a 256-frame queue; a blocked writer remains subject to `1011 slow_consumer` isolation. Smoke failure records retain the full exception chain, including the original cell failure when teardown also fails. Preserve the smoke command's complete output when investigating a failed deployment; the deployment stamp contains only a shortened command detail.

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

### QA dispatch containment

The optional [QA dispatch reject counter](../../docs/qa-dispatch-counter.md) adds
`spec-issue adjudicate/diagnose/show` and explicit QA surface/cycle fields for
immediate and scheduled reviews. Enforcement defaults off; the coordinated
release owner activates it after upgrading callers.

## Attachment send correlation

A send claims its logical message before materializing attachments. Retries match
the target, actor, optimistic ID, display text and canonical attachment metadata
within the existing recency window; replaying a request ID remains request-scoped.
Materialized file paths belong only to provider wire correlation. Before any paste,
the daemon appends the final wire correlation to the same receipt history so an
early provider USER event already carries the caption, attachments and optimistic
ID. The newest receipt outcome remains authoritative, including a materialized
`not_landed` result that permits retry. No prior receipt row is rewritten.

When receipt projection replaces provider text, ingress stores a
`provider_text_digest` on that same event before persistence. It hashes the
original normalized provider text and contains no staged path. Both durable USER
proof and the Comms event-proof reader use this shared digest; a caption match
alone cannot confirm an image prompt. Ingress discards producer-supplied digests,
and malformed proof metadata cannot fall back to caption equality. Existing
stream, generation, event-kind and post-watermark proof fences still apply.
