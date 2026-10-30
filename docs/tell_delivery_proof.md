# Tell delivery proof

A tell's ID binds its resolved target and original payload. A same-ID retry
never pastes or presses Enter again. Reusing an ID with a different payload or
resolved target is a conflict.

Ordinary tells retain their original pre-input event watermark, lifecycle
generation, session birth and exact wire text. If native USER evidence arrives
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
