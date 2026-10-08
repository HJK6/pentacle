# Front desk wake reduction

Only the primary assistant's bound direct-primary front desk uses this policy (the unprefixed `PENTACLE_ASSISTANT_*` composite, whatever you have titled it). Other parents and
router-backed assistant seats keep their existing delivery behavior.

Operator input and question answers, GATE and BLOCKER tells, child reports,
rejected or revised lane rulings, failed spawn releases, routed external email,
and the desk's timed wakes arrive immediately. So does one notice when the bound
lane-ruling authority seat is lost (closed, pane dead, or no such seat): it is
sent once per continuous loss of that seat generation, also with no request
pending, and again only after the authority was seen ready or was rebound. A
seat whose state is merely unknown, as right after a daemon start, is not
reported. The daemon does not rebind the authority; requests keep being
released without a ruling until you rebind or replace it. The chat line and the
deadline notice for a request that went unruled state what the release did:
`admitted unruled`, or `not released, blocked (<code>)` when nothing launched. Successful unchanged-scope spawn
approvals produce no front-desk tell. Tree-idle and child context-advisory notices
are dropped; their facts remain available through `agent-orch list` and `inspect`.
The daemon's own [external-work reminder](../services/chat-stream-v2/docs/external-work.md)
also arrives immediately; a wire client cannot mint one.

Other inputs, including START/END, receipts, concurs and watched-child inactivity,
are held in the existing durable outbound-notice store. The oldest held item's
creation time sets the deadline: `PENTACLE_FRONT_DESK_DIGEST_S` defaults to 3600
seconds. Later arrivals never reset it. At the deadline, all held items become
one `lane_digest` notice through the existing outbound queue. No empty digest is
sent. Wake inputs arrive alone and do not flush held items. The queue's normal
five-second sweep bounds scheduling precision; its existing transport and proof
recovery govern the resulting notice. Held inputs survive daemon restarts.

`PENTACLE_FRONT_DESK_DIGEST_ENABLED` defaults to on. Set it to `0`, `false`,
`no`, or `off` in the daemon environment to disable ingress holding and dropping:
every new input then passes through. Existing held rows are folded into one
digest on the next outbound sweep, without waiting for the deadline. With the
switch on, a restart preserves the oldest-item deadline. Pending rows for the
same target from an earlier binding generation are included in the digest under
the current generation, so they cannot be stranded by the old generation filter.
The resulting digest uses the normal queue's delivery and recovery rules.

Canonical direct operator dispatches bypass admission using the private marker
introduced by `Comms.send_assistant_backend`, before any hold/drop decision.
The bypass applies only to marked sends; forwarded composite peer tells also
carry the marker but remain subject to admission. Wire clients cannot supply
that marker; copying the daemon dispatch header in a peer tell, addressed to
either the desk or its composite, does not make it operator input. GATE/BLOCKER classification applies
to the tell's leading label (after the notice prefix), not references to another
seat's GATE buried in a progress update.

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
