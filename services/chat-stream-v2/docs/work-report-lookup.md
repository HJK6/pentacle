# Accepted report lookup

`agent-orch report get <report_id> --json` reads an ingested report independently
of its reporter's current session. `work.report.get` accepts only `report_id`
besides the RPC envelope and returns `report_id`, `status`, `from_stream_id`,
`provenance_qualified_spec_ids` and `created_at`. Missing rows return `not_found`;
transport or store failures return `unavailable`. Report prose and rejection
history stay private. A previously rejected attempt does not invalidate a
later accepted row. Consumers must check status and qualified spec themselves.

Operators and authenticated internal seats can use this read verb. An isolated
publisher uses the fixed `work-report-reader` service principal. Configure the
daemon's `PENTACLE_WORK_REPORT_READER_TOKEN_FILE` with its own private mode-0600
file owned by the daemon user. This uses the existing file-principal verifier:
the credential cannot be shared with another service, is rechecked on every
request, and removal or rotation revokes existing connections. Its only business
verb is `work.report.get`; its RPC hello cannot request a snapshot, and it
receives no broadcasts. It has no seat or operator authority.

The privileged publisher launcher provisions the same credential in root-owned
configuration and passes `PENTACLE_WORK_REPORT_READER_TOKEN` to its isolated
process with the daemon URL configured through `AGENT_ORCH_WS_URL`. Never pass
a seat token. When that environment variable is present, the CLI ignores all
inferred seat and operator credentials, including on authentication failure.
Keep credential values out of command arguments, output, reports and source.
Provisioning and runtime changes belong to the deployment owner.

The client makes one attempt with a maximum twenty-second deadline covering
connect, reply and cleanup. `--timeout` may shorten this deadline. Failure returns
a JSON `work.report.get.error` and exit 1. Success returns the metadata
with the usual type/request-id envelope and exit 0. Publisher activation
depends on installed lookup and a proven launcher-identity read journey.

## Advisor rule acceptance

The current front desk uses
`agent-orch tell --advisor-rule-message-id <operator-input-id> -- <advisor-stream> '<exact original text>'`.
Use the complete canonical operator input ID from the primary Bart composite
and preserve its UTF-8 text, including line endings and trailing spaces. The
input must originate from a registered operator WebSocket admission with the
server's origin stamp. The protected tell reserves its full ID and captures the
front desk binding revision and both seat generations before delivery. It
qualifies when the original advisor generation receives the exact wire and
the daemon confirms submission.

The retained advisor files a single inline `agent-orch report --status progress
--msg-id 0 --report-id <stable-id> --result '<JSON>'` with `extras.advisor_rule`.
Its object contains `version: 1`, `verdict: "accept"`, `operator_message_id`,
`fd_event_id` (the full tell ID), `path` fixed to
`docs/reference/assistant_bart_context.md`, `base_revision`,
`new_bytes_sha256`, and `scope`. Scope has `mode` and sorted `removed` and
`added` arrays of lowercase SHA256 hashes of complete changed rule lines
without the LF delimiter. Modes are `single`, `batch`, and `reorganize`.
The accepted report leaves the advisor seat open.

For this report, the dedicated service reader receives `session_generation`
and `advisor_rule_accept`. The attestation contains the reviewed object plus
`advisor_stream_id`, `advisor_generation`, `fd_stream_id`, `fd_generation`,
`fd_binding_revision`, and `operator_text_sha256`. The daemon persists those
fields with the report after rechecking the advisor binding and confirmed tell.
The original operator text, report prose, and unverified caller payload stay
outside the lookup response. An identical authenticated replay returns the
stored report. Its attestation survives seat changes, tell expiry, and daemon
restart.

A protected tell ID stays reserved after delivery retention removes its proof.
Repeating that forward returns `advisor_rule_event_unverified`. A changed
payload or an ordinary tell with the same ID returns `tell_id_conflict`.
A fresh protected tell can reference the original stamped input with a new ID.

For a publisher that cannot traverse the daemon user's home, install the
read-only consumer at `/usr/local/libexec/pentacle-work-report/agent-orch`.
The deployment owner runs the reviewed `install_fleet_spawn_tooling.py` with
`--commit <reader-release-SHA> --install-report-consumer <existing-release-dir>
--consumer-manifest-sha256 <reviewed-manifest-SHA256>` as root, using the
publisher's isolated Python. This separate mode performs no fleet or daemon
activation. Refresh it as the paired step of each subsequent CLI release.
It verifies the copied manifest/archive before importing, installs root-owned
read-only modules and bundled dependencies, probes them as the publisher, and
atomically selects `releases/<SHA>` through `active`; `previous` retains the
rollback target. The wrapper accepts only `report get`, requires its dedicated
credential and URL, and strips seat/operator variables before dispatch.

The fixed publisher launcher reads `reader.json` in that prefix before clearing
its environment. The file has exactly `token` and `ws_url`, owner root:wheel,
mode 0600, and exactly the named-user ACL `user:_triforce_memory allow read`.
Never grant primary-group access: the publisher's group may be shared. The
launcher supplies the credential, URL and an empty `AGENT_ORCH_TOKEN` after the
reset, preserving authenticated memory-principal attribution. Preserve launcher,
config and pointer preimages, and prove the actual publisher identity before
publisher activation; installation alone is not that journey.
