# Front desk wake reduction

Only Bart's bound direct-primary front desk uses this policy. Other parents and
router-backed assistant seats keep their existing delivery behavior.

Operator input and question answers, GATE and BLOCKER tells, child reports,
rejected or revised lane rulings, failed spawn releases, routed external email,
and the desk's timed wakes arrive immediately. Successful unchanged-scope spawn
approvals produce no front-desk tell. Tree-idle and child context-advisory notices
are dropped; their facts remain available through `agent-orch list` and `inspect`.

Other inputs, including START/END, receipts, concurs and watched-child inactivity,
are held in the existing durable outbound-notice store. The oldest held item's
creation time sets the deadline: `PENTACLE_FRONT_DESK_DIGEST_S` defaults to 3600
seconds. Later arrivals never reset it. At the deadline, all held items become
one `lane_digest` notice through the existing outbound queue. No empty digest is
sent. Wake inputs arrive alone and do not flush held items. The queue's normal
five-second sweep bounds scheduling precision; its existing transport and proof
recovery govern the resulting notice. Held inputs survive daemon restarts.

The front-desk Claude advisory/compact thresholds are 150K/200K tokens, configured
with `PENTACLE_CONTEXT_ASSISTANT_ADVISORY_ABS` and
`PENTACLE_CONTEXT_ASSISTANT_COMPACT_ABS`. Other Claude seats retain 400K/500K.
An unproven compact input can retry after `PENTACLE_CONTEXT_COMPACT_COOLDOWN_S`
when the seat remains over threshold, idle and has a proven-empty composer.
Late proof prevents a repaste; a real draft still defers. A successful compact
whose proof was missed can be repeated after the cooldown on an old high reading.

The accepted spec in shared memory,
`work/in_progress/pentacle__front_desk_wake_reduction_2026_10/spec.md`, owns rollout
and live acceptance. Dot's email and pace contract lives in
`docs/reference/dot_work_delivery.md` there. Runtime activation and the one-time
Dot message remain with the accepting parent.
