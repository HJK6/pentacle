# Tell delivery proof

A tell's ID binds its resolved target and original payload. A same-ID retry
never pastes or presses Enter again. Reusing an ID with a different payload or
resolved target is a conflict.

Ordinary tells retain their original pre-input event watermark, lifecycle
generation, session birth and exact wire text. After input admission and before
the first paste, the existing tell table durably records `proof_unavailable`
custody, with zero confirmed attempts. This is an unknown outcome, not a claim
that input happened. The result can replace that prepared row only through an
exact payload-and-anchor comparison in the existing promotion transaction.

Send and tell capture their pre-input watermark with a current-session durable
row-ID lookup, without decoding or projecting history. Proof polls select only
rows above that watermark, newest 500 raw rows first, then decode, project and
return them oldest-first. This preserves the former projected-tail result after
watermark filtering, including undecodable rows consuming the raw-row limit.
An undecodable newest row can raise the numeric watermark without changing
proof eligibility: subsequent events receive larger IDs. Both reads retain the
current `sessions.created_at` fence; proof rules and deadlines are unchanged.

A caller cancellation, proof-reader failure, or daemon restart between
persistence and paste cannot authorize a second paste. Definite input-admission
refusals occur before this receipt is prepared. Pending custody is already
visible to first-pin readiness while input or proof is in flight; the global
pin decision lock is never held across delivery or evidence waiting.

If native USER evidence arrives
after the initial confirmation window, a same-ID retry can reconcile the
original receipt. The evidence lookup and promotion run in one Store
transaction. They require an exact post-watermark USER or authenticated TELL
event from the original stream and lifecycle. Retained events older than the
live tail remain eligible.

Another overlapping identical bare input, multiple matching events, missing
original anchors, a changed lifecycle, or unavailable evidence cannot prove
delivery. The receipt stays pending and input is not repeated. Existing legacy
rows are never given fabricated pre-input anchors. A later unrelated reply,
pane text or elapsed time cannot acknowledge them.

This reconciliation uses the existing tell request and promotion boundary. It
does not add an acknowledgement verb, a background input sender, or a mutation
to ledger reads. Custody and lifecycle disposition of a holder whose original
proof cannot be recovered require their own authorization.

The focused regressions are `test_tell_late_proof_ack_diagnostic.py` and
`test_tell_late_proof_controls.py` under `services/chat-stream-v2/tests`.
