# Durable send-receipt design note

The send path should expose one durable receipt for each accepted request. The receipt is appended with the existing user event and can be read by a later client to reconcile an optimistic row.

Governance scope: subtractive/compatibility-only
Governance ruling: necessity=make delivery state observable; reused substrate=existing event append and pull query; authority=repository review

The durable receipt uses the existing daemon and adds no background actor, token, or private endpoint. A receipt contains a request id, optimistic id, bounded status, and server timestamp. Duplicate request ids resolve to the existing receipt. An explicit retry of a known failure creates a new request id. Retry on an indeterminate row reconciles the original request id at any age, including after a daemon restart. Its row shows Delivery uncertain with a Retry action; concurrent clicks cannot create another in-flight dispatch. Reconciliation errors keep that identity and uncertainty until authoritative receipt evidence settles the row. Tests use a temporary database and synthetic text.

## Caption resolution for a committed-but-unconfirmed receipt (issue #5)

A send whose paste the daemon committed but whose landed proof it could not confirm (a busy agent queues the input) records a committed-pending receipt — state `not_landed`/`accepted`, delivery `not_landed`/`committed_pending_proof`/`proof_pending` — not a failure. There is no path that upgrades such a receipt to `landed` once the durable USER echo arrives. So the caption rule, applied only to an already-correlated direct-ID echo (client-origin row, `receiptDirectMatch`, and a strictly-numeric finite `correlatedDaemonSeq`), reads those committed states as **Sent**: the echo is the terminal's own confirmation of receipt. `landed` is Sent and `proof_unavailable` is Failed as before; a blank, whitespace, or unrecognized receipt is an unresolved partial and stays Sending. This captions an already-bound row per the daemon's committed assertion; it does not change which echo correlates. Per-attempt/session correlation identity is a separate concern (retry reuses the optimistic id; the process-local id counter resets on reload), tracked outside this note.

## Committed results for installed clients

Committed-pending `send.result` frames also expose their durable `state:
accepted`, including coalesced replies. This supports installed clients that
recognize accepted state but do not recognize the `committed_pending_proof`
delivery value. The canonical delivery, `submission_confirmed: false`, and
do-not-resubmit guidance remain intact. Accepted state keeps the send
indeterminate and reconcilable; only later positive evidence permits a landed
acknowledgement. This adds no dispatch or receipt mutation.

## Provider native queue (`provider_queued`)

Claude Code and Codex accept input typed during a running turn into their own queue (Claude: the prompt sits above the spinner with `ctrl+x ctrl+s to send now`; Codex: under `Messages to be submitted after next tool call`) and submit it only when the turn yields. The daemon treats that as delivered — `state`/`delivery` stay `landed`, `submission_confirmed: true` — and adds `provider_queued: true` to `send.result` when a post-delivery capture shows the prompt in that queue; the receipt records `reason: "provider_queued"`. A coalesced replay of such a send keeps `provider_queued: true` and records `reason: "coalesced_replay:<winner>;provider_queued"`. The capture is informational: it never changes the delivery classification, and a missed capture degrades to the ordinary landed path.

Client captions for the latest uncorrelated client-origin row: `provider_queued` → **Queued**; `landed` without it (optimistic status `acked`) → **Sent** at once, without waiting for the transcript echo; otherwise **Sending**. The correlated USER echo then applies the rules above. `provider_queued` is distinct from `committed_pending_proof` (submission proof not yet surfaced) and from the client-side hold (`turn_queued`, which alone drives queued-send flushing); it does not change `PentacleSendState`.

## Pre-admission refusal recovery

The web/desktop main client retries a send once after 250 ms only for the
current pending request's exact `send.error`, `error_code: server_overloaded`,
`retryable: true` refusal. This daemon frame is emitted before admission to the
handler. The retry preserves the serialized payload, request/logical IDs and
send bypass priority. A second refusal surfaces normally. Receipt-bearing,
accepted, committed, queued or do-not-resubmit evidence is ineligible, as are
provider limits, timeouts and generic busy errors. Disconnect, replacement,
result or shutdown cancels the backoff; no uncertain send is retried by a timer.
Delivery captions describe delivery only: Sent or Queued is not model completion.

The counted browser/restart acceptance uses a fresh output directory, the
provisioned daemon Python, Chrome and tmux:

```bash
node test/e2e/send_refusal_gate.cjs OUTPUT_DIRECTORY /path/to/provisioned/python
```

It owns loopback services, a private tmux namespace and a native-format counted
provider counterpart. It causes a real pre-handler admission refusal, loses the
accepted reply at a transport proxy, waits over 60 real seconds, restarts only
the scratch daemon on the same database, and clicks the real row's Retry. The
external submission counter spans both processes. JSON/DOM/PNG receipts and
source/asset hashes stay in the output directory; plaintext operator token and
owned runtime resources are removed. `--red` records the expected unpatched
refusal failure and is for a baseline checkout, not candidate acceptance.

### Web-host connection recovery

When an unresolved send loses its browser-to-web-host connection, the local transport marks that exact failure with its connection epoch. The row stays **Delivery uncertain**. After the current authenticated connection has merged a connected daemon snapshot, the renderer reconciles the original request once for that web epoch. It preserves request/input IDs, text, attachments, reply/voice metadata and an explicit Send urgently flag. Existing receipts or matching USER history settle the same row. Missing receipt evidence never creates a new identity.

This path is separate from daemon reconnect generations and from the bounded pre-admission overload retry. Repeated readiness callbacks coalesce; manual Retry joins an in-flight reconciliation; late receipts win. No generic web RPC is replayed. Automatic recovery uses the existing30-minute reconnect bound; an older unresolved row keeps explicit original-ID Retry. Queued records provider admission, not assistant completion.

Real acceptance: `node test/e2e/web_host_recovery_gate.cjs <fresh-output> <provisioned-python>` with `PHASE2_KIND=ordinary`, `composite` or `urgent`. Each counted run loses a real ACK, waits over60seconds, restarts the isolated web host and daemon on persisted stores and checks one original provider submission/USER row under the same IDs. Urgent additionally checks one initial Escape and no second Escape on reconciliation. `--manual-overlap` clicks the actual Retry while automatic recovery is in flight; `--before-admission` drops the first browser request before it reaches the host. `--quick` is development-only and does not satisfy the real elapsed-time acceptance. The provider is a controlled native-format counterpart; its counter survives both process restarts. No production store or provider is used.
