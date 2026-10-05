# Durable send-receipt design note

The send path should expose one durable receipt for each accepted request. The receipt is appended with the existing user event and can be read by a later client to reconcile an optimistic row.

Governance scope: subtractive/compatibility-only
Governance ruling: necessity=make delivery state observable; reused substrate=existing event append and pull query; authority=repository review

The design adds no background actor, retry loop, token, or private endpoint. A receipt contains a request id, optimistic id, bounded status, and server timestamp. Duplicate request ids resolve to the existing receipt; an explicit retry creates a new request id. Tests use a temporary database and synthetic text.

## Caption resolution for a committed-but-unconfirmed receipt (issue #5)

A send whose paste the daemon committed but whose landed proof it could not confirm (a busy agent queues the input) records a committed-pending receipt — state `not_landed`/`accepted`, delivery `not_landed`/`committed_pending_proof`/`proof_pending` — not a failure. There is no path that upgrades such a receipt to `landed` once the durable USER echo arrives. So the caption rule, applied only to an already-correlated direct-ID echo (client-origin row, `receiptDirectMatch`, and a strictly-numeric finite `correlatedDaemonSeq`), reads those committed states as **Sent**: the echo is the terminal's own confirmation of receipt. `landed` is Sent and `proof_unavailable` is Failed as before; a blank, whitespace, or unrecognized receipt is an unresolved partial and stays Sending. This captions an already-bound row per the daemon's committed assertion; it does not change which echo correlates. Per-attempt/session correlation identity is a separate concern (retry reuses the optimistic id; the process-local id counter resets on reload), tracked outside this note.

## Provider native queue (`provider_queued`)

Claude Code and Codex accept input typed during a running turn into their own queue (Claude: the prompt sits above the spinner with `ctrl+x ctrl+s to send now`; Codex: under `Messages to be submitted after next tool call`) and submit it only when the turn yields. The daemon treats that as delivered — `state`/`delivery` stay `landed`, `submission_confirmed: true` — and adds `provider_queued: true` to `send.result` when a post-delivery capture shows the prompt in that queue; the receipt records `reason: "provider_queued"`. A coalesced replay of such a send keeps `provider_queued: true` and records `reason: "coalesced_replay:<winner>;provider_queued"`. The capture is informational: it never changes the delivery classification, and a missed capture degrades to the ordinary landed path.

Client captions for the latest uncorrelated client-origin row: `provider_queued` → **Queued**; `landed` without it (optimistic status `acked`) → **Sent** at once, without waiting for the transcript echo; otherwise **Sending**. The correlated USER echo then applies the rules above. `provider_queued` is distinct from `committed_pending_proof` (submission proof not yet surfaced) and from the client-side hold (`turn_queued`, which alone drives queued-send flushing); it does not change `PentacleSendState`.
