# Morning household digest

`services/chat-stream-v2/tools/household_due_reminders.py` is a short, standard-library
Python process for a private launchd job. It reads the five Cosmo lists and routines,
then publishes one deterministic due/overdue digest each morning at 08:00
America/Chicago. Empty days send nothing. It never calls a model or changes Cosmo.

## Private configuration

Keep the JSON config, credentials and delivery state outside the source checkout.
The config must be an owned regular file with mode `0600`; state directories use
`0700`. Configure the actual person, composite, local host, CLI and paths locally:

```json
{
  "enabled": false,
  "not_before": "2030-01-01",
  "cosmo_url": "https://household.example.invalid",
  "cosmo_token_file": "/private/config/household.token",
  "self_person": "owner",
  "composite_stream_id": "home:assistant",
  "operator_request_ref": "message:operator-request-id",
  "host_id": "home",
  "seat_cwd": "/private/agent-workspace",
  "sessions_db": "/private/state/sessions.db",
  "binding_name": "assistant",
  "agent_orch": ["/opt/bin/agent-orch"],
  "ws_url": "ws://127.0.0.1:7791",
  "state_root": "/private/state/household-due"
}
```

The FD installs this reminder job under its existing local account. It reads only
`stream_id`, `generation`, `revision` from `v2_assistant_direct_binding` at the
configured `name`, using SQLite URI `mode=ro`, `query_only=ON` and a one-second
connection timeout. This is an untrusted discovery hint. The existing protected
seat token-file path is derived using `launch.py::stream_token_file_for`; an
owned `0600` regular non-symlink file is required.

Before every outbound attempt, the same candidate seat/token environment must
get `assistant.binding.ok` from the local CLI with exactly matching stream,
generation and revision, plus the configured composite. Any mismatch, missing
schema/row/file, remote host or stale/closed seat fails closed. Inherited seat
identities/tokens are cleared, and an empty `AGENT_ORCH_TOKEN` suppresses operator
credential loading. No operator authority, credential minting/copying, worker
borrowing or generic identity selection is used. This path is specific to this
FD-installed reminder job.

Set `operator_request_ref` to the existing operator dispatch or message that
authorized these reminders. The job refuses missing or malformed values before
publishing and sends that reference with `--note-type reminder`. After agreement,
the verified current seat can run `assistant publish --proactive
--publish-kind prose --response-state final` or `tell assistant`. The daemon also
checks the current binding at publication. If no valid current local seat can
authenticate, the job sends nothing and retains a durable unacknowledged BLOCKER
for retry on each timer wake; it never claims alert delivery before an
authenticated receipt. Schema changes do not trigger migrations, legacy/kv
fallbacks or weaker credentials. No credential bytes enter argv, state or logs.

## Installation and rehearsal

Render `services/chat-stream-v2/tools/household_due_reminders.plist.example` with
absolute interpreter/tool/config/log paths. Retain prior plist/config and hashes,
install the tool from accepted public source at an immutable path, and use
`launchctl bootstrap gui/<uid> <plist>`. `RunAtLoad` and `StartInterval=300` wake a
short process; the process applies Chicago time, DST, the activation date and the
08:00–11:59 delivery window. No daemon deployment is needed to run this tool with
the existing CLI/API protocol.

Start with `enabled: false`. Rehearse live reads and one real FD-only delivery:

```sh
python3 services/chat-stream-v2/tools/household_due_reminders.py \
  --config /private/config/reminders.json --once \
  --as-of 2030-01-01T08:00:00-06:00 --to-fd \
  --state-root /private/state/reminder-rehearsal
```

Use the intended first-delivery date, a separate private state directory and the
real live store. Repeating the same rehearsal state sends nothing a second time.
Injected time refuses operator publication; test mode refuses production state.
FD-only test alert IDs include the resolved test-state identity, so rehearsal
failures cannot conflict with or acknowledge a production BLOCKER. Production
alert IDs are unchanged.
Verify the FD receipt, selected source IDs and body hash in `delivery.json`.
Then enable production with the accepted `not_before` date and verify the loaded
job and installed source/config hashes. Verify the first actual publish receipt
and conversation history on that morning; rehearsal proves no live operator send.

## Delivery state and failure custody

An exclusive nonblocking file lock protects `delivery.json`. The pending body,
SHA256 and `household-due:YYYY-MM-DD` request key are atomically saved and fsynced
before publication. Retries use that exact body/key, including after an ambiguous
RPC result or FD handoff. Only `assistant.publish.ok` marks a production day
delivered. Existing daemon dedup also prevents duplicate pushes on replay.

A complete empty read closes the day as `empty`; later additions wait until the
next morning. Open overdue items repeat once on later days. Failed/malformed
reads never become empty success. Cross-read routine/item identity races get one
complete reread, then fail rather than invent an occurrence. Existing done/undo
occurrences suppress their routine projection; pausing hides a projection while
an existing open occurrence remains due.

Each failed run records a safe error class and durable BLOCKER with an evidence
path. An eligible digest attempts delivery before pending alert retries. If a
frozen BLOCKER conflicts after FD handoff, one existing `ledger get` read under
the current seat may recover its original committed receipt, requiring the same
ID/body and canonical logical-FD payload hash. Missing or mismatched evidence
keeps the alert unacknowledged; no new key or former-seat credential is used. The same timer retries unacknowledged alerts even outside the send window.
Noon closes an unconfirmed day as `missed`; restart detects missed days since
`not_before` and alerts without replaying old digests. A powered-off host alerts
on recovery. The FD retains the first-delivery fallback and must reconcile any
pending publication key/receipt before a manual send. Never erase dedup state.

Rollback boots out only this job, retains delivery state/receipts and restores
prior config/artifacts. Return reminder custody to the FD. Do not change Cosmo
routine dates as part of installation, rehearsal or rollback.

## Focused validation

```sh
python3 -m pytest services/chat-stream-v2/tests/test_household_due_reminders.py \
  services/chat-stream-v2/tests/test_household_due_reminders_binding.py \
  services/chat-stream-v2/tests/test_household_due_reminders_rpc.py \
  services/chat-stream-v2/tests/test_household.py
```

The real-daemon test exercises the exact wrapper and CLI with isolated credential
storage, a fixture primary seat and a bounded FD sink. It proves auth, replay,
rebind, single push and BLOCKER delivery without a model prompt. The normal v2
merge gate remains required before source promotion.
